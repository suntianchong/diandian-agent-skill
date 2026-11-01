"""点点自有账号体系（AUTH-SYS-02）：谷歌 OAuth 登录 + 邮箱验证码登录。

边界（与既有约定一致，见 docs/engineering/ACCOUNT_SYSTEM_TODO.md）：
- 自有账号是独立身份层，签发 ``ddacc_`` 前缀的不透明会话令牌；
  **不能把自有令牌当底座（Vikunja）JWT 使用**，底座身份仍由 ``Service.human`` 验证。
- 自有账号通过 ``vikunjaUserId`` 字段与底座用户建立映射（显式关联，单一指向）。
- 所有敏感值（验证码、令牌、exchange 码）只存 SHA-256 摘要，原文只出现一次。
- 谷歌凭证与 SMTP 凭证来自环境变量，缺配置时接口返回明确的 503 错误码，
  不做任何本地降级或假登录。
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from .service import fail
from .webhook import SUPPRESSION, apply_event

SESSION_TTL = 30 * 86400
CODE_TTL = 600
EXCHANGE_TTL = 300
CODE_ATTEMPTS = 5
SEND_INTERVAL = 60
SEND_HOURLY = 10
IP_SEND_HOURLY = 30
PURGE_INTERVAL = 3600
CODE_RETENTION = 7 * 86400
STATE_TTL = 600
TOKEN_PREFIX = "ddacc_"

RESEND_API_URL = "https://api.resend.com/emails"
# Resend 前置 Cloudflare 会拦截无 UA 的请求（python-httpx 默认 UA 命中 1010），
# 发信必须声明可识别的客户端标识
MAIL_USER_AGENT = "diandian-agent/0.2 (+https://puppytodo.ai)"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value):
    email = (value or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise ValueError("email_invalid")
    return email


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def client_ip(request):
    """取真实来源 IP：反代已写入 X-Real-IP / X-Forwarded-For 时优先采信。"""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def identity_config_from_env():
    env = os.environ.get
    return {
        "google_client_id": env("DIANDIAN_GOOGLE_CLIENT_ID", ""),
        "google_client_secret": env("DIANDIAN_GOOGLE_CLIENT_SECRET", ""),
        "google_redirect_uri": env("DIANDIAN_GOOGLE_REDIRECT_URI", ""),
        "smtp_host": env("DIANDIAN_SMTP_HOST", ""),
        "smtp_port": env("DIANDIAN_SMTP_PORT", "465"),
        "smtp_user": env("DIANDIAN_SMTP_USER", ""),
        "smtp_password": env("DIANDIAN_SMTP_PASSWORD", ""),
        "smtp_from": env("DIANDIAN_SMTP_FROM", ""),
        "resend_api_key": env("DIANDIAN_RESEND_API_KEY", ""),
        "resend_webhook_secret": env("DIANDIAN_RESEND_WEBHOOK_SECRET", ""),
        "return_base": env("DIANDIAN_AUTH_RETURN_BASE", ""),
    }


def mail_ready(config):
    """发信是否可用：有 Resend API Key，或 SMTP 主机与发件人齐全。"""
    return bool(config.get("smtp_from")) and bool(
        config.get("resend_api_key") or config.get("smtp_host"))


CODE_BODY = ("你的点点登录验证码是 {code}，{minutes} 分钟内有效。"
             "若非本人操作，请忽略本邮件。")

CODE_HTML = """\
<div style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#1f1f1f">
  <p style="font-size:15px;margin:0 0 4px">点点登录验证码</p>
  <p style="font-size:32px;font-weight:600;letter-spacing:6px;margin:0 0 20px;color:#111">{code}</p>
  <p style="font-size:14px;line-height:1.6;color:#666;margin:0 0 12px">{minutes} 分钟内有效，请勿转发给他人。</p>
  <p style="font-size:13px;line-height:1.6;color:#999;margin:0">若非本人操作，请忽略本邮件。</p>
</div>"""


def sender_address(config):
    """把配置里的发件人解析成合法头部。

    支持 ``名称 <地址>`` 与纯地址两种写法；中文名称交给 ``formataddr`` 做
    RFC 2047 编码，避免直接拼接中文导致发信时编码异常。
    """
    raw = (config.get("smtp_from") or "").strip()
    name, address = parseaddr(raw)
    if "@" not in address:
        raise ValueError("smtp_from_invalid")
    return formataddr((name or "点点", address))


def send_mail(config, to, code):
    """发送验证码邮件：有 Resend API Key 时走 HTTPS API，否则走 SMTP。

    HTTPS 出口只依赖 443，能绕开不少主机对 465/587 的出网限制；SMTP 分支保持
    通用，可接任何标准邮件服务。
    """
    if config.get("resend_api_key"):
        return send_via_resend(config, to, code)
    return send_via_smtp(config, to, code)


def send_via_resend(config, to, code):
    minutes = CODE_TTL // 60
    reply = httpx.post(
        config.get("resend_api_url") or RESEND_API_URL,
        headers={"Authorization": f"Bearer {config['resend_api_key']}",
                 "Content-Type": "application/json",
                 # Resend 前面挂着 Cloudflare：不带 UA 的请求（httpx 默认 python-httpx/...）
                 # 会被当作 bot 拦成 403 / error code 1010，必须显式声明客户端标识
                 "User-Agent": MAIL_USER_AGENT},
        json={"from": sender_address(config), "to": [to], "subject": "点点登录验证码",
              "text": CODE_BODY.format(code=code, minutes=minutes),
              "html": CODE_HTML.format(code=code, minutes=minutes)},
        timeout=15)
    if reply.status_code >= 400:
        raise RuntimeError(f"resend_api_{reply.status_code}: {reply.text[:200]}")
    return reply.json()


def send_via_smtp(config, to, code):
    """通过 SMTP 发送验证码邮件。端口 465 走 SSL，其余端口先 STARTTLS。

    默认参数可直接指向 Resend：``smtp.resend.com`` / 465 / 用户 ``resend`` /
    密码为 API Key。也可用于任何标准 SMTP 服务。
    """
    message = EmailMessage()
    message["From"] = sender_address(config)
    message["To"] = to
    message["Subject"] = "点点登录验证码"
    # Gmail 会按该头部聚合会话，每条验证码独立成信更利于用户查找
    message["X-Entity-Ref-ID"] = secrets.token_hex(16)
    minutes = CODE_TTL // 60
    message.set_content(CODE_BODY.format(code=code, minutes=minutes))
    message.add_alternative(CODE_HTML.format(code=code, minutes=minutes), subtype="html")
    host = config["smtp_host"]
    port = int(config.get("smtp_port") or 465)
    user, password = config.get("smtp_user"), config.get("smtp_password")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)


class AccountAuth:
    """自有账号存储与认证逻辑。数据复用 Store 的 records 表（新 category）。"""

    def __init__(self, store, clock=time.time, config=None, sender=None, http=None):
        self.store = store
        self.clock = clock
        self.config = config if config is not None else identity_config_from_env()
        self._sender = sender or send_mail
        self._http = http
        self._last_purge = 0.0

    # -- 会话 ---------------------------------------------------------------

    def _issue_session(self, account_id):
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.store.put("auth_session", digest(token), {
            "accountId": account_id, "created": self.clock(),
            "expiresAt": self.clock() + SESSION_TTL})
        return token

    def session(self, token):
        record = self.store.get("auth_session", digest(token))
        if not record or record["expiresAt"] <= self.clock():
            fail(401, "identity_session_invalid")
        account = self.store.get("account", record["accountId"])
        if not account:
            fail(401, "identity_session_invalid")
        # 管理后台禁用后立即失效（ADMIN-CONSOLE-01）
        if account.get("status") == "disabled":
            fail(403, "account_disabled")
        return self.safe_account(account)

    def signout(self, token):
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM records WHERE category='auth_session' AND id=?",
                (digest(token),))
        return {"signedOut": True}

    # -- 账号 ---------------------------------------------------------------

    def _find_account(self, email):
        for account in self.store.all("account"):
            if account["email"] == email:
                return account
        return None

    def _upsert_account(self, email, name=None, avatar=None):
        now = self.clock()
        account = self._find_account(email)
        if account:
            account["lastLogin"] = now
            if name:
                account["name"] = name
            if avatar:
                account["avatar"] = avatar
        else:
            account = {"id": secrets.token_hex(16), "email": email,
                       "provider": None, "name": name or "", "avatar": avatar or "",
                       "vikunjaUserId": None, "created": now, "lastLogin": now,
                       # 管理后台（ADMIN-CONSOLE-01）：首个账号是超管，其余为普通用户
                       "role": "superadmin" if not self.store.all("account") else "user",
                       "status": "active"}
        account.setdefault("role", "user")
        account.setdefault("status", "active")
        self.store.put("account", account["id"], account)
        return account

    @staticmethod
    def safe_account(account):
        return {k: account.get(k) for k in
                ("id", "email", "provider", "name", "avatar", "vikunjaUserId", "created")}

    # -- 邮箱验证码 ----------------------------------------------------------

    def send_email_code(self, email, client_ip=None):
        if not mail_ready(self.config):
            fail(503, "email_send_not_configured")
        # 硬退信/投诉过的地址不再发信：继续发只会持续拉低域名信誉
        if self.store.get(SUPPRESSION, email):
            fail(400, "email_undeliverable")
        now = self.clock()
        self._purge(now)
        record = self.store.get("auth_code", email) or {}
        if record and now - record.get("lastSent", 0) < SEND_INTERVAL:
            self._too_many(SEND_INTERVAL - (now - record["lastSent"]))
        recent = [t for t in record.get("sentTimes", []) if t > now - 3600]
        if len(recent) >= SEND_HOURLY:
            self._too_many(recent[0] + 3600 - now if recent else SEND_INTERVAL)
        if client_ip:
            self._check_ip(client_ip, now)
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        previous = record or None
        self.store.put("auth_code", email, {
            "codeHash": digest(code), "expiresAt": now + CODE_TTL, "attempts": 0,
            "lastSent": now, "sentTimes": recent + [now]})
        try:
            self._sender(self.config, email, code)
        except Exception:
            # 没发出去的码不该占掉 60 秒重发窗口：恢复上一条记录后原样报错
            with self.store.transaction():
                if previous:
                    self.store.put("auth_code", email, previous)
                else:
                    self.store.db.execute(
                        "DELETE FROM records WHERE category='auth_code' AND id=?", (email,))
            fail(502, "email_send_failed")
        return {"sent": True, "expiresIn": CODE_TTL, "resendAfter": SEND_INTERVAL}

    @staticmethod
    def _too_many(retry_after):
        raise HTTPException(429, detail={
            "code": "code_send_rate_limited",
            "retryAfter": max(1, int(retry_after))})

    def _check_ip(self, client_ip, now):
        """同一来源 IP 的滑动窗口限流，防止换邮箱刷走发信额度。"""
        key = digest(client_ip)
        record = self.store.get("auth_send_ip", key) or {}
        times = [t for t in record.get("times", []) if t > now - 3600]
        if len(times) >= IP_SEND_HOURLY:
            self._too_many(times[0] + 3600 - now)
        times.append(now)
        self.store.put("auth_send_ip", key, {"times": times})

    def _purge(self, now):
        """按小时节流清理过期验证码与会话，避免 records 表无限增长。"""
        if now - self._last_purge < PURGE_INTERVAL:
            return
        self._last_purge = now
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM records WHERE category='auth_code' "
                "AND json_extract(value,'$.expiresAt') < ?", (now - CODE_RETENTION,))
            self.store.db.execute(
                "DELETE FROM records WHERE category='auth_send_ip' "
                "AND json_extract(value,'$.times[0]') < ?", (now - 86400,))

    def ingest_mail_event(self, payload):
        """处理 Resend webhook：硬退信与投诉进抑制名单，投递成功则解除。"""
        return apply_event(self.store, payload, self.clock())

    def verify_email_code(self, email, code):
        now = self.clock()
        record = self.store.get("auth_code", email)
        if not record:
            fail(400, "code_invalid")
        if now > record["expiresAt"]:
            fail(410, "code_expired")
        if record["attempts"] >= CODE_ATTEMPTS:
            fail(429, "code_attempts_exceeded")
        if not hmac.compare_digest(digest(code), record["codeHash"]):
            with self.store.transaction():
                record["attempts"] += 1
                self.store.put("auth_code", email, record)
            fail(400, "code_invalid")
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM records WHERE category='auth_code' AND id=?", (email,))
            account = self._upsert_account(email)
            account["provider"] = account["provider"] or "email"
            self.store.put("account", account["id"], account)
        if account.get("status") == "disabled":
            fail(403, "account_disabled")
        return {"token": self._issue_session(account["id"]),
                "account": self.safe_account(account)}

    # -- 谷歌 OAuth ----------------------------------------------------------

    def google_start_url(self):
        client_id = self.config.get("google_client_id")
        redirect_uri = self.config.get("google_redirect_uri")
        if not client_id or not self.config.get("google_client_secret") or not redirect_uri:
            fail(503, "google_not_configured")
        state = secrets.token_urlsafe(32)
        self.store.put("oauth_state", digest(state),
                       {"expiresAt": self.clock() + STATE_TTL})
        query = urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": "openid email profile",
            "state": state, "prompt": "select_account"})
        return f"{GOOGLE_AUTH_URL}?{query}"

    def _redirect(self, base, params):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"{base}/#account?{urlencode(params)}", status_code=302)

    def google_callback(self, code, state, return_base):
        base = return_base or "/"
        record = self.store.get("oauth_state", digest(state)) if state else None
        if not record or record["expiresAt"] <= self.clock():
            return self._redirect(base, {"authError": "state_invalid"})
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM records WHERE category='oauth_state' AND id=?",
                (digest(state),))
        if not code:
            return self._redirect(base, {"authError": "google_cancelled"})
        try:
            token_reply = self._google_http("post", GOOGLE_TOKEN_URL, data={
                "code": code, "client_id": self.config["google_client_id"],
                "client_secret": self.config["google_client_secret"],
                "redirect_uri": self.config["google_redirect_uri"],
                "grant_type": "authorization_code"}).json()
            access_token = token_reply.get("access_token")
            if not access_token:
                return self._redirect(base, {"authError": "google_exchange_failed"})
            profile = self._google_http(
                "get", GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"}).json()
        except Exception:
            return self._redirect(base, {"authError": "google_exchange_failed"})
        email = (profile.get("email") or "").strip().lower()
        if not email or not profile.get("email_verified", True):
            return self._redirect(base, {"authError": "google_email_unavailable"})
        with self.store.transaction():
            account = self._upsert_account(email, profile.get("name"), profile.get("picture"))
            account["provider"] = "google"
            self.store.put("account", account["id"], account)
            if account.get("status") == "disabled":
                return self._redirect(base, {"authError": "account_disabled"})
            exchange = secrets.token_urlsafe(32)
            self.store.put("login_exchange", digest(exchange), {
                "accountId": account["id"], "expiresAt": self.clock() + EXCHANGE_TTL})
        return self._redirect(base, {"exchange": exchange})

    def _google_http(self, method, url, **kwargs):
        if self._http is not None:
            return getattr(self._http, method)(url, **kwargs)
        with httpx.Client(timeout=15) as client:
            return getattr(client, method)(url, **kwargs)

    def exchange(self, code):
        now = self.clock()
        with self.store.transaction():
            record = self.store.get("login_exchange", digest(code))
            if not record or now > record["expiresAt"]:
                fail(410, "exchange_code_invalid")
            self.store.db.execute(
                "DELETE FROM records WHERE category='login_exchange' AND id=?",
                (digest(code),))
            account = self.store.get("account", record["accountId"])
            if not account:
                fail(410, "exchange_code_invalid")
        return {"token": self._issue_session(account["id"]),
                "account": self.safe_account(account)}

    # -- 底座映射 ------------------------------------------------------------

    def link_vikunja(self, account_id, vikunja_token, human_validator):
        """将自有账号与底座（Vikunja）用户建立映射。映射关系单一指向。"""
        account = self.store.get("account", account_id)
        if not account:
            fail(401, "identity_session_invalid")
        user = human_validator(vikunja_token)
        for other in self.store.all("account"):
            if other["id"] != account_id and other.get("vikunjaUserId") == user["id"]:
                fail(409, "vikunja_already_linked")
        account["vikunjaUserId"] = user["id"]
        self.store.put("account", account["id"], account)
        return {"account": self.safe_account(account),
                "vikunjaUser": {"id": user["id"], "username": user.get("username", "")}}

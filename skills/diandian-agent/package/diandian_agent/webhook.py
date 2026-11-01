"""Resend webhook 接收入口：Svix 签名校验 + 退信/投诉地址隔离。

边界：
- 只信任带合法 Svix 签名的请求；未配置密钥时直接 503，不做「先收下再说」的降级。
- 签名合法不等于业务成功：处理逻辑必须幂等（重复投递只覆盖同一条记录）。
- 软退信（Transient）不拉黑，只记录；硬退信与投诉才进入抑制名单。
"""

import base64
import hashlib
import hmac
import json
import time

SIGNATURE_TOLERANCE = 300          # 秒；防止旧请求被重放
SUPPRESSION = "email_suppression"

BOUNCE_SUPPRESS_TYPES = {"Permanent", "Undetermined"}
DELIVERED_EVENTS = {"email.delivered"}


def _secret_bytes(secret):
    """``whsec_`` 前缀后的内容是 base64 编码的 HMAC 密钥。"""
    raw = (secret or "").strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_"):]
    padding = -len(raw) % 4
    return base64.b64decode(raw + "=" * padding)


def expected_signature(secret, msg_id, timestamp, body):
    """按 Svix 规则计算签名：base64(HMAC256(key, "id.timestamp.body"))。"""
    signed = b".".join([msg_id.encode(), str(timestamp).encode(), body])
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_signature(secret, msg_id, timestamp, signature_header, body, now=None,
                     tolerance=SIGNATURE_TOLERANCE):
    """校验 svix-signature 头。头里可能是多个空格分隔的 ``v1,<sig>``。"""
    if not secret or not msg_id or not timestamp or not signature_header:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance:
        return False
    expected = expected_signature(secret, msg_id, timestamp, body)
    for candidate in signature_header.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def _recipients(payload):
    data = payload.get("data") or {}
    to = data.get("to") or []
    if isinstance(to, str):
        to = [to]
    return [str(address).strip().lower() for address in to if "@" in str(address)]


def apply_event(store, payload, now=None):
    """把 Resend 事件落到抑制名单，返回处理结果摘要。"""
    kind = (payload or {}).get("type") or ""
    stamp = int(now if now is not None else time.time())
    recipients = _recipients(payload or {})
    data = (payload or {}).get("data") or {}

    if kind in DELIVERED_EVENTS:
        with store.transaction():
            for address in recipients:
                store.db.execute(
                    "DELETE FROM records WHERE category=? AND id=?", (SUPPRESSION, address))
        return {"handled": kind, "released": len(recipients)}

    if kind == "email.bounced":
        bounce = data.get("bounce") or {}
        bounce_type = bounce.get("type") or "Undetermined"
        if bounce_type not in BOUNCE_SUPPRESS_TYPES:
            return {"handled": kind, "suppressed": 0, "bounceType": bounce_type}
        with store.transaction():
            for address in recipients:
                store.put(SUPPRESSION, address, {
                    "reason": "bounced", "bounceType": bounce_type,
                    "message": (bounce.get("message") or "")[:300], "at": stamp})
        return {"handled": kind, "suppressed": len(recipients), "bounceType": bounce_type}

    if kind == "email.complained":
        with store.transaction():
            for address in recipients:
                store.put(SUPPRESSION, address, {
                    "reason": "complained", "at": stamp})
        return {"handled": kind, "suppressed": len(recipients)}

    return {"handled": kind, "ignored": True}


def parse_body(body):
    """webhook 载荷必须是 JSON 对象，否则视为无效请求。"""
    payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    return payload

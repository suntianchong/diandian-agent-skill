import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from fastapi import HTTPException

from .models import Metadata
from .collaboration import Collaboration, public_person
from .upstream import UpstreamFailure, public_failure


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_request(request):
    # New optional defaults must not invalidate pre-upgrade action fingerprints.
    return {k: v for k, v in request.items() if not (
        k in ("ifVersion", "labelIds") and v is None or k == "dryRun" and v is False)}


def agent_action_request(req):
    request = req.model_dump(mode="json")
    if req.meta:
        request["meta"] = req.meta.model_dump(mode="json", exclude_unset=True)
    return canonical_request(request)


def fail(code, message):
    raise HTTPException(code, detail={"code": message})


class Service(Collaboration):
    def __init__(self, store, upstream, key, clock=time.time):
        self.store, self.upstream, self.clock = store, upstream, clock
        self.crypto = Fernet(key)

    def human(self, token):
        if token.startswith("dd_"):
            fail(401, "human_jwt_required")
        try:
            user = self.upstream.call(token, "GET", "/user")
        except UpstreamFailure as exc:
            raise public_failure(exc) from None
        if not user.get("id") or user.get("bot_owner_id"):
            fail(403, "human_jwt_required")
        return user

    def project(self, token, project, write=False, admin=False, allow_archive=False):
        obj = self.upstream.call(token, "GET", f"/projects/{project}")
        if obj.get("max_permission", -1) < (2 if admin else 1 if write else 0):
            fail(403, "project_permission_denied")
        if write and obj.get("is_archived") and not allow_archive:
            fail(409, "project_archived")
        return obj

    def bot_token(self, space):
        if space.get("tokenExpires", 0) <= self.clock():
            fail(401, "upstream_bot_credential_expired")
        return self.crypto.decrypt(space["encryptedToken"].encode()).decode()

    # -- 自有账号 → 底座会话（AUTH-LOGIN-05）-----------------------------------
    # 底座只做任务存储，不对用户露出：点点账号（谷歌/邮箱验证码）登录成功后，
    # 扩展服务自动在底座开通或复用对应存储用户并下发会话。底座密码随机生成，
    # 用服务密钥加密保存，用户全程无感，界面不出现任何账号密码表单。

    def _vikunja_login(self, username, password):
        reply = self.upstream.call("", "POST", "/login", {"username": username, "password": password})
        token = reply.get("token")
        if not token:
            raise UpstreamFailure(502)
        return token

    def ensure_vikunja_session(self, account):
        """返回底座 JWT：已开通用户凭加密凭证重新登录；未开通用户自动注册并绑定。"""
        cred = self.store.get("vikunja_credential", account["id"])
        if account.get("vikunjaUserId"):
            if not cred or not cred.get("encryptedPassword"):
                # 早期手工关联的账号没有底座凭证，无法自动签发会话
                fail(409, "vikunja_session_unavailable")
            token = self._vikunja_login(cred["username"],
                                        self.crypto.decrypt(cred["encryptedPassword"].encode()).decode())
            return {"token": token,
                    "user": {"id": account["vikunjaUserId"], "username": cred["username"]}}
        base = re.sub(r"[^a-z0-9._-]", "", account["email"].split("@", 1)[0].lower()) or "user"
        last_failure = None
        for _ in range(3):
            username = f"{base[:24]}-{secrets.token_hex(3)}"
            password = secrets.token_urlsafe(24)
            try:
                self.upstream.call("", "POST", "/register",
                                   {"username": username, "password": password,
                                    "email": account["email"]})
                token = self._vikunja_login(username, password)
                user = self.upstream.call(token, "GET", "/user")
            except UpstreamFailure as exc:
                last_failure = exc
                continue  # 用户名冲突等可重试错误：换随机后缀再来
            with self.store.transaction():
                account["vikunjaUserId"] = user["id"]
                self.store.put("account", account["id"], account)
                self.store.put("vikunja_credential", account["id"], {
                    "username": username, "vikunjaUserId": user["id"],
                    "encryptedPassword": self.crypto.encrypt(password.encode()).decode(),
                    "created": self.clock()})
            return {"token": token, "user": {"id": user["id"], "username": username}}
        raise public_failure(last_failure)

    def space(self, owner, sid):
        obj = self.store.get("spaces", sid)
        if not obj or obj["owner"] != owner:
            fail(404, "space_not_found")
        return obj

    def connection(self, credential, scope="read", project=None):
        con = self.store.get("connections", digest(credential))
        if not con or con["revoked"] or con["expiresAt"] <= self.clock():
            fail(401, "connection_invalid")
        if scope not in con["scopes"]:
            fail(403, "scope_denied")
        if project is not None and project not in con["projectIds"]:
            fail(403, "project_outside_scope")
        space = self.store.get("spaces", con["spaceId"])
        token = self.manage_token(space) if "manage" in con["scopes"] else self.bot_token(space)
        for pid in ([project] if project is not None else con["projectIds"]):
            self.project(token, pid, write=scope in ("execute", "manage"), allow_archive=scope == "manage")
        return con, token

    @staticmethod
    def safe_space(space):
        return {k: v for k, v in space.items() if k not in (
            "encryptedToken", "tokenId", "encryptedManageToken", "manageTokenId")}

    @staticmethod
    def safe_connection(con):
        return {k: v for k, v in con.items() if k != "credentialHash"}

    def action_result(self, rec):
        steps = {}
        for name, step in rec["steps"].items():
            result = step.get("result", {})
            steps[name] = {"status": step["status"], "id": result.get("id")}
        return {"actionId": rec["actionId"], "status": rec["status"],
                "result": rec.get("result"), "steps": steps, "error": rec.get("error")}

    def reconcile_action(self, key, rec):
        """Callers and writers hold store.lock; an observed running record is interrupted."""
        if rec["status"] == "unknown":
            return rec
        if rec["status"] == "running" or any(
                step["status"] == "inflight" for step in rec["steps"].values()):
            inflight = [s for s in rec["steps"].values() if s["status"] == "inflight"]
            last = list(rec["steps"].values())[-1] if rec["steps"] else {}
            safe = (bool(inflight) and all(s.get("localSafe") for s in inflight)
                    or not inflight and last.get("localSafe")) and (
                bool(rec.get("retryVersions")) or not rec["steps"]
                or rec["steps"].get("delete", {}).get("status") == "done")
            if safe:
                rec["steps"] = {n: s for n, s in rec["steps"].items() if s["status"] != "inflight"}
                rec["recoveredLocal"] = True
            rec["status"] = "partial" if safe else "unknown"
            rec["error"] = {"code": "interrupted_local_checkpoint" if safe else "interrupted_write",
                            "retrySafe": safe}
            self.store.put("actions", key, rec)
        return rec

    def action(self, actor, action_id, request, work, source=None, recovery_token=None):
        request = canonical_request(request)
        key = f"{actor}:{action_id}"
        summary = digest(json.dumps(request, sort_keys=True, separators=(",", ":")))
        with self.store.lock:
            rec = self.store.get("actions", key)
            if rec:
                if rec["hash"] != summary:
                    fail(409, "action_id_conflict")
                self.reconcile_action(key, rec)
                if rec["status"] in ("complete", "unknown"):
                    return self.action_result(rec)
                if recovery_token and rec.get("retryVersions") and (
                        actor.startswith("agent:") or request.get("ifVersion") or rec.get("recoveredLocal")):
                    self.check_retry_versions(recovery_token, rec)
            else:
                rec = {"actionId": action_id, "actor": actor, "hash": summary,
                       "request": request, "steps": {}, "status": "running", "source": source}
                self.store.put("actions", key, rec)

            def step(name, operation, local_safe=False, atomic_local=False):
                local_safe = local_safe or atomic_local
                old = rec["steps"].get(name)
                if old and old["status"] == "done":
                    return old["result"]
                if local_safe and recovery_token:
                    self.remember_retry_versions(recovery_token, key, request.get("taskId"),
                                                 request.get("operation", "task.execution"),
                                                 request.get("projectId"))
                    rec["retryVersions"] = self.store.get("actions", key).get("retryVersions", {})
                rec["steps"][name] = {"status": "inflight", "localSafe": local_safe}
                self.store.put("actions", key, rec)
                try:
                    if atomic_local:
                        with self.store.transaction():
                            result = operation()
                            rec["steps"][name] = {"status": "done", "result": result, "localSafe": True}
                            if recovery_token:
                                self.remember_retry_versions(recovery_token, key, request.get("taskId"),
                                                             request.get("operation", "task.execution"),
                                                             request.get("projectId"))
                                rec["retryVersions"] = self.store.get("actions", key).get("retryVersions", {})
                            self.store.put("actions", key, rec)
                        return result
                    result = operation()
                except UpstreamFailure as exc:
                    if not exc.unknown:
                        del rec["steps"][name]
                    raise
                except HTTPException:
                    del rec["steps"][name]
                    raise
                rec["steps"][name] = {"status": "done", "result": result, "localSafe": local_safe}
                self.store.put("actions", key, rec)
                return result

            try:
                rec["result"] = work(step)
                rec["status"], rec["error"] = "complete", None
                self.store.put("actions", key, rec)
            except (UpstreamFailure, HTTPException) as exc:
                unknown = isinstance(exc, UpstreamFailure) and exc.unknown
                rec["status"] = "unknown" if unknown else "partial"
                rec["error"] = {"code": "upstream_unknown" if unknown else "step_failed",
                                "upstreamStatus": getattr(exc, "status", getattr(exc, "status_code", 500)),
                                "retrySafe": not unknown}
                self.store.put("actions", key, rec)
            # Unexpected storage/process failures retain inflight checkpoints. Never auto-retry.
            return self.action_result(rec)

    def prepare(self, user, token, req):
        owner = user["id"]
        sid = f"{owner}:daily" if req.mode == "daily" else f"{owner}:project:{req.projectId}"
        existing = self.store.get("spaces", sid)
        if existing and existing.get("projectId"):
            self.project(token, existing["projectId"], admin=True)

        def work(step):
            space = self.store.get("spaces", sid) or {
                "id": sid, "owner": owner, "mode": req.mode, "status": "preparing"}
            if req.projectId and space.get("projectId") not in (None, req.projectId):
                fail(409, "registered_project_mismatch")
            if req.botId and space.get("botId") not in (None, req.botId):
                fail(409, "registered_bot_mismatch")
            if not space.get("projectId"):
                project = (self.project(token, req.projectId, admin=True, write=True)
                           if req.projectId else step("project", lambda: self.upstream.call(
                               token, "POST", "/projects", {"title": "Diandian Daily"})))
                space["projectId"] = project["id"]
                self.store.put("spaces", sid, space)
            pid = space["projectId"]
            self.store.put("action_access", f"{owner}:{req.actionId}", {"projectId": pid})
            self.project(token, pid, admin=True, write=True)
            if not space.get("botId"):
                if req.botId:
                    bot = self.upstream.call(token, "GET", f"/user/bots/{req.botId}")
                    if bot.get("bot_owner_id") != owner:
                        fail(403, "bot_not_owned")
                else:
                    bot = step("bot", lambda: self.upstream.call(token, "POST", "/user/bots", {
                        "username": f"bot-dd-{owner}-{secrets.token_hex(5)}",
                        "name": "Diandian shared Bot" if req.mode == "daily" else "Diandian project Bot"}))
                space.update(botId=bot["id"], botUsername=bot["username"])
                self.store.put("spaces", sid, space)
            bot = self.upstream.call(token, "GET", f"/user/bots/{space['botId']}")
            if bot.get("bot_owner_id") != owner or bot.get("status", 0) != 0:
                fail(403, "bot_not_active_or_owned")
            space.update(publicOwner=public_person(user), publicBot=public_person(bot))
            members = self.upstream.all(token, f"/projects/{pid}/users")
            existing = next((m for m in members if m["id"] == space["botId"]), None)
            if not existing:
                step("membership", lambda: self.upstream.call(token, "POST", f"/projects/{pid}/users",
                                                             {"username": space["botUsername"], "permission": 1}))
            elif existing.get("permission", 0) < 1:
                step("membership-upgrade", lambda: self.upstream.call(
                    token, "PUT", f"/projects/{pid}/users/{space['botUsername']}", {"permission": 1}))
            if not space.get("encryptedToken") or space.get("tokenExpires", 0) <= self.clock():
                def create_token():
                    expiry = self.clock() + 90 * 86400
                    raw = self.upstream.call(token, "POST", "/tokens", {
                        "title": "Diandian gateway Bot", "owner_id": space["botId"],
                        "permissions": {"projects": ["read_one"], "tasks": ["read_all", "read_one", "update"],
                                        "tasks_assignees": ["read_all"]},
                        "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat()})
                    # Do not checkpoint plaintext token even in internal action records.
                    return {"id": raw["id"], "encryptedToken": self.crypto.encrypt(
                        raw["token"].encode()).decode(), "expiresAt": expiry}
                saved = step("token", create_token)
                space.update(encryptedToken=saved["encryptedToken"], tokenId=saved["id"],
                             tokenExpires=saved["expiresAt"])
                self.store.put("spaces", sid, space)
            self.project(self.bot_token(space), pid, write=True)
            self.upstream.call(self.bot_token(space), "GET", f"/projects/{pid}/tasks",
                               params={"page": 1, "per_page": 1})
            space["status"] = "ready"
            self.store.put("spaces", sid, space)
            return self.safe_space(space)

        return self.action(f"human:{owner}", req.actionId, req.model_dump(mode="json"), work)

    def issue_code(self, user, token, req):
        with self.store.lock:
            space = self.space(user["id"], req.spaceId)
            if space["status"] != "ready":
                fail(409, "space_not_ready")
            # A space currently represents one authorized project, never arbitrary UI projects.
            if set(req.projectIds) != {space["projectId"]} or not req.scopes or "read" not in req.scopes:
                fail(403, "invalid_project_or_scope")
            self.project(token, space["projectId"], admin=True, write=True)
            if "manage" in req.scopes:
                self.authorize_manage(user, token, space)
            self.project(self.bot_token(space), space["projectId"], write="execute" in req.scopes)
            code = secrets.token_hex(8).upper()
            rec = {"id": secrets.token_urlsafe(18), "hash": digest(code),
                   "owner": user["id"], "spaceId": req.spaceId, "projectIds": req.projectIds,
                   "scopes": sorted(set(req.scopes)), "expiresAt": self.clock() + 600,
                   "used": False, "revoked": False}
            self.store.put("codes", rec["hash"], rec)
            return {"codeId": rec["id"], "code": code, "expiresAt": rec["expiresAt"]}

    def redeem(self, req, remote):
        # Rate limiting and single redemption are serialized across request threads.
        with self.store.lock:
            key = digest(remote)
            attempt = self.store.get("attempts", key) or {"start": self.clock(), "count": 0}
            if self.clock() - attempt["start"] >= 600:
                attempt = {"start": self.clock(), "count": 0}
            if attempt["count"] >= 5:
                fail(429, "pair_rate_limited")
            rec = self.store.get("codes", digest(req.code.upper()))
            if not rec or rec["used"] or rec["revoked"] or rec["expiresAt"] <= self.clock():
                attempt["count"] += 1
                self.store.put("attempts", key, attempt)
                fail(400, "pair_code_invalid")
            credential, con = self.new_connection(rec, req.name)
            with self.store.transaction():
                rec["used"] = True
                self.store.put("codes", rec["hash"], rec)
                self.store.put("connections", con["credentialHash"], con)
            return {"credential": credential, "connection": self.safe_connection(con)}

    def new_connection(self, rec, name):
        space = self.store.get("spaces", rec["spaceId"])
        token = self.manage_token(space) if "manage" in rec["scopes"] else self.bot_token(space)
        for pid in rec["projectIds"]:
            self.project(token, pid, write=bool({"execute", "manage"} & set(rec["scopes"])))
        credential = "dd_" + secrets.token_urlsafe(32)
        return credential, {
            "id": secrets.token_urlsafe(18), "credentialHash": digest(credential),
            "name": name, "owner": rec["owner"], "botId": space["botId"],
            "spaceId": rec["spaceId"], "projectIds": rec["projectIds"],
            "scopes": rec["scopes"], "revoked": False,
            "expiresAt": min(self.clock() + 30 * 86400, space[
                "manageTokenExpires" if "manage" in rec["scopes"] else "tokenExpires"]),
            "createdAt": self.clock(),
        }

    def start_device(self, req, remote):
        with self.store.lock:
            key = digest("device:" + remote)
            attempt = self.store.get("attempts", key) or {"start": self.clock(), "count": 0}
            if self.clock() - attempt["start"] >= 600:
                attempt = {"start": self.clock(), "count": 0}
            if attempt["count"] >= 20:
                fail(429, "authorization_rate_limited")
            attempt["count"] += 1
            self.store.put("attempts", key, attempt)
            secret = secrets.token_urlsafe(32)
            rec = {"id": secrets.token_urlsafe(24), "name": req.name,
                   "secretHash": digest(secret), "status": "pending",
                   "expiresAt": self.clock() + 600}
            self.store.put("devices", rec["id"], rec)
            return {"requestId": rec["id"], "deviceSecret": secret,
                    "verificationPath": f"/#authorize?request={rec['id']}",
                    "expiresAt": rec["expiresAt"], "interval": 2}

    def device(self, request_id):
        rec = self.store.get("devices", request_id)
        if not rec:
            fail(404, "authorization_not_found")
        if rec["expiresAt"] <= self.clock():
            fail(410, "authorization_expired")
        return rec

    def device_info(self, request_id):
        with self.store.lock:
            rec = self.device(request_id)
            return {k: rec[k] for k in ("id", "name", "status", "expiresAt")}

    def decide_device(self, user, token, request_id, req):
        with self.store.lock:
            rec = self.device(request_id)
            if rec["status"] != "pending":
                fail(409, "authorization_already_decided")
            if req.approve:
                space = self.space(user["id"], req.spaceId)
                if space["status"] != "ready":
                    fail(409, "space_not_ready")
                if not req.scopes or "read" not in req.scopes:
                    fail(403, "invalid_project_or_scope")
                self.project(token, space["projectId"], admin=True, write=True)
                if "manage" in req.scopes:
                    self.authorize_manage(user, token, space)
                self.project(self.bot_token(space), space["projectId"], write="execute" in req.scopes)
                rec.update(owner=user["id"], spaceId=space["id"],
                           projectIds=[space["projectId"]], scopes=sorted(set(req.scopes)))
            rec["status"] = "approved" if req.approve else "denied"
            self.store.put("devices", rec["id"], rec)
            return {"status": rec["status"]}

    def poll_device(self, request_id, secret):
        with self.store.lock:
            rec = self.device(request_id)
            if not secrets.compare_digest(rec["secretHash"], digest(secret)):
                fail(401, "authorization_secret_invalid")
            if rec["status"] in ("pending", "denied"):
                return {"status": rec["status"]}
            if rec.get("encryptedCredential"):
                credential = self.crypto.decrypt(rec["encryptedCredential"].encode()).decode()
                con, _ = self.connection(credential)
            else:
                credential, con = self.new_connection(rec, rec["name"])
                rec["encryptedCredential"] = self.crypto.encrypt(credential.encode()).decode()
                with self.store.transaction():
                    self.store.put("connections", con["credentialHash"], con)
                    self.store.put("devices", rec["id"], rec)
            return {"status": "authorized", "credential": credential,
                    "connection": self.safe_connection(con)}

    def agent_logout(self, credential):
        with self.store.lock:
            con = self.store.get("connections", digest(credential))
            if not con:
                fail(401, "connection_invalid")
            con["revoked"] = True
            self.store.put("connections", con["credentialHash"], con)
            return {"revoked": True}

    def identify_client(self, credential, name):
        with self.store.lock:
            con, _ = self.connection(credential)
            con["name"] = name
            self.store.put("connections", con["credentialHash"], con)
            return {"status": "authorized", "connection": self.safe_connection(con)}

    def cancel_device(self, request_id, secret):
        with self.store.lock:
            # Expiry blocks approval/redemption, not revocation of an issued connection.
            rec = self.store.get("devices", request_id)
            if not rec:
                fail(404, "authorization_not_found")
            if not secrets.compare_digest(rec["secretHash"], digest(secret)):
                fail(401, "authorization_secret_invalid")
            with self.store.transaction():
                if rec.get("encryptedCredential"):
                    credential = self.crypto.decrypt(rec["encryptedCredential"].encode()).decode()
                    con = self.store.get("connections", digest(credential))
                    con["revoked"] = True
                    self.store.put("connections", con["credentialHash"], con)
                rec.pop("encryptedCredential", None)
                rec["status"] = "denied"
                self.store.put("devices", rec["id"], rec)
            return {"status": "cancelled"}

    def revoke_code(self, owner, code_id):
        with self.store.lock:
            rec = next((c for c in self.store.all("codes")
                        if c["id"] == code_id and c["owner"] == owner), None)
            if not rec:
                fail(404, "code_not_found")
            rec["revoked"] = True
            self.store.put("codes", rec["hash"], rec)
            return {"revoked": True}

    def disconnect(self, owner, cid):
        with self.store.lock:
            con = next((c for c in self.store.all("connections")
                        if c["id"] == cid and c["owner"] == owner), None)
            if not con:
                fail(404, "connection_not_found")
            con["revoked"] = True
            self.store.put("connections", con["credentialHash"], con)
            return {"revoked": True}

    def task_meta(self, task):
        return self.store.get("meta", task) or Metadata().model_dump(mode="json")

    def initialize_meta(self, task_id, draft, parent=None, event_parent=False):
        """Preserve workflow progress when repairing a partially created task."""
        meta = draft.meta.model_dump(mode="json")
        persisted = self.store.get("meta", task_id)
        if persisted:
            meta.update(persisted)
        if parent:
            meta["event"] = parent
        if event_parent:
            meta["kind"] = "event"
        return meta

    def emit(self, owner, project, task_id, action_id, intent):
        eid = digest(f"{owner}:{action_id}:{task_id}")
        payload = {"eventId": eid, "actionId": action_id, "taskId": task_id,
                   "projectId": project, "creatorId": owner, "intent": intent,
                   "automaticExecution": intent == "agent", "createdAt": self.clock(),
                   "type": "task.created"}
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO events(id,owner,project,payload) VALUES (?,?,?,?)",
                                  (eid, owner, project, json.dumps(payload)))
            self.store.db.execute("INSERT OR IGNORE INTO human_notices(event,owner) VALUES (?,?)", (eid, owner))
            for con in self.store.all("connections"):
                if (con["owner"] == owner and not con["revoked"]
                        and con["expiresAt"] > self.clock() and project in con["projectIds"]):
                    self.store.db.execute("INSERT OR IGNORE INTO deliveries(event,connection) VALUES (?,?)",
                                          (eid, con["id"]))
        return {"eventId": eid}

    def eligible_bot(self, owner, project, bot_id):
        matches = [c for c in self.store.all("connections") if c["owner"] == owner
                   and c["botId"] == bot_id and project in c["projectIds"]
                   and not c["revoked"] and c["expiresAt"] > self.clock() and "execute" in c["scopes"]]
        if not matches:
            fail(403, "bot_has_no_eligible_connection")
        for con in matches:
            token = self.bot_token(self.store.get("spaces", con["spaceId"]))
            self.project(token, project, write=True)

    def validate_assignee(self, user, token, project, uid):
        if uid == user["id"]:
            return False
        owned_bots = []
        if not user.get("botActor"):
            try:
                owned_bots = self.upstream.all(token, "/user/bots")
            except UpstreamFailure as exc:
                if exc.status not in (403, 404):
                    raise
        candidates = owned_bots + self.upstream.all(token, f"/projects/{project}/users")
        person = next((u for u in candidates if u["id"] == uid), None)
        if person is None:
            # Existing team assignees are discoverable without pretending direct shares
            # enumerate every inherited member.
            for task in self.upstream.all(token, f"/projects/{project}/tasks"):
                person = next((u for u in task.get("assignees", []) if u["id"] == uid), None)
                if person:
                    break
        if person is None:
            fail(422, "assignee_not_discovered")
        if person.get("bot_owner_id"):
            self.eligible_bot(person["bot_owner_id"], project, uid)
            return True
        # Native relationship writes also enforce actual membership sources.
        return False

    def task_action(self, user, token, req, con=None):
        with self.store.lock:
            if not con and req.ifVersion:
                key = f"human:{user['id']}:{req.actionId}"
                record = self.store.get("actions", key)
                if record:
                    self.reconcile_action(key, record)
                    if record["status"] == "partial":
                        self.check_retry_versions(token, record)
                elif req.taskId:
                    task = self.upstream.call(token, "GET", f"/tasks/{req.taskId}")
                    self.project(token, task["project_id"], write=True)
                    self.check_version(task, req.ifVersion)
                elif req.operation in ("project.update", "project.archive"):
                    project = self.project(token, req.projectId, write=True,
                                           allow_archive=req.operation == "project.archive")
                    self.check_version(project, req.ifVersion, "project")
            if req.dryRun:
                if req.taskId:
                    task = self.upstream.call(token, "GET", f"/tasks/{req.taskId}")
                    self.project(token, task["project_id"], write=True)
                elif req.projectId:
                    self.project(token, req.projectId, write=True,
                                 allow_archive=req.operation == "project.archive")
                return {"actionId": req.actionId, "status": "dry_run", "request": req.model_dump(mode="json")}
            result = self._task_action(user, token, req, con)
            if (con or req.ifVersion) and result["status"] == "partial":
                self.remember_retry_versions(token, f"agent:{con['id']}:{req.actionId}" if con else
                                             f"human:{user['id']}:{req.actionId}",
                                             req.taskId, req.operation, req.projectId)
            if result["status"] == "partial":
                self.audit_partial_action(token, f"agent:{con['id']}:{req.actionId}" if con else
                                          f"human:{user['id']}:{req.actionId}",
                                          req.taskId, req.operation, bool(req.draft))
            return result

    def remember_retry_versions(self, token, key, task_id, operation, project_id):
        rec = self.store.get("actions", key)
        targets = {"task:" + str(task_id)} if task_id else set()
        if operation.startswith("project.") and project_id:
            targets.add(f"project:{project_id}")
        draft = rec["request"].get("draft") or {}
        event = (draft.get("meta") or {}).get("event")
        if event:
            targets.add(f"task:{event}")
        for name, step in rec["steps"].items():
            if name == "project" and operation.startswith("project.") and step.get("result", {}).get("id"):
                targets.add(f"project:{step['result']['id']}")
            if (name == "parent" or name.startswith("child:") and name.count(":") == 1) and step.get("result", {}).get("id"):
                targets.add(f"task:{step['result']['id']}")
        versions = {}
        for target in targets:
            kind, oid = target.split(":")
            try:
                obj = self.upstream.call(token, "GET", f"/{'tasks' if kind == 'task' else 'projects'}/{oid}")
            except UpstreamFailure as exc:
                if exc.status == 404 and operation == "task.delete":
                    continue
                raise
            versions[target] = self.object_version(obj, kind)
        rec["retryVersions"] = versions
        self.store.put("actions", key, rec)

    def audit_partial_action(self, token, key, task_id, operation, is_create=False):
        rec = self.store.get("actions", key)
        actor = rec.get("source")
        if not actor:
            return
        complete_steps = sorted(name for name, step in rec["steps"].items() if step["status"] == "done")
        if not complete_steps:
            return
        targets = {task_id} if task_id else set()
        for name, step in rec["steps"].items():
            if (name == "parent" or name.startswith("child:") and name.count(":") == 1
                    ) and step.get("result", {}).get("id"):
                targets.add(step["result"]["id"])
        for tid in targets:
            if is_create and not self.store.get("sources", tid):
                continue
            if operation == "task.delete" and "delete" in complete_steps:
                self.record_change(token, (self.store.get("versions", f"task:{tid}") or {})["projectId"],
                                   tid, actor, rec["actionId"], operation,
                                   complete_steps, deleted=True)
            else:
                task = self.upstream.call(token, "GET", f"/tasks/{tid}")
                if any(name in complete_steps for name in ("update", "meta", "assignee", "label")) or is_create:
                    self.record_change(token, task["project_id"], tid, actor, rec["actionId"],
                                       operation + ".partial." + digest(",".join(complete_steps))[:12],
                                       complete_steps)

    def _task_action(self, user, token, req, con=None):
        owner = user["id"]
        actor = f"agent:{con['id']}" if con else f"human:{owner}"
        action_key = f"{actor}:{req.actionId}"
        access_category = "agent_action_access" if con else "action_access"
        access_key = f"{con['id'] if con else owner}:{req.actionId}"
        access = self.store.get(access_category, access_key)
        if access:
            # Idempotent response replay still requires current project read permission.
            self.project(token, access["projectId"])
        elif req.projectId:
            self.store.put(access_category, access_key, {"projectId": req.projectId})

        def source():
            return self.store.get("actions", action_key).get("source") or {
                "kind": "unknown", "userId": owner, "connectionId": None,
                "clientNameAtCreation": "", "createdAt": None}

        def work(step):
            op = req.operation
            if op == "project.create":
                obj = step("project", lambda: self.upstream.call(
                    token, "POST", "/projects", {"title": req.title, "description": req.description or ""}))
                self.store.put(access_category, access_key, {"projectId": obj["id"]})
                step("audit:project", lambda: self.record_project_change(obj, source(), req.actionId, op), local_safe=True)
                return {"projectId": obj["id"], "version": self.object_version(obj, "project")}
            if op in ("project.archive", "project.update"):
                obj = self.project(token, req.projectId, write=True, allow_archive=op == "project.archive")
                if op == "project.archive":
                    obj["is_archived"] = req.archived
                else:
                    if req.title is not None:
                        obj["title"] = req.title
                    if req.description is not None:
                        obj["description"] = req.description
                saved = step(op, lambda: self.upstream.call(token, "PUT", f"/projects/{obj['id']}", obj))
                step("audit:project", lambda: self.record_project_change(saved, source(), req.actionId, op), local_safe=True)
                return {"projectId": saved["id"], "version": self.object_version(saved, "project"),
                        **({"archived": req.archived} if op == "project.archive" else {})}
            if op in ("task.create", "event.create"):
                self.project(token, req.projectId, write=True)

                def create(draft, name, parent=None):
                    parent = parent or draft.meta.event
                    if parent:
                        self.validate_event(token, req.projectId, parent)
                    assignees = list(dict.fromkeys(draft.assigneeIds))
                    daily = next((s for s in self.store.all("spaces")
                                  if s["owner"] == owner and s["mode"] == "daily"
                                  and s.get("projectId") == req.projectId), None)
                    if draft.intent == "self" and daily:
                        if any(uid != owner for uid in assignees):
                            fail(422, "self_intent_must_assign_human")
                        assignees = [owner]
                    if con and draft.intent == "self" and not daily and not assignees:
                        # Project self intent need not imply assignment, unlike daily self.
                        assignees = []
                    if draft.intent == "agent" and not assignees:
                        if not daily and not con:
                            fail(422, "agent_intent_requires_assignees")
                        assignees = [daily["botId"] if daily else con["botId"]]
                    qualifications = [self.validate_assignee(user, token, req.projectId, uid) for uid in assignees]
                    if draft.intent == "agent" and not any(qualifications):
                        fail(422, "agent_intent_requires_bot")
                    obj = step(name, lambda: self.upstream.call(token, "POST",
                               f"/projects/{req.projectId}/tasks",
                               {"title": draft.title, "description": draft.description, "priority": draft.priority}))
                    tid = obj["id"]
                    current = self.upstream.call(token, "GET", f"/tasks/{tid}")
                    if current["project_id"] != req.projectId:
                        fail(409, "created_task_moved")
                    step(f"{name}:source", lambda: self.source(tid, source()), local_safe=True)
                    meta = self.initialize_meta(tid, draft, parent,
                                                name == "parent" and op == "event.create")
                    step(f"{name}:meta", lambda: self.save_meta(tid, meta), atomic_local=True)
                    if parent:
                        step(f"{name}:relation", lambda: self.upstream.call(
                            token, "POST", f"/tasks/{parent}/relations",
                            {"other_task_id": tid, "relation_kind": "subtask"}))
                        step(f"{name}:parentaudit", lambda: self.record_change(
                            token, req.projectId, parent, source(), req.actionId,
                            f"task.relation.add.{tid}", ["related_tasks"]), local_safe=True)
                    for uid in assignees:
                        step(f"{name}:assignee:{uid}", lambda uid=uid: self.upstream.call(
                            token, "POST", f"/tasks/{tid}/assignees", {"user_id": uid}))
                    for lid in dict.fromkeys(draft.labelIds):
                        step(f"{name}:label:{lid}", lambda lid=lid: self.upstream.call(
                            token, "POST", f"/tasks/{tid}/labels", {"label_id": lid}))
                    step(f"{name}:audit", lambda: self.record_change(
                        token, req.projectId, tid, source(), req.actionId, op,
                        ["title", "description", "priority", "meta", "assignees", "labels"]), local_safe=True)
                    return tid

                tid = create(req.draft, "parent")
                children = [create(d, f"child:{i}", tid) for i, d in enumerate(req.children)]
                if children:
                    step("parent:finalaudit", lambda: self.record_change(
                        token, req.projectId, tid, source(), req.actionId,
                        "event.create.finalized", ["related_tasks"]), local_safe=True)
                if op == "task.create":
                    step("signal", lambda: self.emit(owner, req.projectId, tid, req.actionId, req.draft.intent))
                else:
                    for i, (child_id, draft) in enumerate(zip(children, req.children)):
                        step(f"signal:{i}", lambda child_id=child_id, draft=draft: self.emit(
                            owner, req.projectId, child_id, req.actionId, draft.intent))
                return {"taskId": tid, "childTaskIds": children,
                        "version": self.object_version(self.upstream.call(token, "GET", f"/tasks/{tid}"))}

            existing = self.store.get("actions", action_key)
            if op == "task.delete" and existing["steps"].get("delete", {}).get("status") == "done":
                step("audit", lambda: self.record_change(
                    token, self.store.get(access_category, access_key)["projectId"], req.taskId,
                    source(), req.actionId, op, ["deleted"], deleted=True), local_safe=True)
                return {"taskId": req.taskId, "deleted": True}
            task = self.upstream.call(token, "GET", f"/tasks/{req.taskId}")
            pid = task["project_id"]
            self.store.put(access_category, access_key, {"projectId": pid})
            if req.projectId and req.projectId != pid:
                fail(403, "project_mismatch")
            self.project(token, pid, write=True)
            if op == "task.delete":
                # A confirmed DELETE can be replayed even when its audit checkpoint failed.
                self.object_version(task)
                step("delete", lambda: self.upstream.call(token, "DELETE", f"/tasks/{req.taskId}"))
                step("audit", lambda: self.record_change(
                    token, pid, req.taskId, source(), req.actionId, op, ["deleted"], deleted=True), local_safe=True)
                return {"taskId": req.taskId, "deleted": True}
            if op.startswith("assignee."):
                if op == "assignee.add":
                    self.validate_assignee(user, token, pid, req.userId)
                    step("assignee", lambda: self.upstream.call(token, "POST",
                         f"/tasks/{req.taskId}/assignees", {"user_id": req.userId}))
                else:
                    step("assignee", lambda: self.upstream.call(
                         token, "DELETE", f"/tasks/{req.taskId}/assignees/{req.userId}"))
            elif op.startswith("label."):
                method = "POST" if op == "label.add" else "DELETE"
                path = f"/tasks/{req.taskId}/labels" + (f"/{req.labelId}" if method == "DELETE" else "")
                step("label", lambda: self.upstream.call(token, method, path,
                                                       {"label_id": req.labelId} if method == "POST" else None))
            elif op in ("task.update", "event.update"):
                meta = self.task_meta(req.taskId)
                if req.meta:
                    meta = {**meta, **req.meta.model_dump(mode="json", exclude_unset=True)}
                if meta["event"] != self.task_meta(req.taskId)["event"]:
                    fail(409, "event_membership_change_not_supported")
                if meta["event"]:
                    self.validate_event(token, pid, meta["event"])
                if self.task_meta(req.taskId)["state"] == "review" and meta["state"] != "review":
                    fail(409, "human_confirmation_not_defined")
                if req.done and (meta["state"] == "review" or self.task_meta(req.taskId)["state"] == "review"):
                    fail(409, "human_confirmation_not_defined")
                if task.get("done") and meta["state"] in ("doing", "review"):
                    fail(409, "external_done_conflict")
                if req.title is not None:
                    task["title"] = req.title
                if req.description is not None:
                    task["description"] = req.description
                if req.priority is not None:
                    task["priority"] = req.priority
                if req.done is not None:
                    task["done"] = req.done
                if any(v is not None for v in (req.title, req.description, req.priority, req.done)):
                    step("update", lambda: self.upstream.call(token, "PUT", f"/tasks/{req.taskId}", task))
                if req.meta:
                    step("meta", lambda: self.save_meta(req.taskId, meta), atomic_local=True)
                if req.labelIds is not None:
                    # Persist the original diff before relationship writes; retry names stay stable.
                    plan = step("labels:plan", lambda: {
                        "remove": sorted({x["id"] for x in task.get("labels") or []} - set(req.labelIds)),
                        "add": sorted(set(req.labelIds) - {x["id"] for x in task.get("labels") or []})},
                        atomic_local=True)
                    for lid in plan["remove"]:
                        step(f"labels:remove:{lid}", lambda lid=lid: self.upstream.call(
                            token, "DELETE", f"/tasks/{req.taskId}/labels/{lid}"))
                    for lid in plan["add"]:
                        step(f"labels:add:{lid}", lambda lid=lid: self.upstream.call(
                            token, "POST", f"/tasks/{req.taskId}/labels", {"label_id": lid}))
            step("audit", lambda: self.record_change(token, pid, req.taskId, source(), req.actionId,
                 op, sorted(req.model_fields_set - {"actionId", "operation", "ifVersion", "dryRun", "taskId", "projectId"})),
                 local_safe=True)
            return {"taskId": req.taskId,
                    "version": self.object_version(self.upstream.call(token, "GET", f"/tasks/{req.taskId}"))}

        return self.action(actor, req.actionId, agent_action_request(req) if con else req.model_dump(mode="json"), work,
                           source=self.actor_snapshot(user, con), recovery_token=token)

    def save_meta(self, tid, meta):
        self.store.put("meta", tid, meta)
        return {"id": tid}

    def validate_event(self, token, project, event):
        parent = self.upstream.call(token, "GET", f"/tasks/{event}")
        if parent["project_id"] != project or self.task_meta(event)["kind"] != "event":
            fail(403, "invalid_event_parent")
        self.project(token, project, write=True)

    def execute(self, credential, task_id, req, require_version=False):
        # Validate on every call, including an idempotent replay after revocation/removal.
        with self.store.lock:
            con, token = self.connection(credential, "execute")
            task = self.upstream.call(token, "GET", f"/tasks/{task_id}")
            self.connection(credential, "execute", task["project_id"])
            assignees = self.upstream.all(token, f"/tasks/{task_id}/assignees")
            if con["botId"] not in {a["id"] for a in assignees}:
                fail(403, "bot_no_longer_assigned")
            if task.get("done"):
                fail(409, "external_done_conflict")
            if self.task_meta(task_id)["state"] == "review" and req.state != "review":
                fail(409, "human_confirmation_not_defined")
            key = f"agent:{con['id']}:{req.actionId}"
            rec = self.store.get("actions", key)
            request = canonical_request({"taskId": task_id, **req.model_dump(mode="json")})
            if rec and rec["request"] != request:
                fail(409, "action_id_conflict")
            if require_version and not req.ifVersion:
                fail(422, "if_version_required")
            if not rec and (require_version or req.ifVersion):
                self.check_version(task, req.ifVersion)
            if rec and rec["status"] == "partial" and require_version:
                self.check_retry_versions(token, rec)
            if req.dryRun:
                return {"actionId": req.actionId, "status": "dry_run", "operation": "task.execution",
                        "projectId": task["project_id"], "taskId": task_id,
                        "version": self.object_version(task), "request": request}
            self.store.put("agent_action_access", f"{con['id']}:{req.actionId}", {"projectId": task["project_id"]})

            def work(step):
                if req.description is not None:
                    task["description"] = req.description
                    # Current facts are echoed, no new done/assignee/project fields accepted.
                    step("update", lambda: self.upstream.call(token, "PUT", f"/tasks/{task_id}", task))
                meta = self.task_meta(task_id)
                meta["state"] = req.state
                step("meta", lambda: self.save_meta(task_id, meta), atomic_local=True)
                step("audit", lambda: self.record_change(
                    token, task["project_id"], task_id, self.store.get("actions", key).get("source") or {
                        "kind": "unknown", "userId": con["owner"], "connectionId": None,
                        "clientNameAtCreation": "", "createdAt": None},
                    req.actionId, "task.execution", ["state"] + (["description"] if req.description is not None else [])),
                    local_safe=True)
                return {"taskId": task_id, "state": req.state,
                        "version": self.object_version(self.upstream.call(token, "GET", f"/tasks/{task_id}"))}

            result = self.action(f"agent:{con['id']}", req.actionId,
                                 request, work, source=self.actor_snapshot({"id": con["owner"]}, con),
                                 recovery_token=token)
            if result["status"] == "partial":
                self.remember_retry_versions(token, key, task_id, "task.execution", task["project_id"])
                self.audit_partial_action(token, key, task_id, "task.execution")
            return result

    def cursor(self, seq, identity):
        return self.crypto.encrypt(json.dumps({"seq": seq, "identity": identity}).encode()).decode()

    def decode_cursor(self, cursor, identity):
        if not cursor:
            return 0
        try:
            data = json.loads(self.crypto.decrypt(cursor.encode()))
            if data["identity"] != identity or not isinstance(data["seq"], int) or data["seq"] < 0:
                raise ValueError()
            return data["seq"]
        except Exception:
            fail(422, "invalid_cursor")

    def events(self, credential, cursor, limit):
        with self.store.lock:
            con, _ = self.connection(credential)
            seq = self.decode_cursor(cursor, con["id"])
            rows = self.store.db.execute(
                "SELECT e.seq,e.project,e.payload FROM events e JOIN deliveries d ON e.id=d.event "
                "WHERE d.connection=? AND d.acked=0 AND e.seq>? ORDER BY e.seq",
                (con["id"], seq)).fetchall()
            visible = []
            for row in rows:
                self.connection(credential, project=row["project"])
                visible.append(row)
                if len(visible) > limit:
                    break
            page = visible[:limit]
            return {"items": [json.loads(r["payload"]) for r in page],
                    "nextCursor": self.cursor(page[-1]["seq"], con["id"]) if len(visible) > limit else None}

    def ack(self, credential, ids):
        with self.store.lock:
            con, _ = self.connection(credential)
            for eid in ids:
                if not self.store.db.execute("SELECT 1 FROM deliveries WHERE event=? AND connection=?",
                                             (eid, con["id"])).fetchone():
                    fail(403, "event_not_delivered_to_connection")
                row = self.store.db.execute("SELECT project FROM events WHERE id=?", (eid,)).fetchone()
                self.connection(credential, project=row["project"])
            with self.store.transaction():
                for eid in ids:
                    self.store.db.execute("UPDATE deliveries SET acked=1 WHERE event=? AND connection=?",
                                          (eid, con["id"]))
            return {"acked": ids}

    def notices(self, user, token, cursor, limit):
        identity = f"human:{user['id']}"
        seq = self.decode_cursor(cursor, identity)
        rows = self.store.db.execute(
            "SELECT e.seq,e.project,e.payload,n.acked FROM events e JOIN human_notices n ON e.id=n.event "
            "WHERE n.owner=? AND e.seq>? ORDER BY e.seq", (user["id"], seq)).fetchall()
        visible = []
        for row in rows:
            try:
                self.project(token, row["project"])
            except UpstreamFailure as exc:
                if exc.status in (403, 404):
                    continue
                raise
            visible.append(row)
            if len(visible) > limit:
                break
        page = visible[:limit]
        return {"items": [{**json.loads(r["payload"]), "read": bool(r["acked"]), "source": "diandian"}
                          for r in page],
                "nextCursor": self.cursor(page[-1]["seq"], identity) if len(visible) > limit else None}

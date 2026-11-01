import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import RedirectResponse

from .account_auth import AccountAuth, client_ip
from .admin import AdminConsole
from .models import (Ack, Action, AdminUserUpdate, ClientIdentity, DeviceDecision, DeviceStart,
                     EmailCodeRequest, EmailVerifyRequest, ExchangeRequest,
                     Execution, LinkVikunjaRequest, PairCode, Preferences,
                     Prepare, Redeem)
from .service import Service, fail
from .storage import Store
from .upstream import Upstream, UpstreamFailure, public_failure
from .webhook import parse_body, verify_signature

PREFIX = "/api/diandian/v1"


def create_app(db_path=None, upstream_url=None, key=None, transport=None, clock=None,
               identity_config=None, identity_sender=None, identity_http=None):
    store = Store(db_path or os.environ.get("DIANDIAN_DB", "diandian.sqlite"))
    try:
        configured_key = key or os.environ["DIANDIAN_KEY"]
        upstream = Upstream(upstream_url or os.environ.get("VIKUNJA_URL", "http://127.0.0.1:3456"), transport)
        service = Service(store, upstream, configured_key, **({"clock": clock} if clock else {}))
        accounts = AccountAuth(store, **{"clock": clock} if clock else {},
                               config=identity_config, sender=identity_sender, http=identity_http)
        admin = AdminConsole(store, **{"clock": clock} if clock else {})
        admin.bootstrap()
    except BaseException:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        yield
        upstream.close()
        store.close()

    app = FastAPI(title="Diandian Agent", version="0.2.0", lifespan=lifespan)
    app.state.service = service
    app.state.accounts = accounts
    app.state.admin = admin

    @app.middleware("http")
    async def no_store(request, call_next):
        """账户态数据一律不许缓存。

        这个服务的读写是「写后立刻重读」（PUT /preferences 之后紧跟 GET /state），
        FastAPI 默认不发 Cache-Control，浏览器会按启发式规则把 GET /state 存进磁盘缓存，
        于是重读拿到写入前的快照：操作明明成功，界面却纹丝不动。带 Vary: Authorization
        是给中间层（Cloudflare）一个分键依据，避免不同账号互相串用。
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
        return response

    @app.exception_handler(UpstreamFailure)
    async def upstream_error(request, exc):
        from fastapi.responses import JSONResponse
        error = public_failure(exc)
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    def bearer(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or len(authorization) <= 7:
            fail(401, "bearer_required")
        return authorization[7:]

    def human(token=Depends(bearer)):
        return service.human(token), token

    @app.get(PREFIX + "/health")
    def health():
        return {"status": "ok", "schemaVersion": 1, "apiVersion": "0.2.0",
                "collaborationSchemaVersion": 2}

    @app.get(PREFIX + "/me")
    def me(auth=Depends(human)):
        return auth[0]

    @app.post(PREFIX + "/spaces/prepare")
    def prepare(req: Prepare, auth=Depends(human)):
        return service.prepare(*auth, req)

    @app.get(PREFIX + "/state")
    def state(auth=Depends(human)):
        user, token = auth
        with store.lock:
            spaces = [service.safe_space(s) for s in store.all("spaces") if s["owner"] == user["id"]]
            connections = [service.safe_connection(c) for c in store.all("connections") if c["owner"] == user["id"]]
            projects = upstream.all(token, "/projects")
            tasks = []
            seen_tasks = set()
            people = {user["id"]: user}
            for bot in upstream.all(token, "/user/bots"):
                people[bot["id"]] = bot
            for project in projects:
                for task in upstream.all(token, f"/projects/{project['id']}/tasks"):
                    if task["id"] not in seen_tasks:
                        tasks.append(decorate(task))
                        seen_tasks.add(task["id"])
                    for person in task.get("assignees") or []:
                        people[person["id"]] = person
                for person in upstream.all(token, f"/projects/{project['id']}/users"):
                    people[person["id"]] = person
            daily = next((s for s in spaces if s["mode"] == "daily"), {})
            notices_page = service.notices(user, token, None, 100)
            return {"user": user, "daily_project_id": daily.get("projectId"),
                    "daily_bot_id": daily.get("botId"), "spaces": spaces, "connections": connections,
                    "projects": projects, "tasks": tasks, "people": list(people.values()),
                    "labels": upstream.all(token, "/labels"),
                    "notices": notices_page["items"], "noticesNextCursor": notices_page["nextCursor"],
                    "preferences": store.get("preferences", user["id"]) or Preferences().model_dump()}

    @app.post(PREFIX + "/pairing/codes")
    def issue(req: PairCode, auth=Depends(human)):
        return service.issue_code(*auth, req)

    @app.delete(PREFIX + "/pairing/codes/{code_id}")
    def revoke(code_id: str, auth=Depends(human)):
        return service.revoke_code(auth[0]["id"], code_id)

    @app.post(PREFIX + "/pairing/redeem")
    def redeem(req: Redeem, request: Request):
        return service.redeem(req, request.client.host if request.client else "unknown")

    @app.post(PREFIX + "/auth/requests")
    def start_auth(req: DeviceStart, request: Request):
        return service.start_device(req, request.client.host if request.client else "unknown")

    @app.get(PREFIX + "/auth/requests/{request_id}")
    def auth_info(request_id: str, auth=Depends(human)):
        return service.device_info(request_id)

    @app.post(PREFIX + "/auth/requests/{request_id}/decision")
    def auth_decision(request_id: str, req: DeviceDecision, auth=Depends(human)):
        return service.decide_device(*auth, request_id, req)

    @app.post(PREFIX + "/auth/requests/{request_id}/poll")
    def auth_poll(request_id: str, token=Depends(bearer)):
        return service.poll_device(request_id, token)

    @app.post(PREFIX + "/auth/requests/{request_id}/cancel")
    def auth_cancel(request_id: str, token=Depends(bearer)):
        return service.cancel_device(request_id, token)

    @app.post(PREFIX + "/agent/logout")
    def agent_logout(token=Depends(bearer)):
        return service.agent_logout(token)

    # -- 自有账号体系（AUTH-SYS-02）：谷歌 OAuth + 邮箱验证码 ----------------

    @app.get(PREFIX + "/auth/google/start")
    def google_start():
        return RedirectResponse(accounts.google_start_url(), status_code=302)

    @app.get(PREFIX + "/auth/google/callback")
    def google_callback(request: Request, code: str = Query(""),
                        state: str = Query("")):
        base = accounts.config.get("return_base") or str(request.base_url).rstrip("/")
        return accounts.google_callback(code, state, base)

    @app.post(PREFIX + "/auth/email/code")
    def email_code(req: EmailCodeRequest, request: Request):
        return accounts.send_email_code(req.email, client_ip(request))

    @app.post(PREFIX + "/auth/email/verify")
    def email_verify(req: EmailVerifyRequest):
        return accounts.verify_email_code(req.email, req.code)

    @app.post(PREFIX + "/auth/session/exchange")
    def session_exchange(req: ExchangeRequest):
        return accounts.exchange(req.code)

    @app.get(PREFIX + "/auth/account")
    def account_me(token=Depends(bearer)):
        return {"account": accounts.session(token)}

    @app.post(PREFIX + "/auth/account/signout")
    def account_signout(token=Depends(bearer)):
        return accounts.signout(token)

    @app.post(PREFIX + "/auth/account/link-vikunja")
    def account_link(req: LinkVikunjaRequest, token=Depends(bearer)):
        account = accounts.session(token)
        return accounts.link_vikunja(account["id"], req.token, service.human)

    # 自有账号登录后领取底座会话：底座自动开通对用户不可见，见 Service.ensure_vikunja_session
    @app.post(PREFIX + "/auth/account/vikunja-session")
    def account_vikunja_session(token=Depends(bearer)):
        account = accounts.session(token)
        return service.ensure_vikunja_session(account)

    # -- 管理后台（ADMIN-CONSOLE-01）：三级角色与账号管理 -------------------
    # 会话仍用 ddacc_ 令牌；权限矩阵在 AdminConsole 内校验，前端只做展示。

    @app.get(PREFIX + "/admin/me")
    def admin_me(token=Depends(bearer)):
        return admin.me(accounts.session(token))

    @app.get(PREFIX + "/admin/users")
    def admin_users(token=Depends(bearer)):
        return admin.list_users(accounts.session(token))

    @app.patch(PREFIX + "/admin/users/{account_id}")
    def admin_update(account_id: str, req: AdminUserUpdate, token=Depends(bearer)):
        return admin.update(accounts.session(token), account_id, req)

    @app.delete(PREFIX + "/admin/users/{account_id}")
    def admin_delete(account_id: str, token=Depends(bearer)):
        return admin.delete(accounts.session(token), account_id)

    # -- 送达事件入口（Resend webhook）-------------------------------------
    # 只接受带合法 Svix 签名的请求；未配置密钥时明确 503，不做无签名降级。
    @app.post(PREFIX + "/webhooks/resend")
    async def resend_webhook(request: Request):
        secret = accounts.config.get("resend_webhook_secret") or ""
        if not secret:
            fail(503, "webhook_not_configured")
        raw = await request.body()
        if len(raw) > 256 * 1024:
            fail(413, "webhook_payload_too_large")
        if not verify_signature(
                secret, request.headers.get("svix-id", ""),
                request.headers.get("svix-timestamp", ""),
                request.headers.get("svix-signature", ""), raw):
            fail(401, "webhook_signature_invalid")
        try:
            payload = parse_body(raw)
        except ValueError:
            fail(400, "webhook_payload_invalid")
        return accounts.ingest_mail_event(payload)


    @app.put(PREFIX + "/agent/client")
    def identify_client(req: ClientIdentity, token=Depends(bearer)):
        return service.identify_client(token, req.name)

    @app.delete(PREFIX + "/connections/{connection_id}")
    def disconnect(connection_id: str, auth=Depends(human)):
        return service.disconnect(auth[0]["id"], connection_id)

    @app.get(PREFIX + "/agent/status")
    def status(token=Depends(bearer)):
        with store.lock:
            con, _ = service.connection(token)
            return {"connection": service.safe_connection(con), "status": "authorized",
                    "onlineExecution": "not_observed"}

    @app.post(PREFIX + "/actions")
    def actions(req: Action, auth=Depends(human)):
        return service.task_action(*auth, req)

    @app.get(PREFIX + "/actions/{action_id}")
    def action_status(action_id: str, auth=Depends(human)):
        user, token = auth
        with store.lock:
            key = f"human:{user['id']}:{action_id}"
            record = store.get("actions", key)
            if not record:
                fail(404, "action_not_found")
            access = store.get("action_access", f"{user['id']}:{action_id}")
            if access:
                service.project(token, access["projectId"])
            service.reconcile_action(key, record)
            return service.action_result(record)

    def decorate(task):
        return service.decorate(task)

    @app.get(PREFIX + "/agent/context")
    def agent_context(token=Depends(bearer)):
        return service.agent_context(token)

    @app.get(PREFIX + "/agent/capabilities")
    def agent_capabilities(token=Depends(bearer)):
        return service.agent_capabilities(token)

    @app.get(PREFIX + "/agent/tasks")
    def agent_tasks(projectId: int | None = Query(None, gt=0), q: str = Query("", max_length=500),
                    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                    token=Depends(bearer)):
        return service.agent_tasks(token, projectId, q, page, per_page)

    @app.get(PREFIX + "/agent/tasks/{task_id}")
    def agent_task(task_id: int, token=Depends(bearer)):
        with store.lock:
            con, _, task = service.agent_task(token, task_id)
            return service.decorate(task, con)

    @app.post(PREFIX + "/agent/actions")
    def agent_actions(req: Action, token=Depends(bearer)):
        return service.agent_actions(token, req)

    @app.get(PREFIX + "/agent/actions/{action_id}")
    def agent_action_status(action_id: str, token=Depends(bearer)):
        return service.agent_action_status(token, action_id)

    @app.post(PREFIX + "/agent/tasks/{task_id}/execution")
    def agent_execution(task_id: int, req: Execution, token=Depends(bearer)):
        return service.execute(token, task_id, req, require_version=True)

    @app.get(PREFIX + "/agent/projects/{project_id}")
    def agent_project(project_id: int, token=Depends(bearer)):
        return service.agent_project(token, project_id)

    @app.get(PREFIX + "/agent/projects/{project_id}/members")
    def agent_members(project_id: int, token=Depends(bearer)):
        try:
            return service.agent_members(token, project_id)
        except UpstreamFailure as exc:
            if exc.status in (401, 403):
                fail(403, "capability_unavailable")
            raise

    @app.get(PREFIX + "/agent/history")
    def agent_history(projectId: int | None = Query(None, gt=0), taskId: int | None = Query(None, gt=0),
                      page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                      token=Depends(bearer)):
        return service.agent_history(token, projectId, taskId, page, per_page)

    @app.get(PREFIX + "/agent/sync")
    def agent_sync(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), token=Depends(bearer)):
        return service.agent_sync(token, cursor, limit)

    @app.get(PREFIX + "/tasks/{task_id}")
    def get_task(task_id: int, token=Depends(bearer)):
        with store.lock:
            if token.startswith("dd_"):
                _, upstream_token = service.connection(token)
                task = upstream.call(upstream_token, "GET", f"/tasks/{task_id}")
                service.connection(token, project=task["project_id"])
            else:
                service.human(token)
                task = upstream.call(token, "GET", f"/tasks/{task_id}")
                service.project(token, task["project_id"])
            return decorate(task)

    @app.get(PREFIX + "/projects")
    def projects(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100), auth=Depends(human)):
        return upstream.call(auth[1], "GET", "/projects", params={"page": page, "per_page": per_page})

    @app.get(PREFIX + "/tasks")
    @app.get(PREFIX + "/history")
    def history(projectId: int = Query(..., gt=0), page: int = Query(1, ge=1),
                per_page: int = Query(50, ge=1, le=100), q: str = Query("", max_length=500),
                token=Depends(bearer)):
        with store.lock:
            if token.startswith("dd_"):
                _, token = service.connection(token, project=projectId)
            else:
                service.human(token)
                service.project(token, projectId)
            data = upstream.call(token, "GET", f"/projects/{projectId}/tasks",
                                 params={"page": page, "per_page": per_page, "q": q})
            return {**data, "items": [decorate(t) for t in data.get("items", [])]}

    @app.get(PREFIX + "/meta")
    def meta(projectId: int = Query(..., gt=0), page: int = Query(1, ge=1),
             per_page: int = Query(50, ge=1, le=100), auth=Depends(human)):
        service.project(auth[1], projectId)
        data = upstream.call(auth[1], "GET", f"/projects/{projectId}/tasks",
                             params={"page": page, "per_page": per_page})
        return {**data, "items": [{"taskId": t["id"], **service.task_meta(t["id"])}
                                for t in data.get("items", [])]}

    @app.post(PREFIX + "/tasks/{task_id}/execution")
    def execute(task_id: int, req: Execution, token=Depends(bearer)):
        return service.execute(token, task_id, req)

    @app.get(PREFIX + "/agent/events")
    def events(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), token=Depends(bearer)):
        return service.events(token, cursor, limit)

    @app.post(PREFIX + "/agent/ack")
    def ack(req: Ack, token=Depends(bearer)):
        return service.ack(token, req.eventIds)

    @app.get(PREFIX + "/notices")
    def notices(cursor: str | None = None, limit: int = Query(50, ge=1, le=100), auth=Depends(human)):
        with store.lock:
            return service.notices(*auth, cursor, limit)

    @app.post(PREFIX + "/notices/ack")
    def notice_ack(req: Ack, auth=Depends(human)):
        user, token = auth
        with store.lock:
            for eid in req.eventIds:
                row = store.db.execute("SELECT project FROM events WHERE id=? AND owner=?",
                                       (eid, user["id"])).fetchone()
                if not row:
                    fail(404, "notice_not_found")
                service.project(token, row["project"])
            with store.transaction():
                for eid in req.eventIds:
                    store.db.execute("UPDATE human_notices SET acked=1 WHERE owner=? AND event=?",
                                     (user["id"], eid))
        return {"acked": req.eventIds}

    @app.get(PREFIX + "/preferences")
    def get_preferences(auth=Depends(human)):
        return store.get("preferences", auth[0]["id"]) or Preferences().model_dump()

    @app.put(PREFIX + "/preferences")
    def preferences(req: Preferences, auth=Depends(human)):
        store.put("preferences", auth[0]["id"], req.model_dump())
        return req

    return app


def factory():
    return create_app()

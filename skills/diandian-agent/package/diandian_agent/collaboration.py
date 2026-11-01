"""Project-scoped collaboration, durable provenance and independent sync."""

import json
import secrets

from .models import Preferences
from .upstream import UpstreamFailure


def public_person(person):
    return {k: person[k] for k in ("id", "username", "name", "bot_owner_id", "status")
            if k in person}


class Collaboration:
    def actor_snapshot(self, user, con=None):
        return {"kind": "agent" if con else "human", "userId": user["id"],
                "connectionId": con["id"] if con else None,
                "displayName": con["name"] if con else user.get("name", ""),
                "clientNameAtCreation": con["name"] if con else user.get("name", ""),
                "createdAt": self.clock()}

    def authorize_manage(self, user, token, space):
        """Only invoked by a new, explicit human manage consent."""
        from datetime import datetime, timezone
        from .service import fail

        self.project(token, space["projectId"], admin=True, write=True)
        bot = self.upstream.call(token, "GET", f"/user/bots/{space['botId']}")
        if bot.get("bot_owner_id") != user["id"] or bot.get("status", 0) != 0:
            fail(403, "bot_not_active_or_owned")
        # Discover the native permission names, not a token-all approximation.
        routes = self.upstream.call(token, "GET", "/routes")
        required = {"projects": ["read_one", "update"],
                    "tasks": ["read_all", "read_one", "create", "update", "delete"],
                    "tasks_assignees": ["read_all", "create", "delete"],
                    "tasks_labels": ["create", "delete"],
                    "tasks_relations": ["create"],
                    "projects_users": ["read_all"], "labels": ["read_all"]}
        if not isinstance(routes, dict) or any(
                permission not in routes.get(group, {}) for group, permissions in required.items()
                for permission in permissions):
            fail(409, "manage_native_routes_unavailable")
        if space.get("manageTokenExpires", 0) <= self.clock():
            expires = self.clock() + 90 * 86400
            raw = self.upstream.call(token, "POST", "/tokens", {
                "title": "Diandian scoped management Bot", "owner_id": space["botId"],
                "permissions": required,
                "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()})
            space.update(encryptedManageToken=self.crypto.encrypt(raw["token"].encode()).decode(),
                         manageTokenId=raw["id"], manageTokenExpires=expires)
        space["publicOwner"] = public_person(user)
        space["publicBot"] = public_person(bot)
        self.store.put("spaces", space["id"], space)
        self.project(self.manage_token(space), space["projectId"], write=True)

    def manage_token(self, space):
        from .service import fail
        if space.get("manageTokenExpires", 0) <= self.clock() or not space.get("encryptedManageToken"):
            fail(401, "upstream_manage_credential_expired")
        return self.crypto.decrypt(space["encryptedManageToken"].encode()).decode()

    def source(self, tid, snapshot):
        with self.store.transaction():
            if not self.store.get("sources", tid):
                self.store.put("sources", tid, snapshot)
        return {"id": tid}

    def object_version(self, obj, kind="task"):
        from .service import digest
        key = f"{kind}:{obj['id']}"
        content = {k: obj.get(k) for k in (
            "id", "title", "description", "project_id", "done", "done_at", "priority",
            "due_date", "start_date", "end_date", "repeat_after", "repeat_mode",
            "hex_color", "percent_done", "created", "updated", "is_archived")}
        if kind == "task":
            for field in ("assignees", "labels"):
                content[field] = sorted(item["id"] for item in obj.get(field) or [])
            content["related_tasks"] = {
                relation: sorted(item["id"] for item in items or [])
                for relation, items in (obj.get("related_tasks") or {}).items()}
            content["diandianMeta"] = self.task_meta(obj["id"])
        fingerprint = digest(json.dumps(content, sort_keys=True, separators=(",", ":")))
        old = self.store.get("versions", key)
        if not old or old["fingerprint"] != fingerprint or old.get("deleted"):
            record = {"fingerprint": fingerprint, "revision": (old or {}).get("revision", 0) + 1,
                      "projectId": obj["project_id"] if kind == "task" else obj["id"]}
            record["version"] = f"{record['revision']}-{fingerprint[:24]}"
            self.store.put("versions", key, record)
        return self.store.get("versions", key)["version"]

    def check_version(self, obj, expected, kind="task"):
        from .service import fail
        if not expected:
            fail(422, "if_version_required")
        if self.object_version(obj, kind) != expected:
            fail(409, "version_conflict")

    def decorate(self, task, con=None):
        meta = self.task_meta(task["id"])
        creator = self.store.get("sources", task["id"])
        pending = False
        if not creator:
            # A known create checkpoint is authoritative even before provenance repair.
            for rec in self.store.all("actions"):
                if any(step.get("result", {}).get("id") == task["id"]
                       for name, step in rec["steps"].items()
                       if name == "parent" or name.startswith("child:") and name.count(":") == 1):
                    if rec.get("source"):
                        creator, pending = rec["source"], True
                        break
        if not creator:
            native = task.get("created_by") or {}
            creator = {"kind": "unknown" if native.get("bot_owner_id") or not native.get("id") else "human",
                       "userId": native.get("bot_owner_id") or native.get("id"),
                       "connectionId": None, "clientNameAtCreation": native.get("name", ""),
                       "createdAt": task.get("created"), "source": "upstream"}
        version = self.object_version(task)
        modifier = self.store.get("modifiers", task["id"])
        if modifier and modifier.get("version") != version:
            modifier = {"kind": "unknown", "connectionId": None, "source": "upstream"}
        modifier = modifier or creator
        cid = con["id"] if con else None
        return {"task": task, "meta": meta,
                "conflict": "external_done" if task.get("done") and meta["state"] in ("doing", "review") else None,
                "creator": creator, "lastModifiedBy": modifier, "version": version,
                "sourceStatus": "pending" if pending else "recorded" if self.store.get("sources", task["id"]) else "upstream",
                "createdByCurrentConnection": bool(cid and creator.get("connectionId") == cid),
                "modifiedByCurrentConnection": bool(cid and modifier.get("connectionId") == cid)}

    def record_change(self, token, pid, tid, actor, action_id, operation, fields, deleted=False):
        from .service import digest
        change_id = digest(f"{actor['kind']}:{actor.get('connectionId') or actor['userId']}:{action_id}:{tid}:{operation}")
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM changes WHERE id=?", (change_id,)).fetchone():
                return {"id": tid}
            obj = None if deleted else self.upstream.call(token, "GET", f"/tasks/{tid}")
            with self.store.transaction():
                if deleted:
                    previous = self.store.get("versions", f"task:{tid}") or {}
                    revision = previous.get("revision", 0) + 1
                    version = f"{revision}-deleted"
                    self.store.put("versions", f"task:{tid}", {
                        **previous, "projectId": pid, "revision": revision, "version": version, "deleted": True})
                else:
                    version = self.object_version(obj)
                modifier = {**actor, "actionId": action_id, "modifiedAt": self.clock(), "version": version}
                self.store.put("modifiers", tid, modifier)
                payload = {"type": "task.deleted" if deleted else "task.changed", "taskId": tid,
                           "projectId": pid, "version": version, "actionId": action_id,
                           "operation": operation, "actor": actor, "fields": fields,
                           "createdAt": self.clock()}
                if not deleted:
                    payload["object"] = self.decorate(obj)
                self.store.db.execute("INSERT INTO changes(id,project,payload) VALUES (?,?,?)",
                                      (change_id, pid, json.dumps(payload, sort_keys=True)))
        return {"id": tid, "version": version}

    def record_project_change(self, obj, actor, action_id, operation):
        from .service import digest
        change_id = digest(f"{actor['kind']}:{actor.get('connectionId') or actor['userId']}:{action_id}:project")
        with self.store.transaction():
            version = self.object_version(obj, "project")
            payload = {"type": "project.changed", "projectId": obj["id"], "version": version,
                       "actionId": action_id, "operation": operation, "actor": actor,
                       "fields": ["title", "description", "is_archived"], "createdAt": self.clock(),
                       "project": obj}
            self.store.db.execute("INSERT OR IGNORE INTO changes(id,project,payload) VALUES (?,?,?)",
                                  (change_id, obj["id"], json.dumps(payload, sort_keys=True)))
        return {"id": obj["id"], "version": version}

    def agent_task(self, credential, tid, scope="read"):
        con, token = self.connection(credential, scope)
        task = self.upstream.call(token, "GET", f"/tasks/{tid}")
        self.connection(credential, scope, task["project_id"])
        return con, token, task

    def agent_actions(self, credential, req):
        from .service import agent_action_request, fail
        with self.store.lock:
            con, token = self.connection(credential, "manage", req.projectId)
            if req.operation == "project.create":
                fail(403, "account_scope_required")
            permitted = {
                "task.create": {"projectId", "draft"}, "event.create": {"projectId", "draft", "children"},
                "task.update": {"projectId", "taskId", "title", "description", "priority", "meta", "done", "labelIds"},
                "event.update": {"projectId", "taskId", "title", "description"},
                "task.delete": {"projectId", "taskId"},
                "assignee.add": {"projectId", "taskId", "userId"},
                "assignee.remove": {"projectId", "taskId", "userId"},
                "label.add": {"projectId", "taskId", "labelId"},
                "label.remove": {"projectId", "taskId", "labelId"},
                "project.update": {"projectId", "title", "description"},
                "project.archive": {"projectId", "archived"}}
            if req.model_fields_set - (permitted[req.operation] | {"actionId", "operation", "ifVersion", "dryRun"}):
                fail(422, "operation_fields_not_allowed")
            actor = f"agent:{con['id']}"
            rec = self.store.get("actions", f"{actor}:{req.actionId}")
            if rec:
                access = self.store.get("agent_action_access", f"{con['id']}:{req.actionId}")
                if access:
                    self.connection(credential, "manage", access["projectId"])
                if rec["request"] != agent_action_request(req):
                    fail(409, "action_id_conflict")
                self.reconcile_action(f"{actor}:{req.actionId}", rec)
                if rec["status"] in ("complete", "unknown"):
                    return self.action_result(rec)
                if not req.ifVersion and req.operation not in ("task.create", "event.create"):
                    fail(422, "if_version_required")
                self.check_retry_versions(token, rec)
                if req.operation == "task.delete" and rec["steps"].get("delete", {}).get("status") == "done":
                    return self.task_action({"id": con["owner"], "botActor": True}, token, req, con=con)
            if req.taskId:
                con, token, task = self.agent_task(credential, req.taskId, "manage")
                pid = task["project_id"]
                if req.projectId and req.projectId != pid:
                    fail(403, "project_mismatch")
                # Known partial retries resume checkpointed steps, not a second mutation.
                if not rec:
                    self.check_version(task, req.ifVersion)
                self.project(token, pid, write=True)
                meta = self.task_meta(req.taskId)
                if meta["kind"] == "event" and "done" in req.model_fields_set:
                    fail(409, "event_completion_not_defined")
                if req.operation == "event.update" and meta["kind"] != "event":
                    fail(422, "event_required")
                if req.operation == "task.delete" and (
                        meta["kind"] == "event" or task.get("related_tasks", {}).get("subtask")
                        or any(m.get("event") == req.taskId for m in self.store.all("meta"))):
                    fail(409, "event_delete_not_supported")
                if meta["state"] == "review" and (req.operation == "task.delete" or req.done
                        or req.meta and req.meta.state != "review"):
                    fail(409, "human_confirmation_not_defined")
            else:
                pid = req.projectId
                obj = self.project(token, pid, write=True, allow_archive=req.operation == "project.archive")
                if req.operation.startswith("project.") and not rec:
                    self.check_version(obj, req.ifVersion, "project")
            self.agent_preflight(con, token, req, pid)
            if req.dryRun:
                return {"actionId": req.actionId, "status": "dry_run", "projectId": pid,
                        "operation": req.operation, "request": req.model_dump(mode="json")}
            self.store.put("agent_action_access", f"{con['id']}:{req.actionId}", {"projectId": pid})
            return self.task_action({"id": con["owner"], "botActor": True}, token, req, con=con)

    def check_retry_versions(self, token, rec):
        from .service import fail
        for key, expected in rec.get("retryVersions", {}).items():
            kind, oid = key.split(":")
            try:
                obj = self.upstream.call(token, "GET", f"/{'tasks' if kind == 'task' else 'projects'}/{oid}")
            except UpstreamFailure as exc:
                if exc.status == 404:
                    fail(409, "version_conflict")
                raise
            if self.object_version(obj, kind) != expected:
                fail(409, "version_conflict")

    def agent_preflight(self, con, token, req, pid):
        from .service import fail
        if req.operation in ("task.update", "event.update"):
            task = self.upstream.call(token, "GET", f"/tasks/{req.taskId}")
            old_meta = self.task_meta(req.taskId)
            meta = {**old_meta, **(req.meta.model_dump(mode="json", exclude_unset=True) if req.meta else {})}
            if meta["event"] != old_meta["event"] or meta["kind"] != old_meta["kind"]:
                fail(409, "event_membership_change_not_supported")
            if task.get("done") and meta["state"] in ("doing", "review"):
                fail(409, "external_done_conflict")
            if old_meta["state"] != meta["state"]:
                fail(403, "execution_scope_required")
        if req.operation == "assignee.add":
            self.validate_assignee({"id": con["owner"], "botActor": True}, token, pid, req.userId)
        drafts = ([req.draft] + req.children) if req.draft else []
        space = self.store.get("spaces", con["spaceId"])
        for index, draft in enumerate(drafts):
            if draft.meta.state != "todo":
                fail(403, "execution_scope_required")
            if draft.meta.kind != "task" and not (
                    req.operation == "event.create" and index == 0 and draft.meta.kind == "event"):
                fail(422, "event_operation_required")
            if req.operation == "event.create" and draft.meta.event is not None:
                fail(422, "nested_event_not_supported")
            if draft.meta.event:
                self.validate_event(token, pid, draft.meta.event)
            ids = list(dict.fromkeys(draft.assigneeIds))
            if draft.intent == "self" and space["mode"] == "daily":
                if any(uid != con["owner"] for uid in ids):
                    fail(422, "self_intent_must_assign_human")
                ids = [con["owner"]]
            if draft.intent == "agent" and not ids:
                ids = [con["botId"]]
            bot_flags = [self.validate_assignee({"id": con["owner"], "botActor": True}, token, pid, uid) for uid in ids]
            if draft.intent == "agent" and not any(bot_flags):
                fail(422, "agent_intent_requires_bot")
        label_ids = set(req.labelIds or []) | ({req.labelId} if req.operation == "label.add" else set())
        for draft in drafts:
            label_ids.update(draft.labelIds)
        if label_ids:
            visible = {label["id"] for label in self.upstream.all(token, "/labels")}
            if not label_ids <= visible:
                fail(422, "label_not_discovered")

    def agent_action_status(self, credential, action_id):
        from .service import fail
        with self.store.lock:
            con, _ = self.connection(credential)
            key = f"agent:{con['id']}:{action_id}"
            rec = self.store.get("actions", key)
            if not rec:
                fail(404, "action_not_found")
            access = self.store.get("agent_action_access", f"{con['id']}:{action_id}")
            if access:
                self.connection(credential, project=access["projectId"])
            self.reconcile_action(key, rec)
            return self.action_result(rec)

    def agent_tasks(self, credential, pid=None, q="", page=1, per_page=50):
        with self.store.lock:
            con, token = self.connection(credential, project=pid)
            tasks = []
            for project in [pid] if pid else con["projectIds"]:
                self.connection(credential, project=project)
                tasks.extend(self.upstream.all(token, f"/projects/{project}/tasks"))
            tasks = sorted((t for t in tasks if q.lower() in (
                t.get("title", "") + "\n" + t.get("description", "")).lower()), key=lambda t: t["id"])
            total = len(tasks)
            return {"items": [self.decorate(t, con) for t in tasks[(page - 1) * per_page:page * per_page]],
                    "page": page, "per_page": per_page, "pageSize": per_page, "total": total,
                    "total_pages": max(1, (total + per_page - 1) // per_page)}

    def agent_project(self, credential, pid):
        with self.store.lock:
            _, token = self.connection(credential, project=pid)
            obj = self.project(token, pid)
            return {**obj, "version": self.object_version(obj, "project")}

    def agent_members(self, credential, pid):
        with self.store.lock:
            _, token = self.connection(credential, project=pid)
            return {"items": [public_person(p) for p in self.upstream.all(token, f"/projects/{pid}/users")]}

    def agent_context(self, credential):
        with self.store.lock:
            con, token = self.connection(credential)
            space = self.store.get("spaces", con["spaceId"])
            people = {}
            for person in (space.get("publicOwner"), space.get("publicBot")):
                if person:
                    people[person["id"]] = public_person(person)
            projects, labels, unavailable = [], {}, []
            for pid in con["projectIds"]:
                projects.append(self.agent_project(credential, pid))
                for task in self.upstream.all(token, f"/projects/{pid}/tasks"):
                    for person in [task.get("created_by")] + (task.get("assignees") or []):
                        if person:
                            people[person["id"]] = public_person(person)
                    for label in task.get("labels") or []:
                        labels[label["id"]] = {k: label[k] for k in ("id", "title", "hex_color") if k in label}
                try:
                    for person in self.agent_members(credential, pid)["items"]:
                        people[person["id"]] = person
                except UpstreamFailure as exc:
                    if exc.status not in (401, 403, 404):
                        raise
                    unavailable.append("project.members")
            try:
                for label in self.upstream.all(token, "/labels"):
                    labels[label["id"]] = {k: label[k] for k in ("id", "title", "hex_color") if k in label}
            except UpstreamFailure as exc:
                if exc.status not in (401, 403, 404):
                    raise
                unavailable.append("label.list")
            preferences = self.store.get("preferences", con["owner"]) or Preferences().model_dump()
            peers = [{**{key: c[key] for key in ("id", "name", "owner", "botId")},
                      "projectIds": sorted(set(c["projectIds"]) & set(con["projectIds"]))}
                     for c in self.store.all("connections")
                     if not c["revoked"] and c["expiresAt"] > self.clock()
                     and set(c["projectIds"]) & set(con["projectIds"])]
            return {"connection": self.safe_connection(con), "connections": peers,
                    "spaces": [self.safe_space(space)], "projects": projects,
                    "people": list(people.values()), "labels": list(labels.values()),
                    "preferences": {"name": preferences["name"], "timezone": preferences["timezone"],
                                    "timezoneConfigured": preferences["timezone"] not in ("local", "", "unknown")},
                    "unavailable": unavailable}

    def agent_history(self, credential, pid=None, tid=None, page=1, per_page=50):
        with self.store.lock:
            con, _ = self.connection(credential, project=pid)
            projects = [pid] if pid else con["projectIds"]
            rows = self.store.db.execute("SELECT seq,project,payload FROM changes ORDER BY seq DESC").fetchall()
            items = [dict(seq=r["seq"], **json.loads(r["payload"])) for r in rows if r["project"] in projects]
            if tid:
                items = [i for i in items if i.get("taskId") == tid]
            return {"items": items[(page - 1) * per_page:page * per_page], "total": len(items),
                    "page": page, "per_page": per_page, "pageSize": per_page,
                    "total_pages": max(1, (len(items) + per_page - 1) // per_page)}

    def sync_identity(self, con):
        from .service import digest
        return digest(json.dumps({k: con[k] for k in
                                  ("id", "spaceId", "projectIds", "scopes", "expiresAt")}, sort_keys=True))

    def sync_cursor(self, data, identity):
        return self.crypto.encrypt(json.dumps({**data, "identity": identity, "syncVersion": 1}).encode()).decode()

    def agent_sync(self, credential, cursor=None, limit=50):
        from .service import fail
        with self.store.lock:
            con, _ = self.connection(credential)
            identity = self.sync_identity(con)
            if cursor:
                try:
                    data = json.loads(self.crypto.decrypt(cursor.encode()))
                    if data["identity"] != identity or data["syncVersion"] != 1 or data["kind"] not in ("page", "resume", "changes_page"):
                        raise ValueError()
                except Exception:
                    fail(422, "invalid_sync_cursor")
            else:
                watermark = self.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0]
                snapshot_id = secrets.token_urlsafe(24)
                _, token = self.connection(credential)
                tasks = []
                for pid in con["projectIds"]:
                    tasks.extend(self.decorate(t, con) for t in self.upstream.all(
                        token, f"/projects/{pid}/tasks"))
                tasks.sort(key=lambda t: t["task"]["id"])
                items = [{"type": "task.snapshot", "taskId": t["task"]["id"],
                          "projectId": t["task"]["project_id"], "version": t["version"], "object": t}
                         for t in tasks]
                self.store.put("sync_snapshots", snapshot_id, {"identity": identity, "items": items,
                                                             "watermark": watermark, "expiresAt": self.clock() + 3600})
                data = {"kind": "page", "snapshot": snapshot_id, "offset": 0}
            if data["kind"] == "page":
                snapshot = self.store.get("sync_snapshots", data.get("snapshot"))
                if not snapshot or snapshot["identity"] != identity or snapshot["expiresAt"] <= self.clock():
                    fail(410, "sync_snapshot_expired")
                offset = data.get("offset")
                if not isinstance(offset, int) or offset < 0 or offset > len(snapshot["items"]):
                    fail(422, "invalid_sync_cursor")
                items = snapshot["items"][offset:offset + limit]
                next_cursor = self.sync_cursor({**data, "offset": offset + limit}, identity) if offset + limit < len(snapshot["items"]) else None
                watermark = snapshot["watermark"]
                resume = self.sync_cursor({"kind": "resume", "seq": watermark}, identity)
                return {"items": items, "nextCursor": next_cursor, "resumeCursor": resume,
                        "watermark": watermark, "phase": "snapshot"}
            seq = data.get("seq")
            if not isinstance(seq, int) or seq < 0:
                fail(422, "invalid_sync_cursor")
            high = data.get("high") if data["kind"] == "changes_page" else self.store.db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0]
            if not isinstance(high, int) or high < seq:
                fail(422, "invalid_sync_cursor")
            rows = self.store.db.execute(
                "SELECT seq,project,payload FROM changes WHERE seq>? AND seq<=? ORDER BY seq", (seq, high)).fetchall()
            visible = [r for r in rows if r["project"] in con["projectIds"]]
            page = visible[:limit]
            position = page[-1]["seq"] if len(visible) > limit else high
            resume = self.sync_cursor({"kind": "resume", "seq": position}, identity)
            items = [json.loads(r["payload"]) for r in page]
            for item in items:
                if item.get("object"):
                    obj = item["object"]
                    obj["createdByCurrentConnection"] = obj["creator"].get("connectionId") == con["id"]
                    obj["modifiedByCurrentConnection"] = obj["lastModifiedBy"].get("connectionId") == con["id"]
            next_cursor = self.sync_cursor({"kind": "changes_page", "seq": position, "high": high}, identity)
            return {"items": items, "nextCursor": next_cursor if len(visible) > limit else None,
                    "resumeCursor": resume, "watermark": position, "phase": "changes"}

    def agent_capabilities(self, credential):
        with self.store.lock:
            con, token = self.connection(credential)
            unavailable = set()
            for name, path in [("label.list", "/labels")] + [
                    ("project.member.list", f"/projects/{pid}/users") for pid in con["projectIds"]]:
                try:
                    self.upstream.call(token, "GET", path, params={"page": 1, "per_page": 1})
                except UpstreamFailure as exc:
                    if exc.status not in (401, 403, 404):
                        raise
                    unavailable.add(name)
            read = ["auth.permissions", "space.list", "space.get", "connection.list", "connection.get",
                    "person.list", "person.get", "label.list", "preference.get",
                    "task.list", "task.get", "task.search", "task.summary", "task.+agenda",
                    "event.list", "event.search", "event.get", "project.list", "project.get",
                    "project.search", "project.member.list", "history.list", "sync.pull", "sync.watch",
                    "notification.list", "notification.ack", "action.get", "action.resume",
                    "capability.list", "capability.get", "schema", "version"]
            manage = ["task.create", "task.update", "task.schedule", "task.assign", "task.unassign",
                      "task.complete", "task.reopen", "task.delete", "event.create", "event.update",
                      "project.update", "project.archive", "project.unarchive", "task.+create-personal", "event.+plan"]
            blocked = {"project.create": "account_scope_required", "label.create": "account_scope_required",
                       "label.update": "account_scope_required", "label.delete": "account_scope_required",
                       "event.delete": "policy_not_ready", "event.restore": "policy_not_ready",
                       "task.restore": "policy_not_ready", "review.approve": "policy_not_ready"}
            items = []
            for name in read + manage + ["task.execution"] + list(blocked):
                scopes = ["manage"] if name in manage else ["execute"] if name == "task.execution" else ["read"]
                reason = blocked.get(name) or ("scope_required" if not set(scopes) <= set(con["scopes"]) else
                    "capability_unavailable" if name in unavailable else None)
                items.append({"name": name, "id": name, "domain": name.split(".")[0], "action": ".".join(name.split(".")[1:]),
                              "serverSupported": name not in blocked, "supported": name not in blocked,
                              "policyReady": name not in blocked, "requiredScopes": scopes,
                              "grantedScopes": con["scopes"], "available": reason is None,
                              "maturity": "blocked" if name in blocked else "supported", "reason": reason,
                              "nativeRouteAvailable": name not in unavailable})
            return {"cliVersion": "0.2.0", "apiVersion": "0.2.0", "schemaVersion": 2, "items": items,
                    "limitations": ["legacy_execution_without_version", "upstream_writes_not_in_incremental_sync",
                                    "upstream_put_not_cross_service_atomic", "no_exclusive_execution_claim"]}

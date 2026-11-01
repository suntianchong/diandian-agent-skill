"""Collaboration CLI parsing, projections, versioned writes and safe recovery."""

import argparse
import copy
import json
import math
import re
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .capabilities import ACTION_FIELDS, API_VERSION, CLI_VERSION, DRAFT_FIELDS, EXTRA_PROFILES, FIELDS, META_FIELDS
from .capabilities import QUERY, REGISTRY, SCHEMA_VERSION
from .capabilities import local_schema, merge_capabilities

PREFIX = "/api/diandian/v1"


class CLIError(Exception):
    def __init__(self, code, message, retry_safe=False, action_id=None, preview=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.retry_safe, self.action_id = retry_safe, action_id
        self.preview = preview
        self.progress = None

    def payload(self):
        data = {"code": self.code, "message": self.message,
                "retrySafe": self.retry_safe, "actionId": self.action_id}
        if self.preview is not None:
            data["preview"] = self.preview
        if self.progress is not None:
            data.update(self.progress)
        return data


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError("invalid_parameters", message)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def finite_positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def iso_date(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        date.fromisoformat(value)
        return value
    except ValueError:
        raise argparse.ArgumentTypeError("expected ISO date YYYY-MM-DD")


def calendar_time(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("expected HH:mm")
    return value


def option(parser, name):
    flag = "--" + name.replace("_", "-")
    kwargs = {"default": None}
    if name in ("page", "page_size", "project", "event", "assignee_id", "label_id", "limit"):
        kwargs["type"] = positive
    if name in ("assignee_id", "label_id"):
        kwargs["action"] = "append"
    if name in ("date", "date_from", "date_to", "overdue_before", "completed_from", "completed_to"):
        kwargs["type"] = iso_date
    if name == "time":
        kwargs["type"] = calendar_time
    if name == "priority":
        kwargs.update(type=int, choices=range(6))
    if name in ("all", "undated", "clear_date", "clear_time", "clear_labels",
                "dry_run", "yes", "offline", "include_overdue", "include_undated", "include_done"):
        kwargs["action"] = "store_true"
    if name == "state":
        kwargs["choices"] = ("todo", "doing", "review", "done")
    if name == "intent":
        kwargs["choices"] = ("self", "agent")
    if name == "archived":
        kwargs["choices"] = ("active", "archived", "all")
    if name in ("interval", "timeout"):
        kwargs["type"] = finite_positive
    parser.add_argument(flag, **kwargs)


def common(parser):
    parser.add_argument("--format", choices=("json", "table"), default=argparse.SUPPRESS)
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--profile", default=argparse.SUPPRESS)


def register_parsers(sub, auth_sub, history_sub):
    domains = {"auth": auth_sub, "history": history_sub}
    project_members = None
    for key, spec in REGISTRY.items():
        domain, action = spec["domain"], spec["action"]
        if domain not in domains:
            root = sub.add_parser(domain, help=f"{domain} collaboration commands")
            common(root)
            domains[domain] = root.add_subparsers(dest=f"{domain}_command", required=True)
        target = domains[domain]
        if action == "member.list":
            member = target.add_parser("member", help="visible project members; not member management")
            common(member)
            project_members = member.add_subparsers(dest="member_command", required=True)
            target, action = project_members, "list"
        parser = target.add_parser(action, help=spec.get("reason") or key)
        common(parser)
        parser.set_defaults(collaboration_key=key)
        if spec["positional"]:
            name = spec["positional"]
            parser.add_argument(name, **({"nargs": "*"} if name == "event_ids" else
                                        {"type": positive} if name == "id" else {}))
        names = []
        for profile in spec["profiles"]:
            names.extend(QUERY.get(profile, FIELDS.get(profile, EXTRA_PROFILES.get(profile, ()))))
        for name in dict.fromkeys(names):
            option(parser, name)
        if key == "task.execution":
            # List state and execution state intentionally have different enums.
            next(a for a in parser._actions if a.dest == "state").choices = ("doing", "review")
    schema = sub.add_parser("schema", help="local request contract and authenticated capability metadata")
    common(schema)
    schema.add_argument("key", choices=sorted(REGISTRY))
    schema.add_argument("--offline", action="store_true")
    schema.set_defaults(collaboration_key="schema")
    version = sub.add_parser("version", help="CLI/API compatibility versions (offline supported)")
    common(version)
    version.add_argument("--offline", action="store_true")
    version.set_defaults(collaboration_key="version")


def value(args, name, default=None):
    result = getattr(args, name, None)
    return default if result is None else result


def emit(data, output_format="json"):
    if output_format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rows = data.get("items", [data]) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = [rows]
    if not rows:
        print("(empty)")
        return
    flattened = []
    for row in rows:
        if not isinstance(row, dict):
            row = {"value": row}
        flat = dict(row.get("task", {}))
        flat.update({k: v for k, v in row.items() if k != "task"})
        flattened.append(flat)
    columns = list(dict.fromkeys(k for row in flattened for k in row))
    print("\t".join(columns))
    for row in flattened:
        print("\t".join(json.dumps(row.get(k), ensure_ascii=False) for k in columns))


def checked_action_id(action_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", action_id):
        raise CLIError("invalid_parameters", "actionId must be 8..128 letters, digits, underscores or hyphens")
    return action_id


def response_data(response, action_id=None, *, allow_action_state=False):
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        detail = data.get("detail", data) if isinstance(data, dict) else {}
        error = detail.get("error", detail) if isinstance(detail, dict) else {}
        code = error.get("code", f"http_{response.status_code}")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            code = f"http_{response.status_code}"
        # Do not echo raw server messages, URLs or request bodies.
        raise CLIError(code, f"request rejected ({response.status_code}); check permissions and object version",
                       error.get("retrySafe") is True, action_id)
    if data is None:
        raise CLIError("invalid_response", "server returned invalid JSON", action_id=action_id)
    if not allow_action_state and isinstance(data, dict) and data.get("status") in ("partial", "unknown", "failed"):
        error = data.get("error") or {}
        code = error.get("code", "action_" + data["status"])
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            code = "action_" + data["status"]
        raise CLIError(code, f"action {data['status']}; use action get before recovery",
                       error.get("retrySafe") is True and data["status"] == "partial", action_id)
    return data


class Session:
    def __init__(self, client, config, config_path):
        self.client, self.config, self.config_path = client, config, config_path
        self.url = config["server"] + PREFIX + "/agent"
        self.headers = {"Authorization": f"Bearer {config['credential']}"}
        self._context = None
        self._capabilities = None

    def request(self, method, path, *, body=None, params=None, action_id=None, timeout=None):
        try:
            kwargs = {"timeout": timeout} if timeout is not None else {}
            response = self.client.request(method, self.url + path, headers=self.headers,
                                           json=body, params=params, **kwargs)
            return response_data(response, action_id, allow_action_state=method == "GET")
        except httpx.HTTPError:
            raise CLIError("transport_error", "request outcome unavailable; use action get before retry",
                           action_id=action_id) from None

    def context(self):
        if self._context is None:
            self._context = self.request("GET", "/context")
        return self._context

    def capabilities(self):
        if self._capabilities is None:
            self._capabilities = self.request("GET", "/capabilities")
        return self._capabilities

    def require(self, key):
        spec = REGISTRY[key]
        if not spec["policyReady"]:
            raise CLIError("policy_not_ready", spec["reason"])
        context = self.context()
        missing = set(spec["requiredScopes"]) - set(context["connection"].get("scopes", []))
        if missing:
            raise CLIError("scope_required", "requires " + ",".join(sorted(missing)) +
                           "; explicitly reauthorize this space in App")
        try:
            metadata = self.capabilities()
        except CLIError as exc:
            if exc.code == "http_404":
                raise CLIError("capability_unavailable", "legacy API has no collaboration capability contract")
            raise
        capability = next(item for item in merge_capabilities(metadata, context) if item["id"] == key)
        if not capability["available"]:
            raise CLIError(capability["reason"], f"{key} unavailable for this connection")

    def project(self, args, *, all_projects=False):
        context = self.context()
        connection = context["connection"]
        allowed = connection.get("projectIds", [])
        space, project = value(args, "space"), value(args, "project")
        if space is not None and project is not None:
            raise CLIError("invalid_parameters", "--space and --project are mutually exclusive")
        if space is not None:
            matches = [s for s in context.get("spaces", []) if str(s["id"]) == str(space)]
            if len(matches) != 1:
                raise CLIError("scope_required", "space must exactly match one existing authorized space")
            project = matches[0]["projectId"]
        if project is not None:
            if project not in allowed:
                raise CLIError("scope_required", "project is outside this connection's authorization")
            return project
        if all_projects:
            return None
        if len(allowed) != 1:
            raise CLIError("invalid_parameters", "explicit --space or --project required for ambiguous context")
        return allowed[0]

    def pages(self, path, params=None):
        items, page, seen = [], 1, set()
        while True:
            data = self.request("GET", path, params={**(params or {}), "page": page, "per_page": 100})
            batch = data.get("items") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise CLIError("invalid_response", "paginated response must contain items")
            signature = json.dumps(batch, sort_keys=True)
            if batch and signature in seen:
                raise CLIError("invalid_response", "server repeated a page; refusing incomplete results")
            seen.add(signature)
            items.extend(batch)
            total = data.get("total")
            if total is not None:
                if len(items) >= total:
                    break
                if not batch:
                    raise CLIError("invalid_response", "pagination ended before total")
            elif not batch or len(batch) < data.get("pageSize", data.get("per_page", 100)):
                break
            page += 1
        return items

    def task(self, task_id, project=None):
        data = self.request("GET", f"/tasks/{task_id}")
        if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
            raise CLIError("invalid_response", "expected decorated task detail")
        actual = data["task"].get("project_id", data["task"].get("projectId"))
        if actual not in self.context()["connection"].get("projectIds", []):
            raise CLIError("scope_required", "object is outside authorized projects")
        if project is not None and actual != project:
            raise CLIError("scope_required", "object does not belong to the selected project")
        return data

    def store_path(self, action_id):
        return Path(self.config_path).expanduser().parent / "requests" / (checked_action_id(action_id) + ".json")

    def save_request(self, path, body):
        from .cli import read_config, write_config
        from .auth_flow import profile_lock
        target = self.store_path(body["actionId"])
        record = {"server": self.config["server"], "connectionId": self.context()["connection"]["id"],
                  "path": path, "body": body}
        with profile_lock(target):
            if target.exists() or target.is_symlink():
                old = read_config(target)
                if old != record:
                    raise CLIError("action_id_conflict", "actionId already has a different private original request",
                                   action_id=body["actionId"])
            else:
                # Same private-directory and atomic file rules as credentials, but no tokens.
                write_config(target, record)
        return target

    def send_write(self, path, body):
        try:
            self.save_request(path, body)
        except (OSError, ValueError):
            raise CLIError("private_request_store_invalid",
                           "private original request could not be safely saved; no request sent",
                           action_id=body["actionId"]) from None
        data = self.request("POST", path, body=body, action_id=body["actionId"])
        if isinstance(data, dict):
            data.setdefault("actionId", body["actionId"])
        return data


def paginate(items, args):
    page, size = value(args, "page", 1), value(args, "page_size", 50)
    return {"items": items if value(args, "all", False) else items[(page - 1) * size:page * size],
            "total": len(items), "page": page, "pageSize": size}


def assignment_progress(plan):
    steps = plan["steps"]
    failed = [s["actionId"] for s in steps if s["status"] not in ("complete", "pending")]
    return {
        "actionId": plan["body"]["actionId"],
        "status": "complete" if all(s["status"] == "complete" for s in steps) else "partial",
        "completed": [s["actionId"] for s in steps if s["status"] == "complete"],
        "failed": failed, "failedActionId": failed[0] if failed else None,
        "pending": [s["actionId"] for s in steps if s["status"] == "pending"],
        "items": [{**s.get("response", {}), "actionId": s["actionId"],
                   "userId": s["userId"], "status": s["status"]} for s in steps],
    }


def assignment_plan_path(session, action_id):
    target = session.store_path(action_id).with_suffix(".plan.json")
    if target.exists() or target.is_symlink():
        return target
    root, separator, index = action_id.rpartition("_")
    if separator and index.isdecimal():
        target = session.store_path(root).with_suffix(".plan.json")
        if target.exists() or target.is_symlink():
            return target
    return None


def run_assignments(session, *, body=None, ids=None, project=None, action_id=None):
    from .cli import read_config, write_config
    from .auth_flow import profile_lock

    resume = body is None
    target = (assignment_plan_path(session, action_id) if resume else
              session.store_path(body["actionId"]).with_suffix(".plan.json"))
    if target is None:
        raise CLIError("original_request_missing", "private assignment plan unavailable",
                       action_id=action_id)
    plan, bound = None, False
    try:
        # Serialize the journal and the whole continuation, including concurrent resumes.
        with profile_lock(target):
            if resume or target.exists() or target.is_symlink():
                plan = read_config(target)
                if (plan.get("server") != session.config["server"] or
                        plan.get("connectionId") != session.context()["connection"]["id"]):
                    raise CLIError("scope_required", "assignment plan belongs to a different connection/server")
                bound = True
                if not resume and (plan.get("body") != body or plan.get("ids") != ids):
                    raise CLIError("action_id_conflict", "actionId already has a different assignment plan")
            else:
                plan = {"server": session.config["server"],
                        "connectionId": session.context()["connection"]["id"],
                        "body": body, "ids": ids, "projectId": project,
                        "steps": [{"actionId": body["actionId"] if i == 0 else f"{body['actionId']}_{i}",
                                   "userId": user_id, "status": "pending"} for i, user_id in enumerate(ids)]}
                bound = True
                # Persist the entire intent, including later steps whose version is not yet known.
                write_config(target, plan)
            base, steps = plan["body"], plan["steps"]
            forbid_unknown(base, ACTION_FIELDS)
            if base.get("operation") not in ("assignee.add", "assignee.remove") or base.get("dryRun"):
                raise CLIError("original_request_invalid", "private plan is not an assignment operation")
            expected_ids = [base["actionId"] if i == 0 else f"{base['actionId']}_{i}"
                            for i in range(len(plan["ids"]))]
            if (len(steps) < 2 or [s["actionId"] for s in steps] != expected_ids or
                    [s["userId"] for s in steps] != plan["ids"] or
                    any(type(i) is not int or i <= 0 for i in plan["ids"]) or
                    (resume and action_id not in expected_ids)):
                raise CLIError("original_request_invalid", "private plan IDs do not match its original intent")
            session.project(argparse.Namespace(project=plan["projectId"]))
            session.require("task.assign" if base["operation"] == "assignee.add" else "task.unassign")
            if not resume and any(s["status"] != "pending" for s in steps):
                raise CLIError("resume_unsafe", "existing assignment plan requires action resume")
            next_version = base["ifVersion"]
            for step in steps:
                if step["status"] != "complete":
                    request = {**base, "actionId": step["actionId"], "userId": step["userId"],
                               "ifVersion": next_version}
                    if step["status"] != "pending":
                        try:
                            original = read_config(session.store_path(step["actionId"]))
                        except (OSError, ValueError):
                            raise CLIError("original_request_missing",
                                           "private original step request unavailable; do not reconstruct") from None
                        if original != {"server": plan["server"], "connectionId": plan["connectionId"],
                                        "path": "/actions", "body": request}:
                            raise CLIError("original_request_invalid", "private step request differs from its plan")
                        result = session.request("GET", "/actions/" + step["actionId"])
                        error = result.get("error") or {}
                        if result.get("status") == "complete":
                            step.update(status="complete", response=result)
                            write_config(target, plan)
                        elif (result.get("status") != "partial" or
                              result.get("retrySafe", error.get("retrySafe")) is not True):
                            raise CLIError("resume_unsafe",
                                           "only server-confirmed retrySafe partial steps may resume")
                    if step["status"] != "complete":
                        session.save_request("/actions", request)
                        step["status"] = "unknown"
                        write_config(target, plan)
                        try:
                            result = session.send_write("/actions", request)
                            if not isinstance(result, dict) or result.get("status") != "complete":
                                raise CLIError("invalid_response", "server did not confirm assignment completion",
                                               action_id=step["actionId"])
                        except CLIError as exc:
                            step.update(status="partial" if exc.retry_safe else (
                                            "unknown" if exc.code in ("transport_error", "action_unknown",
                                                                      "invalid_response") else "failed"),
                                        response=exc.payload())
                            write_config(target, plan)
                            raise
                        step.update(status="complete", response=result)
                        write_config(target, plan)
                result = step["response"]
                next_version = result.get("version") or (result.get("result") or {}).get("version")
                if step is not steps[-1] and (type(next_version) not in (str, int) or str(next_version) == ""):
                    raise CLIError("capability_unavailable",
                                   "continuation requires the successful action's returned version; "
                                   "do not adopt a fresh external version")
            return assignment_progress(plan)
    except (OSError, ValueError):
        exc = CLIError("private_request_store_invalid", "private assignment plan could not be safely stored/read",
                       action_id=action_id or body["actionId"])
        if plan is not None and bound:
            exc.progress = assignment_progress(plan)
        raise exc from None
    except CLIError as exc:
        if exc.action_id is None:
            exc.action_id = action_id or body["actionId"]
        if plan is not None and bound:
            exc.progress = assignment_progress(plan)
        raise


def state(item):
    task = item["task"]
    if task.get("done"):
        return "done"
    return item.get("meta", {}).get("state", "todo")


def matches_actor(item, actor, field):
    if actor is None:
        return True
    source = item.get("creator" if field == "created" else "lastModifiedBy") or {}
    own = item.get("createdByCurrentConnection" if field == "created" else "modifiedByCurrentConnection")
    kind = source.get("kind", "unknown")
    if actor == "human":
        return kind == "human"
    if actor == "self-agent":
        return kind == "agent" and own is True
    if actor == "other-agent":
        return kind == "agent" and own is False and source.get("connectionId") is not None
    if actor == "unknown":
        return kind == "unknown" or (kind == "agent" and source.get("connectionId") is None)
    if actor.startswith("connection:"):
        return str(source.get("connectionId")) == actor.partition(":")[2]
    if actor.startswith("user:"):
        return str(source.get("userId")) == actor.partition(":")[2]
    raise CLIError("invalid_parameters", "source expects human/self-agent/other-agent/unknown/connection:ID/user:ID")


def validate_filters(args):
    if value(args, "undated") and any(value(args, name) for name in
                                    ("date", "date_from", "date_to", "overdue_before")):
        raise CLIError("invalid_parameters", "--undated conflicts with planned-date filters")
    if value(args, "date") and any(value(args, name) for name in ("date_from", "date_to", "overdue_before")):
        raise CLIError("invalid_parameters", "--date conflicts with planned-date range/overdue filters")
    for start, end in (("date_from", "date_to"), ("completed_from", "completed_to")):
        if value(args, start) and value(args, end) and value(args, start) >= value(args, end):
            raise CLIError("invalid_parameters", "date range must have start before exclusive end")
    for field in ("created_by", "modified_by"):
        actor = value(args, field)
        if actor and actor not in ("human", "self-agent", "other-agent", "unknown") and not re.fullmatch(
                r"(connection|user):[a-zA-Z0-9_-]+", actor):
            raise CLIError("invalid_parameters", "invalid source selector")


def completed_date(task, preferences):
    raw = task.get("done_at", task.get("doneAt"))
    if not raw or str(raw).startswith("0001"):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        zone = preferences.get("timezone", "local")
        if zone in ("local", "unknown", None) or parsed.tzinfo is None:
            return None
        return parsed.astimezone(ZoneInfo(zone)).date().isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return None


def filter_tasks(items, args, preferences):
    result = []
    for original in items:
        item = copy.deepcopy(original)
        task, meta = item["task"], item.get("meta", {})
        planned = meta.get("plannedDate")
        current_state = state(item)
        if value(args, "state") and current_state != args.state:
            continue
        if value(args, "event") and meta.get("event") != args.event:
            continue
        assignees = value(args, "assignee_id")
        assigned = {a.get("id") if isinstance(a, dict) else a for a in task.get("assignees", [])}
        if assignees and not set(assignees).issubset(assigned):
            continue
        if not matches_actor(item, value(args, "created_by"), "created") or not matches_actor(
                item, value(args, "modified_by"), "modified"):
            continue
        if value(args, "undated") and planned is not None:
            continue
        if value(args, "date") and planned != args.date:
            continue
        if value(args, "date_from") and (not planned or planned < args.date_from):
            continue
        if value(args, "date_to") and (not planned or planned >= args.date_to):
            continue
        if value(args, "overdue_before") and (
                not planned or planned >= args.overdue_before or current_state == "done"):
            continue
        if task.get("done"):
            item["completedDate"] = completed_date(task, preferences)
            item["completedDateStatus"] = "known" if item["completedDate"] else "unknown"
        completion = item.get("completedDate")
        if value(args, "completed_from") and (not completion or completion < args.completed_from):
            continue
        if value(args, "completed_to") and (not completion or completion >= args.completed_to):
            continue
        result.append(item)
    return result


def counts(items):
    groups = {"events": [], "subtasks": [], "tasks": []}
    for item in items:
        meta = item.get("meta", {})
        groups["events" if meta.get("kind") == "event" else
               "subtasks" if meta.get("event") else "tasks"].append(item)
    return {key: {"total": len(rows), "done": sum(state(row) == "done" for row in rows),
                  "pending": sum(state(row) != "done" for row in rows),
                  "states": {s: sum(state(row) == s for row in rows)
                             for s in ("todo", "doing", "review", "done")}}
            for key, rows in groups.items()}


def body_input(args):
    raw, filename = value(args, "json"), value(args, "body_file")
    if raw is not None and filename is not None:
        raise CLIError("invalid_parameters", "use --json OR --body-file")
    if filename is not None:
        raw = sys.stdin.read() if filename == "-" else Path(filename).read_text()
    if raw is None:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise CLIError("invalid_parameters", "body must be valid JSON") from None
    if not isinstance(body, dict):
        raise CLIError("invalid_parameters", "body must be a JSON object")
    return body


def forbid_unknown(body, allowed, label="body"):
    unknown = set(body) - set(allowed)
    if unknown:
        raise CLIError("invalid_parameters", f"{label} contains forbidden or unsupported fields: " +
                       ",".join(sorted(unknown)))


def validate_meta(meta, *, creating=False, persisted=False):
    if not isinstance(meta, dict):
        raise CLIError("invalid_parameters", "meta must be an object")
    forbid_unknown(meta, META_FIELDS, "meta")
    for field, validator in (("plannedDate", iso_date), ("time", calendar_time)):
        if meta.get(field) is not None:
            if persisted and field == "time" and isinstance(meta[field], str) and re.fullmatch(
                    r"(?:[01]\d|2[0-3]):[0-5]\d:00(?:\.0+)?", meta[field]):
                continue
            try:
                validator(meta[field])
            except (TypeError, argparse.ArgumentTypeError):
                raise CLIError("invalid_parameters", f"invalid meta.{field}") from None
    if "kind" in meta and meta["kind"] not in ("task", "event"):
        raise CLIError("invalid_parameters", "invalid meta.kind")
    if "state" in meta and meta["state"] not in ("todo", "doing", "review"):
        raise CLIError("invalid_parameters", "invalid meta.state; done uses task complete")
    if meta.get("event") is not None and (type(meta["event"]) is not int or meta["event"] <= 0):
        raise CLIError("invalid_parameters", "meta.event must be a positive ID")
    if creating and meta.get("state", "todo") != "todo":
        raise CLIError("invalid_parameters", "creation cannot impersonate execution/review")


def validate_fields(body, *, draft=False):
    if "title" in body and (not isinstance(body["title"], str) or not 1 <= len(body["title"].strip()) <= 500):
        raise CLIError("invalid_parameters", "title must contain 1..500 characters")
    if "description" in body and (not isinstance(body["description"], str) or len(body["description"]) > 100000):
        raise CLIError("invalid_parameters", "description must be a string of at most 100000 characters")
    if "priority" in body and (type(body["priority"]) is not int or not 0 <= body["priority"] <= 5):
        raise CLIError("invalid_parameters", "priority must be integer 0..5")
    if "intent" in body and body["intent"] not in ("self", "agent"):
        raise CLIError("invalid_parameters", "intent must be self or agent")
    for key in ("assigneeIds", "labelIds"):
        if key in body:
            if not isinstance(body[key], list) or len(body[key]) > 100 or any(
                    type(i) is not int or i <= 0 for i in body[key]):
                raise CLIError("invalid_parameters", f"{key} must be at most 100 positive explicit IDs")
            body[key] = list(dict.fromkeys(body[key]))
    if "meta" in body:
        validate_meta(body["meta"], creating=draft)


def merge_flags(body, args, fields):
    mapping = {"assignee_id": "assigneeIds", "label_id": "labelIds"}
    for field in fields:
        given = value(args, field)
        if given is None:
            continue
        if field in ("clear_date", "clear_time", "clear_labels"):
            continue
        if field in ("date", "time", "event"):
            target = {"date": "plannedDate", "time": "time", "event": "event"}[field]
            meta = body.setdefault("meta", {})
            if not isinstance(meta, dict) or target in meta:
                raise CLIError("invalid_parameters", f"--{field} collides with JSON meta.{target}")
            meta[target] = given
        else:
            target = mapping.get(field, field)
            if target in body:
                raise CLIError("invalid_parameters", f"--{field.replace('_', '-')} collides with JSON {target}")
            body[target] = list(dict.fromkeys(given)) if isinstance(given, list) else given
    for clear, flag, target in (("clear_date", "date", "plannedDate"),
                                ("clear_time", "time", "time")):
        if value(args, clear):
            meta = body.setdefault("meta", {})
            if value(args, flag) is not None or target in meta:
                raise CLIError("invalid_parameters", f"--{clear.replace('_', '-')} collides with {flag}")
            meta[target] = None
    if value(args, "clear_labels"):
        if "labelIds" in body:
            raise CLIError("invalid_parameters", "--clear-labels collides with labelIds")
        body["labelIds"] = []


def version_check(current, requested):
    if type(requested) not in (str, int) or str(requested) == "":
        raise CLIError("invalid_parameters", "existing-object writes require --if-version")
    if current is None:
        raise CLIError("capability_unavailable", "server object has no version; cannot use new write endpoint")
    if str(current) != str(requested):
        raise CLIError("version_conflict", "object changed; reread and obtain user intent before a new action")
    return current


def build_write(args, session):
    key = args.collaboration_key
    spec = REGISTRY[key]
    input_body = body_input(args)
    body = copy.deepcopy(input_body)
    create = key in ("task.create", "event.create")
    execution = key == "task.execution"
    fields = FIELDS.get("create" if create else "event_update" if key in ("event.update", "project.update")
                        else spec["action"], ())
    if create:
        envelope = "draft" in body
        if envelope:
            forbid_unknown(body, ("actionId", "operation", "projectId", "draft", "children", "dryRun"))
            draft = body.setdefault("draft", {})
            if not isinstance(draft, dict):
                raise CLIError("invalid_parameters", "draft must be an object")
        else:
            children = body.pop("children", [])
            controls = {k: body.pop(k) for k in ("actionId", "operation", "projectId", "dryRun") if k in body}
            draft, body = body, {**controls, "children": children}
        forbid_unknown(draft, DRAFT_FIELDS, "draft")
        merge_flags(draft, args, fields)
        if "title" not in draft:
            raise CLIError("invalid_parameters", "creation requires title")
        validate_fields(draft, draft=True)
        draft.setdefault("meta", {})
        if key == "event.create":
            if draft["meta"].get("event") is not None:
                raise CLIError("invalid_parameters", "event parent cannot belong to another event")
            if draft["meta"].get("kind", "task") not in ("task", "event"):
                raise CLIError("invalid_parameters", "invalid event draft kind")
            # Event operation owns the resulting parent kind; TaskDraft preflight expects task.
            draft["meta"]["kind"] = "task"
        elif draft["meta"].get("kind", "task") != "task":
            raise CLIError("invalid_parameters", "use event create for event parents")
        body["draft"] = draft
        children = body.get("children", [])
        if not isinstance(children, list) or len(children) > 100 or (children and key != "event.create"):
            raise CLIError("invalid_parameters", "children are only supported for event create (max 100)")
        for child in children:
            if not isinstance(child, dict):
                raise CLIError("invalid_parameters", "child must be a draft object")
            forbid_unknown(child, DRAFT_FIELDS, "child")
            if "title" not in child:
                raise CLIError("invalid_parameters", "each child requires title")
            validate_fields(child, draft=True)
            if child.get("meta", {}).get("kind", "task") != "task" or child.get("meta", {}).get("event") is not None:
                raise CLIError("invalid_parameters", "child parent relationship is server assigned")
        if key == "task.create":
            body.pop("children", None)
    else:
        allowed = spec["bodyFields"]
        forbid_unknown(body, (*allowed, "actionId", "ifVersion", "dryRun", "operation", "taskId", "projectId"))
        merge_flags(body, args, fields)
        validate_fields(body)
        if execution and body.get("state") not in ("doing", "review"):
            raise CLIError("invalid_parameters", "execution requires state doing or review")
    for flag, target in (("action_id", "actionId"), ("if_version", "ifVersion"), ("dry_run", "dryRun")):
        if value(args, flag) is not None:
            if target in body:
                raise CLIError("invalid_parameters", f"--{flag.replace('_', '-')} collides with JSON {target}")
            body[target] = value(args, flag)
    body["actionId"] = checked_action_id(body.get("actionId") or uuid.uuid4().hex)
    if "dryRun" in body and type(body["dryRun"]) is not bool:
        raise CLIError("invalid_parameters", "dryRun must be boolean")
    if spec.get("operation"):
        if body.get("operation", spec["operation"]) != spec["operation"]:
            raise CLIError("invalid_parameters", "operation does not match command")
        body["operation"] = spec["operation"]
    elif "operation" in body:
        raise CLIError("invalid_parameters", "execution has no operation field")
    if value(args, "project") is not None and "projectId" in input_body:
        raise CLIError("invalid_parameters", "--project collides with JSON projectId")
    if value(args, "space") is not None and "projectId" in input_body:
        raise CLIError("invalid_parameters", "--space collides with JSON projectId")
    if "projectId" in body:
        if type(body["projectId"]) is not int or body["projectId"] <= 0:
            raise CLIError("invalid_parameters", "projectId must be a positive ID")
        scope_args = copy.copy(args)
        scope_args.project = body["projectId"]
    else:
        scope_args = copy.copy(args)
        if key.startswith("project.") and value(args, "project") is None and value(args, "space") is None:
            scope_args.project = args.id
    project = session.project(scope_args)
    current = None
    if create or key.startswith("project."):
        if create and ("ifVersion" in body or "taskId" in body):
            raise CLIError("invalid_parameters", "creation cannot target an existing object version")
        body["projectId"] = project if not key.startswith("project.") else args.id
        if key.startswith("project."):
            if args.id not in session.context()["connection"].get("projectIds", []):
                raise CLIError("scope_required", "project is outside connection scope")
            if value(args, "space") is not None or value(args, "project") is not None:
                if args.id != project:
                    raise CLIError("invalid_parameters", "project target and selected context disagree")
            current = session.request("GET", f"/projects/{args.id}")
    else:
        if "taskId" in body and body["taskId"] != args.id:
            raise CLIError("invalid_parameters", "JSON taskId does not match positional ID")
        current = session.task(args.id, project)
        if not execution:
            body["taskId"] = args.id
        else:
            body.pop("taskId", None)
            body.pop("projectId", None)
    if spec["versionRequired"]:
        body["ifVersion"] = version_check(current.get("version"), body.get("ifVersion"))
    if current and not key.startswith("project."):
        meta = current.get("meta", {})
        if key == "event.update" and meta.get("kind") != "event":
            raise CLIError("invalid_parameters", "event update requires an event parent")
        if key in ("task.delete", "task.complete", "task.reopen") and meta.get("kind") == "event":
            raise CLIError("policy_not_ready", "event deletion/overall completion is not available")
        if "meta" in body:
            changes = body["meta"]
            if changes.get("kind", meta.get("kind")) != meta.get("kind") or changes.get(
                    "event", meta.get("event")) != meta.get("event"):
                raise CLIError("invalid_parameters", "cannot change task kind or parent")
            if "state" in changes and changes["state"] != meta.get("state"):
                raise CLIError("invalid_parameters", "use execution/complete/reopen to change state")
            body["meta"] = {**meta, **changes}
            validate_meta(body["meta"], persisted=True)
        if key in ("task.complete", "task.reopen"):
            if meta.get("state") == "review" or (key == "task.complete" and meta.get("state") == "doing"):
                raise CLIError("policy_not_ready", "cannot bypass Agent execution/review with ordinary completion")
            body["done"] = key == "task.complete"
        if key == "task.delete":
            children = session.pages("/tasks", {"projectId": project})
            if any(item.get("meta", {}).get("event") == args.id for item in children):
                raise CLIError("policy_not_ready", "task with children requires unavailable event cascade deletion")
            preview = {"taskId": args.id, "title": current["task"].get("title"),
                       "projectId": project, "version": current["version"], "permanent": True,
                       "restorable": False}
            if not value(args, "yes") and not body.get("dryRun"):
                raise CLIError("confirmation_required", "permanent deletion requires this version and --yes",
                               action_id=body["actionId"], preview=preview)
            print("Permanent deletion preview: " + json.dumps(preview, ensure_ascii=True), file=sys.stderr)
    if create:
        space = next((s for s in session.context().get("spaces", []) if s.get("projectId") == project), {})
        drafts = [body["draft"], *body.get("children", [])]
        for draft in drafts:
            draft.setdefault("intent", "self")
            if space.get("mode") == "daily" and draft["intent"] == "self":
                owner = session.context()["connection"].get("owner", space.get("owner"))
                if type(owner) is not int:
                    raise CLIError("invalid_response", "daily self creation requires explicit owner ID")
                if draft.get("assigneeIds") and draft["assigneeIds"] != [owner]:
                    raise CLIError("invalid_parameters", "daily self intent must assign the owning person")
                draft["assigneeIds"] = [owner]
            if draft["intent"] == "agent":
                context = session.context()
                visible_people = {p["id"]: p for p in context.get("people", [])}
                eligible = {pid for pid, p in visible_people.items()
                            if p.get("kind") == "bot" or p.get("isBot") is True
                            or p.get("is_bot") is True or (
                                type(p.get("bot_owner_id")) is int and p["bot_owner_id"] > 0)}
                bot_ids = [context["connection"].get("botId"), *(
                    c.get("botId") for c in context.get("connections", []))]
                eligible.update(pid for pid in bot_ids if pid in visible_people)
                if not draft.get("assigneeIds") or not set(draft["assigneeIds"]).issubset(eligible):
                    raise CLIError("invalid_parameters", "agent intent requires explicitly resolved visible Bot IDs")
    if key in ("project.archive", "project.unarchive"):
        body["archived"] = key == "project.archive"
    if not create and not execution and key in ("task.update", "event.update", "task.schedule", "project.update"):
        if not any(field in body for field in ("title", "description", "priority", "labelIds", "meta")):
            raise CLIError("invalid_parameters", "update requires at least one changed field")
    return body, current


def run_write(args, session):
    body, current = build_write(args, session)
    key = args.collaboration_key
    session.require(key)
    path = f"/tasks/{args.id}/execution" if key == "task.execution" else "/actions"
    if key in ("task.assign", "task.unassign"):
        ids = body.pop("assigneeIds", None)
        if ids is not None and "userId" in body:
            raise CLIError("invalid_parameters", "assigneeIds collides with userId")
        ids = ids or ([body["userId"]] if body.get("userId") else [])
        if not ids or any(type(i) is not int or i <= 0 for i in ids):
            raise CLIError("invalid_parameters", "assignment requires explicit positive assignee IDs")
        ids = list(dict.fromkeys(ids))
        results, root = [], body["actionId"]
        if len(root) > 116 and len(ids) > 1:
            raise CLIError("invalid_parameters", "multi-assignee actionId must be at most 116 characters")
        if body.get("dryRun"):
            for index, user_id in enumerate(ids):
                step = {**body, "userId": user_id,
                        "actionId": root if index == 0 else f"{root}_{index}"}
                result = session.request("POST", path, body=step, action_id=step["actionId"])
                if not isinstance(result, dict) or result.get("status") != "dry_run":
                    raise CLIError("invalid_response", "server did not confirm assignment dry-run",
                                   action_id=step["actionId"])
                results.append(result)
            return {"status": "dry_run", "dryRun": True, "actionId": root, "items": results}
        if len(ids) == 1:
            return session.send_write(path, {**body, "userId": ids[0]})
        return run_assignments(session, body=body, ids=ids,
                               project=current["task"].get("project_id", current["task"].get("projectId")))
    if body.get("dryRun"):
        result = session.request("POST", path, body=body, action_id=body["actionId"])
        if not isinstance(result, dict) or result.get("status") != "dry_run":
            raise CLIError("invalid_response", "server did not confirm side-effect-free dry-run validation",
                           action_id=body["actionId"])
        return {**result, "dryRun": True}
    return session.send_write(path, body)


def discovery(args, session=None):
    key = args.collaboration_key
    metadata, context = None, None
    if session is not None and not value(args, "offline"):
        try:
            metadata, context = session.capabilities(), session.context()
        except CLIError as exc:
            if exc.code != "http_404":
                raise
    capabilities = merge_capabilities(metadata, context)
    if key == "version":
        return {"cliVersion": CLI_VERSION, "apiVersion": (metadata or {}).get("apiVersion"),
                "schemaVersion": (metadata or {}).get("schemaVersion", SCHEMA_VERSION),
                "minCliVersion": (metadata or {}).get("minCliVersion", CLI_VERSION),
                "minApiVersion": API_VERSION, "localOnly": metadata is None}
    if key == "capability.list":
        return {"items": capabilities, "total": len(capabilities), "localOnly": metadata is None}
    if args.key not in REGISTRY:
        raise CLIError("invalid_parameters", "unknown capability")
    capability = next(c for c in capabilities if c["id"] == args.key)
    if key == "schema":
        return {**local_schema(args.key), "capability": capability,
                "serverSchema": next((c.get("schema") for c in (metadata or {}).get("items", [])
                                      if c.get("id", c.get("name")) == args.key), None),
                "localOnly": metadata is None}
    return capability


def run(args, session):
    key = args.collaboration_key
    if key in ("version", "schema", "capability.list", "capability.get"):
        return discovery(args, session)
    if not REGISTRY[key]["policyReady"]:
        raise CLIError("policy_not_ready", REGISTRY[key]["reason"])
    if "write" in REGISTRY[key]["profiles"]:
        return run_write(args, session)
    session.require(key)
    context = session.context()
    if key == "auth.permissions":
        return {"connection": context["connection"], "spaces": context.get("spaces", []),
                "requiredScopes": ["read", "manage", "execute"],
                "grantedScopes": context["connection"].get("scopes", []),
                "reauthorize": "explicitly authorize manage in App; old connections are not upgraded"}
    domain, action = key.split(".", 1)
    if domain in ("space", "person", "connection", "label", "preference"):
        project = session.project(args, all_projects=domain in ("space", "preference"))
        if domain == "preference":
            return context.get("preferences", {"timezone": "unknown", "timezoneConfigured": False})
        objects = context.get({"space": "spaces", "person": "people", "label": "labels"}.get(domain, domain + "s"))
        if objects is None and domain == "connection":
            objects = [context["connection"]]
        allowed = context["connection"].get("projectIds", [])
        objects = [o for o in (objects or [])
                   if (o.get("projectId") is None or o["projectId"] in allowed)
                   and (not o.get("projectIds") or set(allowed).intersection(o["projectIds"]))
                   and (project is None or (o.get("projectId", project) == project
                        and (not o.get("projectIds") or project in o["projectIds"])))]
        if action == "get":
            matches = [o for o in objects if str(o.get("id")) == args.key]
            if len(matches) != 1:
                raise CLIError("object_not_found", "no unique visible object with this exact ID")
            return matches[0]
        return paginate(objects, args)
    if domain == "project":
        scope_args = copy.copy(args)
        if action == "get" and value(args, "project") is None and value(args, "space") is None:
            scope_args.project = args.id
        project = session.project(scope_args, all_projects=action in ("list", "search"))
        if action == "member.list":
            data = session.request("GET", f"/projects/{project}/members")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise CLIError("invalid_response", "member response must contain items")
            return paginate(data["items"], args)
        if action == "get":
            if args.id not in context["connection"].get("projectIds", []):
                raise CLIError("scope_required", "project is not authorized")
            if project is not None and project != args.id:
                raise CLIError("scope_required", "selected project does not match object")
            return session.request("GET", f"/projects/{args.id}")
        items = [p for p in context.get("projects", []) if p["id"] in context["connection"].get("projectIds", [])
                 and (project is None or p["id"] == project)]
        archived = value(args, "archived", "active")
        items = [p for p in items if archived == "all" or bool(p.get("is_archived", p.get("archived", False)))
                 == (archived == "archived")]
        if action == "search":
            if not value(args, "query"):
                raise CLIError("invalid_parameters", "search requires --query")
            items = [p for p in items if args.query.casefold() in
                     (p.get("title", "") + " " + p.get("description", "")).casefold()]
        return paginate(items, args)
    if domain in ("task", "event"):
        validate_filters(args)
        project = session.project(args)
        if action == "get":
            item = session.task(args.id, project)
            if domain == "task":
                return item
            if item.get("meta", {}).get("kind") != "event":
                raise CLIError("invalid_parameters", "event get requires an event parent")
            children = [t for t in session.pages("/tasks", {"projectId": project})
                        if t.get("meta", {}).get("event") == args.id]
            return {"parent": item, "children": paginate(children, args), "summary": counts(children)}
        params = {"projectId": project}
        if action == "search":
            if not value(args, "query"):
                raise CLIError("invalid_parameters", "search requires --query")
            params["q"] = args.query
        items = session.pages("/tasks", params)
        if domain == "event":
            items = [i for i in items if i.get("meta", {}).get("kind") == "event"]
        if action == "+agenda":
            if not value(args, "date"):
                raise CLIError("invalid_parameters", "+agenda requires --date")
            broad = copy.copy(args)
            broad.date = None
            items = filter_tasks(items, broad, context.get("preferences", {}))
            seen, groups = set(), {"scheduled": [], "overdue": [], "undated": []}
            for item in items:
                if item["task"]["id"] in seen or (state(item) == "done" and not value(args, "include_done")):
                    continue
                planned = item.get("meta", {}).get("plannedDate")
                group = "scheduled" if planned == args.date else "overdue" if (
                    planned and planned < args.date and state(item) != "done" and value(args, "include_overdue")) else (
                    "undated" if not planned and value(args, "include_undated") else None)
                if group:
                    groups[group].append(item)
                    seen.add(item["task"]["id"])
            return {"date": args.date, "groups": groups, "total": len(seen)}
        items = filter_tasks(items, args, context.get("preferences", {}))
        if action == "summary":
            return {"total": len(items), "groups": counts(items)}
        return paginate(items, args)
    if domain == "notification":
        if action == "list":
            return session.request("GET", "/events", params={k: v for k, v in
                                   {"cursor": value(args, "cursor"), "limit": value(args, "limit", 50)}.items()
                                   if v is not None})
        body = body_input(args)
        forbid_unknown(body, ("eventIds",))
        if args.event_ids and "eventIds" in body:
            raise CLIError("invalid_parameters", "positional IDs collide with JSON eventIds")
        ids = args.event_ids or body.get("eventIds", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(
                not isinstance(i, str) or not i for i in ids):
            raise CLIError("invalid_parameters", "ack requires 1..100 explicit event IDs")
        return session.request("POST", "/ack", body={"eventIds": list(dict.fromkeys(ids))})
    if domain == "history":
        project = session.project(args)
        return paginate(session.pages("/history", {"projectId": project}), args)
    if domain == "action":
        action_id = checked_action_id(args.key)
        if action == "resume" and assignment_plan_path(session, action_id) is not None:
            return run_assignments(session, action_id=action_id)
        data = session.request("GET", "/actions/" + quote(action_id, safe=""))
        if action == "get":
            return data
        error = data.get("error") or {}
        if data.get("status") != "partial" or (data.get("retrySafe", error.get("retrySafe")) is not True):
            raise CLIError("resume_unsafe", "only server-confirmed retrySafe partial actions may resume",
                           action_id=action_id)
        from .cli import read_config
        try:
            original = read_config(session.store_path(action_id))
        except (OSError, ValueError):
            raise CLIError("original_request_missing", "private original request unavailable; do not reconstruct",
                           action_id=action_id) from None
        if original.get("connectionId") != context["connection"]["id"] or original["server"] != session.config["server"]:
            raise CLIError("scope_required", "original action belongs to a different connection/server",
                           action_id=action_id)
        path, body = original.get("path"), original.get("body", {})
        if body.get("actionId") != action_id or not (path == "/actions" or
                                                    re.fullmatch(r"/tasks/[1-9]\d*/execution", path or "")):
            raise CLIError("original_request_invalid", "private request does not match a permitted action endpoint",
                           action_id=action_id)
        if path == "/actions":
            forbid_unknown(body, ACTION_FIELDS)
            operation = body.get("operation")
            scope = "manage"
            if operation not in {s["operation"] for s in REGISTRY.values() if s["policyReady"]}:
                raise CLIError("policy_not_ready", "original operation is unavailable", action_id=action_id)
        else:
            forbid_unknown(body, ("actionId", "state", "description", "ifVersion", "dryRun"))
            scope = "execute"
        if scope not in context["connection"].get("scopes", []):
            raise CLIError("scope_required", f"resume requires {scope}", action_id=action_id)
        capability_key = "task.execution" if path != "/actions" else next(
            k for k, spec in REGISTRY.items() if spec["operation"] == body["operation"]
            and spec["policyReady"])
        session.require(capability_key)
        return session.send_write(path, body)
    if domain == "sync":
        cursor = value(args, "cursor")
        interval, timeout = value(args, "interval", 5.0), value(args, "timeout", 60.0)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if action == "watch" and remaining <= 0:
                return None
            data = session.request("GET", "/sync", params={k: v for k, v in
                                   {"cursor": cursor, "limit": value(args, "limit", 100)}.items() if v is not None},
                                   timeout=min(20, remaining) if action == "watch" else None)
            if action == "pull":
                return data
            print(json.dumps(data, ensure_ascii=False), flush=True)
            next_cursor = data.get("nextCursor")
            cursor = next_cursor or data.get("resumeCursor")
            if cursor is None:
                raise CLIError("invalid_response", "sync response lacks resumeCursor")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not next_cursor:
                time.sleep(min(interval, remaining))
                if time.monotonic() >= deadline:
                    return None
    raise CLIError("capability_unavailable", "command has no implemented handler")

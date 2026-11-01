"""Local command contract. Registration drives parser, help and discovery."""

CLI_VERSION = "0.2.0"
API_VERSION = "0.2.0"
SCHEMA_VERSION = "2"

META_FIELDS = ("plannedDate", "time", "kind", "event", "state")
DRAFT_FIELDS = ("title", "description", "priority", "intent", "assigneeIds", "labelIds", "meta")
ACTION_FIELDS = ("actionId", "operation", "projectId", "taskId", "draft", "children", "title",
                 "description", "priority", "meta", "userId", "labelId", "labelIds", "done",
                 "archived", "ifVersion", "dryRun")
BODY_FIELDS = {
    "task.create": ("draft", "children"), "event.create": ("draft", "children"),
    "task.update": ("title", "description", "priority", "labelIds", "meta"),
    "task.schedule": ("meta",), "event.update": ("title", "description"),
    "task.assign": ("assigneeIds", "userId"), "task.unassign": ("assigneeIds", "userId"),
    "task.complete": (), "task.reopen": (), "task.delete": (),
    "task.execution": ("state", "description"), "project.update": ("title", "description"),
    "project.archive": (), "project.unarchive": (),
}
JSON_PROPERTIES = {
    "actionId": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{8,128}$"},
    "operation": {"type": "string"}, "projectId": {"type": "integer", "minimum": 1},
    "taskId": {"type": "integer", "minimum": 1}, "userId": {"type": "integer", "minimum": 1},
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "description": {"type": "string", "maxLength": 100000},
    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
    "intent": {"enum": ["self", "agent"]},
    "assigneeIds": {"type": "array", "maxItems": 100,
                    "items": {"type": "integer", "minimum": 1}},
    "labelIds": {"type": "array", "maxItems": 100,
                "items": {"type": "integer", "minimum": 1}},
    "ifVersion": {"type": ["string", "integer"]}, "dryRun": {"type": "boolean"},
    "done": {"type": "boolean"}, "archived": {"type": "boolean"},
    "state": {"enum": ["doing", "review"]},
    "meta": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "plannedDate": {"type": ["string", "null"], "format": "date"},
            "time": {"type": ["string", "null"], "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
            "kind": {"enum": ["task", "event"]},
            "event": {"type": ["integer", "null"], "minimum": 1},
            "state": {"enum": ["todo", "doing", "review"]},
        },
    },
}
JSON_PROPERTIES["draft"] = {
    "type": "object", "additionalProperties": False, "required": ["title"],
    "properties": {k: JSON_PROPERTIES[k] for k in DRAFT_FIELDS},
}
JSON_PROPERTIES["children"] = {"type": "array", "maxItems": 100,
                               "items": JSON_PROPERTIES["draft"]}

QUERY = {
    "scope": ("space", "project"),
    "pagination": ("page", "page_size", "all"),
    "filters": ("date", "date_from", "date_to", "undated", "overdue_before",
                "completed_from", "completed_to", "state", "assignee_id", "event",
                "created_by", "modified_by"),
}
FIELDS = {
    "create": ("title", "description", "priority", "intent", "assignee_id",
               "label_id", "date", "time", "event"),
    "update": ("title", "description", "priority", "label_id", "date", "time",
               "clear_date", "clear_time", "clear_labels"),
    "schedule": ("date", "time", "clear_date", "clear_time"),
    "execution": ("state", "description"),
    "assign": ("assignee_id",),
    "unassign": ("assignee_id",),
    "event_update": ("title", "description"),
}
EXTRA_PROFILES = {
    "offline": ("offline",), "search": ("query",),
    "project_filter": ("archived",), "body": ("json", "body_file"),
    "write": ("if_version", "action_id", "dry_run"),
    "delete": ("yes",), "notification": ("cursor", "limit"),
    "sync": ("cursor", "limit"), "watch": ("interval", "timeout"),
    "agenda": ("include_overdue", "include_undated", "include_done"),
}


def definition(key, scope="read", *, operation=None, profiles=(), positional=None,
               maturity="supported", reason=None):
    domain, action = key.split(".", 1)
    write = operation is not None or key in ("task.execution", "notification.ack")
    return {
        "id": key, "domain": domain, "action": action,
        "cliName": f"{domain} {action.replace('.', ' ')}",
        "requiredScopes": [scope] if scope else [], "maturity": maturity,
        "reason": reason, "policyReady": maturity != "blocked",
        "operation": operation, "profiles": profiles, "positional": positional,
        "bodyFields": BODY_FIELDS.get(key, ()),
        "write": write,
        "versionRequired": write and action not in ("create", "ack")
        and domain not in ("action",),
        "dryRun": bool(operation or key == "task.execution"),
        "confirmation": "permanent-delete" if key == "task.delete" else None,
        "cliVersion": CLI_VERSION, "minCliVersion": CLI_VERSION,
        "minApiVersion": API_VERSION, "schemaVersion": SCHEMA_VERSION,
        "target": domain, "authorization": "current connection's existing projects",
        "retry": "original request and actionId; server retrySafe partial only",
        "productReference": "CLI_COLLABORATION_DESIGN.md sections 3/12/13",
    }


REGISTRY = {}


def register(key, scope="read", **kwargs):
    REGISTRY[key] = definition(key, scope, **kwargs)


register("auth.permissions")
for domain, actions in {
    "space": ("list", "get"), "person": ("list", "get"),
    "connection": ("list", "get"), "label": ("list",),
    "preference": ("get",), "capability": ("list", "get"),
}.items():
    for action in actions:
        register(f"{domain}.{action}", None if domain == "capability" else "read",
                 positional="key" if action == "get" and domain != "preference" else None,
                 profiles=("offline",) if domain == "capability" else
                 ("scope", "pagination") if action == "list" else ("scope",))
for action in ("list", "get", "search", "member.list"):
    register(f"project.{action}", positional="id" if action == "get" else None,
             profiles=("scope", "pagination", "project_filter")
             + (("search",) if action == "search" else ()))
for domain in ("task", "event"):
    for action in ("list", "get", "search"):
        register(f"{domain}.{action}", positional="id" if action == "get" else None,
                 profiles=("scope", "pagination", "filters")
                 + (("search",) if action == "search" else ()))
register("task.summary", profiles=("scope", "pagination", "filters"))
register("task.+agenda", profiles=("scope", "pagination", "filters", "agenda"))
for domain in ("task", "event"):
    register(f"{domain}.create", "manage", operation=f"{domain}.create",
             profiles=("scope", "create", "body", "write"))
    register(f"{domain}.update", "manage", operation=f"{domain}.update", positional="id",
             profiles=("scope", "update" if domain == "task" else "event_update",
                       "body", "write"))
for action, operation in {
    "schedule": "task.update", "assign": "assignee.add", "unassign": "assignee.remove",
    "complete": "task.update", "reopen": "task.update", "delete": "task.delete",
}.items():
    register(f"task.{action}", "manage", operation=operation, positional="id",
             profiles=("scope", "body", "write") +
             ((action,) if action in FIELDS else ()) +
             (("delete",) if action == "delete" else ()))
register("task.execution", "execute", positional="id",
         profiles=("scope", "execution", "body", "write"))
register("notification.list", profiles=("notification",))
register("notification.ack", positional="event_ids", profiles=("body",))
register("sync.pull", profiles=("sync",))
register("sync.watch", profiles=("sync", "watch"))
register("history.list", profiles=("scope", "pagination"))
register("action.get", positional="key")
register("action.resume", positional="key")
for action in ("update", "archive", "unarchive"):
    register(f"project.{action}", "manage", operation=
             "project.update" if action == "update" else "project.archive",
             positional="id", profiles=("scope", "body", "write") +
             (("event_update",) if action == "update" else ()))
for key, reason in {
    "project.create": "account-level project creation scope is not ready; create and authorize in App",
    "label.create": "global label management scope is not ready",
    "label.update": "global label management scope is not ready",
    "label.delete": "global label management scope is not ready",
    "task.restore": "deleted IDs cannot be restored; retention contract is not ready",
    "event.delete": "version-bound persistent cascade deletion is not ready",
    "event.restore": "event and child ID restoration contract is not ready",
    "review.get": "human review policy and authorization are not ready",
    "review.approve": "human review policy and authorization are not ready",
    "review.request-changes": "human review policy and authorization are not ready",
    "task.claim": "exclusive executor and lease policy is not ready",
    "task.release": "exclusive executor and lease policy is not ready",
    "task.+create-personal": "shortcut not implemented; use task.create with intent self",
    "event.+plan": "shortcut not implemented; use event.create",
}.items():
    register(key, "manage", maturity="blocked", reason=reason,
             positional="key" if key.split(".")[1] not in ("create", "+plan", "+create-personal")
             else None)


def local_schema(key):
    spec = REGISTRY[key]
    parameters = ["format"]
    for profile in spec["profiles"]:
        parameters.extend(QUERY.get(profile, FIELDS.get(profile, EXTRA_PROFILES.get(profile, ()))))
    if "body" in spec["profiles"]:
        parameters.extend(("json", "body_file"))
    if "write" in spec["profiles"]:
        parameters.extend(("if_version", "action_id", "dry_run"))
    json_fields = list(spec["bodyFields"])
    required = []
    if spec["write"] and spec.get("dryRun"):
        json_fields.extend(("actionId", "ifVersion", "dryRun"))
        required = ["actionId"]
        if spec["versionRequired"]:
            required.append("ifVersion")
        if spec["operation"]:
            json_fields.extend(("operation", "projectId", "taskId"))
            required.append("operation")
            if spec["domain"] == "project":
                required.append("projectId")
            elif spec["versionRequired"]:
                required.append("taskId")
        if key in ("task.create", "event.create"):
            required.extend(("projectId", "draft"))
        if key == "task.execution":
            required.append("state")
    if spec["positional"]:
        parameters.append(spec["positional"])
    return {
        **spec, "parameters": sorted(set(parameters)),
        "requestSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {k: JSON_PROPERTIES[k] for k in json_fields},
            "required": required,
            "fields": list(FIELDS.get(spec["action"], ())),
            "mapping": {"date": "meta.plannedDate", "time": "meta.time",
                        "label_id": "labelIds (complete collection)",
                        "assignee_id": "draft.assigneeIds or Action.userId per stable step"},
            "forbidden": ["creator", "lastModifiedBy", "createdByCurrentConnection",
                          "modifiedByCurrentConnection", "audit", "extra"],
        },
        "responseSchema": {"query": "items,total,page,pageSize",
                           "object": "task,meta,version,creator,lastModifiedBy,conflict",
                           "action": "actionId,status,result,error.retrySafe",
                           "sync": "items,nextCursor,resumeCursor"},
        "limits": ["date range start inclusive/end exclusive",
                   "existing writes require ifVersion",
                   "dry-run calls server preflight without mutation or private request persistence",
                   "source and permissions are server-authoritative"],
    }


def merge_capabilities(metadata=None, context=None):
    remote = {item.get("id", item.get("capability", item.get("name"))): item
              for item in (metadata or {}).get("items", [])}
    granted = (context or {}).get("connection", {}).get("scopes", [])
    result = []
    for key, spec in REGISTRY.items():
        server = remote.get(key, {})
        supported = spec["maturity"] in ("supported", "deprecated")
        server_supported = server.get("serverSupported",
                                      server.get("supported", server.get("maturity") == "supported"))
        policy = spec["policyReady"] and server.get("policyReady", True)
        scopes = sorted(set(spec["requiredScopes"]) | set(server.get("requiredScopes", [])))
        effective_granted = server.get("grantedScopes", granted)
        missing = set(scopes) - set(effective_granted)
        reason = None
        minimum = server.get("minCliVersion", (metadata or {}).get("minCliVersion", CLI_VERSION))
        try:
            upgrade = tuple(map(int, CLI_VERSION.split("."))) < tuple(map(int, minimum.split(".")))
        except (ValueError, AttributeError):
            upgrade = True
        if not spec["policyReady"]:
            reason = "policy_not_ready"
        elif not supported or upgrade:
            reason = "cli_upgrade_required"
        elif metadata is None or not server_supported:
            reason = "capability_unavailable"
        elif not policy:
            reason = "policy_not_ready"
        elif missing:
            reason = "scope_required"
        elif server.get("available") is False:
            reason = server.get("reason") or "capability_unavailable"
        result.append({**spec, "cliSupported": supported, "serverSupported": bool(server_supported),
                       "requiredScopes": scopes, "grantedScopes": effective_granted,
                       "policyReady": policy, "available": reason is None, "reason": reason,
                       "policyReason": spec.get("reason"),
                       "localOnly": metadata is None})
    return result

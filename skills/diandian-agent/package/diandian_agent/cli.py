import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .commands import CLIError, PREFIX, Parser, Session, common, discovery, emit
from .commands import positive, register_parsers, response_data, run, version_check


def server_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("server must be an http(s) URL without credentials")
    if parts.query or parts.fragment:
        raise ValueError("server must not contain query/fragment")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("remote server requires HTTPS")
    return url.rstrip("/")


def write_config(path, data):
    target = Path(path).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077):
        raise ValueError("config parent directory must have mode 0700")
    if target.is_symlink():
        raise OSError("config symlinks are forbidden")
    if target.exists():
        existing = target.stat()
        if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid() or existing.st_nlink != 1:
            raise ValueError("config must be an owned regular file, not a hard link")
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(path):
    target = Path(path).expanduser()
    parent = target.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077):
        raise ValueError("config parent directory must be owned with mode 0700")
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("config must be owned by current user with mode 0600")
        config = json.load(handle)
    config["server"] = server_url(config["server"])
    return config


def build_parser():
    parser = Parser(prog="diandian-agent")
    parser.add_argument("--config", default="~/.config/diandian-agent/config.json",
                        help="explicit local credential file (0600, directory 0700)")
    parser.add_argument("--profile", help="separate local identity for this Agent (letters, digits, hyphens)")
    parser.add_argument("--format", choices=("json", "table"), default="json")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="browser login, saved binding and logout")
    common(auth)
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_sub.add_parser("login", help="reuse binding or open browser and automatically pair")
    auth_login.add_argument("--server", required=True)
    auth_login.add_argument("--name", "--client-name", help="host product name; otherwise detect environment")
    auth_login.add_argument("--no-browser", action="store_true")
    auth_login.add_argument("--no-wait", action="store_true", help="return the approval URL; complete with auth wait")
    auth_login.add_argument("--reauthorize", action="store_true",
                            help="request fresh explicit browser consent; retain old binding until paired")
    auth_login.add_argument("--timeout", type=int, choices=range(1, 601), metavar="1..600", default=600)
    auth_wait = auth_sub.add_parser("wait", help="resume pending browser authorization")
    auth_wait.add_argument("--timeout", type=int, choices=range(1, 601), metavar="1..600", default=600)
    auth_status = auth_sub.add_parser("status", help="check saved binding without displaying credentials")
    auth_logout = auth_sub.add_parser("logout", help="revoke this connection and remove local binding")
    identify = auth_sub.add_parser("identify", help="report/update this connection's host product name")
    identify.add_argument("--name", "--client-name", help="any host product name; otherwise detect environment")
    for child in (auth_login, auth_wait, auth_status, auth_logout, identify):
        common(child)
    serve = sub.add_parser("serve", help="single process gateway; DIANDIAN_KEY required")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    pair = sub.add_parser("pair")
    common(pair)
    pair.add_argument("--server", required=True)
    pair.add_argument("--code", required=True)
    pair.add_argument("--name", default="Agent")
    notifications = sub.add_parser("notifications")
    common(notifications)
    notifications.add_argument("--cursor")
    notifications.add_argument("--limit", type=positive, default=50)
    ack = sub.add_parser("ack")
    common(ack)
    ack.add_argument("event_ids", nargs="+")
    history = sub.add_parser("history")
    common(history)
    history.add_argument("--project", type=positive)
    history.add_argument("--page", type=positive, default=1)
    history.add_argument("--per-page", type=positive, default=50)
    history_sub = history.add_subparsers(dest="history_command")
    status = sub.add_parser("status")
    common(status)
    status.add_argument("--task", type=int, help="task to update (otherwise show connection status)")
    status.add_argument("--state", choices=["doing", "review"])
    status.add_argument("--action-id")
    status.add_argument("--description")
    register_parsers(sub, auth_sub, history_sub)
    return parser


def legacy_status(args, session):
    """Upgrade old syntax; only a missing new endpoint permits legacy execution."""
    if not args.state or not args.action_id:
        raise CLIError("invalid_parameters", "status --task requires --state and --action-id")
    from .commands import checked_action_id
    checked_action_id(args.action_id)
    body = {"state": args.state, "actionId": args.action_id, "description": args.description}
    try:
        current = session.request("GET", f"/tasks/{args.task}")
    except CLIError as exc:
        if exc.code != "http_404":
            raise
        print("Legacy execution fallback: old server lacks versioned task detail; "
              "execute-only, no new concurrency guarantee. Upgrade the server.", file=sys.stderr)
        # The compatibility endpoint is deliberately outside /agent/tasks.
        try:
            response = session.client.post(session.config["server"] + PREFIX +
                                           f"/tasks/{args.task}/execution",
                                           headers=session.headers, json=body)
        except httpx.HTTPError:
            raise CLIError("transport_error", "legacy execution outcome unavailable; do not blindly retry",
                           action_id=args.action_id) from None
        return response_data(response, args.action_id)
    if current.get("version") is None:
        raise CLIError("capability_unavailable", "new task detail has no version; refusing unversioned execution",
                       action_id=args.action_id)
    body["ifVersion"] = version_check(current["version"], current["version"])
    session.require("task.execution")
    print("Compatibility status adapter read the latest version; use task execution --if-version "
          "for the version originally seen by the user.", file=sys.stderr)
    return session.send_write(f"/tasks/{args.task}/execution", body)


def main(argv=None):
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.profile:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.profile):
                parser.error("invalid profile")
            if args.config != "~/.config/diandian-agent/config.json":
                parser.error("use either --profile or --config")
            args.config = f"~/.config/diandian-agent/{args.profile}/config.json"
        if args.command == "serve":
            import uvicorn
            uvicorn.run("diandian_agent.app:factory", factory=True, host=args.host, port=args.port, workers=1)
            return 0
        key = getattr(args, "collaboration_key", None)
        if key in ("version", "schema", "capability.list", "capability.get"):
            target = Path(args.config).expanduser()
            if getattr(args, "offline", False) or (not target.exists() and not target.is_symlink()):
                emit(discovery(args), args.format)
                return 0
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            if args.command == "auth" and key is None:
                from .auth_flow import run_auth
                run_auth(args, client)
                return 0
            if args.command == "pair":
                from .auth_flow import pending_path, profile_lock
                with profile_lock(args.config):
                    target = Path(args.config).expanduser()
                    if target.exists() or target.is_symlink() or pending_path(target).exists():
                        raise ValueError("binding or authorization exists; use status/wait/logout")
                    server = server_url(args.server)
                    response = client.post(server + PREFIX + "/pairing/redeem",
                                           json={"code": args.code, "name": args.name})
                    data = response_data(response)
                    write_config(args.config, {"server": server, "credential": data["credential"]})
                    print(json.dumps({"connection": data["connection"]}, ensure_ascii=False))
                return 0
            config = read_config(args.config)
            session = Session(client, config, args.config)
            if key:
                result = run(args, session)
                if result is not None:
                    emit(result, args.format)
                return 0
            headers = {"Authorization": f"Bearer {config['credential']}"}
            url = config["server"] + PREFIX
            if args.command == "notifications":
                response = client.get(url + "/agent/events", headers=headers,
                                      params={k: v for k, v in {"cursor": args.cursor, "limit": args.limit}.items()
                                              if v is not None})
            elif args.command == "ack":
                response = client.post(url + "/agent/ack", headers=headers, json={"eventIds": args.event_ids})
            elif args.command == "history":
                if args.project is None:
                    parser.error("legacy history requires --project; use history list for audit")
                response = client.get(url + "/history", headers=headers,
                                      params={"projectId": args.project, "page": args.page, "per_page": args.per_page})
            elif args.task:
                emit(legacy_status(args, session), args.format)
                return 0
            else:
                response = client.get(url + "/agent/status", headers=headers)
            emit(response_data(response), args.format)
            return 0
    except CLIError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False))
        raise SystemExit(1) from None
    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
        # Never include headers, raw HTTP bodies, credentials or pairing codes.
        print(json.dumps(CLIError("request_configuration_failed",
                                 "request/configuration failed; check service and permissions").payload()))
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

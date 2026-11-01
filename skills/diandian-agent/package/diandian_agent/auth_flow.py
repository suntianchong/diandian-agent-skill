"""Browser approval for CLI agents. Browser never receives the polling secret."""
import json
import os
import stat
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from .app import PREFIX


def emit(data, stream=None):
    print(json.dumps(data, ensure_ascii=False), file=stream, flush=True)


def pending_path(config_path):
    return Path(config_path).expanduser().with_name(Path(config_path).name + ".pending")


@contextmanager
def profile_lock(config_path):
    """A nonblocking OS lock prevents two CLI processes replacing one profile."""
    target = Path(config_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = target.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("profile directory must be private")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("profile directory must belong to current user")
    fd = os.open(str(target) + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("unsafe profile lock")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("profile lock must belong to current user")
        if os.name == "nt":
            import msvcrt
            if info.st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def run_auth(args, client):
    with profile_lock(args.config):
        _run_auth(args, client)


def _run_auth(args, client):
    # Import here to share the CLI's private-file checks without an import cycle.
    import sys
    from .cli import read_config, server_url, write_config

    target = Path(args.config).expanduser()
    pending = pending_path(target)

    def cancel_pending(data):
        if data.get("deviceSecret"):
            response = client.post(data["server"] + PREFIX + f"/auth/requests/{data['requestId']}/cancel",
                                   headers={"Authorization": "Bearer " + data["deviceSecret"]})
            response.raise_for_status()

    def report_client(config, name):
        response = client.put(config["server"] + PREFIX + "/agent/client",
                              headers={"Authorization": "Bearer " + config["credential"]},
                              json={"name": name})
        response.raise_for_status()
        return response.json()

    if args.auth_command == "identify":
        from .client_identity import identify_client
        identity = identify_client(args.name)
        if not identity["name"]:
            emit({"status": "needs_client_name", "next": "auth identify --client-name <host product>"})
            return
        result = report_client(read_config(target), identity["name"])
        emit({**result, "clientNameReported": True, "nameSource": identity["source"]})
        return
    if args.auth_command == "status":
        if not target.exists():
            emit({"status": "unpaired", "next": "auth login"})
            return
        config = read_config(target)
        response = client.get(config["server"] + PREFIX + "/agent/status",
                              headers={"Authorization": "Bearer " + config["credential"]})
        response.raise_for_status()
        emit(response.json())
        return
    if args.auth_command == "logout":
        if target.exists():
            config = read_config(target)
            response = client.post(config["server"] + PREFIX + "/agent/logout",
                                   headers={"Authorization": "Bearer " + config["credential"]})
            response.raise_for_status()
            target.unlink()
        if pending.exists():
            data = read_config(pending)
            cancel_pending(data)
            pending.unlink()
        emit({"status": "unpaired", "revoked": True})
        return
    if args.auth_command == "login":
        from .client_identity import identify_client
        identity = identify_client(args.name)
        args.name = identity["name"] or "Agent"
        server = server_url(args.server)
        if target.exists():
            config = read_config(target)
            if config["server"] != server:
                raise ValueError("logout before switching servers")
            response = client.get(server + PREFIX + "/agent/status",
                                  headers={"Authorization": "Bearer " + config["credential"]})
            if response.is_success and not getattr(args, "reauthorize", False):
                result = report_client(config, args.name) if identity["name"] else response.json()
                emit({**result, "reused": True, "clientNameReported": bool(identity["name"])})
                return
            if not response.is_success and response.status_code != 401:
                response.raise_for_status()
        if pending.exists():
            data = read_config(pending)
            if not identity["name"]:
                args.name = data.get("name", "Agent")
            if data.get("status") == "starting":
                pending.unlink()
                data = None
            elif data["server"] != server or data["name"] != args.name:
                raise ValueError("another authorization is pending; wait or logout first")
            elif data["expiresAt"] <= time.time():
                cancel_pending(data)
                pending.unlink()
                data = None
        else:
            data = None
        if data is None:
            if not identity["name"]:
                emit({"status": "needs_client_name",
                      "next": "Use host context or ask once, then auth login --client-name <host product>"})
                return
            # Check local credential destination before issuing an authorization.
            write_config(pending, {"server": server, "status": "starting"})
            response = client.post(server + PREFIX + "/auth/requests", json={"name": args.name})
            response.raise_for_status()
            result = response.json()
            data = {"server": server, "name": args.name, "requestId": result["requestId"],
                    "deviceSecret": result["deviceSecret"], "expiresAt": result["expiresAt"],
                    "verificationUri": server + result["verificationPath"], "interval": result["interval"]}
            write_config(pending, data)
        emit({"status": "pending", "verification_uri_complete": data["verificationUri"],
              "agent": data["name"], "expiresAt": data["expiresAt"]}, stream=sys.stderr)
        if not args.no_browser:
            webbrowser.open(data["verificationUri"])
        if args.no_wait:
            emit({"status": "pending", "next": "auth wait"})
            return
    else:
        data = read_config(pending)
    deadline = min(data["expiresAt"], time.time() + args.timeout)
    while time.time() < deadline:
        response = client.post(data["server"] + PREFIX + f"/auth/requests/{data['requestId']}/poll",
                               headers={"Authorization": "Bearer " + data["deviceSecret"]})
        if response.status_code in (401, 410):
            cancel_pending(data)
            pending.unlink(missing_ok=True)
            emit({"status": "invalid" if response.status_code == 401 else "expired",
                  "next": "auth login"})
        response.raise_for_status()
        result = response.json()
        if result["status"] == "authorized":
            config = {"server": data["server"], "credential": result["credential"]}
            write_config(target, config)
            pending.unlink(missing_ok=True)
            reported = report_client(config, data["name"])
            emit({**reported, "clientNameReported": True})
            return
        if result["status"] == "denied":
            pending.unlink(missing_ok=True)
            raise ValueError("authorization denied")
        time.sleep(data["interval"])
    emit({"status": "pending" if time.time() < data["expiresAt"] else "expired",
          "next": "auth wait" if time.time() < data["expiresAt"] else "auth login"})
    raise TimeoutError("authorization not completed within this wait")

"""Generate host-specific local MCP settings and install project-scoped Skills."""
import argparse
import json
import os
from pathlib import Path
import sys

from .cli import server_url

CLIENTS = ("codex", "claude-code", "trae", "cursor", "gemini-cli", "vscode",
           "claude-desktop", "cline", "roo-code", "opencode", "goose", "grok-bot", "generic")


def settings(client, command, args):
    entry = {"command": command, "args": args}
    if client == "codex":
        return "[mcp_servers.diandian]\ncommand = " + json.dumps(command) + "\nargs = " + json.dumps(args) + "\n"
    if client == "vscode":
        return {"servers": {"diandian": {"type": "stdio", **entry}}}
    if client == "opencode":
        return {"mcp": {"diandian": {"type": "local", "command": [command, *args], "enabled": True}}}
    if client == "goose":
        return {"extensions": {"diandian": {"name": "diandian", "type": "stdio", "enabled": True,
                                            "cmd": command, "args": args, "envs": {}, "timeout": 60}}}
    return {"mcpServers": {"diandian": entry}}


def exclusive_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never silently replace an existing project/client file.
    with path.open("x") as handle:
        handle.write(content)


def main():
    parser = argparse.ArgumentParser(description="Prepare Diandian config for a client; never modify user-global settings.")
    parser.add_argument("--client", choices=CLIENTS, required=True)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--server", default="http://127.0.0.1:4195")
    parser.add_argument("--connection", help="distinct local name; defaults to client")
    parser.add_argument("--apply", action="store_true", help="merge into supported project config; preserve other servers")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("setup_client requires Python >=3.11; use the documented Python 3.12 environment")
    workspace = Path(args.workspace).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    connection = args.connection or args.client
    import re
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", connection):
        parser.error("connection must be 1-64 letters, digits, underscores or hyphens")
    if not workspace.is_dir():
        parser.error("workspace must already exist")
    origin = server_url(args.server)
    runtime = source_root / "_runtime" / "diandian-local"
    credential = runtime / "connections" / connection / "config.json"
    output = runtime / "client-configs" / connection
    command = sys.executable
    command_args = ["-m", "diandian_agent.mcp_server", "--config", str(credential),
                    "--server", origin, "--name", connection]
    generated = settings(args.client, command, command_args)
    text = generated if isinstance(generated, str) else json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    filename = "mcp.toml" if args.client == "codex" else "mcp.json"
    destination = output / filename
    if destination.exists():
        if destination.read_text() != text:
            raise SystemExit("Generated config differs from existing file; choose a new --connection name.")
    else:
        exclusive_write(destination, text)

    canonical = source_root / ".trae" / "skills" / "diandian-agent"
    folders = {"codex": ".agents/skills", "claude-code": ".claude/skills",
               "trae": ".trae/skills", "cursor": ".cursor/skills", "gemini-cli": ".gemini/skills"}
    if args.client in folders:
        skill = workspace / folders[args.client] / "diandian-agent"
        skill.parent.mkdir(parents=True, exist_ok=True)
        if not skill.exists() and not skill.is_symlink():
            skill.symlink_to(os.path.relpath(canonical, skill.parent), target_is_directory=True)
        elif skill.resolve() != canonical.resolve():
            raise SystemExit("An existing Skill is preserved; resolve its name conflict first.")

    paths = {"codex": ".codex/config.toml", "claude-code": ".mcp.json",
             "cursor": ".cursor/mcp.json", "gemini-cli": ".gemini/settings.json", "vscode": ".vscode/mcp.json"}
    if args.apply:
        if args.client not in paths:
            print("This host needs import in its settings UI; generated a config snippet only.")
        else:
            target = workspace / paths[args.client]
            if target.is_symlink():
                raise SystemExit("Refusing to modify a symlinked client config.")
            if args.client == "codex":
                import tomllib
                old = target.read_text() if target.exists() else ""
                parsed = tomllib.loads(old)
                existing = parsed.get("mcp_servers", {}).get("diandian")
                if existing is not None:
                    expected = {"command": command, "args": command_args}
                    if existing != expected:
                        raise SystemExit("Existing diandian MCP configuration preserved; merge manually.")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a") as handle:
                        handle.write("\n" + text)
            else:
                data = json.loads(target.read_text()) if target.exists() else {}
                key = "servers" if args.client == "vscode" else "mcpServers"
                entry = generated[key]["diandian"]
                current = data.setdefault(key, {}).get("diandian")
                if current is not None and current != entry:
                    raise SystemExit("Existing diandian MCP configuration preserved; merge manually.")
                data[key]["diandian"] = entry
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".diandian-new")
                exclusive_write(temporary, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                os.replace(temporary, target)
            print(f"Project MCP configured for {args.client}. Restart/reload that client.")
    print(f"Generated non-secret host settings: {destination}")
    print("Each host needs a DIFFERENT one-time code. Open the App, keep the code dialog open, then ask this host to call diandian_pair.")


if __name__ == "__main__":
    main()

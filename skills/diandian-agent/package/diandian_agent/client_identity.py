"""Host identity hints; arbitrary Agent-reported product names are supported."""
import argparse
import hashlib
import json
import os
import re
import unicodedata


def clean_name(value):
    name = value.strip()
    if not name or len(name) > 100 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("client name must be 1..100 visible characters")
    return name


def identify_client(explicit=None, environ=None):
    env = os.environ if environ is None else environ
    if explicit is not None:
        return {"name": clean_name(explicit), "source": "agent"}
    # Read only identity hints, never tokens, config files or process command lines.
    for key in ("DIANDIAN_CLIENT_NAME", "AGENT_CLIENT_NAME"):
        if env.get(key):
            return {"name": clean_name(env[key]), "source": "environment"}
    markers = [
        ("CODEX_THREAD_ID", "Codex"), ("CLAUDECODE", "Claude Code"),
        ("CURSOR_AGENT", "Cursor"), ("GEMINI_CLI", "Gemini CLI"),
        ("TRAE_SESSION_ID", "TRAE"),
    ]
    matches = {name for key, name in markers if env.get(key) not in (None, "", "0", "false")}
    if len(matches) == 1:
        return {"name": matches.pop(), "source": "environment"}
    # TERM_PROGRAM by itself identifies the terminal, not necessarily the Agent.
    return {"name": None, "source": "unknown"}


def profile_for(name):
    name = clean_name(name)
    normalized = name.casefold()
    slug = re.sub(r"[^a-z0-9_-]+", "-", normalized).strip("-")
    if slug == normalized and len(slug) <= 80:
        return slug
    return (slug[:60] or "agent") + "-" + hashlib.sha256(normalized.encode()).hexdigest()[:8]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", "--client-name")
    args = parser.parse_args()
    result = identify_client(args.name)
    if result["name"]:
        result["profile"] = profile_for(result["name"])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

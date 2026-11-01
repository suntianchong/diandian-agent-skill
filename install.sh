#!/bin/sh
# 一行安装：curl -fsSL https://raw.githubusercontent.com/suntianchong/diandian-agent-skill/main/install.sh | sh -s -- --host claude
# 所有参数都会透传给 skills/diandian-agent/scripts/install.py
set -eu

REPO="suntianchong/diandian-agent-skill"
REF="${DIANDIAN_SKILL_REF:-main}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

say() { printf '%s\n' "$*" >&2; }

command -v curl >/dev/null 2>&1 || { say "需要 curl"; exit 1; }
command -v tar  >/dev/null 2>&1 || { say "需要 tar"; exit 1; }
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { say "需要 Python >= 3.10"; exit 1; }

say "下载 $REPO@$REF ..."
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" | tar -xz -C "$TMP"
ROOT="$(find "$TMP" -maxdepth 1 -type d -name 'diandian-agent-skill-*' | head -n 1)"
[ -n "$ROOT" ] || { say "解压失败"; exit 1; }

if [ "$#" -eq 0 ]; then
  if [ -d "$HOME/.claude/skills" ]; then
    set -- --host claude --scope user
  elif [ -d "$HOME/.workbuddy/skills" ]; then
    set -- --host workbuddy --scope user
  elif [ -d "$HOME/.codex/skills" ]; then
    set -- --host codex --scope user
  else
    set -- --host generic --project .
  fi
  say "未指定客户端，按已存在的目录选择：$*"
fi

exec "$PY" "$ROOT/skills/diandian-agent/scripts/install.py" "$@"

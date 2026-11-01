#!/usr/bin/env python3
"""把点点 Skill 装到各种 AI 客户端的约定位置。

两种模式：
  skill    —— 复制整个 Skill 目录到宿主的 skills 目录（宿主原生支持 SKILL.md 时最佳）
  pointer  —— 在宿主读的规则/指令文件里写一段带标记的引用块，指向 SKILL.md 绝对路径

用法：
  python3 install.py --list-hosts
  python3 install.py --host claude --scope user
  python3 install.py --host cursor,windsurf --project .
  python3 install.py --target ~/my-agent/skills
  python3 install.py --host claude --uninstall
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SKILL_NAME = "diandian-agent"
MARK_START = "<!-- diandian-agent:start -->"
MARK_END = "<!-- diandian-agent:end -->"

# dest 为相对项目根或相对 home 的路径；scope=user 时基于 home，project 时基于项目目录。
HOSTS = {
    "claude": {"mode": "skill", "user": ".claude/skills", "project": ".claude/skills"},
    "trae": {"mode": "skill", "project": ".trae/skills"},
    "workbuddy": {"mode": "skill", "user": ".workbuddy/skills", "project": ".workbuddy/skills"},
    "codex": {"mode": "skill", "user": ".codex/skills", "project": ".codex/skills"},
    "opencode": {"mode": "skill", "user": ".config/opencode/skills", "project": ".opencode/skills"},
    "cursor": {"mode": "pointer", "project": ".cursor/rules/diandian-agent.mdc"},
    "windsurf": {"mode": "pointer", "project": ".windsurf/rules/diandian-agent.md"},
    "cline": {"mode": "pointer", "project": ".clinerules/diandian-agent.md"},
    "copilot": {"mode": "pointer", "project": ".github/copilot-instructions.md"},
    "gemini": {"mode": "pointer", "project": "GEMINI.md"},
    "aider": {"mode": "pointer", "project": "CONVENTIONS.md"},
    "generic": {"mode": "pointer", "project": "AGENTS.md"},
}

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "*.egg-info", ".pytest_cache")


def skill_root():
    return Path(__file__).resolve().parents[1]


def block(skill_path):
    return "\n".join([
        MARK_START,
        f"## 点点（Diandian）任务接入：{SKILL_NAME}",
        "",
        "当用户提到点点、安排/记录点点事项、点点协作、执行点点任务、安装或配对点点、读取点点任务时，",
        f"先完整读取 {skill_path}/SKILL.md 并按其步骤执行（安装、授权、CLI 命令、边界都在里面）。",
        "该 Skill 需要能执行终端命令；没有服务端或无法执行命令时，如实说明限制。",
        MARK_END,
    ])


def write_pointer(target, skill_path, force):
    target.parent.mkdir(parents=True, exist_ok=True)
    content = block(skill_path)
    if target.exists():
        text = target.read_text(encoding="utf-8")
        if MARK_START in text and MARK_END in text:
            head, rest = text.split(MARK_START, 1)
            tail = rest.split(MARK_END, 1)[1]
            text = head + content + tail
            target.write_text(text, encoding="utf-8")
            return "updated"
        if not force:
            text = text.rstrip("\n") + "\n\n" + content + "\n"
            target.write_text(text, encoding="utf-8")
            return "appended"
        text = text.rstrip("\n") + "\n\n" + content + "\n"
        target.write_text(text, encoding="utf-8")
        return "appended"
    target.write_text(content + "\n", encoding="utf-8")
    return "created"


def remove_pointer(target):
    if not target.exists():
        return "missing"
    text = target.read_text(encoding="utf-8")
    if MARK_START not in text:
        return "unchanged"
    head, rest = text.split(MARK_START, 1)
    tail = rest.split(MARK_END, 1)[1] if MARK_END in rest else ""
    remaining = (head.rstrip("\n") + "\n" + tail.lstrip("\n")).strip()
    if not remaining:
        target.unlink()
        return "removed"
    target.write_text(remaining + "\n", encoding="utf-8")
    return "cleaned"


def install_skill(dest, source, force, link, dry_run):
    if dest.exists() or dest.is_symlink():
        if not force:
            return {"status": "exists", "path": str(dest),
                    "hint": "目标已存在；加 --force 覆盖，或先 --uninstall"}
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    if dry_run:
        return {"status": "dry-run", "path": str(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    if link:
        os.symlink(source, dest, target_is_directory=True)
        return {"status": "linked", "path": str(dest)}
    shutil.copytree(source, dest, ignore=IGNORE)
    return {"status": "copied", "path": str(dest)}


def resolve(host, scope, project):
    spec = HOSTS[host]
    key = "user" if scope == "user" else "project"
    if key not in spec:
        raise SystemExit(f"{host} 不支持 --scope {scope}；可选 --scope project")
    base = Path.home() if scope == "user" else project
    return spec, base / spec[key]


def main():
    parser = argparse.ArgumentParser(description="Install the Diandian agent skill into an AI client.")
    parser.add_argument("--host", help="claude|trae|workbuddy|codex|opencode|cursor|windsurf|cline|copilot|gemini|aider|generic，可用逗号分隔")
    parser.add_argument("--scope", choices=("user", "project"), default="project")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="项目目录，默认当前目录")
    parser.add_argument("--target", type=Path, help="自定义 skills 目录（skill 模式）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的安装")
    parser.add_argument("--link", action="store_true", help="用符号链接代替复制（便于跟随源码更新）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--list-hosts", action="store_true")
    parser.add_argument("--json", action="store_true", help="只输出机器可读结果")
    args = parser.parse_args()

    if args.list_hosts:
        for name, spec in HOSTS.items():
            where = spec.get("user", "-")
            print(f"{name:10s} {spec['mode']:8s} user:{where:28s} project:{spec['project']}")
        return

    source = skill_root()
    if not (source / "SKILL.md").is_file():
        raise SystemExit(f"找不到 {source}/SKILL.md，请在完整 Skill 目录内运行")
    skill_path = source
    results = []

    targets = []
    if args.target:
        targets.append(("custom", {"mode": "skill"}, args.target.expanduser().resolve() / SKILL_NAME))
    for host in [h for h in (args.host or "").split(",") if h]:
        if host not in HOSTS:
            raise SystemExit(f"未知宿主 {host}；用 --list-hosts 查看")
        spec, dest = resolve(host, args.scope, args.project.expanduser().resolve())
        targets.append((host, spec, dest / SKILL_NAME if spec["mode"] == "skill" else dest))
    if not targets:
        raise SystemExit("请指定 --host 或 --target；用 --list-hosts 查看可用宿主")

    for name, spec, dest in targets:
        if args.uninstall:
            if spec["mode"] == "skill":
                if dest.exists() or dest.is_symlink():
                    if dest.is_symlink() or dest.is_file():
                        dest.unlink()
                    else:
                        shutil.rmtree(dest)
                    results.append({"host": name, "status": "removed", "path": str(dest)})
                else:
                    results.append({"host": name, "status": "missing", "path": str(dest)})
            else:
                results.append({"host": name, "status": remove_pointer(dest), "path": str(dest)})
            continue
        if spec["mode"] == "skill":
            results.append({"host": name, **install_skill(dest, skill_path, args.force, args.link, args.dry_run)})
        else:
            if args.dry_run:
                results.append({"host": name, "status": "dry-run", "path": str(dest)})
            else:
                results.append({"host": name, "status": write_pointer(dest, skill_path, args.force), "path": str(dest)})

    if args.json:
        print(json.dumps({"skill": str(skill_path), "results": results}, ensure_ascii=False))
        return
    print(f"Skill 源目录: {skill_path}")
    for item in results:
        print(f"- {item['host']}: {item['status']} -> {item['path']}")
    print("提示：装完请确认你的客户端会读取该目录/文件；不读取时用 --target 指定它真正的 Skill 目录。")


if __name__ == "__main__":
    main()

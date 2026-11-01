"""Prepare a local Diandian CLI environment and start human browser authorization.

stdout 只输出一行 JSON（给 Agent 解析）；人类可读进度写 stderr。
失败路径同样输出 JSON，避免 Agent 去读 traceback。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_SERVER = "http://127.0.0.1:4195"
MIN_PYTHON = (3, 10)
MIN_CLI = (0, 2)
PIP_TIMEOUT = 600

# 只读环境标识，不读 Token、配置文件或进程命令行。
HOST_MARKERS = (
    (("CODEX_THREAD_ID", "CODEX_SANDBOX"), "Codex"),
    (("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"), "Claude Code"),
    (("CURSOR_AGENT", "CURSOR_TRACE_ID"), "Cursor"),
    (("GEMINI_CLI",), "Gemini CLI"),
    (("TRAE_SESSION_ID", "TRAE_AI_SESSION"), "TRAE"),
    (("COPILOT_AGENT_SESSION_ID",), "GitHub Copilot"),
    (("AIDER_MODEL", "AIDER_CHAT_DB"), "Aider"),
    (("OPENCODE_CONFIG",), "OpenCode"),
)


def emit(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    sys.exit(code)


def log(message):
    print(message, file=sys.stderr, flush=True)


def fail(code, message, next_step=None, **extra):
    payload = {"status": "error", "code": code, "error": message}
    if next_step:
        payload["next"] = next_step
    payload.update(extra)
    emit(payload, 1)


def detect_host(explicit=None, environ=None):
    env = os.environ if environ is None else environ
    if explicit:
        return explicit.strip(), "agent"
    for key in ("DIANDIAN_CLIENT_NAME", "AGENT_CLIENT_NAME"):
        if env.get(key):
            return env[key].strip(), "environment"
    matched = {name for keys, name in HOST_MARKERS if any(env.get(k) for k in keys)}
    if len(matched) == 1:
        return matched.pop(), "environment"
    return None, "unknown"


def valid_profile(value):
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value or ""))


def profile_for(name):
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.casefold()).strip("-")
    if slug == name.casefold() and len(slug) <= 80:
        return slug
    return (slug[:60] or "agent") + "-" + hashlib.sha256(name.casefold().encode()).hexdigest()[:8]


def pick_python(requested):
    candidate = requested or os.environ.get("DIANDIAN_PYTHON")
    if candidate:
        path = Path(candidate).expanduser()
        if not path.is_file():
            fail("python_missing", f"--python 指向的文件不存在: {path}",
                 "传入可用的 Python 3.10+ 绝对路径，或用 DIANDIAN_PYTHON 指定")
        return path
    if sys.version_info < MIN_PYTHON:
        fail("python_too_old", f"需要 Python >= 3.10，当前 {sys.version.split()[0]}",
             "用 --python 指向已安装的新版 Python")
    return Path(sys.executable)


def find_package(skill, repo):
    if repo:
        package = repo / "agent-layer"
        if (package / "pyproject.toml").is_file():
            return package
        fail("package_missing", f"{repo} 下没有 agent-layer/pyproject.toml",
             "去掉 --repo，或指向完整仓库根目录")
    bundled = skill / "package"
    if (bundled / "pyproject.toml").is_file():
        return bundled
    for parent in skill.parents:
        candidate = parent / "agent-layer"
        if (candidate / "pyproject.toml").is_file():
            return candidate
    fail("package_missing", "找不到 CLI 源码（package/ 或 agent-layer/）",
         "下载完整 Skill 包（含 package/），或用 --repo 指向仓库根目录")


def venv_python(environment):
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def has_pip(python):
    try:
        return subprocess.run([str(python), "-m", "pip", "-V"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_venv(environment, base_python):
    """优先建独立环境；建不起来时退回已装好的解释器，只在确实没有依赖时才报错。"""
    python = venv_python(environment)
    if python.is_file() and has_pip(python):
        return python, []
    log(f"[onboard] 创建独立环境: {environment}")
    detail = ""
    for command in ([str(base_python), "-m", "venv", str(environment)],
                    [str(base_python), "-m", "venv", "--clear", "--without-pip", str(environment)]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as error:
            detail = f"{type(error).__name__}: {error}"
            continue
        if result.returncode == 0 and python.is_file():
            detail = ""
            break
        detail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-3:])
    if python.is_file() and not has_pip(python):
        subprocess.run([str(python), "-m", "ensurepip", "--upgrade"],
                       capture_output=True, text=True, timeout=180)
    if python.is_file() and has_pip(python):
        return python, []
    if has_pip(base_python):
        log("[onboard] 无法建独立环境，改用当前解释器（不污染系统，仅复用已装依赖）")
        return base_python, ["无法创建虚拟环境，已退回当前 Python 解释器；依赖会装进该解释器"]
    fail("venv_failed", "创建虚拟环境失败，且当前解释器不可用", "指定可写目录 --environment ~/venvs/diandian，或用 --python 指定已装 pip 的解释器",
         detail=detail)


def probe(python):
    code = (
        "import diandian_agent.client_identity, diandian_agent.commands\n"
        "from importlib.metadata import version\n"
        "v = version('diandian-agent').split('.')\n"
        "assert (int(v[0]), int(v[1])) >= MIN, v\n"
    ).replace("MIN", repr(MIN_CLI))
    return subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=60)


def install(python, package, editable, index_url, timeout):
    command = [str(python), "-m", "pip", "install", "--upgrade", "--no-input"]
    if index_url:
        command.extend(["--index-url", index_url])
    if editable:
        command.append("-e")
    command.append(str(package))
    log(f"[onboard] 安装 CLI: {' '.join(command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("install_timeout", f"pip 安装超过 {timeout}s 未完成",
             "检查网络或加 --pip-index-url 后重试")
    if result.returncode:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-8:])
        fail("install_failed", "pip 安装 CLI 失败", "检查网络/代理后重试；内网可加 --pip-index-url",
             detail=tail)


def parse_output(result):
    """stdout 是机器输出；stderr 里可能带公开授权链接。"""
    data = {}
    for stream in (result.stdout, result.stderr):
        for line in reversed((stream or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                data.update({k: v for k, v in payload.items() if v is not None})
    return data


def main():
    parser = argparse.ArgumentParser(description="Prepare the Diandian CLI and start browser authorization.")
    parser.add_argument("--server", help=f"点点服务地址；默认读 DIANDIAN_SERVER，否则 {DEFAULT_SERVER}")
    parser.add_argument("--client-name", help="宿主产品名，由 Agent 自报；任意产品名均可")
    parser.add_argument("--profile", help="本机身份名，同一宿主多个独立 Agent 时区分")
    parser.add_argument("--config", type=Path, help="改用已有配置/凭证文件，而不是 profile 目录")
    parser.add_argument("--repo", type=Path, help="完整仓库根目录（含 agent-layer/）")
    parser.add_argument("--environment", type=Path, help="专用的 Python venv 目录")
    parser.add_argument("--python", help="用指定 Python 建环境（>=3.10）")
    parser.add_argument("--pip-index-url", help="pip 镜像地址，内网环境使用")
    parser.add_argument("--pip-timeout", type=int, default=PIP_TIMEOUT, help=f"pip 安装超时秒数，默认 {PIP_TIMEOUT}")
    parser.add_argument("--editable", action="store_true", help="以可编辑方式安装 CLI（默认装副本）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只返回链接")
    parser.add_argument("--check", action="store_true", help="只检查环境与绑定，不发起授权")
    args = parser.parse_args()

    if args.profile and not valid_profile(args.profile):
        fail("invalid_profile", "profile 只能包含字母、数字、- 和 _，长度 1..80")

    server, server_source = args.server or os.environ.get("DIANDIAN_SERVER"), "argument"
    if not server:
        server, server_source = DEFAULT_SERVER, "default"
    if not re.match(r"^https?://", server or ""):
        fail("invalid_server", f"服务地址必须以 http(s):// 开头: {server}")
    if server.startswith("http://") and not re.match(
            r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/?$", server):
        fail("insecure_server", "远程服务必须使用 HTTPS；http 只能用于本机地址")
    server = server.rstrip("/")

    skill = Path(__file__).resolve().parents[1]
    package = find_package(skill, args.repo.resolve() if args.repo else None)
    base_python = pick_python(args.python)

    environment = (args.environment.expanduser().resolve() if args.environment
                   else Path.home() / ".local/share/diandian-agent/venv")
    if os.name == "nt" and args.environment is None:
        environment = Path.home() / "AppData/Local/diandian-agent/venv"
    cli_python, env_warnings = ensure_venv(environment, base_python)

    if probe(cli_python).returncode:
        install(cli_python, package, args.editable, args.pip_index_url, max(60, args.pip_timeout))
        if probe(cli_python).returncode:
            fail("cli_unusable", "CLI 安装后仍无法导入", "删除 --environment 目录重装，或查看 pip 输出")

    host, source = detect_host(args.client_name)
    if not host:
        emit({"status": "needs_client_name", "cli_python": str(cli_python),
              "next": "用你自己的宿主产品名重跑：--client-name \"<产品名>\"（不要用模型名）"})
    if len(host) > 100:
        fail("invalid_client_name", "宿主名过长（>100 字符）")

    profile = args.profile or profile_for(host)
    identity = (["--config", str(args.config.expanduser())] if args.config
                else ["--profile", profile])
    warnings = list(env_warnings)
    if server_source == "default":
        warnings.append(f"未指定服务地址，使用默认 {DEFAULT_SERVER}；远程服务请加 --server https://...")
    payload = {"cli_python": str(cli_python), "profile": profile, "clientName": host,
               "nameSource": source, "server": server, "serverSource": server_source,
               "python": str(base_python), "skill": str(skill),
               "warnings": warnings}

    if args.check:
        status = subprocess.run([str(cli_python), "-m", "diandian_agent", *identity, "auth", "status"],
                                capture_output=True, text=True, timeout=60)
        payload["auth"] = parse_output(status) or {"status": "unknown", "exit": status.returncode}
        emit({**payload, "status": "checked"})

    log("[onboard] 发起授权请求（由人在浏览器确认）")
    command = [str(cli_python), "-m", "diandian_agent", *identity, "auth", "login",
               "--server", server, "--client-name", host, "--no-wait"]
    if args.no_browser:
        command.append("--no-browser")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        fail("login_timeout", "发起授权请求超时", "确认点点服务可访问后重试")
    if result.returncode:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-8:])
        fail("login_failed", "发起授权失败", "确认服务地址可访问、版本匹配后重试", detail=tail)

    login = parse_output(result)
    if login.get("status") == "authorized":
        emit({**payload, "status": "authorized", "auth": login})
    if login.get("status") == "needs_client_name":
        emit({"status": "needs_client_name", "cli_python": str(cli_python),
              "next": "用你自己的宿主产品名重跑：--client-name \"<产品名>\"（不要用模型名）"})

    url = login.get("verification_uri_complete") or login.get("verificationUrl")
    identity_flag = f"--config {args.config}" if args.config else f"--profile {profile}"
    payload.update({"status": "pending", "authorizationUrl": url,
                    "expiresAt": login.get("expiresAt"),
                    "next": f"{cli_python} -m diandian_agent {identity_flag} auth wait --timeout 45"})
    if not url:
        payload["warnings"].append("未从 CLI 输出解析到授权链接；用同 profile 运行 auth login 查看 stderr")
    emit(payload)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - 兜底也输出 JSON
        fail("unexpected", f"{type(error).__name__}: {error}", "重跑 --check 定位；仍失败请附完整输出反馈")

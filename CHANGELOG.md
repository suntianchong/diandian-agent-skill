# Changelog

## 1.0.0 — 2026-11-01

首个公开版本（对应点点 CLI 0.2.0）。

- 把 Skill 从客户端专属目录（`.trae/skills`）移到中立的 `skills/diandian-agent/`，不再绑定单一编辑器。
- `description` 重写为「中文触发词 + 英文能力描述」，任意客户端都能命中触发条件。
- 新增 `references/hosts.md`：Claude Code / TRAE / WorkBuddy / Codex / OpenCode / Cursor / Windsurf / Cline / Copilot / Gemini CLI / Aider / 自定义宿主的安装位置与识别方式；不确定时用 `--target` 或指针模式，不猜目录。
- 新增 `scripts/install.py`：skill 复制 / 符号链接 / 规则文件引用块三种安装方式，支持 `--uninstall`、`--dry-run`、`--list-hosts`、`--json`。
- `scripts/onboard.py` 重写：
  - 所有结果（含失败）只输出一行 JSON，Agent 不必解析 traceback；
  - `--server` 改为可选，支持 `DIANDIAN_SERVER`，默认 `http://127.0.0.1:4195` 并给出 warning；
  - 直接解析出 `authorizationUrl` 放进 JSON，避免 Agent 去翻 stderr；
  - 新增 `--check` 体检、`--python` / `DIANDIAN_PYTHON`、`--pip-index-url`、`--editable`，默认装副本而非可编辑安装；
  - 宿主识别扩展，仍只读少量公开环境变量，不碰 Token 与进程参数；
  - 校验服务地址（远程必须 HTTPS）与 profile 字符集。
- 新增 `references/troubleshooting.md`：安装/授权/权限错误码与行为层面的坑（时区、抢执行、覆盖人工修改、把数据当指令）。
- 修正旧文档与 CLI 不一致处：`task list --state open` → `--state todo`；`task +agenda` 不存在 `--include-overdue/--include-undated`。
- 移除内部编号、内部路径与「编辑者」署名，补上 MIT LICENSE、README、安装脚本。

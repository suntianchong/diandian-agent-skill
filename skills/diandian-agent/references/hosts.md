# 宿主适配：装在哪、怎么被认出

点点 Skill 不绑定任何一家 AI 客户端。它只要求三件事：宿主能读到一个 `SKILL.md`、能执行终端命令、能把自己的产品名报出来。

## 安装位置速查

`install.py --host <名字>` 会写到下表位置；`--scope user` 写用户目录，`--scope project` 写当前项目。

| 宿主 | 模式 | 用户级 | 项目级 |
| --- | --- | --- | --- |
| Claude Code | skill | `~/.claude/skills/diandian-agent/` | `.claude/skills/diandian-agent/` |
| TRAE | skill | — | `.trae/skills/diandian-agent/` |
| WorkBuddy | skill | `~/.workbuddy/skills/diandian-agent/` | `.workbuddy/skills/diandian-agent/` |
| Codex | skill | `~/.codex/skills/diandian-agent/` | `.codex/skills/diandian-agent/` |
| OpenCode | skill | `~/.config/opencode/skills/diandian-agent/` | `.opencode/skills/diandian-agent/` |
| Cursor | pointer | — | `.cursor/rules/diandian-agent.mdc` |
| Windsurf | pointer | — | `.windsurf/rules/diandian-agent.md` |
| Cline | pointer | — | `.clinerules/diandian-agent.md` |
| GitHub Copilot | pointer | — | `.github/copilot-instructions.md` |
| Gemini CLI | pointer | — | `GEMINI.md` |
| Aider | pointer | — | `CONVENTIONS.md` |
| 其他/自研 | pointer | — | `AGENTS.md` |

- **skill 模式**：整目录复制（`--link` 改成符号链接）。宿主原生支持 Skill 时优先用这个，模型能直接看到 frontmatter 里的触发描述。
- **pointer 模式**：往宿主本来就会读的规则文件里写一段带 `<!-- diandian-agent:start -->` 标记的引用块，指向 `SKILL.md` 的绝对路径。适合只认规则文件的产品；重复安装会就地更新，不会堆叠多份。

产品版本更新后目录可能变。**列表只是默认值，不要把它当契约**：目录不存在就 `--target` 指到真实位置；规则文件不叫这些名字就手工把引用块贴进去。

## 宿主名怎么报

`onboard.py --client-name "<产品名>"` 是主路径，任何名字都接受。优先用你自己产品的正式名（Claude Code、Cursor、Codex、TRAE、Windsurf、Cline、Gemini CLI、GitHub Copilot、Aider、WorkBuddy、OpenCode、通义灵码、自研 Agent…）。

不要做的三件事：

1. 不要用模型名冒充宿主（"GPT"、"Claude"、"DeepSeek" 都不是产品名，会让 App 里同名连接混在一起）。
2. 不要扫进程参数、私人配置或 Token 来猜宿主；`onboard.py` 只读少量公开环境变量（`DIANDIAN_CLIENT_NAME`、`AGENT_CLIENT_NAME`、`CODEX_THREAD_ID`、`CLAUDECODE`、`CURSOR_AGENT`、`GEMINI_CLI`、`TRAE_SESSION_ID` 等）。
3. 环境里有多条线索冲突时不要猜，问用户一次。

自动识别失败时脚本返回 `{"status": "needs_client_name"}`，带上 `--client-name` 重跑即可，不会浪费授权请求。

## 同一台机器多个 Agent

```bash
python3 scripts/onboard.py --server https://... --client-name "Claude Code" --profile claude-code
python3 scripts/onboard.py --server https://... --client-name "Cursor" --profile cursor-main
```

profile 决定本机凭证隔离，两个 profile 是两个独立连接，在 App 里分别显示、分别撤权。同一个 profile 不要给两个 Agent 共用，否则来源识别会混。

## 不支持 Skill / 不能跑终端的宿主

- **有 MCP 没有 Skill**：CLI 提供 `diandian_agent.setup_client` 生成 MCP 配置，或让 CLI 复用已有 MCP 的 `--config` 完成授权；这条不是首次接入的前提。
- **只有聊天框、不能调用工具**：无法连接 localhost，也说不了"随时帮你盯着任务"。直接说明限制，别硬编流程。
- **CI / 无人值守环境**：没有人在浏览器确认，就无法完成授权。可以在有人值守的机器上授权后，把 profile 目录按部署规范带过去（注意：那里的 `config.json` 是凭证，按密钥管理，不要进仓库）。

## 跨平台

- Windows：venv 用 `Scripts\python.exe`，`onboard.py` 自动处理；命令里的路径加引号。
- macOS / Linux：默认 `~/.local/share/diandian-agent/venv`。
- 公司网络：`--pip-index-url` 指定镜像；`--python` 指向已有 3.10+ 解释器可跳过建环境。

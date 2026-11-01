# 点点 Agent Skill

让 Claude Code、Cursor、Codex、TRAE、Windsurf、Cline、Gemini CLI、GitHub Copilot、Aider 等 AI Agent 通过命令行接入**点点（Diandian）**待办服务：替人记录任务、安排日期、查看计划、按分配执行并回写状态。

一份 `SKILL.md` + CLI，不绑定具体模型，也不绑定具体客户端。

> English: a portable Agent Skill + CLI that lets any terminal-capable AI agent record, schedule and execute Diandian tasks with explicit human authorization. Bring your own Diandian server.

## 你需要什么

| 条件 | 说明 |
| --- | --- |
| 一个点点服务端 | **必需**。本仓库不含服务端，也不是公共 SaaS；安装时 `--server` 指向你部署的实例 |
| Python >= 3.10 | 安装脚本会自动建独立 venv，不污染系统环境 |
| 能执行命令的宿主 | 只有聊天窗口、不能调工具的产品用不了 |

## 快速开始

```bash
# 1. 取 Skill（推荐克隆，Agent 能直接读 SKILL.md 和 references）
gh repo clone suntianchong/diandian-agent-skill      # 或 git clone https://github.com/suntianchong/diandian-agent-skill.git

# 2. 用克隆下来的 Skill 装到你的客户端（不写 --host 时自动挑一个已存在的目录）
python3 diandian-agent-skill/skills/diandian-agent/scripts/install.py --host claude --scope user

# 3. 准备 CLI 并发起人工授权
python3 ~/.claude/skills/diandian-agent/scripts/onboard.py \
  --server https://你的点点服务地址 \
  --client-name "Claude Code"
```

不想克隆也可以用一行装（会临时下载仓库 tarball 再执行同一份 `install.py`）：

```bash
curl -fsSL https://raw.githubusercontent.com/suntianchong/diandian-agent-skill/main/install.sh | sh -s -- --host claude --scope user
```

第 2 步会返回一行 JSON，里面 `authorizationUrl` 是给人打开的链接；在浏览器里登录、选空间、勾选权限并确认后：

```bash
<cli_python> -m diandian_agent --profile <profile> auth status
```

拿到 `authorized` 就可以让 Agent 干活了：

> 帮我把"周五前交方案"记到我自己的日常，排在周四上午。

## 支持哪些客户端

| 客户端 | 安装方式 |
| --- | --- |
| Claude Code | `--host claude`（`~/.claude/skills`） |
| TRAE | `--host trae`（项目 `.trae/skills`） |
| WorkBuddy | `--host workbuddy` |
| Codex | `--host codex` |
| OpenCode | `--host opencode` |
| Cursor / Windsurf / Cline / Copilot / Gemini CLI / Aider | `--host <名字>`，写规则引用块 |
| 其他或自研 | `--target <你的 skills 目录>` 或 `--host generic`（写 `AGENTS.md`） |

完整清单与差异见 [skills/diandian-agent/references/hosts.md](skills/diandian-agent/references/hosts.md)。客户端的目录约定会随版本变化，**目录不在列表里就用 `--target` 指过去**，不要猜。

## 它做什么、不做什么

做：

- 安装/升级 CLI，用 `--check` 体检环境
- 发起浏览器授权并等待人工确认（不代填密码、不读 Token）
- 按日期记录个人任务、改期、查询今日计划和历史
- 项目拆解为事件与子任务、分别分配、按版本条件回写执行状态
- 冲突保护（`--if-version`）、失败恢复（`action resume`）、增量同步（`sync pull`）

不做：

- 不常驻、不定时提醒：只有 Agent 被唤起时才读写
- 不替代用户授权，不绕过人工确认
- 看不到手机端未上传的离线数据
- 不提供点点服务端

## 安全

- 凭证由 CLI 托管在 `~/.config/diandian-agent/<profile>/config.json`（目录 0700、文件 0600），Skill 文档明确禁止读取、打印、粘贴。
- 权限分 `read` / `manage` / `execute` 三级，创建者与执行者分离；缺权限只能重新授权，不能绕过。
- 任务标题、描述、通知正文都按数据处理，不允许当成新指令。
- 没有任何遥测；网络只连你指定的点点服务地址。

## 目录结构

```
skills/diandian-agent/
├── SKILL.md                 # 入口：首次接入、权限、日常协作、边界
├── VERSION
├── references/
│   ├── hosts.md             # 各客户端安装位置与识别方式
│   ├── install.md           # 安装、授权、重授权、卸载
│   ├── workflow.md          # 命令参考与协作流程
│   └── troubleshooting.md   # 错误码与常见坑
├── scripts/
│   ├── onboard.py           # 建环境 + 装 CLI + 发起授权，只输出 JSON
│   └── install.py           # 装到各种客户端的约定位置
└── package/                 # CLI 源码快照（pip 可安装）
```

## 开发

CLI 与 Skill 在同一个主仓库维护，本仓库是发布产物。改 Skill 后重新生成：

```bash
python3 skills/build_public_repo.py --stage ../diandian-agent-skill
```

`package/` 是从主仓库对应提交导出的快照，用 `package/VENDORED.md` 记录来源提交；不要在这里手改。

## License

MIT，见 [LICENSE](LICENSE)。

# 安装、浏览器授权与复用

## 前置条件

| 条件 | 说明 |
| --- | --- |
| 点点服务端 | 必需。`--server` 指向你已部署的实例；本机开发默认 `http://127.0.0.1:4195`。本 Skill 不含服务端，也不提供公共托管。 |
| Python >= 3.10 | 用来建独立 venv 并安装 CLI；不需要污染系统环境。 |
| 能执行命令的宿主 | Claude Code、Cursor、Codex、TRAE 等能跑终端的 Agent；纯聊天网页不行。 |

## 1. 把 Skill 放到宿主能读到的地方

用安装脚本，或手动复制整个 `diandian-agent/` 目录（`SKILL.md`、`references/`、`scripts/`、`package/` 都要保留，只复制 `SKILL.md` 会丢脚本和 CLI）。

```bash
# 看支持哪些宿主
python3 skills/diandian-agent/scripts/install.py --list-hosts
# 装到 Claude Code 的用户级目录
python3 skills/diandian-agent/scripts/install.py --host claude --scope user
# 装到当前项目（Cursor / Windsurf 等用指针文件）
python3 skills/diandian-agent/scripts/install.py --host cursor,windsurf --project .
# 宿主目录不在列表里，直接指过去
python3 skills/diandian-agent/scripts/install.py --target ~/my-agent/skills
```

宿主适配细节（哪些产品读 `skills/`、哪些读规则文件）见 [宿主适配](hosts.md)。

## 2. 准备 CLI 并发起授权

```bash
python3 skills/diandian-agent/scripts/onboard.py \
  --server https://diandian.example.com \
  --client-name "<宿主产品名>"
```

脚本做四件事：建 venv（默认 `~/.local/share/diandian-agent/venv`）→ 安装/升级 CLI → 复用有效绑定 → 否则发起授权请求。stdout 是一行 JSON，进度写 stderr。

返回示例：

```json
{"cli_python": "/home/me/.local/share/diandian-agent/venv/bin/python",
 "profile": "claude-code", "clientName": "Claude Code", "nameSource": "agent",
 "server": "https://diandian.example.com", "serverSource": "argument",
 "status": "pending",
 "authorizationUrl": "https://diandian.example.com/auth/agent?request=...",
 "next": "... -m diandian_agent --profile claude-code auth wait --timeout 45"}
```

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--server` | 服务地址；也可以设 `DIANDIAN_SERVER`。远程必须 HTTPS |
| `--client-name` | 宿主产品名，Agent 自报；也可设 `DIANDIAN_CLIENT_NAME` |
| `--profile` | 同一宿主多个独立 Agent 时区分身份 |
| `--environment` | 自定义 venv 目录 |
| `--python` | 指定用于建环境的解释器（或 `DIANDIAN_PYTHON`） |
| `--pip-index-url` | 内网/镜像源 |
| `--pip-timeout` | pip 安装超时秒数，默认 600 |
| `--check` | 只检查环境与绑定，不发起授权 |
| `--no-browser` | 不自动开浏览器，只返回链接 |
| `--editable` | 以可编辑方式装 CLI（跟进源码时用） |

## 3. 人在浏览器确认

1. 把 `authorizationUrl` 原样给用户：这是公开的请求定位链接，不是凭证。
2. 用同一个 `cli_python` 和 `profile` 等待（后台会话，别阻塞用户）：

   ```bash
   <cli_python> -m diandian_agent --profile <profile> auth wait --timeout 45
   ```

3. `--timeout` 只是这次等待的时长；超时保留请求，重跑同一条即可。授权请求十分钟后过期，过期才需要重新 `auth login`。
4. 只有 `status == "authorized"` 且退出码 0 才算绑定成功；`pending` 不是。

浏览器无法自动打开时，把链接贴给用户自己开。远端 Agent 必须使用人能访问的 HTTPS 地址——远端机器的 `localhost` 不是用户电脑。

也可以直接用已安装的 CLI：

```bash
diandian-agent --profile codex auth login --server https://diandian.example.com --name "Codex"
diandian-agent --profile codex auth status
diandian-agent --profile codex auth logout
```

`login` 默认自动打开浏览器并等待；`--no-wait` 立即返回后用 `auth wait` 领取；`--no-browser` 适合无桌面环境。有效连接会直接返回 `reused: true`。

## 4. 权限与重新授权

`read` 只读；`manage` 才能创建/改期/分配；`execute` 只能回写分配给这个 Bot 的状态。缺 manage 时重新发起授权，由人在浏览器勾选：

```bash
<cli_python> -m diandian_agent --profile <profile> auth login \
  --server <同一地址> --client-name "<宿主名>" --reauthorize
```

旧连接不会自动升级；新授权成功前保留原绑定，成功后原连接仍在，可在 App 里按需移除。不能用人的 JWT、备用码或直接调底座绕过。

## 5. 上报/纠正宿主名称

授权成功会自动上报客户端名称，有效绑定复用时也会上报。需要补报或改名：

```bash
<cli_python> -m diandian_agent --profile <profile> auth identify --client-name "任何产品名"
```

只改展示名称，Bot、空间、权限与凭证不变。名称只用于展示，不能当身份或权限证明；多个环境线索冲突时不要猜，直接问用户。

## 6. 凭证与隔离

- 每个 profile 独立存放：`~/.config/diandian-agent/<profile>/config.json`（目录 0700、文件 0600）。
- 由 CLI 管理，**不要 cat、不要打印、不要贴进日志或对话**。
- 换服务前先 `auth logout`；连接到期或被撤销（`authorization_expired`）时重新 `login`，需要人再确认一次。
- 同一 profile 的并发授权由文件锁保护；多个 Agent 想互不干扰就用不同 profile。

## 7. 备用手工配对（只在用户明确要求时）

App「Agent」页生成一次性码，保持弹层不要关。CLI：

```bash
diandian-agent pair --server <地址> --code <码> --name "<宿主名>" --profile <独立profile>
```

一码一次、十分钟有效；必须用独立 profile 或 `--config`，不要覆盖既有绑定；码不写进共享日志。

## 8. 卸载与迁移

```bash
python3 skills/diandian-agent/scripts/install.py --host claude --uninstall   # 移除 Skill
<cli_python> -m diandian_agent --profile <profile> auth logout               # 撤销服务端连接
rm -rf ~/.local/share/diandian-agent/venv                                    # 删除本机 CLI 环境（可选）
```

换机器时重跑一遍 `onboard.py` 即可；旧机器的绑定在 App 里单独移除。

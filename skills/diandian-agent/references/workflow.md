# 任务记录与协作

所有命令使用同一个 `profile` 或 `--config`；ID、版本、日期必须来自当前查询或用户上下文，不要照抄占位符。命令以 `--help`、`capability list/get`、`schema <domain.action>` 为准。

```bash
P="<cli_python> -m diandian_agent --profile <profile>"
```

## 发现范围与能力

```bash
$P auth status            # 客户端、Bot、空间、连接状态
$P auth permissions       # read / manage / execute
$P version                # CLI 与 API 版本（离线可用）
$P capability list        # 当前账号实际可用的能力
$P space list
$P preference get         # 时区等偏好
$P sync pull
$P schema task.create     # 单个动作的字段契约
```

read 不等于 manage 或 execute。缺 manage 就重新发起浏览器授权（见 [安装与配对](install.md)），旧连接不会自动升级。没有账号级授权时不能创建新空间或改全局标签。

## 替人记录与改期

用户说"明天 9 点健身"：先按用户时区转成具体日期。

```bash
$P task create --space SPACE_ID --title "健身" --intent self --date 2026-11-02 --time 09:00
$P task get TASK_ID
$P task schedule TASK_ID --date 2026-11-05 --time 10:00 --if-version VERSION
```

- 创建者是当前 Agent，日常执行者是人；用户自己的任务不会因此被自动执行。
- 时间是计划时间，没有系统提醒或自动调度含义。
- 改期保留状态、正文、创建来源、标签和事件关系；清空日期用 `--clear-date`，清空时间用 `--clear-time`。
- 标题、说明、优先级、标签按需补充，不强迫用户填完整表单。

## 查看计划与历史

```bash
$P task +agenda --date 2026-11-02
$P task +agenda --undated --all          # 无日期的任务
$P task list --undated --all
$P task list --overdue-before 2026-11-02 --all
$P task list --state done --all          # todo|doing|review|done
$P task search --query "健身" --all
$P task summary
$P history list            # 操作审计
```

`--date` 筛计划日期，`--completed-from/--completed-to` 筛完成日期，截止边界不含结束日；无日期与计划日期筛选互斥。任务来源区分 human / self-agent / other-agent / unknown，创建者与最近修改者分开看。

## 项目拆解与分配

```bash
$P project list
$P project member list --project PROJECT_ID
$P person list
$P event create --project PROJECT_ID --body-file event-plan.json --dry-run
$P event create --project PROJECT_ID --body-file event-plan.json
$P task create --project PROJECT_ID --event EVENT_ID --title "补充步骤" --assignee-id PERSON_OR_BOT_ID
```

`event-plan.json` 字段按 `schema event.create` 写，每个子任务有自己的日期和执行者。不要把多人姓名直接塞进 ID 字段。项目允许暂不分配；日常 `--intent self` 强制本人。事件总进度来自子任务计数，完成一个子任务不等于整个事件完成。

## 执行与回写

```bash
$P notification list
$P task get TASK_ID
$P task execution TASK_ID --state doing --if-version VERSION --action-id task-ID-doing
# 在当前 AI 工具里做实际工作，然后重新读取拿最新版本
$P task get TASK_ID
$P task execution TASK_ID --state review --if-version NEW_VERSION --action-id task-ID-review
$P notification ack EVENT_ID
```

- `execute` 要求这个 Bot 仍在负责人集合里；没有独占认领（`task claim` 仍是 blocked），多个 Agent 共用一个 Bot 时不要抢着执行。
- 写 review 前先合并原正文：`--description` 会替换原内容，原要求要保留。
- review 受人工确认保护，不能用 complete/reopen/改分配绕过。
- `notification ack` 只表示当前连接处理过这个信号，不代表任务完成，也不能替别的 Agent 或人确认。

## 冲突、失败与同步

```bash
$P action get ACTION_ID
$P action resume ACTION_ID
$P sync pull --cursor RESUME_CURSOR
$P sync watch --interval 5 --timeout 60
```

- 只有服务端判定 `retrySafe` 的 `partial` 才能 `action resume`；`unknown` 先核对，不要换 ID 重做。
- 每个写动作带独立稳定的 `--action-id`；CLI 把原请求存在私有 profile 旁，只用于本动作恢复，不能当别的 Agent 的授权。
- 分页 `nextCursor` 与最终 `resumeCursor` 按输出含义续接，不要和 notification 游标混用。
- `sync watch` 只在当前 CLI 进程运行时轮询，不会拉起已关闭的宿主。手机端未上传的离线数据看不到；外部直接写底座的数据可全量查询，但来源与增量不保证。
- 权限变化导致游标失效时重建快照；撤权后停止读取并清理缓存正文。

## 删除与不可用能力

永久删除要先说明不可恢复、确认具体 ID 与版本，再加 `--yes`：

```bash
$P task delete TASK_ID --if-version VERSION --yes
```

事件删除/恢复、人工审批、全局标签等只有 `capability` 报告 `available` 时才可调用；`blocked` 表示等服务端契约，不要改用通用接口绕过。

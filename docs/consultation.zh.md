<a id="optional-consultation-backend"></a>
# 可选咨询后端

[English](consultation.md) | [简体中文](consultation.zh.md)

任何本地 stdio MCP client 都可以使用自己的 agent 规划，再调用确定性的计算和存储工具。标准
[Agent 工作流](agent-workflow.zh.md)不要求 Codex 或服务端咨询。

咨询是可选的只读建议服务。内置后端在独立 worker 中运行 Codex，把仓库计算 skill 和调用方
整理的 context 提供给它，并持久化结果。建议不会提交、启动、取消或编辑任何内容。后端模型与
调用 MCP 的 client 所用模型彼此独立。

<a id="enable-the-codex-backend"></a>
## 启用 Codex 后端

MCP server 默认为 `--consultation-backend none`。此模式提供 15 个基础工具，不导入咨询
worker，也不要求 Codex、`--repo-root` 或 `skills/intensive-compute-runner/SKILL.md`。

要启用后端，先在 server 机器上安装并登录 Codex。所选 repository root 必须存在，并包含
`skills/intensive-compute-runner/SKILL.md`。使用以下命令启动 server：

```bash
determined-compute-mcp \
  --profile /absolute/path/to/profile.yaml \
  --db /absolute/local/path/to/tasks.sqlite3 \
  --owner your-owner \
  --secrets-file /absolute/path/to/credentials.env \
  --verify-ssl \
  --repo-root /absolute/path/to/repository \
  --consultation-backend codex \
  --consultation-model MODEL_ID \
  --consultation-codex-bin /absolute/path/to/codex
```

`--consultation-model` 可省略，当前默认值是 `gpt-5.6-sol`。
`--consultation-codex-bin` 也可省略，默认使用 `PATH` 中的 `codex`。`--repo-root` 也可通过
`DETERMINED_COMPUTE_REPO_ROOT` 提供。这些是部署参数，不是 MCP 工具参数。咨询会增加
`compute_consult` 和 `workflow_status`，使工具总数变为 17。

worker 把自己的表存入 `--db` 指定的同一个本地 SQLite 文件。该文件含有问题、整理后的
context、生命周期日志和结果，应按服务状态加以保护。分离后的 worker 进程必须能继续访问仓库
和数据库。

<a id="mcp-interface"></a>
## MCP 接口

`compute_consult(question, request_id, context?)` 会验证并持久化一次请求，启动分离的 worker，
并立即返回一个小型接受对象，其中包含 `workflow_id`、`request_id`、`status`、
`deduplicated` 和 `created_at`。它不会等待 Codex 完成。

`workflow_status(workflow_id)` 返回 owner 范围内持久化的状态、时间戳、最多 100 条按顺序排列
的生命周期日志、最终 `result` 或清理后的 `error`，以及 `stale` 和 `recoverable`。查询其他
owner 的 workflow 与不存在相同。服务从 MCP 进程取得 owner，调用方不能传入 owner。

状态包括 `queued`、`running`、`succeeded`、`failed` 和 `timed_out`。应轮询到终态。client
断开不会停止独立 worker。

`(owner, request_id)` 组成幂等键。使用相同问题和 context 重试会返回现有 workflow；使用不同
内容复用该键会返回 `workflow_conflict`。

问题上限为 16 KiB，最终结果上限为 64 KiB。context 是可选的有限 JSON 对象，上限为 64 KiB。
名称疑似 password、token、API key、cookie、private key、authorization、secret 或 credential
的 key 会被递归拒绝。该 key 检查不是 secret 扫描器：不要把 secret 写进问题，也不要放在
看似无害的 context key 下。仓库 skill 上限为 128 KiB。

<a id="lifecycle-and-isolation"></a>
## 生命周期与隔离

`WorkflowManager.submit()` 先提交 queued 记录，再启动分离的 Python worker。worker 通过事务
认领一个 workflow、标为 running，然后启动 Codex。heartbeat 和记录的进程 ID 用于保守恢复；
它们不代表 Determined experiment 健康状态，也不会让 shell 保持运行。

worker 使用参数数组调用 Codex，并通过标准输入提供生成的 prompt，因此不会让 shell 解释调用
方文本。调用参数包括 `--ignore-user-config`、`--ignore-rules`、`--ephemeral`、
`--sandbox read-only`、JSON event 模式、已配置模型和显式 `mcp_servers={}` 覆盖。repository root
是其工作目录。

prompt 要求 Codex 只诊断或规划，禁止 launch、cancel、submit、mutation、edit、MCP 调用和
delegation，并包含仓库计算 skill、问题及整理后的 context。只读 sandbox 和空 MCP 配置限制
模型侧操作，但不能替代主机文件系统权限。

worker 用一小组白名单重新构造环境：常规用户、locale、path、TLS、临时目录变量，以及用于
认证的 `CODEX_HOME`。Determined、存储和其他服务凭据不会传入。这也会阻止它递归调用本服务的
MCP server。

每次运行都有 wall-clock timeout，当前默认是 900 秒。超时时，worker 会终止整个 Codex 进程
组并持久化 `timed_out`。Codex 非零退出会持久化 `failed` 和长度受限的错误。成功输出从
last-message 文件读取，必要时在 64 KiB 处截断。

<a id="recovery-and-operations"></a>
## 恢复与运维

queued workflow 的 dispatch heartbeat 过期后即可恢复，当前默认阈值为 120 秒。重复同一个幂等
提交会启动替代 worker；事务认领保证只有一个 worker 能启动 Codex。

running workflow 只有在 heartbeat 已过期，且 worker 与 Codex 两个进程 ID 都不再存活时才可
恢复。服务没有自动重试循环。检查持久化状态后，操作者可以显式运行 worker 命令：

```bash
python -m determined_compute.agent_worker worker \
  --db /absolute/local/path/to/tasks.sqlite3 \
  --repo-root /absolute/path/to/repository \
  --workflow-id WORKFLOW_ID \
  --codex-bin /absolute/path/to/codex \
  --model MODEL_ID \
  --timeout-seconds 900 \
  --stale-after-seconds 120
```

有效运行仍在进行时，claim 会忽略重复 worker。重新认领过期的 running workflow 前，它会确认
记录的两个进程均已退出，并向持久化日志写入恢复事件。PID 复用可能保守地推迟恢复，直到操作
者检查记录；硬崩溃可能使只读咨询在过期间隔后再次运行。

SQLite 在单台主机上提供持久状态和事务认领，但不是分布式队列。主机重启后，应使用进程
supervisor 重新检查 queued 记录，并让分离 worker 留在拥有本地数据库和仓库的主机上。不要把
此数据库放在共享 NFS 上。

Codex 输出只是建议，不能信任为已验证事实。把建议操作传给 `compute_launch`、
`compute_cancel` 或存储传输前必须检查。升级 Codex 时，用 `codex exec --help` 核对已安装 CLI
的契约。

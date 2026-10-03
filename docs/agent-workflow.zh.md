<a id="agent-workflow"></a>
# Agent 工作流

[English](agent-workflow.md) | [简体中文](agent-workflow.zh.md)

[首页](../README.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md) · [故障排查](troubleshooting.zh.md)

任何能够调用本地 stdio MCP 工具的 agent 或客户端都可以遵循本工作流。模型由客户端自行选择。常规存储和计算操作不依赖 Codex、仓库 skill 或服务端咨询。

<a id="describe-the-goal-and-success-criteria"></a>
## 描述目标与成功判据

说明要运行的内容，以及哪项可观察结果代表成功。提供项目版本、输入与输出位置、预期产物或指标，以及已知的资源需求。只引用凭据文件或 SSH 别名，不要提供凭据值。

以下部署输入必须来自集群管理员或项目已有配置，不要自行猜测：

- Determined API 地址与账户凭据
- 已批准的镜像和资源池
- 集群计算节点宿主机路径及其容器挂载路径
- 可选的共享存储本地挂载或登录节点 SSH 访问方式

例如：“使用一个槽位评估这个版本，不排队，将 `metrics.json` 写入共享结果目录，并报告任务 ID、退出结果和该文件是否存在。”

<a id="read-local-configuration-first"></a>
## 先读取本地配置

阅读 `AGENTS.zh.md`、项目自身说明、已配置的计算 profile、相关请求示例，以及存在时的存储访问配置。复用项目中明确且仍然有效的选择。如果缺少必需部署参数，应询问用户，不要猜测。

不要为了确认配置而读取或输出凭据值。MCP 服务通过 secrets 文件或环境接收凭据。除非项目或管理员已经明确选择，否则示例中的镜像和资源池都只是占位符。

根据工作内容选择任务类型。以下四种类型由简到繁排列：

- `command` 在容器中运行一次你的命令，命令退出即结束。用于有限的非交互任务，例如评估、转换或构建。
- `shell` 提供一个可通过 SSH 连接的容器，而不是运行一个命令。用于交互调试和环境检查。
- `generic` 像 `command` 一样运行一次你的命令，此外还可以暂停以释放其槽位，之后以同一任务 ID 恢复。恢复时会在新容器中从头再次运行命令，失败后也不会自动重启，所以只用于可安全重跑的长时间批处理任务；见[暂停与恢复](#pause-and-resume)。它要求 Determined master 来自 research-cluster fork 0.40.1 或更高版本，`kind: auto` 从不选择它。
- `experiment` 将你的命令作为一个或多个 trial 运行，并增加 Determined 的实验功能：
  - searcher（在 `experiment_config.searcher` 中设置）：运行单个 trial，或在超参数空间上运行多个 trial（网格、随机，或提前停止较差 trial 的自适应搜索）；
  - 自动重启：失败的 trial（包括其 agent 丢失的情况）会重新启动，最多 `max_restarts` 次（Determined 默认值为 5）；
  - 检查点：由任务通过 Determined Core API 保存，存放在 `checkpoint_storage` 中并按保留策略（`save_trial_best`、`save_trial_latest`）管理，因此重启的 trial 可以从最新检查点继续，而不是从头开始；
  - 指标：任务通过 Core API 上报的训练和验证指标，供 searcher 比较，并由 `compute_status` 作为 trial 进度和汇总指标返回；
  - 暂停与恢复：暂停时每个 trial 会被要求保存检查点并停止，恢复时每个 trial 从其最新检查点继续。

  不使用 Core API 的任务仍有 searcher、自动重启以及暂停与恢复，但重启或恢复时会从头运行，也不会上报检查点或指标。训练、超参数搜索，以及需要在节点故障后继续的长时间或过夜任务，使用 experiment。

MCP 不接受 `kind: notebook`。

<a id="understand-the-three-path-namespaces"></a>
## 理解三种路径空间

| 路径空间 | 使用位置 | 含义示例 |
| --- | --- | --- |
| 容器路径 | `workdir`、`output_dir`、`storage_check.path`，以及 `storage_sync`/`storage_fetch.shared_dir` | Determined 任务容器内可见的路径 |
| 集群计算节点宿主机路径 | `mounts[].host_path` 和共享文件系统检查点配置 | Determined agent 挂载的路径，由部署配置提供 |
| MCP 服务端本地路径 | `storage_sync.local_dir` 和 `storage_fetch.local_dir` | 运行 MCP 服务的机器上的绝对路径 |

计算 profile 将容器路径映射到集群计算节点宿主机路径。可选存储配置再把宿主机路径映射到本地挂载，或通过 SSH 访问。显示聊天界面的机器可能不是运行 MCP 服务的机器，因此不要根据 UI 中可见的文件推测 `local_dir`。

源码、数据、依赖包、检查点和输出都应放在映射的共享存储上。`workdir` 和 `output_dir` 必须使用可写的容器路径。不要通过 Determined 发送源码归档或项目上传。

<a id="prepare-shared-files-safely"></a>
## 安全准备共享文件

如果项目已经完整地位于共享存储，且调用方提供了其路径，只使用计算 API 的工作流可以直接继续规划和提交；它不需要本地挂载、SSH 登录或存储配置。已经配置存储访问时，可用 `storage_check` 验证相关容器路径。需要准备文件或由客户端直接验证时，先配置存储访问，然后：

1. 对目标或其已有父目录调用 `storage_check(path)`。
2. 调用 `storage_sync(local_dir, shared_dir, dry_run=true)`。
3. 检查解析后的源、目标、后端、排除规则和逐项变更。
4. 仅当预览正确时，以 `dry_run=false` 调用完全相同的操作。
5. 对准备好的工作目录和所需输入再次调用 `storage_check`。

传输会复制目录内容，不会删除目标中多余的文件；但可能覆盖同名文件，因此预览是安全检查的一部分。没有存储后端时，规划仍不会验证远端文件是否存在或权限是否有效；应让任务自身验证所需输入并写出可观察的结果。SSH 认证、排除规则和传输行为见[共享存储访问](shared-storage-access.zh.md)。

<a id="check-capacity-and-avoid-accidental-queues"></a>
## 检查容量并避免意外排队

使用所需资源池和槽位数调用 `compute_resources(slots, pool)`。零槽位 command 仍需检查辅助容器容量。容量结果只是当前快照，不是资源预留。

除非用户明确要求等待，否则保持 `allow_queue: false`。容量不足或无法确定时，报告该结果。不要擅自切换资源池、改变槽位数或开启排队。

<a id="plan-review-and-launch-once"></a>
## 规划、审核并只提交一次

创建请求时填写有意义的 `name` 和 `description`，并提供选定的 `kind`、命令、容器 `workdir`、容器 `output_dir`、槽位数、`allow_queue`，以及存在时的版本或内容标识。镜像和资源池可来自计算 profile，也可使用明确批准的覆盖值。

```json
{
  "name": "evaluate-checkpoint",
  "description": "Evaluate the selected checkpoint and write metrics to shared storage.",
  "kind": "command",
  "command": ["bash", "-lc", "python scripts/evaluate.py --output \"$COMPUTE_OUTPUT_DIR/metrics.json\""],
  "workdir": "/shared-container/project/repo",
  "output_dir": "/shared-container/project/results",
  "slots": 1,
  "code_revision": "REVISION_OR_CONTENT_ID",
  "allow_queue": false
}
```

调用 `compute_plan(request)`，检查解析后的任务类型、镜像、资源池、挂载、工作目录、输出目录、资源字段和提示信息。规划只在本地验证并渲染配置，不能证明远端文件、权限、凭据或实时容量有效。

生成一个稳定且由调用方控制的 `request_id`，再调用 `compute_launch(request, request_id)`。在工作记录中保留返回的本地 `task_id` 和远端 ID。相同请求使用同一 request ID 重试是幂等的；将该 ID 用于不同内容会被拒绝。

如果提交结果不确定，不要生成新的 request ID，也不要再次提交。检查本地任务和远端系统。`compute_reconcile(task_id, remote_id)` 只用于修复这条状态不确定的本地提交，而且必须先找到相符的远端任务。参见[故障排查](troubleshooting.zh.md#submission-outcome-is-uncertain)。

<a id="monitor-and-accept-the-result"></a>
## 跟踪并验收结果

调用 `compute_status(task_id)`，直到任务进入终态；使用 `compute_logs(task_id, tail)` 检查进度和最后的消息。用户不再需要运行中的任务时，调用 `compute_cancel(task_id)`。

需要了解运行中的任务实际使用了多少 CPU、内存和 GPU 时，例如在提议调整资源、取消或重新提交之前确认 GPU 利用率是否接近零或 allocation 是否空闲，调用 `compute_usage(task_id)`；对于已结束的任务，它报告任务结束前的窗口。该工具只读，并要求 master 启用任务资源集成；`task_resources_disabled` 或 `task_resources_unsupported` 表示无法取得测量值，而不是任务空闲。先检查 `warnings`。null 或缺失值表示没有测量，绝不表示零；空的 `series` 列表表示该窗口没有数据。数值是每 `step` 秒一次的点采样，GPU 指标覆盖整块分配到的设备，可能包含其他进程。除非指定 `trial_id`，experiment 报告其最新 trial。即使 `metrics` 隐藏了 GPU 序列，`gpus` 仍会比较每个 allocation 的各块 GPU：`utilization_spread_percent` 较大、`least_utilized_gpu_uuid` 的均值很低或 `idle_fraction` 较高，都提示存在空闲或掉队的 GPU；`gpu_count` 小于 `requested_slots` 表示返回了序列的 GPU 少于该 allocation 持有的槽位，并不一定表示其余 GPU 未被使用。对于 experiment，`trial.batches_per_second_lower_bound` 是整个生命周期的下界，因为作为分母的挂钟时间还可能计入镜像拉取、启动、初始化以及因重启损失的 allocation 时间（不含调度排队时间和 allocation 之间的暂停间隔）；工作负载不通过 Determined 的 Core API 报告时，`total_batches_processed` 为 0 属于预期。只报告观察结果；更改槽位数或资源池仍需明确的任务决策。参见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

提交成功或进入终态本身不等于验收通过。检查进程退出信息和任务开始时定义的成功判据。已经配置存储访问时，使用 `storage_check` 验证预期共享产物；否则使用任务输出或另一项明确的任务内检查。需要本地副本时，先配置存储访问，再调用 `storage_fetch(shared_dir, local_dir, dry_run=true)` 预览，审核后以 `dry_run=false` 执行，并检查取回的结果。

报告本地 task ID、远端 ID、最终状态、存在时的退出结果、输出路径，以及实际观察到的产物或指标。绝不包含 token、密码、私钥、cookie 或 secrets 文件内容。

<a id="pause-and-resume"></a>
## 暂停与恢复

暂停会释放任务的槽位但不结束任务：任务保留其 ID，之后可以恢复。experiment 和 generic 任务可以暂停；command 和 shell 不能暂停，会返回 `unsupported_kind`。

暂停会通过 Determined Core API 的抢占信号要求工作负载停止，并在任务的 `preemption_timeout` 结束时停止其容器。experiment 的超时默认为一小时，以便每个 trial 保存检查点后退出；generic 任务默认为 0，即立即停止。不使用 Core API 的普通脚本会在超时结束时被停止。

恢复的方式因类型而异：

- experiment 的每个 trial 从其最新检查点继续；没有检查点的 trial 从头开始。
- generic 任务在同一任务 ID 下启动新容器，并从头再次运行命令。其命令应能在任意时刻被停止并再次启动：按单元处理工作，每个单元的输出先写入临时名称、完成后再重命名，跳过最终输出已存在的单元，并在启动时删除或重做不完整的单元。除非子任务设置了 `no_pause: true`，否则子任务会随之暂停；以 `no_pause: true` 提交的任务不能暂停。

暂停与恢复的步骤：

1. 调用 `compute_pause(task_id)`。
2. 轮询 `compute_status(task_id)`，直到 `remote_state` 为 `STATE_PAUSED`。已暂停的任务尚未结束。generic 任务在停止期间报告 `STATE_STOPPING_PAUSED`；experiment 在暂停被接受后立即报告 `STATE_PAUSED`，其 trial 可能要到超时结束才停止。
3. 需要继续时调用 `compute_resume(task_id)`，并用 `compute_logs` 确认它从检查点继续，或跳过了已完成的单元。

master 拒绝的暂停或恢复（例如暂停已暂停的任务）会以错误返回 master 给出的原因，且没有任何改变。早于 research-cluster fork 中 generic 任务修复的 master 会把被拒绝的 generic 任务请求报告为服务器错误；这些错误以 `submission_uncertain` 返回，再次尝试前先检查 `compute_status`。

`compute_cancel` 会终止 generic 任务及其所有后代。退出状态 0 使 generic 任务以 `STATE_COMPLETED` 结束；非零退出或 agent 丢失使其以 `STATE_ERROR` 结束，且不会重启。

与其他提交一样，为 generic 任务设置有意义的 `name` 和 `description`。不支持 generic 任务名称的旧 master 会拒绝它们；服务随后会去掉这两个字段提交一次，在本地记录中保留它们，并返回 `generic_task_metadata_unsupported` 警告。该警告仅供参考。此时任务在 WebUI 中没有名称，因此应记录本地 ID 和远端 ID。

<a id="discover-and-adopt-existing-remote-tasks"></a>
## 发现并登记已有远端任务

对于通过 Determined WebUI、原生 CLI 或另一台设备独立创建，且属于同一 Determined 账户的任务，使用发现和登记流程：

1. 调用 `compute_discover(kind, limit=50, offset=0)`，其中 kind 为 `command`、`shell` 或 `experiment`。这是只读远端查询，不会创建本地记录，也不会提交任务。generic 任务不能发现或接管，因为 Determined 不报告其所属账户。
2. 选择目标结果，再调用 `compute_adopt(kind, remote_id)`。
3. 保存返回的本地 `task_id`，然后用它调用 `compute_status`、`compute_logs`、`compute_usage` 和 `compute_cancel`。

登记时会核对实际集群、当前认证账户和远端 owner。它会创建幂等的本地记录，绝不会重新启动远端任务。未知的工作路径、输出路径或版本仍保持未知。登记不会授予存储访问权或新的集群权限。

Reconcile 的用途更窄：`compute_reconcile` 通过核对提交标记，修复远端接受状态不确定的已有本地提交。它不能导入独立创建的任务。如果已经存在状态不确定的本地记录，应对该记录执行 reconcile，不要登记对应的远端任务。

<a id="keep-identity-boundaries-separate"></a>
## 区分各身份边界

任务身份和访问涉及四个彼此独立的值：

| 值 | 含义 |
| --- | --- |
| SQLite 数据库 | 本地持久任务记录、幂等和 reconcile 状态 |
| `owner` | 该数据库中的命名空间；它不是身份认证 |
| Determined 账户 | 由凭据选择的 API 身份和远端权限 |
| 集群身份 | 用于避免跨集群任务混淆的实际远端集群 |

只有使用相同数据库和 owner 的会话才共享本地记录。不同数据库可以分别登记同一个远端任务。数据库应放在本地持久磁盘，不要放在共享 NFS 中。共享 owner 不等于共享凭据，更换凭据也不会重命名 owner 命名空间。

在使用 basic authorization 的 Determined fork 0.40.1 或更高版本上，只有任务的 Determined 所有者或管理员可以取消任务。submitted 记录绑定配置和端点而不是账户，因此把凭据切换到另一个账户后，`compute_cancel` 可能对 command 或 shell 返回 HTTP 403，对 experiment 返回 HTTP 404；已登记的记录则返回 `ownership_mismatch`。请使用拥有该任务的账户。

<a id="optional-consultation"></a>
## 可选咨询

客户端 agent 可以直接完成本工作流。服务端咨询默认设置为 `none`，任何确定性工具都不依赖它。部署方可以启用独立的只读 Codex 后端并指定其模型；该模型与 MCP 客户端模型彼此独立。咨询只能返回建议，不能提交或取消任务、传输文件，也不能使用调用方的 MCP 工具。参见[可选咨询](consultation.zh.md)。

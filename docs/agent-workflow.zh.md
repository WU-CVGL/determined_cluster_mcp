<a id="agent-workflow"></a>
# Agent 工作流

[English](agent-workflow.md) | [简体中文](agent-workflow.zh.md)

[首页](../README.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md) · [故障排查](troubleshooting.zh.md)

任何能够调用本地 stdio MCP 工具的 agent 或客户端都可以遵循本工作流。模型由客户端自行选择。常规存储和计算操作不依赖仓库 skill。

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
- `generic` 像 `command` 一样运行一次你的命令，并带有名称和子任务；以 `pausable: true` 提交时，还可以暂停以释放其槽位，之后以同一任务 ID 恢复。恢复时会在新容器中从头再次运行命令，失败后也不会自动重启，所以只在任务可安全重跑时才设为可暂停；见[暂停与恢复](#pause-and-resume)。它要求 Determined master 来自带有 WU-CVGL/determined#27 的 research-cluster fork，该版本能列出 generic 任务及其所有者；在较旧的 master 上，提交会在创建任何内容之前以 `unsupported` 失败。`kind: auto` 从不选择它。
- `experiment` 将你的命令作为一个或多个 trial 运行，并增加 Determined 的实验功能：
  - searcher（在 `experiment_config.searcher` 中设置）：运行单个 trial，或在超参数空间上运行多个 trial（网格、随机，或提前停止较差 trial 的自适应搜索）；
  - 自动重启：失败的 trial（包括其 agent 丢失的情况）会重新启动，最多 `max_restarts` 次（Determined 默认值为 5）；
  - 检查点：由任务通过 Determined Core API 保存，存放在 `checkpoint_storage` 中并按保留策略（`save_trial_best`、`save_trial_latest`）管理，因此重启的 trial 可以从最新检查点继续，而不是从头开始；
  - 指标：任务通过 Core API 上报的训练和验证指标，供 searcher 比较，并由 `compute_usage` 作为 trial 进度和汇总指标返回；
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

传输会复制目录内容，不会删除目标中多余的文件；但可能覆盖同名文件，因此预览是安全检查的一部分。可能重启或恢复的任务，应把每个版本放在单独的目录中，之后不要再向该目录同步；重启或恢复会运行该目录当时的内容，而 `code_revision` 仍记录原来的版本。没有存储后端时，规划仍不会验证远端文件是否存在或权限是否有效；应让任务自身验证所需输入并写出可观察的结果。SSH 认证、排除规则和传输行为见[共享存储访问](shared-storage-access.zh.md)。

<a id="check-capacity-and-avoid-accidental-queues"></a>
## 检查容量并避免意外排队

需要选择资源、回答容量问题或排查容量拒绝时，调用 `compute_resources(slots, pool)`。正槽位数检查可调度的 agent slot，零槽位检查辅助容器容量。结果只是快照，不是资源预留。`allow_queue: false` 时，`compute_launch` 会在提交前执行这项准入检查，因此不必在每次提交前单独查询容量。

除非用户明确要求等待，否则保持 `allow_queue: false`。容量不足或无法确定，或资源池不存在或对你不可用时，报告该结果；指明某个资源池的 `permission_denied` 错误表示当前账户无权使用该资源池。不要擅自切换资源池、改变槽位数或开启排队。

<a id="plan-review-and-launch-once"></a>
## 规划、审核并只提交一次

创建请求时填写有意义的 `name`（最多 128 个字符）和 `description`（最多 2,048 个字符），并提供选定的 `kind`、命令、容器 `workdir`、容器 `output_dir`、槽位数、`allow_queue`，以及存在时的版本或内容标识。镜像和资源池可来自计算 profile，也可使用明确批准的覆盖值。过长的名称或描述会在提交前以 `invalid_request` 拒绝；缩短后提交修正的请求即可。

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

调用 `compute_plan(request)`，检查解析后的任务类型、镜像、资源池、挂载、工作目录、输出目录、资源字段和提示信息。规划只在本地验证并渲染配置，不能证明远端文件、权限、凭据、资源池访问权限或实时容量有效。

调用一次 `compute_launch(request)`。它返回任务的 `kind` 和 `id`，即 Determined 自身的任务 ID：command、shell 和 generic 任务为 UUID，experiment 为整数。之后的所有工具都使用这一对值。MCP 不保存这次提交的记录，所以请把 kind、ID、名称和 `submission_marker` 写入自己的工作记录。每次调用都是一次新的提交：用同一请求再次调用 `compute_launch` 会启动第二个任务。

如果提交返回 `submission_uncertain`，说明提交未确认：Determined 可能创建了任务，也可能没有，任务也可能稍后才出现。不要自动再次提交。用错误 details 中的 `submission_marker` 和较小的 `limit` 调用 `compute_list(kind, marker=...)`。只返回一个任务时，它很可能就是这次提交，继续使用它的 ID；返回多个任务时，它们共用一份复制的配置，应交给用户判断，而不是自行选择。空结果不能证明提交失败，因为每次搜索只覆盖一页，master 也可能稍后才保存任务。报告这次未确认的提交，是否重新提交由用户决定。参见[故障排查](troubleshooting.zh.md#submission-outcome-is-uncertain)。

<a id="monitor-and-accept-the-result"></a>
## 跟踪并验收结果

调用 `compute_status(kind, id)`，直到任务进入终态；使用 `compute_logs(kind, id, tail)` 检查进度和最后的消息。用户不再需要运行中的任务时，调用 `compute_cancel(kind, id)`。

任务尚未结束时，`compute_status` 还返回 `queue`，即其作业在资源池队列中的情况。`STATE_QUEUED` 的作业正在等待，前面有 `jobs_ahead` 个作业；报告等待情况时引用这个数字；资源池的调度器不为作业排序时它为 `null`。已调度作业的 `placement` 列出每个 agent 及作业在其上持有的 GPU device ID。`queue: null` 加 `queue_note` 表示在该队列中找不到作业；加 `context_unavailable: ["queue"]` 表示查询失败，这绝不说明任务没有排队。无论哪种情况，`state` 仍是权威状态。参见[状态](compute-service.zh.md#status-logs-and-cancellation)。

需要了解运行中的任务实际使用了多少 CPU、内存和 GPU 时，例如在提议调整资源、取消或重新提交之前确认 GPU 利用率是否接近零或 allocation 是否空闲，调用 `compute_usage(kind, id)`；对于已结束的任务，它报告任务结束前的窗口。该工具只读，并要求 master 启用任务资源集成；`task_resources_disabled` 或 `task_resources_unsupported` 表示无法取得测量值，而不是任务空闲。先检查 `warnings`。null 或缺失值表示没有测量，绝不表示零；空的 `series` 列表表示该窗口没有数据。数值是每 `step` 秒一次的点采样，GPU 指标覆盖整块分配到的设备，可能包含其他进程。除非指定 `trial_id`，experiment 报告其最新 trial。即使 `metrics` 隐藏了 GPU 序列，`gpus` 仍会比较每个 allocation 的各块 GPU：`utilization_spread_percent` 较大、`least_utilized_gpu_uuid` 的均值很低或 `idle_fraction` 较高，都提示存在空闲或掉队的 GPU；`gpu_count` 小于 `requested_slots` 表示返回了序列的 GPU 少于该 allocation 持有的槽位，并不一定表示其余 GPU 未被使用。对于 experiment，`trial.batches_per_second_lower_bound` 是整个生命周期的下界，因为作为分母的挂钟时间还可能计入镜像拉取、启动、初始化以及因重启损失的 allocation 时间（不含调度排队时间和 allocation 之间的暂停间隔）；工作负载不通过 Determined 的 Core API 报告时，`total_batches_processed` 为 0 属于预期。只报告观察结果；更改槽位数或资源池仍需明确的任务决策。参见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

提交成功或进入终态本身不等于验收通过。检查进程退出信息和任务开始时定义的成功判据。已经配置存储访问时，使用 `storage_check` 验证预期共享产物；否则使用任务输出或另一项明确的任务内检查。需要本地副本时，先配置存储访问，再调用 `storage_fetch(shared_dir, local_dir, dry_run=true)` 预览，审核后以 `dry_run=false` 执行，并检查取回的结果。

报告任务的 kind 和 ID、最终状态、存在时的退出结果、输出路径，以及实际观察到的产物或指标。绝不包含 token、密码、私钥、cookie 或 secrets 文件内容。

<a id="pause-and-resume"></a>
## 暂停与恢复

暂停会释放任务的槽位但不结束任务：任务保留其 ID，之后可以恢复。experiment 以及以 `pausable: true` 提交的 generic 任务可以暂停；command 和 shell 返回 `unsupported_kind`，暂停不可暂停的 generic 任务会失败，任务继续运行。

暂停会通过 Determined Core API 的抢占信号要求工作负载停止，并在任务的 `preemption_timeout` 结束时停止其容器。experiment 的超时默认为一小时，以便每个 trial 保存检查点后退出；generic 任务默认为 0，即立即停止。不使用 Core API 的普通脚本会在超时结束时被停止。

恢复的方式因类型而异：

- experiment 的每个 trial 从其最新检查点继续；没有检查点的 trial 从头开始。
- generic 任务在同一任务 ID 下启动新容器，并从头再次运行命令。其命令应能在任意时刻被停止并再次启动：按单元处理工作，每个单元的输出先写入临时名称、完成后再重命名，跳过最终输出已存在的单元，并在启动时删除或重做不完整的单元。可暂停的子任务会随之暂停；不可暂停的子任务继续运行。

暂停与恢复的步骤：

1. 调用 `compute_pause(kind, id)`。
2. 轮询 `compute_status(kind, id)`，直到 `state` 为 `STATE_PAUSED`。已暂停的任务尚未结束。generic 任务在停止期间报告 `STATE_STOPPING_PAUSED`；experiment 在暂停被接受后立即报告 `STATE_PAUSED`，其 trial 可能要到超时结束才停止。
3. 需要继续时调用 `compute_resume(kind, id)`，并用 `compute_logs` 确认它从检查点继续，或跳过了已完成的单元。

master 拒绝的暂停或恢复（例如暂停已暂停的任务）会以错误返回 master 给出的原因，且没有任何改变。早于 research-cluster fork 中 generic 任务修复的 master 会把被拒绝的 generic 任务请求报告为服务器错误；这些错误以 `submission_uncertain` 返回，再次尝试前先检查 `compute_status`。

`compute_cancel` 会终止 generic 任务及其所有后代。退出状态 0 使 generic 任务以 `STATE_COMPLETED` 结束；非零退出或 agent 丢失使其以 `STATE_ERROR` 结束，且不会重启。

与其他提交一样，为 generic 任务设置有意义的 `name` 和 `description`；Determined 会保存它们，并在 WebUI 和 `compute_list` 中显示。

<a id="find-existing-tasks"></a>
## 查找已有任务

`compute_list(kind, limit=50, offset=0)` 按从新到旧列出已认证 Determined 账户拥有的任务，无论它们是通过本 MCP、WebUI、原生 CLI 还是另一台设备提交的。每个条目包含 kind、ID、名称、状态、资源池和开始时间，`pagination.next_offset` 指向下一页。把 kind 和 ID 用于 `compute_status`、`compute_logs`、`compute_usage`、`compute_cancel`、`compute_pause` 和 `compute_resume`。列表是只读的。generic 任务需要带有 research-cluster fork generic 任务列表（WU-CVGL/determined#27）的 master；较旧的 master 返回 `unsupported`。

指定 `marker` 时，`compute_list` 返回所选页中配置带有该提交标记的任务。它会读取该页中的每个任务，所以查找刚刚提交的任务时，应使用较小的 `limit`（例如 5 或 10），并沿 `pagination.next_offset` 查看更早的页。标记是关联标签而不是身份：在 MCP 之外复制的配置带有同一个标记，所以可能有多个任务匹配；某一页为空也不能说明任务从未创建。

<a id="ownership-and-records"></a>
## 所有权与记录

MCP server 的凭据所选定的 Determined 账户是唯一的身份。每个操作任务的工具都会先检查该账户是否拥有该任务；对其他用户的任务，即使该账户是管理员，也会以 `ownership_mismatch` 拒绝。master 无法报告所有者的 generic 任务会以 `ownership_unavailable` 拒绝。要操作其他账户的任务，请使用该账户的凭据。

MCP 不保存任务记录。任务、日志和 experiment 数据保存在 Determined 中；提交了什么以及为什么提交（例如 kind、ID、名称、版本和输出路径）由你自己记录。Determined 只在已结束的 command 或 shell 结束后 24 小时内提供它，因此应在此期间读取其日志和用量。

<a id="local-workstation-runs"></a>
## 本地工作站运行

只有用户已为当前工作授权本地执行时，才在本地工作站运行短小的单 GPU 任务。训练、长时间运行，以及大量使用 CPU 或内存的工作留在集群。集群认证或容量错误不构成本地回退授权。

使用项目已有的 Docker 命令，或通过工作站已安装的 GPU runtime 直接执行 `docker run`。从 `compute_plan` 渲染出的配置中取镜像、环境变量和 `entrypoint`，而不只是请求本身：只有渲染出的 entrypoint 包含服务的 `mkdir`/`cd` 准备步骤，只有渲染出的环境变量包含 `COMPUTE_WORKDIR`、`COMPUTE_OUTPUT_DIR`，以及请求设置了 `code_revision` 时的 `COMPUTE_CODE_REVISION`。直接 `docker run` 时，用 `--entrypoint ''` 清除镜像入口，因为 Determined 也会替换它。`entrypoint` 为列表时（command 和 generic 任务），按原顺序作为独立参数传入；为字符串时（experiment），像 Determined 一样把完整字符串作为 `sh -c` 后的一个参数。将每个本地源目录映射到所需的容器路径，并保留只读挂载。检查选定 GPU 的可用情况和主机内存，再明确设置 `--cpus` 和 `--memory` 上限，为其他工作留出余量；将 `--memory-swap` 设为与 `--memory` 相同的值，禁用容器交换空间。

以分离模式（`docker run -d`）并用唯一的 `--name` 启动容器，避免 shell 工具超时或 `docker` 客户端中断后留下无法找到和停止的运行中容器。保留容器 ID，收集日志和退出结果，并在项目选定的位置验证预期输出。运行被中断或不再继续时，停止该容器并确认它已退出。报告实际 GPU、镜像、版本和结果；本地容器没有 Determined 任务 ID。

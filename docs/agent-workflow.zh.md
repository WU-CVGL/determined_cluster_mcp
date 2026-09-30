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

根据工作内容选择任务类型：

| 类型 | 适用场景 |
| --- | --- |
| `command` | 有限的非交互任务，例如评估、转换或构建 |
| `shell` | 需要可重新连接环境的交互调试 |
| `experiment` | 训练、搜索、trial，或使用 Determined 实验功能的长时间任务 |

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

对于运行已提交代码的持久任务，如果部署配置了 `snapshots.root`，优先使用 `storage_snapshot(repo_dir, revision)`：先预览，检查被排除的类似机密的文件和警告，再以 `dry_run=false` 重复调用，并把结果中的 `request_fields.workdir` 和 `request_fields.code_revision` 写入请求。使用 include 时，内容已不再等于该提交，因此 `code_revision` 为 `<commit>+<snapshot_key>`。相同内容只发布一次，之后的任务直接复用。快照目录是只读的，因此工作负载必须写入 `output_dir`。任务需要的生成文件或未跟踪文件可用 `include` 添加。参见[发布代码快照](shared-storage-access.zh.md#publish-a-code-snapshot)。

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

调用 `compute_plan(request)`，检查解析后的任务类型、镜像、资源池、挂载、工作目录、输出目录、资源字段、提示信息和 `path_checks`。当 MCP server 能看到相应路径时，`path_checks` 报告每个 bind mount、工作目录、experiment 检查点目录和输出目录是否存在；`unverified` 表示看不到该路径，而不表示路径不存在。缺少必需路径时返回 `path_not_found`，并在 `details.missing_paths` 中列出。experiment 的检查点目录会在入口运行前被 bind mount，因此它尚不存在时，请添加 `"create_directories": ["checkpoint_storage"]`（需要时再加 `"output_dir"`），launch 会在提交前创建它。规划不能证明权限、凭据或实时容量有效。

工作负载需要特定 GPU 型号、驱动、数量或空闲显存下限时，添加 `gpu_admission`，例如 `{"names": ["APPROVED_GPU_NAME*"], "min_free_mib": 16384}`，其中的值应来自项目或管理员。任务随后会在工作负载启动前检查 `nvidia-smi` 在容器内报告的 GPU。slot 多于一个的 experiment 还需要设置 `experiment_config.resources.is_single_node: true`。参见 [GPU 准入](compute-service.zh.md#gpu-admission)。

生成一个稳定且由调用方控制的 `request_id`，再调用 `compute_launch(request, request_id)`。在工作记录中保留返回的本地 `task_id` 和远端 ID。相同请求使用同一 request ID 重试是幂等的；将该 ID 用于不同内容会被拒绝。

如果提交结果不确定，不要生成新的 request ID，也不要再次提交。检查本地任务和远端系统。`compute_reconcile(task_id, remote_id)` 只用于修复这条状态不确定的本地提交，而且必须先找到相符的远端任务。参见[故障排查](troubleshooting.zh.md#submission-outcome-is-uncertain)。

<a id="monitor-and-accept-the-result"></a>
## 跟踪并验收结果

调用 `compute_status(task_id)`，直到任务进入终态；使用 `compute_logs(task_id, tail)` 检查进度和最后的消息。用户不再需要运行中的任务时，调用 `compute_cancel(task_id)`。

需要了解运行中的任务实际使用了多少 CPU、内存和 GPU 时，例如在提议调整资源、取消或重新提交之前确认 GPU 利用率是否接近零或 allocation 是否空闲，调用 `compute_usage(task_id)`；对于已结束的任务，它报告任务结束前的窗口。该工具只读，并要求 master 启用任务资源集成；`task_resources_disabled` 或 `task_resources_unsupported` 表示无法取得测量值，而不是任务空闲。先检查 `warnings`。null 或缺失值表示没有测量，绝不表示零；空的 `series` 列表表示该窗口没有数据。数值是每 `step` 秒一次的点采样，GPU 指标覆盖整块分配到的设备，可能包含其他进程。除非指定 `trial_id`，experiment 报告其最新 trial。即使 `metrics` 隐藏了 GPU 序列，`gpus` 仍会比较每个 allocation 的各块 GPU：`utilization_spread_percent` 较大、`least_utilized_gpu_uuid` 的均值很低或 `idle_fraction` 较高，都提示存在空闲或掉队的 GPU；`gpu_count` 小于 `requested_slots` 表示返回了序列的 GPU 少于该 allocation 持有的槽位，并不一定表示其余 GPU 未被使用。对于 experiment，`trial.batches_per_second_lower_bound` 是整个生命周期的下界，因为作为分母的挂钟时间还可能计入镜像拉取、启动、初始化以及因重启损失的 allocation 时间（不含调度排队时间和 allocation 之间的暂停间隔）；工作负载不通过 Determined 的 Core API 报告时，`total_batches_processed` 为 0 属于预期。只报告观察结果；更改槽位数或资源池仍需明确的任务决策。参见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

使用 `gpu_admission` 时，日志中有一行 `determined-compute gpu_admission: passed|failed ...`，`output_dir` 中有 JSON 回执及其 `.jsonl` 历史。策略未通过时，预检会在工作负载启动前以退出码 86 结束；但工作负载自身也可能以 86 退出，因此退出码只能作为提示：请通过 `determined-compute gpu_admission: failed` 日志行，或 `determined.allocation_id` 与该任务 allocation 相符的 `.jsonl` 记录确认准入失败（`compute_usage` 列出每个 allocation 的 `allocation_id` 和 `exit_reason`）。请报告该记录中的 `failures`，因为 `.json` 回执只保存最近一条记录，可能属于共享同一 `output_dir` 的其他 trial 或任务。未经明确决策不要更改策略或资源池。在 experiment 中，每次准入失败都会消耗一次重启。

提交成功或进入终态本身不等于验收通过。检查进程退出信息和任务开始时定义的成功判据。已经配置存储访问时，使用 `storage_check` 验证预期共享产物；否则使用任务输出或另一项明确的任务内检查。需要本地副本时，先配置存储访问，再调用 `storage_fetch(shared_dir, local_dir, dry_run=true)` 预览，审核后以 `dry_run=false` 执行，并检查取回的结果。

报告本地 task ID、远端 ID、最终状态、存在时的退出结果、输出路径，以及实际观察到的产物或指标。绝不包含 token、密码、私钥、cookie 或 secrets 文件内容。

<a id="discover-and-adopt-existing-remote-tasks"></a>
## 发现并登记已有远端任务

对于通过 Determined WebUI、原生 CLI 或另一台设备独立创建，且属于同一 Determined 账户的任务，使用发现和登记流程：

1. 调用 `compute_discover(kind, limit=50, offset=0)`，其中 kind 为 `command`、`shell` 或 `experiment`。这是只读远端查询，不会创建本地记录，也不会提交任务。
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

submitted 记录还绑定提交它的计算配置和端点。在同一端点和集群标签下，其他配置仍可只读地查询状态、日志和用量：服务会先验证任务所有者和提交标记，结果中的 `binding.mode` 为 `cross_profile`，`mutations_allowed` 为 `false`。取消与调和仍要求原来的配置，否则返回 `binding_mismatch`；配置改变后的 launch 重试返回 `idempotency_conflict`，因为配置指纹是请求哈希的一部分。请使用原来的配置取消、调和或重试。当 Determined 不再提供任务实体时（例如 command 或 shell 结束超过 24 小时，或 master 重启之后），跨配置读取返回 `cross_profile_unverifiable`；请使用原来的配置读取其日志和用量。`compute_list_tasks` 会显示每条记录的离线 `binding`。

只有使用相同数据库和 owner 的会话才共享本地记录。不同数据库可以分别登记同一个远端任务。数据库应放在本地持久磁盘，不要放在共享 NFS 中。共享 owner 不等于共享凭据，更换凭据也不会重命名 owner 命名空间。

在使用 basic authorization 的 Determined fork 0.40.1 或更高版本上，只有任务的 Determined 所有者或管理员可以取消任务。submitted 记录绑定配置和端点而不是账户，因此把凭据切换到另一个账户后，`compute_cancel` 可能对 command 或 shell 返回 HTTP 403，对 experiment 返回 HTTP 404；已登记的记录则返回 `ownership_mismatch`。请使用拥有该任务的账户。

<a id="optional-consultation"></a>
## 可选咨询

客户端 agent 可以直接完成本工作流。服务端咨询默认设置为 `none`，任何确定性工具都不依赖它。部署方可以启用独立的只读 Codex 后端并指定其模型；该模型与 MCP 客户端模型彼此独立。咨询只能返回建议，不能提交或取消任务、传输文件，也不能使用调用方的 MCP 工具。参见[可选咨询](consultation.zh.md)。

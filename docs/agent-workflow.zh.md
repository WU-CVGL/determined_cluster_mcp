<a id="agent-workflow"></a>
# Agent 工作流

[English](agent-workflow.md) | [简体中文](agent-workflow.zh.md)

[首页](../README.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md) · [故障排查](troubleshooting.zh.md)

本工作流适用于任何能调用本地 stdio MCP 工具的 agent 或客户端。客户端自行选择模型。常规存储和计算工作不需要仓库中的 skill。

<a id="describe-the-goal-and-success-criteria"></a>
## 描述目标与成功判据

说明要运行什么，以及哪种可观察结果算作成功。包括项目版本、输入和输出位置、预期产物或指标，以及已知资源需求。只引用凭据文件或 SSH 别名，不写凭据值。

以下部署参数必须来自集群管理员或项目现有配置，不要自行编造：

- Determined API 地址和账户凭据
- 经批准的镜像和资源池
- 计算节点宿主机路径及其容器挂载路径
- 可选的共享存储本地挂载或登录节点 SSH 访问

有用的请求示例：“用一个 slot 评估这个提交，把 `metrics.json` 写到共享结果目录，并报告任务 ID、退出类别以及该文件是否存在。”

<a id="read-local-configuration-first"></a>
## 先读取本地配置

阅读 `AGENTS.md`、项目自身说明、已配置的策略、`cfg/examples/` 中相关的请求示例，以及存在时的存储访问配置。沿用项目中当前且明确的选择。缺少必需的部署参数时应询问，不要猜测。

不要为了确认配置而读取或打印凭据值。MCP server 通过 secrets 文件或环境变量获得凭据。除非项目或管理员明确选定，示例中的镜像和资源池只是占位符。

按工作内容选择任务类型：

| 类型 | 用途 |
| --- | --- |
| `command` | 有限的非交互运行，例如评估、转换或构建 |
| `shell` | 需要可重连环境的交互式调试 |
| `experiment` | 训练、搜索、trial，或使用 Determined 实验功能的长时间工作 |

<a id="understand-the-three-path-namespaces"></a>
## 理解三种路径空间

| 路径空间 | 使用者 | 作用示例 |
| --- | --- | --- |
| 容器路径 | `output_dir`、`git` 的 `code.repo`、`path` 的 `code.dir`、`storage_check.path`，以及传输的 `shared_dir` | Determined 任务内可见的路径 |
| 计算节点宿主机路径 | 策略中的 `mounts[].host_path` | 管理员在每个 agent 上挂载的路径 |
| MCP server 本地路径 | `context` 的 `code.repo`，以及传输的 `local_dir` | 运行 MCP server 的机器上的绝对路径 |

策略把容器路径映射到计算节点宿主机路径。可选的存储配置再把这些宿主机路径映射到本地挂载，或通过 SSH 访问。显示对话的机器可能不同于运行 MCP server 的机器，因此不要根据界面中看到的内容推断本地路径。`workdir` 相对于代码根目录。

<a id="choose-how-code-reaches-the-task"></a>
## 选择代码进入任务的方式

| 来源 | 适用情况 | 规划固定的内容 |
| --- | --- | --- |
| `git` | 仓库位于共享存储上，且其根目录在本机已挂载 | 提交；任务克隆该提交，不上传任何内容 |
| `context` | 代码在本地工作树中，且不超过 99,614,720 字节 | 提交和上传文件的清单 |
| `path` | 代码必须在共享目录中原地运行，例如在 shell 中 | 不固定：内容为 `unpinned` |

本版本只通过本地挂载读取仓库来规划 `git` 代码；通过 SSH 时返回 `storage_not_local`。运行所需的内容都要提交：`git` 的提交必须位于某个分支或标签上。数据、依赖包、检查点和输出应放在映射的共享存储上，不要放进 `context` 上传。

<a id="prepare-shared-files-safely"></a>
## 安全准备共享文件

如果项目已完整存在于共享存储中，只进行计算的工作流仅凭 Determined 认证即可继续规划和提交。需要暂存文件或在客户端验证时，先配置存储访问，然后：

1. 对目标或其已存在的父目录调用 `storage_check(path)`，并阅读其中的 `viewpoint`：权限属于本地或 SSH 用户，而不是容器用户。
2. 调用 `storage_sync(local_dir, shared_dir, dry_run=true)`。
3. 检查解析后的源路径、目标路径、后端、排除项和逐项变更。
4. 仅当预览正确时，才以 `dry_run=false` 执行完全相同的操作。
5. 再次对准备好的输入调用 `storage_check`。

传输复制的是目录内容，不会删除目标中多余的文件。未设置 `overwrite` 时，所有已有文件以及已有目录的属性都保持不变。SSH 认证、排除规则和传输行为见[共享存储访问](shared-storage-access.zh.md)。

<a id="plan-review-and-launch-once"></a>
## 规划、审核并只提交一次

编写一个 `TaskSpec`：有意义的 `name`、`kind`、`command`、`code` 来源、位于可写共享存储上的 `output_dir`，以及 slots 数。除非有经批准的覆盖值，镜像和资源池来自策略。

```json
{
  "kind": "command",
  "name": "evaluate-checkpoint",
  "command": "python scripts/evaluate.py --output \"$COMPUTE_OUTPUT_DIR/metrics.json\"",
  "code": {"source": "git", "repo": "/shared-container/project/repo", "revision": "main"},
  "output_dir": "/shared-container/project/results/evaluate-checkpoint",
  "slots": 1
}
```

调用 `compute_plan(spec)`。它固定版本、应用策略，并在 master 上对完全相同的请求做 dry run；不创建任何内容。审核解析后的 `spec`、`commit`、`code` 摘要（`context` 的 `included` 和 `excluded` 路径）、`effective_config` 以及每条警告。`path_not_bind_mounted` 表示任务无法访问某个路径；`secret_like_included` 表示某个 include 会上传看起来像 secret 的文件。master 和资源池默认值按提交时的值生效。

提交前不评估放置。`compute_resources` 以快照形式显示资源池及其设备型号；slots 超过资源池当前容量的任务会在队列中等待。不要悄悄更换资源池或 slots 数，这是工作负载层面的决定。

用规划返回的值调用 `compute_launch(spec, request_id, request_digest)`；传入返回的 `spec`，而不是原始 spec。在工作记录中保存 `job_id` 和 `request_id`。

- 超时、响应丢失或返回 `unavailable` 后，用相同的 `spec`、`request_id` 和 `request_digest` 重复完全相同的提交。即使工作树已变化，它也会返回同一任务，且 `replayed: true`，绝不会创建第二个任务。也可以在 `compute_list` 中按 `request_id` 查找该任务；列表覆盖该账户的所有客户端。
- 如果返回 `internal`，再重复一次；第二次仍是同样错误说明没有创建任何内容。
- 如果返回 `plan_changed`，说明没有创建任何内容：规划之后代码或请求发生了变化。请重新规划，并在提交前审核新的提交。
- 超时或其他结果不确定的情况下，绝不要用新的 `request_id` 重新提交。新规划会生成新的键，master 会在可能已存在的任务之外再创建一个任务。

<a id="monitor-and-accept-the-result"></a>
## 跟踪并验收结果

调用 `compute_status(job_id)` 直到任务结束，并用 `compute_logs(job_id, tail=...)` 查看进度和最终信息；对实验，`trial_id` 选择 trial。`explanation` 解读状态：trial 仍在等待资源的活动实验显示为 `running`，说明中会写明它在等待调度器。不再需要的任务用 `compute_cancel(job_id)` 取消。

调用 `compute_usage(job_id)` 查看任务使用了多少 CPU、内存和 GPU，例如在建议调整规模或取消前发现空闲 GPU。它是只读的；`measurement: "unmeasured"` 表示 master 没有 task-resources 集成，而不是任务空闲。先检查 `warnings`。空值或缺失值表示没有测量，绝不表示零；`series` 为空表示该窗口没有数据。GPU 指标覆盖整张分配的设备。实验默认报告最新的 trial，`trial_id` 或 `allocation_id` 可以选择其他 trial。`gpus` 比较每个 allocation 的 GPU：`utilization_spread_percent` 大、`least_utilized_gpu_uuid` 的均值低或 `idle_fraction` 高，都提示存在空闲或拖后的 GPU。`trial.batches_per_second_lower_bound` 是整个生命周期的下限；工作负载不通过 Determined Core API 报告进度时，`total_batches_processed` 为 0 属于正常。见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

任务已提交或已结束，本身都不等于验收通过。检查任务的 `exit_class`、日志以及开始时定义的成功判据。prelude（代码交付、`output_dir` 或 `workdir`）失败时会打印一行以 `compute:` 开头的信息，并归类为 `workload_failed`。配置了存储访问时，用 `storage_check` 验证预期的共享产物；需要本地副本时，先预览 `storage_fetch(shared_dir, local_dir, dry_run=true)`，审核后再以 `dry_run=false` 执行。

报告任务 ID、request ID、最终状态、退出类别、输出路径以及观察到的产物或指标。绝不包含 token、密码、私钥、cookie 或 secrets 文件内容。

<a id="report-failures-as-failures"></a>
## 如实报告失败

任务可以失败，但失败绝不能报告为成功；结果不确定的工作也绝不会被悄悄重新运行。

- 如实报告 `failed` 或 `canceled` 的任务，并附上 `exit_class`、`exit_reason` 以及说明原因的日志行，例如 prelude 打印的 `compute:` 行。任务结束不等于工作完成，未经检查的输出也不算结果。
- 绝不自动重新提交，无论是在失败、取消、`plan_changed` 之后，还是提交结果不确定时。重复完全相同的提交不算重新提交：它返回已有的任务。新的运行是一次新的规划，带新的 `request_id`，需经审核并由用户决定。
- 除实验自身 `max_restarts` 允许的重启外，没有任何机制会替你重试失败的任务。
- 请求被拒绝时（例如 `admission_unsupported`、`protocol_unsupported`，或策略、代码检查拒绝），停止并报告该拒绝。不要改用其他资源池、代码来源或工具绕过它。

<a id="keep-identity-boundaries-separate"></a>
## 区分各身份边界

已认证的 Determined 账户拥有它提交的每个任务，记录保存在 master 上。没有本地数据库或 owner 命名空间：同一账户的任何客户端都看到相同的任务，`request_id` 属于该账户的任务。在 basic 授权下，只有任务所有者或管理员可以取消任务，因此请使用提交该任务的账户。写在命令或 `env` 中的 secret 会保存在任务配置里，能读取任务的人都能看到；应改为保存在共享存储上的文件中。

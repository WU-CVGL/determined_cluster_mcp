<a id="architecture"></a>
# 架构

[English](architecture.md) | [简体中文](architecture.zh.md)

本文描述计算 MCP 的目标架构，以及它要求 Determined fork 做出的改动，面向两个仓库的维护者。当前的 MCP 接口见[计算服务参考](compute-service.zh.md)。

Fork 路径相对于 `8e26a69`（release 0.40.1）处的 fork 根目录，MCP 路径相对于本仓库的 `2404d0d`。“PR #1” 指尚未合并的 `feat/research-workflow-support` 分支。所有引用都来自阅读源码，而不是实际运行；它们指向上述基准提交，不会随代码变化而维护。

<a id="what-changes-for-users"></a>
## 对用户的变化

- **代码作为任务 context 发送。** 作业以 Determined 现有的任务 context 携带某个 git revision
  中被跟踪的文件及显式 include 的文件，上限约 95 MiB。代码不再从共享存储上可变的 `workdir`
  运行。数据、输出和检查点仍在共享存储上，因此超过上限的仓库（例如包含数据的仓库）需要把这些
  数据移到共享存储。
- **单一句柄。** `job_id` 取代本地 `task_id`，`--owner` 和 `--db` 被移除。同一账户的所有客户端
  看到并控制相同的作业。
- **`allow_queue=false`。** 在 MCP 1.0 中，它先进行评估，只提交当前可以放置的请求，这是某一时刻的
  检查；从 MCP 1.1 起，它是原子的立即准入。
- **更少的工具。** reconcile、discover、adopt、resources、`storage_check` 和 `determined-compute`
  CLI 被移除；由 `compute_list` 和 `det` 覆盖其用途。

<a id="principles"></a>
## 原则

1. **每项能力都放在其事实来源所在的位置。**
   - master 负责身份、幂等、准入、放置和失败类别。
   - agent 负责节点事实。
   - 工作负载自行创建所需目录。
   - MCP 负责研究意图、比 RBAC 更窄的单次调用策略、用户的工作树以及结果解读。
2. **job 行就是台账。** `tasks.job_id`、`experiments.job_id` 和 `jobs.owner_id` 已经存在（`master/pkg/model/task.go:78`、`experiment.go:336`、`job.go:87-94`）。`job_id` 是唯一的句柄。
3. **单一任务契约。** 现有的四种创建请求就是契约，评估就是对同一请求执行 `dry_run`。
4. **由调度器决定。** 评估和立即准入复用资源池自身的 `Schedule` 和 `findFits`，任何地方都不重新推导放置。
5. **失败在发生处确定类型，并且只记录一次。** 每个结束的 allocation 恰好得到一个退出类别。
6. **没有兼容路径。** 只有一个最低 fork 版本和一次版本检查，不做探测，也没有客户端替代实现。

<a id="layers-and-responsibilities"></a>
## 分层与职责

| 能力 | 负责方 | MCP 保留的部分 |
|---|---|---|
| 身份、owner、提交的请求 | `jobs` 行上的新列 | `job_id` |
| 幂等提交 | master，在 `(owner_id, idempotency_key)` 上唯一 | 由 `compute_plan` 生成的 `request_id` |
| spec 合并、默认值、bind mount | master 创建路径、资源池 `task_container_defaults`、模板 | 渲染为创建请求的 `TaskSpec`，显式指定资源池和 slot |
| 容量与放置的答复 | RM 评估，经由 `dry_run` 提供 | 呈现结论 |
| 排队或立即准入 | RM 调度 tick，在 `rp.mu` 下进行 | 把 `allow_queue` 映射为准入方式 |
| GPU 型号、总显存、单节点 | 调度器硬约束 | spec 字段 |
| 空闲显存、利用率、挂载源 | agent，在 `CreateContainer` 之前 | 无 |
| 输出目录 | 工作负载 | 渲染 `mkdir -p` |
| 退出类别与重试 | allocation、trial、command | 解释类别 |
| 跨客户端的状态、列表、取消 | `GetSubmission`、`ListSubmissions`、`CancelSubmission` | 透传 |
| 代码交付 | 现有的 context 目录或 model definition | git 枚举、secret 规则、清单 |
| 数据、输出、检查点 | 通过资源池 bind mount 访问的共享存储；`checkpoint_storage` | 文件传输（`storage_sync`、`storage_fetch`） |
| 条件触发的启动 | 基于创建 envelope 的独立服务（如果将来构建） | 无 |
| 资源池允许列表、最大 slot 数、`allow_queue`、`overwrite` | MCP | 全部 |
| 用量解读 | MCP | 全部 |
| 跨客户端配额 | 现有的组 `max_slots` 和配置策略 | 无 |

<a id="platform-changes"></a>
## 平台改动

<a id="ledger-columns-on-jobs"></a>
### `jobs` 上的台账列

迁移文件为 `2026MMDD000000_job-submissions.tx.up.sql`。按照 fork 自身的先例，它配有 `.down.sql`。

```sql
ALTER TABLE jobs
  ADD COLUMN idempotency_key text,
  ADD COLUMN request_digest  text,
  ADD COLUMN request         jsonb,                        -- canonical, redacted
  ADD COLUMN admission       text NOT NULL DEFAULT 'QUEUE';
CREATE UNIQUE INDEX jobs_owner_idempotency_key ON jobs (owner_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

- **不回填。** 升级前创建的 job 显示为 NULL，其提交时间取自 `tasks.start_time` 或 `experiments.start_time`。
- **仅限受管创建。** 只有受管创建路径接受键，因为 `PutExperiment` 在重放时会泄漏一个 job 行（`master/internal/db/postgres_experiments.go:436-462`）。
- **重放语义**沿用动态资源池：相同 hash 时重放，不同 hash 时冲突（`master/internal/db/postgres_dynamic_resource_pools.go:56-116`）。

<a id="create-envelope"></a>
### 创建 envelope

`LaunchCommand`、`LaunchShell`、`CreateGenericTask` 和 `CreateExperiment` 各增加一个可选字段，不新增任何创建 endpoint。

```proto
enum Admission { ADMISSION_UNSPECIFIED = 0; ADMISSION_QUEUE = 1; ADMISSION_IMMEDIATE = 2; }
message SubmitOptions {
  string idempotency_key = 1;  // optional; <= 128 chars of [A-Za-z0-9._:-]
  Admission admission = 2;     // UNSPECIFIED means QUEUE
  bool dry_run = 3;
}
message SubmitResult {
  string job_id = 1;                           // empty on dry_run
  bool replayed = 2;
  string request_digest = 3;
  AdmissionOutcome outcome = 4;                // QUEUED | PLACED | REJECTED | PENDING
  SchedulingEvaluation evaluation = 5;         // dry_run only
  google.protobuf.Struct effective_config = 6; // dry_run only, secrets stripped
}
// <Create>Request  += SubmitOptions submit
// <Create>Response += SubmitResult  submission   (on replay only this field is set)
```

`validate_only` 保留为 `dry_run` 的别名，因为 CLI 会发送它（`harness/determined/cli/experiment.py:188`）。

<a id="submit-handler-order"></a>
### 提交处理顺序

由一个共享的 `submission` 包为全部四个 handler 实现以下顺序。

1. **摘要。** 规范化客户端请求并计算摘要。此时不运行任何有副作用的操作。
2. **重放。** 如果提供了键，查找 `(owner_id, key)`。摘要相同时，对已存储的 job 检查读取授权，并以 `replayed=true` 返回该 job；摘要不同时，返回 `ALREADY_EXISTS`，并指明已有的 `job_id`。
3. **解析。** 解析、合并、授权并应用配置策略。会话签发和 shell 密钥生成移到第 4 步之后。目前它们运行得过早：
   - `master/internal/core_experiment.go:401`，早于 `api_experiment.go:1655` 处的 `ValidateOnly` 返回；
   - `api_command.go:166-170`；
   - `api_generic_tasks.go:153-158`；
   - `api_shell.go:263-269`。
4. **Dry run。** 如果设置了 `dry_run`，执行评估并返回。此时尚未写入任何内容。
5. **事务提交。** 由一个事务写入：
   - job 行，包括键、摘要、请求和准入方式；
   - 对于任务：task 行、context 目录、`allocation_workspace_info`、状态为 `PENDING` 的第一条 allocation 行，以及 `command_state`（包括 `generic_task_spec`）。目前 `command_state` 在 `StartAllocation` 之后才写入（`command/command.go:181-186`、`api_generic_tasks.go:378`），两者之间发生崩溃会留下一个永远不会被恢复的任务。由于该行现在已经存在，`requestResources` 改为加载它，而不是插入它（`task/allocation.go:523-529`）。
   - 对于 `activate=true` 的 experiment：experiment 行，以 `ACTIVE` 状态提交。

   遇到唯一索引冲突时，回滚事务，删除已签发的会话和 `GroupPriorityChangeRegistry` 条目（`command/command.go:117-119`），然后回到第 2 步。起保护作用的是索引，而不是 `cs.mu`。
6. **启动。** 调用 `StartAllocation` 或 `e.Start`。如果在进程内失败，以 `INFRASTRUCTURE_FAILED` 关闭 `PENDING` allocation 并结束任务，或把 experiment 标记为 `ERROR`。此后的重放会返回一条终态记录。
7. **等待（仅 IMMEDIATE）。** 启动调用返回后，在 `cs.mu`（`command/command_service.go:95`）之外，通过 allocation service 的读锁轮询该 allocation，直到它离开 `PENDING`。等待上限为 5 秒，超时后结果为 `PENDING`。

**恢复规则。** `start_time IS NULL` 的 allocation 从未被放置。`RestoreAllCommands` 和 `restoreGenericTasks`（`command_service.go:55-72`、`core.go:880-955`）按准入方式处理它：

- QUEUE：以同一个 allocation ID 重新发起全新请求。
- IMMEDIATE：以 `PLACEMENT_UNSATISFIED` 结束它。

这与 trial 的 `IsReattachableOnlyAfterStarted`（`trial.go:807-809`）一致。它还修复了目前在重启时仍处于排队状态的 command 出现的 `RestoreError` “0 container snapshots”（`rm/agentrm/resource_pool.go:189-190`）。

<a id="durable-reads-and-cancel"></a>
### 持久读取与取消

```proto
rpc GetSubmission(GetSubmissionRequest) returns (GetSubmissionResponse);          // GET  /api/v1/submissions/{job_id}
rpc ListSubmissions(ListSubmissionsRequest) returns (ListSubmissionsResponse);    // GET  /api/v1/submissions
rpc CancelSubmission(CancelSubmissionRequest) returns (CancelSubmissionResponse); // POST /api/v1/submissions/{job_id}/cancel

message ListSubmissionsRequest { optional int32 owner_id = 1; /* default: caller */
  Kind kind = 2; State state = 3; google.protobuf.Timestamp submitted_after = 4;
  int32 limit = 5; string page_token = 6; }
message Submission {
  string job_id = 1; Kind kind = 2;          // COMMAND | SHELL | GENERIC | EXPERIMENT
  string entity_id = 3;                      // task_id or experiment id
  int32 owner_id = 4; string owner = 5; int32 workspace_id = 6; optional int32 project_id = 7;
  string name = 8;
  optional string idempotency_key = 9;       // owner and admins only
  optional string request_digest = 10;       // owner and admins only
  google.protobuf.Struct request = 11;       // redacted; absent before the upgrade
  Admission admission = 12;
  google.protobuf.Timestamp submitted_at = 13; optional google.protobuf.Timestamp ended_at = 14;
  State state = 15;        // QUEUED RUNNING PAUSED COMPLETED FAILED CANCELED DELETED
  ExitClass exit_class = 16; string exit_reason = 17;   // from the allocation that ended the job
  repeated SubmissionTask tasks = 18;        // task_id, optional trial_id, repeated taskv1.Allocation
}
// taskv1.Allocation += resource_pool, exit_class, google.protobuf.Struct exit_detail,
//                      repeated Placement placements  /* node, accelerator_uuids */
```

- **来源。** `master/static/srv/` 下的一个命名查询，只读取数据库。它把 `jobs` 与 task 或 experiment、allocation 以及 `allocation_accelerators` 连接，并过滤 `job_type IN (COMMAND, SHELL, GENERIC, EXPERIMENT)`。
- **Workspace** 通过推导得到，不单独存储：experiment 经由 project 推导（experiment 可以移动），任务经由 `command_state` 推导。
- **任务状态**来自 `end_time`、`task_state` 和最后一个 allocation。没有存活 allocation、且最后一个 allocation 已结束的任务视为已结束，其状态取自该 allocation 的类别，因此它不会永远显示 QUEUED。
- **Experiment 状态**来自 `experiments.state`。已删除的 experiment 为 `DELETED`，因为 `DeleteExperiments` 会保留 job 行（`db/postgres_experiments.go:783-806`）。
- **授权。** 每一行都要通过现有的按类型读取授权。`DELETED` experiment 已经没有 experiment 行可供检查（`db/postgres_experiments.go:800-803`），因此只有其 owner 和管理员能看到它。
- **`get_task.sql`** 还会选取 `slots`、`exit_reason` 和 `status_code`。这些是现有的 `taskv1.Allocation` 字段（`proto/src/determined/task/v1/task.proto:93-98`）。
- **`CancelSubmission`** 是幂等的：已结束的 job 原样返回，存活的 job 走现有的终止路径及其授权。它不需要 registry 回退，因为存活的 job 总是在 registry 中。

<a id="scheduling-evaluation"></a>
### 调度评估

`rp.Evaluate(reqs)` 持有 `rp.mu`，在一个临时资源池上运行该资源池自身的 `scheduler.Schedule`（`rm/agentrm/`）：

- **任务列表。** `TaskList.Clone()` 按值复制请求（因为 `priority.go:134` 会通过共享指针写入），并共享 allocation 指针。
- **组。** 它复制各个组，并添加一个带优先级的合成组，从而避免 `priority.go:293-296` 处的 nil 优先级 panic。它从不调用 `getOrCreateGroup`，因为该函数会注册回调（`resource_pool.go:340-345`）。
- **静态可行性。** `findFits` 在移除了容器的 agent 深拷贝上运行。处于重连窗口内的 agent 视为已启用（`agent.go:80-83`）。
- **N。** 对于任务，N 为 1；对于搜索，N 为 `min(max_concurrent_trials or max_trials, max_trials)`。

```proto
message SchedulingEvaluation {
  string resource_pool = 1;
  Verdict verdict = 2;          // PLACEABLE_NOW | WOULD_PREEMPT | WOULD_QUEUE | INFEASIBLE
  int32 requested = 3; int32 placeable_now = 4;
  repeated EvaluatedPlacement placements = 5;   // agent_id, slots, device brands
  repeated string blocked_nodes = 6; repeated string reasons = 7;
  google.protobuf.Timestamp evaluated_at = 8;
}
```

- **限流。** `dry_run` 评估按用户限流（master 配置，默认每秒一次），因为每次评估会把一个 tick 推迟（`resource_pool.go:348-385`）。
- **校验共用适配结果。** `ValidateResources` 和 `CapacityCheck`（`resource_pool.go:568-661`）基于静态适配重建，由此引入“按每个 agent 的 slot 数取整数倍”规则和空闲 agent 规则（`fitting.go:123,176-186`）。provider 支持的资源池保留现有的实例数计算（`:576-577,632-647`），因为只对当前恰好在线的 agent 做静态适配会报告没有容量。
- **每个调用点保留各自的结果：**
  - 创建路径保持原样：单节点请求得到错误（`agent_resource_manager.go:589-600`），其他请求得到警告（`:602-607`），而 `launch_error` 会把该警告变为致命错误（`spec_util.go:51-53`）。
  - Experiment 恢复只记录日志、从不失败；而目前 `restore.go:86-93` 把错误视为致命。
  - `checkResourcePoolRemainingCapacity`（`trial.go:670-684`）改用正确的规则。
- **其他资源管理器。** Kubernetes 和 dispatcher RM 返回 `Unimplemented`。

<a id="immediate-admission"></a>
### 立即准入

- **标志。** `AllocateRequest.Immediate`（`sproto/task.go:25-54`）标记该请求。
- **入队。** `rp.Allocate` 仍然只负责入队并设置 `reschedule`（`resource_pool.go:118-124`）。它从不运行调度轮次，因为 `StartAllocation` 在调用它的整个过程中持有 allocation service 写锁（`task/allocation_service.go:55-67`）。
- **决策。** 在 `schedulerTick` 中，紧接真实的 `Schedule`（`resource_pool.go:368`）之前，用一轮试算覆盖所有尚未决策的立即请求。
  - 未出现在试算 `toAllocate` 中的请求会被移除，并以 `ResourcesFailedError{PlacementUnsatisfied}` 发布，detail 为 `static` 或 `busy`。
  - 兜底：在本次 tick 中已决策、但真实调度轮次没有放置的请求同样会被拒绝。
- **拒绝路径。** 拒绝走现有的异步退出路径（`allocation.go:265-266,894-921`），该路径会为 `PENDING` 行设置 `end_time`（`:913-919`）。
- **范围。** IMMEDIATE 适用于提交本身创建的每个 allocation，包括系统重试。之后由用户触发的 allocation（例如 generic 任务的 resume）会排队。
- **提交时拒绝。** Experiment 得到 `INVALID_ARGUMENT`，provider 支持的资源池得到 `FAILED_PRECONDITION`。

<a id="exit-classes"></a>
### 退出类别

```proto
enum ExitClass {
  EXIT_CLASS_UNSPECIFIED = 0;                // allocations that ended before the upgrade
  EXIT_CLASS_NONE = 1;                       // completed, preempted, or paused
  EXIT_CLASS_CANCELED = 2;
  EXIT_CLASS_PLACEMENT_UNSATISFIED = 3;
  EXIT_CLASS_NODE_PREFLIGHT_FAILED = 4;
  EXIT_CLASS_WORKLOAD_INITIALIZATION_FAILED = 5;
  EXIT_CLASS_WORKLOAD_FAILED = 6;
  EXIT_CLASS_INFRASTRUCTURE_FAILED = 7;
}
```

`allocations` 增加 `exit_class text` 和 `exit_detail jsonb`，由 `SetExitStatus`（`task/allocation.go:1074-1095`）写入。`closeOpenAllocations`（`core.go:957-963`）不经过该调用就会关闭 allocation，它写入 `INFRASTRUCTURE_FAILED`。分类器覆盖所有情况：

| 退出情况 | 类别 |
|---|---|
| 用户请求的终止（kill endpoint 通过 `Signal` 传递一个标志，`allocation.go:317-327`；抢占超时和 `crash` 也会到达该处） | `CANCELED` |
| 无错误 | `NONE` |
| `PlacementUnsatisfied` | `PLACEMENT_UNSATISFIED` |
| `PreflightFailed` | `NODE_PREFLIGHT_FAILED` |
| `ResourcesFailed` 或 `TaskError` | 如果失败的资源从未报告工作负载启动，则为 `WORKLOAD_INITIALIZATION_FAILED`，否则为 `WORKLOAD_FAILED` |
| 其他所有情况：agent 错误、`RestoreError`、`ResourcesMissing`、中止、未知 | `INFRASTRUCTURE_FAILED` |

**所有层一次性改动。** `calculateExitStatus` 遇到未列出的类型会 panic（`allocation.go:1219-1220`），因此以下改动必须一起落地：

- `aproto.PreflightFailed`（`master/pkg/aproto/exit.go:95-120`）；
- `sproto` 常量及其 `Proto`/`From` 映射（`sproto/resources.go:250-330`）；
- `taskv1` 枚举；
- switch 分支，包括缺失的 `ResourcesMissing` 分支，以及一个进行分类而不是 panic 的 default 分支。

同一改动还把未知的 agent 类型映射为 `UnknownError`（`resources.go:327-328`），并把 `allocation.go:921` 处的 `a.crash(msg)` 改为 `a.crash(*msg)`：目前该指针无法匹配按值类型编写的 switch，最终落入 “handler crashed”。

<a id="initialization-boundary"></a>
### 初始化边界

- **列。** `allocation_resources` 增加 `workload_started_at`。
- **RPC。** 新增内部 RPC `PostAllocationWorkloadStarted{allocation_id, resources_id}`，其授权方式与 `AllocationReady`（`api.proto:921`）相同。
- **由谁发送。** 第一次 `prep_container` 调用在最后一步发送它，位于 context 下载之后、startup hook 运行之前。调用点为 `master/static/srv/command-entrypoint.sh:11`、`shell-entrypoint.sh:7`、`generic-task-entrypoint.sh:13` 和 `entrypoint.sh:9`。trial 之后的 `--rendezvous` 调用不发送。hook 属于用户代码。
- **何时设定类别。** 目前 `finalize` 在 `purgeRestorableResources` 之后才调用 `SetExitStatus`（`allocation.go:584-588`）；类别改为在清除之前计算。这些行的生命周期与 allocation 完全一致，因为启动时只清除已关闭 allocation 的行（`taskmodel/resources.go:53-63`）。

<a id="system-retries"></a>
### 系统重试

- **每次重试一个新 allocation。** 重试总是新的 allocation `<task>.<n+1>`，从不重新放置已有的 allocation。
- **预算。** `resources.max_system_retries`（master 默认 3）限制 `NODE_PREFLIGHT_FAILED` 和 `WORKLOAD_INITIALIZATION_FAILED` 的重试次数。预算通过统计该任务中带有这些类别的 allocation 推导得到，因此无需计数器即可在重启后保留。
- **屏蔽节点。** 只有 `NODE_PREFLIGHT_FAILED` 会把 `(task, node, "preflight:<check>")` 写入现有的屏蔽节点表；该表以 task ID 为键，没有 FK（`logpattern/logpattern.go:133-159`）。
- **停止。** 预算耗尽，或屏蔽节点导致不再存在静态适配时，重试停止。
- **Trial。** 在瞬时检查（`trial.go:621`）之前新增一个分支，重新分配 allocation 且不增加 `restarts`。
- **Command 与 shell。** 它们获得重新分配能力。在 `OnExit` 完成任务并删除其会话 token（`command.go:239-263`）之前，command 会带着自己的屏蔽节点启动 `<task>.<n+1>`，更新 `command_state.allocation_id`，并写入 workspace 记录。
- **保持不变。** `INFRASTRUCTURE_FAILED` 保留现有的瞬时处理（`sproto/resources.go:353-371`）。generic 任务与目前一样以其类别结束（`spec_util.go:151-160`）。

<a id="placement-constraints"></a>
### 放置约束

- **配置。** 在 expconf（`master/pkg/schemas/expconf/experiment_config.go:203-217` 及 JSON schema）和 `model.ResourcesConfig`（`master/pkg/model/experiment_config.go:85-97`）中新增一个 `resources.accelerators` 块。它不能叫 `devices`：`resources.devices` 已经是宿主机设备挂载列表（`:216`、`:96`）。

  ```yaml
  accelerators:
    models: ["NVIDIA A100*"]      # static: scheduler
    min_memory_mib: 40000         # static: scheduler
    min_free_memory_mib: 30000    # dynamic: agent preflight
    max_utilization_percent: 10   # dynamic: agent preflight
  ```

- **调度器。** `FittingRequirements`（`sproto/scheduler.go:4-7`）增加 `DeviceModels` 和 `MinDeviceMemoryMiB`。一个 `deviceSatisfied` 硬约束同时加入两个内联列表（`fitting.go:123`、`:227`）。
  - 在 v1 中，agent 的所有已启用设备都必须匹配，因为 `allocateFreeDevices` 没有设备过滤（`agent_state.go:158-186`）。
  - 新字段接入 `trial.go:412,459`、`command.go:162`、`api_generic_tasks.go:358` 和 `generic_task_resume.go:371`。
- **检测。** nvidia-smi 查询增加 `memory.total`，因此解析器的字段数从 3 变为 4（`agent/internal/detect/nvidia.go:25-27,92-94`）。MIG 设备报告 0。显存信息在设备旁边传递，即 `AgentStarted.DeviceMemoryMiB map[device.ID]int`，保存在每个 `slot` 上，并在每次 `AgentStarted` 时替换。`device.Device`（`master/pkg/device/device.go:43-48`）保持不变，因为它是 `agentState.Devices`（`agent_state.go:49`）的键，并且会持久化到容器快照中，恢复时按值查找（`:526-530`）。因此重连比较（`agent_state.go:261-292`）也保持不变。

<a id="agent-preflight"></a>
### Agent 预检

- **Spec。** `cproto.Spec` 增加 `Preflight{MinFreeMemoryMiB, MaxUtilizationPercent, Timeout}`（`master/pkg/cproto/spec.go:14-18`），由 `ToDockerSpec` 填写（`master/pkg/tasks/task.go:261`）。
- **在启动路径中的位置。** 预检在 `PullImage` 之后、`CreateContainer` 和 `c.spec = nil` 之前运行（`agent/internal/container/container.go:198-222`）。它不能在 `manager.StartContainer` 中运行，因为那里的错误只会被记录日志（`agent/internal/agent.go:183-185`）。
- **检查项。** 它每秒对分配到的 UUID 采样一次 `nvidia-smi --query-gpu=uuid,memory.used,memory.total,utilization.gpu`。阈值满足即通过，超过 `Timeout`（master 默认 30 秒）则失败。这段宽限期用于等待任务的上一个容器释放显存。
- **不做的事。** 它从不对宿主机路径执行 `stat`，也不使用 `--query-compute-apps`，因为文档所述的 agent 容器只能看到 `docker.sock` 及其配置（`docs/setup-cluster/on-prem/options/docker.rst:117-171`）。
- **缺失的挂载源。** bind mount 的类型为 `mount.TypeBind`（`master/pkg/tasks/mounts.go:19-27`），因此宿主机源路径缺失时，Docker 会使 `CreateContainer` 失败。agent 用 Docker errdefs 匹配该错误，并将其映射为 `PreflightFailed{check: mount_source, subject: <host path>}`。目前它最终成为 `TaskError`（`container.go:336-343`）。
- **失败详情。** 失败携带 `{check, device, observed, required}`，同时写入 exit detail 和一行容器日志。

<a id="carried-fixes"></a>
### 顺带修复

- **`IdentifyTask`。** 它通过 `COALESCE` 从 `generic_task_spec` 解析 GENERIC 任务的 workspace（`command/postgres_command.go:46-60`）。目前它把这些任务解析为 workspace 0。
- **生成的 binding。** 每个 fork PR 都提交重新生成的 `bindings.py` 和 `api-ts-sdk`。

<a id="semantics"></a>
## 语义

<a id="phases-and-failure-classes"></a>
### 阶段与失败类别

| 阶段 | 位置 | 检查 | 失败 |
|---|---|---|---|
| 提交 | master 创建 handler | schema、授权、配置策略、键与摘要 | 错误；不创建任何内容，键仍未被占用 |
| 放置 | RM tick | 仅 IMMEDIATE：在本轮中无需抢占即被放置 | `PLACEMENT_UNSATISFIED` |
| 节点接受 | agent：拉取、预检、`CreateContainer` | 分配到的 UUID 上的 GPU 阈值、bind 源路径 | `NODE_PREFLIGHT_FAILED`；其他拉取或创建错误为 `WORKLOAD_INITIALIZATION_FAILED` |
| 初始化 | 容器：`task-setup.sh`，然后 `prep_container` | Python、wheel、context 下载、代理 | `WORKLOAD_INITIALIZATION_FAILED` |
| 工作负载 | startup hook 与用户命令 | 退出码 | `WORKLOAD_FAILED` |
| 任意 | agent 或连接 | agent 丢失、恢复 | `INFRASTRUCTURE_FAILED` |

<a id="restart-accounting"></a>
### 重启计数

| 类别 | 计入 `max_restarts` | 自动重试 | 屏蔽节点 |
|---|---|---|---|
| `PLACEMENT_UNSATISFIED` | 否 | 无 | 否 |
| `NODE_PREFLIGHT_FAILED` | 否 | 在 `max_system_retries` 内新建 allocation | 是 |
| `WORKLOAD_INITIALIZATION_FAILED` | 否 | 在 `max_system_retries` 内新建 allocation | 否 |
| `WORKLOAD_FAILED` | 仅 trial | trial，与目前相同 | 通过日志策略，与目前相同 |
| `INFRASTRUCTURE_FAILED` | 否 | 与目前相同（trial 视为瞬时） | 否 |
| `CANCELED`、`NONE` | 否 | 无 | 否 |

系统重试适用于 trial、command 和 shell。

<a id="idempotency-and-digest-scope"></a>
### 幂等与摘要范围

- **范围。** 键的范围是 `(jobs.owner_id, key)`，因此同一用户的所有客户端共享它。键永不过期，正如 job 行永不删除。
- **绑定。** dry run 从不绑定键。事务提交前出现的错误不绑定任何内容。任何已完成事务提交的提交请求都会消耗该键，包括以 `PLACEMENT_UNSATISFIED` 结束的请求，因此新的尝试需要新的键。
- **摘要。** 摘要是对客户端请求的 JCS 规范化 JSON 计算的 SHA-256，在应用 master 默认值之前计算。合并后的 spec 不能作为身份：petname、sshd 端口、SSH 密钥、会话 token 和资源池默认值在完全相同的请求之间也会变化。

  | 包含 | 排除 |
  |---|---|
  | kind、workspace、project、模板名称 | `idempotency_key`、`dry_run` |
  | config，从 YAML 或 Struct 解析后再规范化 | 文件 `mtime`、`uid`、`gid` |
  | 文件清单 `{path, type, mode, sha256}` | master 默认值与合并后的值 |
  | parent、fork、inherit、`no_pause`、`activate`、`admission` | |

- **准入方式是摘要的一部分**，因此用同一个键、不同的 `allow_queue` 重试会返回 409。
- **存储的请求。** 环境变量的值替换为 `"<redacted>"`，文件以清单形式存储。存储的请求与 config 的可见性相同。
- **键与摘要的可见性。** 只有 owner 和管理员能看到它们，因此不带密钥的普通摘要也不会向其他读取者暴露任何可被暴力破解的内容。

<a id="ownership"></a>
### 所有权

owner 是已认证的用户，记录在 `jobs.owner_id` 中。MCP 的 `--owner` 命名空间被移除。控制操作沿用 fork 的现有规则：basic authz 下为 owner 或管理员（`master/internal/api_ntsc_control.go:15-26`），RBAC 下为 workspace 权限。重放会对已存储的 job 重新检查读取授权，因此已撤销的访问权限会生效。

<a id="allow_queue"></a>
### `allow_queue`

- **映射。** `allow_queue=false` 表示 IMMEDIATE，`true` 表示 QUEUE。未指定准入方式时为 QUEUE，因此 `det` 和 WebUI 的行为不变。
- **IMMEDIATE。** 请求在做出决策的 tick 中由资源池的调度器放置，且不抢占。否则以 `PLACEMENT_UNSATISFIED`（`static` 或 `busy`）失败，并且永远不会排队。
- **静态不可行**在 QUEUE 下仍然只是警告。
- **搜索。** IMMEDIATE 会被拒绝，因为 trial 是异步创建的，而且没有 gang 调度。`dry_run` 以信息形式报告 N 个中的 `placeable_now` 数量，MCP 要求 experiment 使用 `allow_queue=true`。

<a id="placement-constraints-1"></a>
### 放置约束

- **静态事实是硬约束**：GPU 型号、总显存，以及保持不变的 `is_single_node`。command 仍然只使用单个 agent（`command.go:161-176`）。
- **动态事实是预检检查**：空闲显存和利用率。二者都永远不会成为放置事实。
- **不做预留。** 预检通过不会预留任何资源，评估是在 `evaluated_at` 时刻拍下的快照。

<a id="artifact-references"></a>
### 产物引用

- **代码**以现有 context 的形式传递：任务使用 `files`，experiment 使用 `model_definition`。
  - 它在 job 事务中提交，大小受 `MAX_CONTEXT_SIZE` 限制，约 95 MiB（`harness/determined/common/constants.py:5-18`）。
  - 摘要中的清单能精确标识它。
  - MCP 添加 `.code-provenance.json`，内容为 `{commit, dirty, excluded}`。
- **数据和输出**位于资源池 bind mount 下的共享存储上，由 `storage_sync` 和 `storage_fetch` 移动。
- **检查点**与目前一样使用 `checkpoint_storage`。
- **不在本设计中**：快照服务、对象存储或 `snapshot_id`。

<a id="defaults"></a>
### 默认值

| 设置 | 默认值 | 设置位置 |
|---|---|---|
| `resources.max_system_retries` | 3 | expconf，另有 master 默认值 |
| `dry_run` 评估频率 | 每用户每秒一次 | master 配置 |
| IMMEDIATE handler 等待 | 5 秒 | master |
| agent 预检宽限期（`Preflight.Timeout`） | 30 秒 | master 默认值 |
| 幂等键 | 最多 128 个 `[A-Za-z0-9._:-]` 字符 | API |
| 任务 context 大小 | 约 95 MiB（现有的 `MAX_CONTEXT_SIZE`） | harness |
| 最低 fork 版本 | MCP 1.0 为 0.41.0，MCP 1.1 为 0.42.0 | MCP |

<a id="what-remains-unverified"></a>
### 仍未经验证的内容

身份、owner、状态、放置和退出类别都是权威信息，`cross_profile_unverifiable` 已不复存在。仍有四项未经验证，并在它们出现的每个地方加以标注：

- 评估，它是 `evaluated_at` 时刻的快照；
- 预检通过，它不预留任何资源；
- 升级前的 job，它们没有请求或摘要，无法重放；
- task-resources 数据不可用时的 `unmeasured` 用量。

<a id="choices-between-alternatives"></a>
### 备选方案之间的取舍

| 问题 | 决定 | 理由 |
|---|---|---|
| 台账放在哪里 | `jobs` 上的可空列 | `jobs` 很少更新（只有 `q_position`，`api_experiment.go:1524-1528`）。不需要新表、不需要回填，也没有阻止删除的 FK。 |
| 创建 API | 在四个 RPC 上加 envelope，不使用 `Submit` oneof | 所有客户端共用一条创建路径，也不会出现代码生成器从未处理过的 oneof 请求体结构。 |
| 句柄 | 只用 `job_id` | 所有读取和操作共用一个句柄。 |
| 何时决定 IMMEDIATE | 下一个 tick，在一轮试算中 | 在 `rp.Allocate` 内运行调度轮次会持有 allocation service 写锁。 |
| 搜索的 IMMEDIATE | 拒绝 | 没有 gang 调度就不存在原子性保证。 |
| 重试时的 IMMEDIATE | 由提交创建的 allocation 继承 | 只有 command、shell 和 generic 任务使用它，而且它们都没有工作负载重启。 |
| 容量与放置 | 一个类别，附带 `static` 或 `busy` 详情 | 只需一条提交后路径。 |
| 重试机制 | 新 allocation，推导出的预算 | 被重新放置的 allocation 在恢复时看起来已经启动（`start_time` 在 Pulling 时设置，`allocation.go:749-753`）。 |
| Agent 丢失 | 不变 | 预算为 3 会在维护期间结束长时间运行的 trial。 |
| Command 和 shell 重试 | 新增 | 批处理类型仍然是 command，屏蔽节点行按任务记录。 |
| Generic 任务重试 | 推迟 | 需要先重做全局变更锁（`generic_task_resume.go:25-28`）和 resume 序号。 |
| 初始化边界存放在哪里 | 按资源存储，在清除前分类 | 一个节点无法掩盖另一个节点的失败。 |
| 挂载源检查 | Docker bind 错误 | 容器化的 agent 无法 stat 宿主机路径。 |
| 目录 | 工作负载自己的 `mkdir -p` | 少一个配置字段，也不需要路径策略。 |
| 代码 | 现有 context | 没有存储、没有 GC、没有悬空引用。 |
| 摘要 | 普通 SHA-256，仅 owner 可见 | 不需要新的 master secret。 |
| `GetCommand` 的数据库回退 | 推迟 | `GetSubmission` 是面向所有客户端的持久读取接口。 |
| 咨询、`compute_cli.py` | 移出、删除 | 两者都与计算无关，而且 `det` 和 WebUI 就是其他客户端。 |

<a id="the-mcp-after-the-refactor"></a>
## 重构后的 MCP

<a id="tools"></a>
### 工具

共 9 个工具，少于 `main` 上的 16 个（14 个基础工具加 2 个咨询工具）。

| 工具 | 行为 |
|---|---|
| `compute_plan(spec: TaskSpec, evaluate=True)` | 编译 spec、应用策略，并生成 UUIDv4 `request_id`。启用 `evaluate` 时，以 `dry_run` 调用创建接口，返回生效配置概要、摘要、评估结果和警告，其中包括位于生效 bind mount 目标之外的路径。否则离线渲染。 |
| `compute_launch(spec, request_id, allow_queue=False)` | 检查策略，然后带 `SubmitOptions` 发起一次创建调用。要求 `request_id` 为 UUID。返回 `job_id`、`replayed`、`submitted_at` 和 `outcome`。experiment 需要 `allow_queue=True`。`allow_queue=False` 时，1.0 先评估，只提交 `PLACEABLE_NOW` 的请求（某一时刻的检查）；从 1.1 起以 IMMEDIATE 准入提交。 |
| `compute_status(job_id)` | `GetSubmission`，并附带对退出类别的解释。 |
| `compute_list(kind=None, state=None, limit=50, cursor=None)` | 针对调用方的 `ListSubmissions`，覆盖所有客户端。 |
| `compute_logs(job_id, trial_id=None, tail=200)` | 任务日志。 |
| `compute_usage(job_id, trial_id=None, allocation_id=None, window_seconds=3600, metrics=None, include_samples=False)` | 任务资源，并附带解读。 |
| `compute_cancel(job_id)` | `CancelSubmission`。 |
| `storage_sync(local_dir, shared_dir, dry_run=True, overwrite=False)` | rsync；未设置 `overwrite` 时添加 `--ignore-existing`。 |
| `storage_fetch(shared_dir, local_dir, dry_run=True, overwrite=False)` | 反方向的 rsync。 |

<a id="taskspec"></a>
### `TaskSpec`

`TaskSpec` 是一个 pydantic 模型，作为输入 schema 发布。它包含以下字段：

- `kind`：`command`、`shell` 或 `experiment`。
- `name` 和 `command`。
- `workdir` 和 `output_dir`：容器路径。渲染后的命令先对 `output_dir` 执行 `mkdir -p`，再 `cd` 到 `workdir`。
- `image`、`pool` 和 `slots`。资源池和 slot 数总是根据策略默认值显式发送。
- `accelerators`。
- `env`、`workspace` 和 `project`。
- `code`：`repo_dir`、`revision`（默认 `HEAD`）、`include` 和 `exclude`，打包进 context。被跟踪的文件取自 `revision` 处的 git 对象；`include` 路径取自工作树，`dirty` 记录工作树是否有差异。
- `experiment`：类型由 fork 的 expconf JSON schema 定义，这些 schema 以锁定版本 vendor 到本仓库。搜索必须设置 `max_concurrent_trials`，以便 `max_slots` 能限制 slot 数与并发数的乘积。

<a id="modules"></a>
### 模块

| 模块 | 内容 | 来源 |
|---|---|---|
| `mcp_server.py` | 工具表 | 重写 |
| `spec.py` | `TaskSpec` 与编译器 | `compute/service.py` 中的 spec 部分，包括 `_render_entrypoint`（`:585-599`） |
| `policy.py` | 默认值、资源池允许列表、最大 slot 数、`overwrite`、容器到宿主机的映射 | `compute/profile.py` 中保留的部分 |
| `client.py` | 传输、认证、脱敏、版本门槛、创建路由、submission、日志、trial、task-resources，以及用量所需的资源池和 GPU 型号查询 | `core/api_client.py`，去掉删除的部分 |
| `context.py` | git 枚举、include 与 secret 规则、清单、来源信息 | PR #1 `storage/snapshot.py:1-513,736-995` |
| `usage.py` | 用量解读 | `compute/service.py:67-103,176-230,772-1190` |
| `storage/` | sync、fetch、配置、认证、askpass | 现有的包，去掉 `check` |
| `utils/secrets.py` | 不变 | – |

<a id="local-state-and-version-gate"></a>
### 本地状态与版本门槛

- **没有本地状态。** 不再使用 SQLite，`--owner` 和 `--db` 被移除。配置包含 API URL 和凭据、策略、存储访问以及容器到宿主机的映射。没有本地别名或草稿：`compute_plan` 返回规划，调用方把它传回 `compute_launch`，而 `compute_list` 可以找回丢失的 `job_id`。没有任何功能依赖本地缓存，因此以后可以添加缓存而无需改变契约。
- **版本门槛。** MCP 启动时调用一次 `GET /api/v1/master`。如果 fork 的 release 版本低于 0.41.0，则拒绝提供服务；0.41.0 的预发布构建可以通过。

<a id="deletion-list"></a>
### 删除清单

| 目标 | 删除内容 |
|---|---|
| 整个文件 | `compute/store.py`、`compute/admission.py`、`compute_cli.py` 及其在 `pyproject.toml` 中的 `determined-compute` 脚本。`agent_worker.py` 以及 `compute_consult` 和 `workflow_status` 工具移到它们自己的入口点。 |
| `compute/models.py` | `TaskRecord`（`:32-70`） |
| `compute/service.py`（拆解） | 上传字段拒绝 `:53-64,109-120`；认领、状态标记与不确定性 `:166-175,601-661,725-735`；路径校验 `:489-515,569-583`；容量钩子 `:662-668`；幂等与旧版 hash `:669-724,1435-1452`；能力检查 `:831-835`；远端身份 `:1212-1294`；发现、接管与调和 `:1295-1434`；绑定 `:1453-1526`；提交标记 `:1527-1573` |
| `core/api_client.py` | kind、用户与集群辅助函数 `:220-264`；`list_remote_tasks` `:265-321`；提交标记与上传字段 `:381-465`；按类型分派 `:466-521`，它将改为四个创建路由加 `CancelSubmission`；不支持时的回退 `:567-582`。`list_resource_pools` 和 `list_gpu_devices`（`:647-690`）保留，因为用量会调用它们（`compute/service.py:922,942`），而 `allocation_accelerators` 不存储 GPU 型号。 |
| `storage/service.py` | `check` 和 `_ssh_check`（`:70-108,429-466`） |
| `compute/profile.py` | 路径校验器（`:161-199`）、`cluster_identity` 和 `fingerprint`（`:200-215`）；挂载列表保留，作为容器到宿主机的映射 |
| `mcp_server.py` | reconcile、list、discover、adopt、resources 和 `storage_check` 工具（`:142-192`）；`--owner` 和 `--db` 的接线 |
| 测试 | `test_adoption_store`、`test_remote_adoption`、`test_compute_legacy_retry`、`test_owner_namespace`、`test_admission`、`test_compute_cli`；`test_compute_service` 和 `test_api_client` 中与台账和绑定相关的部分 |
| PR #1 | 不合并，直接关闭。丢弃：`gpu_admission.py` 及其 NVML 分支、`storage/paths.py`、跨 profile 代码、启动路径校验、`create_directories`、`snapshot.py` 中属于 store 的一半（`:514-735,996-1097`），以及它们的测试。保留：文件枚举、secret 规则、约 565 行测试以及 `resolve_api_url`。 |

预期结果是约 3,000 行源码，而 `main` 上约为 6,300 行，加上 PR #1 则约为 8,600 行。

<a id="delivery-plan"></a>
## 交付计划

<a id="phasing"></a>
### 分阶段

第一个 fork 版本只包含能删除 MCP 代码的部分：台账和评估，以及 `GetSubmission` 报告的退出类别。
立即准入、系统重试和 agent 预检放在第二个版本。

| Fork 版本 | Fork PR | MCP 版本 |
|---|---|---|
| 0.41.0 | F1, F2, F3a | 1.0：M1, M2, M3 |
| 0.42.0 | F3b, F4, F5 | 1.1：M4 |

<a id="fork-pull-requests"></a>
### Fork PR

| PR | 内容 | 依赖 |
|---|---|---|
| F1 退出类别与修复 | 贯穿所有层的 `ExitClass`；新的失败类型；覆盖所有情况的分类器；`closeOpenAllocations` 的类别；`UnknownError` 映射；`crash(*msg)`；`allocations.exit_class` 和 `exit_detail`；`IdentifyTask` 修复。在 F4 加入初始化边界之前，`ResourcesFailed` 和 `TaskError` 分类为 `WORKLOAD_FAILED` | – |
| F2 台账 | `jobs` 迁移；`SubmitOptions` 和 `SubmitResult`；handler 顺序，其中 `dry_run` 无副作用（尚不评估），以及 `validate_only` 别名；在 F3b 之前 `ADMISSION_IMMEDIATE` 返回 `UNIMPLEMENTED`；单一的提交事务；`ACTIVE` experiment；从未放置的恢复规则；`Get`/`List`/`CancelSubmission`；`get_task.sql` 字段 | F1 |
| F3a 评估 | `TaskList.Clone`；`rp.Evaluate`；让旧检查基于静态适配，同时保留每个调用点的结果；`dry_run` 评估及其限流；其他 RM 上的 `Unimplemented` | F2 |
| F3b 立即准入 | tick 决策与 handler 等待；IMMEDIATE 恢复；provider 资源池拒绝 | F3a |
| F4 初始化边界与重试 | `workload_started_at`、内部 RPC 以及 `prep_container` 的发送；带推导预算的 `max_system_retries`；trial 分支；command 和 shell 重新分配；屏蔽节点行 | F1, F3b |
| F5 设备与预检 | 在 `device.Device` 旁边传递的显存检测；`resources.accelerators`；`deviceSatisfied`；`cproto.Preflight`；agent 钩子；挂载源映射 | F3a, F4 |

master 和 agent 一起升级。运行中的容器不受影响，因为 `device.Device` 和重连比较保持不变。

<a id="mcp-pull-requests"></a>
### MCP PR

| PR | 内容 | 依赖 |
|---|---|---|
| M1 收窄 server | 把咨询移到独立入口点；删除 `compute_cli.py`；关闭 PR #1 | – |
| M2 改用台账 | `client.py`、版本门槛以及 `job_id` 句柄；基于 submission 的 launch、status、list、logs、usage 和 cancel；删除 store、提交标记、reconcile、discover、adopt、绑定和 owner 命名空间 | F2, F3a |
| M3 类型化 spec | `TaskSpec`、`spec.py`、`policy.py` 和 `context.py`；通过 `dry_run` 规划；`allow_queue=false` 实现为先评估后提交；删除 `admission.py`、`compute_resources`、`storage_check` 和路径校验 | F3a |
| M4 加速器与立即准入 | `TaskSpec` 中的 `accelerators`；`allow_queue=false` 以 IMMEDIATE 准入提交 | F3b, F5 |

- **合并顺序。** MCP PR 只有在其 fork 依赖进入 fork `main` 之后才能合并。集成测试针对目标 fork 版本的预发布构建运行。
- **发布。** 在 fork 打出 0.41.0 tag 之前不发布 MCP 1.0，MCP 1.1 等待 0.42.0。
- **文档。** 每个 PR 都更新其涉及的英文和中文文档。M3 重写[计算服务参考](compute-service.zh.md)、[Agent 工作流](agent-workflow.zh.md)、[故障排查](troubleshooting.zh.md)和 `AGENTS` 路由。

<a id="out-of-scope"></a>
## 不在范围内

- **把 generic 任务作为唯一类型。** 包括把各类型合并为 GENERIC、generic 重新分配以及重做 generic 锁。
- **搜索。** 搜索的 gang 调度和严格准入。
- **快照与产物。** 不提供快照或产物服务。如果将来需要，它必须把键的范围限定为 `(owner_id, snapshot_id)`，对每个引用进行授权，并在从上传到首次引用的期间持有租约。
- **其他启动功能。** `max_slots` 和配置策略之外的配额机制。条件触发的启动如果将来需要，应作为独立服务，通过同一个创建 envelope 并使用自己的幂等键提交，绝不成为调度循环的一部分。
- **旧版读取。** 为 `det cmd` 和 WebUI 任务列表提供的数据库回退。
- **其他资源管理器。** Kubernetes 和 dispatcher RM，以及 ROCm 和 MIG 预检。
- **混合 GPU。** 在混合 GPU 的 agent 上进行设备过滤。
- **检查点 GC。** 检查点 GC 任务的读取授权（`master/internal/api_tasks.go:65-69`）。
- **咨询。** 重新设计咨询。

<a id="risks-and-open-questions"></a>
## 风险与未决问题

1. **调度延迟。** `dry_run` 和立即准入的试算各自最多在 `rp.mu` 下增加一轮调度。应在限流之外增加一个延迟指标。
2. **两轮结果不一致。** 如果试算轮次与真实轮次的结果不一致，兜底机制会拒绝该请求，而绝不会让它排队。
3. **严格的 IMMEDIATE。** 由于 IMMEDIATE 从不抢占也不插队，它在繁忙的集群上会经常拒绝。替代方案是 `allow_queue=true`。
4. **以 `ACTIVE` 提交的 experiment。** 创建路径必须跳过 `ActivateExperiment`（`api_experiment.go:1682-1687`），并以恢复时的方式启动 experiment；恢复已经能处理 nil 快照（`restore.go:118-124`）。在 F2 中验证。
5. **Docker bind 错误。** 挂载源分类依赖 Docker 的 bind 错误。应在实际部署的 Docker 版本上测试。
6. **重连窗口。** 把处于重连窗口内的 agent 视为已启用，需要用到它们暂存的状态（`agent.go:80-83`）。在 F3a 中验证。
7. **Generic 取消。** 对 generic 任务调用 `CancelSubmission` 可能遇到全局变更锁（`api_generic_tasks.go:580-583`），此时返回可重试的 `UNAVAILABLE`。
8. **命令中的 secret。** 在命令行中输入的 secret 会按提交时的内容存储。文档必须说明这一点。
9. **重试预算。** `max_system_retries` 的默认值 3 是否合适？
10. **旧版 allocation。** 升级前已结束的 allocation 显示为 `EXIT_CLASS_UNSPECIFIED`，这是唯一未分类的状态。

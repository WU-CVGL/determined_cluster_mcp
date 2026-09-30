<a id="architecture"></a>
# 架构

[English](architecture.md) | [简体中文](architecture.zh.md)

本文描述计算 MCP 的目标架构，以及它要求 Determined fork 做出的改动，面向两个仓库的维护者。当前的 MCP 接口见[计算服务参考](compute-service.zh.md)。

Fork 路径相对于 `8e26a69`（release 0.40.1）处的 fork 根目录，MCP 路径相对于本仓库的 `2404d0d`。“PR #1” 指尚未合并的 `feat/research-workflow-support` 分支。所有引用都来自阅读源码，而不是实际运行；它们指向上述基准提交，不会随代码变化而维护。这些引用已在 fork `ec9a865` 上重新核对，该提交与 `8e26a69` 的差异仅在 `harness/determined/deploy` 之下。

<a id="what-changes-for-users"></a>
## 对用户的变化

- **代码来自三种显式来源之一。** 由 `code.source` 选择。
  - `git` 指定共享存储上的一个仓库和一个 revision。规划会固定提交，容器把它克隆到本地临时空间并检出。不上传任何内容，也没有大小上限。该提交必须位于某个分支或 tag 上，镜像需要提供 `git`（LFS 文件还需要 `git-lfs`）。
  - `context` 以 Determined 的任务 context 发送某个 revision 中被跟踪的文件及显式 include 的文件，上限约 95 MiB。它适合小型仓库和未提交的改动。
  - `path` 直接在共享存储上的某个目录中原地运行。这类代码是可变的，因此需要显式选择启用，用于 shell 和调试。

  `workdir` 现在相对于代码根目录。
- **存储根目录归管理员所有。** 作业从不指定 bind 源路径。数据和输出位于作业在已挂载根目录内创建的运行目录中。检查点只设置相对路径 `checkpoint_storage.storage_path`；`host_path` 是管理员设置。
- **单一句柄。** `job_id` 取代本地 `task_id`，`--owner` 和 `--db` 被移除。同一账户的所有客户端
  看到并控制相同的作业。
- **启动与其规划绑定。** `compute_launch` 发送 `compute_plan` 返回的摘要。如果代码或配置自规划以来发生了变化，它返回 `plan_changed`，不创建任何内容。每次规划都会访问 master。
- **准入方式是显式的。** `admission=queue` 是默认值，与 `det` 一样排队；在 `main` 上，MCP 默认不排队（`compute/service.py:279`）。只有 `queue` 这一种准入方式：`admission=immediate` 以 `admission_unsupported` 被拒绝，从不被悄悄降级。`compute_plan` 通过 master 的 `dry_run` 校验并渲染确切的请求，但不评估放置；由调度器在提交之后决定。
- **更少的工具。** reconcile、discover、adopt 和 `determined-compute` CLI 被移除；由 `compute_list` 覆盖其用途。只读的 `compute_resources` 和 `storage_check` 作为透传工具保留。咨询 worker 被移除：实验分析由官方 W&B MCP 负责。

<a id="principles"></a>
## 原则

1. **可靠地失败。** 它决定第一个版本的范围（见[推迟的设计](#deferred-designs)）。
   - 作业可以失败，但失败从不被报告为成功。
   - 用户可以重新提交，但当结果不确定时，系统从不悄悄地重新执行。
   - 某项能力可以缺失，但它的缺失从不损害其他作业、资源归属或取消。
   - 新的复杂度必须通过同一道门槛：这种情况能否用显式拒绝、明确的失败、人工修复或以同一身份进行的查询来处理，而不引入新的状态、后台工作或自动重试？只有错误执行、重复的副作用、资源归属不清、取消丢失或数据泄露的风险，才值得采用更强的协议。
2. **每项能力都放在其事实来源所在的位置。**
   - master 负责身份、幂等、规划绑定、准入、放置和失败类别。
   - agent 负责节点事实。
   - 管理员负责存储根目录。工作负载在其中自行创建运行目录，从不创建 bind 源路径。
   - MCP 负责研究意图、比 RBAC 更窄的单次调用策略、用户的工作树、代码交付以及结果解读。
3. **job 行就是台账。** `tasks.job_id`、`experiments.job_id` 和 `jobs.owner_id` 已经存在（`master/pkg/model/task.go:78`、`experiment.go:336`、`job.go:87-94`）。`job_id` 是唯一的句柄。作业的下一步（启动、重试、结束、取消）在执行之前先提交，恢复会完成崩溃所中断的一切。
4. **单一任务契约。** 现有的四种创建请求就是契约，规划就是对同一请求执行 `dry_run`。
5. **由调度器决定。** 任何地方都不重新推导放置。推迟的评估和立即准入复用资源池自身的 `Schedule` 和 `findFits`。
6. **失败在发生处确定类型，并且只记录一次。** 每个结束的 allocation 恰好得到一个退出类别。
7. **没有兼容路径。** 只有一个最低提交协议编号和一次检查，不做探测，也没有客户端替代实现。

<a id="layers-and-responsibilities"></a>
## 分层与职责

| 能力 | 负责方 | MCP 保留的部分 |
|---|---|---|
| 身份、owner、键、摘要、准入方式、取消请求 | `jobs` 行上的新列 | `job_id` |
| 幂等提交 | master，在 `(owner_id, idempotency_key)` 上唯一 | 由 `compute_plan` 生成的 `request_id` |
| 规划绑定 | master，在重放之后检查 `expected_digest` | 来自规划的 `request_digest` |
| spec 合并、默认值 | master 创建路径、资源池 `task_container_defaults`、模板 | 渲染为创建请求的 `TaskSpec`，显式指定资源池和 slot |
| 容量与放置的答复 | RM 调度器，在提交之后；`dry_run` 只校验、不评估放置（评估已推迟） | 无；`compute_resources` 仍是一种投影 |
| 排队准入（立即准入已推迟） | RM 调度 tick，在 `rp.mu` 下进行；`immediate` 返回 `UNIMPLEMENTED` | 透传 `admission`；以 `admission_unsupported` 拒绝 `immediate` |
| GPU 型号、总显存、单节点 | 调度器硬约束（型号与显存已推迟，F5） | 资源池；spec 字段已推迟（M4） |
| 空闲显存、利用率 | agent，在 `CreateContainer` 之前（已推迟，F5） | 无 |
| bind 源路径 | 管理员：`task_container_defaults.bind_mounts`、workspace 或 master 的 `checkpoint_storage` | 容器到宿主机的映射 |
| 运行目录 | 工作负载，在这些根目录内；`storage_path` 由 harness 负责 | 渲染 `mkdir -p` |
| 退出类别与重试 | allocation、trial、command（command 重试已推迟，F4） | 解释类别 |
| 跨客户端的状态、列表、取消 | `GetSubmission`、`ListSubmissions`、`CancelSubmission` | 透传 |
| 资源池与设备事实 | `GetResourcePools`、`GetAgents` | `compute_resources`，一种投影 |
| 代码交付 | 渲染后的命令（`git`、`path`）或现有的 context（`context`）；fork 无改动 | revision 固定、枚举、secret 规则、清单、前导命令 |
| 数据、输出、检查点 | 管理员根目录下的共享存储；`checkpoint_storage` | `storage_sync`、`storage_fetch`、`storage_check` |
| 实验记录：config、指标、产物、血缘、报告 | W&B，与作业关联（[使用 W&B 追踪实验](#experiment-tracking-with-wb)） | 无；分析通过官方 W&B MCP 进行 |
| 条件触发的启动 | 通过窄的提交适配器使用 W&B Automations（如果将来构建） | 无 |
| 资源池允许列表、最大 slot 数、`overwrite` | MCP | 全部 |
| 用量解读 | MCP | 全部 |
| slot 上限 | 仅按作业：experiment 的 `resources.max_slots`，即以作业为键的调度组（`rm/agentrm/resource_pool.go:47`），只有 fair-share 调度器会强制执行它（`fair_share.go:214-216`），默认的 priority 调度器不会（`config/scheduler_config.go:28-37`）；以及配置策略，它会拒绝超过 workspace 或全局限制的单次提交（`configpolicy/task_config_policy.go:45-60`） | 按请求的资源池允许列表和最大 slot 数 |

<a id="platform-changes"></a>
## 平台改动

<a id="ledger-columns-on-jobs"></a>
### `jobs` 上的台账列

迁移文件为 `2026MMDD000000_job-submissions.tx.up.sql`。按照 fork 自身的先例，它配有 `.down.sql`。

```sql
ALTER TABLE jobs
  ADD COLUMN idempotency_key text,
  ADD COLUMN request_digest  text,
  ADD COLUMN cancel_requested_at timestamptz,
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
  string expected_digest = 4;  // optional; the request_digest a dry run returned
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

**第一个版本。** `SubmitResult.evaluation` 保留，但始终为空：dry run 校验请求，但不评估放置（见[调度评估](#scheduling-evaluation)）。`ADMISSION_IMMEDIATE` 返回 `UNIMPLEMENTED`，对 experiment 则返回 `INVALID_ARGUMENT`。

**协议版本。** `GetMasterResponse` 增加 `int32 submission_protocol = 17;`（`proto/src/determined/api/v1/master.proto:49-101`）。`GetMaster` 无需登录（`master/internal/grpcutil/auth.go:47-51`）。没有该字段的 master 报告 0。F1 和 F2 进入 fork `main` 后，该编号变为 1，由完成这两者的 PR 设置。每项推迟的能力在落地时提高它。

<a id="submit-handler-order"></a>
### 提交处理顺序

由一个共享的 `submission` 包为全部四个 handler 实现以下顺序。

1. **摘要。** 规范化客户端请求并计算摘要。此时不运行任何有副作用的操作。
2. **重放。** 如果提供了键，查找 `(owner_id, key)`。设置了 `expected_digest` 时，把已存储的摘要与它比较，否则与计算出的摘要比较。二者相等时，对已存储的 job 检查读取授权，把它传给 `dispatch`（第 7 步），并以 `replayed=true` 返回该 job；不相等时，返回 `ALREADY_EXISTS`，并指明已有的 `job_id`。
3. **规划检查。** 如果设置了 `expected_digest`，且它与计算出的摘要不同，返回带 `plan_changed` 的 `FAILED_PRECONDITION`。不写入任何内容，键仍未被占用。
4. **解析。** 解析、合并、授权并应用配置策略。会话签发和 shell 密钥生成移到第 5 步之后。目前它们运行得过早：
   - `master/internal/core_experiment.go:401`，早于 `api_experiment.go:1655` 处的 `ValidateOnly` 返回；
   - `api_command.go:166-170`；
   - `api_generic_tasks.go:153-158`；
   - `api_shell.go:263-269`。
5. **Dry run。** 如果设置了 `dry_run`，不评估放置，直接返回。此时尚未写入任何内容。
6. **事务提交。** 由一个事务写入：
   - job 行，包括键、摘要和准入方式；
   - 对于任务：task 行、context 目录、`allocation_workspace_info`、状态为 `PENDING` 的第一条 allocation 行，以及 `command_state`（包括 `generic_task_spec`）。目前 `command_state` 在 `StartAllocation` 之后才写入（`command/command.go:181-186`、`api_generic_tasks.go:378`），两者之间发生崩溃会留下一个永远不会被恢复的任务。由于该行现在已经存在，`requestResources` 改为加载它，而不是插入它（`task/allocation.go:523-529`）。
   - 对于 `activate=true` 的 experiment：experiment 行，以 `ACTIVE` 状态提交。

   遇到唯一索引冲突时，回滚事务，删除已签发的会话和 `GroupPriorityChangeRegistry` 条目（`command/command.go:117-119`），然后回到第 2 步。起保护作用的是索引，而不是 `cs.mu`。使事务提交结果未知的错误（例如 `COMMIT` 期间连接断开）不算回滚：不清理任何内容，handler 重新查找该键。如果该作业存在，就继续执行第 7 步；否则返回可重试的 `UNAVAILABLE`。
7. **分派。** 把作业传给 `dispatch(job_id)`。它脱离请求的上下文运行，因此在事务提交之后断开连接的客户端无法中止它。`dispatch` 是幂等的，并按作业串行执行。它读取 `command_state` 指向的尝试（或 experiment），如果该尝试已经结束，或已在 allocation service 中注册（对于 experiment，已在 experiment registry 中运行），则什么也不做。否则它调用 `StartAllocation` 或 `e.Start`，然后重新读取 `cancel_requested_at`，如果它已设置则终止该作业。allocation service 拒绝对同一 allocation ID 的第二次注册，因此每个 ID 至多有一个运行时实例。如果启动在进程内失败，`dispatch` 以 `INFRASTRUCTURE_FAILED` 关闭 `PENDING` allocation 并设置 `tasks.end_time`，或把 experiment 标记为 `ERROR`，因此重放会返回一条终态记录。它有三个调用方：事务提交之后的 handler、每次重放，以及一个由 master 负责的扫描，该扫描每 30 秒分派一次当前尝试处于 `PENDING`、尚未注册且存在时间超过该间隔的已提交作业。因此，已完成事务提交的提交请求总会继续推进，无需重启 master。

**恢复。** 放置就是 allocation 自身的 `ASSIGNED` 写入（`task/allocation.go:630`），它总是早于容器启动（`:723`）。`start_time` 无论如何都不能作为证据：它只在 Pulling 时设置（`:749-753`），而 `CloseOpenAllocations` 会在未关闭的行上写入它（`db/postgres_tasks.go:314-315`）。`RestoreAllCommands` 和 `restoreGenericTasks`（`command/command_service.go:55-72`、`core.go:880-955`）根据持久化的 `allocations.state` 决定如何处理一个未结束的尝试：

- **`PENDING`：从未被放置。** 删除该尝试的 `allocation_resources` 行（级联删除 `resourcemanagers_agent_containers`），因为 agent RM 会在 allocation 确认这些行之前的 tick 中写入它们（`rm/agentrm/resource_pool.go:443-454`）。然后，它以同一个 allocation ID 重新发起请求。如果设置了 `cancel_requested_at`，则改为结束该任务。
- **任何更靠后的状态：已放置**，即使 `start_time` 为 NULL。它以 `Restore=true` 恢复，并通过 agent 重新挂接进行调和；如果设置了 `cancel_requested_at`，注册之后的取消检查会终止它。它从不被重新请求。如果它没有被重新挂接，则以 `INFRASTRUCTURE_FAILED` 结束。

`RestoreAllCommands` 经由 `command_state` 以 `tasks.end_time IS NULL` 选择任务，而不是经由未关闭的 allocation，并取 `command_state` 指向的那个尝试。那里还适用另外两种情况：

- **已结束的尝试：** 它得到被崩溃中断的退出决策。在第一个版本中，该决策结束任务：没有系统重试（见[系统重试](#system-retries)，已推迟）。同一情况也会结束升级前遗留的未结束任务。
- **恢复失败：** 由一个事务把该尝试以 `INFRASTRUCTURE_FAILED` 关闭并设置 `tasks.end_time`，与 experiment 的做法一致（`core.go:832-837`）。目前它只被记录日志（`command_service.go:71-81`）。

`Command.Start` 使用已存储的 allocation ID，而不是 `<task>.1`（`command/command.go:120`）。以下四项 agent RM 与数据库修复保证已放置分支的正确性：

- 在发送 `StartContainer` 之前持久化启动记录（`rm/agentrm/agent.go:219-223`），因此任何可能存在的容器都在 agent 快照中。如果写入失败，则不发送任何内容。
- 没有启动记录的容器快照在恢复时以 `RestoreError` 结束。
- 重新挂接时的状态不一致会携带一个失败（`agent.go:823-829`），因此被终止的容器分类为 `INFRASTRUCTURE_FAILED`，而不是 “stopped early”。
- `CloseOpenAllocations` 只在它关闭的行上写入 `start_time`。

这还修复了目前在重启时仍处于排队状态的 command 出现的 `RestoreError` “0 container snapshots”（`resource_pool.go:189-190`）。只评估了 agent RM。

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
  Admission admission = 11;
  google.protobuf.Timestamp submitted_at = 12; optional google.protobuf.Timestamp ended_at = 13;
  State state = 14;        // QUEUED RUNNING PAUSED COMPLETED FAILED CANCELED DELETED
  ExitClass exit_class = 15; string exit_reason = 16;   // from the allocation that ended the job
  repeated SubmissionTask tasks = 17;        // task_id, optional trial_id, repeated taskv1.Allocation
}
// taskv1.Allocation += resource_pool, exit_class, google.protobuf.Struct exit_detail,
//                      repeated Placement placements  /* node, accelerator_uuids */
```

- **来源。** `master/static/srv/` 下的一个命名查询，只读取数据库。它把 `jobs` 与 task 或 experiment、allocation 以及 `allocation_accelerators` 连接，并过滤 `job_type IN (COMMAND, SHELL, GENERIC, EXPERIMENT)`。
- **Workspace** 通过推导得到，不单独存储：experiment 经由 project 推导（experiment 可以移动），任务经由 `command_state` 推导。
- **任务状态。** 任务所属的作业恰好在 `tasks.end_time` 被设置时结束，但处于 `PAUSED` 或 `STOPPING_PAUSED` 的 GENERIC 任务为 `PAUSED`，因为暂停会写入 `end_time`（`db/postgres_tasks.go:145-156`）。当 `command_state` 指向的尝试已被放置且尚未结束时，存活的任务为 `RUNNING`，否则为 `QUEUED`，包括两次尝试之间以及 unpause 期间。尝试的 `end_time` 已设置或其状态为 `TERMINATED` 时，该尝试已结束。设置了 `cancel_requested_at` 的已结束作业为 `CANCELED`；取消从不在已结束的作业上设置它。否则，作业状态取自其最后一次尝试的类别：`NONE` 为 `COMPLETED`，失败类别为 `FAILED`，升级前的 `UNSPECIFIED` 仅在 allocation 记录了 `exit_error` 时为 `FAILED`。每条结束任务的路径都会写入 `end_time`：退出决策、恢复、恢复失败以及取消。
- **Experiment 状态**来自 `experiments.state`。已删除的 experiment 为 `DELETED`，因为 `DeleteExperiments` 会保留 job 行（`db/postgres_experiments.go:783-806`）。
- **授权。** 每一行都要通过现有的按类型读取授权。`DELETED` experiment 已经没有 experiment 行可供检查（`db/postgres_experiments.go:800-803`），因此只有其 owner 和管理员能看到它。
- **`get_task.sql`** 还会选取 `slots`、`exit_reason` 和 `status_code`。这些是现有的 `taskv1.Allocation` 字段（`proto/src/determined/task/v1/task.proto:93-98`）。
- **`CancelSubmission`** 先持久化。它根据数据库进行授权（`jobs.owner_id`，以及经由 `command_state` 或 project 得到的 workspace），锁定 job 行，对已结束的作业原样返回，否则设置 `cancel_requested_at`。事务提交后，它通过 allocation service（而不是 command registry）向当前尝试发送信号，或终止 experiment；allocation 不存在不算错误。每次 allocation 启动（首次分派、恢复、experiment 启动，以及今后的任何系统重试）都会在注册 allocation 之后检查该标志，因此两者中总有一方能看到另一方。没有存活 allocation 的 GENERIC 任务（例如已暂停的任务）直接以 `CANCELED` 结束。`KillCommand`、`KillShell` 和 `KillGenericTask` 走同一路径；目前前两者在 registry 未命中时会以 `NotFound` 失败（`api_command.go:259-262`、`api_shell.go:126-129`）。退出决策和取消锁定同一 job 行，因此迟到的取消永远不会把 `COMPLETED` 变成 `CANCELED`。
- **GENERIC 取消。** 每个 GENERIC 任务（无论是父任务还是子任务）都有自己的作业，取消其中一个会覆盖该任务及其所有后代，与目前的 `KillGenericTask` 一致（`api_generic_tasks.go`、`generic_task_resume.go`）。它解析出子树，在做任何改动之前对每个成员进行授权，并跳过已经 `COMPLETED` 或 `CANCELED` 的成员。一个事务在每个成员的作业上设置 `cancel_requested_at`，并取消这些成员未完成的 `generic_task_resume` 行。事务提交后，它向每个成员当前的或预定的 allocation 发送信号。两者都没有的成员（例如其 `no_pause` 子任务仍在运行的已暂停父任务）直接以 `CANCELED` 结束，同时子任务被终止。带 `kill_from_root` 的 `KillGenericTask` 解析出根任务，并以同样的方式取消根任务的作业。恢复从不继续其任务已设置 `cancel_requested_at` 的 resume；它以 `CANCELED` 结束该任务。

<a id="exit-classes"></a>
### 退出类别

```proto
enum ExitClass {
  EXIT_CLASS_UNSPECIFIED = 0;                // ended before the upgrade
  EXIT_CLASS_NONE = 1;                       // did not fail: completed, stopped, preempted, killed, aborted before placement
  EXIT_CLASS_PLACEMENT_UNSATISFIED = 2;
  EXIT_CLASS_NODE_PREFLIGHT_FAILED = 3;
  EXIT_CLASS_WORKLOAD_INITIALIZATION_FAILED = 4;
  EXIT_CLASS_WORKLOAD_FAILED = 5;
  EXIT_CLASS_INFRASTRUCTURE_FAILED = 6;
}
```

`allocations` 增加 `exit_class text` 和 `exit_detail jsonb`。第一个版本产生 `NONE`、`WORKLOAD_FAILED` 和 `INFRASTRUCTURE_FAILED`。`PLACEMENT_UNSATISFIED`、`NODE_PREFLIGHT_FAILED` 和 `WORKLOAD_INITIALIZATION_FAILED` 留给[推迟的设计](#deferred-designs)。

**退出记录。** `finalize` 在改动其他任何内容之前，用一条 UPDATE 写入完整的退出记录：`state = TERMINATED`、`end_time`、`exit_reason`、`exit_error`、`status_code`、`exit_class` 和 `exit_detail`。只有在这条 UPDATE 提交之后，它才清除并释放可恢复的资源，此后才运行任务级的退出决策。清除、释放和退出通知都可以重复执行，因此在任何时刻崩溃，留下的要么是由恢复处理的未关闭 allocation，要么是一条完整的记录。目前 `finalize` 先写入 `TERMINATED`，再清除，最后写入退出状态（`task/allocation.go:584-588,1074-1095`）；在这些步骤之间崩溃会留下一个没有类别的已终止 allocation，而其证据已经不复存在。

启动时先执行恢复，然后关闭恢复没有保留的所有 allocation（`core.go:1432-1444`）。恢复为它所决定的尝试分类。`closeOpenAllocations`（`core.go:957-963`）在关闭其余 allocation 的同一条 UPDATE 中按状态为它们分类：`PENDING` 为 `NONE`（通常是被恢复替换掉的、排队中的 trial allocation，`trial.go:806-808`），任何更靠后的状态为 `INFRASTRUCTURE_FAILED`。类别描述的是结果而不是原因：是谁要求停止作业只记录一次，即 `jobs.cancel_requested_at`。分类器覆盖所有情况：

| 退出情况 | 类别 |
|---|---|
| 无错误，包括被终止、被抢占或在启动前中止（`TaskAborted`、`ResourcesAborted`） | `NONE` |
| `PlacementUnsatisfied` | `PLACEMENT_UNSATISFIED`；随[立即准入](#immediate-admission)推迟（F3b） |
| `PreflightFailed` | `NODE_PREFLIGHT_FAILED`；随 [agent 预检](#agent-preflight)推迟（F5） |
| `SpecRejected` | 随 F5 推迟：`WORKLOAD_INITIALIZATION_FAILED`，detail 为 `{reason: spec_rejected, node, error}` |
| `ResourcesFailed` 或 `TaskError` | `WORKLOAD_FAILED`。推迟的[初始化边界](#initialization-boundary)会细化这一点：如果该 allocation 报告工作负载启动，且失败的资源没有 `workload_started_at`，则为 `WORKLOAD_INITIALIZATION_FAILED` |
| 其他所有情况：agent 错误、`RestoreError`、`ResourcesMissing`、handler 错误、未知 | `INFRASTRUCTURE_FAILED` |

**所有层一次性改动。** `calculateExitStatus` 遇到未列出的类型会 panic（`allocation.go:1219-1220`），因此以下改动必须一起落地：

- `aproto.PreflightFailed`（`master/pkg/aproto/exit.go:95-120`）；
- `sproto` 常量及其 `Proto`/`From` 映射（`sproto/resources.go:250-330`）；
- `taskv1` 枚举；
- switch 分支，包括缺失的 `ResourcesMissing` 分支，以及一个进行分类而不是 panic 的 default 分支。

同一改动还把未知的 agent 类型映射为 `UnknownError`（`resources.go:327-328`），并把 `allocation.go:921` 处的 `a.crash(msg)` 改为 `a.crash(*msg)`：目前该指针无法匹配按值类型编写的 switch，最终落入 “handler crashed”。推迟的 F5 以同样的方式、一次性贯穿所有层加入 `aproto.SpecRejected`。

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
| 提交 | master 创建 handler | schema、授权、配置策略、键与摘要、规划摘要 | 错误；不创建任何内容，键仍未被占用 |
| 放置（已推迟，F3b） | RM tick | 仅 IMMEDIATE：在本轮中无需抢占即被放置 | `PLACEMENT_UNSATISFIED` |
| 节点接受（已推迟，F5） | agent：拉取、预检、`CreateContainer` | 分配到的 UUID 上的 GPU 阈值 | `NODE_PREFLIGHT_FAILED`；Docker 在创建时拒绝的 spec（例如缺失的 bind 源路径）以及其他拉取或创建错误为 `WORKLOAD_INITIALIZATION_FAILED` |
| 初始化（已推迟，F4） | 容器：`task-setup.sh`，然后 `prep_container` | Python、wheel、context 下载、代理、工作负载启动通知的发送 | `WORKLOAD_INITIALIZATION_FAILED` |
| 工作负载 | startup hook，然后是渲染后的命令：MCP 的前导命令和用户命令 | 退出码 | `WORKLOAD_FAILED` |
| 任意 | agent 或连接 | agent 丢失、恢复 | `INFRASTRUCTURE_FAILED` |

标记为已推迟的行属于[推迟的设计](#deferred-designs)。在它们落地之前，节点接受或初始化期间的失败按[退出类别](#exit-classes)表分类，为 `WORKLOAD_FAILED` 或 `INFRASTRUCTURE_FAILED`。

<a id="restart-accounting"></a>
### 重启计数

在第一个版本中，trial 保留 `max_restarts` 和目前的处理方式，其他任何内容都不会自动重试。下表是推迟的计划（见[系统重试](#system-retries)）。

| 类别 | 计入 `max_restarts` | 自动重试 | 屏蔽节点 |
|---|---|---|---|
| `PLACEMENT_UNSATISFIED` | 否 | 无 | 否 |
| `NODE_PREFLIGHT_FAILED` | 否 | 在 `max_system_retries` 内新建 allocation | 是 |
| `WORKLOAD_INITIALIZATION_FAILED` | 否 | 在 `max_system_retries` 内新建 allocation，被拒绝的 spec 除外 | 否 |
| `WORKLOAD_FAILED` | 仅 trial | trial，与目前相同 | 通过日志策略，与目前相同 |
| `INFRASTRUCTURE_FAILED` | 否 | 与目前相同（trial 视为瞬时） | 否 |
| `NONE` | 否 | 无 | 否 |

在该计划中，系统重试适用于 trial、command 和 shell。

<a id="idempotency-and-digest-scope"></a>
### 幂等与摘要范围

- **范围。** 键的范围是 `(jobs.owner_id, key)`，因此同一用户的所有客户端共享它。键永不过期，正如 job 行永不删除。
- **绑定。** dry run 从不绑定键。事务提交前出现的错误不绑定任何内容，包括 `plan_changed`。任何已完成事务提交的提交请求都会消耗该键，包括最终失败的请求，因此新的尝试需要新的键。
- **规划绑定。** 启动请求把规划的摘要作为 `expected_digest` 携带，master 拒绝摘要不同的请求。该检查在重放之后运行，因此对响应丢失的启动请求进行重试时，即使工作树此后已经改变，仍会返回其作业。
- **规划绑定的内容。** `expected_digest` 绑定客户端请求、代码和模板的内容。master 和资源池默认值（例如 `task_container_defaults`）不被绑定：它们是管理员策略，规划与启动之间的改动会应用于这次启动。因此，规划把其生效配置标注为规划时观察到的值，而不是冻结的值。
- **摘要。** 摘要是对客户端请求的 master 规范 JSON（键排序、UTF-8、不做 HTML 转义）计算的 SHA-256，在应用 master 默认值之前计算。只有 master 计算它，因此客户端无需复现这一形式，也不需要规范化库。合并后的 spec 不能作为身份：petname、sshd 端口、SSH 密钥、会话 token 和资源池默认值在完全相同的请求之间也会变化。

  | 包含 | 排除 |
  |---|---|
  | kind、workspace、project、模板名称与内容 | `idempotency_key`、`dry_run`、`expected_digest` |
  | config，从 YAML 或 Struct 解析后再规范化 | 文件 `mtime`、`uid`、`gid` |
  | 文件清单 `{path, type, mode, sha256}` | master 默认值与合并后的值 |
  | parent、fork、inherit、`no_pause`、`activate`、`admission` | |

- **摘要中的代码。** 对于 `git`，是固定的 SHA，由渲染后的命令和 `COMPUTE_CODE_COMMIT` 携带。对于 `context`，是文件清单，包括 `.code-provenance.json`。对于 `path`，只有目录字符串，因此规划把内容报告为 `unpinned`。
- **键的复用。** 只有当键的已存储摘要等于 `expected_digest`（如果已设置），否则等于请求的摘要时，才会重放；其他任何摘要都返回 409。`admission` 是摘要的一部分。
- **不存储请求。** 作业只保留摘要。生效配置与现在一样从任务或 experiment 读取，因此不会再保存一份可能包含 secret 的请求副本。
- **键与摘要的可见性。** 只有 owner 和管理员能看到它们，因此不带密钥的普通摘要也不会向其他读取者暴露任何可被暴力破解的内容。

<a id="ownership"></a>
### 所有权

owner 是已认证的用户，记录在 `jobs.owner_id` 中。MCP 的 `--owner` 命名空间被移除。控制操作沿用 fork 的现有规则：basic authz 下为 owner 或管理员（`master/internal/api_ntsc_control.go:15-26`），RBAC 下为 workspace 权限。重放会对已存储的 job 重新检查读取授权，因此已撤销的访问权限会生效。

<a id="code-and-storage"></a>
### 代码与存储

- **来源。** `code.source` 选择代码如何到达容器。它完全位于 MCP 渲染后的命令中；fork 保持不变。

  | 来源 | 代码根目录（`$COMPUTE_CODE_ROOT`） | 交付方式 | 可变 | 大小 |
  |---|---|---|---|---|
  | `git` | `/run/determined/code` | 前导命令以 `--shared --no-checkout` 把 `repo` 克隆到容器本地的临时空间，并以 `--detach` 检出固定的提交。不上传任何内容。 | 否 | 无上限；受节点磁盘限制 |
  | `context` | `/run/determined/workdir` | `revision` 处被跟踪的文件加上显式的 `include` 路径，作为任务 context（`files` 或 `model_definition`）发送。Determined 在 startup hook 之前解压它（`harness/determined/exec/prep_container.py:28-36`）。 | 否 | 99,614,720 字节 |
  | `path` | 共享存储上的 `dir` | 原地运行。 | 是 | 无 |

- **工作目录。** MCP 从不发送 `work_dir`。Determined 会拒绝它与 context 同时出现（`master/internal/spec_util.go:98-103`），trial 会忽略它（`master/pkg/tasks/task_trial.go:52`），而且它会成为任务用户的 home（`master/pkg/tasks/task.go:384-388`）。渲染后的命令自己切换目录，因此资源池默认的 `work_dir` 无法移动代码。`workdir` 相对于代码根目录，且永远不会越出它。规划只在字面上检查 `workdir`（相对路径，且不含 `..`），不解析符号链接，因此这一保证来自运行时的物理检查：进入 `workdir` 之后，前导命令把 `pwd -P` 与解析后的代码根目录比较；如果它位于代码根目录之外，就在第一条用户语句之前以 `compute: the workdir resolves to X, outside the code root` 失败。停留在树内的符号链接可以正常使用；离开树的符号链接，无论是提交在仓库中的还是位于共享存储上的，都会导致失败。对于 `path`，解析后的 `DIR` 就是根目录，因此 `dir` 本身可以是符号链接。该检查不会留下命令能看到的任何变量。使用 `context` 的 experiment 还会在 `/run/determined/train/model` 下得到一份副本。
- **渲染。** command 在 `bash -lc` 下运行，experiment entrypoint 在 `sh -c` 下运行（`harness/determined/exec/launch.py:43-44`），因此渲染出的文本是 POSIX sh，并且每种来源都采用同一种形式：

  ```sh
  <prelude> || exit $?
  <command>
  ```

  - `git`：一个子 shell 交付代码，随后前导命令创建 `OUT` 并进入 `WD`。前导命令只有一行；这里每个步骤各占一行，`…` 表示省略的文本：

    ```sh
    ( fail() { printf '%s\n' "compute: git code delivery failed: $1" >&2; exit 1; }
      <unset every exported GIT_* variable> || fail …
      export HOME=/dev/null/home XDG_CONFIG_HOME=/dev/null/home GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 || fail …
      git -c safe.directory=R clone -q --template= --shared --no-checkout -- R /run/determined/code || fail …
      git -C /run/determined/code checkout -q --detach SHA || fail …
      test "$(git -C /run/determined/code rev-parse --show-toplevel)" = "$(cd -- /run/determined/code && pwd -P)" || fail …
      test "$(git -C /run/determined/code rev-parse HEAD)" = SHA || fail … ) &&
    mkdir -p -- OUT && cd -- /run/determined/code/WD && CHECK
    ```

    该子 shell 对每个已导出的 `GIT_*` 变量执行 `unset`，变量名由 `sed` 从 `env` 的输出中列出；一个探测变量证明这一步确实运行过，因此缺少 `env` 或 `sed` 的镜像会使交付失败，而不是跳过这一步。它把 `HOME` 和 `XDG_CONFIG_HOME` 指向 `/dev/null` 之下（那里不可能存在任何内容），并关闭系统配置和系统 attributes，因此即使 git 版本过旧、不支持 `GIT_CONFIG_GLOBAL`，也不会应用任何用户或系统设置。克隆使用空模板，因此镜像的默认模板不会植入任何 hook、配置、attributes 或 ref。检出之后，工作树的顶层目录必须是 `/run/determined/code`，`HEAD` 必须是 `SHA`。因此，无论来自镜像、startup hook 还是 `TaskSpec.env`，任何 `GIT_*` 变量、用户或系统配置、系统 attributes 或克隆模板都无法移动或改变交付的树：要么固定的树位于根目录且 `HEAD` 位于 `SHA`，要么交付打印一行 `compute: git code delivery failed: …`，前导命令在任何用户语句之前失败。这种隔离只作用于交付，从不作用于工作负载：它随子 shell 一同结束，因此命令看到的是自己未被改变的 `GIT_*` 变量、`HOME` 和配置。当该 revision 含有 LFS 指针时，检出以 `GIT_LFS_SKIP_SMUDGE=0` 运行，并加上 `-c filter.lfs.process='git-lfs filter-process' -c filter.lfs.required=true -c lfs.fetchinclude= -c lfs.fetchexclude=`，因此缺少 `git-lfs` 时会失败，而不是留下指针文件，镜像或环境中的设置也无法跳过 smudge。从不递归处理 submodule。克隆目标不是 workdir，因为 `git clone` 需要空的目标目录，而 `/run/determined/workdir` 可能是任务用户的 `HOME`（`master/pkg/tasks/task.go:384-388`、`task-setup.sh:49-54`），startup hook 可能在那里写入。`/run/determined` 属于任务用户（`task.go:336`）。
  - `context`：`mkdir -p -- OUT && cd -- /run/determined/workdir/WD && CHECK`。
  - `path`：`mkdir -p -- OUT && cd -- DIR/WD && CHECK`。

  `CHECK` 是上文描述的 workdir 检查；当 `WD` 为 `.` 时省略它，因为此时的物理路径就是解析后的根目录。无论命令采用何种形式（`a; b`、`a & b`、多行），失败的前导命令都会在任何用户语句之前终止作业。在 `main` 上，`mkdir -p OUT && cd WD && CMD` 在失败后仍会运行后续语句，并可能以 0 退出（`compute/service.py:585-599`）。前导命令在初始化边界之后运行，因此它的失败分类为 `WORKLOAD_FAILED`，永远不会被系统重试；MCP 根据退出码和日志把它们报告为代码交付失败。shell 运行 `sshd`，没有命令，因此只接受 `context` 和 `path`，也没有前导命令。旧式的 `module:Class` experiment entrypoint 会被拒绝，因为任何前缀都会破坏它（`launch.py:32-40`）。
- **`git` 的规划检查。** `compute_plan` 通过配置的存储访问方式（本地挂载或 SSH）对仓库运行只读的 `git`。`repo` 必须位于某个 bind mount 目标之下。`revision` 必须解析为一个提交，该提交被固定为完整 SHA。固定确定的是代码的身份，并不保证该提交的对象一直可用（风险 5）。该提交必须包含在某个分支或 tag 中（`commit_not_on_ref`），因为克隆通过 alternates 借用对象，而源仓库中的 `git gc` 会清除不可达的对象。partial clone 会被拒绝，因为延迟获取需要网络；linked worktree 和带 alternates 的仓库也会被拒绝，因为它们的对象可能位于容器无法解析的路径。缺失的 LFS 对象是错误（`lfs_object_missing`），submodule 会得到 `submodule_not_checked_out` 警告。镜像必须提供 `git`，容器用户必须能读取该仓库；规划无法检查这两点。
- **只读规划。** 规划以参数列表运行 `git`，不读取用户或系统配置，清空所有 filter 驱动并禁用延迟获取，因此不会运行仓库配置的任何命令。它像容器克隆一样通过 `safe.directory` 信任该仓库。规划需要 git 2.32 或更高版本，并在对任何仓库运行命令之前检查版本（`git_too_old`，指明找到的版本和要求的版本）。缺失的对象通过一个从不获取对象的列举找出，且在读取任何对象之前进行；所有传输协议都被拒绝，因此在规划期间，任何 git 版本都不会延迟获取或联系远程仓库。
- **`context` 的规划检查。** 大小在最终载荷上计算，方式与 harness 的 `v1File_size` 完全相同（`harness/determined/common/v1file_utils.py:9-13`）：每条带内容的记录（包括符号链接的目标和 `.code-provenance.json`）计为其 base64 内容长度乘以 3/4，即其大小向上取整到 3 的倍数。总数超过 99,614,720 字节（`context.py:19-28`、`constants.py:5-18`）时返回 `context_too_large`，附带总大小、限制、最大的几个路径，以及改用 `git` 或共享存储的提示，且不发起创建调用。MCP 不模拟 master 对整个请求的限制，即 96 MiB 的 gRPC 消息限制（`master/internal/grpcutil/api.go:81-85`）：超限的请求会在 master 处失败，且不创建任何内容。解析后仍位于树内的相对符号链接会被保留；绝对的或越出树的符号链接是规划错误（`unsafe_symlink`），因为 harness 会在初始化时拒绝整个归档（`harness/determined/common/tarfile_utils.py:38-76`）。harness 会丢弃归档中的所有权信息，并把权限模式屏蔽为 0755（`tarfile_utils.py:78-89`）。命中硬性 secret 规则的文件从不上传，命中软性规则的文件只有在 `include` 中点名时才上传，两者都列为 `excluded`。LFS 指针会得到 `lfs_pointer` 警告，被跟踪的根目录 `startup-hook.sh`（Determined 会在命令之前 source 它）会得到 `startup_hook`。任何能读取该作业的人都能读取 context；在 basic authz 下，这意味着 experiment 的任何查看者（`experiment/authz_basic_impl.go:26-30`）。这就是 secret 规则只适用于 `context` 的原因。
- **来源信息。** 配置携带 `COMPUTE_CODE_SOURCE`、`COMPUTE_CODE_ROOT`，对于 `git` 和 `context` 还有 `COMPUTE_CODE_COMMIT`。它们取代 `COMPUTE_WORKDIR` 和 `COMPUTE_CODE_REVISION`（`compute/service.py:426-431`）。`context` 还会附带 `.code-provenance.json`，内容为 `{commit, dirty, included, excluded, skipped}`，不含时间戳，因此未改变的树会渲染出相同的摘要。对于 `path`，规划报告它观察到的 HEAD 和 dirty 状态，并标注为未经验证。
- **存储根目录。** 管理员挂载每个 bind 源路径：`task_container_defaults.bind_mounts`，以及 workspace 或 master `checkpoint_storage` 的 `shared_fs.host_path`。在任何作业运行之前，每个源路径都存在于该资源池的每个 agent 上。作业从不指定 bind 源路径。
- **数据和输出**位于工作负载在这些根目录内创建的运行目录中，由 `storage_sync` 和 `storage_fetch` 移动。`git` 仓库和 `path` 目录也位于某个根目录之下。
- **检查点**只设置相对的 `storage_path`。`host_path` 先继承 workspace 默认值，再继承 master 默认值（`master/internal/core_experiment.go:335-349`）；没有 `shared_fs` 默认值时，提交和 `dry_run` 都无法通过完整性检查（`:371`）。harness 创建该目录（`harness/determined/common/storage/base.py:58-80`），检查点 GC 挂载同一个根目录（`master/pkg/tasks/task_gc.go:126-136`）。
- **不在本设计中**：快照服务、对象存储或 `snapshot_id`。

<a id="defaults"></a>
### 默认值

| 设置 | 默认值 | 设置位置 |
|---|---|---|
| `admission` | `queue` | `TaskSpec`，与 `ADMISSION_UNSPECIFIED` 对应 |
| 幂等键 | 最多 128 个 `[A-Za-z0-9._:-]` 字符 | API |
| 任务 context 大小 | 按 harness 方式计数的 99,614,720 字节（现有的 `MAX_CONTEXT_SIZE`） | harness 常量，由 MCP 检查 |
| 最低提交协议 | MCP 1.0 为 1 | MCP |

<a id="what-remains-unverified"></a>
### 仍未经验证的内容

身份、owner、状态、放置和退出类别都是权威信息，`cross_profile_unverifiable` 已不复存在。以下各项仍未经验证，并在它们出现的每个地方加以标注：

- 作业将被放置在哪里，因为提交之前不评估放置；
- 规划的生效配置，其中的 master 默认值可能在启动前改变；
- 升级前的 job，它们没有键或摘要，无法重放；
- `path` 代码，它是可变的；规划报告它观察到的内容；
- task-resources 数据不可用时的 `unmeasured` 用量。

<a id="choices-between-alternatives"></a>
### 备选方案之间的取舍

| 问题 | 决定 | 理由 |
|---|---|---|
| 第一个版本的范围 | 只包含 F1 和 F2 | 先让提交、观察和取消变得可靠；评估、立即准入、自动重试和预检等到有需求时再做（可靠地失败原则）。 |
| 台账放在哪里 | `jobs` 上的可空列 | `jobs` 很少更新（只有 `q_position`，`api_experiment.go:1524-1528`）。不需要新表、不需要回填，也没有阻止删除的 FK。 |
| 创建 API | 在四个 RPC 上加 envelope，不使用 `Submit` oneof | 所有客户端共用一条创建路径，也不会出现代码生成器从未处理过的 oneof 请求体结构。 |
| 句柄 | 只用 `job_id` | 所有读取和操作共用一个句柄。 |
| 规划绑定 | `expected_digest`，在重放之后检查 | 客户端无法重新计算 master 的摘要，而响应丢失后的重试仍必须能够重放。 |
| 规划冻结什么 | 请求、代码和模板；不包括 master 默认值 | 默认值是管理员策略。绑定合并后的配置需要一份易变字段列表（petname、端口、会话 token），而 master 一旦改变，这份列表就会悄无声息地失效。 |
| 作业结束 | `tasks.end_time`，由退出决策写入 | 从 allocation 推断结束会误判重试间隙和暂停。 |
| 容量与放置 | 一个类别，附带 `static` 或 `busy` 详情 | 只需一条提交后路径。 |
| 重试机制 | 新 allocation，推导出的预算 | 恢复以状态为依据；在 `ASSIGNED` 之后复用 allocation ID 会把两次启动的资源行混在一起。 |
| Agent 丢失 | 不变 | 预算为 3 会在维护期间结束长时间运行的 trial。 |
| Command 和 shell 重试 | 新增 | 批处理类型仍然是 command，屏蔽节点行按任务记录。 |
| Generic 任务重试 | 推迟 | 需要先重做全局变更锁（`generic_task_resume.go:25-28`）和 resume 序号。 |
| 初始化边界存放在哪里 | 按资源存储，在清除前分类 | 一个节点无法掩盖另一个节点的失败。 |
| 跨越升级的初始化证据 | `allocations.reports_workload_start` | 容器保留其创建时的 entrypoint，因此只有在本应报告的地方，NULL 的启动时间才构成证据。 |
| 缺失的 bind 源路径 | `WORKLOAD_INITIALIZATION_FAILED`，不可恢复，不屏蔽节点 | 这是请求错误；屏蔽节点按任务记录；挂载点存在但文件系统未挂载时，Docker 无法察觉。 |
| 目录 | 由工作负载在管理员根目录内创建 | bind 源路径必须在容器创建之前存在。 |
| 代码 | 三种显式来源：`git`、`context`、`path` | `git` 无需上传也没有上限，`context` 可以携带未提交的改动，`path` 只有在显式选择时才可变。没有存储、没有 GC、没有悬空引用。 |
| 摘要 | 普通 SHA-256，仅 owner 可见 | 不需要新的 master secret。 |
| 版本门槛 | 整数 `submission_protocol` | release 字符串无法标识功能集。 |
| `GetCommand` 的数据库回退 | 推迟 | `GetSubmission` 是面向所有客户端的持久读取接口。 |
| 咨询、`compute_cli.py` | 移除 | 实验分析属于官方 W&B MCP，而且 `det` 和 WebUI 就是其他客户端。 |

<a id="experiment-tracking-with-wb"></a>
## 使用 W&B 追踪实验

部署可以在 Determined 旁边运行 W&B。两者保存不同的记录，并显式关联：

| 记录 | 负责方 | 回答的问题 |
|---|---|---|
| 执行：作业、trial、任务、allocation、退出类别、放置、用量 | Determined，通过本 MCP | 是否已提交、在哪里运行、为何未启动、占用了什么 |
| 实验：config、指标、代码与数据引用、检查点、血缘、报告 | W&B，通过其官方 MCP | 效果如何、由哪些代码和输入产生、哪个版本最好 |

- **关联。** 每个任务容器都有 `DET_TASK_ID` 和 `DET_ALLOCATION_ID`（`master/pkg/tasks/task.go:199-200`），trial 还有 `DET_EXPERIMENT_ID` 和 `DET_TRIAL_ID`（`task_trial.go:115-116`）。F2 增加 `DET_JOB_ID`。`DET_CLUSTER_ID` 已经存在：agent 会把它加入自己启动的每个容器（`agent/internal/containers/spec.go:219`），Kubernetes 和 dispatcher 也会设置它（`kubernetesrm/spec.go:137`、`dispatcher_task.go:800`）。使用 W&B 的工作负载在 run 的 config 中记录这些值，按作业对 run 分组，并由集群和 trial（command 则为任务）推导出稳定的 run ID，使重启的 trial 继续同一个 run。多 trial 的 experiment 对应多个 run；不同 trial 从不混入同一个 run。
- **代码来源。** run 记录 MCP 固定的代码：`git` 为提交，`context` 为 `.code-provenance.json` 中的清单摘要，`path` 为未固定的目录。它不会再从可能已经变化的工作树中重新收集代码。
- **数据与检查点。** 大文件留在共享存储上。W&B 引用产物可以记录其路径和校验值而无需上传，但引用既不会冻结这些文件，也不能证明每个节点都能访问它们。
- **触发。** 研究事件（例如新的产物版本或别名）可以驱动 W&B Automations。一个窄的适配器校验事件、选择已批准的模板、固定产物版本，并通过创建 envelope 提交，其幂等键由事件推导，因此重复或延迟的 webhook 不会创建第二个作业。取消、派发和重试仍由 Determined 负责；W&B 中 `Crashed` 的 run 从不重启作业。
- **搜索。** 每个 experiment 只有一个搜索控制者，默认是 Determined searcher。W&B Sweeps 可以代替它驱动 trial，但两者从不同时使用。
- **不采用。** W&B Launch 没有 Determined 后端，而它自己的后端会在同一批 GPU 上增加第二个执行控制者。
- **部署检查。** Automations、Registry 和 run 状态触发取决于 W&B 的版本、部署类型和许可。依赖它们之前，先在部署上确认。
- **凭据。** 与其他 secret 一样，W&B 密钥不进入任务 spec，也不进入作业台账。

<a id="the-mcp-after-the-refactor"></a>
## 重构后的 MCP

<a id="tools"></a>
### 工具

工具分为四组：规划（`compute_plan`、`compute_launch`）；只读观察（`compute_status`、`compute_list`、`compute_logs`、`compute_usage`、`compute_resources`、`storage_check`）；控制（`compute_cancel`）；以及传输（`storage_sync`、`storage_fetch`）。共 11 个，少于 `main` 上的 16 个（14 个基础工具加 2 个咨询工具）。数量本身不是目标：只读工具只要透传平台或存储事实、而不推导新的事实，就会保留。

| 工具 | 行为 |
|---|---|
| `compute_plan(spec: TaskSpec)` | 把 `code.revision` 解析为提交 SHA，渲染 spec，应用策略，并以 `dry_run` 发起一次创建调用。返回解析后的 spec、新的 UUIDv4 `request_id`、master 的 `request_digest`（对客户端不透明）、提交 SHA、内容摘要（`context` 为清单，`git` 为 SHA，`path` 为 `unpinned`）、生效配置概要和警告，其中包括位于生效 bind mount 目标之外的路径。它不评估放置；由调度器在启动之后决定。每次规划都会访问 master。 |
| `compute_launch(spec, request_id, request_digest)` | 再次渲染 spec，并以 `idempotency_key=request_id` 和 `expected_digest=request_digest` 发起一次创建调用。固定到 SHA 的 spec 从不重新解析。如果内容发生了变化（例如 spec 仍指定一个会移动的 revision），它返回 `plan_changed`，附带新的提交 SHA 和内容摘要，且不创建任何内容。使用相同参数重试会重放该作业。返回 `job_id`、`replayed`、`submitted_at` 和 `outcome`。 |
| `compute_status(job_id)` | `GetSubmission`，并附带对退出类别的解释。 |
| `compute_list(kind=None, state=None, limit=50, cursor=None)` | 针对调用方的 `ListSubmissions`，覆盖所有客户端。 |
| `compute_logs(job_id, trial_id=None, tail=200)` | 任务日志。 |
| `compute_usage(job_id, trial_id=None, allocation_id=None, window_seconds=3600, metrics=None, include_samples=False)` | 任务资源，并附带解读。 |
| `compute_resources(pool=None)` | 来自 `GetResourcePools` 的资源池（名称、类型、agent、可用与已用 slot、slot 类型、每个 agent 的 slot 数、辅助容量），以及来自 `GetAgents` 的设备型号，并标注 `observed_at`。这是不带结论的投影；放置由调度器在启动之后决定。 |
| `storage_check(path)` | 容器路径是否存在、其类型，以及是否可读、可写，并附带视角：后端（本地挂载或 SSH 主机）及其运行时使用的用户。权限是该视角的权限，而不是容器用户的权限。 |
| `compute_cancel(job_id)` | `CancelSubmission`。 |
| `storage_sync(local_dir, shared_dir, dry_run=True, overwrite=False)` | rsync；未设置 `overwrite` 时添加 `--ignore-existing`。 |
| `storage_fetch(shared_dir, local_dir, dry_run=True, overwrite=False)` | 反方向的 rsync。 |

<a id="taskspec"></a>
### `TaskSpec`

`TaskSpec` 是一个 pydantic 模型，作为输入 schema 发布。它包含以下字段：

- `kind`：`command`、`shell` 或 `experiment`。
- `name` 和 `command`。
- `code`，省略时表示没有代码：
  - `{source: git, repo, revision}`：`repo` 是共享存储上的容器路径；
  - `{source: context, repo, revision, include, exclude}`：`repo` 是本地工作树。被跟踪的文件取自 `revision` 处的 git 对象；`include` 路径取自工作树，`dirty` 记录工作树是否有差异；
  - `{source: path, dir}`：`dir` 是共享存储上的容器路径。

  `revision` 默认为 `HEAD`，规划会把它固定为完整 SHA 后返回。
- `workdir`：相对于代码根目录（默认 `.`）。`output_dir`：位于某个已挂载根目录内的容器路径。前导命令先对 `output_dir` 执行 `mkdir -p`，再 `cd` 到 `workdir`，并在其物理路径位于代码根目录之外时失败。
- `admission`：`queue`（默认值，也是唯一的取值）；`immediate` 以 `admission_unsupported` 被拒绝。
- `image`、`pool` 和 `slots`。资源池和 slot 数总是根据策略默认值显式发送。
- `env`、`workspace` 和 `project`。
- `experiment`：experiment 配置。MCP 为自己的外层字段定义类型，并在此应用其更严格的策略；Determined 通过 `dry_run` 自行校验 experiment 配置（完整 schema、默认值、配置策略），MCP 不 vendor expconf schema。与顶层字段重复的键（`resources.resource_pool`、`resources.slots_per_trial`、`environment.image`、`environment.environment_variables`）会被拒绝，从而使编译出的请求没有歧义，也无法绕过 MCP 的限制。搜索必须设置 `max_concurrent_trials`，以便 MCP 按请求设置的最大 slot 数能限制 slot 数与并发数的乘积。MCP 拒绝 `bind_mounts`、`checkpoint_storage` 的 `host_path` 或 `container_path`，以及旧式的 `checkpoint_path` 和 `tensorboard_path`；`storage_path` 必须是相对路径，且不含 `..`。

没有 bind mount 字段。`accelerators` 在推迟的 M4 之前不加入。

<a id="modules"></a>
### 模块

| 模块 | 内容 | 来源 |
|---|---|---|
| `mcp_server.py` | 工具表 | 重写 |
| `spec.py` | `TaskSpec`、编译器和前导命令渲染器 | `compute/service.py` 中的 spec 部分；`_render_entrypoint`（`:585-599`）被替换 |
| `policy.py` | 默认值、资源池允许列表、最大 slot 数、`overwrite`、容器到宿主机的映射 | `compute/profile.py` 中保留的部分 |
| `client.py` | 传输、认证、脱敏、协议门槛、创建路由、submission、日志、trial、task-resources，以及供 `compute_resources` 和用量使用的资源池与 agent 读取 | `core/api_client.py`，去掉删除的部分 |
| `code.py` | revision 固定与 `git` 规划检查、`context` 枚举、include 与 secret 规则、清单、来源信息 | PR #1 `storage/snapshot.py:1-513,736-995` |
| `usage.py` | 用量解读 | `compute/service.py:67-103,176-230,772-1190` |
| `storage/` | sync、fetch、带视角的 check、只读 `git` 访问、配置、认证、askpass | 现有的包 |
| `utils/secrets.py` | 不变 | – |

<a id="local-state-and-version-gate"></a>
### 本地状态与版本门槛

- **没有本地状态。** 不再使用 SQLite，`--owner` 和 `--db` 被移除。配置包含 API URL 和凭据、策略、存储访问以及容器到宿主机的映射。没有本地别名或草稿：`compute_plan` 返回解析后的 spec 及其摘要，调用方把两者传回 `compute_launch`，而 `compute_list` 可以找回丢失的 `job_id`。没有任何功能依赖本地缓存，因此以后可以添加缓存而无需改变契约。
- **版本门槛。** MCP 启动时读取无需登录的 `GET /api/v1/master`，当 `submission_protocol` 低于其最低要求时拒绝提供服务：MCP 1.0 为 1。之后需要某项推迟能力的 MCP 会提高其最低要求。release 字符串只出现在错误信息中。它不能作为门槛：本地构建报告的是上一个 tag（`version.sh:69-114`），而 PR 候选构建会在其阶段完成之前就报告目标 release（`.github/workflows/fork-release.yml:52-65`）。

<a id="deletion-list"></a>
### 删除清单

| 目标 | 删除内容 |
|---|---|
| 整个文件 | `compute/store.py`、`compute/admission.py`、`compute_cli.py` 及其在 `pyproject.toml` 中的 `determined-compute` 脚本。`agent_worker.py` 及其数据表、`compute_consult` 和 `workflow_status` 工具，以及 `docs/consultation.md` 及其中文版本被移除。 |
| `compute/models.py` | `TaskRecord`（`:32-70`） |
| `compute/service.py`（拆解） | 上传字段拒绝 `:53-64,109-120`；认领、状态标记与不确定性 `:166-175,601-661,725-735`；路径校验 `:489-515,569-583`；容量钩子 `:662-668`；幂等与旧版 hash `:669-724,1435-1452`；能力检查 `:831-835`；远端身份 `:1212-1294`；发现、接管与调和 `:1295-1434`；绑定 `:1453-1526`；提交标记 `:1527-1573` |
| `core/api_client.py` | kind、用户与集群辅助函数 `:220-264`；`list_remote_tasks` `:265-321`；提交标记与上传字段 `:381-465`；按类型分派 `:466-521`，它将改为四个创建路由加 `CancelSubmission`；不支持时的回退 `:567-582`。`list_resource_pools` 和 `list_gpu_devices`（`:647-690`）保留，因为用量会调用它们（`compute/service.py:922,942`），而 `allocation_accelerators` 不存储 GPU 型号；资源池读取会为 `compute_resources` 扩展。 |
| `compute/profile.py` | 路径校验器（`:161-199`）、`cluster_identity` 和 `fingerprint`（`:200-215`）；挂载列表保留，作为容器到宿主机的映射 |
| `mcp_server.py` | reconcile、list、discover 和 adopt 工具（`:142-176`）；`--owner` 和 `--db` 的接线 |
| 测试 | `test_adoption_store`、`test_remote_adoption`、`test_compute_legacy_retry`、`test_owner_namespace`、`test_admission`、`test_compute_cli`；`test_compute_service` 和 `test_api_client` 中与台账和绑定相关的部分 |
| PR #1 | 不合并，直接关闭。丢弃：`gpu_admission.py` 及其 NVML 分支、`storage/paths.py`、跨 profile 代码、启动路径校验、`create_directories`、`snapshot.py` 中属于 store 的一半（`:514-735,996-1097`），以及它们的测试。保留：文件枚举、secret 规则、约 565 行测试以及 `resolve_api_url`。 |

预期结果是约 3,000 行源码，而 `main` 上约为 6,300 行，加上 PR #1 则约为 8,600 行。

<a id="delivery-plan"></a>
## 交付计划

<a id="phasing"></a>
### 分阶段

第一个版本只让提交、观察和取消变得可靠，不包含其他内容：fork 0.41.0 包含 F1 和 F2，提交协议为 1；MCP 1.0 包含 M1、M2 和 M3。[推迟的设计](#deferred-designs)没有对应的版本，每项只在出现真实需求时才排期。

| Fork 版本 | Fork PR | 提交协议 | MCP 版本 |
|---|---|---|---|
| 0.41.0 | F1, F2 | 1 | 1.0：M1, M2, M3 |
| 推迟 | F3a, F3b, F4, F5 | 各自提高 | M4 |

<a id="fork-pull-requests"></a>
### Fork PR

| PR | 内容 | 依赖 |
|---|---|---|
| F1 退出类别与修复 | 贯穿所有层的 `ExitClass`；新的失败类型；覆盖所有情况的分类器；按状态设定的 `closeOpenAllocations` 类别；`UnknownError` 映射；`crash(*msg)`；`allocations.exit_class` 和 `exit_detail`；在清除之前用一条 UPDATE 写入的退出记录；`IdentifyTask` 修复。在 F4 加入初始化边界之前，`ResourcesFailed` 和 `TaskError` 分类为 `WORKLOAD_FAILED`；F4 之后，对于在它之前创建的 allocation 仍然如此 | – |
| F2 台账 | `jobs` 迁移；`SubmitOptions`（含 `expected_digest`）和 `SubmitResult`；handler 顺序，包括规划检查、无副作用的 `dry_run`（尚不评估）以及 `validate_only` 别名；在 F3b 之前 `ADMISSION_IMMEDIATE` 返回 `UNIMPLEMENTED`（对 experiment 返回 `INVALID_ARGUMENT`）；单一的提交事务；`dispatch` 及其三个调用方和提交结果未知分支；每次启动后的取消检查；`ACTIVE` experiment；经由 `command_state`、以状态为依据的恢复，包括 `PENDING` 清除、启动预写、结束未启动的快照、状态不一致失败、限定范围的 `start_time` 写入，以及使用已存储 ID 的 `Command.Start`；`Get`/`List`/`CancelSubmission`，包括 GENERIC 子树和 resume 处理；由 `CancelSubmission` 和现有 kill endpoint（`KillCommand`、`KillShell`、`KillGenericTask`，`api.proto:1539,1490,2645`）写入的 `cancel_requested_at`；`get_task.sql` 字段；任务容器中的 `DET_JOB_ID`；`submission_protocol` 为 1 | F1 |
| F3a 评估 | `TaskList.Clone`；`rp.Evaluate`；让旧检查基于静态适配，同时保留每个调用点的结果；`dry_run` 评估及其限流；其他 RM 上的 `Unimplemented`；提高 `submission_protocol` | 推迟；F2 |
| F3b 立即准入 | tick 决策与 handler 等待；`allocations.immediate` 和 `ADMITTING` 状态；IMMEDIATE 恢复；provider 资源池拒绝；提高 `submission_protocol` | 推迟；F3a |
| F4 初始化边界与重试 | `workload_started_at` 和 `allocations.reports_workload_start`；内部 RPC；每个 entrypoint 中的 `prep_container --workload-start`；带推导预算的 `max_system_retries`；trial 分支；command 和 shell 的单事务退出决策，限定于已结束的 allocation；按 allocation 判定的重试资格，并停止接受工作负载启动通知的发送；屏蔽节点行；提高 `submission_protocol` | 推迟；F1, F3b |
| F5 设备与预检 | 在 `device.Device` 旁边传递的显存检测；`resources.accelerators`；`deviceSatisfied`；`cproto.Preflight`；agent 钩子；agent 版本门槛；`SpecRejected` 映射；提高 `submission_protocol` | 推迟；F3a, F4 |

master 和 agent 一起升级。fork 保持重新挂接路径的兼容性。`device.Device` 和重连比较保持不变，因为比较不一致会使 agent 关闭（`rm/agentrm/agent.go:609-634`）。容器标签版本同样不变（`agent/internal/containers/spec.go:171-175`）。只有当 agent 在新 master 启动后的 `agent_reconnect_wait`（默认 150 秒，`aproto/net.go:9-17`）内重新连接，并且容器仍处于停止前记录的状态（`containers/manager.go:287-295`）时，运行中的容器才会被重新挂接。否则它会被终止，其 allocation 以 `RestoreError` 结束，分类为 `INFRASTRUCTURE_FAILED`。重新挂接的容器保留旧的 entrypoint 和 wheel。新 master 继续提供上一版本的任务 API，该容器的 allocation 在没有初始化边界的情况下分类。

<a id="mcp-pull-requests"></a>
### MCP PR

| PR | 内容 | 依赖 |
|---|---|---|
| M1 收窄 server | 移除咨询 worker；删除 `compute_cli.py`；关闭 PR #1 | – |
| M2 改用台账 | `client.py`、协议门槛以及 `job_id` 句柄；基于 submission 的 launch、status、list、logs、usage 和 cancel；删除 store、提交标记、reconcile、discover、adopt、绑定和 owner 命名空间 | F2 |
| M3 类型化 spec | `TaskSpec`、`spec.py`、`policy.py` 和 `code.py`；三种代码来源与前导命令渲染器；通过 `dry_run` 规划，并通过 `expected_digest` 绑定启动；透传 `admission`；作为投影的 `compute_resources` 和带视角的 `storage_check`；删除 `admission.py` 和路径校验 | F2 |
| M2 + M3 接入 | 一个切换 PR 同时包含 M2 和 M3 的接入部分（`policy.py`、编译器、规划与启动、`compute_resources`、`storage_check` 以及 M3 的删除项），叠加在 M3 核心（`TaskSpec`、`code.py` 和前导命令渲染器）之上 | F2 |
| M4 加速器 | `TaskSpec` 中的 `accelerators` | 推迟；F3b, F5 |

- **合并顺序。** MCP PR 只有在其 fork 依赖进入 fork `main` 之后才能合并。集成测试针对目标 fork 版本的预发布构建运行。
- **发布。** 在 fork 打出 0.41.0 tag 之前不发布 MCP 1.0。
- **文档。** 每个 PR 都更新其涉及的英文和中文文档。M3 重写[计算服务参考](compute-service.zh.md)、[Agent 工作流](agent-workflow.zh.md)、[故障排查](troubleshooting.zh.md)和 `AGENTS` 路由。

<a id="deferred-designs"></a>
## 推迟的设计

以下设计已经完成，但尚未排期。每项只在出现真实需求、并通过[原则](#principles)中的复杂度门槛时才构建；每项落地时都会提高 `submission_protocol`。

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
- **Handler 等待。** 在[提交处理顺序](#submit-handler-order)中，分派之后增加第 8 步：启动调用返回后，handler 在 `cs.mu`（`command/command_service.go:95`）之外，通过 allocation service 的读锁轮询该 allocation，直到它离开 `PENDING`。等待上限为 5 秒，超时后结果为 `PENDING`。
- **范围。** IMMEDIATE 适用于提交本身创建的每个 allocation，包括系统重试。之后由用户触发的 allocation（例如 generic 任务的 resume）会排队。每个 allocation 在 `allocations.immediate boolean NOT NULL DEFAULT false` 中记录自己的准入方式，在插入时设置。恢复和状态推导读取的是它，从不读取 `jobs.admission`，因此 IMMEDIATE 作业的排队 resume 会以排队方式恢复。
- **恢复。** 标记为 `immediate` 的 `PENDING` 尝试在清除之后以 `PLACEMENT_UNSATISFIED` 结束，而不是重新发起请求。已放置的尝试永远不会以 `PLACEMENT_UNSATISFIED` 结束。
- **状态。** 在首次决策之前，立即准入的尝试读作 `ADMITTING`，F3b 把该状态加入 `Submission.State` 和任务状态规则。它从不读作 `QUEUED`：它要么被放置，要么以 `PLACEMENT_UNSATISFIED` 结束。
- **提交时拒绝。** 每个 experiment（包括只有单个 trial 的 experiment）都得到 `INVALID_ARGUMENT`（见[准入](#admission)），provider 支持的资源池得到 `FAILED_PRECONDITION`。

<a id="initialization-boundary"></a>
### 初始化边界

- **列。** `allocation_resources` 增加 `workload_started_at`。`allocations` 增加 `reports_workload_start boolean NOT NULL DEFAULT false`，F4 在它插入的每条 allocation 行上设置该列（`task/allocation.go:527`）。它不在 `AddAllocation` 的 `ON CONFLICT` 列表中（`db/postgres_tasks.go:190-193`），因此插入后永不改变。
- **为什么需要标记。** 容器保留其创建时的 entrypoint 和 wheel（`master/pkg/tasks/task.go:151-159`、`copy.go:35-37`），而恢复的 allocation 不启动任何容器（`allocation.go:666-693`）。因此，跨越升级运行的 allocation 永远不会发送该通知。NULL 的 `workload_started_at` 表示“未报告”；只有当该 allocation 报告工作负载启动时，它才表示“未启动”。否则 `exit_detail` 携带 `init_boundary: "unknown"`。
- **RPC。** `PostAllocationWorkloadStarted{allocation_id, resources_id}`，其授权方式与 `AllocationReady`（`api.proto:921`）相同。它只设置一次时间，重复调用时返回 OK。对于未知或已关闭的 allocation，或属于其他 allocation 的资源，它会失败。
- **由谁发送。** `prep_container --workload-start`，作为该调用在 startup hook 之前的最后一步，出现在每个调用 `prep_container` 的 entrypoint 中：`master/static/srv/command-entrypoint.sh:11`、`shell-entrypoint.sh:7`、`generic-task-entrypoint.sh:13`、`entrypoint.sh:9`、`notebook-entrypoint.sh:14`、`tensorboard-entrypoint.sh:12` 和 `gc-checkpoints-entrypoint.sh:7`。该标志是显式的，因为 Slurm 的 `task-setup.sh:40` 会更早调用 `prep_container`。trial 之后的 `--rendezvous` 调用不发送。hook 属于用户代码。
- **失败即阻断。** 发送失败会抛出异常，`prep_container` 以非零状态退出，而每个调用点都已开启的 `set -e` 会在 hook 之前终止脚本。来自镜像的、早于 F4 的 harness（`DET_SKIP_PIP_INSTALL`、`task-setup.sh:26-33`）会拒绝该标志，并以同样的方式失败；这类镜像必须携带 F4 的 harness。已提交但丢失响应的发送以 `WORKLOAD_FAILED` 结束，它永远不会重新运行用户代码。
- **何时设定类别。** 类别在写入退出记录之前根据 `workload_started_at` 计算，这些行只在该记录提交之后才被清除（见[退出类别](#exit-classes)）。这些行的生命周期与 allocation 完全一致，因为启动时只清除已关闭 allocation 的行（`taskmodel/resources.go:53-63`）。

<a id="system-retries"></a>
### 系统重试

- **每次重试一个新 allocation。** 重试总是新的 allocation `<task>.<n+1>`，从不重新放置已有的 allocation。
- **预算。** `resources.max_system_retries`（master 默认 3）限制 `NODE_PREFLIGHT_FAILED` 和 `WORKLOAD_INITIALIZATION_FAILED` 的重试次数，但被拒绝的 spec 除外，它不可恢复：F5 把 `ResourcesFailedError{SpecRejected}` 加入 `sproto.IsUnrecoverableSystemError`（`sproto/resources.go:342-349`）。预算通过统计该任务中带有这些类别的 allocation 推导得到，因此无需计数器即可在重启后保留。
- **屏蔽节点。** 只有 `NODE_PREFLIGHT_FAILED` 会把 `(task, node, "preflight:<check>")` 写入现有的屏蔽节点表；该表以 task ID 为键，没有 FK（`logpattern/logpattern.go:133-159`）。
- **停止。** 预算耗尽，或屏蔽节点导致不再存在静态适配时，重试停止。
- **Trial。** 在 `trial.go:612` 与 `:621` 之间新增一个分支，重新分配 allocation 且不增加 `restarts`。`:603` 处的不可恢复检查先执行，因此被拒绝的 spec 会使 trial 以 `ERROR` 结束，且不增加 `restarts`。
- **Command 与 shell。** 它们通过 `OnExit` 中的单一退出决策获得重新分配能力。一个事务锁定 job 行，读取已结束尝试的类别（它已经持久化，因为退出记录在 `onExit` 之前提交）和预算，然后要么把 `<task>.<n+1>` 以 `PENDING` 状态插入，连同其 workspace 记录和屏蔽节点行，并让 `command_state` 指向它，要么设置 `tasks.end_time`。只有在 `cancel_requested_at` 为 NULL 时才会重试。新尝试只在事务提交后启动，加载已存在的行，然后检查取消标志。重试的退出会保留会话 token 和 registry 条目，也不安排垃圾回收；只有结束分支运行目前的 `OnExit` 收尾部分（`command.go:239-279`）。在任何时刻崩溃，留下的状态都会由恢复完成。
- **资格按 allocation 判定。** 系统重试要求该 allocation 报告工作负载启动，且它的任何资源都没有 `workload_started_at`。退出决策首先让该 allocation 不再接受工作负载启动通知的发送（allocation 进入退出过程后，该 RPC 会失败），因此决策之后不会再有资源越过边界。如果已有资源越过边界，该 allocation 保留其类别（取自失败的资源），但不进行系统重试：command 和 shell 结束，trial 遵循 `max_restarts`。因此，第一个节点已运行其 startup hook 的多节点 allocation 永远不会作为初始化重试重新运行。
- **决策范围。** 只有当 `command_state`（或 trial 的当前 allocation）指向已结束的 allocation 时，退出决策才会生效。`.2` 已存在之后，`.1` 迟到或重复的退出不产生任何作用：它既不会结束 `.2`，也不会插入 `.3`。预算根据行统计，因此不会重复消耗。
- **保持不变。** `INFRASTRUCTURE_FAILED` 保留现有的瞬时处理（`sproto/resources.go:353-371`）。generic 任务与目前一样以其类别结束（`spec_util.go:151-160`）。

<a id="placement-constraints-scheduler"></a>
### 放置约束（调度器）

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
- **升级门槛。** 只有当 agent 的 `AgentStarted.Version`（`master/pkg/aproto/master_message.go:80`）支持设备显存和预检时，它才适配带 `accelerators` 的请求。较旧的 agent 永远不会收到这类请求，`dry_run` 把它们列在 `blocked_nodes` 中，原因为 `agent_upgrade_pending`，因此启用加速器无需整个资源池同时切换。
- **检测。** nvidia-smi 查询增加 `memory.total`，因此解析器的字段数从 3 变为 4（`agent/internal/detect/nvidia.go:25-27,92-94`）。MIG 设备报告 0。显存信息在设备旁边传递，即 `AgentStarted.DeviceMemoryMiB map[device.ID]int`，保存在每个 `slot` 上，并在每次 `AgentStarted` 时替换。`device.Device`（`master/pkg/device/device.go:43-48`）保持不变，因为它是 `agentState.Devices`（`agent_state.go:49`）的键，并且会持久化到容器快照中，恢复时按值查找（`:526-530`）。因此重连比较（`agent_state.go:261-292`）也保持不变。

<a id="agent-preflight"></a>
### Agent 预检

- **Spec。** `cproto.Spec` 增加 `Preflight{MinFreeMemoryMiB, MaxUtilizationPercent, Timeout}`（`master/pkg/cproto/spec.go:14-18`），由 `ToDockerSpec` 填写（`master/pkg/tasks/task.go:261`）。
- **在启动路径中的位置。** 预检在 `PullImage` 之后、`CreateContainer` 和 `c.spec = nil` 之前运行（`agent/internal/container/container.go:198-222`）。它不能在 `manager.StartContainer` 中运行，因为那里的错误只会被记录日志（`agent/internal/agent.go:183-185`）。
- **检查项。** 它每秒对分配到的 UUID 采样一次 `nvidia-smi --query-gpu=uuid,memory.used,memory.total,utilization.gpu`。阈值满足即通过，超过 `Timeout`（master 默认 30 秒）则失败。这段宽限期用于等待任务的上一个容器释放显存。GPU 阈值是它唯一的检查项。
- **不做的事。** 它从不对宿主机路径执行 `stat`，也不使用 `--query-compute-apps`，因为文档所述的 agent 容器只能看到 `docker.sock` 及其配置（`docs/setup-cluster/on-prem/options/docker.rst:117-171`）。
- **被拒绝的 spec。** bind 源路径必须在 `CreateContainer` 之前存在。bind mount 的类型为 `mount.TypeBind`（`master/pkg/tasks/mounts.go:19-27`），Docker 在创建时校验它们，并返回 `InvalidParameter`。agent 把任何满足 `errdefs.IsInvalidParameter` 的 `CreateContainer` 错误映射为 `aproto.SpecRejected`；目前它是 `TaskError`（`container.go:341-342`）。这不是预检检查，也永远不会屏蔽节点。fork 从不设置 `BindOptions.CreateMountpoint`，因为那样 Docker 会在本地磁盘上创建缺失的根目录。
- **失败详情。** 失败携带 `{check, device, observed, required}`，同时写入 exit detail 和一行容器日志。

<a id="admission"></a>
### 准入

- **`queue`** 是默认值，与 `ADMISSION_UNSPECIFIED` 对应，因此 `det` 和 WebUI 的行为不变。MCP 在 dry run 和启动中都把 `TaskSpec.admission` 作为 `SubmitOptions.admission` 透传。它没有先评估后提交的路径。
- **`immediate`。** 资源池的调度器在做出决策的 tick 中放置该请求，且不抢占。否则它以 `PLACEMENT_UNSATISFIED`（`static` 或 `busy`）结束，并且永远不会排队。在 F3b 之前，master 返回 `UNIMPLEMENTED`，MCP 将其报告为 `admission_unsupported`。
- **静态不可行**在 `queue` 下仍然只是警告。
- **Experiment。** 对每个 experiment（包括只有单个 trial 的 experiment），`immediate` 都会以 `INVALID_ARGUMENT` 被拒绝。IMMEDIATE 只决定在提交调用内请求的 allocation。experiment 激活后，它的每个 trial 各自请求自己的 allocation（`api_experiment.go:1674-1687`、`trial.go:371-374`），后续 trial 在前面的 trial 退出时才创建，而重启是新的 allocation（`trial.go:692-700`）。只决定最初的几个 allocation，会让一次搜索的一部分获得准入，而其余部分排队。`dry_run` 以信息形式报告 N 个中的 `placeable_now` 数量，仅供参考。

<a id="placement-constraints-semantics"></a>
### 放置约束（语义）

- **静态事实是硬约束**：GPU 型号、总显存，以及保持不变的 `is_single_node`。command 仍然只使用单个 agent（`command.go:161-176`）。
- **动态事实是预检检查**：空闲显存和利用率。二者都永远不会成为放置事实。
- **不做预留。** 预检通过不会预留任何资源，评估是在 `evaluated_at` 时刻拍下的快照。

<a id="deferred-choices"></a>
### 推迟的取舍

以下取舍适用于[立即准入](#immediate-admission)。

| 问题 | 决定 | 理由 |
|---|---|---|
| 何时决定 IMMEDIATE | 下一个 tick，在一轮试算中 | 在 `rp.Allocate` 内运行调度轮次会持有 allocation service 写锁。 |
| experiment 的 IMMEDIATE | 拒绝，包括只有单个 trial 的 experiment | experiment 在提交调用之后才请求其 allocation，而且没有 gang 调度。 |
| 重试时的 IMMEDIATE | 由提交创建的 allocation 继承 | 只有 command、shell 和 generic 任务使用它，而且它们都没有工作负载重启。 |

<a id="deferred-acceptance-tests"></a>
### 推迟的验收测试

以下各行在负责它们的推迟 PR 构建之后适用。

| 场景 | 要求的结果 | 负责方 |
|---|---|---|
| 重试过渡期间崩溃：`.1` 结束之后、决策之前；或重试提交之后、启动之前 | 恢复执行该决策并插入 `.2`；或按 ID 重新请求 `.2`。没有 `.3`，`.2` 不会被关闭，恢复重新挂接的是 `.2` 而不是 `.1`。 | F4 |
| 跨越系统重试的轮询 | 永远不会出现 `FAILED` 之后又是 `QUEUED`。 | F4 |
| 第一次尝试结束 25 小时后仍在运行的重试 | 仍然已注册、可终止，并持有其会话 token。 | F4 |
| 升级前已在运行的 allocation 在升级后因用户代码失败 | `WORKLOAD_FAILED`，带 `init_boundary: "unknown"`，没有 `<task>.<n+1>`，其副作用只发生一次；旧的 trial 计入 `max_restarts`。 | F4 |
| 分类器覆盖 `reports_workload_start` × `workload_started_at` | 只有 (true, NULL) 得到 `WORKLOAD_INITIALIZATION_FAILED`。 | F4 |
| 工作负载启动通知发送失败；发送已提交但响应丢失；镜像中的 harness 早于 F4 | hook 和命令都不运行，类别为 `WORKLOAD_INITIALIZATION_FAILED`，预算内会重试；`WORKLOAD_FAILED`，不进行系统重试（command 和 shell 不重新运行；trial 遵循 `max_restarts`）；在 hook 之前退出，`WORKLOAD_INITIALIZATION_FAILED`。 | F4 |
| 缺失的 bind 源路径（command，以及 experiment 的 `host_path`） | 只有一个 allocation，`WORKLOAD_INITIALIZATION_FAILED`，带 `spec_rejected` 和宿主机路径，没有屏蔽节点行，trial 处于 `ERROR` 且 `restarts` 不变。GPU 预检失败仍会屏蔽节点并重试。 | F5 |
| 两个 IMMEDIATE 请求争抢最后的 slot | 恰好一个被放置；另一个以 `PLACEMENT_UNSATISFIED`（`busy`）结束；两者都永远不会读作 `QUEUED`。 | F3b |
| IMMEDIATE GENERIC 作业的排队 resume 在重启时处于 `PENDING` | 它以排队方式恢复，永远不会以 `PLACEMENT_UNSATISFIED` 结束。 | F3b |
| 在退出记录之后、清除之后以及退出决策之前崩溃 | 恢复保留重试预算。 | F4 |
| `.1` 的重复退出在 `.2` 运行之后到达 | `.2` 继续运行；没有 `.3`；预算不变。 | F4 |
| 资源 A 已发送工作负载启动通知并运行了一个有副作用的 hook；随后资源 B 预检失败 | 不进行系统重试，A 的 hook 只运行一次；类别为 `NODE_PREFLIGHT_FAILED`，trial 遵循 `max_restarts`。 | F4, F5 |
| 早于 F5 的 agent 位于接收 `accelerators` 请求的资源池中 | 这类请求永远不会被放置到它上面；`dry_run` 把它列为 `agent_upgrade_pending`。 | F5 |

<a id="deferred-open-questions"></a>
### 推迟的未决问题

1. **调度延迟。** `dry_run` 和立即准入的试算各自最多在 `rp.mu` 下增加一轮调度。应在限流之外增加一个延迟指标。
2. **两轮结果不一致。** 如果试算轮次与真实轮次的结果不一致，兜底机制会拒绝该请求，而绝不会让它排队。
3. **严格的 IMMEDIATE。** 由于 IMMEDIATE 从不抢占也不插队，它在繁忙的集群上会经常拒绝。替代方案是 `admission=queue`。
4. **重连窗口。** 把处于重连窗口内的 agent 视为已启用，需要用到它们暂存的状态（`agent.go:80-83`）。在 F3a 中验证。
5. **重试预算。** `max_system_retries` 的默认值 3 是否合适？
6. **升级窗口。** 跨越 F4 升级运行的 allocation 不会报告工作负载启动。失败时，它们分类为 `WORKLOAD_FAILED`，永远不会被系统重试。

<a id="acceptance-tests"></a>
## 验收测试

每一行都是必须达到的结果。负责方是必须证明该结果的 PR。

| 场景 | 要求的结果 | 负责方 |
|---|---|---|
| 使用相同键和内容的并发提交 | 只有一条 job 行。失败的一方触发唯一索引冲突，回滚，删除其会话和 registry 条目，并以 `replayed=true` 返回胜出方的作业。 | F2 |
| 相同的键、不同的摘要（内容、`admission` 或 `expected_digest`） | `ALREADY_EXISTS`，并指明已有的 `job_id`；不写入任何内容。 | F2 |
| 重复的 `dry_run` | 没有 job、task 或 allocation 行，没有会话、shell 密钥或 registry 条目；键仍未被占用；每次调用都返回相同的 `request_digest`。 | F2 |
| 规划漂移：规划之后 HEAD 移动，或被 include 的文件发生变化 | 固定后的 spec 仍提交规划时的 SHA。内容已变化的 spec 返回 `plan_changed`；不写入任何行，重新规划并使用新键即可成功。 | F2, M3 |
| 启动响应丢失，随后工作树发生变化，客户端重试 | `replayed=true`，返回相同的 `job_id`；没有重复。 | F2, M3 |
| master 在 `ASSIGNED` 之后、Pulling 之前崩溃 | 以 `Restore=true` 在同一个 allocation ID 下恢复；没有第二条 allocation 行，也没有新的 `StartContainer`。 | F2 |
| 重启时带有 tick 写入的资源行的 `PENDING` allocation | 在以同一 ID 重新请求之前清除这些行；之后的重启恰好恢复一个容器。 | F2 |
| 启动预写 | 只有在 agent 快照列出该容器之后才发送 `StartContainer`；如果该写入失败，则不发送任何内容。 | F2 |
| 重新挂接时发现容器状态已改变 | 容器被终止，allocation 以 `INFRASTRUCTURE_FAILED` 而不是 `NONE` 结束。 | F2 |
| 跨越两次重启的排队 command | 以同一 ID 重新请求，`start_time` 仍为 NULL；不出现 “0 container snapshots”。 | F2 |
| 在内存对象存在之前取消（事务提交之后、注册之前；或 registry 中缺失的作业） | allocation 在注册后立即被终止，作业以 `CANCELED` 结束。对于存活的作业，`KillCommand` 和 `KillShell` 永远不会返回 `NotFound`。 | F2 |
| 取消后、终止生效前崩溃 | 恢复以 `CANCELED` 结束该任务，且不重新请求该尝试。 | F2 |
| 取消与成功完成竞争 | 如果结束先提交，则为 `COMPLETED`；只有标志先提交时才为 `CANCELED`。 | F2 |
| 恢复失败 | 在一个事务中把该尝试置为 `INFRASTRUCTURE_FAILED` 并设置 `tasks.end_time`；`GetSubmission` 读到 `FAILED`。 | F2 |
| 已暂停和正在 unpause 的 generic 任务 | `PAUSED` 和 `STOPPING_PAUSED` 读作 `PAUSED`；进行中的 unpause 读作 `QUEUED`；取消已暂停的任务会以 `CANCELED` 结束它。 | F2 |
| 相对的检查点 `storage_path` | 落在继承的 `host_path` 之下；没有 `shared_fs` 默认值时，提交和 `dry_run` 都无法通过完整性检查，且不创建任何内容。 | F2, M3 |
| `admission=immediate` | `admission_unsupported`，不创建任何内容；experiment 还会在 dry run 时得到 `INVALID_ARGUMENT`。 | M3, F2 |
| 前导命令失败后的多语句命令，针对每种来源 | `a; b`、`false \|\| b`、两行命令以及 `a & b; wait` 都以前导命令的状态退出，且不运行任何用户语句，在 `sh -c` 和 `bash -lc` 下均如此。 | M3 |
| 渲染器形式 | 恰好是 `<prelude> \|\| exit $?`、一个换行符和命令；任何配置中都没有 `work_dir`；`module:Class` 被拒绝。 | M3 |
| 交付时存在恶意的 `GIT_*` 变量或镜像 git 配置 | 要么固定的树位于根目录且 `HEAD` 位于固定的 SHA，要么前导命令在任何用户语句之前失败；工作负载的环境保持不变。 | M3 |
| workdir 经由离开树的符号链接 | 前导命令在任何用户语句之前失败；树内的符号链接可以正常使用。 | M3 |
| `git` 规划检查 | 固定的 SHA；`commit_not_on_ref`；partial clone 被拒绝；`lfs_object_missing`；使用 `git` 的 shell 被拒绝。 | M3 |
| context 限制 | 计数为 99,614,718 字节的 context 可以通过；单个 99,614,719 字节的文件计为 99,614,721 字节，返回 `context_too_large`，且不发起创建调用；越出树的符号链接返回 `unsafe_symlink`；未改变的树渲染出相同的摘要；计数的大小等于 harness 对最终载荷的计数，包括符号链接。 | M3 |
| 协议门槛 | 没有 `submission_protocol` 或低于最低要求的 master 会被拒绝，无论其 release 字符串是什么。 | M2 |
| `COMMIT` 成功但 handler 看到错误，或客户端在事务提交之后断开连接；master 持续运行 | 作业无需重启即可启动，途径是 handler 的重新检查、重放或扫描；并发的重放只注册一个 allocation 和一个作业。 | F2 |
| 取消一个已暂停的 GENERIC 父任务，其 `no_pause` 子任务正在运行且其 resume 尚未完成，然后重启 | 每个成员都以 `CANCELED` 结束，resume 不会被继续，并且在任何改动之前每个成员都已通过授权。 | F2 |
| 模板在规划与启动之间发生变化 | `plan_changed`；master 默认值的改动则直接生效，不会导致 `plan_changed`。 | F2, M3 |
| 在退出记录之后、清除之后以及退出决策之前崩溃 | 记录完整，恢复保留类别。 | F1 |
| 观察工具 | `compute_resources` 只返回投影字段和 `observed_at`；`storage_check` 总是说明其视角。 | M3 |

**沿用自 PR #1。** PR #1 的四项行为必须在现在负责它们的层中重新验证：

| 行为 | 新的负责方 |
|---|---|
| GPU 不匹配时永不运行用户代码 | 在第一个版本中，GPU 型号通过选择资源池来选定；在用户代码运行之前，不检查型号或空闲显存。调度器硬约束和 `CreateContainer` 之前的 agent 预检已推迟（F5）。 |
| 失败的前导命令永不运行后续语句 | MCP 渲染器，`<prelude> \|\| exit $?`（M3） |
| 规划漂移永远不会伪装成已审阅的规划 | master 对请求、代码和模板的 `expected_digest` 检查（F2），以及规划中的 SHA 固定（M3）；master 默认值是观察到的，而不被绑定 |
| 身份检查永远不会为了方便而被绕过 | master 授权：重放会重新检查读取授权，`CancelSubmission` 根据数据库进行授权（F2） |

<a id="out-of-scope"></a>
## 不在范围内

- **把 generic 任务作为唯一类型。** 包括把各类型合并为 GENERIC、generic 重新分配以及重做 generic 锁。
- **搜索。** 搜索的 gang 调度和严格准入。
- **快照与产物。** 不提供快照或产物服务。如果将来需要，它必须把键的范围限定为 `(owner_id, snapshot_id)`，对每个引用进行授权，并在从上传到首次引用的期间持有租约。
- **汇总配额。** 按用户或按 workspace 计算的总量。平台和 MCP 限制的是每个作业，而不是用户的总量。
- **条件触发的启动。** W&B Automations 背后的适配器不在本计划之内。无论由谁提交，都使用创建 envelope 和自己的幂等键，绝不进入调度循环。
- **旧版读取。** 为 `det cmd` 和 WebUI 任务列表提供的数据库回退。
- **其他资源管理器。** Kubernetes 和 dispatcher RM（包括它们的恢复调和），以及 ROCm 和 MIG 预检。
- **混合 GPU。** 在混合 GPU 的 agent 上进行设备过滤。
- **检查点 GC。** 检查点 GC 任务的读取授权（`master/internal/api_tasks.go:65-69`）。

<a id="risks-and-open-questions"></a>
## 风险与未决问题

1. **以 `ACTIVE` 提交的 experiment。** 创建路径必须跳过 `ActivateExperiment`（`api_experiment.go:1682-1687`），并以恢复时的方式启动 experiment；恢复已经能处理 nil 快照（`restore.go:118-124`）。在 F2 中验证。
2. **Generic 取消。** 对 generic 任务调用 `CancelSubmission` 可能遇到全局变更锁（`api_generic_tasks.go:580-583`），此时返回可重试的 `UNAVAILABLE`。
3. **命令中的 secret。** 在命令行中输入的 secret 与现在一样存储在任务或 experiment 的配置中。文档必须说明这一点。
4. **旧版记录。** 升级前已结束的 allocation 显示为 `EXIT_CLASS_UNSPECIFIED`，升级前的作业没有键和摘要。F2 针对这类记录测试 `GetSubmission` 和 `ListSubmissions`。
5. **源仓库清理。** `git` 克隆从源仓库借用对象。如果曾包含某个固定提交的分支或 tag 被移动或删除，并且源仓库运行了 `git gc`，该作业之后的启动就会失败。
6. **克隆目标。** 已在 M3 中解决：`git` 代码克隆到 `/run/determined/code`，而不是可能作为任务用户 `HOME` 的 workdir（见[代码与存储](#code-and-storage)）。

<a id="troubleshooting"></a>
# 故障排查

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[首页](../README.zh.md) · [Agent 工作流](agent-workflow.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md)

<a id="the-mcp-server-does-not-start"></a>
## MCP 服务无法启动

确认客户端使用虚拟环境中可执行程序的绝对路径，且配置中的每个文件路径都是绝对路径。服务需要通过 `--profile` 传入可读的策略文件；它不使用数据库，也没有 owner 参数。

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
```

服务把启动错误写入 stderr，因为 stdout 用于传输 MCP 帧；请在 MCP 客户端的服务日志中查看准确信息。以下情况服务以状态码 2 退出：

- 收到服务不接受的参数，例如早期版本的 `--db`、`--owner`、`--repo-root` 或 `--consultation-*` 参数；请删除它（见[从早期版本升级](../README.zh.md#upgrade-from-an-earlier-release)）；
- 策略文件、存储访问文件或凭据来源无效。早期版本的策略会因 `cluster_identity`、`shell_inactivity_seconds` 或 `shared_mounts` 返回 `invalid_policy`；
- 环境变量 `DET_MASTER` 与 secrets 文件中的不同（见[身份认证失败](#authentication-fails)）；
- master 有响应，但未通过协议检查（见[master 缺少 submission protocol](#the-master-lacks-the-submission-protocol)）。

master 无法连接不会阻止启动。服务记录连接错误，存储工具照常可用，并会在第一次调用 master 前再次执行协议检查；在 master 响应之前，计算工具返回 `unavailable`。

升级后请重启每个 MCP 进程：升级前启动的进程保留旧代码、旧工具和旧数据库。

计算任务不需要 `--storage-config`。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再添加 `--storage-config /absolute/path/to/.local/storage.yaml`。

<a id="the-master-lacks-the-submission-protocol"></a>
## master 缺少 submission protocol

`protocol_unsupported` 表示 master 不支持 submission protocol 1，即本 MCP 所需的任务台账：它是上游发行版或较旧的构建。此时服务在启动时以状态码 2 退出，错误信息会给出 master 的版本。如果启动时 master 无法连接，服务仍会启动，改为在第一次调用计算工具时返回 `protocol_unsupported`。master 对 submission 路由返回 `Unimplemented` 时也会返回该错误。

版本字符串不能作为依据，因为本地构建报告上一个标签，候选版本报告下一个版本。请从无需登录的 `GET /api/v1/master` 读取 `submissionProtocol`：

```bash
curl -s https://determined.example.org/api/v1/master | python3 -c 'import json, sys; print(json.load(sys.stdin).get("submissionProtocol"))'
```

请把 master 升级为带有任务台账的 Determined fork 构建。不会回退到旧 API；在此期间存储工具照常可用。

<a id="authentication-fails"></a>
## 身份认证失败

确认 API 地址和凭据属于同一个 Determined 部署。Secrets 文件可以使用 `DET_API_TOKEN`，也可以同时使用 `DET_USERNAME` 和 `DET_PASSWORD`：

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

secrets 文件指定了 `DET_MASTER` 时，其凭据只发往该 master：环境中的 `DET_API_TOKEN`、`DET_USERNAME` 和 `DET_PASSWORD` 会被忽略；如果环境变量 `DET_MASTER` 指向另一个 master，服务停止启动。请删除其中之一，或用 `--api-url` 明确选择。secrets 文件没有 `DET_MASTER` 时，master 来自环境，且环境中的 token 优先于文件中的。

不要把凭据写进策略、TaskSpec、任务名称或命令。限制 secrets 文件的访问权限，只检查所需变量名是否存在，不查看变量值。每个任务的所有者都是已认证的账户。

<a id="tls-certificate-verification-fails"></a>
## TLS 证书验证失败

使用该部署发布的 CA 证书。如果它已安装在 MCP 进程所用的操作系统或 Python 运行时信任库中，则不需要额外设置。应用需要单独的 PEM 证书包时，把 Requests 的 `REQUESTS_CA_BUNDLE` 设为其绝对路径，并启用验证：

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

对于 MCP，在服务参数中保留 `--verify-ssl`，并通过 stdio 客户端的环境配置传入 CA 变量。外层字段请按客户端的 MCP 语法调整：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

图形界面应用可能不会继承终端中导出的变量。请在客户端的 MCP 环境设置中配置该变量，或从包含该变量的环境启动客户端，然后重启 MCP 服务。MCP 进程必须能读取 CA 证书包。

未知签发者需要通过正确的 CA 链解决。证书过期或主机名不匹配必须由部署运维方修正；关闭验证并不能修复证书身份。

<a id="a-spec-or-argument-is-refused"></a>
## spec 或参数被拒绝

未通过校验的 spec 或参数返回 `invalid_request`，`details.errors` 中每项都给出位置（例如 `spec.experiment`）和原因。常见原因：shell 带有命令、`output_dir`、`workdir` 或 `git` 代码；command 或 experiment 缺少 `output_dir`；实验设置了 MCP 负责渲染的字段（`entrypoint`、`resources.slots_per_trial`、`environment.image` 等）、`bind_mounts` 或 `checkpoint_storage` 的 `host_path`；`storage_path` 没有配套 `type: shared_fs`；搜索未设置 `searcher.max_concurrent_trials`；或使用旧式 `module:Class` 命令。`admission: immediate` 返回 `admission_unsupported`：本版本让每个任务排队，且提交前不评估放置，因此请使用默认值 `queue`。以上情况都不会创建任何内容。

其余实验配置由 master 在规划的 dry run 中校验，问题以 `invalid_request` 报告，并附 master 自己的信息。

<a id="a-shared-path-is-rejected-or-missing"></a>
## 共享路径被拒绝或不存在

确认参数属于哪个路径空间。`output_dir`、`git` 的 `code.repo`、`path` 的 `code.dir`、`storage_check.path` 以及传输的 `shared_dir` 使用策略中的容器路径。`mounts[].host_path` 是计算节点路径。`context` 的 `code.repo` 和 `local_dir` 是运行 MCP server 的机器上的绝对路径。

`path_not_mounted` 表示没有策略挂载包含该路径；`read_only_storage` 表示路径位于 `read_only: true` 的挂载下，允许读取和取回，但不能作为 `output_dir` 或同步目标。规划警告 `path_not_bind_mounted` 表示 master 生效的 bind mount 不包含该路径，任务无法访问它；请联系管理员。本地映射、SSH 主机密钥、认证和 rsync 要求见[共享存储访问](shared-storage-access.zh.md)。

<a id="git-code-cannot-be-planned"></a>
## 无法规划 git 代码

`storage_not_local` 表示在运行 MCP server 的机器上，无法通过本地挂载读取仓库根目录。本版本只通过本地挂载规划 `git` 代码，不支持 SSH：请在存储访问文件的 `local_mounts` 中映射该根目录（`mode: auto` 或 `local`），或把它挂载在与宿主机相同的路径上，或改用 `context` 发送代码。

`commit_not_on_ref` 表示固定的提交不在任何分支或标签上；请推送或打标签，因为任务中的克隆借用仓库的对象，而 `git gc` 会清理不可达对象。`git_too_old` 会给出找到的 git 版本和所需版本（2.32）。部分克隆、linked worktree 和使用 alternates 的仓库会被拒绝，因为任务无法解析它们的对象。`lfs_object_missing` 表示仓库中缺少某个 LFS 对象；请先获取它。

<a id="ssh-storage-access-fails"></a>
## SSH 存储访问失败

使用能访问已配置计算节点宿主机路径的登录节点 `Host` 别名。网关属于 `ProxyJump`，不是存储端点。在自动化之前完成首次交互式连接并验证主机密钥。

使用 `auth: openssh` 时，服务继承已有的 agent。stdio MCP 进程必须继承可用的 `SSH_AUTH_SOCK`；较早启动的图形界面客户端可能没有它。使用密码或 keyring 认证时，按照[共享存储访问](shared-storage-access.zh.md#authentication-choices)中的凭据放置规则操作。

修正 SSH、路径或权限错误后，务必重新执行 dry run。未审核新的解析端点和逐项变更之前，不要把失败的预览直接改为实际传输。

<a id="a-job-stays-queued"></a>
## 任务一直排队

提交前不评估放置，每个任务都进入队列。资源池没有空闲 slots 时任务会等待；规划中的 `current_slots_exceeded` 警告请求所需 slots 超过集群当前容量。调用 `compute_resources(pool)` 查看资源池的 slots 和设备型号，调用 `compute_status(job_id)` 查看说明。trial 仍在等待的活动实验显示为 `running`，其说明会写明它在等待调度器。

更换资源池或 slots 数是工作负载层面的决定：只有用户同意时，才取消排队的任务并规划新任务。`compute_resources` 未列出的资源池，要么 master 不认识，要么当前账户不可见。

<a id="a-launch-outcome-is-uncertain"></a>
## 提交结果不确定

master 以 `request_id` 保存每个任务，因此用相同的 `spec`、`request_id` 和 `request_digest` 重复提交总是安全的：

- 响应丢失、超时、`unavailable` 或 `invalid_response`：重复提交。如果任务已创建，会返回该任务且 `replayed: true`；即使规划后工作树、策略或 workspace 已变化也是如此。
- `internal`：再重复一次。仍是同样错误说明没有创建任何内容；修正请求后重新规划。
- 渲染错误在 details 中给出 `request_id` 并说明结果未知：说明无法询问 master。重新规划前，先在 `compute_list` 中查找该 `request_id`。

结果不确定时，不要为新的 `request_id` 重新规划。`compute_list` 会列出该账户在所有客户端提交的每个任务及其 `request_id`。

`plan_changed` 和 `key_conflict` 并非结果不确定：要么没有创建任何内容，要么错误中给出了该任务；见下一节。

<a id="a-launch-returns-plan_changed-or-key_conflict"></a>
## 提交返回 plan_changed 或 key_conflict

`plan_changed` 表示提交渲染出的请求与规划 dry run 的请求不同，因此 master 没有创建任何内容，该 `request_id` 仍未被占用。常见原因：

- 提交时传入的是原始 spec 而不是解析后的 spec，且其分支在规划后发生了移动。`compute_plan` 返回的 `spec` 固定了提交和策略默认值；提交时始终使用它。
- 对 `context`，某个 `include` 路径在工作树中发生了变化，或工作树在 dirty 与 clean 之间切换，这一状态记录在 `.code-provenance.json` 中。
- 对 command 或 shell，spec 中指定的 workspace 现在解析到了另一个 workspace。

错误的 `details` 给出提交渲染出的 `commit` 和 `content_digest`。审核后重新调用 `compute_plan`，并用新规划的 `spec`、`request_id` 和 `request_digest` 提交。master 或资源池默认值的变化不会导致 `plan_changed`：它们按提交时的值生效。

`key_conflict` 表示该 `request_id` 已绑定到请求不同的任务，见 `details.job_id`；当 `request_id` 与另一次规划的 spec 或摘要一起重用时会发生。用 `compute_status` 读取该任务：它可能正是之前已提交的目标任务。否则重新规划以生成新的 `request_id`；绝不要手动修改 `request_id`。

<a id="a-job-ended-but-the-result-is-unclear"></a>
## 任务已结束但结果不明确

使用 `compute_status(job_id)` 和 `compute_logs(job_id)`。任务的 `exit_class` 说明结束原因：`workload_failed` 表示工作负载出错，包括 prelude 失败，此时日志中有一行以 `compute:` 开头的信息（例如 `compute: git code delivery failed: ...` 或 `compute: the workdir resolves to ..., outside the code root`）；`workload_initialization_failed` 表示容器在工作负载启动前失败，例如拉取镜像；`infrastructure_failed` 表示 agent 或其连接丢失。没有退出类别的任务是在台账之前提交的，或是 trial 从未启动就被取消的实验。

仅凭结束状态不能证明成功。检查提交前定义的成功判据；配置了存储访问时，用 `storage_check` 验证预期的共享产物，需要本地副本时先预览再执行 `storage_fetch`。要了解任务是否用到了 CPU、内存或 GPU，调用 `compute_usage(job_id)`；对已结束的任务，窗口在任务或其最后一个 allocation 结束时结束。

<a id="usage-measurements-are-unavailable-or-empty"></a>
## 用量测量不可用或为空

`measurement: "unmeasured"` 表示 master 没有 task-resources 集成，需要管理员配置 `integrations.task_resources`；这不能说明任务空闲。`unavailable` 表示测量后端繁忙或无法访问；请稍后重试。窗口返回 `invalid_request` 可能是时钟偏差：master 会拒绝比自身时钟超前 60 秒以上的窗口结束时间。

`task_not_started` 表示任务或所选 trial 还没有 task。`not_found` 表示该 trial 或 allocation 属于其他任务；请从 `compute_status` 的 `allocations` 中选择。allocation 返回 `invalid_request` 表示它属于 `trial_id` 以外的 trial；省略 `trial_id` 即可选择该 allocation 所属的 trial。

`context_unavailable` 非空并不致命：它列出失败的尽力查询（`trial`、`resource_pool` 或 `gpu_models`），返回的测量值仍然有效。`series` 为空表示该窗口没有数据：对照 `window` 及其 `anchor` 与各 allocation，或使用更长的 `window_seconds`。如果 `samples_omitted` 为 true，请缩小窗口、减少指标或只选一个 allocation。工作负载不通过 Determined Core API 报告进度时，`total_batches_processed` 为 0 属于正常。字段含义见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

<a id="cancellation-is-rejected"></a>
## 取消请求被拒绝

在 basic 授权下，只有任务所有者或管理员可以取消任务；对其他账户的任务，`compute_cancel` 返回 `permission_denied` 或 `not_found`。请使用提交该任务的账户，或请管理员取消。`cancel: "recorded"` 不是失败：master 已记录取消，任务很快结束；`compute_status` 会显示何时结束。

<a id="a-transfer-is-partial-or-different-from-the-preview"></a>
## 传输不完整或与预览不同

传输从不加入 `--delete`，因此目标中无关的文件会保留。未设置 `overwrite` 时，已有文件以及已有目录的属性保持不变；设置 `overwrite`（需策略允许）时，同名文件会被替换。实际执行失败的传输可能留下部分目标内容；rsync 退出码 23 表示部分文件或属性未能传输。

`overwrite_not_allowed` 表示传入了 `overwrite=true`，但策略中的 `allow_overwrite` 为 `false`；没有传输任何内容。早期版本默认替换同名文件；现在除非设置了 `overwrite` 且策略允许，目标中已有的文件都会保留。请写入新的运行目录，或请管理员允许覆盖。

检查长度受限的传输输出，修正文件系统或配置问题，重新执行 dry run 并审核后再执行。不要自动改用其他权限保留参数重试。详细规则见[共享存储访问](shared-storage-access.zh.md#check-preview-and-transfer)。

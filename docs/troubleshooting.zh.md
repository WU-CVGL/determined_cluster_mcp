<a id="troubleshooting"></a>
# 故障排查

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[首页](../README.zh.md) · [Agent 工作流](agent-workflow.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md)

<a id="the-mcp-server-does-not-start"></a>
## MCP 服务无法启动

确认客户端使用虚拟环境中可执行程序的绝对路径，且配置中的每个文件路径都是绝对路径。MCP 进程需要可读的计算 profile、本地持久数据库路径和非空 owner。服务会自动创建数据库父目录，但进程必须有写入权限。

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute --profile /absolute/path/to/profile.yaml plan --request-file /absolute/path/to/request.json
```

服务使用 stdout 传输 MCP 协议帧，启动错误写入 stderr。请在 MCP 客户端的服务日志中查看准确错误。更改安装或升级服务后，重启共享该数据库的所有 MCP 进程，使其加载相同的工具和数据库结构。profile 和存储配置文件会拒绝未知字段，因此升级引入的新字段（例如 `snapshots`）只能在读取该文件的所有进程都运行新版本后再添加。

计算任务不需要 `--storage-config`。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再添加 `--storage-config /absolute/path/to/.local/storage.yaml`。

<a id="authentication-fails"></a>
## 身份认证失败

确认 API 地址和凭据属于同一个 Determined 部署。Secrets 文件可以使用 `DET_API_TOKEN`，也可以同时使用 `DET_USERNAME` 和 `DET_PASSWORD`：

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

不要把凭据放进计算 profile、任务请求、owner、任务名称或描述。限制 secrets 文件的访问权限；排查时只检查必需变量名是否存在，不要读取其值。

配置的 `owner` 不会选择 Determined 用户，它只是本地 SQLite 数据库中的命名空间。远端权限来自 API 凭据。因此更换 owner 无法修复 API 权限错误，共享 owner 也不代表共享远端权限。

<a id="tls-certificate-verification-fails"></a>
## TLS 证书验证失败

使用该部署发布的 CA 证书。如果它已经安装在 MCP 进程使用的操作系统或 Python 运行时信任库中，则不需要额外设置 CA。应用需要独立 PEM bundle 时，将 Requests 的 `REQUESTS_CA_BUNDLE` 设置为其绝对路径，并启用验证：

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

使用 MCP 时，在服务参数中保留 `--verify-ssl`，并通过 stdio 客户端的环境配置传入 CA 变量。外围字段需按客户端的 MCP 语法调整：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--db", "/absolute/local/path/to/tasks.sqlite3", "--owner", "your-owner", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

GUI 应用可能不会继承终端中导出的变量。应在客户端的 MCP 环境设置中配置该变量，或从包含该变量的环境启动客户端，然后重启 MCP 服务。MCP 进程必须能够读取 CA bundle。

正确的 CA 链可以解决未知签发者问题。证书过期或主机名不匹配必须由部署运维方修正；关闭验证不能修复证书身份。

<a id="a-shared-path-is-rejected-or-missing"></a>
## 共享路径被拒绝或不存在

先确认参数要求哪一种路径空间。任务的 `workdir` 和 `output_dir`、`storage_check.path`，以及传输的 `shared_dir` 一侧，都使用计算 profile 中的容器路径。`mounts[].host_path` 是集群计算节点路径。`local_dir` 是运行 MCP 服务的机器上的绝对路径。

规划会检查配置的路径边界。CLI 和 MCP 服务还会为 bind mount、工作目录、experiment 检查点目录和输出目录报告 `path_checks`；参见[启动路径检查](compute-service.zh.md#launch-path-checks)。`path_not_found` 表示通过可信本地视图（`local_mounts` 条目，或在 MCP 所在机器上已挂载的配置主机根目录）看到某个必需路径不存在或不是目录，`details.missing_paths` 会列出该路径。如果该路径在集群上存在，请确认本地挂载或 `local_mounts` 条目显示的是集群的文件系统。请创建它、修正请求，或对输出目录和 experiment 检查点目录使用 `create_directories`。否则，缺失的 experiment 检查点 `host_path` 会让任务在容器启动前失败，因为 Determined 在启动时 bind mount 该路径。

`unverified` 状态永远不会导致失败，只表示客户端无法判定。请依据 `status` 判断；`reason` 的取值是开放的。常见原因有：`not_locally_visible`（MCP 所在机器没有挂载该主机根目录；若挂载在其他位置，请添加 `local_mounts` 条目）、`local_mount_unavailable`（覆盖该路径的 `local_mounts` 条目不存在或不可读；请重新挂载或修正该条目）、`local_view_unconfirmed`（本机存在同名主机根目录，但它不是挂载点；如果它确实是集群的文件系统，例如位于网络挂载之下的目录，请用 `local_mounts` 把它映射到自身）、`ssh_only_access`、`permission_denied`、`timeout`（文件系统未在 10 秒内响应）、`storage_config_unavailable`（无法加载存储配置；请运行 `storage_check` 或修正该文件）、`invalid_storage_path`（本地视图经由符号链接解析到映射根目录之外），以及 `os_error:<ERRNO>`，例如 `os_error:ESTALE`。此时 `create_directories` 需要可信本地视图或 SSH 访问，否则 launch 会在认领任务记录之前返回 `configuration_required`。launch 返回 `storage_timeout` 表示待创建的目录或其创建过程未在 10 秒内响应；此时没有写入记录，也没有提交任务，文件系统恢复响应后请用同一 `request_id` 重试。如果共享根目录本身不可写，但其子目录可写，请用 `local_mounts` 映射该子目录。

MCP 服务具有已配置的本地或 SSH 访问方式时，可以使用 `storage_check`。如果任务文件已经位于共享存储，而且不需要从客户端检查或传输，计算操作可以不配置存储访问。

标记为 `read_only: true` 的挂载允许读取和取回，但会拒绝其下的工作目录、输出目录、检查点目标或同步目标。本地映射、SSH 主机密钥、认证和 rsync 要求见[共享存储访问](shared-storage-access.zh.md)。

<a id="ssh-storage-access-fails"></a>
## SSH 存储访问失败

使用能够访问已配置集群计算节点宿主机路径的登录节点 `Host` 别名。网关应配置在 `ProxyJump` 中，而不是作为存储端点。自动化前先完成首次交互连接并核对主机密钥。

使用 `auth: openssh` 时，服务只继承已有 agent。stdio MCP 进程必须继承可用的 `SSH_AUTH_SOCK`；更早启动的 GUI 客户端可能没有该变量。使用密码或 keyring 认证时，遵循[共享存储访问](shared-storage-access.zh.md)中的凭据放置规则。

修正 SSH、路径或权限错误后，始终重新执行 dry run。不要在未审核新的解析端点和逐项变更时，直接把失败的预览改为实际传输。

<a id="gpu-admission-failed-exit-code-86"></a>
## GPU 准入失败（退出码 86）

带 `gpu_admission` 的任务在 `nvidia-smi` 于容器内报告的 GPU 不满足策略时，会在工作负载启动前以退出码 86 结束。工作负载自身也可能以 86 退出，因此退出码 86 本身只是一个提示：准入失败要由一行 `determined-compute gpu_admission: failed: ...` 日志，或 `determined.allocation_id` 与该任务 allocation 相符的 `.jsonl` 记录确认（`compute_usage` 列出每个 allocation 的 `allocation_id` 和 `exit_reason`）。`output_dir` 中有回执（默认 `gpu-admission.json`），其中包含 `devices`、`unparsed_lines` 和 `failures`，每次尝试还会在 `.jsonl` 中追加一行。回执只保存共享同一 `output_dir` 的任意 trial 或任务最近写入的记录，因此相符的 `.jsonl` 记录才是权威记录。常见原因包括 GPU 数量与 `count` 不同、GPU 名称或驱动不在允许的模式内、其他进程占用设备导致空闲显存低于 `min_free_mib`，或镜像中没有可用的 `nvidia-smi`。检查不应用 `CUDA_VISIBLE_DEVICES`，因此数量不符也可能表示资源管理器只通过该变量限制 GPU。请报告相符的记录；更改策略、资源池或槽位数需要明确的任务决策。在 experiment 中，每次准入失败都会消耗一次重启，因此若希望第一次失败即停止，请设置 `max_restarts: 0`。参见 [GPU 准入](compute-service.zh.md#gpu-admission)。

<a id="capacity-is-unavailable-or-unknown"></a>
## 容量不足或无法确定

针对所需资源池和槽位数调用 `compute_resources`。零槽位检查辅助容器容量，而不是空闲 GPU。容量结果只是快照；新任务提交前服务会再次检查。

当 `allow_queue: false` 时，容量不足或无法确定会拒绝提交，而不是进入队列。不要擅自选择建议的其他资源池、减少资源或设置 `allow_queue: true`；这些变化需要明确的任务决策。认证或资源清单结构错误属于错误，不能当作容量存在的证据。

管理员动态创建的资源池只有在进入 Ready 状态后才会出现在 `compute_resources` 中。处于 Pending 或 Failed 状态的资源池不会出现：结果会说明该资源池不在集群资源清单中且可用性未知，不排队的提交会以 `capacity_unknown` 被拒绝。本 MCP 不提供动态资源池管理 API，请向管理员确认该资源池的状态。

<a id="submission-outcome-is-uncertain"></a>
## 提交结果不确定

修改请求发出后的连接故障可能表示远端已经接受任务，但客户端没有收到 ID。服务会记录 `submission_uncertain`，且不会自动重试。

保留原本的本地 `task_id`、请求和 `request_id`。不要使用新的 request ID 再次提交。检查该 Determined 账户下的对应远端任务，然后仅对同一条状态不确定的本地提交调用 `compute_reconcile(task_id, remote_id)`。Reconcile 会先验证保留的提交标记，再绑定记录；标记不符会被拒绝。

对于独立创建的远端任务使用 `compute_adopt`。Adopt 不能作为状态不确定的本地提交的绕过手段。具体边界见[发现并登记](agent-workflow.zh.md#discover-and-adopt-existing-remote-tasks)。

<a id="a-task-is-terminal-but-the-result-is-unclear"></a>
## 任务已终止但结果不明确

使用本地 task ID 调用 `compute_status` 和 `compute_logs`。API 提交成功、获得远端 ID 或任务进入终态，本身都不能证明工作负载成功。检查退出信息和提交前定义的成功判据。已经配置存储访问时，用 `storage_check` 验证预期共享产物；需要本地副本时，先预览再执行 `storage_fetch`。没有存储访问时，使用任务输出或另一项明确的任务内检查。要确认任务结束前是否真正使用了 CPU、内存或 GPU，调用 `compute_usage(task_id)`；对于已结束的任务或已暂停的 trial，窗口终点是任务或其最后一个 allocation 的结束时间。

Experiment 在 trial 启动前可能没有 trial 日志。Shell 仍然可用时，可以使用经过清理的重连命令。报告可以包含任务 ID、状态、经过清理的命令、路径和错误，但不得包含凭据值或 secrets 文件内容。

<a id="usage-measurements-are-unavailable-or-empty"></a>
## 用量测量不可用或为空

`compute_usage` 依赖 master 的任务资源 API。`task_resources_disabled` 表示 master 具备该 API，但管理员尚未启用 `integrations.task_resources`。`task_resources_unsupported` 表示 master 缺少该 API，需要 research-cluster fork 0.40.1 或更高版本的 Determined master。两者都不可重试，应联系管理员。无法取得测量值不能作为任务空闲的证据。

HTTP 503 表示测量后端繁忙或不可用；每个 master 同时最多运行四个资源查询，因此应稍后重试。HTTP 400 可能表示时钟偏差：master 会拒绝比其自身时钟超前 60 秒以上的窗口终点，应校正运行 MCP 服务的机器的时钟。HTTP 404 表示 Determined task 或指定的 trial ID 不存在，或当前账户无权访问。

`task_not_started` 表示 experiment 尚无 trial，或其 trial 尚无 Determined task；应等待任务启动。`trial_not_found` 表示请求的 trial 不属于该 experiment。`allocation_not_found` 表示所选 task 未列出该 allocation；应从返回的 `allocations` 中选择。`remote_id_unknown` 表示本地提交尚未绑定，必须先执行 reconcile。

`context_unavailable` 非空并不致命。它列出因 Determined API 错误而失败的上下文查询（`resource_pool`、`allocation_details` 或 `gpu_models`）；相关字段为空或 `null`，但返回的测量值仍然有效。需要这些上下文时，可稍后重试。出现传输失败后会跳过其余查询，因此可能同时列出多个名称。对于已结束的 command 或 shell，`resource_pool` 为 `null` 而 `context_unavailable` 中没有相应条目，表示 Determined 已不再提供该任务的实体，重试也无济于事。`gpu_model` 为 `null` 而 `context_unavailable` 中没有 `gpu_models` 时，可能是 RBAC 对当前账户隐藏了设备 UUID，因而无法匹配型号名称。工作负载不通过 Determined 的 Core API 报告进度（例如普通 bash 入口）或尚未报告时，trial 的 `total_batches_processed` 为 0 属于预期；这不能说明工作负载没有任何进展，应改为根据日志、实测用量和预期产物判断进展。

空的 `series` 列表表示该窗口没有数据，而不是任务空闲：任务可能未在该窗口内运行，或监控系统没有保留其数据。将窗口及其 `anchor` 与 `task_start_time` 和 `allocations` 对照，或使用更长的 `window_seconds`。如果 `samples_omitted` 为 true，应缩短窗口、减少指标或选择一个 allocation。agent 断开期间，任务可能仍保持 `RUNNING` 最多约 150 秒，因为该 fork 默认会等待 agent 重连这么久（`agent_reconnect_wait`）；因此仅凭 `RUNNING` 状态不能证明任务在推进。字段含义和限制见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

<a id="cancellation-is-rejected"></a>
## 取消请求被拒绝

在使用 basic authorization 的 Determined fork 0.40.1 或更高版本上，只有任务的 Determined 所有者或管理员可以终止或取消任务。对于其他账户拥有的任务，`compute_cancel` 对 command 或 shell 返回 HTTP 403，对 experiment 返回 HTTP 404 `experiment '<id>' not found`。这通常发生在更换凭据之后：submitted 记录绑定配置和端点而不是账户，因此服务仍会发出请求。应恢复拥有该任务的账户凭据，或请管理员取消任务。已登记（adopted）的记录会在发出任何取消请求之前返回 `ownership_mismatch`。

<a id="a-task-reports-binding_mismatch"></a>
## 任务返回 binding_mismatch

submitted 记录绑定提交它的计算配置和端点。如果只有配置改变，`compute_cancel` 或 `compute_reconcile` 返回的 `binding_mismatch` 会说明状态、日志和用量仍可只读查询；请使用任务原来的配置取消或调和。如果端点或 `cluster_identity` 标签不同，所有操作都会被拒绝，因为该记录可能描述的是另一个 master 上的任务。`compute_list_tasks` 显示每条记录的离线 `binding`（`profile`、`cross_profile`、`mismatch`、`unknown` 或 `adopted`）。跨配置读取返回 `identity_mismatch` 或 `ownership_mismatch` 时，表示找到的远端任务的提交标记或所有者与记录不符；不要把它当作同一个任务。跨配置读取返回 `cross_profile_unverifiable` 时，表示任务实体的读取收到 HTTP 404，因此无法验证所有者和提交标记：Determined 会在已结束的 command 或 shell 结束 24 小时后、以及 master 重启时丢弃其实体。该错误不可重试，也不表示任务日志已经丢失；请使用任务原来的配置读取其日志和用量。参见[跨配置只读观察](compute-service.zh.md#cross-profile-observation)。

<a id="a-transfer-is-partial-or-different-from-the-preview"></a>
## 传输不完整或与预览不同

传输不会添加 `--delete`，因此目标中无关的文件会保留。正常 rsync 行为仍可能覆盖同名文件。执行中的失败可能留下不完整目标；rsync 退出码 23 明确表示部分文件或属性未能传输。

检查长度受限的传输输出，修正文件系统或配置问题，重新执行 dry run 并审核后再执行。不要自动更改权限保留参数后重试。详细规则见[共享存储访问](shared-storage-access.zh.md)。

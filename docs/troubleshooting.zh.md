<a id="troubleshooting"></a>
# 故障排查

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[首页](../README.zh.md) · [Agent 工作流](agent-workflow.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md)

<a id="the-mcp-server-does-not-start"></a>
## MCP 服务无法启动

确认客户端使用虚拟环境中可执行程序的绝对路径，且配置中的每个文件路径都是绝对路径。MCP 进程需要可读的计算 profile。它拒绝 `--db` 和 `--owner`，配置加载也拒绝 `cluster_identity`；为带任务数据库的版本编写的配置需删除它们（见[升级](compute-service.zh.md#upgrading-from-a-version-with-a-task-database)）。

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
```

客户端列出工具后，用一个请求调用 `compute_plan` 检查 profile 和路径；它不需要访问集群。服务使用 stdout 传输 MCP 协议帧，启动错误写入 stderr。请在 MCP 客户端的服务日志中查看准确错误。更改安装或升级服务后，重启所有 MCP 进程，使其加载当前的工具。

计算任务不需要 `--storage-config`。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再添加 `--storage-config /absolute/path/to/.local/storage.yaml`。

<a id="authentication-fails"></a>
## 身份认证失败

确认 API 地址和凭据属于同一个 Determined 部署。Secrets 文件可以使用 `DET_API_TOKEN`，也可以同时使用 `DET_USERNAME` 和 `DET_PASSWORD`：

```dotenv
DET_MASTER=http://determined.example.org:8080
DET_API_TOKEN=replace-with-your-token
```

Secrets 文件设置了 `DET_MASTER` 时，其凭据只发送给该 master。若报错说明 `--api-url` 或 `DET_MASTER` 指向的 master 与 secrets 文件不同，表示有覆盖项指向了别处：取消该覆盖项，或改用属于该 master 的 secrets 文件。

不要把凭据放进计算 profile、任务请求、任务名称或描述。限制 secrets 文件的访问权限；排查时只检查必需变量名是否存在，不要读取其值。

凭据选定 Determined 账户，MCP 只操作该账户拥有的任务。因此更换凭据会改变 `compute_list` 显示的任务，以及其他工具接受的任务。

<a id="tls-certificate-verification-fails"></a>
## TLS 证书验证失败

使用该部署发布的 CA 证书。如果它已经安装在 MCP 进程使用的操作系统或 Python 运行时信任库中，则不需要额外设置 CA。应用需要独立 PEM bundle 时，将 Requests 的 `REQUESTS_CA_BUNDLE` 设置为其绝对路径，并启用验证：

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

使用 MCP 时，在服务参数中加入 `--verify-ssl`，并通过 stdio 客户端的环境配置传入 CA 变量。外围字段需按客户端的 MCP 语法调整：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

GUI 应用可能不会继承终端中导出的变量。应在客户端的 MCP 环境设置中配置该变量，或从包含该变量的环境启动客户端，然后重启 MCP 服务。MCP 进程必须能够读取 CA bundle。

正确的 CA 链可以解决未知签发者问题。证书过期或主机名不匹配必须由部署运维方修正；关闭验证不能修复证书身份。

Python 3.13 及以上版本以 OpenSSL 的严格 X.509 模式验证。此时私有 CA 证书需要标记为 critical 且为 `CA:TRUE` 的 `basicConstraints`、包含 `keyCertSign` 的 `keyUsage` 和 Subject Key Identifier，服务端证书需要 Authority Key Identifier。否则验证失败，错误消息会给出原因，例如 `Missing Authority Key Identifier`、`CA cert does not include key usage extension` 或 `Basic Constraints of CA cert not marked critical`，即使 curl 和较旧的 Python 版本接受该证书链。以严格模式检查证书链：

```bash
openssl verify -x509_strict -CAfile ca.pem server.pem
```

解决方法是重新签发带有这些扩展的证书；MCP 始终保持严格验证。TLS 连接失败（例如 `WRONG_VERSION_NUMBER`）通常表示 `DET_MASTER` 指向纯 HTTP 端口，例如 master 的 `:8080`；请使用部署公布的 HTTPS 地址。经代理访问 HTTPS master 时，不带验证原因的 TLS 连接失败（例如 `UNEXPECTED_EOF_WHILE_READING`）通常表示代理无法访问 master；见[通过代理无法访问 master](#the-master-is-unreachable-through-a-proxy)。

<a id="the-master-is-unreachable-through-a-proxy"></a>
## 通过代理无法访问 master

Requests 从 MCP 进程环境读取 `HTTPS_PROXY`、`HTTP_PROXY` 和 `NO_PROXY` 或其优先生效的小写形式，该环境来自 MCP 客户端；secrets 文件中的代理变量不会生效。错误消息提示无法连接代理时，表示 MCP 连不上这些变量指定的代理：修正或删除这些变量，或按下文让对 master 的请求绕过代理。代理无法访问 master 时，请求失败，错误消息提示代理拒绝或无法访问 master，或者对 HTTPS master 提示与 master 的 TLS 连接失败；其他工具可能报告 TLS connect error。如果只能经代理访问 master，请检查代理及其凭据。如果不经代理即可访问 master，把其主机名或域名后缀（例如 `.example.org`）同时加入客户端 `env` 中的 `NO_PROXY` 和 `no_proxy`，然后重启 MCP 服务；示例见[可选 HTTPS](compute-service.zh.md#optional-https)。代理代替 master 应答的情况见[提交结果不确定](#submission-outcome-is-uncertain)。

<a id="a-shared-path-is-rejected-or-missing"></a>
## 共享路径被拒绝或不存在

先确认参数要求哪一种路径空间。任务的 `workdir` 和 `output_dir`、`storage_check.path`，以及传输的 `shared_dir` 一侧，都使用计算 profile 中的容器路径。`mounts[].host_path` 是集群计算节点路径。`local_dir` 是运行 MCP 服务的机器上的绝对路径。

规划会检查配置的路径边界，但不会查询远端文件或权限。MCP 服务具有已配置的本地或 SSH 访问方式时，可以使用 `storage_check`。如果任务文件已经位于共享存储，而且不需要从客户端检查或传输，计算操作可以不配置存储访问。

标记为 `read_only: true` 的挂载允许读取和取回，但会拒绝其下的工作目录、输出目录、检查点目标或同步目标。本地映射、SSH 主机密钥、认证和 rsync 要求见[共享存储访问](shared-storage-access.zh.md)。

<a id="ssh-storage-access-fails"></a>
## SSH 存储访问失败

使用能够访问已配置集群计算节点宿主机路径的登录节点 `Host` 别名。网关应配置在 `ProxyJump` 中，而不是作为存储端点。自动化前先完成首次交互连接并核对主机密钥。

使用 `auth: openssh` 时，服务只继承已有 agent。stdio MCP 进程必须继承可用的 `SSH_AUTH_SOCK`；更早启动的 GUI 客户端可能没有该变量。使用密码或 keyring 认证时，遵循[共享存储访问](shared-storage-access.zh.md)中的凭据放置规则。

修正 SSH、路径或权限错误后，始终重新执行 dry run。不要在未审核新的解析端点和逐项变更时，直接把失败的预览改为实际传输。

<a id="capacity-is-unavailable-or-unknown"></a>
## 容量不足或无法确定

针对所需资源池和槽位数调用 `compute_resources`。零槽位检查辅助容器容量，而不是空闲 GPU。容量结果只是快照；新任务提交前服务会再次检查。

当 `allow_queue: false` 时，容量不足或无法确定会拒绝提交，而不是进入队列。不要擅自选择建议的其他资源池、减少资源或设置 `allow_queue: true`；这些变化需要明确的任务决策。认证或资源清单结构错误属于错误，不能当作容量存在的证据。

管理员动态创建的资源池只有在进入 Ready 状态后才会出现在 `compute_resources` 中。处于 Pending 或 Failed 状态的资源池不会出现：结果会说明该资源池不在集群资源清单中且可用性未知，不排队的提交会以 `capacity_unknown` 被拒绝。本 MCP 不提供动态资源池管理 API，请向管理员确认该资源池的状态。

<a id="submission-outcome-is-uncertain"></a>
## 提交结果不确定

提交请求发出后连接中断或超时、返回 HTTP 5xx、返回重定向（HTTP 3xx，任何变更请求都不会跟随），或响应中没有任务 ID，都可能表示 Determined 已经创建了任务，只是客户端没有收到 ID。此时 `compute_launch` 返回 `submission_uncertain`，错误 details 中包含 `kind` 和 `submission_marker`，且不会重试。在任何连接建立之前发生的失败（例如连接被拒绝或域名解析失败）则是可重试的 `transport_error`：请求没有发出。

错误 details 带有 `source: "proxy"` 时，作出应答的是本机与 master 之间的 HTTP 代理，而不是 Determined。master 很可能无法访问：检查 API 地址、master 是否在运行，以及其地址是否应加入 `NO_PROXY`。以这种方式得到应答的提交或其他修改仍属于未确认，因为代理可能在转发请求之后才失败。如果代理在 `Proxy-Status` 头中表明它从未连上 master（例如 `error=dns_error` 或 `error=connection_refused`），则返回带有 `proxy_error` 的可重试 `transport_error`，因为请求没有到达 master。代理应答的识别方式见[未确认的提交](compute-service.zh.md#unconfirmed-launches)。

不要自动再次提交。使用较小的 `limit` 调用 `compute_list(kind, marker=submission_marker)`：它读取该账户最新的任务，并返回存储配置中带有该标记的所有任务。只返回一个任务时，它很可能就是这次提交，继续使用它的 ID；返回多个任务时，它们共用一份复制的配置，应由用户判断哪一个（如果有的话）是这次提交。空结果不能证明提交失败：可以沿 `pagination.next_offset` 查看更早的页，稍后再次搜索（master 可能在搜索之后才保存任务），或在 WebUI 中查看。Determined 不再提供的已结束 command 或 shell 根本不会出现。是否重新提交由用户在检查之后决定；事后取消的重复任务可能已经写入文件或产生了取消无法撤销的其他影响。参见[未确认的提交](compute-service.zh.md#unconfirmed-launches)。

<a id="a-task-is-terminal-but-the-result-is-unclear"></a>
## 任务已终止但结果不明确

使用任务的 kind 和 ID 调用 `compute_status` 和 `compute_logs`。API 提交成功、获得任务 ID 或任务进入终态，本身都不能证明工作负载成功。检查退出信息和提交前定义的成功判据。已经配置存储访问时，用 `storage_check` 验证预期共享产物；需要本地副本时，先预览再执行 `storage_fetch`。没有存储访问时，使用任务输出或另一项明确的任务内检查。要确认任务结束前是否真正使用了 CPU、内存或 GPU，调用 `compute_usage(kind, id)`；对于已结束的任务或已暂停的 trial，窗口终点是任务或其最后一个 allocation 的结束时间。

Experiment 在 trial 启动前可能没有 trial 日志。Shell 仍然可用时，可以使用经过清理的重连命令。报告可以包含任务 ID、状态、经过清理的命令、路径和错误，但不得包含凭据值或 secrets 文件内容。

<a id="usage-measurements-are-unavailable-or-empty"></a>
## 用量测量不可用或为空

`compute_usage` 依赖 master 的任务资源 API。`task_resources_disabled` 表示 master 具备该 API，但管理员尚未启用 `integrations.task_resources`。`task_resources_unsupported` 表示 master 缺少该 API，需要 research-cluster fork 0.40.1 或更高版本的 Determined master。两者都不可重试，应联系管理员。无法取得测量值不能作为任务空闲的证据。

HTTP 503 表示测量后端繁忙或不可用；每个 master 同时最多运行四个资源查询，因此应稍后重试。HTTP 400 可能表示时钟偏差：master 会拒绝比其自身时钟超前 60 秒以上的窗口终点，应校正运行 MCP 服务的机器的时钟。HTTP 404 表示 Determined task 或指定的 trial ID 不存在，或当前账户无权访问。Determined 只在已结束的 command 或 shell 结束后 24 小时内提供它，master 重启后也不再提供；此后无法验证其所有者，因此其用量、状态和日志都返回 HTTP 404，重试也无济于事。

`task_not_started` 表示 experiment 尚无 trial，或其 trial 尚无 Determined task；应等待任务启动。`trial_not_found` 表示请求的 trial 不属于该 experiment。`allocation_not_found` 表示所选 task 未列出该 allocation；应从返回的 `allocations` 中选择。

`context_unavailable` 非空并不致命。它列出因 Determined API 错误而失败的上下文查询（`resource_pool`、`allocation_details` 或 `gpu_models`）；相关字段为空或 `null`，但返回的测量值仍然有效。需要这些上下文时，可稍后重试。出现传输失败后会跳过其余查询，因此可能同时列出多个名称。`gpu_model` 为 `null` 而 `context_unavailable` 中没有 `gpu_models` 时，可能是 RBAC 对当前账户隐藏了设备 UUID，因而无法匹配型号名称。工作负载不通过 Determined 的 Core API 报告进度（例如普通 bash 入口）或尚未报告时，trial 的 `total_batches_processed` 为 0 属于预期；这不能说明工作负载没有任何进展，应改为根据日志、实测用量和预期产物判断进展。

空的 `series` 列表表示该窗口没有数据，而不是任务空闲：任务可能未在该窗口内运行，或监控系统没有保留其数据。将窗口及其 `anchor` 与 `task_start_time` 和 `allocations` 对照，或使用更长的 `window_seconds`。如果 `samples_omitted` 为 true，应缩短窗口、减少指标或选择一个 allocation。agent 断开期间，任务可能仍保持 `RUNNING` 最多约 150 秒，因为该 fork 默认会等待 agent 重连这么久（`agent_reconnect_wait`）；因此仅凭 `RUNNING` 状态不能证明任务在推进。字段含义和限制见[任务用量测量](compute-service.zh.md#task-usage-measurements)。

<a id="a-task-operation-is-refused"></a>
## 任务操作被拒绝

MCP 只操作已认证账户拥有的任务。`ownership_mismatch` 表示任务属于其他账户；即使凭据属于管理员，服务也会在读取任务后、发出任何后续请求之前拒绝。应使用拥有该任务的账户凭据，或请管理员直接通过 Determined 操作。`ownership_unavailable` 表示 master 没有报告 generic 任务的所有者，因为它缺少 research-cluster fork 的 generic 任务列表（WU-CVGL/determined#27）；请管理员升级 master。在这样的 master 上，提交或列出 generic 任务会以 `unsupported` 失败；提交会在创建任何内容之前检查这一点。

Determined 本身也会执行权限检查。在使用 basic authorization 的 fork 0.40.1 或更高版本上，只有任务所有者或管理员可以终止、取消、暂停或恢复任务；其他账户会收到 HTTP 403，experiment 则返回 HTTP 404 `experiment '<id>' not found`。

<a id="a-transfer-is-partial-or-different-from-the-preview"></a>
## 传输不完整或与预览不同

传输不会添加 `--delete`，因此目标中无关的文件会保留。正常 rsync 行为仍可能覆盖同名文件。执行中的失败可能留下不完整目标；rsync 退出码 23 明确表示部分文件或属性未能传输。

检查长度受限的传输输出，修正文件系统或配置问题，重新执行 dry run 并审核后再执行。不要自动更改权限保留参数后重试。详细规则见[共享存储访问](shared-storage-access.zh.md)。

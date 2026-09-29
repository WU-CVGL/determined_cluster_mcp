<a id="shared-storage-access-from-a-client"></a>
# 从客户端访问共享存储

[English](shared-storage-access.md) | [简体中文](shared-storage-access.zh.md)

Determined 任务使用计算配置中的共享主机路径与容器路径映射。可选的存储客户端让未挂载这些文件系统的机器通过登录节点检查、同步和取回文件。它不会通过 Determined 上传源码。

如果工作目录已经准备在共享存储上，提交任务只需要 Determined 认证。只有在所选路径没有本地挂载、并调用 `storage_check`、`storage_sync` 或 `storage_fetch` 时才需要 SSH。

<a id="configure-access-separately"></a>
## 单独配置存储访问

把访问方式放在独立 YAML 文件中，通过 `--storage-config PATH` 或 `DETERMINED_COMPUTE_STORAGE` 指定。该文件不会改变计算配置指纹或任务身份。

```yaml
mode: auto                 # auto、local 或 ssh
local_mounts:
  - host_path: /SSD
    local_path: /Volumes/cluster-ssd
ssh:
  host: cluster-login      # 建议使用 ~/.ssh/config 中的 Host 别名
  # user: alice            # 已在 SSH 配置中设置时可省略
  # port: 22
  # identity_file: ~/.ssh/id_ed25519
  # config_file: ~/.ssh/config
  auth: openssh            # openssh、password 或 keyring
  # keyring_service: determined-compute
connect_timeout_seconds: 10
timeout_seconds: 120
preserve_permissions: true # 仅在确认文件系统不兼容时设为 false
# snapshots:               # 可选；见“发布代码快照”
#   root: /SSD/project/snapshots  # 可写挂载下的容器路径
#   link_mode: auto        # auto、reflink、hardlink 或 copy
```

`local_mounts` 把计算配置中的主机根目录或子目录映射到客户端绝对路径。`auto` 先使用显式映射，或在同名主机根目录本地存在时直接使用该根目录，否则回退到已配置的 SSH；`local` 要求本地可访问；`ssh` 始终访问登录节点。`shared_dir` 参数始终使用容器命名空间路径。服务先解析权威的计算配置挂载，再转换为集群主机路径和所选客户端后端路径。

没有存储文件时，服务使用空的 `auto` 配置。如果存储操作既没有本地访问方式也没有 SSH，会返回 `configuration_required`；计算规划和提交仍然可用。`connect_timeout_seconds` 允许 1–120 秒，默认 10；`timeout_seconds` 允许 1–3600 秒，默认 120。`preserve_permissions` 是布尔值，默认 `true`。

CLI 和 MCP server 在规划和提交时也使用此文件：它决定哪些启动路径可以在本地检查，以及如何创建
请求中 `create_directories` 指定的目录（通过本地视图，或在 SSH 登录节点上执行 `mkdir -p`）。
参见[启动路径检查](compute-service.zh.md#launch-path-checks)。旧版本会拒绝未知字段，因此只有在
读取此文件的所有进程（包括共用任务数据库的每个 MCP server）都已运行支持 `snapshots` 的版本后，
才能添加该字段。

`ssh.host` 应指向登录节点。网关只应作为可选的 `ProxyJump`，不能把网关误当成存储端点。建议使用 SSH 别名，把用户名、密钥、端口、跳板路由和主机密钥策略留在 `~/.ssh/config`：

```sshconfig
Host cluster-gateway
  HostName gateway.example.org
  User alice

Host cluster-login
  HostName login.internal.example.org
  User alice
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  # ProxyJump cluster-gateway
```

OpenSSH 说明 `ProxyJump` 会先连接跳板机，并建议把目标机和跳板机各自的设置写入 `~/.ssh/config`。首次连接时应交互式核对可信来源提供的主机密钥指纹，再进入自动化。存储后端强制使用 `StrictHostKeyChecking=yes`，因此经验证的密钥必须已经写入 `known_hosts`；不要使用 `StrictHostKeyChecking=no`。参见官方 [`ssh_config(5)`](https://man.openbsd.org/ssh_config.5)。

<a id="authentication-choices"></a>
## 认证方式

<a id="ssh-key-and-agent-auth-openssh"></a>
### SSH 密钥与 agent（`auth: openssh`）

服务只继承已有 agent，不会启动或解锁 agent。先检查当前 agent；仅当环境中没有可用 socket 时再启动：

```bash
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
  eval "$(ssh-agent -s)"
fi
ssh-add -l
ssh-add ~/.ssh/id_ed25519
```

`ssh-add` 需要正在运行的 agent 和 `SSH_AUTH_SOCK`；如果私钥已加密，它会在用户终端读取口令。参见 [`ssh-agent(1)`](https://man.openbsd.org/ssh-agent.1) 和 [`ssh-add(1)`](https://man.openbsd.org/ssh-add)。

在 macOS 上，需要把密钥口令保存到钥匙串时，应使用 Apple 的系统程序：

```bash
/usr/bin/ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

Apple 的 OpenSSH launch agent 会发布 `SSH_AUTH_SOCK`；GitHub 的官方 macOS 说明指出 `--apple-use-keychain` 属于 `/usr/bin/ssh-add`。参见 [Apple launch-agent 源码](https://github.com/apple-oss-distributions/OpenSSH/blob/main/com.openssh.ssh-agent.plist) 和 [GitHub 的 macOS 说明](https://docs.github.com/en/authentication/troubleshooting-ssh/error-ssh-add-illegal-option----apple-use-keychain)。

如果 Codex 启动 MCP 服务，需要从 Codex 的本地环境转发该 socket：

```toml
[mcp_servers.determined-compute]
env_vars = ["SSH_AUTH_SOCK"]
```

Codex 官方文档把 `env_vars` 定义为转发给 stdio MCP 服务的环境变量白名单。GUI 客户端本身也必须继承 `SSH_AUTH_SOCK`：先退出已经运行的实例，再从准备好 agent 的终端启动新实例（macOS 可用 `open -na Codex`）；或者在客户端启动器环境中传入该变量，然后重启 MCP 服务。参见 [Codex MCP 配置](https://developers.openai.com/codex/mcp)。

<a id="password-auth-password"></a>
### 密码（`auth: password`）

把登录节点凭据写入现有 secrets 文件，不要放进存储配置、MCP 参数、任务请求或报告：

```dotenv
SSH_USERNAME=alice
SSH_PASSWORD=replace-me
```

把文件权限限制为当前用户。SSH 密码与 `DET_*` 凭据相互独立；内部 askpass 辅助程序不得把密码暴露在进程参数或输出中。

可以通过现有的全局 `--secrets-file` 参数或 `DETERMINED_COMPUTE_SECRETS` 选择非默认 secrets 文件。省略 `ssh.user` 时，`SSH_USERNAME` 可以提供用户名；两者同时存在时必须一致。

<a id="os-keyring-auth-keyring"></a>
### 系统钥匙串（`auth: keyring`）

在服务使用的 Python 环境中安装可选凭据后端：

```bash
python -m pip install -e '.[mcp,keyring]'
```

同时设置 `ssh.user` 和 `ssh.keyring_service`，再为该用户名保存登录节点密码：

```bash
python -m keyring set determined-compute alice
```

所选 keyring 后端必须已经解锁，并且服务进程能够访问。这里保存的是账户密码，不负责解锁 SSH 私钥；私钥口令应通过 `ssh-add` 加载到 `ssh-agent`。[keyring 文档](https://keyring.readthedocs.io/en/stable/)说明了后端选择、诊断以及 `get_password`/`set_password` 的行为。

<a id="check-preview-and-transfer"></a>
## 检查、预览和传输

Python 接口为 `StorageService.check(path)`、`sync(local_dir, shared_dir, dry_run=True)` 和 `fetch(shared_dir, local_dir, dry_run=True)`。CLI 与 MCP 使用相同的路径规则。`check` 返回所选后端、容器路径、转换后的主机路径、可选本地路径、存在性、类型以及读写权限。同步/取回复制目录内容，并返回操作、后端、解析后的两端路径、主机路径、排除项、实际 `preserve_permissions`、dry-run/完成状态，以及带 `truncated` 标志的长度受限输出。本地结果包含映射路径；SSH 结果只暴露配置的主机别名，不返回用户名、密钥路径或凭据。

```bash
export DETERMINED_COMPUTE_PROFILE=/path/to/compute-profile.yaml
export DETERMINED_COMPUTE_STORAGE=/path/to/storage-access.yaml

determined-compute storage-check /SSD/project/run

# 默认为预览，不修改文件。
determined-compute storage-sync "$PWD/repo" /SSD/project/run/repo
determined-compute storage-fetch /SSD/project/run/results "$PWD/results"

# 检查预览后才执行传输。
determined-compute storage-sync "$PWD/repo" /SSD/project/run/repo --execute
determined-compute storage-fetch /SSD/project/run/results "$PWD/results" --execute
```

MCP 提供 `storage_check(path)`、`storage_sync(local_dir, shared_dir, dry_run=True)` 和 `storage_fetch(shared_dir, local_dir, dry_run=True)`。默认为预览；检查解析后的源路径、目标路径、传输方式和排除规则之后，才传入 `dry_run=false`。CLI 使用 `--execute` 表达同一授权。只读咨询 worker 没有 SSH/存储凭据或工具，也不应让它测试凭据。

客户端的 `local_dir` 必须是绝对路径。传输使用 `rsync -a --safe-links --mkpath --itemize-changes`；SSH 传输还使用隔离参数选项（`-s`）。默认的 `preserve_permissions: true` 会让归档模式保留权限、所有者、用户组和目录时间。只有确认某个挂载拒绝这些操作时才设为 `false`；此时本地和 SSH 传输都会加入 `--no-owner --no-group --no-perms --omit-dir-times`。不要全局关闭保留行为，不要根据存储名称猜测，也不要在失败后自动改参数重试。

Dry-run 不创建目标目录，并返回长度受限的预览输出。传输不会加入 `--delete`、`--copy-links`，也不会把密码放进进程参数。同步会排除版本库元数据、本地缓存、SSH/云配置、常见环境/凭据/密钥文件；如果已配置的 secrets 文件位于源目录中，也会精确排除。排除规则只是纵深防御，并不是完整的 secret 扫描器；执行前仍需检查项目自己的 secret 文件名。

不使用 `--delete` 表示目标中多余的文件会保留；实际执行仍可能按照 rsync 归档模式的正常规则覆盖目标中同名文件。因此应先检查逐项 dry-run 输出。

Rsync 退出码 23 表示部分文件或属性未能传输，目标中可能已经留下部分副本。应检查长度受限的输出，修正文件系统或配置原因，重新预览并审核之后再执行。服务不得盲目重试失败的传输。

同步要求本地源目录已经存在，共享目标必须位于挂载根目录之下，不能等于根目录。执行时可以创建嵌套目标，但本地映射根目录必须事先存在。取回目标可以是本地输出目录，或父目录已存在的新目录。本地映射会规范化真实路径，防止符号链接越出配置根目录；远端 SSH 主机的策略和权限仍由配置该端点的操作者负责。

请确认客户端和登录节点都安装了 rsync 3.2.3 或更高版本，因为后端始终使用 `--mkpath`。官方 [rsync 手册](https://rsync.samba.org/ftp/rsync/rsync.1)还说明 `-s` 通过协议而不是远端 shell 传递参数。

<a id="publish-a-code-snapshot"></a>
## 发布代码快照

`storage_snapshot(repo_dir, revision="HEAD", include=None, exclude=None, dry_run=True, verify=False)`
和 `determined-compute snapshot REPO_DIR [--revision REV] [--include PATH]... [--exclude GLOB]... [--execute] [--verify]`
把某个 git 提交中被跟踪的内容原样发布为共享存储上的只读工作目录，使重复运行的任务复用同一份
副本，而不必每次复制工作区。需要配置 `snapshots.root`（可写挂载下、但不是挂载根目录的容器
路径），`snapshots.link_mode` 可选。本版本要求快照根目录有本地可写视图；只有 SSH 访问时返回
`configuration_required`。与传输一样，默认只预览。

`repo_dir` 是运行服务的机器上某个 git 工作树的顶层目录。revision 会解析为完整提交，文件从 git
对象库而不是工作树读取，因此快照与该提交完全一致。`include` 添加工作树中的文件或目录（例如
生成的或未跟踪的文件），并覆盖同名的已跟踪路径；它们必须位于仓库内，本身不能是符号链接，
也不能经过符号链接。可执行位会保留。留在树内的相对符号链接会重建，经由快照中其他符号链接
解析时也必须留在树内。直接或经由这种链越出树、或形成循环的符号链接返回 `unsafe_symlink`，
预览时就会报告。子模块会被跳过并报告，Git LFS 指针会产生警告。include 的目录中出现的
`.git` 文件或目录（例如 git worktree 或子模块检出中的）会被跳过，并以原因 `git_metadata`
报告；把 `.git` 内的路径作为 include 返回 `invalid_include`。

类似机密的已跟踪文件会被排除：上文传输排除规则匹配的名称、位于仓库内的已配置 secrets 文件、
名称含 `credential` 或 `secret` 的路径，以及名为 `token`、`.token` 或 `*.token` 的文件。同一
排除列表中的缓存目录和 `*.pyc` 也会被排除，但显式 include 可以恢复它们。匹配机密规则的
include 会返回 `secret_like_include`，而不会被静默丢弃。`exclude` 添加按路径分量匹配的
rsync 风格模式：以 `/` 结尾只匹配目录，以 `/` 开头则锚定在仓库根目录。每个被排除的路径都会
连同 `reason` 和 `rule` 一起报告。发布到共享存储之前，应先检查预览。

`content_id` 是规范化文件列表（路径、SHA-256、大小和模式）与符号链接目标的 SHA-256，工作目录
为 `<root>/trees/<content_id>`，因此相同内容无论来自哪个 revision 都只发布一次。
`<root>/manifests/<snapshot_key>.json` 中的 manifest 记录 `schema_version`、revision、tree、
来源、文件、符号链接、排除项、跳过项、警告和 `created_utc`，从不记录远端 URL。重复同一快照
会返回已有 manifest 和相同的 `manifest_sha256`；内容相同的其他 revision 会得到自己的
manifest，并共用同一棵树。

文件在 `<root>/objects/sha256/` 下只存储一次（可执行文件因硬链接共用同一模式而单独存储，带
`.x` 后缀），再链接到每棵树中。`auto` 依次探测 reflink、硬链接和复制。无法链接的文件（例如
达到链接数上限或跨设备）改为复制，结果会报告 `link_mode` 和 `link_fallbacks`。`copy` 不使用
对象库，只对整棵树去重。对象、树和 manifest 都先写入临时名称，之后从不修改或删除；并发发布
相同内容只会产生一棵树。复用已有树之前会检查文件大小，`verify` 时还会检查完整 hash；不匹配时
返回 `snapshot_corrupt`，且从不自动修复。树是只读的，因此任务必须写入其 `output_dir`。硬链接
与对象库共用 inode；在属主被映射（squash）的网络挂载上，模式位无法阻止蓄意写入，需要时请使用
`verify` 或 `link_mode: copy`。

结果报告 `dry_run`、`revision`、`tree`、`content_id`、`snapshot_key`、`workdir`（容器路径）、
`host_path`、`local_path`、`manifest_path` 和 `manifest_sha256`（预览时为 null，除非已经
发布）、`existing`（manifest 已存在）、`tree_existing`、`files`、`symlinks` 和 `bytes` 总计、
`new_objects`、`new_bytes`、`link_mode`、`link_fallbacks`、`excluded`、`skipped`、`warnings`，
以及 `request_fields`；其中的 `workdir` 和 `code_revision` 可直接写入计算请求。

```bash
determined-compute snapshot "$PWD"                   # 预览；不写入任何内容
determined-compute snapshot "$PWD" --execute         # 发布
determined-compute snapshot "$PWD" --revision v1.2 --include generated/ --exclude '*.log'
```

<a id="reuse-an-ssh-connection"></a>
## 复用 SSH 连接

连接复用可以减少重复认证。把控制 socket 放进只有当前用户可写的目录：

```sshconfig
Host cluster-login
  ControlMaster auto
  ControlPath ~/.ssh/controlmasters/%C
  ControlPersist 10m
```

```bash
install -d -m 700 ~/.ssh/controlmasters
ssh -MNf cluster-login
ssh -O check cluster-login
# 不再需要存储操作时：
ssh -O exit cluster-login
```

OpenSSH 建议使用私有的 `ControlPath` 目录，并在路径中包含 `%h/%p/%r` 或 `%C`；`-M` 创建主连接，`-N` 不运行远端命令，`-f` 在认证后转入后台。这只为后续存储操作保留登录节点传输连接，与 GPU 活动无关，也不会让 Determined 任务或 shell 保持运行。参见 [`ssh_config(5)`](https://man.openbsd.org/ssh_config.5) 和 [`ssh(1)`](https://man.openbsd.org/ssh.1)。

计算配置中的 `read_only: true` 同样约束存储操作：禁止向该挂载上传，允许检查与取回；取回的本地目标也不能映射回只读共享目录。`storage_check` 返回 `read_only`，其 `writable` 同时考虑文件系统权限和配置策略。

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
```

`local_mounts` 把计算配置中的主机根目录或子目录映射到客户端绝对路径。`auto` 先使用显式映射，或在同名主机根目录本地存在时直接使用该根目录，否则回退到已配置的 SSH；`local` 要求本地可访问；`ssh` 始终访问登录节点。`shared_dir` 参数始终使用容器命名空间路径。服务先解析权威的计算配置挂载，再转换为集群主机路径和所选客户端后端路径。

没有存储文件时，服务使用空的 `auto` 配置。如果存储操作既没有本地访问方式也没有 SSH，会返回 `configuration_required`；计算规划和提交仍然可用。`connect_timeout_seconds` 允许 1–120 秒，默认 10；`timeout_seconds` 允许 1–3600 秒，默认 120。`preserve_permissions` 是布尔值，默认 `true`。

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

Python 接口为 `StorageService.check(path)`、`sync(local_dir, shared_dir, dry_run=True)` 和 `fetch(shared_dir, local_dir, dry_run=True)`。MCP 存储工具调用这些方法。`check` 返回所选后端、容器路径、转换后的主机路径、可选本地路径、存在性、类型以及读写权限。同步/取回复制目录内容，并返回操作、后端、解析后的两端路径、主机路径、排除项、实际 `preserve_permissions`、dry-run/完成状态，以及带 `truncated` 标志的长度受限输出。本地结果包含映射路径；SSH 结果只暴露配置的主机别名，不返回用户名、密钥路径或凭据。

MCP 提供 `storage_check(path)`、`storage_sync(local_dir, shared_dir, dry_run=True)` 和 `storage_fetch(shared_dir, local_dir, dry_run=True)`。默认为预览；检查解析后的源路径、目标路径、传输方式和排除规则之后，才传入 `dry_run=false`。

客户端的 `local_dir` 必须是绝对路径。传输使用 `rsync -a --safe-links --mkpath --itemize-changes`；SSH 传输还使用隔离参数选项（`-s`）。默认的 `preserve_permissions: true` 会让归档模式保留权限、所有者、用户组和目录时间。只有确认某个挂载拒绝这些操作时才设为 `false`；此时本地和 SSH 传输都会加入 `--no-owner --no-group --no-perms --omit-dir-times`。不要全局关闭保留行为，不要根据存储名称猜测，也不要在失败后自动改参数重试。

Dry-run 不创建目标目录，并返回长度受限的预览输出。传输不会加入 `--delete`、`--copy-links`，也不会把密码放进进程参数。同步会排除版本库元数据、本地缓存、SSH/云配置、常见环境/凭据/密钥文件；如果已配置的 secrets 文件位于源目录中，也会精确排除。排除规则只是纵深防御，并不是完整的 secret 扫描器；执行前仍需检查项目自己的 secret 文件名。

不使用 `--delete` 表示目标中多余的文件会保留；实际执行仍可能按照 rsync 归档模式的正常规则覆盖目标中同名文件。因此应先检查逐项 dry-run 输出。

Rsync 退出码 23 表示部分文件或属性未能传输，目标中可能已经留下部分副本。应检查长度受限的输出，修正文件系统或配置原因，重新预览并审核之后再执行。服务不得盲目重试失败的传输。

同步要求本地源目录已经存在，共享目标必须位于挂载根目录之下，不能等于根目录。执行时可以创建嵌套目标，但本地映射根目录必须事先存在。取回目标可以是本地输出目录，或父目录已存在的新目录。本地映射会规范化真实路径，防止符号链接越出配置根目录；远端 SSH 主机的策略和权限仍由配置该端点的操作者负责。

请确认客户端和登录节点都安装了 rsync 3.2.3 或更高版本，因为后端始终使用 `--mkpath`。官方 [rsync 手册](https://rsync.samba.org/ftp/rsync/rsync.1)还说明 `-s` 通过协议而不是远端 shell 传递参数。

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

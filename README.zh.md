<a id="determined-cluster-mcp"></a>
# Determined Cluster MCP

[English](README.md) | [简体中文](README.zh.md)

通过本地 stdio MCP 服务运行 Determined command、shell、generic 任务和 experiment，暂停和恢复 experiment 与 generic 任务，并可从 OpenSSH、IDE 或 [ssh-mcp](https://github.com/tufantunc/ssh-mcp) 等 SSH MCP 服务经 SSH 访问运行中的 shell。代码、数据、检查点和输出都保存在映射的共享存储中。任何能启动本地 stdio 服务的 MCP 客户端都可以使用本服务，并使用自己的模型。

<a id="install"></a>
## 安装

需要 Python 3.10+、Determined 账户，以及集群管理员或项目配置提供的部署参数。

```bash
git clone https://github.com/WU-CVGL/determined_cluster_mcp.git
cd determined_cluster_mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

<a id="configure"></a>
## 配置

```bash
mkdir -p .local
cp cfg/compute-profile.example.yaml .local/profile.yaml
cp cfg/examples/command_request.json .local/request.json
```

在 `.local/credentials.env` 中设置 API 地址和账户凭据：

```dotenv
DET_MASTER=http://determined.example.org:8080
DET_API_TOKEN=replace-with-your-token
```

也支持 `DET_USERNAME` 和 `DET_PASSWORD`。不要提交凭据文件。使用管理员提供的镜像、资源池、计算节点宿主机路径和容器挂载路径填写 `profile.yaml`。任务请求使用容器路径。

HTTPS 为可选项：设置 `DET_MASTER=https://determined.example.org`，在 MCP 参数中加入 `--verify-ssl`，并在客户端的 `env` 中为私有 CA 传入 `REQUESTS_CA_BUNDLE`，代理无法访问 master 时传入 `NO_PROXY` 和 `no_proxy`。参见[可选 HTTPS](docs/compute-service.zh.md#optional-https)。

计算任务不需要客户端存储配置。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再把 `--storage-config /absolute/path/to/.local/storage.yaml` 加入 MCP 参数。

<a id="connect-a-stdio-mcp-client"></a>
## 接入 stdio MCP 客户端

按 MCP 客户端使用的语法添加以下服务。将所有示例值替换为本机绝对路径或实际部署参数：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/determined_cluster_mcp/.local/profile.yaml",
    "--secrets-file", "/absolute/path/to/determined_cluster_mcp/.local/credentials.env"
  ]
}
```

凭据决定所使用的 Determined 账户。服务只操作该账户的任务，且不保存任务记录：工具通过 Determined 自身的 ID 指定任务，记录已提交的工作由调用方负责。需要 SSH 或私有 CA 时，将相应环境传给 stdio 进程，具体见下方故障排查文档。

从带任务数据库的版本升级时，请从 client 配置中删除 `--db` 和 `--owner`，并从计算配置中删除 `cluster_identity`；旧的数据库文件可以删除。参见[升级](docs/compute-service.zh.md#upgrading-from-a-version-with-a-task-database)。

<a id="reach-shells-over-ssh-optional"></a>
## 经 SSH 访问 shell（可选）

`compute_shell_connect` 为该账户的一个运行中 shell 打开本地 SSH 端点（如有要求，会先等待其启动），并返回简短的 `ssh_command`，例如 `ssh -F <config> det-4ed328fa`，使 agent 或 IDE 能在该 shell 中工作；在可以多路复用时，第一条命令之后的命令复用同一个连接。它还会将对应的 profile 写入为可选的 [ssh-mcp](https://github.com/tufantunc/ssh-mcp) 服务生成的配置中；每次新增或改变 profile 的 connect 之后都必须启动或重新连接该服务：

```bash
claude mcp add --transport stdio ssh-mcp -- \
  ssh-mcp --config="$HOME/.cache/determined-compute/shell-access/ssh-mcp.toml" --hostKeyMode=strict
```

参见[shell 访问](docs/compute-service.zh.md#shell-access)。

<a id="install-the-agent-skill-optional"></a>
## 安装 agent skill（可选）

MCP 工具不依赖该 skill 即可使用。`intensive-compute-runner` skill 为 Codex、Claude Code 等支持 skill 的 agent 提供任务指引。在仓库根目录下，将 skill 目录链接到 agent 的 skill 目录：

```bash
# Codex
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/intensive-compute-runner" "${CODEX_HOME:-$HOME/.codex}/skills/"

# Claude Code
mkdir -p "$HOME/.claude/skills"
ln -s "$PWD/skills/intensive-compute-runner" "$HOME/.claude/skills/"
```

启动新的 agent 会话即可加载。使用链接时，`git pull` 后 skill 会自动保持最新，skill 中的相对链接也会经由该链接的真实路径指向本仓库的 `docs/`；因此应使用链接而不是复制，并保持仓库目录不动。若只想在某个 Claude Code 项目中使用，改为链接到该项目的 `.claude/skills/`。skill 要求上述 MCP 服务已接入。删除该链接即可卸载。

<a id="documentation"></a>
## 文档

- [Agent 工作流](docs/agent-workflow.zh.md)：准备、规划、提交、跟踪和验收任务
- [计算服务参考](docs/compute-service.zh.md)：配置、请求、工具、用量测量、shell 访问、任务身份与所有权，以及未确认的提交
- [共享存储访问](docs/shared-storage-access.zh.md)：本地挂载、SSH、预览和传输
- [故障排查](docs/troubleshooting.zh.md)：启动、认证、TLS、代理、路径、容量、未确认的提交、用量测量、被拒绝的任务操作和 shell 访问

<a id="determined-cluster-mcp"></a>
# Determined Cluster MCP

[English](README.md) | [简体中文](README.zh.md)

通过本地 stdio MCP 服务运行 Determined `command`、`shell` 和 `experiment` 任务。代码、数据、检查点和输出都保存在映射的共享存储中。任何能启动本地 stdio 服务的 MCP 客户端都可以使用本服务，并使用自己的模型。

<a id="install"></a>
## 安装

需要 Python 3.10+、Determined 账户，以及集群管理员或项目配置提供的部署参数。master 必须是带有任务台账的 Determined fork 构建：`GET /api/v1/master` 必须报告 `submission_protocol` 1 或更高。上游 Determined 发行版不报告 submission protocol，服务会拒绝它们。

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
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

也支持 `DET_USERNAME` 和 `DET_PASSWORD`。不要提交凭据文件。secrets 文件指定了 `DET_MASTER` 时，其凭据只发往该 master；环境变量指向另一个 master 时，服务拒绝启动。使用管理员提供的镜像、资源池、计算节点宿主机路径和容器挂载路径填写策略文件 `profile.yaml`。任务请求使用容器路径。服务不保存本地状态：Determined master 记录每个任务。

计算任务不需要客户端存储配置。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再把 `--storage-config /absolute/path/to/.local/storage.yaml` 加入 MCP 参数。

<a id="connect-a-stdio-mcp-client"></a>
## 接入 stdio MCP 客户端

按 MCP 客户端使用的语法添加以下服务。将所有示例值替换为本机绝对路径或实际部署参数：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/determined_cluster_mcp/.local/profile.yaml",
    "--secrets-file", "/absolute/path/to/determined_cluster_mcp/.local/credentials.env",
    "--verify-ssl"
  ]
}
```

存储访问需要单独的文件时，加入 `"--storage-config", "/absolute/path/to/.local/storage.yaml"`；只有不使用 TLS 的部署才使用 `--no-verify-ssl`。凭据决定所使用的 Determined 账户，该账户拥有服务提交的每个任务。启动时服务检查 master 的 submission protocol；master 低于 protocol 1 或配置无效时以状态码 2 退出，master 无法连接则不影响启动。需要 SSH 或私有 CA 时，将相应环境传给 stdio 进程，具体见下方故障排查文档。

<a id="upgrade-from-an-earlier-release"></a>
## 从早期版本升级

1.0 版本不保存本地状态：master 记录每个任务，`job_id` 是任务的唯一句柄。升级步骤：

1. 先升级 master。master 没有 submission protocol 1 时，新服务以状态码 2 退出。
2. 从每个 MCP 客户端配置中删除 `--db`、`--owner` 以及 `DETERMINED_COMPUTE_DB`、`DETERMINED_COMPUTE_OWNER` 变量，同时删除 `--repo-root` 和 `--consultation-*` 参数。服务不再接受这些参数，遇到未知参数时以状态码 2 退出。
3. 从策略中删除 `cluster_identity` 和 `shell_inactivity_seconds`，并把 `shared_mounts` 改名为 `mounts`；未知字段返回 `invalid_policy`。策略新增 `pools`、`max_slots` 和 `allow_overwrite`，见[策略参考](docs/compute-service.zh.md#policy)。
4. 更新代码并用 `python -m pip install -e '.[mcp]'` 重新安装，然后重启每个客户端、每个会话中的所有 MCP 进程。正在运行的进程保留已加载的代码，会继续提供旧工具并写入旧数据库。

旧的 SQLite 数据库（`--db`，默认为 `~/.local/state/determined-compute/tasks.sqlite3`）不再被读取或写入，可以保留作为记录，也可以删除。任务 ID 现在来自 master，因此其中保存的本地 `task_id` 不再可用。`compute_list` 列出该账户在所有客户端提交的每个任务，包括升级前提交、没有 `request_id` 的任务；从中取得它们的 `job_id`。请求现在是 `TaskSpec`（见 `cfg/examples/`），`compute_list` 取代了 `compute_list_tasks`、`compute_reconcile`、`compute_discover` 和 `compute_adopt`。`storage_sync` 和 `storage_fetch` 现在保留目标中已有的文件，除非设置 `overwrite=true` 且策略允许。

<a id="documentation"></a>
## 文档

- [Agent 工作流](docs/agent-workflow.zh.md)：准备、规划、提交、跟踪和验收任务
- [计算服务参考](docs/compute-service.zh.md)：策略、TaskSpec、11 个工具、用量测量和错误
- [共享存储访问](docs/shared-storage-access.zh.md)：本地挂载、SSH、预览和传输
- [故障排查](docs/troubleshooting.zh.md)：启动、协议检查、认证、TLS、spec、路径、git 规划、任务排队、规划绑定、提交结果不确定、用量测量、取消和传输

<a id="agent-instructions"></a>
# Agent 说明

[English](AGENTS.md) | [简体中文](AGENTS.zh.md)

遵循调用方要求的范围，只阅读当前任务需要的路线：

- 集群任务或存储操作：[Agent 工作流](docs/agent-workflow.zh.md)
- 请求字段、工具、身份或恢复：[计算服务参考](docs/compute-service.zh.md)
- 本地挂载、SSH 或文件传输：[共享存储访问](docs/shared-storage-access.zh.md)
- 可选的服务端建议：[咨询](docs/consultation.zh.md)
- 启动或运行故障：[故障排查](docs/troubleshooting.zh.md)

API 地址、账户、镜像、资源池和挂载路径必须来自管理员或项目配置。不要自行编造部署参数，也不要暴露凭据。

只做文档或代码说明时无需访问集群。对于普通 agent 任务，如果调用方要求的范围需要当前任务、日志、用量、存储或容量信息，则执行只读实时查询；仅当所请求的工作需要时才使用修改状态的工具。如果作为可选咨询 worker 运行，只提供建议：不要修改文件、调用 MCP 工具、访问凭据，也不要提交、取消任务或传输文件。

本仓库是 MCP 安装、配置、工作流、API 使用和故障排查的权威文档；维护时保持中英文同步、使用相对链接，并使这些说明独立于具体部署和兄弟仓库。

# Determined 引导层

[English](README.md) | [简体中文](README.zh.md)

本版本化部署配方将服务实际注入的 Determined 0.42.1 wheel 的依赖安装到独立的 `/usr/local/bin/python`，并检查科学环境的全部软件包版本保持不变。SDK wheel 由服务提供在 `/opt/determined/wheels`，不嵌入镜像。`bootstrap.txt` 固定其 67 个依赖版本，包括 pip 26.2.1 和 requests 2.31.0。服务升级 SDK 时须重新审查依赖并运行准入检查。

从已提交的配方版本和外部声明的不可变科学镜像构建；每个镜像分别声明科学配置及其源码版本。科学解释器必须禁用用户 site，独立的 `/usr/local/bin/python` 必须启用它。包装器仅对子进程设置 `PIP_BREAK_SYSTEM_PACKAGES=1`，不激活或修改科学环境。

```sh
timeout 20m docker build --cpus 8 --memory 16g \
  --build-arg BASE_IMAGE=SCIENTIFIC_IMAGE \
  --build-arg SCIENCE_PROFILE=SCIENTIFIC_PROFILE \
  --build-arg SCIENCE_REVISION=SCIENTIFIC_SOURCE_REVISION \
  --build-arg SDK_VERSION=0.42.1 \
  --build-arg SCIENCE_PYTHON=/opt/venv/bin/python \
  --tag NEW_RUNTIME_IMAGE .
```

构建前拒绝已有输出目录或镜像标签，在新输出目录记录准确输入、构建日志和最终退出状态。命令限时 20 分钟、8 CPU、16 GiB。失败重试须使用新目录和标签。Docker 守护进程必须支持并落实这些限制；客户端中断后的守护进程取消和清理由操作员负责。导入准入程序不会构建或提交集群任务。

接受镜像前，通过 `/usr/local/bin/bm-bootstrap-python` 以真实非 root 作业 UID 运行 `admit.py sdk /opt/determined/wheels`，并覆盖 passwd 中不存在该 UID 的情况。设置 `LOGNAME=bm-runtime`、全新的作业所有 `PYTHONUSERBASE` 和作业所有 `XDG_CACHE_HOME`、`PIP_CACHE_DIR`、`CUDA_CACHE_PATH`；不要全局覆盖 HOME。TMPDIR 使用短路径（如 `/tmp`），避免 CUDA 编译路径长度限制。检查仅离线安装实际注入的 SDK 到用户 site，核对依赖并导入它，不修改镜像。输出保存在作业旁。同一作业环境中，用科学解释器运行 `admit.py compare /opt/determined-bootstrap/science-before.json` 再次检查。GPU 仿真验收是独立任务。

构建检查只证明已安装发行包的版本一致，不证明原生仿真器兼容性；科学配置仍须验证解释器、Torch/CUDA、仿真器导入及实际执行。不能仅凭 SDK 检查接受科学配置。

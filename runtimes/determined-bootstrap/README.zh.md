# Determined 引导层

[English](README.md) | [简体中文](README.zh.md)

本版本化部署配方将服务实际注入的 Determined 0.42.1 wheel 的依赖安装到独立的 `/usr/local/bin/python`，并检查科学环境的全部软件包版本保持不变。SDK wheel 由服务提供在 `/opt/determined/wheels`，不嵌入镜像。`bootstrap.txt` 固定其 67 个依赖版本，包括 pip 26.2.1 和 requests 2.31.0。服务升级 SDK 时须重新审查依赖并运行准入检查。

从已提交的配方版本和外部声明的不可变科学镜像构建；每个镜像分别声明科学配置及其源码版本。科学解释器必须禁用用户 site，独立的 `/usr/local/bin/python` 必须启用它。包装器仅对子进程设置 `PIP_BREAK_SYSTEM_PACKAGES=1`，不激活或修改科学环境。

```sh
docker buildx create --name NEW_BUILDER --driver docker-container \
  --driver-opt memory=16g,cpu-period=100000,cpu-quota=800000 --bootstrap
docker inspect buildx_buildkit_NEW_BUILDER0 \
  --format '{{.HostConfig.Memory}} {{.HostConfig.CpuPeriod}} {{.HostConfig.CpuQuota}}'
timeout 20m docker buildx build --builder NEW_BUILDER --load \
  --build-arg BASE_IMAGE=SCIENTIFIC_IMAGE \
  --build-arg SCIENCE_PROFILE=SCIENTIFIC_PROFILE \
  --build-arg SCIENCE_REVISION=SCIENTIFIC_SOURCE_REVISION \
  --build-arg SDK_VERSION=0.42.1 \
  --build-arg SCIENCE_PYTHON=/opt/venv/bin/python \
  --tag NEW_RUNTIME_IMAGE .
```

构建前拒绝已有输出目录或镜像标签，在新输出目录记录准确输入、构建日志和最终退出状态。每次尝试使用新的构建器名称。开始构建前，检查其容器必须显示 memory=17179869184、cpu-period=100000 和 cpu-quota=800000，即专用构建器的 16 GiB 内存限制与 8 CPU 配额。timeout 将客户端限制为 20 分钟；检查确认前，这些只是请求的配置。失败重试须使用新目录和标签。Docker 守护进程必须支持并落实这些限制；无论成功、失败或客户端超时，都用 `docker buildx rm NEW_BUILDER` 停止并删除专用构建器，并验证其容器已停止。不能假设客户端超时会停止守护进程中的任务。部署所需的 CPU 亲和性、网络及代理配置应保留在外部执行配置中。导入准入程序不会构建或提交集群任务。

接受镜像前，通过 `/usr/local/bin/bm-bootstrap-python` 以真实非 root 作业 UID 运行 `admit.py sdk /opt/determined/wheels`，并覆盖 passwd 中不存在该 UID 的情况。设置 `LOGNAME=bm-runtime`、全新的作业所有 `PYTHONUSERBASE` 和作业所有 `XDG_CACHE_HOME`、`PIP_CACHE_DIR`、`CUDA_CACHE_PATH`；不要全局覆盖 HOME。TMPDIR 使用短路径（如 `/tmp`），避免 CUDA 编译路径长度限制。检查仅离线安装实际注入的 SDK 到用户 site，核对依赖、导入它，并在同一 UID 和引导环境中以 60 秒限时运行 `python -m determined.exec.prep_container --help`。该版本 SDK 的 argparse 在读取任务配置或向服务器认证前即为 `--help` 退出，不修改镜像。输出保存在作业旁。同一作业环境中，用科学解释器运行 `admit.py compare /opt/determined-bootstrap/science-before.json` 再次检查。GPU 仿真验收是独立任务。

构建检查只证明已安装发行包的版本一致，不证明原生仿真器兼容性；科学配置仍须验证解释器、Torch/CUDA、仿真器导入及实际执行。不能仅凭 SDK 检查接受科学配置。

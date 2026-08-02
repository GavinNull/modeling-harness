# Agent 镜像契约

任何语言、Agent 框架或模型供应商都可以接入，只要镜像满足以下稳定接口。

## 文件接口

- 启动时读取 `/workspace/task_packet.json`，UTF-8 JSON，不得修改。
- 输入工件只从 TaskPacket 声明的 `/inputs/**` 路径读取。
- 所有临时和最终写入只能位于 `/workspace`。
- 正常退出前原子写入 `/workspace/result_packet.json`。
- `result_packet.json` 必须满足 `ResultPacket` Schema，并与 task、run、attempt、role、prompt hash 和输入 manifest hash 完整绑定。

## 进程接口

- 镜像以非 root 用户运行。
- ENTRYPOINT 直接启动 Agent 程序，不使用 `sh -c`、`bash -c`、`cmd /c` 或 PowerShell command string。
- ENTRYPOINT 可以忽略编排器附加的角色标识参数，也可以验证它与 TaskPacket 一致。
- 成功返回 0；任何未处理错误返回非零。
- 必须响应 SIGTERM；超时后执行器可能强制终止。

## 网络和依赖

默认无网络。需要检索的角色只能通过经过证明的 proxy-only 网络访问显式域名。镜像应固定依赖和模型客户端版本；任何远程响应都要进入来源与工件血缘。禁止将 API key 烘焙进镜像或写入输出。

## 构建

```powershell
# 开发构建可以使用默认 tag；生产构建必须传入实际解析并审查过的 digest。
docker build -f examples/Dockerfile.agent -t modeling-agent:dev .
docker build --build-arg PYTHON_BASE_IMAGE="python@sha256:<verified-digest>" `
  -f examples/Dockerfile.agent -t modeling-agent:production .
docker inspect --format "{{index .RepoDigests 0}}" modeling-agent:dev
```

生产配置必须使用 `repository@sha256:<64 hex>`，不能使用可变 tag。

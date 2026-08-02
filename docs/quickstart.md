# 快速开始

## 1. 安装与检查

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\modeling-harness.exe validate-config
.\.venv\Scripts\modeling-harness.exe verify-benchmarks
.\.venv\Scripts\modeling-harness.exe production-preflight
```

`production-preflight` 同时检查 Docker binary、客户端版本和 daemon。任何一项失败都表示生产容器执行不可用。

## 2. 构建 Agent 镜像

参考 `examples/Dockerfile.agent`。镜像必须使用 digest 固定版本，并遵循 [Agent 镜像契约](agent-image-contract.md)。镜像可以连接任意模型或本地推理服务，但网络必须由 allowlist 与经过证明的只读出站控制器提供，凭据不能写入镜像或结果工件。

## 3. 创建严格沙箱计划

为每次角色任务创建新的 `attempt_id`、session、container name 和 host write root。原始输入和提升工件以只读方式挂载，不得挂载其他 Agent 工作区。生产计划必须由 `DockerSandboxBackend` 生成。

## 4. 执行与验证

将 `PacketValidator`、严格计划和持久化 `ExecutionAttestationLedger` 交给 `DockerTaskExecutor`。成功结果包含日志哈希、结果哈希、计划哈希与 ledger 证明。失败时工作区、日志和审计记录保留，容器由 `--rm` 或强制删除流程清理。

## 5. 进入能力评估

按“定义—理解—创造—验证”运行完整链路。公开历史任务只作端到端参考；主要比较使用隐藏变体和独立新任务。构建角色只收到主 Agent 转译后的通用能力缺陷。


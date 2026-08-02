# 运行手册

## 启动检查

每次生产运行前执行：

```powershell
modeling-harness doctor
```

Doctor 检查配置、16 个角色与提示词、登记基准文件的大小和 SHA-256，以及 Docker 客户端和 daemon。任一失败返回非零退出码。

## 一次执行的生命周期

1. 主 Agent 签发严格验证的 `TaskPacket`。
2. 隔离后端核验只读输入、批准根、内容哈希、网络证明和新工作区。
3. 执行器复核计划，创建唯一 host write root。
4. 写入只读 `task_packet.json`。
5. 以参数数组、`shell=False` 启动唯一命名容器。
6. 容器写出 `result_packet.json`。
7. 执行器验证结构、任务身份、提示词与输入血缘。
8. 日志、结果、计划和任务包计算 SHA-256。
9. 执行证明写入持久化追加式 ledger，工作区保留供审计。

## 失败策略

- 无 Docker、daemon 失败：不创建运行工作区，生产不可用。
- 计划或挂载证明不一致：执行前拒绝。
- 超时或取消：终止客户端并请求 `docker rm -f`。
- 非零退出：保存 stdout、stderr 和审计记录，强制清理残留容器。
- 缺失或无效结果：保留工作区并拒绝结果。
- ledger 写入或验证失败：不得声称获得生产执行证明。

所有重试必须使用新的 attempt、container、session 和 write root。失败工作区只能封存，不能复用。

## Ledger

`ExecutionAttestationLedger` 的 SQLite 表由禁止 UPDATE/DELETE 的触发器保护，并通过 `previous_hash` 形成 SHA-256 链。备份时应复制 SQLite 主文件及其 WAL，并在恢复后调用 `verify()`。

生命周期状态事件使用独立的 run ledger，可通过：

```powershell
modeling-harness verify-ledger exported-run-ledger.json
```

进行重放校验。


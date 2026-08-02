# 通用数学建模研究 Agent 系统架构规范

版本：2.5.0（generic-agent v2.5）  
状态：通用治理发布版  
范围：架构、接口、隔离、状态、恢复和验收；不包含业务模型或实现代码

## 1. 目标与不可变原则

系统面向此前未见的开放式现实问题、原始附件和有限资源，按“定义—理解—创造—验证”闭环形成可追踪、可解释、可独立复算、可在干净环境复现的研究工件。

以下原则优先于吞吐量、成本和单次任务分数：

1. 主 Agent 只编写提示词、派遣任务、审查、批准或拒绝，不创建模型、代码、图表、报告正文、DTP、MAP 或修改方案，也不求解任务。
2. 16 个 Subagent 采用星型通信，只能与主 Agent 交换结构化数据。
3. 产生、执行或评审工件的每次任务均使用新建容器、独立会话和唯一可写目录。
4. 原始输入和已批准工件只读挂载；其他 Agent 工作区永不挂载。
5. 工件按 SHA-256 内容寻址，只有主 Agent 能批准提升；下游只能读取提升后的不可变快照。
6. 作者与评审的容器、状态和权限隔离，三类评审首次互盲。
7. 原始评审证据只对主 Agent 可见；任何真实或合成任务、评测内容及其全部衍生即使抽象、去标识、跨题或聚合也不得成为本体修改动机。构建角色只能收到任务无关独立一般研究支持的 DTP/MAP，或经独立审查的直接用户治理要求。
8. 所有改动优化通用能力，禁止按题目身份、特定参数或答案进行优化。
9. 历史原题只作完整性参考；隐藏变体和独立审查的新任务承担主要泛化评估。
10. 真实题面、附件、评分、讲评、优秀解和测试失败是隔离的作答/评估材料，不得进入 Agent 身份、能力、系统提示词、路由或共享实现。
11. 任何任务或评测证据永远只允许评测或修订当前答案工件；重复暴露、去标识、抽象、聚合与跨任务处理不能解除禁令。Agent 本体修改动机仅限任务无关独立一般研究或经独立审查的直接用户治理，并由相应协议强制准入。

角色和权限的唯一注册源为 [role_registry.yaml](role_registry.yaml)，生命周期的唯一状态定义为 [state_machine.yaml](state_machine.yaml)。

## 2. 逻辑组件

| 组件 | 职责 | 信任级别 |
|---|---|---|
| 主 Agent 控制面 | 提示词、TaskPacket、DTP/MAP 审查、提升与放行决策 | 高；不可读取隐藏答案键；不得构造任务工件、实现方案或求解任务 |
| 编排器 | 按已批准状态机机械分配容器、挂载、校验和记录事件 | 高；不得作业务判断 |
| Agent 运行沙箱 | 执行单个 TaskPacket，写唯一目录并返回 ResultPacket | 低；按角色最小权限 |
| 工件暂存区 | 保存未批准结果和不可变清单 | 不可信输入区 |
| 提升工件库 | 保存经主 Agent 批准的内容寻址快照 | 只读可信区 |
| 原始评审库 | 保存 ReviewPacket 和复现证据 | 受限；仅主 Agent |
| 隐藏基准库 | 保存变体、新任务、答案键与评分器 | 最高；仅基准管理员 |
| 事件账本 | 追加记录派遣、哈希、状态、权限、提升和失败事件 | 只追加 |

编排器可以执行模式校验、哈希重算、容器调度和状态落账，但不能代替主 Agent 批准提示词、工件、缺陷或版本。

## 3. 信任边界与运行隔离

### 3.1 容器要求

平台实现必须满足：

- 运行身份非 root；基础镜像以 digest 固定。
- 根文件系统只读，仅 TaskPacket 的 `write_root` 可写。
- `/inputs/...` 只读，挂载前后均核验清单和内容哈希。
- 不挂载宿主套接字、其他工作区、提升库写入口或隐藏基准库。
- 默认禁网；需检索的角色只能经只读、域名白名单、全量记录的代理访问。
- CPU、内存、磁盘和墙钟时间来自 TaskPacket，超限转入可恢复失败。
- 任务结束后撤销凭据并封存工作区；重试必须使用新容器、新目录和新 `attempt_id`。

逻辑写目录固定为：

```text
/runs/{project}/{task_id}/{role_id}/{attempt_id}/
```

短消息或纯控制面判断无需创建业务容器；任何会产生、运行或评审工件的任务都必须创建。

### 3.2 信息流

允许的信息流只有：

```text
用户/输入暂存 → 主 Agent → 单个 Subagent → 主 Agent
                              ↓
                         候选工件暂存

主 Agent 批准 → 提升工件库 → 后续 Subagent 的只读输入
任务/评测/评审 → 主 Agent → 评估、当前答案 RevisionDecision、发布通过或拒绝、回滚、报告
可信任务无关独立一般研究 → DTP/MAP → 主 Agent 审查 → 被授权的核心构建角色
### Revision 3E authoritative sound boundary

This section is authoritative wherever older descriptive wording is broader.

- Control sources are exactly `DIRECT_USER` and `INDEPENDENT_GENERAL_RESEARCH`.
- Direct-user construction lineage is `DirectUserExecutableInstructionV1 → AgentBodyControlAuthorizationV1 → AgentBodyCandidateSourceV1 → AgentBodyProposalV1 → AgentBodyAdmissionV1 → AgentBodyMergeV1`.
- Research construction lineage is `IndependentGeneralResearchManifestV1 → DTPV1 → MAPV1 → AgentBodyControlAuthorizationV1 → AgentBodyCandidateSourceV1 → AgentBodyProposalV1 → AgentBodyAdmissionV1 → AgentBodyMergeV1`.
- All construction objects are exact, versioned, hash-bound closed carriers. `AgentBodyProvenanceV1` contains only the closed control source and candidate lineage.
- Evaluation taint is permanent and has a disjoint type graph. Only `OpaqueEvaluationReceiptV1` enters the independent release ledger; it can never authorize construction or parent construction provenance.
- Release exposes only candidate/plan/receipt hashes and pass/reject, yielding only terminal `RELEASED` or `RELEASE_REJECTED`; it never authorizes rollback, reporting, or successor Agent-body construction. Task-plane rollback/report remains separate.
- `main_agent` actions are exactly `dispatch`, `write_prompts`, `review`, `approve`, `reject`; writes are exactly `dispatch_records`, `task_packets`, `prompt_registry`, `review_decisions`, `approval_decisions`, `rejection_decisions`. It never creates DirectUser/DTP/MAP/candidate/build/solution artifacts and never implements or solves.
- The boundary is enforced by positive carrier types, exact schemas, closed enums, hash bindings, exact permission sets and unreachable states, never by expanding semantic keyword blacklists.
- Authorization permissions are exactly `CONSTRUCT_CANDIDATE`, `PROPOSE_CANDIDATE`, `ADMIT_CANDIDATE`, and `PROMOTE_CANDIDATE`.
- `ConstructionLineageLedger`, `OpaqueEvaluationReceiptLedger`, and `ReleaseGateLedger` have disjoint nominal entries, stores, and state enums.
- Authorization, admission, and promotion never inspect natural language or task/evaluation content. Only closed positive types, trusted hashes, exact provenance, exact permissions, and reachable states decide the boundary.
- Agent-body rejection never emits or authorizes body rollback, body reporting, or body revision. Only the separate task plane may revise the current answer or roll back/report the current task/evaluation, and it cannot generate a new Agent-body modification.

直接用户治理文档哈希 + 独立审查哈希 → `DirectUserExecutableInstructionV1` → `AgentBodyControlAuthorizationV1` → 被授权的核心构建角色
基准管理员 → 匿名化指标 → 主 Agent → 发布通过或拒绝、回滚、报告
```

以下行为属于安全事件并直接隔离：跨工作区访问、直接同伴通信、隐藏集泄漏、哈希错配、来源伪造、未授权写入、题目特化规则。

## 4. “定义—理解—创造—验证”流水线

### 定义

`problem_definition_router` 只读取原始任务和附件，输出子问题树、目标、变量、单位、约束、附件映射、验收条件和需求追踪矩阵。未通过覆盖和一致性门槛时不得进入理解阶段。

### 理解

`domain_evidence_researcher` 与 `data_assumption_analyst` 使用不同容器并行工作。二者只共享已提升的定义快照，分别输出来源证据和数据/假设工件。主 Agent 审查后以哈希合并提升，不允许相互读取未提升工作区。

### 创造

`model_architect` 先比较基线与候选模型并按能力路由专业角色。机理数值、统计学习、优化决策角色只处理被路由的模块。`compute_reproducibility_engineer` 按批准语义实现，`visualization_report_author` 只根据已提升证据组织表达，不得补造结果。

### 验证

主 Agent 冻结候选清单后，三类评审员在全新容器中读取同一哈希快照：

- 数学逻辑：假设、推导、适配、边界、可辨识性与可行性；
- 数值复现：干净环境重建、独立复算、约束残差、随机稳定性；
- 表达证据：任务覆盖、主张血缘、图文和程序结果一致性。

评审员不修改候选工件，也不读取作者状态或其他评审结果。重大分歧不做平均，由第四个全新盲审环境复核争议标准。

## 5. 接口契约

所有 JSON 使用 UTF-8、JSON Schema Draft 2020-12、UTC RFC 3339 时间和小写十六进制 SHA-256。接收端先做 Schema 校验，再做下述语义校验。

| 接口 | 生产者 → 消费者 | Schema | 关键语义检查 |
|---|---|---|---|
| TaskPacket | 主 Agent → Subagent | `schemas/TaskPacket.json` | 角色存在；工具/数据权限不越权；目录唯一；输入已提升 |
| ResultPacket | Subagent → 主 Agent | `schemas/ResultPacket.json` | 与 TaskPacket、角色、运行和尝试一致；声明工件存在 |
| ArtifactManifest | Subagent/提升服务 → 主 Agent/下游 | `schemas/ArtifactManifest.json` | 重算内容哈希；路径无穿越；血缘闭合；提升审批合法 |
| ReviewPacket | 评审员 → 主 Agent | `schemas/ReviewPacket.json` | 评审类型匹配角色；独立性证明；候选哈希一致 |
| DTPV1 / MAPV1 | 独立通用研究控制面 → 核心构建角色 | `schemas/DTPV1.json` / `schemas/MAPV1.json` | 只含可信研究 manifest/review 哈希和关闭枚举；无自由文本、评测材料或任意扩展 |
| OpaqueEvaluationReceiptV1 | 独立 evaluation gate → release ledger | `schemas/OpaqueEvaluationReceiptV1.json` | 只含候选/计划/receipt 哈希和 pass/reject；永不成为构建父节点 |
| RoleCharter | Subagent → 主 Agent | `schemas/RoleCharter.json` | 与注册表职责和权限一致；10–20 个岗位测试 |

### 5.1 幂等与关联

- `packet_id` 全局唯一；同一 `packet_id + sha256` 重复提交返回原结果。
- 同一 `packet_id` 出现不同内容哈希时视为篡改并隔离。
- `task_id/run_id/attempt_id/role_id` 必须在 TaskPacket、ResultPacket 和 Manifest 中一致。
- 所有下游工件必须列出输入清单哈希、提示词哈希、容器镜像 digest 和随机种子。

### 5.2 工件提升

提升不是移动或覆盖原文件，而是：

1. 封存作者工作区并重算每个内容哈希；
2. 验证 ArtifactManifest 和血缘；
3. 主 Agent 记录批准或拒绝；
4. 提升服务创建引用原始清单哈希的新清单；
5. 将内容复制到只读内容寻址库；
6. 后续 TaskPacket 引用该提升清单和精确内容哈希。

任何已提升内容不得原位修改。变化必须产生新工件、新清单版本和新哈希。

## 6. 评估防火墙、独立研究 DTP 与反特化门

### 6.1 数据用途与边界

真实题面与附件只可进入当次作答链、基准管理员和独立评估角色；评分、讲评、优秀解、逐题失败与精确测试失败只可进入主 Agent、基准管理员及获授权评估角色。`system_architect`、`sandbox_platform_engineer`、`standards_delivery_manager` 以及任何角色的 identity-build 模式不得读取这些材料。

主 Agent 必须把“当次答案修订”和“Agent 本体修改”分成两个不可混用的控制流：

- 单一任务失败可生成仅指向当前运行答案工件的修订任务；不得改身份、能力、提示词、路由或共享实现。
- Agent 本体构建者只接收通用 core 工件和 `AgentBodyControlAuthorizationV1`；它不接收验证结果。DirectUser 与独立一般研究只通过关闭 carrier 和哈希绑定形成该 authorization。
- 评估者和基准管理员可为评估读取真实材料，但其原始输出必须回到主 Agent 受控证据库，不能直接路由给构建者。

### 6.2 独立一般研究 DTP

原始 ReviewPacket 可以包含任务内的复现位置，但固定为 `main-agent-only`，且只可用于评估、发布通过或拒绝、回滚、报告，或通过 task-bound `RevisionDecision` 修订当前答案。任何真实或合成任务、评测、评审及其衍生都不生成 DTP/MAP 或研究请求。DTP 只能由可信来源、任务无关的独立一般研究提交，主 Agent 仅审查而不构造；该 DTP 必须删除：

- 题号、题名和可反推任务身份的背景词；
- 特定参数、答案、目标区间和具体解法；
- 针对单个任务的条件分支或修改步骤；
- 原始数据行、隐藏变体差异和答案键内容。

Agent 本体修改首先要求动机来自具可信来源、与任务无关的独立一般研究，或经独立审查的直接用户治理要求。任何真实或合成任务、变体、提示词、数据、答案、评分、失败、评审及其全部衍生永久不具备资格。预注册的内容无关属性测试与契约测试只验证已许可修改，失败不得反向产生修改动机。
硬安全缺陷可以立即隔离和止血，但单次安全事件本身不授权长期 Agent 本体能力修改。

此外，独立一般研究必须映射到固定能力枚举，并通过 `IndependentGeneralResearchManifestV1 → DTPV1 → MAPV1` 的精确哈希链进入 authorization。proposal/admission/merge 不含验证结果；验证只在后置独立 release ledger 中决定 RELEASED 或 RELEASE_REJECTED。

## 7. 三层测试与放行

### 7.1 测试层

| 层 | 作用 | 放行权重 |
|---|---|---|
| 历史原始任务 | 端到端完整性、工具和交付格式回归 | 参考，不证明泛化 |
| 隐藏变体 | 改变数据、单位、约束、噪声或缺失模式 | 主要泛化证据 |
| 独立新任务 | 保留能力结构但替换领域背景、数据和参数 | 主要未见任务证据 |

新任务必须由基准管理员创建、由独立审查者验证自洽性与难度后锁箱。构建者、提示词作者和业务作者不得读取隐藏内容。每个候选版本至少运行三次，以测量随机稳定性。

### 7.2 硬门槛

版本放行至少要求：

- 子问题、目标和硬约束完整覆盖；
- 关键主张能够追溯到数据、代码输出、推导、原始证据或明确假设；
- 关键结果可由评审员独立复算；
- 程序能从锁定环境在干净容器运行；
- 限制、不确定性和假设失效影响明确；
- 三项独立评审通过且无未解决 P0/P1；
- 无隔离、血缘、隐藏集或题目特化违规；
- 隐藏变体和未见任务无统计或实践意义上的明显退化；
- 成本、时延和人工介入未超过版本预算。

内部总分或阈值只用于版本比较和退化检测，不映射外部奖项或排名。

## 8. 状态、失败和恢复

完整状态和守卫见 `state_machine.yaml`。任何前进迁移必须由主 Agent 授权并记录输入输出清单哈希。

### 可恢复失败

超时、资源耗尽、暂时工具失败、无效 ResultPacket 或缺少声明工件进入 `FAILED_RECOVERABLE`。平台封存日志和部分清单，撤销凭据；最多重试三次，且必须创建新 `attempt_id`、新容器和新工作区。若提示词变化，必须产生新提示词版本。

### 安全失败

跨工作区访问、隐藏集泄漏、哈希错配、来源伪造、题目特化和未授权写入进入 `QUARANTINED`。立即终止容器、撤销凭据、隔离所有后代工件，并在任何重试前完成独立事件审查。

### 业务修订

任务或评测质量问题不复用为 Agent 本体修改状态，只能产生评估、当前答案 RevisionDecision、发布拒绝或回滚与报告。独立任务无关一般研究或直接用户治理已获准的本体修改才可从批准的影响阶段创建新尝试；保留的上游内容必须是已提升的不可变快照。

## 9. 平台实施验收

平台构建完成的最低证据：

1. 六个示例包分别通过对应 JSON Schema 正例校验，且越权、缺字段、坏哈希、路径穿越等反例被拒绝。
2. 两个并行 Agent 无法发现、读取或写入彼此工作区。
3. 作者容器终止后，评审容器在无作者缓存的情况下从清单复现结果。
4. 未经主 Agent 批准的暂存工件无法作为下游输入。
5. 修改已提升文件会导致哈希校验失败并隔离。
6. ReviewPacket 与任何 evaluation carrier 都无法路由到本体构建者；指定核心构建者只接收关闭的 `AgentBodyControlAuthorizationV1`。
7. 非基准管理员无法挂载隐藏基准库；主 Agent 只能读取匿名化报告。
8. 同一任务的超时重试产生不同容器、目录和 `attempt_id`，且保留完整事件链。
9. 三类盲审在结果提交前无法读取彼此输出。
10. 带题号、题名、特定参数、答案或任务专属条件的修改包被反特化门拒绝。

上述十项全部通过后，才允许启动其余角色的身份调研和端到端基准测试。

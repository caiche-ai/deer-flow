# tender-review 开发设计

## 1. 架构边界

`tender-review` 是招投标智能评审的领域层，与组价知识层 `ce-code` 平级。它可以复用 deer-flow 的模型、检索和 Agent 基础设施，但评审规则、项目数据、证据链和报告模型在本目录独立演进。

建议的数据流：

```text
项目文件
  → 文档解析/OCR
  → 章节、表格与附件结构化
  → 招标要求提取
  → 投标响应映射
  → 确定性规则 + 语义评审
  → 人工复核
  → 可审计报告
```

## 2. 当前代码

- `domain.py`：评审上下文、发现项、严重性、建议结论和报告。
- `pipeline.py`：按注册顺序执行规则，校验返回类型并汇总报告。
- `rules.py`：规则协议；后续规则实现应按业务域拆分子包。
- `document.py`：读取 MinerU `content_list.json`，按物理页生成稳定父证据块，并提供中文 BM25、逐页读取和证据回读。
- `retrieval.py`：把父证据块拆为 BGE 检索子块，使用 RRF 合并关键词和语义排名；同时通过 pgvector 查询带版本、日期、地区和草案过滤的公共知识库。
- `knowledge_ingestion.py`：发现三类公共知识库，按业务语义和文档结构切分 MinerU 产物，调用 BGE Embedding，并以可增量、可迁移的控制面契约写入 PostgreSQL/pgvector。
- `report.py`：校验八维审核覆盖、证据 ID 和风险发现，生成可审计 JSON/Markdown 报告。
- `review_team.py`：以代码注册四个文档级 `SubagentExecutor` 配置，前三个分域审核，第四个在第二波复核一致性与证据；子 Agent 只获得四个只读文档工具。
- `interaction.py`：大文档预检、Lead Agent 澄清状态、阶段进度、流式事件、任务持久化和问题处置契约。
- `agent.py`：通过 DeerFlow `create_deerflow_agent` 组装 Lead、五个原子审核工具和 `delegate_tender_review` 委派工具，并提供 CLI。
- `../backend/app/tender_review/service.py`：用户隔离的任务生命周期、Agent 后台执行、持久化状态恢复和 SSE 事件重放。
- `../backend/app/gateway/routers/tender_review.py`：任务、快照、续跑、问题处置、报告下载和 SSE HTTP 契约。
- `../agents/tender-review/`：代码托管、全用户可见的招投标智能体配置与 SOUL，不写入用户私有运行目录。
- `../frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/`：复用原有对话、附件、历史和流式消息界面；招投标入口绑定 `tender-review`，后端按 Agent 配置自动加载专用 Lead 提示词并关闭子智能体。
- `../skills/public/tender-document-review/`：公开审核 Skill，保存稳定方法、八维检查清单和输出/证据契约。

领域层不直接依赖数据库、HTTP 或大模型 SDK，便于单测和复用。外部能力通过适配器注入。

招标文件审核 Agent 属于应用适配层：`document.py` 和 `report.py` 保持纯 Python；只有构建 Agent/工具时才延迟导入 DeerFlow 和 LangChain。这样未安装 Agent 依赖时仍可运行领域单测。

### 公共知识库向量管线

知识库按业务用途划分为 `tender_templates`、`tender_regulations` 和 `qualification_standards`，排版格式只影响解析与切分策略。`knowledge_ingestion.py` 只选择各文档的 MinerU 主 `*_content_list.json` 产物，保存文档版本 SHA-256、章节路径、条款号、页码、bbox 和表格元数据。

当前向量契约为 `BAAI/bge-large-zh-v1.5`、1024 维、cosine、归一化，物理投影表为 `kb_vectors_bge_large_zh_v15_1024`。`kb_embedding_models` 和 `kb_chunk_embeddings` 不绑定 pgvector 的物理实现；迁移 Milvus 时保留文档、分块、模型版本、`vector_id` 和 outbox，只替换向量投影消费者。内容哈希未变化的分块必须复用已有向量，切分策略收缩时必须禁用多余分块及对应向量。

待审文件使用两层索引：`document.py` 的约 1800 字页级块是可审计父证据，`retrieval.py` 默认生成 420 字、60 字重叠的检索子块。首次语义查询时才批量生成子块向量，并在同一审核运行内复用；检索通过 RRF 融合 BM25 与 cosine 排名，只把父证据 ID 交给报告。Embedding 服务异常时自动降级为关键词检索，不允许影响精确证据回读。

公共知识库工具返回 `basis_id=kb:<knowledge-base-code>:<chunk-id>`。搜索结果不等于已核证依据：形成报告前必须用 `get_tender_knowledge_evidence` 回读，报告校验器只接受本次运行已经回读的 `basis_ids`。文件 `evidence_ids` 与知识库 `basis_ids` 分栏保存，外部知识不能替代待审文件证据。

### 文档级 Agent 团队

```text
Lead tender-review-agent
  ├─ 第一波：compliance-reviewer
  ├─ 第一波：evaluation-technical-reviewer
  ├─ 第一波：commercial-contract-reviewer
  └─ 第二波：consistency-evidence-reviewer
       ↓
Lead 补证、合并、调用 save_tender_review
```

`delegate_tender_review` 不走全局 `task` 工具，因为后者会按全局配置重新装配工具，无法继承当前 CLI 中绑定的 `MineruDocument`。委派工具仍使用 DeerFlow `SubagentExecutor`，但把当前文档的只读工具显式注入每个子 Agent；保存工具只留给 Lead。这个边界既复用框架的模型、Skill、超时和轮次控制，又避免使用进程级可变文档状态。

### 大文档交互状态

`ReviewInteractionStore` 在 `output_dir/_sessions/<thread-id>.json` 保存预检结果、审核口径、四阶段状态、事件、报告位置和用户问题处置。写入采用同进程锁和临时文件替换，任务同时绑定 `document_id` 与 MinerU 产物 SHA-256，防止同一 thread/task 串到另一份文件。

原生聊天的 `UploadsMiddleware` 在每轮保存新旧附件的完整校验路径；通用 `task` 工具把这些路径注入隔离子 Agent 的任务提示，因此经过 `ask_clarification` 的后续轮次仍能读取 `/mnt/user-data/uploads/*.md`，而不会退回默认 workspace 查找。

DeerFlow 工具契约：

- `start_tender_review`：预检并建立审核口径；所有新任务强制暂停，Lead 再通过 `ask_clarification` 展示拟定口径。
- `resume_tender_review`：Lead 收到用户自然语言回复后解释确认结果与字段更新，再解除口径闸；后端不代替 Agent 解析决定。
- `get_tender_review_state`：读取预检、口径、阶段进度、报告和问题动作。
- `record_tender_issue_action`：报告后记录确认、误报、暂缓、指派或重审请求，不覆盖原报告。

`delegate_tender_review` 自动维护阶段状态并发出 `review_stage_started`、`review_stage_completed`、`review_stage_failed` custom events。第二波复核在状态层硬性依赖三个第一波阶段完成；`save_tender_review` 也在写文件前检查四阶段全部完成。

Gateway 将内部事件映射为 `review.*` 公共事件，使用 `sequence` 做至少一次投递下的前端去重。Lead 的 `ask_clarification` ToolMessage 会同步写入任务快照；REST `/resume` 只追加用户消息，用户决定由同一 Agent 线程解释。任务状态记录源文档绝对路径供服务恢复，但 API 返回前会转换为 `data/parsed` 相对路径，报告物理路径也只转换为受鉴权下载 URL。

仓库内置 Agent 由 `deerflow.config.agents_config` 从运行目录相邻的 `agents/` 发现，解析优先级为用户私有、仓库内置、旧版共享目录。内置 Agent 不获得 `update_agent` 工具，避免对话运行把代码托管的人格和配置复制/覆盖到用户目录。

## 3. 关键契约

每条 `ReviewFinding` 必须具备：

- 在单次运行内唯一的 `finding_id`；
- 稳定的 `criterion_id`，指向项目评审项；
- 五类之一的 `scope`；
- 明确的 `severity`；
- 对人可读的标题和说明；
- 至少一个 `evidence_ref`，能够回到页码、章节、表格单元格或结构化字段；
- 建议人工采取的动作。

自动汇总口径：

- 存在 `blocker` → 建议 `reject`；
- 否则存在 `major` → 建议 `manual_review`；
- 只有 `minor`/`info` 或无发现 → 建议 `pass`。

上述值是系统建议，不是最终评审决定。

招标文件发布前审核另使用八维覆盖契约：

- `basic_information`、`schedule`、`qualification`、`rejection_clauses`、`evaluation_method`、`technical`、`commercial_price`、`contract` 必须各有且只有一条覆盖记录；
- 覆盖记录和风险发现都必须引用已加载文档中真实存在的 `document:page=N:chunk=M`；
- 每条正式引用在报告形成前必须经 `get_tender_evidence` 回读；
- 报告持久化时将已回读的文件证据和知识库依据展开为 `issue_evidence`、`legal_basis`，保留页码、原文、块区间、bbox、章节和条款；
- 原生聊天最终答复使用 `tender-review` JSON 代码块传递同一结构，前端隐藏机器块并渲染可点击结论卡片；
- 报告风险等级由校验层根据发现严重性计算，不接受模型直接指定；
- 法规检索未返回已回读依据，或版本、地域和效力仍不明确时，模型不得对疑似违法违规问题作法律定性。

## 4. 数据目录

| 目录 | 内容 | Git 策略 |
|---|---|---|
| `data/raw/` | 招投标原始文件 | 不入 Git，仅保留说明与占位文件 |
| `data/parsed/` | OCR、版面和文档解析产物 | 默认不入 Git，样例需脱敏后单独评审 |
| `data/structured/` | 要求、响应、证据索引 | 默认不入 Git，schema/脱敏 fixture 可入 Git |
| `data/review_results/` | 运行报告和人工操作记录 | 不入 Git，测试金标另设受控目录 |

任何真实投标文件都可能含商业秘密或个人信息，不得提交到代码仓库。

## 5. 扩展约定

- 确定性规则实现 `ReviewRule` 协议，规则 ID 和版本进入运行清单。
- 模型输出先做 schema 校验、证据校验和置信度分流，再转换为领域对象。
- 文档中的页码、章节、bbox、表格行列等定位信息使用稳定证据 ID 关联，避免只保存摘录文本。
- 项目规则不得用全局默认值静默覆盖；缺少评审依据时返回待人工处理。
- 金标集按评审维度分层，区分字段抽取、要求映射、规则判断和报告生成，不用端到端单一分数掩盖问题。

## 6. 开发命令

所有命令从 `tender-review/` 执行：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

运行知识库入库还需安装 `knowledge` extra：

```bash
uv sync --extra knowledge --extra dev
uv run python -m tender_review.knowledge_ingestion --help
```

运行 DeerFlow Agent 需额外安装 `agent` extra：

```bash
uv sync --extra agent --extra knowledge --extra dev
uv run python -m tender_review.agent --parsed <content_list.json> --thinking
```

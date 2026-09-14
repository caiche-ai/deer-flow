# tender-review

招投标智能评审域项目，与 `ce-code/`、`backend/` 平级。

新同事接手前请先阅读 [招投标功能交接文档](HANDOVER.md)，其中区分了当前前端 Lead-only 链路与仍保留子 Agent 的独立审核引擎。

只交接 MinerU、知识库入库与 PostgreSQL/pgvector 连接时，直接阅读 [MINERU_PG_HANDOVER.md](MINERU_PG_HANDOVER.md)。

本项目负责把招标文件、投标文件和项目评审办法转换为可执行的评审任务，输出带原文证据、规则依据和人工复核状态的评审报告。系统只提供辅助评审结论，最终定标或否决决定由有权限的评审人员作出。

## 当前能力

- 定义资格、符合性、技术、商务和报价五类评审范围。
- 以可插拔规则组成评审流水线。
- 每条问题强制关联证据位置，避免无依据结论。
- 按问题严重性生成 `pass`、`manual_review` 或 `reject` 建议。
- 基于 DeerFlow `create_deerflow_agent` 提供招标文件发布前审核 Agent。
- 基于 DeerFlow `SubagentExecutor` 编排三类并行专业审核与一类二次一致性复核。
- 提供 `tender-document-review` 公开 Skill，统一检查清单、证据契约与法律边界。
- 为大文档提供持久化预检、统一的 Lead Agent 审核口径澄清、逐阶段进度、流式事件和问题处置状态。
- 对 MinerU 解析产物做中文全文检索、逐页回读与稳定证据引用。
- 八个审核维度未全部覆盖或引用了不存在的证据时，拒绝生成正式报告。
- 通过 DeerFlow Gateway 提供用户隔离的任务、快照、续跑、报告与问题处置 API。
- 通过统一、可重放的 SSE 事件流向前端推送预检、HITL 和四阶段审核状态。
- 提供与 DeerFlow 原有聊天一致的“招投标智能体”界面及纯 Python/TypeScript 单元测试。

当前已具备领域骨架、MinerU 批量解析工具、三类公共知识库向量检索、命令行 Agent、业务 API 和审核工作台；投标响应映射尚未接入 Agent，详见 [TODO.md](TODO.md)。

## 文档解析

`scripts/parse_documents.py` 通过 MinerU API 批量解析 PDF 和 Office 文档，保留源目录结构，支持最多三并发、中断续跑、旧版 DOC/XLS 转换映射及增量 manifest：

```bash
python scripts/parse_documents.py --source-root data/招投标 --output-root data/parsed --converted-root /tmp/tender-review-converted --workers 3
```

批处理完成后，可生成逐文档耗时、页数、块数、表格和图片统计：

```bash
python scripts/summarize_parse_results.py --manifest data/parsed/manifest.json --output-prefix data/parsed/document_stats
```

## 知识库 Embedding

`knowledge_ingestion.py` 从 MinerU 的 `*_content_list.json` 产物发现并处理三类公共知识库：招标文件范本、政策法规和资质标准。切分优先保留标题、法规条款、页码、bbox 与表格上下文，而不是按文档排版格式分别建库。程序使用稳定 ID 和内容哈希，可重复执行并只重算发生变化的分块。

136 服务器当前提供 `BAAI/bge-large-zh-v1.5`，OpenAI 兼容端点为 `http://127.0.0.1:8097/v1/embeddings`，输出 1024 维归一化向量。首次安装依赖并执行：

```bash
uv sync --extra knowledge
export DATABASE_URL='postgresql://<user>:<password>@127.0.0.1:5433/deerflow'
uv run python -m tender_review.knowledge_ingestion --parsed-root /home/caic/code/calvin/code/deer-flow/tender-review/data/parsed --embedding-url http://127.0.0.1:8097 --embedding-model /model --model-name BAAI/bge-large-zh-v1.5
```

向量写入 PostgreSQL/pgvector 表 `kb_vectors_bge_large_zh_v15_1024`，控制面状态保存在 `kb_chunk_embeddings`、`kb_ingestion_jobs` 和 `kb_vector_outbox`。可先加 `--dry-run` 检查文档路由和分块数，也可用 `--category 政策法规` 等中文目录名参数单独更新一个知识库。当前基线为 33 份文档、4,363 个有效分块/向量。

## 招标文件审核 Agent

Agent 使用 DeerFlow 的模型工厂、Agent 工厂、`SubagentExecutor`、Skill 加载、工具调用和循环保护能力。它不会把数百页全文一次性塞给模型：页级父块保留完整证据上下文，约 420 字的重叠子块使用关键词+BGE 混合召回，命中后仍返回稳定的父块 `evidence_id`。Agent 先读取目录，再按两波审核：第一波由 `compliance-reviewer`、`evaluation-technical-reviewer`、`commercial-contract-reviewer` 分工审核八个维度；第二波由 `consistency-evidence-reviewer` 重新回读原文，复核跨章节一致性、候选证据、重复项和严重性。只有 Lead 可以调用 `save_tender_review` 生成正式报告。

四个子 Agent 共享当前文档绑定的只读工具，不使用进程级“当前文档”变量，因此多文档运行不会串上下文。公共知识库通过 `search_tender_knowledge` 检索，通过 `get_tender_knowledge_evidence` 回读并把 `basis_id` 写入报告；查询只使用当前文档版本和有效分块，过滤征求意见稿/草案，并支持生效日期与地区元数据过滤。公开 Skill 位于 `skills/public/tender-document-review/`，已在 `extensions_config.json` 启用。

### 大文档交互

Lead 在委派前调用 `start_tender_review`。系统完成页数、证据块、目录和四阶段计划预检后，所有新任务都强制进入 `awaiting_input`；Lead 随即调用 `ask_clarification`，一次性向用户展示并确认审核目标、地区、采购制度/项目类型、是否对照法规和内部规则。后端不再解析 `confirmed/profile_updates`，只把用户自然语言回复放回同一 Agent 线程；Lead 解释回复并调用 `resume_tender_review`，信息仍不足时继续澄清。

每个子 Agent 开始、完成或失败时，系统同步写入持久化状态，并发送 DeerFlow custom stream event。用户询问进度时调用 `get_tender_review_state`；报告生成后可用 `record_tender_issue_action` 记录 `confirmed`、`false_positive`、`deferred`、`assigned` 或 `recheck_requested`。状态文件位于输出目录的 `_sessions/<thread-id>.json`，绑定文档 SHA-256；同一任务 ID 不能换文件复用。

### 招投标智能体对话界面

登录 DeerFlow 后点击侧栏“招投标智能体”，或访问 `/workspace/agents/tender-review/chats/new`。界面完全复用原有对话页，支持附件上传、历史会话、停止生成、模型选择、产物和 token 统计。上传招标 PDF 并用自然语言提出审核要求即可；该内置 Agent 通过 `agents/tender-review/config.yaml` 自动选择招投标专用 Lead 提示词、关闭子智能体，并只加载 `tender-document-review` Skill。旧入口 `/workspace/tender-review` 会重定向到新对话。

PDF 上传请求会携带 `agent_name=tender-review`。Gateway 在提交对话前调用 MinerU，在线程上传目录生成同名的页级 Markdown 和 `_content_list.json`，并把 Markdown 虚拟路径交给 `grep`/`read_file`，因此智能体不会尝试直接用文本工具读取 PDF。返回的上传元数据包含解析器、页数和耗时。可用 `TENDER_REVIEW_MINERU_API_URL`、`TENDER_REVIEW_MINERU_BACKEND`、`TENDER_REVIEW_MINERU_TIMEOUT` 覆盖默认服务参数；Office 文件仍遵循全局 `uploads.auto_convert_documents` 配置。

澄清后的审核仍会继承线程中经过校验的附件路径，包括历史上传的 Markdown 主文本。Lead Agent 必须从 `/mnt/user-data/uploads` 读取这些路径，不能因默认 workspace 为空而判定文件缺失，并由 Lead 生成正式报告。

最终审核答复附带经过校验的 `tender-review` 结构化结果。前端将每条发现渲染为独立卡片，分别展示招标文件原文和法规/规范依据；点击问题证据会在右侧阅读器打开原 PDF 并跳到物理页码，点击外部依据则展示回读原文及真实来源链接。专用审核引擎保存的 JSON/Markdown 报告也会展开 evidence ID 和 basis ID，保留页码、块位置、bbox、条款及逐字原文。

由于上传响应会等待 MinerU 完成，Next.js 内部 rewrite 代理默认超时提高为一小时；可通过 `DEER_FLOW_PROXY_TIMEOUT_MS` 调整。该值应大于 `TENDER_REVIEW_MINERU_TIMEOUT` 或至少覆盖预期的最大单文档解析耗时，否则解析产物可能已生成，但浏览器会因代理先断开而显示上传失败。

内置 Agent 定义位于仓库 `agents/tender-review/`，所有用户只读共享，不依赖前端生成的用户私有 Agent。底层任务 API 仍保留：`POST /tasks` 使用 `document_path + request` 启动 Lead，`POST /tasks/{task_id}/resume` 使用 `message` 原样转发澄清回复，其余 REST 负责快照、处置和下载；`GET /api/tender-review/tasks/{task_id}/events` 提供带单调 `sequence` 的可重放 SSE，供后续对话内结构化进度卡片使用。

Gateway 通过登录用户 ID 隔离 `data/review_results/api/users/<user-hash>/<task-id>`，且只允许选择 `data/parsed` 内的 MinerU 主产物和下载当前任务目录内的报告。

首次安装 Agent 依赖：

```bash
uv sync --extra agent --extra knowledge --extra dev
```

审核已经由 MinerU 解析的原始文件：

```bash
uv run python -m tender_review.agent --document "data/招投标/招标文件/招标文件正文 (1).pdf" --parsed-root data/parsed --output-dir data/review_results --thinking
```

也可以直接指定解析产物：

```bash
uv run python -m tender_review.agent --parsed "data/parsed/招标文件/招标文件正文 (1)/hybrid_auto/招标文件正文 (1)_content_list.json" --output-dir data/review_results --thinking
```

成功后生成同名的 `*_tender_review.json` 和 `*_tender_review.md`。报告中的文件证据 ID 形如 `招标文件正文_1:page=12:chunk=2`，知识库依据 ID 形如 `kb:tender_regulations:<chunk-id>`。待审文件采用父子分块的 BM25+BGE 混合检索，公共知识库使用 pgvector 语义检索；BGE 不可用时待审文件自动降级为 BM25，文件证据回读和报告覆盖闸不受影响。后续迁移 Milvus 时可保持控制面和工具契约不变。

注意：文件证据证明“待审文件写了什么”，知识库依据用于辅助对照。即使检索到法规，也必须确认版本、地域、采购制度和效力状态；疑似违法、限制竞争等问题仍只标记人工复核风险，不作最终法律定性。

## 目录

```text
tender-review/
├── data/                       # 原始文件、解析产物、结构化数据和评审结果
├── src/tender_review/          # 领域模型、证据检索、报告校验与 DeerFlow Agent 团队
├── tests/                      # 单元测试
├── PRD.md                      # 产品范围与验收原则
├── DEV.md                      # 技术设计与开发约定
└── TODO.md                     # 迭代计划
```

## 本地验证

要求 Python 3.12 和 uv。首次进入本目录后执行：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

按仓库约定，本地不提交 `uv.lock`；锁文件由实际部署环境统一维护。

## 最小用法

```python
from tender_review import ReviewContext, ReviewPipeline

context = ReviewContext(
    project_id="demo-project",
    tender_document_id="tender-v1",
    bid_document_id="bidder-a-v1",
)
report = ReviewPipeline([]).run(context)
assert report.recommendation.value == "pass"
```

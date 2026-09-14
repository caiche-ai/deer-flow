# 招投标功能交接文档

- 最后核对日期：2026-09-14
- 目标环境：136 服务器
- 仓库目录：`/mnt/nvme/calvin/code/deer-flow`
- 当前运行方式：DeerFlow 前后端均以开发模式运行，不使用 DeerFlow Docker Compose
- 主要用户入口：`/workspace/agents/tender-review/chats/new`

## 1. 先看结论

当前面向用户的“招投标智能体”建立在 DeerFlow 原生聊天链路上。进入专用页面后，前端把 `agent_name=tender-review` 带到 Gateway，后端自动加载仓库内置 Agent 配置、招投标专用 Lead 提示词和两个 Skill。该链路明确采用 **Lead-only** 模式，`subagent_enabled: false` 是后端强制策略，前端参数不能重新开启子智能体。

当前已提供的主要能力：

1. 招标 PDF 上传后自动调用 MinerU，生成带物理页标记的 Markdown 和 `content_list.json`。
2. Lead 按八个维度完成发布前审核：基本信息、时间安排、资格条件、否决条款、评标办法、技术要求、商务报价、合同条款。
3. 对文件本身做完整性、一致性、公平竞争、评审可执行性及合同风险检查。
4. 用户要求与范本比较时，按“功能单元”识别缺章、实质删改、占位符、附件缺失和流程断裂等重大差异。
5. 最终答复中的 `tender-review` JSON 会由前端解析成问题卡片，并支持打开原文件证据。
6. 另有一套独立的 `tender-review` Python 审核引擎、CLI 和 REST/SSE API，具备混合检索、知识库和四个 reviewer 编排能力；它不是当前聊天页面的主执行链路。

系统只输出辅助审核意见，不代替招标人、评标委员会或法律专业人员作出废标、定标、授标或法律定性。

## 2. 两条执行链路必须区分

### 2.1 原生聊天链路（当前前端入口）

```text
侧栏“招投标智能体”
  -> /workspace/agents/tender-review/chats/<thread-id>
  -> 对话上下文携带 agent_name=tender-review
  -> PDF 上传接口按 agent_name 触发 MinerU
  -> PDF + 同名 Markdown + content_list.json 同步到线程沙箱
  -> Gateway 创建 Lead Agent
  -> 加载 agents/tender-review/{config.yaml,lead_agent.yaml,SOUL.md}
  -> 加载两个公开 Skill 并按 allowed-tools 收窄工具集合
  -> Lead 使用文件工具分段检索、回读、核证和生成报告
  -> 前端解析 tender-review JSON，渲染问题与证据卡片
```

这条链路的关键特点：

- 只有 Lead，不调用全局 `task` 工具，也不委派子智能体。
- 上传附件是主要输入；Lead 从 `/mnt/user-data/uploads` 读取 MinerU Markdown。
- 正式附件输出应写到 `/mnt/user-data/outputs`，再通过 `present_files` 展示。
- Skill 加载属于提示词注入和工具权限控制，前端不一定显示独立的“Skill 调用”图标；实际工具调用仍按具体工具显示。
- Skill 的 `allowed-tools` **不会注册工具**，只会从当前已注册工具中做白名单过滤。
- `inspect_tender_document`、`search_tender_document`、`get_tender_evidence` 等专用工具目前只在独立 Python Agent 中构建；原生聊天没有自动获得这些工具时，会使用 `ls`、`glob`、`grep`、`read_file`、`write_file` 等文件工具完成审核。

### 2.2 独立审核引擎（CLI/REST POC）

```text
MinerU content_list.json
  -> MineruDocument 页级父证据
  -> 420 字检索子块
  -> BM25 + BGE + RRF 混合召回
  -> 第一波三个专业 reviewer
  -> 第二波 consistency-evidence-reviewer
  -> Lead 补证、去重、覆盖校验并保存 JSON/Markdown
  -> REST 任务状态 / SSE / 问题处置 / 报告下载
```

这条链路仍包含四个文档级 reviewer：

- `compliance-reviewer`
- `evaluation-technical-reviewer`
- `commercial-contract-reviewer`
- `consistency-evidence-reviewer`

它们由 `tender-review/src/tender_review/review_team.py` 中的 `SubagentExecutor` 驱动。若产品要求所有入口都严格 Lead-only，不要直接把当前前端切到这套 API；应先重构该引擎的编排方式。

## 3. 代码位置与职责

| 路径 | 职责 |
|---|---|
| `agents/tender-review/config.yaml` | 内置 Agent 名称、专用提示词路径、子智能体策略和 Skill 列表 |
| `agents/tender-review/lead_agent.yaml` | 招投标 Lead 的完整系统提示词，定义澄清、八维审核、证据和输出要求 |
| `agents/tender-review/SOUL.md` | Agent 角色、边界和对话行为补充 |
| `skills/public/tender-document-review/` | 通用招标文件发布前审核方法、八维检查清单和前端输出契约 |
| `skills/public/tender-template-completeness/` | 范本完整性检查方法、重大差异判定和输出契约 |
| `extensions_config.json` | 两个公开 Skill 的全局启用开关 |
| `backend/packages/harness/deerflow/config/agents_config.py` | 内置/用户 Agent 发现与 `system_prompt_path`、`subagent_enabled` 配置模型 |
| `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | 创建 Lead；应用命名 Agent 的强制子智能体策略和 Skill 工具过滤 |
| `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` | 按 Agent 解析专用提示词；相对路径从仓库根目录解析 |
| `backend/packages/harness/deerflow/tools/builtins/update_agent_tool.py` | 更新 Agent 时保留运维管理的提示词和子智能体字段 |
| `backend/app/gateway/routers/uploads.py` | 上传入口；仅当 `agent_name=tender-review` 且文件为 PDF 时触发 MinerU |
| `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py` | 在多轮对话中保留并校验历史上传路径 |
| `tender-review/src/tender_review/mineru.py` | MinerU HTTP 客户端及页级 Markdown 生成 |
| `frontend/src/components/workspace/workspace-nav-chat-list.tsx` | 侧栏“招投标智能体”入口 |
| `frontend/src/app/workspace/tender-review/page.tsx` | 旧入口重定向到 Agent 聊天页 |
| `frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx` | 原生 Agent 对话页面，透传 `agent_name` |
| `frontend/src/core/agents/display.ts` | `tender-review` 的中文展示名 |
| `frontend/src/core/tender-review/findings.ts` | `tender-review` JSON 的严格解析和安全字段过滤 |
| `frontend/src/components/workspace/messages/tender-review-findings.tsx` | 风险卡片、文件证据和外部依据展示 |
| `tender-review/src/tender_review/` | 独立领域模型、文档检索、知识入库、Agent 团队、报告和交互状态 |
| `backend/app/tender_review/service.py` | 用户隔离的独立审核任务生命周期和事件重放 |
| `backend/app/gateway/routers/tender_review.py` | 独立审核任务 REST/SSE API |
| `tender-review/tests/` | 独立审核引擎单元测试 |
| `backend/tests/test_tender_review_service.py` | Gateway 任务服务测试 |

## 4. Agent 自动启用机制

前端侧栏链接固定指向：

```text
/workspace/agents/tender-review/chats/new
```

发送消息及上传文件时都会携带 `agent_name=tender-review`。Gateway 加载 Agent 配置时的查找顺序是：

1. 当前用户私有目录：`.deer-flow/users/<user-id>/agents/tender-review/`
2. 仓库内置目录：`agents/tender-review/`
3. 旧版共享目录：`.deer-flow/agents/tender-review/`

因此，如果某个用户曾创建同名私有 Agent，它会遮蔽仓库内置配置。出现“同一页面在不同账号表现不同”时，首先检查这个目录优先级，不要直接修改全局提示词。

当前仓库配置的核心内容是：

```yaml
name: tender-review
system_prompt_path: agents/tender-review/lead_agent.yaml
subagent_enabled: false
skills:
  - tender-document-review
  - tender-template-completeness
```

注意事项：

- `subagent_enabled: false` 会覆盖请求端传入值，保证前端不能重新打开子智能体。
- `model` 未在 Agent 配置中指定，使用前端请求模型或全局默认模型。
- 内置 Agent 不获得 `update_agent` 工具，防止对话修改仓库托管配置。
- 两个 Skill 还必须在 `extensions_config.json` 中保持 `enabled: true`。
- 修改 Agent 配置、提示词或 Skill 后建议重启 Gateway，以清除进程内提示词/Skill 缓存。

## 5. 审核规则与输出契约

### 5.1 标准招标文件审核

Lead 首先确认附件和解析产物，然后一次性向用户确认：

1. 审核目标和文件版本；
2. 适用地区；
3. 采购制度或项目类型；
4. 是否对照法规、范本和内部规则；
5. 用户指定的重点和排除范围。

正式审核覆盖以下八维，且每个维度至少使用两组关键词检索并扩读上下文：

- `basic_information`
- `schedule`
- `qualification`
- `rejection_clauses`
- `evaluation_method`
- `technical`
- `commercial_price`
- `contract`

每条问题必须有待审文件原文。矛盾问题必须同时取得冲突两侧证据；法规或范本依据不能代替文件证据。证据不足时使用 `insufficient_evidence` 或写入 `limitations`，不得默认为通过。

### 5.2 范本完整性检查

只有用户明确要求范本比较、完整性检查或重大差异排查时，才同时加载 `tender-template-completeness`。

基线优先级是：用户明确指定/上传的范本 > 同地区、同制度、同项目类型且有效的权威范本 > 用户确认的近似范本。没有可靠范本时必须返回 `baseline_missing`，不能声称“无重大差异”。

比较基于功能单元而非逐字 diff，至少覆盖公告与邀请、投标人须知、资格审查、投标格式、评标办法、合同、技术需求、报价清单和附件。主报告只列 `blocker`、`major`；每个重大差异必须包含范本证据、待审文件证据或缺失检索记录、具体影响和修改建议。

### 5.3 前端结构化结果

最终可读答复后必须追加且只需追加一个合法代码块：

````markdown
```tender-review
{
  "version": 1,
  "summary": "...",
  "overall_risk": "low|medium|high",
  "source_document": {
    "filename": "xxx.pdf",
    "path": "/mnt/user-data/uploads/xxx.pdf"
  },
  "findings": [
    {
      "finding_id": "F-001",
      "dimension": "qualification",
      "severity": "info|minor|major|blocker",
      "title": "...",
      "issue": "...",
      "recommendation": "...",
      "issue_evidence": [
        {
          "document_name": "xxx.pdf",
          "path": "/mnt/user-data/uploads/xxx.pdf",
          "page": 12,
          "section": "...",
          "quote": "逐字原文"
        }
      ],
      "legal_basis": []
    }
  ],
  "limitations": []
}
```
````

前端解析器采用失败关闭策略：任一 finding 缺少必填字段、严重性非法或 `issue_evidence` 为空，整个代码块都不会渲染成卡片。文件路径只接受 `/mnt/user-data/` 前缀，外部链接只接受 HTTP/HTTPS。

## 6. PDF 上传与 MinerU

原生聊天上传 PDF 时，`backend/app/gateway/routers/uploads.py` 会同步等待 MinerU 完成。默认配置：

- API：`http://172.19.2.2:18000/mineru/file_parse`
- Backend：`hybrid-auto-engine`
- 读取超时：3600 秒
- 可覆盖变量：`TENDER_REVIEW_MINERU_API_URL`、`TENDER_REVIEW_MINERU_BACKEND`、`TENDER_REVIEW_MINERU_TIMEOUT`

成功后在线程上传目录生成：

- 原始 `xxx.pdf`
- 同名页级 `xxx.md`
- `xxx_content_list.json`

Markdown 使用 `<!-- page: N -->` 和 `## 第 N 页` 标记物理页。Lead 应先定位并读取 Markdown，不能直接用文本工具读 PDF，也不能在未检查解析产物前报告“PDF 无法解析”。

上传请求可能持续数十分钟。前端 `DEER_FLOW_PROXY_TIMEOUT_MS` 必须大于 MinerU 超时或至少覆盖最大文件耗时，否则服务端可能已经解析成功，但浏览器显示上传失败。

## 7. 独立审核引擎和知识库

独立引擎的主要模块：

- `document.py`：MinerU 文件、页级父证据、中文 BM25、逐页和证据回读。
- `retrieval.py`：子块、BGE、RRF、pgvector 知识检索。
- `knowledge_ingestion.py`：范本、法规、资质标准三类公共知识库入库。
- `review_team.py`：三类专业审核加第二波一致性复核。
- `interaction.py`：预检、澄清、阶段事件、快照和人工处置。
- `report.py`：八维覆盖、证据引用、风险等级和报告持久化校验。
- `agent.py`：专用工具注册、Lead 组装和 CLI。

当前知识库约定：

- Embedding 服务：`http://127.0.0.1:8097/v1/embeddings`
- 模型：`BAAI/bge-large-zh-v1.5`
- 维度：1024，归一化 cosine
- 向量表：`kb_vectors_bge_large_zh_v15_1024`
- 控制表：`kb_chunk_embeddings`、`kb_ingestion_jobs`、`kb_vector_outbox`
- 知识库分类：`tender_templates`、`tender_regulations`、`qualification_standards`

独立 REST API 均要求登录鉴权：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/tender-review/documents` | 可审核解析文档列表 |
| `POST` | `/api/tender-review/tasks` | 创建异步审核任务 |
| `GET` | `/api/tender-review/tasks/{task_id}` | 查询任务和快照 |
| `POST` | `/api/tender-review/tasks/{task_id}/resume` | 原样提交澄清回复并续跑 |
| `GET` | `/api/tender-review/tasks/{task_id}/events` | 可重放 SSE，支持 `Last-Event-ID` |
| `POST` | `/api/tender-review/tasks/{task_id}/findings/{finding_id}/action` | 记录确认、误报、暂缓、指派或重审 |
| `GET` | `/api/tender-review/tasks/{task_id}/report?format=json|markdown` | 下载报告 |

任务数据按登录用户隔离在 `tender-review/data/review_results/api/users/<user-hash>/<task-id>`。真实招投标材料、解析结果和报告均不得提交到 Git。

## 8. 136 开发方式启动

### 8.1 依赖关系

- Gateway：`8001`
- Frontend：`2026`
- PostgreSQL：现有 `postgres` 容器映射到主机 `5433`，数据库名 `deerflow`
- BGE：`8097`
- MinerU：`172.19.2.2:18000`
- `8000` 是服务器上的其他服务，不要停止或占用

DeerFlow 自身不通过 Docker 启动，但当前 PostgreSQL 复用了服务器已有容器。不得重建或删除该容器。

### 8.2 启动后端

根目录 `.env` 保存模型和频道相关变量，不要在终端、日志或文档中打印其值。`DATABASE_URL` 当前需从已有 PostgreSQL 容器安全注入：

```bash
cd /mnt/nvme/calvin/code/deer-flow
DB_USER="$(docker exec postgres printenv POSTGRES_USER)"
DB_PASS="$(docker exec postgres printenv POSTGRES_PASSWORD)"
export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5433/deerflow"
cd backend
setsid -f .venv/bin/python -m uvicorn app.gateway.app:app \
  --host 0.0.0.0 --port 8001 --env-file ../.env \
  >> tender-review-gateway.log 2>&1 </dev/null
```

当前数据库密码已确认可直接放入 URL；密码策略变化后如包含 `@`、`:`、`/` 等保留字符，需要先做 URL 编码。

### 8.3 启动前端

```bash
cd /mnt/nvme/calvin/code/deer-flow/frontend
IFS= read -r BETTER_AUTH_SECRET < ../backend/.deer-flow/.better-auth-secret
export BETTER_AUTH_SECRET
setsid -f pnpm exec next dev --turbo --hostname 0.0.0.0 --port 2026 \
  >> attachment-reader-frontend.log 2>&1 </dev/null
```

启动顺序建议先 Gateway、后 Frontend。重启前先用下面的命令确认 PID 和工作目录，再只终止目标 PID；不要使用宽泛的 `pkill python` 或 `pkill node`：

```bash
ss -ltnp | grep -E ':(8001|2026)[[:space:]]'
readlink -f /proc/<pid>/cwd
kill -TERM <pid>
```

### 8.4 健康检查

```bash
curl -fsS http://127.0.0.1:8001/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2026/login
tail -100 backend/tender-review-gateway.log
tail -100 frontend/attachment-reader-frontend.log
```

预期 Gateway 返回 `{"status":"healthy","service":"deer-flow-gateway"}`，登录页返回 200。未登录访问 Agent/API 返回 401 或跳转登录是正常行为。

## 9. 验证与验收

### 9.1 后端和领域测试

```bash
cd /mnt/nvme/calvin/code/deer-flow/backend
uv run pytest \
  tests/test_custom_agent.py \
  tests/test_lead_agent_prompt.py \
  tests/test_lead_agent_skills.py \
  tests/test_uploads_router.py \
  tests/test_tender_review_service.py
uv run ruff check \
  packages/harness/deerflow/config/agents_config.py \
  packages/harness/deerflow/agents/lead_agent/agent.py \
  packages/harness/deerflow/agents/lead_agent/prompt.py

cd ../tender-review
uv run pytest
uv run ruff check .
```

### 9.2 前端测试

```bash
cd /mnt/nvme/calvin/code/deer-flow/frontend
pnpm typecheck
pnpm prettier --check \
  'src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx' \
  src/core/tender-review/findings.ts \
  src/components/workspace/messages/tender-review-findings.tsx
```

### 9.3 手工验收

1. 登录后点击侧栏“招投标智能体”，确认进入 `/workspace/agents/tender-review/chats/new`。
2. 上传一份 PDF，确认响应中有 `parser=mineru`、页数、Markdown 和 `content_list` 元数据。
3. 发起审核，确认 Lead 在正式结论前一次性询问地区、制度、审核目标和依据范围。
4. 确认运行中没有 `task` 子智能体调用；文件工具调用可正常显示。
5. 确认最终消息的机器 JSON 被隐藏并渲染成风险卡片。
6. 点击文件证据，确认右侧阅读器打开原 PDF 并跳到对应物理页。
7. 发起“与范本比较”的任务：有明确范本时只报告重大差异；没有可靠范本时必须返回 `baseline_missing`。
8. 直接请求 `/api/tender-review/...` 时确认未登录为 401，登录用户之间不能读取对方任务。

## 10. 常见问题

| 现象 | 首查项 | 处理建议 |
|---|---|---|
| Gateway 启动时报 `DATABASE_URL not found` | 当前 shell 是否导出 `DATABASE_URL` | 按 8.2 从现有 `postgres` 容器注入，不要把密码写入 Git |
| 前端日志出现 `ECONNREFUSED 127.0.0.1:8001` | Gateway 是否先启动并健康 | 恢复 8001 后刷新页面；启动期间的单次旧日志可忽略 |
| 上传长时间后浏览器失败 | MinerU 状态、Gateway 日志、代理超时 | 保证 `DEER_FLOW_PROXY_TIMEOUT_MS` 覆盖 MinerU 最长耗时 |
| PDF 上传后 Agent 说不能读取 | 是否生成同名 `.md` 和 `content_list.json` | 检查上传元数据与沙箱同步，让 Lead 读取 Markdown |
| 专用提示词未生效 | 私有同名 Agent、路径、YAML 的 `system_prompt` 字段 | 检查 Agent 查找优先级，修正后重启 Gateway |
| 仍出现子智能体调用 | 实际加载的 Agent 配置与日志中的 `subagent_enabled` | 确认没有私有同名配置遮蔽，并确保值为 `false` |
| Skill 看得到但工具不可用 | 是否把 `allowed-tools` 当成工具注册 | 在 Gateway 工具注册层挂载实际工具；Skill 只能过滤已有工具 |
| 新 Skill 没有加载 | Agent `skills`、`extensions_config.json`、SKILL frontmatter | 三处同时检查，之后重启 Gateway |
| 风险卡片不显示 | JSON fence、version、finding 字段和证据 | 对照 `frontend/src/core/tender-review/findings.ts`；任一非法 finding 会使整块失败 |
| 证据无法跳页 | path、page、quote 是否齐全 | path 必须在 `/mnt/user-data/` 下，page 是大于 0 的 PDF 物理页码 |
| 日志出现微信二维码过期 | `channels.wechat` 的扫码状态 | 与招投标审核核心链路无关；需要微信渠道时重新扫码 |

## 11. 已知限制与技术债

1. 原生聊天和独立审核引擎目前是两条并存链路。前端 Lead-only 尚未直接复用独立引擎的 BM25+BGE、稳定 `evidence_id`、持久化阶段状态和专用报告校验工具。
2. 独立引擎仍使用四个子 Agent，不符合“所有入口只保留 Lead”的统一目标。
3. `tender-review/README.md` 的一处描述仍只写了 `tender-document-review`，实际配置已经加载两个 Skill，应以后者为准并修正文档。
4. `tender-review/README.md`/`DEV.md` 提到 `uv sync --extra knowledge`，但当前 `pyproject.toml` 没有声明 `knowledge` optional dependency 组；新环境部署知识入库前必须先补齐依赖元数据。
5. 当前 PostgreSQL 复用 `ce-code` Compose 项目的 `postgres` 容器，存在跨项目运维耦合，后续应为 DeerFlow 建立独立、受管的数据库服务和凭据。
6. 上传接口同步等待 MinerU，超长文档会占用请求；后续可改成异步解析任务和进度事件。
7. 范本金标集、端到端回归、性能/并发、安全和多租户隔离测试仍未完成。
8. 136 工作区当前有大量未提交修改和未跟踪文件。接手前先查看 `git status`，不要使用 `git reset --hard` 或覆盖式同步。

## 12. 常见改动应该改哪里

- 调整审核口径或八维方法：先改 `agents/tender-review/lead_agent.yaml` 和对应 Skill；人格边界才改 `SOUL.md`。
- 增加/删除 Skill：同时改 Agent `config.yaml`、`extensions_config.json` 和 Skill 目录。
- 新增真正可调用工具：在 Gateway 工具注册层实现并挂载；只写 `allowed-tools` 不会生效。
- 修改结构化结果：同步修改 Skill 输出契约、`frontend/src/core/tender-review/findings.ts`、卡片组件和测试。
- 修改 PDF 解析：改 `tender-review/mineru.py` 与上传路由，并覆盖超时、恶意 ZIP、重复文件名和失败清理测试。
- 修改独立审核 API：同步检查 router、service、interaction store、用户隔离和 SSE sequence。
- 修改前端入口或 Agent 名称：同步检查侧栏、旧路由重定向、显示名、消息上下文和上传参数。

交接完成前，建议由接手同事亲自跑一遍 9.3 的手工验收，并选一份脱敏招标文件和一份明确版本的范本保存为受控测试样例。真实招标文件、投标文件、用户信息、数据库凭据和频道密钥不得进入 Git、Issue 或普通聊天记录。

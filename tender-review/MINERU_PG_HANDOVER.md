# 招投标 MinerU 与 PostgreSQL 知识库专项交接

本文只交接招投标文件的 MinerU 解析、知识切片、Embedding、PostgreSQL/pgvector 入库及运行时检索，不覆盖审核界面、提示词、工作流和报告前端。

## 1. 先明确两条流程

系统中有两条都会调用 MinerU、但用途完全不同的流程。

```text
用户在招投标聊天页上传 PDF
  -> Gateway 同步调用 MinerU
  -> 在线程上传目录生成 Markdown + content_list.json
  -> Lead 通过文件工具审核当前文件
  -> 不自动写入公共知识库 PG

运维人员批量准备范本/法规/资质标准
  -> parse_documents.py 调用 MinerU
  -> data/parsed 下生成 content_list.json
  -> knowledge_ingestion.py 切片并调用 BGE Embedding
  -> 写入 PostgreSQL 控制表和 pgvector 投影表
  -> 专用招投标审核引擎检索这些公共依据
```

最容易误解的一点是：前端上传成功只代表当前待审文件已解析，并不代表文件已经进入 PostgreSQL。公共知识库需要单独执行批量解析和入库命令。

## 2. 代码入口

| 环节 | 入口 |
|---|---|
| 前端上传触发 MinerU | `backend/app/gateway/routers/uploads.py` 中的 `_parse_tender_pdf` |
| 单 PDF MinerU 客户端 | `tender-review/src/tender_review/mineru.py` |
| 批量解析 | `tender-review/scripts/parse_documents.py` |
| MinerU 结果统计 | `tender-review/scripts/summarize_parse_results.py` |
| 切片、Embedding、PG 入库 | `tender-review/src/tender_review/knowledge_ingestion.py` |
| PG/pgvector 运行时检索 | `tender-review/src/tender_review/retrieval.py` |
| PG 业务表 ORM | `backend/packages/harness/deerflow/persistence/knowledge/model.py` |
| PG 连接配置 | 根目录 `config.yaml` |

## 3. MinerU 是怎么调用的

### 3.1 前端上传路径

招投标聊天页上传文件时，请求带 `agent_name=tender-review`。Gateway 保存文件后，仅当文件是 PDF 时执行：

```python
parse_pdf_with_mineru(file_path)
```

默认请求：

- URL：`http://172.19.2.2:18000/mineru/file_parse`
- HTTP：`POST multipart/form-data`
- 后端：`hybrid-auto-engine`
- 语言：`ch`
- 解析方式：`auto`
- 公式、表格、图片分析：开启
- 返回：Markdown、`content_list`、ZIP
- 读超时：3,600 秒

可用环境变量覆盖：

```bash
export TENDER_REVIEW_MINERU_API_URL='http://172.19.2.2:18000/mineru/file_parse'
export TENDER_REVIEW_MINERU_BACKEND='hybrid-auto-engine'
export TENDER_REVIEW_MINERU_TIMEOUT='3600'
```

客户端会校验 HTTP 200、ZIP 文件头、ZIP 路径安全和 `content_list` JSON 结构，然后在上传文件旁原子生成：

- `原文件名.md`：按物理页插入页码标记，供 Lead 的 `grep/read_file` 使用；
- `原文件名_content_list.json`：保留 MinerU 块、页码、bbox、表格等结构；
- 原 PDF：保留用于前端预览和证据跳页。

MinerU 失败会使上传请求失败，不会悄悄降级成直接读取 PDF。

### 3.2 批量知识库解析路径

在 136 上执行：

```bash
cd /mnt/nvme/calvin/code/deer-flow/tender-review

python scripts/parse_documents.py \
  --source-root data/招投标 \
  --output-root data/parsed \
  --converted-root /tmp/tender-review-converted \
  --workers 3
```

批处理支持 PDF、DOCX、PPTX、XLSX、PNG、JPG/JPEG。老式 DOC/XLS 需要先转换，再通过 `--converted-root` 映射。脚本最多三并发，默认重试三次，并用 `manifest.json` 支持增量续跑。

目录结构必须保留知识类别。例如：

```text
data/招投标/
├── 招标文件范本/
├── 政策法规/
├── 资质标准/
└── 招标文件/
```

只有前三类会进入公共 PG 知识库；`招标文件` 是待审业务文件，不会被 `knowledge_ingestion.py` 发现和入库。

解析完成后可汇总：

```bash
python scripts/summarize_parse_results.py \
  --manifest data/parsed/manifest.json \
  --output-prefix data/parsed/document_stats
```

## 4. PostgreSQL 数据是怎么生产的

### 4.1 数据发现与分类

`knowledge_ingestion.py` 只扫描以下目录中的主 `*_content_list.json`，并排除 `*_content_list_v2.json`：

| 磁盘目录 | PG 知识库 code | 文档类型 |
|---|---|---|
| `招标文件范本` | `tender_templates` | `template` |
| `政策法规` | `tender_regulations` | `regulation` |
| `资质标准` | `qualification_standards` | `qualification_standard` |

### 4.2 切片

默认每个切片约 360 字、重叠 60 字。切片会尽量保留：

- 文档标题和知识类型；
- 标题层级、章节路径和条款号；
- 物理页起止页；
- MinerU bbox；
- 表格正文；
- 内容哈希和近似 token 数。

Embedding 输入还会拼入知识类型、文档标题、章节、条款和正文，并截到 480 字。

### 4.3 Embedding

136 当前使用：

- 服务：`http://127.0.0.1:8097/v1/embeddings`
- OpenAI 兼容模型参数：`/model`
- 实际模型：`BAAI/bge-large-zh-v1.5`
- 维度：1,024
- 默认批量：16
- 距离：cosine
- 写入前：L2 归一化

配置覆盖项：

```bash
export TENDER_REVIEW_EMBEDDING_URL='http://127.0.0.1:8097'
export TENDER_REVIEW_EMBEDDING_MODEL='/model'
export TENDER_REVIEW_EMBEDDING_TIMEOUT='30'
export TENDER_REVIEW_EMBEDDING_BATCH_SIZE='16'
export TENDER_REVIEW_TENANT_ID='default'
```

### 4.4 入库顺序与幂等

每份文件的处理顺序是：

1. 按 tenant、知识库 code、相对路径和内容哈希生成稳定 UUID；
2. upsert 知识库和 owner 成员；
3. upsert 逻辑文档；
4. 按 `content_list` SHA-256 创建不可变版本；
5. 把旧版本切片和向量设为 disabled；
6. upsert 新版本切片；
7. 比较模型、内容哈希和同步状态，找出需要重算的切片；
8. 写 `kb_chunk_embeddings` 和 `kb_vector_outbox`；
9. 调 BGE 生成归一化向量；
10. upsert pgvector 投影，标记 embedding 已同步、outbox 已处理；
11. 在 `kb_ingestion_jobs` 记录 completed 或 failed。

因此同一批文件可重复执行。内容和模型未变化时会显示 `skipped`，不会重复生成向量；内容变化时会创建新版本并停用旧版本。

## 5. PG 表关系

```text
kb_knowledge_bases
├── kb_knowledge_base_members
├── kb_documents
│   └── kb_document_versions
│       └── kb_chunks
│           ├── kb_chunk_embeddings
│           └── kb_vectors_bge_large_zh_v15_1024
├── kb_embedding_models
├── kb_vector_outbox
└── kb_ingestion_jobs
```

主要职责：

| 表 | 内容 |
|---|---|
| `kb_knowledge_bases` | 三个公共知识库及 tenant |
| `kb_documents` | 稳定的逻辑文档 |
| `kb_document_versions` | MinerU 源文件 URI、SHA-256、版本和有效期 |
| `kb_chunks` | 正文、章节、条款、页码、bbox、内容哈希 |
| `kb_embedding_models` | 模型名、维度、距离和投影表 |
| `kb_chunk_embeddings` | 切片到 vector_id 的控制面映射与同步状态 |
| `kb_vector_outbox` | 可重试的向量 upsert/delete 事件 |
| `kb_vectors_bge_large_zh_v15_1024` | 1,024 维实际向量及检索范围字段 |
| `kb_ingestion_jobs` | 每份文档的处理数量、阶段、状态和错误 |

基础 `kb_*` 表由 DeerFlow SQLAlchemy metadata 在 Gateway 启动时通过 `create_all()` 建立。入库程序的 `ensure_schema()` 会建立 `vector` 扩展、1,024 维向量投影表、scope 索引、HNSW cosine 索引，并注册 embedding 模型。全新数据库应先启动一次 Gateway，确认基础表存在，再执行入库。

## 6. PG 怎么连接

### 6.1 136 开发模式

DeerFlow 以开发方式运行，但复用了已有的 `postgres` 容器作为依赖。容器把 PostgreSQL 映射到服务器回环地址的 5433 端口，数据库名是 `deerflow`。

不要把密码写进仓库或交接文档。当前服务器可从容器环境读取：

```bash
DB_USER="$(docker exec postgres printenv POSTGRES_USER)"
DB_PASS="$(docker exec postgres printenv POSTGRES_PASSWORD)"
export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5433/deerflow"
```

若密码含 `@`、`:`、`/`、`#` 等 URL 特殊字符，必须先进行 URL 编码。

根目录 `config.yaml` 复用同一个环境变量：

```yaml
database:
  backend: postgres
  postgres_url: $DATABASE_URL
checkpointer:
  type: postgres
  connection_string: $DATABASE_URL
```

地址选择：

| 运行位置 | PG host/port |
|---|---|
| 136 主机上的开发进程 | `127.0.0.1:5433` |
| Docker 内的 DeerFlow | `host.docker.internal:5433` |
| 个人电脑数据库客户端 | SSH 隧道到 136 的 `127.0.0.1:5433` |

不要为了连接方便把 PostgreSQL 暴露到新的网卡。

### 6.2 首次或增量入库

```bash
cd /mnt/nvme/calvin/code/deer-flow/tender-review
uv sync --extra knowledge

# 先只检查路由和切片，不连接 PG、不调用 embedding
uv run python -m tender_review.knowledge_ingestion \
  --parsed-root /mnt/nvme/calvin/code/deer-flow/tender-review/data/parsed \
  --dry-run

# 正式增量入库
uv run python -m tender_review.knowledge_ingestion \
  --parsed-root /mnt/nvme/calvin/code/deer-flow/tender-review/data/parsed \
  --embedding-url http://127.0.0.1:8097 \
  --embedding-model /model \
  --model-name BAAI/bge-large-zh-v1.5
```

只更新一个知识库时，`--category` 接收中文目录名，不接收 PG code：

```bash
uv run python -m tender_review.knowledge_ingestion \
  --parsed-root /mnt/nvme/calvin/code/deer-flow/tender-review/data/parsed \
  --category 政策法规
```

## 7. 审核时怎么检索 PG

`build_runtime_retrieval()` 从 `DATABASE_URL` 创建 `PostgresKnowledgeRetriever`。查询过程为：

1. 用同一个 BGE 模型生成查询向量；
2. L2 归一化；
3. 在 `kb_vectors_bge_large_zh_v15_1024` 使用 `embedding <=> query` 做 cosine 距离排序；
4. 联查当前文档版本和切片正文；
5. 过滤 tenant、知识库类别、active/enabled、模型、生效日期和地区；
6. 排除标题包含“征求意见”或“草案”的文档；
7. 返回稳定依据 ID：`kb:<knowledge-base-code>:<chunk-id>`；
8. 报告生成前通过依据 ID 回读 PG 原文。

专用招投标审核引擎把这层封装成 `search_tender_knowledge` 和 `get_tender_knowledge_evidence`。需要注意：当前前端的 Lead-only 原生聊天主要通过文件工具读取用户上传的 Markdown；若要让它直接检索 PG，必须确认上述两个专用工具已注册到 Gateway Agent。不能仅凭 PG 有数据就认为原生 Lead 一定会查询这些数据。

若没有 `DATABASE_URL`，公共知识库检索会被禁用；当前待审文件仍可走页级关键词检索，并在 embedding 不可用时退化到 lexical fallback。

## 8. 验证与排障

### 8.1 服务连通性

```bash
curl http://127.0.0.1:8097/v1/models

# MinerU 只允许 POST，GET 返回 405 也说明网络和服务入口可达
curl -o /dev/null -w '%{http_code}\n' \
  http://172.19.2.2:18000/mineru/file_parse
```

2026-09-14 在 136 实测：Embedding 返回模型 `/model`；MinerU GET 返回 405，连接约 1 ms。

### 8.2 数据基线

2026-09-14 在 `deerflow` 库实测：

| 项目 | 数量 |
|---|---:|
| pgvector 版本 | 0.8.6 |
| 知识库 | 3 |
| 文档 | 33 |
| 文档版本 | 33 |
| 切片 | 4,363 |
| chunk embedding | 4,363 |
| 向量 | 4,363 |
| 入库任务记录 | 67 |

最近三条任务均为 `stage=completed,status=completed`。

### 8.3 常用只读 SQL

```bash
docker exec postgres psql -U "$DB_USER" -d deerflow -P pager=off -c "
SELECT code, name, status FROM kb_knowledge_bases ORDER BY code;
"

docker exec postgres psql -U "$DB_USER" -d deerflow -P pager=off -c "
SELECT
  (SELECT count(*) FROM kb_documents) AS documents,
  (SELECT count(*) FROM kb_chunks WHERE enabled) AS active_chunks,
  (SELECT count(*) FROM kb_chunk_embeddings WHERE sync_status = 'synced') AS synced,
  (SELECT count(*) FROM kb_vectors_bge_large_zh_v15_1024 WHERE enabled) AS vectors;
"

docker exec postgres psql -U "$DB_USER" -d deerflow -P pager=off -c "
SELECT status, stage, processed_items, total_items, last_error, started_at
FROM kb_ingestion_jobs
ORDER BY started_at DESC
LIMIT 20;
"
```

### 8.4 常见故障

| 现象 | 优先检查 |
|---|---|
| 上传长时间不返回 | MinerU 服务、`TENDER_REVIEW_MINERU_TIMEOUT`、前端代理超时 |
| MinerU HTTP 405 | 使用了 GET；真正解析必须 POST multipart |
| 找不到解析文件 | 目录必须是三类中文类别，文件名必须是主 `*_content_list.json` |
| `DATABASE_URL is required` | 在启动 Gateway 和执行入库的同一 shell/service 环境中导出变量 |
| `relation kb_* does not exist` | 先让 Gateway 对 `deerflow` 库执行一次 metadata `create_all()` |
| `extension vector does not exist` | 确认 PostgreSQL 安装 pgvector，且账号有创建扩展权限 |
| embedding 维度错误 | 必须是 BGE large zh 1,024 维；不要把其他维度写进该投影表 |
| 重跑全部 skipped | 正常，说明内容哈希和模型均未变化 |
| PG 有向量但审核没引用 | 检查是否走专用审核引擎、检索工具是否挂载、`DATABASE_URL` 是否进入运行进程 |
| 旧法规仍被召回 | 检查文档版本、生效日期、状态和 `enabled`；必要时更新元数据后重入库 |

## 9. 最小交接清单

接手人至少应能独立完成：

1. 判断是“当前上传解析”还是“公共知识库入库”；
2. 从 136 访问 MinerU 与 BGE 服务；
3. 把资料放进正确的中文类别目录并运行批量解析；
4. 用 `--dry-run` 核对文档数和切片数；
5. 安全构造 `DATABASE_URL`，不复制明文密码；
6. 执行增量入库并理解 embedded/skipped；
7. 用 SQL 核对文档、切片、同步状态、向量和失败任务；
8. 明确 PG 检索工具是否真正挂载到所用 Agent。

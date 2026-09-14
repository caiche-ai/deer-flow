# Tender knowledge base schema

The tender knowledge base uses PostgreSQL as the system of record and treats
pgvector or Milvus as a rebuildable search projection. Original PDF, Office,
and image files belong in object storage; PostgreSQL stores their URI, digest,
version, extracted text, business facts, access rules, and processing state.

The repository deployment reuses the rootless `postgres` container on host
port `5433`. The existing `ce_cost` database remains isolated from the
`deerflow` database and role. Dockerized DeerFlow connects through
`host.docker.internal:5433`; desktop database tools should use an SSH tunnel
to server loopback instead of exposing PostgreSQL to additional networks.

## Data model

```text
kb_knowledge_bases ─┬─ kb_knowledge_base_members
                    ├─ kb_tender_projects
                    ├─ kb_documents ─ kb_document_versions ─ kb_chunks
                    │                                      ├─ kb_tender_requirements
                    │                                      └─ kb_bid_evidence_items
                    ├─ kb_embedding_models ─ kb_chunk_embeddings
                    │                       └─ kb_vector_outbox
                    └─ kb_ingestion_jobs

kb_tender_requirements ─ kb_requirement_evidence_matches ─ kb_bid_evidence_items
```

The ORM definitions live in
`packages/harness/deerflow/persistence/knowledge/model.py` and are registered
with the shared `Base.metadata`. Development environments receive the tables
through the existing `create_all()` startup path. Production deployments
should generate and review an Alembic migration before rollout.

| Table | Responsibility |
|---|---|
| `kb_knowledge_bases` | Tenant-owned corpus and lifecycle |
| `kb_knowledge_base_members` | User/group roles (`owner`, `editor`, `viewer`) |
| `kb_tender_projects` | Procurement identity, deadline, budget, purchaser, and agency |
| `kb_documents` | Stable logical document across amendments and re-parses |
| `kb_document_versions` | Immutable source object, checksum, validity, and parser state |
| `kb_chunks` | Searchable text with section, clause, page, and bounding-box provenance |
| `kb_embedding_models` | Versioned model/dimension/metric/backend contract |
| `kb_chunk_embeddings` | Control-plane mapping from a chunk to a backend `vector_id` |
| `kb_tender_requirements` | Qualification, technical, commercial, compliance, scoring, and delivery requirements |
| `kb_bid_evidence_items` | Reusable qualifications, certificates, personnel, cases, products, and templates |
| `kb_requirement_evidence_matches` | Explainable, reviewable requirement-to-evidence matches |
| `kb_ingestion_jobs` | Retryable parse/chunk/extract/embed workflow state |
| `kb_vector_outbox` | Transactional delivery of vector upsert/delete operations |

## Domain conventions

String fields intentionally avoid database enums so categories can evolve
without enum migrations. Validate values in API/service schemas. Recommended
initial values are:

- `document_type`: `tender_notice`, `tender_file`, `clarification`,
  `amendment`, `bid_file`, `contract`, `regulation`, `case`, `certificate`,
  `personnel`, `product`, `template`.
- `document_side`: `tender`, `bid`, `reference`.
- `requirement_type`: `qualification`, `compliance`, `technical`,
  `commercial`, `scoring`, `delivery`, `contract`.
- `evidence_type`: `enterprise_qualification`, `certificate`, `personnel`,
  `project_case`, `financial`, `product`, `solution`, `commitment`, `template`.
- Lifecycle status values should be append-only. Do not reuse an old value
  with a new meaning.

`mandatory` means a response is required. `knockout` means non-compliance can
invalidate a bid. They are separate because not every mandatory response is a
rejection clause. Every extracted requirement and evidence item points back to
an immutable document version and exact source chunk, making reviews auditable.

## Vector projection

The core schema deliberately does not expose a database-specific `embedding`
column. `kb_chunk_embeddings.vector_id` is the stable cross-backend key, while
`kb_vector_outbox` makes index writes retryable. This prevents pgvector SQL
from leaking into document and tender business logic.

For pgvector, create one physical projection table per active embedding
dimension. For example, a 1024-dimensional cosine model can use:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_vectors_bge_m3_1024 (
    vector_id varchar(128) PRIMARY KEY,
    chunk_id varchar(36) NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
    embedding_model_id varchar(36) NOT NULL
        REFERENCES kb_embedding_models(id) ON DELETE CASCADE,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(36) NOT NULL,
    tender_project_id varchar(36),
    document_id varchar(36) NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    embedding vector(1024) NOT NULL
);

CREATE INDEX ix_kb_vectors_bge_m3_scope
    ON kb_vectors_bge_m3_1024 (tenant_id, knowledge_base_id, enabled);

CREATE INDEX ix_kb_vectors_bge_m3_hnsw
    ON kb_vectors_bge_m3_1024
    USING hnsw (embedding vector_cosine_ops);
```

Replace the table name and dimension when a different model is registered.
Do not mix dimensions in one approximate-nearest-neighbour index. The vector
worker should perform this sequence:

1. Commit the chunk change and an outbox event in one PostgreSQL transaction.
2. Generate the embedding from the exact `content_hash` recorded by the event.
3. Upsert the vector projection using `vector_id` as the idempotency key.
4. Mark `kb_chunk_embeddings` synced only if its current `content_hash` still
   matches the event; stale workers must not overwrite a newer chunk vector.
5. Mark the outbox event processed. Retry failures with bounded backoff.

For Milvus, use the same scalar field names in the collection and keep
`vector_id` as its primary key. Migration is then backfill, dual-write,
shadow-read comparison, and read cutover; none of the document, requirement,
evidence, or permission tables need to change.

## Retrieval rules

- Resolve knowledge-base membership before search. Never rely on a tenant id
  supplied by the client without authorization.
- Push `tenant_id`, `knowledge_base_id`, project, document side, and active
  status filters into the vector query.
- Fetch final chunk text and citations from PostgreSQL by `chunk_id`; vector
  payload text is only a projection and is not authoritative.
- Preserve `document_version_id`, page range, clause number, and bounding box
  in every answer citation.
- Retrieve extra candidates before applying dynamic per-user ACL or validity
  filters, then rerank the surviving results.
- Soft deletion changes business visibility immediately and emits vector
  deletion events; a later compaction job may remove old immutable versions.

## Initial rollout order

1. Knowledge bases, membership, projects, documents, and versions.
2. Parsing/chunking with page and clause provenance.
3. Requirement and evidence extraction with human review.
4. pgvector projection and hybrid retrieval evaluation.
5. Requirement-to-evidence matching and bid compliance workflows.

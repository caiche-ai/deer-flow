"""Relational source-of-truth models for a tender-domain knowledge base.

The tables deliberately keep business data and vector-index state separate.
PostgreSQL remains authoritative for documents, traceable chunks, tender
requirements, and reusable bid evidence. A pgvector table or a Milvus
collection is a rebuildable projection keyed by ``vector_id``.

Enums are represented as strings so new document and requirement categories
do not require disruptive enum migrations. Service-layer schemas should
validate the currently supported values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class KnowledgeBaseRow(_TimestampMixin, Base):
    """A tenant-owned corpus, such as regulations or one project's files."""

    __tablename__ = "kb_knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="tender")
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    owner_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_kb_tenant_code"),
        Index("ix_kb_tenant_status", "tenant_id", "status"),
    )


class KnowledgeBaseMemberRow(_TimestampMixin, Base):
    """User or group access to a knowledge base."""

    __tablename__ = "kb_knowledge_base_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="viewer")
    granted_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "principal_type", "principal_id", name="uq_kb_member_principal"),
        Index("ix_kb_member_lookup", "principal_type", "principal_id", "role"),
    )


class TenderProjectRow(_TimestampMixin, Base):
    """A procurement project whose tender and bid materials are correlated."""

    __tablename__ = "kb_tender_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="SET NULL"))
    project_code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    purchaser: Mapped[str | None] = mapped_column(String(256))
    agency: Mapped[str | None] = mapped_column(String(256))
    region: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    procurement_method: Mapped[str | None] = mapped_column(String(64))
    bid_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="preparing")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "project_code", name="uq_kb_tender_project_code"),
        Index("ix_kb_project_tenant_status", "tenant_id", "status"),
        Index("ix_kb_project_deadline", "tenant_id", "bid_deadline"),
    )


class KnowledgeDocumentRow(_TimestampMixin, Base):
    """Stable logical document identity across amendments and re-parses."""

    __tablename__ = "kb_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    tender_project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("kb_tender_projects.id", ondelete="SET NULL"))
    document_code: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    document_type: Mapped[str] = mapped_column(String(48), nullable=False)
    document_side: Mapped[str] = mapped_column(String(24), nullable=False, default="reference")
    confidentiality: Mapped[str] = mapped_column(String(24), nullable=False, default="internal")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    current_version_no: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "document_code", name="uq_kb_document_code"),
        Index("ix_kb_document_scope", "tenant_id", "knowledge_base_id", "document_type", "status"),
        Index("ix_kb_document_project", "tenant_id", "tender_project_id", "document_side"),
    )


class KnowledgeDocumentVersionRow(_TimestampMixin, Base):
    """Immutable source file and parser lifecycle for one document revision."""

    __tablename__ = "kb_document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    source_uri: Mapped[str | None] = mapped_column(Text)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    parser_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    created_by: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_kb_document_version"),
        UniqueConstraint("document_id", "sha256", name="uq_kb_document_content"),
        Index("ix_kb_document_version_status", "document_id", "parser_status"),
    )


class KnowledgeChunkRow(_TimestampMixin, Base):
    """Traceable retrieval unit with denormalized scalar-filter fields."""

    __tablename__ = "kb_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    tender_project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("kb_tender_projects.id", ondelete="SET NULL"))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_document_versions.id", ondelete="CASCADE"), nullable=False)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(512))
    section_path: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    clause_no: Mapped[str | None] = mapped_column(String(128))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    bbox_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    token_count: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_no", name="uq_kb_version_chunk"),
        Index("ix_kb_chunk_scope", "tenant_id", "knowledge_base_id", "enabled"),
        Index("ix_kb_chunk_project", "tenant_id", "tender_project_id", "enabled"),
        Index("ix_kb_chunk_document", "document_id", "document_version_id", "chunk_no"),
        Index("ix_kb_chunk_clause", "document_version_id", "clause_no"),
        Index("ix_kb_chunk_content_hash", "content_hash"),
    )


class EmbeddingModelRow(_TimestampMixin, Base):
    """Versioned embedding contract shared by pgvector and Milvus."""

    __tablename__ = "kb_embedding_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    model_revision: Mapped[str | None] = mapped_column(String(128))
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(16), nullable=False, default="cosine")
    normalize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    backend: Mapped[str] = mapped_column(String(24), nullable=False, default="pgvector")
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_kb_embedding_model_code"),
        Index("ix_kb_embedding_model_backend", "tenant_id", "backend", "status"),
    )


class ChunkEmbeddingRow(_TimestampMixin, Base):
    """Control-plane record for a vector stored in the selected backend."""

    __tablename__ = "kb_chunk_embeddings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_chunks.id", ondelete="CASCADE"), nullable=False)
    embedding_model_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_embedding_models.id", ondelete="RESTRICT"), nullable=False)
    vector_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    backend: Mapped[str] = mapped_column(String(24), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("chunk_id", "embedding_model_id", name="uq_kb_chunk_embedding_model"),
        Index("ix_kb_embedding_sync", "backend", "sync_status", "updated_at"),
        Index("ix_kb_embedding_scope", "tenant_id", "knowledge_base_id", "embedding_model_id"),
    )


class TenderRequirementRow(_TimestampMixin, Base):
    """Auditable qualification, compliance, scoring, or delivery requirement."""

    __tablename__ = "kb_tender_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tender_project_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_tender_projects.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_document_versions.id", ondelete="CASCADE"), nullable=False)
    source_chunk_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_chunks.id", ondelete="RESTRICT"), nullable=False)
    requirement_code: Mapped[str] = mapped_column(String(128), nullable=False)
    requirement_type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    response_guidance: Mapped[str | None] = mapped_column(Text)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    knockout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    structured_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("tender_project_id", "document_version_id", "requirement_code", name="uq_kb_tender_requirement_code"),
        Index("ix_kb_requirement_project_type", "tenant_id", "tender_project_id", "requirement_type"),
        Index("ix_kb_requirement_risk", "tender_project_id", "knockout", "mandatory", "review_status"),
        Index("ix_kb_requirement_source", "document_version_id", "source_chunk_id"),
    )


class BidEvidenceItemRow(_TimestampMixin, Base):
    """Reusable bidder evidence: qualification, person, case, product, etc."""

    __tablename__ = "kb_bid_evidence_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_document_versions.id", ondelete="CASCADE"), nullable=False)
    source_chunk_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_chunks.id", ondelete="RESTRICT"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(256))
    identifier: Mapped[str | None] = mapped_column(String(256))
    issuer: Mapped[str | None] = mapped_column(String(256))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_kb_evidence_type_status", "tenant_id", "knowledge_base_id", "evidence_type", "status"),
        Index("ix_kb_evidence_identifier", "tenant_id", "identifier"),
        Index("ix_kb_evidence_validity", "tenant_id", "valid_to", "status"),
        Index("ix_kb_evidence_source", "document_version_id", "source_chunk_id"),
    )


class RequirementEvidenceMatchRow(_TimestampMixin, Base):
    """Human-reviewable match between a requirement and bidder evidence."""

    __tablename__ = "kb_requirement_evidence_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_tender_requirements.id", ondelete="CASCADE"), nullable=False)
    evidence_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_bid_evidence_items.id", ondelete="CASCADE"), nullable=False)
    match_type: Mapped[str] = mapped_column(String(24), nullable=False, default="semantic")
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    decision: Mapped[str] = mapped_column(String(24), nullable=False, default="candidate")
    rationale: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("requirement_id", "evidence_item_id", name="uq_kb_requirement_evidence"),
        Index("ix_kb_requirement_match_decision", "requirement_id", "decision", "match_score"),
        Index("ix_kb_evidence_match", "evidence_item_id", "decision"),
    )


class IngestionJobRow(_TimestampMixin, Base):
    """Retryable parsing, chunking, extraction, and embedding job state."""

    __tablename__ = "kb_ingestion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_document_versions.id", ondelete="CASCADE"), nullable=False)
    embedding_model_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("kb_embedding_models.id", ondelete="SET NULL"))
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="full")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_items: Mapped[int | None] = mapped_column(Integer)
    parser_name: Mapped[str | None] = mapped_column(String(128))
    parser_version: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_kb_ingestion_queue", "status", "stage", "created_at"),
        Index("ix_kb_ingestion_document", "document_version_id", "created_at"),
        Index("ix_kb_ingestion_scope", "tenant_id", "knowledge_base_id", "status"),
    )


class VectorOutboxRow(Base):
    """Transactional outbox used to project chunk changes to a vector backend."""

    __tablename__ = "kb_vector_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_chunks.id", ondelete="CASCADE"), nullable=False)
    embedding_model_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_embedding_models.id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_kb_vector_outbox_queue", "status", "available_at", "id"),
        Index("ix_kb_vector_outbox_chunk", "chunk_id", "embedding_model_id", "created_at"),
    )

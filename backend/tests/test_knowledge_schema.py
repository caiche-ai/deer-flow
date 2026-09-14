"""Schema tests for the tender-domain knowledge base tables."""

from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.base import Base
from deerflow.persistence.knowledge.model import (
    BidEvidenceItemRow,
    ChunkEmbeddingRow,
    EmbeddingModelRow,
    IngestionJobRow,
    KnowledgeBaseMemberRow,
    KnowledgeBaseRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    KnowledgeDocumentVersionRow,
    RequirementEvidenceMatchRow,
    TenderProjectRow,
    TenderRequirementRow,
    VectorOutboxRow,
)

EXPECTED_TABLES = {
    "kb_knowledge_bases",
    "kb_knowledge_base_members",
    "kb_tender_projects",
    "kb_documents",
    "kb_document_versions",
    "kb_chunks",
    "kb_embedding_models",
    "kb_chunk_embeddings",
    "kb_tender_requirements",
    "kb_bid_evidence_items",
    "kb_requirement_evidence_matches",
    "kb_ingestion_jobs",
    "kb_vector_outbox",
}


def _unique_column_sets(model: type) -> set[tuple[str, ...]]:
    return {tuple(column.name for column in constraint.columns) for constraint in model.__table__.constraints if isinstance(constraint, UniqueConstraint)}


def test_all_knowledge_models_are_registered() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)
    assert KnowledgeBaseRow.__tablename__ == "kb_knowledge_bases"
    assert KnowledgeBaseMemberRow.__tablename__ == "kb_knowledge_base_members"
    assert TenderProjectRow.__tablename__ == "kb_tender_projects"
    assert KnowledgeDocumentRow.__tablename__ == "kb_documents"
    assert KnowledgeDocumentVersionRow.__tablename__ == "kb_document_versions"
    assert KnowledgeChunkRow.__tablename__ == "kb_chunks"
    assert EmbeddingModelRow.__tablename__ == "kb_embedding_models"
    assert ChunkEmbeddingRow.__tablename__ == "kb_chunk_embeddings"
    assert TenderRequirementRow.__tablename__ == "kb_tender_requirements"
    assert BidEvidenceItemRow.__tablename__ == "kb_bid_evidence_items"
    assert RequirementEvidenceMatchRow.__tablename__ == "kb_requirement_evidence_matches"
    assert IngestionJobRow.__tablename__ == "kb_ingestion_jobs"
    assert VectorOutboxRow.__tablename__ == "kb_vector_outbox"


def test_business_keys_prevent_duplicate_versions_chunks_and_matches() -> None:
    assert ("tenant_id", "code") in _unique_column_sets(KnowledgeBaseRow)
    assert ("knowledge_base_id", "principal_type", "principal_id") in _unique_column_sets(KnowledgeBaseMemberRow)
    assert ("tenant_id", "project_code") in _unique_column_sets(TenderProjectRow)
    assert ("knowledge_base_id", "document_code") in _unique_column_sets(KnowledgeDocumentRow)
    assert ("document_id", "version_no") in _unique_column_sets(KnowledgeDocumentVersionRow)
    assert ("document_version_id", "chunk_no") in _unique_column_sets(KnowledgeChunkRow)
    assert ("chunk_id", "embedding_model_id") in _unique_column_sets(ChunkEmbeddingRow)
    assert ("requirement_id", "evidence_item_id") in _unique_column_sets(RequirementEvidenceMatchRow)


def test_chunk_contains_traceability_and_milvus_filter_fields() -> None:
    columns = KnowledgeChunkRow.__table__.columns
    assert {
        "tenant_id",
        "knowledge_base_id",
        "tender_project_id",
        "document_id",
        "document_version_id",
        "content",
        "content_hash",
        "section_path",
        "clause_no",
        "page_start",
        "page_end",
        "bbox_json",
    } <= set(columns.keys())


def test_requirements_and_evidence_keep_source_citations() -> None:
    requirement_fks = {fk.target_fullname for fk in TenderRequirementRow.__table__.foreign_keys}
    evidence_fks = {fk.target_fullname for fk in BidEvidenceItemRow.__table__.foreign_keys}
    assert "kb_chunks.id" in requirement_fks
    assert "kb_document_versions.id" in requirement_fks
    assert "kb_chunks.id" in evidence_fks
    assert "kb_document_versions.id" in evidence_fks


def test_vector_projection_is_backend_neutral() -> None:
    columns = ChunkEmbeddingRow.__table__.columns
    assert {"vector_id", "backend", "sync_status", "content_hash", "embedding_model_id"} <= set(columns.keys())
    assert "embedding" not in columns
    assert VectorOutboxRow.__table__.c.event_key.unique is True


@pytest.mark.anyio
async def test_schema_can_be_created_on_sqlite(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with engine.connect() as connection:
            table_names = set(await connection.run_sync(lambda sync_connection: sync_connection.dialect.get_table_names(sync_connection)))
        assert EXPECTED_TABLES <= table_names
    finally:
        await engine.dispose()

"""Hybrid retrieval for long tender documents and public tender knowledge."""

from __future__ import annotations

import math
import os
import re
import threading
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from tender_review.document import EvidenceChunk, MineruDocument

PUBLIC_KNOWLEDGE_BASES = frozenset(
    {"tender_templates", "tender_regulations", "qualification_standards"}
)
VECTOR_TABLE = "kb_vectors_bge_large_zh_v15_1024"
DEFAULT_MODEL_CODE = "bge-large-zh-v1.5-v1"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
_BASIS_ID_RE = re.compile(
    r"^kb:(tender_templates|tender_regulations|qualification_standards):([\w-]+)$"
)


class Embedder(Protocol):
    dimensions: int
    batch_size: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class RetrievalChunk:
    child_id: str
    parent_evidence_id: str
    page: int
    text: str


@dataclass(frozen=True, slots=True)
class HybridSearchHit:
    evidence: EvidenceChunk
    score: float
    retrieval_mode: str
    matched_child_text: str


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    basis_id: str
    knowledge_base: str
    knowledge_base_name: str
    document_id: str
    document_title: str
    chunk_id: str
    chunk_no: int
    content: str
    section_title: str | None
    section_path: tuple[str, ...]
    clause_no: str | None
    page_start: int
    page_end: int
    bbox_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    evidence: KnowledgeEvidence
    score: float


class KnowledgeRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        categories: Sequence[str] | None = None,
        top_k: int = 8,
        region: str | None = None,
        as_of: str | None = None,
    ) -> Sequence[KnowledgeSearchHit]: ...

    def get_evidence(self, basis_ids: Sequence[str]) -> Sequence[KnowledgeEvidence]: ...


@dataclass(frozen=True, slots=True)
class TenderRetrievalServices:
    document: DocumentHybridRetriever | None = None
    knowledge: KnowledgeRetriever | None = None


def _split_child_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                text.rfind(mark, start + max_chars // 2, end)
                for mark in ("。", "；", "！", "？", "\n")
            )
            if boundary >= start + max_chars // 2:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return pieces


def _normalise_vector(values: Sequence[float], dimensions: int) -> array:
    if len(values) != dimensions:
        raise ValueError(f"embedding dimension {len(values)} does not match {dimensions}")
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if norm == 0:
        raise ValueError("embedding service returned a zero vector")
    return array("f", (float(value) / norm for value in values))


class DocumentHybridRetriever:
    """Retrieve small semantic children and return their page-scoped evidence parents."""

    def __init__(
        self,
        document: MineruDocument,
        embedder: Embedder,
        *,
        child_chars: int = 420,
        overlap_chars: int = 60,
        max_embedding_chars: int = 480,
    ) -> None:
        if child_chars < 100:
            raise ValueError("child_chars must be at least 100")
        if overlap_chars < 0 or overlap_chars >= child_chars // 2:
            raise ValueError("overlap_chars must be non-negative and less than half child_chars")
        self.document = document
        self.embedder = embedder
        self.max_embedding_chars = max_embedding_chars
        self.child_chunks = tuple(self._build_child_chunks(child_chars, overlap_chars))
        self._parents = {chunk.evidence_id: chunk for chunk in document.evidence_chunks}
        self._vectors: tuple[array, ...] | None = None
        self._semantic_error: str | None = None
        self._index_lock = threading.Lock()
        self._embedding_lock = threading.Lock()

    def _build_child_chunks(self, child_chars: int, overlap_chars: int) -> list[RetrievalChunk]:
        children: list[RetrievalChunk] = []
        for parent in self.document.evidence_chunks:
            pieces = _split_child_text(parent.text, child_chars, overlap_chars)
            children.extend(
                RetrievalChunk(
                    child_id=f"{parent.evidence_id}:part={index}",
                    parent_evidence_id=parent.evidence_id,
                    page=parent.page,
                    text=piece,
                )
                for index, piece in enumerate(pieces, start=1)
            )
        return children

    def _embedding_text(self, chunk: RetrievalChunk) -> str:
        return (
            f"知识类型：待审核招标文件\n文档：{self.document.document_id}\n"
            f"页码：{chunk.page}\n正文：{chunk.text}"
        )[: self.max_embedding_chars]

    def _ensure_vectors(self) -> bool:
        if self._vectors is not None:
            return True
        if self._semantic_error is not None:
            return False
        with self._index_lock:
            if self._vectors is not None:
                return True
            if self._semantic_error is not None:
                return False
            try:
                vectors: list[array] = []
                batch_size = max(int(getattr(self.embedder, "batch_size", 16)), 1)
                for offset in range(0, len(self.child_chunks), batch_size):
                    batch = self.child_chunks[offset : offset + batch_size]
                    with self._embedding_lock:
                        embedded = self.embedder.embed(
                            [self._embedding_text(chunk) for chunk in batch]
                        )
                    if len(embedded) != len(batch):
                        raise ValueError("embedding response count does not match child chunks")
                    vectors.extend(
                        _normalise_vector(vector, self.embedder.dimensions) for vector in embedded
                    )
                self._vectors = tuple(vectors)
                return True
            except Exception as exc:  # noqa: BLE001 - lexical retrieval is the fail-open path
                self._semantic_error = type(exc).__name__
                return False

    def status(self) -> dict[str, Any]:
        if self._vectors is not None:
            semantic_status = "ready"
        elif self._semantic_error is not None:
            semantic_status = "unavailable"
        else:
            semantic_status = "lazy"
        return {
            "mode": "parent_child_hybrid",
            "parent_chunks": len(self.document.evidence_chunks),
            "child_chunks": len(self.child_chunks),
            "semantic_status": semantic_status,
            "embedding_dimensions": self.embedder.dimensions,
        }

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> tuple[HybridSearchHit, ...]:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        lexical = self.document.search(
            query,
            top_k=min(20, max(top_k * 3, 8)),
            page_start=page_start,
            page_end=page_end,
        )
        if not self._ensure_vectors():
            return tuple(
                HybridSearchHit(
                    evidence=hit.evidence,
                    score=hit.score,
                    retrieval_mode="lexical_fallback",
                    matched_child_text=hit.evidence.text,
                )
                for hit in lexical[:top_k]
            )

        try:
            with self._embedding_lock:
                query_values = self.embedder.embed([f"{QUERY_INSTRUCTION}{query}"])[0]
            query_vector = _normalise_vector(query_values, self.embedder.dimensions)
        except Exception as exc:  # noqa: BLE001 - lexical retrieval is the fail-open path
            self._semantic_error = type(exc).__name__
            return tuple(
                HybridSearchHit(
                    evidence=hit.evidence,
                    score=hit.score,
                    retrieval_mode="lexical_fallback",
                    matched_child_text=hit.evidence.text,
                )
                for hit in lexical[:top_k]
            )

        parent_semantic: dict[str, tuple[float, str]] = {}
        for child, vector in zip(self.child_chunks, self._vectors or (), strict=True):
            if page_start is not None and child.page < page_start:
                continue
            if page_end is not None and child.page > page_end:
                continue
            similarity = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            previous = parent_semantic.get(child.parent_evidence_id)
            if previous is None or similarity > previous[0]:
                parent_semantic[child.parent_evidence_id] = (similarity, child.text)

        semantic_order = sorted(
            parent_semantic,
            key=lambda evidence_id: (
                -parent_semantic[evidence_id][0],
                self._parents[evidence_id].page,
                self._parents[evidence_id].chunk,
            ),
        )[: min(len(parent_semantic), max(top_k * 3, 8))]
        lexical_ranks = {
            hit.evidence.evidence_id: rank for rank, hit in enumerate(lexical, start=1)
        }
        semantic_ranks = {
            evidence_id: rank for rank, evidence_id in enumerate(semantic_order, start=1)
        }
        candidate_ids = set(lexical_ranks) | set(semantic_ranks)

        def fused_score(evidence_id: str) -> float:
            score = 0.0
            if evidence_id in lexical_ranks:
                score += 1 / (60 + lexical_ranks[evidence_id])
            if evidence_id in semantic_ranks:
                score += 1 / (60 + semantic_ranks[evidence_id])
            return score

        ranked = sorted(
            candidate_ids,
            key=lambda evidence_id: (
                -fused_score(evidence_id),
                self._parents[evidence_id].page,
                self._parents[evidence_id].chunk,
            ),
        )[:top_k]
        return tuple(
            HybridSearchHit(
                evidence=self._parents[evidence_id],
                score=round(fused_score(evidence_id), 8),
                retrieval_mode="hybrid",
                matched_child_text=parent_semantic.get(
                    evidence_id, (0.0, self._parents[evidence_id].text)
                )[1],
            )
            for evidence_id in ranked
        )


class PostgresKnowledgeRetriever:
    """Search the authoritative public tender corpora through their pgvector projection."""

    def __init__(
        self,
        database_url: str,
        embedder: Embedder,
        *,
        tenant_id: str = "default",
        model_code: str = DEFAULT_MODEL_CODE,
        connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.database_url = re.sub(
            r"^postgresql\+[^:]+://", "postgresql://", database_url.strip().lstrip("\ufeff")
        )
        self.embedder = embedder
        self.tenant_id = tenant_id
        self.model_code = model_code
        self._connection_factory = connection_factory
        self._embedding_lock = threading.Lock()

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(self.database_url)
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "knowledge retrieval requires the 'knowledge' optional dependency"
            ) from exc
        return psycopg.connect(self.database_url)

    def _query_vector(self, query: str) -> str:
        if not query.strip():
            raise ValueError("query must not be empty")
        with self._embedding_lock:
            vector = self.embedder.embed([f"{QUERY_INSTRUCTION}{query}"])[0]
        normalised = _normalise_vector(vector, self.embedder.dimensions)
        return "[" + ",".join(f"{value:.9g}" for value in normalised) + "]"

    @staticmethod
    def _row_to_evidence(row: Sequence[Any]) -> KnowledgeEvidence:
        section_path = row[8] if isinstance(row[8], list) else []
        bbox_json = row[12] if isinstance(row[12], dict) else {}
        return KnowledgeEvidence(
            basis_id=f"kb:{row[1]}:{row[0]}",
            knowledge_base=str(row[1]),
            knowledge_base_name=str(row[2]),
            document_id=str(row[3]),
            document_title=str(row[4]),
            chunk_id=str(row[0]),
            chunk_no=int(row[5]),
            content=str(row[6]),
            section_title=str(row[7]) if row[7] is not None else None,
            section_path=tuple(str(item) for item in section_path),
            clause_no=str(row[9]) if row[9] is not None else None,
            page_start=int(row[10]),
            page_end=int(row[11]),
            bbox_json=bbox_json,
        )

    @staticmethod
    def _validate_categories(categories: Sequence[str] | None) -> list[str]:
        selected = list(categories or sorted(PUBLIC_KNOWLEDGE_BASES))
        unknown = sorted(set(selected) - PUBLIC_KNOWLEDGE_BASES)
        if unknown:
            raise ValueError(f"unsupported knowledge categories: {', '.join(unknown)}")
        return selected

    def search(
        self,
        query: str,
        *,
        categories: Sequence[str] | None = None,
        top_k: int = 8,
        region: str | None = None,
        as_of: str | None = None,
    ) -> tuple[KnowledgeSearchHit, ...]:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        selected = self._validate_categories(categories)
        effective_date = (
            date.fromisoformat(as_of).isoformat() if as_of else datetime.now(UTC).date().isoformat()
        )
        vector_literal = self._query_vector(query)
        sql = f"""
            SELECT
                c.id, kb.code, kb.name, d.id, d.title, c.chunk_no,
                c.content, c.section_title, c.section_path, c.clause_no,
                c.page_start, c.page_end, c.bbox_json,
                v.embedding <=> %s::vector AS distance
            FROM {VECTOR_TABLE} v
            JOIN kb_embedding_models em ON em.id = v.embedding_model_id
            JOIN kb_chunks c ON c.id = v.chunk_id
            JOIN kb_documents d ON d.id = c.document_id
            JOIN kb_document_versions dv
              ON dv.document_id = d.id AND dv.version_no = d.current_version_no
            JOIN kb_knowledge_bases kb ON kb.id = d.knowledge_base_id
            WHERE kb.tenant_id = %s
              AND kb.code = ANY(%s)
              AND kb.status = 'active' AND kb.deleted_at IS NULL
              AND d.status = 'active' AND d.deleted_at IS NULL
              AND dv.parser_status = 'completed'
              AND c.document_version_id = dv.id AND c.enabled
              AND v.enabled AND em.code = %s AND em.status = 'active'
              AND (dv.effective_from IS NULL OR dv.effective_from <= %s::date)
              AND (dv.effective_to IS NULL OR dv.effective_to >= %s::date)
              AND (%s::text IS NULL OR COALESCE(d.metadata_json->>'region', '') IN ('', '全国', %s::text))
              AND d.title !~ '(征求意见|草案)'
            ORDER BY distance
            LIMIT %s
        """
        params = (
            vector_literal,
            self.tenant_id,
            selected,
            self.model_code,
            effective_date,
            effective_date,
            region,
            region,
            top_k,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return tuple(
            KnowledgeSearchHit(
                evidence=self._row_to_evidence(row),
                score=round(1 - float(row[13]), 8),
            )
            for row in rows
        )

    def get_evidence(self, basis_ids: Sequence[str]) -> tuple[KnowledgeEvidence, ...]:
        if not basis_ids or len(basis_ids) > 20:
            raise ValueError("basis_ids must contain between 1 and 20 values")
        parsed: list[tuple[str, str]] = []
        for basis_id in basis_ids:
            match = _BASIS_ID_RE.fullmatch(str(basis_id).strip())
            if match is None:
                raise ValueError(f"invalid knowledge basis id: {basis_id}")
            parsed.append((match.group(1), match.group(2)))
        chunk_ids = [chunk_id for _, chunk_id in parsed]
        sql = """
            SELECT
                c.id, kb.code, kb.name, d.id, d.title, c.chunk_no,
                c.content, c.section_title, c.section_path, c.clause_no,
                c.page_start, c.page_end, c.bbox_json, 0::double precision
            FROM kb_chunks c
            JOIN kb_documents d ON d.id = c.document_id
            JOIN kb_document_versions dv
              ON dv.document_id = d.id AND dv.version_no = d.current_version_no
            JOIN kb_knowledge_bases kb ON kb.id = d.knowledge_base_id
            WHERE kb.tenant_id = %s
              AND kb.code = ANY(%s)
              AND c.id = ANY(%s)
              AND kb.status = 'active' AND kb.deleted_at IS NULL
              AND d.status = 'active' AND d.deleted_at IS NULL
              AND c.document_version_id = dv.id AND c.enabled
              AND d.title !~ '(征求意见|草案)'
        """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql,
                (self.tenant_id, sorted(PUBLIC_KNOWLEDGE_BASES), chunk_ids),
            )
            evidence_by_id = {
                evidence.basis_id: evidence
                for evidence in (self._row_to_evidence(row) for row in cursor.fetchall())
            }
        missing = [basis_id for basis_id in basis_ids if basis_id not in evidence_by_id]
        if missing:
            raise ValueError(f"unknown or inactive knowledge basis ids: {', '.join(missing)}")
        return tuple(evidence_by_id[basis_id] for basis_id in basis_ids)


def build_runtime_retrieval(document: MineruDocument) -> TenderRetrievalServices:
    """Build lazy runtime adapters from environment without making network calls."""

    from tender_review.knowledge_ingestion import (
        DEFAULT_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        EmbeddingClient,
    )

    default_host = "host.docker.internal" if Path("/.dockerenv").exists() else "127.0.0.1"
    embedding_url = os.getenv("TENDER_REVIEW_EMBEDDING_URL", f"http://{default_host}:8097")
    timeout = float(os.getenv("TENDER_REVIEW_EMBEDDING_TIMEOUT", "30"))
    client_options = {
        "base_url": embedding_url,
        "model": os.getenv("TENDER_REVIEW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "dimensions": DEFAULT_DIMENSIONS,
        "batch_size": int(os.getenv("TENDER_REVIEW_EMBEDDING_BATCH_SIZE", "16")),
        "timeout": timeout,
        "max_attempts": 1,
    }
    embedder = EmbeddingClient(**client_options)
    document_retriever = DocumentHybridRetriever(
        document,
        embedder,
        child_chars=int(os.getenv("TENDER_REVIEW_CHILD_CHARS", "420")),
        overlap_chars=int(os.getenv("TENDER_REVIEW_CHILD_OVERLAP", "60")),
    )
    database_url = os.getenv("DATABASE_URL")
    knowledge_retriever = (
        PostgresKnowledgeRetriever(
            database_url,
            EmbeddingClient(**client_options),
            tenant_id=os.getenv("TENDER_REVIEW_TENANT_ID", "default"),
        )
        if database_url
        else None
    )
    return TenderRetrievalServices(document_retriever, knowledge_retriever)

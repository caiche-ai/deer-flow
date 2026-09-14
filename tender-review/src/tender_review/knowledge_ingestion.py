"""Ingest MinerU tender corpora into PostgreSQL and a pgvector projection."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import NAMESPACE_URL, uuid4, uuid5

import requests

VECTOR_TABLE = "kb_vectors_bge_large_zh_v15_1024"
DEFAULT_EMBEDDING_MODEL = "/model"
DEFAULT_MODEL_NAME = "BAAI/bge-large-zh-v1.5"
DEFAULT_DIMENSIONS = 1024

_AUXILIARY_BLOCK_TYPES = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}
_CLAUSE_TOKEN = (
    r"第[〇零一二三四五六七八九十百千万两\d]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两\d]+)?"
)
_MARKDOWN_CLAUSE_RE = re.compile(rf"\*\*\s*({_CLAUSE_TOKEN})\s*\*\*")
_LINE_CLAUSE_RE = re.compile(rf"(?m)^[ \t]*({_CLAUSE_TOKEN})")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True, slots=True)
class CorpusSpec:
    category: str
    code: str
    name: str
    document_type: str
    description: str


CORPUS_SPECS: dict[str, CorpusSpec] = {
    "招标文件范本": CorpusSpec(
        category="招标文件范本",
        code="tender_templates",
        name="招标文件范本",
        document_type="template",
        description="招标文件章节、表单和示范条款参考库",
    ),
    "政策法规": CorpusSpec(
        category="政策法规",
        code="tender_regulations",
        name="招投标政策法规",
        document_type="regulation",
        description="招投标法律、行政法规、规章和规范性文件库",
    ),
    "资质标准": CorpusSpec(
        category="资质标准",
        code="qualification_standards",
        name="招投标资质标准",
        document_type="qualification_standard",
        description="企业资质类别、等级、人员、资产和业绩标准库",
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    corpus: CorpusSpec
    title: str
    relative_path: str
    content_list_path: Path


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_no: int
    content: str
    content_hash: str
    section_title: str | None
    section_path: tuple[str, ...]
    clause_no: str | None
    page_start: int
    page_end: int
    bbox_json: dict[str, Any]
    token_count: int


@dataclass(frozen=True, slots=True)
class DocumentIngestionResult:
    title: str
    corpus_code: str
    chunks: int
    embedded: int
    skipped: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stable_id(kind: str, *parts: object) -> str:
    key = ":".join(("deerflow", "tender-kb", kind, *(str(part) for part in parts)))
    return str(uuid5(NAMESPACE_URL, key))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, list):
        value = "\n".join(str(item) for item in value if item is not None)
    text = str(value)
    text = re.sub(r"</(?:tr|p|div|li|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:td|th)\s*>", " | ", text, flags=re.IGNORECASE)
    text = html.unescape(_HTML_TAG_RE.sub(" ", text))
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _block_text(block: dict[str, Any]) -> str:
    values: list[str] = []
    for field in (
        "text",
        "table_caption",
        "table_body",
        "list_items",
        "content",
        "img_caption",
        "image_caption",
        "formula",
        "code_body",
        "code_caption",
    ):
        text = _clean_text(block.get(field))
        if text and text not in values:
            values.append(text)
    return "\n".join(values)


def discover_parsed_documents(
    parsed_root: str | Path,
    *,
    categories: Iterable[str] | None = None,
) -> list[ParsedDocument]:
    root = Path(parsed_root).expanduser().resolve()
    selected = tuple(categories or CORPUS_SPECS)
    unknown = sorted(set(selected) - set(CORPUS_SPECS))
    if unknown:
        raise ValueError(f"unsupported corpus categories: {', '.join(unknown)}")
    documents: list[ParsedDocument] = []
    for category in selected:
        corpus_root = root / category
        if not corpus_root.is_dir():
            continue
        for path in sorted(corpus_root.rglob("*_content_list.json")):
            if path.name.endswith("_content_list_v2.json"):
                continue
            relative = path.relative_to(root).as_posix()
            title = path.stem.removesuffix("_content_list").strip()
            documents.append(
                ParsedDocument(
                    corpus=CORPUS_SPECS[category],
                    title=title,
                    relative_path=relative,
                    content_list_path=path,
                )
            )
    return documents


def load_content_list(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid MinerU content list {source}: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError(f"MinerU content list must be an array of objects: {source}")
    return payload


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    segments: list[str] = []
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
        segment = text[start:end].strip()
        if segment:
            segments.append(segment)
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return segments


def _split_clause_text(text: str) -> list[tuple[str | None, str]]:
    markers: dict[int, str] = {}
    for pattern in (_MARKDOWN_CLAUSE_RE, _LINE_CLAUSE_RE):
        for match in pattern.finditer(text):
            markers.setdefault(match.start(), match.group(1))
    if not markers:
        return [(None, text)]
    ordered = sorted(markers.items())
    units: list[tuple[str | None, str]] = []
    if ordered[0][0] > 0:
        prefix = text[: ordered[0][0]].strip()
        if prefix:
            units.append((None, prefix))
    for index, (start, clause_no) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(text)
        content = text[start:end].strip()
        if content:
            units.append((clause_no, content))
    return units


def chunk_content_list(
    blocks: Sequence[dict[str, Any]],
    *,
    corpus: CorpusSpec,
    document_title: str,
    max_chars: int = 360,
    overlap_chars: int = 60,
) -> list[KnowledgeChunk]:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if overlap_chars < 0 or overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be non-negative and less than half max_chars")

    chunks: list[KnowledgeChunk] = []
    section_levels: dict[int, str] = {}
    current_clause: str | None = None
    buffer: list[tuple[str, int, list[Any] | None]] = []

    def section_path() -> tuple[str, ...]:
        return tuple(section_levels[level] for level in sorted(section_levels))

    def flush(*, retain_overlap: bool = False) -> None:
        nonlocal buffer
        if not buffer:
            return
        content = "\n".join(piece[0] for piece in buffer).strip()
        if not content:
            buffer = []
            return
        pages = [piece[1] for piece in buffer]
        bbox_blocks = [
            {"page": page, "bbox": bbox}
            for _, page, bbox in buffer
            if isinstance(bbox, list) and len(bbox) == 4
        ]
        path = section_path()
        chunks.append(
            KnowledgeChunk(
                chunk_no=len(chunks) + 1,
                content=content,
                content_hash=_sha256_text(content),
                section_title=path[-1] if path else None,
                section_path=path,
                clause_no=current_clause,
                page_start=min(pages),
                page_end=max(pages),
                bbox_json={"blocks": bbox_blocks},
                token_count=len(content),
            )
        )
        if retain_overlap and overlap_chars and len(content) > overlap_chars:
            tail = content[-overlap_chars:].lstrip()
            last = buffer[-1]
            buffer = [(tail, last[1], last[2])] if tail else []
        else:
            buffer = []

    def add_piece(text: str, page: int, bbox: list[Any] | None) -> None:
        projected = len(text) + sum(len(piece[0]) + 1 for piece in buffer)
        if buffer and projected > max_chars:
            flush(retain_overlap=True)
        if buffer and len(text) + sum(len(piece[0]) + 1 for piece in buffer) > max_chars:
            flush(retain_overlap=False)
        buffer.append((text, page, bbox))

    last_page = 1
    for block in blocks:
        block_type = str(block.get("type", "text"))
        if block_type in _AUXILIARY_BLOCK_TYPES:
            continue
        text = _block_text(block)
        if not text:
            continue
        raw_page = block.get("page_idx")
        if isinstance(raw_page, int) and raw_page >= 0:
            last_page = raw_page + 1
        page = last_page
        bbox = block.get("bbox") if isinstance(block.get("bbox"), list) else None

        heading_level = block.get("text_level")
        if isinstance(heading_level, int) and heading_level > 0 and block_type == "text":
            flush(retain_overlap=False)
            section_levels[heading_level] = text[:512]
            for level in tuple(section_levels):
                if level > heading_level:
                    del section_levels[level]
            current_clause = None
            continue

        is_table = block_type in {"table", "chart"} or bool(block.get("table_body"))
        if is_table:
            flush(retain_overlap=False)
            for segment in _split_long_text(text, max_chars, overlap_chars):
                buffer.append((segment, page, bbox))
                flush(retain_overlap=False)
            continue

        for detected_clause, clause_text in _split_clause_text(text):
            if detected_clause:
                flush(retain_overlap=False)
                current_clause = detected_clause
            for segment in _split_long_text(clause_text, max_chars, overlap_chars):
                add_piece(segment, page, bbox)
    flush(retain_overlap=False)
    return chunks


def build_embedding_text(
    corpus: CorpusSpec,
    document_title: str,
    chunk: KnowledgeChunk,
    *,
    max_chars: int = 480,
) -> str:
    context = [f"知识类型：{corpus.category}", f"文档：{document_title}"]
    if chunk.section_path:
        context.append(f"章节：{' > '.join(chunk.section_path)}")
    if chunk.clause_no:
        context.append(f"条款：{chunk.clause_no}")
    context.append(f"正文：{chunk.content}")
    return "\n".join(context)[:max_chars]


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        batch_size: int = 16,
        timeout: float = 120,
        max_attempts: int = 3,
        session: Any | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        all_vectors: list[list[float]] = []
        for offset in range(0, len(texts), self.batch_size):
            batch = list(texts[offset : offset + self.batch_size])
            payload: dict[str, Any] | None = None
            last_error: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = self.session.post(
                        f"{self.base_url}/v1/embeddings",
                        json={"model": self.model, "input": batch},
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:  # noqa: BLE001 - transport adapters differ
                    last_error = exc
                    if attempt < self.max_attempts:
                        time.sleep(attempt * 2)
            if payload is None:
                raise RuntimeError(f"embedding request failed: {last_error}") from last_error
            data = sorted(payload.get("data", []), key=lambda item: item.get("index", -1))
            if len(data) != len(batch):
                raise ValueError(
                    f"embedding response count {len(data)} does not match request {len(batch)}"
                )
            for item in data:
                vector = [float(value) for value in item.get("embedding", [])]
                if len(vector) != self.dimensions:
                    raise ValueError(
                        f"embedding dimension {len(vector)} does not match {self.dimensions}"
                    )
                norm = math.sqrt(sum(value * value for value in vector))
                if norm == 0:
                    raise ValueError("embedding service returned a zero vector")
                all_vectors.append([value / norm for value in vector])
        return all_vectors


class PostgresKnowledgeStore:
    def __init__(
        self,
        database_url: str,
        *,
        tenant_id: str,
        model_code: str,
        model_name: str,
        model_revision: str | None,
        dimensions: int,
    ) -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL ingestion requires the 'knowledge' optional dependency"
            ) from exc
        self._psycopg = psycopg
        sanitized_url = database_url.strip().lstrip("\ufeff")
        try:
            self.connection = psycopg.connect(sanitized_url)
        except psycopg.Error as exc:
            raise RuntimeError(f"failed to connect to PostgreSQL: {type(exc).__name__}") from None
        self.tenant_id = tenant_id
        self.model_code = model_code
        self.model_name = model_name
        self.model_revision = model_revision
        self.dimensions = dimensions
        if dimensions != DEFAULT_DIMENSIONS:
            raise ValueError(f"this projection requires {DEFAULT_DIMENSIONS} dimensions")
        self.embedding_model_id = _stable_id("embedding-model", tenant_id, model_code)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
                    vector_id varchar(128) PRIMARY KEY,
                    chunk_id varchar(36) NOT NULL
                        REFERENCES kb_chunks(id) ON DELETE CASCADE,
                    embedding_model_id varchar(36) NOT NULL
                        REFERENCES kb_embedding_models(id) ON DELETE CASCADE,
                    tenant_id varchar(64) NOT NULL,
                    knowledge_base_id varchar(36) NOT NULL,
                    tender_project_id varchar(36),
                    document_id varchar(36) NOT NULL,
                    enabled boolean NOT NULL DEFAULT true,
                    embedding vector({DEFAULT_DIMENSIONS}) NOT NULL
                )
                """
            )
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_kb_vectors_bge_large_scope
                ON {VECTOR_TABLE} (tenant_id, knowledge_base_id, enabled)
                """
            )
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_kb_vectors_bge_large_hnsw
                ON {VECTOR_TABLE} USING hnsw (embedding vector_cosine_ops)
                """
            )
            now = _utc_now()
            cursor.execute(
                """
                INSERT INTO kb_embedding_models (
                    id, tenant_id, code, provider, model_name, model_revision,
                    dimensions, distance_metric, normalize, backend,
                    collection_name, status, config_json, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, 'vllm', %s, %s, %s, 'cosine', true,
                    'pgvector', %s, 'active', %s::json, %s, %s
                )
                ON CONFLICT (tenant_id, code) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    model_name = EXCLUDED.model_name,
                    model_revision = EXCLUDED.model_revision,
                    dimensions = EXCLUDED.dimensions,
                    distance_metric = EXCLUDED.distance_metric,
                    normalize = EXCLUDED.normalize,
                    backend = EXCLUDED.backend,
                    collection_name = EXCLUDED.collection_name,
                    status = EXCLUDED.status,
                    config_json = EXCLUDED.config_json,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (
                    self.embedding_model_id,
                    self.tenant_id,
                    self.model_code,
                    self.model_name,
                    self.model_revision,
                    self.dimensions,
                    VECTOR_TABLE,
                    json.dumps({"max_model_len": 512}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self.embedding_model_id = cursor.fetchone()[0]
        self.connection.commit()

    def ensure_corpus(self, corpus: CorpusSpec) -> str:
        knowledge_base_id = _stable_id("knowledge-base", self.tenant_id, corpus.code)
        member_id = _stable_id("knowledge-base-member", knowledge_base_id, self.tenant_id)
        now = _utc_now()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO kb_knowledge_bases (
                    id, tenant_id, code, name, kind, description, status,
                    owner_user_id, metadata_json, deleted_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'tender', %s, 'active', %s, %s::json, NULL, %s, %s)
                ON CONFLICT (tenant_id, code) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    status = 'active',
                    deleted_at = NULL,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (
                    knowledge_base_id,
                    self.tenant_id,
                    corpus.code,
                    corpus.name,
                    corpus.description,
                    self.tenant_id,
                    json.dumps({"category": corpus.category}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            knowledge_base_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO kb_knowledge_base_members (
                    id, knowledge_base_id, principal_type, principal_id, role,
                    granted_by, created_at, updated_at
                ) VALUES (%s, %s, 'user', %s, 'owner', %s, %s, %s)
                ON CONFLICT (knowledge_base_id, principal_type, principal_id)
                DO UPDATE SET role = 'owner', updated_at = EXCLUDED.updated_at
                """,
                (
                    member_id,
                    knowledge_base_id,
                    self.tenant_id,
                    self.tenant_id,
                    now,
                    now,
                ),
            )
        self.connection.commit()
        return knowledge_base_id

    def prepare_document(
        self,
        document: ParsedDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> tuple[str, str, str, list[tuple[str, KnowledgeChunk]]]:
        knowledge_base_id = self.ensure_corpus(document.corpus)
        document_code = f"{document.corpus.code}:{_sha256_text(document.relative_path)[:24]}"
        document_id = _stable_id("document", self.tenant_id, knowledge_base_id, document_code)
        source_bytes = document.content_list_path.read_bytes()
        source_hash = _sha256_bytes(source_bytes)
        now = _utc_now()
        metadata = {
            "category": document.corpus.category,
            "parsed_relative_path": document.relative_path,
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO kb_documents (
                    id, tenant_id, knowledge_base_id, tender_project_id,
                    document_code, title, document_type, document_side,
                    confidentiality, status, current_version_no, metadata_json,
                    deleted_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, NULL, %s, %s, %s, 'reference', 'internal',
                    'active', NULL, %s::json, NULL, %s, %s
                )
                ON CONFLICT (knowledge_base_id, document_code) DO UPDATE SET
                    title = EXCLUDED.title,
                    document_type = EXCLUDED.document_type,
                    metadata_json = EXCLUDED.metadata_json,
                    status = 'active', deleted_at = NULL,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (
                    document_id,
                    self.tenant_id,
                    knowledge_base_id,
                    document_code,
                    document.title,
                    document.corpus.document_type,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            document_id = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT id, version_no FROM kb_document_versions
                WHERE document_id = %s AND sha256 = %s
                """,
                (document_id, source_hash),
            )
            existing = cursor.fetchone()
            if existing:
                version_id, version_no = existing
            else:
                cursor.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM kb_document_versions WHERE document_id = %s",
                    (document_id,),
                )
                version_no = cursor.fetchone()[0]
                version_id = _stable_id("document-version", document_id, source_hash)
                pages = [chunk.page_end for chunk in chunks]
                cursor.execute(
                    """
                    INSERT INTO kb_document_versions (
                        id, document_id, version_no, source_type, source_uri,
                        object_uri, file_name, mime_type, sha256, file_size,
                        page_count, language, published_at, effective_from,
                        effective_to, parser_status, created_by, metadata_json,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, 'mineru', %s, %s, %s, 'application/json',
                        %s, %s, %s, 'zh-CN', NULL, NULL, NULL, 'completed',
                        %s, %s::json, %s, %s
                    )
                    """,
                    (
                        version_id,
                        document_id,
                        version_no,
                        document.content_list_path.as_uri(),
                        document.content_list_path.as_uri(),
                        document.content_list_path.name,
                        source_hash,
                        len(source_bytes),
                        max(pages, default=0),
                        self.tenant_id,
                        json.dumps(metadata, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            cursor.execute(
                """
                UPDATE kb_documents SET current_version_no = %s, updated_at = %s
                WHERE id = %s
                """,
                (version_no, now, document_id),
            )
            cursor.execute(
                "UPDATE kb_chunks SET enabled = false, updated_at = %s WHERE document_id = %s AND document_version_id <> %s AND enabled",
                (now, document_id, version_id),
            )
            cursor.execute(
                f"UPDATE {VECTOR_TABLE} SET enabled = false WHERE document_id = %s AND chunk_id IN (SELECT id FROM kb_chunks WHERE document_id = %s AND document_version_id <> %s)",
                (document_id, document_id, version_id),
            )

            chunk_rows: list[tuple[str, KnowledgeChunk]] = []
            for chunk in chunks:
                chunk_id = _stable_id("chunk", version_id, chunk.chunk_no)
                chunk_rows.append((chunk_id, chunk))
                cursor.execute(
                    """
                    INSERT INTO kb_chunks (
                        id, tenant_id, knowledge_base_id, tender_project_id,
                        document_id, document_version_id, chunk_no, content,
                        content_hash, section_title, section_path, clause_no,
                        page_start, page_end, bbox_json, token_count, enabled,
                        metadata_json, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s,
                        %s::json, %s, %s, %s, %s::json, %s, true, %s::json, %s, %s
                    )
                    ON CONFLICT (document_version_id, chunk_no) DO UPDATE SET
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        section_title = EXCLUDED.section_title,
                        section_path = EXCLUDED.section_path,
                        clause_no = EXCLUDED.clause_no,
                        page_start = EXCLUDED.page_start,
                        page_end = EXCLUDED.page_end,
                        bbox_json = EXCLUDED.bbox_json,
                        token_count = EXCLUDED.token_count,
                        enabled = true,
                        metadata_json = EXCLUDED.metadata_json,
                        updated_at = EXCLUDED.updated_at
                    RETURNING id
                    """,
                    (
                        chunk_id,
                        self.tenant_id,
                        knowledge_base_id,
                        document_id,
                        version_id,
                        chunk.chunk_no,
                        chunk.content,
                        chunk.content_hash,
                        chunk.section_title,
                        json.dumps(chunk.section_path, ensure_ascii=False),
                        chunk.clause_no,
                        chunk.page_start,
                        chunk.page_end,
                        json.dumps(chunk.bbox_json, ensure_ascii=False),
                        chunk.token_count,
                        json.dumps({"category": document.corpus.category}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                actual_chunk_id = cursor.fetchone()[0]
                if actual_chunk_id != chunk_id:
                    chunk_rows[-1] = (actual_chunk_id, chunk)
            cursor.execute(
                """
                UPDATE kb_chunks SET enabled = false, updated_at = %s
                WHERE document_version_id = %s AND chunk_no > %s AND enabled
                """,
                (now, version_id, len(chunks)),
            )
            cursor.execute(
                f"""
                UPDATE {VECTOR_TABLE} SET enabled = false
                WHERE chunk_id IN (
                    SELECT id FROM kb_chunks
                    WHERE document_version_id = %s AND NOT enabled
                )
                """,
                (version_id,),
            )
        self.connection.commit()
        return knowledge_base_id, document_id, version_id, chunk_rows

    def pending_chunks(
        self,
        chunk_rows: Sequence[tuple[str, KnowledgeChunk]],
        embedding_texts: dict[str, str],
    ) -> tuple[list[tuple[str, KnowledgeChunk, str, str, str]], int]:
        pending: list[tuple[str, KnowledgeChunk, str, str, str]] = []
        skipped = 0
        now = _utc_now()
        with self.connection.cursor() as cursor:
            for chunk_id, chunk in chunk_rows:
                embedding_input_hash = _sha256_text(embedding_texts[chunk_id])
                vector_id = _stable_id("vector", self.embedding_model_id, chunk_id)
                embedding_id = _stable_id("chunk-embedding", self.embedding_model_id, chunk_id)
                cursor.execute(
                    f"""
                    SELECT EXISTS (
                        SELECT 1
                        FROM kb_chunk_embeddings ce
                        JOIN {VECTOR_TABLE} v ON v.vector_id = ce.vector_id
                        WHERE ce.chunk_id = %s
                          AND ce.embedding_model_id = %s
                          AND ce.content_hash = %s
                          AND ce.sync_status = 'synced'
                    )
                    """,
                    (chunk_id, self.embedding_model_id, embedding_input_hash),
                )
                if cursor.fetchone()[0]:
                    skipped += 1
                    continue
                cursor.execute(
                    """
                    INSERT INTO kb_chunk_embeddings (
                        id, tenant_id, knowledge_base_id, chunk_id,
                        embedding_model_id, vector_id, backend, content_hash,
                        sync_status, indexed_at, last_error, created_at, updated_at
                    )
                    SELECT %s, %s, knowledge_base_id, id, %s, %s, 'pgvector',
                           %s, 'pending', NULL, NULL, %s, %s
                    FROM kb_chunks WHERE id = %s
                    ON CONFLICT (chunk_id, embedding_model_id) DO UPDATE SET
                        vector_id = EXCLUDED.vector_id,
                        backend = EXCLUDED.backend,
                        content_hash = EXCLUDED.content_hash,
                        sync_status = 'pending', indexed_at = NULL,
                        last_error = NULL, updated_at = EXCLUDED.updated_at
                    """,
                    (
                        embedding_id,
                        self.tenant_id,
                        self.embedding_model_id,
                        vector_id,
                        embedding_input_hash,
                        now,
                        now,
                        chunk_id,
                    ),
                )
                event_key = _stable_id("vector-outbox", vector_id, embedding_input_hash)
                cursor.execute(
                    """
                    INSERT INTO kb_vector_outbox (
                        event_key, tenant_id, knowledge_base_id, chunk_id,
                        embedding_model_id, operation, payload_json, status,
                        attempt_count, available_at, locked_at, locked_by,
                        last_error, created_at, processed_at
                    )
                    SELECT %s, %s, knowledge_base_id, id, %s, 'upsert', %s::json,
                           'pending', 0, %s, NULL, NULL, NULL, %s, NULL
                    FROM kb_chunks WHERE id = %s
                    ON CONFLICT (event_key) DO UPDATE SET
                        status = 'pending', available_at = EXCLUDED.available_at,
                        last_error = NULL, processed_at = NULL
                    """,
                    (
                        event_key,
                        self.tenant_id,
                        self.embedding_model_id,
                        json.dumps({"vector_id": vector_id, "content_hash": embedding_input_hash}),
                        now,
                        now,
                        chunk_id,
                    ),
                )
                pending.append((chunk_id, chunk, vector_id, event_key, embedding_input_hash))
        self.connection.commit()
        return pending, skipped

    def write_vectors(
        self,
        *,
        knowledge_base_id: str,
        document_id: str,
        rows: Sequence[tuple[str, KnowledgeChunk, str, str, str]],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        now = _utc_now()
        with self.connection.cursor() as cursor:
            for (chunk_id, _chunk, vector_id, event_key, embedding_input_hash), vector in zip(
                rows, vectors, strict=True
            ):
                vector_literal = "[" + ",".join(f"{value:.9g}" for value in vector) + "]"
                cursor.execute(
                    f"""
                    INSERT INTO {VECTOR_TABLE} (
                        vector_id, chunk_id, embedding_model_id, tenant_id,
                        knowledge_base_id, tender_project_id, document_id,
                        enabled, embedding
                    ) VALUES (%s, %s, %s, %s, %s, NULL, %s, true, %s::vector)
                    ON CONFLICT (vector_id) DO UPDATE SET
                        chunk_id = EXCLUDED.chunk_id,
                        embedding_model_id = EXCLUDED.embedding_model_id,
                        tenant_id = EXCLUDED.tenant_id,
                        knowledge_base_id = EXCLUDED.knowledge_base_id,
                        document_id = EXCLUDED.document_id,
                        enabled = true,
                        embedding = EXCLUDED.embedding
                    """,
                    (
                        vector_id,
                        chunk_id,
                        self.embedding_model_id,
                        self.tenant_id,
                        knowledge_base_id,
                        document_id,
                        vector_literal,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE kb_chunk_embeddings
                    SET sync_status = 'synced', indexed_at = %s,
                        last_error = NULL, updated_at = %s
                    WHERE chunk_id = %s AND embedding_model_id = %s
                      AND content_hash = %s
                    """,
                    (
                        now,
                        now,
                        chunk_id,
                        self.embedding_model_id,
                        embedding_input_hash,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE kb_vector_outbox
                    SET status = 'processed', processed_at = %s,
                        locked_at = NULL, locked_by = NULL, last_error = NULL
                    WHERE event_key = %s
                    """,
                    (now, event_key),
                )
        self.connection.commit()

    def start_job(
        self,
        *,
        knowledge_base_id: str,
        document_version_id: str,
        total_items: int,
    ) -> str:
        job_id = str(uuid4())
        now = _utc_now()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO kb_ingestion_jobs (
                    id, tenant_id, knowledge_base_id, document_version_id,
                    embedding_model_id, job_type, stage, status, attempt_count,
                    processed_items, total_items, parser_name, parser_version,
                    started_at, finished_at, last_error, config_json,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, 'full', 'embedding', 'running', 1,
                    0, %s, 'MinerU', NULL, %s, NULL, NULL, %s::json, %s, %s
                )
                """,
                (
                    job_id,
                    self.tenant_id,
                    knowledge_base_id,
                    document_version_id,
                    self.embedding_model_id,
                    total_items,
                    now,
                    json.dumps({"model_code": self.model_code, "vector_table": VECTOR_TABLE}),
                    now,
                    now,
                ),
            )
        self.connection.commit()
        return job_id

    def finish_job(
        self,
        job_id: str,
        *,
        processed_items: int,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE kb_ingestion_jobs
                SET stage = %s, status = %s, processed_items = %s,
                    finished_at = %s, last_error = %s, updated_at = %s
                WHERE id = %s
                """,
                (
                    "failed" if error else "completed",
                    "failed" if error else "completed",
                    processed_items,
                    now,
                    error,
                    now,
                    job_id,
                ),
            )
        self.connection.commit()


def ingest_document(
    store: PostgresKnowledgeStore,
    client: EmbeddingClient,
    document: ParsedDocument,
    *,
    chunk_chars: int,
    overlap_chars: int,
    max_embedding_chars: int,
) -> DocumentIngestionResult:
    blocks = load_content_list(document.content_list_path)
    chunks = chunk_content_list(
        blocks,
        corpus=document.corpus,
        document_title=document.title,
        max_chars=chunk_chars,
        overlap_chars=overlap_chars,
    )
    knowledge_base_id, document_id, version_id, chunk_rows = store.prepare_document(
        document, chunks
    )
    embedding_texts = {
        chunk_id: build_embedding_text(
            document.corpus,
            document.title,
            chunk,
            max_chars=max_embedding_chars,
        )
        for chunk_id, chunk in chunk_rows
    }
    pending, skipped = store.pending_chunks(chunk_rows, embedding_texts)
    job_id = store.start_job(
        knowledge_base_id=knowledge_base_id,
        document_version_id=version_id,
        total_items=len(chunks),
    )
    embedded = 0
    try:
        for offset in range(0, len(pending), client.batch_size):
            batch = pending[offset : offset + client.batch_size]
            texts = [embedding_texts[row[0]] for row in batch]
            vectors = client.embed(texts)
            store.write_vectors(
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                rows=batch,
                vectors=vectors,
            )
            embedded += len(batch)
        store.finish_job(job_id, processed_items=embedded + skipped)
    except Exception as exc:
        store.finish_job(job_id, processed_items=embedded + skipped, error=str(exc)[:4000])
        raise
    return DocumentIngestionResult(
        title=document.title,
        corpus_code=document.corpus.code,
        chunks=len(chunks),
        embedded=embedded,
        skipped=skipped,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parsed-root", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8097")
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--model-revision")
    parser.add_argument("--model-code", default="bge-large-zh-v1.5-v1")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--category", action="append", choices=tuple(CORPUS_SPECS))
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--chunk-chars", type=int, default=360)
    parser.add_argument("--overlap-chars", type=int, default=60)
    parser.add_argument("--max-embedding-chars", type=int, default=480)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    documents = discover_parsed_documents(args.parsed_root, categories=args.category)
    if args.limit is not None:
        documents = documents[: args.limit]
    if not documents:
        parser.error(f"no supported MinerU content lists found under {args.parsed_root}")
    if args.dry_run:
        total_chunks = 0
        for document in documents:
            chunks = chunk_content_list(
                load_content_list(document.content_list_path),
                corpus=document.corpus,
                document_title=document.title,
                max_chars=args.chunk_chars,
                overlap_chars=args.overlap_chars,
            )
            total_chunks += len(chunks)
            print(
                f"{document.corpus.code}|chunks={len(chunks)}|{document.title}",
                flush=True,
            )
        print(f"documents={len(documents)} chunks={total_chunks} dry_run=true", flush=True)
        return 0
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    client = EmbeddingClient(
        base_url=args.embedding_url,
        model=args.embedding_model,
        dimensions=DEFAULT_DIMENSIONS,
        batch_size=args.batch_size,
    )
    totals = {"documents": 0, "chunks": 0, "embedded": 0, "skipped": 0}
    with PostgresKnowledgeStore(
        args.database_url,
        tenant_id=args.tenant_id,
        model_code=args.model_code,
        model_name=args.model_name,
        model_revision=args.model_revision,
        dimensions=DEFAULT_DIMENSIONS,
    ) as store:
        store.ensure_schema()
        for index, document in enumerate(documents, start=1):
            result = ingest_document(
                store,
                client,
                document,
                chunk_chars=args.chunk_chars,
                overlap_chars=args.overlap_chars,
                max_embedding_chars=args.max_embedding_chars,
            )
            totals["documents"] += 1
            totals["chunks"] += result.chunks
            totals["embedded"] += result.embedded
            totals["skipped"] += result.skipped
            print(
                f"[{index}/{len(documents)}] {result.corpus_code} "
                f"chunks={result.chunks} embedded={result.embedded} "
                f"skipped={result.skipped} {result.title}",
                flush=True,
            )
    print(" ".join(f"{key}={value}" for key, value in totals.items()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

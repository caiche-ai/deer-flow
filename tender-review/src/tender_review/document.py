"""MinerU document loading, chunking and evidence-grounded retrieval."""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_SPACE_RE = re.compile(r"\s+")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value if item)
    text = html.unescape(_TAG_RE.sub(" ", str(value)))
    return _SPACE_RE.sub(" ", text).strip()


def _block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in (
        "text",
        "table_caption",
        "table_body",
        "img_caption",
        "image_caption",
        "formula",
    ):
        text = _clean_text(block.get(field))
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _normalise_for_match(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _ASCII_WORD_RE.findall(lowered)
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return cleaned or "tender-document"


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    """A stable, page-scoped piece of evidence exposed to the agent."""

    evidence_id: str
    page: int
    chunk: int
    block_start: int
    block_end: int
    text: str
    bboxes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked evidence result."""

    evidence: EvidenceChunk
    score: float


class MineruDocument:
    """Read a MinerU ``content_list.json`` and expose bounded retrieval primitives."""

    def __init__(
        self,
        *,
        source_path: Path,
        document_id: str,
        raw_blocks: list[dict[str, Any]],
        chunk_chars: int = 1800,
    ) -> None:
        if chunk_chars < 300:
            raise ValueError("chunk_chars must be at least 300")
        self.source_path = source_path
        self.document_id = _safe_identifier(document_id)
        self._raw_blocks = tuple(raw_blocks)
        self._block_type_counts = Counter(str(block.get("type", "unknown")) for block in raw_blocks)
        self._chunks = self._build_chunks(raw_blocks, chunk_chars)
        self._chunks_by_id = {chunk.evidence_id: chunk for chunk in self._chunks}
        self._chunks_by_page: dict[int, list[EvidenceChunk]] = defaultdict(list)
        for chunk in self._chunks:
            self._chunks_by_page[chunk.page].append(chunk)
        self._token_counts = [Counter(_tokens(chunk.text)) for chunk in self._chunks]
        self._document_frequency = Counter(
            token for counts in self._token_counts for token in counts
        )
        self._average_length = (
            sum(sum(counts.values()) for counts in self._token_counts) / len(self._token_counts)
            if self._token_counts
            else 1.0
        )

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        document_id: str | None = None,
        chunk_chars: int = 1800,
    ) -> MineruDocument:
        source_path = Path(path).expanduser().resolve()
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"MinerU content list not found: {source_path}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid MinerU JSON at {source_path}: {exc}") from exc
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("MinerU content list must be a JSON array of objects")
        inferred_id = source_path.stem.removesuffix("_content_list")
        return cls(
            source_path=source_path,
            document_id=document_id or inferred_id,
            raw_blocks=payload,
            chunk_chars=chunk_chars,
        )

    @property
    def page_count(self) -> int:
        pages = [block.get("page_idx") for block in self._raw_blocks]
        valid_pages = [page for page in pages if isinstance(page, int) and page >= 0]
        return max(valid_pages, default=-1) + 1

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(self._chunks_by_id)

    @property
    def evidence_chunks(self) -> tuple[EvidenceChunk, ...]:
        """Return immutable page-scoped parent chunks used for exact evidence reads."""

        return self._chunks

    def overview(self, heading_limit: int = 80) -> dict[str, Any]:
        headings: list[dict[str, Any]] = []
        for block in self._raw_blocks:
            level = block.get("text_level")
            text = _block_text(block)
            page_idx = block.get("page_idx")
            if text and isinstance(level, int) and isinstance(page_idx, int):
                headings.append({"page": page_idx + 1, "level": level, "text": text[:300]})
                if len(headings) >= heading_limit:
                    break
        return {
            "document_id": self.document_id,
            "source": str(self.source_path),
            "pages": self.page_count,
            "evidence_chunks": self.chunk_count,
            "block_types": dict(sorted(self._block_type_counts.items())),
            "headings": headings,
            "heading_list_truncated": len(headings) == heading_limit,
        }

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> tuple[SearchHit, ...]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        self._validate_page_range(page_start, page_end)

        query_counts = Counter(_tokens(query))
        normalised_query = _normalise_for_match(query)
        total_documents = max(len(self._chunks), 1)
        scored: list[SearchHit] = []
        for index, chunk in enumerate(self._chunks):
            if page_start is not None and chunk.page < page_start:
                continue
            if page_end is not None and chunk.page > page_end:
                continue
            counts = self._token_counts[index]
            length = max(sum(counts.values()), 1)
            score = 0.0
            for token, query_frequency in query_counts.items():
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency[token]
                inverse_frequency = math.log(
                    1 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.2 * (0.25 + 0.75 * length / self._average_length)
                score += query_frequency * inverse_frequency * frequency * 2.2 / denominator
            if normalised_query and normalised_query in _normalise_for_match(chunk.text):
                score += 8.0
            if score > 0:
                scored.append(SearchHit(evidence=chunk, score=round(score, 6)))
        scored.sort(key=lambda hit: (-hit.score, hit.evidence.page, hit.evidence.chunk))
        return tuple(scored[:top_k])

    def read_pages(self, start_page: int, end_page: int) -> tuple[EvidenceChunk, ...]:
        if end_page - start_page + 1 > 10:
            raise ValueError("read at most 10 pages per call")
        self._validate_page_range(start_page, end_page)
        return tuple(
            chunk
            for page in range(start_page, end_page + 1)
            for chunk in self._chunks_by_page.get(page, ())
        )

    def get_evidence(self, evidence_ids: list[str]) -> tuple[EvidenceChunk, ...]:
        if not evidence_ids:
            raise ValueError("evidence_ids must not be empty")
        if len(evidence_ids) > 20:
            raise ValueError("request at most 20 evidence chunks per call")
        missing = [
            evidence_id for evidence_id in evidence_ids if evidence_id not in self._chunks_by_id
        ]
        if missing:
            raise ValueError(f"unknown evidence ids: {', '.join(missing)}")
        return tuple(self._chunks_by_id[evidence_id] for evidence_id in evidence_ids)

    def _validate_page_range(self, page_start: int | None, page_end: int | None) -> None:
        if page_start is not None and page_start < 1:
            raise ValueError("page_start must be at least 1")
        if page_end is not None and page_end < 1:
            raise ValueError("page_end must be at least 1")
        if page_start is not None and page_end is not None and page_start > page_end:
            raise ValueError("page_start must not exceed page_end")
        if page_end is not None and page_end > self.page_count:
            raise ValueError(f"page_end exceeds document page count {self.page_count}")

    def _build_chunks(
        self, raw_blocks: list[dict[str, Any]], chunk_chars: int
    ) -> tuple[EvidenceChunk, ...]:
        page_blocks: dict[int, list[tuple[int, str, list[Any] | None]]] = defaultdict(list)
        for block_index, block in enumerate(raw_blocks, start=1):
            page_idx = block.get("page_idx")
            text = _block_text(block)
            if isinstance(page_idx, int) and page_idx >= 0 and text:
                raw_bbox = block.get("bbox")
                bbox = raw_bbox if isinstance(raw_bbox, list) and len(raw_bbox) == 4 else None
                page_blocks[page_idx + 1].append((block_index, text, bbox))

        chunks: list[EvidenceChunk] = []
        for page in sorted(page_blocks):
            chunk_number = 0
            current: list[str] = []
            current_indices: list[int] = []
            current_bboxes: dict[int, list[Any]] = {}

            def flush(page_number: int = page) -> None:
                nonlocal chunk_number, current, current_indices, current_bboxes
                if not current:
                    return
                chunk_number += 1
                chunks.append(
                    EvidenceChunk(
                        evidence_id=(f"{self.document_id}:page={page_number}:chunk={chunk_number}"),
                        page=page_number,
                        chunk=chunk_number,
                        block_start=min(current_indices),
                        block_end=max(current_indices),
                        text="\n".join(current),
                        bboxes=tuple(
                            {"block": block_index, "bbox": bbox}
                            for block_index, bbox in sorted(current_bboxes.items())
                        ),
                    )
                )
                current = []
                current_indices = []
                current_bboxes = {}

            for block_index, text, bbox in page_blocks[page]:
                segments = [
                    text[offset : offset + chunk_chars]
                    for offset in range(0, len(text), chunk_chars)
                ]
                for segment in segments:
                    projected = sum(len(item) for item in current) + len(segment)
                    if current and projected > chunk_chars:
                        flush()
                    current.append(segment)
                    current_indices.append(block_index)
                    if bbox is not None:
                        current_bboxes[block_index] = bbox
            flush()
        return tuple(chunks)


def resolve_mineru_content_path(document_path: str | Path, parsed_root: str | Path) -> Path:
    """Resolve a source document to its sibling MinerU content-list artifact."""

    document = Path(document_path).expanduser()
    parsed = Path(parsed_root).expanduser()
    stem = document.stem

    relative_parent = Path()
    parts = list(document.parent.parts)
    if "招投标" in parts:
        relative_parent = Path(*parts[parts.index("招投标") + 1 :])
    elif document.parent.name:
        relative_parent = Path(document.parent.name)

    candidate = parsed / relative_parent / stem / "hybrid_auto" / f"{stem}_content_list.json"
    if candidate.is_file():
        return candidate.resolve()

    matches = [
        path
        for path in parsed.rglob(f"{stem}_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    if len(matches) == 1:
        return matches[0].resolve()
    if not matches:
        raise FileNotFoundError(
            f"No MinerU content list found for {document}; expected {candidate}"
        )
    raise ValueError(
        f"Multiple MinerU content lists found for {document}: "
        + ", ".join(str(path) for path in matches)
    )

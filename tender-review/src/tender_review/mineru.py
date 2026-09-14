"""Small, synchronous MinerU HTTP client for chat-uploaded tender PDFs."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import requests

DEFAULT_API_URL = "http://172.19.2.2:18000/mineru/file_parse"
DEFAULT_BACKEND = "hybrid-auto-engine"
DEFAULT_TIMEOUT = 3600


@dataclass(frozen=True, slots=True)
class MineruParseArtifacts:
    markdown_path: Path
    content_list_path: Path
    page_count: int
    elapsed_seconds: float


def _safe_archive_names(archive: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    for member in archive.infolist():
        normalized = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe ZIP member: {member.filename!r}")
        if not member.is_dir():
            names.append(member.filename)
    return names


def _select_artifact(names: list[str], suffix: str) -> str | None:
    matches = sorted(name for name in names if name.endswith(suffix))
    return matches[0] if matches else None


def _clean_block_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value if item)
    return re.sub(r"\n{3,}", "\n\n", str(value)).strip()


def _block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ("text", "table_caption", "table_body", "img_caption", "image_caption", "formula"):
        text = _clean_block_value(block.get(field))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


def _render_page_markdown(source_name: str, blocks: list[dict[str, Any]]) -> tuple[str, int]:
    valid_pages = [
        block.get("page_idx") for block in blocks if isinstance(block.get("page_idx"), int)
    ]
    page_count = max((page for page in valid_pages if page >= 0), default=-1) + 1
    lines = [
        f"# {source_name}",
        "",
        f"> MinerU 解析文本；共 {page_count} 页。页码为原 PDF 物理页码。",
        "",
    ]
    for page_idx in range(page_count):
        lines.extend((f"<!-- page: {page_idx + 1} -->", f"## 第 {page_idx + 1} 页", ""))
        page_has_text = False
        for block_index, block in enumerate(blocks, start=1):
            if block.get("page_idx") != page_idx:
                continue
            text = _block_text(block)
            if not text:
                continue
            page_has_text = True
            lines.extend((f"<!-- block: {block_index} -->", text, ""))
        if not page_has_text:
            lines.extend(("_本页未提取到可读文本。_", ""))
    return "\n".join(lines).rstrip() + "\n", page_count


def parse_pdf_with_mineru(
    source_path: str | Path,
    *,
    api_url: str | None = None,
    backend: str | None = None,
    timeout: int | None = None,
) -> MineruParseArtifacts:
    """Parse one PDF and materialize searchable sidecars beside the upload."""

    source = Path(source_path).resolve()
    if source.suffix.lower() != ".pdf":
        raise ValueError("MinerU chat conversion currently accepts PDF files only")
    endpoint = api_url or os.getenv("TENDER_REVIEW_MINERU_API_URL", DEFAULT_API_URL)
    engine = backend or os.getenv("TENDER_REVIEW_MINERU_BACKEND", DEFAULT_BACKEND)
    read_timeout = timeout or int(os.getenv("TENDER_REVIEW_MINERU_TIMEOUT", str(DEFAULT_TIMEOUT)))
    mime_type = mimetypes.guess_type(source.name)[0] or "application/pdf"
    form = {
        "backend": engine,
        "lang_list": "ch",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "image_analysis": "true",
        "return_md": "true",
        "return_content_list": "true",
        "return_images": "false",
        "response_format_zip": "true",
        "return_original_file": "false",
    }
    started = time.monotonic()
    with source.open("rb") as file_handle:
        response = requests.post(
            endpoint,
            data=form,
            files={"files": (source.name, file_handle, mime_type)},
            timeout=(30, read_timeout),
        )
    if response.status_code != 200 or not response.content.startswith(b"PK"):
        detail = response.text[:1000]
        raise RuntimeError(f"MinerU HTTP {response.status_code}: {detail}")

    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            names = _safe_archive_names(archive)
            content_name = _select_artifact(names, "_content_list.json")
            if content_name is None:
                raise RuntimeError("MinerU ZIP contains no primary content_list JSON")
            raw_content_list = archive.read(content_name)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("MinerU returned an invalid ZIP archive") from exc

    try:
        payload = json.loads(raw_content_list.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MinerU returned an invalid content_list JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("MinerU content_list must be an array of objects")

    markdown, page_count = _render_page_markdown(source.name, payload)
    markdown_path = source.with_suffix(".md")
    content_list_path = source.with_name(f"{source.stem}_content_list.json")
    markdown_tmp = markdown_path.with_name(f".{markdown_path.name}.tmp")
    content_tmp = content_list_path.with_name(f".{content_list_path.name}.tmp")
    try:
        markdown_tmp.write_text(markdown, encoding="utf-8")
        content_tmp.write_bytes(raw_content_list)
        markdown_tmp.replace(markdown_path)
        content_tmp.replace(content_list_path)
    finally:
        markdown_tmp.unlink(missing_ok=True)
        content_tmp.unlink(missing_ok=True)

    return MineruParseArtifacts(
        markdown_path=markdown_path,
        content_list_path=content_list_path,
        page_count=page_count,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse one PDF through the configured MinerU API")
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()
    result = parse_pdf_with_mineru(args.pdf)
    print(
        json.dumps(
            {
                "markdown_path": str(result.markdown_path),
                "content_list_path": str(result.content_list_path),
                "page_count": result.page_count,
                "elapsed_seconds": result.elapsed_seconds,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Batch-parse tender documents through a MinerU HTTP API.

The runner preserves the source directory layout, limits concurrency, writes an
incremental manifest, and refuses unsafe paths in returned ZIP archives. Legacy
DOC/XLS inputs can be paired with pre-converted DOCX/XLSX files via
``--converted-root`` while retaining the original source identity in the manifest.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
LEGACY_CONVERSIONS = {".doc": ".docx", ".xls": ".xlsx"}


@dataclass(frozen=True, slots=True)
class ParseJob:
    source_relative: str
    input_path: str
    converted: bool


@dataclass(slots=True)
class ParseResult:
    source_relative: str
    input_path: str
    converted: bool
    status: str
    elapsed_seconds: float
    attempts: int = 1
    content_list: str | None = None
    markdown: str | None = None
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def discover_jobs(source_root: Path, converted_root: Path | None) -> list[ParseJob]:
    jobs: list[ParseJob] = []
    unsupported: list[str] = []
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = source.relative_to(source_root)
        suffix = source.suffix.lower()
        if suffix in SUPPORTED_SUFFIXES:
            jobs.append(ParseJob(relative.as_posix(), str(source), False))
            continue
        converted_suffix = LEGACY_CONVERSIONS.get(suffix)
        if converted_suffix and converted_root is not None:
            converted = converted_root / relative.with_suffix(converted_suffix)
            if converted.is_file():
                jobs.append(ParseJob(relative.as_posix(), str(converted), True))
                continue
        unsupported.append(relative.as_posix())
    if unsupported:
        formatted = "\n".join(f"  - {path}" for path in unsupported)
        raise RuntimeError(f"unsupported files without conversions:\n{formatted}")
    return jobs


def _existing_result(output_root: Path, job: ParseJob) -> tuple[Path, Path | None] | None:
    relative = Path(job.source_relative)
    document_root = output_root / relative.parent / relative.stem
    if not document_root.is_dir():
        return None
    content_lists = sorted(
        path
        for path in document_root.rglob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    if not content_lists:
        return None
    markdowns = sorted(document_root.rglob("*.md"))
    return content_lists[0], markdowns[0] if markdowns else None


def _safe_extract(payload: bytes, output_parent: Path) -> tuple[Path, Path | None]:
    output_parent.mkdir(parents=True, exist_ok=True)
    base = output_parent.resolve()
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for member in archive.infolist():
            target = (output_parent / member.filename).resolve()
            if target != base and base not in target.parents:
                raise RuntimeError(f"unsafe ZIP member: {member.filename!r}")
        archive.extractall(output_parent)
        names = [member.filename for member in archive.infolist()]

    content_names = [
        name
        for name in names
        if name.endswith("_content_list.json") and not name.endswith("_content_list_v2.json")
    ]
    if not content_names:
        raise RuntimeError("MinerU ZIP contains no primary content_list JSON")
    markdown_names = [name for name in names if name.endswith(".md")]
    content_list = output_parent / content_names[0]
    markdown = output_parent / markdown_names[0] if markdown_names else None
    return content_list, markdown


def parse_one(
    job: ParseJob,
    *,
    source_root: Path,
    output_root: Path,
    api_url: str,
    backend: str,
    timeout: int,
    max_attempts: int,
) -> ParseResult:
    started = time.monotonic()
    existing = _existing_result(output_root, job)
    if existing is not None:
        content_list, markdown = existing
        return ParseResult(
            source_relative=job.source_relative,
            input_path=job.input_path,
            converted=job.converted,
            status="skipped",
            elapsed_seconds=0.0,
            attempts=0,
            content_list=str(content_list.relative_to(output_root)),
            markdown=str(markdown.relative_to(output_root)) if markdown else None,
        )

    input_path = Path(job.input_path)
    mime_type = mimetypes.guess_type(input_path.name)[0] or "application/octet-stream"
    form = {
        "backend": backend,
        "lang_list": "ch",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "image_analysis": "true",
        "return_md": "true",
        "return_content_list": "true",
        "return_images": "true",
        "response_format_zip": "true",
        "return_original_file": "false",
    }
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with input_path.open("rb") as file_handle:
                response = requests.post(
                    api_url,
                    data=form,
                    files={"files": (input_path.name, file_handle, mime_type)},
                    timeout=(30, timeout),
                )
            if response.status_code != 200 or not response.content.startswith(b"PK"):
                detail = response.text[:1000]
                raise RuntimeError(f"HTTP {response.status_code}: {detail}")

            relative = Path(job.source_relative)
            content_list, markdown = _safe_extract(response.content, output_root / relative.parent)
            return ParseResult(
                source_relative=job.source_relative,
                input_path=job.input_path,
                converted=job.converted,
                status="success",
                elapsed_seconds=round(time.monotonic() - started, 3),
                attempts=attempt,
                content_list=str(content_list.relative_to(output_root)),
                markdown=str(markdown.relative_to(output_root)) if markdown else None,
            )
        except Exception as exc:  # noqa: BLE001 - transient API failures are retried
            last_error = exc
            if attempt < max_attempts:
                time.sleep(30 * attempt)

    return ParseResult(
        source_relative=job.source_relative,
        input_path=job.input_path,
        converted=job.converted,
        status="failed",
        elapsed_seconds=round(time.monotonic() - started, 3),
        attempts=max_attempts,
        error=f"{type(last_error).__name__}: {last_error}",
    )


def write_manifest(
    manifest_path: Path,
    *,
    source_root: Path,
    output_root: Path,
    api_url: str,
    backend: str,
    started_at: str,
    results: list[ParseResult],
) -> None:
    counts = {
        status: sum(result.status == status for result in results)
        for status in ("success", "skipped", "failed")
    }
    payload = {
        "started_at": started_at,
        "updated_at": _now(),
        "source_root": str(source_root),
        "output_root": str(output_root),
        "api_url": api_url,
        "backend": backend,
        "counts": counts,
        "results": [asdict(result) for result in sorted(results, key=lambda item: item.source_relative)],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, manifest_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--converted-root", type=Path)
    parser.add_argument(
        "--api-url",
        default="http://172.19.2.2:18000/mineru/file_parse",
    )
    parser.add_argument("--backend", default="hybrid-auto-engine")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    converted_root = args.converted_root.resolve() if args.converted_root else None
    manifest_path = args.manifest or output_root / "manifest.json"
    if not source_root.is_dir():
        parser.error(f"source root does not exist: {source_root}")
    if args.workers < 1 or args.workers > 3:
        parser.error("workers must be between 1 and the MinerU service limit of 3")

    jobs = discover_jobs(source_root, converted_root)
    previous: dict[str, Any] = {}
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous_by_source = {
        item["source_relative"]: item for item in previous.get("results", [])
    }
    results: list[ParseResult] = []
    pending_jobs: list[ParseJob] = []
    for job in jobs:
        old = previous_by_source.get(job.source_relative)
        if old and old.get("status") == "success" and _existing_result(output_root, job):
            old.setdefault("attempts", 1)
            results.append(ParseResult(**old))
        else:
            pending_jobs.append(job)
    started_at = previous.get("started_at") or _now()
    print(
        f"discovered={len(jobs)} retained={len(results)} pending={len(pending_jobs)} "
        f"workers={args.workers} backend={args.backend}",
        flush=True,
    )
    write_manifest(
        manifest_path,
        source_root=source_root,
        output_root=output_root,
        api_url=args.api_url,
        backend=args.backend,
        started_at=started_at,
        results=results,
    )

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                parse_one,
                job,
                source_root=source_root,
                output_root=output_root,
                api_url=args.api_url,
                backend=args.backend,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
            ): job
            for job in pending_jobs
        }
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                write_manifest(
                    manifest_path,
                    source_root=source_root,
                    output_root=output_root,
                    api_url=args.api_url,
                    backend=args.backend,
                    started_at=started_at,
                    results=results,
                )
                print(
                    f"[{len(results):02d}/{len(jobs):02d}] {result.status:<7} "
                    f"{result.elapsed_seconds:8.3f}s {result.source_relative}",
                    flush=True,
                )

    failures = [result for result in results if result.status == "failed"]
    print(f"completed={len(results)} failed={len(failures)} manifest={manifest_path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

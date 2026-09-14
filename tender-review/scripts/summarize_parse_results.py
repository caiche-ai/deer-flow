"""Build per-document MinerU parsing statistics from a batch manifest."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def pdf_page_count(path: Path) -> int | None:
    try:
        completed = subprocess.run(
            ["pdfinfo", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    return None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def document_stats(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    source_root = Path(manifest["source_root"])
    output_root = Path(manifest["output_root"])
    source_path = source_root / result["source_relative"]
    content_path = output_root / result["content_list"] if result.get("content_list") else None
    markdown_path = output_root / result["markdown"] if result.get("markdown") else None

    parsed_pages: set[int] = set()
    type_counts: Counter[str] = Counter()
    block_count = 0
    if content_path and content_path.is_file():
        items = json.loads(content_path.read_text(encoding="utf-8"))
        if isinstance(items, list):
            block_count = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                page_idx = item.get("page_idx")
                if isinstance(page_idx, int) and page_idx >= 0:
                    parsed_pages.add(page_idx + 1)
                type_counts[str(item.get("type") or "unknown")] += 1

    suffix = source_path.suffix.lower()
    source_pages = pdf_page_count(source_path) if suffix == ".pdf" else None
    parsed_page_count = len(parsed_pages)
    missing_content_pages = (
        sorted(set(range(1, source_pages + 1)) - parsed_pages)
        if source_pages is not None
        else []
    )
    duration = float(result.get("elapsed_seconds") or 0.0)
    profile_dir = content_path.parent if content_path else None
    image_count = 0
    if profile_dir and profile_dir.is_dir():
        image_count = sum(
            path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            for path in profile_dir.rglob("*")
            if path.is_file()
        )

    return {
        "source_relative": result["source_relative"],
        "source_format": suffix.removeprefix("."),
        "source_size_bytes": source_path.stat().st_size if source_path.is_file() else None,
        "converted_for_mineru": bool(result.get("converted")),
        "status": result["status"],
        "elapsed_seconds": duration,
        "attempts": int(result.get("attempts") or 1),
        "page_count": source_pages if source_pages is not None else parsed_page_count,
        "source_pdf_pages": source_pages,
        "parsed_page_count": parsed_page_count,
        "parsed_first_page": min(parsed_pages) if parsed_pages else None,
        "parsed_last_page": max(parsed_pages) if parsed_pages else None,
        "pages_without_content_blocks": missing_content_pages,
        "page_coverage_matches_source": (
            min(parsed_pages, default=None) == 1
            and max(parsed_pages, default=None) == source_pages
            if source_pages is not None
            else None
        ),
        "seconds_per_page": round(duration / parsed_page_count, 3)
        if duration and parsed_page_count
        else None,
        "block_count": block_count,
        "text_blocks": type_counts["text"],
        "title_blocks": type_counts["title"],
        "table_blocks": type_counts["table"],
        "image_blocks": type_counts["image"],
        "formula_blocks": type_counts["equation"],
        "extracted_image_files": image_count,
        "markdown_size_bytes": markdown_path.stat().st_size
        if markdown_path and markdown_path.is_file()
        else None,
        "content_list_size_bytes": content_path.stat().st_size
        if content_path and content_path.is_file()
        else None,
        "content_list": result.get("content_list"),
        "markdown": result.get("markdown"),
        "error": result.get("error"),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def write_markdown(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# MinerU 文档解析统计",
        "",
        f"- 文档总数：{summary['document_count']}",
        f"- 成功/跳过/失败：{summary['success_count']}/{summary['skipped_count']}/{summary['failed_count']}",
        f"- 总页数：{summary['total_pages']}",
        f"- API 累计耗时：{summary['total_api_seconds']:.3f} 秒",
        f"- 批处理墙钟耗时：{summary['wall_clock_seconds']} 秒",
        "",
        "| 文档 | 格式 | 总页数 | 内容页 | 无内容页 | 块数 | 表格 | 图片 | 耗时(s) | 尝试 | 状态 |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        missing_pages = ",".join(str(page) for page in row["pages_without_content_blocks"]) or "-"
        lines.append(
            "| {source_relative} | {source_format} | {page_count} | {parsed_page_count} | "
            f"{missing_pages} | "
            "{block_count} | {table_blocks} | {image_blocks} | {elapsed_seconds:.3f} | "
            "{attempts} | {status} |".format(**row)
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = [document_stats(result, manifest) for result in manifest.get("results", [])]
    rows.sort(key=lambda row: row["source_relative"])
    started_at = parse_datetime(manifest.get("started_at"))
    updated_at = parse_datetime(manifest.get("updated_at"))
    wall_clock_seconds = (
        round((updated_at - started_at).total_seconds(), 3)
        if started_at is not None and updated_at is not None
        else None
    )
    summary = {
        "document_count": len(rows),
        "success_count": sum(row["status"] == "success" for row in rows),
        "skipped_count": sum(row["status"] == "skipped" for row in rows),
        "failed_count": sum(row["status"] == "failed" for row in rows),
        "total_pages": sum(int(row["page_count"] or 0) for row in rows),
        "total_blocks": sum(int(row["block_count"] or 0) for row in rows),
        "total_api_seconds": round(sum(float(row["elapsed_seconds"] or 0) for row in rows), 3),
        "wall_clock_seconds": wall_clock_seconds,
    }
    output = {"summary": summary, "documents": rows}
    json_path = args.output_prefix.with_suffix(".json")
    csv_path = args.output_prefix.with_suffix(".csv")
    markdown_path = args.output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, rows)
    write_markdown(markdown_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"json={json_path} csv={csv_path} markdown={markdown_path}", flush=True)
    return 1 if summary["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

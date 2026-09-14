import json
import zipfile
from io import BytesIO

import pytest

from tender_review.mineru import parse_pdf_with_mineru


def _mineru_zip(*, markdown: str = "# 原始结果") -> bytes:
    payload = BytesIO()
    blocks = [
        {"type": "text", "text": "第一章 招标公告", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "投标截止时间为2026年9月30日", "page_idx": 1},
    ]
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("result/hybrid_auto/demo.md", markdown)
        archive.writestr(
            "result/hybrid_auto/demo_content_list.json",
            json.dumps(blocks, ensure_ascii=False),
        )
    return payload.getvalue()


def test_parse_pdf_writes_page_annotated_markdown_and_content_list(tmp_path, monkeypatch):
    source = tmp_path / "招标文件.pdf"
    source.write_bytes(b"pdf")

    class Response:
        status_code = 200
        content = _mineru_zip()
        text = ""

    def fake_post(url, *, data, files, timeout):
        assert url == "http://mineru.test/file_parse"
        assert data["return_content_list"] == "true"
        assert files["files"][0] == source.name
        assert timeout == (30, 123)
        return Response()

    monkeypatch.setattr("tender_review.mineru.requests.post", fake_post)

    result = parse_pdf_with_mineru(
        source,
        api_url="http://mineru.test/file_parse",
        timeout=123,
    )

    assert result.page_count == 2
    assert result.markdown_path == source.with_suffix(".md")
    assert result.content_list_path == tmp_path / "招标文件_content_list.json"
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "<!-- page: 1 -->" in markdown
    assert "## 第 2 页" in markdown
    assert "投标截止时间为2026年9月30日" in markdown


def test_parse_pdf_rejects_unsafe_or_incomplete_archive(tmp_path, monkeypatch):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"pdf")
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape_content_list.json", "[]")

    class Response:
        status_code = 200
        content = payload.getvalue()
        text = ""

    monkeypatch.setattr("tender_review.mineru.requests.post", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="unsafe ZIP member"):
        parse_pdf_with_mineru(source, api_url="http://mineru.test/file_parse")


def test_parse_pdf_surfaces_api_error_without_creating_sidecars(tmp_path, monkeypatch):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"pdf")

    class Response:
        status_code = 503
        content = b"busy"
        text = "GPU queue is full"

    monkeypatch.setattr("tender_review.mineru.requests.post", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="HTTP 503"):
        parse_pdf_with_mineru(source, api_url="http://mineru.test/file_parse")
    assert not source.with_suffix(".md").exists()
    assert not (tmp_path / "document_content_list.json").exists()

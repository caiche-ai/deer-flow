import json

import pytest

from tender_review.document import MineruDocument, resolve_mineru_content_path


@pytest.fixture
def content_list(tmp_path):
    path = tmp_path / "示例_content_list.json"
    path.write_text(
        json.dumps(
            [
                {"type": "text", "text": "第一章 招标公告", "text_level": 1, "page_idx": 0},
                {"type": "text", "text": "投标截止时间为2026年9月30日。", "page_idx": 0},
                {
                    "type": "text",
                    "text": "资格要求：具备设备供货业绩。",
                    "page_idx": 1,
                    "bbox": [100, 220, 900, 280],
                },
                {"type": "table", "table_body": "评分项目 技术方案 30分", "page_idx": 2},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_load_overview_search_and_exact_evidence(content_list):
    document = MineruDocument.load(content_list, document_id="tender-v1", chunk_chars=300)

    overview = document.overview()
    hits = document.search("投标截止时间", top_k=2)
    evidence = document.get_evidence([hits[0].evidence.evidence_id])

    assert overview["pages"] == 3
    assert overview["headings"][0]["text"] == "第一章 招标公告"
    assert hits[0].evidence.page == 1
    assert "2026年9月30日" in evidence[0].text
    assert evidence[0].evidence_id == "tender-v1:page=1:chunk=1"

    qualification = document.search("设备供货业绩", top_k=1)[0].evidence
    assert qualification.bboxes == ({"block": 3, "bbox": [100, 220, 900, 280]},)


def test_page_read_is_bounded(content_list):
    document = MineruDocument.load(content_list, chunk_chars=300)

    assert {chunk.page for chunk in document.read_pages(1, 2)} == {1, 2}
    with pytest.raises(ValueError, match="at most 10"):
        document.read_pages(1, 11)


def test_resolve_source_document_to_mineru_output(tmp_path):
    document = tmp_path / "data" / "招投标" / "招标文件" / "正文.pdf"
    parsed = tmp_path / "data" / "parsed"
    expected = parsed / "招标文件" / "正文" / "hybrid_auto" / "正文_content_list.json"
    expected.parent.mkdir(parents=True)
    expected.write_text("[]", encoding="utf-8")

    assert resolve_mineru_content_path(document, parsed) == expected.resolve()


def test_invalid_content_list_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON array"):
        MineruDocument.load(path)

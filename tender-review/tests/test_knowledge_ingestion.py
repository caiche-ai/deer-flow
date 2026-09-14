from __future__ import annotations

import math
from pathlib import Path

import pytest

from tender_review.knowledge_ingestion import (
    CORPUS_SPECS,
    EmbeddingClient,
    build_embedding_text,
    chunk_content_list,
    discover_parsed_documents,
)


def test_discover_parsed_documents_routes_only_supported_corpora(tmp_path: Path) -> None:
    for category in (*CORPUS_SPECS, "招标文件"):
        artifact = tmp_path / category / "示例文件" / "hybrid_auto"
        artifact.mkdir(parents=True)
        (artifact / "示例文件_content_list.json").write_text("[]", encoding="utf-8")
        (artifact / "示例文件_content_list_v2.json").write_text("[]", encoding="utf-8")

    documents = discover_parsed_documents(tmp_path)

    assert len(documents) == 3
    assert {document.corpus.category for document in documents} == set(CORPUS_SPECS)
    assert all(not document.content_list_path.name.endswith("_v2.json") for document in documents)


def test_chunker_preserves_heading_clause_page_and_table_context() -> None:
    blocks = [
        {"type": "text", "text": "第三章 投标", "text_level": 1, "page_idx": 0},
        {
            "type": "text",
            "text": "第二十六条 投标人应当具备承担招标项目的能力。",
            "page_idx": 0,
            "bbox": [1, 2, 3, 4],
        },
        {"type": "text", "text": "具体条件按照招标文件执行。", "page_idx": 1},
        {
            "type": "table",
            "table_caption": ["资格条件表"],
            "table_body": "资质等级|人员要求\n二级|注册建造师不少于12人",
            "page_idx": 1,
            "bbox": [10, 20, 30, 40],
        },
    ]

    chunks = chunk_content_list(
        blocks,
        corpus=CORPUS_SPECS["政策法规"],
        document_title="中华人民共和国招标投标法",
        max_chars=120,
        overlap_chars=20,
    )

    assert chunks
    assert chunks[0].section_path == ("第三章 投标",)
    assert chunks[0].clause_no == "第二十六条"
    assert chunks[0].page_start == 1
    assert any("注册建造师不少于12人" in chunk.content for chunk in chunks)
    table_chunk = next(chunk for chunk in chunks if "资格条件表" in chunk.content)
    assert table_chunk.page_end == 2
    assert table_chunk.bbox_json["blocks"][0]["bbox"] == [10, 20, 30, 40]


def test_embedding_text_is_contextualized_and_bounded() -> None:
    chunk = chunk_content_list(
        [{"type": "text", "text": "第一条 " + "测试正文" * 200, "page_idx": 0}],
        corpus=CORPUS_SPECS["政策法规"],
        document_title="测试法规",
        max_chars=300,
        overlap_chars=30,
    )[0]

    text = build_embedding_text(
        CORPUS_SPECS["政策法规"],
        "测试法规",
        chunk,
        max_chars=480,
    )

    assert text.startswith("知识类型：政策法规\n文档：测试法规")
    assert "条款：第一条" in text
    assert len(text) <= 480


def test_chunker_splits_markdown_clauses_inside_one_mineru_block() -> None:
    chunks = chunk_content_list(
        [
            {
                "type": "text",
                "text": "总则。 **第十四条** 招标代理机构应当具备专业力量。 "
                "**第十五条** 招标代理机构应当依法开展业务。",
                "page_idx": 2,
            }
        ],
        corpus=CORPUS_SPECS["政策法规"],
        document_title="测试法规",
        max_chars=120,
        overlap_chars=20,
    )

    by_clause = {chunk.clause_no: chunk for chunk in chunks if chunk.clause_no}
    assert {"第十四条", "第十五条"} <= set(by_clause)
    assert "专业力量" in by_clause["第十四条"].content
    assert "依法开展业务" not in by_clause["第十四条"].content
    assert by_clause["第十五条"].page_start == 3


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 3.0, 4.0]},
                {"index": 0, "embedding": [2.0, 0.0, 0.0]},
            ]
        }


class _FakeSession:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def post(self, _url: str, *, json: dict, timeout: float) -> _FakeResponse:
        assert timeout == 30
        self.payloads.append(json)
        return _FakeResponse()


def test_embedding_client_sorts_validates_and_normalizes_vectors() -> None:
    session = _FakeSession()
    client = EmbeddingClient(
        base_url="http://example.test:8097",
        model="/model",
        dimensions=3,
        batch_size=2,
        timeout=30,
        session=session,
    )

    vectors = client.embed(["第一条", "第二条"])

    assert session.payloads == [{"model": "/model", "input": ["第一条", "第二条"]}]
    assert vectors[0] == [1.0, 0.0, 0.0]
    assert vectors[1] == pytest.approx([0.0, 0.6, 0.8])
    assert all(math.isclose(sum(value * value for value in vector), 1.0) for vector in vectors)


def test_embedding_client_rejects_wrong_dimension() -> None:
    client = EmbeddingClient(
        base_url="http://example.test:8097",
        model="/model",
        dimensions=2,
        batch_size=2,
        timeout=30,
        session=_FakeSession(),
    )

    with pytest.raises(ValueError, match="dimension"):
        client.embed(["第一条", "第二条"])

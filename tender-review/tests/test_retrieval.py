import json

from tender_review.document import MineruDocument
from tender_review.retrieval import (
    DocumentHybridRetriever,
    KnowledgeEvidence,
    PostgresKnowledgeRetriever,
)


class KeywordEmbedder:
    dimensions = 3
    batch_size = 8

    def embed(self, texts):
        vectors = []
        for text in texts:
            if "履约担保" in text or "合同保障措施" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "投标截止" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class BrokenEmbedder:
    dimensions = 3
    batch_size = 8

    def embed(self, _texts):
        raise RuntimeError("embedding unavailable")


def _document(tmp_path, blocks, *, chunk_chars=300):
    path = tmp_path / "长招标文件_content_list.json"
    path.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    return MineruDocument.load(path, document_id="long-tender", chunk_chars=chunk_chars)


def test_child_chunks_are_bounded_and_point_to_parent_evidence(tmp_path):
    document = _document(
        tmp_path,
        [{"type": "text", "text": "甲" * 180 + "。" + "乙" * 180, "page_idx": 0}],
        chunk_chars=600,
    )

    retriever = DocumentHybridRetriever(
        document,
        KeywordEmbedder(),
        child_chars=200,
        overlap_chars=30,
    )

    assert len(retriever.child_chunks) >= 2
    assert all(len(chunk.text) <= 200 for chunk in retriever.child_chunks)
    assert {chunk.parent_evidence_id for chunk in retriever.child_chunks} == {
        "long-tender:page=1:chunk=1"
    }


def test_hybrid_search_can_find_semantic_match_without_keyword_overlap(tmp_path):
    document = _document(
        tmp_path,
        [
            {"type": "text", "text": "投标截止时间为九月三十日。", "page_idx": 0},
            {"type": "text", "text": "中标人应当提交履约担保。", "page_idx": 1},
        ],
    )
    retriever = DocumentHybridRetriever(document, KeywordEmbedder())

    hits = retriever.search("合同保障措施", top_k=1)

    assert hits[0].evidence.page == 2
    assert hits[0].retrieval_mode == "hybrid"
    assert "履约担保" in hits[0].matched_child_text


def test_hybrid_search_falls_back_to_lexical_when_embedding_fails(tmp_path):
    document = _document(
        tmp_path,
        [{"type": "text", "text": "投标截止时间为九月三十日。", "page_idx": 0}],
    )
    retriever = DocumentHybridRetriever(document, BrokenEmbedder())

    hits = retriever.search("投标截止", top_k=1)

    assert hits[0].evidence.page == 1
    assert hits[0].retrieval_mode == "lexical_fallback"
    assert retriever.status()["semantic_status"] == "unavailable"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


def test_public_knowledge_search_applies_visibility_and_validity_filters():
    rows = [
        (
            "chunk-1",
            "tender_regulations",
            "招投标政策法规",
            "document-1",
            "中华人民共和国招标投标法",
            26,
            "第二十六条 投标人应当具备承担招标项目的能力。",
            "第三章 投标",
            ["第三章 投标"],
            "第二十六条",
            8,
            8,
            {"blocks": []},
            0.12,
        )
    ]
    cursor = FakeCursor(rows)
    retriever = PostgresKnowledgeRetriever(
        "postgresql://unused",
        KeywordEmbedder(),
        tenant_id="tenant-a",
        connection_factory=lambda _dsn: FakeConnection(cursor),
    )

    hits = retriever.search(
        "投标人资格",
        categories=["tender_regulations"],
        region="全国",
        as_of="2026-09-09",
        top_k=3,
    )

    assert hits[0].evidence == KnowledgeEvidence(
        basis_id="kb:tender_regulations:chunk-1",
        knowledge_base="tender_regulations",
        knowledge_base_name="招投标政策法规",
        document_id="document-1",
        document_title="中华人民共和国招标投标法",
        chunk_id="chunk-1",
        chunk_no=26,
        content="第二十六条 投标人应当具备承担招标项目的能力。",
        section_title="第三章 投标",
        section_path=("第三章 投标",),
        clause_no="第二十六条",
        page_start=8,
        page_end=8,
        bbox_json={"blocks": []},
    )
    assert hits[0].score == 0.88
    assert "d.current_version_no" in cursor.query
    assert "effective_from" in cursor.query
    assert "征求意见" in cursor.query
    assert "%s::text IS NULL" in cursor.query
    assert cursor.params[1] == "tenant-a"

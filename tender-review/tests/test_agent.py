import json
from unittest.mock import MagicMock, patch

import pytest

from tender_review.agent import (
    TENDER_REVIEW_SYSTEM_PROMPT,
    build_tender_review_tools,
    make_tender_review_agent,
)
from tender_review.document import MineruDocument
from tender_review.report import AuditDimension, TenderReportStore
from tender_review.retrieval import KnowledgeEvidence, KnowledgeSearchHit, TenderRetrievalServices


def test_prompt_requires_all_dimensions_and_report_tool():
    for dimension in AuditDimension:
        assert dimension.value in TENDER_REVIEW_SYSTEM_PROMPT
    assert "save_tender_review" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "不得凭记忆编造" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "必须用 get_tender_evidence 回读证据" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "compliance-reviewer" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "evaluation-technical-reviewer" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "commercial-contract-reviewer" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "consistency-evidence-reviewer" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "search_tender_knowledge" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "get_tender_knowledge_evidence" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "ask_clarification" in TENDER_REVIEW_SYSTEM_PROMPT
    assert "每个新任务" in TENDER_REVIEW_SYSTEM_PROMPT


def _document(tmp_path):
    path = tmp_path / "agent_content_list.json"
    path.write_text(
        json.dumps(
            [{"type": "text", "text": "投标截止时间为9月30日", "page_idx": 0}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return MineruDocument.load(path, chunk_chars=300)


def test_bound_search_tool_returns_stable_evidence(tmp_path):
    pytest.importorskip("langchain_core")
    document = _document(tmp_path)
    tools = {
        tool.name: tool
        for tool in build_tender_review_tools(
            document,
            TenderReportStore(document, tmp_path / "reports"),
        )
    }

    payload = json.loads(tools["search_tender_document"].invoke({"query": "投标截止时间"}))

    assert payload[0]["evidence_id"] == "agent:page=1:chunk=1"
    assert "save_tender_review" in tools


def test_agent_is_assembled_by_deerflow_factory(tmp_path):
    pytest.importorskip("deerflow")
    document = _document(tmp_path)
    compiled_graph = MagicMock(name="compiled_graph")
    checkpointer = MagicMock(name="checkpointer")

    with patch("deerflow.agents.factory.create_agent", return_value=compiled_graph) as factory:
        graph, store = make_tender_review_agent(
            document=document,
            model=MagicMock(name="model"),
            output_dir=tmp_path / "reports",
            checkpointer=checkpointer,
        )

    assert graph is compiled_graph
    assert store.document is document
    assert factory.call_args.kwargs["checkpointer"] is checkpointer
    tool_names = {tool.name for tool in factory.call_args.kwargs["tools"]}
    assert {
        "delegate_tender_review",
        "get_tender_review_state",
        "inspect_tender_document",
        "record_tender_issue_action",
        "resume_tender_review",
        "search_tender_document",
        "start_tender_review",
        "read_tender_pages",
        "get_tender_evidence",
        "save_tender_review",
    }.issubset(tool_names)
    start_tool = next(
        tool for tool in factory.call_args.kwargs["tools"] if tool.name == "start_tender_review"
    )
    assert "force_confirmation" not in start_tool.args_schema.model_fields


class FakeDocumentRetriever:
    def search(self, query, *, top_k=8, page_start=None, page_end=None):
        document = self.document
        hit = document.search(query, top_k=top_k, page_start=page_start, page_end=page_end)[0]
        return [
            type(
                "Hit",
                (),
                {
                    "evidence": hit.evidence,
                    "score": hit.score,
                    "retrieval_mode": "hybrid",
                    "matched_child_text": hit.evidence.text,
                },
            )()
        ]

    def status(self):
        return {"semantic_status": "ready", "child_chunks": 1}


class FakeKnowledgeRetriever:
    evidence = KnowledgeEvidence(
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

    def search(self, *_args, **_kwargs):
        return [KnowledgeSearchHit(evidence=self.evidence, score=0.91)]

    def get_evidence(self, basis_ids):
        assert basis_ids == [self.evidence.basis_id]
        return [self.evidence]


def test_agent_tools_expose_hybrid_and_verified_public_knowledge(tmp_path):
    pytest.importorskip("langchain_core")
    document = _document(tmp_path)
    document_retriever = FakeDocumentRetriever()
    document_retriever.document = document
    knowledge_retriever = FakeKnowledgeRetriever()
    report_store = TenderReportStore(document, tmp_path / "reports")
    tools = {
        tool.name: tool
        for tool in build_tender_review_tools(
            document,
            report_store,
            retrieval=TenderRetrievalServices(document_retriever, knowledge_retriever),
        )
    }

    document_payload = json.loads(tools["search_tender_document"].invoke({"query": "投标截止时间"}))
    knowledge_payload = json.loads(tools["search_tender_knowledge"].invoke({"query": "投标人资格"}))
    basis_id = knowledge_payload[0]["basis_id"]
    tools["get_tender_knowledge_evidence"].invoke({"basis_ids": [basis_id]})

    assert document_payload[0]["retrieval_mode"] == "hybrid"
    assert basis_id == "kb:tender_regulations:chunk-1"
    assert basis_id in report_store.verified_basis_ids

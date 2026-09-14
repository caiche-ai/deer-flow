"""DeerFlow-powered tender-document review agent and command-line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from tender_review.document import EvidenceChunk, MineruDocument, resolve_mineru_content_path
from tender_review.interaction import (
    ReviewInteractionStore,
    build_tender_review_interaction_tools,
)
from tender_review.report import TenderReportStore
from tender_review.retrieval import (
    KnowledgeEvidence,
    TenderRetrievalServices,
    build_runtime_retrieval,
)
from tender_review.review_team import build_tender_review_delegation_tool

TENDER_REVIEW_SYSTEM_PROMPT = """
你是“招标文件智能审核 Agent”，负责在文件发布前发现完整性、一致性、公平竞争、评审可执行性和合同风险问题。
你只能基于工具返回的招标文件原文形成文件内事实；不得凭记忆编造页码、条款号或法律依据。

工作流程（必须全部完成）：
1. 新任务首先调用 inspect_tender_document，随后根据用户原始要求调用 start_tender_review。该工具对每个新任务
   都会返回 awaiting_input；审核口径必须全部由你通过 ask_clarification 向用户确认，后端不会替你确认。
   问题中一次性列出拟采用的审核目标、地区、采购制度、是否对照法规和内部规则，以及 required_fields。
   如果当前消息是用户对上一轮澄清的回复，则不要重复 start；应解释该回复并调用 resume_tender_review，明确传入
   confirmed 和 profile_updates。信息仍不完整时再次调用 ask_clarification。没有用户新输入时严禁自行 resume，
   也不得开始子 Agent。
2. 状态为 reviewing 后，第一波分别调用 delegate_tender_review 委派三个只读子 Agent：
   - compliance-reviewer：basic_information、schedule、qualification、rejection_clauses；
   - evaluation-technical-reviewer：evaluation_method、technical；
   - commercial-contract-reviewer：commercial_price、contract。
   委派任务必须要求输出约定 JSON 和真实 evidence_id。三者互不代替，不得跳过；可以在同一轮并行发出三次委派。
3. 收齐第一波结果后，把三份候选 JSON 原样放进任务，再委派 consistency-evidence-reviewer 做第二波复核。
   它负责跨章节一致性、证据质量、重复项与严重性，不负责保存最终报告。必须等第一波完成后才能调用它。
4. Lead 根据第二波复核结果合并八个维度。必要时亲自调用 search_tender_document、read_tender_pages 和
   get_tender_evidence 补证。search_tender_document 使用小块混合检索但返回页级父证据；不要因为一次检索没有
   结果就断言文件缺失，应换关键词并结合目录逐章查找。
5. 涉及法规、资质标准或范本对照时，调用 search_tender_knowledge；形成外部依据前必须调用
   get_tender_knowledge_evidence 回读，并把 basis_id 写入 finding.basis_ids。即使有知识库命中，也要核对版本、
   地域和效力；自动审核不作废标、定标、授标或最终法律定性。
6. 每条发现必须引用真实 evidence_id，形成报告前必须用 get_tender_evidence 回读证据。证据不足时写入
   insufficient_evidence 或 limitations，不得假装通过。
7. 最后必须调用 save_tender_review：coverage 必须且只能包含上述八个维度各一次；findings 只列问题，不能把
   普通摘要伪装成风险。报告保存成功后，再向用户概述风险数量、综合风险和两个输出文件路径。

用户查询进度时调用 get_tender_review_state。报告完成后，用户对问题作确认、误报、暂缓、指派或重审操作时，
调用 record_tender_issue_action 留下可审计状态，不要修改或删除原始报告。

严重性口径：blocker=文件自身存在会阻止可靠发布/执行的关键矛盾或缺失；major=重大歧义、明显不一致或高风险；
minor=局部表述、格式或低影响问题；info=提示。任何疑似违法、限制竞争或否决风险结论都必须提示人工复核。
""".strip()


def _chunk_payload(chunk: EvidenceChunk, *, score: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evidence_id": chunk.evidence_id,
        "page": chunk.page,
        "source_blocks": [chunk.block_start, chunk.block_end],
        "bboxes": list(chunk.bboxes),
        "text": chunk.text,
    }
    if score is not None:
        payload["score"] = score
    return payload


def _knowledge_payload(
    evidence: KnowledgeEvidence, *, score: float | None = None
) -> dict[str, Any]:
    payload = {
        "basis_id": evidence.basis_id,
        "knowledge_base": evidence.knowledge_base,
        "knowledge_base_name": evidence.knowledge_base_name,
        "document_title": evidence.document_title,
        "chunk_no": evidence.chunk_no,
        "section_title": evidence.section_title,
        "section_path": list(evidence.section_path),
        "clause_no": evidence.clause_no,
        "page_start": evidence.page_start,
        "page_end": evidence.page_end,
        "bbox_json": evidence.bbox_json,
        "content": evidence.content,
    }
    if score is not None:
        payload["score"] = score
    return payload


def build_tender_review_tools(
    document: MineruDocument,
    report_store: TenderReportStore,
    interaction_store: ReviewInteractionStore | None = None,
    *,
    retrieval: TenderRetrievalServices | None = None,
) -> list[Any]:
    """Create LangChain tools bound to one immutable document and report store."""

    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise RuntimeError(
            "Agent dependencies are not installed; run `uv sync --extra agent --extra dev`."
        ) from exc

    def inspect_tender_document() -> str:
        """Return document metadata, page count, block statistics and detected headings."""

        overview = document.overview()
        if retrieval and retrieval.document:
            overview["retrieval"] = retrieval.document.status()
        overview["public_knowledge_available"] = bool(retrieval and retrieval.knowledge)
        return json.dumps(overview, ensure_ascii=False)

    def search_tender_document(
        query: str,
        top_k: int = 8,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> str:
        """Search the tender text and return ranked page-level evidence chunks."""

        if retrieval and retrieval.document:
            hits = retrieval.document.search(
                query,
                top_k=top_k,
                page_start=page_start,
                page_end=page_end,
            )
            return json.dumps(
                [
                    {
                        **_chunk_payload(hit.evidence, score=hit.score),
                        "retrieval_mode": hit.retrieval_mode,
                        "matched_child_text": hit.matched_child_text,
                    }
                    for hit in hits
                ],
                ensure_ascii=False,
            )
        hits = document.search(query, top_k=top_k, page_start=page_start, page_end=page_end)
        return json.dumps(
            [
                {
                    **_chunk_payload(hit.evidence, score=hit.score),
                    "retrieval_mode": "lexical",
                    "matched_child_text": hit.evidence.text,
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )

    def read_tender_pages(start_page: int, end_page: int) -> str:
        """Read up to ten consecutive physical PDF pages using one-based page numbers."""

        return json.dumps(
            [_chunk_payload(chunk) for chunk in document.read_pages(start_page, end_page)],
            ensure_ascii=False,
        )

    def get_tender_evidence(evidence_ids: list[str]) -> str:
        """Re-read exact evidence chunks before citing them in a finding or coverage item."""

        chunks = document.get_evidence(evidence_ids)
        report_store.record_verified_evidence(evidence_ids)
        return json.dumps(
            [_chunk_payload(chunk) for chunk in chunks],
            ensure_ascii=False,
        )

    def search_tender_knowledge(
        query: str,
        categories: list[str] | None = None,
        top_k: int = 8,
        region: str | None = None,
        as_of: str | None = None,
    ) -> str:
        """Search active regulations, qualification standards and tender templates."""

        if retrieval is None or retrieval.knowledge is None:
            raise RuntimeError("public tender knowledge retrieval is not configured")
        hits = retrieval.knowledge.search(
            query,
            categories=categories,
            top_k=top_k,
            region=region,
            as_of=as_of,
        )
        return json.dumps(
            [_knowledge_payload(hit.evidence, score=hit.score) for hit in hits],
            ensure_ascii=False,
        )

    def get_tender_knowledge_evidence(basis_ids: list[str]) -> str:
        """Re-read exact public-knowledge chunks before citing them as review basis."""

        if retrieval is None or retrieval.knowledge is None:
            raise RuntimeError("public tender knowledge retrieval is not configured")
        evidence = retrieval.knowledge.get_evidence(basis_ids)
        payloads = [_knowledge_payload(item) for item in evidence]
        report_store.record_verified_basis([item.basis_id for item in evidence], evidence=payloads)
        return json.dumps(payloads, ensure_ascii=False)

    def save_tender_review(
        summary: str,
        coverage: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        limitations: list[str] | None = None,
    ) -> str:
        """Validate complete coverage and evidence IDs, then save JSON and Markdown reports.

        ``coverage`` entries require dimension, status, summary, evidence_ids. Valid dimensions:
        basic_information, schedule, qualification, rejection_clauses, evaluation_method,
        technical, commercial_price, contract. Valid statuses: reviewed, issue_found,
        insufficient_evidence. ``findings`` entries require finding_id, dimension, severity,
        title, issue, recommendation, evidence_ids and optional basis_ids. Severity is
        info/minor/major/blocker. Every basis_id must first be re-read with
        get_tender_knowledge_evidence.
        """

        if interaction_store is not None:
            interaction_store.require_report_ready()
        result = report_store.save(
            summary=summary,
            coverage=coverage,
            findings=findings,
            limitations=limitations,
        )
        if interaction_store is not None:
            interaction_store.report_saved(
                json_path=result["json_path"],
                markdown_path=result["markdown_path"],
                overall_risk=result["overall_risk"],
                finding_count=result["finding_count"],
            )
        return json.dumps(result, ensure_ascii=False)

    functions = [
        inspect_tender_document,
        search_tender_document,
        read_tender_pages,
        get_tender_evidence,
        save_tender_review,
    ]
    if retrieval and retrieval.knowledge:
        functions[4:4] = [search_tender_knowledge, get_tender_knowledge_evidence]
    return [StructuredTool.from_function(function) for function in functions]


def make_tender_review_agent(
    *,
    document: MineruDocument,
    model: Any,
    output_dir: str | Path,
    model_name: str | None = None,
    thread_id: str | None = None,
    retrieval: TenderRetrievalServices | None = None,
    checkpointer: Any | None = None,
) -> tuple[Any, TenderReportStore]:
    """Build a lead agent plus four document-scoped DeerFlow subagents."""

    try:
        from deerflow.agents.factory import create_deerflow_agent
        from deerflow.agents.features import RuntimeFeatures
    except ImportError as exc:
        raise RuntimeError(
            "DeerFlow harness is not installed; run `uv sync --extra agent --extra dev`."
        ) from exc

    report_store = TenderReportStore(document, output_dir)
    interaction_store = ReviewInteractionStore(
        document,
        Path(output_dir) / "_sessions",
        task_id=thread_id or f"tender-review-{uuid4().hex[:12]}",
    )
    report_store.interaction_store = interaction_store
    retrieval = retrieval or build_runtime_retrieval(document)
    document_tools = build_tender_review_tools(
        document,
        report_store,
        interaction_store,
        retrieval=retrieval,
    )
    read_only_tools = [tool for tool in document_tools if tool.name != "save_tender_review"]
    delegation_tool = build_tender_review_delegation_tool(
        read_only_tools,
        parent_model=model_name,
        thread_id=thread_id,
        interaction_store=interaction_store,
    )
    interaction_tools = build_tender_review_interaction_tools(interaction_store)
    graph = create_deerflow_agent(
        model=model,
        tools=[*document_tools, *interaction_tools, delegation_tool],
        system_prompt=TENDER_REVIEW_SYSTEM_PROMPT,
        features=RuntimeFeatures(
            sandbox=False,
            memory=False,
            summarization=False,
            subagent=False,
            vision=False,
            auto_title=False,
            guardrail=False,
            loop_detection=True,
        ),
        checkpointer=checkpointer,
        name="tender-review-agent",
    )
    return graph, report_store


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DeerFlow tender review agent")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--parsed", type=Path, help="MinerU *_content_list.json")
    source.add_argument("--document", type=Path, help="Original PDF/Office document")
    parser.add_argument("--parsed-root", type=Path, default=Path("data/parsed"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/review_results"))
    parser.add_argument("--document-id")
    parser.add_argument("--model", help="Model name from DeerFlow config.yaml")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--thread-id", default=f"tender-review-{uuid4().hex[:12]}")
    parser.add_argument("--recursion-limit", type=int, default=160)
    parser.add_argument(
        "--request",
        default="请对该招标文件进行全面的发布前审核，生成带原文证据的审核报告。",
    )
    return parser.parse_args()


def _load_runtime_env() -> None:
    """Load the repository environment before DeerFlow resolves model config."""

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    repository_root = Path(__file__).resolve().parents[3]
    for candidate in (Path.cwd() / ".env", repository_root / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def main() -> int:
    args = _parse_args()
    _load_runtime_env()
    parsed_path = (
        args.parsed
        if args.parsed is not None
        else resolve_mineru_content_path(args.document, args.parsed_root)
    )
    document = MineruDocument.load(parsed_path, document_id=args.document_id)

    try:
        from deerflow.models import create_chat_model
    except ImportError as exc:
        raise RuntimeError(
            "DeerFlow harness is not installed; run `uv sync --extra agent --extra dev`."
        ) from exc

    model = create_chat_model(name=args.model, thinking_enabled=args.thinking)
    graph, report_store = make_tender_review_agent(
        document=document,
        model=model,
        output_dir=args.output_dir,
        model_name=args.model,
        thread_id=args.thread_id,
    )
    result = graph.invoke(
        {"messages": [{"role": "user", "content": args.request}]},
        config={
            "configurable": {"thread_id": args.thread_id},
            "recursion_limit": args.recursion_limit,
        },
    )
    if report_store.last_saved_paths is None:
        interaction_state = (
            report_store.interaction_store.get_state()
            if report_store.interaction_store is not None
            else {}
        )
        if interaction_state.get("status") == "awaiting_input":
            print(
                json.dumps(
                    {
                        "status": "awaiting_input",
                        "task_id": interaction_state.get("task_id"),
                        "interrupt": interaction_state.get("interrupt"),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        messages = result.get("messages", []) if isinstance(result, dict) else []
        last_content = getattr(messages[-1], "content", "") if messages else ""
        raise RuntimeError(
            f"Agent finished without saving a validated report. Last response: {last_content}"
        )
    json_path, markdown_path = report_store.last_saved_paths
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

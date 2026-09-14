"""Document-scoped DeerFlow subagents for tender review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

READ_ONLY_REVIEW_TOOLS = (
    "inspect_tender_document",
    "search_tender_document",
    "read_tender_pages",
    "get_tender_evidence",
    "search_tender_knowledge",
    "get_tender_knowledge_evidence",
)
FIRST_WAVE_REVIEWERS = (
    "compliance-reviewer",
    "evaluation-technical-reviewer",
    "commercial-contract-reviewer",
)
SECOND_WAVE_REVIEWER = "consistency-evidence-reviewer"

_COMMON_REVIEW_RULES = """
你是招标文件发布前审核团队的只读子 Agent。只审核分配给你的范围，不替代 Lead 汇总报告。

硬性规则：
- 先 inspect，再用不同关键词 search；需要上下文时 read pages，引用前必须 get evidence 回读。
- 只能使用工具返回的文件事实和 evidence_id，不得编造页码、条款号、法律依据或缺失结论。
- 一次搜索无结果不等于文件缺失；至少更换关键词并结合目录复查。
- search_tender_document 返回检索子块命中的父证据；需要完整上下文时必须继续 read pages。
- 涉及法规、资质或范本对照时调用 search_tender_knowledge，引用前必须 get tender knowledge evidence 回读，并在 finding 的 basis_ids 中记录依据。
- 即使取得知识库依据也不作废标、定标、授标或最终法律结论；版本、地域或效力不明确时标记人工复核。
- 不调用保存工具。最终只输出一个 JSON 对象，不加 Markdown 围栏。

第一波审核输出：
{"reviewer":"角色名","coverage":[{"dimension":"维度","status":"reviewed|issue_found|insufficient_evidence","summary":"说明","evidence_ids":["..."]}],"findings":[{"finding_id":"角色前缀-001","dimension":"维度","severity":"info|minor|major|blocker","title":"标题","issue":"问题","recommendation":"建议","evidence_ids":["..."],"basis_ids":["kb:..."]}],"limitations":["..."]}

只把真正的问题放入 findings；正常条文只用于 coverage。blocker 仅用于文件内部关键矛盾或缺失足以阻止可靠发布/执行的情形。
""".strip()


@dataclass(frozen=True, slots=True)
class ReviewSubagentSpec:
    """Configuration kept independent from optional DeerFlow dependencies."""

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] = READ_ONLY_REVIEW_TOOLS
    disallowed_tools: tuple[str, ...] = (
        "task",
        "save_tender_review",
        "delegate_tender_review",
        "present_files",
        "ask_clarification",
    )
    skills: tuple[str, ...] = ("tender-document-review",)
    model: str = "inherit"
    max_turns: int = 36
    timeout_seconds: int = 900

    def to_deerflow_config(self) -> Any:
        """Convert to the framework config only when agent dependencies are installed."""

        from deerflow.subagents.config import SubagentConfig

        return SubagentConfig(
            name=self.name,
            description=self.description,
            system_prompt=self.system_prompt,
            tools=list(self.tools),
            disallowed_tools=list(self.disallowed_tools),
            skills=list(self.skills),
            model=self.model,
            max_turns=self.max_turns,
            timeout_seconds=self.timeout_seconds,
        )


REVIEW_SUBAGENT_CONFIGS: dict[str, ReviewSubagentSpec] = {
    "compliance-reviewer": ReviewSubagentSpec(
        name="compliance-reviewer",
        description="审核基本信息、时限、资格条件、公平竞争风险和否决条款。",
        system_prompt=_COMMON_REVIEW_RULES
        + """

你的唯一范围是 basic_information、schedule、qualification、rejection_clauses。
重点核对项目名称/编号/标段、公告与须知的时间链、资格条件可验证性、资格与业绩门槛、否决条款是否清晰且跨章节一致。
对品牌、地域、所有制、奖项或不合理业绩门槛，只能报告“疑似限制竞争，需结合适用法规人工复核”。
""",
    ),
    "evaluation-technical-reviewer": ReviewSubagentSpec(
        name="evaluation-technical-reviewer",
        description="审核评标办法、评分可执行性、技术参数、验收和交付要求。",
        system_prompt=_COMMON_REVIEW_RULES
        + """

你的唯一范围是 evaluation_method、technical。
重点核对评审流程、评分项/分值/计算口径、主客观分设置、否决与评分边界、技术参数、性能指标、检测验收、交付和偏离规则。
评分合计或技术参数矛盾必须分别引用冲突两侧证据；无法取得两侧证据时降级为 insufficient_evidence。
""",
    ),
    "commercial-contract-reviewer": ReviewSubagentSpec(
        name="commercial-contract-reviewer",
        description="审核报价口径、最高限价、付款、质保、违约和合同风险。",
        system_prompt=_COMMON_REVIEW_RULES
        + """

你的唯一范围是 commercial_price、contract。
重点核对报价构成、税率/币种/计价单位、最高限价、暂列金额、价格调整，及合同工期、付款节点、履约担保、质保、违约、验收和风险分配。
金额、比例、期限或付款条件矛盾必须引用冲突两侧证据；仅有单侧信息时不得断言矛盾。
""",
    ),
    "consistency-evidence-reviewer": ReviewSubagentSpec(
        name="consistency-evidence-reviewer",
        description="在三类专业审核完成后复核跨章节一致性、证据质量、重复项和严重性。",
        system_prompt=_COMMON_REVIEW_RULES
        + """

你是第二波复核者。任务中会包含三个第一波子 Agent 的候选 JSON。
重新检索原文，检查项目名称/编号、日期时限、金额比例、资格门槛、评分分值、技术参数、报价口径、工期和付款条件的跨章节一致性；核验每个候选发现是否有足够证据，合并重复项并校正严重性。
不得仅凭第一波文本确认问题，必须自行回读其 evidence_id；若候选证据错误或不足，明确剔除或降级。
最终只输出：
{"reviewer":"consistency-evidence-reviewer","validated_findings":[...],"rejected_findings":[{"finding_id":"...","reason":"..."}],"cross_checks":[{"topic":"...","status":"consistent|issue_found|insufficient_evidence","summary":"...","evidence_ids":["..."]}],"limitations":["..."]}
""",
    ),
}


def build_tender_review_delegation_tool(
    document_tools: list[Any],
    *,
    parent_model: str | None = None,
    thread_id: str | None = None,
    interaction_store: Any | None = None,
) -> Any:
    """Create a tool that executes a bounded reviewer with shared document tools."""

    try:
        # Import the agent package first; DeerFlow's built-in task tool imports the
        # subagent package during factory initialization.
        import deerflow.agents.factory  # noqa: F401
        from deerflow.subagents import SubagentExecutor
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise RuntimeError(
            "Agent dependencies are not installed; run `uv sync --extra agent --extra dev`."
        ) from exc

    def delegate_tender_review(subagent_type: str, task: str) -> str:
        """Run one tender-review specialist over the current immutable document.

        Valid subagent_type values are compliance-reviewer, evaluation-technical-reviewer,
        commercial-contract-reviewer, and consistency-evidence-reviewer. Run the first three
        before the consistency reviewer. Include prior candidate JSON in the second-wave task.
        """

        spec = REVIEW_SUBAGENT_CONFIGS.get(subagent_type)
        if spec is None:
            available = ", ".join(REVIEW_SUBAGENT_CONFIGS)
            raise ValueError(
                f"unknown tender review subagent: {subagent_type}; available: {available}"
            )
        if not task.strip():
            raise ValueError("task must not be empty")

        if interaction_store is not None:
            interaction_store.require_reviewing()
            interaction_store.stage_started(subagent_type)
            _emit_review_event("review_stage_started", subagent_type)

        try:
            executor = SubagentExecutor(
                config=spec.to_deerflow_config(),
                tools=document_tools,
                parent_model=parent_model,
                thread_id=thread_id,
            )
            result = executor.execute(task.strip())
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            if interaction_store is not None:
                interaction_store.stage_failed(subagent_type, detail)
                _emit_review_event("review_stage_failed", subagent_type, error=detail)
            raise RuntimeError(f"{subagent_type} failed: {detail}") from exc
        status = getattr(getattr(result, "status", None), "value", "unknown")
        if status != "completed" or not result.result:
            detail = result.error or f"subagent ended with status={status}"
            if interaction_store is not None:
                interaction_store.stage_failed(subagent_type, detail)
                _emit_review_event("review_stage_failed", subagent_type, error=detail)
            raise RuntimeError(f"{subagent_type} failed: {detail}")
        if interaction_store is not None:
            interaction_store.stage_completed(subagent_type)
            _emit_review_event("review_stage_completed", subagent_type)
        return result.result

    return StructuredTool.from_function(delegate_tender_review)


def _emit_review_event(event_type: str, stage: str, **payload: Any) -> None:
    """Best-effort DeerFlow custom event for streaming progress UIs."""

    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"type": event_type, "stage": stage, **payload})
    except Exception:  # noqa: BLE001
        # Direct StructuredTool.invoke calls have no active stream writer.
        return

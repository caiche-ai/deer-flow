from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tender_review.review_team import (
    FIRST_WAVE_REVIEWERS,
    REVIEW_SUBAGENT_CONFIGS,
    SECOND_WAVE_REVIEWER,
    build_tender_review_delegation_tool,
)

READ_ONLY_TOOLS = {
    "inspect_tender_document",
    "search_tender_document",
    "read_tender_pages",
    "get_tender_evidence",
    "search_tender_knowledge",
    "get_tender_knowledge_evidence",
}


def test_review_team_has_three_first_wave_roles_and_one_second_wave_role():
    assert FIRST_WAVE_REVIEWERS == (
        "compliance-reviewer",
        "evaluation-technical-reviewer",
        "commercial-contract-reviewer",
    )
    assert SECOND_WAVE_REVIEWER == "consistency-evidence-reviewer"
    assert set(REVIEW_SUBAGENT_CONFIGS) == {*FIRST_WAVE_REVIEWERS, SECOND_WAVE_REVIEWER}


@pytest.mark.parametrize("name", [*FIRST_WAVE_REVIEWERS, SECOND_WAVE_REVIEWER])
def test_review_subagents_are_read_only_and_load_the_review_skill(name):
    config = REVIEW_SUBAGENT_CONFIGS[name]

    assert set(config.tools or []) == READ_ONLY_TOOLS
    assert "save_tender_review" in (config.disallowed_tools or [])
    assert "task" in (config.disallowed_tools or [])
    assert config.skills == ("tender-document-review",)


def test_delegation_tool_rejects_unknown_reviewer():
    pytest.importorskip("deerflow")
    tool = build_tender_review_delegation_tool([], parent_model="test-model")

    with pytest.raises(ValueError, match="unknown tender review subagent"):
        tool.invoke({"subagent_type": "invented-reviewer", "task": "审核"})


def test_delegation_tool_uses_deerflow_executor_and_returns_result():
    pytest.importorskip("deerflow")
    document_tools = [MagicMock(name="document_tool")]
    document_tools[0].name = "inspect_tender_document"
    executor = MagicMock()
    executor.execute.return_value = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        result='{"reviewer":"compliance-reviewer"}',
        error=None,
    )

    with patch("deerflow.subagents.SubagentExecutor", return_value=executor) as executor_type:
        tool = build_tender_review_delegation_tool(
            document_tools,
            parent_model="test-model",
            thread_id="tender-test",
        )
        result = tool.invoke(
            {
                "subagent_type": "compliance-reviewer",
                "task": "审核资格条件并输出 JSON。",
            }
        )

    assert result == '{"reviewer":"compliance-reviewer"}'
    config = executor_type.call_args.kwargs["config"]
    assert config.name == "compliance-reviewer"
    assert executor_type.call_args.kwargs["tools"] is document_tools
    assert executor_type.call_args.kwargs["parent_model"] == "test-model"
    assert executor_type.call_args.kwargs["thread_id"] == "tender-test"
    executor.execute.assert_called_once()


def test_delegation_tool_records_progress_when_interaction_store_is_bound():
    pytest.importorskip("deerflow")
    executor = MagicMock()
    executor.execute.return_value = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        result='{"reviewer":"compliance-reviewer"}',
        error=None,
    )
    interaction_store = MagicMock()

    with patch("deerflow.subagents.SubagentExecutor", return_value=executor):
        tool = build_tender_review_delegation_tool(
            [],
            parent_model="test-model",
            interaction_store=interaction_store,
        )
        tool.invoke(
            {
                "subagent_type": "compliance-reviewer",
                "task": "审核资格条件。",
            }
        )

    interaction_store.require_reviewing.assert_called_once_with()
    interaction_store.stage_started.assert_called_once_with("compliance-reviewer")
    interaction_store.stage_completed.assert_called_once_with("compliance-reviewer")
    interaction_store.stage_failed.assert_not_called()


def test_delegation_tool_surfaces_subagent_failure():
    pytest.importorskip("deerflow")
    executor = MagicMock()
    executor.execute.return_value = SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        result=None,
        error="model unavailable",
    )

    with patch("deerflow.subagents.SubagentExecutor", return_value=executor):
        tool = build_tender_review_delegation_tool([], parent_model="test-model")
        with pytest.raises(RuntimeError, match="model unavailable"):
            tool.invoke(
                {
                    "subagent_type": "compliance-reviewer",
                    "task": "审核资格条件。",
                }
            )

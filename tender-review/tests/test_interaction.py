import json

import pytest

from tender_review.document import MineruDocument
from tender_review.interaction import (
    REVIEW_STAGES,
    ReviewInteractionStore,
    build_tender_review_interaction_tools,
)


def _document(tmp_path):
    path = tmp_path / "large_content_list.json"
    path.write_text(
        json.dumps(
            [
                {"type": "text", "text": "第一章 招标公告", "page_idx": 0, "text_level": 1},
                {"type": "text", "text": "第300页 合同条款", "page_idx": 299},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return MineruDocument.load(path, chunk_chars=300)


def test_every_review_waits_for_lead_agent_clarification(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-large",
    )

    state = store.start(
        review_goal="招标文件发布前审核",
        legal_review=False,
    )

    assert state["status"] == "awaiting_input"
    assert state["preflight"]["pages"] == 300
    assert state["preflight"]["stages_total"] == 4
    assert state["interrupt"]["gate_type"] == "review_profile"
    assert state["progress"]["percent"] == 0
    assert "文件内部" in state["profile"]["scope_statement"]
    assert state["source_content_list"].endswith("large_content_list.json")


def test_agent_queue_and_clarification_question_are_persisted(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-agent-clarification",
    )

    queued = store.queue_agent()
    assert queued["status"] == "queued"
    assert queued["events"][-1]["type"] == "agent_started"

    waiting = store.start(
        review_goal="发布前法律合规审核",
        legal_review=True,
    )
    assert waiting["status"] == "awaiting_input"

    clarified = store.record_agent_clarification("请确认项目地区和采购制度。")
    assert clarified["interrupt"]["source"] == "lead_agent"
    assert clarified["interrupt"]["question"] == "请确认项目地区和采购制度。"
    assert clarified["events"][-1]["type"] == "agent_clarification_requested"


def test_review_failure_is_persisted_as_terminal_event(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-failed",
    )
    store.start(review_goal="发布前审核")

    failed = store.fail_review("model unavailable")

    assert failed["status"] == "failed"
    assert failed["error"] == "model unavailable"
    assert failed["events"][-1]["type"] == "review_failed"


def test_legal_review_pauses_for_region_and_procurement_regime(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-legal",
    )

    waiting = store.start(review_goal="发布前合规审核", legal_review=True)

    assert waiting["status"] == "awaiting_input"
    assert waiting["interrupt"]["gate_type"] == "review_profile"
    assert waiting["interrupt"]["required_fields"] == ["region", "procurement_regime"]

    resumed = store.resume(
        confirmed=True,
        profile_updates={
            "region": "深圳",
            "procurement_regime": "工程建设项目货物招标",
        },
    )

    assert resumed["status"] == "reviewing"
    assert resumed["profile"]["region"] == "深圳"
    assert resumed["interrupt"] is None


def test_explicit_profile_confirmation_gate_can_be_cancelled(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-confirm",
    )
    waiting = store.start(
        review_goal="发布前审核",
        region="深圳",
        procurement_regime="工程建设项目货物招标",
    )
    assert waiting["status"] == "awaiting_input"

    cancelled = store.resume(confirmed=False)

    assert cancelled["status"] == "cancelled"


def test_stage_progress_and_issue_actions_are_persisted(tmp_path):
    sessions = tmp_path / "sessions"
    document = _document(tmp_path)
    store = ReviewInteractionStore(document, sessions, task_id="tender-progress")
    store.start(review_goal="发布前审核")
    store.resume(confirmed=True)

    for stage in REVIEW_STAGES:
        running = store.stage_started(stage)
        assert running["status"] == "reviewing"
        assert running["stages"][stage]["status"] == "running"
        completed = store.stage_completed(stage)
        assert completed["stages"][stage]["status"] == "completed"

    done = store.report_saved(
        json_path="reports/review.json",
        markdown_path="reports/review.md",
        overall_risk="high",
        finding_count=2,
    )
    assert done["status"] == "done"
    assert done["progress"]["percent"] == 100

    action = store.record_issue_action(
        finding_id="F-001",
        action="confirmed",
        note="编制人员确认修改",
    )
    assert action["issue_actions"]["F-001"]["action"] == "confirmed"

    restored = ReviewInteractionStore(document, sessions, task_id="tender-progress")
    assert restored.get_state()["status"] == "done"
    assert restored.get_state()["report"]["finding_count"] == 2


def test_stage_name_and_issue_action_are_validated(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-invalid",
    )
    store.start(review_goal="发布前审核")
    store.resume(confirmed=True)

    with pytest.raises(ValueError, match="unknown review stage"):
        store.stage_started("invented-reviewer")
    with pytest.raises(ValueError, match="unsupported issue action"):
        store.record_issue_action("F-001", "delete")


def test_second_wave_and_report_wait_for_required_stages(tmp_path):
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-order",
    )
    store.start(review_goal="发布前审核")
    store.resume(confirmed=True)

    with pytest.raises(RuntimeError, match="requires all first-wave stages"):
        store.stage_started("consistency-evidence-reviewer")
    with pytest.raises(RuntimeError, match="before all review stages"):
        store.require_report_ready()


def test_persisted_task_cannot_be_reused_for_another_document(tmp_path):
    sessions = tmp_path / "sessions"
    ReviewInteractionStore(
        _document(tmp_path),
        sessions,
        task_id="tender-isolation",
    ).start(review_goal="发布前审核")
    other_path = tmp_path / "other_content_list.json"
    other_path.write_text(
        json.dumps([{"type": "text", "text": "另一份文件", "page_idx": 0}], ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="belongs to another document"):
        ReviewInteractionStore(
            MineruDocument.load(other_path, chunk_chars=300),
            sessions,
            task_id="tender-isolation",
        )


def test_interaction_tools_expose_deerflow_workflow_contract(tmp_path):
    pytest.importorskip("langchain_core")
    store = ReviewInteractionStore(
        _document(tmp_path),
        tmp_path / "sessions",
        task_id="tender-tools",
    )
    tools = {tool.name: tool for tool in build_tender_review_interaction_tools(store)}

    started = json.loads(
        tools["start_tender_review"].invoke(
            {
                "review_goal": "法律合规审核",
                "legal_review": True,
            }
        )
    )
    state = json.loads(tools["get_tender_review_state"].invoke({}))

    assert set(tools) == {
        "start_tender_review",
        "resume_tender_review",
        "get_tender_review_state",
        "record_tender_issue_action",
    }
    assert started["status"] == "awaiting_input"
    assert state["task_id"] == "tender-tools"

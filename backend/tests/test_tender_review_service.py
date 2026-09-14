from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError
from tender_review.interaction import ReviewInteractionStore

from app.gateway.routers.tender_review import CreateReviewRequest, ResumeReviewRequest
from app.tender_review.service import TenderReviewTaskManager, format_sse_event


def _write_document(root: Path, name: str = "sample") -> str:
    target = root / "data" / "parsed" / name / "hybrid_auto" / f"{name}_content_list.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            [
                {"type": "text", "page_idx": 0, "text": "第一章 招标公告", "text_level": 1},
                {"type": "text", "page_idx": 1, "text": "投标截止时间为2026年10月1日"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target.relative_to(root / "data" / "parsed").as_posix()


def test_format_sse_event_has_replayable_id_and_typed_envelope() -> None:
    frame = format_sse_event(
        task_id="task-1",
        sequence=3,
        event_type="review.stage.completed",
        payload={"stage": "compliance-reviewer"},
    )

    assert frame.startswith("id: task-1:3\nevent: tender-review\ndata: ")
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["event_id"] == "task-1:3"
    assert data["sequence"] == 3
    assert data["type"] == "review.stage.completed"
    assert frame.endswith("\n\n")


def test_task_api_accepts_only_agent_messages_for_clarification() -> None:
    created = CreateReviewRequest(document_path="sample/content_list.json", request="请审核")
    resumed = ResumeReviewRequest(message="确认，地区为四川。")

    assert created.request == "请审核"
    assert resumed.message == "确认，地区为四川。"
    with pytest.raises(ValidationError):
        CreateReviewRequest(
            document_path="sample/content_list.json",
            profile={"region": "四川"},
            force_confirmation=True,
        )
    with pytest.raises(ValidationError):
        ResumeReviewRequest(confirmed=True, profile_updates={"region": "四川"})


def test_document_discovery_and_path_traversal_protection(tmp_path: Path) -> None:
    relative_path = _write_document(tmp_path)
    manager = TenderReviewTaskManager(project_root=tmp_path)

    documents = manager.list_documents()

    assert [item["path"] for item in documents] == [relative_path]
    assert documents[0]["pages"] == 2
    with pytest.raises(ValueError, match="parsed document root"):
        manager.resolve_document("../secret_content_list.json")
    with pytest.raises(ValueError, match="content_list"):
        manager.resolve_document("sample/hybrid_auto/readme.md")


def test_create_launches_lead_agent_and_agent_handles_clarification(tmp_path: Path) -> None:
    relative_path = _write_document(tmp_path)
    requests: list[str] = []

    async def runner(context) -> None:
        requests.append(context.request)
        store = ReviewInteractionStore(
            context.document,
            context.state_dir,
            task_id=context.task_id,
        )
        if len(requests) == 1:
            store.start(
                review_goal="发布前审核",
                legal_review=True,
            )
            store.record_agent_clarification("请确认地区和采购制度。")
            return
        store.resume(
            confirmed=True,
            profile_updates={
                "region": "四川省",
                "procurement_regime": "工程建设项目招标",
            },
        )

    manager = TenderReviewTaskManager(project_root=tmp_path, runner=runner)
    state = manager.create_task(
        user_id="user-1",
        document_path=relative_path,
        request="请做发布前法律合规审核",
    )

    assert state["status"] == "queued"
    assert state["source_content_list"] == relative_path
    assert manager.active_task_count == 1

    asyncio.run(manager.wait_for_task(state["task_id"]))
    waiting = manager.get_task(user_id="user-1", task_id=state["task_id"])
    assert waiting["status"] == "awaiting_input"
    assert waiting["interrupt"]["source"] == "lead_agent"
    assert waiting["interrupt"]["question"] == "请确认地区和采购制度。"

    resumed = manager.resume_task(
        user_id="user-1",
        task_id=state["task_id"],
        message="确认，地区是四川省，采用工程建设项目招标制度。",
    )
    assert resumed["status"] == "awaiting_input"

    asyncio.run(manager.wait_for_task(state["task_id"]))
    reviewed = manager.get_task(user_id="user-1", task_id=state["task_id"])
    assert reviewed["status"] == "reviewing"
    assert reviewed["profile"]["region"] == "四川省"
    assert len(requests) == 2
    assert "用户对审核口径澄清的回复" in requests[1]
    assert "地区是四川省" in requests[1]


def test_sse_replays_only_events_after_requested_sequence(tmp_path: Path) -> None:
    relative_path = _write_document(tmp_path)
    manager = TenderReviewTaskManager(project_root=tmp_path)
    state = manager.create_task(
        user_id="u",
        document_path=relative_path,
        request="请审核",
    )

    frames = manager.replay_events(user_id="u", task_id=state["task_id"], after_sequence=0)
    assert len(frames) == 1
    envelope = json.loads(frames[0].split("data: ", 1)[1])
    assert envelope["sequence"] == 1
    assert envelope["type"] == "review.agent.started"
    assert envelope["payload"]["state"]["status"] == "queued"
    assert manager.replay_events(user_id="u", task_id=state["task_id"], after_sequence=1) == []


def test_agent_clarification_is_read_from_tool_message() -> None:
    result = {
        "messages": [
            ToolMessage(
                content="请确认审核地区。",
                name="ask_clarification",
                tool_call_id="clarify-1",
            )
        ]
    }

    assert TenderReviewTaskManager._find_agent_clarification(result) == "请确认审核地区。"


def test_historical_clarification_before_latest_user_reply_is_ignored() -> None:
    result = {
        "messages": [
            ToolMessage(
                content="请确认审核地区。",
                name="ask_clarification",
                tool_call_id="clarify-1",
            ),
            HumanMessage(content="确认，地区为四川省。"),
            ToolMessage(content="审核口径已确认", tool_call_id="resume-1"),
        ]
    }

    assert TenderReviewTaskManager._find_agent_clarification(result) is None


def test_fast_user_reply_is_queued_behind_active_clarification_turn(tmp_path: Path) -> None:
    relative_path = _write_document(tmp_path)

    async def scenario() -> tuple[list[str], dict]:
        requests: list[str] = []
        clarification_ready = asyncio.Event()
        release_first_turn = asyncio.Event()

        async def runner(context) -> None:
            requests.append(context.request)
            store = ReviewInteractionStore(
                context.document,
                context.state_dir,
                task_id=context.task_id,
            )
            if len(requests) == 1:
                store.start(review_goal="发布前审核")
                store.record_agent_clarification("请确认审核口径。")
                clarification_ready.set()
                await release_first_turn.wait()
                return
            store.resume(confirmed=True)

        manager = TenderReviewTaskManager(project_root=tmp_path, runner=runner)
        created = manager.create_task(
            user_id="fast-user",
            document_path=relative_path,
            request="请审核",
        )
        await clarification_ready.wait()
        manager.resume_task(
            user_id="fast-user",
            task_id=created["task_id"],
            message="确认。",
        )
        release_first_turn.set()
        await manager.wait_for_task(created["task_id"])
        return requests, manager.get_task(user_id="fast-user", task_id=created["task_id"])

    requests, state = asyncio.run(scenario())

    assert len(requests) == 2
    assert state["status"] == "reviewing"

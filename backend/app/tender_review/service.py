"""Persistent tender-review tasks and replayable SSE event delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tender_review.document import MineruDocument
from tender_review.interaction import ReviewInteractionStore

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
EVENT_TYPES = {
    "agent_started": "review.agent.started",
    "agent_clarification_requested": "review.awaiting_input",
    "awaiting_profile_confirmation": "review.profile.prepared",
    "review_started": "review.started",
    "profile_confirmed": "review.profile.confirmed",
    "review_cancelled": "review.cancelled",
    "stage_started": "review.stage.started",
    "stage_completed": "review.stage.completed",
    "stage_failed": "review.stage.failed",
    "report_saved": "review.report.ready",
    "review_failed": "review.failed",
    "issue_action_recorded": "review.finding.action",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _user_key(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:20]


def _clean_error(error: BaseException | str) -> str:
    text = str(error).strip()
    return text[:2000] if text else "unknown review failure"


def format_sse_event(*, task_id: str, sequence: int, event_type: str, payload: dict[str, Any]) -> str:
    """Format one stable, typed and replayable SSE frame."""

    envelope = {
        "event_id": f"{task_id}:{sequence}",
        "sequence": sequence,
        "task_id": task_id,
        "type": event_type,
        "timestamp": _now(),
        "payload": payload,
    }
    return f"id: {envelope['event_id']}\nevent: tender-review\ndata: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"


@dataclass(frozen=True, slots=True)
class ReviewTaskContext:
    task_id: str
    document: MineruDocument
    output_dir: Path
    state_dir: Path
    request: str
    model_name: str | None


ReviewRunner = Callable[[ReviewTaskContext], Awaitable[None]]


class TenderReviewTaskManager:
    """Own document discovery, review lifecycle, task isolation and SSE replay."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        runner: ReviewRunner | None = None,
    ) -> None:
        default_root = Path(__file__).resolve().parents[3] / "tender-review"
        self.project_root = Path(project_root or os.getenv("TENDER_REVIEW_PROJECT_ROOT") or default_root).expanduser().resolve()
        self.parsed_root = (self.project_root / "data" / "parsed").resolve()
        self.results_root = (self.project_root / "data" / "review_results" / "api").resolve()
        self._runner = runner or self._run_deerflow_agent
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, ReviewTaskContext] = {}

    @property
    def active_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values()) + len(self._pending)

    def list_documents(self) -> list[dict[str, Any]]:
        """List primary MinerU content lists available for review."""

        if not self.parsed_root.is_dir():
            return []
        documents: list[dict[str, Any]] = []
        candidates = sorted(self.parsed_root.rglob("*_content_list.json"))
        for path in candidates:
            if path.name.endswith("_content_list_v2.json") or "_converted_inputs" in path.parts:
                continue
            try:
                document = MineruDocument.load(path)
            except (OSError, ValueError):
                continue
            documents.append(
                {
                    "path": path.relative_to(self.parsed_root).as_posix(),
                    "name": path.stem.removesuffix("_content_list"),
                    "document_id": document.document_id,
                    "pages": document.page_count,
                    "evidence_chunks": document.chunk_count,
                }
            )
        return documents

    def resolve_document(self, relative_path: str) -> Path:
        if not relative_path or not relative_path.endswith("_content_list.json"):
            raise ValueError("document path must reference a MinerU *_content_list.json")
        candidate = (self.parsed_root / Path(relative_path)).resolve()
        try:
            candidate.relative_to(self.parsed_root)
        except ValueError:
            raise ValueError("document path must stay inside the parsed document root") from None
        if not candidate.is_file():
            raise FileNotFoundError(f"parsed document not found: {relative_path}")
        return candidate

    def create_task(
        self,
        *,
        user_id: str,
        document_path: str,
        request: str,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        source = self.resolve_document(document_path)
        document = MineruDocument.load(source)
        task_id = f"tender-{uuid4().hex[:16]}"
        output_dir = self._task_dir(user_id, task_id)
        state_dir = output_dir / "_sessions"
        store = ReviewInteractionStore(document, state_dir, task_id=task_id)
        state = store.queue_agent()
        self._launch(
            ReviewTaskContext(
                task_id=task_id,
                document=document,
                output_dir=output_dir,
                state_dir=state_dir,
                request=self._build_initial_request(request),
                model_name=model_name,
            )
        )
        return self._public_state(state)

    def get_task(self, *, user_id: str, task_id: str) -> dict[str, Any]:
        return self._public_state(self._read_state(user_id, task_id))

    def resume_task(
        self,
        *,
        user_id: str,
        task_id: str,
        message: str,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        store, document, output_dir = self._restore_store(user_id, task_id)
        state = store.get_state()
        if state["status"] != "awaiting_input":
            raise RuntimeError("review task is not waiting for Lead Agent clarification")
        if not message.strip():
            raise ValueError("clarification message must not be empty")
        self._launch(
            ReviewTaskContext(
                task_id=task_id,
                document=document,
                output_dir=output_dir,
                state_dir=output_dir / "_sessions",
                request=self._build_resume_request(state, message),
                model_name=model_name,
            )
        )
        return self._public_state(state)

    def record_issue_action(
        self,
        *,
        user_id: str,
        task_id: str,
        finding_id: str,
        action: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        store, _, _ = self._restore_store(user_id, task_id)
        return self._public_state(store.record_issue_action(finding_id, action, note))

    def report_path(self, *, user_id: str, task_id: str, report_format: str) -> Path:
        state = self._read_state(user_id, task_id)
        report = state.get("report") or {}
        key = {"json": "json_path", "markdown": "markdown_path"}.get(report_format)
        if key is None:
            raise ValueError("report format must be json or markdown")
        raw_path = report.get(key)
        if not raw_path:
            raise FileNotFoundError("review report is not ready")
        candidate = Path(raw_path).resolve()
        task_dir = self._task_dir(user_id, task_id)
        try:
            candidate.relative_to(task_dir)
        except ValueError:
            raise ValueError("report path escaped the task directory") from None
        if not candidate.is_file():
            raise FileNotFoundError("review report file is missing")
        return candidate

    def replay_events(self, *, user_id: str, task_id: str, after_sequence: int = 0) -> list[str]:
        state = self._read_state(user_id, task_id)
        public_state = self._public_state(state)
        frames = []
        for sequence, event in enumerate(state.get("events", []), start=1):
            if sequence <= max(after_sequence, 0):
                continue
            event_type = EVENT_TYPES.get(str(event.get("type")), "review.event")
            detail = {key: value for key, value in event.items() if key not in {"type", "ts"}}
            frames.append(
                format_sse_event(
                    task_id=task_id,
                    sequence=sequence,
                    event_type=event_type,
                    payload={"detail": detail, "state": public_state},
                )
            )
        return frames

    async def stream_events(self, *, user_id: str, task_id: str, after_sequence: int = 0) -> AsyncIterator[str]:
        sequence = max(after_sequence, 0)
        heartbeat_ticks = 0
        while True:
            frames = self.replay_events(user_id=user_id, task_id=task_id, after_sequence=sequence)
            for frame in frames:
                sequence += 1
                yield frame
            state = self._read_state(user_id, task_id)
            if state.get("status") in TERMINAL_STATUSES and sequence >= len(state.get("events", [])):
                return
            await asyncio.sleep(1)
            heartbeat_ticks += 1
            if heartbeat_ticks >= 15:
                heartbeat_ticks = 0
                yield ": heartbeat\n\n"

    async def wait_for_task(self, task_id: str) -> None:
        context = self._pending.pop(task_id, None)
        if context is not None:
            await self._execute(context)
            return
        task = self._tasks.get(task_id)
        if task is not None:
            await task

    def _task_dir(self, user_id: str, task_id: str) -> Path:
        if not re.fullmatch(r"tender-[a-f0-9]{16}", task_id):
            raise ValueError("invalid tender review task id")
        return (self.results_root / "users" / _user_key(user_id) / task_id).resolve()

    def _state_path(self, user_id: str, task_id: str) -> Path:
        return self._task_dir(user_id, task_id) / "_sessions" / f"{task_id}.json"

    def _read_state(self, user_id: str, task_id: str) -> dict[str, Any]:
        path = self._state_path(user_id, task_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"tender review task not found: {task_id}") from None
        if not isinstance(value, dict) or value.get("task_id") != task_id:
            raise ValueError("invalid tender review task state")
        return value

    def _restore_store(self, user_id: str, task_id: str) -> tuple[ReviewInteractionStore, MineruDocument, Path]:
        state = self._read_state(user_id, task_id)
        source = Path(str(state.get("source_content_list") or "")).resolve()
        try:
            source.relative_to(self.parsed_root)
        except ValueError:
            raise ValueError("task document escaped the parsed document root") from None
        document = MineruDocument.load(source, document_id=state.get("document_id"))
        output_dir = self._task_dir(user_id, task_id)
        return (
            ReviewInteractionStore(document, output_dir / "_sessions", task_id=task_id),
            document,
            output_dir,
        )

    def _public_state(self, state: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(state, ensure_ascii=False))
        source = Path(str(value.get("source_content_list") or "")).resolve()
        try:
            value["source_content_list"] = source.relative_to(self.parsed_root).as_posix()
        except ValueError:
            value["source_content_list"] = source.name
        report = value.get("report")
        if isinstance(report, dict):
            task_id = value["task_id"]
            value["report"] = {
                "overall_risk": report.get("overall_risk"),
                "finding_count": report.get("finding_count", 0),
                "json_url": f"/api/tender-review/tasks/{task_id}/report?format=json",
                "markdown_url": f"/api/tender-review/tasks/{task_id}/report?format=markdown",
            }
        return value

    def _launch(self, context: ReviewTaskContext) -> None:
        current = self._tasks.get(context.task_id)
        if current is not None and not current.done():
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._execute_after(current, context),
                name=f"tender-review:{context.task_id}:continuation",
            )
            self._tasks[context.task_id] = task
            return
        if context.task_id in self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending[context.task_id] = context
            return
        task = loop.create_task(self._execute(context), name=f"tender-review:{context.task_id}")
        self._tasks[context.task_id] = task

    async def _execute_after(
        self,
        previous: asyncio.Task[None],
        context: ReviewTaskContext,
    ) -> None:
        await previous
        await self._execute(context)

    async def _execute(self, context: ReviewTaskContext) -> None:
        try:
            await self._runner(context)
        except Exception as exc:
            store = ReviewInteractionStore(context.document, context.state_dir, task_id=context.task_id)
            store.fail_review(_clean_error(exc))

    @staticmethod
    def _build_initial_request(request: str) -> str:
        return (
            "请开始这份招标文件的审核。以下是用户的原始要求："
            f"{request.strip()}\n"
            "先执行文档预检并拟定审核口径，然后必须使用 ask_clarification "
            "由你亲自向用户一次性确认审核目标、地区、采购制度、是否对照法规和内部规则；"
            "不要替用户确认，也不要在本回合启动审核子 Agent。"
        )

    @staticmethod
    def _build_resume_request(state: dict[str, Any], message: str) -> str:
        context = {
            "proposed_profile": state.get("profile"),
            "required_fields": (state.get("interrupt") or {}).get("required_fields", []),
            "previous_question": (state.get("interrupt") or {}).get("question"),
        }
        return (
            "用户对审核口径澄清的回复如下：\n"
            f"{message.strip()}\n"
            f"当前待确认上下文：{json.dumps(context, ensure_ascii=False)}\n"
            "请由你解释用户回复并调用 resume_tender_review，传入 confirmed 和 profile_updates。"
            "如果信息仍不足，继续使用 ask_clarification；确认完成后再执行四阶段审核。"
        )

    @staticmethod
    async def _run_deerflow_agent(context: ReviewTaskContext) -> None:
        def invoke() -> None:
            from tender_review.agent import make_tender_review_agent

            from deerflow.models import create_chat_model
            from deerflow.runtime.checkpointer import get_checkpointer

            model = create_chat_model(name=context.model_name, thinking_enabled=False)
            graph, report_store = make_tender_review_agent(
                document=context.document,
                model=model,
                output_dir=context.output_dir,
                model_name=context.model_name,
                thread_id=context.task_id,
                checkpointer=get_checkpointer(),
            )
            result = graph.invoke(
                {"messages": [{"role": "user", "content": context.request}]},
                config={
                    "configurable": {"thread_id": context.task_id},
                    "recursion_limit": 160,
                },
            )
            state = report_store.interaction_store.get_state()
            clarification = TenderReviewTaskManager._find_agent_clarification(result)
            if clarification:
                state = report_store.interaction_store.record_agent_clarification(clarification)
            if report_store.last_saved_paths is None and state.get("status") not in {
                "awaiting_input",
                "cancelled",
                "done",
            }:
                raise RuntimeError("Agent run ended before save_tender_review produced a report")

        await asyncio.to_thread(invoke)

    @staticmethod
    def _find_agent_clarification(result: Any) -> str | None:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in reversed(messages):
            name = message.get("name") if isinstance(message, dict) else getattr(message, "name", None)
            message_type = message.get("type") or message.get("role") if isinstance(message, dict) else getattr(message, "type", None)
            if message_type in {"human", "user"}:
                return None
            if name == "ask_clarification":
                content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return None

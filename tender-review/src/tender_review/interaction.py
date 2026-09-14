"""Persistent interaction state for long-running tender-document reviews."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tender_review.document import MineruDocument

REVIEW_STAGES = (
    "compliance-reviewer",
    "evaluation-technical-reviewer",
    "commercial-contract-reviewer",
    "consistency-evidence-reviewer",
)
FIRST_WAVE_STAGES = REVIEW_STAGES[:3]
ISSUE_ACTIONS = frozenset(
    {"confirmed", "false_positive", "deferred", "assigned", "recheck_requested"}
)
_PROFILE_FIELDS = frozenset(
    {"review_goal", "region", "procurement_regime", "legal_review", "internal_rules"}
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return stem or "tender-review"


class ReviewInteractionStore:
    """Thread-safe JSON state used by a DeerFlow review conversation."""

    def __init__(
        self,
        document: MineruDocument,
        state_dir: str | Path,
        *,
        task_id: str,
    ) -> None:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        self.document = document
        self.task_id = task_id.strip()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_path = self.state_dir / f"{_safe_file_stem(self.task_id)}.json"
        self._lock = threading.RLock()
        self._state = self._load() or self._initial_state()

    def _initial_state(self) -> dict[str, Any]:
        source_sha256 = hashlib.sha256(self.document.source_path.read_bytes()).hexdigest()
        return {
            "task_id": self.task_id,
            "document_id": self.document.document_id,
            "source_content_list": str(self.document.source_path),
            "document_sha256": source_sha256,
            "status": "not_started",
            "created_at": _now(),
            "updated_at": _now(),
            "preflight": None,
            "profile": None,
            "interrupt": None,
            "stages": {
                stage: {"status": "pending", "started_at": None, "completed_at": None}
                for stage in REVIEW_STAGES
            },
            "progress": {"completed": 0, "total": len(REVIEW_STAGES), "percent": 0},
            "events": [],
            "report": None,
            "error": None,
            "issue_actions": {},
        }

    def _load(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid review interaction state: {self.state_path}") from exc
        if not isinstance(payload, dict) or payload.get("task_id") != self.task_id:
            raise ValueError(f"review interaction state does not match task: {self.task_id}")
        current_sha256 = hashlib.sha256(self.document.source_path.read_bytes()).hexdigest()
        if (
            payload.get("document_id") != self.document.document_id
            or payload.get("document_sha256") != current_sha256
        ):
            raise ValueError(f"review interaction task belongs to another document: {self.task_id}")
        return payload

    def _save(self) -> dict[str, Any]:
        stages = self._state.get("stages") or {}
        completed = sum(
            1 for stage in REVIEW_STAGES if stages.get(stage, {}).get("status") == "completed"
        )
        self._state["progress"] = {
            "completed": completed,
            "total": len(REVIEW_STAGES),
            "percent": round(completed / len(REVIEW_STAGES) * 100),
            "running": [
                stage for stage in REVIEW_STAGES if stages.get(stage, {}).get("status") == "running"
            ],
        }
        self._state["updated_at"] = _now()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
        return copy.deepcopy(self._state)

    def _event(self, event_type: str, **payload: Any) -> None:
        self._state["events"].append({"ts": _now(), "type": event_type, **payload})

    def queue_agent(self) -> dict[str, Any]:
        """Persist a new task before the Lead Agent starts its first turn."""

        with self._lock:
            if self._state["status"] != "not_started":
                return copy.deepcopy(self._state)
            self._state["status"] = "queued"
            self._event("agent_started")
            return self._save()

    def record_agent_clarification(self, question: str) -> dict[str, Any]:
        """Attach the Lead Agent's rendered clarification to the task snapshot."""

        message = question.strip()
        if not message:
            raise ValueError("agent clarification question must not be empty")
        with self._lock:
            if self._state["status"] != "awaiting_input":
                raise RuntimeError("agent clarification requires an awaiting review profile")
            interrupt = dict(self._state.get("interrupt") or {})
            interrupt.update({"source": "lead_agent", "question": message})
            self._state["interrupt"] = interrupt
            self._event("agent_clarification_requested", question=message)
            return self._save()

    def start(
        self,
        *,
        review_goal: str = "招标文件发布前审核",
        region: str | None = None,
        procurement_regime: str | None = None,
        legal_review: bool = False,
        internal_rules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run preflight and require the Lead Agent to confirm the profile with the user."""

        with self._lock:
            if self._state["status"] not in {"not_started", "queued"}:
                return copy.deepcopy(self._state)

            profile = {
                "review_goal": review_goal.strip() or "招标文件发布前审核",
                "region": region.strip() if isinstance(region, str) and region.strip() else None,
                "procurement_regime": (
                    procurement_regime.strip()
                    if isinstance(procurement_regime, str) and procurement_regime.strip()
                    else None
                ),
                "legal_review": bool(legal_review),
                "internal_rules": [
                    value.strip()
                    for value in (internal_rules or [])
                    if isinstance(value, str) and value.strip()
                ],
                "scope_statement": (
                    "文件内部完整性、一致性与可执行性审核；不作法律定性"
                    if not legal_review
                    else "文件内部审核并对照适用法律规则；法律结论仍需人工复核"
                ),
            }
            overview = self.document.overview()
            self._state["preflight"] = {
                "pages": overview["pages"],
                "evidence_chunks": overview["evidence_chunks"],
                "block_types": overview["block_types"],
                "heading_count": len(overview["headings"]),
                "heading_list_truncated": overview["heading_list_truncated"],
                "stages_total": len(REVIEW_STAGES),
                "stages": list(REVIEW_STAGES),
                "warnings": (
                    ["未识别到目录标题，审核时需要扩大检索关键词"]
                    if not overview["headings"]
                    else []
                ),
            }
            self._state["profile"] = profile
            missing = []
            if legal_review and not profile["region"]:
                missing.append("region")
            if legal_review and not profile["procurement_regime"]:
                missing.append("procurement_regime")

            self._state["status"] = "awaiting_input"
            self._state["interrupt"] = {
                "gate_type": "review_profile",
                "node": "preflight",
                "question": "请确认审核口径；如有调整，请一次性补充地区、采购制度或内部规则。",
                "required_fields": missing,
                "proposed_profile": profile,
            }
            self._event("awaiting_profile_confirmation", missing=missing)
            return self._save()

    def resume(
        self,
        *,
        confirmed: bool,
        profile_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply the user's review-profile decision and continue or cancel."""

        with self._lock:
            if self._state["status"] != "awaiting_input":
                return copy.deepcopy(self._state)
            if not confirmed:
                self._state["status"] = "cancelled"
                self._state["interrupt"] = None
                self._event("review_cancelled")
                return self._save()

            updates = profile_updates or {}
            unknown = sorted(set(updates) - _PROFILE_FIELDS)
            if unknown:
                raise ValueError(f"unsupported profile fields: {', '.join(unknown)}")
            profile = dict(self._state["profile"] or {})
            for field, value in updates.items():
                if field == "internal_rules":
                    profile[field] = [
                        item.strip()
                        for item in (value or [])
                        if isinstance(item, str) and item.strip()
                    ]
                elif field == "legal_review":
                    profile[field] = bool(value)
                else:
                    profile[field] = (
                        value.strip() if isinstance(value, str) and value.strip() else None
                    )

            missing = []
            if profile.get("legal_review") and not profile.get("region"):
                missing.append("region")
            if profile.get("legal_review") and not profile.get("procurement_regime"):
                missing.append("procurement_regime")
            self._state["profile"] = profile
            profile["scope_statement"] = (
                "文件内部完整性、一致性与可执行性审核；不作法律定性"
                if not profile.get("legal_review")
                else "文件内部审核并对照适用法律规则；法律结论仍需人工复核"
            )
            if missing:
                self._state["interrupt"]["required_fields"] = missing
                self._state["interrupt"]["proposed_profile"] = profile
                self._event("profile_still_incomplete", missing=missing)
                return self._save()

            self._state["status"] = "reviewing"
            self._state["interrupt"] = None
            self._event("profile_confirmed", profile=profile)
            return self._save()

    def require_reviewing(self) -> None:
        with self._lock:
            if self._state["status"] != "reviewing":
                raise RuntimeError(
                    "review session is not ready; call start_tender_review and resolve its gate first"
                )

    def _validate_stage(self, stage: str) -> None:
        if stage not in REVIEW_STAGES:
            raise ValueError(f"unknown review stage: {stage}")

    def stage_started(self, stage: str) -> dict[str, Any]:
        with self._lock:
            self.require_reviewing()
            self._validate_stage(stage)
            if stage == "consistency-evidence-reviewer":
                incomplete = [
                    name
                    for name in FIRST_WAVE_STAGES
                    if self._state["stages"][name]["status"] != "completed"
                ]
                if incomplete:
                    raise RuntimeError(
                        "consistency review requires all first-wave stages: "
                        + ", ".join(incomplete)
                    )
            current = self._state["stages"][stage]
            current.update({"status": "running", "started_at": _now(), "error": None})
            self._event("stage_started", stage=stage)
            return self._save()

    def stage_completed(self, stage: str) -> dict[str, Any]:
        with self._lock:
            self._validate_stage(stage)
            current = self._state["stages"][stage]
            current.update({"status": "completed", "completed_at": _now(), "error": None})
            self._event("stage_completed", stage=stage)
            return self._save()

    def stage_failed(self, stage: str, error: str) -> dict[str, Any]:
        with self._lock:
            self._validate_stage(stage)
            current = self._state["stages"][stage]
            current.update({"status": "failed", "completed_at": _now(), "error": error})
            self._event("stage_failed", stage=stage, error=error)
            return self._save()

    def report_saved(
        self,
        *,
        json_path: str,
        markdown_path: str,
        overall_risk: str,
        finding_count: int,
    ) -> dict[str, Any]:
        with self._lock:
            self.require_report_ready()
            self._state["status"] = "done"
            self._state["report"] = {
                "json_path": json_path,
                "markdown_path": markdown_path,
                "overall_risk": overall_risk,
                "finding_count": finding_count,
            }
            self._event("report_saved", **self._state["report"])
            return self._save()

    def fail_review(self, error: str) -> dict[str, Any]:
        """Persist a terminal orchestration failure for API/SSE consumers."""

        with self._lock:
            message = error.strip() or "unknown review failure"
            self._state["status"] = "failed"
            self._state["error"] = message
            self._state["interrupt"] = None
            self._event("review_failed", error=message)
            return self._save()

    def require_report_ready(self) -> None:
        """Reject finalization until every professional and consistency stage completed."""

        with self._lock:
            incomplete = [
                stage
                for stage in REVIEW_STAGES
                if self._state["stages"][stage]["status"] != "completed"
            ]
            if incomplete:
                raise RuntimeError(
                    "cannot complete report before all review stages: " + ", ".join(incomplete)
                )

    def record_issue_action(
        self,
        finding_id: str,
        action: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if action not in ISSUE_ACTIONS:
                raise ValueError(
                    f"unsupported issue action: {action}; allowed: {', '.join(sorted(ISSUE_ACTIONS))}"
                )
            if not finding_id.strip():
                raise ValueError("finding_id must not be empty")
            if self._state.get("status") != "done":
                raise RuntimeError("issue actions can only be recorded after the report is saved")
            self._state["issue_actions"][finding_id.strip()] = {
                "action": action,
                "note": note.strip() if isinstance(note, str) and note.strip() else None,
                "updated_at": _now(),
            }
            self._event("issue_action_recorded", finding_id=finding_id.strip(), action=action)
            return self._save()

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)


def build_tender_review_interaction_tools(store: ReviewInteractionStore) -> list[Any]:
    """Create DeerFlow tools for preflight, HITL resume, progress and issue actions."""

    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise RuntimeError(
            "Agent dependencies are not installed; run `uv sync --extra agent --extra dev`."
        ) from exc

    def start_tender_review(
        review_goal: str = "招标文件发布前审核",
        region: str | None = None,
        procurement_regime: str | None = None,
        legal_review: bool = False,
        internal_rules: list[str] | None = None,
    ) -> str:
        """Run document preflight and establish the user-facing review profile.

        Every new review pauses here. The Lead Agent must use ask_clarification to present
        the proposed profile and all required fields itself.
        """

        return json.dumps(
            store.start(
                review_goal=review_goal,
                region=region,
                procurement_regime=procurement_regime,
                legal_review=legal_review,
                internal_rules=internal_rules,
            ),
            ensure_ascii=False,
        )

    def resume_tender_review(
        confirmed: bool,
        profile_updates: dict[str, Any] | None = None,
    ) -> str:
        """Apply the Lead Agent's interpretation of a new user clarification reply."""

        return json.dumps(
            store.resume(confirmed=confirmed, profile_updates=profile_updates),
            ensure_ascii=False,
        )

    def get_tender_review_state() -> str:
        """Return preflight, profile, per-stage progress, report and issue-action state."""

        return json.dumps(store.get_state(), ensure_ascii=False)

    def record_tender_issue_action(
        finding_id: str,
        action: str,
        note: str | None = None,
    ) -> str:
        """Record a user's action on a finding after review.

        action must be confirmed, false_positive, deferred, assigned, or recheck_requested.
        """

        return json.dumps(
            store.record_issue_action(finding_id=finding_id, action=action, note=note),
            ensure_ascii=False,
        )

    return [
        StructuredTool.from_function(function)
        for function in (
            start_tender_review,
            resume_tender_review,
            get_tender_review_state,
            record_tender_issue_action,
        )
    ]

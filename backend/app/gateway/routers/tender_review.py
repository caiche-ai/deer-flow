"""HTTP and SSE endpoints for long-running tender-document reviews."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.gateway.authz import get_auth_context, require_auth
from app.tender_review.service import TenderReviewTaskManager

router = APIRouter(prefix="/api/tender-review", tags=["tender-review"])
manager = TenderReviewTaskManager()


class CreateReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_path: str = Field(min_length=1, max_length=1000)
    request: str = Field(default="请对这份招标文件进行发布前审核。", min_length=1, max_length=4000)
    model_name: str | None = Field(default=None, max_length=100)


class ResumeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    model_name: str | None = Field(default=None, max_length=100)


class IssueActionRequest(BaseModel):
    action: Literal["confirmed", "false_positive", "deferred", "assigned", "recheck_requested"]
    note: str | None = Field(default=None, max_length=2000)


def _user_id(request: Request) -> str:
    auth = get_auth_context(request)
    if auth is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(auth.require_user().id)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ValueError, RuntimeError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Tender review operation failed")


@router.get("/documents")
@require_auth
async def list_documents(request: Request) -> dict[str, Any]:
    _user_id(request)
    return {"documents": manager.list_documents()}


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED)
@require_auth
async def create_review(body: CreateReviewRequest, request: Request) -> dict[str, Any]:
    try:
        return manager.create_task(
            user_id=_user_id(request),
            document_path=body.document_path,
            request=body.request,
            model_name=body.model_name,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}")
@require_auth
async def get_review(task_id: str, request: Request) -> dict[str, Any]:
    try:
        return manager.get_task(user_id=_user_id(request), task_id=task_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/resume")
@require_auth
async def resume_review(task_id: str, body: ResumeReviewRequest, request: Request) -> dict[str, Any]:
    try:
        return manager.resume_task(
            user_id=_user_id(request),
            task_id=task_id,
            message=body.message,
            model_name=body.model_name,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/findings/{finding_id}/action")
@require_auth
async def record_finding_action(
    task_id: str,
    finding_id: str,
    body: IssueActionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return manager.record_issue_action(
            user_id=_user_id(request),
            task_id=task_id,
            finding_id=finding_id,
            action=body.action,
            note=body.note,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/events")
@require_auth
async def stream_review_events(
    task_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    user_id = _user_id(request)
    if last_event_id:
        try:
            event_task_id, raw_sequence = last_event_id.rsplit(":", 1)
            if event_task_id == task_id:
                after_sequence = max(after_sequence, int(raw_sequence))
        except (ValueError, TypeError):
            pass
    try:
        manager.get_task(user_id=user_id, task_id=task_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    return StreamingResponse(
        manager.stream_events(
            user_id=user_id,
            task_id=task_id,
            after_sequence=after_sequence,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/report")
@require_auth
async def download_report(
    task_id: str,
    request: Request,
    format: Literal["json", "markdown"] = Query(default="json"),
) -> FileResponse:
    try:
        path = manager.report_path(user_id=_user_id(request), task_id=task_id, report_format=format)
    except Exception as exc:
        raise _http_error(exc) from exc
    media_type = "application/json" if format == "json" else "text/markdown"
    return FileResponse(path, media_type=media_type, filename=path.name)

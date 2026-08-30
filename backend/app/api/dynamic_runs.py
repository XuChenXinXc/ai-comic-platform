from __future__ import annotations

import re
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from app.db import Database, get_database
from app.dependencies import get_dynamic_workflow_service
from app.dynamic_pipeline.delivery import DeliveryServiceError, DynamicDeliveryService
from app.dynamic_pipeline.schemas import (
    ExpectedRevisionRequest,
    StageActionRequest,
    StageConfirmRequest,
    StageDraftUpdate,
    StageKey,
    WorkflowRunCreate,
)
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import bad_request, conflict, not_found

router = APIRouter(tags=["dynamic-runs"])


class DeliveryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    strict: bool = True
    allow_partial: bool = False
    exported_at: str = ""


def get_dynamic_delivery_service(
    database: Database = Depends(get_database),
) -> DynamicDeliveryService:
    return DynamicDeliveryService(database)


_SERVER_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)[^\s,;]+|"
    r"(?<![\w:])/(?:[^/\s,;]+/)*[^/\s,;]+"
)


def _safe_delivery_detail(exc: DeliveryServiceError) -> str:
    detail = str(exc).strip() or "动态交付请求失败"
    return _SERVER_PATH_RE.sub("[已隐藏路径]", detail)[:1000]


def _raise_delivery_error(exc: DeliveryServiceError) -> NoReturn:
    detail = _safe_delivery_detail(exc)
    code = getattr(exc, "code", "delivery_error")
    http_status = getattr(exc, "http_status", 400)

    # 统一按错误码 + http_status 路由，不再依赖字符串子串匹配。
    if code == "workflow_run_not_found" or http_status == 404:
        raise not_found("运行") from exc

    if code == "preflight_blocked" or code == "input_stale" or http_status == 409:
        raise conflict(detail) from exc

    raise bad_request(detail) from exc


@router.post("/free-create/dynamic-runs", status_code=status.HTTP_201_CREATED)
def create_free_create_run(
    body: WorkflowRunCreate,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.create_run(body)


@router.post("/projects/{project_id}/dynamic-runs", status_code=status.HTTP_201_CREATED)
def create_project_run(
    project_id: str,
    body: WorkflowRunCreate,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.create_run(body, project_id)


@router.get("/projects/{project_id}/dynamic-runs")
def list_project_runs(
    project_id: str,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> list[dict]:
    return service.list_runs(project_id)


@router.get("/dynamic-runs/{run_id}")
def get_dynamic_run(
    run_id: str,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.get_run(run_id)


@router.get("/dynamic-runs/{run_id}/delivery/checklist")
def get_dynamic_delivery_checklist(
    run_id: str,
    service: DynamicDeliveryService = Depends(get_dynamic_delivery_service),
) -> dict[str, Any]:
    try:
        return service.get_checklist(run_id)
    except DeliveryServiceError as exc:
        _raise_delivery_error(exc)


@router.get("/dynamic-runs/{run_id}/stages")
def get_dynamic_run_with_stages(
    run_id: str,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.get_run_with_stages(run_id)


@router.post("/dynamic-runs/{run_id}/delivery/preflight")
def preflight_dynamic_delivery(
    run_id: str,
    body: DeliveryOptions,
    service: DynamicDeliveryService = Depends(get_dynamic_delivery_service),
) -> dict[str, Any]:
    try:
        return service.preflight(
            run_id,
            strict=body.strict,
            allow_partial=body.allow_partial,
        ).model_dump(mode="json")
    except DeliveryServiceError as exc:
        _raise_delivery_error(exc)


@router.post("/dynamic-runs/{run_id}/delivery/export")
def export_dynamic_delivery(
    run_id: str,
    body: DeliveryOptions,
    service: DynamicDeliveryService = Depends(get_dynamic_delivery_service),
) -> Response:
    try:
        zip_bytes, filename, _manifest = service.export(
            run_id,
            strict=body.strict,
            allow_partial=body.allow_partial,
            exported_at=body.exported_at,
        )
    except DeliveryServiceError as exc:
        _raise_delivery_error(exc)

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/dynamic-runs/{run_id}/stages/{stage_key}")
def get_dynamic_stage(
    run_id: str,
    stage_key: StageKey,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.get_stage(run_id, stage_key)


@router.post("/dynamic-runs/{run_id}/start")
def start_dynamic_run(
    run_id: str,
    body: ExpectedRevisionRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.start_run(run_id, body.expected_revision)


@router.post("/dynamic-runs/{run_id}/pause")
def pause_dynamic_run(
    run_id: str,
    body: ExpectedRevisionRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.pause_run(run_id, body.expected_revision)


@router.post("/dynamic-runs/{run_id}/cancel")
def cancel_dynamic_run(
    run_id: str,
    body: ExpectedRevisionRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.cancel_run(run_id, body.expected_revision)


@router.post("/dynamic-runs/{run_id}/resume")
def resume_dynamic_run(
    run_id: str,
    body: ExpectedRevisionRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.resume_run(run_id, body.expected_revision)


@router.put("/dynamic-runs/{run_id}/stages/{stage_key}/draft")
def save_dynamic_stage_draft(
    run_id: str,
    stage_key: StageKey,
    body: StageDraftUpdate,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.save_stage_draft(run_id, stage_key, body)


@router.post("/dynamic-runs/{run_id}/stages/{stage_key}/confirm")
def confirm_dynamic_stage(
    run_id: str,
    stage_key: StageKey,
    body: StageConfirmRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.confirm_stage(run_id, stage_key, body)


@router.post("/dynamic-runs/{run_id}/stages/{stage_key}/regenerate")
def regenerate_dynamic_stage(
    run_id: str,
    stage_key: StageKey,
    body: StageActionRequest,
    service: DynamicWorkflowService = Depends(get_dynamic_workflow_service),
) -> dict:
    return service.regenerate_stage(run_id, stage_key, body.expected_revision)

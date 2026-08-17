from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.dependencies import get_dynamic_workflow_service
from app.dynamic_pipeline.schemas import (
    ExpectedRevisionRequest,
    StageActionRequest,
    StageConfirmRequest,
    StageDraftUpdate,
    StageKey,
    WorkflowRunCreate,
)
from app.services.dynamic_workflow_service import DynamicWorkflowService

router = APIRouter(tags=["dynamic-runs"])


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

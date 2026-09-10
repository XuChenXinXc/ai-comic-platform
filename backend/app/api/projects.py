import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import ValidationError

from app.api.schemas import ProjectCreate, ProjectUpdate
from app.dependencies import get_project_service
from app.dynamic_pipeline.visual_anchor import (
    build_visual_style_spec,
    compile_visual_bible,
    render_visual_style_spec,
)
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


def _visual_bible_payload(
    values: dict[str, Any],
) -> dict[str, Any]:
    """把结构化 visual_bible 校验后转为存储 JSON 字符串（只存结构化字段）。

    visual_bible 显式为 null 表示清除（回到按 style_prompt 自动推导）；
    未提供时保持不变。
    """
    payload = dict(values)
    if "visual_bible" not in payload:
        return payload
    visual_bible = payload.pop("visual_bible")
    if visual_bible is None:
        payload["visual_bible_json"] = ""
        return payload
    try:
        spec = build_visual_style_spec(style_prompt="", overrides=visual_bible)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"visual_bible 结构非法：{exc}",
        ) from exc
    structured = spec.model_dump(
        include={
            "line": True,
            "coloring": True,
            "shading": True,
            "material": True,
            "lighting": True,
            "depth_of_field": True,
            "realism": True,
            "texture": True,
            "forbidden_styles": True,
        },
        exclude_none=True,
    )
    payload["visual_bible_json"] = json.dumps(structured, ensure_ascii=False)
    return payload


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.create(_visual_bible_payload(body.model_dump()))


@router.get("")
def list_projects(
    kind: Literal["regular", "free"] | None = Query(default=None),
    service: ProjectService = Depends(get_project_service),
) -> list[dict]:
    return service.list(kind=kind)


@router.get("/{project_id}")
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.get(project_id)


@router.get("/{project_id}/visual-bible-preview")
def preview_visual_bible(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    """返回当前项目画风实际生效的结构化视觉规范（推导 + 覆盖合并结果）。

    前端视觉圣经面板在「自动推导」模式下用它回显：让用户看到画风原文
    推导出了哪些结构化约束，而不是永远空白。
    """
    current = service.get(project_id)
    spec = compile_visual_bible(
        current["style_prompt"],
        current.get("visual_bible"),
    )
    structured = spec.model_dump(
        include={
            "line": True,
            "coloring": True,
            "shading": True,
            "material": True,
            "lighting": True,
            "depth_of_field": True,
            "realism": True,
            "texture": True,
            "forbidden_styles": True,
        },
        exclude_none=True,
        mode="json",
    )
    return {
        "style_prompt": current["style_prompt"],
        "visual_bible": current.get("visual_bible"),
        "derived": structured,
        "rendered": render_visual_style_spec(spec),
    }


@router.put("/{project_id}")
def update_project(
    project_id: str,
    body: ProjectUpdate,
    service: ProjectService = Depends(get_project_service),
) -> dict:
    return service.update(
        project_id,
        _visual_bible_payload(body.model_dump(exclude_unset=True)),
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Response:
    service.delete(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

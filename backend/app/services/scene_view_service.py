"""Scene View（场景资产包）服务。

一个物理场景（asset，asset_type='scene'）从「一张图」升级为「Scene Pack」：
MASTER + 多个 Scene View。本服务负责 View 元数据（view_key/name/prompt）的
创建、查询、更新与删除；View 的图片生成复用 MediaService 现有流水线
（asset_image 行 + 生成 job + 版本激活），不做第二套媒体体系。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update

from app.db import Database, db
from app.models.tables import asset, asset_image, scene_view, shot
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import project_media_url
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.signature_service import SHOT_SIGNATURE_FIELDS, shot_signature
from app.services.utils import json_loads, new_id, row_dict, utc_now

# 分镜 JSON 列的别名 → 实际列映射（与 shot_service._JSON_FIELDS 一致），
# 删除视图重算分镜 own_signature 时需要先解码 JSON 列。
_SHOT_JSON_FIELDS = {
    "character_ids": "character_ids_json",
    "prop_ids": "prop_ids_json",
    "positions": "positions_json",
    "expressions": "expressions_json",
    "dialogue": "dialogue_json",
    "reference_bindings": "reference_bindings_json",
}

# 默认 Scene View 清单：view_key / 展示名 / 排序 / 默认视角附加提示词。
# 视角附加提示词只描述摄像机位置与构图变化，强调与 MASTER 的空间一致性，
# 绝不重新设计场景。MASTER 的附加提示词为空（用完整的场景母版模板编译）。
DEFAULT_SCENE_VIEWS: tuple[tuple[str, str, int, str], ...] = (
    (
        "master",
        "母版",
        0,
        "",
    ),
    (
        "front",
        "正向",
        1,
        "摄影机位于空间正向主视角方向、以正常平视高度正面观察整个空间。"
        "保持 MASTER 中所有空间布局、门窗位置、家具位置、固定装饰、材质、"
        "主色调、灯光环境与空间比例完全一致，只改变摄影机位置与构图，"
        "不重新设计场景、不移动或增减任何陈设。画面为无人纯背景空镜。",
    ),
    (
        "reverse",
        "反向",
        2,
        "摄影机从主视角的对面方向拍摄（反向机位），以正常平视高度观察空间。"
        "保持 MASTER 中所有空间布局、门窗位置、家具位置、固定装饰、材质、"
        "主色调、灯光环境与空间比例完全一致，只改变摄影机位置与构图，"
        "不重新设计场景、不移动或增减任何陈设、不镜像翻转空间。画面为无人纯背景空镜。",
    ),
    (
        "left45",
        "左前45°",
        3,
        "摄影机位于空间左前方约45°方向，以正常平视高度观察空间。"
        "保持 MASTER 中所有空间布局、门窗位置、家具位置、固定装饰、材质、"
        "主色调、灯光环境与空间比例完全一致，只改变摄影机位置与构图，"
        "不重新设计场景、不移动或增减任何陈设。画面为无人纯背景空镜。",
    ),
    (
        "right45",
        "右前45°",
        4,
        "摄影机位于空间右前方约45°方向，以正常平视高度观察空间。"
        "保持 MASTER 中所有空间布局、门窗位置、家具位置、固定装饰、材质、"
        "主色调、灯光环境与空间比例完全一致，只改变摄影机位置与构图，"
        "不重新设计场景、不移动或增减任何陈设。画面为无人纯背景空镜。",
    ),
)

_MASTER_KEY = "master"


class SceneViewService:
    def __init__(
        self,
        database: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.database = database or db
        self.jobs = jobs or JobService(self.database)
        self._media = MediaService(self.database, self.jobs)

    # ---------- 内部查询 ----------

    def _scene(self, asset_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(asset).where(asset.c.id == asset_id)).first())
        if record is None or record["asset_type"] != "scene":
            raise not_found("场景素材")
        return record

    def _view(self, view_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(scene_view).where(scene_view.c.id == view_id)).first()
            )
        if record is None:
            raise not_found("场景视图")
        return record

    @staticmethod
    def _decode_view(
        record: dict[str, Any],
        project_id: str,
        current_image: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result = dict(record)
        result["current_image"] = current_image
        if current_image and current_image.get("file_path"):
            current_image["file_url"] = project_media_url(project_id, current_image["file_path"])
            current_image["media_url"] = current_image["file_url"]
        return result

    def _current_image(
        self,
        connection: Any,
        asset_id: str,
        view_id: str,
    ) -> dict[str, Any] | None:
        record = row_dict(
            connection.execute(
                select(asset_image)
                .where(
                    asset_image.c.asset_id == asset_id,
                    asset_image.c.scene_view_id == view_id,
                    asset_image.c.is_current.is_(True),
                )
                .order_by(asset_image.c.version_no.desc())
            ).first()
        )
        if not record:
            return None
        record["is_current"] = bool(record["is_current"])
        return record

    # ---------- 查询 ----------

    def list(self, asset_id: str) -> list[dict[str, Any]]:
        current = self._scene(asset_id)
        with self.database.engine.connect() as connection:
            records = []
            for row in connection.execute(
                select(scene_view)
                .where(scene_view.c.asset_id == asset_id)
                .order_by(scene_view.c.sort_order, scene_view.c.view_key, scene_view.c.id)
            ):
                record = dict(row._mapping)
                current_image = self._current_image(connection, asset_id, record["id"])
                records.append(self._decode_view(record, current["project_id"], current_image))
        return records

    # ---------- 创建（幂等补齐默认视图） ----------

    def ensure_defaults(self, asset_id: str) -> list[dict[str, Any]]:
        self._scene(asset_id)
        now = utc_now()
        with self.database.engine.begin() as connection:
            existing_keys = set(
                connection.execute(
                    select(scene_view.c.view_key).where(scene_view.c.asset_id == asset_id)
                ).scalars()
            )
            for view_key, name, sort_order, prompt in DEFAULT_SCENE_VIEWS:
                if view_key in existing_keys:
                    continue
                connection.execute(
                    insert(scene_view).values(
                        id=new_id("scene_view"),
                        asset_id=asset_id,
                        view_key=view_key,
                        name=name,
                        prompt=prompt,
                        prompt_override="",
                        sort_order=sort_order,
                        status="draft",
                        created_at=now,
                        updated_at=now,
                    )
                )
                existing_keys.add(view_key)
        return self.list(asset_id)

    # ---------- 更新 ----------

    def update(self, view_id: str, values: dict[str, Any]) -> dict[str, Any]:
        view = self._view(view_id)
        prompt = str(values.get("prompt") or "").strip()
        changes: dict[str, Any] = {
            "prompt": prompt,
            "prompt_override": "",
            "status": "ready" if prompt else "draft",
            "updated_at": utc_now(),
        }
        with self.database.engine.begin() as connection:
            connection.execute(
                update(scene_view).where(scene_view.c.id == view_id).values(**changes)
            )
        with self.database.engine.connect() as connection:
            current_image = self._current_image(connection, view["asset_id"], view_id)
        updated = {**view, **changes}
        scene = self._scene(view["asset_id"])
        return self._decode_view(updated, scene["project_id"], current_image)

    # ---------- 删除 ----------

    def delete(self, view_id: str) -> None:
        view = self._view(view_id)
        if view["view_key"] == _MASTER_KEY:
            raise conflict("场景母版（MASTER）不可删除；可删除其他视角，或用素材库图片管理切换版本")
        with self.database.engine.begin() as connection:
            # 分镜引用自动置空：场景视角删除后分镜回退到 MASTER/旧图，不丢场景绑定。
            # scene_view_id 属于分镜签名字段，置空后必须重算 own_signature 并推进
            # revision，否则落库签名与实际列不一致，后续签名比对会把分镜误判为过期。
            for row in connection.execute(
                select(shot).where(shot.c.scene_view_id == view_id)
            ):
                decoded = dict(row._mapping)
                for alias, column in _SHOT_JSON_FIELDS.items():
                    decoded[alias] = json_loads(decoded[column], [])
                decoded["scene_view_id"] = None
                semantic = {
                    name: decoded[name]
                    for name in SHOT_SIGNATURE_FIELDS
                    if not name.endswith("_json") and name in decoded
                }
                connection.execute(
                    update(shot)
                    .where(shot.c.id == row.id)
                    .values(
                        scene_view_id=None,
                        own_signature=shot_signature(semantic),
                        revision=decoded["revision"] + 1,
                        updated_at=utc_now(),
                    )
                )
            # 该视图的当前图降级为普通历史版本，避免其 is_current 残留导致
            # 与旧场景图（NULL 分区）同时作为当前图。
            connection.execute(
                update(asset_image)
                .where(
                    asset_image.c.scene_view_id == view_id,
                    asset_image.c.is_current.is_(True),
                )
                .values(is_current=0)
            )
            connection.execute(delete(scene_view).where(scene_view.c.id == view_id))

    # ---------- 生成 View 图片 ----------

    def generate(
        self,
        asset_id: str,
        view_key: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = dict(options or {})
        with self.database.engine.connect() as connection:
            view = row_dict(
                connection.execute(
                    select(scene_view).where(
                        scene_view.c.asset_id == asset_id,
                        scene_view.c.view_key == view_key,
                    )
                ).first()
            )
        if view is None:
            raise bad_request(f"场景视图不存在：{view_key}")
        if view["view_key"] != _MASTER_KEY:
            # 非母版视图以 MASTER 当前图为参考生成：由 MediaService.queue_asset_image
            # 解析 master 图并放进 reference_files（多参考图能力由 Provider 决定）。
            with self.database.engine.connect() as connection:
                master_view = row_dict(
                    connection.execute(
                        select(scene_view.c.id).where(
                            scene_view.c.asset_id == asset_id,
                            scene_view.c.view_key == _MASTER_KEY,
                        )
                    ).first()
                )
                master_image = (
                    self._current_image(connection, asset_id, master_view["id"])
                    if master_view
                    else None
                )
            if master_image is None or not master_image.get("file_path"):
                raise conflict(
                    "生成场景视角图需要先完成 MASTER 母版图：请先为场景生成母版图，"
                    "再生成各视角，以保证空间一致性。"
                )
            options["reference_master_image_id"] = master_image["id"]
        options["scene_view_id"] = view["id"]
        options["scene_view_key"] = view["view_key"]
        return self._media.queue_asset_image(asset_id, options)
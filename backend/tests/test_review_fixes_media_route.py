"""Review 修复回归（B-L10）：/media 静态路由的 project_id→根目录缓存。

覆盖项：
- 缓存命中：TTL 内重复请求不再查库（数据库记录变化也不影响已缓存根目录）。
- 缓存失效：TTL 过期后重新查库解析最新记录。
- 项目删除（目录被移除）后缓存条目失效，按无目录/无记录处理。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, update

from app.db import Database, utc_now
from app.main import ProjectMediaStaticFiles
from app.models.tables import project


def _setup(tmp_path: Path) -> tuple[Database, Path, Path]:
    database = Database(f"sqlite:///{(tmp_path / 'media-cache.db').as_posix()}")
    database.create_all()
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    storage_root = tmp_path / "selected-root"
    project_dir = storage_root / "项目一" / "角色"
    project_dir.mkdir(parents=True)
    (project_dir / "frame.txt").write_text("categorized", encoding="utf-8")
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-1",
                name="测试项目",
                storage_root=str(storage_root),
                storage_name="项目一",
                storage_layout="categorized",
                created_at=now,
                updated_at=now,
            )
        )
    return database, fallback_dir, storage_root


def _build_app(database: Database, directory: Path) -> FastAPI:
    test_app = FastAPI()
    test_app.mount(
        "/media",
        ProjectMediaStaticFiles(directory=directory, database=database),
    )
    return test_app


def test_media_root_cache_hits_within_ttl_and_expires_after(
    tmp_path: Path,
) -> None:
    database, fallback_dir, storage_root = _setup(tmp_path)
    static = ProjectMediaStaticFiles(directory=fallback_dir, database=database)
    try:
        first = static._resolve_project_root("project-1")
        assert first is not None
        assert first == (storage_root / "项目一").resolve()
        assert "project-1" in static._root_cache

        # 修改数据库记录（模拟并发改配置），TTL 内仍命中缓存。
        with database.engine.begin() as connection:
            connection.execute(
                update(project)
                .where(project.c.id == "project-1")
                .values(storage_name="项目一改")
            )
        assert static._resolve_project_root("project-1") == first

        # TTL 过期后重新查库，解析到新目录（该目录不存在则返回 None 前的
        # 解析失败不适用——这里目录真实存在与否由上层 StaticFiles 处理）。
        cached_at, cached_root = static._root_cache["project-1"]
        static._root_cache["project-1"] = (cached_at - 3600.0, cached_root)
        refreshed = static._resolve_project_root("project-1")
        assert refreshed == (storage_root / "项目一改").resolve()
    finally:
        database.dispose()


def test_media_root_cache_invalidates_when_directory_removed(
    tmp_path: Path,
) -> None:
    """项目删除（目录与记录一并移除）后：缓存逐出，解析返回 None（404）。"""
    database, fallback_dir, storage_root = _setup(tmp_path)
    static = ProjectMediaStaticFiles(directory=fallback_dir, database=database)
    try:
        original_root = static._resolve_project_root("project-1")
        assert original_root is not None

        # 模拟项目删除：目录被移除 + 数据库记录删除。
        shutil.rmtree(storage_root)
        with database.engine.begin() as connection:
            connection.execute(delete(project).where(project.c.id == "project-1"))

        # 缓存命中检查发现目录不存在 → 逐出并回源；记录已删除 → None。
        assert static._resolve_project_root("project-1") is None
        assert "project-1" not in static._root_cache
    finally:
        database.dispose()


def test_media_requests_are_served_and_cached_across_requests(
    tmp_path: Path,
) -> None:
    """同一项目的连续 /media 请求命中缓存，响应内容一致。

    响应必须带 ``Cache-Control: no-cache``：当前版本文件复用固定路径，
    浏览器无验证缓存会让上传/生成新图后仍显示旧内容。
    """
    database, fallback_dir, _storage_root = _setup(tmp_path)
    test_app = _build_app(database, fallback_dir)
    try:
        with TestClient(test_app) as client:
            relative_path = quote("角色/frame.txt", safe="/")
            first = client.get(f"/media/project-1/{relative_path}")
            second = client.get(f"/media/project-1/{relative_path}")
            missing = client.get("/media/project-unknown/角色/frame.txt")

        assert first.status_code == 200
        assert first.text == "categorized"
        assert first.headers["cache-control"] == "no-cache"
        assert second.status_code == 200
        assert second.text == "categorized"
        assert missing.status_code == 404
        static = test_app.routes[-1].app  # mount 的 ProjectMediaStaticFiles
        assert "project-1" in static._root_cache
    finally:
        database.dispose()


def test_media_replaced_file_returns_new_content_with_stale_etag(
    tmp_path: Path,
) -> None:
    """当前版本文件复用固定路径：内容被替换后，旧 ETag 请求必须返回新内容。

    回归：上传/生成新图后 URL 不变，浏览器缓存旧内容导致新图不回显。
    """
    database, fallback_dir, storage_root = _setup(tmp_path)
    test_app = _build_app(database, fallback_dir)
    target = storage_root / "项目一" / "角色" / "frame.txt"
    try:
        with TestClient(test_app) as client:
            relative_path = quote("角色/frame.txt", safe="/")
            first = client.get(f"/media/project-1/{relative_path}")
            old_etag = first.headers["etag"]

            # 模拟上传新图覆盖当前版本文件（路径不变、内容替换）
            target.write_text("新内容-new-content", encoding="utf-8")
            second = client.get(
                f"/media/project-1/{relative_path}",
                headers={"If-None-Match": old_etag},
            )

        assert first.status_code == 200
        assert first.text == "categorized"
        assert old_etag
        assert second.status_code == 200
        assert second.text == "新内容-new-content"
        assert second.headers["cache-control"] == "no-cache"
    finally:
        database.dispose()


def test_media_cache_is_bounded_by_max_entries(tmp_path: Path) -> None:
    """容量上限：超过最大条目数时整体清空，避免无界增长。"""
    database, fallback_dir, storage_root = _setup(tmp_path)
    static = ProjectMediaStaticFiles(directory=fallback_dir, database=database)
    try:
        now = utc_now()
        with database.engine.begin() as connection:
            for index in range(300):
                connection.execute(
                    insert(project).values(
                        id=f"project-bulk-{index}",
                        name=f"项目{index}",
                        storage_root=str(storage_root),
                        storage_name=f"项目一-{index}",
                        storage_layout="categorized",
                        created_at=now,
                        updated_at=now,
                    )
                )
        for index in range(300):
            static._resolve_project_root(f"project-bulk-{index}")
        assert len(static._root_cache) <= 256
    finally:
        database.dispose()


def test_media_cache_ttl_boundaries(tmp_path: Path) -> None:
    """缓存时间戳使用 monotonic 时钟；写入后条目带正向时间戳。"""
    database, fallback_dir, _ = _setup(tmp_path)
    static = ProjectMediaStaticFiles(directory=fallback_dir, database=database)
    try:
        before = time.monotonic()
        static._resolve_project_root("project-1")
        cached_at, _root = static._root_cache["project-1"]
        assert cached_at >= before
    finally:
        database.dispose()

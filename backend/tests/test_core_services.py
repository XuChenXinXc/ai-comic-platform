from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest
import requests

from app.db import Database
from app.services.composition_service import _ffconcat_escape, _ffmpeg_filter_quote
from app.services.errors import ServiceError
from app.services.file_service import (
    FileService,
    FileServiceError,
    download_url_atomic,
    project_media_url,
    sanitize_project_storage_name,
)
from app.services.project_service import ProjectService
from app.services.signature_service import (
    asset_image_signature,
    calculate_signature,
    canonical_json,
    is_stale,
    shot_signature,
    source_signature,
    video_signature,
)


def test_ffconcat_escape_handles_single_quote() -> None:
    assert _ffconcat_escape("C:/proj/林'风.png") == "C:/proj/林'\\''风.png"


def test_ffmpeg_filter_quote_escapes_quote_and_backslash() -> None:
    assert _ffmpeg_filter_quote(r"FontName=A'B\C") == r"'FontName=A'\''B\\C'"


def test_source_signature_with_mixed_line_endings_returns_normalized_hash() -> None:
    expected = hashlib.sha256("第一行\n第二行".encode()).hexdigest()

    assert source_signature("第一行\r\n第二行\r\n") == expected


def test_canonical_json_with_different_key_order_returns_same_value() -> None:
    first = canonical_json({"b": 2, "a": {"d": 4, "c": 3}})
    second = canonical_json({"a": {"c": 3, "d": 4}, "b": 2})

    assert first == second


def test_asset_image_signature_after_reference_content_changes_returns_stale(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"first")
    saved = asset_image_signature("asset-signature", "mock", "model", {}, [reference])
    reference.write_bytes(b"second")
    current = asset_image_signature("asset-signature", "mock", "model", {}, [reference])

    assert is_stale(saved, current) is True


def test_shot_signature_after_reference_binding_changes_returns_stale() -> None:
    saved = shot_signature(
        {
            "action": "主角向前走",
            "reference_bindings": [
                {
                    "role": "character_identity",
                    "asset_id": "asset-1",
                    "asset_image_id": "image-1",
                }
            ],
        }
    )
    current = shot_signature(
        {
            "action": "主角向前走",
            "reference_bindings": [
                {
                    "role": "character_identity",
                    "asset_id": "asset-1",
                    "asset_image_id": "image-2",
                }
            ],
        }
    )

    assert is_stale(saved, current) is True


def test_shot_signature_with_empty_prompt_overrides_matches_legacy_signature() -> None:
    legacy = shot_signature(
        {
            "action": "主角向前走",
            "image_prompt": "主角站在门前",
            "video_prompt": "主角推开房门",
        }
    )

    current = shot_signature(
        {
            "action": "主角向前走",
            "image_prompt": "主角站在门前",
            "image_prompt_override": "",
            "video_prompt": "主角推开房门",
            "video_prompt_override": "",
        }
    )

    assert current == legacy


def test_video_signature_without_reference_bindings_keeps_existing_signature() -> None:
    legacy = calculate_signature(
        "keyframe-1", "动作提示词", 6, "video-1", "model-1", {"size": "16:9"}
    )

    current = video_signature(
        "keyframe-1",
        "动作提示词",
        6,
        "video-1",
        "model-1",
        {"size": "16:9"},
    )

    assert current == legacy


def test_file_service_write_bytes_atomic_saves_relative_project_file(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path)

    target = service.write_bytes_atomic(
        "project-1",
        "keyframes/shot-1/v001.png",
        b"valid-image-content",
    )

    assert target.read_bytes() == b"valid-image-content"
    assert service.relative_project_path("project-1", target) == ("keyframes/shot-1/v001.png")
    assert list((service.project_dir("project-1") / "temp").glob("*.part*")) == []


def test_project_media_url_with_reserved_characters_returns_encoded_url() -> None:
    result = project_media_url("project-1", "项目#1-角色/a%1.png")

    assert result == ("/media/project-1/%E9%A1%B9%E7%9B%AE%231-%E8%A7%92%E8%89%B2/a%251.png")


def test_file_service_initialize_project_creates_named_media_directories(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path)

    project_dir = service.initialize_project("project-1", "项目一")

    assert {path.name for path in project_dir.iterdir() if path.is_dir()} == {
        "项目一-角色",
        "项目一-物品",
        "项目一-场景",
        "项目一-分镜关键帧",
        "项目一-分镜视频",
    }


@pytest.mark.parametrize(
    ("asset_type", "folder"),
    [
        ("character", "项目一-角色"),
        ("prop", "项目一-物品"),
        ("scene", "项目一-场景"),
    ],
)
def test_file_service_asset_image_path_with_storage_name_uses_named_folder(
    tmp_path: Path,
    asset_type: str,
    folder: str,
) -> None:
    service = FileService(tmp_path)

    target = service.asset_image_path(
        "project-1",
        asset_type,
        "asset-1",
        1,
        storage_name="项目一",
    )

    assert service.relative_project_path("project-1", target) == (f"{folder}/asset-1/v001.png")


def test_file_service_keyframe_path_with_storage_name_uses_named_folder(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path)

    target = service.keyframe_path(
        "project-1",
        "shot-1",
        1,
        storage_name="项目一",
    )

    assert service.relative_project_path("project-1", target) == (
        "项目一-分镜关键帧/shot-1/v001.png"
    )


def test_file_service_video_path_with_storage_name_uses_named_folder(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path)

    target = service.video_path(
        "project-1",
        "shot-1",
        1,
        storage_name="项目一",
    )

    assert service.relative_project_path("project-1", target) == ("项目一-分镜视频/shot-1/v001.mp4")


@pytest.mark.parametrize(
    ("name", "folder"),
    [
        ("项目/一", "项目_一-角色"),
        ("项目\\一", "项目_一-角色"),
        ("..", "项目-角色"),
    ],
)
def test_file_service_initialize_project_with_invalid_name_sanitizes_component(
    tmp_path: Path,
    name: str,
    folder: str,
) -> None:
    service = FileService(tmp_path)

    project_dir = service.initialize_project("project-1", name)

    assert (project_dir / folder).is_dir() is True


def test_file_service_sanitize_long_chinese_name_limits_utf16_units() -> None:
    assert sanitize_project_storage_name("项" * 200) == "项" * 80


def test_file_service_sanitize_long_emoji_name_limits_utf16_units() -> None:
    assert sanitize_project_storage_name("😀" * 200) == "😀" * 40


def test_file_service_with_deep_root_and_long_name_writes_media_file(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path)
    project_id = "project_12345678901234567890123456789012"
    asset_id = "asset_12345678901234567890123456789012"
    storage_name = service.project_storage_name(project_id, "😀" * 200)
    service.initialize_project(project_id, storage_name)
    target = service.asset_image_path(
        project_id,
        "character",
        asset_id,
        1,
        storage_name=storage_name,
    )

    relative_path = service.relative_project_path(project_id, target)
    service.write_bytes_atomic(project_id, relative_path, b"image")

    assert target.read_bytes() == b"image"


def test_file_service_asset_path_keeps_persisted_storage_name_prefix(
    tmp_path: Path,
) -> None:
    service = FileService(tmp_path / ("深" * 20))
    storage_name = "项" * 80

    target = service.asset_image_path(
        "project-1",
        "character",
        "asset-1",
        1,
        storage_name=storage_name,
    )

    assert target.parent.parent.name == f"{storage_name}-角色"


def test_project_service_create_initializes_storage_and_returns_path(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    storage = FileService(tmp_path / "files")

    record = ProjectService(database, storage).create({"name": "项目一", "source_text": "测试小说"})

    assert record["storage_path"] == str(storage.project_dir(record["id"]))
    database.dispose()


def test_project_service_list_filters_regular_and_free_create_projects(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    service = ProjectService(database, FileService(tmp_path / "files"))
    regular = service.create({"name": "常规项目"})
    free = service.create({"name": "自由制作", "is_free_create": True})

    assert {item["id"] for item in service.list(kind="regular")} == {regular["id"]}
    assert {item["id"] for item in service.list(kind="free")} == {free["id"]}
    assert {item["id"] for item in service.list()} == {regular["id"], free["id"]}
    assert service.get(free["id"])["is_free_create"] is True
    database.dispose()


def test_project_service_create_with_too_deep_storage_returns_400(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    storage = FileService(tmp_path / ("深" * 50))

    with pytest.raises(ServiceError) as exc_info:
        ProjectService(database, storage).create({"name": "项目一"})

    assert exc_info.value.status_code == 400
    database.dispose()


def test_project_service_update_name_keeps_existing_storage_directories(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    database.create_all()
    storage = FileService(tmp_path / "files")
    service = ProjectService(database, storage)
    record = service.create({"name": "项目一", "source_text": "测试小说"})
    original_project_dir = Path(record["storage_path"])

    service.update(record["id"], {"name": "项目二"})

    project_dir = storage.project_dir(record["id"])
    assert project_dir == original_project_dir
    assert (project_dir / "角色").is_dir() is True
    database.dispose()


def test_file_service_with_path_traversal_rejects_path(tmp_path: Path) -> None:
    service = FileService(tmp_path)

    with pytest.raises(FileServiceError):
        service.resolve_project_path("project-1", "../outside.txt")


def test_file_service_delete_file_removes_only_target(tmp_path: Path) -> None:
    service = FileService(tmp_path)
    first = service.write_bytes_atomic("project-1", "uploads/first.bin", b"first")
    second = service.write_bytes_atomic("project-1", "uploads/second.bin", b"second")

    assert service.delete_file("project-1", "uploads/first.bin") is True
    assert first.exists() is False
    assert second.read_bytes() == b"second"


@pytest.mark.parametrize("project_id", ["", ".", "nested/.."])
def test_file_service_delete_project_with_root_alias_rejects_deletion(
    tmp_path: Path,
    project_id: str,
) -> None:
    service = FileService(tmp_path)
    marker = service.write_bytes_atomic("project-1", "uploads/marker.bin", b"marker")

    with pytest.raises(FileServiceError):
        service.delete_project(project_id)

    assert marker.read_bytes() == b"marker"


def test_download_url_atomic_with_signed_http_error_hides_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingResponse:
        is_redirect = False
        is_permanent_redirect = False

        def __enter__(self) -> FailingResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def raise_for_status() -> None:
            raise requests.HTTPError(
                "403 Client Error for url: https://cdn.invalid/video.mp4?signature=secret-token"
            )

    monkeypatch.setattr(
        "app.services.file_service.requests.get",
        lambda *_args, **_kwargs: FailingResponse(),
    )

    with pytest.raises(FileServiceError) as exc_info:
        download_url_atomic(
            "https://cdn.invalid/video.mp4?signature=secret-token",
            tmp_path / "video.mp4",
        )

    assert "secret-token" not in str(exc_info.value)
    assert "https://cdn.invalid" not in str(exc_info.value)


def test_download_url_atomic_decodes_gzip_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"decoded-image-bytes"

    class Raw:
        def __init__(self) -> None:
            self.decode_content = False
            self._compressed = gzip.compress(payload)
            self._done = False

        def read(self, size: int = -1) -> bytes:
            if self._done:
                return b""
            self._done = True
            return payload if self.decode_content else self._compressed

    class GzipResponse:
        raw = Raw()
        is_redirect = False
        is_permanent_redirect = False

        def __enter__(self) -> GzipResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr(
        "app.services.file_service.requests.get",
        lambda *_args, **_kwargs: GzipResponse(),
    )

    target = tmp_path / "image.png"
    download_url_atomic("https://cdn.invalid/image.png", target)

    assert GzipResponse.raw.decode_content is True
    assert target.read_bytes() == payload


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/media.mp4",
        "http://localhost:8000/media.mp4",
        "http://ip6-localhost/media.mp4",
        "http://service.local/media.mp4",
        "http://internal.cluster.internal/media.mp4",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_download_url_atomic_blocks_private_and_local_hosts(tmp_path: Path, url: str) -> None:
    """SSRF 防护：严禁下载指向内网 IP、localhost 与本地/保留主机的地址。"""
    with pytest.raises(FileServiceError, match="指向内网或保留地址|指向本地或保留主机名"):
        download_url_atomic(url, tmp_path / "out.bin")


def test_first_chapter_inherits_project_source_text(tmp_path: Path) -> None:
    """首章自动继承项目创建时录入的正文，避免分章模型下小说正文丢失。"""
    from app.db import Database
    from app.services.chapter_service import ChapterService

    database = Database(f"sqlite:///{(tmp_path / 'chapter-inherit.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    try:
        project = ProjectService(database).create(
            {"name": "继承测试", "source_text": "雨落在旧城巷口。"}
        )
        chapter = ChapterService(database).create(project["id"], {"title": "第1章"})
        assert chapter["source_text"] == "雨落在旧城巷口。"
        assert chapter["source_signature"] == source_signature("雨落在旧城巷口。")
        # 第二章节不继承（分章录入）
        second = ChapterService(database).create(project["id"], {"title": "第2章"})
        assert second["source_text"] == ""
    finally:
        database.dispose()


def test_bgm_volume_is_coerced_and_clamped() -> None:
    """BGM 音量在拼入 ffmpeg filter 前必须为数字，越界夹取、非法值 400。"""
    from app.services.composition_service import CompositionService

    assert CompositionService._coerce_bgm_volume("0.3") == 0.3
    assert CompositionService._coerce_bgm_volume(2) == 2.0
    assert CompositionService._coerce_bgm_volume(-1) == 0.0
    assert CompositionService._coerce_bgm_volume(9) == 5.0
    for bad in ("0.3,evil", "nan", "inf", None):
        with pytest.raises(ServiceError):
            CompositionService._coerce_bgm_volume(bad)


def test_chapter_rejects_invalid_status_and_guards_concurrent_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """章节状态只允许合法枚举；修订号被并发推进后更新必须报冲突。"""
    from sqlalchemy import update

    from app.models.tables import chapter as chapter_table
    from app.services.chapter_service import ChapterService

    database = Database(f"sqlite:///{(tmp_path / 'chapter-guard.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    try:
        project = ProjectService(database).create({"name": "并发守卫"})
        service = ChapterService(database)

        with pytest.raises(ServiceError) as create_error:
            service.create(project["id"], {"title": "非法状态", "status": "bogus"})
        assert create_error.value.status_code == 400

        record = service.create(project["id"], {"title": "第一章", "source_text": "初始正文"})
        with pytest.raises(ServiceError) as update_error:
            service.update(record["id"], {"status": "bogus"})
        assert update_error.value.status_code == 400

        # 模拟并发编辑：服务读到旧修订号后，另一个写入者把修订号推进
        stale = service.get(record["id"])
        with database.engine.begin() as connection:
            connection.execute(
                update(chapter_table)
                .where(chapter_table.c.id == record["id"])
                .values(source_revision=stale["source_revision"] + 5)
            )
        monkeypatch.setattr(service, "get", lambda _chapter_id: stale)

        with pytest.raises(ServiceError) as conflict_error:
            service.update(record["id"], {"source_text": "并发后的正文"})
        assert conflict_error.value.status_code == 409
    finally:
        database.dispose()


def test_chapter_renumber_avoids_large_user_chapter_numbers(tmp_path: Path) -> None:
    """用户可使用大编号章节，重排的临时区间不能与之碰撞。"""
    database = Database(f"sqlite:///{(tmp_path / 'chapter-renumber.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    try:
        from app.services.chapter_service import ChapterService

        project = ProjectService(database).create({"name": "重排测试"})
        service = ChapterService(database)
        first = service.create(project["id"], {"title": "A", "chapter_no": 100000})
        second = service.create(project["id"], {"title": "B", "chapter_no": 100001})

        reordered = service.reorder(project["id"], [second["id"], first["id"]])

        assert [(item["title"], item["chapter_no"]) for item in reordered] == [
            ("B", 1),
            ("A", 2),
        ]
    finally:
        database.dispose()


def test_request_schema_limits_reject_abuse_but_keep_valid_values() -> None:
    """新增的请求上限只拦截异常输入，合法请求保持可用。"""
    from pydantic import ValidationError

    from app.api.schemas import GenerateRequest, ProjectCreate, SubtitleItem

    nested: object = 1
    for _ in range(20):
        nested = {"k": nested}
    with pytest.raises(ValidationError):
        GenerateRequest(params={"deep": nested})
    with pytest.raises(ValidationError):
        GenerateRequest(params={"blob": "x" * (64 * 1024 + 1)})
    with pytest.raises(ValidationError):
        ProjectCreate(name="超长正文", source_text="a" * 5_000_001)

    assert SubtitleItem(text="台词", start_time=0, end_time=1).style_preset == "default"
    with pytest.raises(ValidationError):
        SubtitleItem.model_validate(
            {"text": "台词", "start_time": 0, "end_time": 1, "style_preset": "unknown"}
        )

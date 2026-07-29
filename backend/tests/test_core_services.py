from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests

from app.services.file_service import FileService, FileServiceError, download_url_atomic
from app.services.signature_service import (
    asset_image_signature,
    canonical_json,
    is_stale,
    shot_signature,
    source_signature,
)


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
        def __enter__(self) -> FailingResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def raise_for_status() -> None:
            raise requests.HTTPError(
                "403 Client Error for url: "
                "https://cdn.invalid/video.mp4?signature=secret-token"
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

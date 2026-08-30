from __future__ import annotations

import io
import json
import zipfile

import pytest
from pydantic import ValidationError

from app.dynamic_pipeline.exporter import (
    STAGE_EXPORTS,
    DeliveryExportInput,
    DeliveryManifest,
    ExportEntry,
    build_delivery_files,
    build_delivery_manifest,
    build_zip_bytes,
    render_stage_markdown,
)


def _input(
    *,
    stages: list[dict[str, object]] | dict[str, object] | None = None,
    checklist: dict[str, object] | None = None,
    export_mode: str = "strict",
) -> DeliveryExportInput:
    return DeliveryExportInput(
        run={
            "id": "run-export-1",
            "status": "confirmed",
            "revision": 4,
            "input_signature": "run-signature",
            "provenance": {"source": "test"},
        },
        stages=stages or [],
        checklist=checklist
        or {
            "references": {"completed": 2, "total": 2},
            "keyframes": {"completed": 1, "total": 1},
            "videos": {"completed": 1, "total": 1},
            "audio": {"completed": 1, "total": 1},
            "subtitle": {"completed": 1, "total": 1},
            "bgm": {"status": "none"},
            "composition": {"status": "completed"},
        },
        files=[{"kind": "video", "path": "第01章/分镜视频/001.mp4", "status": "completed"}],
        versions=[{"id": "version-1", "input_signature": "version-signature"}],
        export_mode=export_mode,  # type: ignore[arg-type]
        exported_at="2026-08-18T12:00:00Z",
    )


def _stage(key: str, output: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    return {
        "stage_key": key,
        "status": "confirmed",
        "attempt": 2,
        "revision": 3,
        "input_signature": f"{key}-input",
        "gate_result": {"status": "ready", "passed": True, "diagnostics": []},
        "provenance": {"stage": key, "provider_model": "mock-v1"},
        "draft_output": output or {"title": f"draft-{key}"},
        "normalized_output": {"title": f"normalized-{key}"},
        "raw_output": {"provider": "raw-provider-output"},
        **extra,
    }


def test_export_input_and_manifest_are_strict_models() -> None:
    with pytest.raises(ValidationError):
        DeliveryExportInput.model_validate(
            {
                "run": {},
                "stages": [],
                "checklist": {},
                "files": [],
                "versions": [],
                "export_mode": "strict",
                "exported_at": "now",
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError):
        DeliveryManifest.model_validate({"exported_at": "now", "unknown": True})


def test_seven_stage_files_and_missing_placeholders_are_always_exported() -> None:
    data = _input(stages=[_stage("0-source")])

    files = build_delivery_files(data)
    names = [entry.arcname for entry in files]

    assert names[:8] == list(STAGE_EXPORTS.values())
    assert "metadata/manifest.json" in names
    assert names.count("metadata/stages/0-source.json") == 1
    assert names.count("metadata/stages/5b-video-prompts.json") == 1
    missing_markdown = next(
        entry.text for entry in files if entry.arcname == STAGE_EXPORTS["4-storyboard"]
    )
    assert missing_markdown is not None
    assert "状态占位" in missing_markdown
    manifest = build_delivery_manifest(data)
    assert manifest.status == "blocked"
    assert any(item["stage_key"] == "4-storyboard" for item in manifest.missing)


def test_draft_output_has_priority_over_normalized_output() -> None:
    markdown = render_stage_markdown(
        "3-script",
        {
            "stage_key": "3-script",
            "status": "ready",
            "draft_output": {"scene": "人工 draft"},
            "normalized_output": {"scene": "normalized"},
            "gate_result": {"status": "ready", "passed": True},
        },
    )

    assert "人工 draft" in markdown
    assert "normalized" not in markdown
    assert "draft_output" in markdown


def test_prompt_markdown_shows_final_and_retains_three_layers_in_metadata() -> None:
    prompt_bundle = {
        "generated": {"prompt": "generated prompt"},
        "human_override": {"image_prompt": "human prompt", "override_version": "2"},
        "final": {"prompt": "final prompt"},
        "input_hash": "prompt-signature",
        "provenance": {"compiler_version": "compiler-1"},
    }
    markdown = render_stage_markdown(
        "5a-image-prompts",
        {
            "stage_key": "5a-image-prompts",
            "status": "ready",
            "draft_output": {"bundles": {"shot-1": prompt_bundle}},
            "normalized_output": {},
            "gate_result": {"status": "ready", "passed": True},
        },
    )

    assert "final prompt" in markdown
    assert "generated prompt" in markdown
    assert "human prompt" in markdown
    assert "#### final" in markdown
    assert "#### generated" in markdown
    assert "#### human_override" in markdown


def test_raw_output_is_only_in_stage_json_and_manifest_is_redacted() -> None:
    data = _input(
        stages=[
            _stage(
                "0-source",
                {"summary": "正文", "api_key": "api-secret"},
                raw_output={
                    "api_key": "api-secret",
                    "cookie": "cookie-secret",
                    "authorization": "Bearer bearer-secret",
                    "url": "https://example.test/a?X-Amz-Signature=signed-secret",
                    "path": "C:\\private\\secret.txt",
                },
            )
        ],
    )
    files = build_delivery_files(data)
    markdown = next(entry.text for entry in files if entry.arcname == "00_拆解.md")
    metadata = next(
        entry.text for entry in files if entry.arcname == "metadata/stages/0-source.json"
    )
    assert markdown is not None and "raw-provider-output" not in markdown
    assert "api-secret" not in (metadata or "")
    assert "cookie-secret" not in (metadata or "")
    assert "bearer-secret" not in (metadata or "")
    assert "signed-secret" not in (metadata or "")
    assert "private\\secret.txt" not in (metadata or "")
    assert "<redacted>" in (metadata or "")


def test_manifest_preserves_status_gate_diagnostics_attempt_revision_signature_provenance() -> None:
    data = _input(
        stages=[
            _stage(
                "0-source",
                {"content": "ok", "artifact_meta": {"content_signature": "content-1"}},
                diagnostics=[{"level": "warning", "code": "source_warning", "message": "提示"}],
                gate_result={
                    "status": "warning",
                    "passed": True,
                    "diagnostics": [
                        {
                            "level": "warning",
                            "code": "gate_warning",
                            "message": "门禁提示",
                        }
                    ],
                },
            )
        ]
    )

    manifest = build_delivery_manifest(data)
    stage = manifest.stages[0]
    assert stage["status"] == "confirmed"
    assert stage["gate_result"]["status"] == "warning"
    assert stage["attempt"] == 2
    assert stage["revision"] == 3
    assert stage["input_signature"] == "0-source-input"
    assert stage["provenance"]["stage"] == "0-source"
    assert any(item["diagnostic"]["code"] == "gate_warning" for item in manifest.diagnostics)


def test_partial_export_is_explicit_and_checklist_is_rendered_in_readme() -> None:
    data = _input(
        stages=[_stage("0-source")],
        checklist={
            "references": {"completed": 1, "total": 2, "reason": "缺少一个角色参考图"},
            "keyframes": {"completed": 0, "total": 1},
            "videos": {"completed": 0, "total": 1},
            "audio": {"status": "skipped", "reason": "未启用"},
            "subtitle": {"completed": 0, "total": 1},
            "bgm": {"status": "none"},
            "composition": {"status": "pending"},
        },
        export_mode="partial",
    )

    manifest = build_delivery_manifest(data)
    files = build_delivery_files(data, manifest)
    readme = next(entry.text for entry in files if entry.arcname == "README.md")

    assert manifest.export_mode == "partial"
    assert manifest.status == "partial"
    assert manifest.missing
    assert manifest.skipped
    assert readme is not None
    for label in ("角色/场景参考图", "关键帧", "分镜视频", "配音", "字幕", "BGM", "成片"):
        assert label in readme
    assert "export_mode：`partial`" in readme


def test_manifest_uses_preflight_checklist_and_keeps_signature_provenance() -> None:
    data = _input(
        stages=[_stage(key) for key in STAGE_EXPORTS if key != "README.md"],
    ).model_copy(
        update={
            "preflight": {
                "workflow_run_id": "run-export-1",
                "status": "ready",
                "input_signature": "preflight-signature",
                "items": [
                    {
                        "kind": "keyframe",
                        "id": "keyframe-1",
                        "status": "ready",
                        "path": "keyframes/1.png",
                        "reason": "",
                        "required": True,
                    }
                ],
                "missing": [],
                "failed": [],
                "skipped": [],
                "provenance": {"manifest_hash": "preflight-manifest"},
            },
            "input_signature": "preflight-signature",
            "provenance": {"workflow_run_id": "run-export-1", "source": "snapshot"},
        }
    )

    manifest = build_delivery_manifest(data)

    assert manifest.status == "success"
    assert manifest.input_signature == "preflight-signature"
    assert manifest.preflight["status"] == "ready"
    assert manifest.provenance["preflight"]["manifest_hash"] == "preflight-manifest"
    assert manifest.checklist[1]["status"] == "completed"


def test_strict_failed_and_partial_skipped_reasons_are_not_silent() -> None:
    data = _input(
        stages=[
            _stage("0-source"),
            _stage("1-character-bios", status="failed", error="Provider 失败"),
            _stage("2-rhythm", status="skipped", error="用户跳过"),
        ]
    )

    manifest = build_delivery_manifest(data)

    assert manifest.status == "blocked"
    assert any(item["stage_key"] == "1-character-bios" for item in manifest.failed)
    assert any(item["stage_key"] == "2-rhythm" for item in manifest.skipped)
    assert any(item["reason"] == "Provider 失败" for item in manifest.failed)
    assert any(item["reason"] == "用户跳过" for item in manifest.skipped)


def test_planned_composition_does_not_make_strict_manifest_blocked() -> None:
    data = _input(
        stages=[_stage(key) for key in STAGE_EXPORTS if key != "README.md"],
        checklist={
            "references": {"status": "completed", "completed": 0, "total": 0},
            "keyframes": {"completed": 1, "total": 1},
            "videos": {"completed": 1, "total": 1},
            "audio": {"completed": 1, "total": 1},
            "subtitle": {"completed": 1, "total": 1},
            "bgm": {"status": "none"},
            "composition": {"status": "planned"},
        },
    )

    manifest = build_delivery_manifest(data)

    assert manifest.status == "success"
    assert manifest.missing == []
    assert manifest.failed == []


def test_stable_entries_and_zip_bytes_for_same_input() -> None:
    data = _input(stages=[_stage(key) for key in STAGE_EXPORTS if key != "README.md"])
    first = build_delivery_files(data)
    second = build_delivery_files(data)
    assert [(item.arcname, item.sha256, item.size) for item in first] == [
        (item.arcname, item.sha256, item.size) for item in second
    ]
    assert build_zip_bytes(first) == build_zip_bytes(second)

    with zipfile.ZipFile(io.BytesIO(build_zip_bytes(first))) as archive:
        assert archive.namelist()[:8] == list(STAGE_EXPORTS.values())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        manifest = json.loads(archive.read("metadata/manifest.json"))
    assert manifest["exported_at"] == "2026-08-18T12:00:00Z"


def test_zip_entry_path_safety_rejects_absolute_drive_and_parent_paths() -> None:
    for arcname in ("../secret.txt", "/secret.txt", r"C:\\secret.txt", r"folder\\..\\secret.txt"):
        with pytest.raises((ValidationError, ValueError)):
            build_zip_bytes([ExportEntry(arcname=arcname, text="secret")])


def test_legacy_media_export_entry_point_is_not_replaced() -> None:
    from app.services.media_service import MediaService

    assert MediaService.export_current_videos.__name__ == "export_current_videos"
    assert MediaService.export_current_videos.__module__ == "app.services.media_service"

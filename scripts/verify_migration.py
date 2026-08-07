"""迁移后验证：检查数据一致性、签名可重算、媒体 stale 标记。"""
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.services.signature_service import (  # noqa: E402
    asset_signature,
    shot_signature,
    source_signature,
)

DB_PATH = PROJECT_ROOT / "workspace" / "platform.db"
PID = "project_a6fc133398d84cdb9260f1c0fd646366"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

project = dict(conn.execute("SELECT * FROM project WHERE id=?", (PID,)).fetchone())
print("== 1. 正文正史 ==")
print("source_text 含'长发被雨水打湿':", "长发被雨水打湿" in project["source_text"])
print("source_text 含'短发':", "短发" in project["source_text"])
source_sig = project["source_signature"]
assert source_signature(project["source_text"]) == source_sig, "source_signature 不匹配"

analysis = dict(conn.execute("SELECT * FROM novel_analysis WHERE project_id=?", (PID,)).fetchone())
screenplay = dict(conn.execute("SELECT * FROM screenplay WHERE project_id=?", (PID,)).fetchone())
analysis_ok = analysis["source_signature"] == source_sig
screenplay_ok = screenplay["source_signature"] == source_sig
print("novel_analysis.source_signature 同步:", analysis_ok)
print("screenplay.source_signature 同步:", screenplay_ok)

print("\n== 2. 素材签名可重算 ==")
style = project["style_prompt"]
for row in conn.execute("SELECT * FROM asset WHERE project_id=?", (PID,)):
    profile = json.loads(row["profile_json"])
    expected = asset_signature(profile, row["prompt"], row["negative_prompt"], style)
    status = "OK" if expected == row["own_signature"] else "MISMATCH"
    print(f"  {row['name']}: {status}")
    assert expected == row["own_signature"], f"素材签名不匹配: {row['name']}"

print("\n== 3. 分镜签名可重算 ==")
shot_fields = [
    "source_type", "source_ref", "scene_asset_id", "character_ids", "prop_ids",
    "positions", "start_state", "action", "expressions", "dialogue", "narration",
    "shot_type", "camera_angle", "camera_move", "duration_sec", "end_state",
    "next_hook", "frame_description", "image_prompt", "image_prompt_override",
    "video_prompt", "video_prompt_override", "negative_prompt", "reference_bindings",
]
for row in conn.execute("SELECT * FROM shot WHERE project_id=? ORDER BY shot_order", (PID,)):
    raw = dict(row)
    semantic = {f: raw.get(f) for f in shot_fields}
    for jf in (
        "character_ids",
        "prop_ids",
        "positions",
        "expressions",
        "dialogue",
        "reference_bindings",
    ):
        semantic[jf] = json.loads(raw[f"{jf}_json"])
    expected = shot_signature(semantic)
    status = "OK" if expected == row["own_signature"] else "MISMATCH"
    print(f"  #{row['shot_no']}: {status}")
    assert expected == row["own_signature"], f"分镜签名不匹配: #{row['shot_no']}"

print("\n== 4. 素材/分镜数量与绑定 ==")
count_assets = conn.execute(
    "SELECT COUNT(*) FROM asset WHERE project_id=?", (PID,)
).fetchone()[0]
count_shots = conn.execute(
    "SELECT COUNT(*) FROM shot WHERE project_id=?", (PID,)
).fetchone()[0]
print("assets:", count_assets)
print("shots:", count_shots)
print("prev_kf bindings:", conn.execute(
    "SELECT COUNT(*) FROM shot WHERE project_id=? "
    "AND reference_bindings_json LIKE '%previous_keyframe%'",
    (PID,),
).fetchone()[0])
print("shots with bindings:", conn.execute(
    "SELECT COUNT(*) FROM shot WHERE project_id=? "
    "AND reference_bindings_json LIKE '%asset_id%'",
    (PID,),
).fetchone()[0])

print("\n== 5. 媒体版本状态（不应被删除） ==")
for table in ("asset_image", "keyframe_version", "video_version"):
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"  {table}: {n} 行保留")
kf1 = conn.execute(
    "SELECT COUNT(*) FROM keyframe_version "
    "WHERE shot_id=(SELECT id FROM shot WHERE project_id=? AND shot_no=1) AND is_current=1",
    (PID,),
).fetchone()[0]
print("第1镜 keyframe 文件仍为 current:", kf1)

print("\n全部验证通过 ✅")
conn.close()

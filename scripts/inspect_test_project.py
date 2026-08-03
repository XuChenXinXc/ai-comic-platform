"""只读查询：确认「测试文章」项目数据现状（素材/分镜/绑定/媒体版本）。"""

import json
import sqlite3

conn = sqlite3.connect("workspace/platform.db")
conn.row_factory = sqlite3.Row
pid = "project_a6fc133398d84cdb9260f1c0fd646366"
assets = {
    r["id"]: r["name"]
    for r in conn.execute("SELECT id, name FROM asset WHERE project_id=?", (pid,))
}
n_binds = sum(
    len(json.loads(r[0] or "[]"))
    for r in conn.execute(
        "SELECT reference_bindings_json FROM shot WHERE project_id=?", (pid,)
    )
)
n_shots = conn.execute(
    "SELECT COUNT(*) FROM shot WHERE project_id=?", (pid,)
).fetchone()[0]
n_prev = conn.execute(
    "SELECT COUNT(*) FROM shot WHERE project_id=? "
    "AND reference_bindings_json LIKE '%previous_keyframe%'",
    (pid,),
).fetchone()[0]
print(
    "assets:",
    len(assets),
    "| shots:",
    n_shots,
    "| bindings:",
    n_binds,
    "| prev_kf:",
    n_prev,
)
for r in conn.execute(
    "SELECT shot_no, scene_asset_id, character_ids_json, prop_ids_json "
    "FROM shot WHERE project_id=? ORDER BY shot_order",
    (pid,),
):
    chars = [assets.get(i, i) for i in json.loads(r["character_ids_json"] or "[]")]
    props = [assets.get(i, i) for i in json.loads(r["prop_ids_json"] or "[]")]
    print(
        r["shot_no"],
        "| scene:",
        assets.get(r["scene_asset_id"], "?"),
        "| chars:",
        chars,
        "| props:",
        props,
    )

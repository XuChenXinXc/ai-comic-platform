"""导出「测试文章」项目完整数据（小说原文/素材/分镜）到临时 JSON，供迁移分析。"""

import json
import sqlite3

conn = sqlite3.connect("workspace/platform.db")
conn.row_factory = sqlite3.Row
pid = "project_a6fc133398d84cdb9260f1c0fd646366"

project = dict(conn.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone())
assets = [
    dict(r) for r in conn.execute("SELECT * FROM asset WHERE project_id=?", (pid,))
]
shots = [
    dict(r)
    for r in conn.execute(
        "SELECT * FROM shot WHERE project_id=? ORDER BY shot_order", (pid,)
    )
]

with open("scripts/_export_test_project.json", "w", encoding="utf-8") as f:
    json.dump(
        {
            "project": project,
            "assets": assets,
            "shots": shots,
        },
        f,
        ensure_ascii=False,
        indent=1,
    )
print("project:", project["name"])
print("source_text len:", len(project["source_text"]))
print("assets:", [(a["name"], a["asset_type"]) for a in assets])
print("shots:", len(shots))

"""将 40 镜核心语义字段打印为紧凑摘要，供人工核查。"""

import json

with open("scripts/_export_test_project.json", encoding="utf-8") as f:
    data = json.load(f)

assets = {a["id"]: a["name"] for a in data["assets"]}
for s in data["shots"]:
    chars = [assets.get(c, c) for c in json.loads(s["character_ids_json"])]
    props = [assets.get(p, p) for p in json.loads(s["prop_ids_json"])]
    print(
        f"== #{s['shot_no']} dur={s['duration_sec']:g} "
        f"scene={assets.get(s['scene_asset_id'], '?')} chars={chars} props={props}"  # noqa: E501
    )
    print(f"  start: {s['start_state']}")
    print(f"  action: {s['action']}")
    print(f"  end: {s['end_state']}")
    print(f"  next: {s['next_hook']}")
    print(f"  frame: {s['frame_description']}")

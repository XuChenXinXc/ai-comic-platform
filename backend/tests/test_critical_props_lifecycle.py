from __future__ import annotations

import pytest

from app.db import Database
from app.services.asset_service import AssetService
from app.services.shot_service import ShotService
from app.services.writing_service import WritingService

LifecycleFixture = tuple[
    Database,
    WritingService,
    AssetService,
    ShotService,
    dict[str, object],
    dict[str, object],
]


@pytest.fixture
def lifecycle_services(tmp_path) -> LifecycleFixture:
    database = Database(f"sqlite:///{tmp_path / 'test_props.db'}")
    database.create_all()
    writing_service = WritingService(database)
    asset_service = AssetService(database)
    shot_service = ShotService(database)

    project_record = {
        "name": "道具测试项目",
        "source_text": "少年手持青铜古剑，走进厨房，放下纸袋和手机，看着灶台。",
    }
    with database.engine.begin() as conn:
        from app.models.tables import chapter, project
        from app.services.utils import new_id, utc_now

        p_id = new_id("proj")
        c_id = new_id("chap")
        now = utc_now()
        conn.execute(
            project.insert().values(
                id=p_id,
                name=project_record["name"],
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            chapter.insert().values(
                id=c_id,
                project_id=p_id,
                chapter_no=1,
                title="第一章",
                source_text=project_record["source_text"],
                source_signature="sig_test",
                created_at=now,
                updated_at=now,
            )
        )

    return (
        database,
        writing_service,
        asset_service,
        shot_service,
        {"id": p_id, "name": project_record["name"]},
        {
            "id": c_id,
            "project_id": p_id,
            "source_text": project_record["source_text"],
            "source_signature": "sig_test",
        },
    )


def test_screenplay_filters_non_critical_props_on_save_and_confirm(
    lifecycle_services: LifecycleFixture,
) -> None:
    """剧本在保存和确认时，自动剔除所有非关键道具（如用户报错的10个物品），只保留关键道具。"""
    _, writing_service, _, _, _, chapter_dict = lifecycle_services
    chapter_id = str(chapter_dict["id"])

    dirty_props = [
        "纸袋",
        "咖啡袋",
        "文件夹",
        "手机",
        "抱枕",
        "遥控器",
        "锅铲",
        "灶台",
        "门框",
        "碗筷",
        "青铜古剑",
        "染血的信封",
    ]

    screenplay_content = {
        "title": "测试剧本",
        "logline": "故事梗概",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "厨房",
                "time_weather": "夜晚",
                "characters": ["少年"],
                "objective": "寻找线索",
                "action": "少年手持青铜古剑走入厨房，靠着门框，放下手机和纸袋。",
                "dialogue": [],
                "narration": [],
                "emotion": "警惕",
                "transition_to_next": "次日清晨离开",
                "props": dirty_props,
            }
        ],
    }

    # 保存剧本
    saved = writing_service.update_screenplay(
        chapter_id,
        {"content": screenplay_content, "status": "confirmed"},
    )

    cleaned_props = saved["content"]["scenes"][0]["props"]
    # 验证非关键道具全部被清除，仅保留真正的关键道具
    assert cleaned_props == ["青铜古剑", "染血的信封"]
    for non_prop in [
        "纸袋", "咖啡袋", "文件夹", "手机", "抱枕",
        "遥控器", "锅铲", "灶台", "门框", "碗筷",
    ]:
        assert non_prop not in cleaned_props


def test_asset_audit_lifecycle_with_legacy_dirty_screenplay(
    lifecycle_services: LifecycleFixture,
) -> None:
    """即使存量剧本未清洗且包含非关键道具，素材需求与审计依然只核对真正的关键道具，不产生阻断。"""
    database, _, asset_service, _, project_dict, chapter_dict = lifecycle_services
    project_id = str(project_dict["id"])
    chapter_id = str(chapter_dict["id"])

    # 模拟一个存量旧剧本，直接写库绕过保存清洗
    dirty_screenplay_content = {
        "title": "旧剧本",
        "logline": "旧梗概",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "客厅",
                "time_weather": "白天",
                "characters": ["少年"],
                "action": "少年坐在沙发上拿手机看纸袋。",
                "dialogue": [],
                "narration": [],
                "source_refs": ["P001"],
                "props": ["手机", "纸袋", "灶台", "传家信物"],
            }
        ],
    }
    with database.engine.begin() as conn:
        from app.models.tables import screenplay
        from app.services.signature_service import screenplay_signature
        from app.services.utils import json_dumps, utc_now

        conn.execute(
            screenplay.insert().values(
                chapter_id=chapter_id,
                content_json=json_dumps(dirty_screenplay_content),
                status="confirmed",
                source_signature="sig_test",
                own_signature=screenplay_signature(dirty_screenplay_content),
                revision=1,
                updated_at=utc_now(),
            )
        )

    # 1. 此时没有素材，进行审计：
    # 必须只报缺少 scene「客厅」、character「少年」、prop「传家信物」；
    # 绝不得报缺少「手机」、「纸袋」、「灶台」！
    audit = asset_service.audit_screenplay_assets(chapter_id)
    missing_by_type = {
        item["asset_type"]: [
            m["suggested_name"]
            for m in audit["missing_assets"]
            if m["asset_type"] == item["asset_type"]
        ]
        for item in audit["missing_assets"]
    }
    assert "prop" in missing_by_type
    assert missing_by_type["prop"] == ["传家信物"]
    assert "手机" not in missing_by_type.get("prop", [])
    assert "纸袋" not in missing_by_type.get("prop", [])
    assert "灶台" not in missing_by_type.get("prop", [])

    # 2. 补齐角色、场景和关键道具
    asset_service.create(project_id, {"asset_type": "scene", "name": "客厅"})
    asset_service.create(project_id, {"asset_type": "character", "name": "少年"})
    asset_service.create(project_id, {"asset_type": "prop", "name": "传家信物"})

    # 3. 再次审计：此时素材齐全，必须完全通过（complete is True）
    audit_after = asset_service.audit_screenplay_assets(chapter_id)
    assert audit_after["complete"] is True
    assert audit_after["missing_assets"] == []


def test_shot_binding_preserves_key_props_and_ignores_non_critical(
    lifecycle_services: LifecycleFixture,
) -> None:
    """分镜提取保存时，非关键道具不进入 prop_ids，关键道具准确绑定到素材 ID。"""
    _, _, asset_service, shot_service, project_dict, chapter_dict = lifecycle_services
    project_id = str(project_dict["id"])

    asset_service.create(project_id, {"asset_type": "scene", "name": "林间空地"})
    asset_service.create(project_id, {"asset_type": "character", "name": "少年"})
    sword_asset = asset_service.create(project_id, {"asset_type": "prop", "name": "青铜古剑"})

    # 模拟分镜模型输出的分镜数据，prop_names 中混杂了关键道具和日常非关键物品
    raw_shots = [
        {
            "shot_no": 1,
            "scene_no": 1,
            "scene_name": "林间空地",
            "character_names": ["少年"],
            "prop_names": ["青铜古剑", "手机", "纸袋", "门框"],
            "duration_sec": 6,
            "start_state": "少年站立在空地中。",
            "action": "少年拔出青铜古剑，拿起手机。",
            "end_state": "少年手握青铜古剑神色凝重。",
            "next_hook": "远处传来风声。",
            "shot_type": "中景",
            "camera_angle": "平视",
            "camera_move": "固定",
            "narration": "",
            "image_prompt": "",
            "video_prompt": "",
            "negative_prompt": "",
            "frame_description": "少年手持青铜古剑直视前方。",
            "transition_from_prev": "scene_change",
        }
    ]

    saved_result = shot_service._save_extracted(
        current_chapter=chapter_dict,
        source_type="novel",
        input_data={"scope": "all"},
        extracted_shots=raw_shots,
    )

    assert saved_result["count"] == 1
    shot_item = shot_service.get(saved_result["shot_ids"][0])

    # prop_ids 中必须只包含关键道具「青铜古剑」的 ID
    assert shot_item["prop_ids"] == [sword_asset["id"]]

    # 验证没有报“分镜物品名未匹配到素材库：手机、纸袋、门框”的虚假警告
    warnings = saved_result.get("warnings", [])
    prop_warns = [w for w in warnings if "分镜物品名未匹配" in w.get("message", "")]
    assert len(prop_warns) == 0


def test_full_lifecycle_canon_consistency(
    lifecycle_services: LifecycleFixture,
) -> None:
    """全生命周期验证：小说分析确立资产基准 -> 剧本复用基准 -> 素材精准建档 -> 分镜100%绑定。"""
    database, writing_service, asset_service, shot_service, project_dict, chapter_dict = (
        lifecycle_services
    )
    project_id = str(project_dict["id"])
    chapter_id = str(chapter_dict["id"])

    # 1. 小说正文分析：确立全剧核心资产基准
    analysis_content = {
        "summary": "少年林舟在苏棠公寓客厅中找到了一把青铜古剑。",
        "genre": "奇幻",
        "time_period": "现代",
        "worldview": "都市隐秘修真",
        "main_characters": ["林舟", "苏棠"],
        "character_relations": [{"character1": "林舟", "character2": "苏棠", "relation": "盟友"}],
        "locations": ["苏棠公寓客厅"],
        "key_props": ["青铜古剑"],
        "core_conflict": "寻找古剑的下落",
        "plot_structure": {},
        "emotion_curve": [],
        "foreshadowing": [],
        "visualizable_content": ["古剑泛着幽幽冷光"],
        "hard_to_visualize": [],
        "production_risks": [],
    }
    writing_service.update_analysis(chapter_id, {"content": analysis_content})

    # 2. 剧本生成基于小说分析基准：出场角色为林舟、苏棠，场景为苏棠公寓客厅，props为青铜古剑
    screenplay_content = {
        "title": "第一场 古剑苏醒",
        "logline": "林舟在苏棠客厅发现青铜古剑",
        "scenes": [
            {
                "scene_no": 1,
                "scene_name": "苏棠公寓客厅",
                "time_weather": "夜晚",
                "characters": ["林舟", "苏棠"],
                "source_refs": ["P001"],
                "objective": "确认古剑真伪",
                "action": "林舟拔出青铜古剑，苏棠在旁边注视。",
                "dialogue": [{"speaker": "林舟", "text": "这就是传说中的青铜古剑？"}],
                "narration": [],
                "emotion": "震撼",
                "transition_to_next": "次日清晨",
                "props": ["青铜古剑", "手机"],  # 即使混入日常手机，也会被自动清洗
            }
        ],
    }
    confirmed_screenplay = writing_service.update_screenplay(
        chapter_id,
        {"content": screenplay_content, "status": "confirmed"},
    )
    # 剧本保存清洗后，props 只剩下青铜古剑
    assert confirmed_screenplay["content"]["scenes"][0]["props"] == ["青铜古剑"]

    # 3. 素材库根据剧本事实建档
    char1 = asset_service.create(project_id, {"asset_type": "character", "name": "林舟"})
    char2 = asset_service.create(project_id, {"asset_type": "character", "name": "苏棠"})
    scene1 = asset_service.create(project_id, {"asset_type": "scene", "name": "苏棠公寓客厅"})
    prop1 = asset_service.create(project_id, {"asset_type": "prop", "name": "青铜古剑"})

    # 审计验证：素材需求完全对齐，complete 必须为 True
    audit = asset_service.audit_screenplay_assets(chapter_id)
    assert audit["complete"] is True
    assert audit["missing_assets"] == []

    # 4. 分镜提取与绑定：分镜模型根据素材库输出分镜
    raw_shots = [
        {
            "shot_no": 1,
            "scene_no": 1,
            "scene_name": "苏棠公寓客厅",
            "character_names": ["林舟", "苏棠"],
            "prop_names": ["青铜古剑"],
            "duration_sec": 6,
            "start_state": "林舟与苏棠站在客厅中央。",
            "action": "林舟缓缓拔出青铜古剑。",
            "end_state": "古剑出鞘半截寒光逼人。",
            "next_hook": "苏棠上前一步观察。",
            "shot_type": "近景",
            "camera_angle": "平视",
            "camera_move": "推镜头",
            "narration": "",
            "image_prompt": "",
            "video_prompt": "",
            "negative_prompt": "",
            "frame_description": "林舟手握青铜古剑，剑身寒芒闪烁，苏棠立于身侧。",
            "transition_from_prev": "scene_change",
        }
    ]
    saved_result = shot_service._save_extracted(
        current_chapter=chapter_dict,
        source_type="screenplay",
        input_data={"scope": "all", "expected_source_signature": audit["screenplay_signature"]},
        extracted_shots=raw_shots,
    )
    assert saved_result["count"] == 1
    shot_data = shot_service.get(saved_result["shot_ids"][0])

    # 验证资产绑定 100% 精确一致
    assert shot_data["scene_asset_id"] == scene1["id"]
    assert set(shot_data["character_ids"]) == {char1["id"], char2["id"]}
    assert shot_data["prop_ids"] == [prop1["id"]]
    assert len(saved_result["warnings"]) == 0


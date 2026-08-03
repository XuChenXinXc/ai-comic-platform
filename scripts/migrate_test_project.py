"""「测试文章」项目数据迁移脚本（幂等，可重复执行）。

按审查报告一次性修复当前项目：
1. 修正素材正史设定（林舟服装/表情、苏晴长发开场、林父负向、怀表结构、
   工具箱精密修表工具、钟表店基础场景、青石巷雨势与方向）。
2. 新增「地铁口便利店雨夜」场景素材与「旧合影照片」物品素材。
3. 修正分镜：1-3 镜场景绑定、补齐缺失角色/物品绑定、第 2 镜 previous_keyframe、
   31-36 镜因果顺序重排、30/38 镜怀表结尾意象、深色夹克统一为灰色风衣。
4. 重新计算素材/分镜 own_signature 并 revision+1，旧媒体自动标记 is_stale。

运行方式（在项目根目录）：
    backend/.venv/Scripts/python.exe scripts/migrate_test_project.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.signature_service import (  # noqa: E402
    asset_signature,
    shot_signature,
    source_signature,
)
from app.services.utils import utc_now  # noqa: E402

DB_PATH = PROJECT_ROOT / "workspace" / "platform.db"
PROJECT_ID = "project_a6fc133398d84cdb9260f1c0fd646366"


def load_style_prompt(conn: sqlite3.Connection) -> str:
    """签名计算必须使用项目实际的 style_prompt，避免硬编码漂移。"""
    row = fetch_all(
        conn, "SELECT style_prompt FROM project WHERE id=?", (PROJECT_ID,)
    )
    if not row:
        raise RuntimeError("项目不存在")
    return str(row[0]["style_prompt"] or "")

# ---- 素材 ID ----
ASSET_LIN_ZHOU = "asset_ff9efbb4c3b04e9c8dd023ad3f86a828"  # 林舟
ASSET_SU_QING = "asset_14c5617a0e224ff78627ed0f6c97f1bd"  # 苏晴
ASSET_LIN_FU = "asset_e0e83219e2db49ada9ef56d3761c5b8e"  # 林父
ASSET_WATCH = "asset_46132087de864197b8c5f79e99f90e07"  # 银色怀表
ASSET_TOOLBOX = "asset_584104beedb146ef89cfe0ec883f93c7"  # 黑色工具箱
ASSET_LANE = "asset_a464f7a13bf84e8ba9e9c457ba7d6ef8"  # 青石巷雨夜
ASSET_SHOP = "asset_19f1c3c6433d4987bfbdfc7ed1e5fb3f"  # 林记钟表店内部
SCENE_CONVENIENCE = "asset_convenience_rain_night"  # 新增：地铁口便利店雨夜
PROP_OLD_PHOTO = "asset_old_family_photo"  # 新增：旧合影照片


def backup_database() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = PROJECT_ROOT / "workspace" / f"platform.db.bak-{stamp}"
    # 使用 sqlite3 backup API 做一致性快照：主库为 WAL 模式，
    # 直接复制文件会缺失尚在 -wal 中未 checkpoint 的已提交事务。
    source = sqlite3.connect(DB_PATH)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    print(f"[备份] 已创建一致性快照 -> {target.name}")
    return target


def load_json(value: str, fallback):
    try:
        return json.loads(value or "null")
    except json.JSONDecodeError:
        return fallback


def dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def fetch_all(conn: sqlite3.Connection, sql: str, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params)]


def update_asset(
    conn: sqlite3.Connection,
    asset_id: str,
    *,
    profile: dict | None = None,
    prompt: str | None = None,
    negative_prompt: str | None = None,
    consistency_keywords: list | None = None,
    composition: str | None = None,
    style_prompt: str | None = None,
) -> None:
    if style_prompt is None:
        style_prompt = load_style_prompt(conn)
    row = fetch_all(conn, "SELECT * FROM asset WHERE id=?", (asset_id,))
    if not row:
        raise RuntimeError(f"素材不存在: {asset_id}")
    current = row[0]
    next_profile = load_json(current["profile_json"], {})
    if profile is not None:
        next_profile = {**next_profile, **profile}
    next_prompt = current["prompt"] if prompt is None else prompt
    next_negative = current["negative_prompt"] if negative_prompt is None else negative_prompt
    next_consistency = (
        load_json(current["consistency_json"], [])
        if consistency_keywords is None
        else consistency_keywords
    )
    next_composition = current["composition"] if composition is None else composition
    own_signature = asset_signature(next_profile, next_prompt, next_negative, style_prompt)
    conn.execute(
        "UPDATE asset SET profile_json=?, prompt=?, negative_prompt=?, consistency_json=?, "
        "composition=?, own_signature=?, revision=revision+1, updated_at=? WHERE id=?",
        (
            dump_json(next_profile),
            next_prompt,
            next_negative,
            dump_json(next_consistency),
            next_composition,
            own_signature,
            utc_now(),
            asset_id,
        ),
    )


def insert_asset(
    conn: sqlite3.Connection,
    asset_id: str,
    *,
    asset_type: str,
    name: str,
    profile: dict,
    prompt: str,
    negative_prompt: str,
    consistency_keywords: list,
    composition: str,
    style_prompt: str | None = None,
    importance: str = "core",
) -> None:
    if style_prompt is None:
        style_prompt = load_style_prompt(conn)
    exists = fetch_all(conn, "SELECT id FROM asset WHERE id=?", (asset_id,))
    if exists:
        print(f"[跳过] 素材已存在: {name}")
        return
    own_signature = asset_signature(profile, prompt, negative_prompt, style_prompt)
    now = utc_now()
    conn.execute(
        "INSERT INTO asset (id, project_id, asset_type, name, profile_json, prompt, "
        "negative_prompt, consistency_json, composition, importance, status, "
        "source_signature, own_signature, revision, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            asset_id,
            PROJECT_ID,
            asset_type,
            name,
            dump_json(profile),
            prompt,
            negative_prompt,
            dump_json(consistency_keywords),
            composition,
            importance,
            "ready",
            "",  # source_signature 由迁移后统一回填
            own_signature,
            1,
            now,
            now,
        ),
    )
    print(f"[新增] 素材: {name} ({asset_type})")


def update_shot(
    conn: sqlite3.Connection,
    shot_id: str,
    *,
    scene_asset_id: str | None = None,
    character_ids: list[str] | None = None,
    prop_ids: list[str] | None = None,
    positions: list | None = None,
    expressions: list | None = None,
    dialogue: list | None = None,
    start_state: str | None = None,
    action: str | None = None,
    end_state: str | None = None,
    next_hook: str | None = None,
    frame_description: str | None = None,
    image_prompt: str | None = None,
    video_prompt: str | None = None,
    negative_prompt: str | None = None,
    reference_bindings: list | None = None,
) -> None:
    row = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_id,))
    if not row:
        raise RuntimeError(f"分镜不存在: {shot_id}")
    current = row[0]
    semantic = {
        "source_type": current["source_type"],
        "source_ref": current["source_ref"],
        "scene_asset_id": current["scene_asset_id"] if scene_asset_id is None else scene_asset_id,
        "character_ids": (
            load_json(current["character_ids_json"], []) if character_ids is None else character_ids
        ),
        "prop_ids": load_json(current["prop_ids_json"], []) if prop_ids is None else prop_ids,
        "positions": load_json(current["positions_json"], []) if positions is None else positions,
        "start_state": current["start_state"] if start_state is None else start_state,
        "action": current["action"] if action is None else action,
        "expressions": (
            load_json(current["expressions_json"], []) if expressions is None else expressions
        ),
        "dialogue": load_json(current["dialogue_json"], []) if dialogue is None else dialogue,
        "narration": current["narration"],
        "shot_type": current["shot_type"],
        "camera_angle": current["camera_angle"],
        "camera_move": current["camera_move"],
        "duration_sec": current["duration_sec"],
        "end_state": current["end_state"] if end_state is None else end_state,
        "next_hook": current["next_hook"] if next_hook is None else next_hook,
        "frame_description": (
            current["frame_description"] if frame_description is None else frame_description
        ),
        "image_prompt": current["image_prompt"] if image_prompt is None else image_prompt,
        "image_prompt_override": current["image_prompt_override"],
        "video_prompt": current["video_prompt"] if video_prompt is None else video_prompt,
        "video_prompt_override": current["video_prompt_override"],
        "negative_prompt": current["negative_prompt"]
        if negative_prompt is None
        else negative_prompt,
        "reference_bindings": (
            load_json(current["reference_bindings_json"], [])
            if reference_bindings is None
            else reference_bindings
        ),
    }
    own_signature = shot_signature(semantic)
    conn.execute(
        "UPDATE shot SET scene_asset_id=?, character_ids_json=?, prop_ids_json=?, "
        "positions_json=?, start_state=?, action=?, expressions_json=?, dialogue_json=?, "
        "end_state=?, next_hook=?, frame_description=?, image_prompt=?, video_prompt=?, "
        "negative_prompt=?, reference_bindings_json=?, own_signature=?, revision=revision+1, "
        "updated_at=? WHERE id=?",
        (
            semantic["scene_asset_id"],
            dump_json(semantic["character_ids"]),
            dump_json(semantic["prop_ids"]),
            dump_json(semantic["positions"]),
            semantic["start_state"],
            semantic["action"],
            dump_json(semantic["expressions"]),
            dump_json(semantic["dialogue"]),
            semantic["end_state"],
            semantic["next_hook"],
            semantic["frame_description"],
            semantic["image_prompt"],
            semantic["video_prompt"],
            semantic["negative_prompt"],
            dump_json(semantic["reference_bindings"]),
            own_signature,
            utc_now(),
            shot_id,
        ),
    )


def shot_id_by_no(conn: sqlite3.Connection, shot_no: int) -> str:
    row = fetch_all(
        conn, "SELECT id FROM shot WHERE project_id=? AND shot_no=?", (PROJECT_ID, shot_no)
    )
    if not row:
        raise RuntimeError(f"分镜 #{shot_no} 不存在")
    return row[0]["id"]


def current_asset_image(conn: sqlite3.Connection, asset_id: str) -> str | None:
    row = fetch_all(
        conn,
        "SELECT id FROM asset_image WHERE asset_id=? AND is_current=1 AND status='completed'",
        (asset_id,),
    )
    return row[0]["id"] if row else None


def shot_bindings(
    conn: sqlite3.Connection,
    scene_asset_id: str | None,
    character_ids: list[str],
    prop_ids: list[str],
    *,
    extra: list[dict] | None = None,
) -> list[dict]:
    bindings: list[dict] = []
    if scene_asset_id:
        image_id = current_asset_image(conn, scene_asset_id)
        bindings.append(
            {
                "role": "scene_layout",
                "asset_id": scene_asset_id,
                **({"asset_image_id": image_id} if image_id else {}),
            }
        )
    for asset_id in character_ids:
        image_id = current_asset_image(conn, asset_id)
        bindings.append(
            {
                "role": "character_identity",
                "asset_id": asset_id,
                **({"asset_image_id": image_id} if image_id else {}),
            }
        )
    for asset_id in prop_ids:
        image_id = current_asset_image(conn, asset_id)
        bindings.append(
            {
                "role": "prop_identity",
                "asset_id": asset_id,
                **({"asset_image_id": image_id} if image_id else {}),
            }
        )
    if extra:
        bindings.extend(extra)
    return bindings


# ---------------------------------------------------------------------------
# 素材正史设定（审查报告：素材逐项检查）
# ---------------------------------------------------------------------------


def patch_assets(conn: sqlite3.Connection) -> None:
    # 林舟：统一灰色中长款防水风衣；删除电子表与快递单；表情收敛为 3 个；
    # 设定板改中性背景，去除"写实电影感/胶片颗粒"与二维国漫的冲突。
    update_asset(
        conn,
        ASSET_LIN_ZHOU,
        profile={
            "accessories": "无额外配饰（删除无剧情作用的电子表）",
            "carried_item": "内袋携带一张旧照片",
            "expression_group": ["平静", "疑惑", "释然"],
            "style_tone": "细腻干净的二维国漫质感",
            "lighting": "标准角色设定柔光",
            "atmosphere": "克制、压抑中透出一点温暖",
        },
        prompt=(
            "25岁中国男性，黑色短发，深棕色眼睛，清瘦面容，穿灰色中长款防水风衣，"
            "内搭白色衬衫和深灰色针织背心，深蓝色牛仔裤，深色运动鞋，神情内敛沉静。"
            "背景为中性纯色，无任何环境道具与文字。"
        ),
        composition=(
            "半身像，侧45度角，视线朝向画面左侧，人物位于画面中央偏右，"
            "背景为单一中性纯色，留出左方构图空间。"
        ),
        consistency_keywords=["黑色短发", "深棕色眼睛", "灰色中长款防水风衣", "清瘦身形"],
    )

    # 苏晴：保留长发（唯一正史），开场描述统一为长发；负向已含"不要短发"。
    update_asset(
        conn,
        ASSET_SU_QING,
        profile={
            "hair": "黑色长直发，自然垂在肩后，发尾微弯",
            "costume_variants": [
                "雨夜刚到时发梢微湿，风衣上有雨珠",
                "擦拭柜台时用腰带把头发扎成低马尾",
            ],
            "style_tone": "温暖生活化的二维国漫质感",
        },
    )

    # 林父：正向现代工装夹克与负向"不要现代服装"冲突，修正负向并补第三个表情。
    update_asset(
        conn,
        ASSET_LIN_FU,
        profile={"expression_group": ["温柔微笑", "愧疚凝视", "慈爱回望"]},
        negative_prompt="不要古装、潮流街头服饰、赛博元素，不要戴帽子，不要愁眉苦脸，不要模糊面部",
    )

    # 银色怀表：统一表冠/吊环在 12 点、链条只接顶部；"舟"字极小；
    # 删除无来源的固定时间 10:10；比例改为略大于普通硬币。
    update_asset(
        conn,
        ASSET_WATCH,
        profile={
            "description": (
                "银色怀表，直径约4.5cm，厚1.2cm，金属表壳，白色珐琅表盘，黑色罗马数字，"
                "表盖外侧刻有极小的'舟'字，配银色蛇骨链，表壳有轻微划痕和氧化，指针静止。"
            ),
            "front_structure": (
                "正面为可开启的表盖，表盖中央偏上刻有极小的'舟'字，"
                "围绕字周有细花纹，表盖边缘有卡扣"
            ),
            "key_structure_position": (
                "表冠与吊环统一位于12点方位；表链只连接顶部吊环；"
                "刻字'舟'极小，位于表盖中央偏上"
            ),
            "side_structure": "顶部有一枚表冠，表冠顶端有棱纹，吊环位于表冠旁，表链只接顶部",
            "silhouette": "圆形表盘，顶部有表冠与吊环，链条只接顶部，整体轮廓简约圆润",
            "dimensions": "直径4.5cm，厚度1.2cm，表链长20cm",
            "scale_reference": "略大于普通硬币，可握于掌心，普通人一只手可完全遮住",
        },
        prompt=(
            "一只银色机械怀表，圆形表壳，表面有拉丝工艺。表盖中央偏上刻有极小的'舟'字，"
            "白色珐琅表盘，配黑色罗马数字，指针为蓝钢针且静止，表壳边缘有细微划痕和氧化斑点。"
            "表冠与吊环在12点方位，银色蛇骨链只连接顶部吊环，整体气质复古精致。"
            "背景为中性纯色。"
        ),
        composition=(
            "俯视特写，表盘居中，表链自然盘绕在深色桌面上，侧光使得表壳边缘产生高光，"
            "表盖微微开启呈现内部白色表盘。"
        ),
        consistency_keywords=[
            "圆形银色表壳",
            "极小的'舟'字",
            "白色珐琅表盘",
            "罗马数字",
            "顶部吊环蛇骨链",
        ],
    )

    # 黑色工具箱：强调精密修表工具，排除大扳手/钳子/锤子；删除剧情外信件。
    update_asset(
        conn,
        ASSET_TOOLBOX,
        profile={
            "description": (
                "黑色铁皮工具箱，长约40cm，宽20cm，高15cm。箱体烤漆已经部分脱落，"
                "露出铁皮底色，表面有数道划痕和碰撞凹痕，正面左右各有一个金属搭扣和一个锁孔。"
                "箱内上层托盘可拿出，下层内衬为深棕色绒布，整齐放置精密修表工具"
                "（小号螺丝刀、镊子、放大镜、细齿齿轮）和一本皮质笔记本。"
            ),
            "special_features": [
                "箱内第一页笔记写有给林舟的生日承诺",
                "工具按类型排列，体现出工匠习惯",
                "暗层中藏有现金和修表记录本",
            ],
        },
        prompt=(
            "一只深黑色铁皮工具提箱，长方体，箱体表面有轻微凹陷和铁锈，正面有两个金属搭扣，"
            "顶部为弧形提手。箱盖打开45度，露出内部两层：上层整齐排列精密修表工具"
            "（小号螺丝刀、镊子、放大镜、细齿齿轮），下层有一本浅黄色皮质笔记本。"
            "箱子放置在木凳上，光线从侧面照亮内部。背景为中性纯色。"
        ),
        negative_prompt=(
            "不要人物，不要塑料箱，不要彩色贴纸，不要工具杂乱，不要现代材质（如尼龙袋），"
            "不要大扳手、钳子、锤子等汽修工具"
        ),
    )

    # 钟表店内部：基础场景移除可移动道具与已开启机关；钟表时间统一为停摆。
    update_asset(
        conn,
        ASSET_SHOP,
        profile={
            "detail_zone": ["挂钟群及其停摆指针", "木柜台", "木架与落尘"],
            "prop_position": "柜台台面保持空置，不摆放工具箱、怀表等可移动道具；暗格关闭状态不可见",
            "special_material": "柜台为杉木实木，挂钟外壳为金属或木质，吊灯为铁质灯罩",
            "furnishings": [
                "木柜台",
                "组架",
                "维修工作台",
                "铁皮柜",
                "木椅",
                "落地式挂钟",
                "墙上挂钟群",
            ],
        },
        prompt=(
            "老式钟表修理店内部，约30平米，四面墙挂满停摆的挂钟，所有指针统一停在六点五十分。"
            "木质柜台位于进门正前方，柜台台面空置。侧面墙有木架，靠后墙是工作台。"
            "空间昏暗，只有一盏吊灯照明，灰尘在光柱中飘浮。画面无人物、无文字。"
        ),
        negative_prompt=(
            "不要人物、不要人影、不要剪影、不要人物倒影、不要现代物品、不要明亮自然光、"
            "不要工具箱、不要怀表、不要暗格机关"
        ),
    )

    # 青石巷雨夜：强化雨丝、积水涟漪和屋檐流水；统一南向北观察方向；招牌文字后期添加。
    update_asset(
        conn,
        ASSET_LANE,
        prompt=(
            "雨夜中的青石巷，雨丝密集斜落，屋檐持续流水成线，青石板路面湿滑，"
            "积水表面有细密涟漪，倒映暖黄色路灯。窄巷两侧是老旧砖木结构居民楼，"
            "一侧有店面，木门挂着生锈锁，玻璃橱窗蒙灰，招牌为空白牌面（文字后期添加）。"
            "整条巷子无人，只有街灯和雨丝。"
        ),
        composition=(
            "统一由南向北观察方向，从巷口向内延伸的三点透视，两侧墙面逐渐汇聚于远处，"
            "路灯在画面中上位置，水面反射光斑，构图留出中央为主要通道。"
        ),
    )


# ---------------------------------------------------------------------------
# 新增素材
# ---------------------------------------------------------------------------


def insert_new_assets(conn: sqlite3.Connection) -> None:
    insert_asset(
        conn,
        SCENE_CONVENIENCE,
        asset_type="scene",
        name="地铁口便利店雨夜",
        profile={
            "concept": "故事开场场景：地铁口旁的便利店屋檐下，两人初次相遇",
            "place_type": "地铁口/便利店门口",
            "worldview": "现代中国小城，雨夜",
            "space_keywords": ["地铁口", "便利店屋檐", "雨幕", "昏黄灯光"],
            "layout": "画面右侧为便利店屋檐，屋檐下可避雨；左侧为地铁口台阶与雨幕",
            "architecture": "老城街道旁的小型便利店，门头有招牌（空白牌面），屋檐向外伸出",
            "regions": ["便利店屋檐下（避雨区）", "地铁口台阶", "街道雨幕"],
            "entrance_position": "便利店门位于屋檐下右侧",
            "furnishings": ["便利店橱窗", "门头招牌（无文字）", "屋檐排水槽"],
            "lighting": "便利店招牌与橱窗透出暖黄灯光，雨幕冷蓝",
            "main_light": "便利店橱窗暖黄灯光",
            "fill_light": "街道冷蓝环境光",
            "main_view": "从街道望向便利店屋檐，人物避雨位置在画面右侧",
            "standard_view_1": "从地铁口望向便利店屋檐",
            "standard_view_2": "从屋檐下望向街道雨幕",
            "detail_zone": ["屋檐滴水", "橱窗暖光", "积水倒影"],
            "color_mood": "暖黄与冷蓝对比",
            "main_color": "暖黄、深灰",
            "accent_color": "冷蓝雨幕",
            "weather": "大雨",
            "time_of_day": "傍晚",
            "indoor_outdoor": "室外",
            "shape_scale": "街道宽度约6米，屋檐进深约2米",
        },
        prompt=(
            "雨夜中的地铁口街道，右侧是便利店屋檐，屋檐下可以避雨，门头有空白招牌（无文字）；"
            "左侧是地铁口台阶和密集雨幕，路面有积水倒映暖黄灯光。整条街道无人，只有雨丝和灯光。"
        ),
        negative_prompt="不要人物、不要人影、不要剪影、不要行人、不要晴天、不要明亮光线、不要文字招牌",
        consistency_keywords=["便利店屋檐", "地铁口台阶", "暖黄灯光", "雨幕", "积水倒影"],
        composition=(
            "平视全景，便利店屋檐占据画面右侧，地铁口与雨幕在左侧，暖黄灯光与冷蓝雨幕对比，"
            "构图留出屋檐下避雨区。"
        ),
    )

    insert_asset(
        conn,
        PROP_OLD_PHOTO,
        asset_type="prop",
        name="旧合影照片",
        profile={
            "concept": "林舟随身携带的旧照片：七岁的他与父亲在钟表店门口的合影",
            "category": "照片/回忆物件",
            "owner": "林舟",
            "plot_function": "连接父子回忆，指向林记钟表店地址与银色怀表",
            "description": (
                "一张泛黄、边缘卷曲的老照片，照片中约七岁的男孩站在钟表店门口，"
                "身后是挂有'林记钟表'招牌（照片内文字模糊）的店面，父亲蹲在男孩身后，"
                "手里拿着一块银色怀表。照片表面有折痕和轻微褪色。"
            ),
            "silhouette": "长方形照片，横版构图",
            "dimensions": "约10cm×7cm，可单手握住",
            "material": "相纸，泛黄做旧",
            "color": "泛黄、褪色",
            "surface_texture": "折痕、卷边、褪色斑点",
            "wear": "边缘卷曲，中间有横折痕",
            "scale_reference": "与成年男性手掌相当",
            "detail_zone": ["男孩与父亲的合影", "钟表店门头", "父亲手中的银色怀表"],
        },
        prompt=(
            "一张泛黄的老照片，横版，照片中约七岁的男孩站在老钟表店门口，"
            "父亲蹲在男孩身后、手里拿着一块银色怀表，门头招牌文字模糊不可读。"
            "照片有折痕、卷边和褪色痕迹，背景为中性纯色。"
        ),
        negative_prompt="不要人物面部变形、不要现代服饰、不要清晰可读的文字、不要彩色照片",
        consistency_keywords=["泛黄老照片", "七岁男孩与父亲", "钟表店门头", "父亲手持怀表"],
        composition=("照片整体平铺展示，略俯视，完整呈现整张合影，背景为单一中性纯色。"),
    )


# ---------------------------------------------------------------------------
# 分镜修复
# ---------------------------------------------------------------------------


def _binding(role: str, asset_id: str, asset_image_id: str | None = None) -> dict:
    item = {"role": role, "asset_id": asset_id}
    if asset_image_id:
        item["asset_image_id"] = asset_image_id
    return item


def patch_shots(conn: sqlite3.Connection) -> None:
    # ---- 第 1-3 镜：场景改为「地铁口便利店雨夜」，照片可见镜绑定旧合影照片 ----
    for no, scene_id in ((1, SCENE_CONVENIENCE), (2, SCENE_CONVENIENCE), (3, SCENE_CONVENIENCE)):
        shot_id = shot_id_by_no(conn, no)
        current = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_id,))[0]
        character_ids = load_json(current["character_ids_json"], [])
        prop_ids = load_json(current["prop_ids_json"], [])
        if no in (1, 3) and PROP_OLD_PHOTO not in prop_ids:
            prop_ids.append(PROP_OLD_PHOTO)
        bindings = shot_bindings(conn, scene_id, character_ids, prop_ids, extra=[])
        # 旧合影照片无图时保留 binding 但不带 asset_image_id
        update_shot(
            conn, shot_id, scene_asset_id=scene_id, prop_ids=prop_ids, reference_bindings=bindings
        )
        print(f"[分镜 #{no}] 场景 -> 地铁口便利店雨夜；props={prop_ids}")

    # ---- 第 5 镜：绑定旧合影照片 ----
    shot_5 = shot_id_by_no(conn, 5)
    current5 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_5,))[0]
    prop_ids5 = load_json(current5["prop_ids_json"], [])
    if PROP_OLD_PHOTO not in prop_ids5:
        prop_ids5.append(PROP_OLD_PHOTO)
    bindings5 = shot_bindings(
        conn,
        current5["scene_asset_id"],
        load_json(current5["character_ids_json"], []),
        prop_ids5,
    )
    update_shot(conn, shot_5, prop_ids=prop_ids5, reference_bindings=bindings5)
    print("[分镜 #5] 绑定旧合影照片")

    # ---- 第 8 镜：补苏晴（苏晴在画面中旁观林舟找工具） ----
    shot_8 = shot_id_by_no(conn, 8)
    cur8 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_8,))[0]
    chars8 = load_json(cur8["character_ids_json"], [])
    if ASSET_SU_QING not in chars8:
        chars8.append(ASSET_SU_QING)
    bindings8 = shot_bindings(
        conn, cur8["scene_asset_id"], chars8, load_json(cur8["prop_ids_json"], [])
    )
    update_shot(
        conn,
        shot_8,
        character_ids=chars8,
        reference_bindings=bindings8,
        frame_description="中近景，林舟侧身低头寻找砸锁工具，苏晴撑伞站在一旁，门锁在画面一侧。",
    )
    print("[分镜 #8] 补苏晴")

    # ---- 第 9 镜：补林舟（start_state 明确林舟在场） ----
    shot_9 = shot_id_by_no(conn, 9)
    cur9 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_9,))[0]
    chars9 = load_json(cur9["character_ids_json"], [])
    if ASSET_LIN_ZHOU not in chars9:
        chars9.append(ASSET_LIN_ZHOU)
    bindings9 = shot_bindings(
        conn, cur9["scene_asset_id"], chars9, load_json(cur9["prop_ids_json"], [])
    )
    update_shot(conn, shot_9, character_ids=chars9, reference_bindings=bindings9)
    print("[分镜 #9] 补林舟")

    # ---- 第 14、15 镜：补苏晴（15 镜 start_state 明确苏晴在场） ----
    for no in (14, 15):
        shot_id = shot_id_by_no(conn, no)
        cur = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_id,))[0]
        chars = load_json(cur["character_ids_json"], [])
        if ASSET_SU_QING not in chars:
            chars.append(ASSET_SU_QING)
        bindings = shot_bindings(
            conn, cur["scene_asset_id"], chars, load_json(cur["prop_ids_json"], [])
        )
        update_shot(conn, shot_id, character_ids=chars, reference_bindings=bindings)
        print(f"[分镜 #{no}] 补苏晴")

    # ---- 第 22、25、29、33、40 镜：补林舟 ----
    for no in (22, 25, 29, 33, 40):
        shot_id = shot_id_by_no(conn, no)
        cur = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_id,))[0]
        chars = load_json(cur["character_ids_json"], [])
        if ASSET_LIN_ZHOU not in chars:
            chars.append(ASSET_LIN_ZHOU)
        bindings = shot_bindings(
            conn, cur["scene_asset_id"], chars, load_json(cur["prop_ids_json"], [])
        )
        update_shot(conn, shot_id, character_ids=chars, reference_bindings=bindings)
        print(f"[分镜 #{no}] 补林舟")

    # ---- 第 35 镜：正向看向林舟、负向却禁林舟 -> 补林舟并修正负向 ----
    shot_35 = shot_id_by_no(conn, 35)
    cur35 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_35,))[0]
    chars35 = load_json(cur35["character_ids_json"], [])
    if ASSET_LIN_ZHOU not in chars35:
        chars35.append(ASSET_LIN_ZHOU)
    bindings35 = shot_bindings(
        conn, cur35["scene_asset_id"], chars35, load_json(cur35["prop_ids_json"], [])
    )
    negative35 = cur35["negative_prompt"].replace("不要出现林舟", "不要出现第三个人")
    update_shot(
        conn,
        shot_35,
        character_ids=chars35,
        reference_bindings=bindings35,
        negative_prompt=negative35,
    )
    print("[分镜 #35] 补林舟并修正负向")

    # ---- 第 2 镜：绑定第 1 镜关键帧作为 previous_keyframe ----
    keyframe_1 = fetch_all(
        conn,
        "SELECT id FROM keyframe_version WHERE shot_id=? AND is_current=1 AND status='completed'",
        (shot_id_by_no(conn, 1),),
    )
    if keyframe_1:
        shot_2 = shot_id_by_no(conn, 2)
        cur2 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_2,))[0]
        bindings2 = load_json(cur2["reference_bindings_json"], [])
        if not any(b.get("role") == "previous_keyframe" for b in bindings2):
            bindings2.append({"role": "previous_keyframe", "keyframe_id": keyframe_1[0]["id"]})
            update_shot(conn, shot_2, reference_bindings=bindings2)
            print("[分镜 #2] 绑定第1镜关键帧 previous_keyframe")

    # ---- 第 30 镜：怀表放口袋（保留，原文行为）；帧描述统一服装术语 ----
    shot_30 = shot_id_by_no(conn, 30)
    cur30 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_30,))[0]
    update_shot(
        conn,
        shot_30,
        frame_description="近景：林舟的手将银色怀表放入灰色风衣内袋，另一只手合上笔记本。",
        image_prompt=cur30["image_prompt"].replace("深色夹克", "灰色中长款防水风衣"),
        video_prompt=cur30["video_prompt"].replace("夹克内袋", "风衣内袋"),
    )

    # ---- 第 38 镜：怀表留在柜台、暂时不修（原文结尾意象），不再放回口袋 ----
    shot_38 = shot_id_by_no(conn, 38)
    cur38 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_38,))[0]
    update_shot(
        conn,
        shot_38,
        start_state=(
            "林舟修表间隙从口袋取出银色怀表，暂放在柜台上；他站在昏黄灯下，手里拿一块布。"
        ),
        action="林舟看见柜台上的银色怀表，伸手拿布擦了擦表壳，但没有打开它，轻轻留在柜台上，没有急着修。",
        end_state="银色怀表安静地躺在柜台上，指针仍旧停着；林舟转身继续修表。",
        next_hook="林舟继续调整另一只钟；苏晴在角落抬头看他。",
        frame_description="特写：柜台上的银色怀表与林舟的手，布轻轻擦过表壳，表盖没有被打开。",
        video_prompt=(
            "0-1秒：林舟从口袋取出怀表放在柜台上，低头看着它；1-3秒：他拿起布，沿着表壳轻轻擦拭一圈，动作很慢；"
            "3-4秒：他的拇指在表盖上停一下，但没有打开；4-6秒：他将怀表轻轻留在柜台上，转身走向另一只挂钟。"
            "结束于怀表安静躺在柜台上。"
        ),
        image_prompt=cur38["image_prompt"].replace("夹克内袋", "柜台台面"),
    )

    # ---- 第 1/2/3 镜：统一"深色夹克"为"灰色中长款防水风衣" ----
    for no in (1, 2, 3):
        shot_id = shot_id_by_no(conn, no)
        cur = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_id,))[0]
        changed = False
        updates: dict = {}
        for field in ("image_prompt", "video_prompt", "negative_prompt"):
            if "深色夹克" in cur[field]:
                updates[field] = cur[field].replace("深色夹克", "灰色中长款防水风衣")
                changed = True
        if changed:
            update_shot(conn, shot_id, **updates)
            print(f"[分镜 #{no}] 服装术语统一为灰色风衣")

    # ---- 第 1 镜：关键帧改为动作起点状态（刚从地铁口跑出、照片未取出），
    #      与 video_prompt 时间轴（跑入屋檐->停下->掏照片->看照片）一致 ----
    shot_1 = shot_id_by_no(conn, 1)
    cur1 = fetch_all(conn, "SELECT * FROM shot WHERE id=?", (shot_1,))[0]
    old_image_1 = cur1["image_prompt"]
    new_image_1 = (
        "傍晚大雨中的老城街道，画面右侧是地铁口台阶，林舟刚从地铁口跑出、停在雨中，"
        "灰色中长款防水风衣半湿，右手还护着胸前口袋里的旧照片（尚未取出）；"
        "画面左侧是便利店屋檐，昏黄灯光透出，雨幕密集，街道积水倒映灯光，"
        "林舟居右，构图左侧留白指向屋檐，平视中景。"
    )
    if "刚从地铁口跑出、停在雨中" not in old_image_1:
        update_shot(conn, shot_1, image_prompt=new_image_1)
        print("[分镜 #1] 关键帧改为动作起点状态（照片未取出）")


def reorder_shots_31_36(conn: sqlite3.Connection) -> None:
    """31-36 镜因果倒置重排。

    原顺序：31 修钟、32 打扫、33 问怎么处理、34 答先打扫、35 问然后呢、36 答修好。
    正确叙事应为：先对白决定（问处理方式 -> 先打扫 -> 然后呢 -> 修好），
    再执行（林舟修钟、苏晴打扫）。因此重排为：
      31 = 原33（苏晴问"这些东西怎么处理"）
      32 = 原34（林舟答"先把店打扫干净"）
      33 = 原35（苏晴追问"然后呢"）
      34 = 原36（林舟答"把它们一只一只修好"）
      35 = 原31（林舟取下挂钟后盖修钟）
      36 = 原32（苏晴扎发擦柜台）
    同时改写 31-34 的 start_state/end_state/frame 等使因果衔接自然。
    """
    rows = fetch_all(
        conn,
        "SELECT id, shot_no FROM shot WHERE project_id=? AND shot_no BETWEEN 31 AND 36 "
        "ORDER BY shot_order",
        (PROJECT_ID,),
    )
    by_no = {row["shot_no"]: row["id"] for row in rows}
    id31, id32, id33, id34, id35, id36 = (
        by_no[31],
        by_no[32],
        by_no[33],
        by_no[34],
        by_no[35],
        by_no[36],
    )
    shop = ASSET_SHOP
    linzhou = ASSET_LIN_ZHOU
    suqing = ASSET_SU_QING

    base_bindings = [
        _binding("scene_layout", shop, current_asset_image(conn, shop)),
        _binding("character_identity", linzhou, current_asset_image(conn, linzhou)),
        _binding("character_identity", suqing, current_asset_image(conn, suqing)),
    ]

    # 新31（原33）：苏晴问"这些东西，你准备怎么处理？"
    update_shot(
        conn,
        id33,
        start_state="林舟合上笔记本，苏晴站在柜台边，看着他。",
        action="苏晴轻声问林舟：这些东西，你准备怎么处理？",
        end_state="苏晴站在原地，等林舟回答。",
        next_hook="林舟回答：先把店打扫干净。",
        frame_description="苏晴站在柜台边，侧身看向林舟；背景是满屋子挂钟。",
        positions=[
            {
                "character": "苏晴",
                "position": "画面中央柜台一侧",
                "facing": "面向林舟方向",
                "gaze": "林舟",
            }
        ],
        dialogue=[{"speaker": "苏晴", "text": "这些东西，你准备怎么处理？"}],
        image_prompt=(
            "老钟表店内，苏晴站在柜台边，浅色风衣，长发披肩，侧过脸看向林舟，"
            "神情平和带着询问；背景是墙上停摆的挂钟。中近景，平视。"
        ),
        video_prompt=(
            "0-1秒：苏晴看向林舟；1-3秒：她问'这些东西，你准备怎么处理？'；"
            "3-4秒：她安静等待回答。结束于她看着林舟的侧脸。"
        ),
        negative_prompt=(
            "不要出现第三个人，不要改变苏晴服装和发型，不要改变林舟灰色风衣，"
            "不要出现字幕，手部动作不僵硬。"
        ),
        reference_bindings=base_bindings,
    )
    # 新32（原34）：林舟答"先把店打扫干净"
    update_shot(
        conn,
        id34,
        start_state="苏晴问完，林舟抬起头看向她。",
        action="林舟回答：先把店打扫干净。",
        end_state="林舟说完，目光扫过满屋的挂钟。",
        next_hook="苏晴追问：然后呢？",
        frame_description="林舟侧脸特写，神情平静，说出决定。",
        positions=[
            {"character": "林舟", "position": "画面中央", "facing": "面向苏晴方向", "gaze": "苏晴"}
        ],
        dialogue=[{"speaker": "林舟", "text": "先把店打扫干净。"}],
        image_prompt=(
            "老钟表店内，林舟站在柜台边，灰色中长款防水风衣，偏头回答，神情平静；"
            "背景是墙上停摆的挂钟。近景，平视。"
        ),
        video_prompt=(
            "0-1秒：林舟抬起头；1-2秒：他说'先把店打扫干净。'；"
            "2-3秒：他说完目光扫过挂钟。结束于他平静的侧脸。"
        ),
        negative_prompt=(
            "不要出现苏晴入画，不要改变林舟灰色风衣，不要出现字幕，"
            "不要出现现代工具，动作连贯，表情不僵硬。"
        ),
        reference_bindings=base_bindings,
    )
    # 新33（原35）：苏晴追问"然后呢？"
    update_shot(
        conn,
        id35,
        start_state="苏晴听了林舟的回答，站在原地继续追问。",
        action="苏晴简短地追问：然后呢？",
        end_state="苏晴仍在等待林舟的下一句。",
        next_hook="林舟望向满墙的挂钟，说出决定。",
        frame_description="苏晴的中近景，她一手自然垂在身侧，眼神看向林舟方向。",
        positions=[
            {
                "character": "苏晴",
                "position": "画面中央柜台一侧",
                "facing": "面向林舟方向",
                "gaze": "林舟",
            }
        ],
        dialogue=[{"speaker": "苏晴", "text": "然后呢？"}],
        image_prompt=(
            "老钟表店内，苏晴站在柜台边，微微歪头看向林舟，眼神带着询问；"
            "背景里挂钟和灯光虚化。近景，平视。"
        ),
        video_prompt=(
            "0-1秒：苏晴轻轻追问'然后呢？'；1-2秒：她看着林舟，嘴微微抿住，等待回答。"
            "结束于她期待的表情。"
        ),
        negative_prompt=(
            "不要出现第三个人，不要改变苏晴服装和发型，不要出现字幕，"
            "不要有多余动作，表情自然。"
        ),
        reference_bindings=base_bindings,
    )
    # 新34（原36）：林舟答"然后，把它们一只一只修好"
    update_shot(
        conn,
        id36,
        start_state="苏晴追问后，林舟望向满墙停摆的挂钟。",
        action="林舟停下手，抬头望向满墙停摆的挂钟，说出决定：把它们一只一只修好。",
        end_state="林舟的视线扫过整墙挂钟，语气笃定。",
        next_hook="林舟走向挂钟，取下后盖开始修钟。",
        frame_description="林舟站在挂钟前，抬眼看着整面墙，神情沉稳；画面中有多只停摆的挂钟。",
        positions=[
            {
                "character": "林舟",
                "position": "画面中央挂钟前",
                "facing": "面向墙上挂钟",
                "gaze": "满屋停摆的钟",
            }
        ],
        dialogue=[{"speaker": "林舟", "text": "然后，把它们一只一只修好。"}],
        image_prompt=(
            "老钟表店内，林舟站在挂钟墙前，灰色中长款防水风衣，抬起眼睛望向面前"
            "一个接一个停摆的挂钟，神情认真而坚定；昏黄灯光照亮他侧脸。中近景，平视。"
        ),
        video_prompt=(
            "0-1秒：林舟呼吸放缓；1-3秒：他说'然后，把它们一只一只修好。'；"
            "3-5秒：他抬头缓缓扫过墙上挂钟，目光坚定，环境安静。结束于他看向钟墙的侧影。"
        ),
        negative_prompt=(
            "不要出现苏晴，不要改变林舟灰色风衣，不要出现字幕，"
            "不要出现挂钟走动的噪音，不要闪屏，表情不要僵硬。"
        ),
        reference_bindings=base_bindings,
    )
    # 新35（原31）：林舟修钟（取下后盖调齿轮）
    update_shot(
        conn,
        id31,
        start_state="林舟说完决定，走向最近的一只挂钟，手里拿着螺丝刀。",
        action="林舟取下挂钟后盖，用螺丝刀试着调整齿轮，让钟重新走动。",
        end_state="林舟的螺丝刀抵住齿轮，挂钟外壳已打开。",
        next_hook="苏晴摘下风衣腰带，扎起长发，开始帮忙打扫。",
        frame_description="林舟站在墙边挂钟前，钟盖打开，他低头用螺丝刀探进机芯。",
        positions=[
            {
                "character": "林舟",
                "position": "画面中央挂钟前",
                "facing": "面向墙上挂钟",
                "gaze": "挂钟机芯",
            }
        ],
        dialogue=[],
        image_prompt=(
            "老钟表店内，墙上一只挂钟被打开后盖，露出复杂的黄铜齿轮；林舟一手扶住钟壳，"
            "一手握着细长螺丝刀，正小心伸向齿轮；他的表情专注，暖黄灯光照亮局部。中近景，平视。"
        ),
        video_prompt=(
            "0-1秒：林舟抬手打开挂钟的后盖，轻轻放在旁边；1-3秒：他用螺丝刀伸进机芯，"
            "试探性地拨动齿轮，发出细小的金属声；3-5秒：他专注盯着齿轮，动作放慢，调整角度。"
            "结束于螺丝刀接触齿轮的瞬间。"
        ),
        negative_prompt=(
            "不要出现苏晴入画，不要改变挂钟样式，不要出现现代电子工具，"
            "不要改变林舟灰色风衣，不要抖动，手部动作不僵硬。"
        ),
        reference_bindings=[
            _binding("scene_layout", shop, current_asset_image(conn, shop)),
            _binding("character_identity", linzhou, current_asset_image(conn, linzhou)),
        ],
    )
    # 新36（原32）：苏晴扎发擦柜台
    update_shot(
        conn,
        id32,
        start_state="林舟开始修钟，苏晴摘下风衣腰带，站在柜台边。",
        action="苏晴摘下风衣腰带，把长发扎起，然后拿起抹布擦拭柜台上的灰尘。",
        end_state="苏晴的头发被腰带束起，抹布擦过柜台，扬起细微灰尘。",
        next_hook="半小时后，店里第一只挂钟被修好。",
        frame_description="苏晴侧面中景，她把腰带当发绳束起长发，然后俯身用抹布擦柜台。",
        positions=[
            {
                "character": "苏晴",
                "position": "画面中央柜台一侧",
                "facing": "面向柜台台面",
                "gaze": "抹布擦拭区域",
            }
        ],
        dialogue=[],
        image_prompt=(
            "老钟表店内，苏晴站在柜台边，浅色风衣敞开，她两手用风衣腰带把长发拢起束成马尾；"
            "柜台上放着一块灰布，浮尘在昏黄光线下飘动。中近景，平视。"
        ),
        video_prompt=(
            "0-1秒：苏晴解下风衣腰带，长发散落又拢起；1-3秒：她将腰带绕两圈把长发扎高，动作利落；"
            "3-5秒：她拿起抹布，从柜台一端擦到另一端，灰尘在光里浮起。结束于她低头擦柜台。"
        ),
        negative_prompt=(
            "不要出现林舟入画抢焦，不要改变苏晴服装，不要出现清洁剂/水桶等现代物品，"
            "不要出现头发散乱或服装变形，画面稳定。"
        ),
        reference_bindings=[
            _binding("scene_layout", shop, current_asset_image(conn, shop)),
            _binding("character_identity", suqing, current_asset_image(conn, suqing)),
        ],
    )
    # 重排 shot_order：31=原33、32=原34、33=原35、34=原36、35=原31、36=原32
    ordered = [id33, id34, id35, id36, id31, id32]
    temporary_start = 100000
    for offset, shot_id in enumerate(ordered):
        conn.execute(
            "UPDATE shot SET shot_no=?, shot_order=? WHERE id=?",
            (temporary_start + offset, temporary_start + offset, shot_id),
        )
    for index, shot_id in enumerate(ordered, start=31):
        conn.execute(
            "UPDATE shot SET shot_no=?, shot_order=?, updated_at=? WHERE id=?",
            (index, index, utc_now(), shot_id),
        )
    print("[分镜 31-36] 因果顺序重排完成：对白在前（31-34），行动在后（35-36）")


def sync_source_signatures(conn: sqlite3.Connection) -> None:
    """苏晴正史定为长发：正文开场描述统一为长发，同步三处 source_signature。"""
    project_row = fetch_all(conn, "SELECT * FROM project WHERE id=?", (PROJECT_ID,))
    if not project_row:
        raise RuntimeError("项目不存在")
    source_text = project_row[0]["source_text"]
    if "短发" in source_text:
        updated = source_text.replace("短发被雨水打湿", "长发被雨水打湿")
        new_sig = source_signature(updated)
        conn.execute(
            "UPDATE project SET source_text=?, source_signature=?, updated_at=? WHERE id=?",
            (updated, new_sig, utc_now(), PROJECT_ID),
        )
        conn.execute(
            "UPDATE novel_analysis SET source_signature=?, updated_at=? WHERE project_id=?",
            (new_sig, utc_now(), PROJECT_ID),
        )
        conn.execute(
            "UPDATE screenplay SET source_signature=?, updated_at=? WHERE project_id=?",
            (new_sig, utc_now(), PROJECT_ID),
        )
        conn.execute(
            "UPDATE asset SET source_signature=?, updated_at=? WHERE project_id=?",
            (new_sig, utc_now(), PROJECT_ID),
        )
        # shot.source_signature 的语义是 screenplay.own_signature（见 shot_service
        # _current_source_signature），剧本内容未变则无需同步，避免误标 stale。
        print("[正文] '短发被雨水打湿' -> '长发被雨水打湿'，source_signature 已同步")


def main() -> None:
    backup_database()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN")
        sync_source_signatures(conn)
        patch_assets(conn)
        insert_new_assets(conn)
        patch_shots(conn)
        reorder_shots_31_36(conn)
        # 回填新增素材的 source_signature（只限本次新增的两条）
        project_row = fetch_all(
            conn, "SELECT source_signature FROM project WHERE id=?", (PROJECT_ID,)
        )
        sig = project_row[0]["source_signature"]
        conn.execute(
            "UPDATE asset SET source_signature=? WHERE id IN (?, ?)",
            (sig, SCENE_CONVENIENCE, PROP_OLD_PHOTO),
        )
        conn.execute("COMMIT")
        print("\n[完成] 迁移成功。旧媒体版本将通过 input_signature 自动标记 is_stale，文件保留。")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

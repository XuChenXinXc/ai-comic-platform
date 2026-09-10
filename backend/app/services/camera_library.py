# ruff: noqa: E501  （本文件是提示词数据，长字符串为原文模板）
"""运镜词库：AI 漫剧运镜提示词 50 条的结构化库与运镜决策诊断。

数据来源：用户提供的《即梦 4.5 AI 漫剧运镜提示词 50 条》Excel。
设计原则：
- 定镜是默认、运镜是特例：运镜必须自证动机（命中触发表或导演动机）。
- 漏加（该运镜却固定）只给建议，绝不静默修改分镜数据。
- 每条存双模板：template_core 与 template_jimeng 均只保留纯机位/构图/动作描述，
  已剥离品牌词、画风词与摄影越权词；template_jimeng 保留双模板结构，供接入
  即梦类模型时按模型侧参数叠加使用。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 运镜策略档位
POLICY_CONSERVATIVE = "克制"
POLICY_STANDARD = "标准"
POLICY_RICH = "丰富"
SUPPORTED_POLICIES = (POLICY_CONSERVATIVE, POLICY_STANDARD, POLICY_RICH)
DEFAULT_CAMERA_POLICY = POLICY_CONSERVATIVE

# 固定机位标记：camera_move 命中即视为定镜
_FIXED_MARKERS = ("固定", "不动", "静止", "无运镜", "定镜")

# 反模式场景：这些叙事场景优先定镜（对白为主的半身双人镜、情绪特写、静坐凝望等）
_STATIC_PREFERRED_MARKERS = (
    "对谈",
    "交谈",
    "聊天",
    "寒暄",
    "静坐",
    "凝望",
    "发呆",
    "等待",
    "沉默",
)

# 导演运镜动机文本中出现这些词视为「有动机」
_MOTIVATION_MARKERS = ("推", "拉", "摇", "跟", "甩", "俯冲", "环绕", "移", "扫")

_PAREN_RE = re.compile(r"[（(].*?[)）]")


@dataclass(frozen=True)
class CameraPreset:
    """单条运镜预设：名称、双模板、适配场景、触发表与目标端。"""

    id: str
    category: str  # static / dynamic / pov / transition
    category_label: str
    name: str
    template_core: str
    template_jimeng: str
    fit_scenes: str
    target: str  # image / video / both / transition
    is_static: bool
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CameraSuggestion:
    """运镜决策诊断结果（只读，不改分镜数据）。"""

    code: str
    severity: str  # warning / suggestion
    message: str
    recommended_camera_move: str = ""
    details: Mapping[str, Any] | None = None


def _p(
    id: str,
    category: str,
    label: str,
    name: str,
    core: str,
    jimeng: str,
    fit: str,
    target: str,
    *,
    static: bool = False,
    triggers: tuple[str, ...] = (),
) -> CameraPreset:
    return CameraPreset(
        id=id,
        category=category,
        category_label=label,
        name=name,
        template_core=core,
        template_jimeng=jimeng,
        fit_scenes=fit,
        target=target,
        is_static=static,
        triggers=triggers,
    )


# ---------------------------------------------------------------------------
# 基础固定类运镜 12 种：静态构图语言，目标端为分镜图（image）
# ---------------------------------------------------------------------------

_STATIC_PRESETS = (
    _p(
        "static-1",
        "static",
        "基础固定类",
        "全景定镜",
        "全景定镜，画面包含 [教室全景 / 古风庭院]，主体居中，人物与环境比例协调，无画面畸变",
        "全景定镜，画面包含 [教室全景 / 古风庭院]，主体居中，人物与环境比例协调，无画面畸变",
        "开场场景交代",
        "image",
        static=True,
    ),
    _p(
        "static-2",
        "static",
        "基础固定类",
        "人物半身定镜",
        "中景半身定镜，聚焦 [主角坐立姿态]，画面居中对称，人物与背景层次分明，主体突出",
        "中景半身定镜，聚焦 [主角坐立姿态]，画面居中对称，人物与背景层次分明，主体突出",
        "角色对话",
        "image",
        static=True,
    ),
    _p(
        "static-3",
        "static",
        "基础固定类",
        "道具特写定镜",
        "特写定镜，聚焦关键道具 [主角手中的旧信件 / 魔法宝石]，主体突出，背景简洁不抢镜",
        "特写定镜，聚焦关键道具 [主角手中的旧信件 / 魔法宝石]，主体突出，背景简洁不抢镜",
        "关键道具展示",
        "image",
        static=True,
    ),
    _p(
        "static-4",
        "static",
        "基础固定类",
        "群像定镜",
        "群像全景定镜，画面容纳 [5 人以上角色站位]，人物比例协调，前后景层次分明，站位主次清晰",
        "群像全景定镜，画面容纳 [5 人以上角色站位]，人物比例协调，前后景层次分明，站位主次清晰",
        "团队登场",
        "image",
        static=True,
    ),
    _p(
        "static-5",
        "static",
        "基础固定类",
        "侧脸特写定镜",
        "侧脸特写定镜，捕捉 [角色低头微笑的侧脸]，侧脸轮廓完整入画，面部居于画面一侧，留白均衡",
        "侧脸特写定镜，捕捉 [角色低头微笑的侧脸]，侧脸轮廓完整入画，面部居于画面一侧，留白均衡",
        "人物情绪特写",
        "image",
        static=True,
    ),
    _p(
        "static-6",
        "static",
        "基础固定类",
        "风景定镜",
        "远景风景定镜，展现 [夕阳下的海边 / 雪后的山顶]，地平线平直，天地比例均衡，画面静谧无动态",
        "远景风景定镜，展现 [夕阳下的海边 / 雪后的山顶]，地平线平直，天地比例均衡，画面静谧无动态",
        "场景过渡",
        "image",
        static=True,
    ),
    _p(
        "static-7",
        "static",
        "基础固定类",
        "手部动作定镜",
        "特写定镜，聚焦 [角色握笔写字 / 擦拭伤口的手部]，动作定格在关键帧，手部居于画面中央，主体突出",
        "特写定镜，聚焦 [角色握笔写字 / 擦拭伤口的手部]，动作定格在关键帧，手部居于画面中央，主体突出",
        "动作细节强调",
        "image",
        static=True,
    ),
    _p(
        "static-8",
        "static",
        "基础固定类",
        "眼神特写定镜",
        "超特写定镜，放大 [角色含泪的眼睛]，眼睛居于画面中央，背景纯黑，视线方向明确",
        "超特写定镜，放大 [角色含泪的眼睛]，眼睛居于画面中央，背景纯黑，视线方向明确",
        "人物内心戏",
        "image",
        static=True,
    ),
    _p(
        "static-9",
        "static",
        "基础固定类",
        "俯视桌面定镜",
        "45° 俯视定镜，拍摄 [摆满书本的桌面 / 棋盘布局]，物品摆放整齐，桌面居于画面中央，视角无倾斜",
        "45° 俯视定镜，拍摄 [摆满书本的桌面 / 棋盘布局]，物品摆放整齐，桌面居于画面中央，视角无倾斜",
        "桌面互动场景",
        "image",
        static=True,
    ),
    _p(
        "static-10",
        "static",
        "基础固定类",
        "仰视建筑定镜",
        "低角度仰视定镜，拍摄 [高耸的城堡 / 摩天大楼]，建筑笔直无变形，天空占画面 1/3，主体居中",
        "低角度仰视定镜，拍摄 [高耸的城堡 / 摩天大楼]，建筑笔直无变形，天空占画面 1/3，主体居中",
        "建筑场景展示",
        "image",
        static=True,
    ),
    _p(
        "static-11",
        "static",
        "基础固定类",
        "局部细节定镜",
        "特写定镜，聚焦 [角色衣服的刺绣 / 机械零件的齿轮]，局部居于画面中央，主体突出，背景简洁",
        "特写定镜，聚焦 [角色衣服的刺绣 / 机械零件的齿轮]，局部居于画面中央，主体突出，背景简洁",
        "细节伏笔",
        "image",
        static=True,
    ),
    _p(
        "static-12",
        "static",
        "基础固定类",
        "对称构图定镜",
        "对称式全景定镜，画面左右对称（左：门 / 右：窗），人物站在中心，中轴线明确",
        "对称式全景定镜，画面左右对称（左：门 / 右：窗），人物站在中心，中轴线明确",
        "仪式感场景",
        "image",
        static=True,
    ),
)

# ---------------------------------------------------------------------------
# 动态运动类运镜 16 种：视频运镜语言，目标端为视频（video）
# ---------------------------------------------------------------------------

_DYNAMIC_PRESETS = (
    _p(
        "dynamic-1",
        "dynamic",
        "动态运动类",
        "缓慢推镜",
        "缓慢推镜，从全景逐步聚焦到 [主角坚定的面部]，推进匀速，终点落幅居中",
        "缓慢推镜，从全景逐步聚焦到 [主角坚定的面部]，推进匀速，终点落幅居中",
        "人物决心表态",
        "video",
        triggers=("决心", "坚定", "发誓", "表态", "决意", "下定"),
    ),
    _p(
        "dynamic-2",
        "dynamic",
        "动态运动类",
        "快速推镜",
        "快速推镜，从 [房间全景] 瞬间推至 [桌上的警告信]，推进快速果断，终点落幅明确",
        "快速推镜，从 [房间全景] 瞬间推至 [桌上的警告信]，推进快速果断，终点落幅明确",
        "关键线索揭露",
        "video",
        triggers=("线索", "揭露", "发现", "察觉", "揭示", "真相"),
    ),
    _p(
        "dynamic-3",
        "dynamic",
        "动态运动类",
        "渐进式推镜",
        "渐进加速推镜，从慢到快聚焦 [奔跑的角色背影]，加速节奏由缓到急，主体始终居中",
        "渐进加速推镜，从慢到快聚焦 [奔跑的角色背影]，加速节奏由缓到急，主体始终居中",
        "紧张追逐",
        "video",
        triggers=("追逐", "追击", "追赶", "冲刺", "狂奔"),
    ),
    _p(
        "dynamic-4",
        "dynamic",
        "动态运动类",
        "缓慢拉镜",
        "缓慢拉镜，从 [主角特写] 拉至 [被废墟包围的城市全景]，拉镜匀速，画面边缘无拉伸变形",
        "缓慢拉镜，从 [主角特写] 拉至 [被废墟包围的城市全景]，拉镜匀速，画面边缘无拉伸变形",
        "场景规模展现",
        "video",
        triggers=("规模", "广阔", "全景展现", "孤身", "渺小", "废墟"),
    ),
    _p(
        "dynamic-5",
        "dynamic",
        "动态运动类",
        "快速拉镜",
        "快速拉镜，从 [拳头碰撞特写] 拉至 [两人对战的战场全景]，拉镜果断，全景构图完整",
        "快速拉镜，从 [拳头碰撞特写] 拉至 [两人对战的战场全景]，拉镜果断，全景构图完整",
        "战斗场面展开",
        "video",
        triggers=("对战", "交战", "混战", "开战", "战斗"),
    ),
    _p(
        "dynamic-6",
        "dynamic",
        "动态运动类",
        "水平摇镜",
        "从左到右水平摇镜，依次呈现 [樱花树→石板路→秋千]，摇动速度均匀，画面稳定不抖",
        "从左到右水平摇镜，依次呈现 [樱花树→石板路→秋千]，摇动速度均匀，画面稳定不抖",
        "风景游览",
        "video",
        triggers=("游览", "沿途", "风景", "街景", "环顾"),
    ),
    _p(
        "dynamic-7",
        "dynamic",
        "动态运动类",
        "垂直摇镜",
        "从上到下垂直摇镜，从 [角色的头发] 摇至 [沾满泥土的鞋子]，摇动匀速连贯，上下构图完整",
        "从上到下垂直摇镜，从 [角色的头发] 摇至 [沾满泥土的鞋子]，摇动匀速连贯，上下构图完整",
        "角色全身展示",
        "video",
        triggers=("打量", "上下", "全身"),
    ),
    _p(
        "dynamic-8",
        "dynamic",
        "动态运动类",
        "环形摇镜",
        "360° 环形摇镜，围绕 [中间的主角 + 四周的队友] 旋转，旋转匀速，人物始终清晰入画，团队站位完整",
        "360° 环形摇镜，围绕 [中间的主角 + 四周的队友] 旋转，旋转匀速，人物始终清晰入画，团队站位完整",
        "团队登场",
        "video",
        triggers=("团队", "集结", "合围", "众人", "围住"),
    ),
    _p(
        "dynamic-9",
        "dynamic",
        "动态运动类",
        "跟镜",
        "跟镜头，跟随 [向前奔跑的主角] 移动，保持人物在画面中心，背景自然掠过",
        "跟镜头，跟随 [向前奔跑的主角] 移动，保持人物在画面中心，背景自然掠过",
        "上学迟到 / 追逐",
        "video",
        triggers=("奔跑", "迟到", "跑", "狂奔"),
    ),
    _p(
        "dynamic-10",
        "dynamic",
        "动态运动类",
        "跟镜（飞行）",
        "跟镜头，跟随 [骑扫帚飞行的角色] 穿梭云层，视角与人物平行，速度与飞行一致",
        "跟镜头，跟随 [骑扫帚飞行的角色] 穿梭云层，视角与人物平行，速度与飞行一致",
        "空中飞行",
        "video",
        triggers=("飞行", "翱翔", "飞向", "起飞"),
    ),
    _p(
        "dynamic-11",
        "dynamic",
        "动态运动类",
        "跟镜（战斗）",
        "跟镜头，跟随 [挥剑的角色] 移动，镜头随剑的轨迹摆动，速度灵活适配动作",
        "跟镜头，跟随 [挥剑的角色] 移动，镜头随剑的轨迹摆动，速度灵活适配动作",
        "近身战斗",
        "video",
        triggers=("挥剑", "挥刀", "挥砍", "格斗", "近身"),
    ),
    _p(
        "dynamic-12",
        "dynamic",
        "动态运动类",
        "俯冲推镜",
        "高空俯冲推镜，从 [云层俯视] 快速冲向 [地面的主角]，速度由慢到快，地面主体构图完整",
        "高空俯冲推镜，从 [云层俯视] 快速冲向 [地面的主角]，速度由慢到快，地面主体构图完整",
        "高空降落 / 救援",
        "video",
        triggers=("俯冲", "降落", "坠落", "高空", "坠下"),
    ),
    _p(
        "dynamic-13",
        "dynamic",
        "动态运动类",
        "仰角跟镜",
        "低角度仰角跟镜，跟随 [站立起身的主角] 移动，人物显得高大威严，仰角透视自然",
        "低角度仰角跟镜，跟随 [站立起身的主角] 移动，人物显得高大威严，仰角透视自然",
        "角色气场提升",
        "video",
        triggers=("站起", "起身", "逆袭", "气场", "挺直"),
    ),
    _p(
        "dynamic-14",
        "dynamic",
        "动态运动类",
        "甩镜",
        "甩镜，从 [角色 A 的脸] 快速甩至 [角色 B 的脸]，甩动迅猛，起幅与落幅构图明确",
        "甩镜，从 [角色 A 的脸] 快速甩至 [角色 B 的脸]，甩动迅猛，起幅与落幅构图明确",
        "角色对峙",
        "video",
        triggers=("对峙", "针锋相对", "怒视", "回头"),
    ),
    _p(
        "dynamic-15",
        "dynamic",
        "动态运动类",
        "蛇形摇镜",
        "S 形蛇形摇镜，穿梭 [森林的树藤之间]，依次闪过 [隐藏的陷阱 / 小动物]，摇动路径顺滑灵活",
        "S 形蛇形摇镜，穿梭 [森林的树藤之间]，依次闪过 [隐藏的陷阱 / 小动物]，摇动路径顺滑灵活",
        "森林探险",
        "video",
        triggers=("探险", "穿梭", "迷宫", "密林", "森林"),
    ),
    _p(
        "dynamic-16",
        "dynamic",
        "动态运动类",
        "定点跟镜",
        "定点跟镜，镜头固定，跟随 [旋转跳舞的角色] 转动，人物始终在画面中心",
        "定点跟镜，镜头固定，跟随 [旋转跳舞的角色] 转动，人物始终在画面中心",
        "舞蹈 / 仪式场景",
        "video",
        triggers=("跳舞", "舞蹈", "旋转", "起舞"),
    ),
)

# ---------------------------------------------------------------------------
# 视角切换类运镜 12 种：视角/焦段语言，同时服务分镜图与视频（both）
# ---------------------------------------------------------------------------

_POV_PRESETS = (
    _p(
        "pov-1",
        "pov",
        "视角切换类",
        "第一人称 POV（日常）",
        "主观 POV 镜头，以 [主角] 的视线观察 [桌上的早餐 / 黑板的板书]，画面带轻微呼吸感晃动",
        "主观 POV 镜头，以 [主角] 的视线观察 [桌上的早餐 / 黑板的板书]，画面带轻微呼吸感晃动",
        "日常沉浸式体验",
        "both",
        triggers=("视线", "张望", "打量"),
    ),
    _p(
        "pov-2",
        "pov",
        "视角切换类",
        "第一人称 POV（战斗）",
        "主观 POV 镜头，以 [主角] 的视线看向 [挥拳冲来的敌人]，画面带轻微震动",
        "主观 POV 镜头，以 [主角] 的视线看向 [挥拳冲来的敌人]，画面带轻微震动",
        "战斗第一视角",
        "both",
        triggers=("挥拳", "冲来", "逼近", "迎面"),
    ),
    _p(
        "pov-3",
        "pov",
        "视角切换类",
        "第三人称 POV（跟随）",
        "第三人称 POV 镜头，以 [角色背后] 的视角跟随行走，画面随步伐轻微起伏",
        "第三人称 POV 镜头，以 [角色背后] 的视角跟随行走，画面随步伐轻微起伏",
        "角色探索场景",
        "both",
        triggers=("行走", "漫步", "探索", "前行"),
    ),
    _p(
        "pov-4",
        "pov",
        "视角切换类",
        "高空俯视镜头",
        "90° 高空俯视镜头，清晰呈现 [战场的阵营分布 / 迷宫的路线]，人物如棋子般排列，全局布局完整",
        "90° 高空俯视镜头，清晰呈现 [战场的阵营分布 / 迷宫的路线]，人物如棋子般排列，全局布局完整",
        "战场 / 迷宫布局",
        "both",
        triggers=("阵营", "布局", "俯瞰", "路线"),
    ),
    _p(
        "pov-5",
        "pov",
        "视角切换类",
        "斜角俯视镜头",
        "30° 斜角俯视镜头，拍摄 [教室的座位分布 / 角色的互动]，斜角构图稳定，画面有动感",
        "30° 斜角俯视镜头，拍摄 [教室的座位分布 / 角色的互动]，斜角构图稳定，画面有动感",
        "教室日常",
        "both",
        triggers=("教室", "座位"),
    ),
    _p(
        "pov-6",
        "pov",
        "视角切换类",
        "低角度仰视镜头",
        "极低角度仰视镜头，聚焦 [站立的反派角色]，天空占画面 2/3，人物压迫感拉满",
        "极低角度仰视镜头，聚焦 [站立的反派角色]，天空占画面 2/3，人物压迫感拉满",
        "反派登场",
        "both",
        triggers=("反派", "登场", "压迫", "居高临下"),
    ),
    _p(
        "pov-7",
        "pov",
        "视角切换类",
        "平视视角",
        "平视视角镜头，与 [角色的眼睛] 平齐，画面稳定，人物表情清晰",
        "平视视角镜头，与 [角色的眼睛] 平齐，画面稳定，人物表情清晰",
        "角色平等对话",
        "both",
        triggers=("对视", "对话", "平等"),
    ),
    _p(
        "pov-8",
        "pov",
        "视角切换类",
        "侧视视角",
        "侧面平视视角，拍摄 [两人对峙的侧身]，画面左右对称，张力十足",
        "侧面平视视角，拍摄 [两人对峙的侧身]，画面左右对称，张力十足",
        "角色对决",
        "both",
        triggers=("对决", "对峙", "两侧", "侧身"),
    ),
    _p(
        "pov-9",
        "pov",
        "视角切换类",
        "鱼眼视角",
        "鱼眼镜头视角，画面边缘轻微畸变，聚焦 [角色夸张的表情 / 热闹的派对]",
        "鱼眼镜头视角，画面边缘轻微畸变，聚焦 [角色夸张的表情 / 热闹的派对]",
        "夸张喜剧场景",
        "both",
        triggers=("夸张", "派对", "搞笑", "喜剧"),
    ),
    _p(
        "pov-10",
        "pov",
        "视角切换类",
        "长焦视角",
        "长焦镜头视角，远距离拍摄 [远处屋顶的神秘人]，背景压缩，主体突出",
        "长焦镜头视角，远距离拍摄 [远处屋顶的神秘人]，背景压缩，主体突出",
        "暗中观察场景",
        "both",
        triggers=("远处", "窥视", "暗中", "观察", "尾随"),
    ),
    _p(
        "pov-11",
        "pov",
        "视角切换类",
        "微距视角",
        "微距视角镜头，放大 [角色衣服的破洞 / 花瓣的纹理]，细节清晰，主体突出，背景简洁",
        "微距视角镜头，放大 [角色衣服的破洞 / 花瓣的纹理]，细节清晰，主体突出，背景简洁",
        "微观细节展示",
        "both",
        triggers=("花瓣", "纹理", "破洞", "露珠"),
    ),
    _p(
        "pov-12",
        "pov",
        "视角切换类",
        "上帝视角",
        "上帝视角全景镜头，俯瞰 [整个城市的昼夜交替 / 故事的时间线]，全局构图完整，画面宏大",
        "上帝视角全景镜头，俯瞰 [整个城市的昼夜交替 / 故事的时间线]，全局构图完整，画面宏大",
        "全局时间流逝",
        "both",
        triggers=("俯瞰", "昼夜", "时间流逝", "全局"),
    ),
)

# ---------------------------------------------------------------------------
# 转场衔接类运镜 10 种：镜间转场语言，目标端为合成阶段（transition）
# ---------------------------------------------------------------------------

_TRANSITION_PRESETS = (
    _p(
        "transition-1",
        "transition",
        "转场衔接类",
        "叠化转场（回忆）",
        "叠化转场，前镜 [主角的笑脸] 缓慢叠入后镜 [童年的老房子]，过渡时长 2 秒，两镜叠合节奏舒缓",
        "叠化转场，前镜 [主角的笑脸] 缓慢叠入后镜 [童年的老房子]，过渡时长 2 秒，两镜叠合节奏舒缓",
        "回忆闪回",
        "transition",
        triggers=("回忆", "闪回", "想起", "往事"),
    ),
    _p(
        "transition-2",
        "transition",
        "转场衔接类",
        "叠化转场（时间流逝）",
        "叠化转场，前镜 [清晨的街道] 叠入后镜 [深夜的街道]，过渡时长 3 秒，昼夜切换叠合自然",
        "叠化转场，前镜 [清晨的街道] 叠入后镜 [深夜的街道]，过渡时长 3 秒，昼夜切换叠合自然",
        "时间流逝",
        "transition",
        triggers=("时间流逝", "昼夜", "过了"),
    ),
    _p(
        "transition-3",
        "transition",
        "转场衔接类",
        "闪切转场（紧张）",
        "闪切转场，前镜 [角色惊恐的脸] 快速切入后镜 [爆炸的火光]，切换快速果断，两镜边界明确",
        "闪切转场，前镜 [角色惊恐的脸] 快速切入后镜 [爆炸的火光]，切换快速果断，两镜边界明确",
        "突发危机",
        "transition",
        triggers=("爆炸", "危机", "突变", "惊呼"),
    ),
    _p(
        "transition-4",
        "transition",
        "转场衔接类",
        "闪切转场（战斗）",
        "闪切转场，前镜 [剑出鞘特写] 切入后镜 [敌人倒地]，切换快速果断，冲击力强",
        "闪切转场，前镜 [剑出鞘特写] 切入后镜 [敌人倒地]，切换快速果断，冲击力强",
        "战斗高光时刻",
        "transition",
        triggers=("出鞘", "倒地", "高光"),
    ),
    _p(
        "transition-5",
        "transition",
        "转场衔接类",
        "划镜转场（左右）",
        "从左向右划镜转场，前镜 [室内对话] 划转为后镜 [室外追逐]，划动轨迹连贯，两镜衔接明确",
        "从左向右划镜转场，前镜 [室内对话] 划转为后镜 [室外追逐]，划动轨迹连贯，两镜衔接明确",
        "场景切换",
        "transition",
    ),
    _p(
        "transition-6",
        "transition",
        "转场衔接类",
        "划镜转场（上下）",
        "从上向下划镜转场，前镜 [天空的飞鸟] 划转为后镜 [地面的人群]，上下衔接过渡自然",
        "从上向下划镜转场，前镜 [天空的飞鸟] 划转为后镜 [地面的人群]，上下衔接过渡自然",
        "天地场景切换",
        "transition",
    ),
    _p(
        "transition-7",
        "transition",
        "转场衔接类",
        "淡入转场（开场）",
        "淡入转场，从纯黑画面缓慢淡入 [阳光明媚的村庄]，时长 2 秒，画面由黑渐显",
        "淡入转场，从纯黑画面缓慢淡入 [阳光明媚的村庄]，时长 2 秒，画面由黑渐显",
        "剧集开场",
        "transition",
    ),
    _p(
        "transition-8",
        "transition",
        "转场衔接类",
        "淡出转场（结尾）",
        "淡出转场，从 [主角的背影] 缓慢淡出至纯黑，时长 3 秒，画面渐隐节奏舒缓",
        "淡出转场，从 [主角的背影] 缓慢淡出至纯黑，时长 3 秒，画面渐隐节奏舒缓",
        "剧集结尾",
        "transition",
    ),
    _p(
        "transition-9",
        "transition",
        "转场衔接类",
        "画中画转场",
        "画中画转场，前镜 [主角的特写] 缩小成画面一角，后镜 [全景场景] 逐步展开，时长 1.5 秒",
        "画中画转场，前镜 [主角的特写] 缩小成画面一角，后镜 [全景场景] 逐步展开，时长 1.5 秒",
        "视角切换过渡",
        "transition",
    ),
    _p(
        "transition-10",
        "transition",
        "转场衔接类",
        "匹配转场（相似元素）",
        "匹配转场，前镜 [角色手中的圆镜] 与后镜 [满月] 形状匹配，无缝衔接，时长 1 秒",
        "匹配转场，前镜 [角色手中的圆镜] 与后镜 [满月] 形状匹配，无缝衔接，时长 1 秒",
        "意象衔接",
        "transition",
    ),
)

CAMERA_PRESETS: tuple[CameraPreset, ...] = (
    *_STATIC_PRESETS,
    *_DYNAMIC_PRESETS,
    *_POV_PRESETS,
    *_TRANSITION_PRESETS,
)

_CATEGORY_LABELS = {
    "static": "基础固定类",
    "dynamic": "动态运动类",
    "pov": "视角切换类",
    "transition": "转场衔接类",
}

# 参与镜头级运镜决策的类别（转场类属于合成阶段，不参与单镜决策）
_DECISION_CATEGORIES = ("static", "dynamic", "pov")


def normalize_policy(value: Any) -> str:
    """策略归一化：未知值一律回落到最保守的「克制」。"""
    text = str(value or "").strip()
    return text if text in SUPPORTED_POLICIES else DEFAULT_CAMERA_POLICY


def presets_by_category() -> dict[str, list[CameraPreset]]:
    """按分类返回词库，供 API / 前端选择器使用。"""
    grouped: dict[str, list[CameraPreset]] = {}
    for preset in CAMERA_PRESETS:
        grouped.setdefault(preset.category, []).append(preset)
    return grouped


def category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category)


def is_static_camera(camera_move: Any) -> bool:
    """camera_move 为空或命中固定机位标记即视为定镜。"""
    text = str(camera_move or "").strip()
    if not text:
        return True
    return any(marker in text for marker in _FIXED_MARKERS)


def find_preset(camera_move: Any) -> CameraPreset | None:
    """把 camera_move 文本映射到词库条目；未知写法返回 None（不做判断）。

    支持「缓慢推镜（人物）」这类带括号的写法：先剥离括号内容再精确匹配，
    精确匹配失败后按名称包含匹配。转场类不参与单镜匹配。
    """
    text = str(camera_move or "").strip()
    if not text:
        return None
    normalized = _PAREN_RE.sub("", text).strip()
    for preset in CAMERA_PRESETS:
        if preset.category not in _DECISION_CATEGORIES:
            continue
        if normalized == preset.name or (
            normalized and preset.name.replace("（", "").replace("）", "") == normalized
        ):
            return preset
    for preset in CAMERA_PRESETS:
        if preset.category not in _DECISION_CATEGORIES:
            continue
        if preset.name and preset.name in text:
            return preset
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


def _shot_text(shot: Mapping[str, Any]) -> str:
    parts = [
        _text(shot.get("action")),
        _text(shot.get("start_state")),
        _text(shot.get("end_state")),
        _text(shot.get("shot_type")),
    ]
    return " ".join(part for part in parts if part)


def _trigger_hits(preset: CameraPreset, text: str) -> tuple[str, ...]:
    return tuple(trigger for trigger in preset.triggers if trigger and trigger in text)


def _motivation_has_move(motivation: str) -> bool:
    return any(marker in motivation for marker in _MOTIVATION_MARKERS)


def _static_preferred_markers(text: str) -> tuple[str, ...]:
    return tuple(marker for marker in _STATIC_PREFERRED_MARKERS if marker and marker in text)


def best_camera_suggestion(shot_text: str) -> tuple[CameraPreset, tuple[str, ...]] | None:
    """漏加检测：固定机位的分镜命中动态运镜触发表时，给出得分最高的运镜建议。"""
    if not shot_text.strip():
        return None
    best: tuple[CameraPreset, tuple[str, ...]] | None = None
    for preset in _DYNAMIC_PRESETS:
        hits = _trigger_hits(preset, shot_text)
        if not hits:
            continue
        if best is None or len(hits) > len(best[1]):
            best = (preset, hits)
    return best


def camera_decision_diagnostics(
    shot: Mapping[str, Any],
    *,
    camera_motivation: str = "",
    policy: str = DEFAULT_CAMERA_POLICY,
) -> list[CameraSuggestion]:
    """运镜双方向诊断（只读，不改分镜数据）。

    - 多插方向：非固定运镜但没有可读出的动机（触发表 / 导演动机），
      或落在反模式场景（对谈、静坐凝望等）→ warning（克制/标准）或 suggestion（丰富），
      推荐改回「固定」。
    - 漏加方向：固定机位但剧情命中动态运镜触发表 → 克制策略不提醒；
      标准/丰富策略给 suggestion（recommended_camera_move 为建议运镜名），
      由用户在 UI 上决定是否采纳。
    """
    policy = normalize_policy(policy)
    camera_move = _text(shot.get("camera_move"))
    preset = find_preset(camera_move)
    shot_text = _shot_text(shot)
    motivation = _text(camera_motivation)
    issues: list[CameraSuggestion] = []

    if is_static_camera(camera_move) or (preset is not None and preset.is_static):
        # 漏加方向：固定机位 + 命中动态运镜触发表
        if policy == POLICY_CONSERVATIVE:
            return []
        best = best_camera_suggestion(shot_text)
        if best is not None:
            suggested, hits = best
            issues.append(
                CameraSuggestion(
                    code="camera_suggestion",
                    severity="suggestion",
                    message=(
                        f"本镜命中叙事意图「{'、'.join(hits)}」"
                        f"（适配场景：{suggested.fit_scenes}），可考虑改用运镜「{suggested.name}」"
                    ),
                    recommended_camera_move=suggested.name,
                    details={"fit_scenes": suggested.fit_scenes},
                )
            )
        return issues

    if preset is None:
        return []

    # 非固定运镜：检查动机与反模式
    triggered = _trigger_hits(preset, shot_text)
    motivated = bool(triggered) or _motivation_has_move(motivation)
    static_markers = _static_preferred_markers(shot_text)
    severity = "suggestion" if policy == POLICY_RICH else "warning"
    # 克制/标准建议改回固定；丰富档只做温和提示，不推荐改固定
    recommended = "" if policy == POLICY_RICH else "固定"
    if static_markers:
        issues.append(
            CameraSuggestion(
                code="camera_anti_pattern",
                severity=severity,
                message=(
                    f"反模式场景（{'、'.join(static_markers)}）使用了运镜「{preset.name}」，"
                    "此类场景优先定镜，建议改回固定机位"
                ),
                recommended_camera_move=recommended,
                details={"camera_move": preset.name},
            )
        )
        return issues
    if not motivated:
        issues.append(
            CameraSuggestion(
                code="camera_unmotivated",
                severity=severity,
                message=(
                    f"运镜「{preset.name}」缺少可读出的叙事动机"
                    f"（其适配场景为「{preset.fit_scenes}」），"
                    "建议改为固定机位，或把动机写进动作描述"
                ),
                recommended_camera_move=recommended,
                details={"fit_scenes": preset.fit_scenes},
            )
        )
    return issues


__all__ = [
    "CAMERA_PRESETS",
    "CameraPreset",
    "CameraSuggestion",
    "DEFAULT_CAMERA_POLICY",
    "POLICY_CONSERVATIVE",
    "POLICY_RICH",
    "POLICY_STANDARD",
    "SUPPORTED_POLICIES",
    "best_camera_suggestion",
    "camera_decision_diagnostics",
    "category_label",
    "find_preset",
    "is_static_camera",
    "normalize_policy",
    "presets_by_category",
]

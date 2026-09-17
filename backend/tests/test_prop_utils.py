from __future__ import annotations

from app.services.prop_utils import filter_critical_props, is_critical_prop, is_non_critical_prop


def test_user_reported_props_are_all_non_critical() -> None:
    """用户报错中的 10 个日常物品全部必须识别为非关键道具。"""
    user_props = [
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
    ]
    for prop in user_props:
        assert is_non_critical_prop(prop) is True, f"{prop} 应被识别为非关键道具"
        assert is_critical_prop(prop) is False

    assert filter_critical_props(user_props) == []


def test_common_variants_and_everyday_objects() -> None:
    variants = [
        "一个纸袋",
        "外卖咖啡袋",
        "白色抱枕",
        "空调遥控器",
        "厨房灶台",
        "木质门框",
        "铁锅铲",
        "黑色手机",
        "透明文件夹",
        "一副碗筷",
    ]
    for prop in variants:
        assert is_non_critical_prop(prop) is True, f"{prop} 应被识别为非关键道具"
    assert filter_critical_props(variants) == []


def test_critical_props_are_preserved() -> None:
    critical_props = [
        "青铜古剑",
        "染血的日记本",
        "银色怀表",
        "父亲留下的信物",
        "凶器匕首",
        "密封的遗嘱",
        "玉佩",
        "神秘账本",
        "手提箱",
    ]
    for prop in critical_props:
        assert is_critical_prop(prop) is True, f"{prop} 应被识别为关键道具"
        assert is_non_critical_prop(prop) is False

    assert filter_critical_props(critical_props) == critical_props


def test_mixed_props_filtering() -> None:
    mixed = ["手机", "青铜古剑", "纸袋", "玉佩", "门框", "抱枕", "染血的日记本"]
    assert filter_critical_props(mixed) == ["青铜古剑", "玉佩", "染血的日记本"]

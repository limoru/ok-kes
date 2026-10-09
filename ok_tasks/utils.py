from ok import TriggerTask

import re
from collections import namedtuple
import random
import time
import sys
import json
import cv2
import os
import numpy as np
from opencc import OpenCC

from utils_equipment_engraving import (
    PRO_MODE_ACTION_ESCAPE,
    PRO_MODE_ACTION_STOP_FOR_REVIEW,
    PRO_MODE_ACTION_PAUSE_NO_EXIT,
    decide_pro_mode_check_point_action,
    is_pro_mode_escape_check_point,
    name_only_candidate_slot,
    resolve_candidate_engraving_flag,
    resolve_slot_description_after_install,
    should_assign_to_target_member,
    should_refine_free_equipment,
    slots_all_completed,
)

_jp2t = OpenCC('jp2t')  # 日文新字体转繁体
_t2s = OpenCC('t2s')  # 繁体转简体

def _normalize_text(text):
    """先将日文汉字字形转繁体，再统一转换为简体。"""
    return _t2s.convert(_jp2t.convert(text))


def _edit_distance(s1, s2, max_dist=1):
    """计算两个字符串的编辑距离是否 <= max_dist。"""
    if abs(len(s1) - len(s2)) > max_dist:
        return False
    if not s1 or not s2:
        return max(len(s1), len(s2)) <= max_dist
    m, n = len(s1), len(s2)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n] <= max_dist


def is_subsequence(first: str, second: str) -> bool:
    """判断第一个字符串是否为第二个字符串的子序列。"""
    second_iter = iter(second)
    return all(char in second_iter for char in first)


def _move_and_click(task: TriggerTask, x, y):
    """先将鼠标移动到目标位置，等待界面响应后再点击。"""
    page_handler = sys._getframe(1).f_code.co_name
    task.log_info(
        f"页面处理「{page_handler}」触发点击事件，点击目标坐标=({x:.3f}, {y:.3f})"
    )
    task.move_relative(x, y)
    task.sleep(0.5)
    task.click(x, y)


def _simplify_texts(texts):
    """将OCR结果按 jp2t → t2s 批量转换为简体（原地修改）。"""
    for b in texts:
        b.name = _normalize_text(b.name)
    return texts


def _get_config_value(task: TriggerTask, key, default):
    """读取运行时配置，优先从 task.config 读取，其次 default_config，最后使用默认值。返回前将字符串转简体。"""
    if hasattr(task, 'config') and key in task.config:
        value = task.config[key]
    else:
        value = getattr(task, 'default_config', {}).get(key, default)
    if isinstance(value, str):
        value = _normalize_text(value).strip()
    elif isinstance(value, (list, tuple)):
        value = [
            _normalize_text(v).strip() if isinstance(v, str) else v
            for v in value
        ]
    return value


def _get_card_list(task: TriggerTask, key):
    """读取列表配置，解析失败返回空列表。"""
    value = _get_config_value(task, key, [])
    return list(value) if isinstance(value, (list, tuple)) else []


def _get_card_reward_priority(task: TriggerTask):
    """读取卡牌奖励优先级，并将刷初始卡牌配置置于最高优先级。"""
    priority = _get_card_list(task, "卡牌奖励优先级")
    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    if initial_card_name:
        priority = [
            initial_card_name,
            *(name for name in priority if name != initial_card_name),
        ]
    return priority


# 游戏语言 → 映射文件路径
_GAME_LANG_FILE_MAP = {
    "繁体中文": os.path.join(os.path.dirname(__file__), 'assets', 'game_text_map', 'zh_tw.py'),
}
# 已加载的映射缓存 {语言: SERVER_TEXT_MAP字典}
_LOADED_MAPS = {}


def _load_game_text_map(game_lang):
    """加载指定语言的映射表（带缓存）。"""
    if game_lang not in _LOADED_MAPS:
        file_path = _GAME_LANG_FILE_MAP.get(game_lang)
        if file_path and os.path.exists(file_path):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(f"_game_map_{game_lang}", file_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _LOADED_MAPS[game_lang] = getattr(mod, 'SERVER_TEXT_MAP', {})
            except Exception:
                _LOADED_MAPS[game_lang] = {}
        else:
            _LOADED_MAPS[game_lang] = {}
    return _LOADED_MAPS[game_lang]


def _get_game_language(task: TriggerTask):
    """获取当前模式配置的游戏语言。"""
    try:
        return str(task.config.get('游戏语言', '简体中文')).strip() or '简体中文'
    except Exception:
        return '简体中文'


def _get_game_text(task: TriggerTask, default_text):
    """根据当前模式配置的游戏语言，返回对应服务器版本的搜索文本。"""
    game_lang = _get_game_language(task)

    if game_lang == '简体中文':
        return default_text

    mapping = _load_game_text_map(game_lang)
    return mapping.get(default_text, default_text)


# 装备词典文件路径：id <-> 装备名称/所属装备 的固定映射表。
# 详见文件内 "_readme" 字段的维护规则说明。
_EQUIPMENT_DICTIONARY_FILE = os.path.join(
    os.path.dirname(__file__), 'assets', 'equipment_dictionary.json'
)
# 进程内缓存，程序启动后首次用到时加载一次，之后复用，不重复读盘。
_EQUIPMENT_DICTIONARY_CACHE = None


def _load_equipment_dictionary():
    """加载装备词典（带进程内缓存），返回统一结构：
    {
        "by_id": {int id: {"name": str, "slot": int|None}},
        "by_name": {str name: {"id": int, "slot": int|None}},
    }
    文件缺失或格式错误时返回空结构，不影响主流程运行。
    """
    global _EQUIPMENT_DICTIONARY_CACHE
    if _EQUIPMENT_DICTIONARY_CACHE is not None:
        return _EQUIPMENT_DICTIONARY_CACHE

    by_id = {}
    by_name = {}
    try:
        with open(_EQUIPMENT_DICTIONARY_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        for entry in raw.get('equipments', []):
            entry_id = entry.get('id')
            name = entry.get('name')
            slot = entry.get('slot')
            if not isinstance(entry_id, int) or not isinstance(name, str) or not name:
                continue
            slot = slot if isinstance(slot, int) else None
            by_id[entry_id] = {"name": name, "slot": slot}
            by_name[name] = {"id": entry_id, "slot": slot}
    except Exception:
        by_id = {}
        by_name = {}

    _EQUIPMENT_DICTIONARY_CACHE = {"by_id": by_id, "by_name": by_name}
    return _EQUIPMENT_DICTIONARY_CACHE


def get_equipment_id(name):
    """按装备名称查询词典编号；未收录时返回 None。"""
    entry = _load_equipment_dictionary()["by_name"].get(name)
    return entry["id"] if entry else None


def get_equipment_name(equipment_id):
    """按词典编号查询装备名称；未收录时返回 None。"""
    entry = _load_equipment_dictionary()["by_id"].get(equipment_id)
    return entry["name"] if entry else None


def get_equipment_slot_by_name(name):
    """按装备名称查询所属装备下标（0/1/2）；未收录或未标注装备时返回 None。"""
    entry = _load_equipment_dictionary()["by_name"].get(name)
    return entry["slot"] if entry else None


ROUTE_PRIORITY_DEFAULT = ["商店", "休息", "事件", "小怪", "精英"]

# 路线优先级按层分开配置：0=第一层，>=1=第二层。
# 两层都没配置时回退旧配置「路线优先级」，再回退默认顺序。
ROUTE_PRIORITY_LAYER_KEYS = ("第一层路线优先级", "第二层路线优先级")

# 列表项写法归一：旧配置里出现过英文/别名写法，统一成界面上的中文名。
ROUTE_PRIORITY_ALIASES = {"boss": "精英", "shop": "商店"}


def _normalize_route_priority(value, default=None):
    """规整路线优先级列表：统一别名写法，非列表或空列表回退默认顺序。"""
    if isinstance(value, (list, tuple)):
        priority = [
            ROUTE_PRIORITY_ALIASES.get(item, item) if isinstance(item, str) else item
            for item in value
        ]
        if priority:
            return priority
    return list(ROUTE_PRIORITY_DEFAULT if default is None else default)


def _migrate_route_boss_to_elite(task: TriggerTask):
    """迁移用户配置中路线优先级的旧写法（如 "boss"→"精英"、"shop"→"商店"）。"""
    try:
        if not hasattr(task, 'config'):
            return
        for key in ('路线优先级',) + ROUTE_PRIORITY_LAYER_KEYS:
            if key not in task.config:
                continue
            priority = task.config[key]
            if not isinstance(priority, (list, tuple)):
                continue
            new_priority = [
                ROUTE_PRIORITY_ALIASES.get(v, v) if isinstance(v, str) else v
                for v in priority
            ]
            if new_priority != list(priority):
                task.config[key] = new_priority
                from ok.gui.Communicate import communicate
                communicate.task_list_updated.emit()
                task.log_info(f"迁移{key}的旧写法: {new_priority}")
    except Exception:
        pass


def _get_route_priority(task: TriggerTask, layer_index=None):
    """读取路线节点优先级配置，返回列表；解析失败使用默认顺序。

    layer_index 为 None 时只读旧配置「路线优先级」，出击模式等旧调用方行为不变。
    传入层号时按层读取（0=第一层，>=1=第二层），本层未配置或配置为空时依次回退：
    另一层配置 -> 旧配置「路线优先级」 -> 默认顺序。
    """
    if layer_index is None:
        return _normalize_route_priority(
            _get_config_value(task, '路线优先级', None)
        )
    if layer_index <= 0:
        layer_key, other_key = ROUTE_PRIORITY_LAYER_KEYS
    else:
        other_key, layer_key = ROUTE_PRIORITY_LAYER_KEYS
    for key in (layer_key, other_key, '路线优先级'):
        value = _get_config_value(task, key, None)
        if value:
            return _normalize_route_priority(value)
    return list(ROUTE_PRIORITY_DEFAULT)


# ------------------------- 通用工具 -------------------------

def _get_current_credit(task: TriggerTask):
    """读取当前信用点数，从两个可能位置取最大值。"""
    credit = 0
    for pos_x, pos_y in [(0.794, 0.054), (0.734, 0.053)]:
        box = find_box_at_point(task, pos_x, pos_y)
        if box and box.name.isdigit():
            val = int(box.name)
            if val > credit:
                credit = val
    return credit


def _neutral_card_limit(task: TriggerTask):
    """卡厄思模式每局最多允许在商店购买3张中立牌。"""
    return 3 if task.name == "自动卡厄思模式" else None


def _neutral_card_limit_reached(task: TriggerTask):
    """判断本局获得的中立牌是否已达到固定上限。"""
    limit = _neutral_card_limit(task)
    if limit is None:
        return False
    acquired = getattr(task, "node_status", {}).get("neutral_card_count", 0)
    return acquired >= limit


def _mark_pending_shop_removal(task: TriggerTask):
    """标记"这次删卡由德朗商店的「移除卡牌」入口发起"。"""
    task._pending_shop_removal = True


def _consume_pending_shop_removal(task: TriggerTask) -> bool:
    """取下"这次删卡由商店入口发起"的标记，取完即清。

    商店删卡与事件/三选一删卡分开计数：商店入口点开删卡页之后，真正的"删除"
    要等 handle_remove / handle_three_choice_card_remove 点下「移除」才发生，
    所以入口处只打标记，删成功时再按标记把数字计进
    `node_status['shop_removed_card_count']`。
    """
    pending = bool(getattr(task, "_pending_shop_removal", False))
    task._pending_shop_removal = False
    return pending


def _record_removed_cards(task: TriggerTask, count=1, from_shop=False):
    """记录本局实际完成移除的卡牌数量。

    `from_shop=True` 表示这次删卡是德朗商店的「移除卡牌」入口发起的，除了本局
    删卡总数，还单独计入 `node_status['shop_removed_card_count']`（商店删卡次数
    上限只看这个数，事件/三选一删卡不占用商店额度）。
    """
    node_status = getattr(task, "node_status", None)
    if not isinstance(node_status, dict):
        return
    count = max(1, int(count))
    node_status["removed_card_count"] = (
        node_status.get("removed_card_count", 0) + count
    )
    message = (
        f"本局移除卡牌数量增加{count}，"
        f"当前共{node_status['removed_card_count']}张"
    )
    if from_shop:
        node_status["shop_removed_card_count"] = (
            node_status.get("shop_removed_card_count", 0) + count
        )
        message += f"（其中商店删卡{node_status['shop_removed_card_count']}张）"
    task.log_info(message)


def _record_neutral_card(task: TriggerTask):
    """记录本局实际获得一张中立牌。"""
    node_status = getattr(task, "node_status", None)
    if not isinstance(node_status, dict):
        return
    node_status["neutral_card_count"] = (
        node_status.get("neutral_card_count", 0) + 1
    )
    task.log_info(
        f"本局获得中立牌数量增加1，"
        f"当前共{node_status['neutral_card_count']}张"
    )


def _parse_discounted_price(price_text):
    """解析可能将折扣前后价格连在一起的 OCR 数字。"""
    price_text = price_text.strip()
    if not re.fullmatch(r"\d+", price_text):
        return None
    if len(price_text) == 6:
        return int(price_text[-3:])
    if len(price_text) in (4, 5):
        return int(price_text[-2:])
    return int(price_text)


def _get_current_hp_percent(task: TriggerTask):
    """读取当前生命值百分比，无法识别时返回 False。"""
    hp_box = find_box_at_point(task, 0.209, 0.040)
    if not hp_box:
        return False
    hp_match = re.search(r'(\d+)/(\d+)', hp_box.name)
    if not hp_match:
        return False
    current_hp = int(hp_match.group(1))
    max_hp = int(hp_match.group(2))
    if max_hp <= 0:
        return False
    hp_percent = int(current_hp * 100 / max_hp)
    task.log_info(f"当前生命值: {current_hp}/{max_hp} = {hp_percent}%")
    return hp_percent


def find_box_at_point(task: TriggerTask, rel_x, rel_y):
    """查找包含相对坐标点的 box，多个命中时返回面积最小的（最精确）。"""
    px, py = rel_x * task.width, rel_y * task.height
    hits = [b for b in task.all_texts
            if b.x <= px <= b.x + b.width and b.y <= py <= b.y + b.height]
    return min(hits, key=lambda b: b.area()) if hits else None


def find_target_card(task: TriggerTask):
    """查找target卡牌特征，返回特征框列表及其对应的相对点击位置。"""
    search_box = task.box_of_screen(0.090, 0.179, 0.927, 0.342)
    target_boxes = task.find_feature(feature_name="target", box=search_box) or []
    click_positions = []
    for target_box in target_boxes:
        center_x = (target_box.x + target_box.width / 2) / task.width
        center_y = (target_box.y + target_box.height / 2) / task.height
        click_positions.append((
            min(1.0, max(0.0, center_x - 0.0975)),
            min(1.0, max(0.0, center_y + 0.2460)),
        ))
    return target_boxes, click_positions


def _recognize_cards_by_features(
    task: TriggerTask,
    region,
    page,
    feature_types,
    min_feature_distance,
    name_offsets,
    type_offsets,
    description_offsets,
    name_only_feature_thresholds=None,
    allow_empty_type_threshold=None,
):
    """按指定特征和相对位置识别卡牌。"""
    search_box = task.box_of_screen(*region)
    feature_candidates = []
    for feature_name, feature_type in feature_types.items():
        feature_boxes = task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=0.65,
        ) or []
        for feature_box in feature_boxes:
            feature_candidates.append((feature_name, feature_type, feature_box))

    def feature_distance(first, second):
        first_x = (first.x + first.width / 2) / task.width
        first_y = (first.y + first.height / 2) / task.height
        second_x = (second.x + second.width / 2) / task.width
        second_y = (second.y + second.height / 2) / task.height
        return (
            (first_x - second_x) ** 2 + (first_y - second_y) ** 2
        ) ** 0.5

    filtered_features = []
    for candidate in sorted(
        feature_candidates,
        key=lambda item: item[2].confidence,
        reverse=True,
    ):
        if any(
            feature_distance(candidate[2], kept[2]) < min_feature_distance
            for kept in filtered_features
        ):
            continue
        filtered_features.append(candidate)

    cards = []
    log_prefix = f"{page}: " if page else ""
    for feature_name, feature_type, feature_box in filtered_features:
        center_x = (feature_box.x + feature_box.width / 2) / task.width
        center_y = (feature_box.y + feature_box.height / 2) / task.height
        name_region = (
            max(0.0, center_x + name_offsets[0]),
            max(0.0, center_y + name_offsets[1]),
            min(1.0, center_x + name_offsets[2]),
            min(1.0, center_y + name_offsets[3]),
        )
        type_region = (
            max(0.0, center_x + type_offsets[0]),
            max(0.0, center_y + type_offsets[1]),
            min(1.0, center_x + type_offsets[2]),
            min(1.0, center_y + type_offsets[3]),
        )
        desc_region = (
            max(0.0, center_x + description_offsets[0]),
            max(0.0, center_y + description_offsets[1]),
            min(1.0, center_x + description_offsets[2]),
            min(1.0, center_y + description_offsets[3]),
        )
        card_name = _get_region_text(task, name_region).strip()
        task.log_info(
            f"{log_prefix}卡牌识别调试: 特征={feature_name}，"
            f"特征中心=({center_x:.4f},{center_y:.4f})，"
            f"特征置信度={feature_box.confidence:.4f}，"
            f"名称区域={tuple(round(value, 4) for value in name_region)}，"
            f"名称OCR={_region_text_debug_info(task, name_region)}，"
            f"类型区域={tuple(round(value, 4) for value in type_region)}，"
            f"类型OCR={_region_text_debug_info(task, type_region)}，"
            f"描述区域={tuple(round(value, 4) for value in desc_region)}"
        )
        if not card_name:
            task.log_info(f"{log_prefix}卡牌识别调试: 因名称为空排除该特征")
            continue
        card_type = _get_region_text(task, type_region).strip()
        description = _get_region_text(task, desc_region)
        name_only_threshold = (name_only_feature_thresholds or {}).get(
            feature_name
        )
        allow_name_only = (
            name_only_threshold is not None
            and feature_box.confidence > name_only_threshold
        )
        allow_empty_type = (
            allow_empty_type_threshold is not None
            and feature_box.confidence > allow_empty_type_threshold
        )
        if not allow_name_only and (
            not description or (not card_type and not allow_empty_type)
        ):
            task.log_info(
                f"{log_prefix}卡牌识别调试: 因类型或描述缺失排除该特征，"
                f"类型=「{card_type}」，描述=「{description}」"
            )
            continue
        cards.append({
            "name": card_name,
            "type": card_type,
            "description": description,
            "feature_name": feature_name,
            "feature_type": feature_type,
            "confidence": feature_box.confidence,
            "feature_box": feature_box,
            "x": (name_region[0] + name_region[2]) / 2,
            "y": (name_region[1] + name_region[3]) / 2,
            "name_region": name_region,
            "type_region": type_region,
            "description_region": desc_region,
        })
    cards.sort(key=lambda card: card["feature_box"].x)
    if cards:
        task.log_info(f"{log_prefix}卡牌识别到{len(cards)}张卡牌")
        for index, card in enumerate(cards, 1):
            task.log_info(
                f"{log_prefix}卡牌{index}: 名称=「{card['name']}」，"
                f"类型=「{card['type'] or card['feature_type']}」，"
                f"描述=「{card['description']}」，"
                f"特征={card['feature_name']}，置信度={card['confidence']:.4f}"
            )
    return cards


def recognize_cards(
    task: TriggerTask,
    region=(0.021, 0.172, 0.988, 0.432),
    page="",
):
    """识别卡牌选择页面中的卡牌。"""
    return _recognize_cards_by_features(
        task=task,
        region=region,
        page=page,
        feature_types={
            "attack": "攻击/基础攻击",
            "skill": "技能/基础技能",
            "enhance": "强化",
            "hex": "咒术",
            "abnormal": "状态异常",
        },
        min_feature_distance=(
            (0.618 - 0.454) ** 2 + (0.304 - 0.306) ** 2
        ) ** 0.5,
        name_offsets=(-0.0150, -0.0635, 0.1450, -0.0175),
        type_offsets=(0.0120, -0.0205, 0.1090, 0.0245),
        description_offsets=(-0.0565, 0.1190, 0.1495, 0.4900),
    )


def recognize_cards_in_deck(
    task: TriggerTask,
    region=(0.274, 0.108, 0.929, 0.874),
    page="",
):
    """识别卡组区域中的卡牌，并标记金色边框选中的卡牌。"""
    cards = _recognize_cards_by_features(
        task=task,
        region=region,
        page=page,
        feature_types={
            "attack_in_deck": "攻击/基础攻击",
            "skill_in_deck": "技能/基础技能",
            "enhance_in_deck": "强化",
            "hex_in_deck": "咒术",
            "hex_in_deck_tw": "诅咒",
        },
        min_feature_distance=(
            (0.464 - 0.326) ** 2 + (0.175 - 0.175) ** 2
        ) ** 0.5,
        name_offsets=(-0.0090, -0.0435, 0.0900, -0.0105),
        type_offsets=(0.0070, -0.0175, 0.0880, 0.0175),
        description_offsets=(-0.0370, 0.0515, 0.1000, 0.3295),
        name_only_feature_thresholds={
            "hex_in_deck": 0.90,
            "hex_in_deck_tw": 0.90,
        },
        allow_empty_type_threshold=0.90,
    )
    _mark_selected_card_by_gold_border(task, cards, page=page)
    return cards


def recognize_event_options(
    task: TriggerTask,
    region=(0.198, 0.840, 0.803, 1.000),
    page="",
):
    """按事件选项特征识别最多三个事件描述。"""
    event_region = task.box_of_screen(*region)
    event_features = []
    for feature_name in ("event1", "event2", "event3", "event4", "event5", "event6", "event7", "event8"):
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=event_region,
            threshold=0.70,
        ) or []:
            center_x = (feature_box.x + feature_box.width / 2) / task.width
            center_y = (feature_box.y + feature_box.height / 2) / task.height
            event_features.append(
                (feature_name, feature_box, center_x, center_y)
            )

    filtered_features = []
    for candidate in sorted(
        event_features,
        key=lambda item: item[1].confidence,
        reverse=True,
    ):
        if any(
            (
                (candidate[2] - kept[2]) ** 2
                + (candidate[3] - kept[3]) ** 2
            ) ** 0.5 < 0.207
            for kept in filtered_features
        ):
            continue
        filtered_features.append(candidate)
        if len(filtered_features) >= 3:
            break

    filtered_features.sort(key=lambda item: item[2])
    event_options = []
    for feature_name, feature_box, center_x, center_y in filtered_features:
        description_region = (
            max(0.0, center_x - 0.119),
            max(0.0, center_y - 0.208),
            min(1.0, center_x + 0.122),
            min(1.0, center_y - 0.021),
        )
        description = _get_region_text(task, description_region).strip()
        if not description:
            continue
        event_options.append({
            "x": center_x,
            "y": center_y,
            "description": description,
            "description_region": description_region,
            "feature_name": feature_name,
            "confidence": feature_box.confidence,
        })

    if event_options:
        prefix = f"{page}: " if page else ""
        task.log_info(f"{prefix}识别到{len(event_options)}个事件选项")
        for index, event_option in enumerate(event_options, 1):
            task.log_info(
                f"{prefix}事件选项{index}: 描述=「{event_option['description']}」，"
                f"特征={event_option['feature_name']}，"
                f"置信度={event_option['confidence']:.4f}"
            )
    return event_options


def recognize_map_connections(
    task: TriggerTask,
    region=(0.019, 0.633, 0.380, 0.972),
    feature_threshold=0.85,
    line_threshold=0.30,
    special_feature_threshold=0.65,
):
    """识别小地图节点，并根据节点之间亮线的连续覆盖率生成连通关系。"""
    if task.frame is None:
        task.log_info("小地图连通关系识别失败：当前画面为空")
        return {"nodes": [], "connections": [], "adjacency": {}}

    node_types = {
        "position_in_map": "当前位置",
        "settlement_in_map": "结算",
        "enemy_in_map": "小怪",
        "safezoom_in_map": "休息",
        "elite_in_map": "精英",
        "event_in_map": "事件",
    }
    # 值大于0表示优先进入，小于0表示降低进入优先级；后续新增标志只需
    # 在这里登记，不需要改动识别和绑定逻辑。
    special_feature_priorities = {
        "kalei_in_map": 1,
        "shop_in_map": 1,
        "seal_in_map": 1,
        "hard_in_map": -1,
    }
    search_box = task.box_of_screen(*region)
    candidates = []
    for feature_name, node_type in node_types.items():
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=feature_threshold,
        ) or []:
            center_x = (feature_box.x + feature_box.width / 2) / task.width
            center_y = (feature_box.y + feature_box.height / 2) / task.height
            # 当前位置是水滴形图标，线路实际连接点在图标下方尖端而非中心。
            if feature_name == "position_in_map":
                center_y += (feature_box.height / task.height) * 0.36
            candidates.append({
                "feature_name": feature_name,
                "type": node_type,
                "x": center_x,
                "y": center_y,
                "confidence": float(feature_box.confidence),
                "feature_box": feature_box,
            })

    position_candidates = [
        candidate for candidate in candidates
        if candidate["feature_name"] == "position_in_map"
    ]
    position_x = None
    passed_feature_x_limit = None
    if position_candidates:
        position_x = max(
            position_candidates,
            key=lambda item: item["confidence"],
        )["x"]
        passed_feature_x_limit = position_x + 0.027
        original_candidate_count = len(candidates)
        candidates = [
            candidate for candidate in candidates
            if candidate["feature_name"] == "position_in_map"
            or candidate["x"] >= passed_feature_x_limit
        ]
        task.log_info(
            f"小地图当前位置X={position_x:.4f}，过滤X小于"
            f"{passed_feature_x_limit:.4f}的已走过节点，"
            f"排除{original_candidate_count - len(candidates)}个普通节点特征"
        )

    # 同一节点可能被多个普通节点模板命中。按给定的两个参考点
    # (0.229, 0.674)、(0.257, 0.674)之间的距离去重，只保留最高置信度。
    feature_dedup_distance = (
        (0.257 - 0.229) ** 2 + (0.674 - 0.674) ** 2
    ) ** 0.5
    nodes = []
    for candidate in sorted(
        candidates,
        key=lambda item: item["confidence"],
        reverse=True,
    ):
        if any(
            (
                (candidate["x"] - kept["x"]) ** 2
                + (candidate["y"] - kept["y"]) ** 2
            ) ** 0.5 < feature_dedup_distance
            for kept in nodes
        ):
            continue
        nodes.append(candidate)
    # 小地图的推进方向是从左到右。先按横坐标聚类成列，再在每列内
    # 从上到下排序，保证节点编号与实际可选顺序一致。
    columns = []
    for node in sorted(nodes, key=lambda item: item["x"]):
        column = next(
            (
                existing
                for existing in columns
                if abs(node["x"] - existing["center_x"]) < 0.025
            ),
            None,
        )
        if column is None:
            columns.append({"center_x": node["x"], "nodes": [node]})
            continue
        column["nodes"].append(node)
        column["center_x"] = sum(
            item["x"] for item in column["nodes"]
        ) / len(column["nodes"])

    nodes = []
    for column_index, column in enumerate(columns):
        column_nodes = sorted(column["nodes"], key=lambda item: item["y"])
        for row_index, node in enumerate(column_nodes, start=1):
            node["column"] = column_index
            node["row"] = row_index
            node["id"] = len(nodes)
            node["special_features"] = []
            node["special_priority"] = 0
            nodes.append(node)

    # 每个特殊标志只绑定到距离最近的一个节点。节点可以同时具有多个标志。
    special_features = []
    for feature_name, priority in special_feature_priorities.items():
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=special_feature_threshold,
        ) or []:
            special_feature = {
                "feature_name": feature_name,
                "priority": priority,
                "x": (feature_box.x + feature_box.width / 2) / task.width,
                "y": (feature_box.y + feature_box.height / 2) / task.height,
                "confidence": float(feature_box.confidence),
                "feature_box": feature_box,
            }
            if (
                passed_feature_x_limit is not None
                and special_feature["x"] < passed_feature_x_limit
            ):
                task.log_info(
                    f"小地图特殊标志{feature_name}位于已走过区域，"
                    f"X={special_feature['x']:.4f}，排除"
                )
                continue
            special_features.append(special_feature)
    filtered_special_features = []
    for special_feature in sorted(
        special_features,
        key=lambda item: item["confidence"],
        reverse=True,
    ):
        if any(
            (
                (special_feature["x"] - kept["x"]) ** 2
                + (special_feature["y"] - kept["y"]) ** 2
            ) ** 0.5 < feature_dedup_distance
            for kept in filtered_special_features
        ):
            continue
        filtered_special_features.append(special_feature)
    special_features = filtered_special_features
    for special_feature in special_features:
        nearest_node = min(
            nodes,
            key=lambda node: (
                (node["x"] - special_feature["x"]) ** 2
                + (node["y"] - special_feature["y"]) ** 2
            ),
            default=None,
        )
        if nearest_node is None:
            continue
        distance = (
            (nearest_node["x"] - special_feature["x"]) ** 2
            + (nearest_node["y"] - special_feature["y"]) ** 2
        ) ** 0.5
        if distance > 0.035:
            continue
        special_feature["node_id"] = nearest_node["id"]
        nearest_node["special_features"].append(special_feature)
        nearest_node["special_priority"] += special_feature["priority"]

    gray = cv2.cvtColor(task.frame[:, :, :3], cv2.COLOR_BGR2GRAY)
    frame_height, frame_width = gray.shape[:2]
    perpendicular_radius = max(2, round(frame_height * 0.004))

    def line_brightness_ratio(first, second):
        start_x = first["x"] * frame_width
        start_y = first["y"] * frame_height
        end_x = second["x"] * frame_width
        end_y = second["y"] * frame_height
        delta_x = end_x - start_x
        delta_y = end_y - start_y
        pixel_distance = (delta_x ** 2 + delta_y ** 2) ** 0.5
        if pixel_distance <= 0:
            return 0.0
        perpendicular_x = -delta_y / pixel_distance
        perpendicular_y = delta_x / pixel_distance
        sample_count = max(8, round(pixel_distance * 0.40))
        bright_samples = 0
        for progress in np.linspace(0.30, 0.70, sample_count):
            sample_x = start_x + delta_x * progress
            sample_y = start_y + delta_y * progress
            band_values = []
            for offset in range(-perpendicular_radius, perpendicular_radius + 1):
                pixel_x = int(round(sample_x + perpendicular_x * offset))
                pixel_y = int(round(sample_y + perpendicular_y * offset))
                if 0 <= pixel_x < frame_width and 0 <= pixel_y < frame_height:
                    band_values.append(gray[pixel_y, pixel_x])
            if band_values and max(band_values) >= 110:
                bright_samples += 1
        return bright_samples / sample_count

    connections = []
    adjacency = {node["id"]: [] for node in nodes}

    def has_intermediate_node(first, second):
        vector_x = second["x"] - first["x"]
        vector_y = second["y"] - first["y"]
        vector_length_squared = vector_x ** 2 + vector_y ** 2
        if vector_length_squared <= 0:
            return False
        for other in nodes:
            if other is first or other is second:
                continue
            progress = (
                (other["x"] - first["x"]) * vector_x
                + (other["y"] - first["y"]) * vector_y
            ) / vector_length_squared
            if not 0.12 < progress < 0.88:
                continue
            projected_x = first["x"] + vector_x * progress
            projected_y = first["y"] + vector_y * progress
            if (
                (other["x"] - projected_x) ** 2
                + (other["y"] - projected_y) ** 2
            ) ** 0.5 < 0.025:
                return True
        return False

    def has_intermediate_special_feature(first, second):
        """判断候选连线是否穿过属于第三个节点的特殊标志。"""
        vector_x = second["x"] - first["x"]
        vector_y = second["y"] - first["y"]
        vector_length_squared = vector_x ** 2 + vector_y ** 2
        if vector_length_squared <= 0:
            return False
        endpoint_ids = {first["id"], second["id"]}
        for special_feature in special_features:
            if special_feature.get("node_id") in endpoint_ids:
                continue
            progress = (
                (special_feature["x"] - first["x"]) * vector_x
                + (special_feature["y"] - first["y"]) * vector_y
            ) / vector_length_squared
            if not 0.12 < progress < 0.88:
                continue
            projected_x = first["x"] + vector_x * progress
            projected_y = first["y"] + vector_y * progress
            if (
                (special_feature["x"] - projected_x) ** 2
                + (special_feature["y"] - projected_y) ** 2
            ) ** 0.5 < 0.015:
                return True
        return False

    for first_index, first in enumerate(nodes):
        for second in nodes[first_index + 1:]:
            # 只保留当前列指向右侧相邻列的边，不生成反向邻接关系。
            if second["column"] != first["column"] + 1:
                continue
            delta_x = abs(first["x"] - second["x"])
            delta_y = abs(first["y"] - second["y"])
            distance = (delta_x ** 2 + delta_y ** 2) ** 0.5
            if not 0.035 <= distance <= 0.210 or delta_x > 0.125:
                continue
            # 地图连线为横线或斜线；不同排的同列节点不直接相连。
            if delta_y > 0.025 and delta_x < 0.025:
                continue
            if has_intermediate_node(first, second):
                continue
            if has_intermediate_special_feature(first, second):
                continue
            brightness_ratio = line_brightness_ratio(first, second)
            if brightness_ratio < line_threshold:
                continue
            connection = {
                "from": first["id"],
                "to": second["id"],
                "brightness_ratio": brightness_ratio,
            }
            connections.append(connection)
            adjacency[first["id"]].append(second["id"])

    task.log_info(f"小地图识别到{len(nodes)}个节点、{len(connections)}条亮线连接")
    for node in nodes:
        task.log_info(
            f"小地图节点{node['id']}: 类型={node['type']}，"
            f"第{node['column'] + 1}列第{node['row']}个，"
            f"位置=({node['x']:.4f}, {node['y']:.4f})，"
            f"特征={node['feature_name']}，置信度={node['confidence']:.4f}，"
            f"特殊标志={[item['feature_name'] for item in node['special_features']]}，"
            f"特殊优先级={node['special_priority']}"
        )
    for connection in connections:
        task.log_info(
            f"小地图连接: 节点{connection['from']} -> 节点{connection['to']}，"
            f"亮线覆盖率={connection['brightness_ratio']:.2%}"
        )
    task.log_info(f"小地图有向邻接关系: {adjacency}")
    return {
        "nodes": nodes,
        "connections": connections,
        "adjacency": adjacency,
    }


def find_best_map_route(map_info, target_node_type):
    """寻找目标类型节点最多的有向路线，并返回下一列应选择的节点。"""
    nodes = map_info.get("nodes", [])
    adjacency = map_info.get("adjacency", {})
    node_by_id = {node["id"]: node for node in nodes}
    current_node = next(
        (node for node in nodes if node["type"] == "当前位置"),
        None,
    )
    if current_node is None:
        return {
            "target_type": target_node_type,
            "target_count": 0,
            "special_priority_score": 0,
            "route": [],
            "next_node_id": None,
            "next_row": None,
        }

    route_cache = {}

    def best_route_from(node_id):
        if node_id in route_cache:
            return route_cache[node_id]
        node = node_by_id[node_id]
        is_target = node["type"] == target_node_type
        own_score = int(is_target)
        own_special_score = node.get("special_priority", 0) if is_target else 0
        next_node_ids = adjacency.get(node_id, [])
        if not next_node_ids:
            result = (own_score, own_special_score, [node_id])
            route_cache[node_id] = result
            return result

        candidates = []
        for next_node_id in next_node_ids:
            child_score, child_special_score, child_route = best_route_from(
                next_node_id
            )
            candidates.append((
                own_score + child_score,
                own_special_score + child_special_score,
                node_by_id[next_node_id]["row"],
                [node_id, *child_route],
            ))
        # 先比较目标节点数量，再比较目标节点携带的特殊优先级；仍相同时
        # 选择下一节点更靠上的路线。
        best_score, best_special_score, _, best_route = min(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        )
        result = (best_score, best_special_score, best_route)
        route_cache[node_id] = result
        return result

    target_count, special_priority_score, route = best_route_from(
        current_node["id"]
    )
    next_node = node_by_id[route[1]] if len(route) > 1 else None
    return {
        "target_type": target_node_type,
        "target_count": target_count,
        "special_priority_score": special_priority_score,
        "route": route,
        "next_node_id": next_node["id"] if next_node else None,
        "next_row": next_node["row"] if next_node else None,
    }


def find_best_map_route_by_priority(map_info, node_type_priority):
    """按节点类型加权计算最优路线，并返回下一列应选择的节点。"""
    nodes = map_info.get("nodes", [])
    adjacency = map_info.get("adjacency", {})
    node_by_id = {node["id"]: node for node in nodes}
    current_node = next(
        (node for node in nodes if node["type"] == "当前位置"),
        None,
    )
    if current_node is None:
        return None

    route_cache = {}
    priority_weights = {
        node_type: len(node_type_priority) - index
        for index, node_type in enumerate(node_type_priority)
    }
    highest_priority_weight = max(priority_weights.values(), default=1)
    shop_bonus = highest_priority_weight * 2
    # 优先级列表里写了「商店」时，带商店标志的节点改按「商店」自己的排名计分，
    # 和休息/小怪这些一样只是一项；没写时沿用旧的固定加成（商店永远最优先），
    # 保证老配置行为不变。
    shop_in_priority = "商店" in node_type_priority

    def best_route_from(node_id):
        if node_id in route_cache:
            return route_cache[node_id]
        node = node_by_id[node_id]
        has_shop = any(
            item["feature_name"] == "shop_in_map"
            for item in node.get("special_features", [])
        )
        scoring_type = "商店" if (shop_in_priority and has_shop) else node["type"]
        own_counts = tuple(
            int(scoring_type == node_type)
            for node_type in node_type_priority
        )
        own_special_score = sum(
            item["priority"]
            for item in node.get("special_features", [])
            if item["feature_name"] != "shop_in_map"
        )
        own_weighted_score = (
            priority_weights.get(scoring_type, 0) + own_special_score
        )
        if has_shop and not shop_in_priority:
            own_weighted_score += shop_bonus
        # 商店已经进优先级列表时不再额外给"多带商店"的加分，避免和配置打架。
        own_shop_count = int(has_shop) if not shop_in_priority else 0
        next_node_ids = adjacency.get(node_id, [])
        if not next_node_ids:
            result = (
                own_weighted_score,
                own_shop_count,
                own_counts,
                own_special_score,
                [node_id],
            )
            route_cache[node_id] = result
            return result

        candidates = []
        for next_node_id in next_node_ids:
            (
                child_weighted_score,
                child_shop_count,
                child_counts,
                child_special_score,
                child_route,
            ) = best_route_from(next_node_id)
            total_counts = tuple(
                own + child
                for own, child in zip(own_counts, child_counts)
            )
            candidates.append((
                own_weighted_score + child_weighted_score,
                own_shop_count + child_shop_count,
                total_counts,
                own_special_score + child_special_score,
                node_by_id[next_node_id]["row"],
                [node_id, *child_route],
            ))
        (
            best_weighted_score,
            best_shop_count,
            best_counts,
            best_special_score,
            _,
            best_route,
        ) = min(
            candidates,
            key=lambda item: (
                -item[0],
                -item[1],
                tuple(-count for count in item[2]),
                -item[3],
                item[4],
            ),
        )
        result = (
            best_weighted_score,
            best_shop_count,
            best_counts,
            best_special_score,
            best_route,
        )
        route_cache[node_id] = result
        return result

    (
        weighted_score,
        shop_count,
        type_counts,
        special_priority_score,
        route,
    ) = best_route_from(current_node["id"])
    next_node = node_by_id[route[1]] if len(route) > 1 else None
    return {
        "priority": list(node_type_priority),
        "priority_weights": priority_weights,
        "shop_bonus": shop_bonus,
        "weighted_score": weighted_score,
        "shop_count": shop_count,
        "type_counts": dict(zip(node_type_priority, type_counts)),
        "special_priority_score": special_priority_score,
        "route": route,
        "next_node_id": next_node["id"] if next_node else None,
        "next_row": next_node["row"] if next_node else None,
        "next_node_type": next_node["type"] if next_node else None,
        "next_special_features": [
            item["feature_name"]
            for item in next_node.get("special_features", [])
        ] if next_node else [],
    }


def _mark_selected_card_by_gold_border(
    task: TriggerTask,
    cards,
    page="",
    threshold=0.25,
):
    """计算卡牌的金黄色边框得分，并在卡牌信息中写入选中状态。"""
    for card in cards:
        card["selected"] = False
        card["gold_border_score"] = 0.0
        card["gold_border_edges"] = {}
    if not cards or task.frame is None:
        return

    frame = task.frame[:, :, :3]
    frame_height, frame_width = frame.shape[:2]
    band_x = max(2, round(frame_width * 0.004))
    band_y = max(2, round(frame_height * 0.006))

    def gold_ratio(left, top, right, bottom):
        left = max(0, min(frame_width, round(left)))
        right = max(0, min(frame_width, round(right)))
        top = max(0, min(frame_height, round(top)))
        bottom = max(0, min(frame_height, round(bottom)))
        if right <= left or bottom <= top:
            return 0.0
        hsv = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2HSV)
        gold_mask = cv2.inRange(
            hsv,
            np.array((8, 100, 160), dtype=np.uint8),
            np.array((38, 255, 255), dtype=np.uint8),
        )
        return float(cv2.countNonZero(gold_mask)) / gold_mask.size

    scored_cards = []
    for card in cards:
        feature_box = card["feature_box"]
        center_x = feature_box.x + feature_box.width / 2
        center_y = feature_box.y + feature_box.height / 2
        card_left = center_x - frame_width * 0.047
        card_right = center_x + frame_width * 0.103
        card_top = center_y - frame_height * 0.061
        card_bottom = center_y + frame_height * 0.330

        edge_scores = {
            "上": gold_ratio(
                card_left, card_top - band_y, card_right, card_top + band_y
            ),
            "下": gold_ratio(
                card_left, card_bottom - band_y, card_right, card_bottom + band_y
            ),
            "左": gold_ratio(
                card_left - band_x, card_top, card_left + band_x, card_bottom
            ),
            "右": gold_ratio(
                card_right - band_x, card_top, card_right + band_x, card_bottom
            ),
        }
        visible_edge_scores = [
            edge_scores["左"],
            edge_scores["右"],
        ]
        score = sum(visible_edge_scores) / len(visible_edge_scores)
        strong_edge_count = sum(value >= 0.08 for value in visible_edge_scores)
        card["gold_border_score"] = score
        card["gold_border_edges"] = edge_scores
        scored_cards.append((score, strong_edge_count, card))

    prefix = f"{page}: " if page else ""
    for score, strong_edge_count, card in scored_cards:
        if score >= threshold and strong_edge_count == 2:
            card["selected"] = True

    for card in cards:
        edge_scores = card["gold_border_edges"]
        task.log_info(
            f"{prefix}卡牌「{card['name']}」是否选中={card['selected']}，"
            f"金色边框得分={card['gold_border_score']:.4f}，"
            f"上={edge_scores['上']:.4f}，下={edge_scores['下']:.4f}，"
            f"左={edge_scores['左']:.4f}，右={edge_scores['右']:.4f}"
        )

    if not any(card["selected"] for card in cards):
        task.log_info(f"{prefix}未检测到选中卡牌的金色边框")


def find_text(task: TriggerTask, pattern):
    """按正则在所有识别文本中查找第一个匹配的 box。"""
    return next((b for b in task.all_texts if re.search(pattern, b.name)), None)


def _clean_match(name, target):
    """去除OCR文本中的非中文/字母/数字符号后比较是否等于 target。"""
    cleaned = re.sub(r'[^\u4e00-\u9fff\w]', '', name)
    return cleaned == target


def _get_region_text(task: TriggerTask, region):
    """获取指定区域内所有OCR文本，去除空白后用"".join拼接返回。"""
    x1, y1, x2, y2 = region
    texts = [
        b.name.strip() for b in task.all_texts
        if x1 <= (b.x + b.width / 2) / task.width <= x2
        and y1 <= (b.y + b.height / 2) / task.height <= y2
        and b.name.strip()
    ]
    return "".join(texts)


def _region_text_debug_info(task: TriggerTask, region):
    """返回参与区域文本拼接的OCR框信息，用于排查相对区域偏移。"""
    x1, y1, x2, y2 = region
    matched = []
    for box in task.all_texts:
        center_x = (box.x + box.width / 2) / task.width
        center_y = (box.y + box.height / 2) / task.height
        if x1 <= center_x <= x2 and y1 <= center_y <= y2 and box.name.strip():
            matched.append(
                f"「{box.name.strip()}」"
                f"(中心={center_x:.4f},{center_y:.4f},"
                f"置信度={box.confidence:.4f})"
            )
    return "，".join(matched) if matched else "无"


_CARD_TYPE_KEYWORDS = {
    "攻击", "强化", "技能", "技", "咒术", "诅咒",
    "攻", "击", "基础", "基本", "状态", "异常",
}


def _card_has_type_below(task: TriggerTask, box):
    """判断文本框下方是否有卡牌类型标签（卡牌名特征）。"""
    box_bottom_y = (box.y + box.height) / task.height
    box_cx = (box.x + box.width / 2) / task.width
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        dy = cy - box_bottom_y
        dx = abs(cx - box_cx)
        if -0.005 <= dy <= 0.040 and dx <= 0.045 and len(b.name) <= 4:
            for kw in _CARD_TYPE_KEYWORDS:
                if kw in b.name:
                    return True
    return False


def region_white_ratio(task: TriggerTask, region):
    """计算指定区域内白色像素占比。"""
    if task.frame is None:
        return 1.0
    region_box = task.box_of_screen(*region)
    pixels = task.frame[
        region_box.y:region_box.y + region_box.height,
        region_box.x:region_box.x + region_box.width,
        :3,
    ]
    if pixels.size == 0:
        return 1.0
    channel_min = pixels.min(axis=2)
    channel_max = pixels.max(axis=2)
    white_mask = (channel_min >= 240) & ((channel_max - channel_min) <= 15)
    return float(np.count_nonzero(white_mask)) / white_mask.size


def _point_is_white(task: TriggerTask, x, y, page):
    """判断指定点是否为选牌页面滚动条使用的白色。"""
    pixel_x = min(task.width - 1, max(0, round(x * task.width)))
    pixel_y = min(task.height - 1, max(0, round(y * task.height)))
    blue, green, red = (
        int(value) for value in task.frame[pixel_y, pixel_x, :3]
    )
    is_white = min(blue, green, red) >= 240 and (
        max(blue, green, red) - min(blue, green, red)
    ) <= 15
    task.log_info(
        f"{page}: 点({x:.3f}, {y:.3f})颜色="
        f"B{blue}/G{green}/R{red}，是否白色={is_white}"
    )
    return is_white


def _scroll_card_page(task: TriggerTask, x, y, amount, page, distance=0.25):
    """将鼠标移到选牌区域后滚动。"""
    direction = "向下" if amount < 0 else "向上"
    if task.is_adb():
        to_y = max(0.05, y - distance) if amount < 0 else min(0.95, y + distance)
        task.log_info(
            f"{page}: ADB从({x:.3f}, {y:.3f})滑动到({x:.3f}, {to_y:.3f})，"
            f"{direction}浏览卡牌"
        )
        task.swipe_relative(x, y, x, to_y, duration=1, settle_time=1)
        task.sleep(1)
    else:
        task.log_info(f"{page}: 在({x:.3f}, {y:.3f}){direction}滚动")
        task.move_relative(x, y)
        task.sleep(0.05)
        task.scroll_relative(x, y, amount)
        task.sleep(0.5)


def select_card(task: TriggerTask, card_names, count=1, action=""):
    """使用卡组特征识别选择卡牌，支持滚动查找、基础牌移除和兜底选择。"""
    selected = 0
    max_scrolls = 20
    page = f"select_card-{action}" if action else "select_card"
    prefer_remove_base = (
        action == "移除"
        and _get_config_value(task, "优先移除基础牌", True)
    )
    prefer_target_member_row = (
        action == "移除"
        and _get_config_value(task, "刷空档", False) is True
    )
    base_card_type = _get_game_text(task, "基础")
    target_member_box = task.box_of_screen(0.079, 0.092, 0.209, 0.675)
    flash_priority = (
        _get_card_list(task, "闪光优先级")
        if action in ("闪光", "灵光")
        else []
    )

    def record_pending_removal():
        if action == "移除":
            task._pending_removed_card_count = (
                getattr(task, "_pending_removed_card_count", 0) + 1
            )

    def filter_flash_priority_cards(cards):
        """闪光时排除已命中闪光优先级的卡牌，避免重复选择。"""
        if not flash_priority:
            return cards

        filtered_cards = []
        for card in cards:
            combined_text = f"{card['name']}：:{card['description']}"
            matched_keyword = next(
                (
                    keyword.strip()
                    for keyword in flash_priority
                    if isinstance(keyword, str)
                    and keyword.strip()
                    and is_subsequence(keyword.strip(), combined_text)
                ),
                None,
            )
            if matched_keyword:
                task.log_info(
                    f"{page}: 卡牌「{card['name']}」的名称和描述命中"
                    f"闪光优先级「{matched_keyword}」，排除该卡牌"
                )
                continue
            filtered_cards.append(card)
        return filtered_cards

    def refresh_cards():
        task.all_texts = _simplify_texts(task.ocr())
        cards = recognize_cards_in_deck(task, page=page)
        return filter_flash_priority_cards(cards)

    def click_cards(cards, predicate, reason):
        nonlocal selected
        clicked = False
        for card in cards:
            if selected >= count:
                break
            if card["selected"] or not predicate(card):
                continue
            task.log_info(f"{page}: {reason}「{card['name']}」")
            _move_and_click(task, card["x"], card["y"])
            task.sleep(0.3)
            card["selected"] = True
            selected += 1
            record_pending_removal()
            clicked = True
        return clicked

    def click_priority_cards(cards):
        nonlocal selected
        clicked = False
        for target in card_names:
            if selected >= count:
                break
            target = target.strip() if isinstance(target, str) else ""
            if not target:
                continue
            for card in cards:
                if selected >= count:
                    break
                if card["selected"]:
                    continue
                card_name = card["name"].strip()
                if target not in card_name and card_name not in target:
                    continue
                task.log_info(
                    f"{page}: 命中优先级「{target}」，点击目标卡牌「{card['name']}」"
                )
                _move_and_click(task, card["x"], card["y"])
                task.sleep(0.3)
                card["selected"] = True
                selected += 1
                record_pending_removal()
                clicked = True
        return clicked

    def click_target_member_row_cards(cards):
        """刷空档时优先移除目标主战员同一排的卡牌。"""
        if not prefer_target_member_row or selected >= count:
            return False
        if not task.feature_exists("target_member_in_select_card"):
            return False
        target_member = task.find_one(
            feature_name="target_member_in_select_card",
            box=target_member_box,
            threshold=0.6,
        )
        if not target_member:
            return False
        target_y = (
            target_member.y + target_member.height / 2
        ) / task.height
        task.log_info(
            f"{page}: 刷空档找到目标主战员，相似度="
            f"{target_member.confidence:.4f}，中心Y={target_y:.4f}"
        )
        return click_cards(
            cards,
            lambda card: abs(card["y"] - target_y) <= 0.25,
            "刷空档优先移除目标主战员同排卡牌，点击",
        )

    def sync_visible_selected(cards):
        nonlocal selected
        if selected == 0:
            selected = min(count, sum(card["selected"] for card in cards))

    def find_action_button():
        if not action:
            return None
        action_text = _get_game_text(task, action)
        return next(
            (
                box for box in task.all_texts
                if 0.495 <= (box.x + box.width / 2) / task.width <= 0.997
                and 0.878 <= (box.y + box.height / 2) / task.height <= 1.001
                and action_text in box.name
            ),
            None,
        )

    cards = refresh_cards()
    if not cards:
        if find_action_button():
            task.log_info(
                f"{page}: 首次识别卡牌漏识别，但仍存在「{action}」按钮，"
                "继续执行选卡流程"
            )
        else:
            task.log_info(f"{page}: 未识别到任何卡牌或操作按钮，终止选卡")
            return False
    sync_visible_selected(cards)
    scrollbar_white_ratio = region_white_ratio(
        task, (0.976, 0.119, 0.988, 0.858)
    )
    single_page = scrollbar_white_ratio < 0.01
    task.log_info(
        f"{page}: 滚动条区域白色像素占比={scrollbar_white_ratio:.2%}，"
        f"是否仅一页卡牌={single_page}"
    )

    down_scrolls = 0
    while True:
        click_target_member_row_cards(cards)
        if selected >= count:
            task.log_info(f"{page}: 已选中{selected}/{count}张卡牌")
            return True
        click_priority_cards(cards)
        if selected >= count:
            task.log_info(f"{page}: 已选中{selected}/{count}张卡牌")
            return True

        if single_page:
            task.log_info(f"{page}: 当前仅一页卡牌，不执行向下滚动")
            break

        if _point_is_white(task, 0.982, 0.846, page):
            task.log_info(f"{page}: 检测到已到达卡牌底部")
            break

        if down_scrolls >= max_scrolls:
            task.log_info(f"{page}: 向下滚动已达到{max_scrolls}次限制")
            break

        _scroll_card_page(task, 0.251, 0.735, -3, page)
        down_scrolls += 1
        cards = refresh_cards()
        if not cards:
            if find_action_button():
                task.log_info(
                    f"{page}: 向下滚动后卡牌漏识别，但仍存在「{action}」按钮，"
                    "继续向下滚动"
                )
                continue
            task.log_info(f"{page}: 向下滚动后未识别到卡牌或操作按钮，终止选卡")
            return False

    if action == "移除" and selected < count:
        bottom_to_top_cards = sorted(
            cards,
            key=lambda card: (card["y"], card["x"]),
            reverse=True,
        )
        click_cards(
            bottom_to_top_cards,
            lambda card: card["feature_name"] in {
                "hex_in_deck",
                "hex_in_deck_tw",
            },
            "底部页面优先移除咒术卡牌，点击",
        )
        if selected >= count:
            return True

    if prefer_remove_base and selected < count:
        bottom_to_top_cards = sorted(
            cards,
            key=lambda card: (card["y"], card["x"]),
            reverse=True,
        )
        click_cards(
            bottom_to_top_cards,
            lambda card: base_card_type in card["type"],
            "底部页面优先移除基础牌，点击",
        )
        if selected >= count:
            return True

        up_scrolls = 0
        while not single_page:
            if _point_is_white(task, 0.982, 0.128, page):
                task.log_info(f"{page}: 检测到已到达卡牌顶部")
                break

            if up_scrolls >= max_scrolls:
                task.log_info(f"{page}: 向上滚动已达到{max_scrolls}次限制")
                break

            _scroll_card_page(task, 0.252, 0.179, 3, page)
            up_scrolls += 1
            cards = refresh_cards()
            if not cards:
                if find_action_button():
                    task.log_info(
                        f"{page}: 向上滚动后卡牌漏识别，但仍存在「{action}」按钮，"
                        "继续向上滚动"
                    )
                    continue
                task.log_info(f"{page}: 向上滚动后未识别到卡牌或操作按钮，终止选卡")
                return False
            click_target_member_row_cards(cards)
            if selected >= count:
                task.log_info(f"{page}: 已选中{selected}/{count}张卡牌")
                return True
            bottom_to_top_cards = sorted(
                cards,
                key=lambda card: (card["y"], card["x"]),
                reverse=True,
            )
            click_cards(
                bottom_to_top_cards,
                lambda card: base_card_type in card["type"],
                "向上翻页找到基础牌，点击",
            )
            if selected >= count:
                return True

    task.all_texts = _simplify_texts(task.ocr())
    action_box = task.box_of_screen(0.424, 0.882, 1.000, 0.999)
    for button_name in ("跳过", "取消"):
        button = next(
            (
                box for box in task.all_texts
                if action_box.x <= box.x + box.width / 2 <= action_box.x + action_box.width
                and action_box.y <= box.y + box.height / 2 <= action_box.y + action_box.height
                and button_name in box.name
            ),
            None,
        )
        if button:
            task.log_info(f"{page}: 未找到足够卡牌，点击「{button_name}」")
            task.click_box(button)
            if action == "移除":
                task._pending_removed_card_count = 0
                # 这次删卡放弃了：把商店删卡的标记一并清掉，免得下一次事件/
                # 三选一删卡被误记成商店删卡。
                _consume_pending_shop_removal(task)
            return True

    cards = recognize_cards_in_deck(task, page=f"{page}-兜底")
    cards = filter_flash_priority_cards(cards)
    fallback_cards = sorted(
        cards,
        key=lambda card: (card["y"], card["x"]),
        reverse=True,
    )
    click_cards(fallback_cards, lambda card: True, "兜底补选卡牌，点击")
    task.log_info(f"{page}: 兜底处理完成，已选中{selected}/{count}张卡牌")
    return True


def calculate_dominant_hue(task: TriggerTask, region):
    """计算区域的主导色相，返回色相值(0-179)，无有效色相返回-1。"""
    box = task.box_of_screen(*region)
    frame = task.frame[box.y:box.y + box.height, box.x:box.x + box.width, :3]
    hue, sat, val = cv2.split(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV))

    valid_hue = hue[(sat > 30) & (val > 30)]
    if len(valid_hue) == 0:
        return -1

    hist = cv2.calcHist([valid_hue.astype(np.float32)], [0], None, [180], [0, 180])
    return int(np.argmax(hist))


def is_button_active(task: TriggerTask, button_box):
    """判断按钮是否处于可点击状态（激活状态）。

    参数:
        task: TriggerTask实例
        button_box: 按钮文本的Box对象（像素坐标）

    返回:
        bool: True表示按钮可点击（激活），False表示不可点击（未激活/灰色）
    """
    # 计算左侧检测区域（按钮图标/背景区域）
    # 根据按钮文本与左侧图标区域的比例关系计算：
    # 按钮box: (0.898, 0.908, 0.941, 0.950) w=0.043, h=0.042
    # 左侧区域: (0.866, 0.912, 0.895, 0.947) w=0.029, h=0.035
    # 左侧区域宽度 = 按钮宽度 * 0.67，x = 按钮x - 左侧区域宽度 * 1.1
    # 左侧区域高度 = 按钮高度 * 0.83，y = 按钮y + 按钮高度 * 0.1

    left_width = int(button_box.width * 0.67)
    left_height = int(button_box.height * 0.83)
    left_x = button_box.x - int(left_width * 1.1)
    left_y = button_box.y + int(button_box.height * 0.1)

    # 确保区域在屏幕内
    if left_x < 0:
        left_x = 0
    if left_y < 0:
        left_y = 0
    if left_x + left_width > task.width:
        left_width = task.width - left_x
    if left_y + left_height > task.height:
        left_height = task.height - left_y

    if left_width <= 0 or left_height <= 0:
        task.log_info(f"按钮左侧区域无效: ({left_x}, {left_y}, {left_width}, {left_height})")
        return False

    # 提取区域图像
    region_img = task.frame[left_y:left_y + left_height, left_x:left_x + left_width, :3]
    if region_img.size == 0:
        task.log_info("按钮左侧区域图像为空")
        return False

    # 计算平均BGR颜色
    avg_color = cv2.mean(region_img)[:3]  # B, G, R 平均值
    avg_b, avg_g, avg_r = avg_color

    # 判断是否接近禁用灰色 (195,195,195)
    # 容错范围：每个通道在190-200之间，且三个通道值接近
    # target_gray = 195
    tolerance = 5  # 允许±5的误差

    # 计算范围边界
    lower_bound = 120 #target_gray - tolerance  # 190
    upper_bound = 200 #target_gray + tolerance  # 200

    # 检查每个通道是否在目标范围内
    in_range = (
        lower_bound <= avg_b <= upper_bound and
        lower_bound <= avg_g <= upper_bound and
        lower_bound <= avg_r <= upper_bound
    )

    # 检查三个通道是否接近（最大差异小）
    max_diff = max(abs(avg_b - avg_g), abs(avg_g - avg_r), abs(avg_r - avg_b))
    is_close = max_diff < tolerance

    # 如果是接近(195,195,195)的灰色，按钮不可点击
    is_disabled_gray = in_range and is_close

    task.log_info(f"按钮左侧区域颜色: B={avg_b:.1f}, G={avg_g:.1f}, R={avg_r:.1f}, "
                  f"是否禁用灰色={is_disabled_gray} (范围{lower_bound}-{upper_bound}, 最大差异={max_diff:.1f})")

    # 如果是禁用灰色，按钮不可点击；否则可点击
    return not is_disabled_gray


# def group_dialog_columns(task: TriggerTask, region, max_width_ratio=0.25, align_tolerance=0.04):
#     """把区域内文本框按左边缘聚成对话框列。"""
#     x1, y1, x2, y2 = region
#     boxes = [
#         box for box in task.all_texts
#         if x1 <= (box.x + box.width / 2) / task.width <= x2
#         and y1 <= (box.y + box.height / 2) / task.height <= y2
#         and box.width / task.width <= max_width_ratio
#         and len(box.name) > 2
#     ]
#     columns = []
#     for box in sorted(boxes, key=lambda item: item.x):
#         left = box.x / task.width
#         center_x = (box.x + box.width / 2) / task.width
#         if columns and left - columns[-1]["left"] <= align_tolerance:
#             columns[-1]["centers"].append(center_x)
#             columns[-1]["texts"].append(box.name)
#         else:
#             columns.append({"left": left, "centers": [center_x], "texts": [box.name]})
#     return [
#         {"x": sum(column["centers"]) / len(column["centers"]), "texts": column["texts"]}
#         for column in columns
#     ]


# ------------------------- 帧卡住检测 -------------------------

def is_frame_stuck(task: TriggerTask, stuck_threshold_seconds=30, change_threshold=0.08):
    """
    基于像素变化检测画面是否卡住。
    在 task 上缓存 _prev_frame_gray 和 _last_change_time。
    连续 stuck_threshold_seconds 秒变化比例低于 change_threshold 返回 True。
    stuck_threshold_seconds: 判定卡住的连续秒数阈值，默认30秒
    change_threshold: 两帧之间变化像素比例阈值，默认0.08（8%）
    """
    if not hasattr(task, '_last_change_time'):
        task._last_change_time = time.time()
        task._prev_frame_gray = None

    frame = task.frame
    if frame is None:
        return False

    # 缩放灰度图以减少计算量
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (w // 4, h // 4))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    if task._prev_frame_gray is not None and gray.shape == task._prev_frame_gray.shape:
        diff = cv2.absdiff(gray, task._prev_frame_gray)
        _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
        change_ratio = cv2.countNonZero(thresh) / (gray.shape[0] * gray.shape[1])

        if change_ratio >= change_threshold:
            task._last_change_time = time.time()

    task._prev_frame_gray = gray

    return time.time() - task._last_change_time >= stuck_threshold_seconds


def handle_stuck_log(task: TriggerTask):
    """画面卡住超过10秒时依次处理关闭页、特殊怪物、卡牌或未知页面。"""
    if not is_frame_stuck(task, stuck_threshold_seconds=10):
        return False

    stuck_seconds = int(time.time() - task._last_change_time)
    close_page = task.find_one(
        feature_name="close_page",
        box=task.box_of_screen(0.921, 0.003, 0.998, 0.100),
    )
    if close_page:
        task.log_info(
            f"画面卡住已持续{stuck_seconds}秒，检测到close_page特征，点击关闭页面"
        )
        task.click_box(close_page)
        task.sleep(1)
        return True

    from utils_sortie import handle_secret_enemy
    handle_secret_enemy(task)
    cards = recognize_cards(task, page="画面卡住兜底")
    if cards:
        chosen_card = random.choice(cards)
        task.log_info(
            f"画面卡住兜底: 随机点击卡牌「{chosen_card['name']}」"
        )
        _move_and_click(task, chosen_card["x"], chosen_card["y"])
    else:
        handle_unknown_page(task)
    # 普通随机点屏幕兜底暂时停用。
    # click_x = random.uniform(0.059, 0.985)
    # click_y = random.uniform(0.129, 0.981)
    # _move_and_click(task, click_x, click_y)
    task.log_info(f"画面卡住，已持续{stuck_seconds}秒")
    return False


# ------------------------- 页面处理函数（通用） -------------------------
# 约定: 每个函数处理一种页面, 处理成功返回 True, 未命中返回 False。

def handle_auto_stop(task: TriggerTask):
    """自动停止功能: 如果配置"几轮后停止(0为不停止)"不为0，
    且 node_status 中的 total_rounds 达到配置轮数，则自动 disable 当前任务。"""
    stop_rounds = _get_config_value(task, '几轮后停止(0为不停止)', 0)
    if stop_rounds and stop_rounds != 0:
        ns = getattr(task, 'node_status', None)
        if ns and ns.get('total_rounds', 0) >= stop_rounds:
            task.log_info(f"已达到配置的停止轮数 {stop_rounds}，当前 total_rounds={ns['total_rounds']}，自动停止任务")
            task.disable()
            return True
    return False


def log_credit(task: TriggerTask):
    """记录当前信用点数量（仅记录, 不拦截后续处理）。"""
    credit = _get_current_credit(task)
    if credit > 0:
        task.info_set("当前信用点", f"{credit}")
    return False


# def handle_stage_clear(task: TriggerTask):
#     """成功通关页面: 检测(0.142,0.806)处文本是否包含'战斗结束'，成功次数+1。"""
#     box = find_box_at_point(task, 0.142, 0.806)
#     if box and "战斗结束" in box.name:
#         task.log_info("检测到成功通关页面，success_rounds + 1")
#         if hasattr(task, 'node_status'):
#             task.node_status['success_rounds'] += 1
#     return False


def log_node_status(task: TriggerTask):
    """记录当前胜率（仅记录, 不拦截后续处理）。"""
    ns = getattr(task, 'node_status', None)
    if ns:
        try:
            from src.config import version
            app_version = str(version).strip() or "dev"
        except Exception:
            app_version = "dev"
        task.info_set("版本号", app_version)
        task.info_set("游戏语言", _get_game_language(task))
        total = ns.get('total_rounds', 0)
        node_count = ns.get('node_count', 0)
        node_type = ns.get('node_type', "")
        task.info_set("所处层数，节点，类型", f"第{ns['pass_final_boss_count']+1}层，第{node_count}节点，{node_type}")
        task.info_set("是否到达关底boss", f"{ns['reach_final_boss']}")
        task.info_set("是否进入关底boss战斗", f"{ns['final_boss_battle']}")
        task.info_set("是否已逃脱", f"{ns['is_escaped']}")
        task.info_set(
            "是否已获得特定闪光",
            ns.get("get_specific_flash", False),
        )
        task.info_set(
            "获取刷存档主战员头像",
            ns.get("save_target_member", False),
        )
        task.info_set("本局已移除卡牌", ns.get("removed_card_count", 0))
        task.info_set("本局已获得中立牌", ns.get("neutral_card_count", 0))
        equipment = _equipment_state(task)
        equipment_names = [
            _current_equipment_for_slot(task, equipment, slot)[0]
            for slot in range(3)
        ]
        task.info_set(
            "装备信息",
            "，".join(
                f"{slot + 1}号位：{name or '空'}"
                for slot, name in enumerate(equipment_names)
            ),
        )
        meditation_state = _member_deck_state(task).get("冥想", {})
        if isinstance(meditation_state, dict):
            for card_name, pending in meditation_state.items():
                task.info_set(f"冥想：{card_name}", pending)
        if total > 0:
            task.info_set("当前胜率", f"{ns['success_rounds']}/{total} ({ns['success_rounds']*100//total}%)")
        else:
            task.info_set("当前胜率",f"{ns['success_rounds']}/{total} NaN")
        task.log_info("")
    return False


def handle_battle_crash(task: TriggerTask):
    """战斗信息错乱 / 点击重试: 点击屏幕中央恢复。"""
    if (find_text(task, r'出现错乱')
            or find_text(task, r'点击重试')
            or find_text(task, r'通讯不稳定.*重新尝试')):
        task.log_info("战斗信息出现错乱，点击恢复")
        _move_and_click(task, 0.5, 0.5)
        return True
    return False


def handle_close_page(task: TriggerTask):
    """提示"点击屏幕事件": 点击屏幕。"""
    box = find_text(task, _get_game_text(task, '点击屏幕'))
    if box:
        task.log_info("点击屏幕事件，点击屏幕")
        task.click_box(box)
        return True
    return False


def handle_refine_equipment_credit(task: TriggerTask):
    """提炼装备信用点页面：点击“以信用点接收”。"""
    box = find_box_at_point(task, 0.598, 0.635)
    receive_text = _get_game_text(task, "以信用点接收")
    if box and receive_text in box.name:
        task.log_info(f"检测到提炼装备信用点页面，点击{receive_text}")
        task.click_box(box)
        task.sleep(0.5)
        return True
    return False


def handle_center_confirm(task: TriggerTask):
    """页面中央的"确认"按钮。"""
    confirm_region = (0.009, 0.168, 0.977, 0.875)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _clean_match(text_box.name, "确认")
            and confirm_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= confirm_region[2]
            and confirm_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= confirm_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到页面中央确认按钮，点击确认")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_settlement(task: TriggerTask):
    """点击"结算"按钮。"""
    box = find_box_at_point(task, 0.941, 0.917)
    if box and _clean_match(box.name, "结算"):
        _move_and_click(task, 0.941, 0.917)
        if hasattr(task, 'node_status') and task.node_status.get('reach_final_boss', False):
            task.node_status['pass_final_boss_count'] += 1
            passed = task.node_status['pass_final_boss_count']
            task.log_info(f"检测到boss结算页面且 reach_final_boss=True，通关层数+1 (当前: {passed})")
            reset_layer_status(task)
        task.sleep(1)
        return True
    return False


def handle_skip(task: TriggerTask):
    """点击"跳过"按钮。"""
    box = find_box_at_point(task, 0.941, 0.917)
    if box and _clean_match(box.name, "跳过"):
        task.log_info("跳过页面触发跳过事件，点击「跳过」按钮")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_destiny_choice(task: TriggerTask):
    """命运选择奖励页面: 随机选择一个命运标题。"""
    box = find_box_at_point(task, 0.499, 0.932)
    if box and _get_game_text(task, '请选择你的命运') in box.name:
        task.log_info("检测到命运选择奖励，进行相应操作")
        task.sleep(2)  # 给按钮一些加载时间

        # # 检查确认按钮是否已处于激活状态
        # # 在确认按钮点击位置附近查找"确认"文本
        # confirm_box = find_box_at_point(task, 0.884, 0.931)
        # if confirm_box and confirm_box.name == "确认":
        #     if is_button_active(task, confirm_box):
        #         task.log_info("确认按钮已激活，跳过选择（由其他逻辑处理确认）")
        #         return False  # 按钮已激活，不处理，让其他逻辑点击确认
        # 在命运标题区域随机选择一个
        titles = [
            b for b in task.all_texts
            if 0.202 <= (b.x + b.width / 2) / task.width <= 0.800
            and 0.474 <= (b.y + b.height / 2) / task.height <= 0.600
            and len(b.name.strip()) > 1
            and b.name not in ["确认", "返回", "跳过"]
        ]
        if titles:
            chosen = random.choice(titles)
            task.log_info(f"随机选择命运: {chosen.name}")
            task.click_box(chosen)
            task.sleep(1)
            # 选择命运后不点击确认按钮，返回False让其他逻辑处理
            return True
    return False


def _prioritize_target_member_click(task: TriggerTask, click_positions, search_region):
    """匹配目标成员头像，并将距离最近的候选点击位置移到最前。"""
    if not click_positions:
        task.log_info("刷存档主战员匹配失败：没有可绑定的候选点击位置")
        return click_positions
    if not task.feature_exists("target_member_large"):
        task.log_info(
            "刷存档主战员匹配跳过：尚未保存target_member_large头像特征，"
            "保持原主战员点击顺序"
        )
        return click_positions
    target_member = task.find_one(
        feature_name="target_member_large",
        box=task.box_of_screen(*search_region),
        threshold=0.35,
    )
    if not target_member:
        task.log_info(
            "刷存档主战员头像匹配失败："
            f"在区域{search_region}内未找到target_member_large（阈值=0.35），"
            "保持原主战员点击顺序"
        )
        return click_positions

    center_x = (target_member.x + target_member.width / 2) / task.width
    center_y = (target_member.y + target_member.height / 2) / task.height
    target_position = min(
        click_positions,
        key=lambda position: (
            (position[0] - center_x) ** 2 + (position[1] - center_y) ** 2
        ),
    )
    task.log_info(
        f"刷存档主战员头像匹配成功，相似度={target_member.confidence:.4f}，"
        f"绑定点击位置{target_position}并设为最高优先级"
    )
    return [target_position] + [
        position for position in click_positions if position != target_position
    ]


def handle_main_member_flash(task: TriggerTask):
    """主战员闪光选择页面: 依次尝试主战员，直到出现确认特征。"""
    box = find_box_at_point(task, 0.495, 0.936)
    if not (box and _get_game_text(task, "请选择获得") in box.name):
        return False

    task.log_info("检测主战员闪光选择，进行相应操作")
    confirm_box = task.box_of_screen(0.145, 0.044, 0.856, 0.214)
    click_positions = [(0.228, 0.510), (0.504, 0.504), (0.755, 0.508)]
    click_positions = _prioritize_target_member_click(
        task, click_positions, (0.173, 0.232, 0.858, 0.508)
    )

    for cx, cy in click_positions:
        task.log_info(f"点击位置({cx}, {cy})")
        _move_and_click(task, cx, cy)
        task.sleep(0.5)

        feature = task.wait_feature(
            "flashmemberconfirm",
            box=confirm_box,
            threshold=0.7,
            time_out=2,
        )
        if feature:
            task.log_info(
                f"点击位置({cx}, {cy})后成功找到flashmemberconfirm"
            )
            return True
        task.log_info(
            f"点击位置({cx}, {cy})后未找到flashmemberconfirm，继续尝试"
        )

    task.log_info("所有尝试均未找到flashmemberconfirm")
    return True


def handle_card_reward(task: TriggerTask):
    """卡牌奖励页面: 按类型特征识别卡牌，并按优先级选择。"""
    page_title = _get_region_text(task, (0.345, 0.012, 0.642, 0.141))
    if "卡牌奖励" not in page_title:
        return False

    task.log_info("检测到卡牌奖励页面")
    cards = recognize_cards(task, page="卡牌奖励页面")
    if not cards:
        task.log_info("卡牌奖励页面未识别到卡牌，等待下一轮处理")
        return True

    target_boxes, target_click_positions = find_target_card(task)
    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"卡牌奖励页面: 检测到target卡牌，点击位置{click_position}"
        )
        _move_and_click(task, *click_position)
        return True

    priority = _get_card_reward_priority(task)

    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    node_status = getattr(task, "node_status", {})
    is_initial_node = (
        node_status.get("pass_final_boss_count", 0) == 0
        and node_status.get("node_count", 0) == 0
    )
    if initial_card_name and is_initial_node:
        initial_card = next(
            (card for card in cards if initial_card_name in card["name"]),
            None,
        )
        if initial_card:
            task.log_info(
                f"刷初始卡牌命中「{initial_card_name}」，点击该卡牌"
            )
            _move_and_click(task, initial_card["x"], initial_card["y"])
            task.sleep(1)
            return True
        task.log_info(
            f"刷初始卡牌未找到「{initial_card_name}」，点击ESC重新开始"
        )
        _move_and_click(task, 0.960, 0.053)
        task.sleep(1)
        return True

    chosen_card = None
    for pri_name in priority:
        chosen_card = next(
            (
                card for card in cards
                if pri_name
                and pri_name in card["name"]
                and _edit_distance(pri_name, card["name"], max_dist=1)
            ),
            None,
        )
        if chosen_card:
            task.log_info(
                f"按优先级选择卡牌: {chosen_card['name']}（配置: {pri_name}）"
            )
            break

    if chosen_card is None:
        refresh_boxes = []
        for box in task.all_texts:
            center_x = (box.x + box.width / 2) / task.width
            center_y = (box.y + box.height / 2) / task.height
            if not (
                0.105 <= center_x <= 0.903
                and 0.764 <= center_y <= 0.851
            ):
                continue
            match = re.fullmatch(r"\s*(\d)\s*/\s*3\s*", box.name)
            if match and int(match.group(1)) != 0:
                refresh_boxes.append((box, int(match.group(1)), 3))
        if refresh_boxes:
            for refresh_box, remaining, maximum in refresh_boxes:
                task.log_info(
                    f"卡牌奖励页面未命中优先级卡牌，"
                    f"点击刷新次数「{remaining}/{maximum}」刷新卡牌"
                )
                task.click_box(refresh_box)
            return True

    if chosen_card is None and cards:
        task.log_info("未命中优先级卡牌，跳过非优先级卡牌")
        # 在区域(0.620,0.883,0.990,0.983)内查找包含"跳过"的box并点击
        skip_box = next((b for b in task.all_texts
                         if 0.620 <= (b.x + b.width / 2) / task.width <= 0.990
                         and 0.883 <= (b.y + b.height / 2) / task.height <= 0.983
                         and "跳过" in b.name), None)
        if skip_box:
            task.log_info("卡牌奖励页面触发跳过事件，点击「跳过」按钮")
            task.click_box(skip_box)
        else:
            task.log_info("未找到跳过按钮，点击固定位置")
            _move_and_click(task, 0.745, 0.933)
        task.sleep(0.5)
        return True

    if chosen_card:
        task.log_info(f"卡牌奖励页面触发选卡事件，点击「{chosen_card['name']}」")
        _move_and_click(task, chosen_card["x"], chosen_card["y"])
        task.sleep(1)
        return True
    return False


_EQUIPMENT_TYPE_SLOTS = {"攻击力": 0, "防御力": 1, "生命值": 2}
_EQUIPMENT_QUALITY_RANKS = {"": 0, "普通": 1, "史诗": 2, "传说": 3}
_EQUIPMENT_NORMAL_RGB = (61, 76, 138)
_EQUIPMENT_EPIC_RGB = (160, 88, 69)
_EQUIPMENT_EMPTY_RGB = (15, 15, 15)
_EQUIPMENT_RGB_TOLERANCE = 30


def _equipment_slot(task: TriggerTask, type_text):
    """根据装备类型文本返回 equipment 下标，无法识别时返回 None。"""
    return next((slot for equipment_type, slot in _EQUIPMENT_TYPE_SLOTS.items()
                 if _get_game_text(task, equipment_type) in type_text), None)


def _equipment_priority(task: TriggerTask, slot):
    """读取指定装备位的优先级配置。"""
    priority = _get_config_value(task, f"装备{slot + 1}号位优先级", [])
    return list(priority) if isinstance(priority, (list, tuple)) else []



# 装备名比较前的归一化：只保留中文/英文/数字，去掉中英文引号、书名号、
# 连字符、空格等 OCR 容易识别错漏的符号，以及图标/emoji 字符。
# 货架列表页和购买详情页对同一件装备的 OCR 结果常常在这些字符上不一致
# （例如货架读到"时间调节器"，详情页读到"时间调节器🎆"），不归一化就会
# 导致整条装备名匹配失败（例如"W-52「多巴胺注射器」"）。
_EQUIPMENT_NAME_PUNCTUATION_PATTERN = re.compile(
    r'[^\w]'
)


def _normalize_equipment_name(name):
    """去除装备名中容易被 OCR 误识别的标点符号和图标字符，用于容错比较。"""
    return _EQUIPMENT_NAME_PUNCTUATION_PATTERN.sub('', name or '')


def _match_equipment_name(ocr_name, priority):
    """用双向包含匹配装备名（比较前做标点符号归一化），
    返回配置中的标准名称及优先级下标。"""
    if not ocr_name:
        return None, None
    normalized_ocr_name = _normalize_equipment_name(ocr_name)
    if not normalized_ocr_name:
        return None, None
    for index, config_name in enumerate(priority):
        if not config_name:
            continue
        normalized_config_name = _normalize_equipment_name(config_name)
        if not normalized_config_name:
            continue
        if (
            normalized_ocr_name in normalized_config_name
            or normalized_config_name in normalized_ocr_name
        ):
            return config_name, index
    return None, None


def _equipment_rank(name, priority):
    """返回已记录装备的优先级下标，未命中配置时排在配置装备之后。"""
    _, rank = _match_equipment_name(name, priority)
    return rank if rank is not None else len(priority)


def _equipment_state(task: TriggerTask):
    """获取并修正目标主战员的装备状态字典。"""
    member_status = getattr(task, "member_status", None)
    if not isinstance(member_status, dict):
        member_status = _initial_member_status()
        task.member_status = member_status
    equipment = member_status.setdefault("equipment", {})
    if not isinstance(equipment, dict):
        equipment = {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        }
        member_status["equipment"] = equipment
    elif "names" not in equipment or "descriptions" not in equipment:
        old_equipment = equipment
        equipment = {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        }
        for equipment_name, description in old_equipment.items():
            for slot in range(3):
                if _match_equipment_name(
                    equipment_name,
                    _equipment_priority(task, slot),
                )[0]:
                    equipment["names"][slot] = equipment_name
                    equipment["descriptions"][slot] = description
                    break
        member_status["equipment"] = equipment
    for key in ("names", "descriptions", "qualities"):
        values = equipment.get(key)
        if not isinstance(values, list):
            values = []
        equipment[key] = (values + ["", "", ""])[:3]
    if not isinstance(member_status.get("deck"), dict):
        member_status["deck"] = {}
    return equipment


def _member_deck_state(task: TriggerTask):
    """获取并修正目标主战员的卡组状态字典。"""
    member_status = getattr(task, "member_status", None)
    if not isinstance(member_status, dict):
        member_status = _initial_member_status()
        task.member_status = member_status
    deck = member_status.setdefault("deck", {})
    if not isinstance(deck, dict):
        deck = {}
        member_status["deck"] = deck
    return deck


def _reset_meditation_state(task: TriggerTask):
    """按当前配置重建目标主战员的冥想状态。"""
    configured_cards = _get_config_value(task, "需要冥想的卡牌", [])
    if not isinstance(configured_cards, (list, tuple)):
        configured_cards = []

    meditation_state = {}
    for card_name in configured_cards:
        normalized_name = str(card_name).strip()
        if normalized_name and normalized_name not in meditation_state:
            meditation_state[normalized_name] = False

    _member_deck_state(task)["冥想"] = meditation_state


def _matching_meditation_card_names(task: TriggerTask, cards):
    """返回与识别卡牌名称匹配的冥想配置名称。"""
    meditation_state = _member_deck_state(task).get("冥想", {})
    if not isinstance(meditation_state, dict):
        return []

    matched_names = []
    for configured_name in meditation_state:
        for card in cards:
            recognized_name = str(card.get("name", "")).strip()
            if (
                recognized_name
                and (configured_name in recognized_name or recognized_name in configured_name)
                and _edit_distance(configured_name, recognized_name, max_dist=1)
            ):
                matched_names.append(configured_name)
                break
    return matched_names


def _current_equipment_for_slot(task: TriggerTask, equipment, slot):
    """按槽位读取当前装备名称及其优先级。"""
    priority = _equipment_priority(task, slot)
    names = equipment.get("names", [])
    equipment_name = names[slot] if slot < len(names) else ""
    if not equipment_name:
        return "", len(priority)
    return equipment_name, _equipment_rank(equipment_name, priority)


def _pixel_rgb(task: TriggerTask, point):
    """读取归一化坐标的像素，并将 OpenCV BGR 转为 RGB。"""
    if task.frame is None:
        return None
    x = min(task.width - 1, max(0, round(point[0] * task.width)))
    y = min(task.height - 1, max(0, round(point[1] * task.height)))
    blue, green, red = (int(value) for value in task.frame[y, x, :3])
    return red, green, blue


def _rgb_is_close(rgb, target, tolerance=_EQUIPMENT_RGB_TOLERANCE):
    """判断 RGB 各通道是否均在指定容差内。"""
    return rgb is not None and all(
        abs(value - expected) <= tolerance
        for value, expected in zip(rgb, target)
    )


def _equipment_quality_at(task: TriggerTask, point, allow_empty=False):
    """按指定点颜色识别装备品质；安装槽位可额外识别空槽。"""
    rgb = _pixel_rgb(task, point)
    if rgb is None:
        return None, None
    if allow_empty and _rgb_is_close(rgb, _EQUIPMENT_EMPTY_RGB):
        return "", rgb
    if _rgb_is_close(rgb, _EQUIPMENT_NORMAL_RGB):
        return "普通", rgb
    if _rgb_is_close(rgb, _EQUIPMENT_EPIC_RGB):
        return "史诗", rgb
    return "传说", rgb


def _member_equipment_qualities(task: TriggerTask, level_box):
    """根据等级文本的相对位置读取该主战员三个装备槽的品质。"""
    level_center_x = (level_box.x + level_box.width / 2) / task.width
    level_center_y = (level_box.y + level_box.height / 2) / task.height
    relative_offsets = (
        (0.130, -0.0655),
        (0.201, -0.0665),
        (0.270, -0.0655),
    )
    qualities = []
    for slot, (offset_x, offset_y) in enumerate(relative_offsets):
        point = (level_center_x + offset_x, level_center_y + offset_y)
        quality, rgb = _equipment_quality_at(task, point, allow_empty=True)
        qualities.append(quality)
        task.log_info(
            f"第{slot + 1}号装备位颜色RGB={rgb}，"
            f"识别品质={quality or '未安装'}"
        )
    return qualities


def _should_install_equipment(task, current_name, current_quality, new_equipment):
    """先比较配置装备优先级，优先级相同时再比较品质。"""
    priority = new_equipment["priority"]
    _, current_rank = _match_equipment_name(current_name, priority)
    new_rank = new_equipment["rank"]
    if new_rank is not None or current_rank is not None:
        if new_rank is not None and (
            current_rank is None or new_rank < current_rank
        ):
            return True, "配置优先级更高"
        if current_rank is not None and (
            new_rank is None or current_rank < new_rank
        ):
            return False, "当前装备配置优先级更高"

    current_quality_rank = _EQUIPMENT_QUALITY_RANKS.get(current_quality or "", 0)
    new_quality_rank = _EQUIPMENT_QUALITY_RANKS.get(
        new_equipment.get("quality") or "", 0
    )
    return (
        new_quality_rank > current_quality_rank,
        f"品质{new_equipment.get('quality') or '未知'}"
        f"{'高于' if new_quality_rank > current_quality_rank else '不高于'}"
        f"{current_quality or '未安装'}",
    )


def _equipment_slot_by_dictionary(ocr_name):
    """按装备词典用双向包含匹配 OCR 名称（比较前做标点符号归一化），
    返回词典记录的装备下标；未收录该装备或未标注装备时返回 None。"""
    if not ocr_name:
        return None
    normalized_ocr_name = _normalize_equipment_name(ocr_name)
    if not normalized_ocr_name:
        return None
    by_name = _load_equipment_dictionary()["by_name"]
    for dict_name, entry in by_name.items():
        if entry["slot"] is None:
            continue
        normalized_dict_name = _normalize_equipment_name(dict_name)
        if not normalized_dict_name:
            continue
        if (
            normalized_ocr_name in normalized_dict_name
            or normalized_dict_name in normalized_ocr_name
        ):
            return entry["slot"]
    return None


def _equipment_info(task: TriggerTask, name_region, type_region, description_region):
    """从指定区域读取装备名称、类型和描述，并解析槽位、品质与配置优先级。

    装备判定优先查装备词典（按名称直接确定，不受OCR类型文本漏识别影响），
    词典未收录该装备时才回退到原有的“类型文本”识别逻辑。
    """
    ocr_name = _get_region_text(task, name_region).strip()
    type_text = _get_region_text(task, type_region).strip()
    description = _get_region_text(task, description_region).strip()
    if not ocr_name:
        return None

    slot = _equipment_slot_by_dictionary(ocr_name)
    if slot is not None:
        task.log_info(f"待选装备「{ocr_name}」命中装备词典，装备={slot + 1}号位")
    else:
        if not type_text:
            return None
        slot = _equipment_slot(task, type_text)
        if slot is None:
            return None

    priority = _equipment_priority(task, slot)
    canonical_name, rank = _match_equipment_name(ocr_name, priority)
    quality, quality_rgb = _equipment_quality_at(task, (0.117, 0.409))
    task.log_info(f"待选装备颜色RGB={quality_rgb}，识别品质={quality or '未知'}")
    return {
        "ocr_name": ocr_name,
        "name": canonical_name or ocr_name,
        "type": type_text,
        "description": description,
        "slot": slot,
        "priority": priority,
        "rank": rank,
        "quality": quality,
    }


def _find_member_level_tags(task: TriggerTask, region, page="主战员选择页面"):
    """在指定区域识别leveltag，按位置去重并从上到下返回最多三个。"""
    level_tags = task.find_feature(
        feature_name="leveltag",
        box=task.box_of_screen(*region),
        threshold=0.7,
    ) or []
    deduplicated_level_tags = []
    for level_tag in sorted(
        level_tags,
        key=lambda feature: feature.confidence,
        reverse=True,
    ):
        level_center_x = level_tag.x + level_tag.width / 2
        level_center_y = level_tag.y + level_tag.height / 2
        if any(
            (
                (level_center_x - (kept.x + kept.width / 2)) ** 2
                + (level_center_y - (kept.y + kept.height / 2)) ** 2
            ) ** 0.5
            < max(level_tag.width, level_tag.height, kept.width, kept.height)
            for kept in deduplicated_level_tags
        ):
            continue
        deduplicated_level_tags.append(level_tag)

    kept_level_tags = sorted(
        deduplicated_level_tags,
        key=lambda feature: feature.y,
    )[:3]
    task.log_info(
        f"{page}识别到{len(level_tags)}个leveltag特征，"
        f"去重并限制后保留{len(kept_level_tags)}个"
    )
    for index, level_tag in enumerate(kept_level_tags, 1):
        task.log_info(
            f"第{index}号主战员leveltag: "
            f"中心=({(level_tag.x + level_tag.width / 2) / task.width:.4f}, "
            f"{(level_tag.y + level_tag.height / 2) / task.height:.4f})，"
            f"置信度={level_tag.confidence:.4f}"
        )
    return kept_level_tags


def _find_target_member_index(
    task: TriggerTask,
    lv_texts,
    region,
    feature_name="target_member_small",
):
    """在指定区域匹配目标成员头像，并返回距离最近的等级文本索引。"""
    if not lv_texts or not task.feature_exists(feature_name):
        return None
    target_member_box = task.find_one(
        feature_name=feature_name,
        box=task.box_of_screen(*region),
        threshold=0.4,
    )
    if not target_member_box:
        return None

    target_center_x = target_member_box.x + target_member_box.width / 2
    target_center_y = target_member_box.y + target_member_box.height / 2
    target_member_index = min(
        range(len(lv_texts)),
        key=lambda index: (
            (lv_texts[index].x + lv_texts[index].width / 2 - target_center_x) ** 2
            + (lv_texts[index].y + lv_texts[index].height / 2 - target_center_y) ** 2
        ),
    )
    task.log_info(
        f"刷存档主战员头像匹配成功，相似度={target_member_box.confidence:.4f}，"
        f"绑定第{target_member_index + 1}号主战员"
    )
    return target_member_index


def _find_refine_box(task: TriggerTask):
    """安装装备页面右下角的"提炼"按钮（把装备分解成信用点）。"""
    return next(
        (b for b in task.all_texts
         if 0.522 <= (b.x + b.width / 2) / task.width <= 0.999
         and 0.879 <= (b.y + b.height / 2) / task.height <= 0.996
         and "提炼" in b.name),
        None
    )


def handle_equipment(task: TriggerTask):
    """装备选择/安装界面: 按装备位优先级选择，并维护目标主战员的装备状态。"""
    title = find_box_at_point(task, 0.499, 0.126)
    if not (title and title.name == "装备"):
        return False

    task.log_info("检测到装备页面")
    equipment = _equipment_state(task)
    equip_hint = find_box_at_point(task, 0.921, 0.135)

    if equip_hint and _get_game_text(task, '请选择主战员') in equip_hint.name:
        task.log_info("检测到安装装备界面")
        purchase_bottom_boxes = [
            box for box in task.all_texts
            if 0.013 <= (box.x + box.width / 2) / task.width <= 0.992
            and 0.881 <= (box.y + box.height / 2) / task.height <= 0.994
            and box.name.strip()
        ]
        cancel_box = next(
            (box for box in purchase_bottom_boxes if "取消" in box.name), None
        )
        purchase_box = next(
            (box for box in purchase_bottom_boxes if "购买" in box.name), None
        )
        price_box = next(
            (box for box in purchase_bottom_boxes
             if re.fullmatch(r"\d+", box.name.strip())),
            None,
        )
        is_purchase_page = bool(cancel_box and purchase_box)
        equipment_price = None
        current_credit = None
        if is_purchase_page:
            task.log_info("检测到购买装备页面")
            equipment_price = (
                _parse_discounted_price(price_box.name) if price_box else None
            )
            current_credit = _get_current_credit(task)
            task.log_info(
                f"购买装备页面: 当前信用点={current_credit}，"
                f"OCR价格=「{price_box.name if price_box else ''}」，"
                f"实际价格={equipment_price}"
            )
            if equipment_price is None:
                task.log_info("购买装备页面未识别到价格，按价格低于当前信用点继续购买")
            elif equipment_price > current_credit:
                task.log_info(
                    f"装备价格{equipment_price}大于当前信用点{current_credit}，点击「取消」"
                )
                task.click_box(cancel_box)
                task.sleep(1)
                return True

            if _get_config_value(task, "刷装备刻印", False):
                credit_floor = _get_config_value(task, "刷装备刻印信用点下限", 0)
                try:
                    credit_floor = int(credit_floor)
                except (TypeError, ValueError):
                    credit_floor = 0
                if credit_floor > 0 and current_credit < credit_floor:
                    task.log_info(
                        f"刷装备刻印: 当前信用点{current_credit}低于下限{credit_floor}，"
                        "跳过本次购买评估，点击「取消」"
                    )
                    task.click_box(cancel_box)
                    task.sleep(1)
                    return True

        bottom_buttons = [
            box for box in task.all_texts
            if 0.563 <= (box.x + box.width / 2) / task.width <= 0.998
            and 0.881 <= (box.y + box.height / 2) / task.height <= 0.997
            and box.name.strip()
        ]
        refine_boxes = [box for box in bottom_buttons if "提炼" in box.name]
        if refine_boxes and len(refine_boxes) == len(bottom_buttons):
            task.log_info("安装装备界面只有提炼按钮，直接点击提炼")
            task.click_box(refine_boxes[0])
            return True

        new_equipment = _equipment_info(
            task,
            (0.217, 0.379, 0.469, 0.436),
            (0.188, 0.444, 0.323, 0.489),
            (0.179, 0.492, 0.542, 0.668),
        )
        if not new_equipment:
            task.log_info("未能识别待安装装备的名称或类型")
            if is_purchase_page:
                task.log_info("购买装备无法识别装备信息，点击「取消」")
                task.click_box(cancel_box)
                task.sleep(1)
                return True
            return False
        equipment_desc = new_equipment["description"]
        task.log_info(f"待安装装备描述: 「{equipment_desc}」")

        lv_texts = _find_member_level_tags(
            task,
            (0.609, 0.290, 0.652, 0.789),
            page="安装装备页面",
        )
        target_member_index = _find_target_member_index(
            task,
            lv_texts,
            (0.607, 0.192, 0.739, 0.856),
            feature_name="target_member_tiny",
        )
        tracks_target_member = "刷存档主战员" in getattr(task, "default_config", {})
        preferred_member_index = (
            target_member_index if tracks_target_member else (0 if lv_texts else None)
        )

        slot = new_equipment["slot"]
        current_name, _ = _current_equipment_for_slot(task, equipment, slot)
        current_quality = equipment["qualities"][slot]
        should_install_first = False
        install_reason = "未找到目标主战员"
        # 本次点开的是商店商品时，取 handle_shop 记录的角标信息，用于区分
        # "候选没有刻印"与"候选自带的非目标刻印"；非商店来源（白拿装备）
        # 拿不到角标，只能用详情页描述里的刻印图标兜底。
        candidate_has_engraving = None
        if is_purchase_page:
            candidate_has_engraving = (
                getattr(task, "node_status", None) or {}
            ).get("shop_opened_has_engraving")
        # 免费获得的装备没有商店角标可用，用详情页描述里的刻印图标兜底：
        # 图标命中就说明候选自带刻印，不能按"不带刻印可继承"放行。
        merged_has_engraving = resolve_candidate_engraving_flag(
            candidate_has_engraving, equipment_desc,
        )
        if merged_has_engraving and candidate_has_engraving is not True:
            task.log_info(
                "待选装备描述里检测到刻印图标，判定为自带刻印"
                f"（角标信息={candidate_has_engraving}）"
            )
        candidate_has_engraving = merged_has_engraving
        engraving_keywords = _get_card_list(task, "装备刻印优先级")
        if preferred_member_index is not None:
            live_qualities = _member_equipment_qualities(
                task, lv_texts[preferred_member_index]
            )
            for equipment_slot, live_quality in enumerate(live_qualities):
                if live_quality is None:
                    continue
                equipment["qualities"][equipment_slot] = live_quality
                if not live_quality:
                    if equipment["names"][equipment_slot]:
                        task.log_info(
                            f"第{equipment_slot + 1}号装备位实际为空，清除记录装备"
                            f"「{equipment['names'][equipment_slot]}」"
                        )
                    equipment["names"][equipment_slot] = ""
                    equipment["descriptions"][equipment_slot] = ""
            current_name, _ = _current_equipment_for_slot(task, equipment, slot)
            current_quality = equipment["qualities"][slot]

            if _get_config_value(task, "刷装备刻印", False):
                # 刷装备刻印模式：整体切换判断标准为"是否推进目标名+目标刻印进展"，
                # 不再使用装备优先级+品质比较，也不做传说装备互斥判断。
                current_description = equipment["descriptions"][slot]
                should_install_first, install_reason = should_assign_to_target_member(
                    current_name,
                    current_description,
                    new_equipment["name"],
                    equipment_desc,
                    new_equipment["priority"],
                    engraving_keywords,
                    candidate_has_engraving,
                )
            else:
                should_install_first, install_reason = _should_install_equipment(
                    task,
                    current_name,
                    current_quality,
                    new_equipment,
                )
                has_legendary_in_other_slot = any(
                    quality == "传说" and equipment_slot != slot
                    for equipment_slot, quality in enumerate(live_qualities)
                )
                if (
                    new_equipment["quality"] == "传说"
                    and has_legendary_in_other_slot
                ):
                    should_install_first = False
                    install_reason = "该主战员其他装备位已有传说装备"
                    task.log_info(
                        "目标主战员已安装传说装备，新传说装备改由其他主战员安装"
                    )

        if should_install_first and preferred_member_index is not None:
            chosen = lv_texts[preferred_member_index]
            if not tracks_target_member or target_member_index is not None:
                equipment["names"][slot] = new_equipment["name"]
                # 候选不带刻印时，覆盖后槽位原刻印会被继承下来（见
                # utils_equipment_engraving 模块头部第 5 条），所以描述要继续
                # 保留原刻印文字，不能直接换成候选描述，否则后面会把该槽位
                # 误判成"没有刻印"，再买一件更差的刻印把继承来的刻印顶掉。
                recorded_description = resolve_slot_description_after_install(
                    equipment["descriptions"][slot],
                    equipment_desc,
                    engraving_keywords,
                    candidate_has_engraving,
                )
                if recorded_description != equipment_desc:
                    task.log_info(
                        f"{slot + 1}号位装备「{new_equipment['name']}」不带刻印，"
                        "覆盖后继承该槽位原有刻印，槽位描述继续记录为"
                        f"「{recorded_description}」"
                    )
                equipment["descriptions"][slot] = recorded_description
                equipment["qualities"][slot] = new_equipment["quality"] or ""
            member_label = (
                "刷存档主战员"
                if target_member_index is not None
                else "第一主战员"
            )
            task.log_info(
                f"{slot + 1}号位装备「{new_equipment['name']}」优于当前装备「{current_name}」，"
                f"原因={install_reason}，"
                f"安装给{member_label}"
            )
            _move_and_click(task, 0.756, (chosen.y + chosen.height / 2) / task.height)
            task.sleep(1)
            if is_purchase_page:
                task.log_info(
                    f"购买装备分配完成，价格={equipment_price}，"
                    f"当前信用点={current_credit}，点击「购买」"
                )
                task.click_box(purchase_box)
                task.sleep(1)
                return True
            return False

        engraving_mode = _get_config_value(task, "刷装备刻印", False)
        if is_purchase_page and engraving_mode:
            task.log_info(
                f"刷装备刻印: {slot + 1}号位装备「{new_equipment['name']}」"
                f"不满足刻印进展，原因={install_reason}，放弃购买，点击「取消」"
            )
            # 记录本次货架周期内已判定不满足刻印的装备名，避免下次进商店
            # 时重新点开同一件装备，导致扫描卡在货架前排、后面商品永远
            # 没机会被检查。用 handle_shop 里读取到的原始 OCR 名称
            # （new_equipment["ocr_name"]）匹配，与货架上的文本口径一致。
            _mark_shop_engraving_rejected(task, new_equipment["ocr_name"])
            task.click_box(cancel_box)
            task.sleep(1)
            return True

        # 刻印专业模式：免费获得（非商店购买）的装备只要不能推进"目标名+
        # 目标刻印"的进展，就直接提炼（分解）成信用点，不再转交给其他主战员
        # 占掉装备位——装到别人身上就再也拿不回来了。
        if should_refine_free_equipment(
            is_purchase_page=is_purchase_page,
            engraving_mode=engraving_mode,
            pro_mode=_get_config_value(task, "刻印专业模式", False),
            slot_evaluated=preferred_member_index is not None,
            assign_to_target_member=should_install_first,
        ):
            refine_box = _find_refine_box(task)
            if refine_box:
                task.log_info(
                    f"刻印专业模式: 免费获得的{slot + 1}号位装备"
                    f"「{new_equipment['name']}」不能推进刻印进展，"
                    f"原因={install_reason}，直接点击提炼换成信用点"
                )
                task.click_box(refine_box)
                task.sleep(1)
                return True
            task.log_info("刻印专业模式: 未找到提炼按钮，回退到原分配逻辑")

        other_members = [
            level_box for index, level_box in enumerate(lv_texts)
            if index != preferred_member_index
        ]
        if other_members:
            chosen = random.choice(other_members)
            if tracks_target_member and target_member_index is None:
                task.log_info("未识别到刷存档主战员，随机安装给其他主战员")
            else:
                task.log_info(
                    f"{slot + 1}号位无需替换当前装备「{current_name}」，"
                    f"原因={install_reason}，"
                    "随机安装给其他主战员"
                )
            _move_and_click(task, 0.756, (chosen.y + chosen.height / 2) / task.height)
            task.sleep(1)
            if is_purchase_page:
                task.log_info(
                    f"购买装备分配给其他主战员，价格={equipment_price}，"
                    f"当前信用点={current_credit}，点击「购买」"
                )
                task.click_box(purchase_box)
                task.sleep(1)
                return True
            return False

        if is_purchase_page:
            task.log_info("购买装备无法分配给任何主战员，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True

        refine_box = _find_refine_box(task)
        if refine_box:
            task.log_info(f"{slot + 1}号位无需替换且没有其他主战员可选，点击提炼")
            task.click_box(refine_box)
            task.sleep(1)
            return True

        task.log_info("未找到可选择的主战员或提炼按钮")
        return False

    candidates = []
    candidate_specs = [
        (
            (0.409, 0.219, 0.678, 0.276),
            (0.384, 0.275, 0.562, 0.319),
            (0.382, 0.324, 0.723, 0.496),
            (0.518, 0.454),
        ),
        (
            (0.410, 0.551, 0.699, 0.608),
            (0.384, 0.613, 0.573, 0.653),
            (0.380, 0.658, 0.720, 0.836),
            (0.521, 0.600),
        ),
    ]
    for name_region, type_region, description_region, click_position in candidate_specs:
        candidate = _equipment_info(
            task, name_region, type_region, description_region
        )
        if not candidate:
            task.log_info("选择装备界面的候选装备信息不完整，等待下轮重新识别")
            return True
        candidate["click_position"] = click_position
        candidates.append(candidate)
    task.log_info(
        f"检测到选择装备界面，候选装备: "
        f"{[(candidate['ocr_name'], candidate['slot'] + 1) for candidate in candidates]}"
    )

    chosen_index = None
    for slot in range(3):
        current_name, current_rank = _current_equipment_for_slot(task, equipment, slot)
        for index, candidate in enumerate(candidates):
            if candidate["slot"] != slot or candidate["rank"] is None:
                continue
            if not current_name or candidate["rank"] < current_rank:
                chosen_index = index
                task.log_info(
                    f"优先选择{slot + 1}号位装备「{candidate['name']}」，当前装备「{current_name}」"
                )
                break
        if chosen_index is not None:
            break

    if chosen_index is None:
        empty_slot_candidates = [
            candidate for candidate in candidates
            if not _current_equipment_for_slot(task, equipment, candidate["slot"])[0]
        ]
        if empty_slot_candidates:
            chosen_candidate = random.choice(empty_slot_candidates)
            click_position = chosen_candidate["click_position"]
            task.log_info(
                f"候选装备均未命中升级条件，优先选择空缺的"
                f"{chosen_candidate['slot'] + 1}号位装备「{chosen_candidate['ocr_name']}」"
            )
        else:
            task.log_info("候选装备均未命中升级条件且对应位置均非空，随机选择一个装备")
            click_position = random.choice([spec[3] for spec in candidate_specs])
    else:
        click_position = candidates[chosen_index]["click_position"]
    _move_and_click(task, *click_position)
    task.sleep(2)
    return False


# 卡牌操作关键词 → 配置 key 映射
_SELECT_CARD_CONFIG_KEYS = {
    "移除": "移除卡牌列表",
    "复制": "复制卡牌列表",
    "闪光": "闪光卡牌列表",
    "灵光": "闪光卡牌列表",
}


def _scroll_to_target_member_for_card_removal(task: TriggerTask):
    """移除卡牌前滚动成员列表，直到目标成员出现或到达底部。"""
    feature_name = "target_member_in_select_card"
    page = "移除卡牌目标主战员查找"
    if not task.feature_exists(feature_name):
        task.log_info(f"{page}: 尚未保存{feature_name}特征，跳过查找")
        return

    search_region = (0.079, 0.092, 0.209, 0.675)
    search_box = task.box_of_screen(*search_region)
    scroll_x = (search_region[0] + search_region[2]) / 2
    scroll_y = (search_region[1] + search_region[3]) / 2
    scrollbar_white_ratio = region_white_ratio(
        task, (0.976, 0.119, 0.988, 0.858)
    )
    single_page = scrollbar_white_ratio < 0.01
    task.log_info(
        f"{page}: 滚动条区域白色像素占比={scrollbar_white_ratio:.2%}，"
        f"是否仅一页卡牌={single_page}"
    )

    max_scrolls = 20
    scroll_count = 0
    while True:
        target_member = task.find_one(
            feature_name=feature_name,
            box=search_box,
            threshold=0.5,
        )
        if target_member:
            task.log_info(
                f"{page}: 找到目标主战员，相似度={target_member.confidence:.4f}"
            )
            return
        if single_page:
            task.log_info(f"{page}: 当前仅一页，未找到目标主战员")
            return
        if _point_is_white(task, 0.982, 0.846, page):
            task.log_info(f"{page}: 已到达底部，未找到目标主战员")
            return
        if scroll_count >= max_scrolls:
            task.log_info(f"{page}: 向下滚动已达到{max_scrolls}次限制")
            return
        _scroll_card_page(
            task,
            scroll_x,
            scroll_y,
            -3,
            page,
            distance=(search_region[3] - search_region[1]) / 4,
        )
        scroll_count += 1


def handle_select_card(task: TriggerTask):
    """统一卡牌选择页面: 在(0.198,0.039)处检测文本，按移除/复制/闪光等关键字匹配配置并选择卡牌。"""
    box = find_box_at_point(task, 0.198, 0.039)
    if not box:
        return False
    m = re.search(r'请选择(\d*)张*.*?(移除|复制|闪光|灵光).*?卡牌', box.name)
    if not m:
        return False
    count_text = m.group(1)
    action = m.group(2)
    count = int(count_text) if count_text else 1
    config_key = _SELECT_CARD_CONFIG_KEYS.get(action)
    if config_key is None:
        return False
    task.log_info(f"检测到卡牌{action}选择，需选择{count}张，配置key={config_key}")

    # 日志打印右下角选牌操作提示
    action_tip = find_box_at_point(task, 0.945, 0.918)
    if action_tip:
        task.log_info(f"右下角选牌操作提示: 「{action_tip.name}」")

    if (
        action in ("移除", "复制", "闪光", "灵光")
        and task.name == "自动卡厄思模式"
    ):
        _scroll_to_target_member_for_card_removal(task)

    select_card(task, _get_card_list(task, config_key), count=count, action=action)
    return True


def handle_copy_card_choice(task: TriggerTask):
    """复制卡牌选择页面: 按类型特征识别卡牌，并按复制卡牌列表优先级选择。"""
    box = find_box_at_point(task, 0.498, 0.133)
    copy_card_prompt = _get_game_text(task, "请选择要复制的卡牌")
    if not (box and copy_card_prompt in box.name):
        return False

    task.log_info("检测到复制卡牌选择页面")
    target_boxes, target_click_positions = find_target_card(task)
    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"复制卡牌选择: 检测到target卡牌，点击位置{click_position}"
        )
        _move_and_click(task, *click_position)
        return True

    cards = recognize_cards(task, page="复制卡牌选择页面")

    priority = _get_config_value(task, '复制卡牌列表', [])
    for pri_name in priority:
        for card in cards:
            if card["name"] and pri_name in card["name"]:
                task.log_info(f"复制卡牌选择: 按优先级选择「{card['name']}」(匹配「{pri_name}」)")
                _move_and_click(task, card["x"], card["y"])
                task.sleep(0.5)
                return True

    task.log_info("复制卡牌选择: 未命中任何优先级，return False")
    return False


def handle_copy_member(task: TriggerTask):
    """选择要复制卡牌的主战员页面。"""
    box = find_box_at_point(task, 0.502, 0.932)
    copy_member_prompt = _get_game_text(task, "选择要复制卡牌的主战员")
    if not (box and copy_member_prompt in box.name):
        return False

    task.log_info("检测到卡牌复制主战员选择事件，进行相应操作")

    confirm_box = task.box_of_screen(0.145, 0.044, 0.856, 0.214)
    click_positions = [(0.228, 0.510), (0.504, 0.504), (0.755, 0.508)]
    click_positions = _prioritize_target_member_click(
        task, click_positions, (0.173, 0.232, 0.858, 0.508)
    )

    for i, (cx, cy) in enumerate(click_positions):
        task.log_info(f"点击位置({cx}, {cy})")
        _move_and_click(task, cx, cy)
        task.sleep(0.5)

        feature = task.wait_feature("copymemberconfirm", box=confirm_box, time_out=2)
        if feature:
            task.log_info(f"点击位置({cx}, {cy})后成功找到copymemberconfirm")
            return True
        else:
            task.log_info(f"点击位置({cx}, {cy})后未找到copymemberconfirm，继续尝试")

    task.log_info("所有尝试均未找到copymemberconfirm")
    return True


def handle_convert_card(task: TriggerTask):
    """转换卡牌页面: 跳过转换。"""
    box = find_box_at_point(task, 0.226, 0.046)
    if box and _get_game_text(task, '转换的卡牌') in box.name:
        task.log_info("检测到卡牌转换选择，进行跳过操作")
        _move_and_click(task, 0.776, 0.926)
        task.sleep(0.5)
        _move_and_click(task, 0.661, 0.632)
        return True
    return False


def handle_negotiation(task: TriggerTask):
    """谈判失败页面: 点击下一步跳过。"""
    title = find_box_at_point(task, 0.498, 0.683)
    if title and title.name in "失败":
        task.log_info("检测到掷骰子失败，跳过掷骰子")
        _move_and_click(task, 0.665, 0.899)
        return True
    return False


def handle_continue(task: TriggerTask):
    """通用"继续"按钮。"""
    continue_region = (0.459, 0.858, 0.992, 0.988)
    continue_text = _get_game_text(task, '继续')
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _clean_match(text_box.name, continue_text)
            and continue_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= continue_region[2]
            and continue_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= continue_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到下一步操作，点击继续")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_confirm(task: TriggerTask):
    """通用"确认"按钮。"""
    confirm_region = (0.267, 0.867, 0.991, 0.979)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _clean_match(text_box.name, "确认")
            and confirm_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= confirm_region[2]
            and confirm_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= confirm_region[3]
        ),
        None,
    )
    if box:
        if is_button_active(task, box):
            task.log_info("检测到确认操作，点击确认")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("确认按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_convert(task: TriggerTask):
    """通用"转换"按钮: 按钮激活则点击转换，未激活则点击跳过(0.776,0.926)。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "转换"):
        if is_button_active(task, box):
            task.log_info("检测到转换按钮，点击转换")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("转换按钮未激活（灰色），点击跳过")
            _move_and_click(task, 0.776, 0.926)
            task.sleep(1)
            return True
    return False

def handle_remove(task: TriggerTask):
    """通用"移除"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "移除"):
        if is_button_active(task, box):
            task.log_info("检测到移除操作，点击移除")
            task.click_box(box)
            removed_count = max(
                1,
                getattr(task, "_pending_removed_card_count", 0),
            )
            _record_removed_cards(
                task, removed_count, from_shop=_consume_pending_shop_removal(task),
            )
            task._pending_removed_card_count = 0
            task.sleep(1)
            return True
        else:
            task.log_info("移除按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_three_choice_card_remove(task: TriggerTask):
    """三选一卡牌移除页面：点击指定区域内已激活的“移除”按钮。"""
    region = (0.507, 0.889, 0.740, 0.967)
    remove_box = next(
        (
            box for box in task.all_texts
            if region[0] <= (box.x + box.width / 2) / task.width <= region[2]
            and region[1] <= (box.y + box.height / 2) / task.height <= region[3]
            and "移除" in box.name
        ),
        None,
    )
    if not remove_box:
        return False
    if not is_button_active(task, remove_box):
        task.log_info("三选一卡牌移除页面的移除按钮未激活（灰色），跳过点击")
        return False

    task.log_info("检测到三选一卡牌移除页面，点击移除")
    task.click_box(remove_box)
    _record_removed_cards(
        task, 1, from_shop=_consume_pending_shop_removal(task),
    )
    task._pending_removed_card_count = 0
    return True

def handle_flash(task: TriggerTask):
    """通用"闪光"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _get_game_text(task, '闪光') in box.name:
        if is_button_active(task, box):
            task.log_info("检测到闪光操作，点击闪光")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_reflash(task: TriggerTask):
    """通用"重新闪光"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _get_game_text(task, '重新闪光') in box.name:
        if is_button_active(task, box):
            task.log_info("检测到重新闪光操作，点击重新闪光")
            task.click_box(box)
            task.sleep(2)
            return True
        else:
            task.log_info("重新闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_grant_flash(task: TriggerTask):
    """通用"赋予闪光"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "赋予闪光"):
        if is_button_active(task, box):
            task.log_info("检测到赋予闪光操作，点击赋予闪光")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("赋予闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_copy(task: TriggerTask):
    """通用"复制"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "复制"):
        if is_button_active(task, box):
            task.log_info("检测到复制操作，点击复制")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("复制按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_enter(task: TriggerTask):
    """通用"进入"按钮。"""
    enter_region = (0.017, 0.771, 0.996, 0.992)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _clean_match(text_box.name, "进入")
            and enter_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= enter_region[2]
            and enter_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= enter_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到进入按钮，点击进入")
        task.click_box(box)
        reset_mission_status(task)
        task.sleep(1)
        return True
    return False

def handle_equipment_recast(task: TriggerTask):
    """装备重铸页面: 点击确认重铸。"""
    box = find_box_at_point(task, 0.501, 0.128)
    if box and _get_game_text(task, '装备重铸') in box.name:
        task.log_info("检测到装备重铸页面，点击跳过")
        _move_and_click(task, 0.749, 0.932)
        task.sleep(1)
        return True
    return False


def handle_event_task(task: TriggerTask):
    """事件任务页面: 识别事件选项特征和描述，按任务优先级选择推进。"""
    bottom_box = find_box_at_point(task, 0.516, 0.971)
    if bottom_box and re.search(r'\d+/\d+', bottom_box.name):
        return False

    rewards = task.find_feature(feature_name="taskreward")
    if rewards:
        reward = rewards[0]
        cx = (reward.x + reward.width / 2) / task.width
        cy = (reward.y + reward.height / 2) / task.height
        if 0.437 <= cx <= 0.902 and 0.350 <= cy <= 0.614:
            task.log_info("检测到任务奖励图标，优先点击")
            task.click_box(reward)
            return True

    tasks_info = recognize_event_options(task, page="事件任务页面")
    if not tasks_info:
        return False

    selectable_tasks = []
    for task_info in tasks_info:
        event_x = task_info["x"]
        event_y = task_info["y"]
        forbidden_region = (
            max(0.0, event_x - 0.131),
            max(0.0, event_y - 0.257),
            min(1.0, event_x - 0.090),
            min(1.0, event_y - 0.189),
        )
        forbidden_feature = task.find_one(
            feature_name="forbidden_event",
            box=task.box_of_screen(*forbidden_region),
        )
        if forbidden_feature:
            task.log_info(
                f"事件选项已被禁止，过滤描述「{task_info['description']}」，"
                f"forbidden_event相似度={forbidden_feature.confidence:.4f}"
            )
            continue
        selectable_tasks.append(task_info)
    tasks_info = selectable_tasks
    if not tasks_info:
        task.log_info("事件任务页面的所有选项均被禁止，跳过本次选择")
        return False

    check_region = task.box_of_screen(0.396, 0.286, 0.960, 0.718)
    check_features = [
        feature
        for feature_name in ("check", "check2")
        if (feature := task.find_one(feature_name=feature_name, box=check_region))
    ]
    check_feature = max(
        check_features,
        key=lambda feature: feature.confidence,
        default=None,
    )
    if check_feature:
        task.log_info(
            f"事件任务页面检测到check特征，相似度={check_feature.confidence:.4f}，优先点击"
        )
        task.click_box(check_feature)
        task.sleep(1)
        return True

    def click_event_option(event_task):
        left, top, right, bottom = event_task["description_region"]
        description_x = (left + right) / 2
        description_y = (top + bottom) / 2
        _move_and_click(task, description_x, description_y)

    def handle_initial_node_task(description_keyword, purpose):
        """初始节点按描述选择任务；未找到目标描述时点击ESC重新开始。"""
        matched_task = next(
            (
                task_info
                for task_info in tasks_info
                if description_keyword in task_info["description"]
            ),
            None,
        )
        if matched_task:
            task.log_info(
                f"{purpose}：选择包含“{description_keyword}”的事件任务"
            )
            click_event_option(matched_task)
        else:
            task.log_info(
                f"{purpose}：未找到包含“{description_keyword}”的事件任务，"
                "点击ESC重新开始"
            )
            _move_and_click(task, 0.959, 0.053)
        task.sleep(1)
        return True

    upper_event_task = next(
        (task_info for task_info in tasks_info if task_info["y"] < 0.925),
        None,
    )
    if upper_event_task is not None:
        task.log_info(
            f"检测到Y坐标小于0.925的任务，立即选择: {upper_event_task['description']}"
        )
        click_event_option(upper_event_task)
        task.sleep(1)
        return True

    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    node_status = getattr(task, "node_status", {})
    is_initial_node = (
        node_status.get("pass_final_boss_count", 0) == 0
        and node_status.get("node_count", 0) == 0
    )
    reroll_empty_deck = _get_config_value(task, "刷空档", False)
    if initial_card_name and reroll_empty_deck is True and is_initial_node:
        task.log_info(
            "“刷初始卡牌”和“刷空档”不能同时进行，"
            "本轮优先执行“刷初始卡牌”"
        )
    if initial_card_name and is_initial_node:
        return handle_initial_node_task(
            "传说卡牌",
            f"刷初始卡牌「{initial_card_name}」",
        )

    if reroll_empty_deck is True and is_initial_node:
        return handle_initial_node_task("移除2张", "刷空档")

    # 检查任务区域中是否有 treasure 特征
    treasure_box = task.box_of_screen(0.477, 0.336, 0.841, 0.540)
    treasure_features = task.find_feature(
        feature_name="treasure",
        box=treasure_box,
        threshold=0.7,
    )
    if treasure_features:
        task.log_info("检测到事件任务区域中有treasure特征，优先点击")
        task.click_box(treasure_features[0])
        task.sleep(2)
        return True

    # 读取拉黑任务列表
    blacklist = _get_config_value(task, '拉黑任务', ["咒术卡牌"])
    blacklist = list(blacklist) if isinstance(blacklist, (list, tuple)) else []
    if blacklist:
        # 过滤掉描述包含拉黑关键词的任务
        filtered_tasks = [
            t for t in tasks_info
            if not any(is_subsequence(bk, t['description']) for bk in blacklist)
        ]
        if len(filtered_tasks) < len(tasks_info):
            task.log_info(f"拉黑任务关键词: {blacklist}，过滤前{len(tasks_info)}个，过滤后{len(filtered_tasks)}个")
            for t in tasks_info:
                if t not in filtered_tasks:
                    task.log_info(f"  已拉黑: 描述: {t['description']}")
        # 如果全部被拉黑，兜底用原列表
        if not filtered_tasks:
            task.log_info("所有任务均被拉黑，兜底使用原列表")
            filtered_tasks = tasks_info
        tasks_info = filtered_tasks

    equipment_priority = []
    for slot in range(1, 4):
        equipment_priority.extend(
            _get_card_list(task, f"装备{slot}号位优先级")
        )
    priority = [
        *equipment_priority,
        *_get_card_list(task, "任务优先级"),
    ]
    chosen = None
    for keyword in priority:
        for t in tasks_info:
            if is_subsequence(keyword, t['description']):
                chosen = t
                task.log_info(f"优先选择「{keyword}」-> 描述: {t['description']}")
                break
        if chosen is not None:
            break

    if chosen is None and task.name == "自动卡厄思模式":
        attack_event_features = task.find_feature(
            feature_name="attack_event",
            threshold=0.95,
        ) or []
        if attack_event_features:
            attack_event = max(
                attack_event_features,
                key=lambda feature: feature.confidence,
            )
            task.log_info(
                f"未命中任务优先级，检测到attack_event特征，"
                f"相似度={attack_event.confidence:.4f}，点击进入战斗任务"
            )
            task.click_box(attack_event)
            task.sleep(1)
            return True

    if chosen is None:
        chosen = random.choice(tasks_info)
        task.log_info(
            f"未命中优先级描述，从{len(tasks_info)}个可选任务中随机选择: "
            f"{chosen['description']}"
        )

    click_event_option(chosen)
    task.sleep(1)
    return True


# 运行中页面右上角"退出"按钮的固定坐标（与 `_finish_only_first_layer` 退出
# 第一层用的是同一个位置）。
RUN_EXIT_FALLBACK_X = 0.959
RUN_EXIT_FALLBACK_Y = 0.050
# 固定坐标框的半宽/半高（归一化）。只用来给 click_box 一个"点"，大小不影响点击
# 位置（click_box 点的是框中心）。
RUN_EXIT_FALLBACK_HALF = 0.02


def _find_run_exit_box(task: TriggerTask):
    """运行中页面右上角的"退出"按钮（点击后弹出「逃脱」确认弹窗）。

    优先用 OCR 在右上角找「退出/逃脱/撤退」文本，找不到就回退到右上角**固定
    坐标**（与 `_finish_only_first_layer` 退出第一层用的是同一个位置）。

    固定坐标兜底不要求该处已经识别出文本：过渡帧中右上角尚未渲染时，
    `find_box_at_point` 可能返回 None，调用方会误判为没有退出按钮并继续
    进入Boss；直接返回固定坐标框可以避免这种误判。
    """
    region = (0.900, 0.005, 0.998, 0.095)
    box = next(
        (
            text_box for text_box in task.all_texts
            if region[0] <= (text_box.x + text_box.width / 2) / task.width <= region[2]
            and region[1] <= (text_box.y + text_box.height / 2) / task.height <= region[3]
            and (
                "退出" in text_box.name
                or _get_game_text(task, "逃脱") in text_box.name
                or "撤退" in text_box.name
            )
        ),
        None,
    )
    if box:
        return box
    task.log_info(
        f"右上角未识别到「退出」文本，回退到固定坐标"
        f"({RUN_EXIT_FALLBACK_X:.3f}, {RUN_EXIT_FALLBACK_Y:.3f})点击「退出」"
    )
    return task.box_of_screen(
        RUN_EXIT_FALLBACK_X - RUN_EXIT_FALLBACK_HALF,
        RUN_EXIT_FALLBACK_Y - RUN_EXIT_FALLBACK_HALF,
        RUN_EXIT_FALLBACK_X + RUN_EXIT_FALLBACK_HALF,
        RUN_EXIT_FALLBACK_Y + RUN_EXIT_FALLBACK_HALF,
        name="退出",
    )


def _target_equipment_snapshot(task: TriggerTask):
    """读取三个装备位的当前状态并判断目标装备是否全部达标。

    Returns:
        (bool, list[str]): (是否全部达标, 未达标装备位的说明列表)
    """
    equipment = _equipment_state(task)
    descriptions = equipment.get("descriptions") or []
    current_states = []
    target_names_per_slot = []
    for slot in range(3):
        name, _ = _current_equipment_for_slot(task, equipment, slot)
        description = descriptions[slot] if slot < len(descriptions) else ""
        current_states.append((name, description or ""))
        target_names_per_slot.append(_equipment_priority(task, slot))
    completed, missing = slots_all_completed(
        current_states,
        target_names_per_slot,
        _get_card_list(task, "装备刻印优先级"),
    )
    return completed, missing


def _pause_for_manual_review():
    """不达标又找不到退出按钮时暂停整个执行器，把画面交给人工处理。

    用的是和"第几层boss前自动暂停"同一套暂停机制（`og.executor.pause()`），
    这里只负责发暂停请求。抽成独立函数是为了让"该不该暂停"这层判断能脱离 ok
    环境做单元测试（测试里 patch 掉本函数即可）。
    """
    from ok import og
    og.executor.pause()


def _maybe_escape_for_engraving_pro_mode(task: TriggerTask, is_final_boss: bool):
    """刻印专业模式：第二个Boss房间前检查装备/刻印，达标停止程序，不达标退出重刷。

    检查点固定为「第二个Boss房间前」（见 `is_pro_mode_escape_check_point`）：
    只有当路线页面上已经没有普通节点、即将进入第二个Boss（地图二的关底Boss）
    时才检查。此时地图二的所有节点（包括商店）都已经走完、商店里的装备也已经
    全部选完，再判断本局值不值得继续打第二个Boss。达标标准是目标主战员三个
    装备位全部拿到「目标名+任意一项目标刻印」，具体判定见 `slots_all_completed`。

    达标时**不进入最后的Boss房间**：立即停止任务并把画面留给人工检查；不达标则点击
    右上角「退出」，由 `handle_escape` 点「逃脱」后重开一局。连「退出」按钮都拿不到
    时（`_find_run_exit_box` 已做固定坐标兜底，正常不会发生）同样改为暂停等人工处理，
    时同样暂停等待人工处理，不会直接进入Boss把本局打完。

    Args:
        is_final_boss: 当前路线页面是否只剩下最终Boss节点（没有识别到任何
            普通节点）。

    Returns:
        bool: True 表示本帧路线页面处理结束（已经停止程序、点击「退出」开始
            逃脱，或者暂停等待人工处理）。
    """
    if not _get_config_value(task, "刻印专业模式", False):
        return False
    if not _get_config_value(task, "刷装备刻印", False):
        return False
    node_status = getattr(task, "node_status", None)
    if node_status is None:
        return False

    layer_index = node_status.get("pass_final_boss_count", 0)
    if not is_pro_mode_escape_check_point(layer_index, is_final_boss):
        return False

    # 同一个检查点只判定/点击一次，避免"退出"没生效时每帧反复点击同一位置。
    check_point = f"第二个Boss房间前@第{layer_index + 1}层"

    # 不达标且找不到退出按钮时进入兜底暂停：画面留在最终Boss房间前，脚本不能
    # 往下走"点击进入Boss"的分支。debug 模式下暂停不会阻塞任务线程（逐帧逻辑
    # 照常执行），所以必须靠这个标记把本帧一直占住。
    if node_status.get("pro_mode_review_paused") == check_point:
        task.sleep(1)
        return True

    if node_status.get("pro_mode_escape_checked") == check_point:
        return False
    node_status["pro_mode_escape_checked"] = check_point

    completed, missing = _target_equipment_snapshot(task)
    # 达标时不去找「退出」按钮：既不点退出，也不点进入Boss。
    exit_box = None if completed else _find_run_exit_box(task)
    action = decide_pro_mode_check_point_action(completed, exit_box is not None)
    if action == PRO_MODE_ACTION_STOP_FOR_REVIEW:
        # 达标即认为本局已刷出指定存档，停止自动化，保留当前画面供人工检查。
        task.log_info(
            f"刻印专业模式: 检查点[{check_point}]三个装备位均已达标，"
            "已刷出指定存档，停止程序"
        )
        task.disable()
        return True

    if action == PRO_MODE_ACTION_ESCAPE:
        task.log_info(
            f"刻印专业模式: 检查点[{check_point}]未达标"
            f"（{'；'.join(missing)}），点击退出并逃脱，重开一局"
        )
        task.click_box(exit_box)
        task.sleep(1)
        return True

    # action == PRO_MODE_ACTION_PAUSE_NO_EXIT：兜底，正常走不到（`_find_run_exit_box`
    # 已经改成固定坐标，正常一定给得出框）。若仍走到这里，则暂停等待人工处理，
    # 并用 pro_mode_review_paused 占住本帧，避免后续"检测到最终boss节点，
    # 点击进入"分支执行。
    node_status["pro_mode_review_paused"] = check_point
    task.log_info(
        f"刻印专业模式: 检查点[{check_point}]未达标"
        f"（{'；'.join(missing)}），且未找到「退出」按钮，暂停程序等待人工处理"
    )
    _pause_for_manual_review()
    task.sleep(5)
    return True


def handle_route_selection(task: TriggerTask):
    """路线选择页面: 识别节点类型，按优先级排序后依次点击所有节点，每次间隔1秒。
    同时负责节点计数：离开路线页面时 node_count +1。"""
    position_box = task.box_of_screen(0.335, 0.568, 0.453, 0.751)
    position_feature = task.find_feature(feature_name="position", box=position_box)
    cant_receive = find_box_at_point(task, 0.186, 0.850)
    is_route_page = position_feature or (cant_receive and "无法接收到梦境号" in cant_receive.name)

    # 如果当前页面不是路线选择页面，但 enter_new_node 为 True（刚离开路线页面），计数+1
    if not is_route_page:
        if hasattr(task, 'node_status') and task.node_status.get('enter_new_node', False):
            task.node_status['enter_new_node'] = False
            task.node_status['node_count'] += 1
            task.log_info(f"离开路线选择页面，当前节点计数: {task.node_status['node_count']}")
        return False

    # 是路线选择页面，标记进入新节点
    if hasattr(task, 'node_status'):
        task.node_status['enter_new_node'] = True

    task.log_info("检测到路线选择页面，按优先级依次点击节点")

    # 更新节点状态：进入路线选择页面时 flash_or_rest 置为 True
    if hasattr(task, 'node_status'):
        task.node_status['flash_or_rest'] = True
        task.log_info("检测到路线选择页面，更新 node_status['flash_or_rest']=True")
        # 检查"进入商店"配置，若为 True 则同时更新 shop 状态
        if _get_config_value(task, '进入商店', False):
            task.node_status['shop'] = True
            task.log_info(f"进入商店配置为True，更新 node_status['shop']=True")
    task.sleep(1)

    route_box = task.box_of_screen(0.656, 0.053, 0.977, 0.908)
    node_feature_types = {
        "safezone": "休息",
        "enemy": "小怪",
        "elite": "精英",
        "event": "事件",
        "settlement": "结算",
    }
    nodes = []
    for feature_name, node_type in node_feature_types.items():
        for feature_box in task.find_feature(feature_name=feature_name, box=route_box):
            nodes.append({
                "feature_name": feature_name,
                "node_type": node_type,
                "box": feature_box,
                "special_features": [],
            })

    # 找不到任何普通节点类型特征时，当前路线节点即为boss。
    if not nodes:
        if hasattr(task, 'node_status'):
            task.node_status['reach_final_boss'] = True
            task.node_status['node_type'] = "boss"

        # 刻印专业模式：第二个Boss房间前检查目标主战员的装备与刻印是否达标，
        # 不达标就直接退出本局（后续由 handle_escape 点击「逃脱」）重刷。放在
        # 进入Boss、暂停之前，保证「一定要在第二个Boss房间前」完成判断；此时
        # 地图二的节点（含商店）都已经走完，商店里的装备也已经全部选完。
        if _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True):
            return True

        task.log_info("检测到最终boss节点，点击进入")

        # 检查"第几层boss前自动暂停"配置
        pause_config = _get_config_value(task, '第几层boss前自动暂停', "不暂停")
        if pause_config != "不暂停":
            try:
                pause_layer = int(pause_config)
                current_layer = task.node_status.get('pass_final_boss_count', 0)
                if pause_layer - 1 == current_layer:
                    task.log_info(f"配置在第{pause_layer}层boss前暂停（当前已通过{current_layer}层），暂停工具")
                    from ok import og
                    og.executor.pause()
                    task.sleep(5)
                    return True
            except (ValueError, TypeError):
                pass

        _move_and_click(task, 0.815, 0.492)
        task.sleep(2)
        return True

    def relative_center(box):
        return (
            (box.x + box.width / 2) / task.width,
            (box.y + box.height / 2) / task.height,
        )

    # 特殊特征优先级：负数高于普通节点，正数低于普通节点。
    special_feature_priorities = {
        "shop": -1,
        "kalei": -1,
        "seal": -1,
        "hard": 1,
    }

    # 每个特殊特征只归属到距离最近的一个节点类型特征。
    for special_name in special_feature_priorities:
        for special_box in task.find_feature(feature_name=special_name, box=route_box):
            special_x, special_y = relative_center(special_box)
            nearest_node = min(
                nodes,
                key=lambda node: (
                    (relative_center(node["box"])[0] - special_x) ** 2
                    + (relative_center(node["box"])[1] - special_y) ** 2
                ),
            )
            nearest_node["special_features"].append(special_name)
            task.log_info(
                f"特殊特征「{special_name}」分配给"
                f"「{nearest_node['node_type']}」节点"
            )

    # 第一层/第二层分别取各自的路线优先级配置（0=第一层，>=1=第二层）。
    layer_index = (getattr(task, "node_status", None) or {}).get(
        "pass_final_boss_count", 0
    )
    priority = _get_route_priority(task, layer_index)
    task.log_info(f"第{layer_index + 1}层路线优先级配置: {priority}")
    task.log_info(
        f"识别到的路线节点: "
        f"{[(node['node_type'], node['special_features']) for node in nodes]}"
    )

    priority_index = {node_type: index for index, node_type in enumerate(priority)}

    # 优先级列表里写了「商店」时，带商店标志的节点按「商店」在列表里的位置排序；
    # 没写时商店节点固定最高优先级（不受节点类型顺序影响）。
    shop_rank = priority_index.get("商店")

    def sort_key(node):
        has_shop = "shop" in node["special_features"]
        if shop_rank is None:
            shop_priority = 0 if has_shop else 1
            type_priority = priority_index.get(node["node_type"], len(priority))
        else:
            shop_priority = 1
            type_priority = (
                shop_rank if has_shop
                else priority_index.get(node["node_type"], len(priority))
            )
        if node["node_type"] == "结算":
            type_priority = len(priority) + 1
        special_priority = min(
            (special_feature_priorities[name] for name in node["special_features"]),
            default=0,
        )
        center_x, center_y = relative_center(node["box"])
        return shop_priority, type_priority, special_priority, center_y, center_x

    # 先根据左下角小地图的完整连通关系规划路线，再用右侧当前可点击节点
    # 校验规划结果。两边识别不一致时，使用当前节点识别结果兜底。
    map_info = recognize_map_connections(task)
    route_plan = find_best_map_route_by_priority(map_info, priority)
    visible_nodes = sorted(nodes, key=lambda item: relative_center(item["box"])[1])
    node = None
    if route_plan and route_plan["next_row"] is not None:
        planned_index = route_plan["next_row"] - 1
        if 0 <= planned_index < len(visible_nodes):
            planned_node = visible_nodes[planned_index]
            expected_specials = {
                name.removesuffix("_in_map")
                for name in route_plan["next_special_features"]
            }
            actual_specials = set(planned_node["special_features"])
            type_matches = (
                planned_node["node_type"] == route_plan["next_node_type"]
            )
            specials_match = actual_specials == expected_specials
            task.log_info(
                f"小地图规划路线={route_plan['route']}，"
                f"下一列第{route_plan['next_row']}个节点，"
                f"预计={route_plan['next_node_type']}+{sorted(expected_specials)}，"
                f"当前节点识别={planned_node['node_type']}+{sorted(actual_specials)}"
            )
            if type_matches and specials_match:
                node = planned_node
                task.log_info("小地图规划与当前节点识别一致，按规划结果进入")
            else:
                task.log_info(
                    "小地图规划与当前节点识别不一致，"
                    "改用当前节点的路线优先级兜底选择"
                )
        else:
            task.log_info(
                f"小地图规划要求进入下一列第{route_plan['next_row']}个节点，"
                f"但当前仅识别到{len(visible_nodes)}个节点，改用优先级兜底"
            )
    else:
        task.log_info("小地图路线规划失败，改用当前节点的路线优先级兜底")

    if node is None:
        node = sorted(nodes, key=sort_key)[0]

    # 更新 node_type 为最优先的节点类型
    if hasattr(task, 'node_status'):
        task.node_status['node_type'] = node["node_type"]
        task.log_info(f"更新 node_type 为「{node['node_type']}」")

    center_x, center_y = relative_center(node["box"])
    click_x = center_x - 0.095
    click_y = center_y - 0.0065
    task.log_info(
        f"点击{node['node_type']}节点"
        f"（特殊特征: {node['special_features']}，位置: {click_x:.3f}, {click_y:.3f}）"
    )
    _move_and_click(task, click_x, click_y)

    task.sleep(2)

    return True


def handle_obtain_reward(task: TriggerTask):
    """获得奖励页面: 点击领取。若此时 reach_final_boss 为 True，说明已通关关底boss，过层+1并重置层状态。"""
    box = find_box_at_point(task, 0.924, 0.922)
    if box and _clean_match(box.name, "获得"):
        task.log_info("检测到获得奖励页面，点击领取")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_leave(task: TriggerTask):
    """离开按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "离开"):
        if is_button_active(task, box):
            task.log_info("检测到离开按钮，点击离开")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("离开按钮未激活（灰色），跳过点击")
            return False
    return False
def handle_next_step(task: TriggerTask):
    """通用"下一步"按钮: 在区域(0.833,0.885,0.954,0.957)内检测文本，编辑距离<=2即匹配。"""
    x1, y1, x2, y2 = 0.833, 0.885, 0.954, 0.957
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            if _edit_distance(b.name, "下一步", max_dist=2):
                task.log_info(f"检测到下一步按钮「{b.name}」，点击")
                task.click_box(b)
                task.sleep(1)
                return True
    return False


def handle_craft(task: TriggerTask):
    """合成按钮。"""
    box = find_box_at_point(task, 0.938, 0.903)
    if box and _clean_match(box.name, "合成"):
        if is_button_active(task, box):
            task.log_info("检测到合成按钮，点击合成")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("合成按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_select(task: TriggerTask):
    """通用"选择"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "选择"):
        if is_button_active(task, box):
            task.log_info("检测到选择按钮，点击选择")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("选择按钮未激活（灰色），跳过点击")
            return False
    return False


def _find_rest_feature(task: TriggerTask):
    """在休息区域查找rest特征，命中时输出置信度。"""
    search_box = task.box_of_screen(0.157, 0.503, 0.467, 0.863)
    rest_feature = task.find_one(feature_name="rest", box=search_box)
    if rest_feature:
        task.log_info(f"检测到rest特征，匹配置信度: {rest_feature.confidence:.2%}")
    return rest_feature


def _wait_for_rest_confirm(task: TriggerTask):
    """等待休息操作后的确认按钮出现。"""
    confirm_boxes = task.wait_ocr(
        0.170, 0.554, to_x=0.855, to_y=0.769,
        match=re.compile(r"确认"), time_out=2,
    )
    if not confirm_boxes:
        task.log_info("等待休息确认按钮超时")
        return False
    return True


def handle_rest(task: TriggerTask):
    """休息界面: 根据血量、信用点和冥想状态选择休息或冥想。"""
    rest_feature = _find_rest_feature(task)
    free_text = _get_region_text(task, (0.154, 0.602, 0.359, 0.847))
    flash_or_rest = (
        hasattr(task, 'node_status')
        and task.node_status.get('flash_or_rest', False)
    )
    can_rest = bool(rest_feature and "免费" in free_text and flash_or_rest)

    meditation_region = (0.671, 0.433, 0.945, 0.801)
    meditate_feature = task.find_one(
        feature_name="meditate",
        box=task.box_of_screen(*meditation_region),
    )
    meditation_cost_boxes = [
        text_box for text_box in task.all_texts
        if re.fullmatch(r"\d+", text_box.name.strip())
        and meditation_region[0] <= (text_box.x + text_box.width / 2) / task.width <= meditation_region[2]
        and meditation_region[1] <= (text_box.y + text_box.height / 2) / task.height <= meditation_region[3]
    ]
    if meditate_feature and meditation_cost_boxes:
        feature_center = (
            meditate_feature.x + meditate_feature.width / 2,
            meditate_feature.y + meditate_feature.height / 2,
        )
        meditation_cost_box = min(
            meditation_cost_boxes,
            key=lambda text_box: (
                (text_box.x + text_box.width / 2 - feature_center[0]) ** 2
                + (text_box.y + text_box.height / 2 - feature_center[1]) ** 2
            ),
        )
        meditation_cost = int(meditation_cost_box.name.strip())
    else:
        meditation_cost = None

    meditation_state = _member_deck_state(task).get("冥想", {})
    has_pending_meditation = (
        isinstance(meditation_state, dict)
        and any(value is True for value in meditation_state.values())
    )
    current_credit = _get_current_credit(task)
    meditation_credit_threshold = _get_config_value(
        task, "多少信用点以上冥想", 300,
    )
    try:
        meditation_credit_threshold = int(meditation_credit_threshold)
    except (TypeError, ValueError):
        meditation_credit_threshold = 300
    can_meditate = bool(
        flash_or_rest
        and meditate_feature
        and meditation_cost is not None
        and has_pending_meditation
        and current_credit > meditation_credit_threshold
        and current_credit > meditation_cost
    )

    if can_rest and can_meditate:
        hp_percent = _get_current_hp_percent(task)
        choose_rest = hp_percent is not False and hp_percent < 50
        task.log_info(
            f"休息与冥想均可用，当前血量="
            f"{hp_percent if hp_percent is not False else '未识别'}%，"
            f"选择{'休息' if choose_rest else '冥想'}"
        )
    else:
        choose_rest = can_rest

    if choose_rest:
        task.log_info("检测到休息界面，点击休息")
        task.move_relative(
            (rest_feature.x + rest_feature.width / 2) / task.width,
            (rest_feature.y + rest_feature.height / 2) / task.height,
        )
        task.click_box(rest_feature)
        if not _wait_for_rest_confirm(task):
            return True
        task.node_status['flash_or_rest'] = False
        return True

    if can_meditate:
        pending_cards = [
            name for name, pending in meditation_state.items() if pending is True
        ]
        task.log_info(
            f"检测到可冥想卡牌{pending_cards}，当前信用点={current_credit}，"
            f"冥想费用={meditation_cost}，点击冥想"
        )
        task.click_box(meditate_feature)
        if not _wait_for_rest_confirm(task):
            return True
        task.node_status['flash_or_rest'] = False
        return True

    if rest_feature and "免费" not in free_text:
        task.log_info("检测到rest特征，但休息区域未找到「免费」，跳过点击休息")

    # 检测是否需要进入德朗商店
    shop_box = find_box_at_point(task, 0.360, 0.138)
    if shop_box and "德朗商店" in shop_box.name and hasattr(task, 'node_status') and task.node_status.get('shop', False):
        task.log_info("检测到德朗商店，且 node_status['shop']=True，进入商店")
        task.click_box(shop_box)
        task.sleep(2)
        return True
    return False


def _shop_engraving_rejected_names(task: TriggerTask):
    """获取本次货架周期内已判定"不满足刻印进展"的装备名集合（已做标点符号
    归一化，避免货架列表页和购买详情页两次OCR识别的标点差异导致漏判）。

    货架刷新（免费或花费信用点）之后要调用 `_clear_shop_engraving_rejected`
    清空这个记录，否则同名的新装备也会被误跳过。
    """
    node_status = getattr(task, "node_status", None)
    if node_status is None:
        return set()
    rejected = node_status.get("shop_engraving_rejected_names")
    if not isinstance(rejected, set):
        rejected = set()
        node_status["shop_engraving_rejected_names"] = rejected
    return rejected


def _is_shop_engraving_rejected(task: TriggerTask, item_name):
    """判断某个装备名（归一化后）是否已在本次货架周期内被判定不满足刻印。"""
    normalized_name = _normalize_equipment_name(item_name)
    if not normalized_name:
        return False
    return normalized_name in _shop_engraving_rejected_names(task)


def _mark_shop_engraving_rejected(task: TriggerTask, item_name):
    """记录某件装备（归一化后）本次货架周期内已被判定为不满足刻印进展，
    避免重复点开。"""
    normalized_name = _normalize_equipment_name(item_name)
    if normalized_name:
        _shop_engraving_rejected_names(task).add(normalized_name)


def _clear_shop_engraving_rejected(task: TriggerTask):
    """货架刷新后清空刻印黑名单，让新一轮商品重新参与评估。"""
    node_status = getattr(task, "node_status", None)
    if node_status is not None:
        node_status["shop_engraving_rejected_names"] = set()


# ── 商店刻印角标检测 ───────────────────────────────────────────

_engraving_badge_templates = None
# 角标模板取同一分辨率（1600x900）下的游戏图标，正常角标匹配分数通常
# 高于 0.9；0.75 过低，装备图标自身的美术图案也可能过线。每个商品格子
# 取模板匹配最高置信度。
_ENGRAVING_BADGE_THRESHOLD = 0.85

# 角标的"边界区间"下沿。低于阈值但达到该分数的格子不能直接判定为
# "确定没有角标"；这些格子交给详情页描述判定，避免误跳过真实刻印装备。
_ENGRAVING_BADGE_BORDERLINE_FLOOR = 0.80


# 一次货架角标检测的结果。
# - engraved_indexes: 命中（置信度 >= 阈值）的信用点图标索引集合；
# - badge_count: 命中数，调用方用它判断"本货架有没有刻印装备"；
# - borderline_confidences: {索引: 置信度}，落在边界区间（>= 下沿且 < 阈值）
#   的格子，调用方对这些商品不跳过、改成进详情页看描述。
ShopBadgeDetection = namedtuple(
    "ShopBadgeDetection",
    ["engraved_indexes", "badge_count", "borderline_confidences"],
)


def _short_template_name(template_name):
    """模板文件名转成短标签（engraving_badge_light.png -> light）。"""
    stem = str(template_name).replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    return stem.replace("engraving_badge_", "") or stem


def _load_engraving_badge_templates():
    """懒加载刻印角标模板图片（光/暗各一张），全局缓存只读一次磁盘。

    模板来源：tests/images 中的刻印图标小图，已裁切到 assets/images 下。
    模板分辨率与 1600x900 游戏截图一致，运行时通过多尺度匹配适配其他分辨率。
    """
    global _engraving_badge_templates
    if _engraving_badge_templates is not None:
        return _engraving_badge_templates
    _engraving_badge_templates = []
    assets_dir = os.path.join("ok_tasks", "assets", "images")
    for name in ("engraving_badge_light.png", "engraving_badge_dark.png"):
        path = os.path.join(assets_dir, name)
        data = np.fromfile(path, dtype=np.uint8)
        if data is None or data.size == 0:
            print(f"[刻印角标] 模板加载失败: {path}")
            continue
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            print(f"[刻印角标] 模板解码失败: {path}")
            continue
        _engraving_badge_templates.append((name, img))
    return _engraving_badge_templates


# 商品格子的取点（相对该商品信用点图标中心的归一化偏移）。
#
# 信用点图标在商品格子左下的价格行，商品图标与名称都在它上方，角标只会画在
# 商品图标范围内，所以角标只在本商品的格子里找，格子外的匹配一律不采信。
# 只在商品自己的格子内匹配，避免全货架匹配后按最近信用点图标归属时，
# 将相邻商品或其他图案误算到当前商品。
_SHOP_CELL_LEFT = 0.030
_SHOP_CELL_RIGHT = 0.150
_SHOP_CELL_TOP = 0.360
_SHOP_CELL_BOTTOM_GAP = 0.020


def _shop_icon_center(width, height, credit_icon):
    """某个商品信用点图标的中心（归一化坐标）。"""
    return (
        (credit_icon.x + credit_icon.width / 2) / width,
        (credit_icon.y + credit_icon.height / 2) / height,
    )


def _shop_product_cell(width, height, credit_icon):
    """商品格子区域（归一化 x0, y0, x1, y1），以该商品的信用点图标为锚点推出。"""
    icon_x, icon_y = _shop_icon_center(width, height, credit_icon)
    return (
        max(0.0, icon_x - _SHOP_CELL_LEFT),
        max(0.0, icon_y - _SHOP_CELL_TOP),
        min(1.0, icon_x + _SHOP_CELL_RIGHT),
        min(1.0, icon_y - _SHOP_CELL_BOTTOM_GAP),
    )


def _shop_item_name_region(width, height, credit_icon):
    """商品名称的读取区域（归一化 x0, y0, x1, y1）。

    与商品格子共用同一个锚点（信用点图标中心），保证两处取的是同一个位置。
    """
    icon_x, icon_y = _shop_icon_center(width, height, credit_icon)
    return (
        max(0.0, icon_x - 0.013),
        max(0.0, icon_y - 0.247),
        min(1.0, icon_x + 0.120),
        min(1.0, icon_y - 0.105),
    )


def _detect_shop_engraving_badges(task, credit_icons):
    """检测商店货架上的刻印角标，按"商品格子"归属到商品索引。

    只在"刷装备刻印"模式开启时调用。模板匹配识别装备图标上的刻印角标
    （光/暗两种），每个商品只在自己的格子里取最高置信度，超过
    _ENGRAVING_BADGE_THRESHOLD 才算命中。

    判定按商品格子归属：先用每个商品的信用点图标推出它的格子，再在格子内
    取最高置信度；落在格子外的匹配一律不采信，避免相邻商品之间串位。

    Args:
        task: 任务对象，需要 frame / width / height。
        credit_icons: 货架上各商品的信用点图标（find_feature("credit_icon")）。
    返回值:
        ShopBadgeDetection:
        - engraved_indexes: set[int]，有角标命中的信用点图标索引
        - badge_count: int，命中的角标数（用于判断检测是否有效）
        - borderline_confidences: dict[int, float]，落在边界区间的格子

    调用方把 badge_count == 0 当作"本货架没有刻印装备"来用：没有角标就整排
    跳过，不再回退到"逐一进详情页检查"。唯一例外是 borderline_confidences
    里的商品：分数紧贴阈值，不能确认它到底有没有角标，这些商品交给
    购买页的描述判定，避免把真实的刻印装备当成"没有角标"漏掉。
    """
    templates = _load_engraving_badge_templates()
    if not templates:
        return ShopBadgeDetection(set(), 0, {})
    frame = task.frame
    if frame is None:
        return ShopBadgeDetection(set(), 0, {})

    h, w = frame.shape[:2]
    # 只在商店商品区域内搜索，排除顶部标题栏和底部刷新按钮
    y1, y2 = int(0.35 * h), int(0.85 * h)
    y1_start = y1
    search_area = frame[y1:y2, :]

    cells = []
    for index, credit_icon in enumerate(credit_icons or []):
        x0, y0, x1, y1_box = _shop_product_cell(w, h, credit_icon)
        cells.append({
            "index": index,
            "box": (x0 * w, y0 * h, x1 * w, y1_box * h),
            "best": None,
        })
    if not cells:
        return ShopBadgeDetection(set(), 0, {})

    # 多尺度模板匹配，适配不同游戏分辨率；每个格子只保留最高分。
    for _, tpl in templates:
        for scale in (0.8, 0.9, 1.0, 1.1, 1.2, 1.3):
            th, tw = tpl.shape[:2]
            new_w = max(1, int(tw * scale))
            new_h = max(1, int(th * scale))
            scaled = cv2.resize(tpl, (new_w, new_h))
            if scaled.shape[0] > search_area.shape[0] or scaled.shape[1] > search_area.shape[1]:
                continue
            result = cv2.matchTemplate(
                search_area, scaled, cv2.TM_CCOEFF_NORMED,
            )
            for cell in cells:
                x0, y0, x1, y1_box = cell["box"]
                col0 = max(0, int(x0))
                col1 = min(result.shape[1], int(x1) + 1)
                row0 = max(0, int(y0) - y1_start)
                row1 = min(result.shape[0], int(y1_box) - y1_start + 1)
                if row0 >= row1 or col0 >= col1:
                    continue
                patch = result[row0:row1, col0:col1]
                local = np.unravel_index(int(np.argmax(patch)), patch.shape)
                confidence = float(patch[local])
                best = cell["best"]
                if best is not None and confidence <= best:
                    continue
                cell["best"] = confidence

    engraved_indexes = set()
    borderline_confidences = {}
    for cell in sorted(cells, key=lambda item: item["index"]):
        best = cell["best"]
        confidence = best if best is not None else 0.0
        if best is None:
            continue
        if confidence >= _ENGRAVING_BADGE_THRESHOLD:
            engraved_indexes.add(cell["index"])
        elif confidence >= _ENGRAVING_BADGE_BORDERLINE_FLOOR:
            # 存 4 位小数，避免 float32 尾数造成无意义的配置抖动。
            borderline_confidences[cell["index"]] = round(confidence, 4)

    return ShopBadgeDetection(
        engraved_indexes, len(engraved_indexes), borderline_confidences,
    )


def _shop_removal_credit_floor(task: TriggerTask) -> int:
    """「商店删卡信用点下限」配置值（0 表示专业模式下不删卡）。"""
    floor = _get_config_value(task, "商店删卡信用点下限", 0)
    try:
        return int(floor)
    except (TypeError, ValueError):
        return 0


def _shop_removal_block_reason(task: TriggerTask) -> str:
    """刻印专业模式拦下德朗商店删卡的原因；返回空串表示允许删卡。

    专业模式依附于「刷装备刻印」：两个开关都打开时，只有当前信用点**超过**
    「商店删卡信用点下限」才允许花信用点删卡；下限为 0（默认）时专业模式下
    本局完全不删卡，避免把信用点花在删卡上、影响刷刻印装备。信用点
    读不到（OCR 失败按 0 处理）时同样按不删卡处理，避免误花信用点。
    """
    if not (
        _get_config_value(task, "刷装备刻印", False)
        and _get_config_value(task, "刻印专业模式", False)
    ):
        return ""
    floor = _shop_removal_credit_floor(task)
    if floor <= 0:
        return "刻印专业模式已开启，且未配置「商店删卡信用点下限」（0 表示不删卡）"
    current_credit = _get_current_credit(task)
    if current_credit <= floor:
        return (
            f"刻印专业模式下当前信用点{current_credit}"
            f"未超过「商店删卡信用点下限」{floor}"
        )
    return ""


def _shop_removal_disabled_by_pro_mode(task: TriggerTask) -> bool:
    """刻印专业模式下德朗商店的「移除卡牌」是否被拦下（原因见 block_reason）。"""
    return bool(_shop_removal_block_reason(task))


# 非专业模式下商店删卡的固定上限。
SHOP_REMOVE_CARD_LIMIT = 5
# 刻印专业模式下「商店删卡次数上限」的默认值（配置缺项/非数字时的兜底）。
SHOP_REMOVE_CARD_LIMIT_PRO_MODE_DEFAULT = 2


def _shop_removal_limit(task: TriggerTask) -> int:
    """本局允许在德朗商店花信用点删卡的次数上限。

    非专业模式沿用固定上限 `SHOP_REMOVE_CARD_LIMIT`（5 张）；「刷装备刻印」
    与「刻印专业模式」都开启时改用「商店删卡次数上限」配置（默认 2，0 表示完全
    不删卡），避免专业模式把信用点都花在删卡上、影响刷刻印装备。

    计数只看商店删卡数（`node_status['shop_removed_card_count']`），事件/三选一里
    删掉的卡牌计入的是 `node_status['removed_card_count']`，不占用商店额度。
    """
    if not (
        _get_config_value(task, "刷装备刻印", False)
        and _get_config_value(task, "刻印专业模式", False)
    ):
        return SHOP_REMOVE_CARD_LIMIT
    limit = _get_config_value(
        task, "商店删卡次数上限", SHOP_REMOVE_CARD_LIMIT_PRO_MODE_DEFAULT,
    )
    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return SHOP_REMOVE_CARD_LIMIT_PRO_MODE_DEFAULT


def _try_remove_shop_card(task: TriggerTask, box):
    """德朗商店: 花信用点移除一张卡牌，真的点了移除返回 True。

    从 handle_shop 里抽出来，方便"刻印模式下降级为低优先级动作"时复用：
    刻印模式下必须等货架上的刻印装备都检查完之后，才轮到这个删卡动作。
    """
    current_credit = _get_current_credit(task)
    shop_removed_card_count = task.node_status.get("shop_removed_card_count", 0)
    task.log_info(f"handle_shop: 当前信用点={current_credit}")
    cost_box = find_box_at_point(task, 0.724, 0.319)
    task.log_info(f"handle_shop: 0.724,0.319处费用文本='{cost_box.name if cost_box else None}'")
    clicked = False
    if cost_box and cost_box.name.isdigit():
        cost = int(cost_box.name)
        remove_limit = _shop_removal_limit(task)
        if shop_removed_card_count >= remove_limit:
            task.log_info(
                f"本局已在商店移除{shop_removed_card_count}张卡牌，"
                f"达到商店删卡上限{remove_limit}，不再使用信用点移除卡牌"
            )
        elif cost <= current_credit and task.node_status['shop'] is True:
            task.log_info(f"德朗商店: 移除卡牌需{cost}信用点，当前{current_credit}，足够，点击移除")
            task.click_box(box)
            task.sleep(1)
            # 点开删卡页还不算删掉：真正的"移除"由 handle_remove 完成，这里先
            # 打标记，那次删除才计入 node_status['shop_removed_card_count']。
            _mark_pending_shop_removal(task)
            clicked = True
        else:
            task.log_info(f"德朗商店: 移除卡牌需{cost}信用点，当前{current_credit}，不足，继续挑选其他商品")
    else:
        task.log_info("handle_shop: 移除卡牌费用读取失败，继续挑选其他商品")
    task.node_status['shop'] = False
    return clicked


def handle_shop(task: TriggerTask):
    """德朗商店: 刻印模式先找刻印装备，再尝试刷新，最后才移除卡牌。"""
    box = find_box_at_point(task, 0.729, 0.261)
    soldout = find_box_at_point(task, 0.727, 0.286)
    if (box and "移除卡牌" in box.name) or (soldout and "售" in soldout.name):
        task.log_info("handle_shop: 通过页面判定（移除卡牌或售罄）")
        remove_card_box = box if (box and "移除卡牌" in box.name) else None
        if soldout and "售" in soldout.name:
            task.log_info(f"德朗商店: 移除卡牌已售罄")
            task.node_status['shop'] = False
            remove_card_box = None
        # 刻印专业模式：默认禁用商店删卡（花信用点移除卡牌），把删卡入口置空，
        # 既不点开移除卡牌页面，也不会在函数末尾执行删卡；只有配置了「商店删卡
        # 信用点下限」且当前信用点超过它时，入口才保留、允许花钱删卡。
        removal_block_reason = _shop_removal_block_reason(task)
        if remove_card_box and removal_block_reason:
            task.log_info(
                f"德朗商店: {removal_block_reason}，跳过「移除卡牌」"
            )
            remove_card_box = None

        # 刻印模式下删卡的优先级要排在"寻找刻印装备"之后：这里先跳过删卡，
        # 等下面的商品扫描确认货架上没有被判定为值得点开的刻印装备之后，
        # 再回头执行删卡，避免一进商店就把信用点花在删卡上。
        defer_remove_card = bool(remove_card_box) and bool(
            _get_config_value(task, "刷装备刻印", False)
        )
        if remove_card_box and not defer_remove_card:
            if _try_remove_shop_card(task, remove_card_box):
                return True

        current_credit = _get_current_credit(task)
        task.log_info(f"德朗商店挑选商品: 当前信用点={current_credit}")
        # 购买页用「刷装备刻印信用点下限」作为闸门：信用点低于该值时一律点「取消」。
        # 这里提前用同一个阈值过滤，避免点开注定被取消的装备，否则会陷入
        # 「点开装备 -> 取消 -> 再点开同一件」的死循环。
        engraving_credit_floor = 0
        if _get_config_value(task, "刷装备刻印", False):
            engraving_credit_floor = _get_config_value(task, "刷装备刻印信用点下限", 0)
            try:
                engraving_credit_floor = int(engraving_credit_floor)
            except (TypeError, ValueError):
                engraving_credit_floor = 0
        credit_icons = sorted(
        task.find_feature(
            feature_name="credit_icon",
            box=task.box_of_screen(0.019, 0.761, 0.979, 0.890),
        ) or [],
        key=lambda feature: feature.x,
        )

        card_type_features = []
        card_type_region = task.box_of_screen(0.032, 0.669, 0.968, 0.799)
        for feature_name in (
            "attack_in_shop",
            "skill_in_shop",
            "enhance_in_shop",
        ):
            for feature in task.find_feature(
            feature_name=feature_name,
            box=card_type_region,
            ) or []:
                card_type_features.append((feature_name, feature))

        card_icon_indexes = set()
        for feature_name, feature in card_type_features:
            if not credit_icons:
                break
            feature_x = feature.x + feature.width / 2
            feature_y = feature.y + feature.height / 2
            closest_index = min(
            range(len(credit_icons)),
            key=lambda index: (
                credit_icons[index].x + credit_icons[index].width / 2 - feature_x
            ) ** 2 + (
                credit_icons[index].y + credit_icons[index].height / 2 - feature_y
            ) ** 2,
            )
            card_icon_indexes.add(closest_index)
            task.log_info(f"德朗商店: {feature_name}特征绑定第{closest_index + 1}个信用点图标")

        card_priority = _get_card_reward_priority(task)
        equipment_priorities = [_equipment_priority(task, slot) for slot in range(3)]

        # 刻印模式预筛选：通过角标模板匹配识别哪些商品是刻印装备，
        # 后续只点击有角标的装备，避免点开无刻印装备浪费时间。
        # 角标识别已验证可靠，所以"没检测到角标"就等于"本货架没有刻印装备"，
        # 整排商品直接跳过，不再回退到"逐一进详情页检查"——回退会把没有角标的
        # 装备也点开，于是"名字对但没有刻印"的装备又会被买走。
        # 角标预筛选的例外（见 name_only_candidate_slot）：槽位"刻印已经拿到、
        # 只差目标名"时，能推进它的候选恰恰是一件命中目标名、自身不带刻印的
        # 装备（同样没有角标）。这里先把三个槽位的当前状态取出来备查。
        equipment = _equipment_state(task)
        equipment_names = equipment.get("names", [])
        equipment_descriptions = equipment.get("descriptions", [])
        engraving_keywords = _get_card_list(task, "装备刻印优先级")
        slot_states = [
            (
                equipment_names[slot] if slot < len(equipment_names) else "",
                equipment_descriptions[slot]
                if slot < len(equipment_descriptions)
                else "",
                equipment_priorities[slot],
            )
            for slot in range(3)
        ]
        engraving_badge_enabled = _get_config_value(task, "刷装备刻印", False)
        # 商品名称先统一读一遍，下面的商品循环复用这份结果。
        item_names = [
            _get_region_text(
                task, _shop_item_name_region(task.width, task.height, icon),
            ).strip()
            for icon in credit_icons
        ]
        engraved_indexes = set()
        borderline_confidences = {}
        if engraving_badge_enabled and credit_icons:
            detection = _detect_shop_engraving_badges(task, credit_icons)
            engraved_indexes = detection.engraved_indexes
            badge_count = detection.badge_count
            borderline_confidences = detection.borderline_confidences
            if badge_count > 0:
                task.log_info(
                    f"德朗商店刻印角标检测: 发现{badge_count}个角标，"
                    f"关联到商品索引={sorted(i + 1 for i in engraved_indexes)}"
                )
            elif borderline_confidences:
                task.log_info(
                    f"德朗商店刻印角标检测: 未发现达到阈值"
                    f"（{_ENGRAVING_BADGE_THRESHOLD:.2f}）的角标，但有"
                    f"{len(borderline_confidences)}件落在边界区间"
                    f"（{_ENGRAVING_BADGE_BORDERLINE_FLOOR:.2f}"
                    f"~{_ENGRAVING_BADGE_THRESHOLD:.2f}），"
                    "这几件进详情页用描述判定，其余商品跳过"
                )
            else:
                task.log_info(
                    "德朗商店刻印角标检测: 未发现任何角标，"
                    "本货架没有刻印装备，跳过全部商品"
                )

        for index, credit_icon in enumerate(credit_icons):
            icon_x = (credit_icon.x + credit_icon.width / 2) / task.width
            icon_y = (credit_icon.y + credit_icon.height / 2) / task.height
            price_box = find_box_at_point(task, icon_x + 0.099, icon_y - 0.001)
            price = _parse_discounted_price(price_box.name) if price_box else None
            item_name = item_names[index] if index < len(item_names) else ""
            is_card = index in card_icon_indexes
            item_type = "卡牌" if is_card else "装备"
            task.log_info(f"德朗商店第{index + 1}个商品: 类型={item_type}，名称=「{item_name}」，价格={price}")
            if not item_name or price is None or price >= current_credit:
                continue

            engraving_mode = (
                not is_card and _get_config_value(task, "刷装备刻印", False)
            )
            if engraving_mode and current_credit < engraving_credit_floor:
                task.log_info(
                    f"德朗商店: 装备「{item_name}」价格{price}虽低于当前信用点"
                    f"{current_credit}，但未达到刷装备刻印下限"
                    f"{engraving_credit_floor}，购买必被取消，跳过该装备"
                )
                continue
            # 角标预筛选：只有带刻印角标的装备才值得点开详情页评估，
            # 没有角标的装备一定不是刻印装备，直接跳过。
            # 例外：槽位"刻印已经拿到、只差目标名"时，能推进它的候选恰恰是一件
            # 命中目标名、自身不带刻印的装备（同样没有角标），必须放行，否则这个
            # 槽位永远补不上名字（见 name_only_candidate_slot）。
            if (
                engraving_mode
                and engraving_badge_enabled
                and index not in engraved_indexes
            ):
                name_only_slot = name_only_candidate_slot(
                    item_name, slot_states, engraving_keywords,
                )
                if name_only_slot is None:
                    borderline_confidence = borderline_confidences.get(index)
                    if borderline_confidence is None:
                        task.log_info(
                            f"德朗商店: 装备「{item_name}」未检测到刻印角标，"
                            "不是刻印装备，跳过"
                        )
                        continue
                    task.log_info(
                        f"德朗商店: 装备「{item_name}」角标置信度="
                        f"{borderline_confidence:.2f} 落在边界区间"
                        f"（{_ENGRAVING_BADGE_BORDERLINE_FLOOR:.2f}"
                        f"~{_ENGRAVING_BADGE_THRESHOLD:.2f}），"
                        "无法确认是否自带刻印，进详情页用描述判定"
                    )
                else:
                    task.log_info(
                        f"德朗商店: 装备「{item_name}」未检测到刻印角标，但"
                        f"{name_only_slot + 1}号位刻印已达标、只差目标名，"
                        "点开详情页确认"
                    )
            if engraving_mode and _is_shop_engraving_rejected(task, item_name):
                task.log_info(
                    f"德朗商店: 装备「{item_name}」本次货架周期内已判定不满足"
                    "刻印进展，跳过，继续检查下一个商品"
                )
                continue
            if is_card:
                if _neutral_card_limit_reached(task):
                    neutral_card_count = task.node_status.get(
                        "neutral_card_count", 0,
                    )
                    neutral_card_limit = _neutral_card_limit(task)
                    task.log_info(
                        f"本局已获得{neutral_card_count}张中立牌，"
                        f"达到固定上限{neutral_card_limit}张，跳过商店卡牌"
                    )
                    continue
                matched_name = next(
                (name for name in card_priority
                 if name and (name in item_name or item_name in name)),
                None,
                )
            elif engraving_mode:
                # 刷装备刻印模式：货架上只能看到名字，看不到刻印描述，
                # 不能靠名字过滤决定要不要点开；只要买得起就先点进详情页，
                # 交给 handle_equipment 的购买页逻辑按刻印判定决定购买或取消。
                matched_name = item_name
            else:
                matched_name = next(
                (canonical_name
                 for priority in equipment_priorities
                 for canonical_name, rank in [_match_equipment_name(item_name, priority)]
                 if rank is not None),
                None,
                )
            if not matched_name:
                continue

            if engraving_mode:
                task.log_info(
                f"德朗商店: {item_type}「{item_name}」（刷装备刻印模式，"
                f"不按名字过滤），价格{price}小于当前信用点{current_credit}，"
                "点击信用点图标查看详情"
                )
            else:
                task.log_info(
                f"德朗商店: {item_type}「{item_name}」命中配置「{matched_name}」，"
                f"价格{price}小于当前信用点{current_credit}，点击信用点图标"
                )
            # 记录本次点开的商品是否带刻印角标：购买页要用它区分
            # "候选真的没有刻印"与"候选自带非目标刻印"这两种在描述文字上
            # 无法区分的情况。能点进来就说明角标检测生效了，这里必然是布尔值。
            node_status = getattr(task, "node_status", None)
            if node_status is not None:
                node_status["shop_opened_has_engraving"] = (
                    index in engraved_indexes if engraving_badge_enabled else None
                )
            task.click_box(credit_icon)
            task.sleep(1)
            return True

        refresh_region_boxes = [
            text_box for text_box in task.all_texts
            if 0.012 <= (text_box.x + text_box.width / 2) / task.width <= 0.258
            and 0.892 <= (text_box.y + text_box.height / 2) / task.height <= 0.979
            and text_box.name.strip()
        ]
        free_box = next(
            (text_box for text_box in refresh_region_boxes if "免费" in text_box.name),
            None,
        )
        if free_box:
            task.log_info("德朗商店没有符合要求的商品，点击「免费」刷新")
            task.click_box(free_box)
            task.sleep(1)
            _clear_shop_engraving_rejected(task)
            return True

        # 免费刷新次数用完后，该区域会改为显示花费信用点刷新的按钮。
        # 只有在"刷装备刻印"开启，且当前信用点超过"刷装备刻印信用点下限"
        # 配置时，才允许花信用点刷新商店，与购买时使用的是同一个阈值配置。
        if _get_config_value(task, "刷装备刻印", False):
            # 该区域里同时有「剩余刷新次数」计数（形如 3/3、0/3）和「刷新费用」
            # 数字（形如 30）。剩余次数为 0 时刷新按钮是灰色的，点了不生效，
            # 必须放弃刷新、把机会让给后面的「离开商店」；否则本函数会一直返回
            # True，脚本卡在商店里反复点击同一个灰色按钮，永远走不出商店。
            remaining_refresh_box = None
            remaining_refresh = None
            for text_box in refresh_region_boxes:
                count_match = re.search(r"(\d+)\s*/\s*(\d+)", text_box.name)
                if count_match:
                    remaining_refresh_box = text_box
                    remaining_refresh = (
                        int(count_match.group(1)), int(count_match.group(2))
                    )
                    break

            # 费用只能取纯数字文本框：用 re.search(r"\d+") 会先命中 0/3 里的 0，
            # 把刷新费用误解析成 0，于是灰色按钮也满足"信用点够花"的条件。
            paid_refresh_box = next(
                (
                    text_box for text_box in refresh_region_boxes
                    if re.fullmatch(r"\d+", text_box.name.strip())
                ),
                None,
            )
            if remaining_refresh and remaining_refresh[0] <= 0:
                task.log_info(
                    f"德朗商店剩余刷新次数为{remaining_refresh[0]}/"
                    f"{remaining_refresh[1]}，刷新按钮已变灰，不刷新商店"
                )
            elif paid_refresh_box:
                refresh_cost = _parse_discounted_price(
                    paid_refresh_box.name.strip()
                )
                current_credit = _get_current_credit(task)
                credit_floor = _get_config_value(task, "刷装备刻印信用点下限", 300)
                try:
                    credit_floor = int(credit_floor)
                except (TypeError, ValueError):
                    credit_floor = 300
                if (
                    current_credit > credit_floor
                    and refresh_cost is not None
                    and refresh_cost > 0
                    and current_credit >= refresh_cost
                ):
                    task.log_info(
                        f"德朗商店没有符合要求的装备，刷装备刻印模式下"
                        f"当前信用点{current_credit}高于下限{credit_floor}，"
                        f"花费{refresh_cost}信用点刷新商店"
                    )
                    task.click_box(remaining_refresh_box or paid_refresh_box)
                    task.sleep(1)
                    _clear_shop_engraving_rejected(task)
                    return True
                task.log_info(
                    f"德朗商店没有符合要求的装备，但当前信用点{current_credit}"
                    f"不满足花费刷新条件（需求下限{credit_floor}，刷新费用"
                    f"{refresh_cost}），不刷新"
                )

        # 刻印模式下删卡的优先级最低：找刻印装备 -> 免费/付费刷新 -> 删卡。
        # 只有货架上没有值得点开的刻印装备、又不能刷新时，才花信用点删卡
        # （刻印模式下删卡在函数开头已经被跳过，这里才真正执行）。
        if (
            defer_remove_card
            and not _shop_removal_disabled_by_pro_mode(task)
            and _try_remove_shop_card(task, remove_card_box)
        ):
            return True

        # 走到这里说明货架上没有值得点开的商品、刷新用不上（按钮已变灰或
        # 信用点不够）、删卡也做不了：本次商店已经逛完，必须清掉 shop 标记。
        # 否则离开商店回到营地页面后，handle_rest 会再一次"检测到德朗商店
        # 且 node_status['shop']=True"点进商店，形成"进店 -> 离开 -> 再进店"
        # 的死循环。
        node_status = getattr(task, "node_status", None)
        if isinstance(node_status, dict):
            node_status["shop"] = False
            task.log_info(
                "德朗商店已无可执行操作，标记 node_status['shop']=False，"
                "本次商店流程结束"
            )

    return False


def handle_view_original(task: TriggerTask):
    """卡牌闪光（查看原件）事件: 按类型特征识别卡牌，并按闪光优先级选择。"""
    box1 = find_box_at_point(task, 0.890, 0.051)
    box2 = find_box_at_point(task, 0.896, 0.131)
    if not ((box1 and (_get_game_text(task, '查看原件') in box1.name or _get_game_text(task, '查看之前的闪光') in box1.name)) or (box2 and (_get_game_text(task, '查看原件') in box2.name or _get_game_text(task, '查看之前的闪光') in box2.name))):
        return False

    cards = recognize_cards(task, page="卡牌闪光页面")
    if not cards:
        return False

    flash_priority = []
    for keyword in _get_card_list(task, '闪光优先级'):
        if not isinstance(keyword, str):
            continue
        normalized_keyword = re.sub(r"\s+", "", keyword)
        if normalized_keyword:
            flash_priority.append(normalized_keyword)
    chosen_card = None
    for desc_keyword in flash_priority:
        for card in cards:
            if is_subsequence(
                desc_keyword,
                card['name'] + "：:" + card['description'],
            ):
                chosen_card = card
                task.log_info(f"优先选择「{card['name']}」({desc_keyword})")
                if (
                    _get_config_value(task, "首层刷特定闪光", False) is True
                    and flash_priority
                    and desc_keyword == flash_priority[0]
                ):
                    task.node_status["get_specific_flash"] = True
                    task.log_info("已命中闪光优先级第一项，记录已获得特定闪光")
                break
            if chosen_card:
                break
        if chosen_card:
            break

    target_boxes, target_click_positions = find_target_card(task)
    matched_meditation_cards = _matching_meditation_card_names(task, cards)
    if matched_meditation_cards:
        meditation_state = _member_deck_state(task)["冥想"]
        meditation_completed = chosen_card is None and not target_boxes
        for configured_name in matched_meditation_cards:
            meditation_state[configured_name] = meditation_completed
            task.log_info(
                f"冥想卡牌「{configured_name}」状态更新为"
                f"{'待冥想' if meditation_completed else '无需冥想'}"
            )

    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"卡牌闪光事件: 检测到target卡牌，点击位置{click_position}"
        )
        _move_and_click(task, *click_position)
        return True

    if not chosen_card:
        chosen_card = random.choice(cards)
        task.log_info(f"随机选择「{chosen_card['name']}」")

    _move_and_click(task, chosen_card['x'], chosen_card['y'])
    return True


def handle_escape(task: TriggerTask):
    """逃脱页面: 检测到逃脱按钮后点击逃脱。"""
    escape_box = find_box_at_point(task, 0.952, 0.928)
    if escape_box and (
        _get_game_text(task, '逃脱') in escape_box.name
        or "脱逃" in escape_box.name
    ):
        task.log_info("检测到逃脱页面，点击逃脱")
        task.click_box(escape_box)
        task.node_status["is_escaped"] = True
        task.sleep(0.5)
        return True
    return False


# def handle_battle_failed(task: TriggerTask):
#     """战斗失败页面: 记录失败并重置boss状态。"""
#     box = find_box_at_point(task, 0.291, 0.718)
#     if box and box.name == "战斗失败":
#         task.log_info("检测到战斗失败，记录失败并重置boss状态")
#         if hasattr(task, 'node_status'):
#             task.node_status['total_rounds'] += 1
#             task.log_info(f"战斗失败，total_rounds={task.node_status['total_rounds']}")
#             task.node_status['pass_final_boss_count'] = 0
#             task.node_status['reach_final_boss'] = False
#             task.node_status['final_boss_battle'] = False
#     return False

def handle_expedition_result(task: TriggerTask):
    """探险结果页面: 如果0.625,0.122处有"探险结果"，则为探险结果页面。
    如果0.928,0.122处有"完成"，则success_rounds+1。"""
    expedition_result_text = _get_game_text(task, "探险结果")
    title_box = find_box_at_point(task, 0.625, 0.122)
    if not (title_box and expedition_result_text in title_box.name):
        return False
    task.sleep(2)
    task.all_texts = _simplify_texts(task.ocr())
    title_box = find_box_at_point(task, 0.625, 0.122)
    if not (title_box and expedition_result_text in title_box.name):
        return False

    task.log_info("检测到探险结果页面")
    complete_box = find_box_at_point(task, 0.928, 0.122)
    failed_box = find_box_at_point(task, 0.296, 0.719)
    if hasattr(task, 'node_status'):
        task.node_status['total_rounds'] += 1
    if complete_box and "完成" in complete_box.name:
        if hasattr(task, 'node_status'):
            task.node_status['success_rounds'] += 1
            task.log_info("出击模式探险结果: 成功")
    elif complete_box and "失败" in complete_box.name:
        if not _get_config_value(task, '只打第一层', False):
            task.log_info("出击模式探险结果: 失败")
        elif task.node_status.get('pass_final_boss_count', 0) == 0:
            task.log_info("出击模式探险结果: 失败")
    elif not complete_box and not failed_box:
        if _get_config_value(task, '只打第一层', False) and task.node_status.get('pass_final_boss_count', 0) >= 1: # 完成第一层任务
            task.log_info("卡厄思模式探险结果: 成功")
        elif not _get_config_value(task, '只打第一层', False) and not task.node_status.get('is_escaped', 0): # 完成了任务且没有逃脱
            task.node_status['success_rounds'] += 1
            task.log_info("卡厄思模式探险结果: 成功")
        else:
            task.log_info("卡厄思模式探险结果: 失败")
    else:
        task.log_info("卡厄思模式探险结果: 失败")
    task.log_info(f"探险完成，成功次数/总次数={task.node_status['success_rounds']}/{task.node_status['total_rounds']}")
    if hasattr(task, 'node_status'):
        reset_mission_status(task)
    return False


def _initial_node_status():
    """返回 node_status 的初始副本。"""
    return {"shop": False, "flash_or_rest": False, "reach_final_boss": False, "final_boss_battle": False,
            "pass_final_boss_count": 0, "total_rounds": 0, "success_rounds": 0,
            "node_count": 0, "enter_new_node": False, "node_type": "", "is_escaped": False,
            "save_target_member": False, "target_mask_card_position": -1,
            "get_specific_flash": False, "removed_card_count": 0,
            "shop_removed_card_count": 0,
            "neutral_card_count": 0}


def _initial_member_status():
    """返回目标主战员状态的初始副本。"""
    return {
        "equipment": {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        },
        "deck": {},
    }


def _finish_only_first_layer(task: TriggerTask) -> bool:
    """检查并完成只打第一层的退出操作：如果 pass_final_boss_count >= 1 且配置'只打第一层'为 True，则成功次数+1、点击退出并返回 True。"""
    if not (
        hasattr(task, 'node_status')
        and task.node_status.get('pass_final_boss_count', 0) >= 1
    ):
        return False

    if (
        _get_config_value(task, "首层刷特定闪光", False) is True
        and task.node_status.get("get_specific_flash", False) is False
    ):
        task.node_status['success_rounds'] += 1
        task.log_info("未刷到指定闪光且已通关第一层，退出重刷")
        _move_and_click(task, 0.959, 0.051)
        task.sleep(1)
        return True

    if _get_config_value(task, '只打第一层', False):
        task.node_status['success_rounds'] += 1
        task.log_info(f"只打第一层任务已完成，success_rounds + 1 (当前: {task.node_status['success_rounds']}), 退出结算页面")
        _move_and_click(task, 0.959, 0.051)
        task.sleep(1)
        return True
    return False


def reset_all_status(task: TriggerTask):
    """重置所有状态：恢复节点状态和目标主战员状态。"""
    if getattr(task, 'node_status', None) is not None:
        task.node_status = _initial_node_status()
    task.member_status = _initial_member_status()
    _reset_meditation_state(task)
    task._pending_removed_card_count = 0
    task._pending_shop_removal = False


def reset_mission_status(task: TriggerTask):
    """重置任务状态：保留任务统计和目标成员特征状态，重置其他状态。"""
    ns = getattr(task, 'node_status', None)
    if ns is not None:
        keep = {'total_rounds': ns.get('total_rounds', 0),
                'success_rounds': ns.get('success_rounds', 0),
                'save_target_member': ns.get('save_target_member', False)}
        task.node_status = _initial_node_status()
        task.node_status['total_rounds'] = keep['total_rounds']
        task.node_status['success_rounds'] = keep['success_rounds']
        task.node_status['save_target_member'] = keep['save_target_member']
    task.member_status = _initial_member_status()
    _reset_meditation_state(task)
    task._pending_removed_card_count = 0
    task._pending_shop_removal = False


def reset_layer_status(task: TriggerTask):
    """重置层状态：保留通关计数、任务统计和目标成员特征状态。"""
    ns = getattr(task, 'node_status', None)
    if ns is not None:
        keep = {'pass_final_boss_count': ns.get('pass_final_boss_count', 0),
                'total_rounds': ns.get('total_rounds', 0),
                'success_rounds': ns.get('success_rounds', 0),
                'save_target_member': ns.get('save_target_member', False),
                'target_mask_card_position': ns.get('target_mask_card_position', -1),
                'get_specific_flash': ns.get('get_specific_flash', False),
                'removed_card_count': ns.get('removed_card_count', 0),
                'shop_removed_card_count': ns.get('shop_removed_card_count', 0),
                'neutral_card_count': ns.get('neutral_card_count', 0)}
        task.node_status = _initial_node_status()
        task.node_status['pass_final_boss_count'] = keep['pass_final_boss_count']
        task.node_status['total_rounds'] = keep['total_rounds']
        task.node_status['success_rounds'] = keep['success_rounds']
        task.node_status['save_target_member'] = keep['save_target_member']
        task.node_status['target_mask_card_position'] = keep['target_mask_card_position']
        task.node_status['get_specific_flash'] = keep['get_specific_flash']
        task.node_status['removed_card_count'] = keep['removed_card_count']
        task.node_status['shop_removed_card_count'] = keep['shop_removed_card_count']
        task.node_status['neutral_card_count'] = keep['neutral_card_count']


def handle_close_button(task: TriggerTask):
    """通用关闭按钮: 检测到关闭按钮则点击关闭。"""
    box = find_box_at_point(task, 0.512, 0.929)
    if box and box.name == "关闭":
        task.log_info("检测到关闭按钮，点击关闭")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_card_assign(task: TriggerTask):
    """卡牌分配页面: 按奖励优先级刷新或跳过，并优先分配给目标主战员。"""
    title_box = find_box_at_point(task, 0.863, 0.133)
    assign_prompt = _get_game_text(task, "请选择要接受卡牌的主战员")
    if not (title_box and assign_prompt in title_box.name):
        return False

    task.log_info("检测到卡牌分配页面")

    purchase_title_region = (0.326, 0.057, 0.671, 0.210)
    purchase_title_boxes = [
        b for b in task.all_texts
        if purchase_title_region[0] <= (b.x + b.width / 2) / task.width <= purchase_title_region[2]
        and purchase_title_region[1] <= (b.y + b.height / 2) / task.height <= purchase_title_region[3]
    ]
    is_purchase_page = any("购买卡牌" in b.name for b in purchase_title_boxes)
    purchase_box = None
    cancel_box = None
    card_price = None
    current_credit = None
    if is_purchase_page:
        task.log_info("检测到购买卡牌页面")
        current_credit = _get_current_credit(task)
        purchase_bottom_boxes = [
            b for b in task.all_texts
            if 0.016 <= (b.x + b.width / 2) / task.width <= 0.995
            and 0.878 <= (b.y + b.height / 2) / task.height <= 0.996
        ]
        cancel_box = next((b for b in purchase_bottom_boxes if "取消" in b.name), None)
        purchase_box = next((b for b in purchase_bottom_boxes if "购买" in b.name), None)
        price_box = next(
            (b for b in purchase_bottom_boxes if re.fullmatch(r"\d+", b.name.strip())),
            None,
        )
        if price_box:
            card_price = _parse_discounted_price(price_box.name)
        task.log_info(
            f"购买卡牌页面: 当前信用点={current_credit}，"
            f"OCR价格=「{price_box.name if price_box else ''}」，实际价格={card_price}"
        )

        if not (cancel_box and purchase_box):
            task.log_info("购买卡牌页面未完整识别取消和购买按钮")
            if cancel_box:
                task.log_info("购买卡牌页面触发识别失败取消事件，点击「取消」")
                task.click_box(cancel_box)
                task.sleep(1)
                return True
            return False
        if card_price is None:
            task.log_info("购买卡牌页面未识别到价格，按价格低于当前信用点继续购买")
        if _neutral_card_limit_reached(task):
            neutral_card_count = task.node_status.get("neutral_card_count", 0)
            neutral_card_limit = _neutral_card_limit(task)
            task.log_info(
                f"本局已获得{neutral_card_count}张中立牌，"
                f"达到固定上限{neutral_card_limit}张，点击「取消」"
            )
            task.click_box(cancel_box)
            task.sleep(1)
            return True

    assigned_cards = recognize_cards(
        task,
        region=(0.101, 0.217, 0.291, 0.365),
        page="卡牌分配页面",
    )
    assigned_card = assigned_cards[0] if assigned_cards else None
    card_name = assigned_card["name"] if assigned_card else ""
    card_desc = assigned_card["description"] if assigned_card else ""
    task.log_info(f"待分配卡牌: 名称=「{card_name}」，描述=「{card_desc}」")

    bottom_boxes = [
        b for b in task.all_texts
        if 0.290 <= (b.x + b.width / 2) / task.width <= 0.998
        and 0.878 <= (b.y + b.height / 2) / task.height <= 0.997
    ]
    refresh_text = _get_game_text(task, "刷新")
    refresh_box = next((b for b in bottom_boxes if refresh_text in b.name), None)
    skip_box = next((b for b in bottom_boxes if "跳过" in b.name), None)
    refresh_count = None
    for bottom_box in bottom_boxes:
        count_match = re.search(r'(\d+)/(\d+)', bottom_box.name)
        if count_match:
            refresh_count = (int(count_match.group(1)), int(count_match.group(2)))
            break

    reward_priority_config = _get_card_list(task, "卡牌奖励优先级")
    has_reward_priority = any(
        isinstance(item, str) and item.strip()
        for item in reward_priority_config
    )
    priority = _get_card_reward_priority(task)
    matched_card_name = next(
        (config_name for config_name in priority
         if card_name and config_name
         and (config_name in card_name or card_name in config_name)),
        None,
    )
    if matched_card_name:
        task.log_info(f"卡牌「{card_name}」命中奖励优先级「{matched_card_name}」")
    else:
        task.log_info(f"卡牌「{card_name}」未命中奖励优先级")
        if is_purchase_page:
            task.log_info("购买卡牌未命中奖励优先级，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        if (
            has_reward_priority
            and refresh_box
            and refresh_count
            and refresh_count[0] > 0
        ):
            task.log_info(f"剩余刷新次数: {refresh_count[0]}/{refresh_count[1]}，点击刷新")
            task.click_box(refresh_box)
            return True
        if skip_box:
            task.log_info("无可用刷新或刷新次数，点击跳过非优先级卡牌")
            task.click_box(skip_box)
            return True

    lv_texts = _find_member_level_tags(
        task,
        (0.426, 0.292, 0.473, 0.783),
        page="卡牌分配页面",
    )
    if not lv_texts:
        task.log_info("未找到主战员leveltag特征")
        if is_purchase_page:
            task.log_info("购买卡牌页面未找到可分配战员，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        return False

    target_member_index = _find_target_member_index(
        task, lv_texts, (0.484, 0.169, 0.652, 0.858)
    )

    available_members = []
    for index, level_box in enumerate(lv_texts):
        level_center_x = (level_box.x + level_box.width / 2) / task.width
        level_center_y = (level_box.y + level_box.height / 2) / task.height
        unavailable_box = find_box_at_point(
            task,
            level_center_x + 0.0615,
            level_center_y - 0.0795,
        )
        if unavailable_box and "无法获得" in unavailable_box.name:
            task.log_info(f"第{index + 1}号主战员无法获得该卡牌，排除")
            continue
        available_members.append((index, level_box))

    if not available_members:
        task.log_info("所有主战员均无法获得该卡牌")
        if is_purchase_page:
            task.log_info("购买卡牌无法分配给任何战员，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        if skip_box:
            task.log_info("尝试点击跳过")
            task.click_box(skip_box)
            return True
        task.log_info("未找到跳过按钮")
        return False

    target_available = next(
        (member for member in available_members if member[0] == target_member_index),
        None,
    )
    chosen_idx, chosen_lv = target_available or available_members[0]
    if target_available:
        task.log_info(f"优先选择刷存档主战员（第{chosen_idx + 1}号）接受卡牌")
    else:
        task.log_info(f"优先选择第{chosen_idx + 1}号主战员接受卡牌")

    if (
        is_purchase_page
        and card_price is not None
        and current_credit <= card_price
    ):
        task.log_info(
            f"购买卡牌需要{card_price}信用点，当前{current_credit}，信用点不足，点击「取消」"
        )
        task.click_box(cancel_box)
        task.sleep(1)
        return True

    tracks_target_member = "刷存档主战员" in getattr(task, "default_config", {})
    if (
        target_member_index is not None and chosen_idx == target_member_index
    ) or (
        not tracks_target_member and chosen_idx == 0
    ):
        deck = _member_deck_state(task)
        deck[matched_card_name or card_name] = card_desc
    _move_and_click(task, 0.756, (chosen_lv.y + chosen_lv.height / 2) / task.height)
    task.sleep(1)
    if is_purchase_page:
        task.log_info(
            f"购买卡牌需要{card_price}信用点，当前{current_credit}，点击「购买」"
        )
        task.click_box(purchase_box)
        _record_neutral_card(task)
        task.sleep(1)
        return True
    _record_neutral_card(task)
    return False

def handle_held_cards_page(task: TriggerTask):
    """持有卡牌页面: 检测到持有卡牌则关闭页面。"""
    box = find_box_at_point(task, 0.500, 0.056)
    if box and box.name == _get_game_text(task, '持有卡牌'):
        task.log_info("检测到持有卡牌页面，点击关闭")
        _move_and_click(task, 0.966, 0.053)
        return True
    return False

def handle_weakness_info(task: TriggerTask):
    """怪物信息页面: 检测到弱点信息则关闭页面。"""
    box = find_box_at_point(task, 0.387, 0.107)
    if box and "弱点" in box.name:
        task.log_info("检测到怪物信息页面，点击关闭")
        _move_and_click(task, 0.502, 0.092)
        return True
    return False

def handle_minimizemap(task: TriggerTask):
    """地图页面: 检测到小地图按钮则点击关闭小地图。"""
    boxes = task.find_feature(feature_name="minimizemap")
    if boxes:
        task.log_info("检测到地图页面，点击关闭小地图")
        task.click_box(boxes[0])
        return True
    return False

def handle_non_battle_page(task: TriggerTask):
    """非出击/卡厄思页面: 检测到故事/营救/方舟城市时自动停止当前模式，优先级最高。"""
    box = find_box_at_point(task, 0.887, 0.160)
    if box and box.name == "故事":
        task.log_info("检测到故事页面，停止当前模式")
        task.disable()
        return True
    box = find_box_at_point(task, 0.101, 0.046)
    if box and box.name == "营救":
        task.log_info("检测到营救页面，停止当前模式")
        task.disable()
        return True
    box = find_box_at_point(task, 0.124, 0.049)
    if box and box.name == "方舟城市":
        task.log_info("检测到方舟城市页面，停止当前模式")
        task.disable()
        return True
    return False

def handle_unknown_page(task: TriggerTask):
    """检测到待确认的未知页面: 确认按钮不可点击时随机点击页面中央区域。"""
    box = find_box_at_point(task, 0.916, 0.931)
    if box and _clean_match(box.name, "确认") and not is_button_active(task, box):
        task.log_info("检测到待确认的未知页面，确认按钮不可点击，随机点击页面区域")
        import random
        rx = random.uniform(0.043, 0.972)
        ry = random.uniform(0.149, 0.843)
        _move_and_click(task, rx, ry)
        task.sleep(1)
        return True
    return False

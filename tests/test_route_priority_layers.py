# -*- coding: utf-8 -*-
"""卡厄思模式「第一层/第二层路线优先级」的纯逻辑测试。

卡厄思模式按第一层、第二层分别读取路线优先级；出击模式继续使用单条
「路线优先级」。

覆盖面：
1. `_get_route_priority(task, layer_index)` 按层取配置（0=第一层，>=1=第二层）；
2. 本层没配置时的回退链：另一层配置 -> 旧配置「路线优先级」-> 默认顺序；
3. 不带 layer_index 的旧调用（出击模式）行为不变，只读「路线优先级」；
4. 旧配置迁移：一条「路线优先级」拆成两条层配置，出击模式数据不受影响；
5. `handle_route_selection` 真的按当前层号选节点（第一层选精英、第二层选小怪）。

本文件只做纯逻辑判断：假 task + 假文本框，不截图、不依赖游戏画面。
"""
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

# 与其他测试保持一致：ok_tasks 内部使用平级导入，需要把仓库根目录和
# ok_tasks 目录都加入 sys.path 才能正常 import。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

import config_io  # noqa: E402
import utils  # noqa: E402
from utils import _get_route_priority, handle_route_selection  # noqa: E402
from utils import find_best_map_route_by_priority  # noqa: E402

LAYER1_KEY = "第一层路线优先级"
LAYER2_KEY = "第二层路线优先级"
LEGACY_KEY = "路线优先级"
LAYER1_VALUE = ["精英", "休息", "事件", "小怪"]
LAYER2_VALUE = ["小怪", "休息", "精英", "事件"]
DEFAULT_VALUE = ["商店", "休息", "事件", "小怪", "精英"]
LAYER_KEYS_CONFIG = {LAYER1_KEY: [], LAYER2_KEY: []}


class _ConfigTask:
    """只保留读配置需要的接口。"""

    def __init__(self, config=None, default_config=None):
        self.config = dict(config or {})
        self.default_config = dict(default_config or {})
        self.logs = []

    def log_info(self, message):
        self.logs.append(message)


class TestRoutePriorityByLayer(unittest.TestCase):
    """`_get_route_priority` 的按层取值与回退链。"""

    def test_first_layer_uses_first_layer_config(self):
        task = _ConfigTask({LAYER1_KEY: LAYER1_VALUE, LAYER2_KEY: LAYER2_VALUE})
        self.assertEqual(LAYER1_VALUE, _get_route_priority(task, 0))

    def test_second_layer_uses_second_layer_config(self):
        task = _ConfigTask({LAYER1_KEY: LAYER1_VALUE, LAYER2_KEY: LAYER2_VALUE})
        self.assertEqual(LAYER2_VALUE, _get_route_priority(task, 1))

    def test_layer_beyond_second_uses_second_layer_config(self):
        """卡厄思模式只有两层，任何 >=1 的层号都按第二层配置处理。"""
        task = _ConfigTask({LAYER1_KEY: LAYER1_VALUE, LAYER2_KEY: LAYER2_VALUE})
        self.assertEqual(LAYER2_VALUE, _get_route_priority(task, 2))

    def test_second_layer_missing_falls_back_to_first_layer(self):
        task = _ConfigTask({LAYER1_KEY: LAYER1_VALUE})
        self.assertEqual(LAYER1_VALUE, _get_route_priority(task, 1))

    def test_empty_layer_config_falls_back_to_other_layer(self):
        """配置项存在但为空列表，等同于没配置，不能被当成"没有优先级"。"""
        task = _ConfigTask({LAYER1_KEY: [], LAYER2_KEY: LAYER2_VALUE})
        self.assertEqual(LAYER2_VALUE, _get_route_priority(task, 0))

    def test_both_layers_missing_falls_back_to_legacy_key(self):
        task = _ConfigTask({LEGACY_KEY: ["事件", "小怪", "精英", "休息"]})
        self.assertEqual(
            ["事件", "小怪", "精英", "休息"], _get_route_priority(task, 1)
        )

    def test_all_missing_uses_default_order(self):
        task = _ConfigTask()
        self.assertEqual(DEFAULT_VALUE, _get_route_priority(task, 0))
        self.assertEqual(DEFAULT_VALUE, _get_route_priority(task, 1))

    def test_default_config_is_used_when_task_config_is_empty(self):
        task = _ConfigTask({}, {LAYER2_KEY: LAYER2_VALUE})
        self.assertEqual(LAYER2_VALUE, _get_route_priority(task, 1))

    def test_legacy_call_without_layer_index_reads_legacy_key(self):
        """出击模式仍走旧签名：只读「路线优先级」，不碰层配置。"""
        task = _ConfigTask({
            LAYER1_KEY: LAYER1_VALUE,
            LEGACY_KEY: ["小怪", "休息", "精英", "事件"],
        })
        self.assertEqual(
            ["小怪", "休息", "精英", "事件"], _get_route_priority(task)
        )

    def test_legacy_call_without_layer_index_keeps_default_when_unset(self):
        task = _ConfigTask({LAYER1_KEY: LAYER1_VALUE})
        self.assertEqual(DEFAULT_VALUE, _get_route_priority(task))

    def test_boss_is_normalized_to_elite(self):
        task = _ConfigTask({LAYER1_KEY: ["休息", "boss", "小怪"]})
        self.assertEqual(["休息", "精英", "小怪"], _get_route_priority(task, 0))


class TestRoutePriorityMigration(unittest.TestCase):
    """旧版一条「路线优先级」拆成第一层/第二层两条。"""

    def test_legacy_key_splits_into_two_layers(self):
        data = {LEGACY_KEY: ["休息", "boss"], "游戏语言": "简体中文"}
        config_io._migrate_route_priority_layers(data, LAYER_KEYS_CONFIG)

        self.assertNotIn(LEGACY_KEY, data)
        self.assertEqual(["休息", "精英"], data[LAYER1_KEY])
        self.assertEqual(["休息", "精英"], data[LAYER2_KEY])
        self.assertEqual("简体中文", data["游戏语言"])
        # 两条层配置必须是各自独立的列表，改一条不能连带改另一条。
        self.assertIsNot(data[LAYER1_KEY], data[LAYER2_KEY])

    def test_existing_layer_config_is_not_overwritten(self):
        data = {LEGACY_KEY: ["休息"], LAYER2_KEY: ["小怪"]}
        config_io._migrate_route_priority_layers(data, LAYER_KEYS_CONFIG)

        self.assertEqual(["休息"], data[LAYER1_KEY])
        self.assertEqual(["小怪"], data[LAYER2_KEY])

    def test_sortie_mode_data_is_untouched(self):
        """出击模式没有层配置项，旧键必须原样保留，行为不变。"""
        data = {LEGACY_KEY: ["休息", "boss"]}
        config_io._migrate_route_priority_layers(data, {LEGACY_KEY: []})

        self.assertEqual(["休息", "boss"], data[LEGACY_KEY])
        self.assertNotIn(LAYER1_KEY, data)
        self.assertNotIn(LAYER2_KEY, data)

    def test_no_legacy_key_is_noop(self):
        data = {LAYER1_KEY: ["休息"]}
        config_io._migrate_route_priority_layers(data, LAYER_KEYS_CONFIG)

        self.assertEqual({LAYER1_KEY: ["休息"]}, data)

    def test_invalid_legacy_value_falls_back_to_default_order(self):
        data = {LEGACY_KEY: "不是列表"}
        config_io._migrate_route_priority_layers(data, LAYER_KEYS_CONFIG)

        self.assertEqual(DEFAULT_VALUE, data[LAYER1_KEY])
        self.assertEqual(DEFAULT_VALUE, data[LAYER2_KEY])

    def test_config_file_migration_rewrites_legacy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = _ConfigTask({}, LAYER_KEYS_CONFIG)
            path = os.path.join(tmp, f"{task.__class__.__name__}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({LEGACY_KEY: ["休息", "boss"]}, handle, ensure_ascii=False)

            with mock.patch.object(config_io, "Config", types.SimpleNamespace(config_folder=tmp)), \
                    mock.patch.object(config_io, "get_relative_path", os.path.join):
                config_io.migrate_route_priority_layers_config_file(task)

            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertNotIn(LEGACY_KEY, data)
            self.assertEqual(["休息", "精英"], data[LAYER1_KEY])
            self.assertEqual(["休息", "精英"], data[LAYER2_KEY])

    def test_config_file_migration_skipped_for_modes_without_layer_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = _ConfigTask({}, {LEGACY_KEY: []})
            path = os.path.join(tmp, f"{task.__class__.__name__}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({LEGACY_KEY: ["休息"]}, handle, ensure_ascii=False)

            with mock.patch.object(config_io, "Config", types.SimpleNamespace(config_folder=tmp)), \
                    mock.patch.object(config_io, "get_relative_path", os.path.join):
                config_io.migrate_route_priority_layers_config_file(task)

            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(["休息"], data[LEGACY_KEY])


class _Box:
    """屏幕框/文本框替身，只保留 utils 里用到的属性。"""

    def __init__(self, name, center_x, center_y, width=60, height=22):
        self.name = name
        self.x = int(center_x - width / 2)
        self.y = int(center_y - height / 2)
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height


def _node_box(feature_name, center_x, center_y):
    return _Box(feature_name, center_x * 1600, center_y * 900)


class _FakeRouteTask:
    """路线选择页面所需的最小假 task（不连游戏、不截图）。"""

    def __init__(self, config, node_status, nodes):
        self.width = 1600
        self.height = 900
        self.config = dict(config)
        self.default_config = {}
        self.node_status = dict(node_status)
        self.all_texts = []
        self.member_status = {"equipment": {"names": ["", "", ""],
                                            "descriptions": ["", "", ""],
                                            "qualities": ["", "", ""]}}
        self.nodes = nodes
        self.logs = []
        self.moves = []
        self.clicks = []
        self.box_clicks = []

    def log_info(self, message):
        self.logs.append(message)

    def move_relative(self, x, y):
        self.moves.append((round(x, 4), round(y, 4)))

    def click(self, x, y):
        self.clicks.append((round(x, 4), round(y, 4)))

    def click_box(self, box):
        self.box_clicks.append(box)

    def sleep(self, _seconds):
        pass

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, width=0.0, height=0.0,
                      name=None, **_kwargs):
        return _Box(name or "screen", x * self.width, y * self.height,
                    width=180, height=120)

    def find_feature(self, feature_name=None, box=None, threshold=0):
        if feature_name == "position":
            return [_Box("position", 0.4 * self.width, 0.65 * self.height)]
        return list(self.nodes.get(feature_name, []))

    def has_log(self, keyword):
        return any(keyword in log for log in self.logs)


class TestHandleRouteSelectionUsesLayerPriority(unittest.TestCase):
    """路线页面真的按当前层号选节点：第一层听第一层配置，第二层听第二层配置。"""

    ENEMY_CENTER = (0.70, 0.30)
    ELITE_CENTER = (0.70, 0.60)
    CLICK_OFFSET = (0.095, 0.0065)

    def _task(self, layer):
        nodes = {
            "enemy": [_node_box("enemy", *self.ENEMY_CENTER)],
            "elite": [_node_box("elite", *self.ELITE_CENTER)],
        }
        return _FakeRouteTask(
            config={LAYER1_KEY: LAYER1_VALUE, LAYER2_KEY: LAYER2_VALUE},
            node_status={"pass_final_boss_count": layer, "node_count": 0},
            nodes=nodes,
        )

    def _expected_click(self, center):
        return (round(center[0] - self.CLICK_OFFSET[0], 4),
                round(center[1] - self.CLICK_OFFSET[1], 4))

    def _run(self, layer):
        task = self._task(layer)
        with mock.patch.object(
            utils, "recognize_map_connections",
            return_value={"nodes": [], "connections": [], "adjacency": {}},
        ):
            self.assertTrue(handle_route_selection(task))
        return task

    def test_first_layer_follows_first_layer_config(self):
        """第一层配置第一个是「精英」，就应该点精英节点。"""
        task = self._run(0)

        self.assertTrue(task.has_log(f"第1层路线优先级配置: {LAYER1_VALUE}"))
        self.assertEqual("精英", task.node_status["node_type"])
        self.assertEqual([self._expected_click(self.ELITE_CENTER)], task.clicks)

    def test_second_layer_follows_second_layer_config(self):
        """第二层配置第一个是「小怪」，就应该点小怪节点。"""
        task = self._run(1)

        self.assertTrue(task.has_log(f"第2层路线优先级配置: {LAYER2_VALUE}"))
        self.assertEqual("小怪", task.node_status["node_type"])
        self.assertEqual([self._expected_click(self.ENEMY_CENTER)], task.clicks)

    def test_missing_layer_config_falls_back_to_other_layer(self):
        """第二层没配置时回退第一层配置：第一层配置的第一个仍是「精英」。"""
        task = self._task(1)
        task.config = {LAYER1_KEY: LAYER1_VALUE}

        with mock.patch.object(
            utils, "recognize_map_connections",
            return_value={"nodes": [], "connections": [], "adjacency": {}},
        ):
            handle_route_selection(task)

        self.assertTrue(task.has_log(f"第2层路线优先级配置: {LAYER1_VALUE}"))
        self.assertEqual("精英", task.node_status["node_type"])
SHOP_FIRST_VALUE = ["商店", "休息", "事件", "小怪", "精英"]
SHOP_LAST_VALUE = ["休息", "事件", "小怪", "精英", "商店"]
SHOP_LEGACY_VALUE = ["休息", "事件", "小怪", "精英"]


def _map_info():
    """最小小地图：当前位置 -> [休息(带商店标志), 小怪]。"""
    return {
        "nodes": [
            {"id": 0, "type": "当前位置", "row": 1, "column": 0, "x": 0.05, "y": 0.50,
             "special_features": []},
            {"id": 1, "type": "休息", "row": 1, "column": 1, "x": 0.15, "y": 0.50,
             "special_features": [{"feature_name": "shop_in_map", "priority": 1}]},
            {"id": 2, "type": "小怪", "row": 2, "column": 1, "x": 0.15, "y": 0.60,
             "special_features": []},
        ],
        "connections": [],
        "adjacency": {0: [1, 2], 1: [], 2: []},
    }


class TestMapRoutePlannerShopPriority(unittest.TestCase):
    """小地图规划：写了「商店」就按它自己的排名算，没写才用旧的固定加成。"""

    def test_shop_in_priority_list_uses_its_rank(self):
        plan = find_best_map_route_by_priority(_map_info(), SHOP_FIRST_VALUE)

        self.assertEqual("休息", plan["next_node_type"])
        self.assertEqual(1, plan["next_row"])
        self.assertEqual(["shop_in_map"], plan["next_special_features"])

    def test_shop_last_in_priority_list_stops_winning(self):
        """把「商店」放到最后：带商店的休息节点不再自动最优先，让位给小怪。"""
        plan = find_best_map_route_by_priority(_map_info(), SHOP_LAST_VALUE)

        self.assertEqual("小怪", plan["next_node_type"])
        self.assertEqual(2, plan["next_row"])

    def test_shop_missing_keeps_legacy_fixed_bonus(self):
        """列表里没写「商店」（老配置）：商店节点仍然固定最优先。"""
        plan = find_best_map_route_by_priority(_map_info(), SHOP_LEGACY_VALUE)

        self.assertEqual("休息", plan["next_node_type"])
        self.assertEqual(1, plan["next_row"])


class TestShopAliasNormalization(unittest.TestCase):
    """旧写法 "shop" 归一成「商店」，不会变成无效列表项。"""

    def test_shop_alias(self):
        task = _ConfigTask({LAYER1_KEY: ["shop", "休息", "boss"]})
        self.assertEqual(["商店", "休息", "精英"], _get_route_priority(task, 0))


class TestHandleRouteSelectionShopEntry(unittest.TestCase):
    """路线页面：「商店」作为列表项时，带商店标志的节点按它在列表里的位置排。"""

    ENEMY_CENTER = (0.70, 0.30)
    SHOP_NODE_CENTER = (0.70, 0.60)
    CLICK_OFFSET = (0.095, 0.0065)

    def _task(self, priority):
        nodes = {
            "enemy": [_node_box("enemy", *self.ENEMY_CENTER)],
            "elite": [_node_box("elite", *self.SHOP_NODE_CENTER)],
            # 商店标志挂在精英节点上（位置重合 -> 归属到距离最近的精英节点）
            "shop": [_node_box("shop", *self.SHOP_NODE_CENTER)],
        }
        return _FakeRouteTask(
            config={LAYER1_KEY: list(priority)},
            node_status={"pass_final_boss_count": 0, "node_count": 0},
            nodes=nodes,
        )

    def _expected_click(self, center):
        return (round(center[0] - self.CLICK_OFFSET[0], 4),
                round(center[1] - self.CLICK_OFFSET[1], 4))

    def _run(self, priority):
        task = self._task(priority)
        with mock.patch.object(
            utils, "recognize_map_connections",
            return_value={"nodes": [], "connections": [], "adjacency": {}},
        ):
            self.assertTrue(handle_route_selection(task))
        return task

    def test_shop_first_clicks_the_shop_node(self):
        task = self._run(SHOP_FIRST_VALUE)

        self.assertEqual("精英", task.node_status["node_type"])
        self.assertEqual([self._expected_click(self.SHOP_NODE_CENTER)], task.clicks)
        self.assertTrue(task.has_log("['shop']"))

    def test_shop_last_no_longer_wins(self):
        """「商店」排在最后：带商店的节点让位给排在前面的小怪。"""
        task = self._run(SHOP_LAST_VALUE)

        self.assertEqual("小怪", task.node_status["node_type"])
        self.assertEqual([self._expected_click(self.ENEMY_CENTER)], task.clicks)

    def test_shop_missing_keeps_legacy_fixed_top_priority(self):
        task = self._run(SHOP_LEGACY_VALUE)

        self.assertEqual("精英", task.node_status["node_type"])
        self.assertEqual([self._expected_click(self.SHOP_NODE_CENTER)], task.clicks)


if __name__ == "__main__":
    unittest.main()

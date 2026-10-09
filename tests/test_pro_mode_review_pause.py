# -*- coding: utf-8 -*-
"""刻印专业模式"第二个Boss房间前达标就停止程序"的纯逻辑测试。

达标：停止任务，保留当前画面供人工检查，不进入最终Boss房间。
不达标：点击右上角「退出」并逃脱重开。

退出按钮的固定坐标兜底不要求该处已有 OCR 文本；连退出按钮都拿不到时，
程序暂停等待人工处理，不自动把不达标的一局打完。本文件只做纯逻辑判断，
不依赖游戏画面。
"""
import pathlib
import sys
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

import utils  # noqa: E402
from utils import (  # noqa: E402
    _maybe_escape_for_engraving_pro_mode,
    handle_route_selection,
)
from utils_equipment_engraving import (  # noqa: E402
    PRO_MODE_ACTION_ESCAPE,
    PRO_MODE_ACTION_STOP_FOR_REVIEW,
    PRO_MODE_ACTION_PAUSE_NO_EXIT,
    decide_pro_mode_check_point_action,
)

PRO_MODE_KEY = "刻印专业模式"
ENGRAVING_KEY = "刷装备刻印"
ENGRAVING_PRIORITY_KEY = "装备刻印优先级"
SLOT1_NAME = "欺骗暗影的匕首"
SLOT2_NAME = "精灵王的金冠"
SLOT3_NAME = "深绿黑夜"
ENGRAVING_KEYWORDS = ["致命一击", "坚韧"]
CHECK_POINT = "第二个Boss房间前@第2层"
STOP_LOG = "已刷出指定存档，停止程序"
ENTER_BOSS_LOG = "检测到最终boss节点，点击进入"
ESCAPE_LOG = "点击退出并逃脱，重开一局"
NO_EXIT_LOG = "未找到「退出」按钮"
ENTER_BOSS_CLICK = (0.815, 0.492)


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

    def __repr__(self):
        return f"<Box {self.name} ({self.x},{self.y},{self.width},{self.height})>"


def _exit_button():
    """运行中页面右上角的「退出」按钮（区域内，_find_run_exit_box 能 OCR 到）。"""
    return _Box("退出", 0.959 * 1600, 0.050 * 900, width=90, height=26)


def _member_status(names, descriptions):
    return {
        "equipment": {
            "names": list(names),
            "descriptions": list(descriptions),
            "qualities": ["", "", ""],
        }
    }


def _qualified_member_status():
    """三个装备位都拿到"目标名+任意一项目标刻印"。"""
    return _member_status(
        [SLOT1_NAME, SLOT2_NAME, SLOT3_NAME],
        ["致命一击 +5", "坚韧不拔", "致命一击 +3"],
    )


def _empty_member_status():
    return _member_status(["", "", ""], ["", "", ""])


def _pro_config(**overrides):
    config = {
        PRO_MODE_KEY: True,
        ENGRAVING_KEY: True,
        ENGRAVING_PRIORITY_KEY: list(ENGRAVING_KEYWORDS),
        "装备1号位优先级": [SLOT1_NAME],
        "装备2号位优先级": [SLOT2_NAME],
        "装备3号位优先级": [SLOT3_NAME],
    }
    config.update(overrides)
    return config


class _FakeTask:
    """还原本次判定依赖的最小接口（假 task，不连游戏、不截图）。"""

    def __init__(self, config=None, node_status=None, all_texts=None,
                 member_status=None, route_page=True):
        self.width = 1600
        self.height = 900
        self.config = dict(config or {})
        self.default_config = {}
        self.node_status = dict(node_status or {})
        self.all_texts = list(all_texts or [])
        self.member_status = member_status or _empty_member_status()
        self.route_page = route_page
        self.logs = []
        self.box_clicks = []
        self.clicks = []
        self.disabled = False

    def log_info(self, message):
        self.logs.append(message)

    def click_box(self, box):
        self.box_clicks.append(box)

    def click(self, x, y):
        self.clicks.append((round(x, 3), round(y, 3)))

    def move_relative(self, x, y):
        self.clicks.append((round(x, 3), round(y, 3)))

    def sleep(self, _seconds):
        pass

    def disable(self):
        self.disabled = True

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, width=0.0, height=0.0,
                      name=None, **_kwargs):
        return _Box(name or "screen", x * self.width, y * self.height,
                    width=180, height=120)

    def find_feature(self, feature_name=None, box=None, threshold=0):
        if feature_name == "position" and self.route_page:
            return [_Box("position", 0.4 * self.width, 0.6 * self.height)]
        return []

    def clicked_names(self):
        return [box.name for box in self.box_clicks]

    def has_log(self, keyword):
        return any(keyword in log for log in self.logs)

    def log_count(self, keyword):
        return len([log for log in self.logs if keyword in log])


class TestCheckPointDecisionTable(unittest.TestCase):
    """检查点走向表：达标一律停止，绝不进Boss。"""

    def test_completed_stops_even_with_exit_button(self):
        """达标：就算右上角有「退出」也不能点，走向只有"停止程序"。"""
        self.assertEqual(
            PRO_MODE_ACTION_STOP_FOR_REVIEW,
            decide_pro_mode_check_point_action(True, True),
        )

    def test_completed_stops_without_exit_button(self):
        self.assertEqual(
            PRO_MODE_ACTION_STOP_FOR_REVIEW,
            decide_pro_mode_check_point_action(True, False),
        )

    def test_not_completed_escapes(self):
        self.assertEqual(
            PRO_MODE_ACTION_ESCAPE,
            decide_pro_mode_check_point_action(False, True),
        )

    def test_not_completed_without_exit_pauses_instead_of_entering_boss(self):
        """不达标又找不到「退出」按钮：兜底改成暂停，绝不自动进 Boss。"""
        self.assertEqual(
            PRO_MODE_ACTION_PAUSE_NO_EXIT,
            decide_pro_mode_check_point_action(False, False),
        )


class TestProModeCheckPointBehaviour(unittest.TestCase):
    """`_maybe_escape_for_engraving_pro_mode` 在检查点上的实际动作。"""

    def _task(self, qualified=True, exit_button=True, layer=1, config=None):
        return _FakeTask(
            config=config or _pro_config(),
            node_status={"pass_final_boss_count": layer},
            all_texts=[_exit_button()] if exit_button else [],
            member_status=(
                _qualified_member_status() if qualified else _empty_member_status()
            ),
        )

    def test_qualified_stops_and_never_clicks(self):
        task = self._task(qualified=True, exit_button=True)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertEqual([], task.clicked_names())
        self.assertTrue(task.disabled)
        self.assertNotIn("pro_mode_review_paused", task.node_status)
        self.assertTrue(task.has_log(STOP_LOG))
        self.assertFalse(task.has_log(ESCAPE_LOG))

    def test_not_qualified_still_clicks_exit(self):
        task = self._task(qualified=False, exit_button=True)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertEqual(["退出"], task.clicked_names())
        self.assertTrue(task.has_log(ESCAPE_LOG))

        self.assertNotIn("pro_mode_review_paused", task.node_status)

    def test_escape_check_still_runs_once_per_check_point(self):
        """不达标那次的"只点一次退出"防重复逻辑不能因为本次改动失效。"""
        task = self._task(qualified=False, exit_button=True)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            first = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)
            clicked_after_first = task.clicked_names()
            second = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(["退出"], clicked_after_first)
        self.assertEqual(clicked_after_first, task.clicked_names())
        pause.assert_not_called()

    def test_not_qualified_without_ocr_exit_clicks_fixed_coordinate(self):
        """OCR 没识别到「退出」文本时，固定坐标兜底照样要能点出去。"""
        task = self._task(qualified=False, exit_button=False)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertEqual(["退出"], task.clicked_names())
        self.assertTrue(task.has_log("回退到固定坐标"))
        self.assertTrue(task.has_log(ESCAPE_LOG))
        self.assertFalse(task.has_log(NO_EXIT_LOG))

    def test_no_exit_box_at_all_pauses_instead_of_entering_boss(self):
        """兜底：连固定坐标框都拿不到时暂停等人工处理，绝不自动进 Boss。"""
        task = self._task(qualified=False, exit_button=True)

        with mock.patch.object(utils, "_find_run_exit_box", return_value=None), \
                mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(result)
        pause.assert_called_once_with()
        self.assertEqual([], task.clicked_names())
        self.assertTrue(task.has_log(NO_EXIT_LOG))
        self.assertEqual(CHECK_POINT, task.node_status["pro_mode_review_paused"])


    def test_no_exit_box_fallback_keeps_holding_later_frames(self):
        """兜底暂停之后也要一直占住本帧，不能下一帧就自己进 Boss。"""
        task = self._task(qualified=False, exit_button=True)

        with mock.patch.object(utils, "_find_run_exit_box", return_value=None), \
                mock.patch.object(utils, "_pause_for_manual_review") as pause:
            first = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)
            second = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertTrue(first)
        self.assertTrue(second)
        pause.assert_called_once_with()
        self.assertEqual([], task.clicked_names())

    def test_first_layer_is_not_a_check_point(self):
        task = self._task(qualified=True, layer=0)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertFalse(result)
        pause.assert_not_called()
        self.assertEqual([], task.clicked_names())
        self.assertFalse(task.has_log(STOP_LOG))

    def test_switch_off_does_nothing(self):
        task = self._task(qualified=True, config=_pro_config(**{PRO_MODE_KEY: False}))

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = _maybe_escape_for_engraving_pro_mode(task, is_final_boss=True)

        self.assertFalse(result)
        pause.assert_not_called()
        self.assertEqual([], task.clicked_names())


class TestRouteSelectionStopsBeforeBoss(unittest.TestCase):
    """页面级（假 task）确认"达标 -> 走不到"点击进入Boss"那一步。"""

    def _task(self, qualified=True, exit_button=True):
        return _FakeTask(
            config=_pro_config(),
            node_status={"pass_final_boss_count": 1},
            all_texts=[_exit_button()] if exit_button else [],
            member_status=(
                _qualified_member_status() if qualified else _empty_member_status()
            ),
        )

    def test_qualified_stops_instead_of_entering_boss(self):
        task = self._task(qualified=True, exit_button=True)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = handle_route_selection(task)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertTrue(task.disabled)
        self.assertEqual([], task.clicks)
        self.assertEqual([], task.clicked_names())
        self.assertFalse(task.has_log(ENTER_BOSS_LOG))
        self.assertTrue(task.node_status["reach_final_boss"])

    def test_not_qualified_escapes_instead_of_entering_boss(self):
        task = self._task(qualified=False, exit_button=True)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = handle_route_selection(task)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertEqual(["退出"], task.clicked_names())
        self.assertEqual([], task.clicks)
        self.assertFalse(task.has_log(ENTER_BOSS_LOG))

    def test_not_qualified_without_ocr_exit_escapes_via_fixed_coordinate(self):
        """OCR 没识别到「退出」文本：用固定坐标逃离，不把不达标的一局送进Boss。"""
        task = self._task(qualified=False, exit_button=False)

        with mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = handle_route_selection(task)

        self.assertTrue(result)
        pause.assert_not_called()
        self.assertEqual(["退出"], task.clicked_names())
        self.assertNotIn(ENTER_BOSS_CLICK, task.clicks)
        self.assertFalse(task.has_log(ENTER_BOSS_LOG))

    def test_not_qualified_without_any_exit_box_pauses_before_boss(self):
        """连固定坐标框都拿不到：暂停等人工处理，绝不点进最终Boss。"""
        task = self._task(qualified=False, exit_button=False)

        with mock.patch.object(utils, "_find_run_exit_box", return_value=None), \
                mock.patch.object(utils, "_pause_for_manual_review") as pause:
            result = handle_route_selection(task)

        self.assertTrue(result)
        pause.assert_called_once_with()
        self.assertEqual([], task.clicked_names())
        self.assertNotIn(ENTER_BOSS_CLICK, task.clicks)
        self.assertFalse(task.has_log(ENTER_BOSS_LOG))


if __name__ == "__main__":
    unittest.main()

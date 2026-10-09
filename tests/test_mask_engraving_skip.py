# -*- coding: utf-8 -*-
"""面具卡牌刻印页：未命中配置且刷新次数为 0 时必须「跳过」。

`handle_chaos_mask_engraving` 的"无替换模式"分支在"未命中 + 刷新次数为 0"时
必须点击「跳过」并 `return True`，不能返回 False 落到通用 `handle_confirm`，
否则会把不匹配的刻印确认装到面具卡上。优先找"跳过"文本框，找不到时点击固定
坐标 0.747,0.932。本文件只做纯逻辑判断，不依赖游戏画面。
"""
import pathlib
import sys
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

import utils  # noqa: E402
import utils_chaos  # noqa: E402
SPEC = "自身攻击卡牌伤害总量提升30%"
MATCHED_DESC = "1回合内，" + SPEC
UNMATCHED_DESC = "Q每有1层光之刻印，即可赋豫全体敌人痛苦1"
SKIP_FIXED_POS = (0.747, 0.932)
REFRESH_POS = (0.913, 0.667)
PAGE_KEY = "面具卡牌刻印"


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


def _title_box():
    return _Box(PAGE_KEY, 0.499 * 1600, 0.126 * 900, width=200, height=30)


def _confirm_box():
    """页面右下角的「确认」按钮：_clean_match(name, "确认") 必须成立。"""
    return _Box("确认", 0.921 * 1600, 0.931 * 900, width=80, height=30)


def _desc_box(text):
    """刻印描述区域 (0.583,0.463,0.937,0.621) 里那一条文本。"""
    return _Box(text, 0.76 * 1600, 0.54 * 900, width=420, height=26)


def _refresh_box(text):
    """刷新按钮 (0.913,0.667)，文本形如 "刷新 3/3"。"""
    return _Box(text, 0.913 * 1600, 0.667 * 900, width=120, height=28)


def _skip_text_box():
    return _Box("跳过", 0.747 * 1600, 0.932 * 900, width=70, height=26)


class _FakeTask:
    """还原 handle_chaos_mask_engraving 依赖的最小接口。"""

    def __init__(self, texts, config=None):
        self.width = 1600
        self.height = 900
        self.all_texts = list(texts)
        self.config = dict(config or {})
        self.default_config = {}
        # 确认按钮是否"激活"看 is_button_active：纯白帧不会被判成禁用灰色。
        self.frame = np.full((900, 1600, 3), 255, dtype=np.uint8)
        self.logs = []
        self.box_clicks = []
        self.clicks = []
        self.moves = []

    def log_info(self, message):
        self.logs.append(message)

    def click_box(self, box):
        self.box_clicks.append(box)

    def click(self, x, y):
        self.clicks.append((round(x, 3), round(y, 3)))

    def move_relative(self, x, y):
        self.moves.append((round(x, 3), round(y, 3)))

    def sleep(self, _seconds):
        pass

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, width=0.0, height=0.0,
                      name=None, **_kwargs):
        return _Box(name or "screen", x * self.width, y * self.height,
                    width=180, height=120)

    def clicked_names(self):
        return [box.name for box in self.box_clicks]

    def has_log(self, keyword):
        return any(keyword in log for log in self.logs)


def _no_replace_frame(desc_text, refresh_text=None, with_skip_text=False):
    texts = [_title_box(), _confirm_box(), _desc_box(desc_text)]
    if refresh_text is not None:
        texts.append(_refresh_box(refresh_text))
    if with_skip_text:
        texts.append(_skip_text_box())
    return texts


def _run_handler_chain_from_mask_engraving(task):
    """按 ChaosMode.run 的写法，从面具刻印处理函数开始往后跑，返回吃帧的处理函数。"""
    handlers = utils_chaos.PAGE_HANDLERS
    start = handlers.index(utils_chaos.handle_chaos_mask_engraving)
    for handle_page in handlers[start:]:
        if handle_page(task):
            return handle_page
    return None


class TestMaskEngravingUnmatchedNoRefresh(unittest.TestCase):
    """未命中 + 刷新次数 0：必须跳过，绝不落到通用确认。"""

    def test_clicks_skip_fixed_position_and_not_confirm(self):
        task = _FakeTask(_no_replace_frame(UNMATCHED_DESC, "刷新 0/3"))
        result = utils_chaos.handle_chaos_mask_engraving(task)

        self.assertTrue(result, "必须吃掉本帧（return True），否则会落到通用确认")
        self.assertEqual([SKIP_FIXED_POS], task.moves)
        self.assertNotIn("确认", task.clicked_names())
        self.assertTrue(task.has_log("无可刷新次数，点击跳过"))

    def test_prefers_skip_text_box_when_recognized(self):
        task = _FakeTask(_no_replace_frame(UNMATCHED_DESC, "刷新 0/3",
                                           with_skip_text=True))
        result = utils_chaos.handle_chaos_mask_engraving(task)

        self.assertTrue(result)
        self.assertEqual(["跳过"], task.clicked_names())
        self.assertNotIn("确认", task.clicked_names())
        self.assertEqual([], task.moves)

    def test_handler_chain_never_presses_generic_confirm(self):
        """行为约束：这一帧往后跑一遍 PAGE_HANDLERS，通用「确认」一次都不能被点。"""
        task = _FakeTask(_no_replace_frame(UNMATCHED_DESC, "刷新 0/3"))
        handle_page = _run_handler_chain_from_mask_engraving(task)

        self.assertIs(handle_page, utils_chaos.handle_chaos_mask_engraving)
        self.assertNotIn("确认", task.clicked_names())
        self.assertEqual([SKIP_FIXED_POS], task.moves)

    def test_generic_confirm_would_press_confirm_button(self):
        """这帧若落到通用确认分支，就会点击「确认」并装上不匹配的刻印。"""
        task = _FakeTask(_no_replace_frame(UNMATCHED_DESC, "刷新 0/3"))
        self.assertTrue(utils.handle_confirm(task))
        self.assertEqual(["确认"], task.clicked_names())


class TestMaskEngravingUnmatchedWithRefresh(unittest.TestCase):
    def test_clicks_refresh_when_charges_left(self):
        task = _FakeTask(_no_replace_frame(UNMATCHED_DESC, "刷新 3/3"))
        result = utils_chaos.handle_chaos_mask_engraving(task)

        self.assertTrue(result)
        self.assertEqual([REFRESH_POS], task.moves)
        self.assertNotIn("确认", task.clicked_names())


class TestMaskEngravingMatched(unittest.TestCase):
    def test_matched_hands_to_confirm_button(self):
        task = _FakeTask(_no_replace_frame(MATCHED_DESC, "刷新 3/3"))
        result = utils_chaos.handle_chaos_mask_engraving(task)

        self.assertFalse(result, "命中配置仍然交给确认按钮（原设计不变）")
        self.assertTrue(task.has_log("交给确认按钮处理"))
        self.assertEqual([], task.moves)
        self.assertEqual([], task.clicked_names())

    def test_matched_frame_chain_presses_confirm(self):
        task = _FakeTask(_no_replace_frame(MATCHED_DESC, "刷新 0/3"))
        _run_handler_chain_from_mask_engraving(task)

        self.assertEqual(["确认"], task.clicked_names())


def _replace_frame(slot1_text, new_text, refresh_text="刷新 3/3"):
    """替换模式（0.495,0.221 处有"替换"文本）的假帧。

    刻印1描述区域 (0.589,0.336,0.898,0.487)、新刻印描述区域
    (0.589,0.601,0.902,0.762)、刷新按钮 (0.916,0.810)。
    """
    return [
        _title_box(),
        _confirm_box(),
        _Box("替换", 0.495 * 1600, 0.221 * 900, width=60, height=24),
        _Box(slot1_text, 0.74 * 1600, 0.41 * 900, width=420, height=26),
        _Box(new_text, 0.75 * 1600, 0.68 * 900, width=420, height=26),
        _Box(refresh_text, 0.916 * 1600, 0.810 * 900, width=120, height=28),
    ]


if __name__ == "__main__":
    unittest.main()

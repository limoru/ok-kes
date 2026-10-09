"""德朗商店"花钱刷新"按钮的状态保护测试。

付费刷新分支必须满足：

1. 剩余次数计数 N/M 里 N 为 0 时判为按钮不可用，直接放弃刷新，让流程往后走
   （最终由 handle_leave 点「离开」）；
2. 刷新费用只认纯数字文本框，"0/3" 这类计数文本不能当成费用；
3. 刷新费用必须大于 0。

本文件只做纯逻辑判断，不需要截图或游戏画面。
"""
import pathlib
import sys
import unittest

# 与其他测试保持一致：ok_tasks 内部使用平级导入，需要把仓库根目录和
# ok_tasks 目录都加入 sys.path 才能正常 import。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

from utils import handle_shop  # noqa: E402


class _Box:
    """OCR 文本框替身，只保留 utils 里用到的属性。"""

    def __init__(self, name, center_x, center_y, width=40, height=20):
        self.name = name
        self.x = int(center_x - width / 2)
        self.y = int(center_y - height / 2)
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height


def _box_at(name, rel_x, rel_y, width=40, height=20):
    return _Box(name, rel_x * 1600, rel_y * 900, width, height)


def _shop_texts(remaining=None, cost="30", credit="474"):
    """构造一帧商店页面的 OCR 文本。

    remaining=计数文本（如 "0/3"），None 表示 OCR 没读到计数；
    cost=刷新费用文本，None 表示没读到费用。
    """
    texts = [
        _box_at("移除卡牌", 0.729, 0.261, width=90, height=26),
        _box_at(credit, 0.794, 0.054, width=40, height=22),
    ]
    if remaining is not None:
        texts.append(_box_at(remaining, 0.072, 0.932, width=50, height=20))
    if cost is not None:
        texts.append(_box_at(cost, 0.116, 0.932, width=30, height=20))
    return texts


class _FakeTask:
    """只还原 handle_shop 依赖的最小接口。"""

    def __init__(self, all_texts, config=None):
        self.width = 1600
        self.height = 900
        self.all_texts = all_texts
        self.config = dict(config or {})
        self.default_config = {}
        self.node_status = {
            "shop": True,
            "shop_engraving_rejected_names": set(),
        }
        self.logs = []
        self.clicks = []

    def log_info(self, message):
        self.logs.append(message)

    def click_box(self, box):
        self.clicks.append(box)

    def sleep(self, _seconds):
        pass

    def box_of_screen(self, *_args):
        return None

    def find_feature(self, *_args, **_kwargs):
        return []

    def clicked_names(self):
        return [box.name for box in self.clicks]


def _engraving_task(all_texts, credit_floor=150):
    return _FakeTask(
        all_texts,
        config={
            "刷装备刻印": True,
            "刷装备刻印信用点下限": credit_floor,
            "刻印专业模式": False,
        },
    )


class TestShopPaidRefreshGuard(unittest.TestCase):
    """付费刷新按钮变灰时的兜底行为。"""

    def test_exhausted_refresh_does_not_click(self):
        """剩余次数 0/3：按钮已灰，不能再点，且要让出流程给"离开商店"。"""
        task = _engraving_task(_shop_texts(remaining="0/3"))

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(any("已变灰" in log for log in task.logs))

    def test_exhausted_refresh_tolerates_ocr_merged_icon(self):
        """OCR 把信用点图标并进计数框（U0/3 / U 0/3）时同样要判为不可刷新。"""
        for remaining in ("U0/3", "U 0/3", "0 / 3"):
            task = _engraving_task(_shop_texts(remaining=remaining))

            self.assertFalse(handle_shop(task), remaining)
            self.assertEqual([], task.clicked_names(), remaining)

    def test_available_refresh_clicks_and_uses_real_cost(self):
        """还有次数时可以刷新，并且费用要按真实数字 30 计算，而不是 2/3 里的 2。"""
        task = _engraving_task(_shop_texts(remaining="2/3"))

        self.assertTrue(handle_shop(task))
        self.assertEqual(["2/3"], task.clicked_names())
        self.assertTrue(
            any("花费30信用点刷新商店" in log for log in task.logs)
        )

    def test_credit_below_floor_does_not_click(self):
        """信用点没超过配置下限时不花钱刷新。"""
        task = _engraving_task(
            _shop_texts(remaining="2/3", credit="100"),
            credit_floor=150,
        )

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("不满足花费刷新条件" in log for log in task.logs)
        )

    def test_missing_counter_falls_back_to_cost_box(self):
        """计数框没被 OCR 识别到时，退回到点击纯数字的费用框。"""
        task = _engraving_task(_shop_texts(remaining=None, cost="30"))

        self.assertTrue(handle_shop(task))
        self.assertEqual(["30"], task.clicked_names())


class TestShopFlagClearedAfterVisit(unittest.TestCase):
    """商店逛完后必须清掉 node_status['shop']，否则会在营地页反复进店。

    handle_shop 在本轮无法执行任何操作时必须清掉 node_status['shop']；
    否则回到营地页后，handle_rest 会再次进入商店，形成反复进出店的循环。
    """

    def test_exhausted_shop_clears_flag(self):
        """货架逛完（刷新按钮变灰、删卡被专业模式禁用）时要清掉 shop 标记。"""
        task = _engraving_task(_shop_texts(remaining="0/3"))
        self.assertTrue(task.node_status["shop"])

        self.assertFalse(handle_shop(task))

        self.assertFalse(task.node_status["shop"])
        self.assertTrue(
            any("本次商店流程结束" in log for log in task.logs)
        )

    def test_non_shop_page_keeps_flag(self):
        """不在商店页面时（没有"移除卡牌"/"售罄"）不能误清 shop 标记。"""
        task = _engraving_task([
            _box_at("德朗商店", 0.360, 0.138, width=90, height=26),
            _box_at("474", 0.794, 0.054, width=40, height=22),
        ])

        self.assertFalse(handle_shop(task))
        self.assertTrue(task.node_status["shop"])


if __name__ == "__main__":
    unittest.main()

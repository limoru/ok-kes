"""德朗商店删卡的两道门槛：信用点下限与次数上限。

刻印专业模式下商店删卡由两道配置门槛约束（逻辑见
`_shop_removal_block_reason`）：

1. 「商店删卡信用点下限」：当前信用点**超过**该值才允许删卡；0（默认）= 不删卡；
2. 「商店删卡次数上限」：本局在德朗商店里删掉的卡牌数达到该值后不再删卡
   （默认 2，0 = 不删卡）。计数用 `node_status['shop_removed_card_count']`，
   只数商店删卡；事件/三选一删掉的卡牌只计入 `removed_card_count`，不占额度。

非专业模式（"刻印专业模式"或"刷装备刻印"关闭）不受这两项影响，使用固定
5 张上限和原有执行顺序。

本文件只做纯逻辑判断，不需要截图或游戏画面。
"""
import pathlib
import sys
import unittest
import unittest.mock

# 与其他测试保持一致：ok_tasks 内部使用平级导入，需要把仓库根目录和
# ok_tasks 目录都加入 sys.path 才能正常 import。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

import utils  # noqa: E402
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


def _shop_texts(credit="800", cost="80"):
    """构造一帧"商店里只剩删卡可做"的 OCR 文本。

    credit=None 表示信用点没被 OCR 读到；cost=None 表示删卡费用没读到。
    """
    texts = [_box_at("移除卡牌", 0.729, 0.261, width=90, height=26)]
    if credit is not None:
        texts.append(_box_at(credit, 0.794, 0.054, width=40, height=22))
    if cost is not None:
        texts.append(_box_at(cost, 0.724, 0.319, width=30, height=20))
    return texts


class _FakeTask:
    """只还原 handle_shop 依赖的最小接口。"""

    def __init__(
        self,
        all_texts,
        config=None,
        removed_card_count=0,
        shop_removed_card_count=0,
    ):
        self.width = 1600
        self.height = 900
        self.all_texts = all_texts
        self.config = dict(config or {})
        self.default_config = {}
        self.node_status = {
            "shop": True,
            "shop_engraving_rejected_names": set(),
            "removed_card_count": removed_card_count,
            "shop_removed_card_count": shop_removed_card_count,
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


def _shop_task(
    all_texts,
    floor=None,
    limit=None,
    removed=0,
    shop_removed=0,
    pro_mode=True,
    engraving=True,
):
    """构造商店页的假 task；floor/limit 为 None 表示配置里不写这一项。

    `removed` 是本局删卡总数（含事件/三选一），`shop_removed` 是其中在商店里
    删掉的数量——上限只看后者。
    """
    config = {
        "刷装备刻印": engraving,
        "刷装备刻印信用点下限": 150,
        "刻印专业模式": pro_mode,
    }
    if floor is not None:
        config["商店删卡信用点下限"] = floor
    if limit is not None:
        config["商店删卡次数上限"] = limit
    return _FakeTask(
        all_texts,
        config=config,
        removed_card_count=removed,
        shop_removed_card_count=shop_removed,
    )


class TestShopRemoveCardCreditFloor(unittest.TestCase):
    """「商店删卡信用点下限」的判定。"""

    def test_zero_floor_never_removes(self):
        """下限为 0（默认）：专业模式下本局完全不删卡。"""
        task = _shop_task(_shop_texts(credit="999"), floor=0)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("未配置「商店删卡信用点下限」" in log for log in task.logs)
        )

    def test_missing_floor_config_never_removes(self):
        """老配置里没有这一项时按 0 处理。"""
        task = _shop_task(_shop_texts(credit="999"), floor=None)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(any("跳过「移除卡牌」" in log for log in task.logs))

    def test_credit_above_floor_removes_card(self):
        """信用点超过下限：允许花信用点删卡。"""
        task = _shop_task(_shop_texts(credit="800", cost="80"), floor=500)

        self.assertTrue(handle_shop(task))
        self.assertEqual(["移除卡牌"], task.clicked_names())
        self.assertTrue(any("足够，点击移除" in log for log in task.logs))

    def test_credit_equal_floor_does_not_remove(self):
        """刚好等于下限不算"超过"，不删卡。"""
        task = _shop_task(_shop_texts(credit="500", cost="80"), floor=500)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("未超过「商店删卡信用点下限」500" in log for log in task.logs)
        )

    def test_credit_below_floor_does_not_remove(self):
        task = _shop_task(_shop_texts(credit="300", cost="80"), floor=500)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())

    def test_unreadable_credit_does_not_remove(self):
        """信用点没读到（按 0 处理）时不删卡，避免误花信用点。"""
        task = _shop_task(_shop_texts(credit=None, cost="80"), floor=300)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())

    def test_cost_above_credit_does_not_remove(self):
        """超过下限但删卡费用不够时不删卡（原有判断不变）。"""
        task = _shop_task(_shop_texts(credit="600", cost="700"), floor=500)

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(any("不足" in log for log in task.logs))

    def test_pro_mode_off_ignores_floor(self):
        """刻印专业模式关闭时门槛不生效，仍按刻印模式常规顺序删卡。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"), floor=99999, pro_mode=False,
        )

        self.assertTrue(handle_shop(task))
        self.assertEqual(["移除卡牌"], task.clicked_names())

    def test_engraving_off_ignores_floor(self):
        """刷装备刻印关闭时门槛不生效，删卡仍在函数开头执行。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"), floor=99999, engraving=False,
        )

        self.assertTrue(handle_shop(task))
        self.assertEqual(["移除卡牌"], task.clicked_names())

    def test_junk_floor_value_falls_back_to_zero(self):
        """下限填了非数字时按 0 处理（不删卡），不能抛异常。"""
        task = _shop_task(_shop_texts(credit="800", cost="80"), floor="abc")

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())

    def test_block_reason_helper(self):
        """`_shop_removal_block_reason`：空串表示允许删卡。"""
        allowed = _shop_task(_shop_texts(credit="800"), floor=500)
        self.assertEqual("", utils._shop_removal_block_reason(allowed))

        blocked = _shop_task(_shop_texts(credit="500"), floor=500)
        self.assertIn("未超过", utils._shop_removal_block_reason(blocked))

        disabled = _shop_task(_shop_texts(credit="800"), floor=0)
        self.assertIn("未配置", utils._shop_removal_block_reason(disabled))


class TestShopRemoveCardLimit(unittest.TestCase):
    """「商店删卡次数上限」的判定（信用点门槛已满足的前提下）。"""

    def test_count_reaches_limit_does_not_remove(self):
        """本局在商店删卡数达到上限（默认 2）后，商店不再删卡。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"),
            floor=500,
            limit=2,
            removed=2,
            shop_removed=2,
        )

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("达到商店删卡上限2" in log for log in task.logs)
        )
        # 上限看的是商店删卡数，日志里要说清楚是哪一份计数满了。
        self.assertTrue(
            any("本局已在商店移除2张卡牌" in log for log in task.logs)
        )

    def test_default_limit_is_two_when_config_missing(self):
        """配置里没有这一项时按默认值 2 处理。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"),
            floor=500,
            limit=None,
            shop_removed=2,
        )

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("达到商店删卡上限2" in log for log in task.logs)
        )

    def test_count_below_limit_removes_card(self):
        """还没到上限时照常删卡。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"),
            floor=500,
            limit=2,
            shop_removed=1,
        )

        self.assertTrue(handle_shop(task))
        self.assertEqual(["移除卡牌"], task.clicked_names())

    def test_zero_limit_never_removes(self):
        """上限为 0：一次都不删（等价于完全禁用商店删卡）。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"),
            floor=500,
            limit=0,
            shop_removed=0,
        )

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("达到商店删卡上限0" in log for log in task.logs)
        )

    def test_junk_limit_value_falls_back_to_two(self):
        """上限填了非数字时按默认 2 处理，不能抛异常。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"),
            floor=500,
            limit="abc",
            shop_removed=2,
        )

        self.assertFalse(handle_shop(task))
        self.assertEqual([], task.clicked_names())

    def test_non_pro_mode_keeps_five_card_limit(self):
        """非专业模式沿用旧的固定上限 5 张。"""
        at_limit = _shop_task(
            _shop_texts(credit="800", cost="80"), pro_mode=False, shop_removed=5,
        )
        self.assertFalse(handle_shop(at_limit))
        self.assertEqual([], at_limit.clicked_names())
        self.assertTrue(
            any("达到商店删卡上限5" in log for log in at_limit.logs)
        )

        below_limit = _shop_task(
            _shop_texts(credit="800", cost="80"), pro_mode=False, shop_removed=4,
        )
        self.assertTrue(handle_shop(below_limit))
        self.assertEqual(["移除卡牌"], below_limit.clicked_names())

    def test_removal_limit_helper(self):
        """`_shop_removal_limit`：专业模式读配置，非专业模式恒为 5。"""
        self.assertEqual(
            3,
            utils._shop_removal_limit(
                _shop_task(_shop_texts(), floor=500, limit=3)
            ),
        )
        self.assertEqual(
            2,
            utils._shop_removal_limit(_shop_task(_shop_texts(), limit=None)),
        )
        self.assertEqual(
            5,
            utils._shop_removal_limit(
                _shop_task(_shop_texts(), pro_mode=False, limit=1)
            ),
        )

    def test_event_removals_do_not_consume_the_shop_quota(self):
        """事件/三选一删卡只加总数，不占商店额度，也不被这个上限拦。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"), floor=500, limit=2,
        )
        # 事件里已经删掉 3 张：removed_card_count 涨了，商店删卡数没动。
        utils._record_removed_cards(task, 3)

        self.assertEqual(3, task.node_status["removed_card_count"])
        self.assertEqual(0, task.node_status["shop_removed_card_count"])
        # 商店额度没被占用，照常删卡。
        self.assertTrue(handle_shop(task))
        self.assertEqual(["移除卡牌"], task.clicked_names())

    def test_shop_entry_removal_is_counted_as_shop_removal(self):
        """商店入口点开删卡页 → handle_remove 真删掉卡牌时才计入商店删卡数。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"), floor=500, limit=2,
        )
        task.all_texts.append(_box_at("移除", 0.945, 0.918, width=60, height=30))
        # 先来 3 张事件删卡，确认不会混进商店计数。
        utils._record_removed_cards(task, 3)

        self.assertTrue(handle_shop(task))
        # 只是点开了删卡页，还没删掉：标记挂着，计数不动。
        self.assertTrue(task._pending_shop_removal)
        self.assertEqual(0, task.node_status["shop_removed_card_count"])

        with unittest.mock.patch.object(
            utils, "is_button_active", return_value=True
        ):
            self.assertTrue(utils.handle_remove(task))

        self.assertEqual(4, task.node_status["removed_card_count"])
        self.assertEqual(1, task.node_status["shop_removed_card_count"])
        self.assertFalse(task._pending_shop_removal)
        self.assertTrue(
            any("其中商店删卡1张" in log for log in task.logs)
        )

    def test_shop_limit_stops_after_two_shop_removals(self):
        """商店里连删两次后第三次不再删；事件删的 3 张不算商店额度。"""
        task = _shop_task(
            _shop_texts(credit="800", cost="80"), floor=500, limit=2,
        )
        task.all_texts.append(_box_at("移除", 0.945, 0.918, width=60, height=30))
        utils._record_removed_cards(task, 3)

        with unittest.mock.patch.object(
            utils, "is_button_active", return_value=True
        ):
            for expected in (1, 2):
                # 真实流程里 node_status['shop'] 由路线节点识别置回 True；
                # 每次删完 _try_remove_shop_card 都会把它清掉，所以这里手动还原。
                task.node_status["shop"] = True
                self.assertTrue(handle_shop(task))
                self.assertTrue(utils.handle_remove(task))
                self.assertEqual(
                    expected, task.node_status["shop_removed_card_count"]
                )

            task.node_status["shop"] = True
            self.assertFalse(handle_shop(task))
            self.assertEqual(2, task.node_status["shop_removed_card_count"])

    def test_limit_is_only_read_by_the_shop_removal_path(self):
        """全模块只有商店删卡会读这个上限，事件/三选一删卡路径完全不看它。"""
        source = pathlib.Path(utils.__file__).read_text(encoding="utf-8")
        # 一处是 def 定义，一处是 _try_remove_shop_card 里的调用。
        self.assertEqual(2, source.count("_shop_removal_limit("))

    def test_negative_limit_is_clamped_to_zero(self):
        """上限填负数时按 0 处理：一次都不删，不能因为负数反而放行。"""
        self.assertEqual(
            0,
            utils._shop_removal_limit(
                _shop_task(_shop_texts(), floor=500, limit=-3)
            ),
        )


if __name__ == "__main__":
    unittest.main()

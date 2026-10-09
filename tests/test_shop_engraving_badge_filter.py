"""德朗商店"刻印角标预筛选"测试。

`badge_count == 0` 表示本货架没有角标装备，handle_shop 应整排商品跳过，
不再逐件进入详情页。例外是槽位"刻印已经拿到、只差目标名"时：能补名字的
候选通常不带角标，必须由 name_only_candidate_slot 放行，否则槽位永远补不上
名字；空槽或槽位没有任何目标刻印时仍然跳过。
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

import ok_tasks.utils as utils  # noqa: E402
from utils_equipment_engraving import (  # noqa: E402
    resolve_candidate_engraving_flag,
    should_assign_to_target_member,
)


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


# 货架商品的信用点图标位置（名称/价格文本框按它推导，保证和 handle_shop 的
# 取点公式一致：价格取 icon_x + 0.099 / icon_y - 0.001，名称取图标上方区域）。
_ITEM_ICON_POSITIONS = ((0.050, 0.800), (0.200, 0.800))


class _FakeTask:
    """只还原 handle_shop 依赖的最小接口。"""

    def __init__(self, item_names, credit_icons, config=None, member_status=None):
        self.width = 1600
        self.height = 900
        self._credit_icons = credit_icons
        self.all_texts = [
            _box_at("移除卡牌", 0.729, 0.261, width=90, height=26),
            _box_at("805", 0.794, 0.054, width=40, height=22),
        ]
        for name, icon in zip(item_names, credit_icons):
            icon_x = (icon.x + icon.width / 2) / self.width
            icon_y = (icon.y + icon.height / 2) / self.height
            self.all_texts.append(
                _box_at(name, icon_x + 0.0535, icon_y - 0.176, width=80, height=24)
            )
            self.all_texts.append(
                _box_at("96", icon_x + 0.099, icon_y - 0.001, width=30, height=20)
            )
        self.config = dict(config or {})
        self.default_config = {}
        if member_status is not None:
            self.member_status = member_status
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

    def find_feature(self, feature_name=None, box=None, **_kwargs):
        if feature_name == "credit_icon":
            return list(self._credit_icons)
        return []

    def clicked_names(self):
        return [box.name for box in self.clicks]


def _credit_icons(count):
    """按货架布局造出 count 个信用点图标特征。"""
    return [
        _Box("", x * 1600, y * 900, 39, 46)
        for x, y in _ITEM_ICON_POSITIONS[:count]
    ]


def _engraving_task(item_names, member_status=None):
    return _FakeTask(
        item_names,
        credit_icons=_credit_icons(len(item_names)),
        config={
            "刷装备刻印": True,
            "刷装备刻印信用点下限": 150,
            "刻印专业模式": False,
            "装备1号位优先级": ["欺骗暗影的匕首", "混沌之识"],
            "装备2号位优先级": ["精灵王的金冠"],
            "装备3号位优先级": ["深绿桎梏", "深绿黑夜"],
            "装备刻印优先级": [
                "己方主动生成5张",
                "己方主动使敌方的行动计数增加",
            ],
        },
        member_status=member_status,
    )


def _member_status(names, descriptions):
    """构造目标主战员的装备状态（与 utils._equipment_state 的结构一致）。"""
    return {
        "equipment": {
            "names": list(names),
            "descriptions": list(descriptions),
            "qualities": ["", "", ""],
        },
    }


class TestShopEngravingBadgeFilter(unittest.TestCase):
    """角标预筛选：没角标就不点开详情页。"""

    def test_no_badge_skips_every_item(self):
        """整排都没有角标 = 本货架没有刻印装备，不能点开任何商品。"""
        task = _engraving_task(["精灵王的金冠", "魔力蓝宝石"])

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection(set(), 0, {})
        ):
            self.assertFalse(utils.handle_shop(task))

        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("本货架没有刻印装备" in log for log in task.logs)
        )
        self.assertFalse(
            any("回退到逐一进详情页检查" in log for log in task.logs)
        )

    def test_item_with_badge_is_opened(self):
        """有角标的商品才点进详情页，并把"带刻印"记进 node_status。"""
        task = _engraving_task(["精灵王的金冠"])

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection({0}, 1, {})
        ):
            self.assertTrue(utils.handle_shop(task))

        self.assertEqual(1, len(task.clicks))
        self.assertIs(True, task.node_status["shop_opened_has_engraving"])

    def test_only_badged_item_is_opened(self):
        """多件商品时，只点有角标的那一件，没角标的直接跳过。"""
        task = _engraving_task(["精灵王的金冠", "魔力蓝宝石"])

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection({1}, 1, {})
        ):
            self.assertTrue(utils.handle_shop(task))

        self.assertEqual(1, len(task.clicks))
        self.assertTrue(
            any("装备「精灵王的金冠」未检测到刻印角标" in log for log in task.logs)
        )
        self.assertIs(True, task.node_status["shop_opened_has_engraving"])

    def test_borderline_confidence_item_is_opened_for_description_check(self):
        """分数贴着阈值时不能当"没有角标"跳过，要进详情页看描述。"""
        task = _engraving_task(["魔力蓝宝石", "精灵王的金冠"])

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges",
            return_value=utils.ShopBadgeDetection(set(), 0, {1: 0.84}),
        ):
            self.assertTrue(utils.handle_shop(task))

        # 没落在边界区间的那件整排跳过，落在边界区间的那件点进详情页
        self.assertTrue(
            any("装备「魔力蓝宝石」未检测到刻印角标" in log for log in task.logs),
            task.logs,
        )
        self.assertTrue(
            any("装备「精灵王的金冠」角标置信度=0.84 落在边界区间" in log
                for log in task.logs),
            task.logs,
        )
        self.assertEqual(1, len(task.clicks))
        self.assertTrue(
            any("装备「精灵王的金冠」（刷装备刻印模式" in log for log in task.logs),
            task.logs,
        )


    def test_name_only_item_is_opened_when_slot_only_misses_name(self):
        """2号位刻印已达标、只差目标名时，没有角标的目标名装备必须点开。"""
        task = _engraving_task(
            ["精灵王的金冠"],
            member_status=_member_status(
                ["", "侦察兵的军靴🎆", "深绿黑夜"],
                [
                    "",
                    "击败敌人时，固定护盾60%🎆己方主动生成5张或更多卡牌时，"
                    "自身额外攻击伤害量+30% (每回合1次)",
                    "",
                ],
            ),
        )

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection(set(), 0, {})
        ):
            self.assertTrue(utils.handle_shop(task))

        self.assertEqual(1, len(task.clicks))
        self.assertTrue(
            any("2号位刻印已达标、只差目标名" in log for log in task.logs)
        )
        self.assertIs(False, task.node_status["shop_opened_has_engraving"])

    def test_name_only_item_is_still_skipped_when_slot_has_no_engraving(self):
        """空槽没有刻印可继承：仍然跳过，不白花信用点买只有名字的装备。"""
        task = _engraving_task(
            ["精灵王的金冠"],
            member_status=_member_status(["", "", ""], ["", "", ""]),
        )

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection(set(), 0, {})
        ):
            self.assertFalse(utils.handle_shop(task))

        self.assertEqual([], task.clicked_names())
        self.assertTrue(
            any("装备「精灵王的金冠」未检测到刻印角标" in log for log in task.logs)
        )

    def test_name_only_item_is_still_skipped_when_slot_name_already_matches(self):
        """槽位名字已经对了：这件没有刻印的候选没有任何新进展，仍然跳过。"""
        task = _engraving_task(
            ["精灵王的金冠"],
            member_status=_member_status(
                ["", "精灵王的金冠", ""],
                ["", "回合结束时，己方主动生成5张牌", ""],
            ),
        )

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection(set(), 0, {})
        ):
            self.assertFalse(utils.handle_shop(task))

        self.assertEqual([], task.clicked_names())

    def test_opened_name_only_item_is_accepted_by_decision_layer(self):
        """整链测试：预筛选放行 -> 角标记录 False -> 购买页判定层按"继承换名"接受。

        这条是这次漏测的关键点：预筛选（本文件）与判定层
        （tests/test_equipment_free_engraving.py）各自都是绿的，但"预筛选放行后
        交给判定层"这一段没人串起来测——把两侧的函数用真实槽位状态接在一起，
        防止以后又出现"点开了却必被取消"或"该点开却被跳过"。
        """
        task = _engraving_task(
            ["精灵王的金冠"],
            member_status=_member_status(
                ["", "侦察兵的军靴🎆", "深绿黑夜"],
                [
                    "",
                    "击败敌人时，固定护盾60%🎆己方主动生成5张或更多卡牌时，"
                    "自身额外攻击伤害量+30% (每回合1次)",
                    "",
                ],
            ),
        )

        with mock.patch.object(
            utils, "_detect_shop_engraving_badges", return_value=utils.ShopBadgeDetection(set(), 0, {})
        ):
            self.assertTrue(utils.handle_shop(task))

        # 预筛选放行后，购买页拿到的角标结论是 False（"确认没有角标"）
        candidate_has_engraving = resolve_candidate_engraving_flag(
            task.node_status["shop_opened_has_engraving"], "",
        )
        equipment = task.member_status["equipment"]
        should_install, reason = should_assign_to_target_member(
            equipment["names"][1],
            equipment["descriptions"][1],
            "精灵王的金冠",
            "回合开始时，获得护盾",  # 详情页描述：不含刻印关键词、也没有刻印图标
            ["精灵王的金冠"],
            ["己方主动生成5张", "己方主动使敌方的行动计数增加"],
            candidate_has_engraving,
        )

        self.assertTrue(should_install, reason)
        self.assertIn("通过继承机制安全替换", reason)


if __name__ == "__main__":
    unittest.main()

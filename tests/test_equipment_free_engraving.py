"""安装装备界面"免费获得装备刻印兜底"的接线测试（ok_tasks/utils.py: handle_equipment）。

免费获得（战利品/事件奖励）没有商店刻印角标，必须用详情页描述中的刻印图标
兜底判断候选是否自带刻印，再由 resolve_candidate_engraving_flag 合并结论。
本文件验证 handle_equipment 这条真实调用链，使用假 task 和假 OCR 文本框，
不依赖游戏画面或截图比对；纯逻辑侧覆盖见 tests/test_equipment_engraving.py。
"""
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

import ok_tasks.utils as utils  # noqa: E402

# 测试用刻印关键词配置
ENGRAVING_KEYWORDS = ["己方主动生成5张", "己方主动使敌方的行动计数增加"]
TARGET_CROWN = "精灵王的金冠"
# 🎆 是刻印图标被 OCR 识别出来的字符，该描述自带的是非目标刻印
FREE_CROWN_DESC = "🎆回合开始时，对持有护盾的随机敌人造成固定伤暴击率+6%害100%"
# 不带动画图标的普通描述："真的没有刻印"的候选（可走继承换名）
NO_ENGRAVING_DESC = "暴击率+6%"
# 槽位已有的最高优先级刻印（排名第 1），用于验证"继承后描述不被覆盖"
TOP_ENGRAVING_DESC = "己方主动生成5张卡牌时，获得1点能量"
# 候选自带的更优目标刻印（排名第 1），用于验证候选描述会覆盖槽位描述
BETTER_ENGRAVING_DESC = "己方主动生成5张卡牌时，获得2点能量"


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


class _LevelTag:
    """leveltag 模板匹配结果替身。"""

    def __init__(self, center_x, center_y, width=40, height=20, confidence=0.9):
        self.x = int(center_x - width / 2)
        self.y = int(center_y - height / 2)
        self.width = width
        self.height = height
        self.confidence = confidence


def _box_at(name, rel_x, rel_y, width=40, height=20):
    return _Box(name, rel_x * 1600, rel_y * 900, width, height)


def _installation_page_texts(extra=()):
    """安装装备页面的关键文本位置（按 1600x900 归一化坐标模拟）。"""
    return [
        _box_at("装备", 0.499, 0.126, width=60, height=24),
        _box_at("请选择主战员。", 0.921, 0.135, width=220, height=24),
        # 底部同时存在「提炼」和「确认」，才不会被"安装装备界面只有提炼
        # 按钮，直接点击提炼"的早退分支拦截，从而真正走到刻印判定。
        _box_at("提炼", 0.700, 0.960, width=60, height=22),
        _box_at("确认", 0.930, 0.960, width=60, height=22),
        *extra,
    ]


def _purchase_page_texts():
    """商店购买装备页面：底部有「取消」「购买」和价格数字，右上角有当前信用点。"""
    return _installation_page_texts(
        extra=(
            _box_at("取消", 0.420, 0.940, width=60, height=22),
            _box_at("购买", 0.600, 0.940, width=60, height=22),
            _box_at("96", 0.760, 0.940, width=30, height=20),
            _box_at("805", 0.794, 0.054, width=40, height=22),
        )
    )


# 安装装备页面识别到的三个 leveltag
LEVEL_TAGS = [
    _LevelTag(0.6297 * 1600, 0.3222 * 900),
    _LevelTag(0.6297 * 1600, 0.5444 * 900),
    _LevelTag(0.6297 * 1600, 0.7667 * 900),
]


def _blank_member_status(slot_names=None, slot_descriptions=None):
    return {
        "equipment": {
            "names": list(slot_names or ["", "", ""]),
            "descriptions": list(slot_descriptions or ["", "", ""]),
            "qualities": ["", "", ""],
        },
        "deck": {},
    }


class _FakeTask:
    """只还原 handle_equipment 依赖的最小接口。"""

    def __init__(
        self,
        candidate,
        config=None,
        member_status=None,
        all_texts=None,
        node_status=None,
    ):
        self.width = 1600
        self.height = 900
        self.config = dict(config or {})
        self.default_config = {"刷存档主战员": "海德玛丽"}
        self.node_status = dict(node_status or {})
        self.member_status = member_status or _blank_member_status()
        self._candidate = candidate
        self.all_texts = list(all_texts or _installation_page_texts())
        self.logs = []
        self.clicked_boxes = []
        self.clicks = []
        self.moves = []

    def log_info(self, message):
        self.logs.append(message)

    def click_box(self, box):
        self.clicked_boxes.append(box)

    def click(self, x, y):
        self.clicks.append((x, y))

    def move_relative(self, x, y):
        self.moves.append((x, y))

    def sleep(self, _seconds):
        pass

    def box_of_screen(self, *_args):
        return None

    def find_feature(self, feature_name=None, box=None, **_kwargs):
        return []

    def find_one(self, feature_name=None, box=None, **_kwargs):
        return None

    def feature_exists(self, _feature_name):
        return True

    def clicked_names(self):
        return [box.name for box in self.clicked_boxes]


def _engraving_config():
    return {
        "刷装备刻印": True,
        "装备刻印优先级": list(ENGRAVING_KEYWORDS),
        "刷装备刻印信用点下限": 150,
        "刻印专业模式": True,
        "装备2号位优先级": [TARGET_CROWN],
    }


def _crown_candidate(description, ocr_name=TARGET_CROWN):
    """精灵王的金冠候选：命中"装备2号位优先级"，也就是命中目标名。"""
    return {
        "ocr_name": ocr_name,
        "name": TARGET_CROWN,
        "description": description,
        "slot": 1,
        "priority": [TARGET_CROWN],
        "rank": 0,
        "quality": "史诗",
    }


def _run_handle_equipment(task):
    """跑真实的 handle_equipment：只把跨模块依赖（OCR/特征匹配）替换成假数据。"""
    with (
        mock.patch.object(utils, "_equipment_info", return_value=task._candidate),
        mock.patch.object(utils, "_find_member_level_tags", return_value=list(LEVEL_TAGS)),
        mock.patch.object(utils, "_find_target_member_index", return_value=1),
        mock.patch.object(
            utils, "_member_equipment_qualities", return_value=[None, None, None]
        ),
    ):
        return utils.handle_equipment(task)


class TestFreeEquipmentEngravingFallback(unittest.TestCase):
    """免费获得装备的刻印图标兜底，必须真的接到 handle_equipment 的判定入口。"""

    def test_free_equipment_with_engraving_icon_is_rejected(self):
        """免费获得的精灵王的金冠描述里有刻印图标，必须拒绝。"""
        task = _FakeTask(
            candidate=_crown_candidate(FREE_CROWN_DESC, ocr_name="精灵王的金冠🎆"),
            config=_engraving_config(),
        )

        self.assertTrue(_run_handle_equipment(task))

        self.assertTrue(
            any("检测到刻印图标" in log for log in task.logs), task.logs
        )
        self.assertTrue(
            any("角标信息=None" in log for log in task.logs), task.logs
        )
        self.assertTrue(
            any("非目标刻印" in log for log in task.logs), task.logs
        )
        # 不能安装给目标主战员
        self.assertFalse(any("优于当前装备" in log for log in task.logs), task.logs)
        self.assertFalse(
            any("安装给刷存档主战员" in log for log in task.logs), task.logs
        )
        self.assertEqual([], task.moves)
        self.assertEqual(["", "", ""], task.member_status["equipment"]["names"])
        # 刻印专业模式：不分配就点提炼
        self.assertEqual(["提炼"], task.clicked_names())

    def test_free_equipment_without_icon_still_inherits(self):
        """描述里没有图标只能算"未知"，仍按"命中目标名且不带刻印"安全换名。"""
        task = _FakeTask(
            candidate=_crown_candidate(NO_ENGRAVING_DESC),
            config=_engraving_config(),
            member_status=_blank_member_status(
                slot_names=["", "旧装备", ""],
                slot_descriptions=["", TOP_ENGRAVING_DESC, ""],
            ),
        )

        self.assertFalse(_run_handle_equipment(task))

        self.assertTrue(
            any("通过继承机制安全替换" in log for log in task.logs), task.logs
        )
        self.assertTrue(
            any("安装给刷存档主战员" in log for log in task.logs), task.logs
        )
        # 槽位原有的好刻印必须被继承保留，不能被候选描述覆盖
        self.assertEqual(
            TOP_ENGRAVING_DESC,
            task.member_status["equipment"]["descriptions"][1],
        )
        self.assertEqual(TARGET_CROWN, task.member_status["equipment"]["names"][1])
        self.assertTrue(
            any("槽位描述继续记录为" in log for log in task.logs), task.logs
        )
        self.assertEqual(1, len(task.moves))
        self.assertEqual([], task.clicked_names())

    def test_shop_purchase_badge_false_but_icon_is_rejected(self):
        """商店角标漏检（False）时，描述里的刻印图标仍要兜底拒绝并取消购买。"""
        task = _FakeTask(
            candidate=_crown_candidate(FREE_CROWN_DESC, ocr_name="精灵王的金冠🎆"),
            config=_engraving_config(),
            all_texts=_purchase_page_texts(),
            node_status={"shop_opened_has_engraving": False},
        )

        self.assertTrue(_run_handle_equipment(task))

        self.assertTrue(
            any("检测到刻印图标" in log for log in task.logs), task.logs
        )
        self.assertTrue(
            any("角标信息=False" in log for log in task.logs), task.logs
        )
        self.assertTrue(any("放弃购买" in log for log in task.logs), task.logs)
        self.assertEqual(["取消"], task.clicked_names())
        # 被拒商品要记入本轮黑名单，避免下次进商店重新点开
        self.assertEqual(
            {utils._normalize_equipment_name(TARGET_CROWN)},
            task.node_status["shop_engraving_rejected_names"],
        )

    def test_shop_purchase_badge_true_with_target_engraving_is_bought(self):
        """角标确认自带刻印且描述命中更优目标刻印：正常购买，槽位描述按候选记录。"""
        task = _FakeTask(
            candidate=_crown_candidate(BETTER_ENGRAVING_DESC),
            config=_engraving_config(),
            all_texts=_purchase_page_texts(),
            node_status={"shop_opened_has_engraving": True},
        )

        self.assertTrue(_run_handle_equipment(task))

        # 角标已经确认有刻印，不需要再靠图标兜底，也不应打兜底日志
        self.assertFalse(any("检测到刻印图标" in log for log in task.logs), task.logs)
        self.assertIn("购买", task.clicked_names())
        self.assertIn(TARGET_CROWN, task.member_status["equipment"]["names"])
        self.assertEqual(
            BETTER_ENGRAVING_DESC,
            task.member_status["equipment"]["descriptions"][1],
        )

    def test_shop_purchase_badge_false_without_icon_keeps_inheritance(self):
        """角标确认没有刻印 + 描述也无图标：保持"可继承"口径，命中目标名照常购买。"""
        task = _FakeTask(
            candidate=_crown_candidate(NO_ENGRAVING_DESC),
            config=_engraving_config(),
            all_texts=_purchase_page_texts(),
            member_status=_blank_member_status(
                slot_names=["", "旧装备", ""],
                slot_descriptions=["", TOP_ENGRAVING_DESC, ""],
            ),
            node_status={"shop_opened_has_engraving": False},
        )

        self.assertTrue(_run_handle_equipment(task))

        self.assertFalse(any("检测到刻印图标" in log for log in task.logs), task.logs)
        self.assertTrue(
            any("通过继承机制安全替换" in log for log in task.logs), task.logs
        )
        self.assertIn("购买", task.clicked_names())
        # 候选没刻印，槽位原刻印被继承，描述不能换成候选描述
        self.assertEqual(
            TOP_ENGRAVING_DESC,
            task.member_status["equipment"]["descriptions"][1],
        )


if __name__ == "__main__":
    unittest.main()

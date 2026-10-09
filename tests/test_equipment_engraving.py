"""装备刻印判定逻辑的单元测试（ok_tasks/utils_equipment_engraving.py）。

这里重点覆盖一个分支：候选装备**自带的是非目标刻印**。

判定函数只能靠"描述文字里是否命中目标刻印关键词"来识别刻印，而候选自带
非目标刻印时，描述里同样不含目标关键词，因此会和"真的没有刻印"一样返回
None。判定时必须区分这两种情况，否则只要装备名命中目标名就直接购买，会出现
"名称对、刻印不对"——买回来的装备会用自带的差刻印覆盖掉槽位已有的好刻印。

为了区分，判定函数接受一个 candidate_has_engraving 参数（由商店刻印角标
检测得到）：

- True  = 确定自带刻印（有角标）。此时若描述不含任何目标刻印关键词，
          说明自带的是非目标刻印，必须拒绝，绝不能按"继承"处理；
- False = 确认没有刻印。命中目标名时可以安全换名（覆盖会继承槽位刻印）；
- None  = 无法判断（角标检测未生效/回退）。保持宽松行为，避免误拒。

同一个模块里还有"刻印专业模式"的出口规则 should_refine_free_equipment：开启
专业模式后，免费获得（非商店购买）的装备被判定为"不分配"时不再转交给其他
主战员，而是直接提炼（分解）成信用点。下面的测试一并钉住它的各条前置条件。

另外还钉住“安装后槽位描述怎么记”（resolve_slot_description_after_install）：
候选不带刻印时覆盖会继承槽位原刻印，脚本侧的描述必须跟着保留，否则后续
判定会把该槽位误当成“没有刻印”，买回更差的刻印把继承来的刻印顶掉。
"""
import pathlib
import sys
import unittest

# 与 tests/test_engraving_badge.py 保持一致：ok_tasks 内部使用平级导入，
# 需要把 ok_tasks 目录也加入 sys.path 才能正常 import。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))

from utils_equipment_engraving import (  # noqa: E402
    best_engraving_rank,
    has_engraving_icon_mark,
    has_target_engraving,
    is_pro_mode_escape_check_point,
    is_slot_completed,
    matches_target_name,
    name_only_candidate_slot,
    resolve_candidate_engraving_flag,
    resolve_slot_description_after_install,
    should_assign_to_target_member,
    should_refine_free_equipment,
    slot_allows_any_name,
    slots_all_completed,
)

# 测试使用与 ChaosMode 配置一致的关键词和目标名
ENGRAVING_KEYWORDS = ["己方主动生成5张", "己方主动使敌方的行动计数增加"]
TARGET_NAMES = ["精灵王的金冠"]

# "精灵王的金冠" 的真实描述：自带的是**非目标**刻印
OTHER_ENGRAVING_DESC = (
    "暴击率+6%墓地中的卡牌数量大于等于15张时，自身攻击卡牌暴击伤害+15%（不可叠加)"
)
# 命中刻印优先级列表第 1 项（下标 0）
TOP_ENGRAVING_DESC = "回合结束时，己方主动生成5张牌"
# 命中刻印优先级列表第 2 项（下标 1）
SECOND_ENGRAVING_DESC = "己方主动使敌方的行动计数增加1点"


class TestBestEngravingRank(unittest.TestCase):
    """刻印关键词命中排名的计算。"""

    def test_returns_none_when_nothing_matched(self):
        """描述里没有任何目标刻印关键词时返回 None。"""
        self.assertIsNone(
            best_engraving_rank(OTHER_ENGRAVING_DESC, ENGRAVING_KEYWORDS)
        )

    def test_returns_smallest_index_when_multiple_matched(self):
        """同时命中多项时取排名最靠前（下标最小）的那一项。"""
        desc = f"{SECOND_ENGRAVING_DESC}；另外{TOP_ENGRAVING_DESC}"
        self.assertEqual(0, best_engraving_rank(desc, ENGRAVING_KEYWORDS))

    def test_returns_none_for_empty_inputs(self):
        """描述或关键词列表为空时返回 None。"""
        self.assertIsNone(best_engraving_rank("", ENGRAVING_KEYWORDS))
        self.assertIsNone(best_engraving_rank(TOP_ENGRAVING_DESC, []))

    def test_has_target_engraving_mirrors_rank(self):
        """has_target_engraving 等价于"rank 不为 None"。"""
        self.assertTrue(has_target_engraving(TOP_ENGRAVING_DESC, ENGRAVING_KEYWORDS))
        self.assertFalse(
            has_target_engraving(OTHER_ENGRAVING_DESC, ENGRAVING_KEYWORDS)
        )


class TestMatchesTargetName(unittest.TestCase):
    """目标装备名匹配。"""

    def test_matches_by_bidirectional_containment(self):
        """双向包含即算命中。"""
        self.assertTrue(matches_target_name("精灵王的金冠", TARGET_NAMES))
        self.assertTrue(
            matches_target_name("精灵王的金冠🎆", TARGET_NAMES)
        )

    def test_returns_false_for_non_target_name(self):
        """不在目标名列表中的装备名不命中。"""
        self.assertFalse(matches_target_name("突击护手", TARGET_NAMES))

    def test_returns_false_for_empty_inputs(self):
        """名称或目标列表为空时不命中。"""
        self.assertFalse(matches_target_name("", TARGET_NAMES))
        self.assertFalse(matches_target_name("精灵王的金冠", []))


class TestIsSlotCompleted(unittest.TestCase):
    """槽位"已完成"判定：目标名 + 任意一项目标刻印。"""

    def test_completed_when_target_name_and_top_engraving(self):
        """目标名 + 最优刻印 = 已完成。"""
        self.assertTrue(
            is_slot_completed(
                "精灵王的金冠", TOP_ENGRAVING_DESC, TARGET_NAMES, ENGRAVING_KEYWORDS
            )
        )

    def test_completed_when_engraving_is_not_top(self):
        """刻印只要命中列表中的任意一项就算完成，不再追求排名第一。"""
        self.assertTrue(
            is_slot_completed(
                "精灵王的金冠",
                SECOND_ENGRAVING_DESC,
                TARGET_NAMES,
                ENGRAVING_KEYWORDS,
            )
        )

    def test_not_completed_when_name_is_not_target(self):
        """名字不命中目标名时不算完成。"""
        self.assertFalse(
            is_slot_completed(
                "突击护手", TOP_ENGRAVING_DESC, TARGET_NAMES, ENGRAVING_KEYWORDS
            )
        )


class TestShouldAssignToTargetMember(unittest.TestCase):
    """候选装备是否分配给目标主战员的核心判定。"""

    def _decide(self, current_name="", current_desc="", candidate_name="",
                candidate_desc="", candidate_has_engraving=None):
        return should_assign_to_target_member(
            current_name,
            current_desc,
            candidate_name,
            candidate_desc,
            TARGET_NAMES,
            ENGRAVING_KEYWORDS,
            candidate_has_engraving,
        )

    # ── 重点分支：自带非目标刻印的候选 ──────────────────────────────

    def test_bagged_candidate_with_non_target_engraving_is_rejected(self):
        """有刻印角标但描述不含目标刻印词 = 自带非目标刻印，必须拒绝。

        若误把它当成"命中目标名且不带任何刻印"，就会买回一件名称正确、
        但会把槽位好刻印覆盖掉的装备。
        """
        should_install, reason = self._decide(
            candidate_name="精灵王的金冠",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")
        self.assertIn("非目标刻印", reason)

    def test_bagged_non_target_engraving_rejected_even_when_slot_has_better(self):
        """槽位已有最优刻印时，带非目标刻印的候选同样必须拒绝。"""
        should_install, reason = self._decide(
            current_name="精灵王的金冠",
            current_desc=TOP_ENGRAVING_DESC,
            candidate_name="精灵王的金冠",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")

    def test_bagged_non_target_engraving_rejected_when_name_not_matched(self):
        """名字不命中时也拒绝（既无目标名也无目标刻印）。"""
        should_install, _ = self._decide(
            candidate_name="突击护手",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install)

    # ── 兼容性：角标信息缺失/为假时的处理 ───────────────────────────

    def test_unknown_badge_falls_back_to_legacy_behaviour(self):
        """角标检测未生效（None）时维持旧的宽松行为，避免误拒。"""
        should_install, reason = self._decide(
            candidate_name="精灵王的金冠",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=None,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")
        self.assertIn("继承机制", reason)

    def test_omitting_argument_defaults_to_unknown(self):
        """candidate_has_engraving 是可选参数，默认等同于 None。"""
        with_default = should_assign_to_target_member(
            "",
            "",
            "精灵王的金冠",
            OTHER_ENGRAVING_DESC,
            TARGET_NAMES,
            ENGRAVING_KEYWORDS,
        )
        self.assertEqual(
            self._decide(
                candidate_name="精灵王的金冠",
                candidate_desc=OTHER_ENGRAVING_DESC,
                candidate_has_engraving=None,
            ),
            with_default,
        )

    def test_confirmed_no_engraving_is_safe_replacement(self):
        """确认没有刻印（False）且命中目标名时，可按继承机制安全换名。"""
        should_install, reason = self._decide(
            candidate_name="精灵王的金冠",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=False,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")
        self.assertIn("继承机制", reason)

    # ── 刻印优先的其余分支 ─────────────────────────────────────────

    def test_better_engraving_wins_even_if_name_not_matched(self):
        """候选刻印更优时优先占刻印，哪怕名字不对。"""
        should_install, reason = self._decide(
            candidate_name="突击护手",
            candidate_desc=TOP_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")

    def test_target_engraving_with_target_name_is_direct_hit(self):
        """槽位无刻印、候选同时命中目标刻印与目标名，一步到位。"""
        should_install, reason = self._decide(
            candidate_name="精灵王的金冠",
            candidate_desc=TOP_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")

    def test_worse_engraving_is_rejected(self):
        """候选自带更差的刻印会造成降级，拒绝。"""
        # 槽位用非目标名，避免先命中"已完成"分支（已完成要求目标名+任意目标刻印）
        should_install, reason = self._decide(
            current_name="突击护手",
            current_desc=TOP_ENGRAVING_DESC,
            candidate_name="精灵王的金冠",
            candidate_desc=SECOND_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")
        self.assertIn("降级", reason)

    def test_equal_engraving_with_target_name_is_accepted(self):
        """刻印等级不变、名字变对，属于安全进展。"""
        should_install, reason = self._decide(
            current_name="突击护手",
            current_desc=TOP_ENGRAVING_DESC,
            candidate_name="精灵王的金冠",
            candidate_desc=TOP_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")

    def test_completed_slot_rejects_any_candidate(self):
        """槽位已完成时，任何候选都不再分配。"""
        should_install, reason = self._decide(
            current_name="精灵王的金冠",
            current_desc=TOP_ENGRAVING_DESC,
            candidate_name="精灵王的金冠",
            candidate_desc=SECOND_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")
        self.assertIn("已完成", reason)

    # ── 完成口径：刻印命中任意一项即可 ─────────────────────────────

    def test_completed_slot_rejects_better_engraving_candidate(self):
        """名字对 + 排名第二的刻印 = 已完成，不再为了"排名第一"更换刻印。

        已完成槽位不能再被更高排名的刻印候选替换，否则装备名也会跟着变错。
        """
        should_install, reason = self._decide(
            current_name="精灵王的金冠",
            current_desc=SECOND_ENGRAVING_DESC,
            candidate_name="侦察兵的军靴🎆",
            candidate_desc=TOP_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")
        self.assertIn("已完成", reason)

    def test_unfinished_slot_still_chases_better_engraving(self):
        """名字还是不对时，刻印继续升级：排名更靠前的候选照买（哪怕名字也不对）。"""
        should_install, reason = self._decide(
            current_name="侦察兵的军靴🎆",
            current_desc=SECOND_ENGRAVING_DESC,
            candidate_name="幸存者的兜帽🎆",
            candidate_desc=TOP_ENGRAVING_DESC,
            candidate_has_engraving=True,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")
        self.assertIn("排名第1", reason)

    def test_unfinished_slot_accepts_target_name_after_engraving_upgrade(self):
        """刻印升级后名字仍不对：目标名 + 无刻印的候选照样补名（走继承，不降级）。"""
        should_install, reason = self._decide(
            current_name="幸存者的兜帽🎆",
            current_desc=TOP_ENGRAVING_DESC,
            candidate_name="精灵王的金冠",
            candidate_desc=OTHER_ENGRAVING_DESC,
            candidate_has_engraving=False,
        )
        self.assertTrue(should_install, f"应允许，实际原因={reason}")
        self.assertIn("继承机制", reason)


class TestShouldRefineFreeEquipment(unittest.TestCase):
    """刻印专业模式：免费获得的装备是否直接提炼成信用点。"""

    def _decide(
        self,
        is_purchase_page=False,
        engraving_mode=True,
        pro_mode=True,
        slot_evaluated=True,
        assign_to_target_member=False,
    ):
        return should_refine_free_equipment(
            is_purchase_page=is_purchase_page,
            engraving_mode=engraving_mode,
            pro_mode=pro_mode,
            slot_evaluated=slot_evaluated,
            assign_to_target_member=assign_to_target_member,
        )

    def test_free_equipment_without_progress_is_refined(self):
        """专业模式开启、免费获得、判定为不分配 -> 提炼换信用点。"""
        self.assertTrue(self._decide())

    def test_pro_mode_off_keeps_legacy_behaviour(self):
        """专业模式关闭时保持原行为（转交给其他主战员），不提炼。"""
        self.assertFalse(self._decide(pro_mode=False))

    def test_engraving_mode_off_is_never_refined(self):
        """未开启"刷装备刻印"时专业模式不生效（专业模式依附于刻印模式）。"""
        self.assertFalse(self._decide(engraving_mode=False))

    def test_purchase_page_is_never_refined(self):
        """商店购买（购买页面）的装备价格/刻印不合适时走"取消"，不提炼。"""
        self.assertFalse(self._decide(is_purchase_page=True))

    def test_unevaluated_slot_falls_back(self):
        """没完成刻印判定（找不到可比较的主战员）时保守回退到原逻辑。"""
        self.assertFalse(self._decide(slot_evaluated=False))

    def test_assignable_equipment_is_not_refined(self):
        """能推进刻印进展的装备要装上，绝不能提炼掉。"""
        self.assertFalse(self._decide(assign_to_target_member=True))

    def test_arguments_are_keyword_only(self):
        """参数必须按关键字传，写成位置参数直接报错，避免调用方传错顺序。"""
        with self.assertRaises(TypeError):
            should_refine_free_equipment(False, True, True, True, False)


class TestSlotsAllCompleted(unittest.TestCase):
    """刻印专业模式的达标判定：三个装备位都要"目标名+任意一项目标刻印"。"""

    def _check(self, current_states):
        return slots_all_completed(
            current_states,
            [TARGET_NAMES, TARGET_NAMES, TARGET_NAMES],
            ENGRAVING_KEYWORDS,
        )

    def test_all_slots_completed(self):
        completed, missing = self._check([
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
        ])
        self.assertTrue(completed)
        self.assertEqual([], missing)

    def test_empty_slot_is_reported_as_missing_name(self):
        """空装备位（名字不在目标名列表）要能定位到具体是几号位。"""
        completed, missing = self._check([
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ("", ""),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
        ])
        self.assertFalse(completed)
        self.assertEqual(1, len(missing))
        self.assertIn("2号位", missing[0])
        self.assertIn("目标名列表", missing[0])

    def test_second_priority_engraving_counts_as_completed(self):
        """拿到排名第二的刻印同样算达标（不再要求排名第一）。"""
        completed, missing = self._check([
            ("精灵王的金冠", SECOND_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
        ])
        self.assertTrue(completed)
        self.assertEqual([], missing)

    def test_non_target_engraving_is_reported(self):
        """自带非目标刻印（描述里没有目标关键词）同样算不达标。"""
        completed, missing = self._check([
            ("精灵王的金冠", OTHER_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ("精灵王的金冠", TOP_ENGRAVING_DESC),
        ])
        self.assertFalse(completed)
        self.assertIn("没有命中任何目标刻印", missing[0])

    def test_shorter_state_list_is_treated_as_empty_slots(self):
        """状态列表缺项时按空装备位处理，不能抛异常影响逃脱判定。"""
        completed, missing = self._check([])
        self.assertFalse(completed)
        self.assertEqual(3, len(missing))


class TestIsProModeEscapeCheckPoint(unittest.TestCase):
    """刻印专业模式的逃脱检查点：第二个Boss房间（第二层关底Boss）之前。"""

    def test_map2_before_final_boss(self):
        """第二层、且当前路线页面只剩下最终Boss节点时，才在进Boss前检查。"""
        self.assertTrue(is_pro_mode_escape_check_point(1, True))

    def test_map1_final_boss_is_never_a_check_point(self):
        """第一层（第一个Boss）之前不检查，即使当前已经是最终Boss节点。"""
        self.assertFalse(is_pro_mode_escape_check_point(0, True))

    def test_map3_final_boss_is_never_a_check_point(self):
        """第三层及以后的Boss前也不检查（逃脱只针对第二个Boss）。"""
        self.assertFalse(is_pro_mode_escape_check_point(2, True))
        self.assertFalse(is_pro_mode_escape_check_point(9, True))

    def test_map2_before_reaching_boss_is_not_a_check_point(self):
        """地图二上还有普通节点可走时不检查，要等节点（含商店）都走完。"""
        self.assertFalse(is_pro_mode_escape_check_point(1, False))


class TestSlotDescriptionAfterInstall(unittest.TestCase):
    """安装后槽位描述的状态维护（继承来的刻印不能在脚本侧被丢掉）。

    "候选不带刻印 -> 覆盖后继承槽位原刻印"是游戏机制（模块头部第 5 条），
    也正是 should_assign_to_target_member"命中目标名且不带刻印 -> 通过继承
    机制安全替换"这条判定的前提。判定按这个前提放行，槽位描述就必须跟着
    保留原刻印文字；直接换成候选描述会让槽位在脚本侧变成"没有刻印"，
    随后把一件自带更差刻印的候选当成"更优刻印"买下，专业模式的逃脱检查
    也会误判该槽位未达标。
    """

    def _resolve(self, current_desc, candidate_desc, candidate_has_engraving=None):
        return resolve_slot_description_after_install(
            current_desc,
            candidate_desc,
            ENGRAVING_KEYWORDS,
            candidate_has_engraving,
        )

    def test_confirmed_no_engraving_inherits_previous_description(self):
        """角标确认候选没有刻印（False）：槽位描述保留原刻印文字。"""
        self.assertEqual(
            TOP_ENGRAVING_DESC,
            self._resolve(TOP_ENGRAVING_DESC, "暴击率+6%", False),
        )

    def test_no_badge_info_still_keeps_previous_description(self):
        """拿不到角标信息（白拿装备）时不命中目标刻印关键词：按继承处理。"""
        self.assertEqual(
            SECOND_ENGRAVING_DESC,
            self._resolve(SECOND_ENGRAVING_DESC, "击败敌人时，固定护盾60%"),
        )

    def test_no_badge_info_with_target_engraving_overwrites_description(self):
        """白拿装备时候选自己带目标刻印：槽位描述必须换成候选带来的刻印。"""
        self.assertEqual(
            TOP_ENGRAVING_DESC,
            self._resolve(SECOND_ENGRAVING_DESC, TOP_ENGRAVING_DESC),
        )

    def test_non_target_engraving_candidate_overwrites_description(self):
        """候选有角标、自带的是非目标刻印：原刻印会被顶掉，用候选描述覆盖。"""
        self.assertEqual(
            OTHER_ENGRAVING_DESC,
            self._resolve(TOP_ENGRAVING_DESC, OTHER_ENGRAVING_DESC, True),
        )

    def test_empty_slot_keeps_candidate_description(self):
        """空槽位（本来就没有刻印可继承）时仍记录候选描述，不写成空串。"""
        self.assertEqual("暴击率+6%", self._resolve("", "暴击率+6%", False))

    def test_missing_keywords_argument_is_tolerated(self):
        """省略 engraving_keywords 时按"没有目标刻印"处理，不能抛异常。"""
        self.assertEqual(
            "暴击率+6%",
            resolve_slot_description_after_install("", "暴击率+6%"),
        )

    def test_inherited_slot_is_completed_and_rejects_worse_candidate(self):
        """继承后槽位直接达标：再出现自带更差刻印的候选必须被拒绝。

        这一条同时覆盖槽位描述维护：若安装后把槽位描述换成候选描述（没有刻印），
        更差的刻印会被当成更优刻印买下，游戏里反而把继承来的好刻印顶掉。
        """
        inherited_desc = self._resolve(TOP_ENGRAVING_DESC, "暴击率+6%", False)
        self.assertTrue(
            is_slot_completed(
                "精灵王的金冠", inherited_desc, TARGET_NAMES, ENGRAVING_KEYWORDS
            ),
            "继承来的刻印应当被识别为排名第一，该槽位直接达标",
        )

        should_install, reason = should_assign_to_target_member(
            "精灵王的金冠",
            inherited_desc,
            "精灵王的金冠",
            SECOND_ENGRAVING_DESC,
            TARGET_NAMES,
            ENGRAVING_KEYWORDS,
            True,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")

        # 若槽位描述被换成候选描述，同一件候选会被判成更优刻印买下；
        # 这里显式对比，避免以后又把槽位描述覆盖回候选描述。
        stale_install, stale_reason = should_assign_to_target_member(
            "精灵王的金冠",
            "暴击率+6%",
            "精灵王的金冠",
            SECOND_ENGRAVING_DESC,
            TARGET_NAMES,
            ENGRAVING_KEYWORDS,
            True,
        )
        self.assertTrue(stale_install, f"修正前应当会买下，实际原因={stale_reason}")


class TestCandidateEngravingFlag(unittest.TestCase):
    """候选"是否自带刻印"的结论合并（角标 + 详情页刻印图标兜底）。

    免费获得的装备（战利品/事件奖励）拿不到商店角标，不能按"不带任何刻印 ->
    可继承"放行；详情页描述里的刻印图标（OCR 成 🎆）命中时按"自带刻印"处理，
    交由判定函数拒绝。
    """

    # 免费获得精灵王的金冠时，描述里的刻印图标被 OCR 识别为 🎆
    FREE_CROWN_DESC = "🎆回合开始时，对持有护盾的随机敌人造成固定伤暴击率+6%害100%"

    def test_badge_true_wins(self):
        """角标确认自带刻印（True）时直接采信。"""
        self.assertTrue(resolve_candidate_engraving_flag(True, "暴击率+6%"))

    def test_badge_false_with_icon_is_promoted(self):
        """角标漏检（False）但描述里有刻印图标：提升为 True。"""
        self.assertTrue(
            resolve_candidate_engraving_flag(False, self.FREE_CROWN_DESC)
        )

    def test_no_badge_info_with_icon_is_detected(self):
        """免费获得装备（角标未知 None）时，靠图标识别出候选自带刻印。"""
        self.assertTrue(
            resolve_candidate_engraving_flag(None, self.FREE_CROWN_DESC)
        )

    def test_confirmed_no_engraving_keeps_false(self):
        """角标确认没有刻印、描述里也没有图标：保持 False（可走继承换名）。"""
        self.assertFalse(
            resolve_candidate_engraving_flag(False, "暴击率+6%")
        )

    def test_no_evidence_keeps_unknown(self):
        """没有任何证据时保持 None（未知），交给判定函数沿用宽松口径。"""
        self.assertIsNone(
            resolve_candidate_engraving_flag(None, "暴击率+6%")
        )

    def test_icon_mark_detection(self):
        """图标检测只认正向证据，空描述不算命中。"""
        self.assertTrue(has_engraving_icon_mark(self.FREE_CROWN_DESC))
        self.assertFalse(has_engraving_icon_mark("暴击率+6%"))
        self.assertFalse(has_engraving_icon_mark(""))
        self.assertFalse(has_engraving_icon_mark(None))

    def test_free_equipment_with_non_target_engraving_is_rejected(self):
        """免费获得的精灵王的金冠自带非目标刻印，必须被拒绝。"""
        flag = resolve_candidate_engraving_flag(None, self.FREE_CROWN_DESC)
        should_install, reason = should_assign_to_target_member(
            "",
            "",
            "精灵王的金冠",
            self.FREE_CROWN_DESC,
            TARGET_NAMES,
            ENGRAVING_KEYWORDS,
            flag,
        )
        self.assertFalse(should_install, f"应拒绝，实际原因={reason}")
        self.assertIn("非目标刻印", reason)

    def test_free_equipment_with_non_target_engraving_not_recorded_as_inherit(self):
        """候选自带刻印时，槽位描述按候选描述记录，不能当成"继承了原刻印"。"""
        self.assertEqual(
            self.FREE_CROWN_DESC,
            resolve_slot_description_after_install(
                TOP_ENGRAVING_DESC,
                self.FREE_CROWN_DESC,
                ENGRAVING_KEYWORDS,
                None,
            ),
        )



class TestNameOnlyCandidateSlot(unittest.TestCase):
    """商店角标预筛选的例外：不带刻印的"目标名装备"什么时候值得点开。

    2号位"刻印已经对了、只差目标名"，而能补上名字的那件装备恰恰不带刻印
    （装上会继承槽位的刻印），在货架上就是没有角标的，因此预筛选必须给这种
    候选放行，否则槽位永远补不上名字。判定见 name_only_candidate_slot。
    """

    def _slot(self, name="", desc="", targets=None):
        return (name, desc, list(TARGET_NAMES if targets is None else targets))

    def test_top_engraving_with_wrong_name_allows_candidate(self):
        """刻印已达标（排名第一）+ 名字不对 -> 放行，返回该槽位下标。"""
        slots = [self._slot(), self._slot("侦察兵的军靴🎆", TOP_ENGRAVING_DESC), self._slot()]
        self.assertEqual(
            1, name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_second_priority_engraving_still_allows_candidate(self):
        """只拿到第二优先级的刻印同样有刻印可继承，换名不会降级 -> 放行。"""
        slots = [self._slot("侦察兵的军靴🎆", SECOND_ENGRAVING_DESC)]
        self.assertEqual(
            0, name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_empty_slot_does_not_allow_candidate(self):
        """空槽没有刻印可继承：买下来只是把名字填上，等于白花信用点。"""
        slots = [self._slot(), self._slot(), self._slot()]
        self.assertIsNone(
            name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_non_target_engraving_does_not_allow_candidate(self):
        """槽位自带的是非目标刻印（描述里没有目标关键词）也不放行。"""
        slots = [self._slot("侦察兵的军靴🎆", OTHER_ENGRAVING_DESC)]
        self.assertIsNone(
            name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_slot_whose_name_already_matches_is_ignored(self):
        """名字已经对了的槽位不需要再补名字（候选自身不带刻印，没有别的进展）。"""
        slots = [self._slot("精灵王的金冠🎆", TOP_ENGRAVING_DESC)]
        self.assertIsNone(
            name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_candidate_not_matching_target_name_is_ignored(self):
        """候选名字不命中目标名列表时不放行（否则又回到"逐一进详情页"）。"""
        slots = [self._slot("侦察兵的军靴🎆", TOP_ENGRAVING_DESC)]
        self.assertIsNone(
            name_only_candidate_slot("魔力蓝宝石", slots, ENGRAVING_KEYWORDS)
        )

    def test_skips_completed_slot_and_returns_next_one(self):
        """名字已经对的槽位跳过，继续往后找真正需要补名字的那个槽位。"""
        targets = ["精灵王的金冠"]
        slots = [
            self._slot("精灵王的金冠🎆", TOP_ENGRAVING_DESC, targets),
            self._slot("侦察兵的军靴🎆", TOP_ENGRAVING_DESC, targets),
            self._slot("", TOP_ENGRAVING_DESC, targets),
        ]
        self.assertEqual(
            1, name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )

    def test_missing_engraving_keywords_does_not_allow_candidate(self):
        """没配置"装备刻印优先级"时判定不出"槽位已有刻印"，保守不放行。"""
        slots = [self._slot("侦察兵的军靴🎆", TOP_ENGRAVING_DESC)]
        self.assertIsNone(name_only_candidate_slot("精灵王的金冠", slots, []))


class TestSlotWithoutTargetName(unittest.TestCase):
    """目标名列表留空 = 该装备位"不限装备名，只要刻印对上"。

    装备位优先级留空表示"任意装备都行，只要满足刻印即可"：

    - `slot_allows_any_name` 判定"这一位是不是不限名字"；
    - `is_slot_completed` / `slots_all_completed` 对这类装备位只看刻印；
    - `matches_target_name` 对空列表**仍然返回 False**："不限名字"不能被当成
      "命中目标名"，否则继承换名分支和 name_only_candidate_slot 会把任意候选
      都当成目标名放行。
    """

    def test_any_name_only_when_list_is_empty(self):
        """列表为空（或只有空串）才是不限名字，配了名字走常规判定。"""
        self.assertTrue(slot_allows_any_name([]))
        self.assertTrue(slot_allows_any_name(None))
        self.assertTrue(slot_allows_any_name([""]))
        self.assertTrue(slot_allows_any_name(["", "   "]))
        self.assertFalse(slot_allows_any_name(TARGET_NAMES))
        self.assertFalse(slot_allows_any_name(["精灵王的金冠", ""]))

    def test_any_name_is_not_target_name_hit(self):
        """不限名字不等于"命中目标名"，matches_target_name 保持不变。"""
        self.assertFalse(matches_target_name("任意一件装备", []))
        self.assertFalse(matches_target_name("", []))

    def test_completed_with_any_name_and_target_engraving(self):
        """不限名字时：名字随便，刻印命中目标刻印就算完成。"""
        self.assertTrue(
            is_slot_completed("任意装备", TOP_ENGRAVING_DESC, [], ENGRAVING_KEYWORDS)
        )
        self.assertTrue(
            is_slot_completed(
                "侦察兵的军靴", SECOND_ENGRAVING_DESC, [], ENGRAVING_KEYWORDS
            )
        )

    def test_not_completed_when_engraving_not_target(self):
        """不限名字时仍然要求刻印命中：自带非目标刻印不算完成。"""
        self.assertFalse(
            is_slot_completed("任意装备", OTHER_ENGRAVING_DESC, [], ENGRAVING_KEYWORDS)
        )

    def test_empty_slot_is_not_completed(self):
        """空装备位（名字与描述都空）不算完成，不会被当成"任意名字"。"""
        self.assertFalse(is_slot_completed("", "", [], ENGRAVING_KEYWORDS))

    def test_any_name_slot_still_completed_when_name_list_configured(self):
        """配了目标名的装备位不受影响：名字不对就不算完成。"""
        self.assertFalse(
            is_slot_completed(
                "侦察兵的军靴", TOP_ENGRAVING_DESC, TARGET_NAMES, ENGRAVING_KEYWORDS
            )
        )

    def test_slots_all_completed_with_middle_slot_any_name(self):
        """2号位留空时，只要刻印对上就算达标，三个装备位一起判。"""
        completed, missing = slots_all_completed(
            [
                ("精灵王的金冠", TOP_ENGRAVING_DESC),
                ("任意一件装备", SECOND_ENGRAVING_DESC),
                ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ],
            [TARGET_NAMES, [], TARGET_NAMES],
            ENGRAVING_KEYWORDS,
        )
        self.assertTrue(completed)
        self.assertEqual([], missing)

    def test_slots_all_completed_missing_text_for_any_name_slot(self):
        """不限名字的装备位没达标时，说明只提刻印，不再提"目标名列表（没配置）"。"""
        completed, missing = slots_all_completed(
            [
                ("精灵王的金冠", TOP_ENGRAVING_DESC),
                ("任意一件装备", OTHER_ENGRAVING_DESC),
                ("精灵王的金冠", TOP_ENGRAVING_DESC),
            ],
            [TARGET_NAMES, [], TARGET_NAMES],
            ENGRAVING_KEYWORDS,
        )
        self.assertFalse(completed)
        self.assertEqual(1, len(missing))
        self.assertIn("2号位", missing[0])
        self.assertIn("没有命中任何目标刻印", missing[0])
        self.assertNotIn("不在目标名列表", missing[0])

    def test_assign_better_engraving_to_any_name_slot(self):
        """不限名字的装备位：槽位还没刻印时，候选带目标刻印就换上，不看名字。"""
        should_install, reason = should_assign_to_target_member(
            "任意一件装备",
            OTHER_ENGRAVING_DESC,
            "另一件装备",
            TOP_ENGRAVING_DESC,
            [],
            ENGRAVING_KEYWORDS,
        )
        self.assertTrue(should_install, reason)
        self.assertIn("不限装备名", reason)

    def test_any_name_slot_locked_by_any_target_engraving(self):
        """不限名字的装备位：任意一项目标刻印命中就锁定，排名2也会锁住。

        沿用模块头部的既有规则——"刻印排名还不够靠前本身不算继续刷的理由"，
        槽位已达标的判定只看"有没有命中刻印优先级列表里的任意一项"。
        """
        should_install, reason = should_assign_to_target_member(
            "任意一件装备",
            SECOND_ENGRAVING_DESC,
            "更优的装备",
            TOP_ENGRAVING_DESC,
            [],
            ENGRAVING_KEYWORDS,
        )
        self.assertFalse(should_install, reason)
        self.assertIn("已完成", reason)

    def test_any_name_slot_locked_against_same_rank_candidate(self):
        """不限名字的装备位配上同排名候选也不换。"""
        should_install, reason = should_assign_to_target_member(
            "任意一件装备",
            TOP_ENGRAVING_DESC,
            "更好的装备",
            TOP_ENGRAVING_DESC,
            [],
            ENGRAVING_KEYWORDS,
        )
        self.assertFalse(should_install, reason)
        self.assertIn("已完成", reason)

    def test_any_name_slot_rejects_candidate_without_target_engraving(self):
        """不限名字的装备位不接受"没有目标刻印"的候选（名字随便也没用）。"""
        should_install, reason = should_assign_to_target_member(
            "任意一件装备",
            SECOND_ENGRAVING_DESC,
            "另一件装备",
            OTHER_ENGRAVING_DESC,
            [],
            ENGRAVING_KEYWORDS,
        )
        self.assertFalse(should_install, reason)

    def test_name_only_candidate_never_targets_any_name_slot(self):
        """不限名字的装备位不需要"补名字"，name_only_candidate_slot 不会选它。"""
        slots = [("任意一件装备", TOP_ENGRAVING_DESC, [])]
        self.assertIsNone(
            name_only_candidate_slot("精灵王的金冠", slots, ENGRAVING_KEYWORDS)
        )


if __name__ == "__main__":
    unittest.main()

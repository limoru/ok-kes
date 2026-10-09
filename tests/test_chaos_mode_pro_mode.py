"""卡厄思模式"刻印专业模式"配置项的接线测试。

"刻印专业模式"只做一件事：被"刷装备刻印"判定为**不分配给目标主战员**的
"免费获得"装备（非商店购买），不再转交给其他主战员占掉装备位，而是直接
提炼（分解）换信用点。提炼动作本身需要连接游戏验证，这里覆盖配置层接线：

1. `ChaosMode.default_config` 里有"刻印专业模式"布尔项；ok 的配置控件工厂会
   把布尔值渲染成 `LabelAndSwitchButton`（普通开关），这就是界面上唯一的开关；
2. "配置操作"的按钮列表里**不能**出现"刻印专业模式"按钮，避免按钮和开关重复
   切换同一个配置项；
3. 这个开关不显示配置说明文案。

判定规则本身（什么时候该提炼、逃脱检查点怎么算）由 utils_equipment_engraving.py
的 `should_refine_free_equipment` / `slots_all_completed` /
`is_pro_mode_escape_check_point` 负责，测试见 tests/test_equipment_engraving.py。

注意：本文件用 ok 的 TaskTestCase（会 init/destroy 整个 ok 环境），请像 CI 那样
用 `python -m unittest tests/test_chaos_mode_pro_mode.py` 单独一个进程运行本文件。
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

from qfluentwidgets import PushButton  # noqa: E402

from src.config import config  # noqa: E402
from ok.gui.tasks.ConfigItemFactory import config_widget  # noqa: E402
from ok.gui.tasks.LabelAndButtons import LabelAndButtons  # noqa: E402
from ok.gui.tasks.LabelAndSpinBox import LabelAndSpinBox  # noqa: E402
from ok.gui.tasks.LabelAndSwitchButton import LabelAndSwitchButton  # noqa: E402
from ok.test.TaskTestCase import TaskTestCase  # noqa: E402

from ChaosMode import ChaosMode  # noqa: E402

PRO_MODE_KEY = "刻印专业模式"
CONFIG_OPERATION_KEY = "配置操作"


class TestProModeConfigWiring(TaskTestCase):
    """ChaosMode 里"刻印专业模式"的配置项接线。"""

    task_class = ChaosMode
    config = config

    def _operation_buttons(self):
        return self.task.config_type[CONFIG_OPERATION_KEY]["buttons"]

    def test_default_config_has_pro_mode_switch(self):
        """默认配置里必须有这个布尔项，否则界面上就没有开关可点。"""
        self.assertIsInstance(self.task.default_config.get(PRO_MODE_KEY), bool)

    def test_pro_mode_has_no_description_text(self):
        """这个开关不显示配置说明。"""
        self.assertFalse(self.task.config_description.get(PRO_MODE_KEY))

    def test_equipment_slot_priorities_have_no_description_text(self):
        """装备位优先级只保留输入框，不显示额外注释。"""
        for key in ("装备1号位优先级", "装备2号位优先级", "装备3号位优先级"):
            self.assertFalse(self.task.config_description.get(key))

    def test_config_operation_has_no_pro_mode_button(self):
        """按钮列表里不能再有"刻印专业模式"按钮：该配置只保留开关一处。"""
        button_texts = [button.get("text") for button in self._operation_buttons()]
        self.assertNotIn(PRO_MODE_KEY, button_texts)
        self.assertNotIn("刻印专业模式开关", button_texts)

    def test_config_widget_renders_pro_mode_as_switch(self):
        """用 ok 的控件工厂渲染该配置项，确认界面上渲染出来的是普通开关。"""
        widget = config_widget(
            self.task.config_type,
            self.task.config_description,
            self.task.config,
            PRO_MODE_KEY,
            self.task.config.get(PRO_MODE_KEY),
            self.task,
        )
        self.assertIsInstance(widget, LabelAndSwitchButton)

    def test_other_operation_buttons_still_render(self):
        """配置操作里其余按钮不受影响，仍然能渲染出来。"""
        widget = config_widget(
            self.task.config_type,
            self.task.config_description,
            self.task.config,
            CONFIG_OPERATION_KEY,
            self.task.config.get(CONFIG_OPERATION_KEY),
            self.task,
        )
        self.assertIsInstance(widget, LabelAndButtons)
        button_texts = [button.text() for button in widget.findChildren(PushButton)]
        self.assertEqual(len(self._operation_buttons()), len(button_texts))
        self.assertNotIn(PRO_MODE_KEY, button_texts)
        self.assertIn("导入配置码", button_texts)


# "第一层/第二层路线优先级"的接线测试：卡厄思模式把原来的一条「路线优先级」
# 拆成两层分别配置，出击模式保持原样（仍然只有一条「路线优先级」）。
# 按层取值的逻辑与回退链由 tests/test_route_priority_layers.py 用假 task 覆盖。
LAYER1_ROUTE_KEY = "第一层路线优先级"
LAYER2_ROUTE_KEY = "第二层路线优先级"
LEGACY_ROUTE_KEY = "路线优先级"


class TestRoutePriorityLayersConfigWiring(TaskTestCase):
    """ChaosMode 里「第一层/第二层路线优先级」的配置项接线。"""

    task_class = ChaosMode
    config = config

    def test_default_config_has_two_layer_keys(self):
        """两层各有一个列表配置项；「商店」也是一项，默认固定排最前。"""
        expected = ["商店", "休息", "事件", "小怪", "精英"]
        self.assertEqual(expected, list(self.task.default_config.get(LAYER1_ROUTE_KEY)))
        self.assertEqual(expected, list(self.task.default_config.get(LAYER2_ROUTE_KEY)))

    def test_legacy_single_route_key_is_gone(self):
        """旧的一条「路线优先级」不再出现在默认配置里，否则界面上会重复三份。"""
        self.assertNotIn(LEGACY_ROUTE_KEY, self.task.default_config)

    def test_layer_keys_have_no_description_text(self):
        """这两项不显示说明文案。"""
        self.assertFalse(self.task.config_description.get(LAYER1_ROUTE_KEY))
        self.assertFalse(self.task.config_description.get(LAYER2_ROUTE_KEY))

    def test_config_widget_renders_layer_keys(self):
        for key in (LAYER1_ROUTE_KEY, LAYER2_ROUTE_KEY):
            widget = config_widget(
                self.task.config_type,
                self.task.config_description,
                self.task.config,
                key,
                self.task.config.get(key),
                self.task,
            )
            self.assertIsNotNone(widget)


# "商店删卡信用点下限"的接线测试：配置层是"一个数字输入框 + 一行短说明"，
# 判定逻辑（信用点 > 下限才删卡）由 tests/test_shop_remove_card_limits.py
# 用假 task 覆盖。
SHOP_REMOVE_FLOOR_KEY = "商店删卡信用点下限"


class TestShopRemoveCardFloorConfigWiring(TaskTestCase):
    """ChaosMode 里"商店删卡信用点下限"的配置项接线。"""

    task_class = ChaosMode
    config = config

    def test_default_config_has_int_floor(self):
        """默认配置里必须有这个数字项，否则界面上没有输入框可填。"""
        self.assertIsInstance(
            self.task.default_config.get(SHOP_REMOVE_FLOOR_KEY), int
        )
        self.assertEqual(0, self.task.default_config.get(SHOP_REMOVE_FLOOR_KEY))

    def test_config_description_marks_pro_mode_only(self):
        """说明文案只保留"仅在刻印专业模式下生效"这一句。"""
        description = self.task.config_description.get(SHOP_REMOVE_FLOOR_KEY) or ""
        self.assertIn("刻印专业模式", description)
        self.assertLessEqual(len(description), 20)

    def test_config_widget_renders_floor_as_spin_box(self):
        """用 ok 的控件工厂渲染，确认界面上渲染出来的是数字输入框。"""
        widget = config_widget(
            self.task.config_type,
            self.task.config_description,
            self.task.config,
            SHOP_REMOVE_FLOOR_KEY,
            self.task.config.get(SHOP_REMOVE_FLOOR_KEY),
            self.task,
        )
        self.assertIsInstance(widget, LabelAndSpinBox)


# "商店删卡次数上限"的接线测试：配置层是"一个数字输入框 + 一行短说明"，判定逻辑
# （本局在商店删卡数达到上限就不再删卡）由 tests/test_shop_remove_card_limits.py
# 用假 task 覆盖。
SHOP_REMOVE_LIMIT_KEY = "商店删卡次数上限"


class TestShopRemoveCardLimitConfigWiring(TaskTestCase):
    """ChaosMode 里"商店删卡次数上限"的配置项接线。"""

    task_class = ChaosMode
    config = config

    def test_default_config_has_int_limit(self):
        """默认配置里必须有这个数字项，缺省删除次数上限是 2。"""
        self.assertIsInstance(
            self.task.default_config.get(SHOP_REMOVE_LIMIT_KEY), int
        )
        self.assertEqual(2, self.task.default_config.get(SHOP_REMOVE_LIMIT_KEY))

    def test_config_description_marks_pro_mode_only(self):
        """说明文案只保留"仅在刻印专业模式下生效"这一句。"""
        description = self.task.config_description.get(SHOP_REMOVE_LIMIT_KEY) or ""
        self.assertIn("刻印专业模式", description)
        self.assertLessEqual(len(description), 20)

    def test_config_widget_renders_limit_as_spin_box(self):
        """用 ok 的控件工厂渲染，确认界面上渲染出来的是数字输入框。"""
        widget = config_widget(
            self.task.config_type,
            self.task.config_description,
            self.task.config,
            SHOP_REMOVE_LIMIT_KEY,
            self.task.config.get(SHOP_REMOVE_LIMIT_KEY),
            self.task,
        )
        self.assertIsInstance(widget, LabelAndSpinBox)


if __name__ == "__main__":
    unittest.main()

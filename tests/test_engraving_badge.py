"""刻印角标检测功能的单元测试。

测试覆盖：
1. 模板加载与缓存机制
2. 真实商店截图上的角标检测
3. 角标到 credit_icon 索引的关联逻辑
4. 边界情况（空帧、空模板、空图标列表等）
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import cv2
import numpy as np
import pathlib

# 确保项目根目录在 sys.path 中
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ok_tasks 内部使用平级导入（如 from utils_equipment_engraving import ...），
# 需要把 ok_tasks 目录也加入 sys.path 才能正常 import
OK_TASKS_DIR = ROOT / "ok_tasks"
if str(OK_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(OK_TASKS_DIR))


import ok_tasks.utils as utils  # noqa: E402
from ok_tasks.utils import _detect_shop_engraving_badges  # noqa: E402


def _read_image_cn(filename):
    """用 np.fromfile + imdecode 读取含中文文件名的图片。"""
    path = ROOT / "tests" / "images" / filename
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _make_credit_icon(x, y, w=39, h=46):
    """构造一个简单的 Box 替身，模拟 credit_icon。"""
    return SimpleNamespace(x=x, y=y, width=w, height=h)


class TestLoadEngravingBadgeTemplates(unittest.TestCase):
    """模板加载相关测试。"""

    def setUp(self):
        # 每个测试前重置全局缓存，保证隔离
        import ok_tasks.utils as utils_mod
        utils_mod._engraving_badge_templates = None

    def test_templates_load_two_files(self):
        """应成功加载两个模板文件（光/暗各一张）。"""
        os.chdir(ROOT)
        from ok_tasks.utils import _load_engraving_badge_templates
        templates = _load_engraving_badge_templates()
        self.assertEqual(2, len(templates), "应加载 2 个角标模板")
        for name, img in templates:
            self.assertIn("engraving_badge", name)
            self.assertIsNotNone(img)
            self.assertGreater(img.shape[0], 10, "模板高度应合理")
            self.assertGreater(img.shape[1], 10, "模板宽度应合理")

    def test_templates_are_cached(self):
        """第二次调用应返回同一对象，不重复读磁盘。"""
        os.chdir(ROOT)
        from ok_tasks.utils import _load_engraving_badge_templates
        first = _load_engraving_badge_templates()
        second = _load_engraving_badge_templates()
        self.assertIs(first, second, "两次调用应返回同一缓存对象")

    def test_templates_file_sizes_match_reference(self):
        """模板尺寸应与参考图标小图近似（允许 ±5 像素裁切偏差）。"""
        os.chdir(ROOT)
        from ok_tasks.utils import _load_engraving_badge_templates
        templates = _load_engraving_badge_templates()

        ref_light = _read_image_cn("光之刻印图标.PNG")
        ref_dark = _read_image_cn("暗之刻印图标.PNG")

        for name, img in templates:
            if "light" in name:
                ref = ref_light
            else:
                ref = ref_dark
            # 高度和宽度差值应在 ±5 像素以内
            self.assertAlmostEqual(img.shape[0], ref.shape[0], delta=5,
                                   msg=f"{name} 高度与参考图标偏差过大")
            self.assertAlmostEqual(img.shape[1], ref.shape[1], delta=5,
                                   msg=f"{name} 宽度与参考图标偏差过大")


class TestDetectEngravingBadgesOnRealScreenshots(unittest.TestCase):
    """用真实商店截图测试角标检测。"""

    def setUp(self):
        os.chdir(ROOT)
        import ok_tasks.utils as utils_mod
        utils_mod._engraving_badge_templates = None

    def _make_task_with_frame(self, image_filename):
        """构造带 frame 属性的 mock task。"""
        task = MagicMock()
        task.frame = _read_image_cn(image_filename)
        return task

    def test_detects_badges_in_light_screenshot(self):
        """光之刻印截图应至少检测到 1 个角标。"""
        task = self._make_task_with_frame("光之刻印.PNG")
        # 构造 6 个 credit_icon，模拟商店 6 个商品
        h, w = task.frame.shape[:2]
        credit_icons = [
            _make_credit_icon(int(w * (0.04 + i * 0.16)), int(h * 0.79))
            for i in range(6)
        ]
        detection = _detect_shop_engraving_badges(task, credit_icons)
        self.assertGreater(
            detection.badge_count, 0, "光之刻印截图应至少检测到 1 个角标",
        )
        self.assertGreater(
            len(detection.engraved_indexes), 0,
            "应至少关联到 1 个 credit_icon 索引",
        )

    def test_detects_badges_in_dark_screenshot(self):
        """暗之刻印截图应至少检测到 1 个角标。"""
        task = self._make_task_with_frame("暗之刻印.PNG")
        h, w = task.frame.shape[:2]
        credit_icons = [
            _make_credit_icon(int(w * (0.04 + i * 0.16)), int(h * 0.79))
            for i in range(6)
        ]
        detection = _detect_shop_engraving_badges(task, credit_icons)
        self.assertGreater(
            detection.badge_count, 0, "暗之刻印截图应至少检测到 1 个角标",
        )

    def test_no_badges_on_blank_frame(self):
        """纯黑帧不应检测到任何角标。"""
        task = MagicMock()
        task.frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        credit_icons = [_make_credit_icon(100, 750)]
        detection = _detect_shop_engraving_badges(task, credit_icons)
        self.assertEqual(0, detection.badge_count, "纯黑帧不应检测到角标")
        self.assertEqual(set(), detection.engraved_indexes)

    def test_none_frame_returns_empty(self):
        """frame 为 None 时应返回空结果。"""
        task = MagicMock()
        task.frame = None
        detection = _detect_shop_engraving_badges(
            task, [_make_credit_icon(100, 750)],
        )
        self.assertEqual(0, detection.badge_count)
        self.assertEqual(set(), detection.engraved_indexes)

    def test_empty_templates_returns_empty(self):
        """模板列表为空时应直接返回空结果。"""
        import ok_tasks.utils as utils_mod
        # 强制缓存为空列表（模拟模板文件缺失）
        utils_mod._engraving_badge_templates = []
        task = MagicMock()
        task.frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        detection = _detect_shop_engraving_badges(
            task, [_make_credit_icon(100, 750)],
        )
        self.assertEqual(0, detection.badge_count)
        self.assertEqual(set(), detection.engraved_indexes)

    def test_empty_credit_icons_returns_zero_indexes(self):
        """credit_icons 为空时，角标数量可能 > 0 但索引集合应为空。"""
        task = self._make_task_with_frame("暗之刻印.PNG")
        detection = _detect_shop_engraving_badges(task, [])
        # 角标可能检测到，但没有 credit_icon 可关联
        self.assertEqual(set(), detection.engraved_indexes)


class TestBadgeToCreditIconMapping(unittest.TestCase):
    """角标到 credit_icon 的关联逻辑测试。

    合成帧在 TM_CCOEFF_NORMED 下会产生大量伪匹配（纯黑背景 + 纯色块
    在每个位置的相关系数都很高），所以这里 mock cv2.matchTemplate，
    精确控制匹配结果矩阵中哪些位置超过阈值，只测试 NMS 和关联逻辑。
    """

    def setUp(self):
        os.chdir(ROOT)

    def _detect_with_mocked_matches(self, match_points, credit_icons,
                                    frame_h=900, frame_w=1600, confidence=0.95):
        """用 mock matchTemplate 模拟指定的角标检测结果。

        match_points: list of (px, py) — 在搜索区域内的匹配坐标
                      （px 是搜索区域内的 x，py 是搜索区域内的 y）
        credit_icons: list of SimpleNamespace
        confidence: 模拟的匹配置信度
        """
        import ok_tasks.utils as utils_mod

        # 注入假模板
        fake_template = np.full((10, 10, 3), 128, dtype=np.uint8)
        original_cache = utils_mod._engraving_badge_templates
        utils_mod._engraving_badge_templates = [("fake.png", fake_template)]

        # 构造 mock matchTemplate 返回值
        def fake_match_template(search_area, template, method):
            sh, sw = search_area.shape[:2]
            th, tw = template.shape[:2]
            result = np.zeros((sh - th + 1, sw - tw + 1), dtype=np.float32)
            for px, py in match_points:
                if 0 <= py < result.shape[0] and 0 <= px < result.shape[1]:
                    result[py, px] = confidence
            return result

        task = MagicMock()
        task.frame = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)

        try:
            with patch("cv2.matchTemplate", side_effect=fake_match_template):
                result = _detect_shop_engraving_badges(task, credit_icons)
            return result
        finally:
            utils_mod._engraving_badge_templates = original_cache

    def test_single_badge_maps_to_nearest_icon(self):
        """单个角标应关联到距离最近的 credit_icon。"""
        icons = [
            _make_credit_icon(100, 700),
            _make_credit_icon(400, 700),
            _make_credit_icon(700, 700),
        ]
        # 搜索区域 y1 = int(0.35 * 900) = 315
        # 角标在帧上 y=500，搜索区域内 py = 500 - 315 = 185
        # 角标 x=420，px=420 → 最近的 icon 是 index 1 (400, 700)
        result = self._detect_with_mocked_matches(
            match_points=[(420, 185)],
            credit_icons=icons,
        )
        self.assertIn(
            1, result.engraved_indexes,
            "角标应关联到最近的 credit_icon（index=1）",
        )

    def test_multiple_badges_map_to_correct_icons(self):
        """多个角标应分别关联到各自最近的 credit_icon。"""
        icons = [
            _make_credit_icon(100, 700),
            _make_credit_icon(400, 700),
            _make_credit_icon(700, 700),
        ]
        # y1=315; 左侧角标 (90, 500) → py=185; 右侧角标 (710, 500) → py=185
        result = self._detect_with_mocked_matches(
            match_points=[(90, 185), (710, 185)],
            credit_icons=icons,
        )
        self.assertIn(0, result.engraved_indexes, "左侧角标应关联到 index=0")
        self.assertIn(2, result.engraved_indexes, "右侧角标应关联到 index=2")

    def test_badge_outside_shop_region_is_filtered(self):
        """y 相对位置不在 0.40~0.82 范围内的角标应被过滤。"""
        icons = [_make_credit_icon(100, 700)]
        # y1=315; 角标在帧上 y=50 → py = 50 - 315 = -265 → 截断到 0
        # 帧上 y = 0 + 315 = 315, 相对位置 = 315/900 ≈ 0.35 < 0.40 → 被过滤
        result = self._detect_with_mocked_matches(
            match_points=[(100, 0)],
            credit_icons=icons,
        )
        self.assertEqual(
            0, result.badge_count, "超出商品区域的角标应被过滤",
        )

    def test_nearby_badges_are_merged_by_nms(self):
        """距离 < 40 像素的重复检测应被 NMS 合并为一个。"""
        icons = [_make_credit_icon(400, 700)]
        # 两个匹配点间距 5 像素，应被 NMS 合并
        result = self._detect_with_mocked_matches(
            match_points=[(400, 185), (405, 190)],
            credit_icons=icons,
        )
        self.assertLessEqual(
            result.badge_count, 1, "近距离重复角标应被 NMS 合并",
        )

    def test_no_matches_returns_empty(self):
        """没有任何匹配时，应返回空结果。"""
        icons = [_make_credit_icon(400, 700)]
        result = self._detect_with_mocked_matches(
            match_points=[],
            credit_icons=icons,
        )
        self.assertEqual(0, result.badge_count)
        self.assertEqual(set(), result.engraved_indexes)


    def test_badge_below_product_cell_is_ignored(self):
        """角标落在商品格子之外（价格行下方）时不采信。

        信用点图标在商品格子左下的价格行，格子下沿在 icon_y - 0.02 处，
        价格行附近的匹配不能计入角标。
        """
        icons = [_make_credit_icon(100, 700)]
        # 帧上 y=730 → 相对 0.811，已在格子下沿 705 之外
        result = self._detect_with_mocked_matches(
            match_points=[(100, 730 - 315)],
            credit_icons=icons,
        )
        self.assertEqual(0, result.badge_count)
        self.assertEqual(set(), result.engraved_indexes)

    def test_low_confidence_match_is_ignored(self):
        """置信度低于 0.85 的匹配不算角标。"""
        icons = [_make_credit_icon(100, 700)]
        result = self._detect_with_mocked_matches(
            match_points=[(100, 185)], credit_icons=icons, confidence=0.80,
        )
        self.assertEqual(0, result.badge_count)
        self.assertEqual(set(), result.engraved_indexes)
        # 0.80 没到阈值，但落在边界区间：不能当作"这件确定没有角标"
        self.assertEqual({0: 0.80}, result.borderline_confidences)

    def test_high_confidence_match_is_kept(self):
        """置信度达到 0.85 的匹配算角标。"""
        icons = [_make_credit_icon(100, 700)]
        result = self._detect_with_mocked_matches(
            match_points=[(100, 185)], credit_icons=icons, confidence=0.90,
        )
        self.assertEqual(1, result.badge_count)
        self.assertIn(0, result.engraved_indexes)
        self.assertEqual({}, result.borderline_confidences)

    def test_borderline_confidence_is_reported_for_description_check(self):
        """0.84 不达阈值但落在边界区间：报给调用方，改用详情页描述判定。"""
        icons = [_make_credit_icon(100, 700)]
        result = self._detect_with_mocked_matches(
            match_points=[(100, 185)], credit_icons=icons, confidence=0.84,
        )
        self.assertEqual(0, result.badge_count)
        self.assertEqual(set(), result.engraved_indexes)
        self.assertEqual({0: 0.84}, result.borderline_confidences)

    def test_confidence_below_borderline_floor_is_not_reported(self):
        """0.79 及以下属于图标美术噪声，不进边界区间，整排跳过。"""
        icons = [_make_credit_icon(100, 700)]
        result = self._detect_with_mocked_matches(
            match_points=[(100, 185)], credit_icons=icons, confidence=0.79,
        )
        self.assertEqual(0, result.badge_count)
        self.assertEqual(set(), result.engraved_indexes)
        self.assertEqual({}, result.borderline_confidences)

if __name__ == "__main__":
    unittest.main()

# 装备刻印判定逻辑（卡厄思模式专用）。
#
# 本模块只负责"给定槏位当前状态与候选装备信息，判断是否应该分配给目标主战员"
# 这一件事，不做任何截图、OCR、点击等操作，也不依赖 ok 框架的 TriggerTask，
# 方便脱离游戏环境单独维护和验证。
#
# 设计背景（与用户确认过的规则）：
# 1. "刷装备刻印"功能开启后，装备分配的判断标准从"名字优先级+品质"整体切换为
#    "是否推进目标名+目标刻印的进展"，原有的 _should_install_equipment 不再参与
#    决策；候选装备只要不能带来进展，一律不分配给目标主战员。
# 2. 目标装备名沿用现有的"装备X号位优先级"配置（一个列表，命中列表中任意一个
#    名称即算"目标名"），目标刻印是全局共享的一套关键词（不区分1/2/3号槏位），
#    命中任意一个即算"带目标刻印"；但"某个槏位是否已经拿到目标刻印"这件事，
#    是按槏位各自独立追踪的（1号位的刻印状态与2/3号位互不影响）。
# 3. 槏位判定为"已完成" = 当前装备名命中目标名列表 且 当前描述命中任一目标
#    刻印关键词。已完成的槏位，后续任何候选都不会再分配给目标主战员。
# 4. 候选装备带目标刻印（无论候选叫什么名字）：
#      - 槏位当前没有目标刻印 -> 分配给目标主战员（优先占住刻印，哪怕名字不对）。
#      - 槏位当前已经有目标刻印，但候选名字不在目标名列表中 -> 不分配（没有新进展）。
#      - 槏位当前已经有目标刻印，且候选命中目标名列表 -> 分配（一步到位）。
# 5. 候选装备不带任何目标刻印，但候选命中目标名列表，且槏位尚未完成 -> 分配
#    （游戏机制：无刻印装备覆盖时会继承原有刻印，是安全的换名操作）。
# 6. 其余情况（候选既不命中目标名列表，也不带目标刻印）-> 不分配，转给其他队员。


def _normalize(text):
    """去除首尾空白，None 转为空字符串，避免各判断函数里重复处理。"""
    return (text or "").strip()


def has_target_engraving(description, engraving_keywords):
    """判断装备描述文字是否命中"装备刻印优先级"里的任意关键词。

    Args:
        description: 装备的完整描述文字（OCR 识别结果）。
        engraving_keywords: 目标刻印关键词列表，命中任意一个即算匹配。

    Returns:
        bool
    """
    description = _normalize(description)
    if not description or not engraving_keywords:
        return False
    for keyword in engraving_keywords:
        keyword = _normalize(keyword)
        if keyword and keyword in description:
            return True
    return False


def matches_target_name(name, target_names):
    """判断装备名是否命中目标名列表中的任意一个（双向包含匹配，
    与项目内 _match_equipment_name 的匹配规则保持一致）。

    Args:
        name: 待判断的装备名称。
        target_names: 目标装备名列表（对应"装备X号位优先级"配置）。

    Returns:
        bool
    """
    name = _normalize(name)
    if not name or not target_names:
        return False
    for target_name in target_names:
        target_name = _normalize(target_name)
        if not target_name:
            continue
        if name in target_name or target_name in name:
            return True
    return False


def is_slot_completed(current_name, current_description, target_names, engraving_keywords):
    """判断某个槏位当前是否已经"完成"：装备名命中目标名列表 且 描述命中目标刻印。

    Args:
        current_name: 该槏位当前装备的名称（可能为空，表示空槏位）。
        current_description: 该槏位当前装备的完整描述文字。
        target_names: 该槏位的目标装备名列表（"装备X号位优先级"配置）。
        engraving_keywords: 目标刻印关键词列表。

    Returns:
        bool
    """
    if not matches_target_name(current_name, target_names):
        return False
    return has_target_engraving(current_description, engraving_keywords)


def should_assign_to_target_member(
    current_name,
    current_description,
    candidate_name,
    candidate_description,
    target_names,
    engraving_keywords,
):
    """核心判定：候选装备是否应该分配给目标主战员。

    Args:
        current_name: 该槏位当前装备名称（可能为空）。
        current_description: 该槏位当前装备描述（可能为空）。
        candidate_name: 待选装备的名称。
        candidate_description: 待选装备的完整描述文字。
        target_names: 该槏位的目标装备名列表（"装备X号位优先级"配置）。
        engraving_keywords: 目标刻印关键词列表。

    Returns:
        (bool, str): (是否分配给目标主战员, 判定原因说明，用于日志)
    """
    if is_slot_completed(current_name, current_description, target_names, engraving_keywords):
        return False, "槏位已完成（目标名+目标刻印均已满足），无需继续替换"

    slot_has_engraving = has_target_engraving(current_description, engraving_keywords)
    candidate_has_engraving = has_target_engraving(candidate_description, engraving_keywords)
    candidate_is_target_name = matches_target_name(candidate_name, target_names)

    if candidate_has_engraving:
        if not slot_has_engraving:
            return True, "候选装备带目标刻印，槏位当前没有刻印，优先占住刻印"
        if candidate_is_target_name:
            return True, "槏位已有目标刻印，候选装备命中目标名，一步到位"
        return False, "槏位已有目标刻印，候选装备名字不在目标名列表中，没有新进展"

    if candidate_is_target_name:
        return True, "候选装备命中目标名且不带刻印，通过继承机制安全替换（保留原有刻印）"

    return False, "候选装备既未命中目标名也不带目标刻印，没有任何进展"

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
#    名称即算"目标名"）。目标刻印同样是一份有序列表（"装备刻印优先级"配置，
#    不区分1/2/3号槏位），**列表顺序代表优先级高低，排在越前面（下标越小）
#    优先级越高**。命中列表中排名第一（下标0）的那一项，才算该槏位"拿到了
#    最优刻印"；命中排名较靠后的项，只算"有目标刻印，但还不是最优"，槏位
#    仍应继续尝试替换成排名更靠前的刻印。"某个槏位当前命中到列表第几项"是
#    按槏位各自独立追踪的（1号位的刻印状态与2/3号位互不影响）。
# 3. 槏位判定为"已完成" = 当前装备名命中目标名列表 且 当前描述命中的是刻印
#    优先级列表中排名第一（下标0）的那一项。已完成的槏位，后续任何候选都
#    不会再分配给目标主战员。
# 4. 候选装备命中的刻印比槏位当前更优（下标更小，或槏位当前没有任何目标
#    刻印）：分配给目标主战员，优先占住更优刻印，不论候选叫什么名字。
# 5. 候选装备完全不带任何目标刻印：这类装备覆盖槏位时会**继承**槏位当前
#    已有的刻印（不会丢失，也不会变差），所以只要候选命中目标名列表，且
#    槏位尚未达到最优刻印，就可以安全换名——分配给目标主战员。
# 6. 候选装备带有跟槏位当前"同等优先级"的刻印，且候选命中目标名列表：换
#    上去刻印等级不变、名字变对，属于安全进展，分配给目标主战员。
# 7. 候选装备带有比槏位当前"更差"的刻印（注意：不是没有刻印，是有刻印但
#    优先级更低）：**无论候选是否命中目标名列表，都绝不能分配**——因为
#    候选自带的（较差）刻印会直接覆盖槏位当前更好的刻印，造成刻印降级，
#    这与"无刻印装备继承原刻印"是完全不同的两种情况，必须严格区分。
# 8. 其余情况（候选既不命中目标名列表，也不带任何目标刻印）：不分配，
#    转给其他队员。


def _normalize(text):
    """去除首尾空白，None 转为空字符串，避免各判断函数里重复处理。"""
    return (text or "").strip()


def best_engraving_rank(description, engraving_keywords):
    """返回装备描述文字命中的"装备刻印优先级"列表中的最高优先级下标
    （数值越小优先级越高，0 为最高）；未命中任何关键词时返回 None。

    描述里可能同时包含多条词条、命中多个关键词，此时取排名最靠前
    （下标最小）的那一个。

    Args:
        description: 装备的完整描述文字（OCR 识别结果）。
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。

    Returns:
        int | None
    """
    description = _normalize(description)
    if not description or not engraving_keywords:
        return None
    best_rank = None
    for rank, keyword in enumerate(engraving_keywords):
        keyword = _normalize(keyword)
        if not keyword:
            continue
        if keyword in description:
            if best_rank is None or rank < best_rank:
                best_rank = rank
    return best_rank


def has_target_engraving(description, engraving_keywords):
    """判断装备描述文字是否命中"装备刻印优先级"里的任意关键词（不区分
    优先级高低，仅用于判断"是否带有任意目标刻印"这一粗粒度问题）。

    Args:
        description: 装备的完整描述文字（OCR 识别结果）。
        engraving_keywords: 目标刻印关键词列表，命中任意一个即算匹配。

    Returns:
        bool
    """
    return best_engraving_rank(description, engraving_keywords) is not None


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
    """判断某个槏位当前是否已经"完成"：装备名命中目标名列表 且 描述命中的
    刻印是"装备刻印优先级"列表中排名第一（优先级最高，下标0）的那一项。

    命中排名较靠后的刻印，不算完成——槏位仍应继续尝试替换成更优先级的刻印。

    Args:
        current_name: 该槏位当前装备的名称（可能为空，表示空槏位）。
        current_description: 该槏位当前装备的完整描述文字。
        target_names: 该槏位的目标装备名列表（"装备X号位优先级"配置）。
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。

    Returns:
        bool
    """
    if not matches_target_name(current_name, target_names):
        return False
    return best_engraving_rank(current_description, engraving_keywords) == 0


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
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。

    Returns:
        (bool, str): (是否分配给目标主战员, 判定原因说明，用于日志)
    """
    if is_slot_completed(current_name, current_description, target_names, engraving_keywords):
        return False, "槏位已完成（目标名+最优刻印均已满足），无需继续替换"

    slot_rank = best_engraving_rank(current_description, engraving_keywords)
    candidate_rank = best_engraving_rank(candidate_description, engraving_keywords)
    candidate_is_target_name = matches_target_name(candidate_name, target_names)

    # 情况1：候选命中的刻印比槏位当前更优（下标更小），或槏位当前没有任何
    # 目标刻印而候选带有刻印 —— 直接占更优刻印，不论候选叫什么名字。
    if candidate_rank is not None and (slot_rank is None or candidate_rank < slot_rank):
        if candidate_is_target_name:
            return True, (
                f"候选装备命中优先级更高的刻印（排名第{candidate_rank + 1}），"
                "且命中目标名，一步到位"
            )
        return True, (
            f"候选装备命中优先级更高的刻印（排名第{candidate_rank + 1}），"
            "优先占住更优刻印（哪怕名字不对）"
        )

    # 情况2：候选完全不带任何目标刻印。这类装备覆盖时会继承槏位当前已有
    # 的刻印，不会造成降级，只要命中目标名即可安全换名。
    if candidate_rank is None:
        if candidate_is_target_name:
            return True, "候选装备命中目标名且不带任何刻印，通过继承机制安全替换（保留原有刻印）"
        return False, "候选装备既未命中目标名，也不带任何目标刻印，没有任何进展"

    # 到这里 candidate_rank 必然不为 None，且 slot_rank 必然不为 None
    # （否则已经在情况1中被处理），且 candidate_rank >= slot_rank。

    # 情况3：候选带有与槏位当前"同等优先级"的刻印，且命中目标名——换上去
    # 刻印等级不变、名字变对，属于安全进展。
    if candidate_rank == slot_rank and candidate_is_target_name:
        return True, (
            f"候选装备刻印优先级与槏位当前相同（排名第{candidate_rank + 1}），"
            "且命中目标名，换名不会让刻印退步"
        )

    # 情况4：候选带有比槏位当前更差的刻印（自带较差刻印会直接覆盖，不会
    # 继承槏位当前更好的刻印，属于降级），无论名字是否命中都绝不能分配。
    if candidate_rank > slot_rank:
        return False, (
            f"候选装备自带的刻印优先级（排名第{candidate_rank + 1}）低于槏位"
            f"当前已有的（排名第{slot_rank + 1}），安装会导致刻印降级，拒绝"
        )

    # candidate_rank == slot_rank 但候选不是目标名：刻印等级不变，名字也
    # 没有进展，没有意义。
    return False, "候选装备刻印优先级与槏位当前相同，但名字不是目标名，没有新进展"

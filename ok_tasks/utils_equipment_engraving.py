# 装备刻印判定逻辑（卡厄思模式专用）。
#
# 本模块只负责"给定装备当前状态与候选装备信息，判断是否应该分配给目标主战员"
# 这一件事，不做任何截图、OCR、点击等操作，也不依赖 ok 框架的 TriggerTask，
# 方便脱离游戏环境单独维护和验证。
#
# 判定规则：
# 1. "刷装备刻印"功能开启后，装备分配的判断标准从"名字优先级+品质"整体切换为
#    "是否推进目标名+目标刻印的进展"，原有的 _should_install_equipment 不再参与
#    决策；候选装备只要不能带来进展，一律不分配给目标主战员。
# 2. 目标装备名沿用现有的"装备X号位优先级"配置（一个列表，命中列表中任意一个
#    名称即算"目标名"）。目标刻印同样是一份有序列表（"装备刻印优先级"配置，
#    不区分1/2/3号装备），**列表顺序代表优先级高低，排在越前面（下标越小）
#    优先级越高**。命中列表中的任意一项都算该装备"已经有目标刻印"，但排名
#    靠后并不算"完成"：装备名还没对上的装备仍会继续尝试换成排名更靠前的
#    刻印。"某个装备当前命中到列表第几项"是按装备各自独立追踪的（1号位的
#    刻印状态与2/3号位互不影响）。
# 3. 装备判定为"已完成" = 当前装备名命中目标名列表 且 当前描述命中了刻印
#    优先级列表中的任意一项（不要求排名第一）。已完成的装备，装备名与刻印
#    一起锁定：后续任何候选都不会再分配给目标主战员——"刻印排名还不够靠前"
#    本身不算继续刷的理由。
# 4. 候选装备命中的刻印比装备当前更优（下标更小，或装备当前没有任何目标
#    刻印）：分配给目标主战员，优先占住更优刻印，不论候选叫什么名字。
# 5. 候选装备完全不带任何目标刻印：这类装备覆盖装备时会**继承**装备当前
#    已有的刻印（不会丢失，也不会变差），所以只要候选命中目标名列表，且
#    装备尚未完成（装备名还不是目标名），就可以安全换名——分配给目标主战员。
# 6. 候选装备带有跟装备当前"同等优先级"的刻印，且候选命中目标名列表：换
#    上去刻印等级不变、名字变对，属于安全进展，分配给目标主战员。
# 7. 候选装备带有比装备当前"更差"的刻印（注意：不是没有刻印，是有刻印但
#    优先级更低）：**无论候选是否命中目标名列表，都绝不能分配**——因为
#    候选自带的（较差）刻印会直接覆盖装备当前更好的刻印，造成刻印降级，
#    这与"无刻印装备继承原刻印"是完全不同的两种情况，必须严格区分。
# 8. 其余情况（候选既不命中目标名列表，也不带任何目标刻印）：不分配，
#    转给其他队员。
# 9. "刻印专业模式"（配置项"刻印专业模式"，由配置面板上的开关切换）在以上规则之上再加
#    一条出口规则：免费获得（非商店购买）的装备被判定为"不分配"时，不再转给
#    其他队员，而是直接提炼（分解）成信用点，见 should_refine_free_equipment。
# 10. 脚本侧的槽位状态必须跟着上面的"继承"一起维护：候选装备不带刻印时，
#    安装后该槽位记录的描述要保留原描述（继承来的刻印还在），不能直接改成
#    候选描述，否则后续判定会把该槽位误当成"没有刻印"，见
#    resolve_slot_description_after_install（覆盖时继承槽位原刻印，槽位描述
#    要继续保留原刻印文字，不能直接改成候选描述）。
# 11. 免费获得的装备（战利品/事件奖励）拿不到商店货架的刻印角标，只能用详情页
#     描述里的"刻印图标"（OCR 会把它识别成 🎆 之类的字符）兜底判断候选是否自带
#     刻印，见 resolve_candidate_engraving_flag；图标没识别出来只能算"未知"，
#     绝不能当成"确认没有刻印"。
# 12. 商店预筛选（"刷装备刻印"模式下只点开带刻印角标的装备）要
#     给"槽位已经拿到目标刻印、只差一件目标名装备"的情况放行：这类槽位唯一能
#     推进的候选恰恰是一件命中目标名、自身不带任何刻印的装备（它同样没有
#     角标），见 name_only_candidate_slot；槽位当前没有任何目标刻印（空槽，或
#     描述里没有目标刻印关键词）时不放行——覆盖过去没有刻印可继承，装上去只是
#     把名字填上，等于白花信用点。
# 13. "刻印专业模式"在"第二个Boss房间前"的检查点上有三种走向，由纯函数
#     decide_pro_mode_check_point_action 给出：三个装备位都达标 -> 停止程序供
#     人工检查、**不进入最后的Boss房间**；不达标 -> 点击退出、逃脱重开一局；
#     不达标又找不到「退出」按钮 -> 暂停等人工处理，绝不自动进入Boss。

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


# 装备描述里出现这些字符，说明这次 OCR 把游戏里的"刻印图标"识别成了文字。
# 商店货架上的装备靠左下角角标（模板匹配）判断是否带刻印，而免费获得装备
# （战利品/事件奖励）的详情页没有角标可匹配，只能用描述里的图标兜底。
ENGRAVING_ICON_MARKS = ("🎆",)


def has_engraving_icon_mark(description):
    """描述文字里是否出现了"刻印图标"（OCR 把图标识别成 🎆 之类的字符）。

    只有正向证据（确实看到图标）才返回 True；看不到图标**不代表**没有刻印——
    OCR 会把这个图标识别成 ?、X、Q 等字符，甚至直接丢掉，所以调用方必须把
    False 当成"未知"，不能当成"确认没有刻印"。

    Args:
        description: 装备的完整描述文字（OCR 识别结果）。

    Returns:
        bool
    """
    description = _normalize(description)
    return any(mark in description for mark in ENGRAVING_ICON_MARKS)


def resolve_candidate_engraving_flag(candidate_has_engraving, candidate_description):
    """合并"候选是否自带刻印"的两个证据来源，给出统一结论。

    - 商店购买页面：刻印角标模板匹配的结果（True/False）；
    - 免费获得装备：拿不到角标，用详情页描述里的刻印图标兜底（看到图标 => True）。

    角标结论是 True 时直接采信；角标没检测出刻印（False/None）时再检查描述里的
    刻印图标，命中就提升为 True（覆盖"角标漏检"的情况）。两者都没有证据时保持
    原值（None = 未知，沿用判定函数的宽松口径）。

    Args:
        candidate_has_engraving: 角标检测结果（True/False/None）。
        candidate_description: 候选装备的描述文字。

    Returns:
        bool | None
    """
    if candidate_has_engraving is True:
        return True
    if has_engraving_icon_mark(candidate_description):
        return True
    return candidate_has_engraving


def resolve_slot_description_after_install(
    current_description,
    candidate_description,
    engraving_keywords=None,
    candidate_has_engraving=None,
):
    """算出"把候选装备装上去之后，该槽位应该记录成什么描述"（脚本侧状态维护）。

    游戏机制（见模块头部第 5 条）：候选装备**完全不带刻印**时，覆盖槽位会
    继承槽位原有刻印；候选**自带刻印**（哪怕是另一个刻印）时，会把槽位原有
    刻印顶掉。而 should_assign_to_target_member 里"命中目标名、且不带任何
    刻印 -> 通过继承机制安全替换"这条判定，前提就是"刻印会被继承保留"。
    脚本侧必须跟着这个前提一起维护槽位描述，不能无脑换成候选描述：

    - 候选自带刻印（有角标，或描述命中目标刻印关键词）-> 用候选描述覆盖，
      槽位记录的就是候选带来的那个刻印；
    - 候选不带刻印（角标确认没有，或拿不到角标信息时沿用判定函数的宽松
      口径）-> 保留槽位原描述（继承来的刻印还在），原描述为空时回退候选描述。

    若直接把槽位描述换成候选描述，脚本会误以为该槽位"没有刻印"：后面再出现
    "自带较差刻印"的候选时会被当成"更优刻印"买下，在游戏里反而把继承来的
    好刻印覆盖掉（判定降级），专业模式的逃脱检查也会误判该槽位未达标。

    Args:
        current_description: 安装前该槽位记录的描述（空槽位为空串）。
        candidate_description: 候选装备的描述。
        engraving_keywords: 目标刻印关键词列表（"装备刻印优先级"配置）。
        candidate_has_engraving: 商店刻印角标检测结果（True/False/None）。

    Returns:
        str: 安装后该槽位应记录的描述。
    """
    candidate_description = _normalize(candidate_description)
    if (
        candidate_has_engraving is True
        or has_engraving_icon_mark(candidate_description)
        or has_target_engraving(candidate_description, engraving_keywords or [])
    ):
        return candidate_description
    inherited = _normalize(current_description)
    return inherited or candidate_description


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


def slot_allows_any_name(target_names):
    """该装备位是否"不限装备名"：目标名列表为空（或全是空串）时为 True。

    把某个"装备X号位优先级"留空，表示这一位不挑装备名，
    "任意装备都行，只要刻印对上"——该位的达标判定只看描述有没有命中
    "装备刻印优先级"里的任意一项（见 is_slot_completed）。

    注意：matches_target_name 对空列表**仍然返回 False**。这里的"不限名字"只作用于
    达标判定与日志说明，不能拿去当成"命中目标名"，否则
    should_assign_to_target_member 的继承换名分支、name_only_candidate_slot
    会把任意候选都当成目标名放行。

    Args:
        target_names: 该装备位的目标装备名列表（"装备X号位优先级"配置）。

    Returns:
        bool
    """
    if not target_names:
        return True
    return all(not _normalize(target_name) for target_name in target_names)


def is_slot_completed(current_name, current_description, target_names, engraving_keywords):
    """判断某个装备当前是否已经"完成"：装备名命中目标名列表 且 描述命中了
    "装备刻印优先级"列表中的任意一项目标刻印（不要求排名第一）。

    该装备的目标名列表留空时"不限装备名"（见 slot_allows_any_name）：名字这一项
    直接视为满足，只看刻印有没有命中——配置留空就等于"任意装备都行，只要刻印对"。

    刻印排名只影响"装备名还没对上"的装备还能不能继续升级刻印；一旦装备名和
    刻印都对上，该装备就锁定：后续既不再更换装备名，也不再更换刻印。

    Args:
        current_name: 该装备当前装备的名称（可能为空，表示空装备）。
        current_description: 该装备当前装备的完整描述文字。
        target_names: 该装备的目标装备名列表（"装备X号位优先级"配置）。
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。

    Returns:
        bool
    """
    if not slot_allows_any_name(target_names) and not matches_target_name(
        current_name, target_names
    ):
        return False
    return has_target_engraving(current_description, engraving_keywords)


def should_assign_to_target_member(
    current_name,
    current_description,
    candidate_name,
    candidate_description,
    target_names,
    engraving_keywords,
    candidate_has_engraving=None,
):
    """核心判定：候选装备是否应该分配给目标主战员。

    Args:
        current_name: 该装备当前装备名称（可能为空）。
        current_description: 该装备当前装备描述（可能为空）。
        candidate_name: 待选装备的名称。
        candidate_description: 待选装备的完整描述文字。
        target_names: 该装备的目标装备名列表（"装备X号位优先级"配置）。
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。
        candidate_has_engraving: 候选装备是否自带刻印（由商店刻印角标判别）。True=确定自带刻印，False/None=未检测到或无法判断。

    Returns:
        (bool, str): (是否分配给目标主战员, 判定原因说明，用于日志)
    """
    if is_slot_completed(current_name, current_description, target_names, engraving_keywords):
        return False, "装备已完成（目标名+目标刻印均已满足），无需继续替换"

    slot_rank = best_engraving_rank(current_description, engraving_keywords)
    candidate_rank = best_engraving_rank(candidate_description, engraving_keywords)
    candidate_is_target_name = matches_target_name(candidate_name, target_names)

    # 情况1：候选命中的刻印比装备当前更优（下标更小），或装备当前没有任何
    # 目标刻印而候选带有刻印 —— 直接占更优刻印，不论候选叫什么名字。
    if candidate_rank is not None and (slot_rank is None or candidate_rank < slot_rank):
        if candidate_is_target_name:
            return True, (
                f"候选装备命中优先级更高的刻印（排名第{candidate_rank + 1}），"
                "且命中目标名，一步到位"
            )
        if slot_allows_any_name(target_names):
            return True, (
                f"候选装备命中优先级更高的刻印（排名第{candidate_rank + 1}），"
                "该装备不限装备名，直接换上"
            )
        return True, (
            f"候选装备命中优先级更高的刻印（排名第{candidate_rank + 1}），"
            "优先占住更优刻印（哪怕名字不对）"
        )

    # 情况2：候选不含任何目标刻印。这里必须先区分两种完全不同的情况：
    #   (a) 候选真的没有刻印 —— 覆盖时继承装备已有刻印，属于安全换名；
    #   (b) 候选自带的是"非目标刻印" —— 装上会用候选自带的刻印覆盖掉装备
    #       当前更好的刻印，属于降级，必须拒绝（见模块头部第 7 条）。
    # 两者的描述文字都不含目标关键词，只能靠"是否带刻印角标"
    # （candidate_has_engraving）区分；为 None 时表示角标检测未生效、
    # 无法判断，沿用旧的宽松行为。
    if candidate_rank is None:
        if candidate_has_engraving:
            return False, (
                "候选装备自带的是非目标刻印，安装会覆盖并降级装备当前刻印，拒绝"
            )
        if candidate_is_target_name:
            return True, "候选装备命中目标名且不带任何刻印，通过继承机制安全替换（保留原有刻印）"
        return False, "候选装备既未命中目标名，也不带任何目标刻印，没有任何进展"

    # 到这里 candidate_rank 必然不为 None，且 slot_rank 必然不为 None
    # （否则已经在情况1中被处理），且 candidate_rank >= slot_rank。

    # 情况3：候选带有与装备当前"同等优先级"的刻印，且命中目标名——换上去
    # 刻印等级不变、名字变对，属于安全进展。
    if candidate_rank == slot_rank and candidate_is_target_name:
        return True, (
            f"候选装备刻印优先级与装备当前相同（排名第{candidate_rank + 1}），"
            "且命中目标名，换名不会让刻印退步"
        )

    # 情况4：候选带有比装备当前更差的刻印（自带较差刻印会直接覆盖，不会
    # 继承装备当前更好的刻印，属于降级），无论名字是否命中都绝不能分配。
    if candidate_rank > slot_rank:
        return False, (
            f"候选装备自带的刻印优先级（排名第{candidate_rank + 1}）低于装备"
            f"当前已有的（排名第{slot_rank + 1}），安装会导致刻印降级，拒绝"
        )

    # candidate_rank == slot_rank 但候选不是目标名：刻印等级不变，名字也
    # 没有进展，没有意义。
    return False, "候选装备刻印优先级与装备当前相同，但名字不是目标名，没有新进展"


def should_refine_free_equipment(
    *,
    is_purchase_page,
    engraving_mode,
    pro_mode,
    slot_evaluated,
    assign_to_target_member,
):
    """刻印专业模式：判断免费获得的装备是否应该直接提炼成信用点。

    专业模式（配置项"刻印专业模式"）只改一件事：免费获得（非商店购买）的
    装备在判定为"不分配给目标主战员"时，不再转交给其他主战员，而是直接提炼
    （分解）换信用点。转给其他主战员会直接占掉对方的装备位，目标名/目标刻印
    一旦装到别人身上就再也拿不回来了，所以专业模式下宁可换成信用点。

    以下条件必须全部满足才返回 True：

    - engraving_mode: 已开启"刷装备刻印"（专业模式依附于刻印模式）；
    - pro_mode: 已开启"刻印专业模式"；
    - 不是购买页面：商店购买的装备价格/刻印不合适时走"取消"，不存在提炼；
    - slot_evaluated: 确实完成了刻印进展判定（找到了可比较的主战员）。
      没有可比较对象时判定结果没有意义，保守回退到原有分配逻辑；
    - assign_to_target_member 为 False: 候选装备不能推进"目标名+目标刻印"
      的进展（见 should_assign_to_target_member 的判定表）。

    Returns:
        bool: True 表示应点击"提炼"，把该装备换成信用点。
    """
    if not (engraving_mode and pro_mode):
        return False
    if is_purchase_page:
        return False
    if not slot_evaluated:
        return False
    return not assign_to_target_member


def name_only_candidate_slot(candidate_name, slot_states, engraving_keywords):
    """找出"这件不带刻印的候选装备"能给哪个槽位补上名字（没有则返回 None）。

    商店在"刷装备刻印"模式下只点开带刻印角标的装备，
    角标同时也是判定"候选是否自带刻印"的正向证据。但槽位处在"刻印已经拿到、
    只是装备名还不对"时，能推进它的候选恰恰是一件**命中目标名、自身不带任何
    刻印**的装备——它走"候选不带刻印"的继承路径（覆盖后保留槽位原有刻印，
    安全换名），
    而在货架上它就是没有角标的那一类。不给它放行，这个槽位就永远补不上名字。

    槽位当前没有任何目标刻印（空槽，或描述里没有目标刻印关键词）时不放行：
    覆盖过去没有刻印可继承，装上去只是把名字填上，等于白花信用点。

    Args:
        candidate_name: 货架上候选装备的名字（OCR 结果）。
        slot_states: 三个槽位各自的 (当前装备名, 当前装备描述, 目标名列表)。
        engraving_keywords: "装备刻印优先级"配置的有序列表。

    Returns:
        int | None: 能补上名字的槽位下标（0 起算）；没有则 None。
    """
    for slot, (current_name, current_description, target_names) in enumerate(
        slot_states
    ):
        if not matches_target_name(candidate_name, target_names):
            continue
        if matches_target_name(current_name, target_names):
            continue
        if best_engraving_rank(current_description, engraving_keywords) is None:
            continue
        return slot
    return None


# 刻印专业模式的逃脱检查点固定在「第二个Boss房间前」：
# 层下标从 0 开始，1 表示第二层（地图二），第二层的关底Boss就是第二个Boss。
# 只有走到「当前路线页面上已经没有普通节点、即将进入最终Boss」时才检查，这样
# 地图二上的所有节点（含商店）都已经走完、装备也全部选完，再判断本局值不值得
# 继续打第二个Boss。
PRO_MODE_ESCAPE_LAYER = 1


def _describe_slot_missing(slot, name, description, target_names, engraving_keywords):
    """拼一段"这个装备位差什么"的人类可读说明，供日志使用。"""
    if not slot_allows_any_name(target_names) and not matches_target_name(
        name, target_names
    ):
        name_text = _normalize(name) or "空"
        target_text = "、".join(
            _normalize(target_name) for target_name in target_names if _normalize(target_name)
        ) or "（没配置）"
        return (
            f"{slot + 1}号位装备名「{name_text}」不在目标名列表"
            f"（{target_text}）里"
        )
    if not has_target_engraving(description, engraving_keywords):
        return f"{slot + 1}号位「{_normalize(name)}」没有命中任何目标刻印"
    # 名字命中目标名、刻印也命中时 is_slot_completed 已判为达标，正常走不到
    # 这里；保留一条保底说明，避免日志里出现空字符串。
    return f"{slot + 1}号位「{_normalize(name)}」未达标"


def slots_all_completed(
    current_states,
    target_names_per_slot,
    engraving_keywords,
):
    """刻印专业模式：判断三个装备位是否都拿到了"目标名+任意一项目标刻印"。

    这是"刻印专业模式逃脱"的达标标准：三个装备位全部满足
    `is_slot_completed` 才算达标，只要有一个不满足就认为本局不值得继续。
    具体的达标细节（哪个装备位差什么）通过第二个返回值给出，用于日志，
    方便定位是配置写错了还是装备确实没刷到。某个装备位的目标名列表留空时，
    该位"不限装备名"，只看刻印是否命中（见 slot_allows_any_name）。

    Args:
        current_states: 每个装备位当前的 (名称, 描述)，按 1/2/3 号位顺序。
        target_names_per_slot: 每个装备位的目标名列表（"装备X号位优先级"）。
        engraving_keywords: 目标刻印关键词列表，列表顺序即优先级高低。

    Returns:
        (bool, list[str]): (三个装备位是否全部达标, 未达标装备位的说明列表)
    """
    missing = []
    for slot in range(3):
        if slot < len(current_states):
            name, description = current_states[slot]
        else:
            name, description = "", ""
        target_names = (
            target_names_per_slot[slot] if slot < len(target_names_per_slot) else []
        )
        if is_slot_completed(name, description, target_names, engraving_keywords):
            continue
        missing.append(
            _describe_slot_missing(
                slot, name, description, target_names, engraving_keywords
            )
        )
    return not missing, missing


def is_pro_mode_escape_check_point(layer_index, is_final_boss):
    """刻印专业模式：判断当前路线页面是否到了「第二个Boss房间前」的检查点。

    检查点固定在**第二个Boss（第二层/地图二的关底Boss）房间之前**：只有当路线
    选择页面上已经识别不到任何普通节点、即将进入关底Boss，且当前正好在第二层
    时才检查。把检查点放在这里有两个好处：

    - 地图二上的所有节点（包括商店）都已经走完，商店里的装备也已经全部选完，
      再检查才不会漏掉刚在商店里买到的那件装备；
    - 检查完立刻决定是退出逃脱还是暂停（不达标才逃脱，达标或找不到「退出」
      按钮都暂停，见 `decide_pro_mode_check_point_action`），满足「一定要在
      第二个Boss房间前做判断」的要求。

    Args:
        layer_index: 当前所处层数，0 表示第一层，1 表示第二层（地图二），
            与 node_status["pass_final_boss_count"] 一致。
        is_final_boss: 当前路线页面是否只剩下最终Boss节点（没有识别到任何
            普通节点）。

    Returns:
        bool: True 表示现在就该检查：不达标就退出重刷，达标或找不到「退出」
            按钮都暂停程序等人工处理（都不会自动进入最终Boss房间）。
    """
    return layer_index == PRO_MODE_ESCAPE_LAYER and bool(is_final_boss)


# 刻印专业模式在"第二个Boss房间前"检查点上的三种走向，由
# decide_pro_mode_check_point_action 给出，utils 侧再据此决定具体动作。
PRO_MODE_ACTION_STOP_FOR_REVIEW = "stop_for_review"
PRO_MODE_ACTION_ESCAPE = "escape"
PRO_MODE_ACTION_PAUSE_NO_EXIT = "pause_no_exit"


def decide_pro_mode_check_point_action(completed, exit_box_found):
    """刻印专业模式：第二个Boss房间前的检查点该往哪走。

    达标标准见 `slots_all_completed`（三个装备位都拿到"目标名+任意一项目标
    刻印"）。达标后**不再自动进入最后的Boss房间**：立即停止程序把画面交给人工
    检查；不达标则点「退出」+「逃脱」重开一局。

    不达标且找不到「退出」按钮时暂停程序等待人工处理，不进入Boss；
    这样可以避免让不达标的一局继续打通关。

    Args:
        completed: 三个装备位是否全部达标。
        exit_box_found: 右上角有没有找到「退出」按钮（达标时根本不去找）。

    Returns:
        str: 三个常量之一。`PRO_MODE_ACTION_PAUSE_NO_EXIT` 只在"不达标、又找不到
        「退出」按钮"时出现；`_find_run_exit_box` 改成固定坐标兜底之后基本不会
        走到，留着当最后一道防御。
    """
    if completed:
        return PRO_MODE_ACTION_STOP_FOR_REVIEW
    if exit_box_found:
        return PRO_MODE_ACTION_ESCAPE
    return PRO_MODE_ACTION_PAUSE_NO_EXIT

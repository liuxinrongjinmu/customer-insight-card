"""把已经算好的结论，按「某一次具体行动」重新组织。

这个模块一行模型都不调。原因很硬：拜访简报、方案准备和交接说明要能贴在会议室墙上，
每一句都要挂得住依据。让模型来写，它就会写出「客户对价格敏感、建议强调价值」
这种听起来对、但在这个客户身上找不到出处的句子——那正是需求里第六条痛点
「用户不敢完全相信系统结论」要防的东西。

所以这里只做四件事：
1. 从已有事实里**挑**（挑哪三条承诺、哪两条坑）；
2. 按固定模板**拼**（谁、什么时候、欠什么）；
3. 缺什么就**说缺**（决策链少一环就写少一环，不替客户编一个）；
4. 每个产出物都带回依据事实的 key，界面上点得到原文。

四个产出物：
    account_strip   档案条 —— 这个客户值多少钱、合作多久
    decision_chain  决策链 —— 谁点头、谁在挡、还缺谁
    collect_todos   今天要做的事 —— 跨客户的待办
    visit_brief     拜访简报 / 方案准备 / 交接说明 —— 能直接发给别人的东西
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# 角色与态度的人话。枚举值对销售没用，「把关人」「态度支持」才有用。
ROLE_CN = {
    "decision_maker": "决策人",
    "champion": "内应",
    "influencer": "影响者",
    "user": "使用者",
    "blocker": "阻力",
    "gatekeeper": "把关人",
}
ATT_CN = {"supportive": "支持", "neutral": "中立", "resistant": "反对", "unknown": "态度未知"}
INFLUENCE_CN = {"high": "话语权重", "medium": "一般", "low": "有限"}

# 一条完整决策链至少要有这三环。少哪环，就是下一次见面要补的功课。
CHAIN_REQUIRED = ("decision_maker", "champion", "gatekeeper")

KIND_CN = {
    "commitment_overdue": "逾期承诺",
    "commitment_imminent": "临期承诺",
    "commitment_no_due": "承诺无期限",
    "conflict_unresolved": "口径待确认",
    "deadline_imminent": "节点临近",
    "deadline_missed": "节点已过",
    "customer_side_deadline": "客户侧节点",
    "concern_blocker": "阻塞性顾虑",
    "missing_decision_maker": "决策链缺口",
    "single_thread": "单线联系",
    "competitor_ahead": "竞品领先",
    "no_competitor_info": "竞品信息空白",
    "stale_need": "情报过期",
    "silence": "长时间失联",
    "thin_profile": "信息不足",
    "need_blocking_requirement": "硬门槛待确认",
    "inferred_need_unconfirmed": "需求待确认",
    "incumbent_pain": "存量痛点",
    "need_voiced_as_complaint": "抱怨里的需求",
}

# 排序用：数字越小越靠前。逾期比临期急，口径矛盾比节点急。
KIND_ORDER = {
    "commitment_overdue": 0,
    "deadline_missed": 1,
    "conflict_unresolved": 2,
    "commitment_imminent": 3,
    "deadline_imminent": 4,
    "concern_blocker": 5,
    "missing_decision_maker": 6,
    "single_thread": 7,
    "competitor_ahead": 8,
}

# 「要做的事」还是「要补的数据」——这条分界线决定这一页值不值得每天打开。
#
# 判据只有一条：**这条做完之后，是客户那边有变化，还是只是系统终于知道了一件事。**
#   打电话兑现承诺 → 客户那边有变化（do）
#   把客户的规模补进档案 → 系统知道了，客户毫无感知（fill）
#
# 之前混在一起排，实测 94 件里 52 件是 fill（55%）。用户打开看到的第一屏
# 有一半是「请补充档案」，他会得出一个正确结论：这玩意儿在给我派活。
#
# 认的是规则 id，不是 P 级别：同一条规则永远落在同一区，
# 不会因为客户不同、日期不同而今天算 do、明天算 fill。
BUCKET_FILL = {
    "thin_profile",               # 档案空着，得去补规模/合同
    "stale_need",                 # 情报过期，得重新问一遍
    "conflict_unresolved",        # 两条口径打架，得去确认哪个对
    "need_blocking_requirement",  # 「不考虑就不考虑」，得问清是不是硬门槛
    "inferred_need_unconfirmed",  # 系统推出来的需求，得让人点一下确认
    "commitment_no_due",          # 承诺没日期，得问清哪天兑现
    "no_competitor_info",         # 一条竞品记录都没有，得先把这个空格填上
}

# 补数据也有先后。空档案比确认一条需求更值得先补——
# 前者让整张卡立不起来，后者只影响一条事实。
FILL_ORDER = {
    "thin_profile": 0,
    "stale_need": 1,
    "conflict_unresolved": 2,
    "commitment_no_due": 3,
    "need_blocking_requirement": 4,
    "inferred_need_unconfirmed": 5,
    # 竞品空白排最后：它天然是 P2，又不影响任何一条已有结论。
    "no_competitor_info": 6,
}

# 顾虑严重度的「人话」。枚举值对销售没用，「客户明确表达过不满」才有用。
SEV_CN = {"high": "客户明确表达过不满", "medium": "提过，程度一般", "low": "轻微提过"}

#: 三种简报各自有哪些块 id。
#:
#: 这三个元组**必须**与下面三个 builder 里写的 `id` 一致——
#: `tests/test_intents.py` 会拿实际产出的块 id 来比对，少一个就红。
#: 为什么需要这份常量：待办深链要落到具体的块上，而「落点不存在」不会报错，
#: 只会让抽屉打开后停在顶部——用户点了「逾期承诺」，看到的是「一句话现状」。
BLOCK_IDS: dict[str, tuple[str, ...]] = {
    "visit": ("summary", "recent", "owed", "pitfalls",
              "competition", "unsure", "questions"),
    "proposal": ("problem", "needs", "competition", "risks", "evidence"),
    "handover": ("who", "wip", "pitfalls", "people", "first48", "unknown"),
}


def _alive(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """还在台上的事实。撤回的不算，过期的也不算。

    交接和简报都是给「现在要动手」用的，把过期情报写进去等于误导。
    """
    return [f for f in facts
            if f.get("status") == "active" and not f.get("retracted") and not f.get("expired")]


def _days_overdue(f: dict[str, Any], today: date) -> int | None:
    """逾期几天。界面上的「逾期 N 天」要有确切数字，不能只写「已逾期」。"""
    due = (f.get("payload") or {}).get("due_date")
    if not due:
        return None
    try:
        return (today - date.fromisoformat(due)).days
    except (ValueError, TypeError):
        return None


# 硬门槛的说法。命中就说「这条是不是不能碰」，问法比断言更合适——
# 系统没见过合同，不能替客户把话定死。
VETO_WORDS = ("就不", "否则", "必须", "前提", "不考虑", "不能接受", "一票否决", "红线")


def _veto_hit(text: str) -> bool:
    return any(w in (text or "") for w in VETO_WORDS)


def _years_since(since: str, today: date) -> str:
    if not since:
        return "—"
    try:
        y, m = (int(x) for x in since.split("-")[:2])
    except (ValueError, TypeError):
        return "—"
    months = (today.year - y) * 12 + (today.month - m)
    if months < 0:
        return "未开始"
    if months < 12:
        return f"{months} 个月"
    return f"{months // 12} 年 {months % 12} 个月" if months % 12 else f"{months // 12} 年"


def _wan(v: Any) -> str:
    """金额的那一格：180.0 写成 180，不写成 180.0。

    最小档案的金额框是人手打进来的，过一遍 `float()` 之后整数会带小数点；
    档案条上写「180.0 万」会让人以为系统算出了什么精度，其实什么都没算。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return str(int(f)) if abs(f - int(f)) < 1e-9 else f"{f:g}"


# ---------------------------------------------------------------------------
# 档案条：回答「这个客户是谁、值多少」
# ---------------------------------------------------------------------------

def account_strip(profile: dict[str, Any] | None, today: date,
                  view: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """档案条。主数据优先，没有主数据就从记录里推——**两头都不编**。

    需求原话第一句是「这个客户是谁」。只靠抽取出来的事实答不了这句：
    事实说的是「他提过什么」，不是「他是谁、合作多久、值多少钱」。
    这一条把主数据摆到卡片最上面，让销售三秒钟判断值不值得花时间。

    33 个存量客户没有主数据档案。原来这里是 `return []`，卡片顶上就是一排
    空行——「空着比编一个『合作 3 年』诚实」，这话没错，但**空着不等于只能空着**：
    记录里有真实的东西可报（聊过几次、第一次是什么时候、近三个月有没有联系），
    这些不用编。所以分两层：

    - 有主数据 → 只画主数据，`src="profile"`。
    - 没有主数据 → 画从记录推出来的统计，`src="derived"`，界面必须标出来
      是推算的。**「首次记录」不叫「合作起始」**：最早一条记录只说明我们最早
      什么时候记过这家，合作可能早就开始了，也可能压根还没开始。
    """
    main = _main_blocks(profile, today)
    if main:
        return main
    if not view:
        return []
    return fact_overview(_alive(view.get("facts", [])),
                         view.get("records", []), today)


def _main_blocks(profile: dict[str, Any] | None, today: date) -> list[dict[str, str]]:
    """主数据那几格。每格标 `src="profile"`，前端据此区分是不是推算的。"""
    if not profile:
        return []
    out: list[dict[str, str]] = []

    if profile.get("scale"):
        out.append({"label": "规模", "value": profile["scale"], "hint": "",
                    "src": "profile"})

    if profile.get("since"):
        out.append({"label": "合作", "value": _years_since(profile["since"], today),
                    "hint": f"{profile['since']} 起", "src": "profile"})

    opp = profile.get("open_opportunity") or {}
    # 只填了金额没填名称也要显示——最小档案只要求金额，卡着名称不给画，
    # 用户填完会以为没存上（P1-5 的整个前提就是「四个字段就够用」）。
    if opp.get("name") or opp.get("amount"):
        wan = _wan(opp.get("amount"))
        name = (opp.get("name") or "").strip()
        out.append({
            "label": "在办商机",
            "value": f"{wan} 万" if wan else "金额未定",
            "hint": " · ".join(x for x in (name, opp.get("stage")) if x) or "名称与阶段未填",
            "src": "profile",
        })

    contract = profile.get("contract") or {}
    if contract.get("expires_on"):
        try:
            days = (date.fromisoformat(str(contract["expires_on"])) - today).days
        except (ValueError, TypeError):
            # 认不出的日期不能折算成 0 天——那会在界面上写成「0 天后到期」，
            # 把一条脏数据渲染成一条紧急情报。照原样摆出来，让人看见它脏。
            out.append({"label": "合同", "value": "到期日认不出",
                        "hint": str(contract["expires_on"]), "src": "profile"})
        else:
            out.append({
                "label": "合同",
                "value": f"{days} 天后到期" if days >= 0 else f"已过期 {-days} 天",
                "hint": contract["expires_on"],
                "src": "profile",
            })

    svc = profile.get("tickets_90d") or {}
    if svc.get("n") is not None:
        out.append({
            "label": "近 90 天客诉",
            "value": f"{svc['n']} 次",
            "hint": f"平均 {svc.get('avg_hours', '—')} 小时到场" if svc.get("n") else "无",
            "src": "profile",
        })

    # 关系与偏好：主观但有用，让销售三秒知道「该怎么开口」
    REL_CN = {"warm": "热", "neutral": "正常", "cold": "冷"}
    if profile.get("relationship"):
        out.append({"label": "关系",
                    "value": REL_CN.get(profile["relationship"], profile["relationship"]),
                    "hint": profile.get("comm_pref", ""), "src": "profile"})
    if profile.get("products"):
        out.append({"label": "已购", "value": profile["products"], "hint": "",
                    "src": "profile"})

    return out


def fact_overview(alive: list[dict[str, Any]],
                  records: list[dict[str, Any]], today: date) -> list[dict[str, str]]:
    """没有主数据档案时，从事实与记录派生概览。不编，只报真实统计。

    存量客户池里 33 个客户没有主数据档案——规模、合同、商机都是空的。
    空着是诚实，但「规模未知、合同未知」对销售没用。折中方案是从记录里
    能算出来的真实数字替代，而且**只放三种**：

    | 格 | 值 | 为什么是它 |
    |---|---|---|
    | 记录 | 几条 + 覆盖了什么 | 「聊过三次、只聊到需求」比「暂无档案」有用 |
    | 首次记录 | 最早那条的日期 | 判「这客户是老关系还是刚认识」的门槛 |
    | 近 90 天 | 这段时间几条 | 0 条就是沉默客户——这条最该被看见 |

    「首次记录」这一格的措辞是刻意的：它**不是「合作起始」**。最早一条记录
    只说明我们最早什么时候记过这家，合作可能三年前就开始了，也可能还没开始。
    拿它冒充合作起始，正是这个项目最不该犯的错。

    每格都带 `src="derived"`，界面据此标出「这是推算的，不是主数据」。
    """
    if not alive and not records:
        return []

    # 覆盖的事实类型
    TYPE_CN_SHORT = {"need": "需求", "concern": "顾虑", "commitment": "承诺",
                     "stakeholder": "关键人", "competition": "竞争",
                     "timeline": "节点", "risk_signal": "风险"}
    types = sorted({f.get("fact_type") for f in alive if f.get("fact_type")})
    type_str = "、".join(TYPE_CN_SHORT.get(t, t) for t in types[:5])

    # 记录日期（记录可能有空日期，不能拿空串当「最早」）
    dates = sorted(d[:10] for d in (r.get("date") or "" for r in records) if d)
    out = [{"label": "记录", "value": f"{len(records)} 条",
            "hint": f"{len(alive)} 条有效事实 · 覆盖 {type_str or '—'}",
            "src": "derived"}]

    if dates:
        newest = dates[-1]
        out.append({"label": "首次记录", "value": dates[0],
                    "hint": "最早一条记录，不等于合作起始", "src": "derived"})
        since = (today - timedelta(days=90)).isoformat()
        recent = sum(1 for d in dates if since <= d <= today.isoformat())
        out.append({"label": "近 90 天",
                    "value": f"{recent} 条",
                    "hint": "这段时间没联系过" if not recent else f"最近一次 {newest}",
                    "src": "derived"})
    return out


# ---------------------------------------------------------------------------
# 决策链：回答「谁点头、谁在挡、还缺谁」
# ---------------------------------------------------------------------------

def decision_chain(view: dict[str, Any]) -> dict[str, Any]:
    """关键人 → 一条链，而不是五张卡。

    数据本来就有（`role_in_deal` / `attitude` / `influence`），但界面上它们
    只是「关键人 · 5」这一组里的五张卡片，读完仍然不知道「这单谁说了算」。
    这一条把它们按角色排成一条链，并且**明确指出缺哪一环**——
    「还没摸到决策人」比「有 5 个关键人」有用得多，前者是下一步动作。
    """
    people = [f for f in _alive(view.get("facts", [])) if f["fact_type"] == "stakeholder"]

    order = {"decision_maker": 0, "champion": 1, "gatekeeper": 2, "influencer": 3,
             "blocker": 4, "user": 5}
    people.sort(key=lambda f: (order.get((f.get("payload") or {}).get("role_in_deal"), 9),
                               -f.get("conf", 0)))

    nodes = []
    for f in people:
        p = f.get("payload") or {}
        name = (p.get("name") or "").strip()
        title = (p.get("title") or "").strip()
        role_cn = ROLE_CN.get(p.get("role_in_deal"), "角色未知")
        # 只知职务：知道「谁签字」，不知道「是谁」。这不是缺数据，是一条
        # 信息量刚好少一半的事实——显示成「未具名」会把两者混为一谈：
        # 「从来没问到」和「问到了但只知道位置」的下一步动作完全不同。
        title_only = bool(p.get("identified_as") == "title") or (not name and bool(title))
        if name:
            label = f"{name}（{title or role_cn}）"
        elif title_only:
            label = f"{title}（仅职务，未识别人）"
        else:
            label = f"未具名（{role_cn}）"
        nodes.append({
            "key": f["key"],
            "name": name or "未具名",
            "title": title,
            "title_only": title_only,
            "label": label,
            "role": p.get("role_in_deal") or "unknown",
            "role_cn": role_cn,
            "attitude": p.get("attitude") or "unknown",
            "attitude_cn": ATT_CN.get(p.get("attitude"), "态度未知"),
            "influence_cn": INFLUENCE_CN.get(p.get("influence"), ""),
            "span": f.get("span") or "",
        })

    have = {(f.get("payload") or {}).get("role_in_deal") for f in people}
    missing = [{"role": r, "role_cn": ROLE_CN[r],
                "hint": {"decision_maker": "还没接触最终签字的人，方案再好也可能卡在门口",
                         "champion": "没有内部帮你说话的人，推进全靠自己跑",
                         "gatekeeper": "不知道谁在把关技术/合规，容易在评标阶段被刷"}[r]}
               for r in CHAIN_REQUIRED if r not in have]

    # 「知道谁签字、不知道是谁」要单独说。上面那句「签字的人已经接触到了」
    # 在只有职务时是假的——接触到的是位置，不是人。
    bare_titles = [n for n in nodes if n["title_only"]]
    bare_names = "、".join(dict.fromkeys(n["title"] for n in bare_titles if n["title"]))
    dm_is_bare = any(n["title_only"] and n["role"] == "decision_maker" for n in nodes)

    # 一句话结论：先说清楚的，再说缺的。两句都有依据，不推测态度以外的动机。
    if not nodes:
        note = "一个关键人都没记录。下次沟通先问清：谁签字、谁评估、谁会用。"
    elif "decision_maker" not in have:
        note = f"已有 {len(nodes)} 位联系人，但还没摸到决策人——这是当前最大的推进风险。"
    elif len(bare_titles) == len(nodes):
        # 整条链只有位置，没有一个具体的人。这时候不能说「见到的就是决策人」：
        # 我们没见过他，只是知道最后要他签字。
        note = (f"决策链上只有位置，还没有人：{bare_names}要签字这件事已经确认，"
                "但还没接触到具体的人——这是位置的确认，不是关系的建立。")
    elif len(nodes) == 1:
        note = (f"见到的就是决策人（{nodes[0]['label']}），但只认识他一个——"
                "单线联系，他一变动这条线就断了。下次争取再摸两个人。")
    else:
        blockers = [n for n in nodes if n["attitude"] == "resistant"]
        if blockers:
            note = (f"决策链基本成形（{len(nodes)} 人），但 {blockers[0]['label']} 持反对态度，"
                    "见面要单独约。")
        elif dm_is_bare:
            note = (f"决策链基本成形（{len(nodes)} 人），但签字那一环只记到职务、还没接触到人——"
                    "这是位置的确认，不是关系的建立。")
        else:
            note = f"决策链基本成形（{len(nodes)} 人），签字的人已经接触到了。"

    # 只知职务的那些不是决策人的（比如「采购总监在把关」），单独点名。
    soft = [n for n in bare_titles if n["role"] != "decision_maker"]
    if soft:
        names = "、".join(dict.fromkeys(n["title"] for n in soft if n["title"]))
        note += f"另外{names}只记到职务，还没接触到具体的人。"

    return {"nodes": nodes, "missing": missing, "note": note}


# ---------------------------------------------------------------------------
# 跨客户待办：回答「今天该打哪几个电话」
# ---------------------------------------------------------------------------

# 一天最多给几个「今日」名额。给多了等于没给：
# P0 占到 26 件时，用户看一眼就知道做不完，然后一个都不做。
DEFAULT_BUDGET = 3


def collect_todos(views: list[dict[str, Any]], today: date,
                  limit: int = 200, budget: int = DEFAULT_BUDGET) -> dict[str, Any]:
    """把每个客户的建议摊平，分成「要做的事」和「要补的数据」两区再排序。

    为什么要有这一页：销售的一天不是从「选一个客户」开始的，是从「今天有哪几个电话
    必须打」开始的。六个客户时挨个点还受得了，三十个客户时就必须先有一张总表。

    39 个客户和 6 个客户不是同一道题：真正要回答的是「今天只有 8 小时，
    先动哪三个」。所以这里不只排序，还要把「重点客户以外还有多少」说出来——
    说不出来，用户就会以为没列出来的那些不存在。

    **为什么要分两区**：实测 94 件待办里 52 件是「请补充档案」这类，
    混在打电话的清单里排，等于每天先给用户看一屏录入任务。
    分成两区之后，第一区是「今天要打的电话」，第二区是「系统缺的信息」，
    后者可以攒着一次补，不必占用今天。

    **为什么要预算**：P0 占到 26 件（28%）时，P0 就等于没有。一天只给
    `budget` 个「今日」名额（默认 3），排在后面的降成「本周」——
    这不是隐藏它们，是把「今天做不完」这件事提前说出来。
    """
    items: list[dict[str, Any]] = []
    for v in views:
        pool = bool(v.get("pool"))
        for a in v.get("actions", []):
            rules = a.get("rules") or []
            kind = next((r for r in rules if r in KIND_CN), rules[0] if rules else "other")
            items.append({
                "cid": v["id"],
                "cname": v["name"],
                "stage": v.get("stage") or "",
                "p": a["p"],
                "title": a["title"],
                "why": a.get("why") or "",
                "basis": a.get("basis"),
                # 「已办」要能对回某一条建议，所以身份原样带过来。
                "aid": a.get("aid"),
                "can_fulfill": bool(a.get("can_fulfill")),
                "kind": kind,
                "kind_cn": KIND_CN.get(kind, "待办"),
                "bucket": "fill" if kind in BUCKET_FILL else "do",
                "n_p0": v.get("n_p0", 0),
                "pool": pool,
                # 金额与 impact 一起带过来：列表每行都要能回答「这值多少」，
                # 而这一屏没有档案条可看（档案条只在单个客户的卡片上）。
                "amount": a.get("amount") or 0,
                "amount_cn": a.get("amount_cn") or "金额未定",
                # 显式判 None：`0 or 50` 会得到 50，把「真算出来是 0」悄悄改掉。
                # 现在 0 分不会出现（最低档 20 分），但这类隐式兜底不该留在排序键上。
                "impact": a["impact"] if a.get("impact") is not None else 50.0,
                # 逾期老化档位。"" 表示这条建议不涉及逾期，"stale" 要求归因，
                # "historic" 超过 90 天没人处理、要单列成历史欠账。
                "aging": a.get("aging") or "",
            })

    def sort_key(it: dict[str, Any]) -> tuple:
        # 两区各自排，且「要做的事」整体排在前：列表第一行应该是今天要打的电话，
        # 不是「请补充档案」。补数据类多半是 P0，而 P0 只说明「不做会出事」，
        # 不说明「今天做得完」。
        if it["bucket"] == "fill":
            # 补数据区**不**按金额排。这一区的动作是「去把信息问回来」，
            # 花的是同一份时间，跟哪一单值多少钱没关系；按金额排反而会让
            # 大单的档案反复插到前面，小单永远补不上。
            return (1, FILL_ORDER.get(it["kind"], 9), 1 if it["pool"] else 0, it["cname"])
        p = {"P0": 0, "P1": 1, "P2": 2}.get(it["p"], 3)
        # 同一个 P 级别里，先按金额（impact）排，再看事情的性质，最后才按客户名。
        #
        # 为什么把金额插在 kind 前面：kind 顺序编码的是「哪类事情更急」，
        # 但在同一档里，「200 万的逾期承诺」和「5 万的逾期承诺」是同一类事、
        # 同一个紧急度，唯一的区别就是值多少钱——而销售的一天只有 8 小时，
        # 这个区别恰恰是决定先打哪个电话的那一个。
        return (0, p, -float(it["impact"]),
                KIND_ORDER.get(it["kind"], 9), 1 if it["pool"] else 0, it["cname"])

    items.sort(key=sort_key)

    # 今日名额只发给重点客户的「要做的事」。存量客户的待办本来就写着
    # 「不急着今天动」，再给它们占名额，等于把今天的 3 格分给了不该动的客户。
    # budget<=0 表示不设上限：演示时想一次看全，比背一个默认值有用。
    #
    # 历史欠账（逾期 >90 天且没人归因）**一律不占今日名额**，也不进「本周」。
    # 它不是「这周记得做」，它是「这件事得有人做个决定」——按周排会一直排下去，
    # 排 116 天就是现在这个问题。单独一栏，逼一次表态。
    slots = int(budget) if int(budget) > 0 else len(items)
    for it in items:
        if it["aging"] == "historic":
            it["when"] = "historic"
            continue
        it["when"] = "week"
        if it["bucket"] == "do" and not it["pool"] and slots > 0:
            it["when"] = "today"
            slots -= 1

    def bucket(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "n_items": len(rows),
            "n_customers": len({r["cid"] for r in rows}),
            "n_p0": sum(1 for r in rows if r["p"] == "P0"),
            "n_overdue": sum(1 for r in rows if r["kind"] == "commitment_overdue"),
        }

    focus = [i for i in items if not i["pool"]]
    rest = [i for i in items if i["pool"]]
    # 页眉只讲重点客户。把 33 个存量客户一起算进去，会得到「33 个客户有 60 件待办」
    # 这种数字——它没错，但它不指导任何行动。
    shown = focus or items
    overdue = sum(1 for i in shown if i["kind"] == "commitment_overdue")
    conflict = sum(1 for i in shown if i["kind"] == "conflict_unresolved")
    imminent = sum(1 for i in shown if i["kind"] in ("commitment_imminent", "deadline_imminent"))

    n_do = sum(1 for i in shown if i["bucket"] == "do")
    n_fill = sum(1 for i in shown if i["bucket"] == "fill")
    n_today = sum(1 for i in shown if i["when"] == "today")
    n_historic = sum(1 for i in shown if i["when"] == "historic")
    # 页眉那句要说整张表，不只是重点客户：历史欠账目前恰好落在存量客户里
    # （泰盛重工那条拖了 116 天）。只报重点客户的数，用户就永远看不到它，
    # 而「看不见」和「不存在」在页面上长得一模一样。
    n_historic_all = sum(1 for i in items if i["when"] == "historic")

    headline = (f"{len({i['cid'] for i in shown})} 个客户有 {len(shown)} 件待办，"
                f"其中逾期承诺 {overdue} 件、口径待确认 {conflict} 件、"
                f"节点/承诺临期 {imminent} 件。")
    # 把两区和预算直接写进第一句。用户先读到的是「今天做几件」，
    # 而不是一个让他自己去数的大数字。
    if n_do or n_fill:
        if n_today and n_today < n_do:
            headline += (f"要今天动手的 {n_do} 件里，先做排在前面的 {n_today} 件；"
                         f"另有 {n_fill} 件是补数据，攒一次一起补就行。")
        else:
            # 上限被关掉（budget<=0）时不能写「先做排在前面的 14 件」——
            # 那不是预算，那是把列表原样念了一遍。
            headline += (f"要今天动手的 {n_do} 件，按急排好了；"
                         f"另有 {n_fill} 件是补数据，攒一次一起补就行。")
        if n_historic_all:
            # 必须单独说一句。不说的话，用户会以为那几条「不见了」；
            # 而它们其实一直都在，只是系统终于不再每天喊一遍。
            headline += (f"另有 {n_historic_all} 件拖过 90 天没人表态，"
                         "单独列成「历史欠账」——不占今天的名额，但要有人决定还发不发。")
    if rest:
        headline += (f"另有 {len({i['cid'] for i in rest})} 个存量客户 {len(rest)} 件，"
                     "不急着今天动，但别当它们不存在。")

    return {
        "as_of": today.isoformat(),
        "items": items[:limit],
        "n_total": len(shown),
        "n_customers": len({i["cid"] for i in shown}),
        "n_overdue": overdue,
        "n_do": n_do,
        "n_fill": n_fill,
        "n_today": n_today,
        "n_week": n_do - n_today - n_historic,
        "n_historic": n_historic,
        "n_historic_all": n_historic_all,
        "budget": max(0, int(budget)),
        "n_accounts": len(views),
        "n_focus_accounts": len(views) - sum(1 for v in views if v.get("pool")),
        "n_pool_accounts": sum(1 for v in views if v.get("pool")),
        "by_pool": {"focus": bucket(focus), "pool": bucket(rest)},
        # 一句人话：今天先干什么。数字全部来自上面那张列表，不另算。
        "headline": headline,
    }


# ---------------------------------------------------------------------------
# 拜访简报 / 交接说明：回答「这一趟去干什么」
# ---------------------------------------------------------------------------

def _questions(view: dict[str, Any], chain: dict[str, Any], today: date) -> list[str]:
    """建议问的三个问题。

    每一条都从已有事实长出来，不是通用话术。宁可只给两条，也不凑第三条——
    凑出来的那一句，客户一听就知道你没做功课。
    """
    out: list[str] = []
    alive = _alive(view.get("facts", []))

    # 1) 硬门槛：说「不…就不…」的需求
    for f in alive:
        if f["fact_type"] == "need" and _veto_hit(f.get("statement", "")):
            out.append(f"「{f['statement'][:26]}」这条是硬门槛还是可以谈？")
            break

    # 2) 最硬的顾虑：问它现在怎么解决
    concerns = [f for f in alive if f["fact_type"] == "concern"]
    concerns.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}.get(
        (f.get("payload") or {}).get("severity"), 3), -f.get("conf", 0)))
    if concerns and len(out) < 3:
        # 引用原文而不是系统的转述：见面时照着自己的复述念，客户会觉得你没听清
        quoted = concerns[0].get("span") or concerns[0]["statement"]
        out.append(f"「{quoted[:26]}」现在你们是怎么处理的？")

    # 3) 硬节点：问日期能不能动
    timelines = [f for f in alive if f["fact_type"] == "timeline"
                 and ((f.get("payload") or {}).get("target_date") or "") >= today.isoformat()]
    timelines.sort(key=lambda f: (f.get("payload") or {}).get("target_date") or "9999")
    if timelines and len(out) < 3:
        t = timelines[0]
        p = t.get("payload") or {}
        out.append(f"「{p.get('event') or t['statement'][:20]}」定在 {p.get('target_date')}，"
                   f"这个日期还有调整余地吗？")

    # 4) 决策链缺口：问还缺谁。这一条最该问，放在有缺口时才出现。
    if chain["missing"] and len(out) < 3:
        m = chain["missing"][0]
        out.append(f"{m['role_cn']}这一环还没接触到，能帮我引荐吗？")

    return out[:3]


def visit_brief(view: dict[str, Any], today: date) -> dict[str, Any]:
    """拜访前 5 分钟看的那张纸。

    结构按「见面顺序」排，不按事实类型排：
    先知道上次答应的事兑没兑现（开场就要提），再知道哪些坑不能踩（谈崩点），
    最后是这次要问的问题（带走什么）。
    """
    alive = _alive(view.get("facts", []))
    chain = decision_chain(view)

    # 上次答应的事：最近的三条承诺，带状态
    commits = [f for f in alive if f["fact_type"] == "commitment"]
    commits.sort(key=lambda f: (f.get("payload") or {}).get("due_date") or "9999")
    commit_items = []
    for f in commits[:3]:
        p = f.get("payload") or {}
        od = _days_overdue(f, today)
        if f.get("overdue") and od is not None:
            state = f"逾期 {od} 天"
        elif f.get("due_in") is not None and f.get("due_in") >= 0:
            state = f"{f['due_in']} 天后到期"
        else:
            state = "已关闭" if p.get("fulfillment") in ("waived", "done") else "日期未定"
        commit_items.append({
            "text": f["statement"],
            "meta": f"{state} · {f.get('source_cn') or ''} {f.get('source_date') or ''}".strip(" ·"),
            "key": f["key"],
            "bad": bool(f.get("overdue")),
        })

    # 别踩的坑：最硬的顾虑原话 + 风险信号
    concerns = [f for f in alive if f["fact_type"] == "concern"]
    concerns.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}.get(
        (f.get("payload") or {}).get("severity"), 3), -f.get("conf", 0)))
    pit_items = [{"text": f"「{f.get('span') or f['statement']}」",
                  "meta": f"{SEV_CN.get((f.get('payload') or {}).get('severity'), '提过')} · 原话直引",
                  "key": f["key"], "bad": True}
                 for f in concerns[:2]]

    # 待确认：矛盾 + 待确认档的事实
    unsure = [f for f in alive if f.get("conflicted") or f.get("tier") != "show"]
    unsure_items = [{"text": f["statement"],
                     "meta": "口径有冲突，见面问清" if f.get("conflicted") else "系统标了待确认",
                     "key": f["key"], "bad": bool(f.get("conflicted"))}
                    for f in unsure[:3]]

    # 竞品动向：客户提过谁、怎么说的
    comps = [f for f in alive if f["fact_type"] == "competition"]
    POS_CN = {"ahead": "我方领先", "behind": "我方落后", "tied": "势均力敌",
              "unknown": "客户未表态"}
    comp_items = [
        {"text": f"{(f.get('payload') or {}).get('competitor_name', '未具名对手')}："
                 f"{(f.get('payload') or {}).get('customer_view') or f['statement'][:40]}",
         "meta": f"我方{POS_CN.get((f.get('payload') or {}).get('our_position'), '未知')} · "
                 f"{f.get('source_cn') or ''} {f.get('source_date') or ''}".strip(" ·"),
         "key": f["key"],
         "bad": (f.get('payload') or {}).get('our_position') == 'behind'}
        for f in comps[:2]
    ]

    # 最近动态：最后一条记录的摘要——让人 3 秒接上话题
    records = view.get("records", [])
    recent_items = []
    if records:
        last = records[-1]
        recent_items = [{
            "text": f"{last.get('type_cn', '')}：{(last.get('text') or '')[:60]}",
            "meta": f"{last.get('date', '')} · {len(records)} 条记录中最近一条",
        }]

    # 每块带一个稳定 id。待办深链要落到「这一条建议对应哪一块」上，
    # 用标题当锚点不行——标题是文案，改一个字锚点就断，
    # 而断了之后不会报错，只会点进去停在抽屉顶部。
    blocks = [
        {"id": "summary", "h": "一句话现状",
         "items": [{"text": view.get("summary", ""), "meta": ""}]},
        {"id": "recent", "h": "最近动态", "items": recent_items or
         [{"text": "没有记录。这是第一次接触。", "meta": ""}]},
        {"id": "owed", "h": "上次答应的事", "items": commit_items or
         [{"text": "没有在办的承诺。这次可以直接谈下一步。", "meta": ""}]},
        {"id": "pitfalls", "h": "别踩的坑", "items": pit_items or
         [{"text": "没有记录到明确顾虑。", "meta": ""}]},
        {"id": "competition", "h": "竞品动向", "items": comp_items or
         [{"text": "没记录过竞品信息。没有消息不等于没有对手。", "meta": ""}]},
        {"id": "unsure", "h": "必须问清", "items": unsure_items or
         [{"text": "没有待确认的矛盾。", "meta": ""}]},
        {"id": "questions", "h": "建议问的问题",
         "items": [{"text": q, "meta": ""} for q in _questions(view, chain, today)]},
    ]
    return {
        "kind": "visit",
        "title": f"拜访简报 · {view['name']}",
        "subtitle": f"{view.get('industry', '')} · {view.get('stage', '')} · 基准日 {today.isoformat()}",
        "blocks": blocks,
        "chain": chain,
        "empty": not alive,
    }


def _one_line(s: str) -> str:
    """把多行压成一行。

    记录原文里常有换行（「上周发的方案您看过了吗？\\n最近在忙审计」）。
    Markdown 的列表项遇到换行就断了：后半句变成独立段落，
    粘到邮件里就是「一句话说了、下一句散落着」，读的人分不清哪条属于哪个标题。
    这里压成一行不是美化，是保住结构。
    """
    return " ".join(str(s).split())


def brief_markdown(brief: dict[str, Any]) -> str:
    """把简报转成能粘贴出去的 Markdown。

    为什么要有这个函数：简报的用途是「贴在会议室墙上」和「写进交接邮件」，
    但在这之前它只存在于一个抽屉里——用户能看，不能带走。截图不算带走：
    接手人拿到图片没法改一个字。

    刻意保持朴素：标题、二级标题、无序列表、一行出处说明。
    不做表格、不做加粗——粘到飞书、企微、Outlook 里都不会变形。
    """
    title = _one_line(brief.get("title") or "简报")
    lines: list[str] = [f"# {title}", ""]
    sub = _one_line(brief.get("subtitle") or "")
    if sub:
        lines += [sub, ""]

    for block in brief.get("blocks") or []:
        head = _one_line(block.get("h") or "")
        if not head:
            continue
        lines += [f"## {head}"]
        items = block.get("items") or []
        if not items:
            lines.append("- （无）")
        for it in items:
            text = _one_line(it.get("text") or "")
            if not text:
                continue
            meta = _one_line(it.get("meta") or "")
            lines.append(f"- {text}" + (f"（{meta}）" if meta else ""))
        lines.append("")

    lines.append("> 这份材料由已有事实与规则拼成，未经模型自由发挥；每条都能在原始记录里找到出处。")
    return "\n".join(lines).rstrip() + "\n"


def _handover_actions(alive: list[dict[str, Any]], chain: dict[str, Any],
                      view: dict[str, Any]) -> list[dict[str, Any]]:
    """接手后 48 小时该做的三件事。每条都是具体动作，不是「关注一下」。"""
    actions: list[dict[str, Any]] = []

    # 1) 逾期承诺：先兑现，别让客户觉得换了人就忘了
    overdue_commits = [f for f in alive if f["fact_type"] == "commitment" and f.get("overdue")]
    if overdue_commits:
        f = overdue_commits[0]
        actions.append({"text": f"联系客户，兑现逾期承诺：{f['statement'][:40]}",
                        "meta": f"逾期 {f.get('source_date', '')} · 换人后第一件事是补欠账",
                        "key": f["key"], "bad": True})

    # 2) 决策链缺口：先认识对的人
    if chain.get("missing"):
        m = chain["missing"][0]
        actions.append({"text": f"找到并认识{m['role_cn']}——现在决策链缺这一环",
                        "meta": "没有这层关系，后面的推进全是盲打", "bad": True})

    # 3) 最硬的顾虑：接手后主动提一次，显示你在乎
    blockers = [f for f in alive if f["fact_type"] == "concern"
                and (f.get("payload") or {}).get("severity") in ("blocker", "high")]
    if blockers:
        f = blockers[0]
        actions.append({"text": f"主动提及「{f.get('span') or f['statement'][:30]}」——问现状、给态度",
                        "meta": "这是客户最在意的事，别等他再提", "key": f["key"], "bad": True})

    # 兜底：没有任何急事时，给一条温和的建议
    if not actions:
        actions.append({"text": "给客户发条消息，告知对接人已变更、承诺不变",
                        "meta": "信息不断档，是信任不断档的前提"})

    return actions[:3]


def handover(view: dict[str, Any], today: date, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """交接说明：把客户交给另一个人时，那封邮件里该写的东西。

    和拜访简报的区别：拜访是「下次怎么谈」，交接是「这段时间发生过什么、
    哪些事不能断」。所以这里多一段「在办事项」和一段「你不知道的坑」，
    并且**明确列出还没搞清的东西**——接手的人最怕的不是信息少，是不知道自己不知道。
    """
    alive = _alive(view.get("facts", []))
    chain = decision_chain(view)
    # 主数据优先；没有就从记录推（`account_strip` 内部已经分好了层，
    # 这里再写一遍 `if not strip` 兜底，等于把同一套规则放在两个地方维护）
    strip = account_strip(profile, today, view)

    def texts(ftype: str, n: int) -> list[dict[str, Any]]:
        rows = [f for f in alive if f["fact_type"] == ftype]
        return [{"text": f["statement"],
                 "meta": f"{f.get('source_cn') or ''} {f.get('source_date') or ''}".strip(),
                 "key": f["key"]} for f in rows[:n]]

    risks = [f for f in alive if f["fact_type"] == "risk_signal"]
    concern_top = [f for f in alive if f["fact_type"] == "concern"][:1]

    left = [{"text": a["title"],
             "meta": f"{a['p']} · {a.get('next_step') or a.get('why', '')[:40]}",
             "rule": a.get("rules")}
            for a in view.get("actions", [])[:3]]

    blocks = [
        {"id": "who", "h": "这是个什么客户",
         "items": [{"text": f"{view.get('industry') or '行业未知'}，当前 {view.get('stage') or '阶段未知'}，"
                            f"负责人 {view.get('owner') or '你'}。",
                    "meta": ""},
                   {"text": view.get("summary", ""), "meta": ""}] +
                  ([{"text": f"{s['label']}：{s['value']}（{s['hint']}）", "meta": ""} for s in strip]
                   if strip else [])},
        {"id": "wip", "h": "正在办的三件事", "items": left or
         [{"text": "没有在办事项。", "meta": ""}]},
        {"id": "pitfalls", "h": "这些坑别再踩", "items":
            [{"text": f"「{f.get('span') or f['statement']}」", "meta": "客户原话", "key": f["key"]}
             for f in concern_top] +
            [{"text": f["statement"], "meta": "风险信号", "key": f["key"]} for f in risks[:2]] or
            [{"text": "没有记录到明确的坑，但也没记录过反面证据。", "meta": ""}]},
        {"id": "people", "h": "关键人", "items":
            [{"text": f"{n['label']} · {n['role_cn']} · {n['attitude_cn']}",
              "meta": n["influence_cn"], "key": n["key"]} for n in chain["nodes"]] or
            [{"text": "一个关键人都没记录。接手后第一件事是问清谁说了算。", "meta": ""}]},
        {"id": "first48", "h": "接手后 48 小时", "items": _handover_actions(alive, chain, view)},
        {"id": "unknown", "h": "还没搞清的", "items":
            [{"text": g, "meta": ""} for g in (view.get("gaps") or [])] or
            [{"text": "常问的几项都有记录。", "meta": ""}]},
    ]
    return {
        "kind": "handover",
        "title": f"交接说明 · {view['name']}",
        "subtitle": f"截至 {today.isoformat()} · {len(view.get('records', []))} 条记录 · "
                    f"{len(alive)} 条有效事实",
        "blocks": blocks,
        "chain": chain,
        "empty": not alive,
    }


# ---------------------------------------------------------------------------
# 方案准备简报：做方案 / 写标书前看的那张纸
# ---------------------------------------------------------------------------

def proposal_brief(view: dict[str, Any], today: date) -> dict[str, Any]:
    """做方案前的准备简报。五个模块全从已有事实拼，零模型调用。

    和拜访简报的区别：拜访是「见面怎么谈」，方案是「纸上怎么写」。
    方案要回答的是「为什么买、买什么、凭什么选你」，所以结构是
    问题 → 需求 → 竞品应对 → 风险 → 证据，按读者的阅读顺序排。
    """
    alive = _alive(view.get("facts", []))

    # 1) 方案要解决什么问题：最硬的顾虑，按 severity 排
    concerns = [f for f in alive if f["fact_type"] == "concern"]
    concerns.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}.get(
        (f.get("payload") or {}).get("severity"), 3), -f.get("conf", 0)))
    problem_items = [
        {"text": f"「{f.get('span') or f['statement']}」",
         "meta": f"{SEV_CN.get((f.get('payload') or {}).get('severity'), '提过')} · "
                 f"{(f.get('payload') or {}).get('category', '')}",
         "key": f["key"], "bad": True}
        for f in concerns[:3]
    ] or [{"text": "没有记录到明确顾虑。方案可以正面写价值主张。", "meta": ""}]

    # 2) 需求对照清单：客户提过的需求，标显式/隐式
    needs = [f for f in alive if f["fact_type"] == "need"]
    EXP_CN = {"explicit": "客户明确提出", "inferred": "推断，待确认"}
    need_items = [
        {"text": f["statement"],
         "meta": f"{EXP_CN.get((f.get('payload') or {}).get('explicitness'), '')} · "
                 f"{(f.get('payload') or {}).get('category', '')}",
         "key": f["key"], "bad": False}
        for f in needs[:5]
    ] or [{"text": "没有抽到明确需求。建议拜访时先问清，再动笔。", "meta": ""}]

    # 3) 竞品应对：客户提过谁、怎么说的、我方位置
    comps = [f for f in alive if f["fact_type"] == "competition"]
    POS_CN = {"ahead": "我方领先", "behind": "我方落后", "tied": "势均力敌",
              "unknown": "客户未表态"}
    comp_items = [
        {"text": f"{(f.get('payload') or {}).get('competitor_name', '未具名对手')}："
                 f"{(f.get('payload') or {}).get('customer_view') or f['statement']}",
         "meta": f"客户视角 · 我方{POS_CN.get((f.get('payload') or {}).get('our_position'), '未知')}",
         "key": f["key"], "bad": (f.get('payload') or {}).get('our_position') == 'behind'}
        for f in comps[:3]
    ] or [{"text": "没记录过竞品信息。没有消息不等于没有对手。", "meta": ""}]

    # 4) 风险与前提：阻塞性顾虑 + 风险信号
    risks = [f for f in alive if f["fact_type"] == "risk_signal"]
    blockers = [f for f in alive if f["fact_type"] == "concern"
                and (f.get("payload") or {}).get("severity") in ("blocker", "high")
                and not (f.get("payload") or {}).get("is_resolved")]
    risk_items = (
        [{"text": f"「{f.get('span') or f['statement']}」",
          "meta": "阻塞性顾虑 · 必须在方案中回应", "key": f["key"], "bad": True}
         for f in blockers[:2]] +
        [{"text": f["statement"],
          "meta": f"风险信号 · {(f.get('payload') or {}).get('signal_type', '')}",
          "key": f["key"], "bad": True}
         for f in risks[:2]]
    ) or [{"text": "没有识别出阻塞性风险。", "meta": ""}]

    # 5) 证据与锚点：已履行的承诺 + 客户说过的正面原话
    fulfilled = [f for f in alive if f["fact_type"] == "commitment"
                 and (f.get("payload") or {}).get("fulfillment") == "fulfilled"]
    positive_quotes = [f for f in alive if f["fact_type"] == "concern"
                       and (f.get("payload") or {}).get("verbatim_quote")
                       and not (f.get("payload") or {}).get("is_resolved")]
    evidence_items = (
        [{"text": f"已履行：{f['statement']}", "meta": "承诺已兑现", "key": f["key"]}
         for f in fulfilled[:2]] +
        [{"text": f"「{(f.get('payload') or {}).get('verbatim_quote', '')}」",
          "meta": "客户原话 · 可写进方案做锚点", "key": f["key"]}
         for f in positive_quotes[:2]]
    ) or [{"text": "暂无已兑现的证据。方案里先写能力与计划，别写成果。", "meta": ""}]

    blocks = [
        {"id": "problem", "h": "方案要解决什么问题", "items": problem_items},
        {"id": "needs", "h": "需求对照清单", "items": need_items},
        {"id": "competition", "h": "竞品应对", "items": comp_items},
        {"id": "risks", "h": "风险与前提", "items": risk_items},
        {"id": "evidence", "h": "证据与锚点", "items": evidence_items},
    ]
    return {
        "kind": "proposal",
        "title": f"方案准备 · {view['name']}",
        "subtitle": f"{view.get('industry', '')} · {view.get('stage', '')} · 基准日 {today.isoformat()}",
        "blocks": blocks,
        "empty": not alive,
    }

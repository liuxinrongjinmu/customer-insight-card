"""把 Fact 列表算成界面能直接吃的视图载荷。

原型里这段逻辑内联在 `build_demo.py` 的 `enrich()` 中：跑一次、吐成静态 JSON。
演示版要反复重算（贴一段新记录、拖基准日），所以必须抽成纯函数：

    输入  客户元信息 + Fact 列表 + 基准日
    输出  渲染层直接吃的 dict

抽出来还有第二个理由：原型与演示版共用同一套算法。
否则很快会出现「原型演示一套、后端跑另一套」，评审时一戳就穿。

**约定：调用方每次传进来的必须是新建的 Fact 对象。**
`src/aggregation/merge.py` 会就地改写 `f.status`（active / conflicted /
superseded / expired）。同一个 Fact 对象被两次不同的 `today` 复用过，
上一轮的 expired 会带进下一轮，日期滑块就会滑出一堆假状态。
serve.py 因此每次都从 canonical dict 重建 Fact，不复用对象。
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.advice.engine import _primary_key, generate_advice  # noqa: E402
from src.advice.rules import impact_of, money_cn  # noqa: E402
from src.aggregation.card import (  # noqa: E402
    COMMITMENT_IMMINENT_DAYS,
    CustomerProfile,
    build_card,
)
from src.aggregation.conflict import (  # noqa: E402
    SAME_DAY_TOLERANCE_DAYS,
    SAME_MOMENT_WINDOW_DAYS,
)
from src.extraction.schema import Fact, _to_date, display_tier  # noqa: E402
from src.pipeline import to_fact  # noqa: E402

# 展示用中文名。与 build_demo.py 中的同名常量必须一致，
# tests/test_demo_viewmodel.py 里有一条断言盯着这件事。
TYPE_CN = {
    "need": "需求", "concern": "顾虑", "commitment": "承诺", "stakeholder": "关键人",
    "competition": "竞争", "timeline": "时间节点", "risk_signal": "风险信号",
}
BASIS_CN = {"direct_quote": "原话", "paraphrase": "转述", "inferred": "推断"}
SOURCE_CN = {
    "visit_note": "拜访纪要", "meeting_minutes": "会议纪要", "email": "邮件",
    "call_transcript": "通话记录", "im_message": "即时通讯", "ticket": "工单",
    "contract": "合同", "crm_field": "CRM 字段", "manual": "手工录入",
}

#: 一次最多给几条建议。P0 不截断，P1/P2 补到 4 条为止——
#: 一次给八条待办，用户一条都不会做。
MAX_ADVICE = 4

#: 「N 天内到期」的提醒窗口。直接引用引擎里的同一个常量，不另写一个 7：
#: 界面上标「3 天内到期」而建议规则按 5 天判，是同一种「两套判据」的老问题。
IMMINENT_DAYS = COMMITMENT_IMMINENT_DAYS

FIELD_CN = {
    "_statement": "陈述内容",
    "due_date": "到期日",
    "target_date": "节点日期",
    "role_in_deal": "在项目中的角色",
    "our_position": "我方相对位置",
    "priority": "需求优先级",
    "severity": "严重程度",
    "fulfillment": "履行状态",
    "attitude": "态度",
    "stage": "竞争阶段",
    "is_resolved": "是否已解决",
}

VERDICT_CN = {
    "conflict": "矛盾：两个说法不一致",
    "evolution": "演进：新的取代旧的",
    "regression": "回退：从终态退回",
    "duplicate": "重复：交叉印证",
    "suspect": "存疑：疑似抽错",
}

_DATE_FIELDS = {"due_date", "target_date"}


def is_overdue(f: Fact, today: date) -> bool:
    """我方承诺逾期未履行。"""
    if f.fact_type != "commitment":
        return False
    p = f.payload or {}
    if p.get("direction") != "we_owe" or p.get("fulfillment") in ("fulfilled", "waived"):
        return False
    due = _to_date(p.get("due_date"))
    return bool(due and due < today)


def date_passed(f: Fact, today: date) -> bool:
    """时间节点已过。"""
    if f.fact_type != "timeline":
        return False
    t = _to_date((f.payload or {}).get("target_date"))
    return bool(t and t < today)


def days_until_due(f: Fact, today: date) -> int | None:
    """我方承诺还有几天到期。负数是已逾期天数；不算我方义务的返回 None。

    这个数字必须出现在卡片上。演示时拖基准日，观众要能看见
    「4 天内到期」变成「逾期 1 天」——变化如果只体现在 P0 数量上，
    就变成了「系统说变了」，而不是「我看见它变了」。
    """
    if f.fact_type != "commitment":
        return None
    p = f.payload or {}
    if p.get("direction") != "we_owe" or p.get("fulfillment") in ("fulfilled", "waived"):
        return None
    due = _to_date(p.get("due_date"))
    if due is None:
        return None
    return (due - today).days


def days_until_target(f: Fact, today: date) -> int | None:
    if f.fact_type != "timeline":
        return None
    t = _to_date((f.payload or {}).get("target_date"))
    return (t - today).days if t else None


# ---------------------------------------------------------------------------
# 裁决解释：把「为什么判改期不判矛盾」从代码里搬到界面上
# ---------------------------------------------------------------------------

def _gap_days(a: str | None, b: str | None) -> int | None:
    da, db = _to_date(a), _to_date(b)
    if da is None or db is None:
        return None
    return abs((da - db).days)


def _rule_text(c: Any, record_gap: int | None, value_gap: int | None) -> str:
    """用真实数字写判据，不写套话。

    演示里被问「为什么这条是改期不是矛盾」，答案必须落到数字上：
    相隔几天、窗口是几天。所以说「因为超过了窗口」是不够的——
    窗口是多少、这次差了多少，都得说出来。
    """
    if c.field in _DATE_FIELDS:
        if c.verdict == "evolution":
            return (
                f"两条记录相隔 {record_gap} 天，分属不同时点，"
                f"而一个承诺只有一个真实日期 → 判为改期，旧值退场。"
                f"若两条写于同一天前后（±{SAME_DAY_TOLERANCE_DAYS} 天内），"
                f"同一件事出现两个日期才是口径矛盾。"
            )
        if c.verdict == "conflict":
            return (
                f"两条记录相隔 {record_gap} 天，落在 ±{SAME_DAY_TOLERANCE_DAYS} 天窗口内，"
                f"同一件事却给出两个日期 → 判为口径矛盾。系统不替你在两个日期里选一个。"
            )
        if c.verdict == "suspect":
            return (
                f"两个日期本身相差 {value_gap} 天，超过一年，"
                f"多半是两件不同的事被归到了一组，而不是同一个日期被改了。"
            )
    else:
        if c.verdict == "conflict":
            return (
                f"该字段没有内在顺序（不是「推进/回退」那种），两个来源说法不一致；"
                f"两条记录相隔 {record_gap} 天，在 {SAME_MOMENT_WINDOW_DAYS} 天窗口内视为同一时点 "
                f"→ 判为矛盾，两条都保留。"
            )
        if c.verdict == "evolution":
            return (
                f"该字段有内在顺序（{c.left_value} → {c.right_value}），"
                f"变化方向是推进 → 判为演进，旧值退场，不打扰用户。"
            )
        if c.verdict == "regression":
            return (
                f"取值从终态退回（{c.left_value} → {c.right_value}），不像正常推进，"
                f"可能是新情况也可能是误抽 → 标出来交给人判断。"
            )
        if c.verdict == "duplicate":
            return "同一件事被两条记录重复提到，属交叉印证，不是矛盾，因此不报红标。"
    return c.reason


def judgement_rows(
    f: Fact,
    group: Any,
    by_id: dict[str, Fact],
    key_of: dict[str, str],
) -> list[dict[str, Any]]:
    """这条事实身上的每一条裁决，连同它的判据。"""
    out: list[dict[str, Any]] = []
    for c in list(getattr(group, "conflicts", []) or []):
        if f.fact_id not in (c.left_id, c.right_id):
            continue
        other_id = c.right_id if c.left_id == f.fact_id else c.left_id
        other = by_id.get(other_id)
        record_gap = _gap_days(f.stated_at, other.stated_at) if other else None
        value_gap = _gap_days(str(c.left_value), str(c.right_value))
        out.append({
            "verdict": c.verdict,
            "verdict_cn": VERDICT_CN.get(c.verdict, c.verdict),
            "field": c.field,
            "field_cn": FIELD_CN.get(c.field, c.field),
            "reason": c.reason,
            "from_value": c.left_value,
            "to_value": c.right_value,
            "record_gap_days": record_gap,
            "value_gap_days": value_gap,
            "other_key": key_of.get(other_id),
            "other_date": other.stated_at if other else None,
            "other_statement": other.statement if other else None,
            "rule": _rule_text(c, record_gap, value_gap),
            "confidence": round(c.confidence, 2),
        })
    return out


def facts_known_at(facts: list[Any], today: date) -> list[Any]:
    """基准日当天已经入库的事实。

    演示里拖基准日，观众看到的不该只是「天数变了」，而是「当时你知道什么」。
    9 月 1 日回看，10 月才录的那篇纪要还不存在——卡片上就不该出现它。
    不做这一步，拖到 9 月系统却说得 10 月的结论，那就不叫时间机器，
    叫拿现在的答案去回答过去的问题。

    为什么用 `recorded_at` 而不是 `stated_at`：补录的 8 月拜访纪要在补录之前
    你并不知道，按事件时间过滤会把「昨天刚补的」当成「8 月就知道」。
    演示数据里两者相同；现场粘的记录 recorded_at 是当天，故当天可见。

    已知简化：真实的补录通常晚于事件数天，这里按同一天处理。
    """
    edge = today.isoformat()
    out = []
    for f in facts:
        stamp = (getattr(f, "recorded_at", "") or getattr(f, "stated_at", "") or "")[:10]
        if not stamp or stamp <= edge:
            out.append(f)
    return out


def fact_rows(facts: list[Fact], card: Any, today: date | None = None) -> list[dict[str, Any]]:
    """Fact → 渲染行。key 是稳定 DOM id，rid 用来把事实挂回原文。

    基准日之后才入库的事实不返回：它当时还不存在。
    已撤回的照常返回，只是状态不同——撤回的卡片要在界面上留着，
    否则用户点完「这条不对」那条就凭空消失，既看不出发生了什么，也没法撤销。
    """
    key_of = {f.fact_id: f"k{i}" for i, f in enumerate(facts)}
    by_id = {f.fact_id: f for f in facts}
    group_of: dict[str, Any] = {}
    for g in card.groups:
        for f in g.facts:
            group_of[f.fact_id] = g

    edge = today.isoformat() if today else None
    rows: list[dict[str, Any]] = []
    for f in facts:
        if edge:
            stamp = (getattr(f, "recorded_at", "") or getattr(f, "stated_at", "") or "")[:10]
            if stamp and stamp > edge:
                continue                      # 那时还没入库
        g = group_of.get(f.fact_id)
        # 代表事实用带印证加成的置信度，其余用自身公式值
        conf = (g.effective_confidence
                if g and g.head and g.head.fact_id == f.fact_id else f.confidence)
        p = f.payload or {}
        conflict_with: list[str] = []
        if g and g.has_conflict:
            conflict_with = [key_of[x.fact_id] for x in g.facts if x.fact_id != f.fact_id]
        due_in = days_until_due(f, card.today)
        target_in = days_until_target(f, card.today)
        rows.append({
            "key": key_of[f.fact_id],
            "fid": f.fact_id,
            "rid": f.source_id,
            "span": f.source_span,
            "fact_type": f.fact_type,
            "statement": f.statement,
            "payload": p,
            "basis": f.confidence_basis,
            "basis_cn": BASIS_CN.get(f.confidence_basis, f.confidence_basis),
            "type_cn": TYPE_CN.get(f.fact_type, f.fact_type),
            "source_cn": SOURCE_CN.get(f.source_type, f.source_type),
            "source_date": f.stated_at,
            "conf": round(conf, 4),
            "tier": display_tier(conf),
            "status": f.status,
            "expired": f.status == "expired",
            "superseded": f.status == "superseded",
            "conflicted": f.status == "conflicted",
            "retracted": f.status == "retracted",
            "overdue": is_overdue(f, card.today),
            "date_passed": date_passed(f, card.today),
            "no_quote": f.fact_type == "concern" and not (p.get("verbatim_quote") or ""),
            "conflict": bool(g and g.has_conflict),
            "conflict_with": conflict_with,
            # 拖基准日时，卡片上要能看见「4 天内到期」→「逾期 1 天」
            "due_in": due_in,
            "target_in": target_in,
            "imminent": due_in is not None and 0 <= due_in <= IMMINENT_DAYS,
            "judgements": judgement_rows(f, g, by_id, key_of) if g else [],
            # 这条事实还能不能被「撤回」。已撤回的不给这个标记，
            # 界面上要给的是「撤销撤回」，两个按钮不能同时出现。
            "can_retract": f.status != "retracted",
        })
    return rows


#: 每条规则对应一个「下一步具体动作」。不是「关注一下」，是「今天能做的事」。
#: 只在展示层加——规则引擎保持纯净，不混入 UI 文案。
NEXT_STEP = {
    "commitment_overdue": "联系客户，说明进度并给出新的交付时间",
    "commitment_imminent": "确认进度能赶上，赶不上就提前打招呼",
    "concern_blocker": "正面回应这个顾虑——在方案中写清，或安排专题沟通",
    "deadline_imminent": "提前排好资源，节点前一周做一次预演",
    "deadline_missed": "查清原因——是延期了还是漏了？赶紧补一条记录",
    "customer_side_deadline": "节点前主动问一句进展，别等客户来找",
    "conflict_unresolved": "约相关人对齐口径，对完在系统里更新",
    "missing_decision_maker": "找到决策人，约一次面对面",
    "single_thread": "发展第二联系人，至少加一个使用者",
    "competitor_ahead": "在落后维度做针对性补强，或换赛道打",
    "stale_need": "重新确认需求是否还有效，更新后入档",
    "silence": "给客户发条消息或打个电话，重新建立联系",
    "thin_profile": "下次拜访多聊背景信息，补全画像",
    "need_blocking_requirement": "确认这条是硬门槛还是可谈，当面问清",
    "inferred_need_unconfirmed": "和客户确认这条推断对不对",
    "incumbent_pain": "把对方踩的坑写进方案的差异化页",
    "need_voiced_as_complaint": "把抱怨翻译成需求，和客户确认目标值",
    "no_competitor_info": "打听竞品情况，至少知道谁在参与",
    # r18 是最后加的一条，这里曾经漏了它。漏的后果不是少一句话，是**整行不渲染**：
    # 前端写的是 `a.next_step ? ... : ''`，拿到空串就当这行不存在。
    # 于是「问清兑现时间」这条建议在界面上只剩标题和理由，没有那个可照做的动作——
    # 而它恰恰是被设计出来补一个沉默缺口的（没有到期日的承诺不进任何监控）。
    # 有一条测试守这个：`tests/test_demo_viewmodel.py` 断言 RULES 与 NEXT_STEP 键集相等。
    "commitment_no_due": "当面问一句「这条大概哪天能给」，问到就补进记录",
}


def action_rows(adv: Any, key_of: dict[str, str],
                type_of: dict[str, str] | None = None,
                amount: float = 0.0) -> list[dict[str, Any]]:
    p0 = [a for a in adv.advice if a.priority == "P0"]
    rest = [a for a in adv.advice if a.priority != "P0"]
    shown = p0 + rest[: max(0, MAX_ADVICE - len(p0))]

    amount_cn = money_cn(amount)

    out: list[dict[str, Any]] = []
    for a in shown:
        # 「已办」要能对得上号，所以每条建议都带一个稳定身份。
        # 这个 id 必须和引擎去重用的那个键**是同一个**——否则会出现
        # 「界面勾掉的是 A，服务端找到的是 B」，勾完还在。
        aid = _primary_key(a)
        ftype = (type_of or {}).get(a.basis[0]) if a.basis else None
        out.append({
            "p": a.priority,
            "title": a.title,
            "why": a.why,
            "basis": key_of.get(a.basis[0]) if a.basis else None,
            "basis_fid": a.basis[0] if a.basis else None,
            "owner": "你",
            "rules": a.tags,
            "aid": aid,
            # 金额出现在每一行，而不是只出现在档案条：待办列表是跨客户看的，
            # 用户在那一屏没有档案条可看，而「这一件值多少钱」正是他决定
            # 先做哪件的主要依据。
            "amount": amount,
            "amount_cn": amount_cn,
            "impact": round(float(getattr(a, "impact", 0) or 0), 2),
            # 逾期老化档位。空串表示不适用；"stale" 要归因，"historic" 进历史欠账。
            "aging": getattr(a, "aging", "") or "",
            # 具体动作句：不是「关注一下」，是「今天能做的事」
            "next_step": NEXT_STEP.get(a.rule_id, ""),
            # 只有「我方欠的承诺」能靠勾一下真办完：把 fulfillment 改成 fulfilled，
            # 规则自然不再命中，状态是真变了。
            # 「还没摸到决策人」这种不是一天能干完的事，勾掉只能算「我知道了别再提醒」，
            # 界面必须把这两件事分开说，否则用户会以为系统以为他有决策人了。
            "can_fulfill": ftype == "commitment",
        })
    return out


def build_view(
    meta: dict[str, Any], facts: list[Fact], today: date,
    n_docs: int | None = None,
) -> dict[str, Any]:
    """客户元信息 + 事实 + 基准日 → 界面视图。

    meta 需要 id / name / industry / stage，其余字段原样带回给前端。

    三件事在这里发生，顺序不能乱：

    1. 按 `recorded_at` 挡掉基准日之后才入库的事实——那时你还不知道它；
    2. 已撤回的事实不进认知卡——但也不从返回里删掉，界面要能撤销；
    3. 剩下的才拿去归并、裁决、生成建议。

    第 2 步放在这里而不是 `merge_facts` 里，是因为「不进卡片」和
    「状态不被覆盖」是两件事：前者是视图口径，后者是状态机不变量。
    两处都做并不重复——merge 那道防的是有人绕过视图直接喂数据。
    """
    known = facts_known_at(facts, today)
    live = [f for f in known if f.status != "retracted"]

    profile_raw = meta.get("profile") or {}
    opp = profile_raw.get("open_opportunity") or {}
    try:
        open_amount = float(opp.get("amount") or 0)
    except (TypeError, ValueError):
        # 档案是 CRM 同步来的，脏数据（"180万" 这种字符串）迟早会出现。
        # 一条脏字段不该让整张卡生成失败——按「拿不到」处理。
        open_amount = 0.0

    profile = CustomerProfile(
        customer_id=meta["id"],
        name=meta["name"],
        industry=meta.get("industry", ""),
        stage=meta.get("stage", ""),
        owner_id=meta.get("owner_id", "me"),
        as_of=today.isoformat(),
        open_amount=open_amount,
    )
    card = build_card(profile, live, today=today, n_docs=n_docs)
    adv = generate_advice(card, today=today)

    key_of = {f.fact_id: f"k{i}" for i, f in enumerate(facts)}
    return {
        "id": meta["id"],
        "name": meta["name"],
        "industry": meta.get("industry", ""),
        "stage": meta.get("stage", ""),
        "owner": meta.get("owner", "你"),
        "facts": fact_rows(facts, card, today),
        "actions": action_rows(adv, key_of, {f.fact_id: f.fact_type for f in facts},
                               open_amount),
        "summary": card.verdict,
        "gaps": card.gaps,
        "stats": card.stats,
        "as_of": today.isoformat(),
        # 给「较昨日变化」这类前端提示用，见第二块
        "n_p0": sum(1 for a in adv.advice if a.priority == "P0"),
        # 界面上要说清「这个客户值多少」。0 表示档案里没有，不等于零元。
        "open_amount": open_amount,
        "open_amount_cn": money_cn(open_amount),
        # 排序用的那一维，直接由档案算，不从建议里读——
        # 没有任何建议的客户也需要这个数（它决定了万一有建议排在哪）。
        "impact": impact_of(profile),
        # 界面要能说清「还有几条事实发生在基准日之后，现在看不到」
        "n_pending_facts": len(facts) - len(known),
        "n_retracted": sum(1 for f in facts if f.status == "retracted"),
    }


def facts_from_data(rows: list[dict[str, Any]]) -> list[Fact]:
    """canonical dict（/api/customers 吐出的 fact_data）→ 全新的 Fact 对象。

    每次都重建，避免 merge.py 的就地改写跨请求残留。
    """
    return [to_fact(r) for r in rows]


def facts_to_data(facts: list[Fact]) -> list[dict[str, Any]]:
    """Fact → 可 JSON 化、可原样回传重建的 canonical dict。"""
    return [asdict(f) for f in facts]


def normalize_extracted(raws: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """抽取层吐出的 dict → canonical dict。

    走一遍 Fact 再序列化，保证前端回传时字段齐全（tags / recorded_at /
    tenant_id 这些抽取层可能没给的，由 to_fact 补齐）。
    补齐失败的那条直接丢掉——宁可少一条，也不要一条渲染时崩掉的。
    """
    out: list[dict[str, Any]] = []
    for r in raws:
        try:
            out.append(asdict(to_fact(r)))
        except (TypeError, ValueError):
            continue
    return out

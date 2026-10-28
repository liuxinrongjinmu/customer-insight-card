"""认知卡：把归并后的事实组织成一张能一眼看完的客户画像。

这一层只做三件事，都不涉及模型：
1. 一句话结论——用规则拼，不用模型写。理由见下面 build_verdict 的注释。
2. 分块与排序——按「对下一步动作的影响」排，不按事实类型编号排。
3. 空白项——显式说出「哪一类事实一条都没有」。用户不知道系统不知道什么，
   就会把「没提到」当成「没问题」，这是这类产品最典型的误导方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from ..extraction.schema import (
    Fact,
    FactStatus,
    FactType,
    _to_date,
)
from .conflict import Verdict
from .merge import FactGroup, MergeResult, merge_facts
from .signals import split_unsupported_signals, unsupported_note


#: 展示顺序：越靠前越影响「接下来做什么」。
SECTION_ORDER = (
    FactType.COMMITMENT.value,
    FactType.CONCERN.value,
    FactType.TIMELINE.value,
    FactType.NEED.value,
    FactType.COMPETITION.value,
    FactType.STAKEHOLDER.value,
    FactType.RISK_SIGNAL.value,
)

TYPE_CN = {
    "need": "需求", "concern": "顾虑", "commitment": "承诺",
    "stakeholder": "关键人", "competition": "竞争", "timeline": "时间节点",
    "risk_signal": "风险信号",
}

#: 少于这个数量，一句话结论直接说「信息不足」，不硬凑。
THIN_FACT_THRESHOLD = 3
THIN_TYPE_THRESHOLD = 3

#: 承诺「临近到期」的提醒窗口（天）。
#: 再短就来不及排期，再长则和「远期计划」混在一起，失去提醒的意义。
#:
#: 定义在这里而不是 advice/rules.py，是因为它有两个消费方：
#: 一句话结论（本模块）和 P0 规则 r16（rules.py，它本来就 import 本模块）。
#: 两边各写一个 7，迟早在某次改动后分叉——结论说「没有临近到期的承诺」，
#: 而 P0 里明明躺着一条，用户只能选择不信这个系统。
COMMITMENT_IMMINENT_DAYS = 7


@dataclass
class CustomerProfile:
    """客户档案。真实系统里这些字段来自 CRM，不在抽取范围。"""

    customer_id: str
    name: str
    industry: str = ""
    stage: str = ""
    owner_id: str = "me"
    as_of: str = ""
    #: 在办商机金额（万元）。0 表示**拿不到**，不是「零元」——
    #: 这两个含义必须分开：前者是不确定，后者是这单没钱。
    #: 存量客户池（33 个）没有主数据档案，全部是 0，靠这一位区分。
    open_amount: float = 0.0

    def today(self) -> date:
        return _to_date(self.as_of) or date.today()


@dataclass
class Section:
    fact_type: str
    title: str
    groups: list[FactGroup] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.groups)


@dataclass
class CognitiveCard:
    profile: CustomerProfile
    verdict: str
    sections: list[Section]
    groups: list[FactGroup]
    gaps: list[str]
    stats: dict[str, Any]
    today: date
    n_live: int = 0          # 未过期、未被取代的事实条数（按条算，不按组算）
    n_all: int = 0           # 事实总条数，含已过期的

    @property
    def is_thin(self) -> bool:
        """信息是否薄到不该下结论。

        判据写在这里而不是散在结论文案和建议规则里：两处各写一遍，
        必然出现「结论说信息不足、建议里却让先补底图」这类自相矛盾的输出。
        """
        if self.n_live < THIN_FACT_THRESHOLD:
            return True
        types = {g.fact_type for g in self.groups if g.head}
        return len(types) < THIN_TYPE_THRESHOLD

    def all_live_facts(self) -> list[Fact]:
        return [
            g.head for g in self.groups
            if g.head and g.head.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)
        ]

    def fact_by_id(self, fact_id: str) -> Fact | None:
        for g in self.groups:
            for f in g.facts:
                if f.fact_id == fact_id:
                    return f
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer": {
                "id": self.profile.customer_id,
                "name": self.profile.name,
                "industry": self.profile.industry,
                "stage": self.profile.stage,
                "open_amount": self.profile.open_amount,
                "as_of": str(self.today),
            },
            "verdict": self.verdict,
            "gaps": self.gaps,
            "stats": self.stats,
            "sections": [
                {
                    "type": s.fact_type,
                    "title": s.title,
                    "count": s.n,
                    "groups": [g.to_dict() for g in s.groups],
                }
                for s in self.sections
            ],
        }


# ---------------------------------------------------------------------------
# 一句话结论
# ---------------------------------------------------------------------------

def _short(text: str, limit: int = 28) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _top_concern(groups: list[FactGroup], today: date) -> Fact | None:
    """最硬的顾虑：未解决、severity 最高、置信度最高。"""
    rank = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    cands = [
        g.head for g in groups
        if g.fact_type == FactType.CONCERN.value
        and g.head
        and g.head.status != FactStatus.SUPERSEDED.value
        and not g.head.is_expired(today)
        and not (g.head.payload or {}).get("is_resolved")
    ]
    if not cands:
        return None
    cands.sort(key=lambda f: (
        rank.get((f.payload or {}).get("severity", "low"), 9),
        -f.confidence,
    ))
    return cands[0]


def _overdue_commitments(groups: list[FactGroup], today: date) -> list[Fact]:
    out = []
    for g in groups:
        f = g.head
        if not f or g.fact_type != FactType.COMMITMENT.value:
            continue
        p = f.payload or {}
        if p.get("direction") != "we_owe":
            continue
        if p.get("fulfillment") in ("fulfilled", "waived"):
            continue
        due = _to_date(p.get("due_date"))
        if due and due < today:
            out.append(f)
    return out


def _imminent_commitments(
    groups: list[FactGroup], today: date,
    window: int = COMMITMENT_IMMINENT_DAYS,
) -> list[Fact]:
    """临近到期但还没逾期的我方承诺。

    结论句里必须能说出这一条。不说的后果是自相矛盾：
    7-28 那天 P0 里躺着「交付《接入方案》（3 天内）」，结论却说
    「没有承诺逾期、硬节点或待确认的矛盾」——两句话都对，但用户看到的
    是「系统说有 P0，又说没事」。P0 数量和一话话结论必须是同一套判据。
    """
    out = []
    for g in groups:
        f = g.head
        if not f or g.fact_type != FactType.COMMITMENT.value:
            continue
        p = f.payload or {}
        if p.get("direction") != "we_owe":
            continue
        if p.get("fulfillment") in ("fulfilled", "waived"):
            continue
        due = _to_date(p.get("due_date"))
        if not due:
            continue
        days = (due - today).days
        if 0 <= days <= window:
            out.append(f)
    return out


def _competitor_line(groups: list[FactGroup], today: date) -> str:
    """一句话说清竞品态势。挑的那家是「最该当心的」，不是「抽得最准的」。

    这两个排序键必须分开，混在一起会说出与事实不符的话：
    恒远重工有云枢科技（已进集团另一家子公司、我方 trailing）和博思数智
    （入围但客户未评价、我方 unknown），两家 stage 都是 shortlisted。
    若第二排序键只看置信度，博思凭「原话直引」胜出，结论就成了
    「最靠前的是博思数智」——而客户真正在看的案例是云枢的。
    销售一眼就能看出这句是错的，然后整张卡的信任就没了。
    """
    rank = {"selected": 0, "preferred": 1, "shortlisted": 2, "evaluated": 3, "mentioned": 4}
    # 我方相对位置越差，越该被点名：trailing 排最前，leading 排最后。
    pos = {"trailing": 0, "even": 1, "unknown": 2, "leading": 3}
    comps = [
        g.head for g in groups
        if g.fact_type == FactType.COMPETITION.value
        and g.head
        and g.head.status != FactStatus.SUPERSEDED.value
        and not g.head.is_expired(today)
    ]
    if not comps:
        return ""
    comps.sort(key=lambda f: (
        rank.get((f.payload or {}).get("stage", "mentioned"), 9),
        pos.get((f.payload or {}).get("our_position", "unknown"), 2),
        -f.confidence,
    ))
    top = comps[0].payload.get("competitor_name") or "未具名"
    if len(comps) == 1:
        return f"{top} 在场"
    return f"{len(comps)} 家竞品在场，最该当心的是{top}"


def _deadline_line(groups: list[FactGroup], today: date, window: int = 30) -> str:
    """最近的硬节点。只报 30 天内或已过但无下文的。"""
    nxt: tuple[int, Fact] | None = None
    for g in groups:
        f = g.head
        if not f or g.fact_type != FactType.TIMELINE.value:
            continue
        if f.status == FactStatus.SUPERSEDED.value:
            continue
        d = _to_date((f.payload or {}).get("target_date"))
        if not d:
            continue
        delta = (d - today).days
        if delta < 0:
            continue
        if nxt is None or delta < nxt[0]:
            nxt = (delta, f)
    if nxt is None:
        return ""
    days, f = nxt
    event = (f.payload or {}).get("event") or f.statement
    hardness = (f.payload or {}).get("hardness")
    prefix = "硬节点" if hardness == "hard" else "节点"
    if days > window:
        return ""
    return f"{prefix}「{_short(event, 18)}」还有 {days} 天"


def build_verdict(
    profile: CustomerProfile,
    groups: list[FactGroup],
    today: date,
    n_all: int | None = None,
) -> str:
    """一句话结论。

    为什么不用模型写：这句话要被人反复引用、被复制到周报、被拿去开会。
    它必须每次对同一份事实产出同一个句子。模型会改语气、会漏项，
    而漏掉「有 1 条承诺已逾期」比语气生硬严重得多。
    规则拼出来的句子不好听，但可复现、可归因、可测试。
    """
    live = [
        g for g in groups
        if g.head and g.head.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)
    ]

    # 有效事实数按「事实条数」算，不按「归并组数」算。
    # 两条互相矛盾的需求会归并成一组——组数是 1，但信息量不是 1 条，
    # 而且这 1 组恰恰是最需要人处理的那种。
    n_live = sum(
        1 for g in groups for f in g.facts
        if f.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)
    )
    total = n_all if n_all is not None else sum(len(g.facts) for g in groups)

    head = f"{profile.industry}客户" if profile.industry else "该客户"
    # 阶段为空是常态，不是异常：CRM 里很多客户的商机阶段没维护。
    # 此时不要输出「当前处于阶段未知」——那句话既占位置又没有任何信息，
    # 还会让用户以为系统出了错。
    stage = profile.stage
    lead = head + (f"，当前处于{stage}" if stage else "")

    conflicted = [
        g for g in groups
        if g.has_conflict and g.head and g.head.status == FactStatus.CONFLICTED.value
    ]

    # 信息不足时，不要用稀疏数据编一个像样的结论。
    # 但「信息不足」不能成为吞掉矛盾的借口：两个来源在打架这件事，
    # 比事实条数少更该让人立刻知道。所以冲突在信息不足时也要说出来。
    thin_types = len({g.fact_type for g in live}) < THIN_TYPE_THRESHOLD
    if n_live < THIN_FACT_THRESHOLD or thin_types:
        base = f"{lead}。"
        if not total:
            return base + "信息不足：还没有任何可用事实，先补一次沟通记录。"
        if not n_live:
            return base + f"信息不足：{total} 条事实全部已过保鲜期，需要重新确认。"
        if n_live < THIN_FACT_THRESHOLD:
            # 说清「一共几条、还剩几条」，否则用户会以为系统把数据弄丢了
            detail = f"{total} 条事实中仅 {n_live} 条仍有效" if total > n_live else f"仅 {n_live} 条事实"
            msg = base + f"信息不足：{detail}，暂不给判断。"
        else:
            msg = base + f"信息不足：只在 {len({g.fact_type for g in live})} 类事实上有关键信息，看不全。"
        if conflicted:
            msg += f"但已有 {len(conflicted)} 处信息互相矛盾，需先确认。"
        return msg

    parts: list[str] = [lead]

    comp = _competitor_line(groups, today)
    if comp:
        parts.append(comp)

    concern = _top_concern(groups, today)
    if concern:
        quote = (concern.payload or {}).get("verbatim_quote")
        text = quote or (concern.payload or {}).get("concern_text") or concern.statement
        parts.append(f"最硬的顾虑是「{_short(text, 26)}」")

    overdue = _overdue_commitments(groups, today)
    if overdue:
        parts.append(f"有 {len(overdue)} 条承诺已逾期")

    near = _imminent_commitments(groups, today)
    if near:
        parts.append(f"有 {len(near)} 条承诺 {COMMITMENT_IMMINENT_DAYS} 天内到期")

    deadline = _deadline_line(groups, today)
    if deadline:
        parts.append(deadline)

    if conflicted:
        parts.append(f"{len(conflicted)} 处信息互相矛盾待确认")

    # 一句有内容的话都拼不出来时，也要说清「看过了，没看出问题」。
    # 直接返回「该客户。」会让用户以为系统坏了——沉默和「暂无异常」在界面上
    # 必须能区分开，前者意味着不可信，后者意味着可以放心去谈。
    if len(parts) == 1:
        return f"{lead}。有 {n_live} 条有效事实，没有承诺逾期、临近到期、硬节点或待确认的矛盾。"

    return "；".join(parts) + "。"


# ---------------------------------------------------------------------------
# 空白项
# ---------------------------------------------------------------------------

#: 哪些类型的空白是「本系统现在产不出」，而不是「没人写过」。
#:
#: risk_signal 的语义是「与基线相比的变化」，而基线只能来自更早的记录。
#: 客户只有一篇记录时，无论如何都读不出变化——这不是漏读，是**没有可比的东西**。
#: 卡片上必须把这个区别说出来，因为两种空白的应对完全不同：
#:
#:   无有效记录   → 去问客户（我们的缺失）
#:   本系统不产出 → 记下一篇就好（系统的边界）
#:
#: 值里的字符串是「为什么」，直接拼进提示语。
STRUCTURAL_GAPS: dict[str, tuple[int, str]] = {
    FactType.RISK_SIGNAL.value: (2, "变化要至少两篇记录才比得出来"),
}


def find_gaps(
    groups: list[FactGroup], today: date, n_docs: int | None = None,
    baseless: Sequence[Any] = (),
) -> list[str]:
    """列出「一条有效事实都没有」的类型，并说清是哪一种空。

    这一项比任何一条事实都重要：用户看到「竞争：0 条」，才知道
    「没有消息」不等于「没有对手」，才会去打听。

    三种空必须分开说，因为它们对用户的意义完全不同：

    1. **无有效记录** —— 真的没有这类信息，该去打听。
    2. **有记录但过保鲜期** —— 东岭铜业的风险信号有一条 `contact_change`，
       只是过了 30 天。说「无记录」而卡片上明明躺着那条灰掉的事实，
       用户会以为系统漏读了原文。
    3. **本系统不产出** —— 结构上产不出来（见 `STRUCTURAL_GAPS`）。
       说成「无有效记录」，用户会读成「这个客户没有风险」——
       一个错误结论，而且看起来跟真的似的。

    `n_docs` 为 None 表示调用方不知道记录篇数，此时不判第 3 种：
    宁可少说一句，也不要在信息不足时替系统下结论。

    `baseless` 是被「同一把尺」摘掉的模型推测（见 `signals.split_unsupported_signals`）。
    它不参与上面三种空的判定——那些事实既然没上卡，就不该影响「这一节有没有东西」。
    但也不能不提：用户翻记录能看到模型说过那句话，卡面却装作没有，
    就成了系统在偷偷改口径。所以并进风险信号那一行，说清「有几条、为什么不算」。
    """
    present = {
        g.fact_type for g in groups
        if g.head and g.head.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)
    }
    # 有过记录、但一条都算不上的那些，按状态分桶，好让措辞对得上事实
    seen: dict[str, list[str]] = {}
    for g in groups:
        if g.fact_type in present:
            continue
        for f in g.facts:
            seen.setdefault(g.fact_type, []).append(f.status)

    gaps: list[str] = []
    for t in SECTION_ORDER:
        if t in present:
            continue
        name = TYPE_CN.get(t, t)
        statuses = seen.get(t) or []
        if statuses:
            n, n_void = len(statuses), sum(
                1 for s in statuses if s == FactStatus.RETRACTED.value)
            if n_void == n:
                gaps.append(f"{name}：{n} 条记录都已被判为无效")
            elif n_void:
                gaps.append(f"{name}：{n} 条记录里 {n_void} 条已被判为无效，其余已过保鲜期")
            else:
                gaps.append(f"{name}：有 {n} 条记录，但已过保鲜期")
            continue
        structural = STRUCTURAL_GAPS.get(t)
        if structural and n_docs is not None and n_docs < structural[0]:
            gaps.append(
                f"{name}：本系统不产出（只有 {n_docs} 篇记录，{structural[1]}）")
            continue
        gaps.append(f"{name}：无有效记录")

    note = unsupported_note(baseless)
    if note:
        risk = TYPE_CN[FactType.RISK_SIGNAL.value]
        for i, line in enumerate(gaps):
            if line.startswith(risk):
                gaps[i] = f"{line}；{note}"
                break
        else:
            # 风险信号那一节有内容（规则读出来的），此时没有那一行空白项。
            # 不硬塞进空白项里，单独加一行说明——它说的是「没上卡的那几条」，
            # 不是「这一节是空的」。
            gaps.append(f"{risk}（说明）：{note}")
    return gaps


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_card(
    profile: CustomerProfile,
    facts: list[Fact],
    today: date | None = None,
    n_docs: int | None = None,
) -> CognitiveCard:
    today = today or profile.today()

    # 先过一道「同一把尺」：模型说的变化，原文里读不出这种变化就不上卡。
    # 放在归并之前，是为了让空白项、n_live、一句话结论看到的是同一批事实；
    # 放在归并之后再做，卡片上会同时出现「支持者离场」和「这一节产不出」，
    # 两句话互相打脸。
    facts, baseless = split_unsupported_signals(facts)

    merged: MergeResult = merge_facts(facts, today=today)

    sections: list[Section] = []
    for t in SECTION_ORDER:
        gs = merged.by_type(t)
        if not gs:
            continue
        # 组内排序：冲突的优先（要人处理），其次按置信度
        gs = sorted(gs, key=lambda g: (
            0 if g.head and g.head.status == FactStatus.CONFLICTED.value else 1,
            -g.effective_confidence,
        ))
        sections.append(Section(fact_type=t, title=TYPE_CN.get(t, t), groups=gs))

    n_live = sum(
        1 for g in merged.groups for f in g.facts
        if f.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)
    )
    n_all = sum(len(g.facts) for g in merged.groups)

    return CognitiveCard(
        profile=profile,
        verdict=build_verdict(profile, merged.groups, today, n_all=n_all),
        sections=sections,
        groups=merged.groups,
        gaps=find_gaps(merged.groups, today, n_docs=n_docs, baseless=baseless),
        stats=merged.stats,
        today=today,
        n_live=n_live,
        n_all=n_all,
    )

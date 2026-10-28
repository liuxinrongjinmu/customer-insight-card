"""建议引擎：跑规则、去重、排序。

去重的规则是「一条事实只产出一条建议」。这不是为了少写代码，是因为
一条逾期的承诺如果同时触发「逾期」「需求过期」「推断未确认」三条建议，
用户会在列表里看到三行说的其实是同一件事，然后开始怀疑系统的判断力。

去重时保留优先级最高的那条，把其它规则的理由并进 why、把 rule_id 并进 tags。
信息不丢，但只占一行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..aggregation.card import CognitiveCard
from .rules import RULES, PRIORITY_ORDER, Advice, AdviceContext, Rule, impact_of


#: 某条规则命中后抑制的规则。避免「信息太少」时刷出一堆细化建议。
SUPPRESS: dict[str, set[str]] = {
    "thin_profile": {"single_thread", "no_competitor_info", "silence"},
    "deadline_missed": {"silence"},          # 已经说了信息断层，不再重复说沉默
}


@dataclass
class AdviceResult:
    advice: list[Advice] = field(default_factory=list)
    fired_rules: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def by_priority(self, p: str) -> list[Advice]:
        return [a for a in self.advice if a.priority == p]

    def to_dict(self) -> dict:
        return {
            "advice": [a.to_dict() for a in self.advice],
            "fired_rules": self.fired_rules,
            "suppressed": self.suppressed,
            "stats": self.stats,
        }


def _primary_key(a: Advice) -> str:
    """去重键。

    scope="fact" 的建议针对某条事实，按事实归并——一条事实只占一行。
    scope="card" 的建议针对整张画像（画像太薄、没有任何竞品记录、只有一个联系人），
    它们会引用很多条事实，但说的不是那些事实本身。若也按事实归并，
    「补齐客户底图」就会和「找不到决策人」撞成一条——两者恰好都拿同一条
    干系人事实当依据，但这是两件完全不同的待办。
    """
    if a.scope == "card":
        # 带上首条依据：同一条规则对两个不同冲突组要各占一行，
        # 否则两处矛盾会被压成一条，用户以为只有一处。
        return f"card:{a.rule_id}:{a.basis[0] if a.basis else ''}"
    return a.basis[0] if a.basis else f"rule:{a.rule_id}"


def _merge_reasons(items: list[Advice]) -> str:
    seen: list[str] = []
    for a in items:
        if a.why and a.why not in seen:
            seen.append(a.why)
    return " ".join(seen[:3])


def generate_advice(
    card: CognitiveCard,
    today: date | None = None,
    rules: list[Rule] | None = None,
) -> AdviceResult:
    ctx = AdviceContext(card=card, today=today or card.today)
    result = AdviceResult()
    # 金额是客户级常量，在这里统一打上，规则一行都不用改。
    #
    # 放在这里而不是每条规则里，是因为「这条建议值多少钱」和「这条规则发现
    # 了什么」是两个维度——让 18 条规则各自读一遍 profile，迟早在某一条里
    # 读漏（读漏的表现是「同客户的建议有的 80 分有的 50 分」，排序乱掉）。
    impact = impact_of(card.profile)

    raw: list[Advice] = []
    for rule in (rules or RULES):
        try:
            produced = rule(ctx) or []
        except Exception as exc:  # noqa: BLE001 - 单条规则出错不该让整张卡失败
            produced = []
            result.stats.setdefault("errors", []).append(f"{rule.__name__}: {exc}")
        for a in produced:
            a.impact = impact
        if produced:
            result.fired_rules.append(rule.__name__)
            raw.extend(produced)

    # --- 抑制 ---
    fired_ids = {a.rule_id for a in raw}
    blocked: set[str] = set()
    for rid, targets in SUPPRESS.items():
        if rid in fired_ids:
            blocked |= targets
    if blocked:
        kept = [a for a in raw if a.rule_id not in blocked]
        result.suppressed = sorted({a.rule_id for a in raw} & blocked)
        raw = kept

    # --- 按事实去重 ---
    buckets: dict[str, list[Advice]] = {}
    for a in raw:
        buckets.setdefault(_primary_key(a), []).append(a)

    merged: list[Advice] = []
    for _, items in buckets.items():
        items.sort(key=lambda a: a.sort_key)
        head = items[0]
        if len(items) > 1:
            head.tags = sorted({head.rule_id} | {x.rule_id for x in items})
            head.why = _merge_reasons(items)
            head.urgency = max(a.urgency for a in items)
        else:
            head.tags = sorted({head.rule_id})
        merged.append(head)

    merged.sort(key=lambda a: a.sort_key)
    result.advice = merged
    result.stats.update({
        "n_advice": len(merged),
        "n_p0": sum(1 for a in merged if a.priority == "P0"),
        "n_p1": sum(1 for a in merged if a.priority == "P1"),
        "n_p2": sum(1 for a in merged if a.priority == "P2"),
        "n_raw": len(raw),
    })
    return result

"""端到端：一堆记录 → 事实 → 认知卡 → 建议。

这是业务侧唯一需要调用的入口。上面三层（抽取 / 聚合 / 建议）都可以单独跑，
但产品只需要一个东西：给定客户和它的历史记录，回答三个问题。

  这个客户是谁   → profile + 一句话结论
  过去发生过什么 → sections（按影响排序的事实分组）
  现在该做什么   → advice（带依据、可点开原文）

为什么把载荷统一成 Fact 对象而不是继续传 dict：
dict 里的字段名没有约束，聚合层写错一个 key 不会报错，只会静默得到 None，
然后某个数字变成 0，某个判断悄悄失效。这类 bug 在评测里看不出来，
只有在界面上表现为「怎么这里空着」才被发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any, Iterable

from .aggregation.card import CognitiveCard, CustomerProfile, build_card
from .advice.engine import AdviceResult, generate_advice
from .advice.rules import Advice
from .extraction.schema import Fact, _to_date


#: 抽取层会额外塞进来的非模型字段，转 Fact 时丢弃。
_EXTRA_KEYS = {"doc_id", "_reason", "_hallucination", "_enum_repaired", "_recomputed_confidence"}


def to_fact(raw: dict[str, Any]) -> Fact:
    """把抽取层的 dict 转成 Fact 对象。

    缺字段时给安全默认值而不是抛异常：真实数据里总会有几条脏记录，
    一条脏记录不该让整个客户的认知卡生成失败。
    """
    allowed = {f.name for f in fields(Fact)}
    data = {k: v for k, v in raw.items() if k in allowed}
    data.setdefault("fact_type", "need")
    data.setdefault("customer_id", "")
    data.setdefault("statement", "")
    data.setdefault("payload", {})
    data.setdefault("source_type", "manual")
    data.setdefault("source_id", raw.get("doc_id", ""))
    data.setdefault("source_span", "")
    data.setdefault("stated_at", "")
    data.setdefault("stated_at_precision", "unknown")
    data.setdefault("confidence", 0.0)
    data.setdefault("confidence_basis", "paraphrase")
    if not data.get("fact_id"):
        data.pop("fact_id", None)
    return Fact(**data)


def to_facts(raws: Iterable[dict[str, Any]]) -> list[Fact]:
    return [to_fact(r) for r in raws]


@dataclass
class CustomerView:
    """一个客户的完整视图，界面直接吃这个对象。"""

    card: CognitiveCard
    advice: AdviceResult

    @property
    def profile(self) -> CustomerProfile:
        return self.card.profile

    @property
    def verdict(self) -> str:
        return self.card.verdict

    def top_actions(self, n: int = 3) -> list[Advice]:
        return self.advice.advice[:n]

    def explain(self) -> str:
        """给日志/排查用的纯文本摘要。"""
        lines = [
            f"{self.profile.name}（{self.profile.industry or '行业未知'} · {self.profile.stage or '阶段未知'}）",
            f"结论：{self.verdict}",
            f"事实 {self.card.stats.get('n_facts', 0)} 条，归并为 {self.card.stats.get('n_groups', 0)} 组，"
            f"冲突 {self.card.stats.get('n_conflict_groups', 0)} 组",
        ]
        if self.card.gaps:
            lines.append("空白：" + "、".join(self.card.gaps))
        for a in self.advice.advice:
            lines.append(f"  [{a.priority}] {a.title} —— {a.why}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card": self.card.to_dict(),
            "advice": self.advice.to_dict(),
        }


def build_view(
    profile: CustomerProfile,
    facts: list[Fact] | list[dict[str, Any]],
    today: date | None = None,
    n_docs: int | None = None,
) -> CustomerView:
    """给定客户档案与事实，产出认知卡与建议。

    `n_docs` 是记录篇数，只用于空白项：`risk_signal` 要两篇以上才比得出变化，
    只有一篇时卡片要说「本系统不产出」而不是「无有效记录」。不传就等于未知，
    空白项按老口径说话。
    """
    if facts and isinstance(facts[0], dict):
        facts = to_facts(facts)  # type: ignore[arg-type]
    card = build_card(profile, facts, today=today, n_docs=n_docs)  # type: ignore[arg-type]
    advice = generate_advice(card, today=today)
    return CustomerView(card=card, advice=advice)


def build_views_from_batch(
    profiles: dict[str, CustomerProfile],
    facts_by_customer: dict[str, list[dict[str, Any]]],
    today: date | None = None,
) -> dict[str, CustomerView]:
    """批量构建。按 customer_id 切开——不同客户的事实绝不能混进同一张卡。"""
    out: dict[str, CustomerView] = {}
    for cid, profile in profiles.items():
        out[cid] = build_view(profile, facts_by_customer.get(cid, []), today=today)
    return out

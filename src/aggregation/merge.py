"""跨文档聚合：把多篇记录抽出的事实归并成「每个主题一条当前结论」。

Stage 1 的产物是「一篇文章里有什么」，Stage 2 的产物是「这个客户现在是什么样」。
中间差的这一步是归并，而归并的难点不在分组，在**裁决**：

同一个主题下往往有好几条事实，它们的差异分三种，处理方式完全不同：

1. 重复（同一件事被两篇记录提到）→ 合并，置信度上调。这是交叉印证，是最便宜的可信度来源。
2. 演进（事情往前走了）→ 新的取代旧的，旧的置 superseded，从默认视图里退场。
3. 冲突（同一时点两个来源说法不一致）→ 两条都留，标 conflicted，交给人判断。

把 3 当 2 处理，系统就会静默骗人；把 2 当 3 处理，系统就会天天狼来了。

状态只取一个值，叠加信息放进 tags。优先级：retracted > superseded > conflicted > expired > active。
理由：被取代的事实不该再占据注意力，哪怕它同时过期或冲突。
「retracted」优先级最高，高于一切引擎判断——它代表「人说过这条不算」，
任何自动规则都不该把它翻回去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from ..extraction.schema import (
    Fact,
    FactStatus,
    _to_date,
)
from .conflict import Conflict, Verdict, compare_pair


#: 跨来源重复一次，置信度上调多少。幅度刻意取小：
#: 三篇记录都提到同一件事，说明它真实且重要，但不该把 0.7 抬成 1.0。
CORROBORATION_BONUS = 0.03
MAX_CORROBORATED = 0.98


@dataclass
class FactGroup:
    """一个归并主题下的全部事实与裁决结果。"""

    fact_type: str
    subject_key: str
    facts: list[Fact] = field(default_factory=list)
    head: Fact | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    support: dict[str, int] = field(default_factory=dict)   # fact_id -> 印证次数

    # --- 派生属性 ---

    @property
    def effective_confidence(self) -> float:
        """代表事实的置信度，含交叉印证加成。"""
        if self.head is None:
            return 0.0
        n = self.support.get(self.head.fact_id, 1)
        return min(MAX_CORROBORATED, self.head.confidence + CORROBORATION_BONUS * (n - 1))

    @property
    def has_conflict(self) -> bool:
        return any(c.verdict == Verdict.CONFLICT for c in self.conflicts)

    @property
    def has_regression(self) -> bool:
        return any(c.verdict == Verdict.REGRESSION for c in self.conflicts)

    @property
    def conflict_fields(self) -> list[str]:
        return [c.field for c in self.conflicts if c.verdict == Verdict.CONFLICT]

    def live(self) -> list[Fact]:
        """当前仍有效的事实（未过期、未被取代）。"""
        return [f for f in self.facts if f.status in (FactStatus.ACTIVE.value, FactStatus.CONFLICTED.value)]

    def superseded(self) -> list[Fact]:
        return [f for f in self.facts if f.status == FactStatus.SUPERSEDED.value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_type": self.fact_type,
            "subject_key": self.subject_key,
            "head_id": self.head.fact_id if self.head else None,
            "head_statement": self.head.statement if self.head else None,
            "confidence": round(self.effective_confidence, 4),
            "status": self.head.status if self.head else None,
            "has_conflict": self.has_conflict,
            "conflict_fields": self.conflict_fields,
            "n_facts": len(self.facts),
            "support": dict(self.support),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


@dataclass
class MergeResult:
    groups: list[FactGroup] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def by_type(self, fact_type: str) -> list[FactGroup]:
        return [g for g in self.groups if g.fact_type == fact_type]

    def conflict_groups(self) -> list[FactGroup]:
        return [g for g in self.groups if g.has_conflict]


def _sort_key(f: Fact) -> tuple[str, float, str]:
    """组内排序：时间早的在前；同一时间取置信度高的在前。

    时间相同但置信度不同时，把置信度高的放前面做 head 候选，
    避免低质量转述压过现场原话。
    """
    return (f.stated_at or "", f.confidence, f.fact_id or "")


def group_facts(facts: Iterable[Fact]) -> dict[tuple[str, str], list[Fact]]:
    """按 (类型, 归并键) 分组。

    归并键直接取 `Fact.subject_key`，不在这里另拼一份——抽取层去重用的是
    同一个函数，两处各写一遍的话，抽取时算两条、聚合时算一组，卡片上就会
    多出一个重复条目，而且不报错。
    """
    buckets: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
        buckets.setdefault((f.fact_type, f.subject_key), []).append(f)
    for bucket in buckets.values():
        bucket.sort(key=_sort_key)
    return buckets


def merge_facts(
    facts: Iterable[Fact],
    today: date | None = None,
) -> MergeResult:
    """把一批事实归并成组，并裁决组内关系。

    传入的 facts 允许来自多篇文档、多个客户；本函数不做客户过滤，
    因为归并键里已经含客户隐含信息（同一批次通常是同一客户）。
    跨客户混用时请先按 customer_id 切开。
    """
    today = today or date.today()
    result = MergeResult()
    buckets = group_facts(facts)

    for (fact_type, skey), items in buckets.items():
        group = FactGroup(fact_type=fact_type, subject_key=skey, facts=items)

        # 撤回是人给的终态，引擎不该覆盖它。
        # 不把这一条挡住的后果很具体：用户点了「这条抽错了」，界面上一瞬间
        # 生效，下一轮归并又把它改回 active——按钮看起来点了，其实是假的。
        # 所以撤回的事实不参与裁决，也不当代表；组里只剩它时，代表为空。
        alive = [f for f in items if f.status != FactStatus.RETRACTED.value]

        # --- 1. 决胜：最新的那条是当前代表 ---
        head = alive[-1] if alive else None
        group.head = head

        # --- 2. 逐对裁决 ---
        superseded_ids: set[str] = set()
        conflicted_ids: set[str] = set()
        corroborated: dict[str, int] = {f.fact_id: 1 for f in alive}

        for i in range(len(alive)):
            for j in range(i + 1, len(alive)):
                left, right = alive[i], alive[j]
                for c in compare_pair(left, right):
                    group.conflicts.append(c)
                    if c.verdict == Verdict.DUPLICATE:
                        # 跨来源重复 = 交叉印证；同源重复不加分
                        if left.source_type != right.source_type:
                            corroborated[right.fact_id] = corroborated.get(right.fact_id, 1) + 1
                            corroborated[left.fact_id] = corroborated.get(left.fact_id, 1) + 1
                    elif c.verdict == Verdict.EVOLUTION:
                        superseded_ids.add(left.fact_id)
                    elif c.verdict in (Verdict.CONFLICT, Verdict.SUSPECT):
                        conflicted_ids.add(left.fact_id)
                        conflicted_ids.add(right.fact_id)
                    elif c.verdict == Verdict.REGRESSION:
                        # 回退不置死旧值，但也不当冲突——标出来让人看
                        conflicted_ids.add(right.fact_id)

        # --- 3. 定状态 ---
        for f in items:
            tags = list(f.tags or [])
            if f.status == FactStatus.RETRACTED.value:
                # 撤回是终态，原样保留。它也不该拿到 superseded 之类的标签——
                # 「被取代」和「被人判定为抽错」是两件事，混在一个标签里，
                # 用户就分不清这条是自己撤的还是系统推下去的。
                tags.append("retracted")
                f.tags = sorted(set(tags))
                continue
            if f.fact_id in superseded_ids:
                f.status = FactStatus.SUPERSEDED.value
                young = next(
                    (x for x in alive
                     if x.fact_id in superseded_ids and x.stated_at > f.stated_at),
                    None,
                )
                if young:
                    f.superseded_by = young.fact_id
                tags.append("superseded")
            elif f.fact_id in conflicted_ids and not f.is_expired(today):
                f.status = FactStatus.CONFLICTED.value
                tags.append("conflicted")
            elif f.is_expired(today):
                f.status = FactStatus.EXPIRED.value
                tags.append("expired")
                if f.fact_id in conflicted_ids:
                    tags.append("conflicted_expired")
            else:
                f.status = FactStatus.ACTIVE.value
            f.tags = sorted(set(tags))

        group.support = corroborated
        result.groups.append(group)
        result.conflicts.extend(group.conflicts)

    # --- 4. 统计 ---
    result.stats = {
        "n_facts": sum(len(g.facts) for g in result.groups),
        "n_groups": len(result.groups),
        "n_conflict_groups": sum(1 for g in result.groups if g.has_conflict),
        "n_superseded": sum(len(g.superseded()) for g in result.groups),
        "n_conflicts": sum(1 for c in result.conflicts if c.verdict == Verdict.CONFLICT),
        "n_duplicates": sum(1 for c in result.conflicts if c.verdict == Verdict.DUPLICATE),
        "n_evolutions": sum(1 for c in result.conflicts if c.verdict == Verdict.EVOLUTION),
        "n_suspects": sum(1 for c in result.conflicts if c.verdict == Verdict.SUSPECT),
    }
    return result

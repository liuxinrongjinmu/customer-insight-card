"""从修正记录里沉淀「下次别再犯」的规则。

`corrections.py` 负责记下来，这里负责让它生效。三类错误各有各的生效方式，
混成一套只会两边都不准：

| 错误 | 生效方式 | 生效门槛 | 为什么是这个门槛 |
|---|---|---|---|
| 这段不是事实 | 原文黑名单：同类型 + 同片段，一律不再产出 | **一次就生效** | 用户明确否决过这段原文，再抽一次是打他的脸 |
| 内容抽错了 | 字段改判表：同类型 + 同字段 + 同「原值→新值」映射 | **≥3 次** | 一次改错可能是特例，三次是系统性偏差 |
| 重复了 | 不另做规则 | — | 归并键已经负责这件事，重复说明键选错了，那是设计问题不是数据问题 |

**为什么黑名单只认「完全相同」**：相似度阈值一加，就再也解释不清「这条为什么
被拦」。用户看到自己没否决过的一条被拦掉，会把这个机制整个关掉——那时它连
一次生效的机会都没有了。

**提示词补丁默认不自动施加**：`suggest_patch()` 只生成一句话供人确认。
自动改提示词等于让线上系统变成没人复核的实验场，而模型行为的每一次改动
都必须能被评测集回答「变好了还是变坏了」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .corrections import Correction, LESSON_REASONS

#: 字段改判要出现几次才成规则。
FIELD_OVERRIDE_MIN = 3


@dataclass(frozen=True)
class Lesson:
    """一条沉淀下来的规则。"""

    kind: str            # span_blacklist | field_override
    fact_type: str
    key: str             # span 或 "field=was→now"
    times: int
    why: str

    @property
    def cn(self) -> str:
        return {"span_blacklist": "原文黑名单", "field_override": "字段改判"}.get(self.kind, self.kind)


def lessons_from(rows: Iterable[Correction], min_field: int = FIELD_OVERRIDE_MIN) -> list[Lesson]:
    """把修正聚类成规则。返回的顺序稳定，便于测试与展示。"""
    spans: dict[tuple[str, str], int] = {}
    fields: dict[tuple[str, str, str, str], int] = {}

    for c in rows:
        if c.reason not in LESSON_REASONS:
            continue
        if c.reason == "not_a_fact" and c.span.strip():
            k = (c.fact_type, c.span.strip())
            spans[k] = spans.get(k, 0) + 1
        elif c.reason == "wrong_value" and c.field:
            k = (c.fact_type, c.field, c.was, c.now)
            fields[k] = fields.get(k, 0) + 1

    out: list[Lesson] = []
    for (ftype, span), n in sorted(spans.items()):
        out.append(Lesson("span_blacklist", ftype, span, n,
                          f"这段原文被判过 {n} 次「不是事实」"))
    for (ftype, field, was, now), n in sorted(fields.items()):
        if n < min_field:
            continue
        out.append(Lesson("field_override", ftype, f"{field}={was}→{now}", n,
                          f"同一处改判出现过 {n} 次"))
    return out


class Blacklist:
    """原文黑名单。查询是 O(1)，因为拦截发生在每一条抽取结果上。"""

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        self._pairs = {(ft.strip(), sp.strip()) for ft, sp in pairs if sp.strip()}

    @classmethod
    def from_corrections(cls, rows: Iterable[Correction]) -> "Blacklist":
        return cls((c.fact_type, c.span) for c in rows if c.reason == "not_a_fact")

    def __len__(self) -> int:
        return len(self._pairs)

    def __contains__(self, pair: tuple[str, str]) -> bool:
        return (str(pair[0]).strip(), str(pair[1]).strip()) in self._pairs

    def blocks(self, fact: dict[str, Any]) -> bool:
        """这条抽取结果该不该被拦。

        只看两个字段：事实类型与原文片段。**不看 payload**——同一个片段抽错了
        `severity`，不代表这个片段不是事实；那种情况属于 `wrong_value`，
        该走字段改判，不该整条拦掉。
        """
        return (str(fact.get("fact_type") or ""),
                str(fact.get("source_span") or "")) in self._pairs


def apply_blacklist(facts: list[dict[str, Any]],
                    blacklist: Blacklist) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (留下的, 拦掉的)。拦掉的要能说清为什么——不能静默吞掉。"""
    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for f in facts:
        (blocked if blacklist.blocks(f) else kept).append(f)
    return kept, blocked


def apply_overrides(fact: dict[str, Any], lessons: Iterable[Lesson]) -> dict[str, Any]:
    """按字段改判表修一条事实的 payload。返回新的 payload（不改原对象）。

    只在「原值完全一致」时改。原值变了说明情况变了，硬套旧改判会造出新错。
    """
    payload = dict(fact.get("payload") or {})
    ftype = str(fact.get("fact_type") or "")
    for ls in lessons:
        if ls.kind != "field_override" or ls.fact_type != ftype:
            continue
        field, _, mapping = ls.key.partition("=")
        was, _, now = mapping.partition("→")
        if str(payload.get(field) or "") == was:
            payload[field] = now
    return payload


def block_note(blocked: list[dict[str, Any]]) -> str:
    """给回执的一句话。说清「拦了几条、为什么」，不说就变成黑箱。"""
    if not blocked:
        return ""
    types = sorted({str(f.get("fact_type") or "") for f in blocked})
    return (f"拦下 {len(blocked)} 条：{'/'.join(types)} 的原文你判过「不是事实」，"
            "这次没有再抽出来")


def suggest_patch(rows: Iterable[Correction]) -> str:
    """把修正归纳成一句话，**供人确认后再用**，不自动施加。

    只处理 `wrong_value` 里最集中的那一处：一句提示词能说清的事有限，
    把五条改判塞进一句话，模型只会抓住其中一条。
    """
    fields: dict[tuple[str, str], list[str]] = {}
    for c in rows:
        if c.reason == "wrong_value" and c.field:
            fields.setdefault((c.fact_type, c.field), []).append(f"{c.was}→{c.now}")
    if not fields:
        return ""
    (ftype, field), pairs = max(fields.items(), key=lambda kv: len(kv[1]))
    if len(pairs) < FIELD_OVERRIDE_MIN:
        return ""
    sample = "；".join(pairs[:3])
    return (f"{ftype}.{field} 的取值容易抽错，出现过 {len(pairs)} 次修正（{sample}）。"
            "拿不准时按原文原话填，不要自行归纳。")

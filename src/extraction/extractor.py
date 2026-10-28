"""抽取管道：提示词 -> 模型 -> 解析 -> 校验 -> 去重。

对外只有一个入口 extract_document，返回结构化结果与被拒记录，便于评测与死信分析。

管道有两段。第一遍是全量抽取（七类都抽）；第二遍是**补抽**（recall），
只针对漏得最重的 need / concern 做定向追问，见 recall.py 的说明。
补抽的产出走同一套校验与溯源，所以它不可能把幻觉混进来——
最坏情况是白花一次调用，而不是污染结论。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .adapter import LLMAdapter, extract_json_array
from .prompt import build_messages
from .recall import build_recall_messages, is_duplicate_of_first, note_for, should_recall
from .schema import subject_key_of
from .validator import Issue, validate_fact


@dataclass
class ExtractResult:
    doc_id: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    raw_output: str = ""
    error: str | None = None
    # 补抽的痕迹。分开存是因为「补抽跑了但没补到」和「压根没补」不是一回事：
    # 前者可以让人更放心，后者只是还没做。
    recall_ran: bool = False
    recall_output: str = ""
    recall_error: str | None = None
    n_recalled: int = 0
    recall_note: str = ""

    @property
    def n_raw(self) -> int:
        return len(self.facts) + len(self.rejected)

    @property
    def n_hallucination(self) -> int:
        return sum(1 for r in self.rejected if r.get("_hallucination"))

    @property
    def facts_first(self) -> list[dict[str, Any]]:
        """第一遍抽到的（不含补抽）。评测里用来算「补抽的净增益」。"""
        return [f for f in self.facts if not f.get("_from_recall")]


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def dedupe(facts: list[dict[str, Any]], threshold: float = 0.90) -> list[dict[str, Any]]:
    """同文档内去重：同类型 + 同归并键 + 陈述高度相似，只保留置信度最高的一条。

    这一层跑在 `to_fact` 之前，手上只有模型给的原始 dict，所以调
    `subject_key_of` 而不是 `Fact.subject_key`——但两者是同一个函数，
    聚合层的 `group_facts` 也用它。三处各拼一份的后果见 `subject_key_of`。
    """
    kept: list[dict[str, Any]] = []
    for fact in sorted(facts, key=lambda f: f.get("confidence", 0), reverse=True):
        duplicate = False
        for other in kept:
            if fact.get("fact_type") != other.get("fact_type"):
                continue
            if (subject_key_of(fact.get("fact_type", ""), fact.get("payload"))
                    != subject_key_of(other.get("fact_type", ""), other.get("payload"))):
                continue
            if _similar(fact.get("statement", ""), other.get("statement", "")) >= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(fact)
    return kept


def _accept_items(items: list[dict[str, Any]], doc: dict[str, Any], result: ExtractResult,
                  context: dict[str, Any], strict: bool) -> list[dict[str, Any]]:
    """解析出来的条目过校验。两段抽取共用，避免两处校验标准漂移。"""
    accepted: list[dict[str, Any]] = []
    for item in items:
        if not item.get("fact_type"):
            result.rejected.append({**item, "_reason": "缺少 fact_type"})
            continue
        vr = validate_fact(item, raw_text=doc.get("text", ""), context=context)
        result.issues.extend(vr.issues)
        if vr.ok or (not strict and not vr.is_hallucination):
            record = dict(vr.fact)
            record["doc_id"] = doc.get("doc_id", "unknown")
            accepted.append(record)
        else:
            result.rejected.append(
                {
                    **vr.fact,
                    "_reason": "；".join(str(i) for i in vr.errors),
                    "_hallucination": vr.is_hallucination,
                }
            )
    return accepted


def extract_document(
    doc: dict[str, Any],
    adapter: LLMAdapter,
    include_examples: bool = True,
    strict: bool = True,
    recall: str = "never",
) -> ExtractResult:
    """抽取单篇文档。

    doc 需含：doc_id、customer_id、source_type、occurred_at、text。
    strict=True 时，溯源失败的记录进入 rejected，不混入 facts。

    recall：补抽模式，never / auto / always。默认 never——调用方不显式要，
    就当这层不存在，成本与行为都可预期。
    """
    doc_id = doc.get("doc_id", "unknown")
    result = ExtractResult(doc_id=doc_id)

    messages = build_messages(doc, include_examples=include_examples)
    try:
        raw = adapter.complete(messages)
    except Exception as exc:  # noqa: BLE001 - 适配器异常统一转错误
        result.error = f"调用失败：{exc}"
        return result

    result.raw_output = raw

    try:
        items = extract_json_array(raw)
    except ValueError as exc:
        result.error = str(exc)
        return result

    context = {
        "customer_id": doc.get("customer_id"),
        "source_id": doc.get("doc_id"),
        "source_type": doc.get("source_type"),
        "recorded_at": doc.get("recorded_at"),
        "occurred_at": doc.get("occurred_at"),
        "current_user_id": doc.get("current_user_id"),
    }

    first = _accept_items(items, doc, result, context, strict)

    # --- 第二段：定向补抽 ------------------------------------------------
    # 只在缺 need/concern 时跑（默认）。失败不致命：补抽只是尽力而为，
    # 第一遍的结果该是什么还是什么，所以补抽抛异常一律吞掉、只记原因。
    recalled: list[dict[str, Any]] = []
    if recall != "never" and should_recall(first, recall):
        result.recall_ran = True
        try:
            rraw = adapter.complete(build_recall_messages(doc, first))
            result.recall_output = rraw or ""
            ritems = extract_json_array(rraw or "")
        except Exception as exc:  # noqa: BLE001
            result.recall_error = str(exc)
            ritems = []
        # 只要这两类。模型偶尔会手痒补一条 timeline，收下就等于开了后门。
        ritems = [i for i in ritems if i.get("fact_type") in ("need", "concern")]
        n_rej = len(result.rejected)
        for f in _accept_items(ritems, doc, result, context, strict):
            if is_duplicate_of_first(f, first):
                continue
            f["_from_recall"] = True
            recalled.append(f)
        # 被拒的也要打标：评测里三种模式的幻觉数得分得清「第一遍编的」
        # 和「补抽编的」，否则 always 的幻觉会算到 never 头上。
        for r in result.rejected[n_rej:]:
            r["_from_recall"] = True
        result.n_recalled = len(recalled)

    result.facts = dedupe(first + recalled)
    # 补抽是可能被去重吞掉的（两遍措辞撞车），note 以最终留下的为准。
    kept = sum(1 for f in result.facts if f.get("_from_recall"))
    by_type: dict[str, int] = {}
    for f in result.facts:
        if f.get("_from_recall"):
            by_type[f.get("fact_type", "")] = by_type.get(f.get("fact_type", ""), 0) + 1
    result.n_recalled = kept
    result.recall_note = note_for(kept, by_type, result.recall_ran,
                                  empty_text=not (doc.get("text") or "").strip())
    return result


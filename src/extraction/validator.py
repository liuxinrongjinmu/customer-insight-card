"""入库前校验：拦截幻觉与非法枚举，执行确定性修正。

对应《事实模型设计》8.3 与 4.3：
- source_span 必须能在原文定位，否则视为编造，整条拒收（E004）。
- 承诺三要素不全 -> fulfillment 强制 pending，置信度乘惩罚系数（W001）。
- 置信度一律以公式重算结果为准，不采信模型自报值（W003）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .schema import (
    BARE_TITLES,
    COMMITMENT_TRIAD,
    COMMON_SPEC,
    IDENTIFIED_TITLE,
    PAYLOAD_SPEC,
    FactStatus,
    FactType,
    _to_date,
    compute_confidence,
    make_fact_id,
    required_fields,
)


ERROR = "error"
WARNING = "warning"

#: 允许缺失但不拒收的 payload 字段。
#: 顾虑的原话是「证据」，不是「事实本身」：没有原话时，事实仍可由 source_span 核对，
#: 因此保留事实并把 confidence 按缺项下调到「待确认」档，比整条丢弃更有用。
TOLERATED_EMPTY = {("concern", "verbatim_quote")}


#: 日级相对时间词。只收**无歧义**的几种：文档日期已知时，偏移量唯一确定是哪天。
#: 「本周五」「月底」「下周给答复」不在此列——它们依赖星期几、月长、或说话人的
#: 口径，差一天就造出一条假的「失信」。推不出来的交给 `r18` 提示人工补齐，
#: 而不是猜一个日期假装精确。
_DAY_OFFSETS: tuple[tuple[str, int], ...] = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明早", 1),
    ("今天", 0),
    ("今晚", 0),
)

#: 各类事实承载「该在哪天完成」的字段。
_DATE_FIELD = {
    FactType.COMMITMENT.value: "due_date",
    FactType.TIMELINE.value: "target_date",
}

#: 取「这件事是什么」的载荷文本字段，按事实类型。
_WHAT_FIELD = {
    FactType.COMMITMENT.value: "commitment_text",
    FactType.TIMELINE.value: "event",
}


def _fill_relative_date(fact: dict[str, Any]) -> None:
    """原文的「今天/明天/后天」+ 文档日期 → 绝对日期。就地修改 fact。

    ## 为什么必须补

    `c_10019` 的原话是「销售-黄：明天上午发您」，文档日是 10-04。模型把
    due_date 填成 null 完全合理——原文确实没有绝对日期。代价是这条承诺
    从此**不进任何提醒**：`r01`（已过期）和 `r16`（7 天内到期）都读 due_date，
    读不到就不报。10-04 该发的报价单，到基准日已经欠了 19 天，系统一次没提过。

    ## 为什么这不是「猜」

    「明天」在文档日已知时是推导，不是估计：10-04 的明天必然是 10-05。
    这和「月底前」完全不同——后者要判断月长、还要判断说话人指的是不是本月底，
    所以那类一律不补，交给 `r18_commitment_no_due` 提示人工确认。

    ## 为什么不覆盖已有值

    模型可能读到了原文里的绝对日期（「本周五（8 月 7 日）」），那比推算可信。
    """
    ft = fact.get("fact_type", "")
    field = _DATE_FIELD.get(ft)
    if not field:
        return
    payload = dict(fact.get("payload") or {})
    if payload.get(field):
        return
    stated = _to_date(fact.get("stated_at"))
    if not stated:
        return

    text = " ".join(filter(None, [
        fact.get("source_span"),
        payload.get(_WHAT_FIELD.get(ft, "")),
        fact.get("statement"),
    ]))
    for word, offset in _DAY_OFFSETS:
        if word in text:
            payload[field] = (stated + timedelta(days=offset)).isoformat()
            fact["payload"] = payload
            return


@dataclass
class Issue:
    code: str
    field: str
    message: str
    severity: str = ERROR

    def __str__(self) -> str:
        return f"[{self.code}] {self.field}: {self.message}"


@dataclass
class ValidationResult:
    fact: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def is_hallucination(self) -> bool:
        return any(i.code == "E004" for i in self.issues)


# ---------------------------------------------------------------------------
# 文本归一化：用于 source_span 定位
# ---------------------------------------------------------------------------

_WS = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[，。、；：？！“”‘’（）《》…—,.;:?!\"'()\[\]]")


def _normalize(text: str) -> str:
    """去空白与标点，仅保留有意义的字符，降低定位误判。"""
    return _PUNCT.sub("", _WS.sub("", text or ""))


def locate_span(raw_text: str, span: str) -> tuple[int, int] | None:
    """在原文中定位片段，返回 (start, end)。定位失败返回 None。

    先精确匹配；失败则用归一化文本匹配，再映射回原坐标。
    """
    if not span:
        return None
    idx = raw_text.find(span)
    if idx >= 0:
        return idx, idx + len(span)

    # 归一化匹配：构造 原坐标 -> 归一化坐标 的映射
    norm_chars: list[str] = []
    mapping: list[int] = []
    for pos, ch in enumerate(raw_text):
        if _WS.match(ch) or _PUNCT.match(ch):
            continue
        norm_chars.append(ch)
        mapping.append(pos)
    norm_raw = "".join(norm_chars)
    norm_span = _normalize(span)
    if not norm_span:
        return None
    n_idx = norm_raw.find(norm_span)
    if n_idx < 0:
        return None
    return mapping[n_idx], mapping[n_idx + len(norm_span) - 1] + 1


# ---------------------------------------------------------------------------
# 主校验逻辑
# ---------------------------------------------------------------------------

def _sanitize_payload_enums(fact: dict[str, Any]) -> list[str]:
    """把越界的 payload 枚举值置空，返回被修正的字段名。

    为什么不整条拒收：category / stage / signal_type 只是分类标签，
    标签错了不代表这条事实是假的。「客户在接触云枢」不会因为 stage 填错就变成不存在。
    模型最容易在封闭枚举上猜词（把 priority 填成 medium、our_position 填成 considered），
    为此丢掉整条事实，等于用分类问题惩罚事实抽取。
    结构字段（fact_type / subject_type / source_type）仍然硬拒——那些错了，事实就归错类了。
    """
    payload = fact.get("payload")
    if not isinstance(payload, dict):
        return []
    spec_map = PAYLOAD_SPEC.get(fact.get("fact_type", ""), {})
    repaired: list[str] = []
    for name, spec in spec_map.items():
        value = payload.get(name)
        if value is None or not spec.values:
            continue
        if value not in spec.values:
            payload[name] = None
            repaired.append(name)
    return repaired


def normalize_fact(raw: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """补齐缺省值并做确定性修正，返回新字典（不修改入参）。"""
    ctx = context or {}
    fact = dict(raw)

    fact.setdefault("fact_id", make_fact_id())
    fact.setdefault("status", FactStatus.ACTIVE.value)
    fact.setdefault("lang", ctx.get("lang", "zh-CN"))
    fact.setdefault("tenant_id", ctx.get("tenant_id", "t_001"))
    fact.setdefault("customer_id", ctx.get("customer_id"))
    fact.setdefault("source_id", ctx.get("source_id"))
    fact.setdefault("source_type", ctx.get("source_type"))
    fact.setdefault("subject_type", "account")
    fact.setdefault("tags", [])

    if not fact.get("recorded_at"):
        fact["recorded_at"] = ctx.get("recorded_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 文档级元数据是可靠时间源：模型未给出事件时间时，用文档发生时间兜底，
    # 但精度降为 unknown，避免把月级信息当成日级事实。
    if not fact.get("stated_at") and ctx.get("occurred_at"):
        fact["stated_at"] = ctx["occurred_at"]
        if not fact.get("stated_at_precision"):
            fact["stated_at_precision"] = "unknown"
    if not fact.get("stated_at_precision"):
        fact["stated_at_precision"] = "unknown"

    # 归属实体 ID：能由文档上下文确定的就注入，不能确定的留空待实体链接。
    # 模型只负责判断「这条事实挂在客户/商机/项目/联系人上」，不负责给 ID。
    if not fact.get("subject_id"):
        st = fact.get("subject_type")
        if st == "account":
            fact["subject_id"] = fact.get("customer_id")
        elif st == "opportunity":
            fact["subject_id"] = ctx.get("opportunity_id")
        elif st == "project":
            fact["subject_id"] = ctx.get("project_id")

    # 相对日期兜底。必须放在承诺三要素检查之前：due_date 补上了，
    # triad_gap 就不该再把它当缺项。
    _fill_relative_date(fact)

    # 承诺三要素不全 -> fulfillment 强制 pending
    # 我方承诺的责任方由系统注入当前操作人，不要求模型从文本中猜
    if fact.get("fact_type") == FactType.COMMITMENT.value:
        payload = dict(fact.get("payload") or {})
        if not payload.get("owner_id") and payload.get("direction") == "we_owe":
            if ctx.get("current_user_id"):
                payload["owner_id"] = ctx["current_user_id"]
        triad_gap = [
            k for k in COMMITMENT_TRIAD
            if payload.get(k) in (None, "")
        ]
        if triad_gap and payload.get("fulfillment") != "waived":
            payload["fulfillment"] = "pending"
        fact["payload"] = payload

    # 干系人的身份：知道是谁，还是只知道哪个位置。
    # 模型很可能不给这个字段（老提示词里没有它），所以在这里按有无姓名推断，
    # 而不是把它设成必填——设成必填等于让所有未升级的调用方全被拒收。
    # 推断放在置信度之前：完整度要按推断后的 identity 算，否则「只有职务」
    # 会被当成「姓名缺失」扣一档，而它其实一项不缺。
    if fact.get("fact_type") == FactType.STAKEHOLDER.value:
        payload = dict(fact.get("payload") or {})
        # 把职务错填进姓名：不拒收，改判。拒收会丢掉「签字这一环」这个信息，
        # 而这正是这条事实唯一的用处；改判既保住信息，又不制造一个假人。
        bare = str(payload.get("name") or "").strip()
        if bare in BARE_TITLES and not str(payload.get("title") or "").strip():
            payload["title"] = bare
            payload["name"] = ""
            payload["identified_as"] = IDENTIFIED_TITLE
            fact["_title_repaired"] = bare
        if not payload.get("identified_as"):
            has_name = bool(str(payload.get("name") or "").strip())
            has_title = bool(str(payload.get("title") or "").strip())
            payload["identified_as"] = "person" if has_name else (
                IDENTIFIED_TITLE if has_title else "person")
        fact["payload"] = payload

    # 枚举越界先修字段，再算置信度，否则算的是未修正 payload 的完整度
    repaired_enums = _sanitize_payload_enums(fact)
    if repaired_enums:
        fact["_enum_repaired"] = repaired_enums

    # 置信度以公式为准
    fact_type = fact.get("fact_type", "")
    payload = fact.get("payload") or {}
    if fact_type in PAYLOAD_SPEC and fact.get("source_type") and fact.get("confidence_basis"):
        computed = compute_confidence(
            fact_type,
            fact["source_type"],
            fact["confidence_basis"],
            payload,
        )
        fact["_recomputed_confidence"] = computed

    return fact


def validate_fact(
    raw: dict[str, Any],
    raw_text: str = "",
    context: dict[str, Any] | None = None,
) -> ValidationResult:
    """校验单条事实。

    raw_text 用于溯源定位；为空时跳过 E004 检查。
    """
    issues: list[Issue] = []
    fact = normalize_fact(raw, context)

    # --- 1. 通用字段：必填与枚举 ---
    for name, spec in COMMON_SPEC.items():
        value = fact.get(name)
        if spec.required and (value is None or (isinstance(value, str) and not value.strip())):
            issues.append(Issue("E001", name, "必填字段为空"))
            continue
        if value is None:
            continue
        if spec.values and value not in spec.values:
            issues.append(Issue("E002", name, f"枚举值非法：{value!r}，允许值 {list(spec.values)}"))
        if spec.kind == "str" and not isinstance(value, str):
            issues.append(Issue("E003", name, f"类型应为字符串，实际 {type(value).__name__}"))
        if spec.max_len and isinstance(value, str) and len(value) > spec.max_len:
            issues.append(Issue("W002", name, f"长度 {len(value)} 超过建议上限 {spec.max_len}", WARNING))

    # --- 2. payload：必填、枚举、未知字段 ---
    fact_type = fact.get("fact_type", "")
    spec_map = PAYLOAD_SPEC.get(fact_type, {})
    payload = fact.get("payload") or {}
    if not isinstance(payload, dict):
        issues.append(Issue("E003", "payload", "payload 必须为对象"))
        payload = {}

    # 被置空的枚举字段只告警，不再按「必填为空」二次拒绝——
    # 否则修复等于没修，事实还是会被 E008 拦掉。
    repaired = set(fact.pop("_enum_repaired", []) or [])
    if repaired:
        issues.append(Issue(
            "W009", "payload",
            f"枚举越界已置空 {sorted(repaired)}，事实保留，置信度按缺项下调", WARNING,
        ))
    required_here = set(required_fields(fact_type, payload))
    for name, spec in spec_map.items():
        if name in repaired:
            continue
        value = payload.get(name)
        if name in required_here and (value is None or (isinstance(value, str) and not value.strip())):
            if (fact_type, name) in TOLERATED_EMPTY:
                issues.append(Issue(
                    "W010", f"payload.{name}",
                    "缺少客户原话，事实保留但置信度下调，展示时须标注「无原话」", WARNING,
                ))
                continue
            issues.append(Issue("E008", f"payload.{name}", "payload 必填字段为空"))
            continue
        if value is None:
            continue
        if spec.values and value not in spec.values:
            issues.append(
                Issue("E002", f"payload.{name}", f"枚举值非法：{value!r}", WARNING)
            )

    unknown = set(payload) - set(spec_map)
    if unknown:
        issues.append(
            Issue("W001", "payload", f"包含未定义字段，将被忽略：{sorted(unknown)}", WARNING)
        )
        payload = {k: v for k, v in payload.items() if k in spec_map}
        fact["payload"] = payload

    # --- 3. 日期与时序 ---
    for name in ("stated_at", "valid_until"):
        if fact.get(name) and _to_date(fact[name]) is None:
            issues.append(Issue("E005", name, f"日期格式无法解析：{fact[name]!r}"))

    # --- 4. source_span 真伪（幻觉检测）---
    span = fact.get("source_span")
    if raw_text and span:
        located = locate_span(raw_text, span)
        if located is None:
            issues.append(
                Issue(
                    "E004",
                    "source_span",
                    "原文中无法定位该片段，判定为编造，整条拒收",
                )
            )
        else:
            fact["span_start"], fact["span_end"] = located

    # --- 5. subject 配对 ---
    # 只把「矛盾」判为错误：none 却有 id。
    # 缺 id 是抽取阶段的常态（模型不可能知道商机 ID），由实体链接补齐，降级为告警。
    st, sid = fact.get("subject_type"), fact.get("subject_id")
    if st == "none" and sid:
        issues.append(Issue("E006", "subject_id", "subject_type 为 none 时不应有 subject_id", WARNING))
    if st != "none" and not sid:
        issues.append(
            Issue(
                "W008",
                "subject_id",
                f"subject_type 为 {st} 但未解析到 ID，待实体链接或文档上下文补齐",
                WARNING,
            )
        )

    # --- 6. 置信度一致性 ---
    computed = fact.pop("_recomputed_confidence", None)
    if computed is None:
        issues.append(
            Issue(
                "W005",
                "confidence",
                "缺项过多，按设计不应产出该事实",
                WARNING,
            )
        )
    else:
        reported = fact.get("confidence")
        if isinstance(reported, (int, float)) and abs(float(reported) - computed) > 0.05:
            issues.append(
                Issue(
                    "W003",
                    "confidence",
                    f"自报值 {reported} 与公式值 {computed} 偏差过大，已采用公式值",
                    WARNING,
                )
            )
        fact["confidence"] = computed

    # --- 7. 类型专属补充检查 ---
    if fact_type == FactType.CONCERN.value:
        quote = payload.get("verbatim_quote")
        if raw_text and quote and locate_span(raw_text, quote) is None:
            issues.append(
                Issue(
                    "W004",
                    "payload.verbatim_quote",
                    "原话并非原文子串，可能已被改写，建议人工复核",
                    WARNING,
                )
            )

    if fact_type == FactType.COMMITMENT.value:
        gap = [k for k in COMMITMENT_TRIAD if not payload.get(k)]
        if gap:
            issues.append(
                Issue(
                    "W001",
                    "payload",
                    f"承诺三要素缺失 {gap}，fulfillment 已强制置为 pending",
                    WARNING,
                )
            )

    if fact_type == FactType.RISK_SIGNAL.value:
        if not payload.get("baseline"):
            issues.append(
                Issue("W006", "payload.baseline", "缺少基线，单独观察值不构成信号", WARNING)
            )

    if fact_type == FactType.STAKEHOLDER.value:
        # 职务当姓名被改判时要说出来：静默改判等于让人以为「董事长」是个人名。
        repaired_title = fact.pop("_title_repaired", None)
        if repaired_title:
            issues.append(
                Issue(
                    "W011",
                    "payload.name",
                    f"「{repaired_title}」是职务不是姓名，已改判为「只知职务」"
                    "（identified_as=title）；补上人名后两条会自动合并",
                    WARNING,
                )
            )
        if not payload.get("contact_id"):
            issues.append(
                Issue(
                    "W007",
                    "payload.contact_id",
                    "未解析到联系人 ID，需实体链接补齐后才能与其他事实关联",
                    WARNING,
                )
            )

    return ValidationResult(fact=fact, issues=issues)


def validate_batch(
    facts: list[dict[str, Any]],
    raw_text: str = "",
    context: dict[str, Any] | None = None,
) -> list[ValidationResult]:
    return [validate_fact(f, raw_text=raw_text, context=context) for f in facts]

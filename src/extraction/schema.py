"""事实模型：枚举、字段规格与置信度计算。

设计原则（对应《事实模型设计》第三、七章）：
1. 表驱动：七类事实的字段规格由 PAYLOAD_SPEC 描述，新增类型只加配置，不改校验代码。
2. 零依赖：仅用标准库，便于在任何环境跑通评测。
3. 双时间轴：stated_at（事件时间）与 recorded_at（入库时间）分离。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 枚举定义
# ---------------------------------------------------------------------------

class FactType(str, Enum):
    NEED = "need"
    CONCERN = "concern"
    COMMITMENT = "commitment"
    STAKEHOLDER = "stakeholder"
    COMPETITION = "competition"
    TIMELINE = "timeline"
    RISK_SIGNAL = "risk_signal"


class SubjectType(str, Enum):
    ACCOUNT = "account"
    CONTACT = "contact"
    OPPORTUNITY = "opportunity"
    PROJECT = "project"
    NONE = "none"


class SourceType(str, Enum):
    """来源类型同时决定置信度的来源权重。"""
    CONTRACT = "contract"
    CRM_FIELD = "crm_field"
    MANUAL = "manual"
    MEETING_MINUTES = "meeting_minutes"
    VISIT_NOTE = "visit_note"
    TICKET = "ticket"
    EMAIL = "email"
    CALL_TRANSCRIPT = "call_transcript"
    IM_MESSAGE = "im_message"


class Precision(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    UNKNOWN = "unknown"


class ConfidenceBasis(str, Enum):
    """表达直接性。比 confidence 数值更能影响用户信任。"""
    DIRECT_QUOTE = "direct_quote"
    PARAPHRASE = "paraphrase"
    INFERRED = "inferred"


class FactStatus(str, Enum):
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RETRACTED = "retracted"


class Polarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class Magnitude(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --- payload 级枚举 ---

NEED_CATEGORY = ("function", "performance", "integration", "compliance", "cost", "service", "other")
EXPLICITNESS = ("explicit", "inferred")
PRIORITY = ("must", "should", "could")

CONCERN_CATEGORY = ("price", "cycle", "risk", "trust", "internal_resistance", "fit", "compliance", "support", "other")
SEVERITY = ("blocker", "high", "medium", "low")

DIRECTION = ("we_owe", "customer_owes")
FULFILLMENT = ("pending", "fulfilled", "overdue", "broken", "waived")

DEAL_ROLE = ("decision_maker", "influencer", "user", "champion", "blocker", "gatekeeper")
ATTITUDE = ("supportive", "neutral", "resistant", "unknown")
INFLUENCE = ("high", "medium", "low")
#: 干系人的身份是「谁」还是「哪个位置」。
#: person = 知道是谁（有姓名）；title = 只知道职务（「最终要董事长签字」）。
IDENTIFIED_AS = ("person", "title")
IDENTIFIED_TITLE = "title"

#: 光秃秃的职务名。模型最常犯的错是把这些填进 `name`——一旦落库就变成
#: 一个叫「董事长」的人，等真名进来再记一次，决策链上会出现两个董事长。
#: 只列「单独出现时一定是职务」的词；「王总」「李经理」这类带姓的不算。
BARE_TITLES = frozenset({
    "董事长", "总经理", "副总", "副总经理", "总监", "经理", "主任", "部长",
    "科长", "厂长", "处长", "局长", "院长", "总裁", "副总裁", "负责人",
    "采购", "财务", "老板",
})

COMPETITOR_STAGE = ("mentioned", "evaluated", "shortlisted", "preferred", "selected", "eliminated")
OUR_POSITION = ("leading", "even", "trailing", "unknown")

TIMELINE_DRIVER = ("budget", "procurement", "compliance", "business", "contract")
HARDNESS = ("hard", "soft")

SIGNAL_TYPE = (
    "response_delay", "contact_change", "decision_chain_expand", "price_pressure",
    "competitor_entry", "champion_loss", "requirement_drift", "silence",
)
DETECTED_BY = ("rule", "model", "human")


# ---------------------------------------------------------------------------
# 字段规格（表驱动）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSpec:
    kind: str                     # "str" | "enum" | "date" | "bool" | "array" | "number"
    required: bool = False
    values: tuple[str, ...] = ()
    max_len: int | None = None
    note: str = ""


COMMON_SPEC: dict[str, FieldSpec] = {
    "fact_id": FieldSpec("str", required=True),
    "tenant_id": FieldSpec("str", required=True),
    "customer_id": FieldSpec("str", required=True),
    "fact_type": FieldSpec("enum", required=True, values=tuple(t.value for t in FactType)),
    "subject_type": FieldSpec("enum", required=True, values=tuple(t.value for t in SubjectType)),
    "subject_id": FieldSpec("str"),
    "opportunity_id": FieldSpec("str"),
    "project_id": FieldSpec("str"),
    "statement": FieldSpec("str", required=True, max_len=80, note="完整陈述句，主语明确"),
    "payload": FieldSpec("dict", required=True),
    "polarity": FieldSpec("enum", values=tuple(p.value for p in Polarity)),
    "magnitude": FieldSpec("enum", values=tuple(m.value for m in Magnitude)),
    "lang": FieldSpec("str", required=True),
    "source_type": FieldSpec("enum", required=True, values=tuple(s.value for s in SourceType)),
    "source_id": FieldSpec("str", required=True),
    "source_span": FieldSpec("str", required=True, max_len=200, note="必须是原文连续子串"),
    "span_start": FieldSpec("number"),
    "span_end": FieldSpec("number"),
    "stated_at": FieldSpec("date", required=True),
    "stated_at_precision": FieldSpec("enum", required=True, values=tuple(p.value for p in Precision)),
    "recorded_at": FieldSpec("str", required=True, note="ISO-8601 datetime"),
    "confidence": FieldSpec("number", required=True),
    "confidence_basis": FieldSpec("enum", required=True, values=tuple(b.value for b in ConfidenceBasis)),
    "status": FieldSpec("enum", required=True, values=tuple(s.value for s in FactStatus)),
    "verified_by": FieldSpec("str"),
    "verified_at": FieldSpec("str"),
    "valid_until": FieldSpec("date"),
    "supersedes": FieldSpec("str"),
    "superseded_by": FieldSpec("str"),
    "retract_reason": FieldSpec("str"),
    "tags": FieldSpec("array"),
}

PAYLOAD_SPEC: dict[str, dict[str, FieldSpec]] = {
    FactType.NEED.value: {
        "need_text": FieldSpec("str", required=True),
        "category": FieldSpec("enum", required=True, values=NEED_CATEGORY),
        "explicitness": FieldSpec("enum", required=True, values=EXPLICITNESS),
        "priority": FieldSpec("enum", values=PRIORITY),
        "raised_by": FieldSpec("str"),
        "scope": FieldSpec("str"),
    },
    FactType.CONCERN.value: {
        "concern_text": FieldSpec("str", required=True),
        "verbatim_quote": FieldSpec("str", required=True, note="客户原话，不得改写"),
        "category": FieldSpec("enum", required=True, values=CONCERN_CATEGORY),
        "severity": FieldSpec("enum", required=True, values=SEVERITY),
        "raised_by": FieldSpec("str"),
        "is_resolved": FieldSpec("bool", required=True),
        "resolution_note": FieldSpec("str"),
    },
    FactType.COMMITMENT.value: {
        "commitment_text": FieldSpec("str", required=True),
        "direction": FieldSpec("enum", required=True, values=DIRECTION),
        # 抽取阶段不强制：we_owe 由系统注入当前操作人，customer_owes 由实体链接补齐。
        # 责任方缺失仍会通过 COMMITMENT_TRIAD 触发 pending + 0.8 惩罚，不会被静默放过。
        "owner_id": FieldSpec("str", note="we_owe 由系统注入；customer_owes 由实体链接补齐"),
        "due_date": FieldSpec("date"),
        "due_precision": FieldSpec("enum", values=tuple(p.value for p in Precision)),
        "fulfillment": FieldSpec("enum", required=True, values=FULFILLMENT),
        "fulfilled_at": FieldSpec("date"),
        "evidence_fact_id": FieldSpec("str"),
    },
    FactType.STAKEHOLDER.value: {
        # 抽取阶段只能给出姓名等线索，contact_id 由实体链接在入库后补齐。
        # 硬要求会让人名级线索被整条拒收，得不偿失。
        "contact_id": FieldSpec("str", note="由实体链接在入库后补齐"),
        # name 与 title 都不是无条件必填：二者必居其一，由 identified_as 决定。
        # 判据在 required_fields() 里，只有一处——见该函数的说明。
        "name": FieldSpec("str", note="有姓名就填；被 hard 校验禁止用来装职务"),
        "identified_as": FieldSpec(
            "enum", values=IDENTIFIED_AS,
            note="person 知道是谁 / title 只知道职务；缺省按有无 name 推断"),
        "title": FieldSpec("str", note="职务；identified_as=title 时必填"),
        "role_in_deal": FieldSpec("enum", required=True, values=DEAL_ROLE),
        "attitude": FieldSpec("enum", required=True, values=ATTITUDE),
        "influence": FieldSpec("enum", values=INFLUENCE),
        "reports_to": FieldSpec("str"),
        "last_contacted_at": FieldSpec("date"),
    },
    FactType.COMPETITION.value: {
        "competitor_name": FieldSpec("str", required=True),
        "stage": FieldSpec("enum", required=True, values=COMPETITOR_STAGE),
        "customer_view": FieldSpec("str"),
        "our_position": FieldSpec("enum", values=OUR_POSITION),
        "dimensions": FieldSpec("array"),
    },
    FactType.TIMELINE.value: {
        "event": FieldSpec("str", required=True),
        "target_date": FieldSpec("date", required=True),
        "precision": FieldSpec("enum", required=True, values=tuple(p.value for p in Precision)),
        "hardness": FieldSpec("enum", required=True, values=HARDNESS),
        "driver": FieldSpec("enum", required=True, values=TIMELINE_DRIVER),
    },
    FactType.RISK_SIGNAL.value: {
        "signal_type": FieldSpec("enum", required=True, values=SIGNAL_TYPE),
        "baseline": FieldSpec("str", required=True),
        "observed": FieldSpec("str", required=True),
        "severity": FieldSpec("enum", required=True, values=SEVERITY),
        "detected_by": FieldSpec("enum", required=True, values=DETECTED_BY),
    },
}

#: 承诺三要素。缺任一则 fulfillment 强制 pending、置信度乘 0.8。
COMMITMENT_TRIAD = ("commitment_text", "owner_id", "due_date")


# ---------------------------------------------------------------------------
# 保鲜期（天）。None 表示不按时间自动过期。
# ---------------------------------------------------------------------------

FRESHNESS_DAYS: dict[str, int | None] = {
    FactType.NEED.value: 180,
    FactType.CONCERN.value: 365,
    FactType.COMMITMENT.value: None,
    FactType.STAKEHOLDER.value: 365,
    FactType.COMPETITION.value: 90,
    FactType.TIMELINE.value: None,
    FactType.RISK_SIGNAL.value: 30,
}

#: 冲突归并键构成（对应设计文档 6.1）。
#:
#: 一个必须守住的约束：**键只能用抽取阶段就能拿到的字段**。
#: 早期版本把 stakeholder 的键设成 contact_id、把 commitment 的键设成
#: (direction, owner_id)，结果是：contact_id 在抽取阶段恒为空，一个客户的
#: 所有干系人全挤进同一组；owner_id 对 we_owe 由系统统一注入为当前用户，
#: 所有我方承诺也全挤进同一组。两种都会造出大量假冲突。
#:
#: 键宁可粗，不可错。粗了自有 conflict._same_subject 做精判兜底；
#: 错了则连兜底的机会都没有——两件事根本进不了同一组。
SUBJECT_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    FactType.NEED.value: ("category",),
    FactType.CONCERN.value: ("category",),
    FactType.COMMITMENT.value: ("direction",),
    FactType.STAKEHOLDER.value: ("name",),
    FactType.COMPETITION.value: ("competitor_name",),
    FactType.TIMELINE.value: ("driver",),
    FactType.RISK_SIGNAL.value: ("signal_type", "detected_by"),
}


def _key_part(value: Any) -> str:
    """归并键里的一段。

    `None` 和「字段缺失」都当成空——两者都表示「不知道」。
    不能直接 `str(v)`：那会拼出 `"None"`，于是「name 为 null」和「没有 name」
    两件事被分进两组，而它们其实是同一件不确定的事。
    """
    if value is None:
        return ""
    return str(value)


def stakeholder_identity(payload: dict[str, Any] | None) -> str:
    """干系人的「是谁」。**唯一**的身份判定实现。

    姓名优先；没有姓名就用职务，并用 `@` 前缀标出来。

    为什么允许「只有职务」：现实里最常见的一句话是「最终要董事长签字」——
    知道位置、不知道人。老版本把它整条丢掉（`name` 必填 → 缺姓名即拒收），
    于是决策链上少了「签字这一环」，而系统还以为这单没摸到决策人。
    **把「只知道职务」记成一条标注清楚的记录，比丢掉它准确。**
    但也绝不能把「董事长」填进 `name` —— 那是把职务当成人，
    同一个人后续用真名再记一次就会变成两个关键人。

    `@` 前缀的两个作用：① 「董事长」这个职务不会与真名叫「董事长」的人撞键；
    ② 键里一眼看得出这条指向位置而不是人。
    """
    data = payload or {}
    name = str(data.get("name") or "").strip()
    if name:
        return name
    title = str(data.get("title") or "").strip()
    return f"@{title}" if title else ""


def subject_key_of(fact_type: str, payload: dict[str, Any] | None) -> str:
    """归并键的**唯一**拼装实现。

    为什么要有这个函数，而不是让各处自己 `"|".join(...)`：
    同一件事在三个地方要判断「是不是同一组事实」——

    | 位置 | 用途 | 判错的后果 |
    |---|---|---|
    | `extractor.dedupe` | 同文档内去重 | 同一句话抽两遍，卡片上两条一样的 |
    | `merge.group_facts` | 跨文档归并 | 一条承诺被当成两件事，挂着两个到期日 |
    | `Fact.subject_key` | 组与组的标识 | 组 id 对不上，界面上的键和引擎的键不是一个 |

    三处各拼一份，只要有人只改一边，就会出现「抽取时算两条、聚合时算一组」。
    这种错不报错，只是在某个客户的卡上多出一个重复条目——最难被发现的那类。

    输入是 `payload` 字典而不是 `Fact`，因为抽取层的去重跑在 `to_fact` 之前，
    那时候手上只有模型给的原始 dict。

    stakeholder 是唯一一个「键有 fallback」的类型：姓名可以缺，缺了就用职务，
    所以它不查 `SUBJECT_KEY_FIELDS` 表，而是走 `stakeholder_identity`。
    别的类型没有第二身份可用，多写一张 fallback 表只会多一处会不同步的地方。
    """
    data = payload or {}
    if fact_type == FactType.STAKEHOLDER.value:
        return stakeholder_identity(data)
    keys = SUBJECT_KEY_FIELDS.get(fact_type, ())
    return "|".join(_key_part(data.get(k)) for k in keys)


# ---------------------------------------------------------------------------
# 置信度计算（设计文档 7.2 / 7.3）
# ---------------------------------------------------------------------------

SOURCE_RELIABILITY: dict[str, float] = {
    SourceType.CONTRACT.value: 1.00,
    SourceType.CRM_FIELD.value: 0.95,
    SourceType.MANUAL.value: 0.90,
    SourceType.MEETING_MINUTES.value: 0.90,
    SourceType.VISIT_NOTE.value: 0.85,
    SourceType.TICKET.value: 0.85,
    SourceType.EMAIL.value: 0.80,
    SourceType.CALL_TRANSCRIPT.value: 0.75,
    SourceType.IM_MESSAGE.value: 0.60,
}

EXPRESSION_WEIGHT: dict[str, float] = {
    ConfidenceBasis.DIRECT_QUOTE.value: 1.00,
    ConfidenceBasis.PARAPHRASE.value: 0.85,
    ConfidenceBasis.INFERRED.value: 0.60,
}

#: 缺失必填项数量 -> 完整度权重
COMPLETENESS_WEIGHT: dict[int, float] = {0: 1.00, 1: 0.80, 2: 0.55}

W_SOURCE = 0.4
W_EXPRESSION = 0.4
W_COMPLETENESS = 0.2

#: 展示阈值
THRESHOLD_SHOW = 0.85
THRESHOLD_CANDIDATE = 0.65

#: 缺项承诺的置信度惩罚系数
INCOMPLETE_COMMITMENT_PENALTY = 0.8


def required_fields(fact_type: str, payload: dict[str, Any] | None) -> tuple[str, ...]:
    """本条事实的必填字段。**唯一**的必填判定实现。

    绝大多数类型就是 `PAYLOAD_SPEC` 里 `required=True` 的那些，函数照抄即可。
    只有一个例外：stakeholder 的「姓名」与「职务」是**二者必居其一**——
    「最终要董事长签字」只知位置不知人，把 name 设成必填等于让这条信息
    被整条拒收（这正是 P2-6 要修的那个结构性缺口）。

    为什么必须只有这一处：校验层按它判 E008，置信度层按它算缺失项数。
    两处各写一份 `if payload.get("identified_as") == "title"`，只要有人只改
    一边，就会出现「校验说必填、置信度说齐了」——事实被拒收但置信度满分，
    或者反过来：一条不合法的记录带着满分进了库。
    """
    spec = PAYLOAD_SPEC.get(fact_type, {})
    base = tuple(name for name, fs in spec.items() if fs.required)
    if fact_type == FactType.STAKEHOLDER.value:
        data = payload or {}
        if data.get("identified_as") == IDENTIFIED_TITLE:
            # 只知职务：要职务，不要姓名。
            return tuple(n for n in base if n != "name") + ("title",)
        # 知道是谁：必须有姓名。没有姓名也没有职务的记录指向不到任何东西，
        # 那种记录唯一的用途是让决策链上多一个「未具名」，所以拒收。
        return ("name",) + tuple(n for n in base if n != "name")
    return base


def missing_required(fact_type: str, payload: dict[str, Any]) -> list[str]:
    """返回 payload 中缺失的必填字段名列表。"""
    missing: list[str] = []
    for name in required_fields(fact_type, payload):
        value = payload.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def compute_confidence(
    fact_type: str,
    source_type: str,
    basis: str,
    payload: dict[str, Any],
) -> float | None:
    """按三因子公式计算置信度。

    返回 None 表示缺项过多，不应产出该事实（设计文档 7.2：缺 3 项及以上不产出）。
    """
    missing = missing_required(fact_type, payload)

    # 承诺三要素另算：缺项不会导致不产出，但会打惩罚系数
    triad_gap = 0
    if fact_type == FactType.COMMITMENT.value:
        triad_gap = sum(
            1 for k in COMMITMENT_TRIAD
            if payload.get(k) is None or (isinstance(payload.get(k), str) and not str(payload.get(k)).strip())
        )

    effective_missing = max(len(missing), triad_gap)
    if effective_missing >= 3:
        return None

    w_source = SOURCE_RELIABILITY.get(source_type, 0.70)
    w_expr = EXPRESSION_WEIGHT.get(basis, 0.60)
    w_complete = COMPLETENESS_WEIGHT.get(effective_missing, 0.55)

    conf = (
        W_SOURCE * w_source
        + W_EXPRESSION * w_expr
        + W_COMPLETENESS * w_complete
    )

    if triad_gap > 0:
        conf *= INCOMPLETE_COMMITMENT_PENALTY

    return round(min(max(conf, 0.0), 1.0), 4)


def display_tier(confidence: float) -> str:
    """返回展示档位：show / confirm / candidate。"""
    if confidence >= THRESHOLD_SHOW:
        return "show"
    if confidence >= THRESHOLD_CANDIDATE:
        return "confirm"
    return "candidate"


# ---------------------------------------------------------------------------
# 事实对象
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    fact_type: str
    customer_id: str
    statement: str
    payload: dict[str, Any]
    source_type: str
    source_id: str
    source_span: str
    stated_at: str
    stated_at_precision: str
    confidence: float
    confidence_basis: str
    tenant_id: str = "t_001"
    subject_type: str = SubjectType.ACCOUNT.value
    subject_id: str | None = None
    opportunity_id: str | None = None
    project_id: str | None = None
    polarity: str | None = None
    magnitude: str | None = None
    lang: str = "zh-CN"
    span_start: int | None = None
    span_end: int | None = None
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    status: str = FactStatus.ACTIVE.value
    verified_by: str | None = None
    verified_at: str | None = None
    valid_until: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    retract_reason: str | None = None
    tags: list[str] = field(default_factory=list)
    fact_id: str | None = None

    def __post_init__(self) -> None:
        if self.fact_id is None:
            self.fact_id = make_fact_id()

    # --- 便捷属性 ---

    @property
    def subject_key(self) -> str:
        return subject_key_of(self.fact_type, self.payload)

    @property
    def tier(self) -> str:
        return display_tier(self.confidence)

    def freshness_deadline(self) -> date | None:
        """按类型保鲜期推算失效日。valid_until 优先。"""
        if self.valid_until:
            return _to_date(self.valid_until)
        days = FRESHNESS_DAYS.get(self.fact_type)
        base = _to_date(self.stated_at)
        if days is None or base is None:
            return None
        return date.fromordinal(base.toordinal() + days)

    def is_expired(self, today: date | None = None) -> bool:
        deadline = self.freshness_deadline()
        if deadline is None:
            return False
        return (today or date.today()) > deadline

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fact":
        return cls(**data)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def make_fact_id() -> str:
    """生成 ULID 风格 ID（时间有序 + 随机后缀），无需第三方库。"""
    import os
    import time

    ts = int(time.time() * 1000)
    head = ""
    for _ in range(10):
        head = _ALPHABET[ts % 32] + head
        ts //= 32
    tail = "".join(_ALPHABET[b % 32] for b in os.urandom(16))
    return head + tail


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def enum_members(enum_cls: Iterable[Enum]) -> tuple[str, ...]:
    return tuple(m.value for m in enum_cls)

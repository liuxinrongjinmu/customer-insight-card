"""冲突裁决：区分「演进」与「冲突」，并给出裁决理由。

Stage 1 判断「这条事实真不真」，Stage 2 判断「这些事实放在一起矛不矛盾」。
后者的错误代价是不对称的：

- 把演进当冲突 → 系统天天报红标，用户点三次之后学会无视所有标记，等于没做
- 把冲突当演进 → 系统静默挑一个留下，用户看到的是自信的错误答案，比没有更糟

判据只有一条：**这个字段的变化有没有内在方向**。

有序字段（competition.stage、commitment.fulfillment、concern.severity、
concern.is_resolved、need.priority、stakeholder.attitude）的变化一定是
「事情往前走了」，是演进；旧值置 superseded，不打扰用户。

无序字段（timeline.target_date、commitment.due_date、金额与百分比、
stakeholder.role_in_deal）的变化没有方向，两个来源说法不一致就是冲突，
两条都保留、标 conflicted、交给人判断。

这条判据不是从数据里学出来的，是从「哪种错更贵」推出来的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..extraction.schema import Fact, _to_date, stakeholder_identity


# ---------------------------------------------------------------------------
# 字段的「有序 / 无序」划分
# ---------------------------------------------------------------------------

#: 取值有内在顺序的字段。变化 = 演进。
#: 顺序由弱到强；不在序列里的取值（如 unknown、eliminated）单独处理。
ORDERED_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("need", "priority"): ("could", "should", "must"),
    ("concern", "severity"): ("low", "medium", "high", "blocker"),
    ("commitment", "fulfillment"): ("pending", "overdue", "broken", "waived", "fulfilled"),
    ("stakeholder", "attitude"): ("resistant", "neutral", "supportive"),
    ("competition", "stage"): ("mentioned", "evaluated", "shortlisted", "preferred", "selected"),
}

#: 终态类取值。进入终态是演进，但从终态退回是「回退」，要单独标出来。
TERMINAL_VALUES: dict[tuple[str, str], set[str]] = {
    ("commitment", "fulfillment"): {"fulfilled", "broken", "waived"},
    ("competition", "stage"): {"selected", "eliminated"},
}

#: 表示「信息缺失」的取值。任一侧缺失就不构成冲突——没有可比的东西。
MISSING_VALUES = {"", "unknown", "none", "null"}

#: 布尔字段：False -> True 是演进，True -> False 是回退。
BOOLEAN_FIELDS: dict[tuple[str, str], str] = {
    ("concern", "is_resolved"): "顾虑已被解决",
}

#: 比较前要归一化的同义取值。
VALUE_ALIAS = {
    "decision_maker": "decision_maker",
    "决策人": "decision_maker",
    "拍板人": "decision_maker",
}

# ---------------------------------------------------------------------------
# 两个窗口，是「改期」与「矛盾」的唯一分界线
# ---------------------------------------------------------------------------

#: 日期字段（due_date / target_date）判定「写于同一时点」的窗口。
#: 一个承诺只有一个真实到期日，出现两个必然是一个取代另一个；
#: 只有「同一场会纪要写 9/15、当天邮件写 10/1」这种同日期的口径分歧才是矛盾。
#:
#: 这个数字必须能被界面读到。演示里要回答「为什么这条判改期不是矛盾」，
#: 答「因为超过了窗口」而说不出窗口是几天，等于没答。
SAME_DAY_TOLERANCE_DAYS = 3

#: 一般字段（角色、相对位置、量级）判定「同一时点」的窗口。
#: 客户三个月前说要 A、上周说要 B，不是系统出错，是需求变了。
SAME_MOMENT_WINDOW_DAYS = 45


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------

class Verdict:
    """一条判定的结论类型。"""

    CONFLICT = "conflict"        # 同一时点、同一件事、两个来源说法不一致
    EVOLUTION = "evolution"      # 事情往前走了，新的取代旧的
    REGRESSION = "regression"    # 从终态退回，需要人看一眼
    DUPLICATE = "duplicate"      # 同一件事被重复抽到，是交叉印证而非矛盾
    SUSPECT = "suspect"          # 数值/日期差距过大，可能是抽错而非真冲突


@dataclass
class Conflict:
    """一对事实之间的判定。"""

    fact_type: str
    field: str
    left_id: str
    right_id: str
    verdict: str
    reason: str
    confidence: float = 0.8          # 判定本身的可信度，不是事实的可信度
    left_value: Any = None
    right_value: Any = None

    @property
    def is_conflict(self) -> bool:
        return self.verdict == Verdict.CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_type": self.fact_type,
            "field": self.field,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
            "left_value": self.left_value,
            "right_value": self.right_value,
        }


# ---------------------------------------------------------------------------
# 数值抽取
# ---------------------------------------------------------------------------

_NUM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(亿元|万元|个工作日|工作日|亿|万|元|%|％|天|周|个月|月|年)"
)

_UNIT_CANON = {
    "％": "%",
    "亿元": "元", "亿": "元",
    "万元": "元", "万": "元",
    "个工作日": "天", "工作日": "天",
    "周": "天",
}
_UNIT_SCALE = {"亿": 1e8, "亿元": 1e8, "万": 1e4, "万元": 1e4, "周": 7}


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """从文本里抽出「单位 + 数值」对。

    只认带单位的数字。「2 家竞品」「3 条记录」这类计数不进结果——
    它们不是业务量纲，拿来比会误判。
    """
    out: list[tuple[str, float]] = []
    for raw_num, raw_unit in _NUM.findall(text or ""):
        unit = _UNIT_CANON.get(raw_unit, raw_unit)
        try:
            value = float(raw_num) * _UNIT_SCALE.get(raw_unit, 1.0)
        except ValueError:
            continue
        out.append((unit, value))
    return out


def _number_profiles(nums: list[tuple[str, float]]) -> dict[str, set[float]]:
    prof: dict[str, set[float]] = {}
    for unit, value in nums:
        prof.setdefault(unit, set()).add(round(value, 4))
    return prof


# ---------------------------------------------------------------------------
# 单字段比较
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return VALUE_ALIAS.get(text, text)


def _same_moment(a: Fact, b: Fact, window_days: int = SAME_MOMENT_WINDOW_DAYS) -> bool:
    """两条事实是否指向同一个业务时点。

    同一时点的矛盾才是「冲突」；跨时点的矛盾多半是「演进」——
    客户三个月前说要 A，上周说要 B，这不是系统出错，是需求变了。
    """
    da, db = _to_date(a.stated_at), _to_date(b.stated_at)
    if da is None or db is None:
        return True                     # 时间不可知时宁可判为同时点，交给人看
    return abs((da - db).days) <= window_days


def _same_day(a: Fact, b: Fact, tolerance: int = SAME_DAY_TOLERANCE_DAYS) -> bool:
    """两条记录是否写于同一天前后。

    日期字段用这个窗口，不用 _same_moment 的 45 天：一个承诺只有一个真实到期日，
    出现两个必然是一个取代另一个。只有「同一场会纪要写 9/15、当天邮件写 10/1」
    这种同日期的口径分歧才是矛盾。窗口放宽到 45 天，每一条正常改期的承诺
    都会亮一个红标。
    """
    da, db = _to_date(a.stated_at), _to_date(b.stated_at)
    if da is None or db is None:
        return True
    return abs((da - db).days) <= tolerance


def _compare_ordered(
    fact_type: str,
    field_name: str,
    left: Fact,
    right: Fact,
) -> Conflict | None:
    seq = ORDERED_FIELDS[(fact_type, field_name)]
    lv, rv = _norm(left.payload.get(field_name)), _norm(right.payload.get(field_name))
    if lv == rv:
        return None
    if lv in MISSING_VALUES or rv in MISSING_VALUES or lv not in seq or rv not in seq:
        return None                      # 缺值或越界，无从比较

    li, ri = seq.index(lv), seq.index(rv)
    terminals = TERMINAL_VALUES.get((fact_type, field_name), set())

    if lv in terminals and rv not in terminals:
        return Conflict(
            fact_type, field_name, left.fact_id, right.fact_id,
            Verdict.REGRESSION,
            f"{field_name} 从终态 {lv!r} 退回 {rv!r}，可能是新情况也可能是误抽",
            0.55, lv, rv,
        )

    direction = "推进" if ri > li else "回退"
    confidence = 0.9 if direction == "推进" else 0.6
    return Conflict(
        fact_type, field_name, left.fact_id, right.fact_id,
        Verdict.REGRESSION if direction == "回退" else Verdict.EVOLUTION,
        f"{field_name} {lv!r} → {rv!r}（{direction}）",
        confidence, lv, rv,
    )


def _compare_boolean(field_name: str, left: Fact, right: Fact, label: str) -> Conflict | None:
    lv, rv = left.payload.get(field_name), right.payload.get(field_name)
    if lv is None or rv is None or lv == rv:
        return None
    if rv is True and lv is False:
        return Conflict(
            "concern", field_name, left.fact_id, right.fact_id,
            Verdict.EVOLUTION, f"{label}：否 → 是", 0.9, lv, rv,
        )
    return Conflict(
        "concern", field_name, left.fact_id, right.fact_id,
        Verdict.REGRESSION, f"{label}：是 → 否，问题重新出现", 0.6, lv, rv,
    )


#: 各类型判断「是否在说同一件事」的文本相似度门槛。
#: 只对结构规整的类型（承诺是动宾句、节点是事件名）有效；
#: 需求和顾虑的措辞千变万化，靠文本比不出来，改用「有没有共同的量纲」判断。
_SUBJECT_SIM = {
    "commitment": 0.50,
    #: timeline 的门槛必须显著高于其他类型。节点事件是 4–6 字的名词短语，
    #: 只共享一个主题词（「招标」）就能拿到 0.4 的相似度——
    #: 「招标文件发出」与「招标定标」是两个不同里程碑，却被判成同一件事，
    #: 于是日期不同 → 报一个「信息矛盾」。实测 c_10031 就是这样误报的。
    #:
    #: 真正的节点冲突长这样：「9 月底上线」vs「10 月中旬上线」，
    #: 事件短语几乎逐字相同，只是日期变了。这类相似度在 0.9 以上。
    #: 所以门槛定在 0.70：容得下「系统上线 / 项目上线」这种同义改写，
    #: 挡得住「只共享主题词」的不同里程碑。
    "timeline": 0.70,
    "need": 0.55,
    "concern": 0.55,
}


def _same_subject(left: Fact, right: Fact) -> bool:
    """两条同组事实是否在说同一件事。

    归并键只是粗筛，同组不代表同一件事。归并键必须做得粗一些（宁可多归），
    否则同一件事因为 category 填得不一样就分不到一组，冲突就永远发现不了；
    代价是同组里混进不相关的事实。所以分组之后必须有这一道精判。

    不做这道精判的后果是具体的：c_10015 里「王主任与另一家供应商关系好」和
    「马涛认可方案」同属 stakeholder 组，比 role_in_deal 就会造出一个
    「同一个人角色矛盾」的假冲突，把两个不同的人当成一个人。
    """
    import difflib

    t = left.fact_type
    lp, rp = left.payload or {}, right.payload or {}

    if t == "stakeholder":
        # 身份可能是姓名，也可能只有职务（`@董事长`）。比 name 的话，
        # 「董事长要签字」和「李董事长要签字」会被当成两个人，各占一格；
        # 更糟的是只有职务那条 name 为空，旧实现直接返回 False，
        # 于是它永远不会与任何人比较——一条记录在冲突检测里彻底隐形。
        li, ri = stakeholder_identity(lp), stakeholder_identity(rp)
        return bool(li and ri and li == ri)

    if t == "competition":
        ln, rn = _norm(lp.get("competitor_name")), _norm(rp.get("competitor_name"))
        return bool(ln and rn and ln == rn)

    if t == "commitment":
        if _norm(lp.get("due_date")) and _norm(lp.get("due_date")) == _norm(rp.get("due_date")):
            return True
        a = (lp.get("commitment_text") or left.statement or "")
        b = (rp.get("commitment_text") or right.statement or "")
        return difflib.SequenceMatcher(None, a, b).ratio() >= _SUBJECT_SIM["commitment"]

    if t == "timeline":
        if _norm(lp.get("target_date")) and _norm(lp.get("target_date")) == _norm(rp.get("target_date")):
            return True
        a = (lp.get("event") or left.statement or "")
        b = (rp.get("event") or right.statement or "")
        return difflib.SequenceMatcher(None, a, b).ratio() >= _SUBJECT_SIM["timeline"]

    # need / concern：有共同的量纲才可比。8% 与 10% 是同一件事的两个数，
    # 8% 与「90 天」不是同一件事，尽管它们可能都在 cost 类下。
    lprof = _number_profiles(extract_numbers(_quantity_text(left)))
    rprof = _number_profiles(extract_numbers(_quantity_text(right)))
    if set(lprof) & set(rprof):
        return True

    thr = _SUBJECT_SIM.get(t, 0.55)
    a, b = _quantity_text(left), _quantity_text(right)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= thr


def compare_pair(left: Fact, right: Fact) -> list[Conflict]:
    """比较两条同组事实，返回所有判定。

    约定 left 的时间不晚于 right；传反了会自动换回来，
    因为「谁取代谁」搞反会让 superseded_by 指错方向。
    """
    t = left.fact_type
    if t != right.fact_type:
        return []
    if (left.stated_at or "") > (right.stated_at or ""):
        left, right = right, left

    # 同组不等于同一件事
    if not _same_subject(left, right):
        return []

    results: list[Conflict] = []

    # --- 1. 重复抽到同一件事：交叉印证，不是矛盾 ---
    if _is_duplicate(left, right):
        return [Conflict(
            t, "_statement", left.fact_id, right.fact_id,
            Verdict.DUPLICATE,
            "同一件事被不同记录重复提到",
            0.9, left.statement, right.statement,
        )]

    # --- 2. 布尔字段 ---
    for (ft, fname), label in BOOLEAN_FIELDS.items():
        if ft == t:
            c = _compare_boolean(fname, left, right, label)
            if c:
                results.append(c)

    # --- 3. 有序字段 ---
    for (ft, fname) in ORDERED_FIELDS:
        if ft == t and fname in left.payload and fname in right.payload:
            c = _compare_ordered(ft, fname, left, right)
            if c:
                results.append(c)

    # --- 4. 类型专属的无序字段 ---
    results.extend(_compare_specific(left, right))

    # 同一条事实对只报最主要的那条判定，避免一个变化刷出四条红标
    if len(results) > 1:
        priority = {Verdict.CONFLICT: 0, Verdict.REGRESSION: 1, Verdict.SUSPECT: 2,
                    Verdict.EVOLUTION: 3, Verdict.DUPLICATE: 4}
        results.sort(key=lambda c: priority.get(c.verdict, 9))
        return results[:1]
    return results


def _cannot_be_same_claim(left: Fact, right: Fact) -> bool:
    """结构化字段已经把「同一件事」否掉了。

    日期字段是硬证据：一个承诺只有一个真实到期日。两条都写了日期却不一样，
    它们就不可能是同一件事的两次重复——不管溯源片段看起来多像。
    """
    for fname in ("due_date", "target_date"):
        lv = _norm((left.payload or {}).get(fname))
        rv = _norm((right.payload or {}).get(fname))
        if lv and rv and lv != rv:
            return True
    return False


def _is_duplicate(left: Fact, right: Fact) -> bool:
    """同一件事被重复提到。

    判据必须同时看内容和出处，只看任一个都会出错：

    - 只看出处（同源同文档就判重复）→ 同一篇纪要里的王主任和马涛会被合并，
      两条完全无关的干系人变成一条。这是曾经真实发生过的 bug。
    - 只看内容（陈述相似度）→「压降 5%」和「压降 9%」的相似度超过 0.95，
      会被当成重复合并，还会拿到虚假的印证加成：两个不同的数字被抹成一个，
      置信度反而被抬高。这也是真实发生过的。

    所以：同源时要求溯源片段一致（同一句话），跨源时要求陈述相似且量级一致。

    还有一道前置闸：**结构化字段不一致的，直接不可能是重复**。
    补这道闸是因为同源分支太弱——两条承诺都抽自同一句话、溯源片段都截了
    前 8 个字（"我方承诺 202…"），片段相等就被判成重复，于是
    相差 496 天的两个到期日既不报矛盾也不报存疑，两条都标 active 躺在卡上。
    用户看到的是「系统认为这是一件事，但又给了两个互斥的日期」。
    """
    import difflib

    if _cannot_be_same_claim(left, right):
        return False

    same_source = (
        left.source_type == right.source_type and left.source_id == right.source_id
    )
    if same_source:
        # 同一篇文档里，只有指向同一段原文才算重复
        if left.source_span and left.source_span == right.source_span:
            return True

    ratio = difflib.SequenceMatcher(None, left.statement or "", right.statement or "").ratio()
    if ratio < 0.90:
        return False

    lp = _number_profiles(extract_numbers(_quantity_text(left)))
    rp = _number_profiles(extract_numbers(_quantity_text(right)))
    for unit in set(lp) & set(rp):
        if lp[unit] != rp[unit]:
            return False
    return True


def _compare_specific(left: Fact, right: Fact) -> list[Conflict]:
    """各类型的无序字段与数值字段比较。"""
    t = left.fact_type
    out: list[Conflict] = []

    if t == "timeline":
        c = _compare_date_field("target_date", left, right,
                                "同一驱动因素的时间节点出现两个日期", 0.85)
        if c:
            out.append(c)
        return out

    if t == "commitment":
        c = _compare_date_field("due_date", left, right,
                                "同一承诺出现两个到期日", 0.85)
        if c:
            out.append(c)
        return out

    if t == "need":
        # 需求量级：压降 8% vs 下降 10%，同一时点两个数字，是真冲突
        out.extend(_compare_quantities(left, right, "需求描述中的量级不一致"))
        return out

    if t == "concern":
        out.extend(_compare_quantities(left, right, "顾虑描述中的量级不一致"))
        return out

    if t == "stakeholder":
        lv, rv = _norm(left.payload.get("role_in_deal")), _norm(right.payload.get("role_in_deal"))
        if lv and rv and lv != rv and _same_moment(left, right):
            out.append(Conflict(
                t, "role_in_deal", left.fact_id, right.fact_id,
                Verdict.CONFLICT,
                f"同一个人在两个来源里的角色不一致：{lv!r} vs {rv!r}",
                0.75, lv, rv,
            ))
        return out

    if t == "competition":
        lv, rv = _norm(left.payload.get("our_position")), _norm(right.payload.get("our_position"))
        if lv and rv and lv != rv and lv not in MISSING_VALUES and rv not in MISSING_VALUES:
            # 领先/落后是判断不是事实，两个来源判断不同，值得摆出来
            out.append(Conflict(
                t, "our_position", left.fact_id, right.fact_id,
                Verdict.CONFLICT,
                f"相对位置判断不一致：{lv!r} vs {rv!r}",
                0.7, lv, rv,
            ))
        return out

    return out


def _compare_date_field(
    field_name: str, left: Fact, right: Fact, reason: str, confidence: float
) -> Conflict | None:
    """比较两个日期字段。

    「同一承诺出现两个到期日」到底是矛盾还是改期，取决于两条记录是不是
    在同一个时点写下的：

    - 9 月 1 日说 9 月 15 日交，9 月 20 日说改成 10 月 10 日 —— 改期。前者已经被取代。
    - 同一场会上，纪要写 9 月 15 日、邮件写 10 月 1 日 —— 口径不一致，是矛盾。

    这个判据和数量比较是同一套逻辑，不该有两套。早期版本对日期一律判冲突，
    结果每一条正常改期的承诺都会亮一个红标，用户很快就学会无视所有红标。
    """
    lv, rv = left.payload.get(field_name), right.payload.get(field_name)
    ld, rd = _to_date(lv), _to_date(rv)
    if ld is None or rd is None or ld == rd:
        return None
    days = abs((rd - ld).days)
    if days > 365:
        # 差一年以上，多半是两件不同的事被归到一组，不是冲突
        return Conflict(
            left.fact_type, field_name, left.fact_id, right.fact_id,
            Verdict.SUSPECT,
            f"两个日期相差 {days} 天，可能不是同一件事，建议人工核对",
            0.5, str(lv), str(rv),
        )
    if not _same_day(left, right):
        return Conflict(
            left.fact_type, field_name, left.fact_id, right.fact_id,
            Verdict.EVOLUTION,
            f"{field_name} 改期：{lv} → {rv}（相差 {days} 天）",
            0.8, str(lv), str(rv),
        )
    return Conflict(
        left.fact_type, field_name, left.fact_id, right.fact_id,
        Verdict.CONFLICT,
        f"{reason}（相差 {days} 天）",
        confidence, str(lv), str(rv),
    )


#: 各类型里承载业务量级的核心文本字段——**用来比数字**。
#:
#: 优先用它而不是 statement：statement 是我们生成的陈述句，
#: 这些字段才是客户诉求的归一化表达，离原始数字更近。
#:
#: 和 `advice.rules.ADVICE_CORE_FIELD` 同名不同义，所以刻意分开命名：
#: 那边判的是「两条事实是不是同一件事」，看重的是**身份**
#: （competition 取 `competitor_name`——同一家竞品才算同一件事）；
#: 这边判的是「同一件事上的数字有没有变」，看重的是**可比性**
#: （competition 取 `customer_view`——「价格低 15%」这个数只在这儿，
#: `competitor_name` 里没有数字可比）。
#: 曾经两处都叫 `_CORE_TEXT_FIELD`，改一处忘另一处，冲突判定会静默失效：
#: 不报错，只是那类冲突永远检不出来。
CONFLICT_CORE_FIELD = {
    "need": "need_text",
    "concern": "concern_text",
    "commitment": "commitment_text",
    "timeline": "event",
    "competition": "customer_view",
    "risk_signal": "observed",
}


def _quantity_text(f: Fact) -> str:
    name = CONFLICT_CORE_FIELD.get(f.fact_type)
    value = (f.payload or {}).get(name) if name else None
    if isinstance(value, str) and value.strip():
        return value
    return f.statement or ""


def _compare_quantities(left: Fact, right: Fact, reason: str) -> list[Conflict]:
    """比较两条事实里的量纲数字。

    只比「同单位」的数：8% 和 10% 可比，8% 和 30 天不可比。
    """
    lp = _number_profiles(extract_numbers(_quantity_text(left)))
    rp = _number_profiles(extract_numbers(_quantity_text(right)))

    shared = set(lp) & set(rp)
    if not shared:
        return []

    for unit in sorted(shared):
        lvals, rvals = lp[unit], rp[unit]
        if lvals & rvals:
            continue                      # 有共同值，说明说的是同一个数
        lv, rv = max(lvals), max(rvals)
        if not _same_moment(left, right):
            # 跨时点的量级变化：客户三个月前说降 8%、上周说降 10%，
            # 这是需求变了，不是系统出错。但只有陈述高度相似时才敢这么判——
            # 否则「压降成本 8%」和「交付周期压到 10 天」也会被归到一起。
            import difflib
            if difflib.SequenceMatcher(
                None, _quantity_text(left), _quantity_text(right)
            ).ratio() < 0.5:
                continue
            return [Conflict(
                left.fact_type, f"quantity:{unit}", left.fact_id, right.fact_id,
                Verdict.EVOLUTION,
                f"量级变化：{lv:g}{unit} → {rv:g}{unit}",
                0.75, lv, rv,
            )]
        return [Conflict(
            left.fact_type, f"quantity:{unit}", left.fact_id, right.fact_id,
            Verdict.CONFLICT,
            f"{reason}：{lv:g}{unit} vs {rv:g}{unit}",
            0.8, lv, rv,
        )]
    return []

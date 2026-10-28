"""建议规则表。

这里没有任何模型调用，全是确定性规则。原因不是「规则比模型准」，
而是**建议必须是可反驳的**：

  「加强客户关系」——无法反驳，也无法执行，用户看完等于没看
  「补发《数据清理方案》，承诺到期 2026-09-05，已逾期 49 天」——可以立刻判断对错

一个建议只要说不清「依据哪条事实、为什么是现在、不做的后果」，就不该出现在这套规则里。
所以每条规则都必须挂到具体 fact_id，界面上能点开原文。

优先级分档的判据是「不做会怎样」，不是「做起来多难」：

  P0  不做会丢单或失信（承诺逾期、阻塞性顾虑、硬节点临近、信息断层）
  P1  不做会拖慢或走偏（需求过期未复核、决策链缺口、竞争落后、长时间无联系）
  P2  不做会让判断失准（画像太薄、竞品空白、推断需求未确认）

宁可少给：一条规则都不命中时返回空列表，不硬凑「建议保持良好沟通」。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ..extraction.schema import Fact, FactStatus, FactType, _to_date, stakeholder_identity
from ..aggregation.card import (
    COMMITMENT_IMMINENT_DAYS,
    CognitiveCard,
    TYPE_CN,
    _short,
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

#: 商机金额分档 → impact 分（0–100）。左边是「万元」下限，右边是分数。
#:
#: 为什么分档而不是连续换算：这个数字最终会显示给人看，必须能一句话解释——
#: 「它排在前面，是因为这一单 200 万以上」，而不是「因为模型给它打了 73.5 分」。
#: 分档的边界取销售自己会用的口径（百万级 / 十万级 / 几万级），不是等距切分。
AMOUNT_TIERS: tuple[tuple[float, float], ...] = (
    (300.0, 95.0),
    (150.0, 80.0),
    (80.0, 65.0),
    (30.0, 50.0),
    (0.0, 30.0),
)

#: 拿不到金额时的分数，**取中位**：不奖也不罚。
#:
#: 这是这一整套里唯一一个必须居中而不是必须最小的值。给 0 分，33 个没有
#: 主数据档案的存量客户会全部沉底——但「档案空着」不等于「这单不值钱」；
#: 给 95 分，它们又会挤掉真正的大单。给 50 分，它们安静地待在中间，
#: 直到有一天有人把金额填进来。缺信息不该被当成负分。
IMPACT_UNKNOWN = 50.0

#: 阶段里明说这单已经黄了/停了的词。命中就打折。
#:
#: 刻意**不做胜率换算**。阶段是 CRM 里人手维护的自由文本
#: （「内部立项（预算待批）」「停滞（对接人调岗）」），从里面解析概率必然错，
#: 而且错了以后没人能复核。只读一种情况：阶段已经明说这单不往前走了——
#: 那时它欠的钱不该继续按全值排队。
DEAD_STAGE_WORDS = ("失单", "落标", "停滞", "终止", "放弃", "已关闭")
DEAD_STAGE_FACTOR = 0.4


def impact_of(profile) -> float:
    """这个客户身上任何一条建议的「值多少」。0–100。

    公式只有两个因子：金额分档 × 阶段折扣。`impact` 是 **客户级** 的常量——
    同一个客户名下的「逾期承诺」和「没摸到决策人」损失的是同一笔钱，
    所以这里不按规则区分。它存在的唯一目的是让跨客户排序有第二个维度，
    而不是只按 P 级别 —— 「5 万的单」和「200 万的单」同为 P0 时，
    不该按客户名字排。
    """
    amount = _amount_of(profile)
    score = IMPACT_UNKNOWN
    if amount > 0:
        for floor, value in AMOUNT_TIERS:
            if amount >= floor:
                score = value
                break
    stage = str(getattr(profile, "stage", "") or "")
    if any(w in stage for w in DEAD_STAGE_WORDS):
        score *= DEAD_STAGE_FACTOR
    return round(max(0.0, min(100.0, score)), 2)


def _amount_of(profile) -> float:
    """从档案里读金额。

    档案是 CRM 同步来的，脏值（`"180万"` 这种带单位的字符串）迟早会出现。
    这里兜住而不是让调用方兜，因为 `impact_of` 在建议引擎的主循环里——
    抛一次就等于这一张卡的全部建议都没了，而用户看到的只是「今天没建议」。
    `impact_of` 的约定是「永远给一个数」，不满足就得先说清这一点。
    """
    raw = getattr(profile, "open_amount", 0)
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def money_cn(amount: float | None) -> str:
    """金额的中文写法。

    「万」这个单位在这个场景里不需要换算：销售的脑子对「180 万」有直觉，
    对「1,800,000」没有。拿不到金额时说「金额未定」，不说「0 万」——
    后者会被读成「这单不值钱」，而它真正的意思是「档案里没这一项」。
    """
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return "金额未定"
    return f"{value:g} 万"


@dataclass
class Advice:
    rule_id: str
    priority: str
    title: str
    why: str
    basis: list[str] = field(default_factory=list)      # fact_id，用于「跳到依据事实」
    owner: str = "me"
    due: str | None = None
    urgency: float = 20.0                               # 0-100，越大越紧迫
    #: 0-100，越大越值钱。由客户级商机金额算，见 `impact_of`。
    #: 默认取中位：规则不需要为它做任何事，引擎统一赋值。
    impact: float = IMPACT_UNKNOWN
    #: 逾期老化档位：""（不适用）/ "stale"（31–90 天，要求归因）/ "historic"（>90 天）。
    #: 单独一个字段而不是塞进 tags：tags 在去重时会被整组重写
    #: （`head.tags = sorted({head.rule_id} | ...)`），放进去当场就丢了。
    aging: str = ""
    tags: list[str] = field(default_factory=list)
    #: 建议的作用域。"fact" 表示针对某条事实，去重时按事实归并；
    #: "card" 表示针对整张画像（如「画像太薄」「只有一个联系人」），
    #: 不参与按事实去重——否则它会和「恰好引用同一条事实」的建议撞成一条。
    scope: str = "fact"

    @property
    def sort_key(self) -> tuple[int, float, float, float]:
        # 先看「不做会怎样」（P 级别），再看「这单值多少」，最后才看「急不急」。
        # 顺序不是随手定的：urgency 会随时间自动涨，让它排在金额前面，
        # 一个 5 万的单放两个月就盖住了 200 万的单——那正是要修的问题。
        return (PRIORITY_ORDER.get(self.priority, 9), -self.impact, -self.urgency, 0.0)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "priority": self.priority,
            "title": self.title,
            "why": self.why,
            "basis": self.basis,
            "owner": self.owner,
            "due": self.due,
            "urgency": round(self.urgency, 2),
            "impact": round(self.impact, 2),
            "aging": self.aging,
            "tags": self.tags,
            "scope": self.scope,
        }


#: 各类型里代表「这件事是什么」的字段——**用来判是不是同一件事**。
#:
#: 和 `aggregation.conflict.CONFLICT_CORE_FIELD` 同名不同义，所以刻意分开命名。
#: 那边是拿同一类字段去比数字（competition 取 `customer_view`，因为
#: 「价格低 15%」这个数只在那儿）；这边是判身份，competition 取
#: `competitor_name`——同一家竞品的两条记录算同一件事，哪怕客户评价变了。
#: 两处混用的后果不是报错，是建议去重失效：同一件事在待办里出两条。
#:
#: stakeholder 例外：身份可能是姓名，也可能只有职务，「董事长要签字」和
#: 「李董事长要签字」指的是同一个人。所以它不在 `_core_text` 里查表，
#: 而是走 `schema.stakeholder_identity`（唯一身份判定）。
ADVICE_CORE_FIELD = {
    "need": "need_text",
    "concern": "concern_text",
    "commitment": "commitment_text",
    "timeline": "event",
    "competition": "competitor_name",
    "stakeholder": "name",
    "risk_signal": "observed",
}


def _core_text(f: Fact) -> str:
    if f.fact_type == FactType.STAKEHOLDER.value:
        return stakeholder_identity(f.payload or {}) or f.statement or ""
    field = ADVICE_CORE_FIELD.get(f.fact_type, "")
    return (f.payload or {}).get(field) or f.statement or ""


def _same_thing(a: Fact, b: Fact, threshold: float = 0.90) -> bool:
    """两条事实是不是同一件事（同一句话被重复记下来的情况）。

    只用来去重建议，不用来判冲突——所以门槛定得很高（0.90）：
    宁可让两条相近的事实各出一条建议，也不要把「发承诺函」和「发报价单」
    这种到期日相同但内容不同的承诺并成一条。
    """
    if a.fact_type != b.fact_type:
        return False
    ta, tb = _core_text(a), _core_text(b)
    if not ta or not tb:
        return False
    return difflib.SequenceMatcher(None, ta, tb).ratio() >= threshold


@dataclass
class AdviceContext:
    card: CognitiveCard
    today: date

    # --- 便捷取数 ---

    @property
    def owner(self) -> str:
        return self.card.profile.owner_id

    def heads(self, fact_type: str, include_expired: bool = False) -> list[Fact]:
        """某类型当前该被检查的事实。

        include_expired 默认 False：过期的事实不该再驱动「去处理它」的建议。
        但有两类规则要显式打开：一类是专门处理过期的（复核需求是否仍有效），
        另一类是承诺与节点——这两类事实本身不按时间失效，过期与否不影响判断。

        **这里返回的是「所有有效事实」，不是「每组一条」。** 早期实现每组只取 head，
        而归并键是粗筛（commitment 只按 direction、timeline 只按 driver），
        于是「同一客户的所有我方承诺」被当成一组，只有一条进得了逾期检查——
        实测 35 条承诺与节点里有 5 条从未被任何规则看到，包括一条逾期 59 天的承诺。
        归并键服务于展示与冲突裁决，不该决定「哪些事实需要被检查」。
        重复的同一件事在这里按内容去重，避免同一句话报两条建议。
        """
        out: list[Fact] = []
        for g in self.card.groups:
            if g.fact_type != fact_type:
                continue
            for f in g.facts:
                if f.status == FactStatus.SUPERSEDED.value:
                    continue
                if f.status == FactStatus.EXPIRED.value and not include_expired:
                    continue
                if any(_same_thing(f, kept) for kept in out):
                    continue
                out.append(f)
        return out

    def groups(self, fact_type: str) -> list:
        return [g for g in self.card.groups if g.fact_type == fact_type]

    def conflict_groups(self) -> list:
        return [g for g in self.card.groups if g.has_conflict]

    def last_activity(self) -> date | None:
        dates = [_to_date(f.stated_at) for g in self.card.groups for f in g.facts]
        dates = [d for d in dates if d]
        return max(dates) if dates else None

    def all_facts(self) -> list[Fact]:
        return [f for g in self.card.groups for f in g.facts]


Rule = Callable[[AdviceContext], list[Advice]]


# ---------------------------------------------------------------------------
# 逾期老化：同一个提醒喊 116 天，等于没有提醒
# ---------------------------------------------------------------------------
#
# 9 条逾期承诺，中位 54 天、最长 116 天。问题不是「系统漏了」，恰恰相反：
# 它每天都在用同一句话喊同一件事，喊到用户学会无视它为止——
# 这是提醒系统最常见的死法（issue 长期无人处理 → 用户把通知全部关掉）。
#
# 所以逾期分三档，靠的是「时间」而不是「严重度」：
#
#   ≤30 天   正常提醒。这时客户还有可能在等，系统该催的是「去兑现」。
#   31–90 天 仍然提醒，但问题已经换了：不是「什么时候发」，是「还发不发」。
#            标题不变，why 里必须问出归因，界面上给三个一键按钮。
#   >90 天   没归因的，移出「今日待办」，单列成「历史欠账」。
#            它还是 P0（逾期 116 天照样会丢单），但**不该占今天的名额**——
#            一天只有 8 小时，把名额花在一件三个月没人动的事上，
#            等于让今天真正能推进的三件事排不上。
#
# 为什么降的是「今天的名额」而不是「P 级别」：P 级别的判据是「不做会怎样」，
# 逾期 116 天依然是「不做会失信」，改这个会污染 P0 审计的召回口径
# （金标准认定的 P0 事实会被系统降成 P1，看起来像漏报）。改提醒方式，
# 不改严重度。
OVERDUE_ATTRIBUTE_DAYS = 30
OVERDUE_HISTORIC_DAYS = 90

#: 老化档位。空字符串表示「不适用」（其它规则产出的建议）。
AGING_STALE = "stale"
AGING_HISTORIC = "historic"


# ---------------------------------------------------------------------------
# P0：不做会丢单或失信
# ---------------------------------------------------------------------------

def r01_commitment_overdue(ctx: AdviceContext) -> list[Advice]:
    """我方承诺逾期未履行。这类事情客户不会催，但一定记着。"""
    out = []
    for f in ctx.heads(FactType.COMMITMENT.value, include_expired=True):
        p = f.payload or {}
        if p.get("direction") != "we_owe":
            continue
        if p.get("fulfillment") in ("fulfilled", "waived"):
            continue
        due = _to_date(p.get("due_date"))
        if not due or due >= ctx.today:
            continue
        days = (ctx.today - due).days
        if days <= 0:
            continue
        text = p.get("commitment_text") or f.statement
        # 越拖越紧迫，但封顶 95——逾期半年的承诺和逾期一周的同样要先办
        urgency = min(95.0, 60.0 + days * 0.5)
        why = f"承诺到期日 {due}，已逾期 {days} 天。客户没催不等于没记账。"
        aging = ""

        if days > OVERDUE_HISTORIC_DAYS:
            # 三个月还没动，说明问题不在「来不及」，在「没人决定」。
            # 停止继续加码（urgency 封顶 40），把它挪出今天的名额。
            aging = AGING_HISTORIC
            urgency = 40.0
            why = (f"承诺到期日 {due}，已逾期 {days} 天——超过 {OVERDUE_HISTORIC_DAYS} 天没人处理。"
                   "这已经不是「什么时候发」的问题，是「还发不发」：还在追就定个新日期，"
                   "客户不需要了就关掉，转内部做也说出来。")
        elif days > OVERDUE_ATTRIBUTE_DAYS:
            aging = AGING_STALE
            why = (f"承诺到期日 {due}，已逾期 {days} 天，超过 {OVERDUE_ATTRIBUTE_DAYS} 天没兑现。"
                   "需要给个说法：还在追、客户已经不需要、还是转成内部事项？"
                   "一直挂着不表态，它就会一直出现在这里。")

        out.append(Advice(
            rule_id="commitment_overdue",
            priority="P0",
            title=f"补发/履行《{_short(text, 16)}》",
            why=why,
            basis=[f.fact_id],
            owner=ctx.owner,
            due=str(ctx.today),
            urgency=urgency,
            aging=aging,
        ))
    return out


def r16_commitment_imminent(
    ctx: AdviceContext,
    days_threshold: int = COMMITMENT_IMMINENT_DAYS,
) -> list[Advice]:
    """我方承诺即将到期，还没交付。

    为什么必须单列一条：r01 只查**已过期**，于是承诺在到期前永远不会被提醒，
    而提醒的价值恰恰在到期前——过期再说，失信已经发生了，只能补救不能避免。

    `c_10009` 就是这样被漏掉的：「两周内提供钉钉打通方案」10-28 到期，
    距基准日 4 天，而这条方案还是客户「不打通就不考虑」那个门槛的解法。
    四个模型在这类上全是 0——不是模型不行，是规则不存在。

    窗口取 7 天：再短就来不及排期，再长则和「远期计划」混在一起，失去提醒的意义。
    """
    out = []
    for f in ctx.heads(FactType.COMMITMENT.value, include_expired=True):
        p = f.payload or {}
        if p.get("direction") != "we_owe":
            continue
        if p.get("fulfillment") in ("fulfilled", "waived"):
            continue
        due = _to_date(p.get("due_date"))
        if not due:
            continue
        days = (due - ctx.today).days
        if days < 0 or days > days_threshold:
            continue
        text = p.get("commitment_text") or f.statement
        when = "今天到期" if days == 0 else f"还有 {days} 天到期"
        out.append(Advice(
            rule_id="commitment_imminent",
            priority="P0",
            title=f"交付《{_short(text, 16)}》（{days} 天内）",
            why=f"承诺 {due} 兑现，{when}。到期前交付是守信，到期后交付是补救。",
            basis=[f.fact_id],
            owner=ctx.owner,
            due=str(due),
            # 越临近越紧迫，但低于已逾期：还来得及，就别用「已经出事」的紧迫度
            urgency=min(89.0, 70.0 + (days_threshold - days) * 2.5),
        ))
    return out


def r02_concern_blocker(ctx: AdviceContext) -> list[Advice]:
    """阻塞性顾虑未解决。这是「谈不下去」的直接原因。

    但先要分清**卡的是谁**。`c_10029` 的「我们必须在下月 15 日前完成整改，
    否则整个系统要下线」被抽成 severity=blocker 的 concern——语气确实硬，
    硬的是客户自己的合规期限，不是我方准入门槛。照 blocker 报 P0，等于让
    销售去「解决」一件我方解决不了的事，而真动作（借势提供支持）在
    `customer_side_deadline`(P1) 里已经有了。

    这条和 `r04` 是同一个漏洞的两半：上次只补了 timeline 那半边。
    """
    out = []
    for f in ctx.heads(FactType.CONCERN.value):
        p = f.payload or {}
        if p.get("is_resolved"):
            continue
        severity = p.get("severity")
        if severity not in ("blocker", "high"):
            continue
        if _is_customer_own_duty(ctx, f):
            continue
        # 客户在骂「在用那家」时，这条 High 不是普通顾虑，是替换窗口：
        # r17 会以 P0 报同一个动作。这里再报一条 P1，用户看到的是两件事。
        if severity == "high" and _incumbent_pain(" ".join(filter(None, [
                f.source_span, p.get("concern_text"), f.statement]))):
            continue
        text = p.get("concern_text") or f.statement
        quote = p.get("verbatim_quote")
        why = f"客户原话「{_short(quote, 24)}」。" if quote else ""
        why += "这条不解决，方案和报价都推不动。" if severity == "blocker" else "这条会影响决策倾向，需要正面回应。"
        out.append(Advice(
            rule_id="concern_blocker",
            priority="P0" if severity == "blocker" else "P1",
            title=f"解决「{_short(text, 14)}」",
            why=why,
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=80.0 if severity == "blocker" else 55.0,
        ))
    return out


def r03_deadline_imminent(ctx: AdviceContext) -> list[Advice]:
    """节点临近。硬节点提前 14 天准备，软节点提前 7 天。

    为什么软节点也要报：`hardness=soft` 说的是「这个日期可变」，不是「这件事不急」。
    `c_10005` 的「月底前解决导出乱码，否则影响向总部汇报」就是软节点，
    但一周内必须交东西，原来因为只判 hard 而完全没提醒。

    窗口按硬度分开，是因为两类节点的准备周期不同：硬节点错过要等下一个预算周期，
    值得提前两周动；软节点提前两周提醒会和「远期计划」混在一起，反而稀释紧迫感。
    """
    out = []
    for f in ctx.heads(FactType.TIMELINE.value, include_expired=True):
        p = f.payload or {}
        hard = p.get("hardness") == "hard"
        window = 14 if hard else 7
        d = _to_date(p.get("target_date"))
        if not d:
            continue
        days = (d - ctx.today).days
        if days < 0 or days > window:
            continue
        event = p.get("event") or f.statement
        out.append(Advice(
            rule_id="deadline_imminent",
            priority="P0",
            title=f"为「{_short(event, 14)}」做准备",
            why=(f"硬节点 {d}，还有 {days} 天。这类节点错过通常要等下一个预算周期。"
                 if hard else
                 f"节点 {d}，还有 {days} 天。日期可变，但留给准备的时间不多了。"),
            basis=[f.fact_id],
            owner=ctx.owner,
            due=str(d),
            urgency=min(94.0, 90.0 - days * 2) if hard else min(88.0, 78.0 - days * 2),
        ))
    return out


def _advanced_since(ctx: AdviceContext, node: Fact, node_date: date) -> bool:
    """节点之后，有没有「关于这件事本身」的新记录。

    早期用的是 `ctx.last_activity()`——客户名下最后一条记录。这个信号太粗：
    `c_10019` 的「条码追溯 9 月底上线」（09-30）本该报断层，但 10-04 有一份谈
    **报价单**的记录，于是被判「事情在推进」而放过。报价单和条码追溯是两件事。

    改期这种情况不需要在这里处理：聚合层判定「演进」时已经把旧节点标成
    superseded，`heads()` 会跳过它。这里只留一道兜底——两条时间线落在不同归并组
    （driver 不同）时不会被比对，这时靠事件名相似度认出「同一个节点被重新录了一遍」。

    判据宁可漏抑制，也不要误抑制：少报一条「确认是否已发生」只是多一条噪声，
    误抑制则是把一次失信藏起来。所以用 `_same_thing` 的高门槛（0.90）。
    """
    for f in ctx.all_facts():
        if f.fact_id == node.fact_id:
            continue
        fd = _to_date(f.stated_at)
        if not fd or fd <= node_date:
            continue
        if _same_thing(node, f):
            return True
    return False


#: 原话里的第一人称。注意：单看这个词说明不了是谁——销售写的纪要里
#: 「我们」指我方，客户发来的邮件里「我们」指客户。
_SELF_REF = ("我们", "我方", "我司")

#: 原话里称呼对方。客户发来的邮件会写「请贵方」，销售写的纪要不会。
_OTHER_REF = ("贵方", "贵司", "你们")


def _is_customer_own_duty(ctx: AdviceContext, node: Fact) -> bool:
    """这条节点是客户自己的义务，不是我方要交付的事。

    两个信号同时成立才判定：

    1. 节点原话的主语是「我们/我方/我司」；
    2. **同一份文档里**出现了「贵方/贵司/你们」。

    第 2 条是关键。只凭「我们」判不了：销售写的拜访纪要里「我们」就是我方，
    那样会把真正属于我们的义务也放过。称呼互指才定得住——
    客户发来的邮件里，「我们」是客户，「贵方」是我们。

    `c_10029` 的邮件原文：「关于等保测评，我们必须在下月15日前完成整改并提交材料，
    否则整个系统要下线。请贵方在8月5日前提供整改清单。」
    等保整改是客户的合规义务，逾期不构成我方失信。

    但这不是「无关信息」：客户的系统面临下线，正是提供支持、建立信任的时机。
    所以降成 P1（借势机会），而不是丢掉。

    样本很薄（全语料 15 条 timeline 里只有 1 条命中），所以条件卡得很死：
    宁可漏判（多报一条我方断层）也不要错判（把我们的失信说成客户的事）。
    """
    span = node.source_span or ""
    if not any(w in span for w in _SELF_REF):
        return False
    for f in ctx.all_facts():
        if f.source_id != node.source_id:
            continue
        if any(w in (f.source_span or "") for w in _OTHER_REF):
            return True
    return False


def r04_deadline_missed(ctx: AdviceContext) -> list[Advice]:
    """计划节点已过，之后关于这件事一条记录都没有 —— 这是信息断层，不是「已完成」。"""
    out = []
    for f in ctx.heads(FactType.TIMELINE.value, include_expired=True):
        p = f.payload or {}
        d = _to_date(p.get("target_date"))
        if not d or d >= ctx.today:
            continue
        days = (ctx.today - d).days
        if days < 3:
            continue                    # 刚过一两天，别急着下结论
        if _advanced_since(ctx, f, d):
            continue                    # 这件事本身有了新记录，说明在推进
        event = p.get("event") or f.statement

        if _is_customer_own_duty(ctx, f):
            # 客户自己的义务：不报我方断层，但仍是借势的由头
            out.append(Advice(
                rule_id="customer_side_deadline",
                priority="P1",
                title=f"客户侧「{_short(event, 14)}」已逾期 {days} 天",
                why=f"节点 {d} 是客户自己的义务，逾期不构成我方失信。"
                    f"但他们大概率正被这件事压着——主动提供支持是建立信任的时机。",
                basis=[f.fact_id],
                owner=ctx.owner,
                urgency=min(70.0, 50.0 + days * 0.2),
            ))
            continue

        out.append(Advice(
            rule_id="deadline_missed",
            priority="P0",
            title=f"确认「{_short(event, 14)}」是否已发生",
            why=f"计划节点 {d} 已过 {days} 天，之后关于这件事没有任何记录。"
                f"这是信息断层，不等于节点已完成。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=min(92.0, 65.0 + days * 0.5),
        ))
    return out


def r05_conflict_unresolved(ctx: AdviceContext) -> list[Advice]:
    """同一件事两个来源说法不一致。系统不替你裁决，但必须让你知道。"""
    out = []
    for g in ctx.conflict_groups():
        if not g.head or g.head.status != FactStatus.CONFLICTED.value:
            continue
        fields = g.conflict_fields
        if not fields:
            continue
        # 只为「会影响下一步动作」的冲突提醒：承诺日期、节点、需求量级、角色
        important = {"due_date", "target_date", "role_in_deal", "our_position"} | {
            f for f in fields if f.startswith("quantity:")
        }
        if not important & set(fields):
            continue
        head = g.head
        label = TYPE_CN.get(g.fact_type, g.fact_type)
        detail = "；".join(
            c.reason for c in g.conflicts if c.verdict == "conflict"
        )
        out.append(Advice(
            rule_id="conflict_unresolved",
            priority="P0",
            title=f"确认{label}口径：「{_short(head.statement, 14)}」",
            why=f"两个来源说法不一致（{detail}）。两种口径可能导致完全不同的判断，需要跟客户核一次。",
            basis=[f.fact_id for f in g.facts],
            owner=ctx.owner,
            urgency=70.0,
            scope="card",
        ))
    return out


#: 「这句话把某件事变成了准入门槛」的表述特征。
#:
#: 为什么需要词表兜底而不是只靠模型分类：这类表述在抽取层最容易被记成 need
#: 而不是 concern——「要和钉钉打通」听起来就是需求。而 P0 规则只看 concern，
#: 于是最硬的一条约束从待办列表里消失了。
#:
#: 实测例子（d009）：原文「如果系统不能和现有钉钉打通，他们就不考虑了，这个是前提」
#: 被抽成 need/integration，模型给出的 need_text 是「系统需与现有钉钉打通」，
#: 语气被抹平了。但 source_span 完整保留了原句——所以词表必须扫 source_span，
#: 只扫 need_text 是扫不到的。
#: 自足的否决表述：出现即成立，不需要看上下文。
#: 「无法通融」「硬性要求」「底线」这些词在任何语境下都只有一个意思。
_STRONG_VETO = (
    "一票否决", "无法通融", "不能通融", "没有商量",
    "这是前提", "作为前提", "前提是", "底线", "硬性要求",
    "不进入", "出局", "就不谈", "不满足就", "达不到就", "必须满足",
)

#: 弱否决：字面意思是「不列入考虑范围」，但到底是说合作门槛，
#: 还是说预算方向 / 需求范围，光看这几个字分不出来。必须看上下文。
#:
#: 实测：`c_10008`「这个钱主要想投在数据治理上，别的地方暂时不考虑」——
#: 「不考虑」修饰的是**预算投向**（别的地方），跟能不能合作无关。
#: 原来按关键词命中就报 P0，等于把一句预算说明说成成交门槛。
_WEAK_VETO = ("不考虑", "不合作")

#: 条件标记。「不打通就不考虑」的否决一定挂在条件从句上，
#: 而「别的地方暂时不考虑」没有条件，是陈述句。
_CONDITIONAL = (
    "如果", "若", "要是", "除非", "否则", "前提",
    "做不到", "不能", "不行", "不满足", "达不到",
)

#: 指向「我方或我方交付物」的词。否决对象是这些，才是对我们的门槛。
_OUR_SIDE = (
    "贵方", "贵司", "你们", "供应商", "厂家",
    "系统", "方案", "产品", "合作", "投标", "进场", "服务",
)

#: 兼容旧引用（`eval/build_p0_advisory.py` 用它列可疑漏判）。
BLOCKING_PATTERNS = _STRONG_VETO + _WEAK_VETO


def _veto_hit(text: str) -> str | None:
    """原话里有没有「不满足就没有下一轮」的表述。

    强否决直接认；弱否决要求两个条件之一：挂在条件从句上，或否决对象指向我方。
    少认一条的代价是多漏一个门槛（可用其它信号补救），错认一条的代价是
    「预算方向」被当成「成交门槛」报 P0——后者会让销售不再相信 P0。
    """
    strong = next((w for w in _STRONG_VETO if w in text), None)
    if strong:
        return strong
    for w in _WEAK_VETO:
        if w not in text:
            continue
        if any(c in text for c in _CONDITIONAL) or any(o in text for o in _OUR_SIDE):
            return w
    return None

#: 「客户在表达不满」的特征。语气比否决级弱，只提示去确认口径，不报 P0。
COMPLAINT_PATTERN = re.compile(r"(太|过于|有点)(长|短|贵|慢|高|低|复杂)|意见很大|抱怨|不满")

#: 判断两条事实是否出自同一句原话时，短片段容易误判（「不考虑」出现在两处不同的话里）。
_MIN_SPAN_FOR_OVERLAP = 8


def _covered_by_blocker_concern(ctx: AdviceContext, need_fact: Fact) -> bool:
    """这句原话是不是已经被记成 blocker 级顾虑了。

    客户说「不打通就不考虑」，好的抽取会出两条：一条 need（要打通）、
    一条 concern（不考虑，severity=blocker）。两条都指向同一句话，
    于是 concern_blocker 和 need_blocking_requirement 各报一条 P0——
    用户看到两个 P0，其实是一件事，只会觉得系统在刷数量。

    同一句话只报一次。以 blocker 级顾虑为准，因为它带客户原话、
    且已经进了 concern 的独立复核流程；need 那条只在「顾虑没抽出来」时补位。
    """
    span = (need_fact.source_span or "").strip()
    if len(span) < _MIN_SPAN_FOR_OVERLAP:
        return False
    for f in ctx.all_facts():
        if f.fact_type != FactType.CONCERN.value:
            continue
        if (f.payload or {}).get("severity") != "blocker":
            continue
        other = (f.source_span or "").strip()
        if len(other) < _MIN_SPAN_FOR_OVERLAP:
            continue
        if span == other or span in other or other in span:
            return True
    return False


def r14_need_blocking_requirement(ctx: AdviceContext) -> list[Advice]:
    """被我方记成需求、但客户原话里带否决后果的约束。

    这类事情一旦漏了，不是「少做一件」，是「做到最后才发现进不了下一轮」。
    交给模型的语气判断不可靠，所以用词表兜住。
    """
    out = []
    for f in ctx.heads(FactType.NEED.value):
        haystack = " ".join([
            (f.payload or {}).get("need_text") or "",
            f.statement or "",
            f.source_span or "",          # 语气只在原文里，归一化后的字段没有
        ])
        hit = _veto_hit(haystack)
        if not hit:
            continue
        if _covered_by_blocker_concern(ctx, f):
            continue
        text = (f.payload or {}).get("need_text") or f.statement
        out.append(Advice(
            rule_id="need_blocking_requirement",
            priority="P0",
            title=f"确认「{_short(text, 13)}」是不是硬门槛",
            why=f"客户原话里用了「{hit}」——这不是普通期望，是不满足就没有下一轮。"
                f"需要问清验收口径，并按这个口径重做方案，而不是等投标时才发现。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=76.0,
        ))
    return out


#: 指向「正在用的那家」的说法。出现即说明客户在拿现状做对比。
_INCUMBENT = (
    "现有供应商", "现在的供应商", "原来的供应商", "上一家", "老供应商",
    "在用系统", "现有的系统", "老系统", "原厂", "现在的厂家", "原来那家",
)

#: 对现状的不满，强到足以驱动换供应商。
_PAIN = (
    "意见很大", "意见大", "受不了", "等不起", "投诉", "抱怨",
    "才有人来", "才来人", "才到", "没人管", "不响应", "响应慢", "太慢",
)


def _incumbent_pain(span: str) -> str | None:
    """客户在抱怨「正在用的那家」。返回命中的痛点词。

    这是换供应商最硬的动因，比任何「必须 / 否则不考虑」都硬——后者是资格线，
    前者是**已经发生过的真实损失**。`c_10001` 的「上个月设备故障报了三天
    才有人来，这个他们意见很大」，一句话同时给出三件事：现状很糟、
    糟到什么程度、客户真的在意。资格线可以谈，这种情况下客户是主动想换。

    传进来的字符串必须是「原文 + 摘要」的合集，不能只给 `source_span`：
    金标准那条的 span 是从「上个月设备故障…」截起的，压根没有「现有供应商」
    四个字，而 `concern_text` 写的是「现有供应商报修三天才到，客户意见很大」。
    只看 span，规则等于要求模型把关键词抄进它选的那一段——那不是规则在工作，
    是抽取在替规则工作。
    """
    if not any(w in span for w in _INCUMBENT):
        return None
    return next((w for w in _PAIN if w in span), None)


def r17_incumbent_pain(ctx: AdviceContext) -> list[Advice]:
    """客户在骂现在用的那家 —— 这是换供应商的窗口，也是方案里最该写满的一页。

    ## 为什么是 P0，而不是普通抱怨

    `c_10001` 的原话是「现有供应商响应慢，上个月设备故障报了三天才有人来，
    这个他们意见很大」。竞品还没出现、预算还没批、决策链还没摸清，但客户
    已经在为现状付代价——这是整批语料里最硬的换供应商动因。回应它的成本
    极低（方案里给一个可量化的响应时效承诺），不回应则等于把客户最痛的
    那一页留白，而这一页恰恰是竞标对手唯一无法照抄的部分。

    ## 为什么同时扫 concern 和 need

    同一件事，模型会抽成两种：`glm-4-flash` 抽成 severity=blocker 的 concern
    （交给 `r02`），`deepseek` 抽成 need「客户希望供应商提升服务响应速度」。
    只在 concern 上判，等于把结论交给模型的类型判断——而类型是这批语料里
    最不稳的字段。两种都认，才不依赖它。

    ## 为什么跳过 blocker 级 concern

    不是漏，是**避免同一句话出两条 P0**。`r02` 已经在报，动作完全一样。
    反过来，severity=high 的 concern 不跳过——`r02` 对 high 只给 P1，
    而这里判断的是「客户在为现状付代价」，该是 P0。`r02` 那边同步让位，
    两处口径必须一致，否则要么漏报要么重复。
    """
    out = []
    done_docs: set[str] = set()
    for ft in (FactType.CONCERN.value, FactType.NEED.value):
        for f in ctx.heads(ft):
            p = f.payload or {}
            if p.get("is_resolved"):
                continue
            # concern 已是 blocker 级：r02 报的同一个动作，不重复。
            if ft == FactType.CONCERN.value and p.get("severity") == "blocker":
                continue
            if f.source_id in done_docs:
                continue
            # 原文 + 摘要都要看：谁写了「现有供应商」不一定，两处拼起来才算准。
            blob = " ".join(filter(None, [
                f.source_span,
                p.get("concern_text"), p.get("need_text"), f.statement,
            ]))
            hit = _incumbent_pain(blob)
            if not hit:
                continue
            done_docs.add(f.source_id)
            text = p.get("concern_text") or p.get("need_text") or f.statement
            out.append(Advice(
                rule_id="incumbent_pain",
                priority="P0",
                title=f"把「{_short(text, 13)}」写成方案的对比页",
                why=f"客户原话用了「{hit}」——他在为现状付代价，这是换供应商的真正动因，"
                    f"比任何资格线都硬。给一个可量化的响应承诺，比再讲一遍功能更有用。",
                basis=[f.fact_id],
                owner=ctx.owner,
                urgency=78.0,
            ))
    return out


def r15_need_voiced_as_complaint(ctx: AdviceContext) -> list[Advice]:
    """客户用抱怨的方式提需求。抱怨不是需求，但它指向一个待确认的目标值。"""
    out = []
    for f in ctx.heads(FactType.NEED.value):
        p = f.payload or {}
        span = f.source_span or ""
        # 抱怨的是在用那家 —— 那是「替换窗口」，由 r17 以 P0 报，这里不重复。
        # 判定要用「原文 + 摘要」的合集，与 r17 保持同一口径。
        if _incumbent_pain(" ".join(filter(None, [span, p.get("need_text"), f.statement]))):
            continue
        m = COMPLAINT_PATTERN.search(span)
        if not m:
            continue
        text = p.get("need_text") or f.statement
        out.append(Advice(
            rule_id="need_voiced_as_complaint",
            priority="P1",
            title=f"把「{_short(text, 13)}」问成具体数字",
            why=f"客户是用「{m.group(0)}」这种方式提的，没有给可验收的目标值。"
                f"「觉得太长」和「要求 60 天上线」在方案里是两件事。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=48.0,
        ))
    return out


# ---------------------------------------------------------------------------
# P1：不做会拖慢或走偏
# ---------------------------------------------------------------------------

def r18_commitment_no_due(ctx: AdviceContext) -> list[Advice]:
    """承诺没有到期日，提醒把时间问清。

    为什么必须单列这一条：`r01` 只查已过期、`r16` 只查 7 天内到期，两条都要
    `due_date`；而校验器又刻意**不**给「月底前」「这周内」这类模糊表达猜日期
    （猜错会造出一条假的失信）。两件事合在一起的结果是：**没有写日期的承诺
    一条提醒都不会产生，而且不报错、不告警**——系统对它完全沉默。

    金标准 14 条承诺里有 4 条属于这一类（29%）。真实记录里「尽快发你」占比只会更高。

    修法不是让它硬进逾期检查（没有基准日，逾期算不出来），而是给一个动作：
    问清是哪天。用户答完，这条承诺才进入 r01 / r16 的视野。
    """
    out = []
    for f in ctx.heads(FactType.COMMITMENT.value, include_expired=True):
        p = f.payload or {}
        if p.get("direction") != "we_owe":
            continue
        if p.get("fulfillment") in ("fulfilled", "waived"):
            continue
        if _to_date(p.get("due_date")):
            continue
        text = p.get("commitment_text") or f.statement
        out.append(Advice(
            rule_id="commitment_no_due",
            priority="P1",
            title=f"问清《{_short(text, 16)}》的兑现时间",
            why="这条承诺没有到期日，进不了逾期与临期监控——系统现在看不住它。"
                "问到具体哪天，它才会在该提醒的时候出现。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=58.0,
        ))
    return out


def r06_stale_need(ctx: AdviceContext) -> list[Advice]:
    """需求已过保鲜期。过期的需求不是假的，但不能直接当现状用。"""
    out = []
    for g in ctx.groups(FactType.NEED.value):
        f = g.head
        if not f or not f.is_expired(ctx.today):
            continue
        # 同组里有更新的需求，说明这条已经被覆盖，不必再提醒
        if any(x.stated_at > f.stated_at for x in g.facts):
            continue
        text = (f.payload or {}).get("need_text") or f.statement
        from ..extraction.schema import FRESHNESS_DAYS
        days = FRESHNESS_DAYS.get("need") or 180
        out.append(Advice(
            rule_id="stale_need",
            priority="P1",
            title=f"复核需求「{_short(text, 14)}」是否仍有效",
            why=f"这条 {f.stated_at} 提出，需求保鲜期 {days} 天，已过期。"
                f"不要直接当现状写进方案。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=45.0,
        ))
    return out


def r07_missing_decision_maker(ctx: AdviceContext) -> list[Advice]:
    """干系人里没有决策人。谈得再好也要过这一关。"""
    people = ctx.heads(FactType.STAKEHOLDER.value)
    if not people:
        return []
    has_dm = any((f.payload or {}).get("role_in_deal") == "decision_maker" for f in people)
    if has_dm:
        return []
    champion = next(
        (f for f in people if (f.payload or {}).get("role_in_deal") == "champion"),
        None,
    )
    why = f"目前只有 {len(people)} 位关键人，没有一位是决策人。"
    if champion:
        why += f"可以先请{champion.payload.get('name') or '对接人'}帮忙引荐。"
    return [Advice(
        rule_id="missing_decision_maker",
        priority="P1",
        title="摸清决策链，找到最终签字人",
        why=why,
        basis=[f.fact_id for f in people],
        owner=ctx.owner,
        urgency=60.0,
        scope="card",
    )]


def r08_competitor_ahead(ctx: AdviceContext) -> list[Advice]:
    """竞品领先。只说「落后」没有用，要说清在哪个维度落后。"""
    out = []
    for f in ctx.heads(FactType.COMPETITION.value):
        p = f.payload or {}
        if p.get("our_position") != "trailing":
            continue
        name = p.get("competitor_name") or "竞品"
        view = p.get("customer_view")
        why = f"当前判断我方落后于{name}。"
        if view:
            why += f"客户视角：「{_short(view, 30)}」。"
        why += "需要针对这一点给出差异化证明，而不是泛泛讲优势。"
        out.append(Advice(
            rule_id="competitor_ahead",
            priority="P1",
            title=f"针对{_short(name, 10)}准备差异化材料",
            why=why,
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=65.0,
        ))
    return out


def r09_silence(ctx: AdviceContext, days_threshold: int = 21) -> list[Advice]:
    """长时间没有任何记录。沉默本身就是信号。"""
    last = ctx.last_activity()
    if not last:
        return []
    days = (ctx.today - last).days
    if days <= days_threshold:
        return []
    basis = [g.head.fact_id for g in ctx.card.groups if g.head][:6]
    return [Advice(
        rule_id="silence",
        priority="P1",
        title="主动跟进一次",
        why=f"最近一条记录在 {last}，已 {days} 天没有新信息。"
            f"客户不主动联系通常说明优先级被别的项目挤掉了。",
        basis=basis,
        owner=ctx.owner,
        urgency=min(75.0, 40.0 + days * 0.5),
        scope="card",
    )]


def r10_single_thread(ctx: AdviceContext) -> list[Advice]:
    """只有一个联系人。这个人休假、调岗、离职，项目就断了。"""
    people = ctx.heads(FactType.STAKEHOLDER.value)
    if len(people) != 1:
        return []
    f = people[0]
    p = f.payload or {}
    return [Advice(
        rule_id="single_thread",
        priority="P1",
        title="拓展第二位联系人",
        why=f"目前只接触到{p.get('name') or '一位关键人'}（{p.get('title') or p.get('role_in_deal')}）。"
            f"单点联系，人一走项目就断。",
        basis=[f.fact_id],
        owner=ctx.owner,
        urgency=50.0,
        scope="card",
    )]


def r11_inferred_need_unconfirmed(ctx: AdviceContext) -> list[Advice]:
    """推断出来的需求。推断可以写进方案，但不能当成客户说过的话。"""
    out = []
    for f in ctx.heads(FactType.NEED.value, include_expired=True):
        p = f.payload or {}
        if p.get("explicitness") != "inferred":
            continue
        text = p.get("need_text") or f.statement
        out.append(Advice(
            rule_id="inferred_need_unconfirmed",
            priority="P1" if p.get("priority") == "must" else "P2",
            title=f"向客户确认「{_short(text, 14)}」",
            why="这条需求是我方推断的，客户没有明确说过。"
                "拿推断去写方案，猜错就是整份方案白做。",
            basis=[f.fact_id],
            owner=ctx.owner,
            urgency=55.0 if p.get("priority") == "must" else 35.0,
        ))
    return out


# ---------------------------------------------------------------------------
# P2：不做会让判断失准
# ---------------------------------------------------------------------------

def r12_thin_profile(ctx: AdviceContext) -> list[Advice]:
    """事实太少，或者覆盖面太窄。少到不该下结论，但要说清缺什么。

    判据直接用 card.is_thin，不在这里另写一套。
    两处各写一套的后果是具体的：结论说「信息不足」，
    而建议列表里没有「补齐底图」，用户不知道该拿这句话怎么办。
    """
    if not ctx.card.is_thin:
        return []
    n = ctx.card.n_live
    total = ctx.card.n_all
    gaps = ctx.card.gaps
    if n < total:
        detail = f"当前 {total} 条事实中只有 {n} 条仍有效"
    else:
        detail = f"当前只有 {n} 条事实"
    if gaps:
        detail += f"，{'、'.join(g.split('：')[0] for g in gaps[:4])}完全空白"
    return [Advice(
        rule_id="thin_profile",
        priority="P2",
        title="补齐客户底图再谈方案",
        why=detail + "。此时做方案等于猜。",
        basis=[f.fact_id for f in ctx.card.all_live_facts()],
        owner=ctx.owner,
        urgency=30.0,
        scope="card",
    )]


def r13_no_competitor_info(ctx: AdviceContext) -> list[Advice]:
    """一条竞品信息都没有。没有消息不等于没有对手。"""
    if ctx.heads(FactType.COMPETITION.value):
        return []
    if len(ctx.card.all_live_facts()) < 3:
        return []                       # 画像太薄时由 r12 统一处理，不重复提醒
    return [Advice(
        rule_id="no_competitor_info",
        priority="P2",
        title="摸清竞品情况",
        why="没有任何竞品记录。B 端采购很少只谈一家，"
            "「没听说有对手」和「没有对手」是两件事。",
        basis=[],
        owner=ctx.owner,
        urgency=25.0,
        scope="card",
    )]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

#: 顺序不重要，排序由 priority + urgency 决定。
RULES: list[Rule] = [
    r01_commitment_overdue,
    r16_commitment_imminent,
    r18_commitment_no_due,
    r02_concern_blocker,
    r17_incumbent_pain,
    r03_deadline_imminent,
    r04_deadline_missed,
    r05_conflict_unresolved,
    r14_need_blocking_requirement,
    r15_need_voiced_as_complaint,
    r06_stale_need,
    r07_missing_decision_maker,
    r08_competitor_ahead,
    r09_silence,
    r10_single_thread,
    r11_inferred_need_unconfirmed,
    r12_thin_profile,
    r13_no_competitor_info,
]

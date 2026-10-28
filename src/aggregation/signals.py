"""风险信号：从「变化」里读出来的，不是从「情绪」里读出来的。

## 为什么这一段必须从模型里拿出来

原设计把 risk_signal 交给模型去抽，提示词里写「与基线相比的负面变化」。
实测 50 篇金标准：**精确率 0.0、召回率 0.0（tp 0 / fp 5 / fn 10）**。

这不是模型不行，是指令本身完不成。`baseline` 要的是「以前是什么样」，
而模型一次只能看见一篇记录——基线在另一篇里，或者根本没有。
让它在单篇内判断「相比之前有没有变坏」，等于要求它拿手上一半的牌
去比大小。它于是抽出 5 条「客户今天不太高兴」式的假信号，
同时把 10 条真的变化全漏了。

所以改成两段：

    规则负责「这里有个变化」   —— 词表 + 结构比对，零模型调用，可复现
    模型负责「这个变化要紧」   —— 仍可从同一段文本里抽 concern / stakeholder

规则不做语义判断，只做**摘录**：它不写自己的话，只把原文里描述变化的
那半句挑出来当 `observed`。这是本模块最重要的一条约束——
规则的输出永远可溯源，因为它的输出就是原文。

## 两级证据

`baseline` 是 schema 的必填字段，它逼着每一条信号回答「原来是怎样」。
按能不能答上来，信号分两级：

| 级别 | 什么时候 | severity | baseline 写什么 |
|---|---|---|---|
| 已确认 | 同篇里有对照（「以前…现在…」）或跨篇比对到了历史事实 | 按规则表 | 原文里的「以前」半句 / 历史事实 |
| 疑似 | 只看见变化后的样子，不知道原来 | 降一档 | 「此前的状态没有记录」 |

把「疑似」也放进来，是因为**不记录比记错更糟**：客户说「预算砍了」，
即使系统不知道砍之前是多少，这也是一条该被看见的信息。降一档 + 明说
「没有基线」，比扔掉它、或者假装它是确凿变化，都更接近事实。

## 为什么规则表长这样

八类 signal_type 来自销售方法里的标准信号集。每一类的判据都写成
「几个词表槽位，每个槽位内部任一命中、槽位之间全部命中」——
因为中文里表达同一件事的说法太多（「调走 / 调离 / 调任」），
但一件事的**结构**是稳定的（有人走 + 有人接手 = 对接人变更）。
写词表而不是写正则，是为了让判据能被一个不懂代码的人读懂并在
`rule.why` 里被反驳。

已知局限，写在前面：金标准里 10 条信号分散在 8 类里，六类各只出现 1 次。
在这个样本量上，任何阈值都会过拟合。因此本模块**不设分数阈值**，
只有「命中 / 没命中」两态——两态判据在小样本上还能被逐条审查，
打分判据不能。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..extraction.schema import (
    SEVERITY,
    FactType,
    compute_confidence,
    stakeholder_identity,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 一张卡上最多几条。超过三条风险，人就一条都不看了——
#: 和 viewmodel.MAX_ADVICE 是同一个道理，只是这里的上限更紧。
MAX_SIGNALS = 3

#: 信号类型的中文名。与 schema.SIGNAL_TYPE 一一对应，测试盯着这件事。
SIGNAL_CN = {
    "response_delay": "响应变慢",
    "contact_change": "对接人变更",
    "decision_chain_expand": "决策链变长",
    "price_pressure": "价格承压",
    "competitor_entry": "竞品进入",
    "champion_loss": "支持者离场",
    "requirement_drift": "需求偏移",
    "silence": "客户收缩沟通",
}

#: 「变化前」的引导词。出现在同一分句里，就把这一分句认作 baseline。
_BEFORE_WORDS = ("以前", "之前", "原来", "原先", "此前", "早先", "过去", "上次")

#: 「变化后」的引导词。
_AFTER_WORDS = ("现在", "如今", "目前", "改为", "改成", "换成", "新来", "新任", "接手")

#: 只用来判「这句话本身在跟以往比」，不用来抽 baseline 的对照词。
#:
#: 「这次要成立选型小组」和「已确定成立选型小组」是两件事：前者说的是
#: 「跟上次不一样」，后者说的是「现在的组织结构就是这样」。差别全在
#: 「这次」两个字上，而这两个字不在「以前/原来」那一族里——它不描述
#: 变化前的状态，只暗示存在对比。所以单独一个集合，不混进 `_BEFORE_WORDS`，
#: 否则 `_split_baseline` 会把「这次要成立选型小组」当成 baseline 抄进去。
_CONTRAST_WORDS = ("这次", "这一轮", "这回", "这一次")

#: 否定/无变化。命中即放弃这条候选。
#:
#: 词表刻意短，且只收**明确的「没变」**：「不再管」这种带「不再」的说法
#: 恰恰是变化本身（不再管这块 = 支持者离场），把它当否定会漏掉真信号。
#: 所以这里收的是「没有变化」而不是「不再」。
NO_CHANGE = (
    "没有变化", "未发生变化", "没有明显变化", "保持不变", "维持不变",
    "照旧", "一如既往", "还是老样子", "已恢复正常", "已经解决", "已解决",
    "不用改", "不需要调整", "不用担心", "不成问题", "没有影响",
)

#: 分句边界。只按逗号/分号切，不按句号切：一个变化的两侧常常写在同一句里。
_CLAUSE = re.compile(r"[，,；;]")

#: 句子边界（含换行）。窗口匹配以句子为最小单位。
_SENT_END = re.compile(r"[。！？!?\n]")


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """一条判据。

    `patterns` 是「或」关系：任一 pattern 命中即算。每个 pattern 是若干
    slot，slot 之间「且」、slot 内部「或」。之所以允许一个 rule 挂多个
    pattern，是因为同一类变化在中文里有几种完全不同的说法结构——
    「以前 A 现在 B」是两侧对照，「下周再说吧」是单侧推脱——
    硬塞进一组槽位只会让判据变成一堆 `and/or` 的迷宫。
    """

    signal_type: str
    patterns: tuple[tuple[tuple[str, ...], ...], ...]
    severity: str
    statement: str
    why: str
    #: 这一类的「变化」必须靠外部参照才成立：句子里没有对照词、又没有历史基线时，
    #: 就不产——**不产一条疑似，也不产一条已确认**。
    #:
    #: 目前只有 `decision_chain_expand` 打开这个开关。「成立了一个选型小组」
    #: 是组织结构，不是变化：它变成变化的前提是「原来不是这样」，
    #: 而「原来不是这样」只能来自对照词或历史记录。别的类型不需要这个开关，
    #: 因为变化写在了句子里——「调走了 + 新来的接手」自带前后两侧，
    #: 「预算砍了」自带方向，读到就知道变了，不用问原来多少。
    needs_contrast: bool = False

    def __post_init__(self) -> None:
        assert self.severity in SEVERITY, self.severity
        assert self.signal_type in SIGNAL_CN, self.signal_type


RULES: tuple[Rule, ...] = (
    # 1) 对接人变更：有人离开 + 有人接手。只有「走」没有「来」，是空缺不是变更，
    #    那种情况由 champion_loss（支持者离场）或者卡片空白项来提示。
    Rule(
        signal_type="contact_change",
        patterns=(
            (("调走", "调离", "调任", "离职", "辞职", "轮岗", "换了个人", "换人",
              "退二线", "退居二线", "退休"),
             ("接手", "接替", "新任", "新来的", "现在由", "改由", "交接")),
            (("组织调整", "架构调整", "人事调整"),
             ("接手", "接替", "新任", "新来的", "现在由", "改由")),
        ),
        severity="high",
        statement="客户侧对接人发生变更。",
        why="有一个人离开、同时有另一个人接手，才叫对接人变更；"
            "只走了没人接是空缺，不是变更。",
    ),

    # 2) 支持者离场：人走了，且原来分管这块。与 1) 的区别是「还管不管」：
    #    走了但换人接手 → 变更（关系可重建）；走了且不再管 → 流失（关系断了）。
    Rule(
        signal_type="champion_loss",
        patterns=(
            (("退二线", "退居二线", "退休", "离职", "调走", "调离", "调任"),
             ("不管", "不再管", "不再负责", "不负责这块", "不管这块", "没表态", "未表态")),
        ),
        severity="high",
        statement="我方支持者离场，接班的人还没表态。",
        why="人走了且原来分管这块，才是我方支持者流失；"
            "如果他走了但业务交给一个明确接手的人，那是对接人变更。",
    ),

    # 3) 响应变慢：同一句里出现「以前 … 现在 …」的对照，或者推脱式推迟。
    Rule(
        signal_type="response_delay",
        patterns=(
            (("以前", "之前", "原来", "原先", "此前"),
             ("现在", "如今", "这阵子", "最近", "后来"),
             ("回消息", "回复", "反馈", "才回", "才给", "才答复",
              "响应速度", "响应时间")),
            # 「再联系」这种客套收尾不算推脱，必须是明确往后推的说法。
            # 实测：只写「再联系」会把「以后有需求会再联系」判成响应变慢。
            (("下周再说", "下周再谈", "下个月再说", "回头再说", "再说吧",
              "等忙完", "在忙", "暂时顾不上", "腾不出手"),),
        ),
        severity="medium",
        statement="客户响应速度变慢。",
        why="要么同一句里给出了快慢两侧的对照，要么是明确的推迟表态；"
            "只有「客户很忙」这类背景信息不算。",
    ),

    # 4) 决策链变长：新增评审组织架构。
    Rule(
        signal_type="decision_chain_expand",
        patterns=(
            (("成立", "组建", "设立", "新增"), ("小组", "评审组", "委员会", "选型组", "评审会")),
            (("改为", "改成", "调整为"),
             ("小组", "多部门", "跨部门", "联合评审", "评审会", "办公会")),
        ),
        severity="high",
        statement="决策链变长，参与方增多。",
        why="有明确的「新设机构」表述，且这句在跟以往比（原来/这次/改为）才算扩张。"
            "「要报上级审批」是流程常态，「已确定有个选型小组」是组织结构——"
            "都不是变化，把它们算进来，每一篇记录都会报警。",
        needs_contrast=True,
    ),

    # 5) 价格承压：预算被单方向压降，或者已谈的价格被要求重谈。
    #    只收「压降」不收「觉得贵」：后者是 concern（顾虑），不是变化。
    Rule(
        signal_type="price_pressure",
        patterns=(
            (("预算",), ("砍", "下调", "压缩", "削减", "收紧", "冻结", "没批", "驳回")),
            (("报价", "价格", "价钱", "单价"),
             ("再谈", "重谈", "重新谈", "再议", "重新议价", "压价", "砍价", "要求降")),
        ),
        severity="high",
        statement="价格或预算被压降，已谈定的条件面临重谈。",
        why="必须是预算/价格被单向压下来。客户说「你们贵」是顾虑，"
            "客户说「预算砍了」才是格局变化。",
    ),

    # 6) 竞品进入：客户开始接触此前没提到过的第三方。
    Rule(
        signal_type="competitor_entry",
        patterns=(
            (("也在看", "也在接触", "也在比较", "也在评估", "也在谈", "对比了", "引入了", "另外一家"),
             ("方案", "报价", "供应商", "竞品", "厂商", "产品")),
            (("竞品", "其他供应商", "友商", "别家"),
             ("进来", "参与", "报价", "介入", "加入")),
        ),
        severity="high",
        statement="出现此前未记录的竞争对手。",
        why="客户主动说起还在看别家，才算竞品进入；"
            "我方在方案里提到竞品不在此列（那是我们自己写的）。",
    ),

    # 7) 需求偏移：原来要的改了，或者优先级换了。
    Rule(
        signal_type="requirement_drift",
        patterns=(
            (("之前", "原来", "原先", "原本", "上次", "早先"),
             ("需求", "要求", "范围", "规格"),
             ("改", "变", "调整", "去掉", "不做了", "往后", "延期")),
            (("现在更关注", "改成优先", "优先做", "转向", "重点是先"),
             ("需求", "功能", "模块", "审批", "报表", "预算")),
        ),
        severity="medium",
        statement="客户需求优先级发生偏移。",
        why="要能指出「原来是什么、现在改成什么」；"
            "只说「有新需求」是新增，不是偏移。",
    ),

    # 8) 客户收缩沟通：明确表态暂时没进展。
    Rule(
        signal_type="silence",
        patterns=(
            (("没什么进展", "暂无进展", "没有进展", "没进展", "没什么新情况"),),
            # 「等我们内部定」也是收缩：客户把球踢回自己内部，接下来一段时间
            # 不会有新进展。这一族是 2026-10-27 补的——那天给模型的信号加
            # 「同一把尺」复核，恒远重工那条 `silence` 的证据是
            # 「等我们内部定，这两天没消息」，语义上确实是收缩，词表里却没有
            # 这一族，于是它被判成「原文没有这种变化的说法」摘掉了。
            # 尺子没错，是词表漏了：这种情况只能补词表，不能放行。
            (("等有结论", "有消息再", "再联系你", "先放一放", "暂缓", "暂时不需要",
              "先停一下", "暂时搁置",
              "等我们内部", "等内部定", "内部先定", "等他们内部"),),
        ),
        severity="medium",
        statement="客户主动收缩沟通节奏。",
        why="客户明说「先放一放 / 等有结论」，是主动收缩；"
            "我方自己没去联系不属于这一类，那是我们的事。",
    ),
)

#: 单文档（无基线）时的严重度降档。
_DOWNGRADE = {"blocker": "high", "high": "medium", "medium": "low", "low": "low"}

#: 同篇内被更强的判断压掉的类型：`被压的 -> 压它的`。
#:
#: 只针对**同一句话里读出来的同一件事**。实测的反例是「孙总上个月退二线了，
#: 现在不管这块。新来的副总对供应商这块还没表态」：这里「退二线」+「新来的」
#: 同时满足对接人变更的词表，但「新来的副总」不是孙总的接班人，
#: 它是另一个人。同一次离场报两条，等于把一件事说成两件，
#: 所以只要支持者流失成立，对接人变更就不再单独报。
#:
#: 只压单文档判据，不压跨文档：跨文档那条比的是两个角色的身份，
#: 是另一条证据链，不是同一句话的第二种读法。
_SUPPRESSED_BY = {"contact_change": "champion_loss"}


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """一条判出来的信号。

    `span` 一定是原文的连续子串——这不是约定，是 `detect()` 的实现方式决定的：
    它只从原文里切，不拼接、不复述。`observed` 与 `baseline` 同样来自原文的
    分句，跨文档时来自历史事实的原文。
    """

    signal_type: str
    statement: str
    baseline: str
    observed: str
    severity: str
    span: str
    confirmed: bool = True
    #: 判据来源：`doc` = 同一篇里读出来的，`cross` = 和历史记录比出来的。
    #: 分开记不是为了展示，是为了评测能分别统计两条路径的命中率——
    #: 混在一起，就分不清到底是词表在起作用还是跨文档比对在起作用。
    origin: str = "doc"
    #: 为什么判出来的（人话），给界面上的「凭什么」用。
    why: str = ""

    @property
    def label(self) -> str:
        return SIGNAL_CN.get(self.signal_type, self.signal_type)

    def as_fact_data(
        self,
        *,
        customer_id: str,
        source_type: str,
        source_id: str,
        doc_date: str,
    ) -> dict[str, Any]:
        """转成可入库的 canonical 前身（交给 `pipeline.to_fact` 收尾）。

        置信度按 schema 的同一套公式算，不自己另写一份——
        `detected_by=rule` 不是降低可信度的理由：规则判出来的这条，
        证据是原文那半句，和模型抽出来的一样硬。
        """
        payload = {
            "signal_type": self.signal_type,
            "baseline": self.baseline,
            "observed": self.observed,
            "severity": self.severity,
            "detected_by": "rule",
        }
        conf = compute_confidence(
            FactType.RISK_SIGNAL.value, source_type, "direct_quote", payload,
        )
        return {
            "fact_type": FactType.RISK_SIGNAL.value,
            "subject_type": "account",
            "customer_id": customer_id,
            "statement": self.statement,
            "payload": payload,
            "source_type": source_type,
            "source_id": source_id,
            "source_span": self.span,
            "stated_at": doc_date,
            "stated_at_precision": "day" if doc_date else "unknown",
            "confidence": conf if conf is not None else 0.0,
            "confidence_basis": "direct_quote",
        }


# ---------------------------------------------------------------------------
# 切句与切分句
# ---------------------------------------------------------------------------

def _sentences(text: str) -> list[tuple[int, int, str]]:
    """按句末标点切成 (start, end, 原文)，偏移对得上原文。"""
    out: list[tuple[int, int, str]] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        seg = text[start:end]
        if seg.strip():
            out.append((start, end, seg))
        start = end
    if start < len(text) and text[start:].strip():
        out.append((start, len(text), text[start:]))
    return out


def _split_baseline(span: str) -> tuple[str, str]:
    """把一段原文拆成（变化前, 变化后）。拆不出来就返回 ("", span)。

    只在**同一段 span 内部**找对照，不做跨 span 的猜测：
    如果「以前」和「现在」不在同一处被规则圈中，那是因为规则圈小了，
    应该调窗口，而不是让这里悄悄拉长证据范围。
    """
    clauses = [c for c in _CLAUSE.split(span) if c.strip()]
    before, after = "", ""
    for c in clauses:
        if not before and any(w in c for w in _BEFORE_WORDS):
            before = c.strip()
        elif not after and any(w in c for w in _AFTER_WORDS):
            after = c.strip()
    return before, (after or span.strip())


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _slot_hit(window: str, slot: Sequence[str]) -> str:
    """槽位在窗口里命中的第一个词；没命中返回空串。"""
    for w in slot:
        if w in window:
            return w
    return ""


def _match_pattern(
    sents: list[tuple[int, int, str]], pat: tuple[tuple[str, ...], ...], max_window: int,
) -> tuple[int, int] | None:
    """在窗口内找一次完整命中，返回 (start, end) 偏移。窗口越小越优先。"""
    n = len(sents)
    for width in range(1, max_window + 1):
        for i in range(0, n - width + 1):
            window = "".join(s[2] for s in sents[i:i + width])
            if any(w in window for w in NO_CHANGE):
                continue
            if all(_slot_hit(window, slot) for slot in pat):
                return sents[i][0], sents[i + width - 1][1]
    return None


def _best_span(
    text: str, rule: Rule, max_window: int,
) -> str:
    """这条规则在原文里最紧凑的一次命中。"""
    sents = _sentences(text)
    best: tuple[int, int] | None = None
    for pat in rule.patterns:
        got = _match_pattern(sents, pat, max_window)
        if got is None:
            continue
        if best is None or (got[1] - got[0]) < (best[1] - best[0]):
            best = got
    return text[best[0]:best[1]].strip() if best else ""


# ---------------------------------------------------------------------------
# 跨文档比对
# ---------------------------------------------------------------------------

def _payload(fact: Any) -> dict[str, Any]:
    if isinstance(fact, dict):
        return fact.get("payload") or {}
    return getattr(fact, "payload", None) or {}


def _ftype(fact: Any) -> str:
    return (fact.get("fact_type") if isinstance(fact, dict)
            else getattr(fact, "fact_type", "")) or ""


def _span(fact: Any) -> str:
    return (fact.get("source_span") if isinstance(fact, dict)
            else getattr(fact, "source_span", "")) or ""


def _cross_doc_signals(
    facts: Iterable[Any], history: Iterable[Any],
) -> list[Signal]:
    """有历史基线时才做得了的两条比对。

    只做这两条，不做别的，因为**只有这两个维度的数据在两篇记录之间是可比的**：

    | 信号 | 可比的字段 | 为什么可比 |
    |---|---|---|
    | competitor_entry | `competition.competitor_name` | 是个名字，不随措辞变 |
    | contact_change | `stakeholder.role_in_deal` + 身份 | 角色是枚举，身份有唯一实现 |

    `response_delay` 需要「上次回复用了几天」，系统没有这个字段；
    `price_pressure` 需要报价历史，那是 CRM 的数据，不在这条链路里。
    宁可只做两条真的可比，也不要用「两篇都在抱怨价格」冒充「价格在恶化」。
    """
    now = list(facts)
    past = list(history)
    if not past:
        return []

    out: list[Signal] = []

    # --- 竞品进入 ---
    names_now = {
        str(_payload(f).get("competitor_name") or "").strip()
        for f in now if _ftype(f) == FactType.COMPETITION.value
    }
    names_past = {
        str(_payload(f).get("competitor_name") or "").strip()
        for f in past if _ftype(f) == FactType.COMPETITION.value
    }
    names_now.discard("")
    names_past.discard("")
    # 历史里一条竞品都没记过时，不能算「竞品进入」：那只是**第一次记录竞品**。
    # 「新出现」的前提是「旧的有过」——没有旧名单，就没法说谁是新的。
    # 实测：不判这一条，会给每一篇首次提到竞品的记录都报一次「竞品进入」。
    known = sorted(names_past)
    for name in (sorted(names_now - names_past) if names_past else []):
        span = next((_span(f) for f in now
                     if _ftype(f) == FactType.COMPETITION.value
                     and str(_payload(f).get("competitor_name") or "").strip() == name), "")
        if not span:
            continue
        out.append(Signal(
            signal_type="competitor_entry",
            statement=f"客户开始接触此前没提过的供应商：{name}。",
            baseline=f"此前记录的对手只有：{'、'.join(known)}",
            observed=span,
            severity="high",
            span=span,
            confirmed=True,
            origin="cross",
            why="当期记录里出现了历史记录里没有的竞品名，且此前确实记录过别的对手。",
        ))

    # --- 对接人变更 ---
    def role_map(bag: Iterable[Any]) -> dict[str, str]:
        out_map: dict[str, str] = {}
        for f in bag:
            if _ftype(f) != FactType.STAKEHOLDER.value:
                continue
            role = str(_payload(f).get("role_in_deal") or "").strip()
            who = stakeholder_identity(_payload(f))
            if role and who:
                out_map[role] = who
        return out_map

    before, after = role_map(past), role_map(now)
    role_cn = {"decision_maker": "决策人", "champion": "支持者",
               "gatekeeper": "把关人", "influencer": "影响者",
               "user": "使用方", "blocker": "阻力方"}
    for role, who in sorted(after.items()):
        old = before.get(role)
        if not old or old == who:
            continue
        span = next((_span(f) for f in now
                     if _ftype(f) == FactType.STAKEHOLDER.value
                     and str(_payload(f).get("role_in_deal") or "").strip() == role), "")
        if not span:
            continue
        out.append(Signal(
            signal_type="contact_change",
            statement=f"决策链上{role_cn.get(role, role)}换人了。",
            baseline=f"此前的{role_cn.get(role, role)}是{old}",
            observed=span,
            severity="high",
            span=span,
            confirmed=True,
            origin="cross",
            why="同一个决策角色的身份在两篇记录里不是一个人。",
        ))

    return out


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------

def detect(
    text: str,
    facts: Iterable[Any] = (),
    history: Iterable[Any] = (),
    *,
    max_window: int = 3,
    limit: int = MAX_SIGNALS,
    skip_covered: bool = True,
) -> list[Signal]:
    """从一段记录里判信号。

    参数
    ----
    text     当期记录原文。**只从这里取 span**，所以 span 天然可溯源。
    facts    当期记录已抽出的事实。已经由模型抽出同类信号的类型不再重复产——
             同一条变化不该在卡上出现两遍，一次是模型说的、一次是规则说的。
    history  同一客户更早记录的事实。为空时（第一篇记录）只走单文档判据。
    skip_covered
             是否让「模型已抽出同类信号」压掉规则信号。线上必须为 True；
             回放评测时必须为 False——评测要量的是**规则本身**的能力，
             拿金标准当 facts 传进去、又让它按金标准去重，等于让被测者
             抄答案再问它考了几分。

    返回按「已确认优先、严重度优先」排好序的至多 `limit` 条。
    """
    # 一次性materialize：下面要遍历两遍（去重检查 + 跨文档比对），
    # 传生成器进来第二遍就空了——那种错不会报错，只会静默少几条。
    facts = list(facts)
    history = list(history)

    known_types = set()
    if skip_covered:
        known_types = {
            str(_payload(f).get("signal_type") or "")
            for f in facts if _ftype(f) == FactType.RISK_SIGNAL.value
        }

    found: list[Signal] = []
    if text:
        for rule in RULES:
            if rule.signal_type in known_types:
                continue
            span = _best_span(text, rule, max_window)
            if not span:
                continue
            before, observed = _split_baseline(span)
            # 对照词也算「这句话在跟以往比」，但它不提供 baseline 文本，
            # 所以 confirmed 与 baseline 是两件事：前者决定要不要产、严重度，
            # 后者只决定 payload 里那句「原来是怎样」怎么写。
            contrast = bool(before) or any(w in span for w in _CONTRAST_WORDS)
            if rule.needs_contrast and not contrast:
                continue
            confirmed = contrast
            severity = rule.severity if confirmed else _DOWNGRADE[rule.severity]
            found.append(Signal(
                signal_type=rule.signal_type,
                statement=rule.statement,
                baseline=before or "此前的状态没有记录（本条只记到变化后的样子）",
                observed=observed,
                severity=severity,
                span=span,
                confirmed=confirmed,
                why=rule.why,
            ))

    for sig in _cross_doc_signals(facts, history):
        if sig.signal_type in known_types:
            continue
        # 跨文档已经确认过同类，就不再叠一条单文档的疑似版本：
        # 同一条变化在卡上出现两次，用户会以为是两件事。
        found = [s for s in found if s.signal_type != sig.signal_type]
        found.append(sig)

    # 同一句话的两种读法：强的那个留着，弱的那个不报。
    doc_types = {s.signal_type for s in found if s.origin == "doc"}
    found = [s for s in found
             if not (s.origin == "doc"
                     and s.signal_type in _SUPPRESSED_BY
                     and _SUPPRESSED_BY[s.signal_type] in doc_types)]

    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    found.sort(key=lambda s: (0 if s.confirmed else 1, order.get(s.severity, 9)))
    return found[:limit]


def suspected_note(signals: Iterable[Signal]) -> str:
    """一句话说明「为什么这条信号是疑似」。界面直接用。

    叫 suspected 而不是 blocked：`src/feedback/lessons.py` 里已经有一个
    `block_note`（说的是「这条被你的黑名单拦下了」），两个概念不是一回事。
    """
    suspected = [s for s in signals if not s.confirmed]
    if not suspected:
        return ""
    types = "、".join(s.label for s in suspected)
    return f"{types}只看到变化后的样子，此前状态没有记录，严重度已下调一档。"


# ---------------------------------------------------------------------------
# 同一把尺：模型说的变化，原文里到底有没有这种变化
# ---------------------------------------------------------------------------

def rule_for(signal_type: str) -> Rule | None:
    """这个类型对应的那条判据；不认识的类型返回 None。"""
    for r in RULES:
        if r.signal_type == signal_type:
            return r
    return None


def evidence_supports(signal_type: str, evidence: str, *, max_window: int = 3) -> bool:
    """这段证据里，能不能读出「这种变化」。

    ## 为什么要这道闸

    规则与模型各自独立读同一段原文。规则是「词槽全中才产」，所以它产出的
    每一条都能指回原文里那句具体的话；模型没有这个约束，它可以在一段
    通篇讲现状的首次拜访里抽出一条「支持者离场」——`source_span` 也能过
    校验（原文里确实有那句话），但那句话说的是「运维这块他很不满意」，
    跟「有人走、且不再管这块」没有关系。

    实测：同一段首次拜访记录连抽 5 次，1 次凭空多出一条 `champion_loss`。
    它进了卡片，客户就多出一条「我方支持者离场，接班的人还没表态」——
    一条原文里根本不存在的坏消息。

    所以这里立一条对称的规矩：**模型说这是变化，就用规则那把尺子量一遍
    它的证据**。量不出来，就不是「变化」，是推测。

    ## 为什么不担心「误杀真信号」

    真有变化的话，规则已经独立扫过同一段原文并且产出了那条（`detect`
    在流程里总会跑）。所以丢掉模型这条不会让信息消失——除非词表没收录
    这个说法，而那种漏只能靠补词表修，不能靠放行一条原文对不上的
    模型信号来修：放行的那条连「凭什么」都答不上来。

    `max_window` 与 `detect` 默认值一致（3 句）：判据允许跨句，因为
    「调走了」和「新来的接手」常常不在同一句里。
    """
    rule = rule_for(signal_type)
    if rule is None:
        # 不认识的类型不替它判定。拒收必须有明确理由，说不清理由就别拒。
        return True
    if not (evidence or "").strip():
        return False
    return bool(_best_span(evidence, rule, max_window))


def split_unsupported_signals(facts: Iterable[Any]) -> tuple[list[Any], list[Any]]:
    """把「模型说的变化、原文里却读不出变化」的那些挑出来。

    只看 `detected_by == "model"` 的：规则产出的每一条本来就是从原文切出来的，
    再拿同一把尺量一遍是同义反复（`_best_span` 就是它自己产出的方式）。

    返回 `(留下的, 摘出来的)`。摘出来的不删——调用方负责在卡面上说一句
    「有 N 条模型推测没有原文依据，未计入」，藏起来比标出来更糟。
    """
    kept: list[Any] = []
    dropped: list[Any] = []
    for f in facts:
        if _ftype(f) != FactType.RISK_SIGNAL.value:
            kept.append(f)
            continue
        payload = _payload(f)
        if str(payload.get("detected_by") or "model") != "model":
            kept.append(f)
            continue
        # 只用**原文类**证据：statement 是系统自己写的措辞，
        # 拿它当证据等于让系统证明自己。
        evidence = "。".join(filter(None, [
            _span(f),
            str(payload.get("observed") or ""),
            str(payload.get("baseline") or ""),
        ]))
        if evidence_supports(str(payload.get("signal_type") or ""), evidence):
            kept.append(f)
        else:
            dropped.append(f)
    return kept, dropped


def unsupported_note(dropped: Sequence[Any]) -> str:
    """被摘下来的那些，用一句话说清。"""
    if not dropped:
        return ""
    labels: list[str] = []
    for f in dropped:
        label = SIGNAL_CN.get(str(_payload(f).get("signal_type") or ""), "变化")
        if label not in labels:
            labels.append(label)
    return (
        f"另有 {len(dropped)} 条模型推测的变化（{'、'.join(labels)}）"
        "在原文里找不到这种变化的说法，未计入"
    )


"""四个动作入口：从「我要做什么」出发，而不是从「哪个客户」出发。

## 为什么需要这一层

原来的动线是「先选客户 → 再看这个客户能给我什么产出物」。对
「下午要去拜访，先把恒远的简报看一眼」是顺的；对另一个更常见的场景是反的：

    客户刚挂电话，说「方案明天发我看」，你现在就得准备材料。

用户心里有动作，没有客户名。让他先在 39 个客户里找那个客户、再在客户页上
找到「方案准备」按钮——三步，而这件事只剩 30 秒。

## 三个客户，不是三件事

按「事」排会得到「同一个客户的 3 件待办」，等于只给了 1 个客户。所以先按
客户去重（每个客户只留该意图下最重的那条），再排。同一客户还剩几件事用
`n_more` 带出去——不说，用户会以为这个客户只有一件事。

## 为什么不做「智能推荐」

每个意图就是一组建议类型（见 `INTENTS`），排序沿用待办那一套
（P → 金额 → 类型优先级 → 客户名）。这样「为什么是这三个」永远答得出来：
因为这三条建议分最高。换成模型打分，用户问「凭什么」时只能回答「模型觉得」，
而演示里最经不起的就是这个回答。

## 两个口径上的选择

1. **不按「要做的事 / 要补的数据」过滤。** 意图是动作，分区是「客户有没有感知」。
   「我要出方案」必须包含「硬门槛待确认」——它在补数据区，可它恰恰是
   方案第一段要写的东西。用分区的口径筛意图，会让「出方案」这个入口
   一个候选都剩不下。
2. **只从重点客户里选，存量客户只报个数。** 存量客户 33 个、待办 78 件，
   混进来之后前 3 名几乎必然全是「不急着今天动」的。跟待办页同一个口径：
   先说重点客户的，再告诉你存量那边还有多少——不说，用户会以为它们不存在。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 四个入口。顺序就是页面上的顺序。
#:
#: `kinds` 的**书写顺序即优先级**：同一个 P 级别、同一档金额里，排前面的先出。
#: 不共用全局 `KIND_ORDER`——那是「哪类事情更急」的全局排序，
#: 而这里是「为了这个动作，先动谁」，两个问题的答案不一样。
#:
#: `focus_by_kind` 是**这个意图内**的落点细分，块 id 必须是 `brief` 那份简报里
#: 真实存在的块（见 `demo/brief.py::BLOCK_IDS`，有单测拿实际产出比对）。
#: 刻意不做跨简报映射：`single_thread` 在「我要联系」里应该落到拜访简报的
#: 「建议问的问题」（能帮我引荐吗），在「我要交接」里才落到交接说明的
#: 「关键人」。按 kind 一维映射，点「我要联系」会弹出一份「交接说明」——
#: 用户会以为点错了按钮。
INTENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "contact",
        "label": "我要联系",
        "hint": "该打出去的电话：失联久了、只认识一个人、承诺快到期",
        "kinds": ("silence", "commitment_imminent",
                  "single_thread", "customer_side_deadline"),
        "brief": "visit",
        "focus": "summary",
        "focus_by_kind": {
            "silence": "recent",
            "commitment_imminent": "owed",
            # 「能帮我引荐吗」就写在这一块里，不是另开一份交接说明
            "single_thread": "questions",
            "customer_side_deadline": "recent",
        },
    },
    {
        "id": "visit",
        "label": "我要拜访",
        "hint": "见面才有用的事：答应的事没交付、顾虑没解开、节点已经压过去了",
        "kinds": ("commitment_overdue", "concern_blocker",
                  "deadline_missed", "incumbent_pain"),
        "brief": "visit",
        "focus": "owed",
        "focus_by_kind": {
            "commitment_overdue": "owed",
            "concern_blocker": "pitfalls",
            "incumbent_pain": "pitfalls",
            "deadline_missed": "recent",
        },
    },
    {
        "id": "proposal",
        "label": "我要出方案",
        "hint": "方案里必须先写清的事：硬门槛、待确认需求、竞品怎么应对",
        "kinds": ("need_blocking_requirement", "competitor_ahead",
                  "inferred_need_unconfirmed", "stale_need", "no_competitor_info"),
        "brief": "proposal",
        "focus": "problem",
        "focus_by_kind": {
            "need_blocking_requirement": "needs",
            "inferred_need_unconfirmed": "needs",
            "stale_need": "needs",
            "competitor_ahead": "competition",
            "no_competitor_info": "competition",
        },
    },
    {
        "id": "handover",
        "label": "我要交接",
        "hint": "要交给别人：决策链缺环、只靠一条线、档案空着",
        "kinds": ("missing_decision_maker", "single_thread", "thin_profile"),
        "brief": "handover",
        "focus": "who",
        "focus_by_kind": {
            "missing_decision_maker": "people",
            "single_thread": "people",
            "thin_profile": "who",
        },
    },
)

INTENT_CN = {it["id"]: it["label"] for it in INTENTS}

#: 只有一条待办、没有动作语境时，点它该打开哪份产出物的哪一块。
#:
#: 这是**默认落点**：意图入口里每个动作有自己的 `focus_by_kind` 覆盖它
#: （比如 `single_thread` 在「我要联系」里落 `questions`，在「我要交接」里
#: 落 `people`）。但待办行本身没有动作语境，只能取一个默认值。
#: 默认值的取舍：`single_thread` 落拜访简报的「建议问的问题」，
#: 因为那一块里就有「内应这一环还没接触到，能帮我引荐吗」——
#: 打开一份交接说明去说「该要个第二联系人」，比打开拜访简报远得多。
KIND_BRIEF: dict[str, tuple[str, str]] = {
    "commitment_overdue": ("visit", "owed"),
    "commitment_imminent": ("visit", "owed"),
    "commitment_no_due": ("visit", "owed"),
    "concern_blocker": ("visit", "pitfalls"),
    "incumbent_pain": ("visit", "pitfalls"),
    "need_voiced_as_complaint": ("visit", "pitfalls"),
    "deadline_missed": ("visit", "recent"),
    "deadline_imminent": ("visit", "recent"),
    "customer_side_deadline": ("visit", "recent"),
    "silence": ("visit", "recent"),
    "conflict_unresolved": ("visit", "unsure"),
    "single_thread": ("visit", "questions"),
    "missing_decision_maker": ("handover", "people"),
    "thin_profile": ("handover", "who"),
    "competitor_ahead": ("proposal", "competition"),
    "no_competitor_info": ("proposal", "competition"),
    "need_blocking_requirement": ("proposal", "needs"),
    "inferred_need_unconfirmed": ("proposal", "needs"),
    "stale_need": ("proposal", "needs"),
}

#: 认不出来的建议类型落到哪。宁可落到「一句话现状」，也不要给一个空落点——
#: 空落点等于点了没反应。
DEFAULT_BRIEF = ("visit", "summary")

#: 默认给几个客户。
DEFAULT_PICKS = 3


def brief_target(kind: str) -> tuple[str, str]:
    """某类建议该打开哪份简报的哪一块。"""
    return KIND_BRIEF.get(kind or "", DEFAULT_BRIEF)


def decorate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给每条待办补上「点它该看哪份产出物、落在哪一块」。

    在服务端算而不是前端算：块 id 是 `demo/brief.py` 定义的，
    前端自己抄一份，改一个 id 就会出现「点了没反应」——
    而这类错不报错、不留日志，只能靠人肉发现。
    """
    for it in items:
        bk, fo = brief_target(it.get("kind") or "")
        it["brief_kind"] = bk
        it["focus"] = fo
    return items


def _rank(intent_kinds: Iterable[str], kind: str) -> int:
    kinds = list(intent_kinds)
    return kinds.index(kind) if kind in kinds else len(kinds)


def _sort_key(it: dict[str, Any], kinds: Iterable[str]) -> tuple:
    """和待办页同一套：先看严重度，再看钱，再看这件事在这个动作里的性质。

    金额插在 kind 前面，理由与 `brief.collect_todos` 一致：
    同一档里「200 万的逾期承诺」和「5 万的逾期承诺」是同一类事、同一个急度，
    唯一的区别就是值多少钱——而这一条决定先给谁打电话。
    """
    p = {"P0": 0, "P1": 1, "P2": 2}.get(it.get("p"), 3)
    return (p, -float(it.get("impact") or 0.0), _rank(kinds, it.get("kind") or ""),
            1 if it.get("pool") else 0, it.get("cname") or "")


def build(items: list[dict[str, Any]], n: int = DEFAULT_PICKS) -> list[dict[str, Any]]:
    """把待办摊平成「每个意图三个客户」。

    `items` 就是 `/api/todo` 里那一份（已带 P / impact / amount / kind / aging）。
    刻意不在这里重新算建议：两处各算一份，迟早在某次规则改动后对不上，
    而对不上的表现是「待办页说 3 件、意图入口说 2 个」——用户只会觉得系统坏了。
    """
    out: list[dict[str, Any]] = []
    for cfg in INTENTS:
        kinds = cfg["kinds"]
        # 历史欠账不进意图推荐：它的问题不是「去做」，是「有人拍板」。
        # 把它塞进「我要拜访」，等于建议用户去拜访一件三个月没人决定的事。
        hit = [it for it in items
               if it.get("kind") in kinds and it.get("aging") != "historic"]
        hit.sort(key=lambda it: _sort_key(it, kinds))

        focus_rows = [it for it in hit if not it.get("pool")]
        pool_rows = [it for it in hit if it.get("pool")]

        picks: list[dict[str, Any]] = []
        seen: dict[str, int] = {}          # cid → 该客户已经在 picks 里的下标
        more: dict[str, int] = {}
        for it in focus_rows:
            cid = it["cid"]
            if cid in seen:
                # 同一客户还有别的同类事。记个数，不占名额——
                # 三个名额里放同一个客户的三个动作，等于只给了一个客户。
                more[cid] = more.get(cid, 0) + 1
                continue
            seen[cid] = len(picks)
            picks.append(it)
            more.setdefault(cid, 0)
            if len(picks) >= n:
                break

        brief_kind, focus = cfg["brief"], cfg["focus"]
        by_kind = cfg.get("focus_by_kind") or {}
        rows = []
        for it in picks:
            # 落点跟着「决定它入选的那条建议」走，但**只在这份简报内取块**：
            # 意图决定打开哪份产出物，建议类型决定落在哪一块。
            fo = by_kind.get(it.get("kind") or "", focus)
            rows.append({
                "cid": it["cid"],
                "cname": it.get("cname") or "",
                "p": it.get("p") or "",
                "kind": it.get("kind") or "",
                "kind_cn": it.get("kind_cn") or "",
                "title": it.get("title") or "",
                "why": it.get("why") or "",
                "impact": it.get("impact") or 0.0,
                "amount": it.get("amount") or 0,
                "amount_cn": it.get("amount_cn") or "金额未定",
                "when": it.get("when") or "",
                # 深链落点：打开哪份简报、落在哪一块
                "brief_kind": brief_kind,
                "focus": fo,
                # 这个客户在这个动作下还剩几件没列出来
                "n_more": more.get(it["cid"], 0),
            })

        out.append({
            "id": cfg["id"],
            "label": cfg["label"],
            "hint": cfg["hint"],
            "brief_kind": brief_kind,
            "focus": focus,
            "n_candidates": len({it["cid"] for it in focus_rows}),
            # 存量客户只报个数：混进前 3 名，等于把「今天」让给「不急着今天动」的。
            "n_pool_candidates": len({it["cid"] for it in pool_rows}),
            "picks": rows,
        })
    return out

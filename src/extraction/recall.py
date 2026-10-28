"""补抽：针对漏得最重的两类做第二次定向追问。

为什么要有这一层
----------------
第一遍抽取的漏抽率约 23%——每五条要点漏一条。漏掉的集中在 need 与 concern
（need F1 0.74、concern 0.63，是七类里最弱的两个）。

漏抽和抽错是两种病。抽错会被置信度分档、溯源校验、待确认标记挡住；
**漏抽是静默的**：卡上少一条，屏幕上没有任何征兆，用户也不知道少了。
需求第六条「不敢完全相信系统结论」要防的不只是错，也是这种无声的少。

补抽不是「把同样的话再问一遍」，是问一个**更窄**的问题：
只找 need/concern、不许重复已有的、确实没有就返回空数组。
它的产出照样过校验与溯源，所以最坏情况是白花一次调用，不会污染结论。

三条设计约束
------------
1. **门槛**（`should_recall`）：默认 auto——第一遍已经抽到 need 和 concern 就不补。
   补抽要花在真漏了的地方，不是每篇都翻倍。
2. **防重**（`is_duplicate_of_first`）：补抽出来的与第一遍高度相似就丢。
   补回来的重复比漏掉更糟：卡上出现两条一样的需求，用户会觉得这系统脑子有问题。
3. **说清**：补了几条、补的是哪类，写进回执。这本身是痛点 F 的一部分——
   「系统为了不漏做了什么」也要能被看见，不能只在后台悄悄跑。
"""

from __future__ import annotations

import difflib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .prompt import PROMPT_DIR, format_document

#: 补抽只管这两类。理由是数据：need 与 concern 是七类里 F1 最低的两个。
RECALL_TYPES = ("need", "concern")

#: 与第一遍陈述相似度到这个数就算重复，丢掉不计增益。
#: 0.80 比抽取内去重的 0.90 宽：补抽的任务就是「换一种说法再说一遍已有的」，
#: 严了会把真正的重复放进来，宽了会把该补的当重复丢掉。
DUPLICATE_OF_FIRST = 0.80

TYPE_CN = {"need": "需求", "concern": "顾虑"}


@lru_cache(maxsize=1)
def load_recall_prompt() -> str:
    return (PROMPT_DIR / "recall_zh.md").read_text(encoding="utf-8")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def should_recall(facts: list[dict[str, Any]], mode: str) -> bool:
    """补抽门槛。

    mode:
      never  —— 关掉，当这层不存在
      always —— 每篇都补，用于评测「上限」
      auto   —— 缺 need 或缺 concern 才补（默认）
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    if mode != "auto":
        raise ValueError(f"未知的补抽模式：{mode}")
    have = {f.get("fact_type") for f in facts}
    return not (set(RECALL_TYPES) <= have)


def is_duplicate_of_first(candidate: dict[str, Any],
                          first: list[dict[str, Any]]) -> bool:
    """补抽出来的是不是第一遍已经有的。

    只看陈述文本就够：补抽提示词明说了「不要重复已有的」，模型要重复也是换个说法。
    同类型才比——「预算 50 万」的 need 和「报价太贵」的 concern 说的是两件事。
    """
    for old in first:
        if old.get("fact_type") != candidate.get("fact_type"):
            continue
        if _similar(candidate.get("statement", ""), old.get("statement", "")) >= DUPLICATE_OF_FIRST:
            return True
    return False


def build_recall_messages(doc: dict[str, Any],
                          existing: list[dict[str, Any]]) -> list[dict[str, str]]:
    """构造补抽的提问。

    把第一遍已经抽到的陈述列出来，是防重最有效的一招：
    光说「不要重复」没用，得让模型看见重复长什么样。
    """
    lines = []
    for f in existing:
        lines.append(f"- [{TYPE_CN.get(f.get('fact_type'), f.get('fact_type'))}] "
                     f"{(f.get('statement') or '').strip()}")
    existing_block = "\n".join(lines) if lines else "（第一遍没有抽到任何事实）"

    user = (
        "# 第一遍已抽到的事实\n"
        f"{existing_block}\n\n"
        "# 待补抽的文档\n"
        f"{format_document(doc)}\n\n"
        "# 你的任务\n"
        "只找**漏掉的** need（需求）与 concern（顾虑）。上面已有的不要再出。\n"
        "确实没有漏的，输出 `[]`。\n"
        "只输出 JSON 数组。"
    )
    return [
        {"role": "system", "content": load_recall_prompt()},
        {"role": "user", "content": user},
    ]


def note_for(n_recalled: int, by_type: dict[str, int], ran: bool,
             empty_text: bool = False) -> str:
    """回执里那句话。必须说清「补没补、补回来什么」，不能含糊。"""
    if not ran:
        return ""
    if n_recalled:
        parts = "、".join(f"{TYPE_CN[t]} {n}" for t, n in by_type.items() if n)
        return f"第二遍补抽找回 {n_recalled} 条（{parts}）"
    if empty_text:
        return "第二遍也找了一遍，同样没有可用事实——这多半是记录本身没写业务内容"
    return "第一遍缺需求/顾虑，第二遍定向找过一遍，同样没有——不等于客户没说，可能没写进记录"

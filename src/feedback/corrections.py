"""修正记录：把「人不同意引擎」这件事变成结构化数据。

对这套系统来说，用户的每一次否决都是最贵的训练信号：他不是在调整界面，
是在说「这条判断不对」。而现在这个信号的去向是——留在前端的一个
`status="retracted"` 字段里，刷新页面还在，重启服务就没了，
**永远不会影响下一次抽取**。同一段话被抽错第二次、第三次，
用户只能再点一次「这条不对」。

这个模块只做三件事，都在确定性范围内：

1. **记下来**：从「改之前 / 改之后」两份事实差分里推出修正，不让用户填表。
   他点了「这条不对」，系统就该知道这是 `not_a_fact` + 哪段原文。
2. **归类**：同类错误出现够多次，才升格成规则（见 `lessons.py`）。
   一次改判可能只是特例，三次就是系统性偏差。
3. **可回滚**：修正落 JSONL，追加写、不覆盖，随时能看是谁在哪天改的。

刻意不做的事：

- **不自动改提示词**。模型行为的改动必须能被评测集回答「变好了还是变坏了」，
  自动改等于让线上系统变成一个没人复核的实验场。
- **不做模糊匹配**。黑名单只拦「完全相同的原文片段 + 同一类事实」，
  相似度阈值一加，误伤就无法解释，而误伤的代价是用户再也不信这个黑名单。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: 修正原因的封闭集。开放文本的理由没法聚类，也就没法沉淀成规则。
REASONS = ("not_a_fact", "wrong_value", "duplicate", "expired", "other")

REASON_CN = {
    "not_a_fact": "这段不是事实",
    "wrong_value": "内容抽错了",
    "duplicate": "重复了",
    "expired": "已经过期",
    "other": "其它原因",
}

#: 会让「同类错误」升格成规则的原因。其余原因只记录、不改行为：
#: 「已经过期」是时间的问题，不是引擎的问题。
LESSON_REASONS = ("not_a_fact", "wrong_value", "duplicate")


@dataclass(frozen=True)
class Correction:
    """一条修正。字段少是刻意的——多一个字段，就多一处没人填的空。"""

    fact_id: str
    fact_type: str
    reason: str
    span: str = ""
    field: str = ""
    was: str = ""
    now: str = ""
    source_id: str = ""
    customer_id: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        if self.reason not in REASONS:
            raise ValueError(f"未知原因 {self.reason!r}，只允许 {REASONS}")

    @property
    def key(self) -> tuple[str, str, str]:
        """同一条修正的指纹：同一类事实、同一段原文、同一个原因。

        用来去重——用户来回点两次「这条不对」不该算两次，
        否则「≥3 次成规则」的门槛会被手抖触发。
        """
        return (self.fact_type, self.span.strip(), self.reason)

    @property
    def cn(self) -> str:
        return REASON_CN.get(self.reason, self.reason)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _val(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def diff(before: list[dict[str, Any]], after: list[dict[str, Any]],
         *, customer_id: str = "") -> list[Correction]:
    """从「改之前 / 改之后」两份 canonical fact_data 推出修正。

    为什么不给用户一个「为什么不对」的下拉框：那一步会让人放弃修改。
    他能做的动作只有两个——点「这条不对」、改一个字段。系统从动作本身
    推断原因，猜错也比让他多填一个表好。

    推断规则只有三条，都能被反例推翻：

    | 看到的变化 | 推断 |
    |---|---|
    | status 变成 retracted | 这段不是事实 |
    | 某个 payload 字段变了 | 内容抽错了，字段名就是 `field` |
    | 事实从列表里消失 | 不做推断（前端删行不等于否决，可能是重算） |
    """
    old = {str(r.get("fact_id") or r.get("key") or ""): r for r in before}
    out: list[Correction] = []
    for row in after:
        fid = str(row.get("fact_id") or row.get("key") or "")
        prev = old.get(fid)
        if prev is None:
            continue
        ftype = str(row.get("fact_type") or "")
        span = str(row.get("source_span") or "").strip()
        source_id = str(row.get("source_id") or prev.get("source_id") or "")

        became_retracted = (str(row.get("status") or "") == "retracted"
                            and str(prev.get("status") or "") != "retracted")
        if became_retracted:
            out.append(Correction(fid, ftype, "not_a_fact", span=span,
                                  source_id=source_id, customer_id=customer_id, at=_now()))
            continue

        # 撤回后又被恢复：不记修正，也不删旧记录——历史是追加的。
        if str(row.get("status") or "") == "retracted":
            continue

        before_payload = prev.get("payload") or {}
        after_payload = row.get("payload") or {}
        for field, new_value in after_payload.items():
            old_value = before_payload.get(field)
            if _val(old_value) != _val(new_value):
                out.append(Correction(
                    fid, ftype, "wrong_value", span=span, field=field,
                    was=_val(old_value), now=_val(new_value),
                    source_id=source_id, customer_id=customer_id, at=_now(),
                ))
    return out


def dedupe(rows: Iterable[Correction]) -> list[Correction]:
    """按指纹去重，保留最早的一条。

    保留最早而不是最晚：规则要回答的是「这类错误第一次出现是什么时候」，
    重复点一次不改变这个事实。
    """
    seen: dict[tuple[str, str, str], Correction] = {}
    for c in rows:
        seen.setdefault(c.key, c)
    return list(seen.values())


def append(path: Path, rows: Iterable[Correction]) -> int:
    """追加写。返回真正写进去的条数（已存在的指纹跳过）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    have = {c.key for c in load(path)}
    n = 0
    with path.open("a", encoding="utf-8") as f:
        for c in rows:
            if c.key in have:
                continue
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
            have.add(c.key)
            n += 1
    return n


def load(path: Path) -> list[Correction]:
    if not path.exists():
        return []
    out: list[Correction] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            out.append(Correction(**{k: row.get(k, "") for k in Correction.__dataclass_fields__}))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue          # 坏行跳过：一条坏记录不该让整个修正历史读不出来
    return out


def counts(rows: Iterable[Correction]) -> dict[str, int]:
    """按原因数一数。用来回答「用户最不满意的是什么」。"""
    out: dict[str, int] = {}
    for c in rows:
        out[c.reason] = out.get(c.reason, 0) + 1
    return out

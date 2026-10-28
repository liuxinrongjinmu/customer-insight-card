#!/usr/bin/env python3
"""最小档案：四个字段就能判断「这个客户值不值得现在花时间」。

为什么是四个
------------
完整档案有十几个字段（行业 / 阶段 / 负责人 / 规模 / 合作起始 / 商机名 /
商机金额 / 商机阶段 / 合同到期 / ARR / 客诉次数 / 关系 / 沟通偏好 / 决策流程）。
对着一个空档案要求人填十四项，实际结果是**一项都不填**：用户不知道自己填了
能换来什么，而那些数字此刻也确实不参与任何判断。

这四个不一样，它们直接进档案条，而档案条决定「三秒看出值不值得动」：

| 字段 | 为什么非填不可 |
|---|---|
| 规模 | 这客户多大——决定要不要按大客户节奏走 |
| 在办金额 | 这单值多少——P1-1 的 `impact` 直接吃这个数，没有它就只能是中位分 |
| 合同到期 | 续约窗口还有多久——过了这个点再谈就是从零开始 |
| 关系 | 该怎么开口——「最近有不满」和「主动找我们」是两套话术 |

其余字段照旧可以填，只是**补录成本要和判断价值匹配**。一次性要求填完，
等于把「档案不全」这个状态永久固定下来。

为什么校验放在服务端
--------------------
日期是人手打进来的，`2027/3/31`、`2027-3-31`、`2027年3月31日`、`20270331`
都会出现。原来的实现是 `date.fromisoformat()` 失败后 `days = 0`，
于是**「格式写错」在界面上表现为「0 天后到期」**——一条错误输入被渲染成
一条紧急情报（合同今天到期），这是最坏的一种静默失败：不报错，只给错结论。

所以这里的原则是：**认得出来的就归一化，认不出来的就报错，绝不猜。**
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

#: 最小档案的四个字段，顺序即界面顺序。
#: 前端表单的 id 与这里一一对应，改一处必须改另一处（`ui_check` 有断言盯着）。
FIELDS = ("scale", "open_amount", "contract_expires", "relationship")

LABELS = {
    "scale": "规模",
    "open_amount": "在办金额",
    "contract_expires": "合同到期",
    "relationship": "关系",
}

HINTS = {
    "scale": "如：3200 人 · 年产值 45 亿",
    "open_amount": "万元，数字，如 180",
    "contract_expires": "如：2027-03-31",
    "relationship": "热 / 正常 / 冷",
}

RELATIONSHIPS = ("warm", "neutral", "cold")

#: 规模那一格放不下长文本：档案条是一行横排的小格子，超过 40 字会挤掉别人。
MAX_SCALE = 40

#: 能认的日期写法。全部要求「年月日」齐全——只给「2027-03」能算出月底，
#: 但那是猜的，而猜出来的到期日会直接进「还有几天到期」这条判断。
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"),
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)

#: 「180万」「1,800」「180.5」「180 万」都认。
_AMOUNT_RE = re.compile(r"^[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)\s*万?\s*元?$")


def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def parse_date(value: Any) -> str:
    """把一周里见过八种写法的日期归一成 ISO。认不出来就报错。

    返回 `""` 表示「清空」（用户把这一格删了），不是错误。
    """
    s = _clean(value)
    if not s:
        return ""
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            raise ValueError(f"「{s}」不是有效日期（月份或日超出了范围）") from None
    raise ValueError(f"「{s}」认不出是哪一天，请写成 2027-03-31 这样")


def parse_amount(value: Any) -> float | None:
    """把「180万」「1,800」「180.5」归一成数字（单位：万）。空值返回 None。

    不做单位换算：写多少就是多少万。原文里出现的「180 万」「1800000」
    到底是不是同一个量级，得让填的人自己确认——自动换算猜错一次，
    这单的优先级就排错了，而排错优先级没有任何提示。
    """
    if value is None:
        return None
    if isinstance(value, bool):          # bool 是 int 的子类，得先挡掉
        raise ValueError("金额要填数字")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("金额不能是负数")
        return float(value)
    s = _clean(value).replace(",", "").replace("，", "")
    if not s:
        return None
    m = _AMOUNT_RE.match(s)
    if not m:
        raise ValueError(f"「{s}」不是金额，请只填数字（单位：万）")
    n = float(m.group(1))
    if n < 0:
        raise ValueError("金额不能是负数")
    return n


def _err(field: str, value: Any, message: str) -> dict[str, str]:
    return {"field": field, "label": LABELS.get(field, field),
            "value": _clean(value), "message": message}


def normalize(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把表单原文归一成 `/api/profile` 认的补丁，顺带收集错误。

    返回 `(patch, errors)`。**有 errors 时 patch 也照常返回**——调用方可以把
    能存的那几项存下去，只让填错的那一格红着。整单退回的做法，用户会以为
    是自己哪里都填错了，而实际只有一个日期格式不对。

    认得两种写法：
    - 最小档案的扁平键：`open_amount` / `contract_expires`
    - 完整档案的嵌套键：`open_opportunity.amount` / `contract.expires_on`
    两条路都要过校验，否则「最小表单拦住了错日期、完整表单没拦住」，
    等于这个校验只挡了一半人。
    """
    patch: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    if "scale" in raw:
        s = _clean(raw.get("scale"))
        if len(s) > MAX_SCALE:
            errors.append(_err("scale", s, f"规模太长（{len(s)} 字），"
                                           f"这一格最多 {MAX_SCALE} 字，写关键的那半句"))
        else:
            patch["scale"] = s

    if "relationship" in raw:
        r = _clean(raw.get("relationship"))
        if r and r not in RELATIONSHIPS:
            errors.append(_err("relationship", r, "关系只能是 热 / 正常 / 冷 三档之一"))
        else:
            patch["relationship"] = r

    # 在办金额：扁平键优先，否则读嵌套键。两边都读，是因为完整表单走的是嵌套。
    opp = raw.get("open_opportunity")
    has_flat_amt = "open_amount" in raw
    if has_flat_amt or isinstance(opp, dict):
        src = raw["open_amount"] if has_flat_amt else opp.get("amount")
        try:
            amount = parse_amount(src)
        except ValueError as exc:
            errors.append(_err("open_amount", src, str(exc)))
        else:
            if amount is not None or has_flat_amt:
                patch.setdefault("open_opportunity", {})["amount"] = amount

    ct = raw.get("contract")
    has_flat_exp = "contract_expires" in raw
    if has_flat_exp or isinstance(ct, dict):
        src = raw["contract_expires"] if has_flat_exp else ct.get("expires_on")
        try:
            expires = parse_date(src)
        except ValueError as exc:
            errors.append(_err("contract_expires", src, str(exc)))
        else:
            if expires or has_flat_exp:
                patch.setdefault("contract", {})["expires_on"] = expires

    return patch, errors


def merge(updates: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把归一后的补丁叠回原载荷。嵌套字典逐键合并，不整块替换。

    整块替换会出事：完整表单只提交了 `contract.expires_on`，
    把 `contract.arr` 一起盖掉，用户的合同金额就凭空消失了。
    """
    out = dict(updates)
    for k, v in patch.items():
        cur = out.get(k)
        if isinstance(v, dict) and isinstance(cur, dict):
            out[k] = {**cur, **v}
        else:
            out[k] = v
    return out


def is_minimal(profile: dict[str, Any] | None) -> bool:
    """档案空着就该开「最小档案」模式：四格，不是十四格。"""
    return not profile

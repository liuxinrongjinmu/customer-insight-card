#!/usr/bin/env python3
"""存量客户池：把评测集里的 33 个客户接进演示，让「规模」是真的。

为什么需要这一层
----------------
只有 6 个客户时，「今天要做的事」永远是三五条，看不出这个工具到底是在帮人
做事还是在帮人挑事。真实销售手里是 30～200 个客户，难点从来不是「想不起
要做什么」，而是「今天只有 8 小时，先动哪三个」——6 个客户的界面回答不了
这个问题，因为它根本不需要排序。

数据从哪来
----------
原文与事实**全部来自 `eval/golden/golden_set.jsonl`**，那是人手标注的金标准，
不是我现编的演示词。所以这 33 个客户身上的每一条事实都有原文出处，
点开「全部记录」能看到高亮片段——和 6 个重点客户是同一条管道、同一套规则。

哪一部分是补的
--------------
评测集只有 `customer_id`（c_10001…c_10033），**没有公司名**。试过用正则
从原文里抠，抠出来的全是「日去江南」「赵总说」这种噪声，只能人工补一张
`DIRECTORY`：原文里出现过的厂名照原样用（江南精密、中科新材、金桥机械……），
没出现的按行业编一个。**名字是占位，不代表真实企业。**

刻意丢掉的三条记录
------------------
`d021`（恒远重工选型会）、`d022`（星海电子拜访）、`d026`（蓝海物流例会）
讲的就是 6 个重点客户里那三家的事。留着会出现两个「恒远重工」，
演示时谁都说不清哪个是真的。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden" / "golden_set.jsonl"

#: 与重点客户内容重叠的记录：同一家公司不能同时挂在两张卡上。
DROP_DOCS = {"d021", "d022", "d026"}

#: customer_id → (公司名, 行业, 阶段)。名字是占位，阶段是从原文内容判的。
DIRECTORY: dict[str, tuple[str, str, str]] = {
    "c_10001": ("江南精密", "精密制造", "选型 POC 阶段"),
    "c_10002": ("融信电子", "电子制造", "上线切换阶段"),
    "c_10003": ("元启商贸", "商贸流通", "初步接触"),
    "c_10004": ("汉威集团", "综合集团", "内部立项（预算待批）"),
    "c_10005": ("星原显示", "电子制造", "售后处理"),
    "c_10006": ("泰盛重工", "装备制造", "已失单"),
    "c_10007": ("嘉合智造", "装备制造", "方案交流"),
    "c_10008": ("中科新材", "新材料", "需求调研"),
    "c_10009": ("长川物流", "仓储物流", "方案交流"),
    "c_10010": ("晟达建材", "建筑材料", "商务比价"),
    "c_10011": ("锦泓纺织", "纺织制造", "关键人更替"),
    "c_10012": ("金桥机械", "机械制造", "招标准备"),
    "c_10013": ("德茂食品", "食品加工", "需求孵化"),
    "c_10014": ("卓远机械", "机械制造", "内部评审"),
    "c_10015": ("长信光电", "光电制造", "需求变更"),
    "c_10016": ("华翔汽配", "汽车零部件", "项目实施"),
    "c_10017": ("天工装备", "装备制造", "复购机会"),
    "c_10018": ("汇通包装", "包装印刷", "方案确认"),
    "c_10019": ("瑞泰包装", "包装印刷", "需求确认"),
    "c_10020": ("东方精工", "装备制造", "初步接触"),
    "c_10021": ("广源化工", "化工", "招标前"),
    "c_10022": ("永盛机械", "机械制造", "商务谈判"),
    "c_10023": ("银河半导体", "半导体", "项目启动"),
    "c_10024": ("明州热电", "能源电力", "试点验证"),
    "c_10025": ("弘信通信", "通信服务", "承诺兑现中"),
    "c_10026": ("立成家居", "家居制造", "产能扩张规划"),
    "c_10027": ("安泰医疗", "医疗器械", "合规审查"),
    "c_10028": ("华阳能源", "能源电力", "内部推动"),
    "c_10029": ("泰宁医院", "医疗机构", "等保整改"),
    "c_10030": ("松岭化工", "化工", "运维服务"),
    "c_10031": ("海通汽配", "汽车零部件", "招标准备"),
    "c_10032": ("双龙玻璃", "玻璃建材", "关键人更替"),
    "c_10033": ("力拓模具", "模具制造", "停滞（对接人调岗）"),
}


def load_pool(path: Path | None = None) -> list[dict[str, Any]]:
    """把金标准读成 demo 的客户字典结构（与 build_demo.CUSTOMERS 同形）。

    读不到文件就返回空列表——演示少 33 个客户还能跑，起不来就没得看了。
    """
    src = path or GOLDEN
    if not src.exists():
        return []

    groups: dict[str, dict[str, Any]] = {}
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        cid = row.get("customer_id") or ""
        doc_id = row.get("doc_id") or ""
        if not cid or not doc_id or doc_id in DROP_DOCS or cid not in DIRECTORY:
            continue

        name, industry, stage = DIRECTORY[cid]
        cust = groups.setdefault(cid, {
            "id": cid,
            "name": name,
            "industry": industry,
            "stage": stage,
            "owner": "你",
            "pool": True,
            # 没有主数据档案。这不是缺数据，是事实层本来就不管这件事：
            # 事实答「他提过什么」，规模/合同额/客诉率只能从 CRM 来。
            "profile": {},
            "records": [],
            "facts": [],
        })

        cust["records"].append({
            "id": doc_id,
            "date": row.get("occurred_at") or "",
            "type": row.get("source_type") or "visit_note",
            "text": row.get("text") or "",
        })

        for f in row.get("gold_facts") or []:
            if not f.get("fact_type") or not f.get("payload"):
                continue
            cust["facts"].append({
                "rid": doc_id,
                "fact_type": f["fact_type"],
                "statement": f.get("statement") or "",
                "span": f.get("source_span") or "",
                "payload": f["payload"],
                "basis": f.get("confidence_basis") or "paraphrase",
                # 事件时间 ≠ 入库时间。c_10006 那条「本条为 9 月底补录」的拜访
                # 就是靠这个字段才能在时间机器上回到 6 月看。
                "stated_at": f.get("stated_at") or "",
            })

    # 按 id 排序，界面上就是固定的 c_10001…c_10033，不会每次刷新都换位置。
    return [groups[k] for k in sorted(groups)]


POOL: list[dict[str, Any]] = load_pool()

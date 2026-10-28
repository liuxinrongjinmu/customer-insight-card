"""对外演示的提词器：六幕，每幕给「点哪里 / 说什么 / 屏幕上现在真的是什么」。

## 为什么是脚本，不是一份台词文档

台词文档里写死的数字，过两周就跟系统对不上了——这个项目已经为此改过三轮
（275 → 642 → 694 → 705 个测试，每个数字都同时出现在五六个地方）。
所以每一幕的「屏幕上现在真的是什么」都现场从接口取：**数据变了，提词器跟着变**，
讲的人不用背任何人的数字，也不会在台上说出一个屏幕上没有的值。

## 只读

不写库、不调模型、不 reset。演示进行到一半也能另开一个终端跑一遍，
它不会把你要讲的那张卡改掉。

用法：

    python3 demo/serve.py --allow-reset    # 终端 1
    python3 demo/tour.py                   # 终端 2：六幕提词

`--act N` 只看第 N 幕；`--base` 指向别的端口。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8000"

WIDTH = 78


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def get(base: str, path: str) -> Any:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.load(r)
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连不上 {base}。先起服务：python3 demo/serve.py --allow-reset\n（{exc}）"
        )


class Live:
    """一次运行内共享的现场数据。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.health = get(base, "/api/health")
        self.customers = get(base, "/api/customers")["customers"]
        self.feedback = get(base, "/api/feedback")

    def by_name(self, name: str) -> dict:
        for c in self.customers:
            if c["name"] == name:
                return c
        raise KeyError(name)

    def richest(self) -> dict:
        """事实最多的那个客户——开场用它最省事，一屏里能看见七类事实里的好几类。"""
        return max(self.customers, key=lambda c: len(c.get("fact_data") or []))

    def n_records(self) -> int:
        return sum(len(c.get("records") or []) for c in self.customers)

    def n_facts(self) -> int:
        return sum(len(c.get("fact_data") or []) for c in self.customers)

    def multi_doc(self) -> list[dict]:
        return [c for c in self.customers if len(c.get("records") or []) >= 2]

    def conflicted(self) -> list[dict]:
        return [c for c in self.customers
                if (c.get("stats") or {}).get("n_conflict_groups")]


# ---------------------------------------------------------------------------
# 排版
# ---------------------------------------------------------------------------

def head(n: int, title: str) -> None:
    print()
    print("═" * WIDTH)
    print(f"  第 {n} 幕　{title}")
    print("═" * WIDTH)


def say(text: str) -> None:
    print(f"  【台词】{text}")


def do(text: str) -> None:
    print(f"  【操作】{text}")


def see(label: str, value: str) -> None:
    print(f"          · {label}：{value}")


def warn(text: str) -> None:
    print(f"  【别这么说】{text}")


# ---------------------------------------------------------------------------
# 六幕
# ---------------------------------------------------------------------------

def act_scale(live: Live) -> None:
    head(1, "规模：这些数字是真的")
    do("打开 http://127.0.0.1:8000/，停在左侧客户列表，别急着点")
    say(f"左边这一列是客户，{len(live.customers)} 个；库里有 "
        f"{live.n_records()} 篇记录、{live.n_facts()} 条事实。")
    say("它不是为演示拼的一个页面——页面上的每一行都是从同一个库里读出来的，"
        "库里没有的东西，这里一行都不会多。")
    see("客户", f"{len(live.customers)} 个")
    see("记录", f"{live.n_records()} 篇")
    see("事实", f"{live.n_facts()} 条")
    see("有 ≥2 篇记录的客户", "%d 个（变化类判断只在这些人身上成立）" % len(live.multi_doc()))
    see("存储", "SQLite 单文件，进程退出不丢（%s）" % live.health["store"]["mode"])
    warn("别说「覆盖了多少万客户」。这里是 39 个，说小了没人信，说大了就是编。")


def act_card(live: Live) -> None:
    c = live.richest()
    head(2, "一张卡：它到底回答了什么")
    do(f"点左侧的「{c['name']}」")
    say("中间这张卡只回答三件事：这个人是谁、过去发生过什么、现在最该做什么。")
    say("最上面那句是系统拼的，不是模型写的——所以你不会看到一个漂亮的总结"
        "底下对不上一行证据。")
    print()
    print("  ┌" + "─" * (WIDTH - 4))
    for line in _wrap(c.get("summary") or "（这张卡还没有一句话结论）", WIDTH - 8):
        print("  │ " + line)
    print("  └" + "─" * (WIDTH - 4))
    see("这张卡的事实数", str((c.get("stats") or {}).get("n_facts", 0)))
    see("冲突组", str((c.get("stats") or {}).get("n_conflict_groups", 0)))
    if c.get("gaps"):
        see("空白项", "%d 条，第一条是「%s」" % (len(c["gaps"]), c["gaps"][0]))
    else:
        see("空白项", "没有——七类事实这一户都有一条能用的")
    warn("别把一句话结论念成销售判断。「最该当心」是规则按证据排的，"
         "它不知道这单能不能成。")


def act_trace(live: Live) -> None:
    c = live.richest()
    fact = _pick_traceable(c)
    head(3, "溯源：每个判断都能点回原文")
    if fact is None:
        print("  这个客户身上没有可展示溯源的事实。换一个客户再跑这一幕。")
        return
    do(f"在「{c['name']}」的卡片里找到这条，点它")
    say("系统说的每一句话，都能指回原文的某几个字；指不回去的，它会在入库前就拒收。")
    print()
    see("卡上这句", fact["statement"])
    see("原文那句", repr(fact["source_span"]))
    see("出自", "%s · %s" % (fact.get("stated_at", "?"), fact.get("source_type", "?")))
    hit = _find_record(c, fact)
    if hit:
        see("在那篇记录里", "第 %d 篇，原文：「%s」" % (hit[0], _clip(hit[1]["text"], 60)))
        print()
        print("  （现场可以当场翻那篇记录，让观众看这几个字确实在里面。）")
    else:
        print("  （这条的原文不在当前客户的记录里，别硬指——换成上面那条记录里的示例。）")
        return
    n_risk = sum(1 for f in (c.get("fact_data") or []) if f["fact_type"] == "risk_signal")
    if n_risk:
        see("变化类信号", f"{n_risk} 条，全部由规则从原文里读，不是模型猜的")
    warn("别说「模型不会编」。模型会编，所以每一句都要过溯源校验；"
         "拒收计数在报表里，不在嘴上。")


def act_conflict(live: Live) -> None:
    head(4, "冲突：两种说法都留着，不替客户圆场")
    cands = live.conflicted()
    if not cands:
        print("  这批数据里没有冲突组。别现编一个——这一幕跳过，直接讲空白项。")
        return
    c = cands[0]
    do(f"点「{c['name']}」，看卡片上标红的那一组")
    say("同一个字段，两篇记录说两个数。系统不挑一个信，也不平均——"
        "它把两种说法并排放着，标成待确认。")
    pair = _conflict_rows(live, c)
    if pair:
        for i, r in enumerate(pair, 1):
            see(f"说法 {i}", f"{r['statement']}（{r.get('source_date', '?')}）")
        print()
        say("注意这两句都是原话摘出来的，不是系统转述的——"
            "所以冲突是真的冲突，不是我把它俩读错了。")
    else:
        see("（渲染行里没找到标红的那一对）", "别硬讲，翻到「这条不对」那一段去")
    see("这一户的冲突组", str((c.get("stats") or {}).get("n_conflict_groups", 0)))
    say("选一个错的信了，比留着两个更糟：前者用户永远不会知道，后者用户会去问一句。")
    warn("别说「系统会自动判断哪个对」。它没有那个能力，也不该有——"
         "谁对只有客户知道。")


def act_gaps(live: Live) -> None:
    head(5, "空白：三种「没有」必须分开说")
    say("这是这套系统最容易被忽略、也最要紧的一处：用户不知道系统不知道什么，"
        "就会把「没提到」当成「没问题」。")
    buckets = {
        "未计入": [],
        "本系统不产出": [],
        "已过保鲜期": [],
        "无有效记录": [],
    }
    claimed: set[tuple[str, str]] = set()
    for c in live.customers:
        for g in c.get("gaps") or []:
            # 按上面的优先级取第一个命中的桶：「无有效记录；另有 1 条…未计入」
            # 同时满足两个桶，只算更具体的那个，否则同一行会在幕上出现两遍。
            for key in buckets:
                if key in g and len(buckets[key]) < 2:
                    if (c["name"], g) not in claimed:
                        buckets[key].append((c["name"], g))
                        claimed.add((c["name"], g))
                    break
    for key, items in buckets.items():
        if not items:
            print(f"\n  ○ 「{key}」：这批数据里没有这种空。别现编一个。")
            continue
        print(f"\n  ○ 「{key}」")
        for name, g in items:
            print(f"      {name}: {g}")
    print()
    say("三种空，三种应对：去打听 / 重新确认 / 再记一篇。说成同一种，"
        "用户会把「系统产不出」读成「这个客户没有风险」。")
    warn("别把空白项讲成功能列表。它是边界声明，不是卖点。")


def act_learn(live: Live) -> None:
    fb = live.feedback
    head(6, "它会不会学：改判进黑名单，下次不再犯")
    do("在任意一条事实上点「这条不对」，选原因，再点一次「记一笔」")
    say("撤回只是把这条事实标成无效，刷新还在、重启就没了。"
        "真正会留下的是两层：这段原文进黑名单，同一段话下次不再抽；"
        "同类型同字段的改判进补丁建议。")
    see("现在的修正", f"{fb['n_corrections']} 条")
    see("黑名单", f"{fb['n_blacklist']} 条")
    for lesson in (fb.get("lessons") or [])[:3]:
        see("记住了", f"{lesson['cn']}：{lesson['key']}（{lesson['why']}）")
    if not fb.get("lessons"):
        see("记住了", "还没有——现场点一条，让观众看见它出现在这里")
    print()
    say("这一层是明着做的，不是「模型越用越聪明」这种话。"
        "它能记住的是具体的几段原文和几个字段，不是一个模糊的偏好。")
    warn("别说「用得越多越准」。没有这条链路，同一个错会一直犯；"
         "有了它，至少同一个错不会犯第二次。")


def act_close(live: Live) -> None:
    head(7, "收尾：四件现在不能吹的事")
    say("演示到最后，主动说清边界。藏着，观众问一句就露；自己说，反而可信。")
    f1 = _metric("eval/results/re_deepseek.json", "overall.closed_field_f1")
    f1_txt = (f"封闭集字段 F1 {f1:.3f}（门槛 0.85）" if f1 is not None
              else "封闭集字段 F1 达不到 0.85（结果文件读不到，现场别报具体数值）")
    lines = [
        "外部效度还没验：91 篇脏数据是我自己造的，只能证明「不崩、不编、不藏」，"
        "不能证明真实销售的话也长这样（P2-1 等真实记录）。",
        f"五项评测门槛里两项没达到：{f1_txt}；归并层还有一处只能靠人确认。"
        "测试通过 ≠ 门槛达到。",
        "没有身份认证：`--user` 是启动参数，同机任何人都能换个身份起服务。"
        "归属隔离只回答「这条数据是谁的」，不回答「你是谁」。",
        "单进程之外没验：多 worker 并发写同一个库会不会丢，这轮没解。",
    ]
    for i, line in enumerate(lines, 1):
        print(f"  {i}. {line}")
    print()
    print("  测试与门槛的现值，别背，跑这三条：")
    print("      python3 -m unittest discover -s tests        # 通过 = 测试都对")
    print("      python3 eval/report_metrics.py --check       # 台账 42 行逐位核对")
    print("      python3 eval/p0_audit.py --pred out/pred_deepseek.jsonl  # P0 21/21")
    print()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行。中文按 2 格算——不折的话终端里会歪。"""
    out: list[str] = []
    line, w = "", 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > width:
            out.append(line)
            line, w = "", 0
        line += ch
        w += cw
    if line:
        out.append(line)
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _metric(rel: str, dotted: str) -> float | None:
    """从结果文件里读一个数——口径不写在台词里，写在台账那套 `文件#JSON路径` 上。"""
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / rel
    try:
        node: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return float(node) if isinstance(node, (int, float)) else None


def _find_record(c: dict, fact: dict) -> tuple[int, dict] | None:
    """这条事实的原文出在哪一篇。找不到就返回 None——不猜。"""
    span = fact.get("source_span") or ""
    if not span:
        return None
    for i, r in enumerate(c.get("records") or [], 1):
        if span in (r.get("text") or ""):
            if not fact.get("source_id") or fact["source_id"] == r.get("id"):
                return i, r
    return None


def _pick_traceable(c: dict) -> dict | None:
    """挑一条原文能在这户记录里找到的事实——现场翻得到，才有说服力。"""
    facts = c.get("fact_data") or []
    for f in facts:
        if f.get("source_span") and _find_record(c, f):
            return f
    return facts[0] if facts else None


def _conflict_rows(live: Live, c: dict) -> list[dict]:
    """标红的那一对渲染行。

    走 `/api/analyze` 且**不传 `fact_data`**：接口会把传进去的事实写回服务端
    （为了演示里刷新还能看到改过的版本），提词器不该动你要讲的那张卡。
    """
    import urllib.request as _r
    body = json.dumps({"customer_id": c["id"], "today": live.health["as_of"]}).encode()
    req = _r.Request(live.base + "/api/analyze", data=body,
                     headers={"Content-Type": "application/json"}, method="POST")
    with _r.urlopen(req, timeout=10) as r:
        view = json.load(r)["view"]
    return [row for row in (view.get("facts") or []) if row.get("conflicted")][:2]


ACTS = (act_scale, act_card, act_trace, act_conflict, act_gaps, act_learn, act_close)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对外演示的提词器（只读）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--act", type=int, default=0, help="只看第 N 幕（1~7），0 = 全跑")
    args = ap.parse_args(argv)

    live = Live(args.base)
    print()
    print(f"  演示提词器 · {args.base}")
    print(f"  模式 {live.health['model']} · 基准日 {live.health['as_of']} · "
          f"身份 {live.health['user']} · {'离线回放' if live.health['offline'] else '实时调用'}")
    print("  所有「屏幕上现在真的是什么」都是刚从接口取的，不是台词稿写死的。")

    picks = ACTS if not args.act else (ACTS[args.act - 1],)
    if args.act and not 1 <= args.act <= len(ACTS):
        raise SystemExit(f"--act 只支持 1~{len(ACTS)}")
    for fn in picks:
        fn(live)
    print("═" * WIDTH)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

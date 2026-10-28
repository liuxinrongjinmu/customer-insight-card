#!/usr/bin/env python3
"""演示前跑一遍：把「照脚本走一遍」变成一条命令。

    python3 demo/serve.py --allow-reset    # 另开一个终端（reset 默认关，必须显式开）
    python3 demo/smoke.py                 # 全部用例（含 3 次真实抽取，约 12 秒）
    python3 demo/smoke.py --skip-model    # 跳过调模型的那几条，只验算法与边界

为什么要单独写一个脚本，而不是靠单元测试：
单元测试验的是「函数对不对」，这里验的是「演示时那条路上会不会卡」——
走的是真 HTTP、真状态、真接口。两者不重叠，也互相替代不了。

覆盖七类：
    A 正常路径    六个客户的首屏、判据、演进链
    B 异常路径    抽不出事实、拒收、撤回、过期、情报失效
    C 边界        请求参数、滑块两端、五种裁决分支、新客户
    D 产出物导出  三种产出物、Markdown 导出、条目不丢、空客户不编
    E 待办治理    两区分开、P0 预算、这条我不做（7 天静默）
    F 记一笔      草稿不落库、勾选才入库、同一份草稿不能落两次
    G 四入口      四个动作各给三个客户、深链落点真实存在、存量只报数
    P 落盘与归属  数据真写进库了没有、改档案的痕迹在不在、reset 能不能收回来

界面交互（拖拽、撤回按钮、判据展开、回执措辞）不在本脚本内，
由 demo/ui_check.py 覆盖（需 node；条数以它自己打印的为准）。

判断标准只有一条：**系统说的和事实一致**。所以这里的断言都盯着
「结论里那句话，能不能被卡片上的数据验算出来」，而不是「接口返回了 200」。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import brief as bf  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8000"
AS_OF = "2026-10-24"


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # 显式禁用代理：本机上常驻的 http_proxy 会把 127.0.0.1 也劫走，
        # 表现是「服务明明活着，请求却 502」。上一轮被这个坑过一次。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            except Exception:                                    # noqa: BLE001
                return exc.code, {"error": str(exc)}
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"连不上 {self.base}。先起服务：python3 demo/serve.py\n（{exc.reason}）"
            ) from exc

    def ok(self, path: str, body: dict | None = None) -> dict:
        code, data = self.call(path, body)
        if code >= 400:
            raise AssertionError(f"{path} 返回 {code}：{data.get('error')}")
        return data


# ---------------------------------------------------------------------------
# 用例注册
# ---------------------------------------------------------------------------

CASES: list[tuple[str, str, str, object]] = []


def case(group: str, cid: str, title: str):
    def deco(fn):
        CASES.append((group, cid, title, fn))
        return fn
    return deco


def need(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class Ctx:
    """一次运行内共享的句柄。用例之间有依赖时按顺序取。

    **基线只在 reset 之后取一次，之后所有用例都从这份快照算。**
    `/api/analyze` 会把传入的 fact_data 写回服务端内存（这是为了让演示里
    刷新页面还能看到改过的版本），所以「每次重新拉 /api/customers」
    拿到的是被上一个用例改过的数据。拿它当基线，
    会得到「撤回前就已经撤回过了」这种自相矛盾的结论——
    这不是被测系统的问题，是测试自己把先后顺序搅混了。
    """

    def __init__(self, cli: Client, skip_model: bool) -> None:
        self.cli = cli
        self.skip_model = skip_model
        self._all: list[dict] | None = None
        self.extracted: dict[str, dict] = {}

    def reset(self) -> None:
        code, data = self.cli.call("/api/reset", {})
        if code == 404:
            # 默认不挂载（P2-3）。这里不能只说「404」，得说清该怎么办——
            # 否则下一个人会去翻路由是不是写错了。
            raise SystemExit(
                "/api/reset 返回 404：服务没有开 --allow-reset。\n"
                "冒烟与界面断言靠 reset 回到干净基线，起服务时请带：\n"
                "    python3 demo/serve.py --allow-reset\n"
                "（默认关掉是有意的：一个能让数据消失的接口，默认必须是关的。）"
            )
        if code >= 400:
            raise AssertionError(f"/api/reset 返回 {code}：{data.get('error')}")
        self._all = None
        self.extracted.clear()

    def customers(self) -> list[dict]:
        if self._all is None:
            self._all = self.cli.ok("/api/customers")["customers"]
        return self._all

    def cust(self, cid: str) -> dict:
        for c in self.customers():
            if c["id"] == cid:
                return c
        raise AssertionError(f"没有这个客户：{cid}")

    def by_name(self, name: str) -> dict:
        for c in self.customers():
            if c["name"] == name:
                return c
        raise AssertionError(f"没有这个客户：{name}")

    def base_facts(self, cid: str) -> list | None:
        for c in self.customers():
            if c["id"] == cid:
                return c["fact_data"]
        return None

    def view(self, cid: str, today: str | None = None, fact_data: list | None = None) -> dict:
        body: dict = {"customer_id": cid, "today": today or AS_OF}
        fd = fact_data if fact_data is not None else self.base_facts(cid)
        if fd is not None:
            body["fact_data"] = fd
        return self.cli.ok("/api/analyze", body)["view"]

    def extract_sample(self, sid: str) -> dict:
        if sid in self.extracted:
            return self.extracted[sid]
        if self.skip_model:
            raise AssertionError("跳过了模型调用（--skip-model）")
        s = next(x for x in self.cli.ok("/api/samples")["samples"] if x["id"] == sid)
        r = self.cli.ok("/api/extract", {
            "text": s["text"], "doc_date": s["doc_date"],
            "source_type": s["source_type"], "customer_name": s["customer_name"],
        })
        self.extracted[sid] = r
        # 抽取会新建客户，视图要从 analyze 拿（customers 列表是缓存的）
        r["view"] = self.view(r["customer"]["id"], AS_OF)
        return r


# ---------------------------------------------------------------------------
# A. 正常路径
# ---------------------------------------------------------------------------

@case("A", "A1", "39 个客户（重点 6 + 存量 33）：有事实、有结论、P0 数量与建议一致")
def _(ctx: Ctx) -> str:
    items = ctx.customers()
    focus = [c for c in items if not c.get("pool")]
    pool = [c for c in items if c.get("pool")]
    need(len(items) == 39, f"应有 39 个客户（重点 6 + 存量 33），实际 {len(items)}")
    need(len(focus) == 6 and len(pool) == 33,
         f"分组不对：重点 {len(focus)} 个、存量 {len(pool)} 个")
    for c in items:
        if not c["facts"]:
            # 存量池里确实有一份只够寒暄的记录（元启商贸，原文 6 句客套话）。
            # 这不是故障：系统该说的是「信息不足」，而不是硬凑一张卡出来。
            need(c["pool"], f"{c['name']} 是重点客户却一条事实都没有")
            need("信息不足" in c["summary"],
                 f"{c['name']} 零事实，但结论没说信息不足：{c['summary']}")
            continue
        need(c["summary"].endswith("。"), f"{c['name']} 结论不以句号结尾：{c['summary']}")
        p0 = [a for a in c["actions"] if a["p"] == "P0"]
        need(len(p0) == c["n_p0"],
             f"{c['name']} n_p0={c['n_p0']} 但 P0 建议有 {len(p0)} 条——页眉和列表会打架")
    return (f"重点 6 个 P0 " + " ".join(f"{c['name']}:{c['n_p0']}" for c in focus)
            + f"；存量 33 个共 {sum(c['n_p0'] for c in pool)} 条 P0")


@case("A", "A2", "每条 P0 都挂得住依据事实")
def _(ctx: Ctx) -> str:
    bad = []
    for c in ctx.customers():
        for a in c["actions"]:
            if a["p"] != "P0":
                continue
            if not a.get("basis"):
                bad.append(f"{c['name']}《{a['title'][:18]}》没有 basis")
                continue
            if a["basis"] not in {f["key"] for f in c["facts"]}:
                bad.append(f"{c['name']}《{a['title'][:18]}》的 basis 指向不存在的事实")
    need(not bad, "；".join(bad))
    total = sum(1 for c in ctx.customers() for a in c["actions"] if a["p"] == "P0")
    return f"{total} 条 P0 全部能跳到依据事实"


@case("A", "A3", "恒远重工：判据写出真实间隔与窗口")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("恒远重工")
    js = [j for f in c["facts"] for j in f["judgements"]]
    need(js, "恒远应当有裁决判据（改期那条）")
    evo = [j for j in js if j["verdict"] == "evolution" and j["field"] == "due_date"]
    need(evo, f"没有判成改期的裁决，实际 {[(j['verdict'], j['field']) for j in js]}")
    j = evo[0]
    need("40 天" in j["rule"], f"判据没写出真实间隔：{j['rule']}")
    need("±3 天" in j["rule"], f"判据没写出窗口大小：{j['rule']}")
    return f"{j['from_value']} → {j['to_value']}，{j['rule'][:34]}…"


@case("A", "A4", "蓝海物流：逾期承诺的天数算得对")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("蓝海物流")
    over = [f for f in c["facts"]
            if f["fact_type"] == "commitment" and f["overdue"] and not f["superseded"]]
    need(len(over) == 1, f"应恰好 1 条逾期承诺，实际 {len(over)}")
    f = over[0]
    need(f["due_in"] < 0, f"due_in 应为负数，实际 {f['due_in']}")
    need(c["n_p0"] >= 1, "有逾期承诺却没有 P0")
    return f"逾期 {-f['due_in']} 天，P0={c['n_p0']}"


@case("A", "A5", "东岭铜业：四条承诺演进成一个终态")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("东岭铜业")
    com = [f for f in c["facts"] if f["fact_type"] == "commitment"]
    need(len(com) == 4, f"应有 4 条承诺，实际 {len(com)}")
    sup = [f for f in com if f["status"] == "superseded"]
    live = [f for f in com if f["status"] == "active"]
    need(len(sup) == 3, f"应 3 条被取代，实际 {len(sup)}")
    need(len(live) == 1, f"应 1 条有效，实际 {len(live)}")
    need(live[0]["payload"].get("fulfillment") == "waived",
         "有效的那条应当是「客户作废」，否则演进链终点不对")
    need(c["n_p0"] == 0, f"承诺已被客户作废，不该还有 P0，实际 {c['n_p0']}")
    return f"07-28 → 08-26 → 09-30 → 作废；P0=0"


@case("A", "A6", "每条事实都挂得回原文")
def _(ctx: Ctx) -> str:
    bad = []
    for c in ctx.customers():
        rec_ids = {r["id"] for r in c["records"]}
        for f in c["facts"]:
            if f["rid"] not in rec_ids:
                bad.append(f"{c['name']}/{f['statement'][:16]}")
            if not f.get("span"):
                bad.append(f"{c['name']}/{f['statement'][:16]}（无溯源片段）")
    need(not bad, "挂不回原文：" + "、".join(bad[:5]))
    total = sum(len(c["facts"]) for c in ctx.customers())
    return f"{total} 条事实全部有记录来源与溯源片段"


@case("A", "A7", "竞品结论挑的是「最该当心的」，不是「抽得最准的」")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("恒远重工")
    need("云枢科技" in c["summary"],
         f"结论应点名云枢科技（客户倾向看其案例、我方落后），实际：{c['summary']}")
    need("最靠前的是博思" not in c["summary"], "把「抽得准」当成了「威胁大」")
    return "云枢科技（我方 trailing）被点名"


# ---------------------------------------------------------------------------
# B. 异常路径
# ---------------------------------------------------------------------------

@case("B", "B1", "抽不出事实时，不假装抽到了")
def _(ctx: Ctx) -> str:
    r = ctx.extract_sample("s3")
    need(r["n_facts"] == 0, f"s3 应当抽 0 条，实际 {r['n_facts']}")
    v = r["view"]
    need(v["n_p0"] == 0, "0 条事实却有 P0")
    need("信息不足" in v["summary"] or "没有任何可用事实" in v["summary"],
         f"结论没说清信息不足：{v['summary']}")
    # 现场粘贴出来的客户，阶段是空的，结论里不该冒出「当前处于信息不足」这种病句
    need("当前处于信息不足" not in v["summary"],
         f"新建客户的结论把「信息不足」当成了阶段：{v['summary']}")
    return f"0 条事实 · P0=0 · {v['summary'][:34]}…"


@case("B", "B2", "拒收要说出原因，而且原因要分类")
def _(ctx: Ctx) -> str:
    r = ctx.extract_sample("s2")
    if not r["n_rejected"]:
        # 模型这次全过了，拒收这条链路就没被走到。
        # 但「跳过断言」等于这条用例时有时无——上一次跑命中过
        # 「payload 必填字段为空」，这一次一条没有，同一份脚本两种结论，
        # 那不是用例，那是看运气。
        # 所以退一步验分类器本身：喂它两类真实格式的原因字符串，
        # 看它会不会把错误码剥掉、会不会把同类归并、条数对不对。
        from demo.serve import summarize_rejections

        got = summarize_rejections([
            {"_reason": "[E008] payload.name: payload 必填字段为空"},
            {"_reason": "[E008] payload.name: payload 必填字段为空"},
            {"_reason": "[E003] source_span: 溯源片段在正文中定位不到"},
            {"_reason": "缺少 fact_type"},
        ])
        need(sum(x["n"] for x in got) == 4, f"分类条数之和应为 4，实际 {got}")
        by = {x["reason"]: x["n"] for x in got}
        need(by.get("payload 必填字段为空") == 2,
             f"同类没归并，或错误码没剥掉：{got}")
        need(all("[" not in x["reason"] for x in got),
             f"分类名里还留着错误码：{got}")
        need(got[0]["reason"] == "payload 必填字段为空",
             f"应按条数降序，实际第一项是 {got[0]}")
        return "本次模型全过（0 条拒收）· 改验分类器：归并/剥码/排序均正确"

    summary = r.get("rejected_summary") or []
    need(summary, "有拒收却没有 rejected_summary")
    total = sum(x["n"] for x in summary)
    need(total == r["n_rejected"],
         f"分类条数之和（{total}）与 n_rejected（{r['n_rejected']}）对不上")
    need(all(x["reason"] for x in summary), "有分类但原因为空")
    return "、".join(f"{x['reason']} {x['n']} 条" for x in summary)


@case("B", "B3", "撤回一条事实：从结论消失，但留在卡上可撤销")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("东岭铜业")
    target = next(f for f in c["facts"]
                  if f["statement"].startswith("客户决定本期不再要求"))
    fd = [dict(d) for d in c["fact_data"]]
    for d in fd:
        if d["fact_id"] == target["fid"]:
            d["status"] = "retracted"
    v = ctx.view(c["id"], AS_OF, fd)
    row = next((f for f in v["facts"] if f["retracted"]), None)
    need(row is not None, "撤回的事实从返回里消失了——用户没法撤销")
    need(row["can_retract"] is False, "已撤回的事实还在给「这条不对」")
    need(v["n_retracted"] == 1, f"n_retracted 应为 1，实际 {v['n_retracted']}")
    return "留在卡上、标已撤回、不给撤销以外的操作"


@case("B", "B4", "撤回「客户说不用做了」，逾期承诺回到台面")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("东岭铜业")
    # 显式传基线 fact_data，不依赖服务端内存：上一个用例撤回过的数据还留在里面，
    # 拿它当 before 会把「撤回后的状态」当成「撤回前」。
    base = c["fact_data"]
    before = ctx.view(c["id"], AS_OF, base)
    need(before["n_p0"] == 0, f"撤回前不该有 P0，实际 {before['n_p0']}")
    fd = [dict(d) for d in base]
    for d in fd:
        if d["statement"].startswith("客户决定本期不再要求"):
            d["status"] = "retracted"
    after = ctx.view(c["id"], AS_OF, fd)
    need(after["n_p0"] == 1, f"撤回后应冒出 1 条 P0，实际 {after['n_p0']}")
    live = [f for f in after["facts"]
            if f["fact_type"] == "commitment" and f["status"] == "active"]
    need(len(live) == 1 and live[0]["due_in"] < 0, "复活的承诺应当已逾期")
    return f"P0 {before['n_p0']} → {after['n_p0']}，逾期 {-live[0]['due_in']} 天"


@case("B", "B5", "整组撤回：不崩、不编、不留代表")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("东岭铜业")
    fd = [dict(d) for d in c["fact_data"]]
    for d in fd:
        if d["fact_type"] == "commitment":
            d["status"] = "retracted"
    v = ctx.view(c["id"], AS_OF, fd)
    need(len([f for f in v["facts"] if f["retracted"]]) == 4, "应有 4 条撤回")
    need(v["n_p0"] == 0, f"承诺全撤回后不该有 P0，实际 {v['n_p0']}")
    need("承诺" not in v["summary"], f"结论里还在提承诺：{v['summary']}")
    return "承诺组清空，结论退化为其余类型"


@case("B", "B6", "已解决的顾虑不再进待办")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("瑞禾食品")
    concern = [f for f in c["facts"] if f["fact_type"] == "concern"]
    need(concern and concern[0]["payload"].get("is_resolved") is True,
         "瑞禾那条顾虑应当是已解决状态")
    titles = " ".join(a["title"] for a in c["actions"])
    need("集成" not in titles, f"已解决的顾虑仍然出现在建议里：{titles}")
    need(c["n_p0"] == 0, f"健康客户不该有 P0，实际 {c['n_p0']}")
    return "有顾虑记录、有结论、无待办"


@case("B", "B7", "情报会过期：拖到 90 天后，竞品从结论里退场")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("恒远重工")
    now = ctx.view(c["id"], AS_OF)
    far = ctx.view(c["id"], "2027-01-22")
    need("竞品" in now["summary"], f"在基准日应当说得出竞品：{now['summary']}")
    need("竞品" not in far["summary"],
         f"90 天后的竞争情报已过保鲜期，不该还当现状：{far['summary']}")
    return "竞争情报 90 天保鲜，过期后结论不再引用"


@case("B", "B8", "销售随手记的一句话也能抽出来；寒暄句则明说没事")
def _(ctx: Ctx) -> str:
    """这条来自一次真事：用户粘了两条一句话记录，都抽 0 条，问「这正常吗」。

    不正常。旧提示词下这两句跑四次零命中，加上「一句话也是事实」之后稳定命中。
    所以这里守两条：业务短句必须抽得出来；寒暄句必须抽不出来，而且要说清
    是「本来就没事实」，不是「太短了再补两句」。
    """
    if ctx.skip_model:
        raise AssertionError("跳过了模型调用")

    short = [("客户优先考虑带AI能力的技术方案", "visit_note"),
             ("客户预算在50万内", "im_message")]
    got = []
    cid = ""
    for text, st in short:
        body = {"text": text, "doc_date": AS_OF, "source_type": st}
        if cid:
            body["customer_id"] = cid
        else:
            body["customer_name"] = "短句抽查"
        r = ctx.cli.ok("/api/extract", body)
        cid = r["customer"]["id"]
        need(r["n_facts"] >= 1, f"一句话的业务记录抽出了 0 条：{text}")
        got.append(f"{text[:10]}→{r['fact_data'][0]['fact_type']}")

    r = ctx.cli.ok("/api/extract", {"text": "收到，谢谢。", "doc_date": AS_OF,
                                    "source_type": "im_message", "customer_id": cid})
    need(r["n_facts"] == 0, f"寒暄句不该抽出事实，实际 {r['n_facts']} 条")

    # 这里不能死盯 empty_reason == "no_fact"。
    # 寒暄句是模型最容易「直接返回空」的输入，而空有两种原因：
    #   no_fact          → 确实没有事实，这是结论
    #   model_flaky_empty → 模型这次没吐东西，这是故障
    # 系统把两者分开正是设计（见 demo/serve.py 里对 empty_reason 的注释）。
    # 硬要求 no_fact，等于要求模型每次都稳定吐一个空 JSON——那是拿运气当验收。
    # 要守的不变量是：**绝不能把故障说成结论**，且两条路都必须给出可执行下一步。
    reason = r["empty_reason"]
    need(reason in ("no_fact", "model_flaky_empty"),
         f"寒暄 0 条的原因不认识：{reason}")
    if reason == "no_fact":
        need("补上" not in (r["empty_hint"] or ""),
             f"寒暄句不该劝人补上下文：{r['empty_hint']}")
    else:
        need("再点一次" in (r["empty_hint"] or ""),
             f"判成偶发失败就必须告诉用户重试，实际：{r['empty_hint']}")
    return " · ".join(got) + f" · 寒暄 0 条（{reason}）"


# ---------------------------------------------------------------------------
# C. 边界
# ---------------------------------------------------------------------------

@case("C", "C1", "空文本 / 超长文本被挡住")
def _(ctx: Ctx) -> str:
    code, r = ctx.cli.call("/api/extract", {"text": "   ", "doc_date": AS_OF})
    need(code == 400 and "文本为空" in r.get("error", ""), f"空文本应 400，实际 {code} {r}")
    code, r = ctx.cli.call("/api/extract", {"text": "客" * 12001, "doc_date": AS_OF})
    need(code == 400 and "过长" in r.get("error", ""), f"超长应 400，实际 {code} {r}")
    return "400 + 可读提示"


@case("C", "C2", "非法日期 / 未知来源被挡住")
def _(ctx: Ctx) -> str:
    code, r = ctx.cli.call("/api/extract", {"text": "客户说要方案", "doc_date": "2026-13-45"})
    need(code == 400, f"非法日期应 400，实际 {code}")
    code, r = ctx.cli.call("/api/extract",
                           {"text": "客户说要方案", "doc_date": AS_OF, "source_type": "telepathy"})
    need(code == 400 and "未知来源" in r.get("error", ""), f"未知来源应 400，实际 {code} {r}")
    return "400 + 指出是哪个字段"


@case("C", "C3", "不存在的客户 / 错类型的 fact_data 被挡住")
def _(ctx: Ctx) -> str:
    code, r = ctx.cli.call("/api/analyze", {"customer_id": "c_nope", "today": AS_OF})
    need(code == 404, f"不存在客户应 404，实际 {code}")
    code, r = ctx.cli.call("/api/analyze", {"customer_id": "c_30001", "fact_data": "x"})
    need(code == 400, f"fact_data 非数组应 400，实际 {code}")
    return "404 / 400 分得开"


@case("C", "C4", "滑块拖到最早一天：只剩第一篇，结论跟着退")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("东岭铜业")
    early = ctx.view(c["id"], "2026-07-28")
    need(len(early["facts"]) < len(c["facts"]),
         f"最早日的事实数应少于默认日（{len(early['facts'])} vs {len(c['facts'])}）")
    need(early["n_pending_facts"] > 0, "应当报出「还有几条事实未入库」")
    need("报表" not in early["summary"], f"11 月才定的节点不该出现在 7-28 的结论：{early['summary']}")
    return f"可见 {len(early['facts'])} 条，另有 {early['n_pending_facts']} 条未入库"


@case("C", "C5", "滑块拖到最晚一天：逾期兑现，结论不炸")
def _(ctx: Ctx) -> str:
    c = ctx.by_name("恒远重工")
    far = ctx.view(c["id"], "2027-01-22")
    over = [f for f in far["facts"] if f["overdue"] and not f["superseded"]]
    need(over, "拖到 +90 天应当有承诺逾期")
    need(far["summary"].endswith("。"), "结论句子没写完")
    need("已逾期" in far["summary"], f"结论应当说出逾期：{far['summary']}")
    return f"逾期 {len(over)} 条，P0={far['n_p0']}"


def _cfact(fid: str, stmt: str, due: str, when: str, src: str = "u01") -> dict:
    return {
        "fact_id": fid, "fact_type": "commitment", "customer_id": "c_30001",
        "statement": stmt,
        "payload": {"commitment_text": "发出承诺函", "direction": "we_owe",
                    "owner_id": "u1", "due_date": due, "due_precision": "day",
                    "fulfillment": "pending"},
        "source_type": "visit_note", "source_id": src, "source_span": stmt[:8],
        "stated_at": when, "stated_at_precision": "day",
        "confidence": 0.9, "confidence_basis": "direct_quote", "recorded_at": when,
        "status": "active", "tags": [], "lang": "zh-CN",
    }


@case("C", "C6", "同一天两个到期日：判口径矛盾，两条都留")
def _(ctx: Ctx) -> str:
    facts = [
        _cfact("f1", "我方承诺 10-15 前发出承诺函。", "2026-10-15", "2026-10-10"),
        _cfact("f2", "我方承诺 11-01 前发出承诺函。", "2026-11-01", "2026-10-10"),
    ]
    v = ctx.view("c_30001", AS_OF, facts)
    js = [j for f in v["facts"] for j in f["judgements"]]
    need(js and all(j["verdict"] == "conflict" for j in js),
         f"应判 conflict，实际 {[j['verdict'] for j in js]}")
    need(all(f["status"] == "conflicted" for f in v["facts"]), "两条都该留在卡上标 conflicted")
    need("口径矛盾" in js[0]["rule"], f"判据没说清是口径矛盾：{js[0]['rule']}")
    return "conflict · 两条都留 · 判据指出 ±3 天窗口内"


@case("C", "C7", "日期相差超过一年：判存疑，不判矛盾")
def _(ctx: Ctx) -> str:
    facts = [
        _cfact("f3", "我方承诺 2026-06-01 前发出承诺函。", "2026-06-01", "2026-01-05"),
        _cfact("f4", "我方承诺 2027-10-10 前发出承诺函。", "2027-10-10", "2026-01-06"),
    ]
    v = ctx.view("c_30001", AS_OF, facts)
    js = [j for f in v["facts"] for j in f["judgements"]]
    need(js and all(j["verdict"] == "suspect" for j in js),
         f"应判 suspect，实际 {[j['verdict'] for j in js]}")
    need("超过一年" in js[0]["rule"], f"判据没说清为什么存疑：{js[0]['rule']}")
    return "suspect · 提示可能是两件事被归到一组"


@case("C", "C8", "同一件事被两条记录重复提到：算交叉印证，不算矛盾")
def _(ctx: Ctx) -> str:
    facts = [
        _cfact("f5", "我方承诺 10-30 前发出承诺函。", "2026-10-30", "2026-10-15", "u01"),
        _cfact("f6", "我方承诺 10-30 前发出承诺函。", "2026-10-30", "2026-10-16", "u02"),
    ]
    v = ctx.view("c_30001", AS_OF, facts)
    js = [j for f in v["facts"] for j in f["judgements"]]
    need(js and all(j["verdict"] == "duplicate" for j in js),
         f"应判 duplicate，实际 {[j['verdict'] for j in js]}")
    need(all(f["status"] == "active" for f in v["facts"]), "重复不该把事实标成冲突")
    return "duplicate · 两条都 active · 不报红标"


@case("C", "C9", "被拒的请求不留副作用（不造出空客户）")
def _(ctx: Ctx) -> str:
    before = len(ctx.customers())
    code, _ = ctx.cli.call("/api/extract", {"text": "   ", "doc_date": AS_OF})
    need(code == 400, "空文本应被挡住")
    after = len(ctx.customers())
    need(after == before,
         f"被拒的请求留下了客户：{before} → {after}"
         "（校验应当发生在建客户之前，否则每点一次报错就多一个空壳）")
    return f"客户数保持 {after} 个"


# ---------------------------------------------------------------------------
# D. 产出物导出
# ---------------------------------------------------------------------------
#
# 这一组验的不是排版好看，是「能不能带走」。简报在抽屉里只有本人看得到，
# 一旦导出就会流进企微、邮件、交接文档——流出去之后没人再回来核对，
# 所以这里盯的是：内容不能比原简报少、空的地方不能凭空消失、不编。

@case("D", "D1", "三种产出物都出得来，标题认客户")
def _(ctx: Ctx) -> str:
    titles = {}
    for kind in ("visit", "handover", "proposal"):
        b = ctx.cli.ok("/api/brief", {"customer_id": "c_10010", "kind": kind, "today": AS_OF})
        need(b.get("title"), f"{kind} 没有标题")
        need(b.get("blocks"), f"{kind} 没有内容块")
        titles[kind] = b["title"]
    need(len(set(titles.values())) == 3, f"三种产出物标题撞了：{titles}")
    need(all("晟达建材" in t for t in titles.values()), f"标题里没有客户名：{titles}")
    return " · ".join(f"{k}={v.split('·')[-1].strip()}" for k, v in titles.items())


@case("D", "D2", "format=md 导出的是 Markdown，不是换皮的 JSON")
def _(ctx: Ctx) -> str:
    md = ctx.cli.ok("/api/brief", {"customer_id": "c_10010", "kind": "visit",
                                   "today": AS_OF, "format": "md"})
    need("markdown" in md, f"没有 markdown 字段，实际键：{list(md)}")
    need("blocks" not in md, "导出了 markdown 却还带着原结构，两个版本会漂移")
    text = md["markdown"]
    need(text.startswith("# "), f"首行不是一级标题：{text[:30]!r}")
    need("\n## " in text, "没有二级标题，粘出去就是一大段")
    need("\n- " in text, "没有列表项")
    need(text.rstrip().split("\n")[-1].startswith("> "), "末尾缺出处声明")
    return f"{len(text)} 字符 · {text.count(chr(10) + '## ')} 个块"


@case("D", "D3", "导出的条目一条不少")
def _(ctx: Ctx) -> str:
    body = {"customer_id": "c_10010", "kind": "visit", "today": AS_OF}
    js = ctx.cli.ok("/api/brief", dict(body))
    md = ctx.cli.ok("/api/brief", dict(body, format="md"))["markdown"]
    want = sum(1 for b in js["blocks"] for i in b["items"] if (i.get("text") or "").strip())
    got = sum(1 for line in md.split("\n") if line.startswith("- ") and line != "- （无）")
    need(got == want, f"简报 {want} 条，导出 {got} 条——导出会丢字，谁还信它")
    missing = [b["h"] for b in js["blocks"] if f"\n## {b['h']}\n" not in md]
    need(not missing, f"这些块没进导出：{missing}")
    return f"{got} 条 / {len(js['blocks'])} 块，逐块对齐"


@case("D", "D4", "空客户的导出不编内容，空格子留「（无）」")
def _(ctx: Ctx) -> str:
    cid = "c_10003"
    need(not (ctx.base_facts(cid) or []), f"{cid} 不是空客户，这条用例失去意义")
    md = ctx.cli.ok("/api/brief", {"customer_id": cid, "kind": "visit",
                                   "today": AS_OF, "format": "md"})["markdown"]
    need("没有在办的承诺" in md, "空客户的承诺块被编了内容")
    need("没有记录到明确顾虑" in md, "空客户的顾虑块被编了内容")
    need("（无）" not in md, "空块整块删掉了——读的人会以为没人想过这件事")
    need(not any(w in md for w in ("万\n", "规模：")), "空客户导出里出现了凭空的钱或规模")
    return "空块保留、无编造、可整段粘走"


@case("D", "D5", "不带 format 时还是原结构（前端抽屉不受影响）")
def _(ctx: Ctx) -> str:
    b = ctx.cli.ok("/api/brief", {"customer_id": "c_10010", "kind": "visit", "today": AS_OF})
    need("blocks" in b, "原结构没了，抽屉会白屏")
    need("markdown" not in b, "不该给抽屉塞一份用不上的 markdown")
    need("chain" in b, "决策链丢了——简报里的图靠它画")
    return f"blocks {len(b['blocks'])} 块 · chain {len(b.get('chain', {}).get('nodes', []))} 个节点"


# ---------------------------------------------------------------------------
# E. 待办治理
# ---------------------------------------------------------------------------
#
# 这一组验的是「打开这一页值不值得」。94 件待办里一半是「请补充档案」时，
# 用户会正确地得出一个结论：这系统在给我派活。分两区、给预算、留一个
# 「我不做」，都是在回答同一个问题——今天到底该做哪几件。

@case("E", "E1", "待办分两区：要打的电话在前，要补的数据在后")
def _(ctx: Ctx) -> str:
    # 先清场再统计。E 组跑在 D 组后面，而 D 组往客户身上追加过事实
    # （`/api/analyze` 会落库），带着那些数据统计出来的「要动手 12 件 ·
    # 补数据 9 件」和界面上真实的首屏（12 / 6）对不上——而这个数字会被人
    # 直接抄进文档。冒烟打印的每个数都必须和用户看到的一致。
    ctx.reset()
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    need(t["n_do"] and t["n_fill"], f"两区没分开：do={t['n_do']} fill={t['n_fill']}")
    need(t["n_do"] + t["n_fill"] == t["n_total"],
         f"两区之和（{t['n_do']}+{t['n_fill']}）与总数（{t['n_total']}）对不上")
    first_fill = next(i for i, it in enumerate(t["items"]) if it["bucket"] == "fill")
    need(all(it["bucket"] != "do" for it in t["items"][first_fill:]),
         "两区交叉了：列表第一屏应该是今天要打的电话，不是请补充档案")
    need(f"{t['n_fill']} 件是补数据" in t["headline"], f"页眉没分开说：{t['headline']}")
    return f"要动手 {t['n_do']} 件 · 补数据 {t['n_fill']} 件"


@case("E", "E2", "P0 每日预算：名额只发给重点客户的要做的事")
def _(ctx: Ctx) -> str:
    t3 = ctx.cli.ok(f"/api/todo?today={AS_OF}&budget=3")
    need(t3["n_today"] == 3, f"给了 3 个名额，实际今日 {t3['n_today']} 件")
    # 今日 + 本周 + 历史欠账 = 要做的事。三个数必须能对上，
    # 否则「降级」就变成了「悄悄丢掉」（历史欠账那一栏藏丢过东西就没人发现）。
    need(t3["n_today"] + t3["n_week"] + t3["n_historic"] == t3["n_do"],
         f"三档加起来（{t3['n_today']}+{t3['n_week']}+{t3['n_historic']}）"
         f"与要做的事（{t3['n_do']}）对不上")
    need(all(i["when"] == "week" for i in t3["items"] if i["pool"] and i["aging"] != "historic"),
         "存量客户占了今日名额：它们本来就写着「不急着今天动」")
    t0 = ctx.cli.ok(f"/api/todo?today={AS_OF}&budget=0")
    need(t0["n_today"] == t0["n_do"] - t0["n_historic"],
         f"budget=0 应当是「不设上限」，实际今日只有 {t0['n_today']} 件")
    bad = ctx.cli.call(f"/api/todo?today={AS_OF}&budget=abc")
    need(bad[0] == 400, f"budget 非数字应当 400，实际 {bad[0]}")
    return (f"预算 3 → 今日 {t3['n_today']} / 本周 {t3['n_week']} / 欠账 {t3['n_historic']}；"
            f"不限 → 今日 {t0['n_today']}")


@case("E", "E4", "逾期老化：过 90 天进「历史欠账」，不占今天的名额")
def _(ctx: Ctx) -> str:
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    hist = [i for i in t["items"] if i["when"] == "historic"]
    need(hist, "没有任何一条落进历史欠账——演示数据里明明有一条拖了 116 天")
    need(all(i["aging"] == "historic" for i in hist),
         "when=historic 但 aging 不是 historic：两处口径分叉了")
    need(all(i["when"] != "today" for i in hist),
         "历史欠账占了今日名额——这正是要修的问题")
    # 90 天以内的不该被误伤
    need(all(i["aging"] != "historic" for i in t["items"] if i["when"] == "today"),
         "今天的电话里混进了历史欠账")
    need("历史欠账" in t["headline"], f"页眉没说这一栏，用户会以为它没了：{t['headline']}")
    return f"{len(hist)} 件欠账不占名额 · 页眉已说明"


@case("E", "E5", "归因三条路：还追 / 客户不需要了 / 转内部")
def _(ctx: Ctx) -> str:
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    it = next(i for i in t["items"] if i["when"] == "historic")

    # 不给原因必须挡住：一条拖了三个月的事，「为什么还没动」才是关键信息
    code, data = ctx.cli.call("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                                            "mode": "attributed", "today": AS_OF})
    need(code == 400, f"不给归因应当被挡住，实际 {code}")
    need("归因必须选一个" in data.get("error", ""), f"拒绝理由没说清：{data}")

    h = ctx.cli.ok("/api/health")
    for k in ("chasing", "not_needed", "internal"):
        need(k in (h.get("attribute_reasons") or {}), f"归因选项少了 {k}")

    r = ctx.cli.ok("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                                 "mode": "attributed", "reason": "chasing", "today": AS_OF})
    need(r["mode"] == "attributed", f"mode 不对：{r['mode']}")
    need(r["silent_until"] == "2026-11-23", f"静默到哪天不对：{r['silent_until']}")
    need(not any(i["aid"] == it["aid"] for i in r["todo"]["items"]), "归因后还在待办里")

    # 撤销要真的退回去
    u = ctx.cli.ok("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                                 "undo": True, "today": AS_OF})
    need(u["mode"] == "undone", "撤销 mode 不对")
    need(any(i["aid"] == it["aid"] for i in u["todo"]["items"]), "撤销后没回到待办里")
    return "原因必填 · 静默到 11-23 · 归因后退场 · 可撤销"


@case("E", "E3", "「这条我不做」必须给原因，且只静默 7 天")
def _(ctx: Ctx) -> str:
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    it = next(i for i in t["items"] if not i["pool"])

    code, data = ctx.cli.call("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                                            "mode": "refused", "today": AS_OF})
    need(code == 400, f"不给原因应当被挡住，实际 {code}")
    need("必须给原因" in data.get("error", ""), f"拒绝理由没说清：{data}")

    r = ctx.cli.ok("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                                 "mode": "refused", "reason": "not_mine", "today": AS_OF})
    need(r["mode"] == "refused", f"mode 应为 refused，实际 {r['mode']}")
    need(r["silent_until"] == "2026-10-31",
         f"静默到哪天算错了：{r['silent_until']}（基准日 + 7 天）")
    need(r["reason_cn"], "回执没给出人话的原因，界面会显示 not_mine")
    need(not any(i["aid"] == it["aid"] for i in r["todo"]["items"]), "说「不做」之后还在列表里")

    # 7 天后必须自己回来：客户情况会变，永久压住等于系统装瞎。
    later = ctx.cli.ok(f"/api/todo?today=2026-11-02")
    need(any(i["aid"] == it["aid"] for i in later["items"]),
         "过了静默期还没回来——那就不是「暂时不做」，是永久删除")

    ctx.cli.ok("/api/done", {"customer_id": it["cid"], "aid": it["aid"],
                             "today": AS_OF, "undo": True})
    back = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    need(any(i["aid"] == it["aid"] for i in back["items"]), "撤销没把它放回来")
    return "400 挡住无原因 · 静默到 10-31 · 11-02 自己回来 · 可撤销"


# ---------------------------------------------------------------------------
# F. 记一笔
# ---------------------------------------------------------------------------
#
# 这一组只验一件事：**没点「确认入库」，库里什么都没变**。
#
# 原来只有一条路——粘上去就入库——于是用户唯一的纠错方式是「入库之后逐条撤回」。
# 撤回改的是事实状态，会在卡上留下一串「已撤回」的痕迹；撤回一半就退出，
# 还会留下一份半成品记录。草稿态把校验挪到入库之前，代价是多了一个
# 必须每条都验一遍的契约：草稿不许写库、勾选才落库、落过的草稿不能落第二次。

def _draft_sample(ctx: Ctx, sid: str = "s1", **extra) -> dict:
    """用样例文本开一份草稿。模型那条路要真跑，--skip-model 时跳过。"""
    if ctx.skip_model:
        raise AssertionError("跳过了模型调用（--skip-model）")
    s = next(x for x in ctx.cli.ok("/api/samples")["samples"] if x["id"] == sid)
    body = {"text": s["text"], "doc_date": s["doc_date"],
            "source_type": s["source_type"], "customer_name": s["customer_name"]}
    body.update(extra)
    return ctx.cli.ok("/api/draft", body)


@case("F", "F1", "草稿态：抽得出事实，但库里一条都没动")
def _(ctx: Ctx) -> str:
    ctx.reset()
    n_before = len(ctx.customers())
    r = _draft_sample(ctx)

    need(r["n_facts"] > 0, "草稿一条事实都没抽出来，后面两条用例没法验")
    need(r["draft_id"].startswith("draft"), f"草稿号不是 draft 前缀：{r['draft_id']}")
    # 草稿用 d 前缀的临时记录号：被丢弃时不该在 u* 序号上留窟窿。
    need(r["doc"]["id"].startswith("d"),
         f"草稿占用了正式记录序号：{r['doc']['id']}（丢弃后 u* 会留窟窿）")
    need(r["facts"] and r["facts"][0]["span"], "草稿行没带原文片段，勾选时无从对照")

    # 硬契约：客户数、客户列表都没变
    need(len(ctx.customers()) == n_before,
         f"草稿建出了客户：{n_before} → {len(ctx.customers())}")
    need(not any(c["name"] == r["customer_name"] for c in ctx.customers()),
         f"草稿客户「{r['customer_name']}」已经出现在列表里了")
    return (f"{r['draft_id']} 抽出 {r['n_facts']} 条 · 客户数仍 {n_before} · "
            f"记录号 {r['doc']['id']} 未占用 u*")


@case("F", "F2", "确认入库：只落勾选的那几条，丢掉的条数要说出来")
def _(ctx: Ctx) -> str:
    r = _draft_sample(ctx)
    keep = [f["fact_id"] for f in r["facts"][:2]]
    c = ctx.cli.ok("/api/commit", {"draft_id": r["draft_id"], "keep": keep})

    need(c["n_facts"] == 2, f"勾了 2 条，落了 {c['n_facts']} 条")
    need(c["n_discarded"] == r["n_facts"] - 2,
         f"丢掉的条数不对：{c['n_discarded']}，应是 {r['n_facts'] - 2}")
    need(c["doc"]["id"].startswith("u"),
         f"入库用的还是草稿记录号：{c['doc']['id']}——「跳到原始记录」会点开空白")
    need(all(f["source_id"] == c["doc"]["id"] for f in c["fact_data"]),
         "事实没指回真实记录：点开出处是一片空白，而且不报错")

    # 客户是到这一步才创建的
    need(any(x["id"] == c["customer"]["id"] for x in ctx.cli.ok("/api/customers")["customers"]),
         "入库后客户没出现在列表里")
    return (f"{r['draft_id']} → {c['doc']['id']} · 落 2 条 · 丢 {c['n_discarded']} 条 · "
            f"客户「{c['customer']['name']}」{c['customer']['id']}")


@case("F", "F3", "同一份草稿不能落两次：一段原文不该有两条记录")
def _(ctx: Ctx) -> str:
    r = _draft_sample(ctx)
    first = ctx.cli.ok("/api/commit", {"draft_id": r["draft_id"]})
    need(first["n_facts"] == r["n_facts"], "不传 keep 应当全收")

    code, data = ctx.cli.call("/api/commit", {"draft_id": r["draft_id"]})
    need(code == 404, f"同一份草稿二次入库应当 404，实际 {code}")
    need("不在了" in data.get("error", ""), f"拒绝理由没说清：{data}")
    return f"{r['draft_id']} 全收 {first['n_facts']} 条 · 二次入库 404"


# ---------------------------------------------------------------------------
# G. 四入口与深链
# ---------------------------------------------------------------------------
#
# 这一组验「两次点击能不能真的拿到东西」。原来只有一条动线——先选客户、
# 再看产出物——而「客户刚挂电话说明天要方案」这个场景里，用户心里有动作、
# 没有客户名。四个入口就是这个开场。
#
# 最容易出的错是**落点不存在**：`focus` 写了一个块 id，但那份简报里没有这一块。
# 它不报错，抽屉照样打开，只是安静地停在顶部——所以这里不光要看 id 长什么样，
# 还要真的把那份简报取回来，核对这一块在不在。

@case("G", "G1", "四个入口：各给最多三个客户，同一个客户不占两个名额")
def _(ctx: Ctx) -> str:
    # 先清场。F 组入库过一份草稿，客户身上多了一条记录和几条事实，
    # 带着它统计出来的「96 条待办」会变成 101——而这个数字会被人抄进文档。
    # 冒烟打印的每个数都必须和干净首屏一致。
    ctx.reset()
    d = ctx.cli.ok(f"/api/intent?today={AS_OF}")
    need(len(d["intents"]) == 4, f"应该有四个入口，实际 {len(d['intents'])}")
    want = {"contact", "visit", "proposal", "handover"}
    got = {g["id"] for g in d["intents"]}
    need(got == want, f"入口 id 不对：{got}")

    detail = []
    for g in d["intents"]:
        need(g["picks"], f"「{g['label']}」一个客户都没有")
        need(len(g["picks"]) <= d["n"], f"「{g['label']}」给了 {len(g['picks'])} 个")
        need(len({p["cid"] for p in g["picks"]}) == len(g["picks"]),
             f"「{g['label']}」里同一个客户出现了两次——等于只给了一个客户")
        # 严重度优先：P0 不该排在 P1 后面
        ps = [p["p"] for p in g["picks"]]
        need(ps == sorted(ps), f"「{g['label']}」没按严重度排：{ps}")
        detail.append(f"{g['label']} {len(g['picks'])}个")
    return " · ".join(detail)


@case("G", "G2", "深链落点是真块：打开那份简报，能找到这一块")
def _(ctx: Ctx) -> str:
    d = ctx.cli.ok(f"/api/intent?today={AS_OF}")
    checked, seen = 0, set()
    for g in d["intents"]:
        for p in g["picks"]:
            k = (p["cid"], p["brief_kind"], p["focus"])
            if k in seen:
                continue
            seen.add(k)
            b = ctx.cli.ok("/api/brief", {"customer_id": p["cid"], "today": AS_OF,
                                          "kind": p["brief_kind"]})
            ids = [blk.get("id") for blk in b["blocks"]]
            need(all(ids), f"{p['brief_kind']} 简报里还有块没带 id：{ids}")
            need(p["focus"] in ids,
                 f"{p['cname']} 的「{g['label']}」落到 {p['brief_kind']}/{p['focus']}，"
                 f"但那份简报只有 {ids}——抽屉会停在顶部，而且不报错")
            checked += 1

    # 待办行上的「简报」按钮也靠同一套落点，逐条核对一遍
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    for it in t["items"]:
        need(it.get("brief_kind") and it.get("focus"),
             f"待办「{it['title'][:16]}」没带落点，行上的「简报」按钮点下去没反应")
        need(it["focus"] in bf.BLOCK_IDS[it["brief_kind"]],
             f"待办「{it['title'][:16]}」落到 {it['brief_kind']}/{it['focus']}，这一块不存在")
    return f"{checked} 个落点真实存在 · {len(t['items'])} 条待办都带了落点"


@case("G", "G3", "存量客户只报数、不占名额；历史欠账不进动作入口")
def _(ctx: Ctx) -> str:
    d = ctx.cli.ok(f"/api/intent?today={AS_OF}")
    pool = {c["id"] for c in ctx.customers() if c.get("pool")}
    for g in d["intents"]:
        for p in g["picks"]:
            need(p["cid"] not in pool,
                 f"「{g['label']}」把存量客户「{p['cname']}」排进前 {d['n']} 了——"
                 "它们本来就写着「不急着今天动」")

    # 历史欠账那条（泰盛重工，拖了 116 天）不该出现在任何动作入口里
    t = ctx.cli.ok(f"/api/todo?today={AS_OF}")
    hist = {(i["cid"], i["kind"]) for i in t["items"] if i["when"] == "historic"}
    need(hist, "没有任何历史欠账，这条断言就没意义了")
    for g in d["intents"]:
        for p in g["picks"]:
            need((p["cid"], p["kind"]) not in hist,
                 f"「{g['label']}」把历史欠账「{p['cname']}」当成今天该做的动作了")
    pool_n = sum(g["n_pool_candidates"] for g in d["intents"])
    need(pool_n > 0, "存量客户那边一个候选都没报出来——不说，用户会以为它们不存在")
    return f"重点客户占名额 · 存量 {pool_n} 个候选只报数 · 历史欠账未混入"


# ---------------------------------------------------------------------------
# H. 最小档案
# ---------------------------------------------------------------------------

@case("H", "H1", "没有主数据档案时，报记录里算得出的，不冒充主数据")
def _(ctx: Ctx) -> str:
    ctx.reset()
    c = next(x for x in ctx.customers() if x["pool"] and not x["profile"])
    labels = [m["label"] for m in c["strip"]]
    need(labels, "存量客户的档案条一格都没有——又回到空白行了")
    need("合作" not in labels,
         f"没有主数据却画出「合作」这一格：{labels}。最早一条记录不等于合作起始")
    need(all(m["src"] == "derived" for m in c["strip"]),
         "推算出来的格子必须标 src=derived，否则界面分不清是不是主数据")
    need("首次记录" in labels, f"应当报「首次记录」这个真实统计，实际：{labels}")
    return f"{c['name']}：{'、'.join(labels)}"


@case("H", "H2", "补四个字段之后，档案条换成主数据，推算的不再混进来")
def _(ctx: Ctx) -> str:
    ctx.reset()
    c = next(x for x in ctx.customers() if x["pool"] and not x["profile"])
    r = ctx.cli.ok("/api/profile", {"customer_id": c["id"], "profile": {
        "scale": "3200 人", "open_amount": "180万",
        "contract_expires": "2027-03-31", "relationship": "warm"}})
    need(r["n_changed"] == 4, f"四个字段应当都进变更日志，实际改了 {r['n_changed']} 项")

    c2 = next(x for x in ctx.cli.ok("/api/customers")["customers"] if x["id"] == c["id"])
    labels = [m["label"] for m in c2["strip"]]
    need(all(m["src"] == "profile" for m in c2["strip"]),
         f"有主数据了还混着推算的格子：{[(m['label'], m['src']) for m in c2['strip']]}")
    need("180 万" in [m["value"] for m in c2["strip"]], f"金额没进档案条：{c2['strip']}")
    need("首次记录" not in labels, f"主数据已经答了这件事，不该再补推算格：{labels}")
    return f"{'、'.join(labels)} · 四项改动都留痕"


@case("H", "H3", "日期写错要 400，写对了要先归一")
def _(ctx: Ctx) -> str:
    ctx.reset()
    c = next(x for x in ctx.customers() if x["pool"])
    code, data = ctx.cli.call("/api/profile", {"customer_id": c["id"],
                                               "profile": {"contract_expires": "下个月"}})
    need(code == 400, f"认不出的日期应当 400，实际 {code}：{data}")
    need("2027-03-31" in (data.get("error") or ""),
         f"报错要给出正确写法，不能只说「格式错误」：{data.get('error')}")

    # 换个写法再来一次：这台机器上没人会严格按 ISO 打
    ctx.cli.ok("/api/profile", {"customer_id": c["id"],
                                "profile": {"contract_expires": "2027/3/31"}})
    c2 = next(x for x in ctx.cli.ok("/api/customers")["customers"] if x["id"] == c["id"])
    got = {m["label"]: m for m in c2["strip"]}
    need("合同" in got, f"归一后的到期日应当画出「合同」这一格：{c2['strip']}")
    need("到期日认不出" not in got["合同"]["value"],
         f"2027/3/31 是能认的写法，不该落到「认不出」：{got['合同']}")
    need(got["合同"]["hint"] == "2027-03-31",
         f"存下来的应当是归一后的 ISO 日期，实际 {got['合同']['hint']}")
    return f"错日期 400 并给出写法 · 2027/3/31 归一为 2027-03-31（{got['合同']['value']}）"


# ---------------------------------------------------------------------------
# I. 只知职务的干系人（P2-6）
# ---------------------------------------------------------------------------

def _with_stakeholder(ctx: Ctx, cid: str, payload: dict) -> dict:
    """把这个客户的关键人换成指定的这一条，再拿一次决策链。

    整组走 `/api/analyze` 的 fact_data 覆盖，不改语料、不动演示数据，
    结束由 `ctx.reset()` 还原。

    探针行以该客户已有的那条关键人为模板复制，只改要验的字段——
    `fact_data` 用的是 Fact 的字段名（`fact_id` / `source_span` / `stated_at`），
    不是视图那一套（`key` / `span` / `conf`）；手搓一份新字典很容易漏掉
    `customer_id`，那条事实就会挂不到任何客户名下、静默消失。
    """
    base = ctx.base_facts(cid) or []
    rows = [r for r in base if r.get("fact_type") != "stakeholder"]
    tpl = next((r for r in base if r.get("fact_type") == "stakeholder"), base[0])
    probe = dict(tpl)
    probe.update({
        "fact_id": "f_probe", "fact_type": "stakeholder",
        "statement": "这份合同最后由董事长签字。",
        "payload": payload, "source_span": "最后得董事长签字",
        "span_start": None, "span_end": None, "source_id": "d_probe",
        "status": "active", "supersedes": None, "superseded_by": None,
    })
    rows.append(probe)
    ctx.view(cid, fact_data=rows)
    fresh = ctx.cli.ok("/api/customers")["customers"]
    return next(c for c in fresh if c["id"] == cid)["chain"]


@case("I", "I1", "只知职务的干系人进得了决策链，不被当成「未具名」")
def _(ctx: Ctx) -> str:
    ctx.reset()
    cid = next(x["id"] for x in ctx.customers() if x["pool"])
    chain = _with_stakeholder(ctx, cid, {
        "identified_as": "title", "title": "董事长",
        "role_in_deal": "decision_maker", "attitude": "neutral"})
    node = next((n for n in chain["nodes"] if n["title"] == "董事长"), None)
    need(node is not None, f"只知职务的干系人没能进决策链——P2-6 等于没做：{chain['nodes']}")
    need(node["title_only"], f"节点没标成「只知职务」：{node}")
    need("仅职务" in node["label"], f"标签要说清这是位置不是人：{node['label']}")
    need("未具名" not in node["label"],
         f"「只知道职务」和「没问到是谁」是两件事，不能都写「未具名」：{node['label']}")
    return node["label"]


@case("I", "I2", "结论不许说成「签字的人已经接触到了」")
def _(ctx: Ctx) -> str:
    ctx.reset()
    cid = next(x["id"] for x in ctx.customers() if x["pool"])
    chain = _with_stakeholder(ctx, cid, {
        "identified_as": "title", "title": "董事长",
        "role_in_deal": "decision_maker", "attitude": "neutral"})
    note = chain["note"]
    need("还没接触到具体的人" in note, f"结论没提这件事还没接触到人：{note}")
    need("位置的确认" in note, f"要把「位置」和「关系」分开说：{note}")
    need("签字的人已经接触到了" not in note,
         f"只记到职务却说签字的人已经接触到了，是假结论：{note}")
    return note[:46] + "…"


@case("I", "I3", "换成人名之后，结论回到原来的说法")
def _(ctx: Ctx) -> str:
    ctx.reset()
    cid = next(x["id"] for x in ctx.customers() if x["pool"])
    chain = _with_stakeholder(ctx, cid, {
        "name": "李国强", "title": "董事长",
        "role_in_deal": "decision_maker", "attitude": "neutral"})
    node = next(n for n in chain["nodes"] if n["title"] == "董事长")
    need(not node["title_only"], f"有姓名却被标成只知职务：{node}")
    need(node["label"] == "李国强（董事长）", f"标签变了：{node['label']}")
    need("只记到职务" not in chain["note"],
         f"有姓名还多说一句「只记到职务」，等于噪声：{chain['note']}")
    return node["label"]


# ---------------------------------------------------------------------------
# K 规则判出的风险信号（P2-2）
#
# 这一组守两条线，都不看「接口返回了 200」，只看系统说的和事实是否一致：
#
#   1. **一条变化只出现一次。** 模型从这段文本里已经读到了变化，规则就
#      不该再补一条同样的。两条并排躺着，用户会以为是两件事。
#   2. **空白要说清是哪一种空。**「本系统不产出」和「无有效记录」对用户
#      是两件事：前者是系统的边界（多记一篇就好），后者是我们的缺失
#      （该去打听）。说反了，一条「这个客户没有风险」的假结论就成立了。
#
# 为什么这一组不跳过 --skip-model：走的是回放缓存，不联网也跑得动。

def _extract_sample(ctx: Ctx, sid: str, **extra) -> dict:
    """直接入库一段样例。走 /api/extract，不需要草稿那两步。"""
    s = next(x for x in ctx.cli.ok("/api/samples")["samples"] if x["id"] == sid)
    body = {"text": s["text"], "doc_date": s["doc_date"],
            "source_type": s["source_type"], "customer_name": s["customer_name"]}
    body.update(extra)
    return ctx.cli.ok("/api/extract", body)


def _gap_of(ctx: Ctx, cid: str, prefix: str) -> str:
    cust = next(c for c in ctx.cli.ok("/api/customers")["customers"] if c["id"] == cid)
    return next((g for g in cust["gaps"] if g.startswith(prefix)), "")


@case("K", "K1", "同一段里的三处变化：读得出来，且同一种变化只记一条")
def _(ctx: Ctx) -> str:
    ctx.reset()
    r = _extract_sample(ctx, "s4")
    risks = [f for f in r["fact_data"] if f["fact_type"] == "risk_signal"]
    kinds = [(f.get("payload") or {}).get("signal_type") for f in risks]

    # 三处变化是这段文本明写的：人走了有人接手 / 回得慢了 / 需求改了。
    # 少读出一处，说明判据漏了真实表达。
    for want in ("contact_change", "response_delay", "requirement_drift"):
        need(want in kinds, f"这段里明写了这处变化，没被读出来：{want} 不在 {kinds}")

    # 这才是这一组真正守的东西：同一种变化只出现一次。
    # 模型读到的和规则读到的如果各记一条，卡上就是两条，
    # 用户会以为是两件事——而且是「同一件事被说了两遍」这种最伤信任的两件事。
    need(len(set(kinds)) == len(kinds),
         f"同一种变化记了两条，用户会以为是两件事：{kinds}")

    # 不管是模型读的还是规则读的，都要能被原文核对。
    # 规则那条的证据是「原文里就这半句」，所以它必须原样出现在文本里；
    # 模型那条的证据是它的 source_span，校验层已经卡过（E004），
    # 这里顺手再核一次，因为这一段最怕的就是「系统说了句原文里没有的话」。
    text = r["doc"]["text"]
    for f in risks:
        by = (f.get("payload") or {}).get("detected_by")
        need(by in ("rule", "model"), f"detected_by 非法：{by}")
        need(f["source_span"] and f["source_span"] in text,
             f"这条信号的原文定位不到：{f['source_span']!r}")
    return " · ".join(f"{k}" for k in kinds)


@case("K", "K2", "只有一篇记录：空白项说「本系统不产出」，不说「无有效记录」")
def _(ctx: Ctx) -> str:
    ctx.reset()
    cid = _extract_sample(ctx, "s1")["customer"]["id"]
    gap = _gap_of(ctx, cid, "风险信号")
    need("本系统不产出" in gap,
         f"一篇记录时说「无有效记录」，会被读成「这单没有风险」——一个假结论：{gap}")
    need("两篇" in gap, f"要说清为什么产不出，否则它只是一句免责声明：{gap}")
    return gap


@case("K", "K3", "第二篇进来之后，这个空白回到「无有效记录」")
def _(ctx: Ctx) -> str:
    ctx.reset()
    cid = _extract_sample(ctx, "s1")["customer"]["id"]
    _extract_sample(ctx, "s1", customer_id=cid)
    gap = _gap_of(ctx, cid, "风险信号")
    need("本系统不产出" not in gap, f"两篇之后结构上就产得出了，不该再这么说：{gap}")
    need("无有效记录" in gap, f"这两篇里确实没有变化，要说「无有效记录」：{gap}")
    return gap


# ---------------------------------------------------------------------------
# P. 落盘与归属（P2-3）
#
#     这一组的断言方式和别的组不一样：**不信接口，直接开一个 sqlite 连接去数。**
#     理由很直接——「接口返回 200」证明不了数据落了盘，接口读的是内存。
#     只有从进程外面看到那些行，才证明它真的写下去了。
#
#     这也是为什么冒烟必须对「真实起法的服务」跑：`--db :memory:` 一样能
#     让所有接口返回 200，但那正是这一组要否掉的东西。
# ---------------------------------------------------------------------------

def _db_path(ctx: Ctx) -> str:
    st = ctx.cli.ok("/api/health").get("store") or {}
    need(st.get("mode") == "sqlite",
         f"服务没在落盘模式（store.mode={st.get('mode')!r}）。"
         "起服务别带 --db :memory:，否则这一组验的就不是真实形态")
    need(st.get("on_disk"), f"health 说在 sqlite 模式，但库文件不存在：{st.get('path')!r}")
    return st["path"]


def _count(path: str, sql: str, args: tuple = ()) -> int:
    """从服务外面直接查库。只读，不写——冒烟不该顺手改别人的数据。"""
    cx = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(cx.execute(sql, args).fetchone()[0])
    finally:
        cx.close()


def _text(path: str, sql: str, args: tuple = ()) -> str:
    """同上，取文本。失败信息里带上真值，下一次不用再猜。"""
    cx = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = cx.execute(sql, args).fetchone()
        return "" if row is None else str(row[0])
    finally:
        cx.close()


@case("P", "P1", "首屏就说清数据在哪：落盘还是只在内存")
def _(ctx: Ctx) -> str:
    h = ctx.cli.ok("/api/health")
    st = h.get("store") or {}
    for key in ("mode", "path", "schema_version", "on_disk", "size", "seeded"):
        need(key in st, f"health.store 少了 {key}：「关掉页面还在不在」没有别的答案来源")
    need(bool(h.get("user")), "health 没报当前身份，归属就无从验起")
    need(isinstance(h.get("allow_reset"), bool), "health 没报 reset 开关")
    path = _db_path(ctx)
    need(st["schema_version"] >= 1, f"结构版本不对劲：{st['schema_version']}")
    need(os.path.getsize(path) > 0, f"库文件是空的：{path}")
    return f"{path}（{st['size'] // 1024} KB）"


@case("P", "P2", "库里的记录条数 = 接口报出的记录条数")
def _(ctx: Ctx) -> str:
    ctx.reset()
    path = _db_path(ctx)
    in_db = _count(path, "SELECT COUNT(*) FROM record")

    # 接口只给基准日之前的记录，基准日之后的单独报一个 n_pending_records。
    # 两段加起来才是这个客户的全部记录——只比 records 会漏掉未来的那部分。
    total = sum(len(c["records"]) + c.get("n_pending_records", 0)
                for c in ctx.cli.ok("/api/customers")["customers"])
    need(in_db == total, f"库里 {in_db} 条，接口说 {total} 条，差 {total - in_db}")
    need(in_db > 0, "一条记录都没有，这条断言等于没跑")
    return f"{in_db} 条记录在库里"


@case("P", "P3", "改一次档案：变更日志真的进了库，reset 之后又退回去")
def _(ctx: Ctx) -> str:
    ctx.reset()
    path = _db_path(ctx)
    name = "东岭铜业"
    cust = ctx.by_name(name)
    before_log = _count(path, "SELECT COUNT(*) FROM change_log")
    old = (cust.get("profile") or {}).get("scale") or ""

    new_scale = "5000 人（现场改的）"
    # 改之前库里不该有这串：否则下面「改完出现了」可能就是本来就有的。
    need(_count(path, "SELECT instr(profile, ?) FROM customer WHERE id=?",
                (new_scale, cust["id"])) == 0, "库里本来就有这个值，这条断言等于没跑")

    r = ctx.cli.ok("/api/profile", {"customer_id": cust["id"],
                                     "profile": {"scale": new_scale}, "who": "冒烟"})
    need(r["n_changed"] == 1, f"改了一个字段，回执说改了 {r['n_changed']} 个")

    # 关键：不信回执，也不信下一次 /api/customers（它读内存），去库里看。
    after_log = _count(path, "SELECT COUNT(*) FROM change_log")
    need(after_log == before_log + 1,
         f"回执说改了，库里 change_log 只从 {before_log} 变成 {after_log}")
    # `instr` 返回的是**位置**不是布尔：命中在第 12 个字符就返回 12。
    # 这里比 >0，别写成 ==1 —— 写成 ==1 会在「值确实在库里」的情况下报错，
    # 而报出来的话是「没出现在库里」，正好把方向指反。
    need(_count(path, "SELECT instr(profile, ?) FROM customer WHERE id=?",
                (new_scale, cust["id"])) > 0,
         "新的 scale 没出现在库里的 customer.profile 上："
         + _text(path, "SELECT profile FROM customer WHERE id=?", (cust["id"],))[:160])

    # reset 之后必须回到原样：落盘之后「恢复初始状态」要连库一起恢复，
    # 否则下一个打开页面的人看到的仍是上一轮改过的数据。
    ctx.reset()
    back = _count(path, "SELECT instr(profile, ?) FROM customer WHERE id=?",
                  (new_scale, cust["id"]))
    need(back == 0, "reset 之后库里还留着改过的值")
    need(_count(path, "SELECT COUNT(*) FROM change_log") == 0, "reset 之后变更日志没清")
    return f"change_log {before_log}→{after_log}→0，scale 回到「{old}」"

# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="演示前自检")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--skip-model", action="store_true", help="跳过调模型的用例（约省 12 秒）")
    args = ap.parse_args(argv)

    cli = Client(args.base)
    ctx = Ctx(cli, args.skip_model)

    health = cli.ok("/api/health")
    mode = "离线回放" if health["offline"] else f"真实模型 {health['model']}"
    print(f"\n客户认知助手 · 演示自检")
    print(f"服务 {args.base} · {mode} · 基准日 {health['as_of']} · 缓存 {health['cache_size']} 条")
    if args.skip_model:
        print("（已跳过调模型的用例）")
    print("=" * 74)

    ctx.reset()
    groups = {"A": "正常路径", "B": "异常路径", "C": "边界情况",
              "D": "产出物导出", "E": "待办治理", "F": "记一笔",
              "G": "四入口与深链", "H": "最小档案", "I": "只知职务的干系人",
              "J": "回写闭环", "K": "规则判出的风险信号",
              "P": "落盘与归属"}
    passed = failed = skipped = 0
    failures: list[str] = []
    last = None

    try:
        for group, cid, title, fn in CASES:
            if group != last:
                print(f"\n{group}. {groups[group]}")
                last = group
            try:
                detail = fn(ctx)
                passed += 1
                print(f"  ✅ {cid} {title}")
                if detail:
                    print(f"        {detail}")
            except AssertionError as exc:
                if "跳过了模型调用" in str(exc):
                    skipped += 1
                    print(f"  ⏭️  {cid} {title}（跳过）")
                else:
                    failed += 1
                    failures.append(f"{cid} {title}：{exc}")
                    print(f"  ❌ {cid} {title}")
                    print(f"        {exc}")
    finally:
        # 一律还原：这个脚本会撤回事实、新建客户，不清场会把演示数据改坏
        ctx.reset()

    print("\n" + "=" * 74)
    print(f"通过 {passed} · 失败 {failed} · 跳过 {skipped}")
    if failures:
        print("\n失败项：")
        for f in failures:
            print("  - " + f)
    print("\n界面层（拖拽、撤回按钮、判据展开、回执措辞）不在本脚本覆盖范围，"
          "接着跑 python3 demo/ui_check.py（需 node）。")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# J. 回写闭环：修正要影响下一次抽取（P2-5）
# ---------------------------------------------------------------------------

def _sample(ctx: Ctx, name: str) -> dict:
    if ctx.skip_model:
        raise AssertionError("跳过了模型调用（--skip-model）")
    s = next(x for x in ctx.cli.ok("/api/samples")["samples"] if x["id"] == "s1")
    return ctx.cli.ok("/api/extract", {
        "text": s["text"], "doc_date": s["doc_date"],
        "source_type": s["source_type"], "customer_name": name})


@case("J", "J1", "点一次「这条不对」，引擎就把它记下来了")
def _(ctx: Ctx) -> str:
    ctx.reset()
    first = _sample(ctx, "回写闭环甲")
    target = next(f for f in first["fact_data"] if f["fact_type"] == "concern")
    rows = [dict(f, status="retracted") if f["fact_id"] == target["fact_id"] else f
            for f in first["fact_data"]]
    r = ctx.cli.ok("/api/analyze", {"customer_id": first["customer"]["id"],
                                    "fact_data": rows, "today": AS_OF})
    need(r["n_corrections"] >= 1, f"撤回没有被记成修正：n_corrections={r['n_corrections']}")

    fb = ctx.cli.ok("/api/feedback")
    need(fb["n_corrections"] >= 1, "修正记录是空的——重启就忘，等于没记")
    need(fb["by_reason"].get("not_a_fact", 0) >= 1,
         f"原因归类不对：{fb['by_reason']}")
    need(fb["n_blacklist"] >= 1, "没有生成黑名单，下次还会抽错同一条")
    return f"{fb['n_corrections']} 条修正 · 黑名单 {fb['n_blacklist']} 条"


@case("J", "J2", "同一段文本再抽一次，那条不再出现——而且说清为什么")
def _(ctx: Ctx) -> str:
    ctx.reset()
    first = _sample(ctx, "回写闭环乙")
    target = next(f for f in first["fact_data"] if f["fact_type"] == "concern")
    rows = [dict(f, status="retracted") if f["fact_id"] == target["fact_id"] else f
            for f in first["fact_data"]]
    ctx.cli.ok("/api/analyze", {"customer_id": first["customer"]["id"],
                                "fact_data": rows, "today": AS_OF})

    second = _sample(ctx, "回写闭环丙")
    need(second["n_blocked"] >= 1, f"黑名单没生效：n_blocked={second['n_blocked']}")
    back = [f for f in second["fact_data"]
            if f["fact_type"] == "concern" and f["source_span"] == target["source_span"]]
    need(not back, f"被你判过「不是事实」的那条又抽出来了：{back}")
    need("不是事实" in (second["blocked_note"] or ""),
         f"拦下来了但没说为什么——那就是黑箱：{second['blocked_note']}")
    need(second["n_facts"] >= 1, "整篇被拦空了，说明黑名单太宽（只该拦那一条）")
    return f"{second['n_facts']} 条 · {second['blocked_note']}"


@case("J", "J3", "reset 之后修正清空——不然下一次抽取会莫名少几条")
def _(ctx: Ctx) -> str:
    ctx.reset()
    fb = ctx.cli.ok("/api/feedback")
    need(fb["n_corrections"] == 0, f"reset 之后还留着 {fb['n_corrections']} 条修正")
    need(fb["n_blacklist"] == 0, f"reset 之后黑名单还有 {fb['n_blacklist']} 条")
    return "修正 0 · 黑名单 0"


if __name__ == "__main__":
    raise SystemExit(main())

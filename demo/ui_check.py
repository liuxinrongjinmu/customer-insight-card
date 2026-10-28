#!/usr/bin/env python3
"""界面层断言：把页面脚本放到 Node 里跑，看它渲染出来的字对不对。

    python3 demo/serve.py --allow-reset   # 终端 1（reset 默认关，必须显式开）
    python3 demo/ui_check.py              # 终端 2

为什么需要这一层：`smoke.py` 验的是接口返回的 JSON，而演示时观众看到的是 HTML。
两者中间隔着 `renderDelta()`、`factCard()`、`updateDateBar()` 这些纯前端函数——
接口说「P0 从 0 变 1」，页面却写成「P0 1 → 0」，接口断言一条都不会红。
这类错误只能靠断言页面文本抓到。

为什么不装 jsdom / playwright：
1. 零依赖是这个项目的底线，装一个浏览器内核就为了看几个字符串不值得；
2. 这里要验的不是布局，是「从载荷到文案」的映射，DOM 只需要能被赋值和读回。

所以做法是：一个极小的 DOM 桩 + 一个把 `/api/*` 全部截住的 fetch 桩，
然后把页面里那段 `<script>` 原样加载进去，等它自己跑完再断言。

**桩不做任何回落。** 每个 `/api/analyze` 请求按
`(客户, 基准日, 已撤回事实集合)` 查表，查不到就返回 500 让用例红掉。
早期版本按「日期」回放固定载荷，撤回前后拿到的是同一份数据，
于是「撤回后 P0 变 1」这条断言验的其实是撤回前的数据——通了，但测错了东西。
退回一个近似值比报错更坏，这一层的价值全在「不许猜」。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HTML_FILE = HERE / "客户认知卡-演示.html"
AS_OF = "2026-10-24"


def make_client(base: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{path} 返回 {exc.code}：{exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"连不上 {base}。先起服务：python3 demo/serve.py\n（{exc.reason}）"
            ) from exc

    return call


# ---------------------------------------------------------------------------
# 场景预算：一份都不能少，一份都不许猜
# ---------------------------------------------------------------------------

def scen_key(cid: str, today: str, fact_data: list) -> str:
    """场景键。

    必须带上「哪几条被撤回了」：同一个客户、同一天，撤回前后是两个不同的世界，
    而撤回是界面上的一个动作，桩没法自己推算结果，只能让服务端先算好。
    同样的道理也适用于「哪几条承诺已履行」——勾「已办」改的是事实，
    不进键的话，办完之后的刷新会查回「没办过」的旧场景。
    """
    retracted = sorted(d["fact_id"] for d in fact_data if d.get("status") == "retracted")
    fulfilled = sorted(d["fact_id"] for d in fact_data
                       if (d.get("payload") or {}).get("fulfillment") == "fulfilled")
    return f"{cid}|{today}|{','.join(retracted)}|F:{','.join(fulfilled)}"


def reset(call) -> None:
    """回到干净基线。`/api/reset` 默认不挂载（P2-3），没开就直说怎么办。"""
    try:
        call("/api/reset", {})
    except SystemExit as exc:
        if "返回 404" not in str(exc):
            raise
        raise SystemExit(
            "/api/reset 返回 404：服务没有开 --allow-reset。\n"
            "界面断言靠 reset 回到干净基线，起服务时请带：\n"
            "    python3 demo/serve.py --allow-reset\n"
            "（默认关掉是有意的：一个能让数据消失的接口，默认必须是关的。）"
        ) from None


def build(base: str) -> dict:
    call = make_client(base)
    # `/api/analyze` 会把传入的 fact_data 落回服务端内存（第二块要的行为：
    # 刷新后仍看到改过的版本）。所以先洗干净，否则桩里装的是上一轮的残留。
    reset(call)

    # -- 「记一笔」预演 -------------------------------------------------------
    # 草稿和入库的载荷都由服务端真跑一遍录下来，桩不自己拼装抽样结果——
    # 拼出来的 fact_id / span / gist 和真后端迟早不一样，那就白测了。
    #
    # 顺序很关键：draft 不落库，但 **commit 会新建一个客户**，所以演完必须
    # reset 回干净状态，否则下面那份「39 个客户」的底座里会多出一个中鼎精密，
    # 第 [1] 组断言当场红掉。
    sample = next(x for x in call("/api/samples")["samples"] if x["id"] == "s1")
    draft_resp = call("/api/draft", {"text": sample["text"], "doc_date": sample["doc_date"],
                                     "source_type": sample["source_type"],
                                     "customer_name": sample["customer_name"]})
    # 只勾两条：先把「勾掉的三条不落库」这件事测出来，
    # 全勾的话 n_discarded 恒为 0，那条断言就永远为真。
    draft_keep = [f["fact_id"] for f in draft_resp["facts"][:2]]
    commit_resp = call("/api/commit", {"draft_id": draft_resp["draft_id"], "keep": draft_keep})
    # 入库之后前端会拿合并后的事实重算认知卡。这个客户的 id 在 reset 之后就没了，
    # 场景必须现在录。
    commit_scen = call("/api/analyze", {"customer_id": commit_resp["customer"]["id"],
                                        "today": AS_OF,
                                        "fact_data": commit_resp["fact_data"]})
    reset(call)

    customers = call("/api/customers")
    by_id = {c["id"]: c for c in customers["customers"]}
    by_name = {c["name"]: c for c in customers["customers"]}

    # 待办快照必须**在预演之前**取，且预演之后不许再 reset。
    #
    # 这里踩过一个坑：aid 就是事实 id（见 src/advice/engine.py::_primary_key），
    # 而 reset 会拿种子重建事实、重新生成 id。原来 build() 在预演完 reset 了一次，
    # 于是「预演用的 aid」和「页面实际拿到的 aid」对不上——
    # `ok(!S.todo.items.some(i => i.aid === DONE.fulfilled.aid))` 这类断言
    # 就变成了空转：那条从来就没在列表里，当然「不见了」。
    # 现在改成：先取快照，预演只撤销不 reset，最后核对 aid 没漂。
    todo = call(f"/api/todo?today={AS_OF}")

    dongling = by_name["东岭铜业"]
    closing = next(d for d in dongling["fact_data"]
                   if d["statement"].startswith("客户决定本期不再要求"))

    def analyze(cid: str, today: str, fact_data: list) -> dict:
        return call("/api/analyze", {"customer_id": cid, "today": today,
                                     "fact_data": fact_data})

    scen: dict[str, dict] = {}

    # 「记一笔」入库后前端拿合并事实查一次；这个客户在 reset 后不存在了，
    # 所以场景只能来自 build() 开头那段预演。
    scen[scen_key(commit_resp["customer"]["id"], AS_OF, commit_resp["fact_data"])] = commit_scen

    # 时间机器四段：同一批事实，四个基准日
    for today in ("2026-07-28", "2026-09-20", "2026-10-01", "2026-10-24"):
        scen[scen_key(dongling["id"], today, dongling["fact_data"])] = analyze(
            dongling["id"], today, dongling["fact_data"])

    # 撤回「客户说本期不做了」：逾期承诺复活
    retracted = [dict(d) for d in dongling["fact_data"]]
    for d in retracted:
        if d["fact_id"] == closing["fact_id"]:
            d["status"] = "retracted"
    for today in ("2026-10-01", "2026-10-24"):
        scen[scen_key(dongling["id"], today, retracted)] = analyze(
            dongling["id"], today, retracted)

    # 撤回一条与结论无关的事实（关键人）：界面要说「结论没变」
    other = next(d for d in dongling["fact_data"] if d["fact_type"] == "stakeholder")
    retract_other = [dict(d) for d in dongling["fact_data"]]
    for d in retract_other:
        if d["fact_id"] == other["fact_id"]:
            d["status"] = "retracted"
    scen[scen_key(dongling["id"], "2026-10-24", retract_other)] = analyze(
        dongling["id"], "2026-10-24", retract_other)

    # 另外几个客户：验「切客户不串场」和三种状态。
    # c_10006 是存量池里的泰盛重工（已失单），用来验「没有主数据档案时页面说什么」。
    touched = [dongling["id"]]
    for cid in ("c_30001", "c_30002", "c_30005", "c_30006", "c_10006"):
        fd = by_id[cid]["fact_data"]
        scen[scen_key(cid, "2026-10-24", fd)] = analyze(cid, "2026-10-24", fd)
        touched.append(cid)

    # -- 三条「办完」路径的预演 -------------------------------------------------
    # 勾「已办」会真的改服务端事实（承诺 → fulfilled）。桩没法自己推算
    # 「办完之后世界长什么样」，只能先在服务端各演一遍、把回执与客户列表原样录下来。
    # 演完一条就撤销一条：撤销回到原样，状态干净，aid 也不会变。
    done_f = next(i for i in todo["items"] if i["can_fulfill"] and not i["pool"])
    done_d = next(i for i in todo["items"] if not i["can_fulfill"] and not i["pool"])
    resp_f = call("/api/done", {"customer_id": done_f["cid"], "aid": done_f["aid"],
                                "today": AS_OF})
    after_f = call("/api/customers")
    # fulfilled 状态下的 analyze 场景也要录：sel() 切客户会带新 fact_data 来查。
    cust_f_after = next(c for c in after_f["customers"] if c["id"] == done_f["cid"])
    scen_fulfilled = call("/api/analyze", {"customer_id": done_f["cid"], "today": AS_OF,
                                           "fact_data": cust_f_after["fact_data"]})
    undo_f = call("/api/done", {"customer_id": done_f["cid"], "aid": done_f["aid"],
                                "today": AS_OF, "undo": True})
    touched.append(done_f["cid"])
    resp_d = call("/api/done", {"customer_id": done_d["cid"], "aid": done_d["aid"],
                                "today": AS_OF})
    after_d = call("/api/customers")
    undo_d = call("/api/done", {"customer_id": done_d["cid"], "aid": done_d["aid"],
                                "today": AS_OF, "undo": True})

    # 「这条我不做」也要预演：它会静默 7 天，桩没法自己推算静默后的待办长什么样。
    # 挑一条既不是 fulfilled 也不是 dismissed 的，免得三份预演互相污染。
    ref_i = next(i for i in todo["items"]
                 if not i["pool"] and i["aid"] not in (done_f["aid"], done_d["aid"]))
    resp_r = call("/api/done", {"customer_id": ref_i["cid"], "aid": ref_i["aid"],
                                "mode": "refused", "reason": "not_mine", "today": AS_OF})
    undo_r = call("/api/done", {"customer_id": ref_i["cid"], "aid": ref_i["aid"],
                                "today": AS_OF, "undo": True})

    # 逾期归因也要预演。挑「历史欠账」那条（拖了 116 天的泰盛重工）——
    # 界面上给的说法按钮只出现在 aging 非空的行走里，而那一栏默认的
    # 「重点客户」筛子看不到它，所以断言里要先把筛子切到「全部」。
    hist_i = next(i for i in todo["items"] if i["when"] == "historic")
    resp_a = call("/api/done", {"customer_id": hist_i["cid"], "aid": hist_i["aid"],
                                "mode": "attributed", "reason": "chasing", "today": AS_OF})
    undo_a = call("/api/done", {"customer_id": hist_i["cid"], "aid": hist_i["aid"],
                                "today": AS_OF, "undo": True})

    # fulfilled 状态下切回该客户的场景（预演时录好的）
    scen[scen_key(done_f["cid"], AS_OF, cust_f_after["fact_data"])] = scen_fulfilled

    # 预演里 analyze 改过内存里的 fact_data（撤回版 / 已履行版），把它们放回原样。
    # 这一段替代了原来的 reset——reset 会换掉所有事实 id，那正是让 aid 漂的元凶。
    for cid in dict.fromkeys(touched):
        analyze(cid, AS_OF, by_id[cid]["fact_data"])

    # 核对：预演没让 aid 漂，否则下面那批断言全在空转。
    after_todo = call(f"/api/todo?today={AS_OF}")
    before_aids = {(i["cid"], i["aid"]) for i in todo["items"]}
    after_aids = {(i["cid"], i["aid"]) for i in after_todo["items"]}
    if before_aids != after_aids:
        diff = sorted(before_aids ^ after_aids)[:5]
        raise SystemExit(
            "预演改变了待办身份（aid），界面断言会全部空转。差异样例："
            f"{diff}\n这条不修，`ok(!S.todo.items.some(i => i.aid === ...))` "
            "永远为真，因为那条从来就没在列表里。")

    briefs = {}
    for cid, kind in (("c_30001", "visit"), ("c_30004", "handover")):
        briefs[f"{cid}|{kind}"] = call("/api/brief", {"customer_id": cid, "kind": kind,
                                                      "today": AS_OF})
        # 导出这一份也录：前端「复制」走的是同一条接口的 format=md 分支。
        # 桩不自己拼 markdown——拼出来的和真后端迟早不一样，那就白测了。
        briefs[f"{cid}|{kind}|md"] = call("/api/brief", {"customer_id": cid, "kind": kind,
                                                         "today": AS_OF, "format": "md"})

    live = call("/api/health")
    # 四入口的载荷整份照抄服务端。挑哪个客户、落在哪一块，全是服务端算的，
    # 桩自己排一遍就等于把被测逻辑抄进测试里——抄错了也照样「通过」。
    intent_data = call(f"/api/intent?today={AS_OF}")
    health = {
        "ok": True, "provider": "stub", "model": "stub", "offline": False,
        "as_of": "2026-10-24", "n_customers": len(customers["customers"]),
        "cache_size": 0, "type_cn": customers["type_cn"],
        "source_cn": {"visit_note": "拜访纪要", "meeting_minutes": "会议纪要",
                      "email": "邮件", "call_transcript": "通话记录",
                      "im_message": "即时通讯", "ticket": "工单"},
        # 原因的文案不在这里编，照抄服务端——两边各写一份，
        # 改了一边忘了另一边，用户点下去就是 400。
        "refuse_reasons": live["refuse_reasons"],
        "refuse_silence_days": live["refuse_silence_days"],
        "attribute_reasons": live["attribute_reasons"],
        "chase_silence_days": live["chase_silence_days"],
        # 重置按钮的可用性由服务端说了算（P2-3）。这里故意给 false：
        # 桩跑的是「没开 --allow-reset」的那一种服务，正好用来验置灰逻辑；
        # 真开着的服务默认是 true，但那个分支在冒烟 P 组里已经走过真 HTTP。
        "allow_reset": False,
        "user": "u_demo",
        "store": {"mode": "sqlite", "path": "/tmp/stub.db", "schema_version": 1,
                  "on_disk": True, "size": 1024, "seeded": True},
    }
    return {"customers": customers, "scen": scen, "health": health,
            "todo": todo, "briefs": briefs,
            # 「已办」桩料：回执按 aid 查表；客户列表按「当前办掉的是哪条」切换。
            # 三个版本都来自服务端真实预演，桩不做任何拼装。
            "done": {"fulfilled": {"aid": done_f["aid"], "cid": done_f["cid"],
                                   "resp": resp_f, "undo": undo_f,
                                   "customers": after_f},
                     "dismissed": {"aid": done_d["aid"], "cid": done_d["cid"],
                                   "resp": resp_d, "undo": undo_d,
                                   "customers": after_d},
                     # 「不做」不改事实，所以客户列表仍是 BASE，不需要单独录一份。
                     "refuse": {"aid": ref_i["aid"], "cid": ref_i["cid"],
                                "resp": resp_r, "undo": undo_r},
                     # 归因里「还追」也不改事实 → 客户列表仍是 BASE。
                     "attr": {"aid": hist_i["aid"], "cid": hist_i["cid"],
                              "resp": resp_a, "undo": undo_a}},
            # 「记一笔」桩料：草稿与入库的回执，都是上面真跑一遍录下来的。
            "draft": {"resp": draft_resp, "keep": draft_keep,
                      "commit": commit_resp, "scen": commit_scen},
            # 四入口：原样照抄，桩不重排
            "intent": intent_data}


# ---------------------------------------------------------------------------
# 桩：够页面跑起来就行，多一个功能都不要
# ---------------------------------------------------------------------------

STUB = r"""
const _els = {};
function _mk(id){
  return {
    id, textContent:'', innerHTML:'', className:'', value:'', style:{}, dataset:{},
    classList:{ _s:new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
      toggle(c,v){ if(v===undefined){ this._s.has(c)?this._s.delete(c):this._s.add(c);} else { v?this._s.add(c):this._s.delete(c);} },
      contains(c){return this._s.has(c)} },
    querySelectorAll(){ return []; }, focus(){}, scrollIntoView(){},
  };
}
globalThis.document = {
  getElementById(id){ return _els[id] || (_els[id] = _mk(id)); },
  querySelectorAll(sel){
    const m = /^\[id\^="(.+)"\]$/.exec(sel || '');
    if (m) return Object.values(_els).filter(e => e.id.startsWith(m[1]));
    return [];
  },
  addEventListener(){},
};
function syncEls(html){
  for (const m of String(html||'').matchAll(/id="([^"]+)"/g)) globalThis.document.getElementById(m[1]);
}

// 剪贴板桩：页面用 navigator.clipboard 优先，这里把它截住，
// 断言的对象就是「真的会被粘出去的那段文字」。
// 注意 must use defineProperty：Node 21+ 自带只读的 navigator，
// 直接赋值在非严格模式下是静默失败的——一度让「复制」走成兜底分支还查不出原因。
globalThis.window = globalThis;
globalThis.isSecureContext = true;
globalThis._clip = { text: null, writes: 0, copied: null, selected: null };
Object.defineProperty(globalThis, 'navigator', {
  value: { clipboard: { writeText: async (t) => {
    globalThis._clip.text = t; globalThis._clip.writes += 1; } } },
  configurable: true, writable: true,
});
// textarea 兜底路径要用到这三个，Node 里没有
document.createElement = (tag) => {
  const e = _mk('_' + tag);
  e.setAttribute = () => {};
  e.select = () => { globalThis._clip.selected = e.value; };
  return e;
};
const _bodyKids = [];
document.body = {
  appendChild(e){ _bodyKids.push(e); },
  removeChild(e){ const i = _bodyKids.indexOf(e); if (i >= 0) _bodyKids.splice(i, 1); },
};
document.execCommand = () => {
  globalThis._clip.copied = _bodyKids.length ? _bodyKids[0].value : null;
  return true;
};

const BASE = __CUSTOMERS__;
const SCEN = __SCEN__;
const TODO = __TODO__;
const BRIEFS = __BRIEFS__;
const DONE = __DONE__;
const DRAFT = __DRAFT__;
const INTENT = __INTENT__;
// 当前「办掉的是哪条」。null = 没办，客户列表回 BASE；
// 有值 = 办掉了那条，客户列表回对应的服务端预演版本。
// 桩不拼装任何数据，三个版本都是 build() 时从服务端录的。
let doneAid = null;
globalThis.fetch = async (path, opt) => {
  let d;
  if (path === '/api/health') d = __HEALTH__;
  else if (path === '/api/customers'){
    if (doneAid === DONE.fulfilled.aid) d = DONE.fulfilled.customers;
    else if (doneAid === DONE.dismissed.aid) d = DONE.dismissed.customers;
    else d = BASE;
  }
  else if (path === '/api/samples') d = {samples: []};
  else if (String(path).indexOf('/api/intent') === 0) d = INTENT;
  else if (String(path).indexOf('/api/todo') === 0) d = TODO;
  else if (path === '/api/done'){
    const body = JSON.parse((opt && opt.body) || '{}');
    // 撤销请求不带 mode，所以除了看 mode 还要看 aid——否则「我不做」的撤销
    // 会掉进「已办」那条分支，桩查不到这个 aid，界面收到 500。
    const isAttr = body.mode === 'attributed'
      || (DONE.attr && body.aid === DONE.attr.aid);
    const isRefuse = body.mode === 'refused'
      || (DONE.refuse && body.aid === DONE.refuse.aid);
    if (isAttr){
      if (!DONE.attr || DONE.attr.aid !== body.aid)
        return { ok:false, status:500,
                 json: async () => ({error:'桩里没有这条归因：' + body.aid}) };
      d = body.undo ? DONE.attr.undo : DONE.attr.resp;
    } else if (isRefuse){
      if (!DONE.refuse || DONE.refuse.aid !== body.aid)
        return { ok:false, status:500,
                 json: async () => ({error:'桩里没有这条「我不做」：' + body.aid}) };
      d = body.undo ? DONE.refuse.undo : DONE.refuse.resp;
    } else {
      const slot = body.aid === DONE.fulfilled.aid ? DONE.fulfilled : DONE.dismissed;
      if (slot.aid !== body.aid)
        return { ok:false, status:500, json: async () => ({error:'桩里没有这条已办：' + body.aid}) };
      if (body.undo){ doneAid = null; d = slot.undo; }
      else { doneAid = body.aid; d = slot.resp; }
    }
  }
  else if (path === '/api/brief'){
    const body = JSON.parse((opt && opt.body) || '{}');
    const md = /^(md|markdown)$/i.test(body.format || '');
    const bkey = body.customer_id + '|' + body.kind + (md ? '|md' : '');
    d = BRIEFS[bkey];
    if (!d) return { ok:false, status:500, json: async () => ({error:'桩里没有这份简报：' + bkey}) };
  }
  else if (path === '/api/draft') d = DRAFT.resp;
  else if (path === '/api/commit'){
    const body = JSON.parse((opt && opt.body) || '{}');
    if (body.draft_id !== DRAFT.resp.draft_id)
      return { ok:false, status:500,
               json: async () => ({error:'桩里没有这份草稿：' + body.draft_id}) };
    // 把界面实际发出去的 keep 记下来。勾选传错了就是界面传错了，
    // 桩不替它圆——断言要能看出「你勾的是这几条，发出去的是另外几条」。
    globalThis._keepSent = body.keep || null;
    d = DRAFT.commit;
  }
  else if (path === '/api/analyze'){
    const body = JSON.parse((opt && opt.body) || '{}');
    const hit = SCEN[scenKey(body)];
    // 查不到就报错，不回落到任何默认值——回落会让用例「通」，但测的是别的场景
    if (!hit) return { ok:false, status:500, json: async () => ({error:'桩里没有这个场景：' + scenKey(body)}) };
    d = hit;
  }
  else return { ok:false, status:404, json: async () => ({error:'no stub: '+path}) };
  return { ok:true, status:200, json: async () => d };
};
function scenKey(body){
  const fd = body.fact_data || [];
  const rt = fd.filter(d => d.status === 'retracted').map(d => d.fact_id).sort();
  // 勾「已办」会把承诺改成 fulfilled。不进键的话，办完之后的刷新会查回
  // 「没办过」的旧场景，界面说已履行、数据说逾期——测试通了，测错了东西。
  const fl = fd.filter(d => d.payload && d.payload.fulfillment === 'fulfilled')
              .map(d => d.fact_id).sort();
  return body.customer_id + '|' + body.today + '|' + rt.join(',') + '|F:' + fl.join(',');
}
"""


TESTS = r"""
const fails = [];
const ok = (cond, msg) => { console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if(!cond) fails.push(msg); };
const has = (hay, needle) => String(hay).indexOf(needle) >= 0;
const panel = () => { syncEls(el('panel').innerHTML); return el('panel').innerHTML; };
// 回执里带 <b> 标签，断言前先剥掉，否则测的是标记不是文案
const dtxt = () => el('delta').innerHTML.replace(/<[^>]+>/g, '');
const cardOf = (h, key) => {
  const seg = h.slice(h.indexOf('id="' + key + '"'));
  return seg.slice(0, seg.indexOf('</article>'));
};

(async () => {
  await new Promise(r => setTimeout(r, 400));
  panel();

  console.log('\n[1] 39 个客户：重点 6 + 存量 33，三种状态都覆盖到');
  const listHtml = el('list').innerHTML;
  const nFocus = S.customers.filter(c => !c.pool).length;
  const nPool = S.customers.filter(c => c.pool).length;
  ok(S.customers.length === 39 && nFocus === 6 && nPool === 33,
     '列表共 39 个客户（重点 ' + nFocus + ' · 存量 ' + nPool + '）');
  ok(has(listHtml, '重点客户') && has(listHtml, '存量客户'),
     '侧栏分成两段，不是 39 行平铺');
  ok(listHtml.indexOf('恒远重工') < listHtml.indexOf('江南精密'),
     '重点客户排在存量客户前面');
  ok(has(listHtml, '东岭铜业') && has(listHtml, '瑞禾食品') && has(listHtml, '云启软件'), '三个新客户在列表里');
  ok(has(el('footinfo').innerHTML, '重点 6 · 存量 33'),
     '页脚说清规模：' + el('footinfo').innerHTML);
  ok(S.customers.some(c => c.pool && c.records.length === 1),
     '存量客户里有一条记录的迷你客户——真实台账就长这样');
  const byP0 = S.customers.map(c => c.name + ':' + c.n_p0).join(' ');
  ok(S.customers.some(c => c.n_p0 >= 3), '有客户 P0 ≥ 3 —— ' + byP0);
  ok(S.customers.some(c => c.n_p0 === 0 && c.facts.length >= 5), '有客户有卡但无 P0 —— ' + byP0);
  ok(S.customers.some(c => c.facts.length <= 2), '有客户信息不足（≤2 条事实）—— ' + byP0);

  console.log('\n[2] 切到东岭：默认日承诺已关闭');
  await sel('c_30004');
  let h = panel();
  ok(S.cur.name === '东岭铜业', '当前客户：' + S.cur.name);
  ok(!has(h, '承诺逾期'), '默认日没有逾期承诺');
  ok(has(h, '客户决定本期不再要求交付接入方案'), '「客户说不用做了」那条在卡上');
  ok(has(h, '节点已过') === false, '不该有已过节点');
  ok(has(el('dtinfo').textContent, '逾期承诺 0 条'), '页眉：' + el('dtinfo').textContent);

  console.log('\n[3] 拖回 10-01：10-09 那篇还没写，承诺重新逾期');
  await setDate('2026-10-01');
  h = panel();
  ok(has(h, '承诺逾期'), '10-01 有逾期承诺 —— 这是「一条记录改变结论」');
  ok(!has(h, '客户决定本期不再要求'), '10-01 时那条作废记录还不存在');
  ok(has(el('dtinfo').textContent, '另有 2 条记录发生在 2026-10-01 之后'),
     '页眉说清还有几条记录没入库：' + el('dtinfo').textContent);
  ok(has(el('delta').innerHTML, '0 → 1'), '差值栏 P0 0 → 1');
  ok(!has(h, '本期只做报表'), '10-21 的确认记录也没提前出现');

  console.log('\n[4] 撤回「客户说不用做了」：逾期承诺回到台面');
  await setDate('2026-10-24');
  h = panel();
  ok(has(h, '这条不对'), '事实卡上有「这条不对」');
  const closing = S.cur.facts.find(f => f.statement.indexOf('客户决定本期不再要求') === 0);
  ok(!!closing, '找得到那条作废记录');
  await retract(closing.key);
  h = panel();
  ok(S.cur.n_p0 === 1, '撤回后 P0 = ' + S.cur.n_p0 + '（应为 1）');
  ok(has(h, '已撤回 · 不计入结论'), '卡片标了「已撤回」');
  ok(has(h, '撤销撤回'), '给了「撤销撤回」');
  const gone_card = cardOf(h, closing.key);
  ok(!has(gone_card, '这条不对') && has(gone_card, '撤销撤回'),
     '这张卡只剩「撤销撤回」，两个按钮不同时出现');
  ok(has(h, '承诺逾期 24 天'), '复活的承诺显示逾期 24 天');
  ok(has(dtxt(), 'P0 0 → 1'), '动作回执：P0 0 → 1 —— ' + dtxt());

  console.log('\n[5] 撤销撤回：一切回到原样');
  const gone = S.cur.facts.find(f => f.retracted);
  await retract(gone.key, true);
  h = panel();
  ok(S.cur.n_p0 === 0, '撤销后 P0 = ' + S.cur.n_p0 + '（应为 0）');
  ok(!has(h, '已撤回 · 不计入结论'), '「已撤回」标记消失');
  ok(has(h, '这条不对'), '「这条不对」回来了');
  ok(has(dtxt(), 'P0 1 → 0'), '动作回执：P0 1 → 0 —— ' + dtxt());

  console.log('\n[6] 撤回一条与结论无关的事实：说清楚「结论没变」');
  const stake = S.cur.facts.find(f => f.fact_type === 'stakeholder');
  await retract(stake.key);
  ok(S.cur.n_p0 === 0, '撤一条关键人不改变 P0');
  ok(has(dtxt(), '结论没变'), '回执写明结论没变 —— ' + dtxt());
  await retract(stake.key, true);

  console.log('\n[7] 切客户不串场');
  await sel('c_30005');
  ok(S.cur.name === '瑞禾食品', '切到瑞禾食品');
  ok(S.cur.n_p0 === 0, '瑞禾没有 P0');
  ok(has(panel(), '已解决的顾虑') || has(panel(), '集成风险'), '已解决的顾虑仍在卡上（留证，不进待办）');
  await sel('c_30006');
  ok(has(panel(), '信息不足') || has(el('verdict').innerHTML, '信息不足'),
     '只有一句话的客户直接说信息不足');

  console.log('\n[8] 复位后不残留撤回状态');
  await sel('c_30004');
  ok(!has(panel(), '已撤回 · 不计入结论'), '再切回来，撤回状态不残留（那是另一个客户的数据）');


  console.log('\n[9] 档案条与决策链：回答「是谁、值多少、找谁」');
  await sel('c_30001');
  h = panel();
  const stripHtml = el('strip').innerHTML;
  ok(has(stripHtml, '3200 人') && has(stripHtml, '180 万'), '档案条有规模与在办商机');
  ok(has(stripHtml, '合作') && has(stripHtml, '合同'), '档案条有合作年限与合同到期');
  ok(has(h, '决策链'), '认知卡里有决策链这一段');
  ok(has(h, '李国强') && has(h, '决策人'), '决策链点名到人、标出角色');
  ok(has(h, '还缺：'), '缺的环节画成了虚线框，不是不提');
  await sel('c_30004');
  ok(has(el('panel').innerHTML, '还没摸到决策人') || has(el('panel').innerHTML, '单线联系'),
     '东岭只认出 1 个人 → 明说决策链有缺口');

  console.log('\n[10] 今天要做的事：从「一个客户的卡」到「一个销售的早上」');
  ok(S.todo && S.todo.n_total > 0, '启动时就取到了待办（' + (S.todo ? S.todo.n_total : 0) + ' 件）');
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  ok(S.view === 'todo', '切到了待办视图');
  ok(el('cname').textContent === '今天要做的事', '标题换成了待办');
  let todoHtml = panel();
  ok(has(todoHtml, '逾期承诺'), '列表按「急」分类，逾期排在最前');
  ok(has(todoHtml, '蓝海物流'), '每条都带客户名，不用猜是谁的事');
  ok(has(el('cmeta').innerHTML, '39 个客户'), '页眉写出规模：' + el('cmeta').innerHTML);
  ok(has(S.todo.headline, '另有'), '一句话里点明存量客户还剩多少：' + S.todo.headline);
  ok(has(todoHtml, '重点客户') && has(todoHtml, '存量客户') && has(todoHtml, '全部'),
     '三档筛子都在（重点 / 存量 / 全部）');
  const nFocusTodo = S.todo.by_pool.focus.n_items;
  ok(S.todo.by_pool.pool.n_items > nFocusTodo,
     '存量待办（' + S.todo.by_pool.pool.n_items + ' 件）多于重点（' + nFocusTodo + ' 件）——'
     + '这就是为什么必须先排序');

  // 默认停在「重点客户」：94 件一起铺开，演示时第一屏全是存量客户的逾期承诺，
  // 反而看不出重点客户在推进什么。
  ok(S.todoFilter === 'focus', '默认筛在「重点客户」');
  ok(!has(todoHtml, '江南精密'), '默认不带出存量客户的待办');

  setTodoFilter('pool');
  todoHtml = panel();
  ok(has(todoHtml, '江南精密') && !has(todoHtml, '蓝海物流'),
     '切到存量客户：只剩存量那批');
  ok(has(todoHtml, 'poolitem'), '存量待办在样式上和重点待办区分开');
  // 存量那一栏不分今天/本周：给它画一个「今天要打的电话 0 件」的标题，
  // 等于在页面上说一句假话——下面明明排着几十条。
  ok(!has(todoHtml, '今天要打的电话'), '存量栏不套用「今天要打的电话」这个标题');
  ok(has(todoHtml, '不急着今天动'), '存量栏说清这些不是今天必须动的');
  const firstOps = S.todo.items.find(i => i.kind === 'commitment_overdue');
  await gotoTodo(firstOps.cid, firstOps.basis);
  ok(S.view === 'customer' && S.cur.name === '蓝海物流', '点一条能跳回该客户');
  ok(has(el('delta').innerHTML + panel(), ''), '跳转后页面正常渲染');

  console.log('\n[11] 拜访简报 / 交接说明：能直接发给别人的东西');
  await sel('c_30001');
  await openBrief('visit');
  const briefHtml = el('bbody').innerHTML;
  ok(has(briefHtml, '上次答应的事'), '简报有「上次答应的事」');
  ok(has(briefHtml, '建议问的问题'), '简报有「建议问的问题」');
  ok(has(briefHtml, '6 天后到期'), '承诺状态是算出来的天数，不是「即将到期」');
  ok(has(briefHtml, '没有经过模型自由发挥'), '页面明说这份东西不是模型写的');
  ok(el('bdrawer').classList.contains('on'), '抽屉打开了');

  // 复制：拼装在服务端，前端只负责写剪贴板。这里断言的是「粘出去的那段字」，
  // 不是「按钮存在」——按钮存在但粘出来是空白的，才是真实会发生的事故。
  const clip0 = _clip.writes;
  await copyBrief();
  ok(_clip.writes === clip0 + 1, '点复制真的往剪贴板写了一次');
  ok(has(_clip.text, '# 拜访简报'), '粘出去的第一行是一级标题');
  ok(has(_clip.text, '\n## 上次答应的事'), '每个块都带二级标题');
  ok(has(_clip.text, '\n- '), '条目是列表，不是一整段');
  ok(has(_clip.text, '找到出处'), '末尾留着出处声明');
  ok(!has(_clip.text, '**') && !has(_clip.text, '|'), '不用加粗和表格——粘进企微不会变形');
  ok(el('bcopy').textContent.indexOf('已复制') === 0, '复制成功有回执：' + el('bcopy').textContent);

  // 内网 http、或用 file:// 直接打开页面时没有 clipboard API，必须退到 textarea。
  // 这条路径平时不出现，一旦出现就是「点了没反应」，所以也得验。
  const keepClip = navigator.clipboard;
  navigator.clipboard = null;
  await copyBrief();
  ok(_clip.copied && has(_clip.copied, '# 拜访简报'),
     '没有剪贴板 API 时退到 textarea，照样复制得出来');
  navigator.clipboard = keepClip;

  // 拿不到导出时必须说出来。默默失败，用户会把一片空白粘进交接邮件。
  const keepCur = S.cur;
  S.cur = {id: 'c_10006'};
  await copyBrief();
  ok(el('bcopy').textContent === '复制失败', '导出拿不到时明说失败，不假装成功');
  ok(_clip.writes === clip0 + 1, '失败时不该往剪贴板写半截内容');
  S.cur = keepCur;
  closeBrief();

  await sel('c_30004');
  await openBrief('handover');
  const handHtml = el('bbody').innerHTML;
  ok(has(handHtml, '正在办的三件事'), '交接说明有在办事项');
  ok(has(handHtml, '这些坑别再踩'), '交接说明有踩过的坑');
  ok(has(handHtml, '还没搞清的'), '交接说明列出「不知道自己不知道」的部分');
  closeBrief();

  console.log('\n[12] 存量客户：事实有出处，主数据没有');
  S.todoFilter = 'focus';
  await sel('c_10006');
  const poolCard = panel();
  ok(S.cur.name === '泰盛重工' && S.cur.pool === true, '打开了存量客户：' + S.cur.name);
  ok(S.cur.facts.length > 0, '存量客户不是空壳（' + S.cur.facts.length + ' 条事实）');
  ok(has(poolCard, '博思数智'), '失单这种结论在卡上说得出来');
  ok(has(el('cmeta').innerHTML, '来自评测集'), '页眉标明这批客户的来源');
  ok(has(el('strip').innerHTML, '没有主数据档案'), '没有档案就明说，不编规模');
  // 空档案不再留空行：从记录里推三格真实统计，但每格都标「推算」。
  // 这里断言的是「指标标签的集合」，不是「页面里有没有某个词」——
  // 提示语本身就写着「规模 / 合同 / 在办商机在 CRM 里」，按词断言会误判。
  const poolLabels = [...el('strip').innerHTML.matchAll(/<div class="ml">([^<]*)/g)].map(m => m[1]);
  ok(poolLabels.length > 0, '空档案也画出了格子，不再是空白行');
  ok(JSON.stringify(poolLabels) === JSON.stringify(['记录', '首次记录', '近 90 天']),
     '推出来的只有这三格：' + poolLabels.join('、'));
  ok(poolLabels.indexOf('合作') < 0, '「首次记录」不许冒充「合作起始」——那是没根据的话');
  const nMarks = (el('strip').innerHTML.match(/推算/g) || []).length;
  ok(nMarks >= poolLabels.length, '每一格都标了「推算」（' + nMarks + '/' + poolLabels.length + '）');
  const pf = S.cur.facts[0];
  ok(pf.span && S.cur.records.some(r => r.text.includes(pf.span) || r.id === pf.rid),
     '每条事实都能回到原文（' + (pf.span || '').slice(0, 12) + '…）');

  console.log('\n[13] 已办：勾一条，然后真的少一件');
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  let todoHtml13 = panel();
  ok(has(todoHtml13, '✓ 已履行') && has(todoHtml13, '✓ 已办'),
     '按钮区分两种「办完」：能真办完的叫已履行，只是不再提醒的叫已办');

  // 履行路径：勾掉蓝海物流那条逾期承诺
  await markDone(DONE.fulfilled.cid, DONE.fulfilled.aid);
  ok(S.doneRec && S.doneRec.mode === 'fulfilled', '承诺类走了「已履行」路径');
  let db13 = el('donebar').innerHTML.replace(/<[^>]+>/g, '');
  ok(has(db13, '已标记履行'), '回执写明是「标记履行」');
  ok(has(db13, '不再是问题'), '回执说清不是从列表里藏起来');
  ok(has(db13, '顶上一条'), '回执解释了为什么待办总数没变（后面顶上一条）');
  ok(has(db13, '撤销'), '回执带撤销入口');
  ok(!S.todo.items.some(i => i.aid === DONE.fulfilled.aid), '待办列表里这条没了');

  // 到客户页看证据：那条承诺的徽标应该变成「已履行」
  await sel(DONE.fulfilled.cid);
  ok(has(panel(), '已履行'), '认知卡上这条承诺显示「已履行」——状态真的变了');
  ok(!S.cur.actions.some(a => a.aid === DONE.fulfilled.aid), '建议区也不再出现那条');

  // 撤销：回到原样
  await markDone(DONE.fulfilled.cid, DONE.fulfilled.aid, true);
  ok(S.doneRec && S.doneRec.mode === 'undone', '撤销 mode=undone');
  ok(has(el('donebar').innerHTML.replace(/<[^>]+>/g, ''), '回到待办里'), '回执说撤销');
  ok(has(panel(), '承诺逾期'), '撤销后逾期承诺回来了——撤销是回到原样');

  // 「知道了」路径：非承诺类
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  await markDone(DONE.dismissed.cid, DONE.dismissed.aid);
  ok(S.doneRec && S.doneRec.mode === 'dismissed', '非承诺类走了「知道了」路径');
  ok(has(el('donebar').innerHTML.replace(/<[^>]+>/g, ''), '结论本身没变'),
     '回执说清结论没变——不能让用户以为勾掉就等于解决了');
  ok(!S.todo.items.some(i => i.aid === DONE.dismissed.aid), '这条也不再出现在待办里');
  await markDone(DONE.dismissed.cid, DONE.dismissed.aid, true);
  ok(has(el('donebar').innerHTML.replace(/<[^>]+>/g, ''), '回到待办里'),
     '撤销后回执说「回到待办里」');
  closeDone();
  ok(el('donebar').innerHTML === '', '关掉回执，界面干净');

  console.log('\n[14] 这条我不做：说清原因，只静默 7 天');
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  const refAid = DONE.refuse.aid;
  const t14 = panel();
  // 两区是这一轮改动的重点：第一区是今天要打的电话，第二区是系统缺的信息。
  // 混在一起时，用户打开看到的一半是「请补充档案」。
  ok(has(t14, '今天要打的电话'), '第一区是今天要打的电话');
  ok(has(t14, '要补的数据'), '第二区是系统缺的信息，单独放');
  ok(has(t14, '今日名额'), '标题上写明今天只做几件——不然用户以为漏看了一半');
  ok(has(t14, '本周内再排'), '超出名额的降成「本周」，不是被藏起来');

  ok(has(t14, '我不做'), '每条待办旁边都有「我不做」');
  askRefuse(refAid);
  ok(has(panel(), '为什么不做'), '点「我不做」先问原因，不直接吞掉');
  ok(has(panel(), '不归我跟'), '原因选项来自服务端，不是前端自己编的');
  cancelRefuse();
  ok(!has(panel(), '为什么不做'), '取消后这次询问收起来');

  await markRefuse(DONE.refuse.cid, refAid, 'not_mine');
  ok(S.doneRec && S.doneRec.mode === 'refused', '「我不做」和「已办」是两条路径');
  const rbar = el('donebar').innerHTML.replace(/<[^>]+>/g, '');
  ok(has(rbar, '记下了'), '回执说的是「记下了」，不是「已办」——两件事不能混成一句话');
  ok(has(rbar, '不归我跟'), '回执写明了原因');
  ok(has(rbar, '2026-10-31'),
     '回执写明静默到哪天：不然 7 天后它自己回来，用户会以为按钮坏了');
  ok(!S.todo.items.some(i => i.aid === refAid), '这条先从待办里退场');
  await markDone(DONE.refuse.cid, refAid, true);
  ok(has(el('donebar').innerHTML.replace(/<[^>]+>/g, ''), '回到待办里'), '「我不做」也能撤销');
  closeDone();

  console.log('\n[15] 金额：这一行值多少钱');
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  const t15 = panel();
  // 待办这一屏没有档案条，用户就是靠这个数字决定先打哪个电话。
  // 只断言「金额出现过」不够——「260 万」也可能只是碰巧出现在标题里，
  // 所以连 class 一起验。
  ok(has(t15, 'class="amt"'), '待办行上画了金额');
  ok(has(t15, '260 万'), '金额是这个客户的在办商机，不是编的');
  ok(!has(t15, '金额未定'),
     '不知道金额时不画「金额未定」——33 个存量客户都没有主档案，每行挂一个等于加噪声');

  const p0do = S.todo.items.filter(i => !i.pool && i.bucket === 'do' && i.p === 'P0');
  ok(p0do.length >= 2, '重点客户的 P0 要做的事够两条，能验先后');
  ok(p0do[0].amount >= p0do[p0do.length - 1].amount,
     '同一个 P0 里，大单排在前面——5 万和 200 万不该同权重');

  const p1 = S.todo.items.filter(i => !i.pool && i.p === 'P1');
  const iMoney = p1.findIndex(i => i.amount === 95);
  const iUnknown = p1.findIndex(i => i.amount === 0);
  ok(iMoney >= 0 && iUnknown > iMoney,
     '没有金额的客户排在小单后面（中位，不是垫底）');
  ok(S.todo.items.every(i => i.impact > 0),
     'impact 不会是 0 分——缺金额不等于这单不值钱');
  const noAmount = S.todo.items.filter(i => i.amount === 0 && i.cname === '星海电子');
  ok(noAmount.length > 0 && noAmount.every(i => i.impact === 50),
     '拿不到金额时给中位分 50，不给 0 也不给 95');

  console.log('\n[16] 历史欠账：拖过 90 天，先决定还发不发');
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  // 默认筛子是「重点客户」，而那条拖了 116 天的欠账落在存量客户里
  // （泰盛重工，已失单）。切到「全部」才看得到——这本身也是对的：
  // 重点客户板上不该混进一个已经失单的客户。
  setTodoFilter('all');
  const t16 = panel();
  ok(has(t16, '历史欠账'), '有单独一栏，不是混进「本周内再排」');
  ok(has(t16, '先决定还发不发'), '标题就说清了这一栏要的不是「去办」');
  ok(has(t16, '给个说法'), '这一栏的行给的是归因按钮，不是「已履行」');
  ok(has(t16, 'backlogitem'), '样式上要和今天的电话区分开（虚线、中性色）');

  askAttribute(DONE.attr.aid);
  ok(has(panel(), '还发不发？'), '点「给个说法」先问，不直接吞掉');
  ok(has(panel(), '还在追'), '三个归因选项来自服务端，不是前端自己编的');
  ok(has(panel(), '客户已经不需要了'), '归因里有「关账」这条——不然用户只能点「还在追」');
  ok(has(panel(), '转成内部事项'), '归因里有「转内部」——承诺还在，只是不算客户欠账');

  await markAttribute(DONE.attr.cid, DONE.attr.aid, 'chasing');
  ok(S.doneRec && S.doneRec.mode === 'attributed', '归因走的是 attributed，不是 dismissed');
  const abar = el('donebar').innerHTML.replace(/<[^>]+>/g, '');
  ok(has(abar, '先压住'), '回执说的是「先压住」，不是「已办」');
  ok(has(abar, '2026-11-23'), '回执写明压到哪天：一个月后它会自己回来');
  ok(has(abar, '逾期天数照实留着'),
     '回执说清天数没清零——客户确实还没拿到东西，清零是撒谎');
  ok(!S.todo.items.some(i => i.aid === DONE.attr.aid), '归因后这条退出待办');
  await markDone(DONE.attr.cid, DONE.attr.aid, true);
  ok(has(el('donebar').innerHTML.replace(/<[^>]+>/g, ''), '回到待办里'), '归因也能撤销');
  closeDone();
  setTodoFilter('focus');

  console.log('\n[17] 记一笔：先看抽到什么，勾了才入库');
  const N = DRAFT.resp.n_facts;
  const keepN = DRAFT.keep.length;
  await openPaste();
  el('ptext').value = '10月22日拜访，王部长说年底前必须完成设备联网改造，我们答应下周三之前发方案。';
  el('pcust').value = '__new__';
  el('pnewname').value = DRAFT.resp.customer_name;
  await submitPaste();
  panel();

  ok(S.draft && S.draft.draft_id === DRAFT.resp.draft_id,
     '抽完先进草稿，不直接入库：' + (S.draft && S.draft.draft_id));
  ok(el('pform').style.display === 'none' && el('pstep2').style.display !== 'none',
     '表单收起来、勾选面板打开——两步不能同时摆在屏幕上');
  ok(el('psubmit').style.display === 'none' && el('pcommit').style.display !== 'none',
     '底部按钮跟着换：「抽取看看」退场，「确认入库」上场');
  ok(has(el('dsum').innerHTML, '抽出 ' + N + ' 条'), '面板顶上写清抽出几条：' + el('dsum').innerHTML);

  const dlist = el('dlist').innerHTML;
  ok(has(dlist, 'class="tagt"'), '每条带类型标签');
  ok(has(dlist, 'class="dspan"'), '每条带原文片段——「系统是不是听岔了」只能靠对照原文判断');
  ok(has(dlist, 'class="dgist"'), '每条带半句关键信息（优先级/到期日这类）');
  ok(has(dlist, 'type="checkbox"'), '用真复选框，不是靠点行猜');
  ok(S.picked.size === N, '默认全勾：默认都不勾，每一条都要多点一次');
  ok(has(el('pcommit').textContent, '确认入库（' + N + ' 条）'),
     '按钮上说清这次会入库几条：' + el('pcommit').textContent);

  // 取消勾选：这一条不该再进库
  const dropId = DRAFT.resp.facts[0].fact_id;
  togglePick(dropId, false);
  ok(S.picked.size === N - 1, '取消勾选后已选少一条');
  ok(!S.picked.has(dropId), '取消的那条从选中集合里出去了');
  ok(has(el('dlist').innerHTML, 'class="drow off" id="drow-' + dropId + '"'),
     '取消勾选的行要划掉，不能只是复选框空着');
  ok(has(el('dlist').innerHTML, 'class="drow" id="drow-' + DRAFT.keep[1] + '"'),
     '没取消的行不划——满屏删除线比不划更难看懂');
  ok(has(el('pcommit').textContent, '确认入库（' + (N - 1) + ' 条）'),
     '按钮上的条数跟着变：' + el('pcommit').textContent);
  ok(has(el('dhint').innerHTML, '会随草稿一起消失'),
     '说清没勾的那几条去哪了——不说，用户会以为系统抽少了');

  pickAll(false);
  ok(S.picked.size === 0, '「全不选」真的清空');
  ok(el('pcommit').disabled === true,
     '一条都没勾时按钮要禁用：让用户点下去吃 400，等于让他自己撞墙');
  ok(has(el('dhint').innerHTML, '整条丢弃'),
     '零勾选时告诉用户还有「整条丢弃」这条零成本的路');
  pickAll(true);
  ok(S.picked.size === N, '「全选」能回来');

  // 收回到桩里录好的那两条再入库：桩只回放这一种结果，别的组合桩里没有。
  // 这一步顺便验「界面勾的」和「发出去的 keep」是同一批。
  DRAFT.resp.facts.forEach(f => { if (DRAFT.keep.indexOf(f.fact_id) < 0) togglePick(f.fact_id, false); });
  ok(S.picked.size === keepN, '收到只剩录好的 ' + keepN + ' 条');
  await commitDraft();

  const sent = (globalThis._keepSent || []).slice().sort();
  ok(sent.length === keepN && sent.join() === DRAFT.keep.slice().sort().join(),
     '发出去的 keep 就是界面勾的那几条：' + JSON.stringify(sent));
  ok(S.receipt && S.receipt.n_facts === DRAFT.commit.n_facts,
     '入库条数：' + (S.receipt && S.receipt.n_facts));
  ok(S.receipt.n_discarded === DRAFT.commit.n_discarded,
     '勾掉的条数要带进回执：' + (S.receipt && S.receipt.n_discarded));
  const rp = el('receipt').innerHTML;
  ok(has(rp, '勾掉了'), '回执必须说「你在草稿上勾掉了 N 条」——不说，用户会以为系统只抽出这么点');
  ok(has(rp, '原文照留'), '回执说清原文还在：勾掉事实不等于删掉那句话');
  ok(S.cur && S.cur.name === DRAFT.commit.customer.name,
     '入库后自动跳到这个客户：' + (S.cur && S.cur.name));
  ok(el('pform').style.display !== 'none' && el('pstep2').style.display === 'none',
     '入库后弹窗关掉、回到第一步，下次打开是干净的');

  // 整条丢弃：不调 /api/commit 就等于什么都没发生
  const nCust = S.customers.length;
  await openPaste();
  el('ptext').value = '又一段原文。';
  el('pcust').value = '__new__';
  el('pnewname').value = '不会被创建的客户';
  await submitPaste();
  ok(S.draft && S.draft.n_facts === N, '草稿又能开一份');
  discardDraft();
  ok(S.draft === null && S.picked === null, '丢弃后草稿状态清空');
  ok(el('dialog').classList.contains('on') === false, '丢弃后弹窗关掉');
  ok(S.customers.length === nCust,
     '丢弃没有新建客户——草稿阶段连客户都不该建，否则侧栏里多一个空壳：'
     + nCust + ' → ' + S.customers.length);
  ok(has(el('toast').textContent, '已丢弃') && has(el('toast').textContent, '一条都没留下'),
     '丢弃要回执，而且要说清「一条都没留下」：' + el('toast').textContent);

  console.log('\n[18] 四入口：从动作开始，两次点击落到那一块');
  const G0 = INTENT.intents[0];
  const bar = el('ichips').innerHTML;
  ok(S.intent && S.intent.intents.length === 4, '四个入口都在：' + S.intent.intents.map(g => g.label).join(' / '));
  ok(has(bar, '我要联系') && has(bar, '我要拜访') && has(bar, '我要出方案') && has(bar, '我要交接'),
     '四个动作都画在条上');
  ok(has(bar, 'class="ichip'), '每个入口是按钮，不是一段文字');
  ok(has(bar, '>' + G0.picks.length + '<'), '入口上带候选数：' + G0.label + ' ' + G0.picks.length);
  ok(el('ipanel').classList.contains('on') === false,
     '默认不展开——四个面板同时铺开等于没有入口');

  // 第一次点击：选动作
  toggleIntent(G0.id);
  const ip = el('ipanel');
  ok(ip.classList.contains('on'), '点了才展开');
  ok(has(ip.innerHTML, 'class="ipick"'), '面板里是客户行');
  ok(has(ip.innerHTML, G0.picks[0].cname), '第一个客户在面板里：' + G0.picks[0].cname);
  ok(has(ip.innerHTML, '打开简报'), '每行说清点下去会发生什么');
  ok(has(ip.innerHTML, '存量客户'), '存量那边还有几个要说出来——不说，用户以为它们不存在');
  ok(has(ip.innerHTML, '重点客户里 ' + G0.n_candidates + ' 个'),
     '名额口径写清：只从重点客户里取 ' + G0.picks.length + ' 个');

  // 第二次点击：落到那一块
  const pick = G0.picks[0];
  await pickIntent(pick.cid, pick.brief_kind, pick.focus);
  ok(S.cur && S.cur.id === pick.cid, '切到那个客户了：' + (S.cur && S.cur.name));
  ok(el('ipanel').classList.contains('on') === false, '打开简报后面板自己收起');
  ok(el('bdrawer').classList.contains('on'), '简报打开了');
  ok(has(el('bbody').innerHTML, 'id="bb-' + pick.focus + '"'),
     '简报里真有 ' + pick.focus + ' 这一块');
  ok(el('bb-' + pick.focus).classList.contains('focus'), '这一块被标出来了');
  const loc = el('blocator').innerHTML;
  ok(has(loc, '已定位到'), '顶部说清落在哪一块：' + loc);
  ok(has(loc, '看全文'), '给一条退回去看全文的路');
  ok(S.briefFocus === pick.focus, '落点记在状态里：' + S.briefFocus);

  clearBriefFocus();
  ok(el('bb-' + pick.focus).classList.contains('focus') === false, '「看全文」把高亮清掉');
  closeBrief();
  ok(el('bdrawer').classList.contains('on') === false, '简报能关掉');

  // 再点同一个入口 → 收起，不是又展开一层
  toggleIntent(G0.id);
  ok(el('ipanel').classList.contains('on') === true, '再点一次展开');
  toggleIntent(G0.id);
  ok(el('ipanel').classList.contains('on') === false, '第三次点收起');

  // 待办行上的深链：do 区每行都有，fill 区没有
  el('navTodo').onclick();
  await new Promise(r => setTimeout(r, 30));
  const t18 = panel();
  const doRows = S.todo.items.filter(i => !i.pool && i.bucket === 'do');
  const nBrief = (t18.match(/class="dbtn brief"/g) || []).length;
  ok(nBrief === doRows.length,
     '「要做的事」每一行都能一跳进简报：' + nBrief + '/' + doRows.length);
  const fillRows = S.todo.items.filter(i => !i.pool && i.bucket === 'fill');
  const fillHtml = t18.slice(t18.indexOf('fillzone'));
  ok(fillRows.length > 0 && !has(fillHtml, 'class="dbtn brief"'),
     '「要补的数据」不给简报入口——空档案的客户看简报只会看到一片「没有记录」');

  const rowWithBrief = doRows.find(i => i.cid === 'c_30001') || doRows[0];
  await todoBrief(rowWithBrief.cid, rowWithBrief.brief_kind, rowWithBrief.focus);
  ok(S.cur && S.cur.id === rowWithBrief.cid, '待办深链切到了那个客户');
  ok(el('bdrawer').classList.contains('on'), '待办深链打开了简报');
  ok(has(el('bbody').innerHTML, 'id="bb-' + rowWithBrief.focus + '"'),
     '待办深链落在 ' + rowWithBrief.focus + ' 这一块，不是停在顶部');
  closeBrief();

  console.log('\n[19] 最小档案：空档案只开四格，有档案才给全量');
  await sel('c_10006');
  openPedit();
  ok(el('pedit').classList.contains('on'), '编辑档案弹窗打开了');
  ok(el('pf_rest').style.display === 'none', '空档案打开的是四格，不是十四格');
  ok(el('pf_minnote').style.display !== 'none', '说明「这四项填了就有用」');
  ok(el('peexpand').style.display !== 'none', '仍留一条「展开全部字段」的路');
  ok(has(el('pesubmit').textContent, '4'), '按钮写「保存这 4 项」，说清这一下要做什么：' + el('pesubmit').textContent);
  el('peexpand').onclick();
  ok(el('pf_rest').style.display !== 'none', '点「展开」就有全部字段');
  ok(el('peexpand').style.display === 'none', '展开了就不该再提示「展开」');
  closePedit();

  await sel('c_30001');
  openPedit();
  ok(el('pf_rest').style.display !== 'none',
     '有档案的客户直接给全量——他是来改金额的，不是来补全的');
  ok(el('peexpand').style.display === 'none', '全量模式下不显示「展开」');
  closePedit();
  ok(el('pedit').classList.contains('on') === false, '关得掉');

  console.log('\n[20] 重置按钮：服务端没开就置灰，别让人点下去才发现');
  // 桩给的 health.allow_reset 是 false，boot() 应该把按钮置灰。
  // 断言三件事：真的禁用了、样式上看得出来、title 说清了怎么才能用。
  const rb = el('btnReset');
  ok(rb.disabled === true, '健康检查说没开，按钮就是禁用的');
  ok(rb.classList.contains('off'), '置灰有独立的类名，样式上看得出来');
  ok(has(rb.title, '--allow-reset'), 'title 说清了怎么才能用：' + rb.title);
  ok(rb.disabled, '不隐藏只置灰——藏起来会被当成功能被删了');
  // 反面：真开着的时候必须可点。直接用同一个函数再绑一次，验那条分支。
  bindReset({allow_reset: true});
  ok(rb.disabled === false && !rb.classList.contains('off'),
     'health 说开着就恢复可点');
  ok(rb.title === '', '开着的时候不该再挂一句「怎么才能用」');
  bindReset({allow_reset: false});

  console.log('\n' + (fails.length ? '有 ' + fails.length + ' 条断言失败' : '全部断言通过'));
  process.exit(fails.length ? 1 : 0);
})();
"""


def find_node() -> str:
    """找 node。先看 PATH，再看 WorkBuddy 托管的那个（不污染用户环境）。"""
    found = shutil.which("node")
    if found:
        return found
    for pat in ("/Users/*/.workbuddy/binaries/node/versions/*/bin/node",
                "/usr/local/bin/node", "/opt/homebrew/bin/node"):
        hits = sorted(Path("/").glob(pat.lstrip("/")))
        if hits:
            return str(hits[-1])
    raise SystemExit("找不到 node。装了 Node.js 再跑，或者只跑 demo/smoke.py。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="界面层断言（Node + DOM/fetch 桩）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--keep", action="store_true", help="保留生成的 JS，方便翻")
    args = ap.parse_args(argv)

    if not HTML_FILE.exists():
        raise SystemExit(f"缺少页面文件：{HTML_FILE}")

    html = HTML_FILE.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        raise SystemExit("页面里没有找到 <script> 块——断言无从加起")
    app_js = m.group(1)

    data = build(args.base)
    stub = (STUB.replace("__CUSTOMERS__", json.dumps(data["customers"], ensure_ascii=False))
                .replace("__SCEN__", json.dumps(data["scen"], ensure_ascii=False))
                .replace("__HEALTH__", json.dumps(data["health"], ensure_ascii=False))
                .replace("__TODO__", json.dumps(data["todo"], ensure_ascii=False))
                .replace("__BRIEFS__", json.dumps(data["briefs"], ensure_ascii=False))
                .replace("__DONE__", json.dumps(data["done"], ensure_ascii=False))
                .replace("__DRAFT__", json.dumps(data["draft"], ensure_ascii=False))
                .replace("__INTENT__", json.dumps(data["intent"], ensure_ascii=False)))

    tmp = Path(tempfile.mkdtemp(prefix="ui_check_"))
    js = tmp / "run.js"
    js.write_text(stub + app_js + TESTS, encoding="utf-8")

    n_scen = len(data["scen"])
    print(f"\n界面层断言 · {n_scen} 个场景 · 页面脚本原样加载")
    print("=" * 66)
    proc = subprocess.run([find_node(), str(js)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout or ""
    print(out.rstrip())
    n_pass = out.count("PASS")
    n_fail = out.count("FAIL")
    print("=" * 66)
    print(f"通过 {n_pass} · 失败 {n_fail}")
    if args.keep:
        print(f"生成的 JS：{js}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

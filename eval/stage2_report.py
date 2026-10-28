"""Stage 2 离线报告：把黄金语料的事实灌进聚合层与建议层，看输出是否说得过去。

用金标准事实而不是模型输出，是为了把「聚合+建议」的质量和「抽取」的质量分开看。
金标准是理想输入，此时卡片若仍不合理，问题一定在 Stage 2 自己身上；
反过来，拿模型输出跑出来的问题，不能直接归罪于聚合层。

用法：
  python3 eval/stage2_report.py                     # 汇总 + 三个代表性客户的详情
  python3 eval/stage2_report.py --customer c_10001  # 看单个客户
  python3 eval/stage2_report.py --all               # 全部客户明细
  python3 eval/stage2_report.py --json out/cards.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.aggregation.card import CustomerProfile, build_card  # noqa: E402
from src.advice.engine import generate_advice  # noqa: E402
from src.extraction.schema import compute_confidence  # noqa: E402
from src.extraction.validator import normalize_fact  # noqa: E402
from src.pipeline import to_fact  # noqa: E402

GOLDEN = ROOT / "eval" / "golden" / "golden_set.jsonl"

#: 评测基准日，全仓唯一来源。
#:
#: 曾经踩过的坑：stage2_report 默认 10-24，而 p0_audit 自己算「文档最大日期」得到 10-22，
#: 于是同一个金标准在两个工具里分别得出 18 条和 16 条 P0。差的两条都是到期日落在
#: 10-22 到 10-24 之间的承诺（如「本周五前发承诺函」到期 10-23）——
#: 基准日不同，「已逾期」的判断就不同。数字对不上时很难想到根因是这里。
#:
#: 任何按「今天」判断逾期/临近的地方都必须引用它，不要再各自推算。
EVAL_TODAY = date(2026, 10, 24)

#: 语料没有客户档案字段，从正文里粗提一个名字，提不到就用编号。
_NAME_PATTERNS = (
    re.compile(r"去([\u4e00-\u9fa5]{2,8})[，,。]"),
    re.compile(r"走访([\u4e00-\u9fa5]{2,8})[（(。]"),
    re.compile(r"拜访([\u4e00-\u9fa5]{2,8})[，,。]"),
    re.compile(r"([\u4e00-\u9fa5]{2,8})(?:项目启动会|月度沟通会)"),
)


def guess_name(text: str, customer_id: str) -> str:
    for pat in _NAME_PATTERNS:
        m = pat.search(text or "")
        if m:
            name = m.group(1)
            # 「去客户那边」这类不是名字，过滤掉
            if not name.startswith(("客户", "现场", "他们")):
                return name
    return f"客户{customer_id[-3:]}"


def load_customers(path: Path = GOLDEN) -> dict:
    """按客户聚合金标准事实，并补齐文档上下文字段。

    金标准里事实不带 source_type / source_id / confidence —— 这些在文档层。
    normalize_fact 负责把它们注入并重算置信度，这样 Stage 2 拿到的
    与真实管道产出同构，不会出现「报告里好看、接上模型就崩」。
    """
    docs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    customers: dict[str, dict] = {}

    for doc in docs:
        cid = doc["customer_id"]
        bucket = customers.setdefault(cid, {"docs": [], "facts": [], "text": doc["text"]})

        for i, g in enumerate(doc.get("gold_facts", [])):
            ctx = {
                "customer_id": cid,
                "source_id": doc["doc_id"],
                "source_type": doc["source_type"],
                "recorded_at": doc.get("recorded_at"),
                "occurred_at": doc["occurred_at"],
                "current_user_id": "me",
            }
            raw = dict(g)
            # 金标准事实的 id 必须跨进程稳定。
            #
            # 原来这里不填 fact_id，交给 normalize_fact 生成 ULID，于是
            # 「昨天标好的 P0 标注」和「今天加载出来的事实」id 全对不上——
            # 一份人工标注隔天就失效，而且失效得很安静：审计会报「命中 0」，
            # 看起来像模型全错，实际是标尺和被测物换了个坐标系。
            # 用「文档 id#序号」做 id：可读、可回溯到原文，且天然唯一。
            raw.setdefault("fact_id", f"{doc['doc_id']}#{i}")
            raw["customer_id"] = cid
            raw["source_type"] = doc["source_type"]
            raw["source_id"] = doc["doc_id"]
            raw["lang"] = "zh-CN"
            raw.setdefault("stated_at", doc["occurred_at"])
            raw.setdefault("stated_at_precision", "day")
            raw["recorded_at"] = doc.get("recorded_at") or doc["occurred_at"]

            fact = normalize_fact(raw, ctx)
            conf = fact.pop("_recomputed_confidence", None)
            if conf is None:
                p = fact.get("payload") or {}
                conf = compute_confidence(
                    fact["fact_type"], fact["source_type"],
                    fact.get("confidence_basis") or "paraphrase", p,
                )
            fact["confidence"] = conf if conf is not None else 0.5
            fact.pop("_enum_repaired", None)
            bucket["facts"].append(to_fact(fact))

        bucket["docs"].append(doc["doc_id"])

    return customers


def load_predictions(
    path: Path = ROOT / "out" / "pred_final.jsonl",
    golden_path: Path = GOLDEN,
) -> dict:
    """用模型的真实输出聚合成客户。

    与 load_customers 的区别只在事实来源：那边是标注的金标准（理想输入），
    这边是 glm-4-flash 实际抽出来的东西（真实噪声）。同一套 Stage 2 跑两遍，
    差值就是「抽取质量对认知卡的影响」——这个数字比抽取 F1 更能说明产品能不能用。
    """
    docs = {
        json.loads(line)["doc_id"]: json.loads(line)
        for line in golden_path.read_text().splitlines() if line.strip()
    }
    customers: dict[str, dict] = {}

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        pred = json.loads(line)
        doc = docs.get(pred["doc_id"])
        if doc is None:
            continue
        cid = doc["customer_id"]
        bucket = customers.setdefault(cid, {"docs": [], "facts": [], "text": doc["text"]})
        bucket["docs"].append(doc["doc_id"])
        for f in pred.get("facts") or []:
            bucket["facts"].append(to_fact(f))

    return customers


def make_profile(cid: str, info: dict) -> CustomerProfile:
    return CustomerProfile(
        customer_id=cid,
        name=guess_name(info["text"], cid),
        industry="",
        stage="",
        owner_id="me",
        as_of="2026-10-24",
    )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def print_summary(rows: list[dict]) -> None:
    head = f"{'客户':<12}{'记录':>4}{'事实':>4}{'归并':>4}{'冲突':>4}{'演进':>4}{'建议':>4}{'P0':>3}  结论"
    print(head)
    print("-" * len(head))
    for r in rows:
        flag = "!" if r["thin"] else " "
        print(
            f"{flag}{r['name']:<11}{r['n_docs']:>4}{r['n_facts']:>4}{r['n_groups']:>4}"
            f"{r['n_conflict']:>4}{r['n_evolution']:>4}{r['n_advice']:>4}{r['n_p0']:>3}  "
            f"{r['verdict'][:40]}"
        )


def print_card(view) -> None:
    card, advice = view.card, view.advice
    print(f"\n{'=' * 78}")
    print(f"{card.profile.name}（{card.profile.customer_id}）")
    print(f"事实 {card.stats.get('n_facts')} 条 / 归并 {card.stats.get('n_groups')} 组 / "
          f"冲突 {card.stats.get('n_conflict_groups')} 组")
    print(f"结论：{card.verdict}")

    if advice.advice:
        print("\n现在该做的事：")
        for a in advice.advice:
            print(f"  [{a.priority}] {a.title}")
            print(f"         {a.why}")
            if a.basis:
                print(f"         依据 {len(a.basis)} 条事实 · {','.join(a.basis[:3])}")
    else:
        print("\n现在该做的事：无（规则一条都没命中）")

    if card.gaps:
        print("\n空白：" + "、".join(card.gaps))

    print("\n事实分组：")
    for s in card.sections:
        print(f"  {s.title} · {s.n}")
        for g in s.groups[:3]:
            h = g.head
            mark = ""
            if g.has_conflict:
                mark = "  <冲突 " + "/".join(g.conflict_fields) + ">"
            elif h.status == "superseded":
                mark = "  <已被取代>"
            elif h.status == "expired":
                mark = "  <过保鲜期>"
            print(f"    {g.effective_confidence:.2f} {h.statement[:36]}{mark}")


# ---------------------------------------------------------------------------

def run_all(customers: dict, today: date) -> tuple[list[dict], dict]:
    """跑完所有客户，返回汇总行与视图。"""
    from src.pipeline import CustomerView

    rows = []
    views = {}
    for cid, info in customers.items():
        profile = make_profile(cid, info)
        card = build_card(profile, info["facts"], today=today)
        advice = generate_advice(card, today=today)
        view = CustomerView(card=card, advice=advice)
        views[cid] = view
        rows.append({
            "customer_id": cid,
            "name": profile.name,
            "n_docs": len(info["docs"]),
            "n_facts": card.stats.get("n_facts", 0),
            "n_groups": card.stats.get("n_groups", 0),
            "n_conflict": card.stats.get("n_conflict_groups", 0),
            "n_evolution": card.stats.get("n_evolutions", 0),
            "n_advice": len(advice.advice),
            "n_p0": advice.stats.get("n_p0", 0),
            "verdict": card.verdict,
            "thin": "信息不足" in card.verdict,
            "verdict_kind": "thin" if card.is_thin else "normal",
        })
    rows.sort(key=lambda r: (-r["n_p0"], -r["n_advice"], r["customer_id"]))
    return rows, views


def print_compare(gold: tuple, pred: tuple) -> dict:
    """金标准 vs 模型输出：抽取质量如何一路影响到「该做什么」。

    这个对比比抽取 F1 更能回答产品问题——F1 掉 0.05 听起来无关痛痒，
    但 P0 建议掉一半，意味着有一半该立刻处理的事没人处理。

    返回一份结构化数字：文档里引用的「104→94」「22→15」必须能一条命令复现，
    不能靠人肉抄。`--json` 就是把这几个数落盘。
    """
    grow, gview = gold
    prow, pview = pred

    g_rule, p_rule = Counter(), Counter()
    for v in gview.values():
        for a in v.advice.advice:
            g_rule.update(a.tags or [a.rule_id])
    for v in pview.values():
        for a in v.advice.advice:
            p_rule.update(a.tags or [a.rule_id])

    def n_p0(rows):
        return sum(r["n_p0"] for r in rows)

    print("\n" + "=" * 62)
    print("金标准 vs 模型输出：抽取质量如何传导到认知卡")
    print("=" * 62)
    print(f"{'指标':<14}{'金标准':>9}{'模型输出':>10}{'变化':>10}")
    print("-" * 62)
    pairs = [
        ("事实总数", sum(r["n_facts"] for r in grow), sum(r["n_facts"] for r in prow)),
        ("归并组数", sum(r["n_groups"] for r in grow), sum(r["n_groups"] for r in prow)),
        ("信息不足客户", sum(1 for r in grow if r["thin"]), sum(1 for r in prow if r["thin"])),
        ("建议总数", sum(r["n_advice"] for r in grow), sum(r["n_advice"] for r in prow)),
        ("P0 建议", n_p0(grow), n_p0(prow)),
        ("P1 建议", sum(1 for v in gview.values() for a in v.advice.advice if a.priority == "P1"),
                     sum(1 for v in pview.values() for a in v.advice.advice if a.priority == "P1")),
    ]
    for label, g, p in pairs:
        delta = f"{p - g:+d}"
        if g:
            delta += f"（{(p - g) / g * 100:+.0f}%）"
        print(f"{label:<14}{g:>9}{p:>10}{delta:>10}")

    print("\nP0 规则命中对比（这些是「不做事就出事」的项）：")
    p0_rules = ["commitment_overdue", "concern_blocker", "deadline_imminent",
                "deadline_missed", "conflict_unresolved", "need_blocking_requirement"]
    print(f"{'规则':<24}{'金标准':>8}{'模型输出':>10}")
    for rule in p0_rules:
        print(f"  {rule:<22}{g_rule.get(rule, 0):>8}{p_rule.get(rule, 0):>10}")

    lost = [r for r in grow if r["n_p0"] > 0]
    lost = [r for r in lost if next((x["n_p0"] for x in prow if x["customer_id"] == r["customer_id"]), 0) == 0]
    if lost:
        print(f"\n模型输出下 P0 归零的客户（金标准里本有 P0）：{len(lost)} 个")
        for r in lost[:8]:
            print(f"  {r['name']}：金标准 {r['n_p0']} 条 P0 → 模型 0 条")

    out = {label: {"gold": g, "pred": p} for label, g, p in pairs}
    out["p0_zeroed_customers"] = {
        "n": len(lost),
        "ids": [r["customer_id"] for r in lost],
    }
    out["p0_by_rule"] = {rule: {"gold": g_rule.get(rule, 0), "pred": p_rule.get(rule, 0)}
                         for rule in p0_rules}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", help="只看某个客户")
    ap.add_argument("--all", action="store_true", help="打印全部客户明细")
    ap.add_argument("--json", help="导出完整结果")
    ap.add_argument("--today", default=EVAL_TODAY.isoformat())
    ap.add_argument("--source", choices=["gold", "pred"], default="gold",
                    help="gold=金标准事实（理想输入）；pred=模型真实输出（真实噪声）")
    ap.add_argument("--pred-file", default=str(ROOT / "out" / "pred_final.jsonl"))
    ap.add_argument("--compare", action="store_true",
                    help="对比金标准与模型输出，看抽取质量如何传导到建议")
    args = ap.parse_args()

    today = date.fromisoformat(args.today)
    if args.source == "pred":
        customers = load_predictions(Path(args.pred_file))
        src_note = f"模型输出 {args.pred_file}"
    else:
        customers = load_customers()
        src_note = "金标准事实（理想输入）"

    rows, views = run_all(customers, today)

    if args.compare:
        # 同一套 Stage 2 跑两遍输入，差值就是「抽取质量对决策的影响」
        gold = run_all(load_customers(), today)
        pred = run_all(load_predictions(Path(args.pred_file)), today)
        stats = print_compare(gold, pred)
        if args.json:
            out = {"today": str(today), "golden": str(GOLDEN),
                   "pred_file": str(args.pred_file), "compare": stats}
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
            print(f"\n已导出 {args.json}")
        return 0

    print(f"语料 {len(customers)} 个客户 / {sum(len(v['docs']) for v in customers.values())} 篇记录 / "
          f"{sum(r['n_facts'] for r in rows)} 条事实   基准日 {today}")
    print(f"事实来源：{src_note}")
    print()
    print_summary(rows if args.all else rows[:12])
    if not args.all and len(rows) > 12:
        print(f"  … 另有 {len(rows) - 12} 个客户未显示（--all 看全部）")

    # --- 全局统计 ---
    rule_counter: Counter = Counter()
    for v in views.values():
        for a in v.advice.advice:
            rule_counter.update(a.tags or [a.rule_id])
    print("\n规则命中分布（按建议条数）：")
    for rule, n in rule_counter.most_common():
        print(f"  {rule:<28}{n:>4}")

    thin = sum(1 for r in rows if r["thin"])
    print(f"\n信息不足（不敢下结论）的客户：{thin}/{len(rows)}")
    conf = sum(1 for r in rows if r["n_conflict"])
    evo = sum(1 for r in rows if r["n_evolution"])
    print(f"出现冲突的客户：{conf}    出现演进的客户：{evo}")

    # --- 明细 ---
    if args.customer:
        if args.customer not in views:
            print(f"\n没有这个客户：{args.customer}")
            return 1
        print_card(views[args.customer])
    elif args.all:
        for r in rows:
            print_card(views[r["customer_id"]])
    else:
        # 默认挑三个：信息最全的、有冲突的、信息最少的
        picks = [rows[0]["customer_id"]]
        for r in rows:
            if r["n_conflict"] and r["customer_id"] not in picks:
                picks.append(r["customer_id"])
                break
        for r in reversed(rows):
            if r["thin"] and r["customer_id"] not in picks:
                picks.append(r["customer_id"])
                break
        for cid in picks:
            print_card(views[cid])

    if args.json:
        out = {
            "today": str(today),
            "summary": rows,
            "views": {cid: v.to_dict() for cid, v in views.items()},
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"\n已导出 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

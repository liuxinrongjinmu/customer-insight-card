#!/usr/bin/env python3
"""把示例文本的模型原始输出预先写进 `demo/cache/extract.jsonl`。

为什么必须有这一步：演示现场断网、额度用尽、模型抽风，任何一种都会让
「贴一段新记录」这个镜头直接崩掉。而这一段是整场演示唯一能证明
「它真的在算」的地方，不能赌网络。

预跑一次之后，即使完全离线（`python3 demo/serve.py --offline`），
这三段示例也照样能抽出正确结果——页面顶部会明示「离线回放」，
不冒充实时推理。

用法：
    python3 demo/seed_cache.py              # 补齐缺失的示例
    python3 demo/seed_cache.py --force      # 全部重跑（改了提示词/示例后）
    python3 demo/seed_cache.py --provider glm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.samples import SAMPLES  # noqa: E402
from demo.serve import App  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="预热示例文本的抽取缓存")
    ap.add_argument("--force", action="store_true", help="已缓存也重跑")
    ap.add_argument("--provider", default=None)
    args = ap.parse_args(argv)

    app = App(offline=False, provider=args.provider)
    info = app.health()
    if info["offline"]:
        print("没有可用的模型配置，无法预热。检查 .env 里的 API Key。", file=sys.stderr)
        return 1

    print(f"模型 {info['model']}（{info['provider']}）· 现有缓存 {info['cache_size']} 条\n")

    if args.force:
        # 缓存按「完整提问」哈希，改了提示词自然就会 miss，
        # 这里只处理「文本没变、但想重新抽一遍」的情况。
        app.cache.data.clear()

    rc = 0
    for s in SAMPLES:
        d = app.extract({
            "text": s["text"],
            "doc_date": s["doc_date"],
            "source_type": s["source_type"],
            "customer_name": s["customer_name"],
        })
        flag = "缓存命中" if d["source"] != "live" else "已写入"
        print(f"[{flag}] {s['title']} → {d['n_facts']} 条事实 · 拒收 {d['n_rejected']} · {d['ms']}ms")
        for f in d["fact_data"]:
            p = f["payload"]
            extra = []
            for k in ("due_date", "target_date", "competitor_name", "severity"):
                if p.get(k):
                    extra.append(f"{k}={p[k]}")
            tail = ("　" + " ".join(extra)) if extra else ""
            print(f"    - {f['fact_type']}: {f['statement'][:38]}{tail}")
        if d["n_facts"] == 0 and s["id"] != "s3":
            # s3 是故意做成 0 条的（信息稀少），其余为 0 说明提示词或模型出了问题
            print("    !! 这条按设计应当抽得出事实，检查提示词或模型", file=sys.stderr)
            rc = 1
        print()

    print(f"完成。缓存 {app.cache.size} 条 → {HERE / 'cache' / 'extract.jsonl'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

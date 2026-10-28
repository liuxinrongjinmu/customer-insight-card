"""提示词组装。

把系统提示词、少样本示例与待抽文档拼成 messages 列表。
不依赖任何 SDK，返回 OpenAI 风格的消息结构，由 adapter 负责发送。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

# PROMPT_DIR 允许被环境变量覆盖，用于提示词消融实验：
# 把候选版本的 system_zh.md / examples_zh.json 放进一个临时目录，
# 跑评测时指向它即可，正式文件不动，避免「实验时改了忘了改回来」。
PROMPT_DIR = Path(os.environ.get("PROMPT_DIR") or (Path(__file__).parent / "prompts"))

USER_TEMPLATE = """<document>
source_type: {source_type}
occurred_at: {occurred_at}
customer_id: {customer_id}
---
{text}
</document>"""

EXAMPLE_TEMPLATE = """<example>
<document>
source_type: {source_type}
occurred_at: {occurred_at}
customer_id: {customer_id}
---
{text}
</document>
<output>
{output}
</output>
</example>"""


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    return (PROMPT_DIR / "system_zh.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_examples() -> list[dict[str, Any]]:
    raw = (PROMPT_DIR / "examples_zh.json").read_text(encoding="utf-8")
    return json.loads(raw)


def format_document(doc: dict[str, Any]) -> str:
    return USER_TEMPLATE.format(
        source_type=doc.get("source_type", "visit_note"),
        occurred_at=doc.get("occurred_at", "unknown"),
        customer_id=doc.get("customer_id", "unknown"),
        text=(doc.get("text") or "").strip(),
    )


def format_example(example: dict[str, Any]) -> str:
    doc = example["document"]
    output = json.dumps(example.get("output", []), ensure_ascii=False, indent=2)
    return EXAMPLE_TEMPLATE.format(
        source_type=doc.get("source_type", "visit_note"),
        occurred_at=doc.get("occurred_at", "unknown"),
        customer_id=doc.get("customer_id", "unknown"),
        text=(doc.get("text") or "").strip(),
        output=output,
    )


def build_messages(
    doc: dict[str, Any],
    include_examples: bool = True,
    max_examples: int | None = None,
) -> list[dict[str, str]]:
    """构造 messages。

    示例放在 user 消息里而非多轮对话，原因是单轮结构更容易做缓存与批量评测，
    且示例与真实输入在同一上下文中，模型更容易对齐格式。
    """
    system = load_system_prompt()

    parts: list[str] = []
    if include_examples:
        examples = load_examples()
        if max_examples is not None:
            examples = examples[:max_examples]
        if examples:
            parts.append("# 示例\n")
            parts.extend(format_example(ex) for ex in examples)

    parts.append("# 待抽取文档\n")
    parts.append(format_document(doc))
    parts.append("\n只输出 JSON 数组。")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


if __name__ == "__main__":
    demo_doc = {
        "source_type": "visit_note",
        "occurred_at": "2026-10-24",
        "customer_id": "c_00001",
        "text": "客户希望系统支持单点登录，我们答应下周三前给方案。",
    }
    msgs = build_messages(demo_doc)
    for m in msgs:
        print(f"=== {m['role']} ({len(m['content'])} tokens approx) ===")
        print(m["content"][:600])
        print()

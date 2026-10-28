"""LLM 适配器与输出解析。

零第三方依赖：HTTP 走标准库 urllib，兼容 OpenAI 风格的 /chat/completions 接口。
本地调试可用 MockAdapter 或 EchoAdapter，无需真实模型即可跑通评测链路。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class LLMAdapter(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json_array(text: str) -> list[dict[str, Any]]:
    """从模型输出中稳健地取出 JSON 数组。

    依次尝试：整段解析 -> 去代码围栏 -> 取首个 [ 到末个 ] -> 单个对象包成数组。
    全部失败时抛 ValueError，由调用方转换为死信。
    """
    if not text or not text.strip():
        raise ValueError("模型输出为空")

    candidates: list[str] = []
    raw = text.strip()
    candidates.append(raw)

    fenced = _FENCE.search(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())

    start, end = raw.find("["), raw.rfind("]")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            # 容忍 {"facts": [...]} 这类包裹
            for key in ("facts", "data", "result", "items"):
                if isinstance(data.get(key), list):
                    return [d for d in data[key] if isinstance(d, dict)]
            return [data]

    raise ValueError(f"无法从输出中解析 JSON 数组，前 200 字符：{raw[:200]!r}")


# ---------------------------------------------------------------------------
# 适配器实现
# ---------------------------------------------------------------------------

@dataclass
class MockAdapter:
    """固定返回。用于单元测试与评测框架自检。"""
    response: str = "[]"

    def complete(self, messages: list[dict[str, str]]) -> str:
        return self.response


class OpenAICompatAdapter:
    """调用 OpenAI 兼容接口。

    环境变量：
      LLM_BASE_URL  默认 https://api.openai.com/v1
      LLM_API_KEY   必填
      LLM_MODEL     默认 gpt-4o-mini
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        timeout: int = 180,
        max_retries: int = 4,
    ) -> None:
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise RuntimeError("缺少 LLM_API_KEY 环境变量")

        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "response_format": {"type": "json_object"}
                if os.environ.get("LLM_JSON_MODE", "0") == "1"
                else None,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        # 去掉 None 字段，避免部分服务端拒绝
        body = re.sub(rb',\s*"response_format":\s*null', b"", body)

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:500]
                # 4xx 里只有 429 值得重试；其余（401 密钥错、404 模型名错、400 参数错）
                # 重试一百次也是同一个结果，立刻失败才能让人去改配置。
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(f"LLM 接口返回 {exc.code}：{detail}") from exc
                last_error = RuntimeError(f"LLM 接口返回 {exc.code}：{detail}")
            except (
                urllib.error.URLError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                socket.timeout,
                TimeoutError,
            ) as exc:
                # 长 prompt + 高并发时，服务端常在已建立连接后掐断（IncompleteRead /
                # RemoteDisconnected）。这类失败重试就好，不该记成「这篇抽不出来」，
                # 否则会把网络抖动算成模型能力不足，评测结果直接失真。
                last_error = exc

            if attempt < self.max_retries:
                # 指数退避 + 抖动：并发 6 时若同时重试，容易再次撞上限流。
                delay = min(0.8 * (2 ** attempt), 8.0) * (0.5 + random.random())
                time.sleep(delay)

        if last_error is not None:
            raise RuntimeError(f"重试 {self.max_retries} 次后仍失败：{last_error}") from last_error

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"响应结构异常：{str(data)[:300]}") from exc


class ReplayAdapter:
    """回放适配器：用缓存的模型原始输出重跑管道。

    存在的理由很实际：改校验规则、改指标口径时，模型输出没变，
    但每次都要重新调 API——费钱、费时，而且模型本身有随机性，
    重跑会把「管道改动带来的差值」和「采样噪声」混在一起。
    把原始输出落盘后回放，能把这两者彻底分开。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.cache: dict[str, str] = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("raw_output"):
                    self.cache[row["doc_key"]] = row["raw_output"]

    def complete(self, messages: list[dict[str, str]]) -> str:
        key = document_key(messages)
        if key not in self.cache:
            raise RuntimeError(
                f"回放缓存缺失 {key}。若刚改过提示词或示例，这是正常现象——"
                f"提问变了，旧缓存不再适用，需要用 --adapter openai 重新生成。"
            )
        return self.cache[key]


_DOC_BLOCK = re.compile(r"<document>.*?---\s*\n(.*?)\n</document>", re.S)


def document_key(messages: str | list[dict[str, str]]) -> str:
    """以**完整提问内容**为键：system 提示词 + user 消息（含示例 + 文档正文）。

    早期版本只哈希「是否带示例 + 文档正文」，是个会静默出错的实现：
    改了示例内容或改了提示词，模型看到的提问已经变了，但键没变，
    回放会照样返回旧答案。后果是「改提示词的收益」在实验里恒等于 0，
    或者更糟——看起来是正的，实际只是两次跑命中了同一批缓存。

    这个 bug 不会报错，只会让所有基于 replay 的对比失去意义，所以键必须
    覆盖一切会进入模型的东西。
    """
    if isinstance(messages, str):
        payload = messages
    else:
        payload = "\n".join(
            f"{m.get('role', '')}:{m.get('content', '')}" for m in messages
        )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def build_adapter(name: str = "auto") -> LLMAdapter:
    """按名称构造适配器。auto 时若配置了 API Key 则走真实模型，否则用 Mock。"""
    if name == "mock":
        return MockAdapter()
    if name.startswith("replay:"):
        return ReplayAdapter(name.split(":", 1)[1])
    if name in ("openai", "auto"):
        if os.environ.get("LLM_API_KEY"):
            return OpenAICompatAdapter()
        if name == "auto":
            return MockAdapter()
        raise RuntimeError("openai 适配器需要 LLM_API_KEY")
    raise ValueError(f"未知适配器：{name}")

#!/usr/bin/env python3
"""本地可演示 Demo 的服务端：让认知卡「能算」。

原型是一台浏览器——数据在构建时写死在 HTML 里。这里是把它变成工具的第一步：
页面上看到的每一条事实，要么来自模型的实时抽取，要么来自回放缓存，
但都经过同一条管道，并且**页面顶部每次都会说清楚是哪一种**。

    起法：  python3 demo/serve.py
            python3 demo/serve.py --offline          # 只用缓存，不联网
            python3 demo/serve.py --provider deepseek
            python3 demo/serve.py --port 8080
            python3 demo/serve.py --db /tmp/x.db     # 换一个库文件
            python3 demo/serve.py --user u_002       # 换一个身份（归属隔离用）
            python3 demo/serve.py --allow-reset      # 打开 /api/reset（默认关）

接口（完整清单，与 docs/archive/demo-plan-2026-10.md §9.15 一致）：

    GET  /api/health       → {provider, model, offline, as_of, ...}
    GET  /api/customers    → 预置客户 + 已抽取的记录，每户带一份算好的视图
    POST /api/extract      → {text, doc_date, customer_id?, source_type} → 事实
    POST /api/analyze      → {customer_id, fact_data, today} → 视图（毫秒级，可反复调）
    GET  /api/todo         → 跨客户待办（?today= 可指定基准日）
    POST /api/brief        → {customer_id, kind, today} → 拜访简报 / 方案准备 / 交接说明
    POST /api/done         → {customer_id, aid, today, undo?} → 标已办 / 撤销
    POST /api/profile      → {customer_id, profile, who?} → 改档案（进变更日志）
    POST /api/upload       → {filename, data(base64)} → 提取文档文本（txt/md/csv/docx）
    POST /api/reset        → 把演示数据恢复到初始状态（**默认不挂载**，见下）
    GET  /api/feedback     → 引擎从你的修正里记住了什么（黑名单/改判/补丁建议）

零依赖：只用标准库。状态落在 `demo/cache/demo.db`（SQLite），进程退出不丢；
下次启动原样读回来。库里没有数据时按种子重灌一次。

`/api/reset` **默认不注册路由**，要 `--allow-reset` 才出现。理由：一个能让
数据消失的接口，默认必须是关的——危险得是一次主动动作，不能靠「有人记得
别配环境变量」来保护。冒烟脚本与界面断言起服务时会显式带上它。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import brief as bf  # noqa: E402
from demo import intents  # noqa: E402
from demo import minprofile  # noqa: E402
from demo import viewmodel as vm  # noqa: E402
from demo.accounts import POOL  # noqa: E402
from demo.build_demo import CUSTOMERS, TYPE_CN, to_facts  # noqa: E402
from demo.samples import SAMPLES  # noqa: E402
from eval.stage2_report import EVAL_TODAY  # noqa: E402
from src.envload import available_providers, load_dotenv, use_provider  # noqa: E402
from src.extraction.adapter import LLMAdapter, build_adapter, document_key  # noqa: E402
from src.extraction.extractor import extract_document  # noqa: E402
from src.extraction.prompt import build_messages  # noqa: E402
from src.aggregation import signals as sg  # noqa: E402
from src.feedback import corrections as fb_corr  # noqa: E402
from src.feedback import lessons as fb_lessons  # noqa: E402
from src.store import Repository, SchemaMismatch  # noqa: E402

HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "客户认知卡-演示.html"
CACHE_FILE = HERE / "cache" / "extract.jsonl"
#: 修正记录（P2-5）。跟演示数据放一起，`/api/reset` 会一起清——
#: 「恢复初始状态」如果留着上一轮的修正，下一次抽取会莫名少几条，
#: 那是最难解释的一类异常。
CORRECTIONS_FILE = HERE / "cache" / "corrections.jsonl"
#: 业务状态的落点（P2-3）。和上面两个一样放 `cache/`：都是运行期产物，
#: 不进版本库。为什么不叫 state：它跟缓存的区别只在于「丢了要不要紧」，
#: 而这个问题不该靠目录名回答——所以文件名直说它是演示库。
DB_FILE = HERE / "cache" / "demo.db"

#: 默认身份。**这不是登录，是归属**：v1 只回答「这条数据是谁的」，
#: 不回答「你是谁」。没有认证就没有权限，只有一个归属标签——
#: 所以它挡得住「两个人的数据混在一起分不清」，挡不住「有人冒充别人的 id」。
#: 真正的认证要另立一项，别把归属当权限用。
DEFAULT_USER = "u_demo"

#: 演示基准日。与 eval 侧同源，避免「页面上 10-24、评测里 10-26」这种对不上的情况。
AS_OF: date = EVAL_TODAY

#: 「这条我不做」的静默期。7 天是折中：客户情况会变（预算批了、决策人换了），
#: 永久压住等于系统从此装瞎；而每天重新提一遍，等于用户第一次说「不归我」时
#: 系统没听见。三个原因共用同一个期限——原因不同，过期重提的理由相同。
REFUSE_SILENCE_DAYS = 7

#: 「这条我不做」的理由。必须给原因，不能只给一个「不要了」的按钮：
#: 三种原因对系统的意义完全不同，将来要拿它反过来校准规则。
#:   invalid           这条事实本身错了（客户没这么说过 / 早就变了）
#:   not_mine          事实没错，但不归我跟（该转给别人）
#:   customer_declined 客户明确说了不做，别再推
REFUSE_REASONS = {
    "invalid": "这条不对，已经不成立了",
    "not_mine": "不归我跟，该转给别人",
    "customer_declined": "客户明确拒绝了",
}

#: 「还追」之后隔多久再问一次。
#:
#: 归因要解决的是「每天喊同一句话」——所以静默期必须比对逾期天数有分量。
#: 30 天意味着：说了「还在追」，一个月内不再提，一个月后如果还没兑现就再问一次。
#:
#: **不重置逾期天数。** 客户那边确实还没拿到东西，把天数清零是撒谎；
#: 重置的是「提醒周期」，不是「欠了多久」。这两个数在界面上分开写，
#: 免得用户以为点一下「还在追」账就平了。
CHASE_SILENCE_DAYS = 30

#: 逾期承诺的归因选项。「还追」是暂时压住，「客户不需要了」是关账，
#: 「转内部」是从客户账上挪走——三种在业务上完全不同，所以不能合成一个「知道了」。
ATTRIBUTE_REASONS = {
    "chasing": "还在追，给我一个月",
    "not_needed": "客户已经不需要了，关掉",
    "internal": "转成内部事项，不再算客户欠账",
}


def extract_docx_text(data: bytes) -> str:
    """从 .docx 文件里抽出纯文本。零依赖——docx 就是 zip，document.xml 在里面。

    只抽 <w:t> 节点的文字，跳过格式、图片、页眉页脚。
    表格会丢结构（只留文字），但对「抽取事实」这个用途够了：
    我们要的是文字内容，不是版式。
    """
    import zipfile
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml_bytes = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ApiError(400, "这个文件不是合法的 .docx 格式。请确认是 Word 文档。")

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(xml_bytes)
    parts: list[str] = []
    for p in root.iter(f"{ns}p"):
        texts = [t.text or "" for t in p.iter(f"{ns}t")]
        line = "".join(texts).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def extract_file_text(filename: str, data: bytes) -> str:
    """按扩展名分派文本提取。只支持能确定性提取纯文本的格式。"""
    fname = (filename or "").lower()
    if fname.endswith((".txt", ".md", ".csv", ".log")):
        return data.decode("utf-8", errors="replace")
    if fname.endswith(".docx"):
        return extract_docx_text(data)
    raise ApiError(400,
                   f"暂不支持 {fname.rsplit('.', 1)[-1]} 格式。"
                   "目前支持 .txt / .md / .csv / .docx，其余格式请复制文本粘贴。")


class ApiError(Exception):
    """带状态码的业务错误。前端直接展示 message。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# 抽取缓存：断网的退路，也是「已验证示例」的落点
# ---------------------------------------------------------------------------

class ExtractCache:
    """以「完整提问内容」为键缓存模型原始输出。

    键用 `document_key(messages)`，覆盖 system 提示词与 user 消息全文。
    只哈希文档正文是不够的：改了提示词或示例，模型看到的提问已经变了，
    旧答案照样会被返回，于是「你以为在用新规则，其实在看旧结果」。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, str] = {}
        self._lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("key") and row.get("raw_output"):
                    self.data[row["key"]] = row["raw_output"]

    @property
    def size(self) -> int:
        return len(self.data)

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, raw: str, note: str = "") -> None:
        with self._lock:
            if key in self.data:
                return
            self.data[key] = raw
            self.path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "key": key,
                "raw_output": raw,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "note": note,
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _is_empty_array(raw: str) -> bool:
    """模型输出是否等价于「空数组」。只做最小判断，不做完整解析。"""
    s = (raw or "").strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.S)
    if fenced:
        s = fenced.group(1).strip()
    return s.lower() in ("", "[]", "null")


class RecordingAdapter:
    """包住真实适配器：落缓存、失败回退，并把「这次是怎么来的」记在案。

    **在线模式不会优先读缓存**，命中也不读——缓存只在三种情况下接管：
    显式 `--offline`、模型调用抛异常、模型返回空数组（且缓存里是非空结果）。
    这是刻意的取舍：演示要给观众看的是模型当场在抽，不是回放。
    一旦「有缓存就用缓存」，第二次演示就会变成放录像，而页面上那句「实时」
    仍然亮着——那是更坏的不诚实。

    所以在线模式下**新文本会写进缓存、旧的键不会被覆盖**（`put()` 遇到同键直接返回，
    第一次写进去的就是已验证的那份）。效果是：粘过的文本越多，断网时能回放的就越多，
    但同一条永远给同一份结果——缓存负责「失败时不要空手」，不负责加速。

    回退这件事必须显式记录而不是静默处理。演示时最怕的情况是
    ——观众以为在看模型实时推理，其实在看三小时前的缓存。
    `used` 会被读到页面上。

    键由外部传入而不是自己从 messages 算：因为键必须抹掉 customer_id，
    见 `cache_key_for()`。
    """

    def __init__(
        self,
        inner: LLMAdapter | None,
        cache: ExtractCache,
        offline: bool,
        key: str,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.offline = offline
        self.key = key
        self.used = "live"
        self.note = ""

    def complete(self, messages: list[dict[str, str]]) -> str:
        key = self.key
        hit = self.cache.get(key)

        if self.offline or self.inner is None:
            if hit is None:
                raise RuntimeError(
                    "离线模式下这段文本没有缓存。"
                    "去掉 --offline 再跑一次（或执行 python3 demo/seed_cache.py 预跑），"
                    "也可以改用页面上的示例文本——示例都有缓存。"
                )
            self.used = "cache"
            self.note = "离线模式，使用预置缓存"
            return hit

        try:
            raw = self.inner.complete(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/额度/鉴权，一律尝试回退
            if hit is None:
                raise
            self.used = "cache"
            self.note = f"模型调用失败，已回退缓存：{exc}"
            return hit

        # 空数组是「模型这次什么都没抽出来」，不是「这段文本没有事实」。
        # 两者在界面上长得一模一样，但一个是故障、一个是结论。
        # 有非空缓存时按故障处理：宁可回放已验过的结果，也不要当场空卡。
        if _is_empty_array(raw) and hit is not None and not _is_empty_array(hit):
            self.used = "cache"
            self.note = "模型本次返回空结果（疑似偶发失败），已回退到已验证的缓存结果"
            return hit

        # 偶发空结果先自动重试一次：实测 deepseek 会偶发返回空（缓存里曾有 3 条），
        # 同文本重试即可抽出。演示现场不能让用户手动点第二次。
        if _is_empty_array(raw):
            try:
                raw = self.inner.complete(messages)
            except Exception:  # noqa: BLE001 - 重试失败按仍空处理
                pass

        # 空结果不进缓存：缓存的任务是「失败时不要空手」，
        # 存一条空进去，等于把离线兜底也变成空手——同键的断网回放会回放这个空。
        if _is_empty_array(raw):
            self.used = "live"
            self.note = "模型连续两次返回空结果（疑似偶发失败），未写入缓存"
            return raw

        self.used = "live"
        self.cache.put(key, raw, note=f"{getattr(self.inner, 'model', '?')}")
        return raw


# ---------------------------------------------------------------------------
# 内存态客户库
# ---------------------------------------------------------------------------

@dataclass
class Customer:
    id: str
    name: str
    industry: str = ""
    stage: str = ""
    owner: str = "你"
    records: list[dict[str, Any]] = field(default_factory=list)
    fact_data: list[dict[str, Any]] = field(default_factory=list)
    synthetic: bool = False  # 演示中现场粘贴出来的
    # 档案层：主数据（规模/合作起始/在办商机/合同/客诉）。事实答「他提过什么」，
    # 这里答「他是谁、值多少」。现场录入的客户没有档案，就留空——不编。
    profile: dict[str, Any] = field(default_factory=dict)
    # 存量客户池（33 个，来自评测集）。它们不是重点客户，但数量本身就是场景：
    # 39 个客户的待办必须先排序才有用，6 个客户的时候排不排都行。
    pool: bool = False
    # 已办的待办：{建议身份 → {mode, at, prev}}。
    # mode="fulfilled" 是「真办完了」（承诺改成已履行），mode="dismissed" 是
    # 「知道了别再提醒」。两者都要记，因为**撤销**需要知道原来是什么状态。
    done: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 档案变更日志：[{at, who, field, old, new}]。
    # 数据错了能改只是第一步——改了什么、谁改的、什么时候改的，得能追溯。
    # 没有这个，「不敢完全信任」的痛点只是从「不信系统」变成「不信改数据的人」。
    change_log: list[dict[str, Any]] = field(default_factory=list)
    # 归属（P2-3）。注意它和 `owner` 不是一回事：`owner` 是界面上显示的
    # 「负责人：王工」这种文本，可能是从 CRM 里带过来的；`owner_id` 是
    # 系统内部用来判「这条数据归谁看」的键，只由服务端写，不由用户编辑。
    owner_id: str = DEFAULT_USER

    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "industry": self.industry,
            "stage": self.stage,
            "owner": self.owner,
            "synthetic": self.synthetic,
            "pool": self.pool,
            # 档案整块带过去：建议引擎要读在办商机金额（impact），
            # 界面要读规模/合同/客诉。这两个消费者都需要它，
            # 各自再查一次 store 只会让「谁有权限看档案」这件事变得含糊。
            "profile": self.profile,
        }


def _max_suffix(ids: list[str], prefix: str) -> int:
    """从一批已有 id 里推出序号最大值。

    不把计数器存进库，而是每次重新算：存了就多一份要跟数据同步的状态，
    而它一旦对不上，表现是「新记录把老记录覆盖了」——最难查的一类错。
    代价是启动时扫一遍，39 个客户几千条记录，可以忽略。
    """
    best = 0
    for raw in ids:
        text = str(raw)
        if not text.startswith(prefix):
            continue
        tail = text[len(prefix):]
        if tail.isdigit():
            best = max(best, int(tail))
    return best


class Store:
    """内存里的一份工作副本 + 可选的落盘。

    **内存态没有取消，只是变成了一层缓存。** 所有读路径仍然读内存
    （`facts_from_data` 每次都要重建 Fact 对象，走一遍序列化再反序列化
    只是白白多一道），写路径在改完内存之后，把**该客户整片**写进库。
    这样 606 条既有单测一行都不用改：`Store()` 不带 repo 时行为与改造前完全一致。

    `repo=None` 就是「不落盘」，两种场景用它：单测，以及显式
    `--db :memory:`。默认起服务会带上真的库文件。
    """

    def __init__(self, repo: Repository | None = None) -> None:
        self.customers: dict[str, Customer] = {}
        self.order: list[str] = []
        self._seq = 0
        self._doc_seq = 0
        self.repo = repo
        # 「改内存 + 写库」必须是一个原子动作。少这把锁，两个线程同时给
        # 同一个客户 append 记录，后写的那片状态会盖掉先写的那片。
        self._lock = threading.RLock()
        if repo is None:
            self.load_presets()
        elif repo.is_seeded():
            self._load_from_repo()
        else:
            # 空库：按种子灌一次。**只在库为空时灌**，不是每次启动都灌——
            # 否则「我改的档案怎么又回去了」会变成每周一次的神秘事件。
            self.load_presets()

    # -- 初始化 ---------------------------------------------------------

    def _load_from_repo(self) -> None:
        self.customers.clear()
        self.order.clear()
        for row in self.repo.load():
            cust = Customer(
                id=row["id"],
                name=row["name"],
                industry=row["industry"],
                stage=row["stage"],
                owner=row["owner"],
                owner_id=row["owner_id"],
                synthetic=row["synthetic"],
                pool=row["pool"],
                profile=row["profile"],
                records=row["records"],
                fact_data=row["fact_data"],
                done=row["done"],
                change_log=row["change_log"],
            )
            self.customers[cust.id] = cust
            self.order.append(cust.id)
        self._seq = _max_suffix(self.order, "c_demo_")
        self._doc_seq = _max_suffix(
            [r["id"] for c in self.customers.values() for r in c.records], "u"
        )

    def load_presets(self) -> None:
        with self._lock:
            self.customers.clear()
            self.order.clear()
            self._seq = 0
            self._doc_seq = 0

            # 重点客户在前、存量池在后：侧栏和待办都按这个顺序读，
            # 「先看 6 个重点、再看 33 个存量」这件事在数据层就定好，不靠前端排序。
            for raw in list(CUSTOMERS) + list(POOL):
                facts = to_facts(dict(raw))
                cust = Customer(
                    id=raw["id"],
                    name=raw["name"],
                    industry=raw.get("industry", ""),
                    stage=raw.get("stage", ""),
                    owner=raw.get("owner", "你"),
                    records=[
                        {
                            "id": r["id"],
                            "date": r["date"],
                            "type": r["type"],
                            "type_cn": r.get("type_cn") or vm.SOURCE_CN.get(r["type"], r["type"]),
                            "text": r["text"],
                        }
                        for r in raw["records"]
                    ],
                    fact_data=vm.facts_to_data(facts),
                    profile=dict(raw.get("profile") or {}),
                    pool=bool(raw.get("pool")),
                )
                self.customers[cust.id] = cust
                self.order.append(cust.id)
            if self.repo is not None:
                self.repo.seed(self.all())

    # -- 查询 -----------------------------------------------------------

    def get(self, cid: str, owner_id: str | None = None) -> Customer:
        """取一个客户。传 `owner_id` 就只在自己的范围内取。

        **越权与不存在返回同一个 404、同一句文案**，这是有意的：
        「这个客户存在，但不是你的」本身就泄露了一条信息（能用来探测
        竞争对手的客户名单）。所以这里不写「无权访问」。
        """
        cust = self.customers.get(cid)
        if cust is None or (owner_id is not None and cust.owner_id != owner_id):
            raise ApiError(404, f"没有这个客户：{cid}")
        return cust

    def all(self, owner_id: str | None = None) -> list[Customer]:
        with self._lock:
            custs = [self.customers[c] for c in self.order]
        if owner_id is None:
            return custs
        return [c for c in custs if c.owner_id == owner_id]

    # -- 写入 -----------------------------------------------------------

    def next_doc_id(self) -> str:
        with self._lock:
            self._doc_seq += 1
            return f"u{self._doc_seq:02d}"

    def create_customer(self, name: str, owner_id: str = DEFAULT_USER) -> Customer:
        with self._lock:
            self._seq += 1
            cid = f"c_demo_{self._seq:02d}"
            cust = Customer(
                id=cid,
                name=name or "未命名客户（现场录入）",
                industry="现场录入",
                # 阶段留空，不要写「信息不足」。
                # 一句话结论会把 stage 拼进去，于是变成「现场录入客户，当前处于信息不足」——
                # 既像病句，又把「我的判断」和「这个客户处于什么阶段」混成了一句。
                # 阶段为空是常态（CRM 里很多商机阶段没维护），结论会自动省略这一句。
                stage="",
                owner="你",
                synthetic=True,
                owner_id=owner_id,
            )
            self.customers[cid] = cust
            self.order.append(cid)
            self.touch(cust)
            return cust

    def append_doc(self, cid: str, doc: dict[str, Any],
                   fact_data: list[dict[str, Any]]) -> None:
        with self._lock:
            cust = self.get(cid)
            cust.records.append(doc)
            cust.fact_data.extend(fact_data)
            self.touch(cust)

    def touch(self, cust: Customer) -> None:
        """把某个客户的整片状态写进库。改完内存就调一次，别攒着。

        为什么不写「save_field(cid, key, value)」这种细粒度接口：
        变更点散在 analyze / mark_done / _refuse / _attribute / edit_profile
        六七处，每处都要列清改了哪些字段，漏一处就是静默的数据丢失。
        整片重写一次事务，漏不掉。
        """
        if self.repo is None:
            return
        with self._lock:
            try:
                ord_ = self.order.index(cust.id)
            except ValueError:
                ord_ = len(self.order)
            self.repo.save(cust, ord_)


# ---------------------------------------------------------------------------
# 应用：路由逻辑与 HTTP 解耦，方便直接跑单测
# ---------------------------------------------------------------------------

class App:
    def __init__(self, offline: bool = False, provider: str | None = None,
                 db: str | Path | None = None, user: str = DEFAULT_USER,
                 allow_reset: bool = False) -> None:
        load_dotenv()
        chosen = provider or os.environ.get("DEMO_PROVIDER") or ""
        if not chosen:
            # 不指定就用 deepseek：评测里事实级 F1 0.66、P0 21 命中 0 误报，
            # 是三个厂商里唯一一个建议层全对的。演示默认要挑最稳的那个。
            if not os.environ.get("LLM_API_KEY") and "deepseek" in available_providers():
                chosen = "deepseek"
        if chosen:
            use_provider(chosen)

        #: 当前身份。只从启动参数来，不从请求头来——
        #: 一个没人验证的 `X-User-Id` 不是权限，是一句自称。
        self.user = (user or DEFAULT_USER).strip() or DEFAULT_USER
        #: 是否挂载 `/api/reset`。默认 False，见模块注释。
        self.allow_reset = bool(allow_reset)
        #: 库。`db=None` 表示不落盘（单测与 `--db :memory:`）。
        self.repo = Repository(db) if db not in (None, "") else None
        self.store = Store(self.repo)
        self.cache = ExtractCache(CACHE_FILE)
        self.offline = offline
        self.provider = chosen or "custom"
        self.model = os.environ.get("LLM_MODEL", "(未配置)")
        self._inner: LLMAdapter | None = None
        # build_adapter("auto") 在没有 Key 时会静默返回 MockAdapter，
        # 它永远吐 "[]"——看起来成功，实际一条事实都抽不出来。
        # 所以这里先自己判断有没有 Key，不能把「没配」交给 auto 去兜。
        if not offline and os.environ.get("LLM_API_KEY"):
            try:
                self._inner = build_adapter("auto")
            except Exception:  # noqa: BLE001 - 配置不全就退化成离线
                self._inner = None
        self.last_source = "live" if self._inner else "cache"
        # 草稿池：{draft_id → 草稿}。只活在内存里，重启即清空——
        # 这是刻意的：草稿的定义就是「还没决定要不要的东西」，
        # 会跨重启活下来的草稿迟早变成一堆没人管的半成品。
        self._drafts: dict[str, dict[str, Any]] = {}
        self._draft_seq = 0
        # 修正记录的读缓存。文件只有几十条，但每个请求都要用，别每次都读盘。
        self._corr_cache: list[fb_corr.Correction] | None = None

    # -- 归属 -----------------------------------------------------------

    def _cust(self, payload_or_cid: dict[str, Any] | str) -> Customer:
        """按当前身份取客户。**所有对外入口都必须走这里。**

        留着 `Store.get(cid)` 不传 owner 的口子是有意的（单测要按 id 直取），
        但 App 里一个都不许用 —— `tests/test_store.py` 有一条断言在扫源码，
        扫到 App 里出现不带 `owner_id=` 的取值调用就红。
        靠人记住「这次要记得传 owner」是靠不住的，靠一条会红的测试才靠得住。
        """
        cid = (payload_or_cid.get("customer_id") or "") if isinstance(payload_or_cid, dict) \
            else payload_or_cid
        return self.store.get(str(cid or ""), owner_id=self.user)

    # -- 元信息 ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider,
            "model": self.model,
            "offline": bool(self.offline or self._inner is None),
            "as_of": AS_OF.isoformat(),
            # 客户数按当前身份算：默认身份下仍是 39，但换 `--user` 起服务
            # 就该是 0——「数据在哪儿、你能看见多少」必须在首屏就能自证。
            "n_customers": len(self.store.all(self.user)),
            "user": self.user,
            "allow_reset": self.allow_reset,
            # 落了盘还是只在内存里，是「关掉页面还在不在」的唯一答案，
            # 所以放进 health 而不是藏在启动日志里。
            "store": dict(
                self.repo.describe() if self.repo is not None
                else {"mode": "memory", "path": "", "schema_version": 0,
                      "on_disk": False, "size": 0, "seeded": False,
                      "note": "未落盘：进程退出即丢失"}
            ),
            "cache_size": self.cache.size,
            "type_cn": TYPE_CN,
            "source_cn": vm.SOURCE_CN,
            # 「我不做」的三个原因由服务端提供，前端照着画按钮。
            # 各写一份的话，改了一边忘了另一边，用户点下去就是 400。
            "refuse_reasons": REFUSE_REASONS,
            "refuse_silence_days": REFUSE_SILENCE_DAYS,
            # 前端不自己编归因选项：文案和天数都由服务端给，
            # 否则改一处漏一处，界面上会出现一个按钮点了没反应的选项。
            "attribute_reasons": ATTRIBUTE_REASONS,
            "chase_silence_days": CHASE_SILENCE_DAYS,
        }

    def samples(self) -> dict[str, Any]:
        return {"samples": SAMPLES, "as_of": AS_OF.isoformat()}

    # -- 查询 -----------------------------------------------------------

    def customers(self, today: date) -> dict[str, Any]:
        out = []
        for cust in self.store.all(self.user):
            # 视图统一走 _view_for：「已办」的过滤只剩一处实现，
            # 不会出现客户页滤了、待办页没滤的情况。
            view = self._view_for(cust, today)
            # 档案条要用记录——没有主数据的客户靠它推「首次记录 / 近 90 天」。
            # 先算好挂在 view 上，档案条和返回体读的是同一份，不会一个含
            # 基准日之后的新记录、一个不含。
            view["records"] = records_until(cust.records, today)
            out.append({
                **view,
                # 档案条与决策链在这里算好一并下发：前端只负责画，
                # 免得同一套「谁缺哪一环」的判断在 JS 和 Python 里各写一遍、
                # 各自漂移（这类双份实现最后一定对不上）。
                "strip": bf.account_strip(cust.profile, today, view),
                "chain": bf.decision_chain(view),
                "synthetic": cust.synthetic,
                "pool": cust.pool,
                "n_pending_records": len(cust.records) - len(view["records"]),
                "fact_data": cust.fact_data,
                "profile": cust.profile,
                "change_log": cust.change_log[-20:],
                "date_range": date_range_for(cust.records),
            })
        return {"as_of": today.isoformat(), "type_cn": TYPE_CN, "customers": out}

    # -- 跨客户待办 / 场景产出物 -----------------------------------------

    def todo(self, today: date, budget: int = bf.DEFAULT_BUDGET) -> dict[str, Any]:
        """跨客户的「今天要做的事」。

        每个客户的建议本来就生成了，这里只是把它们摊到一张表上重排。
        不新增任何判断——否则同一件事在客户页上是 P1、在总表上变成 P0，
        用户就不知道该信哪个。
        """
        views = []
        for cust in self.store.all(self.user):
            view = self._view_for(cust, today)
            # 待办要能分开「重点客户」和「存量池」，所以把来源标在视图上。
            # build_view 不认这个字段，在这里补最省事，也不会污染引擎。
            view["pool"] = cust.pool
            views.append(view)
        data = bf.collect_todos(views, today, budget=budget)
        # 每条待办带上「点它该看哪份产出物、落在哪一块」，
        # 前端只管照着打开。让前端自己按 kind 猜，改一个块 id 就断一条路。
        intents.decorate(data["items"])
        return data

    def intent(self, today: date, n: int = intents.DEFAULT_PICKS) -> dict[str, Any]:
        """四个动作入口：每个动作给「今天最该做这件事的三个客户」。

        故意从 `/api/todo` 那一份 items 里挑，而不是自己再算一遍建议：
        两处各算一份，迟早在某次规则改动后对不上，而那时页面会同时说
        「待办 3 件」和「联系只有 2 个」——用户只会觉得系统坏了。
        """
        items = self.todo(today)["items"]
        return {
            "today": today.isoformat(),
            "n_items": len(items),
            "n": n,
            "intents": intents.build(items, n),
        }

    def brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        """拜访简报 / 交接说明。全部由已有事实与规则拼出来，不调模型。"""
        cust = self._cust(payload)
        today = _parse_date(payload.get("today")) or AS_OF
        fact_data = payload.get("fact_data") or cust.fact_data
        facts = vm.facts_from_data(fact_data)
        view = vm.build_view(cust.meta(), facts, today, n_docs=len(cust.records))
        view["records"] = records_until(cust.records, today)

        kind = (payload.get("kind") or "visit").strip()
        if kind == "handover":
            out = bf.handover(view, today, cust.profile)
        elif kind == "proposal":
            out = bf.proposal_brief(view, today)
        else:
            out = bf.visit_brief(view, today)

        # 导出走服务端而不是前端：格式逻辑只有一份，测试才验得到。
        # 前端拿到 markdown 直接写剪贴板，不参与拼装。
        if (payload.get("format") or "").lower() in ("md", "markdown"):
            return {"kind": out.get("kind"), "title": out.get("title"),
                    "markdown": bf.brief_markdown(out)}
        return out

    # -- 已办 -----------------------------------------------------------

    @staticmethod
    def _drop_done(cust: Customer, view: dict[str, Any], today: date) -> dict[str, Any]:
        """把「知道了，别再提醒」的建议从视图里摘掉，并把 n_p0 跟着重算。

        n_p0 必须重算而不是照抄：页眉写「4 条 P0」而列表只剩 3 条，
        用户会以为系统算错了——而它只是还没把「你刚办掉一条」算进去。
        """
        gone = App._silenced(cust, today)
        if gone:
            actions = [a for a in view.get("actions", []) if a.get("aid") not in gone]
            view["actions"] = actions
            view["n_p0"] = sum(1 for a in actions if a["p"] == "P0")
        view["n_done"] = len(cust.done)
        return view

    @staticmethod
    def _silenced(cust: Customer, today: date) -> set[str]:
        """现在不该再出现的建议。

        四种「不再提醒」，静默期不一样：
          dismissed —— 勾了「已办」，用户说知道了，永久不再提；
          refused   —— 说了「这条我不做」并给了原因，**只静默 7 天**。
                       7 天是个折中：客户情况会变（预算批了、决策人换了），
                       永久压住等于系统从此装瞎；而每天重新提一遍，
                       等于用户第一次说「不归我」时系统没听见。
          chasing   —— 逾期承诺说「还在追」，静默 30 天。这是提醒周期，
                       不是账已平：逾期天数照实写在卡上。
          internal  —— 逾期承诺「转成内部事项」，从客户欠账里挪走，永久静默。
        """
        out = set()
        for aid, rec in cust.done.items():
            mode = rec.get("mode")
            if mode in ("dismissed", "internal"):
                out.add(aid)
            elif mode == "refused":
                at = _parse_date(rec.get("at") or "")
                # 日期解析不了就当没过期：宁可多提一次，也不要静默吞掉一条。
                if at is None or (today - at).days < REFUSE_SILENCE_DAYS:
                    out.add(aid)
            elif mode == "chasing":
                at = _parse_date(rec.get("at") or "")
                if at is None or (today - at).days < CHASE_SILENCE_DAYS:
                    out.add(aid)
        return out

    def _view_for(self, cust: Customer, today: date) -> dict[str, Any]:
        facts = vm.facts_from_data(cust.fact_data)
        return self._drop_done(
            cust,
            vm.build_view(cust.meta(), facts, today, n_docs=len(cust.records)),
            today,
        )

    def mark_done(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把一条待办标成已办。

        分两条路，因为这两件事在现实里就不一样：

        1. **承诺类**：`can_fulfill` 为真时，把事实的 `fulfillment` 改成
           `fulfilled`。规则因此不再命中——不是藏起来，是它真的不再是问题。
           认知卡上那条承诺会显示「已履行」。
        2. **其余**（还没摸到决策人、信息不足、单线联系）：这类不是一天能干完的事。
           勾掉只能算「我知道了，别再提醒我」，**结论一个字都没变**。
           界面必须这么写，否则用户会以为系统以为他有决策人了。
        """
        cust = self._cust(payload)
        aid = (payload.get("aid") or "").strip()
        if not aid:
            raise ApiError(400, "缺少 aid：不知道要办的是哪一条")
        today = _parse_date(payload.get("today")) or AS_OF

        if payload.get("undo"):
            return self._undo_done(cust, aid, today)

        if payload.get("mode") == "refused":
            return self._refuse(cust, aid, payload, today)

        if payload.get("mode") == "attributed":
            return self._attribute(cust, aid, payload, today)

        view = self._view_for(cust, today)
        row = next((a for a in view["actions"] if a.get("aid") == aid), None)
        if row is None:
            raise ApiError(404, "这条待办已经不在了——基准日一变，建议会重算。刷新再看看。")

        before = [a["title"] for a in view["actions"]]
        fid, prev, mode = None, None, "dismissed"
        if row.get("can_fulfill") and row.get("basis_fid"):
            fid = row["basis_fid"]
            for d in cust.fact_data:
                if d.get("fact_id") == fid:
                    p = d.setdefault("payload", {})
                    prev = p.get("fulfillment")
                    p["fulfillment"] = "fulfilled"
                    mode = "fulfilled"
                    break

        cust.done[aid] = {"mode": mode, "at": today.isoformat(), "prev": prev,
                          "fid": fid, "title": row["title"]}
        return self._done_result(cust, aid, today, mode, before)

    def _refuse(self, cust: Customer, aid: str, payload: dict[str, Any],
                today: date) -> dict[str, Any]:
        """「这条我不做」——必须带原因，静默 7 天。

        为什么和「已办」分开：勾「已办」是「我处理过了」，说「我不做」是
        「这条建议本身有问题」。前者该永久消失，后者只是**暂时**不适用。
        把两者混成一个按钮，用户就只有「假装办完」一条路可走——
        而那会让系统以为一件没做的事已经做完了。
        """
        reason = (payload.get("reason") or "").strip()
        if reason not in REFUSE_REASONS:
            raise ApiError(
                400,
                f"「这条我不做」必须给原因，可选：{'、'.join(REFUSE_REASONS)}。"
                "只给一个「不要了」的按钮，等于把这条建议为什么烦人这个信息扔掉了。",
            )

        before_view = self._view_for(cust, today)
        before = [a["title"] for a in before_view["actions"]]
        row = next((a for a in before_view["actions"] if a.get("aid") == aid), None)
        # 已经不在了也算成功：用户点了两次，第二次不该报错。
        title = (row or {}).get("title") or (cust.done.get(aid) or {}).get("title") or ""

        cust.done[aid] = {"mode": "refused", "at": today.isoformat(), "prev": None,
                          "fid": (row or {}).get("basis_fid"), "title": title,
                          "reason": reason}
        out = self._done_result(cust, aid, today, "refused", before, title)
        out["reason"] = reason
        out["reason_cn"] = REFUSE_REASONS[reason]
        out["silent_until"] = (today + timedelta(days=REFUSE_SILENCE_DAYS)).isoformat()
        return out

    def _attribute(self, cust: Customer, aid: str, payload: dict[str, Any],
                   today: date) -> dict[str, Any]:
        """逾期承诺的归因：「还追 / 客户不需要了 / 转内部」。

        为什么必须单独一条通道，而不是复用「这条我不做」：
        「我不做」说的是**建议本身有问题**（事实错了、不归我），
        归因说的是**这件事该怎么收尾**——事实没错、承诺确实欠着。
        混成一个按钮，用户就只能用「这条不对」来表达「这单黄了」，
        而那会让事实层收到一条假信号。

        三种归因落到三个不同的地方：
          chasing    只压提醒周期（30 天），事实一个字不改。
                     逾期天数照实显示——客户确实还没拿到东西。
          not_needed 把事实的 fulfillment 改成 waived。这是真状态变更：
                     这条承诺从此不成立，卡上会显示「已放弃」，可撤销。
          internal   记成永久静默。承诺还在，只是不再算客户账上欠的，
                     因为它已经变成一件内部要推的事。
        """
        reason = (payload.get("reason") or "").strip()
        if reason not in ATTRIBUTE_REASONS:
            raise ApiError(
                400,
                "归因必须选一个，可选："
                f"{'、'.join(ATTRIBUTE_REASONS)}。"
                "一条拖了三个月的事，最需要的信息恰恰是「为什么还没动」——"
                "不给选项，这个信息就永远收不上来。",
            )

        before_view = self._view_for(cust, today)
        before = [a["title"] for a in before_view["actions"]]
        row = next((a for a in before_view["actions"] if a.get("aid") == aid), None)
        title = ((row or {}).get("title")
                 or (cust.done.get(aid) or {}).get("title") or "")
        fid = (row or {}).get("basis_fid")

        prev = None
        if reason == "not_needed" and fid:
            for d in cust.fact_data:
                if d.get("fact_id") == fid:
                    p = d.setdefault("payload", {})
                    prev = p.get("fulfillment")
                    p["fulfillment"] = "waived"
                    break

        rec_mode = {"chasing": "chasing", "not_needed": "waived",
                    "internal": "internal"}[reason]
        cust.done[aid] = {"mode": rec_mode, "at": today.isoformat(), "prev": prev,
                          "fid": fid, "title": title, "reason": reason}

        out = self._done_result(cust, aid, today, "attributed", before, title)
        out["reason"] = reason
        out["reason_cn"] = ATTRIBUTE_REASONS[reason]
        if rec_mode == "chasing":
            out["silent_until"] = (today + timedelta(days=CHASE_SILENCE_DAYS)).isoformat()
        return out

    def _undo_done(self, cust: Customer, aid: str, today: date) -> dict[str, Any]:
        before = [a["title"] for a in self._view_for(cust, today)["actions"]]
        rec = cust.done.pop(aid, None)
        if rec is None:
            raise ApiError(404, "这条没有标过已办，没什么可撤销的。")
        # fulfilled 与 waived 都改过事实的 fulfillment 字段，撤销都得退回去。
        # waived 来自「客户已经不需要了」这条归因——它同样是一次真实的状态变更，
        # 不是「藏起来」，所以撤销也必须真的把它恢复。
        if rec["mode"] in ("fulfilled", "waived") and rec.get("fid"):
            for d in cust.fact_data:
                if d.get("fact_id") == rec["fid"]:
                    d.setdefault("payload", {})["fulfillment"] = rec.get("prev") or "pending"
                    break
        return self._done_result(cust, aid, today, "undone", before, rec.get("title") or "")

    def _done_result(self, cust: Customer, aid: str, today: date, mode: str,
                     before: list[str], title: str | None = None) -> dict[str, Any]:
        """回执里要带什么：一件已经发生的事、一个还能撤回的入口、一组新数字。

        `delta` 不是装饰。承诺类勾掉之后，排在后面的建议会顶上来补位
        （认知卡一次最多显示 4 条），于是**待办总数一点没变**。
        不把「消失的是哪条、顶上来的是哪条」说清楚，用户会以为按钮没生效。
        """
        rec = cust.done.get(aid) or {}
        after = [a["title"] for a in self._view_for(cust, today)["actions"]]
        # 四条「已办 / 撤销 / 不做 / 归因」路径最后都汇到这里，
        # 所以落盘写在这一点上，而不是每处各写一遍——少一处就是一个静默丢状态。
        self.store.touch(cust)
        return {
            "ok": True,
            "mode": mode,
            "aid": aid,
            "title": title if title is not None else (rec.get("title") or ""),
            "n_done": len(cust.done),
            "view": self._view_for(cust, today),
            "todo": self.todo(today),
            "delta": {
                "removed": [t for t in before if t not in after],
                "appeared": [t for t in after if t not in before],
            },
        }

    # -- 重算 -----------------------------------------------------------

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        cid = payload.get("customer_id") or ""
        cust = self._cust(cid)

        fact_data = payload.get("fact_data")
        if fact_data is None:
            fact_data = cust.fact_data
        elif not isinstance(fact_data, list):
            raise ApiError(400, "fact_data 必须是数组")

        today = _parse_date(payload.get("today")) or AS_OF

        t0 = time.perf_counter()
        facts = vm.facts_from_data(fact_data)
        view = vm.build_view(cust.meta(), facts, today, n_docs=len(cust.records))
        ms = (time.perf_counter() - t0) * 1000

        # 前端回传的事实落回内存态：这样下一次不带 fact_data 的调用
        # （比如刷新页面后重新拉列表）看到的仍是用户现场改过的版本。
        #
        # 写回之前先记修正：这一步是「人改了什么」唯一的入口（撤回、改字段
        # 都从这里过），改完之后就不再有新旧两份可比了。
        if payload.get("fact_data") is not None:
            n_fixed = self.record_corrections(cust.fact_data, fact_data, cust.id)
            cust.fact_data = fact_data
            # 只有「真的回传了事实」才可能改到状态，才需要落盘。
            # 不带 fact_data 的调用是纯读，每拖一下日期滑块都写一次库没有意义。
            self.store.touch(cust)
        else:
            n_fixed = 0

        return {
            "view": {
                **view,
                "synthetic": cust.synthetic,
                "records": records_until(cust.records, today),
                "n_pending_records": len(cust.records) - len(records_until(cust.records, today)),
                "date_range": date_range_for(cust.records),
            },
            "ms": round(ms, 2),
            "n_facts": len(facts),
            "n_corrections": n_fixed,
        }

    # -- 抽取 -----------------------------------------------------------

    # -- 记录输入与抽取：extract / draft / commit 共用 ---------------------

    def _record_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验一条记录的输入。

        抽出来是因为「记一笔」的草稿态和「直接入库」必须接受完全相同的输入：
        两边各写一份校验，迟早出现「草稿能过、入库报 400」这种只在现场
        才被发现的错。
        """
        text = (payload.get("text") or "").strip()
        if not text:
            raise ApiError(400, "文本为空。粘一段拜访纪要、通话记录或聊天记录都行。")
        if len(text) > 12000:
            raise ApiError(400, f"文本过长（{len(text)} 字），演示 demo 处理不了这么长的记录。")

        # 演示世界的「今天」：真实日期可能晚于基准日（基准日 2026-10-24，
        # 现场演示日 10-25）。直接用系统时间，会让现场上传的每一条记录
        # 都被判成「基准日之后才入库」而整批进等待区——页面上就是
        # 「抽出 12 条、卡片 0 条」（实测 2026-10-25）。录入动作在演示
        # 世界里不晚于基准日：取 min(真实今天, 基准日)。时间机器语义
        # 不受影响——拖回更早的日子照样看不到今天才录的东西。
        demo_today = min(date.today(), AS_OF)
        doc_date = payload.get("doc_date") or demo_today.isoformat()
        if _parse_date(doc_date) is None:
            raise ApiError(400, f"doc_date 不是合法日期：{doc_date}")
        source_type = payload.get("source_type") or "visit_note"
        if source_type not in vm.SOURCE_CN:
            raise ApiError(400, f"未知来源类型：{source_type}")

        return {
            "text": text,
            "doc_date": doc_date,
            "source_type": source_type,
            "customer_id": (payload.get("customer_id") or "").strip(),
            "customer_name": (payload.get("customer_name") or "").strip(),
        }

    @staticmethod
    def _prompt_doc(rec: dict[str, Any], doc_id: str, customer_id: str) -> dict[str, Any]:
        """提示词只吃这几个键；doc_id 另外给抽取层用作 source_id。

        `recorded_at` 落在演示世界里（见 `_record_input` 的 demo_today 注释），
        否则 `facts_known_at` 会把刚抽的事实整批挡进 pending。
        取 max(基准日, 记录日期)：记录日期没超过基准日 → 基准日当天录入、
        当天可见；用户真填了未来日期 → 事实跟记录一起进「等待录入」，
        两边的可见性保持一致，不会出现「记录看不到、事实却挂出来」。
        """
        _dd = _parse_date(rec["doc_date"]) or AS_OF
        rec_day = max(AS_OF, _dd)
        return {
            "doc_id": doc_id,
            "customer_id": customer_id,
            "source_type": rec["source_type"],
            "occurred_at": rec["doc_date"],
            "recorded_at": f"{rec_day.isoformat()}T{datetime.now().strftime('%H:%M:%S')}",
            "text": rec["text"],
        }

    def _run_extraction(self, prompt_doc: dict[str, Any]):
        """调一次模型（或回放缓存），返回 (result, recording, 毫秒)。"""
        recorder = RecordingAdapter(self._inner, self.cache, self.offline,
                                    cache_key_for(prompt_doc))
        t0 = time.perf_counter()
        result = extract_document(prompt_doc, recorder, include_examples=True, strict=True)
        ms = (time.perf_counter() - t0) * 1000
        if result.error and not result.facts:
            raise ApiError(502, f"抽取失败：{result.error}")
        self.last_source = recorder.used
        return result, recorder, round(ms, 1)

    # -- 回写闭环（P2-5）：修正记录，以及它带来的规则 ----------------

    def corrections(self) -> list[fb_corr.Correction]:
        if self._corr_cache is None:
            self._corr_cache = fb_corr.load(CORRECTIONS_FILE)
        return self._corr_cache

    def record_corrections(self, before: list[dict[str, Any]],
                           after: list[dict[str, Any]], customer_id: str) -> int:
        """从「改之前 / 改之后」两份事实里推出修正并落盘。返回新增条数。

        调用点在 `/api/analyze` 写回内存态之前。为什么在这一层：这里是
        前端唯一会把「人改过的事实」交回来的地方，无论他点的是「这条不对」
        还是改了某个字段，都会从这里经过——不用给每种操作各开一个接口。
        """
        fresh = fb_corr.diff(before, after, customer_id=customer_id)
        if not fresh:
            return 0
        n = fb_corr.append(CORRECTIONS_FILE, fresh)
        if n:
            self._corr_cache = None
        return n

    def feedback(self) -> dict[str, Any]:
        """现在记住了什么。演示时这一段要能当场打开给人看。"""
        rows = self.corrections()
        lessons = fb_lessons.lessons_from(rows)
        return {
            "n_corrections": len(rows),
            "by_reason": fb_corr.counts(rows),
            "reason_cn": fb_corr.REASON_CN,
            "n_blacklist": len(fb_lessons.Blacklist.from_corrections(rows)),
            "lessons": [{"kind": ls.kind, "cn": ls.cn, "fact_type": ls.fact_type,
                         "key": ls.key, "times": ls.times, "why": ls.why} for ls in lessons],
            "patch_suggestion": fb_lessons.suggest_patch(rows),
        }

    def _rule_signals(
        self, text: str, fact_data: list[dict[str, Any]],
        history: list[dict[str, Any]], *, customer_id: str,
        source_type: str, doc_id: str, doc_date: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """从原文里判「变化」，补成 risk_signal 事实。返回（事实, 一句话说明）。

        为什么放在抽取之后、入库之前：规则要用两样东西——当期已抽出的事实
        （去重，别和模型说的重复）和这个客户更早的事实（当基线）。
        这两样在别的时刻都凑不齐：抽取之前没有事实，入库之后历史又包含了当篇。

        为什么它不是一个单独的接口：信号不是用户要的产物，是一篇记录被读完之后
        「顺便看出来的东西」。让它跟着同一段文本一起进草稿，用户才能在自己勾选
        的那一刻看到它、否掉它——**不能有东西绕过人的确认直接进卡**。
        """
        found = sg.detect(text, facts=fact_data, history=history)
        if not found:
            return [], ""
        raws = [
            s.as_fact_data(customer_id=customer_id, source_type=source_type,
                           source_id=doc_id, doc_date=doc_date)
            for s in found
        ]
        return vm.normalize_extracted(raws), sg.suspected_note(found)

    def _filter_by_feedback(self, facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """抽完先过一遍黑名单。拦下来的要带理由回到回执里，不能静默吞。"""
        bl = fb_lessons.Blacklist.from_corrections(self.corrections())
        if not len(bl):
            return facts, []
        return fb_lessons.apply_blacklist(facts, bl)

    @staticmethod
    def _empty_reason(text: str, recorder, result) -> tuple[str, str]:
        """0 条时说清是哪种 0。有事实时调用方不要用这个返回值。"""
        if "返回空结果" in (recorder.note or ""):
            # 模型偶发返回空 ≠ 「没有事实」。把它归因为"没有可成立的事实"
            # 是把故障说成结论——实测（2026-10-25）同一篇纪要重试即抽出 12 条。
            return "model_flaky_empty", (
                "模型这次返回了空结果（偶发失败），这段文本本身是能抽出来的——"
                "再点一次「抽取并重算」即可。")
        return explain_empty(text, len(result.rejected))

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """抽 + 落库，一次做完。适合「我确定这条该进」。"""
        rec_in = self._record_input(payload)

        # 客户：给了 id 就挂到已有客户，否则新建一个
        cid = rec_in["customer_id"]
        if cid:
            cust = self._cust(cid)
        else:
            cust = self.store.create_customer(rec_in["customer_name"], owner_id=self.user)

        doc_id = self.store.next_doc_id()
        prompt_doc = self._prompt_doc(rec_in, doc_id, cust.id)
        result, recorder, ms = self._run_extraction(prompt_doc)

        fact_data, blocked = self._filter_by_feedback(vm.normalize_extracted(result.facts))
        # 规则判出来的风险信号。放在黑名单之后：被用户判过「这段不对」的原文，
        # 规则也不该再从里面读出信号来——黑名单拦的是这段原文，不是模型的某条输出。
        signals, signal_note = self._rule_signals(
            rec_in["text"], fact_data, cust.fact_data,
            customer_id=cust.id, source_type=rec_in["source_type"],
            doc_id=doc_id, doc_date=rec_in["doc_date"],
        )
        fact_data = fact_data + signals
        # 0 条时把原因挂在记录上，这样「全部记录」页里那张卡以后也能解释自己，
        # 而不是只在你刚粘完的那几秒里有说法。
        empty_reason, empty_hint = "", ""
        if not fact_data:
            empty_reason, empty_hint = self._empty_reason(rec_in["text"], recorder, result)

        doc = {
            "id": doc_id,
            "date": rec_in["doc_date"],
            "type": rec_in["source_type"],
            "type_cn": vm.SOURCE_CN.get(rec_in["source_type"], rec_in["source_type"]),
            "text": rec_in["text"],
        }
        if not fact_data:
            doc["empty_reason"] = empty_reason
            doc["empty_hint"] = empty_hint
        self.store.append_doc(cust.id, doc, fact_data)

        return {
            "doc": doc,
            "customer": cust.meta(),
            "fact_data": fact_data,
            "rejected": result.rejected[:20],
            "n_rejected": len(result.rejected),
            # 拒收原因要分类报。全部写成「溯源定位不到原文」是不诚实的：
            # 实测里最常见的拒收其实是「payload 必填字段为空」（抽到一条关键人，
            # 但没说清是谁）。两种原因对用户的意义完全不同——一个是系统不信模型，
            # 一个是模型没答完题——混成一句话，就等于把系统的判断藏起来。
            "rejected_summary": summarize_rejections(result.rejected),
            "n_facts": len(fact_data),
            # 规则判出来的信号单列一个数。混在 n_facts 里，用户会以为
            # 全是模型读出来的；而这两者的可信度来源不同，
            # 一个是「模型读到并转述」，一个是「原文里就这么写的」。
            "n_signals": len(signals),
            "signal_note": signal_note,
            # 被黑名单拦下的不算拒收：拒收是系统不信模型，拦下是「你上次说过这段不对」。
            "n_blocked": len(blocked),
            "blocked_note": fb_lessons.block_note(blocked),
            # 0 条时说清是哪种 0（见 explain_empty）。有事实时留空，
            # 免得界面上出现「抽出了 5 条，但原因是太短」这种自相矛盾。
            "empty_reason": empty_reason,
            "empty_hint": empty_hint,
            "ms": ms,
            "source": recorder.used,          # live / cache
            "note": recorder.note,
            "offline": recorder.used != "live",
            "model": self.model,
        }

    # -- 记一笔：草稿态 ----------------------------------------------------

    #: 草稿上限。演示时用户会反复试不同贴法，不设上限就是内存泄漏。
    MAX_DRAFTS = 20

    def draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只抽不落库：先看看能抽出什么，再由人决定哪几条进。

        为什么必须有这半步：抽取是模型做的，会抽错、会抽多、会把一句客套话
        当需求。原来只有一条路——粘上去就入库——于是用户唯一的纠错方式是
        「入库之后逐条撤回」。那是把校验成本从 3 秒推到了 3 分钟，
        而且撤回改的是事实状态，会在卡上留下一串「已撤回」的痕迹。

        草稿态把这件事挪到入库之前，于是「整条丢弃」是零成本的：
        **不调用 `/api/commit` 就等于什么都没发生**，不需要删除接口，
        也就不存在「删了一半留下孤儿数据」这种状态。
        """
        rec_in = self._record_input(payload)
        cid = rec_in["customer_id"]
        if cid:
            # 客户不存在当场 404，别等用户勾完 12 条才在入库时才知道
            self._cust(cid)

        self._draft_seq += 1
        draft_id = f"draft{self._draft_seq:03d}"
        # 草稿用 d 前缀的临时 id，不占 u* 序号：草稿被丢弃时不该在记录序号上留窟窿。
        # doc_id 不参与提示词（format_document 只写 source_type / occurred_at /
        # customer_id / text），所以换 id 不影响离线缓存键——这一点很关键，
        # 现场断网时草稿态必须还能回放。
        doc_id = f"d{self._draft_seq:03d}"
        prompt_doc = self._prompt_doc(rec_in, doc_id, cid or DRAFT_CUSTOMER)
        result, recorder, ms = self._run_extraction(prompt_doc)

        fact_data, blocked = self._filter_by_feedback(vm.normalize_extracted(result.facts))
        # 草稿阶段就把规则信号一起摆出来。放在这里而不是 commit 时补，
        # 是为了让「这条信号我不要」成为一个可以做的动作——
        # 入库之后才发现、再去撤回，改的是事实状态，会在卡上留下撤回痕迹。
        history = self._cust(cid).fact_data if cid else []
        signals, signal_note = self._rule_signals(
            rec_in["text"], fact_data, history,
            customer_id=cid or DRAFT_CUSTOMER, source_type=rec_in["source_type"],
            doc_id=doc_id, doc_date=rec_in["doc_date"],
        )
        fact_data = fact_data + signals
        empty_reason, empty_hint = "", ""
        if not fact_data:
            empty_reason, empty_hint = self._empty_reason(rec_in["text"], recorder, result)

        self._drafts[draft_id] = {
            "draft_id": draft_id,
            "customer_id": cid,
            "customer_name": rec_in["customer_name"],
            "doc": {
                "id": doc_id,
                "date": rec_in["doc_date"],
                "type": rec_in["source_type"],
                "type_cn": vm.SOURCE_CN.get(rec_in["source_type"], rec_in["source_type"]),
                "text": rec_in["text"],
            },
            "fact_data": fact_data,
            "rejected": result.rejected[:20],
            "n_rejected": len(result.rejected),
            "rejected_summary": summarize_rejections(result.rejected),
            "ms": ms,
            "source": recorder.used,
            "note": recorder.note,
            "offline": recorder.used != "live",
            "model": self.model,
        }
        # 只留最近 MAX_DRAFTS 份。超出就丢最老的——草稿是「还没决定的东西」，
        # 攒着不决定本身就是过期的。
        while len(self._drafts) > self.MAX_DRAFTS:
            self._drafts.pop(next(iter(self._drafts)))

        return {
            "draft_id": draft_id,
            "doc": self._drafts[draft_id]["doc"],
            "customer_id": cid,
            "customer_name": rec_in["customer_name"],
            # 给勾选用的轻量行：界面只需要「这条是什么、凭什么、原文在哪」，
            # 不需要整个 payload。真入库的是 fact_data 原样。
            "facts": [draft_row(f) for f in fact_data],
            "n_facts": len(fact_data),
            "n_signals": len(signals),
            "signal_note": signal_note,
            "n_blocked": len(blocked),
            "blocked_note": fb_lessons.block_note(blocked),
            "rejected": self._drafts[draft_id]["rejected"],
            "n_rejected": len(result.rejected),
            "rejected_summary": self._drafts[draft_id]["rejected_summary"],
            "empty_reason": empty_reason,
            "empty_hint": empty_hint,
            "ms": ms,
            "source": recorder.used,
            "note": recorder.note,
            "offline": recorder.used != "live",
            "model": self.model,
        }

    def commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把草稿里勾选的事实真正入库。

        与 `/api/extract` 走同一条落库路径（append_doc + 前端再 analyze），
        区别只是「事实从哪来」：一个来自刚跑完的模型，一个来自人在草稿上勾的那几条。
        刻意**不给** `/api/extract` 加「有时调模型有时不调」的分支——
        一个接口两种人格，调用方迟早传错参数，而它不会报错，只会静默按另一种意思跑。
        """
        draft_id = (payload.get("draft_id") or "").strip()
        rec = self._drafts.get(draft_id)
        if rec is None:
            raise ApiError(
                404,
                "这份草稿不在了——服务重启过，或者已经确认过一次。重新记一笔吧。",
            )

        all_facts = rec["fact_data"]
        if not all_facts:
            raise ApiError(400, "这份草稿里一条事实都没有，没有可入库的东西。")

        keep = payload.get("keep")
        if keep is None:
            chosen = list(all_facts)
        elif not isinstance(keep, list):
            raise ApiError(400, "keep 必须是 fact_id 数组（不传 = 全要）。")
        else:
            wanted = {str(k) for k in keep}
            chosen = [f for f in all_facts if f.get("fact_id") in wanted]
        if not chosen:
            raise ApiError(
                400,
                "一条都没勾。既然这样就不必入库了——按「整条丢弃」关掉，"
                "原文和事实都不会留下任何东西。",
            )

        # 客户：草稿阶段可能还没定（新客户要到这一步才创建），
        # 所以草稿里的 customer_id 是占位，这里才真正落定。
        cid = rec["customer_id"]
        cust = self._cust(cid) if cid else self.store.create_customer(
            rec["customer_name"], owner_id=self.user)

        doc_id = self.store.next_doc_id()
        doc = {**rec["doc"], "id": doc_id}

        # 草稿用的是占位 id，入库时必须改写成真实的 doc_id / customer_id，
        # 否则「跳到原始记录」会跳到一条不存在的记录上——
        # 而且它不会报错，只会点开一片空白。
        for f in chosen:
            f["source_id"] = doc_id
            f["customer_id"] = cust.id

        self.store.append_doc(cust.id, doc, chosen)
        # 用过就没收：同一份草稿不能落两次库，否则同一段原文会有两条记录、
        # 两套事实，撤回一条另一条还在。
        self._drafts.pop(draft_id, None)

        return {
            "draft_id": draft_id,
            "doc": doc,
            "customer": cust.meta(),
            "fact_data": chosen,
            "n_facts": len(chosen),
            "n_signals": sum(1 for f in chosen
                             if (f.get("payload") or {}).get("detected_by") == "rule"),
            # 「勾掉了 3 条」必须说出来。不说，用户会以为系统抽少了。
            "n_discarded": len(all_facts) - len(chosen),
            "rejected": rec["rejected"],
            "n_rejected": rec["n_rejected"],
            "rejected_summary": rec["rejected_summary"],
            "empty_reason": "",
            "empty_hint": "",
            "ms": rec["ms"],
            "source": rec["source"],
            "note": rec["note"],
            "offline": rec["offline"],
            "model": rec["model"],
        }

    # -- 档案编辑 ---------------------------------------------------------

    #: 可编辑的档案字段白名单。不在名单里的一律不收——
    #: 开放任意字段等于给「手滑改坏数据」开门。
    PROFILE_FIELDS = {
        "scale": str, "since": str,
        "relationship": str, "comm_pref": str, "decision_process": str,
        "products": str,
    }
    PROFILE_NESTED = {
        "open_opportunity": {"name": str, "amount": (int, float), "stage": str},
        "contract": {"expires_on": str, "arr": (int, float)},
        "tickets_90d": {"n": (int, float), "avg_hours": (int, float)},
    }

    def update_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        """更新客户档案。改动进 change_log，谁改的、改了什么都留痕。

        归一化与校验走 `minprofile`，不在这里现写：日期和金额的写法有一堆变体，
        而「格式写错」如果被静默当成一个合法值，界面上就会显示「0 天后到期」
        ——一条错输入变成一条紧急情报。认得出就归一化，认不出就 400。
        """
        cust = self._cust(payload)
        updates = payload.get("profile") or {}
        who = (payload.get("who") or "你").strip()

        if not isinstance(updates, dict):
            raise ApiError(400, "profile 必须是对象")

        patch, errors = minprofile.normalize(updates)
        if errors:
            raise ApiError(400, "；".join(e["message"] for e in errors))
        updates = minprofile.merge(updates, patch)

        changed = []
        now = datetime.now().isoformat(timespec="seconds")

        def _log(key: str, old_val: Any, new_val: Any) -> None:
            if old_val != new_val:
                changed.append({"at": now, "who": who, "field": key,
                                "old": old_val, "new": new_val})

        # 顶层字段（industry/stage/owner）——Customer 对象本身，不在 profile dict 里
        for f in ("industry", "stage", "owner"):
            if f in updates and isinstance(updates[f], str):
                old_val = getattr(cust, f, "")
                if old_val != updates[f]:
                    setattr(cust, f, updates[f])
                    _log(f, old_val, updates[f])

        for key, val in updates.items():
            if key in self.PROFILE_FIELDS:
                if isinstance(val, self.PROFILE_FIELDS[key]):
                    old_val = cust.profile.get(key)
                    if old_val != val:
                        cust.profile[key] = val
                        _log(f"profile.{key}", old_val, val)
            elif key in self.PROFILE_NESTED:
                sub = updates[key]
                if not isinstance(sub, dict):
                    continue
                target = cust.profile.setdefault(key, {})
                for sk, sv in sub.items():
                    if sk in self.PROFILE_NESTED[key] and isinstance(sv, self.PROFILE_NESTED[key][sk]):
                        old_sv = target.get(sk)
                        if old_sv != sv:
                            target[sk] = sv
                            _log(f"{key}.{sk}", old_sv, sv)

        cust.change_log.extend(changed)
        if changed:
            # 一条都没变就不写库：`/api/profile` 会被前端在保存时调，
            # 也可能被「什么都没改就点了保存」调到，空写一次是白开水。
            self.store.touch(cust)
        return {
            "ok": True,
            "n_changed": len(changed),
            "changed": changed[-10:],   # 回执只带最近 10 条
            "change_log": cust.change_log[-20:],
            "profile": cust.profile,
        }

    def reset(self) -> dict[str, Any]:
        """恢复初始状态。**能不能被调用不在这里判断**，在路由层。

        理由：这条路径本身没有「谁能调」的概念，它是无条件执行的动作；
        把它做成「方法里检查一个布尔」等于把安全边界埋在一个可能被
        新入口绕开的地方。谁挂载路由，谁负责那道闸。
        """
        self.store.load_presets()
        # 草稿也要清。不清的话，reset 之后用户还能确认一份「上一个世界里」的草稿，
        # 而那上面引用的客户 id 已经不存在了——报错报在别处，最难查。
        self._drafts.clear()
        self._draft_seq = 0
        # 修正记录也清。「恢复初始状态」如果留着上一轮的修正，下一次抽取会
        # 莫名少几条，而少的那几条看起来跟这次操作毫无关系。
        if CORRECTIONS_FILE.exists():
            CORRECTIONS_FILE.unlink()
        self._corr_cache = None
        return {"ok": True, "n_customers": len(self.store.all(self.user)), "user": self.user}


#: 草稿阶段的客户占位。新客户要到「确认入库」那一步才真正创建——
#: 草稿被丢弃时，库里不该多出一个空壳客户。
DRAFT_CUSTOMER = "_draft"

#: 草稿行里那半句「关键信息」。抽的是 payload 里最值得人一眼看到的一两个字段。
#:
#: 为什么不是把整个 payload 摊开：勾选这一步要的是「这条对不对」，
#: 不是「这条的字段全不全」——字段全不全由校验层负责，它已经把不合格的拒收了。
#: 摊开 12 个字段，人就不看了，全选，于是这一步等于没有。
_GIST_FIELDS = {
    "need": (("priority", "优先级"), ("category", "类别")),
    "concern": (("severity", "严重度"), ("is_resolved", "已解决")),
    "commitment": (("due_date", "到期"), ("direction", "方向")),
    "stakeholder": (("name", "姓名"), ("title", "职务"), ("role_in_deal", "角色")),
    "competition": (("competitor_name", "对手"), ("stage", "局面")),
    "timeline": (("event", "事件"), ("target_date", "日期")),
    "risk_signal": (("observed", "观察"), ("signal_type", "类型")),
}


def draft_row(fact: dict[str, Any]) -> dict[str, Any]:
    """草稿里的一行：够勾选用就行。"""
    payload = fact.get("payload") or {}
    parts: list[str] = []
    for key, label in _GIST_FIELDS.get(fact.get("fact_type", ""), ()):
        val = payload.get(key)
        if val in (None, "", [], {}):
            continue
        if isinstance(val, bool):
            val = "是" if val else "否"
        parts.append(f"{label} {val}")
        if len(parts) >= 2:
            break
    return {
        "fact_id": fact.get("fact_id") or "",
        "fact_type": fact.get("fact_type") or "",
        "type_cn": TYPE_CN.get(fact.get("fact_type") or "", fact.get("fact_type") or ""),
        "statement": fact.get("statement") or "",
        # 原文片段：勾选时最该看的就是这句。「系统是不是听岔了」
        # 只能靠对照原文判断，转述句反而看不出来。
        "span": fact.get("source_span") or "",
        "conf": round(float(fact.get("confidence") or 0), 2),
        "gist": " · ".join(parts),
        # 这条是谁读出来的：模型转述，还是规则从原文里摘的。
        # 界面上必须标出来——两者的可信度来源不同，一个可能转述错，
        # 一个的「证据」就是那段原文本身。
        "by": payload.get("detected_by") or "model",
    }


def summarize_rejections(rejected: list[Any]) -> list[dict[str, Any]]:
    """把拒收原因归成「原因 → 条数」。

    校验层给的 `_reason` 形如 `[E008] payload.name: payload 必填字段为空`。
    错误码对人没用，冒号后面那句才有用，所以只取后半句。
    """
    counts: dict[str, int] = {}
    for item in rejected:
        raw = ""
        if isinstance(item, dict):
            raw = str(item.get("_reason") or item.get("reason") or "")
        else:
            raw = str(item)
        text = raw.split(":", 1)[-1].strip() if "] " in raw else raw.strip()
        text = text or "未给出原因"
        counts[text] = counts.get(text, 0) + 1
    return [{"reason": k, "n": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# 短于这个字数的输入，模型多半判断不出「关于谁、关于什么」。
# 24 是按实测定的：带上下文的三四十字短句基本都能抽，
# 而六到十几字的碎片在旧提示词下稳定漏抽（新提示词已大幅改善，但仍需要解释）。
SHORT_TEXT_CHARS = 24

# 短句里带这些词，说明它大概率在说业务，只是没写全；
# 一个都不带（「收到，谢谢」「好的我先走了」），那多半真是客套，不必劝人补上下文。
BIZ_WORDS = (
    "预算", "报价", "价格", "单价", "折扣", "账期", "付款", "合同", "方案", "工期",
    "上线", "部署", "需求", "功能", "集成", "对接", "接口", "招标", "投标", "采购",
    "竞品", "友商", "对手", "负责", "决策", "签字", "审批", "领导", "担心", "顾虑",
    "抱怨", "不满", "逾期", "交付", "承诺", "演示", "测试", "试点", "评审", "立项",
    "合规", "安全", "培训", "响应", "规模", "门店", "车间", "产线", "数据",
    # 「客户说下周给答复」只有业务词能认出来：它没有「需求/预算」这种显眼的词，
    # 但它是时间节点，不是客套。少这一组会把业务话判成寒暄，比不解释更误导。
    "答复", "回复", "反馈", "评估", "考虑", "决定", "签约", "续约", "验收",
    "拜访", "会议", "电话", "邮件", "面谈", "约见",
)


def explain_empty(text: str, n_rejected: int) -> tuple[str, str]:
    """0 条事实时说清是**哪一种** 0。

    三种 0 对用户的意义完全不同：
      all_rejected  抽到了，被校验挡住 → 这是系统的判断，用户该看拒收原因
      too_short     像业务信息但太碎，模型不肯猜 → 补两句话就能抽
      no_fact       模型判定这段确实没有事实 → 这是结论，寒暄不用抽

    以前三种都显示「抽出 0 条事实」，用户只能得出一个结论：系统漏读了。
    实测（2026-10-24）销售随手记的一句「客户预算在 50 万内」，同一句话跑四次
    零命中——那不是系统坏了，是它不肯猜，但系统一个字都没说，看起来就像坏了。

    但「太短」这个理由也不能乱用：「收到，谢谢」只有 6 个字，它短，可它不是
    「没说全」，它本来就没事。所以先看有没有业务词，再决定是劝人补上下文，
    还是直接告诉人这段没东西可抽。
    """
    if n_rejected:
        return ("all_rejected",
                "抽到了一些，但都没通过校验（溯源片段对不上原文，或必填字段为空）。"
                "这不是漏读，是系统不肯拿没证据的东西下结论。")
    body = (text or "").strip()
    if len(body) <= SHORT_TEXT_CHARS:
        if any(w in body for w in BIZ_WORDS):
            return ("too_short",
                    f"这段只有 {len(body)} 个字，像是在说业务，但看不出是关于谁、关于什么的，"
                    "模型不肯凭一句话猜。补上「谁说的、哪个项目、什么时候」再试一次——"
                    "「客户预算在 50 万内」补成「客户说这次项目预算在 50 万内，超了要走集团审批」就能抽出来。")
        return ("no_fact",
                f"这段只有 {len(body)} 个字，看起来是寒暄或客套，里面没有可抽的事实。"
                "不抽也是一种结论——它没有替你补出预算、时间和决策人。")
    return ("no_fact",
            "模型认为这段没有可成立的事实。寒暄、日程、客套话都属于这一类——"
            "不抽也是一种结论，它没有替你补出预算、时间和决策人。")


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def records_until(records: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """截至基准日已经存在的记录。

    和事实一样，记录也要按时间裁：拖回 9 月还看得见 10 月的会议纪要，
    「全部记录」这一页就成了穿越。裁掉多少条要告诉界面，
    否则用户以为记录丢了。
    """
    edge = today.isoformat()
    return [r for r in records if (str(r.get("date") or ""))[:10] <= edge]


def date_range_for(records: list[dict[str, Any]]) -> dict[str, str]:
    """滑块的可拖范围：最早一篇记录的日期，到基准日往后 90 天。

    起点不能取「今天」——真实客户的第一篇记录往往在几个月前，
    那些日子恰恰是判断「承诺当时怎么说的」最有用的区间。
    """
    days = [_parse_date(r.get("date")) for r in records]
    days = [d for d in days if d is not None]
    lo = min(days) if days else AS_OF - timedelta(days=30)
    hi = max(AS_OF + timedelta(days=90), max(days) + timedelta(days=7) if days else AS_OF)
    return {"min": lo.isoformat(), "max": hi.isoformat()}


def cache_key_for(doc: dict[str, Any]) -> str:
    """算缓存键，但把客户身份抹掉。

    为什么必须抹：`format_document()` 会把 customer_id 写进提示词，于是
    「同一段文本配不同客户」会得到不同的键。而演示里最常见的情况恰恰是
    ——同一段示例文本被反复粘贴，每次新建一个客户。键一变，缓存全 miss，
    离线回放直接失效，而那正是现场断网时唯一的退路。

    文本相同、日期相同（日期会影响「下周三」的归一化，必须留在键里）、
    那么抽出来的东西就该一样。事实的 customer_id 由校验层的 context 写入，
    不依赖模型输出，所以抹掉它对结果没有影响。
    """
    probe = {**doc, "customer_id": "_cache"}
    return document_key(build_messages(probe, include_examples=True))


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DemoServe/1.0"
    app: App  # 由 main 注入

    # -- 工具 -----------------------------------------------------------

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _html(self) -> None:
        if not HTML_FILE.exists():
            self._json({"error": f"缺少页面文件：{HTML_FILE.name}"}, 500)
            return
        body = HTML_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认日志会为每个 /api/analyze 刷一行，拖日期滑块时会淹没终端
        sys.stderr.write(f"  {self.command} {urlparse(self.path).path} → {args[1]}\n")

    # -- 路由 -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html", "/客户认知卡-演示.html"):
                self._html()
            elif path == "/api/health":
                self._json(self.app.health())
            elif path == "/api/customers":
                self._json(self.app.customers(AS_OF))
            elif path == "/api/feedback":
                # 引擎记住你改过什么。放在 GET 里是为了演示时能当场打开：
                # 这一段如果只能靠嘴说，没人会信它真的记住了。
                self._json(self.app.feedback())
            elif path == "/api/todo":
                q = parse_qs(urlparse(self.path).query)
                day = _parse_date((q.get("today") or [""])[0]) or AS_OF
                # 今日名额可配：?budget=5。给 0 就是「不设上限」——
                # 演示时想一次看全，比背一个默认值有用。
                raw = (q.get("budget") or [""])[0].strip()
                budget = bf.DEFAULT_BUDGET
                if raw:
                    try:
                        budget = max(0, int(raw))
                    except ValueError as exc:
                        raise ApiError(400, f"budget 必须是整数：{raw}") from exc
                self._json(self.app.todo(day, budget))
            elif path == "/api/intent":
                q = parse_qs(urlparse(self.path).query)
                day = _parse_date((q.get("today") or [""])[0]) or AS_OF
                raw = (q.get("n") or [""])[0].strip()
                n = intents.DEFAULT_PICKS
                if raw:
                    try:
                        n = max(1, min(10, int(raw)))
                    except ValueError as exc:
                        raise ApiError(400, f"n 必须是整数：{raw}") from exc
                self._json(self.app.intent(day, n))
            elif path == "/api/samples":
                self._json(self.app.samples())
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._json({"error": f"没有这个路径：{path}"}, 404)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"服务端异常：{exc}"}, 500)

    def upload_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """上传文档并提取文本。返回纯文本供前端填入粘贴框。"""
        filename = (payload.get("filename") or "").strip()
        b64 = payload.get("data") or ""
        if not filename or not b64:
            raise ApiError(400, "缺 filename 或 data 字段。")
        try:
            data = base64.b64decode(b64)
        except Exception:
            raise ApiError(400, "base64 解码失败。")
        if len(data) > 5 * 1024 * 1024:
            raise ApiError(400, "文件过大（超过 5MB）。演示版处理不了这么大的文档。")
        text = extract_file_text(filename, data)
        if not text.strip():
            raise ApiError(400, "文档里没有提取到文本内容。")
        return {
            "ok": True,
            "filename": filename,
            "text": text[:12000],   # 和 extract 的文本上限一致
            "n_chars": len(text),
        }

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/extract":
                self._json(self.app.extract(self._read_json()))
            elif path == "/api/draft":
                self._json(self.app.draft(self._read_json()))
            elif path == "/api/commit":
                self._json(self.app.commit(self._read_json()))
            elif path == "/api/analyze":
                self._json(self.app.analyze(self._read_json()))
            elif path == "/api/brief":
                self._json(self.app.brief(self._read_json()))
            elif path == "/api/done":
                self._json(self.app.mark_done(self._read_json()))
            elif path == "/api/profile":
                self._json(self.app.update_profile(self._read_json()))
            elif path == "/api/upload":
                self._json(self.upload_file(self._read_json()))
            elif path == "/api/reset":
                # 默认当它不存在。返回 404 而不是 403：403 等于告诉对方
                # 「有个能清库的接口，只是你没开」，那也是一条情报。
                if not self.app.allow_reset:
                    raise ApiError(404, f"没有这个路径：{path}")
                self._json(self.app.reset())
            else:
                self._json({"error": f"没有这个路径：{path}"}, 404)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"服务端异常：{exc}"}, 500)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地可演示 Demo 服务端")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不调模型")
    ap.add_argument("--provider", default=None, help="切换 .env 里的厂商：deepseek / zhipu / qwen")
    ap.add_argument("--db", default=str(DB_FILE),
                    help=f"库文件路径（默认 {DB_FILE}；传 :memory: 则不落盘）")
    ap.add_argument("--user", default=DEFAULT_USER,
                    help=f"以谁的身份读写（默认 {DEFAULT_USER}）。这不是登录，是归属")
    ap.add_argument("--allow-reset", action="store_true",
                    help="挂载 /api/reset（默认不挂载）。冒烟脚本与界面断言要用")
    args = ap.parse_args(argv)

    try:
        app = App(offline=args.offline, provider=args.provider, db=args.db,
                  user=args.user, allow_reset=args.allow_reset)
    except SchemaMismatch as exc:
        # 结构版本对不上就不要启动。让进程带着一个明确的错退出，
        # 远好过带着一个会写坏数据的库继续跑。
        print(f"\n  库结构不兼容：{exc}\n", file=sys.stderr)
        return 2
    Handler.app = app

    if not HTML_FILE.exists():
        print(f"缺少页面文件：{HTML_FILE}", file=sys.stderr)
        return 1

    info = app.health()
    mode = "离线回放（只用缓存）" if info["offline"] else "真实模型"
    store = info["store"]
    where = (f"{store['path']}（{store['size'] // 1024} KB）"
             if store["mode"] == "sqlite" else "不落盘（进程退出即丢失）")
    print()
    print(f"  客户认知助手 · 本地演示")
    print(f"  ────────────────────────────────────────")
    print(f"  页面    http://{args.host}:{args.port}/")
    print(f"  模式    {mode}")
    print(f"  模型    {info['model']}（{info['provider']}）")
    print(f"  基准日  {info['as_of']}")
    print(f"  缓存    {info['cache_size']} 条")
    print(f"  客户    {info['n_customers']} 个（身份 {info['user']}）")
    print(f"  数据    {where}")
    print(f"  重置    {'开（--allow-reset）' if info['allow_reset'] else '关'}")
    print(f"  ────────────────────────────────────────")
    print(f"  Ctrl-C 退出（状态已落盘，下次启动原样读回）")
    print()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

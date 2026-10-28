"""把演示状态落到一个 SQLite 文件里。

**为什么是这个方案，而不是继续用内存 + JSON 快照**

改造前所有状态都在内存里，进程一退全没。补上落盘有三条路：

1. JSON 快照（读全量 → 改 → 覆盖写）。改动最小，但进程在写一半时被杀就是
   全库损坏；两个人同时写必丢其中一个人的改动。
2. JSONL 追加 + 墓碑。不会丢已写的记录，但「标已办再撤销」这类状态要靠
   事件重放算出来，读路径越来越慢，还得另写压实任务。
3. SQLite 单文件。`sqlite3` 是标准库，不引入第三方依赖；一次写入是事务，
   要么全成要么全败，不存在「写了一半」。

选 3。真正起决定作用的不是性能，是**「写一半」这个状态根本不该存在**：
这个库里存的是「谁答应过什么、什么时候到期」，半条记录比没有记录更糟。

**为什么一次事务写「一个客户的整片状态」，而不是逐字段增量**

逐字段增量要先知道哪些字段变了，而变更点散在 analyze / mark_done / _refuse /
_attribute / edit_profile 六七处，漏一处就是静默的数据丢失。反过来，一个客户的
整片状态只有几十行（记录 + 事实 + 已办 + 变更日志），整片重写一次事务就够快，
而且「要么全成」，没有中间态。

**并发模型**

只防一层：单进程多线程（`ThreadingHTTPServer`）。做法是一把 `RLock`
串起所有访问 + 一条常驻连接。不开多进程 —— 当前没有任何部署证据说明需要，
而多进程会引入「谁持有写锁」这个跨进程问题，那是另一个量级的工程。

WAL 与 `busy_timeout` 仍然开着，但那只是保险：拖日期滑块时 `analyze` 会连着
打十几个请求，WAL 让读不挡写。它们不构成并发模型，别把保险当设计。

**迁移**

`SCHEMA_VERSION` 对不上时**直接报错并拒绝启动**，不猜、不改、不自动升级。
理由：现在只有一个版本，写一条没被任何真实数据检验过的迁移路径，
比不写更危险 —— 它会让人以为升级是安全的。真要升版本时，先有真实库再写。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

#: 库结构版本。任何一次改表结构都要 +1，并且此时必须写迁移——
#: 见模块注释「迁移」一段：没有真实数据的迁移是猜的。
SCHEMA_VERSION = 1

#: 建表语句。逐条执行而不是 `executescript`：后者会先发一次隐式 COMMIT，
#: 把外面的事务断成两截，「要么全成」就没了。
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS customer (
        id        TEXT PRIMARY KEY,
        ord       INTEGER NOT NULL DEFAULT 0,
        name      TEXT NOT NULL,
        industry  TEXT NOT NULL DEFAULT '',
        stage     TEXT NOT NULL DEFAULT '',
        owner     TEXT NOT NULL DEFAULT '',
        owner_id  TEXT NOT NULL DEFAULT '',
        synthetic INTEGER NOT NULL DEFAULT 0,
        pool      INTEGER NOT NULL DEFAULT 0,
        profile   TEXT NOT NULL DEFAULT '{}'
    )""",
    # 记录的主键用 (customer_id, ord) 而不是 record.id：
    # 预置记录用的是 r11 / d001 这类编号，**没有保证跨客户唯一**。
    # 拿它当主键，将来某天两条记录撞号，会静默覆盖掉一条。
    """CREATE TABLE IF NOT EXISTS record (
        customer_id TEXT NOT NULL,
        ord         INTEGER NOT NULL,
        id          TEXT NOT NULL,
        date        TEXT NOT NULL,
        type        TEXT NOT NULL,
        type_cn     TEXT NOT NULL DEFAULT '',
        text        TEXT NOT NULL,
        PRIMARY KEY (customer_id, ord)
    )""",
    """CREATE TABLE IF NOT EXISTS fact (
        customer_id TEXT NOT NULL,
        ord         INTEGER NOT NULL,
        fact_id     TEXT NOT NULL DEFAULT '',
        data        TEXT NOT NULL,
        PRIMARY KEY (customer_id, ord)
    )""",
    """CREATE TABLE IF NOT EXISTS done (
        customer_id TEXT NOT NULL,
        aid         TEXT NOT NULL,
        data        TEXT NOT NULL,
        PRIMARY KEY (customer_id, aid)
    )""",
    """CREATE TABLE IF NOT EXISTS change_log (
        customer_id TEXT NOT NULL,
        ord         INTEGER NOT NULL,
        data        TEXT NOT NULL,
        PRIMARY KEY (customer_id, ord)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_record_cust ON record(customer_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_customer_owner ON customer(owner_id)",
)

_TABLES = ("change_log", "done", "fact", "record", "customer")


class SchemaMismatch(RuntimeError):
    """库里的结构版本与代码要求的不一致。

    单独一个异常类型，是因为这个错**不该被上层当成普通 500 吞掉**：
    它意味着「继续跑下去会写坏数据」，必须让进程起不来。
    """


class Repository:
    """一条常驻连接 + 一把锁。

    `path` 传 `":memory:"` 就是一个纯内存库 —— 给单测用，行为与文件库完全一致
    （同样走建表、同样的序列化），只是不落地。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        #: 一把锁串起所有访问。同一条 sqlite3 连接不能多线程同时用（哪怕只读），
        #: 所以这里不是「写锁」，是「连接锁」——别把它的语义记错。
        self._lock = threading.RLock()
        # isolation_level=None → 自动提交模式，事务由 _write() 显式 BEGIN。
        # 设成默认的 ""，sqlite3 会自己在 DML 前偷偷 BEGIN，
        # 于是「外面再 BEGIN」就变成嵌套，报的错还很难懂。
        self._cx = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._cx.row_factory = sqlite3.Row
        self._cx.execute("PRAGMA journal_mode=WAL")
        self._cx.execute("PRAGMA synchronous=NORMAL")
        self._cx.execute("PRAGMA foreign_keys=ON")
        self._cx.execute("PRAGMA busy_timeout=5000")
        try:
            self.ensure_schema()
        except BaseException:
            # 建表失败（最典型的是版本对不上）就别攥着一条没人会关的连接。
            # 调用方拿到异常后会退出进程，但「靠进程退出来收尾」
            # 在单测里会变成一屏 ResourceWarning，真正的告警就淹了。
            self._cx.close()
            raise

    # -- 事务 -----------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            cx = self._cx
            cx.execute("BEGIN IMMEDIATE")
            try:
                yield cx
            except BaseException:
                cx.rollback()
                raise
            cx.commit()

    # -- 结构 -----------------------------------------------------------

    def ensure_schema(self) -> None:
        with self._lock:
            try:
                row = self._cx.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                # 还没有 meta 表 = 全新的库。
                row = None
            if row is not None:
                got = int(row["value"])
                if got != SCHEMA_VERSION:
                    raise SchemaMismatch(
                        f"{self.path} 的结构版本是 {got}，代码要 {SCHEMA_VERSION}。"
                        "这一层没有自动迁移——先备份，再决定是升级还是重建。"
                    )
                return
            with self._write() as cx:
                for stmt in _DDL:
                    cx.execute(stmt)
                cx.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    # -- 读取 -----------------------------------------------------------

    def is_seeded(self) -> bool:
        with self._lock:
            row = self._cx.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        return bool(row) and row["value"] == "1"

    def load(self) -> list[dict[str, Any]]:
        """按 seed 顺序读回每个客户的整片状态。

        返回的 dict 键与 `Customer` 的字段同名，调用方直接
        `Customer(**row)` 即可；不返回 `Customer` 对象是为了让 `src/`
        不反向依赖 `demo/`。
        """
        with self._lock:
            cx = self._cx
            out: list[dict[str, Any]] = []
            for c in cx.execute("SELECT * FROM customer ORDER BY ord").fetchall():
                cid = c["id"]
                records = [
                    {"id": r["id"], "date": r["date"], "type": r["type"],
                     "type_cn": r["type_cn"], "text": r["text"]}
                    for r in cx.execute(
                        "SELECT * FROM record WHERE customer_id=? ORDER BY ord", (cid,)
                    )
                ]
                facts = [
                    json.loads(r["data"])
                    for r in cx.execute(
                        "SELECT data FROM fact WHERE customer_id=? ORDER BY ord", (cid,)
                    )
                ]
                done = {
                    r["aid"]: json.loads(r["data"])
                    for r in cx.execute("SELECT aid, data FROM done WHERE customer_id=?", (cid,))
                }
                change_log = [
                    json.loads(r["data"])
                    for r in cx.execute(
                        "SELECT data FROM change_log WHERE customer_id=? ORDER BY ord", (cid,)
                    )
                ]
                out.append({
                    "id": cid,
                    "name": c["name"],
                    "industry": c["industry"],
                    "stage": c["stage"],
                    "owner": c["owner"],
                    "owner_id": c["owner_id"],
                    "synthetic": bool(c["synthetic"]),
                    "pool": bool(c["pool"]),
                    "profile": json.loads(c["profile"]),
                    "records": records,
                    "fact_data": facts,
                    "done": done,
                    "change_log": change_log,
                })
            return out

    def count(self, table: str, *, customer_id: str | None = None) -> int:
        """数行数。冒烟脚本从服务外面直接查库，用的就是这个口径。"""
        if table not in _TABLES:
            raise ValueError(f"没有这张表：{table}")
        with self._lock:
            if customer_id is None:
                row = self._cx.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            else:
                row = self._cx.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE customer_id=?", (customer_id,)
                ).fetchone()
        return int(row["n"])

    # -- 写入 -----------------------------------------------------------

    def save(self, cust: Any, ord_: int = 0) -> None:
        """整片重写一个客户的状态。调用方负责把 `ord_` 传对（决定列表顺序）。

        `cust` 只要有 `id / name / industry / stage / owner / owner_id /
        synthetic / pool / profile / records / fact_data / done / change_log`
        这几个属性就行 —— 鸭子类型，避免 `src/` 反向 import `demo/`。
        """
        with self._write() as cx:
            self._write_locked(cx, cust, ord_)

    def seed(self, custs: Sequence[Any]) -> None:
        """清空并整体重灌。`/api/reset` 与首次启动都走这里。"""
        with self._write() as cx:
            for table in _TABLES:
                cx.execute(f"DELETE FROM {table}")
            for i, cust in enumerate(custs):
                self._write_locked(cx, cust, i)
            cx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('seeded', '1')")

    def _write_locked(self, cx: sqlite3.Connection, cust: Any, ord_: int) -> None:
        cx.execute(
            "INSERT OR REPLACE INTO customer"
            "(id, ord, name, industry, stage, owner, owner_id, synthetic, pool, profile)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                cust.id, ord_, cust.name, cust.industry, cust.stage, cust.owner,
                cust.owner_id, int(bool(cust.synthetic)), int(bool(cust.pool)),
                json.dumps(cust.profile, ensure_ascii=False),
            ),
        )
        cid = cust.id
        for table in ("record", "fact", "done", "change_log"):
            cx.execute(f"DELETE FROM {table} WHERE customer_id=?", (cid,))
        cx.executemany(
            "INSERT INTO record(customer_id, ord, id, date, type, type_cn, text)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                (cid, i, r["id"], r["date"], r["type"], r.get("type_cn") or "", r["text"])
                for i, r in enumerate(cust.records)
            ],
        )
        cx.executemany(
            "INSERT INTO fact(customer_id, ord, fact_id, data) VALUES(?,?,?,?)",
            [
                (cid, i, str(d.get("fact_id") or ""), json.dumps(d, ensure_ascii=False))
                for i, d in enumerate(cust.fact_data)
            ],
        )
        cx.executemany(
            "INSERT INTO done(customer_id, aid, data) VALUES(?,?,?)",
            [
                (cid, aid, json.dumps(rec, ensure_ascii=False))
                for aid, rec in cust.done.items()
            ],
        )
        cx.executemany(
            "INSERT INTO change_log(customer_id, ord, data) VALUES(?,?,?)",
            [
                (cid, i, json.dumps(e, ensure_ascii=False))
                for i, e in enumerate(cust.change_log)
            ],
        )

    # -- 生命周期 -------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """给 `/api/health` 用：说清数据现在到底在哪儿。"""
        size = 0
        if self.path != ":memory:":
            # WAL 模式下正文还没写回主文件时，主文件可能只有 4KB，而真正
            # 的数据在 `-wal` 里。只报主文件大小，看的人会以为「什么都没存」。
            for suffix in ("", "-wal", "-shm"):
                p = Path(self.path + suffix)
                if p.exists():
                    size += p.stat().st_size
        return {
            "mode": "sqlite",
            "path": self.path,
            "schema_version": SCHEMA_VERSION,
            "on_disk": self.path != ":memory:" and Path(self.path).exists(),
            "size": size,
            "seeded": self.is_seeded(),
        }

    def close(self) -> None:
        with self._lock:
            self._cx.close()

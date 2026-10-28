"""演示状态的落盘层。

这一层只做一件事：把「一个客户的全部状态」原样存进 SQLite、再原样读回来。
它不认识 `Customer`，也不认识 HTTP —— 传进来的对象只要有那几个属性就行。
这样 `src/` 不必反向依赖 `demo/`，单测可以直接拿一个 `SimpleNamespace` 喂它。
"""

from .repository import SCHEMA_VERSION, Repository, SchemaMismatch

__all__ = ["SCHEMA_VERSION", "Repository", "SchemaMismatch"]

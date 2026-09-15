"""持久化后端契约。

内核只要求「必须有一个能存东西的后端」，不知道它是什么引擎（P1）。
默认选哪个后端是**发行版**的事，写在 ``alterego/defaults.toml`` 里，
选型理由见 ADR-0003。

各 Repository（``persona`` / ``memory`` / ``conversation`` …）不在这里声明——
它们通过注册表按接口单独获取，见 docs/design/03-data-model.md。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


__all__ = ["StorageBackend"]


class StorageBackend(Protocol):
    """一块可靠的、支持事务的持久化存储。"""

    def migrate(self) -> str:
        """把结构升到最新版本，返回应用后的 ``schema_version``。"""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """开一个事务。

        写法必须是 ``with backend.transaction():``——一个 tick 的所有写入
        要么一起成功、要么一起回滚，不允许出现「记忆写了但情绪没写」的状态。
        """
        ...

    def flush(self) -> None:
        """把挂起的写入落盘。"""
        ...

    def checkpoint(self) -> None:
        """整理存储内部结构（例如 WAL checkpoint）。可以很慢，偶尔调用即可。"""
        ...

    def close(self) -> None: ...

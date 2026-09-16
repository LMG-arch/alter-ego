"""``llm_usage`` 这张表的两个方向：记一笔、算一段。

与 ``repositories.py`` 分家纯属体量原因（``AGENTS.md`` § 5 的 900 行上限，
和 ``engine_repositories.py`` 是同一条理由）。它和那边几个仓储的差别是
**一个类同时管读和写**，而且两边都很薄：

- **写**（:class:`~alterego.interfaces.llm.UsageSink`）：网关每调一次模型
  就 ``record()`` 一笔。网关不认识这个类名，也不需要——它只认那个 Protocol。
- **读**（:class:`~alterego.interfaces.repository.UsageRepository`）：统计页
  按天 / 用途 / 模型分组看窗口里烧了多少。

不拆成两半是因为它们读写的是**同一张表**：拆开之后，「按天要取
``substr(created_at, 1, 10)``」这种口径会各写一遍，而两遍里先改的那一遍
会赢——赢在不报错，只是数字开始对不上。

依据: docs/design/03-data-model.md § 3.3、docs/design/09-observability.md
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from alterego.interfaces.llm import LLMUsage
from alterego.interfaces.repository import UsageGroup, UsageTotal
from alterego.storage.sqlite.connection import SqliteConnection
from alterego.storage.sqlite.repositories import _format_dt, _now_iso


__all__ = ["SqliteUsageRepository"]


_LLM_USAGE_INSERT = """
INSERT INTO llm_usage (
    id, persona_id, tick_id, purpose, tier, provider_id, model,
    prompt_tokens, completion_tokens, total_tokens, latency_ms,
    cost_usd, cache_hit, retry_count, success, error, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: 分组名 → 真正的 SQL 表达式。**分成两段写（别名 / 表达式）是有意的**：
#:
#: 1. 这里的值会**拼进 SQL**，所以它只能来自这份白名单，不能来自调用方。
#:    传进来的是 :data:`~alterego.interfaces.repository.UsageGroup` 里的字符串，
#:    查表查不到就报错而不是回落——回落会让统计页安静地答错；
#: 2. 按天分组用 ``substr(created_at, 1, 10)`` 而不是 ``date()``：
#:    ``created_at`` 是**带偏移的 ISO 串**（见 :func:`_format_dt`），
#:    ``date()`` 会把它先换算成 UTC，于是东八区凌晨那次调用的 token
#:    会被算进前一天。取前十个字符就是取出当时写下的那一天。
#:    这也正是视图 ``v_cost_daily`` 用的写法。
_USAGE_GROUPS: dict[str, str] = {
    "day": "substr(created_at, 1, 10)",
    "purpose": "purpose",
    "model": "model",
}


class SqliteUsageRepository:
    """``llm_usage`` 这张表的两个方向。

    结构上满足 :class:`~alterego.interfaces.llm.UsageSink`，所以网关不需要
    认识这个类名，也不必 import 存储层。另一半（统计页要的
    :meth:`totals`）满足 :class:`~alterego.interfaces.repository.UsageRepository`。
    一个类管两个方向是因为它们读写的是**同一张表**，而拆成两个类之后，
    「按天分组用 ``substr(created_at, 1, 10)``」这种口径会各写一遍。

    ``persona_id`` 与 ``tick_id`` 在**构造时**绑定，因为一次 tick 造一个 sink，
    而 ``record()`` 的签名里没有位置放它们。这条只约束**写**的那一半：
    :meth:`totals` 的 ``persona_id`` 由调用方传（界面看的是整段窗口，
    不是某一次 tick）。

    ``cost_usd`` 目前恒为 0：本项目还没有价目表，记一个猜出来的数字比记 0
    更糟——猜的数字会被当真。
    """

    __slots__ = ("_conn", "_persona_id", "_tick_id")

    def __init__(
        self, conn: SqliteConnection, *, persona_id: str, tick_id: str | None = None
    ) -> None:
        self._conn = conn
        self._persona_id = persona_id
        self._tick_id = tick_id

    def record(self, usage: LLMUsage) -> None:
        self.record_many([usage])

    def record_many(self, usages: list[LLMUsage]) -> None:
        if not usages:
            return
        self._conn.raw.executemany(
            _LLM_USAGE_INSERT,
            [self._params(usage) for usage in usages],
        )

    def _params(self, usage: LLMUsage) -> tuple[Any, ...]:
        return (
            uuid.uuid4().hex,
            self._persona_id,
            self._tick_id,
            usage.purpose,
            usage.tier,
            usage.provider_id,
            usage.model,
            int(usage.prompt_tokens),
            int(usage.completion_tokens),
            int(usage.total_tokens),
            int(usage.latency_ms),
            0.0,  # cost_usd：见类文档
            0,  # cache_hit：本批次还没有缓存层
            int(usage.retry_count),
            1 if usage.success else 0,
            usage.error,
            _format_dt(usage.at) if usage.at is not None else _now_iso(),
        )

    def totals(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        group: UsageGroup = "purpose",
    ) -> list[UsageTotal]:
        """:class:`~alterego.interfaces.repository.UsageRepository` 的实现。

        读的时候 ``persona_id`` 由**调用方给**，而不是用构造时绑的那个：
        构造时绑它是因为「一次 tick 一个 sink」，而看账本的是另一个人
        （界面想看整段窗口）。同一个对象两个方向各管一半，所以这里写明。

        ``total_tokens`` 不取库里那一列，改成把两个和相加：那一列本来就是
        ``prompt + completion``（见 ``LLMUsage.total_tokens``），
        两处各算一遍迟早会分叉。
        """
        column = _USAGE_GROUPS.get(group)
        if column is None:
            raise ValueError(f"不认识的用量分组：{group!r}")
        # 顺序：按天要能顺着时间读，按用途/模型要一眼看到最花钱的。
        # 第二排序键永远是 key——同量时的行序不能交给 SQLite 自己定（P6）。
        order = f"{column} ASC" if group == "day" else "total_tokens DESC, key ASC"
        rows = self._conn.query(
            f"SELECT {column} AS key, COUNT(*) AS calls, "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed, "
            "SUM(prompt_tokens) AS prompt_tokens, "
            "SUM(completion_tokens) AS completion_tokens, "
            "SUM(prompt_tokens + completion_tokens) AS total_tokens "
            "FROM llm_usage "
            "WHERE persona_id = ? AND created_at >= ? AND created_at < ? "
            f"GROUP BY key ORDER BY {order}",
            (persona_id, _format_dt(since), _format_dt(until)),
        )
        return [
            UsageTotal(
                key=str(row["key"] or ""),
                calls=int(row["calls"] or 0),
                failed=int(row["failed"] or 0),
                prompt_tokens=int(row["prompt_tokens"] or 0),
                completion_tokens=int(row["completion_tokens"] or 0),
            )
            for row in rows
        ]

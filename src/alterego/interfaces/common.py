"""所有能力接口共用的最小类型。"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = ["HealthStatus"]


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """健康检查结果。

    ``hint`` 不是装饰：``alterego plugins doctor`` 会把它原样打给用户，
    所以它应该是一条**可执行的**建议（``运行 alterego plugins reset <id>``），
    而不是「出错了」这种说了等于没说的话。
    """

    ok: bool
    detail: str = ""
    hint: str | None = None

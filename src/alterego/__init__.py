"""AlterEgo · 拟我

一个会自己生活、自己思考、按自己的节奏找你的数字存在。

设计文档以 ``docs/DESIGN.md`` 为唯一权威来源（Single Source of Truth），
分册位于 ``docs/design/``，架构决策记录位于 ``docs/adr/``。

注意：本模块**刻意不 import 任何子模块**。
``import alterego`` 必须是廉价的——CLI 启动时间目标 ≤ 1 秒，
见 ``docs/adr/0002-use-python-as-implementation-language.md``。
"""

from __future__ import annotations


__version__ = "0.1.0"

__all__ = ["__version__"]

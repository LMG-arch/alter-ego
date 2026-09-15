"""推演层：把「现在发生了什么」变成「它打算做什么」。

推演层的红线是**只用抽象**：不直接依赖某个存储实现、某个模型供应商、某个渠道，
也不使用全局随机与墙上时钟。它要的一切都从 ``PluginContext`` 里取。

依据: docs/design/01-architecture.md § 4
"""

from __future__ import annotations


__all__: list[str] = []

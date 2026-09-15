"""NPC 模拟：Agent 生活里的其他人。

与主体推演**分开跑**，理由有两个：

1. 别人不该抢主人的注意力——同一套 tick 里同时推进所有人的日程，
   会让主循环越来越重，成本与延迟都会失控；
2. 独立性本身就是拟人化的一部分。NPC 有自己的节奏，
   Agent 只能**观察**到他们的动静，不能替他们决定。

NPC 只产生「可被感知的事件」，至于 Agent 要不要因此生出某个意图，
仍然由推演层按同样的规则判断。

依据: docs/design/04-simulation-loop.md § 8
"""

from __future__ import annotations


__all__: list[str] = []

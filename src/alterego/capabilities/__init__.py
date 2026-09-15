"""内置 capability 插件的宿主。

capability 是「Agent 能做什么事」的抽象（读新闻、查天气、记账…），
与「Agent 想做什么」的意图分开：意图层只说自己想干什么，
具体怎么干由 capability 决定，因此**加一个能力不需要改推演引擎**
（设计原则 P4：可插拔优于可配置）。

依据: docs/design/02-plugin-api.md § 2、§ 6.4
"""

from __future__ import annotations


__all__: list[str] = []

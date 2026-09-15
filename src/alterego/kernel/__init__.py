"""内核层。

内核知道**机制**，不知道**实现**：

- 知道「有一个事件总线」「有一个服务注册表」「有一个时钟」
- 不知道「事件会发到哪个 IM」「服务由哪种存储引擎提供」「时间会倍速」

红线由 ``scripts/check_architecture.sh`` 第 1 组机械校验：``kernel/`` 下不得
出现任何**具体技术名词**——存储引擎、模型厂商、HTTP 客户端库、IM 平台的名字
都不行（否则这个 docstring 自己就会触发检查）；也不得 import 任何上层模块。

「这个发行版默认挑了哪个实现」属于数据，写在 ``alterego/defaults.toml`` 里，
不写在内核代码里。

本模块不做 re-export。请从具体子模块 import，例如::

    from alterego.kernel.bus import EventBus
    from alterego.kernel.clock import FrozenClock

依据: docs/design/01-architecture.md § 2
"""

from __future__ import annotations


__all__: list[str] = []

"""跨层共享的纯数据契约。

这一层存在的唯一理由：**上层和下层都需要知道同一个形状，但谁都不该 import 谁。**

例如 ``Channel`` 协议由 ``channels/`` 的实现满足、由 ``sim/`` 的推演流程调用，
而 ``kernel/`` 只负责把它注册进注册表。三方都需要 ``OutboundMessage`` 的定义，
但 ``sim/`` 不该 import ``channels/``（会反向依赖），``kernel/`` 更不该认识
``channels/`` 里的任何东西。把形状抽到这里，三方都只依赖 ``interfaces``。

因此本层有三个特征：

1. **只有数据类与 ``Protocol``**，没有任何实现、没有任何 IO。
2. **不 import 任何 ``alterego`` 子包**（``domain`` / ``sim`` / ``llm`` … 都不行），
   否则就会形成环。需要引用上层类型时用 ``TYPE_CHECKING`` + 字符串注解。
3. **不认识任何具体技术**（P1）：没有 provider 名、引擎名、平台名。

依据: docs/design/02-plugin-api.md § 6
"""

from __future__ import annotations

from alterego.interfaces.channel import (
    Channel,
    InboundMessage,
    OutboundMessage,
    SendResult,
)
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import (
    EmbeddingProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from alterego.interfaces.simulation import (
    Capability,
    CapabilityResult,
    IntentType,
    PromptSource,
    Stage,
    StageResult,
    Tool,
)
from alterego.interfaces.storage import StorageBackend


__all__ = [
    "Capability",
    "CapabilityResult",
    "Channel",
    "EmbeddingProvider",
    "HealthStatus",
    "InboundMessage",
    "IntentType",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "OutboundMessage",
    "PromptSource",
    "SendResult",
    "Stage",
    "StageResult",
    "StorageBackend",
    "Tool",
]

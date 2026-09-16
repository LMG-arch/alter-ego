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

**这个 ``__init__`` 转发六个子模块的全部公开名字**，一个不漏。它必须一个不漏，
因为「部分转发」比「完全不转发」更坏：插件作者写 ``from alterego.interfaces import
PersonaRecord`` 时，拿到 ``ImportError`` 的人会以为是自己的写法错了，接着去翻源码——
而正确的结论只是「这个包没把它导出来」。要么全导，要么一个都不导，没有中间地带。

两种写法都合法，选一种：

```python
from alterego.interfaces import Channel, OutboundMessage  # 一次拿到
from alterego.interfaces.channel import Channel  # 指名道姓（本项目内部一律用这种）
```

依据: docs/design/02-plugin-api.md § 6、docs/guide/plugin-development.md § 3
"""

from __future__ import annotations

from alterego.interfaces.channel import (
    Channel,
    ChannelCapability,
    Direction,
    InboundMessage,
    MessageKind,
    OutboundMessage,
    SendResult,
)
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import (
    EmbeddingProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    UsageSink,
)
from alterego.interfaces.repository import (
    ActivityRecord,
    ActivityRepository,
    BudgetRepository,
    BudgetUsage,
    ConversationRecord,
    ConversationRepository,
    DatasetSourceRepository,
    EmotionRepository,
    MemoryRepository,
    MessageRecord,
    PersonaRecord,
    PersonaRepository,
    ScheduleRecord,
    ScheduleRepository,
    SocialPostRecord,
    SocialPostRepository,
    SourceRecord,
    SourceRepository,
    TickLogDraft,
    TickLogRepository,
    UsageGroup,
    UsageRepository,
    UsageTotal,
)
from alterego.interfaces.simulation import (
    Capability,
    CapabilityResult,
    Intent,
    IntentType,
    Percepts,
    PromptSource,
    Stage,
    StageResult,
    Tool,
)
from alterego.interfaces.storage import StorageBackend


__all__ = [
    "ActivityRecord",
    "ActivityRepository",
    "BudgetRepository",
    "BudgetUsage",
    "Capability",
    "CapabilityResult",
    "Channel",
    "ChannelCapability",
    "ConversationRecord",
    "ConversationRepository",
    "DatasetSourceRepository",
    "Direction",
    "EmbeddingProvider",
    "EmotionRepository",
    "HealthStatus",
    "InboundMessage",
    "Intent",
    "IntentType",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "MemoryRepository",
    "MessageKind",
    "MessageRecord",
    "OutboundMessage",
    "Percepts",
    "PersonaRecord",
    "PersonaRepository",
    "PromptSource",
    "ScheduleRecord",
    "ScheduleRepository",
    "SendResult",
    "SocialPostRecord",
    "SocialPostRepository",
    "SourceRecord",
    "SourceRepository",
    "Stage",
    "StageResult",
    "StorageBackend",
    "TickLogDraft",
    "TickLogRepository",
    "Tool",
    "UsageGroup",
    "UsageRepository",
    "UsageSink",
    "UsageTotal",
]

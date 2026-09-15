"""模型供应商契约。

内核与本层都不知道「哪家供应商」——只认识 :class:`LLMProvider` 这个形状。
具体是哪种 API（OpenAI 兼容、Anthropic、本地推理服务……）由 ``llm`` 类插件实现。

依据: docs/design/02-plugin-api.md § 6.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from alterego.interfaces.common import HealthStatus


__all__ = ["EmbeddingProvider", "LLMProvider", "LLMRequest", "LLMResponse"]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """一次模型调用请求。"""

    prompt: str
    system: str | None = None
    temperature: float = 0.8
    max_tokens: int = 1024
    stop: tuple[str, ...] = ()
    response_format: Literal["text", "json"] = "text"
    json_schema: dict[str, Any] | None = None
    timeout_sec: float = 60.0
    #: 计量与路由用的附加上下文，例如 ``{"tier": "cheap", "purpose": "npc"}``。
    #: 供应商**不应该**把它发给 API——它是本项目自己的账本。
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次模型调用结果。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    latency_ms: int = 0
    raw: dict[str, Any] | None = None


class LLMProvider(Protocol):
    """模型供应商。

    实现者请遵守两条约定：

    - ``complete()`` 失败时抛 :class:`~alterego.kernel.errors.LLMError` 的子类，
      让重试策略能区分「限流（可重试）」与「配额用尽（不可重试）」。
    - 不要在这里做重试与计量——那是 :mod:`alterego.llm` 的职责。
      供应商只管「一次调用」。
    """

    id: str
    tier: Literal["strong", "cheap", "custom"]
    models: tuple[str, ...]

    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None: ...

    def health_check(self) -> HealthStatus: ...


class EmbeddingProvider(Protocol):
    """向量化供应商（可选能力）。

    缺失时记忆检索退化为纯关键词检索——功能降级，但不是故障。
    见 docs/design/03-data-model.md 的检索流程。
    """

    id: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

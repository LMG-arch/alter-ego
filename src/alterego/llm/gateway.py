"""LLM 网关：路由、重试、计量。

一次调用从这里进去，会经过四道关：

1. **路由** —— 用途（``"memory"``）→ 档位（``[llm.routing] memory = "cheap"``）
   → 供应商 id（``[llm.routing] cheap = "deepseek"``）→ 注册表里的实例；
2. **重试** —— 只重试标了 ``retryable`` 的失败，别的立刻上抛；
3. **计量** —— **每一次尝试**都记一条 :class:`LLMUsage`，包括失败的那几次
   （只记成功的账会让「重试率」这个数字永远消失）；
4. **日志** —— 失败 WARNING，成功 DEBUG。

**为什么这四件事必须在一处**：``scripts/check_architecture.sh`` 第 7 组红线
禁止 ``sim/`` 与 ``capabilities/`` 直连 LLM 端点。绕过去的调用不会进
``llm_usage``、会跳过路由、会绕过预算闸门——这三条都只有在月底看账单时
才会被发现。把四件事绑在同一个入口上，是让「绕过」变成一件需要主动破坏
封装才能做到的事（P3：机制约束优于提示词祈祷）。

依据: docs/design/07-model-routing-and-media.md § 2、docs/design/09-observability.md
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from alterego.interfaces.llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    UsageSink,
)
from alterego.kernel.config import LLMRoutingConfig
from alterego.kernel.errors import ConfigError, LLMError
from alterego.kernel.logging import get_logger
from alterego.kernel.registry import ServiceRegistry


__all__ = ["LLMGateway", "LLMUsage", "UsageSink"]


#: 路由表里被当作「档位」的两个键（值是一个供应商名）。
_TIERS: Final[tuple[str, ...]] = ("strong", "cheap")

#: 路由表里除档位以外的键都是**用途**。从配置类的字段推导，
#: 而不是另抄一份清单——抄的那份一定会在加字段那天忘记同步。
_KNOWN_PURPOSES: Final[frozenset[str]] = frozenset(
    name for name in LLMRoutingConfig.__dataclass_fields__ if name not in _TIERS
)

_INITIAL_BACKOFF_SEC: Final[float] = 0.5
_MAX_BACKOFF_SEC: Final[float] = 8.0

#: 登记处里 LLM 供应商的键。
#:
#: 这么写是因为 mypy 的 ``type-abstract`` 不允许把 Protocol 当成
#: ``type[...]`` 传——而 :class:`ServiceRegistry` 的接口参数**就是**用来传接口类的。
#: 注册与取出用的是同一个类对象，这里没有任何抽象类被实例化。
_PROVIDER_KEY: Final[type[LLMProvider]] = LLMProvider  # type: ignore[type-abstract]

_log = get_logger("llm.gateway")


class LLMGateway:
    """按用途路由、重试并计量的模型调用入口。

    上层只表达**意图**（「我在做记忆巩固」），不表达**去哪家、用哪个模型**。
    后者是配置的事，见 `[llm.routing]`。

    Args:
        registry: 服务登记处，用来按名字找 :class:`LLMProvider`。
        routing: `[llm.routing]` 的解析结果。
        max_retries: 单次调用最多再试几次（不含第一次）。
        usage_sink: 用量账本。`None` 表示只记日志不落库（测试用）。
        logger: 日志器，默认用模块级那个。
        sleep: 重试前的等待函数，测试注入一个不真等的。
        now: 时间来源，测试注入固定时钟。
    """

    __slots__ = ("_logger", "_max_retries", "_now", "_registry", "_routing", "_sink", "_sleep")

    def __init__(
        self,
        registry: ServiceRegistry,
        routing: LLMRoutingConfig,
        *,
        max_retries: int = 3,
        usage_sink: UsageSink | None = None,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ConfigError("max_retries 不能为负", max_retries=max_retries)
        self._registry = registry
        self._routing = routing
        self._max_retries = max_retries
        self._sink = usage_sink
        self._logger = logger if logger is not None else _log
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    # ── 路由 ────────────────────────────────────────────────

    def resolve(self, purpose: str, *, tier: str | None = None) -> tuple[str, str]:
        """把一个用途解析成「档位 + 供应商名」。

        Args:
            purpose: 用途名，如 ``"memory"``。
            tier: 直接指定档位，跳过用途查表。给测试与灰度用；
                正常情况下应该留 `None`，让配置说了算。

        Returns:
            ``(档位, 供应商名)``。

        Raises:
            ConfigError: 用途没配置、用途指向了不存在的档位、
                或该档位没有指向任何供应商。**一律报错，不猜。**
        """
        if tier is None:
            if purpose not in _KNOWN_PURPOSES:
                raise ConfigError(
                    "没有这个 LLM 用途",
                    purpose=purpose,
                    known=", ".join(sorted(_KNOWN_PURPOSES)),
                    hint="用途是代码里的概念：新增它要在 LLMRoutingConfig 里加一个字段。",
                )
            configured = getattr(self._routing, purpose, "")
            if not configured:
                raise ConfigError(
                    "这个 LLM 用途没有配置档位",
                    purpose=purpose,
                    hint=f'在 [llm.routing] 里写 {purpose} = "cheap"。',
                )
            tier = configured

        if tier not in _TIERS:
            # 猜错的代价是「用强模型跑了一万次廉价调用」，而且要到月底
            # 看账单才会发现——所以宁可在这里失败。
            raise ConfigError(
                "LLM 用途指向了不存在的档位",
                purpose=purpose,
                tier=tier,
                known=", ".join(_TIERS),
                hint="[llm.routing] 里用途键的值只能是 strong 或 cheap。",
            )

        provider_id = getattr(self._routing, tier, "")
        if not provider_id:
            raise ConfigError(
                "LLM 档位没有指向供应商",
                tier=tier,
                used_by=purpose,
                hint=f'在 [llm.routing] 里写 {tier} = "<供应商名>"。',
            )
        return tier, provider_id

    def provider_for(self, provider_id: str) -> LLMProvider:
        """按名字取供应商实例。

        Raises:
            ConfigError: 没注册过这个名字。
        """
        provider = self._registry.get_optional(_PROVIDER_KEY, name=provider_id)
        if provider is None:
            raise ConfigError(
                "找不到路由指定的模型供应商",
                provider=provider_id,
                registered=", ".join(self._registry.names(_PROVIDER_KEY)) or "（一个都没有）",
                hint="检查 [llm.routing] 里的名字是否与已配置的 [llm.providers.<name>] 一致。",
            )
        return provider

    # ── 调用 ────────────────────────────────────────────────

    async def complete(
        self,
        purpose: str,
        prompt: str,
        *,
        tier: str | None = None,
        system: str | None = None,
        temperature: float = 0.8,
        max_tokens: int = 1024,
        stop: tuple[str, ...] = (),
        response_format: Literal["text", "json"] = "text",
        json_schema: Mapping[str, Any] | None = None,
        timeout_sec: float = 60.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        """按用途发一次调用。

        参数逐个对应 :class:`LLMRequest` 的字段；``metadata`` 是额外塞进
        请求里的自定义信息（如 ``{"correlation_id": tick_id}``），
        **不会被发给服务商**。

        Raises:
            ConfigError: 路由不可解析。
            LLMError: 模型调用失败（重试耗尽或不可重试）。
        """
        resolved_tier, provider_id = self.resolve(purpose, tier=tier)
        provider = self.provider_for(provider_id)

        request = LLMRequest(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            response_format=response_format,
            json_schema=dict(json_schema) if json_schema is not None else None,
            timeout_sec=timeout_sec,
            metadata={"purpose": purpose, "tier": resolved_tier, **dict(metadata or {})},
        )

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await provider.complete(request)
            except LLMError as exc:
                # 失败的那几次也要记账。只记成功的账，「重试率」这个数字就永远
                # 消失了——而它正是判断「是不是该换一个供应商」的唯一依据。
                self._record(purpose, resolved_tier, provider, failure=exc, retry_count=attempt - 1)
                if not exc.retryable or attempt > self._max_retries:
                    raise
                delay = self._backoff(attempt)
                self._logger.warning(
                    "模型调用失败，%.1fs 后重试（%s · %s · 第 %d 次）：%s",
                    delay,
                    purpose,
                    provider_id,
                    attempt,
                    exc,
                )
                await self._sleep(delay)
                continue

            self._record(
                purpose, resolved_tier, provider, response=response, retry_count=attempt - 1
            )
            return response

    async def text(self, purpose: str, prompt: str, **kwargs: Any) -> str:
        """只要正文。等价于 ``(await self.complete(...)).text``。"""
        response = await self.complete(purpose, prompt, **kwargs)
        return response.text

    # ── 内部 ────────────────────────────────────────────────

    @staticmethod
    def _backoff(attempt: int) -> float:
        """第 ``attempt`` 次尝试失败后等多久（``attempt`` 从 1 开始）。

        指数退避，封顶 8 秒。**不加抖动**：抖动需要随机源，而随机源会破坏
        可复现性（P6），而本项目的 LLM 调用是串行的——一个 tick 里不会
        有十个请求同时撞上限流。抖动解决的是并发惊群，这里没有这个问题。
        """
        return min(_INITIAL_BACKOFF_SEC * float(2 ** (attempt - 1)), _MAX_BACKOFF_SEC)

    def _record(
        self,
        purpose: str,
        tier: str,
        provider: LLMProvider,
        *,
        response: LLMResponse | None = None,
        failure: LLMError | None = None,
        retry_count: int = 0,
    ) -> None:
        """记一条账。

        `provider` 上取模型名用 `getattr` 而不是属性访问：
        :class:`LLMProvider` 协议里没有 `model`——各家供应商叫法不同，
        取不到就记空串。少一个模型名，好过因为记不上账而让调用失败。
        """
        model = (
            response.model if response is not None else str(getattr(provider, "model", "") or "")
        )
        usage = LLMUsage(
            purpose=purpose,
            tier=tier,
            provider_id=provider.id,
            model=model,
            prompt_tokens=response.prompt_tokens if response is not None else 0,
            completion_tokens=response.completion_tokens if response is not None else 0,
            latency_ms=response.latency_ms if response is not None else 0,
            retry_count=retry_count,
            success=failure is None,
            error=None if failure is None else f"{failure.code}: {failure.message}",
            at=self._now(),
        )

        if self._sink is None:
            return
        try:
            self._sink.record(usage)
        except Exception:
            # 记账失败**不能**盖住调用结果：一次成功的调用不该因为
            # 「账没记上」而变成失败调用。但必须留下痕迹。
            self._logger.warning("记录 LLM 用量失败", exc_info=True)

"""``alterego.llm.gateway`` 的测试。

网关是**唯一**允许发模型请求的地方（红线 7），它一个人扛四件事：
路由、重试、计量、日志。所以这一组用例围着这四件事转。

账本那部分尤其要盯住：**每一次尝试都要记一条**，包括失败的那几次。
只记成功的账，会让「重试率」这个数字永远消失——而它正是判断
「是不是该换个供应商」的唯一依据。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMProvider, LLMRequest, LLMResponse, LLMUsage
from alterego.kernel.config import LLMRoutingConfig
from alterego.kernel.errors import ConfigError, LLMError, LLMRateLimitError
from alterego.kernel.registry import ServiceRegistry
from alterego.llm import LLMGateway
from alterego.llm.gateway import _MAX_BACKOFF_SEC


AT = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
LOGGER = logging.getLogger("test.gateway")


# ── 假的协作者 ──────────────────────────────────────────────


class FakeProvider:
    """按剧本回答的供应商。剧本里放 :class:`LLMError` 就抛，放响应就返回。"""

    id = "p1"
    tier = "cheap"
    models = ("m1",)
    model = "m1"

    def __init__(self, script: Sequence[Any] = ()) -> None:
        self.script = list(script)
        self.calls: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        item = self.script.pop(0) if self.script else LLMResponse(text="好的", model="m1")
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        return None

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")


class FakeSink:
    """内存账本。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.usages: list[LLMUsage] = []
        self.explode = explode

    def record(self, usage: LLMUsage) -> None:
        if self.explode:
            raise RuntimeError("账本坏了")
        self.usages.append(usage)

    def record_many(self, usages: Sequence[LLMUsage]) -> None:
        for usage in usages:
            self.record(usage)


def _routing(**overrides: str) -> LLMRoutingConfig:
    values: dict[str, str] = {"strong": "p1", "cheap": "p1", "memory": "cheap"}
    values.update(overrides)
    return LLMRoutingConfig(**values)


def _gateway(
    provider: FakeProvider | None = None,
    *,
    routing: LLMRoutingConfig | None = None,
    sink: FakeSink | None = None,
    max_retries: int = 3,
    slept: list[float] | None = None,
) -> tuple[LLMGateway, list[float]]:
    """装一台网关，返回它和「等了多久」这个列表。"""
    provider = provider or FakeProvider()
    registry = ServiceRegistry(logger=LOGGER)
    # 键必须是**接口**：注册表按类型索引，用具体类注册、拿 Protocol 去查
    # 只会查出「一个都没有」。
    registry.register(LLMProvider, provider, name=provider.id)  # type: ignore[type-abstract]
    delays = slept if slept is not None else []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    gateway = LLMGateway(
        registry,
        routing or _routing(),
        max_retries=max_retries,
        usage_sink=sink,
        logger=LOGGER,
        sleep=_sleep,
        now=lambda: AT,
    )
    return gateway, delays


# ────────────────────────────────────────────────────────────
# 路由
# ────────────────────────────────────────────────────────────


class TestResolve:
    def test_a_purpose_maps_to_a_tier_and_a_provider(self) -> None:
        gateway, _ = _gateway()
        assert gateway.resolve("memory") == ("cheap", "p1")

    def test_an_explicit_tier_skips_the_lookup(self) -> None:
        gateway, _ = _gateway(routing=_routing(memory="strong"))
        assert gateway.resolve("memory", tier="cheap") == ("cheap", "p1")

    def test_an_unknown_purpose_is_refused(self) -> None:
        """用途是代码里的概念。拼错一个词不该悄悄用上默认档位。"""
        gateway, _ = _gateway()
        with pytest.raises(ConfigError, match="没有这个 LLM 用途"):
            gateway.resolve("memroy")

    def test_an_unconfigured_purpose_is_refused(self) -> None:
        gateway, _ = _gateway(routing=_routing(npc=""))
        with pytest.raises(ConfigError, match="没有配置档位"):
            gateway.resolve("npc")

    def test_a_purpose_pointing_at_a_bogus_tier_is_refused(self) -> None:
        """猜错的代价是「用强模型跑了一万次廉价调用」，要到月底看账单才发现。"""
        gateway, _ = _gateway(routing=_routing(memory="medium"))
        with pytest.raises(ConfigError, match="不存在的档位"):
            gateway.resolve("memory")

    def test_a_tier_without_a_provider_is_refused(self) -> None:
        gateway, _ = _gateway(routing=_routing(cheap=""))
        with pytest.raises(ConfigError, match="档位没有指向供应商"):
            gateway.resolve("memory")

    def test_a_provider_that_was_never_registered_is_refused(self) -> None:
        gateway, _ = _gateway(routing=_routing(cheap="ghost"))
        with pytest.raises(ConfigError, match="找不到路由指定的模型供应商") as caught:
            gateway.provider_for("ghost")

        assert caught.value.context["provider"] == "ghost"
        # 把「注册了哪些」一起打出来，否则只能对着配置干瞪眼。
        assert "p1" in str(caught.value.context["registered"])

    def test_a_negative_retry_budget_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="不能为负"):
            LLMGateway(ServiceRegistry(logger=LOGGER), _routing(), max_retries=-1)


# ────────────────────────────────────────────────────────────
# 重试
# ────────────────────────────────────────────────────────────


class TestRetry:
    async def test_a_successful_call_does_not_wait(self) -> None:
        gateway, delays = _gateway()
        response = await gateway.complete("memory", "在吗")

        assert response.text == "好的"
        assert delays == []

    async def test_it_retries_a_retryable_failure(self) -> None:
        provider = FakeProvider([LLMRateLimitError("限流", provider="p1")])
        gateway, delays = _gateway(provider)

        response = await gateway.complete("memory", "在吗")

        assert response.text == "好的"
        assert delays == [0.5]

    async def test_the_backoff_doubles(self) -> None:
        provider = FakeProvider(
            [
                LLMRateLimitError("限流", provider="p1"),
                LLMRateLimitError("限流", provider="p1"),
                LLMRateLimitError("限流", provider="p1"),
            ]
        )
        gateway, delays = _gateway(provider, max_retries=5)

        await gateway.complete("memory", "在吗")

        assert delays == [0.5, 1.0, 2.0]

    async def test_a_non_retryable_failure_is_not_retried(self) -> None:
        """重试一百次也一样，那就一次都别重试。"""
        provider = FakeProvider([LLMError("模型请求被拒绝", provider="p1")])
        gateway, delays = _gateway(provider)

        with pytest.raises(LLMError, match="被拒绝"):
            await gateway.complete("memory", "在吗")

        assert len(provider.calls) == 1
        assert delays == []

    async def test_it_gives_up_after_the_retry_budget(self) -> None:
        provider = FakeProvider([LLMRateLimitError("限流", provider="p1")] * 10)
        gateway, delays = _gateway(provider, max_retries=2)

        with pytest.raises(LLMRateLimitError):
            await gateway.complete("memory", "在吗")

        # 一次原始 + 两次重试。
        assert len(provider.calls) == 3
        assert delays == [0.5, 1.0]

    async def test_the_backoff_is_capped(self) -> None:
        """串行调用没有惊群问题，所以不加抖动（P6）。"""
        assert LLMGateway._backoff(1) == 0.5
        assert LLMGateway._backoff(20) == _MAX_BACKOFF_SEC


# ────────────────────────────────────────────────────────────
# 计量
# ────────────────────────────────────────────────────────────


class TestAccounting:
    async def test_a_successful_call_records_one_usage(self) -> None:
        sink = FakeSink()
        provider = FakeProvider([LLMResponse(text="好的", model="m1", prompt_tokens=7)])
        gateway, _ = _gateway(provider, sink=sink)

        await gateway.complete("memory", "在吗")

        assert len(sink.usages) == 1
        usage = sink.usages[0]
        assert usage.purpose == "memory"
        assert usage.tier == "cheap"
        assert usage.provider_id == "p1"
        assert usage.model == "m1"
        assert usage.prompt_tokens == 7
        assert usage.success
        assert usage.error is None
        assert usage.at == AT

    async def test_every_failed_attempt_is_recorded_too(self) -> None:
        """只记成功的账，「重试率」这个数字就永远消失了。"""
        sink = FakeSink()
        provider = FakeProvider([LLMRateLimitError("限流", provider="p1")] * 4)
        gateway, _ = _gateway(provider, sink=sink, max_retries=2)

        with pytest.raises(LLMRateLimitError):
            await gateway.complete("memory", "在吗")

        assert len(sink.usages) == 3
        assert [usage.retry_count for usage in sink.usages] == [0, 1, 2]
        assert all(not usage.success for usage in sink.usages)

    async def test_a_recovered_call_reports_how_many_retries_it_took(self) -> None:
        sink = FakeSink()
        provider = FakeProvider(
            [
                LLMRateLimitError("限流", provider="p1"),
                LLMResponse(text="好的", model="m1"),
            ]
        )
        gateway, _ = _gateway(provider, sink=sink)

        await gateway.complete("memory", "在吗")

        assert [usage.success for usage in sink.usages] == [False, True]
        assert sink.usages[-1].retry_count == 1

    async def test_a_failure_carries_its_code_into_the_ledger(self) -> None:
        sink = FakeSink()
        provider = FakeProvider([LLMError("模型请求被拒绝", provider="p1")])
        gateway, _ = _gateway(provider, sink=sink)

        with pytest.raises(LLMError):
            await gateway.complete("memory", "在吗")

        # 记的是错误码而不是异常类名——报表按码聚合，类名会随重构变。
        assert sink.usages[0].error == "llm_error: 模型请求被拒绝"

    async def test_a_broken_sink_does_not_break_the_call(self) -> None:
        """一次成功的调用不该因为「账没记上」而变成失败调用。"""
        provider = FakeProvider([LLMResponse(text="好的", model="m1")])
        gateway, _ = _gateway(provider, sink=FakeSink(explode=True))

        response = await gateway.complete("memory", "在吗")

        assert response.text == "好的"

    async def test_without_a_sink_nothing_is_recorded(self) -> None:
        gateway, _ = _gateway()
        response = await gateway.complete("memory", "在吗")
        assert response.text == "好的"


# ────────────────────────────────────────────────────────────
# 请求的组装
# ────────────────────────────────────────────────────────────


class TestRequestShape:
    async def test_the_purpose_and_tier_are_stamped_into_metadata(self) -> None:
        """供应商看不到 metadata，但日志与排查能看到。"""
        provider = FakeProvider()
        gateway, _ = _gateway(provider)

        await gateway.complete("memory", "在吗", metadata={"correlation_id": "t1"})

        assert provider.calls[0].metadata == {
            "purpose": "memory",
            "tier": "cheap",
            "correlation_id": "t1",
        }

    async def test_every_parameter_reaches_the_request(self) -> None:
        provider = FakeProvider()
        gateway, _ = _gateway(provider)

        await gateway.complete(
            "memory",
            "在吗",
            system="你是林晚",
            temperature=0.4,
            max_tokens=2048,
            stop=("。",),
            response_format="json",
            json_schema={"type": "object"},
            timeout_sec=30.0,
        )

        request = provider.calls[0]
        assert request.system == "你是林晚"
        assert request.temperature == 0.4
        assert request.max_tokens == 2048
        assert request.stop == ("。",)
        assert request.response_format == "json"
        assert request.json_schema == {"type": "object"}
        assert request.timeout_sec == 30.0

    async def test_text_returns_just_the_body(self) -> None:
        provider = FakeProvider([LLMResponse(text=" 只有正文 ", model="m1")])
        gateway, _ = _gateway(provider)

        assert await gateway.text("memory", "在吗") == " 只有正文 "

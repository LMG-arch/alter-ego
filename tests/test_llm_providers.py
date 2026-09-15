"""``alterego.llm.providers.openai_compatible`` 的测试。

不联真实网络：`httpx.MockTransport` 把请求拦在进程里。要测的也不是
「服务商会不会好好回话」，而是**这一层有没有把状态码与响应形状翻成
正确的异常类型**——网关靠它决定「等一会再试」还是「立刻失败」。

所以每条用例都同时断言两件事：异常类型，以及 ``retryable``。
"""

from __future__ import annotations

import json

import httpx
import pytest

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest
from alterego.kernel.errors import (
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
)
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.llm.providers.openai_compatible import _int_or_zero, _retry_after, _short_body


BASE_URL = "https://example.test/v1"


class _Transport:
    """拦住请求，记下它，然后按剧本回答。"""

    def __init__(self, response: httpx.Response | None = None, *, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.response = response or _ok()
        self.error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response

    @property
    def payload(self) -> dict[str, object]:
        return json.loads(self.requests[-1].content.decode("utf-8"))

    @property
    def url(self) -> str:
        return str(self.requests[-1].url)


def _ok(
    *,
    content: str = "你好",
    finish_reason: str = "stop",
    usage: dict[str, object] | None = None,
    model: str = "m1",
) -> httpx.Response:
    body: dict[str, object] = {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _provider(
    handler: _Transport,
    *,
    model: str = "m1",
    api_key_env: str = "",
    environ: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return OpenAICompatibleProvider(
        "fake",
        base_url=BASE_URL,
        api_key_env=api_key_env,
        model=model,
        client=client,
        environ=environ,
        extra_headers=extra_headers,
    )


# ────────────────────────────────────────────────────────────
# 请求体
# ────────────────────────────────────────────────────────────


class TestRequestPayload:
    async def test_a_minimal_call(self) -> None:
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="在吗"))

        assert handler.payload["model"] == "m1"
        assert handler.payload["messages"] == [{"role": "user", "content": "在吗"}]

    async def test_the_system_prompt_comes_first(self) -> None:
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="在吗", system="你是林晚"))

        messages = handler.payload["messages"]
        assert isinstance(messages, list)
        assert messages[0] == {"role": "system", "content": "你是林晚"}

    async def test_json_mode_is_translated(self) -> None:
        """本项目说 ``response_format="json"``，协议里要的是 ``{"type": "json_object"}``。"""
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="甲", response_format="json"))

        assert handler.payload["response_format"] == {"type": "json_object"}

    async def test_text_mode_sends_no_response_format(self) -> None:
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert "response_format" not in handler.payload

    async def test_stop_sequences_become_a_list(self) -> None:
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="甲", stop=("。", "\n")))

        assert handler.payload["stop"] == ["。", "\n"]

    async def test_metadata_never_leaves_the_process(self) -> None:
        """``metadata`` 是本项目自己的账本，发给服务商只会被忽略或报错。"""
        handler = _Transport()
        await _provider(handler).complete(
            LLMRequest(prompt="甲", metadata={"correlation_id": "t1", "purpose": "memory"})
        )

        body = json.dumps(handler.payload)
        assert "correlation_id" not in body
        assert "purpose" not in body

    async def test_it_posts_to_chat_completions(self) -> None:
        handler = _Transport()
        await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert handler.url == f"{BASE_URL}/chat/completions"

    async def test_a_trailing_slash_in_base_url_is_dropped(self) -> None:
        """留着的话会拼出 ``//chat/completions``，有些服务商为此回 404。"""
        provider = OpenAICompatibleProvider("fake", base_url=f"{BASE_URL}/", model="m1")
        assert provider.base_url == BASE_URL

    async def test_a_missing_model_is_refused_before_the_request(self) -> None:
        handler = _Transport()
        with pytest.raises(LLMError, match="没有指定模型名"):
            await _provider(handler, model="").complete(LLMRequest(prompt="甲"))
        assert handler.requests == []


# ────────────────────────────────────────────────────────────
# 状态码 → 异常类型
# ────────────────────────────────────────────────────────────


class TestStatusMapping:
    async def test_429_is_retryable(self) -> None:
        handler = _Transport(httpx.Response(429, text="slow down", headers={"Retry-After": "3"}))
        with pytest.raises(LLMRateLimitError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.retryable
        assert caught.value.context["retry_after_sec"] == 3.0

    async def test_an_absurd_retry_after_is_capped(self) -> None:
        """等 60 秒不如直接失败——用户还在等回复，不是在等配额。"""
        handler = _Transport(httpx.Response(429, text="", headers={"Retry-After": "600"}))
        with pytest.raises(LLMRateLimitError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.context["retry_after_sec"] == 30.0

    async def test_a_missing_retry_after_defaults_to_one_second(self) -> None:
        handler = _Transport(httpx.Response(429, text=""))
        with pytest.raises(LLMRateLimitError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.context["retry_after_sec"] == 1.0

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_server_errors_are_retryable(self, status: int) -> None:
        """5xx 是服务端的事，等一会再来有真实概率成功。"""
        handler = _Transport(httpx.Response(status, text="oops"))
        with pytest.raises(LLMResponseError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.retryable
        assert caught.value.context["status"] == status

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_client_errors_are_not_retryable(self, status: int) -> None:
        """4xx 通常意味着请求本身不对：模型名不存在、密钥无效、参数越界。"""
        handler = _Transport(httpx.Response(status, json={"error": {"message": "bad"}}))
        with pytest.raises(LLMError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert not caught.value.retryable
        assert caught.value.context["status"] == status

    async def test_a_long_error_body_is_truncated(self) -> None:
        """一个 HTML 错误页不该把整段标签塞进 llm_usage.error。"""
        handler = _Transport(httpx.Response(500, text="<html>" + "x" * 5000 + "</html>"))
        with pytest.raises(LLMResponseError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert len(str(caught.value.context["detail"])) < 400


# ────────────────────────────────────────────────────────────
# 传输层
# ────────────────────────────────────────────────────────────


class TestTransport:
    async def test_a_timeout_is_retryable(self) -> None:
        handler = _Transport(error=httpx.ReadTimeout("timed out"))
        with pytest.raises(LLMTimeoutError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.retryable

    async def test_a_connection_failure_is_retryable(self) -> None:
        handler = _Transport(error=httpx.ConnectError("no route"))
        with pytest.raises(LLMTransportError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.retryable
        assert caught.value.context["reason"] == "ConnectError"


# ────────────────────────────────────────────────────────────
# 响应解析
# ────────────────────────────────────────────────────────────


class TestResponseParsing:
    async def test_a_normal_answer(self) -> None:
        handler = _Transport(_ok(usage={"prompt_tokens": 12, "completion_tokens": 3}))
        response = await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert response.text == "你好"
        assert response.model == "m1"
        assert response.prompt_tokens == 12
        assert response.completion_tokens == 3
        assert response.finish_reason == "stop"
        assert response.latency_ms >= 0

    async def test_usage_counts_written_as_strings_are_tolerated(self) -> None:
        """有些兼容实现会把 token 数写成字符串。"""
        handler = _Transport(_ok(usage={"prompt_tokens": "12", "completion_tokens": None}))
        response = await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert response.prompt_tokens == 12
        assert response.completion_tokens == 0

    async def test_a_non_json_body_is_a_retryable_failure(self) -> None:
        """偶发返回半截 JSON 是真实发生的事：它是「这次调用失败了」，不是 bug。"""
        handler = _Transport(httpx.Response(200, text="<html>gateway</html>"))
        with pytest.raises(LLMResponseError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert caught.value.retryable

    async def test_a_missing_choices_list_is_a_failure(self) -> None:
        handler = _Transport(httpx.Response(200, json={"model": "m1"}))
        with pytest.raises(LLMResponseError, match="没有 choices"):
            await _provider(handler).complete(LLMRequest(prompt="甲"))

    async def test_an_empty_choices_list_is_a_failure(self) -> None:
        handler = _Transport(httpx.Response(200, json={"choices": []}))
        with pytest.raises(LLMResponseError, match="没有 choices"):
            await _provider(handler).complete(LLMRequest(prompt="甲"))

    async def test_blank_content_is_a_failure(self) -> None:
        handler = _Transport(_ok(content="   "))
        with pytest.raises(LLMResponseError, match="没有正文"):
            await _provider(handler).complete(LLMRequest(prompt="甲"))

    async def test_truncation_gets_an_actionable_hint(self) -> None:
        """「空回复」最常见的成因是 max_tokens 太小，得直接说出来。"""
        handler = _Transport(_ok(content="", finish_reason="length"))
        with pytest.raises(LLMResponseError) as caught:
            await _provider(handler).complete(LLMRequest(prompt="甲"))

        assert "max_tokens" in str(caught.value.context["hint"])
        assert caught.value.context["finish_reason"] == "length"


# ────────────────────────────────────────────────────────────
# 密钥与健康检查
# ────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_it_is_ok_when_the_key_is_there(self) -> None:
        provider = _provider(_Transport(), api_key_env="KEY", environ={"KEY": "secret"})
        status = provider.health_check()

        assert isinstance(status, HealthStatus)
        assert status.ok

    async def test_a_missing_key_is_reported(self) -> None:
        provider = _provider(_Transport(), api_key_env="KEY", environ={})
        status = provider.health_check()

        assert not status.ok
        assert "KEY" in status.detail

    async def test_a_missing_key_does_not_block_local_endpoints(self) -> None:
        """Ollama / llama.cpp 不需要密钥，不能把它们挡在门外。"""
        provider = _provider(_Transport(), api_key_env="", environ={})
        assert provider.health_check().ok

    async def test_no_model_is_reported(self) -> None:
        provider = _provider(_Transport(), model="")
        status = provider.health_check()

        assert not status.ok
        assert "模型名" in status.detail

    async def test_a_configured_key_is_sent(self) -> None:
        provider = OpenAICompatibleProvider(
            "fake", base_url=BASE_URL, model="m1", api_key_env="KEY", environ={"KEY": "secret"}
        )

        client = provider._http()

        assert client.headers["Authorization"] == "Bearer secret"
        await provider.aclose()

    async def test_without_a_key_there_is_no_authorization_header(self) -> None:
        provider = OpenAICompatibleProvider("fake", base_url=BASE_URL, model="m1")

        client = provider._http()

        assert "Authorization" not in client.headers
        await provider.aclose()

    async def test_an_injected_client_keeps_its_own_headers(self) -> None:
        """注入 client 意味着连请求头一起交给注入方。

        生产代码从不注入（除测试外），所以密钥总是发得出去；这条用例钉的是
        「注入不只是换掉传输层」这个容易记错的事实——如果哪天要在生产里
        注入 client，得同时把 Authorization 头也装上。
        """
        handler = _Transport()
        provider = _provider(handler, api_key_env="KEY", environ={"KEY": "secret"})
        await provider.complete(LLMRequest(prompt="甲"))

        assert "Authorization" not in handler.requests[-1].headers


# ────────────────────────────────────────────────────────────
# 生命周期
# ────────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_an_injected_client_is_not_closed(self) -> None:
        """谁创建谁关闭——否则共享的 client 会在第一个用例结束时被关掉。"""
        handler = _Transport()
        provider = _provider(handler)
        client = provider._client

        await provider.aclose()

        assert client is not None
        assert not client.is_closed
        await client.aclose()

    async def test_it_works_as_an_async_context_manager(self) -> None:
        handler = _Transport()
        async with _provider(handler) as provider:
            response = await provider.complete(LLMRequest(prompt="甲"))
        assert response.text == "你好"

    def test_models_fall_back_to_the_single_model(self) -> None:
        provider = OpenAICompatibleProvider("fake", base_url=BASE_URL, model="m1")
        assert provider.models == ("m1",)

    def test_a_missing_base_url_is_refused_at_construction(self) -> None:
        with pytest.raises(LLMError, match="缺少 base_url"):
            OpenAICompatibleProvider("fake", base_url="", model="m1")


# ────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize(("raw", "expected"), [(12, 12), ("12", 12), (None, 0), ("x", 0)])
    def test_int_or_zero(self, raw: object, expected: int) -> None:
        assert _int_or_zero(raw) == expected

    def test_retry_after_ignores_a_http_date(self) -> None:
        """``Retry-After`` 也可以是日期格式。读不懂就按 1 秒处理。"""
        response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert _retry_after(response) == 1.0

    def test_short_body_normalises_whitespace(self) -> None:
        response = httpx.Response(500, text="a\n\n  b")
        assert _short_body(response) == "a b"

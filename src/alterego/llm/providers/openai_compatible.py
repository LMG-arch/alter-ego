"""OpenAI 兼容的对话补全供应商。

**一个类覆盖了绝大部分服务商**：DeepSeek、Kimi、通义、OpenAI、Ollama、
llama.cpp server、vLLM、LM Studio …… 它们都讲同一个 HTTP 协议
（``POST {base_url}/chat/completions``），差异只在配置里——地址、密钥环境变量名、
模型名。为它们各写一个类，只会得到七份要分别维护的复制品。

真正需要另写一个实现的是**协议不同**的那些（Anthropic Messages API、
Gemini generateContent）。它们以 ``llm.<name>`` 插件形式出现，实现同一个
:class:`~alterego.interfaces.llm.LLMProvider`。

本模块的职责边界（红线 4「``llm/`` 只做协议适配、重试、计量」）：

- :meth:`complete` 只管**一次**调用。重试、路由、计量都在
  :class:`~alterego.llm.gateway.LLMGateway` 里，这里做了就重复了。
- 失败时抛 :class:`~alterego.kernel.errors.LLMError` 的**具体子类**，
  让网关能区分「限流（等一会再试）」与「请求本身就不对（重试一百次也一样）」。

依据: docs/design/07-model-routing-and-media.md § 9
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest, LLMResponse
from alterego.kernel.errors import (
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
)


if TYPE_CHECKING:
    from types import TracebackType


__all__ = ["OpenAICompatibleProvider"]


#: 服务商返回的这个值表示「输出被长度截断」，而不是正常结束。
_FINISH_LENGTH: Final[str] = "length"

#: 429 响应里的建议等待时间。超过这个数就不等了——等 60 秒不如直接失败，
#: 让上层走降级路径（用户还在等回复，不是在等配额）。
_MAX_RETRY_AFTER_SEC: Final[float] = 30.0


class OpenAICompatibleProvider:
    """讲 OpenAI ``/chat/completions`` 协议的供应商。

    Args:
        provider_id: 供应商标识，与配置里的 ``[llm.providers.<name>]`` 同名。
        base_url: 形如 ``https://api.deepseek.com/v1``。末尾斜杠会被去掉。
        api_key_env: 读哪个环境变量拿密钥。空字符串表示不需要密钥
            （本地推理服务通常如此）。
        model: 默认模型名。
        models: 该供应商已知可用的模型名，仅供展示与校验；为空时退化为 `(model,)`。
        tier: 这个供应商大致属于哪一档，用于展示与降级参考。**不参与路由**——
            路由由 `[llm.routing]` 决定，供应商不自称身份（P1）。
        timeout_sec: 默认超时。单次请求可以用 :attr:`LLMRequest.timeout_sec` 覆盖。
        extra_headers: 额外的请求头，如某些服务商的 ``HTTP-Referer``。
        client: 注入的 `httpx.AsyncClient`，测试用。
        environ: 读密钥用的环境变量映射，测试用。
    """

    __slots__ = (
        "_api_key_env",
        "_base_url",
        "_client",
        "_environ",
        "_extra_headers",
        "_model",
        "_models",
        "_owns_client",
        "_timeout_sec",
        "id",
        "tier",
    )

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str,
        api_key_env: str = "",
        model: str = "",
        models: tuple[str, ...] = (),
        tier: Literal["strong", "cheap", "custom"] = "custom",
        timeout_sec: float = 60.0,
        extra_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not base_url:
            raise LLMError("供应商缺少 base_url", provider=provider_id)
        self.id = provider_id
        self.tier = tier
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._model = model
        self._models = models or ((model,) if model else ())
        self._timeout_sec = timeout_sec
        self._extra_headers = dict(extra_headers or {})
        self._environ = environ
        self._owns_client = client is None
        self._client = client

    # ── 只读属性 ────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """请求基地址。"""
        return self._base_url

    @property
    def models(self) -> tuple[str, ...]:
        """已知可用的模型名。"""
        return self._models

    @property
    def model(self) -> str:
        """默认模型名。"""
        return self._model

    # ── 生命周期 ────────────────────────────────────────────

    async def aclose(self) -> None:
        """关闭底层连接池。

        注入进来的 client 由注入方负责关闭——「谁创建谁关闭」，
        否则测试里的共享 client 会在第一个用例结束时被关掉。
        """
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenAICompatibleProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ── 调用 ────────────────────────────────────────────────

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """发一次请求。

        Raises:
            LLMTimeoutError: 超时。可重试。
            LLMTransportError: 连不上或被切断。可重试。
            LLMRateLimitError: 429。可重试（带建议等待时间）。
            LLMResponseError: 5xx，或 200 但响应体不是预期形状。可重试。
            LLMError: 4xx（除 429）——请求本身不对，重试没有意义。
        """
        payload = self._build_payload(req)
        started = time.perf_counter()

        try:
            response = await self._http().post(
                "/chat/completions",
                json=payload,
                timeout=req.timeout_sec or self._timeout_sec,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                "模型调用超时",
                provider=self.id,
                model=payload["model"],
                timeout_sec=req.timeout_sec or self._timeout_sec,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(
                "连不上模型服务",
                provider=self.id,
                base_url=self._base_url,
                reason=type(exc).__name__,
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(response)

        return self._parse(response, model=payload["model"], latency_ms=latency_ms)

    # ── 内部 ────────────────────────────────────────────────

    def _http(self) -> httpx.AsyncClient:
        """拿到（并惰性创建）HTTP 客户端。

        惰性创建的理由：构造 provider 会发生在配置加载阶段，
        而那时还没有事件循环——`httpx.AsyncClient` 一旦创建就会绑定循环。
        """
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            api_key = self._api_key()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            headers.update(self._extra_headers)
            self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers)
        return self._client

    def _api_key(self) -> str:
        """读密钥。

        读不到就返回空串，由服务商去拒绝——**不在这里抛错**。
        本地推理服务（Ollama / llama.cpp）不需要密钥，
        在这里报「缺少密钥」会把它们一起挡在门外。
        """
        if not self._api_key_env:
            return ""
        environ = os.environ if self._environ is None else self._environ
        return environ.get(self._api_key_env, "")

    def _build_payload(self, req: LLMRequest) -> dict[str, Any]:
        """请求体。

        :attr:`LLMRequest.metadata` **不进请求体**——它是本项目自己的账本
        （记档位与用途），发给服务商只会被忽略或报错。

        模型名取自**本供应商的默认值**，不从请求里拿：
        「这次该用哪个模型」是路由的决定，而路由在网关里，
        它会挑一个 provider 出来——被挑中的 provider 用自己的模型。
        """
        if not self._model:
            raise LLMError(
                "供应商没有指定模型名",
                provider=self.id,
                hint='在 [llm.providers.<name>] 里写 model = "…"。',
            )

        messages: list[dict[str, str]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.stop:
            payload["stop"] = list(req.stop)
        if req.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        """把 HTTP 状态码翻译成本项目的异常类型。"""
        status = response.status_code
        if status < 400:
            return

        detail = _short_body(response)
        if status == 429:
            raise LLMRateLimitError(
                "模型服务限流",
                provider=self.id,
                retry_after_sec=_retry_after(response),
                detail=detail,
            )
        if status >= 500:
            # 5xx 是服务端的事，等一会再来有真实概率成功。
            raise LLMResponseError(
                "模型服务内部错误",
                provider=self.id,
                status=status,
                detail=detail,
            )
        raise LLMError(
            "模型请求被拒绝",
            provider=self.id,
            status=status,
            detail=detail,
            hint="4xx 通常意味着请求本身不对：模型名不存在、密钥无效、或参数越界。",
        )

    def _parse(self, response: httpx.Response, *, model: str, latency_ms: int) -> LLMResponse:
        """解析成功响应。

        形状不对时抛 :class:`LLMResponseError`（可重试）而不是
        `ValueError`——模型服务偶发返回半截 JSON 是真实发生的事，
        它属于「这次调用失败了」，不属于「代码有 bug」。
        """
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                "模型响应不是合法 JSON",
                provider=self.id,
                detail=_short_body(response),
            ) from exc

        if not isinstance(data, dict):
            raise LLMResponseError(
                "模型响应的顶层不是 JSON 对象",
                provider=self.id,
                detail=_short_body(response),
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                "模型响应里没有 choices",
                provider=self.id,
                detail=_short_body(response),
            )

        first = choices[0]
        choice: Mapping[str, Any] = first if isinstance(first, dict) else {}
        raw_message = choice.get("message")
        message: Mapping[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            finish_reason = str(choice.get("finish_reason", ""))
            raise LLMResponseError(
                "模型响应里没有正文",
                provider=self.id,
                finish_reason=finish_reason,
                hint=(
                    "输出被 max_tokens 截断了——把 max_tokens 调大，或让提示词更短。"
                    if finish_reason == _FINISH_LENGTH
                    else "服务商返回了空正文。"
                ),
            )

        raw_usage = data.get("usage")
        usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return LLMResponse(
            text=text,
            model=str(data.get("model") or model),
            prompt_tokens=_int_or_zero(usage.get("prompt_tokens")),
            completion_tokens=_int_or_zero(usage.get("completion_tokens")),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            latency_ms=latency_ms,
            raw=data,
        )

    def health_check(self) -> HealthStatus:
        """健康检查。

        **不做网络探测**：`health_check` 是同步方法，而本项目的调用全是异步的，
        在这里开一次同步 HTTP 请求会把事件循环堵住。它只检查本地就能知道的事
        ——密钥有没有读到。真正的连通性由第一次真实调用去验证，
        失败也会被计量记下来。
        """
        if self._api_key_env and not self._api_key():
            return HealthStatus(
                ok=False,
                detail=f"环境变量 {self._api_key_env} 未设置",
                hint=f"设置 {self._api_key_env}，或把该供应商的 api_key_env 改成空字符串。",
            )
        if not self._model:
            return HealthStatus(
                ok=False,
                detail="未指定默认模型名",
                hint="在配置里给这个供应商写一个 model。",
            )
        return HealthStatus(ok=True, detail=f"{self.id} · {self._model}")


def _int_or_zero(value: Any) -> int:
    """把服务商给的计数转成 int。

    有些兼容实现会把 token 数写成字符串，个别实现返回 `None`。
    这些都不该让一次成功的调用变成失败调用。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _retry_after(response: httpx.Response) -> float:
    """读 ``Retry-After`` 头，读不到给 1 秒，超过上限定为 0（不等）。"""
    raw = response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return min(float(raw), _MAX_RETRY_AFTER_SEC)
    return 1.0


def _short_body(response: httpx.Response, *, limit: int = 300) -> str:
    """截断响应体用于报错。

    不截断的话，一个 HTML 错误页会把整段标签塞进日志与 `llm_usage.error`。
    """
    text = " ".join(response.text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"

"""LLM 供应商实现。

本包放的是**随包分发**的供应商。第三方供应商以 ``llm.<name>`` 插件形式提供，
两者都实现 :class:`alterego.interfaces.llm.LLMProvider`，
内核与上层看不出区别（P4）。

依据: docs/design/02-plugin-api.md § 6.1、docs/design/07-model-routing-and-media.md § 9
"""

from __future__ import annotations

from alterego.llm.providers.openai_compatible import OpenAICompatibleProvider


__all__ = ["OpenAICompatibleProvider"]

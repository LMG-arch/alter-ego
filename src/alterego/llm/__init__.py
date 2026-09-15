"""LLM 抽象层：协议适配、模型路由、提示词装载与成本计量。

本层只做「把一次请求送出去并拿回结果」，**不含业务逻辑**——
它不知道什么是「心情」，也不知道哪次调用是「主动发消息」。

模型选型由配置决定（`[llm.routing]`），不写死在这里（设计原则 P1）。

三层分工：

| 模块 | 职责 | 可以 import |
| --- | --- | --- |
| :mod:`~alterego.llm.prompts` | 装载与渲染提示词模板 | `kernel` |
| :mod:`~alterego.llm.gateway` | 用途路由 / 重试 / 计量 | `interfaces`、`kernel` |
| :mod:`~alterego.llm.providers` | 一次 HTTP 调用 | `interfaces`、`kernel`、`httpx` |

供应商**不在本文件里**导入：它们要 `httpx`，而 `import alterego.llm`
会被不需要发请求的代码路径（如 `alterego memory list`）碰到。
需要哪个供应商就显式从 :mod:`alterego.llm.providers` 取。

依据: docs/design/01-architecture.md § 1、docs/DESIGN.md § 7
"""

from __future__ import annotations

from alterego.llm.gateway import LLMGateway, LLMUsage, UsageSink
from alterego.llm.prompts import (
    PROMPTS_DIR,
    PromptLibrary,
    PromptTemplate,
    placeholders,
    render,
)


__all__ = [
    "PROMPTS_DIR",
    "LLMGateway",
    "LLMUsage",
    "PromptLibrary",
    "PromptTemplate",
    "UsageSink",
    "placeholders",
    "render",
]

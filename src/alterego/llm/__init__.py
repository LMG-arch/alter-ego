"""LLM 抽象层：协议适配、模型路由、提示词装载与成本计量。

本层只做「把一次请求送出去并拿回结果」，**不含业务逻辑**——
它不知道什么是「心情」，也不知道哪次调用是「主动发消息」。

模型选型由配置决定（`[llm.routing]`），不写死在这里（设计原则 P1）。
实际实现位于 `src/alterego/llm/`，以 `llm.<name>` 插件形式提供。

依据: docs/design/01-architecture.md § 1、docs/DESIGN.md § 7
"""

from __future__ import annotations


__all__: list[str] = []

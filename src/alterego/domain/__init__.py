"""领域层：纯函数式的领域模型。

这里**没有 IO、没有网络、不读环境变量、不调 LLM**。
给定状态与事件，返回新状态——不修改输入。

这条约束不是洁癖：领域层是整个项目里唯一能被「重放」的部分。
推演层出现怪行为时，能靠它把同一份输入跑出同一个结果，
才谈得上调试与复现（设计原则 P6）。

依据: docs/design/01-architecture.md § 3
"""

from __future__ import annotations

from alterego.domain import (
    birthday,
    calendar,
    consolidation,
    conversation,
    dataset,
    dataset_render,
    emotion,
    knowledge,
    memory,
    redact,
    schedule,
    study,
    vault,
)


__all__ = [
    "birthday",
    "calendar",
    "consolidation",
    "conversation",
    "dataset",
    "dataset_render",
    "emotion",
    "knowledge",
    "memory",
    "redact",
    "schedule",
    "study",
    "vault",
]

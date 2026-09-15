"""异常层次。

层次结构（``docs/design/01-architecture.md`` § 2.1）::

    AlterEgoError
    ├── ConfigError
    ├── PluginError
    │   ├── PluginManifestError
    │   ├── PluginLoadError
    │   ├── PluginDependencyError
    │   └── PluginRuntimeError
    ├── StorageError
    │   ├── MigrationError
    │   └── IntegrityError
    ├── LLMError
    │   ├── LLMRateLimitError
    │   ├── LLMTimeoutError
    │   ├── LLMResponseError
    │   └── LLMBudgetExceeded
    └── SimulationError
        ├── TickAborted
        └── IntentRejected

约定：

- **所有**异常都携带 ``context: dict``，便于日志与用户提示
- ``message`` 面向人（一句话，不含技术细节），``context`` 面向日志（结构化字段）
- ``code`` 稳定不变，供 Web 与脚本判断，**不要**去匹配 ``message`` 文本
- ``retryable`` 表示调用方是否可以原样重试。仅用于「同一个请求再来一次
  可能成功」的场景，不代表「这个错误可以恢复」
"""

from __future__ import annotations

from typing import Any, ClassVar


__all__ = [
    "AlterEgoError",
    "ConfigError",
    "IntegrityError",
    "IntentRejected",
    "LLMBudgetExceeded",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMTimeoutError",
    "LLMTransportError",
    "MigrationError",
    "PluginDependencyError",
    "PluginError",
    "PluginLoadError",
    "PluginManifestError",
    "PluginRuntimeError",
    "PromptError",
    "SimulationError",
    "StorageError",
    "TickAborted",
    "is_retryable",
]


class AlterEgoError(Exception):
    """所有 AlterEgo 异常的基类。"""

    code: ClassVar[str] = "alterego_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, /, **context: Any) -> None:
        """构造异常。

        Args:
            message: 面向人的一句话说明。
            **context: 结构化上下文，如 ``plugin_id="channel.file"``。
        """
        super().__init__(message)
        self.message: str = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({detail})"

    def to_dict(self) -> dict[str, Any]:
        """转为结构化字典，供日志与 Web 展示。"""
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────


class ConfigError(AlterEgoError):
    """配置缺失或非法。"""

    code: ClassVar[str] = "config_error"


# ─────────────────────────────────────────────────────────────
# 插件
# ─────────────────────────────────────────────────────────────


class PluginError(AlterEgoError):
    """插件相关的错误基类。"""

    code: ClassVar[str] = "plugin_error"


class PluginManifestError(PluginError):
    """``plugin.toml`` 缺失、语法错误或字段非法。"""

    code: ClassVar[str] = "plugin_manifest_error"


class PluginLoadError(PluginError):
    """插件导入或实例化失败。"""

    code: ClassVar[str] = "plugin_load_error"


class PluginDependencyError(PluginError):
    """插件依赖缺失或存在循环。"""

    code: ClassVar[str] = "plugin_dependency_error"


class PluginRuntimeError(PluginError):
    """插件在运行期抛出的异常。

    插件内部的异常会被加载器包成本类型，避免插件把内部实现细节
    泄漏到调用栈上，也让「哪个插件坏了」一目了然。
    """

    code: ClassVar[str] = "plugin_runtime_error"


# ─────────────────────────────────────────────────────────────
# 存储
# ─────────────────────────────────────────────────────────────


class StorageError(AlterEgoError):
    """持久化失败。"""

    code: ClassVar[str] = "storage_error"


class MigrationError(StorageError):
    """数据库迁移失败。

    此时数据库处于**未知状态**，必须由用户显式处理（回滚备份或手工修复），
    因此进程会以退出码 4 终止，而不是继续运行。
    """

    code: ClassVar[str] = "migration_error"


class IntegrityError(StorageError):
    """数据完整性检查失败。"""

    code: ClassVar[str] = "integrity_error"


# ─────────────────────────────────────────────────────────────
# 模型调用
# ─────────────────────────────────────────────────────────────


class LLMError(AlterEgoError):
    """模型调用失败基类。"""

    code: ClassVar[str] = "llm_error"


class LLMRateLimitError(LLMError):
    """被限流。可重试（指数退避）。"""

    code: ClassVar[str] = "llm_rate_limit"
    retryable: ClassVar[bool] = True


class LLMTimeoutError(LLMError):
    """请求超时。可重试。"""

    code: ClassVar[str] = "llm_timeout"
    retryable: ClassVar[bool] = True


class LLMResponseError(LLMError):
    """响应格式非法（例如期望 JSON 却拿到散文）。可重试。

    重试策略上一般只有一次机会：第二次仍失败就降级到规则模式，
    不要把预算浪费在反复重试上。
    """

    code: ClassVar[str] = "llm_response_error"
    retryable: ClassVar[bool] = True


class LLMBudgetExceeded(LLMError):
    """超出预算配额。

    **不可重试**——重试只会更快地烧掉预算。调用方应降级到规则模式
    （见 ``docs/design/04-simulation-loop.md`` § 10.4 降级三档）。
    """

    code: ClassVar[str] = "llm_budget_exceeded"


class LLMTransportError(LLMError):
    """网络层失败：连不上、连接被切断、证书不行。可重试。

    与 :class:`LLMTimeoutError` 分开计数是有用的：超时通常意味着
    「这个模型太慢」，连不上通常意味着「地址或网络不对」——
    前者该调小模型，后者该去改 ``base_url``。
    两者的可重试性相同，但错误率报表上的含义完全不同。
    """

    code: ClassVar[str] = "llm_transport_error"
    retryable: ClassVar[bool] = True


class PromptError(LLMError):
    """提示词模板缺失、为空，或与调用方给的参数对不上。

    **不可重试**：它几乎总是代码问题（模板改了但调用方没跟），
    重试一百次还是同一个错。抛在这里是为了让失败发生在
    **发请求之前**——一次注定失败的调用也要按 token 付钱。
    """

    code: ClassVar[str] = "prompt_error"


# ─────────────────────────────────────────────────────────────
# 推演
# ─────────────────────────────────────────────────────────────


class SimulationError(AlterEgoError):
    """推演失败基类。"""

    code: ClassVar[str] = "simulation_error"


class TickAborted(SimulationError):
    """单次 tick 被中止。

    用于「可以安全放弃这一次 tick」的场景（例如存储繁忙、
    或感知阶段判定当前无需任何行动）。**不**代表系统故障：
    连续 5 次 tick 失败才会进入降级模式。
    """

    code: ClassVar[str] = "tick_aborted"


class IntentRejected(SimulationError):
    """意图被预算或规则拒绝。

    注意：按 ``docs/adr/0005-downgrade-instead-of-discard-suppressed-intents.md``，
    预算拒绝的**正常路径不是抛异常，而是降级**（``reach_out`` → ``reflect_internal``）。
    本异常只用于「连降级都不可行」的极端情况（例如明确的越权请求）。
    """

    code: ClassVar[str] = "intent_rejected"


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得原样重试。

    设计上只对 AlterEgo 自己的异常做判断：第三方库的异常形态不可控，
    由 ``llm/`` 层的适配器负责翻译成本模块的异常。
    """
    return isinstance(exc, AlterEgoError) and exc.retryable

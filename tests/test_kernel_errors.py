"""``alterego.kernel.errors`` 的行为测试。

重点是三条约定：
- 所有异常都携带 ``context``
- ``context`` 出现在 ``str(exc)`` 里（否则日志里看不到关键字段）
- ``retryable`` 的取值符合设计文档（只有限流/超时/响应格式三类可重试）
"""

from __future__ import annotations

import pytest

from alterego.kernel import errors as e


def test_hierarchy_matches_design_doc() -> None:
    """异常层次必须与 01-architecture.md § 2.1 完全一致。"""
    assert issubclass(e.ConfigError, e.AlterEgoError)
    for child in (
        e.PluginManifestError,
        e.PluginLoadError,
        e.PluginDependencyError,
        e.PluginRuntimeError,
    ):
        assert issubclass(child, e.PluginError)
    assert issubclass(e.MigrationError, e.StorageError)
    assert issubclass(e.IntegrityError, e.StorageError)
    for child in (
        e.LLMRateLimitError,
        e.LLMTimeoutError,
        e.LLMResponseError,
        e.LLMBudgetExceeded,
    ):
        assert issubclass(child, e.LLMError)
    assert issubclass(e.TickAborted, e.SimulationError)
    assert issubclass(e.IntentRejected, e.SimulationError)
    # 全部最终都归于 AlterEgoError
    for name in e.__all__:
        obj = getattr(e, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            assert issubclass(obj, e.AlterEgoError), name


def test_context_is_captured_and_rendered() -> None:
    exc = e.PluginError("插件启动失败", plugin_id="channel.file", attempt=2)
    assert exc.context == {"plugin_id": "channel.file", "attempt": 2}
    assert isinstance(exc, Exception)
    text = str(exc)
    assert "插件启动失败" in text
    # 结构化字段必须能被看见，否则排查时只能去翻代码
    assert "plugin_id='channel.file'" in text
    assert "attempt=2" in text


def test_str_without_context_is_just_the_message() -> None:
    assert str(e.ConfigError("缺少 api_key")) == "缺少 api_key"


def test_to_dict_is_stable_for_logging() -> None:
    exc = e.StorageError("写入失败", table="memory")
    assert exc.to_dict() == {
        "code": "storage_error",
        "message": "写入失败",
        "context": {"table": "memory"},
    }


def test_codes_are_unique() -> None:
    """``code`` 供脚本与 Web 判断，重复会让调用方无法区分错误。"""
    codes = [
        getattr(e, name).code
        for name in e.__all__
        if isinstance(getattr(e, name), type) and issubclass(getattr(e, name), Exception)
    ]
    assert len(codes) == len(set(codes))


def test_only_transient_llm_errors_are_retryable() -> None:
    assert e.is_retryable(e.LLMRateLimitError("被限流"))
    assert e.is_retryable(e.LLMTimeoutError("超时"))
    assert e.is_retryable(e.LLMResponseError("不是合法 JSON"))
    # 预算耗尽重试只会更快烧完预算——必须不可重试，迫使调用方降级
    assert not e.is_retryable(e.LLMBudgetExceeded("超出日限额"))
    # 配置错误重试多少次都一样
    assert not e.is_retryable(e.ConfigError("配置非法"))
    # 非 AlterEgo 异常不由本函数判断（第三方异常由适配器翻译）
    assert not e.is_retryable(ValueError("x"))


def test_exception_can_be_raised_and_caught_as_base_class() -> None:
    with pytest.raises(e.AlterEgoError) as info:
        raise e.PluginDependencyError("缺少依赖", missing="channel.web", needed_by="stage.express")
    assert info.value.context["missing"] == "channel.web"
    assert info.value.code == "plugin_dependency_error"

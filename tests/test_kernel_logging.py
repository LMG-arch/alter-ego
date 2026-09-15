"""内核日志层测试：格式与脱敏。

脱敏这部分值得认真测——一个「看起来在工作」的脱敏器比没有脱敏器更危险。
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pytest

from alterego.kernel.logging import (
    ROOT_LOGGER_NAME,
    ConsoleFormatter,
    JsonFormatter,
    SecretFilter,
    _coerce_level,
    get_logger,
    setup_logging,
)


def make_record(message: str, *, args: tuple[object, ...] = ()) -> logging.LogRecord:
    """造一条日志记录，用来单独调用 filter / formatter。"""
    return logging.LogRecord(
        name="alterego.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def applied(message: str, *, args: tuple[object, ...] = ()) -> str:
    """跑一遍 SecretFilter，返回脱敏后的最终文本。"""
    record = make_record(message, args=args)
    assert SecretFilter().filter(record) is True
    return record.getMessage()


@pytest.fixture(autouse=True)
def restore_root_logger() -> object:
    """``setup_logging`` 会改全局 logger，测完必须还原，否则会污染其他测试。"""
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    yield None
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        logger.addHandler(handler)
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate


# ── 脱敏 ────────────────────────────────────────────────────


class TestSecretFilter:
    def test_masks_short_prefix_key(self) -> None:
        secret = "sk-" + "a1b2c3d4" * 4

        cleaned = applied(f"调用失败：{secret}")

        assert secret not in cleaned
        assert "sk-a***" in cleaned  # 留 4 个字符，便于判断「是哪个密钥配错了」

    def test_masks_second_prefixed_key(self) -> None:
        secret = "SEC" + "0123456789abcdef" * 2

        cleaned = applied(f"签名密钥 {secret}")

        assert secret not in cleaned
        assert cleaned.endswith("SEC0***")

    def test_masks_webhook_url_with_path(self) -> None:
        # 真实地址几乎都带路径——只测「无路径」的形态会漏掉整条规则。
        url = "https://qyapi.example.com/cgi-bin/webhook/send?key=693a91f6-7b8c-9d0e"

        cleaned = applied(f"发送到 {url}")

        assert "693a91f6-7b8c-9d0e" not in cleaned
        assert cleaned.endswith("?key=***")

    def test_keeps_url_prefix_but_not_the_key(self) -> None:
        url = "https://example.com/hook?key=abcdef123456"

        cleaned = applied(url)

        # 前缀必须留着：否则「哪个 webhook 出错了」就没法查了。
        assert "https://example.com/hook?key=" in cleaned
        assert "abcdef123456" not in cleaned

    def test_masks_bot_token(self) -> None:
        token = "bot123456789:" + "A" * 35

        cleaned = applied(f"token={token}")

        assert token not in cleaned
        assert "bot1***" in cleaned

    def test_the_single_group_case_does_not_leak(self) -> None:
        """设计文档里那段示意代码在单分组模式下会把密钥原样打印出来。

        这里钉住修正后的行为：单分组 = ``group(1)`` 就是密钥本身，
        必须走「留前缀 + ***」这条路径。
        """
        secret = "sk-" + "z" * 30

        cleaned = applied(secret)

        assert "z" * 30 not in cleaned
        assert cleaned.count("***") == 1

    def test_short_match_is_fully_masked(self) -> None:
        # 极短匹配不值得留前缀——留了就等于没遮。
        filter_ = SecretFilter([re.compile(r"(SEC.)")])

        record = make_record("SECa")
        assert filter_.filter(record) is True
        assert record.getMessage() == "***"

    def test_message_without_secret_is_untouched(self) -> None:
        assert applied("一切正常") == "一切正常"

    def test_substituted_args_are_cleared(self) -> None:
        """脱敏后的文本已经渲染过，args 必须清掉，否则 Formatter 会再渲染一次。"""
        secret = "sk-" + "q" * 30
        record = make_record("密钥 %s 无效", args=(secret,))

        assert SecretFilter().filter(record) is True

        assert record.args == ()
        assert "q" * 30 not in record.getMessage()

    def test_returns_true_so_the_record_passes_through(self) -> None:
        # Filter 返回 False 会吞掉日志——脱敏器不该改变「这条日志存不存在」。
        assert SecretFilter().filter(make_record("来自 %s 的消息", args=("用户",))) is True


# ── 格式 ────────────────────────────────────────────────────


class TestFormatters:
    def test_console_format_shape(self) -> None:
        record = make_record("总线启动")
        record.name = "alterego.kernel.bus"

        line = ConsoleFormatter().format(record)

        assert line.endswith("alterego.kernel.bus  总线启动")
        assert "INFO" in line
        assert line.count(":") >= 2  # HH:MM:SS

    def test_json_format_is_one_object(self) -> None:
        record = make_record("插件已加载")
        record.name = "alterego.kernel.manager"

        payload = json.loads(JsonFormatter().format(record))

        assert payload["level"] == "INFO"
        assert payload["logger"] == "alterego.kernel.manager"
        assert payload["message"] == "插件已加载"
        assert "fields" not in payload  # 没有结构化上下文就不该凭空造一个

    def test_json_format_flattens_alterego_extra(self) -> None:
        record = make_record("tick 完成")
        record.alterego = {"plugin_id": "channel.file", "tick_id": "abc"}

        payload = json.loads(JsonFormatter().format(record))

        assert payload["fields"] == {"plugin_id": "channel.file", "tick_id": "abc"}

    def test_json_format_keeps_chinese_readable(self) -> None:
        # ensure_ascii=False：日志是给人查的，满屏 \uXXXX 没法用。
        assert "已熔断" in JsonFormatter().format(make_record("已熔断"))

    def test_json_format_includes_traceback(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = make_record("失败")
            record.exc_info = sys.exc_info()

        payload = json.loads(JsonFormatter().format(record))

        assert "ValueError: boom" in payload["exception"]


# ── logger 获取与配置 ───────────────────────────────────────


class TestGetLogger:
    def test_empty_name_returns_root(self) -> None:
        assert get_logger() is logging.getLogger(ROOT_LOGGER_NAME)

    def test_name_gets_prefixed(self) -> None:
        assert get_logger("kernel.bus").name == "alterego.kernel.bus"

    def test_prefix_is_not_doubled(self) -> None:
        assert get_logger("alterego.kernel.bus").name == "alterego.kernel.bus"

    def test_root_name_itself_is_accepted(self) -> None:
        assert get_logger(ROOT_LOGGER_NAME).name == ROOT_LOGGER_NAME


class TestSetupLogging:
    def test_installs_one_handler_with_secret_filter(self) -> None:
        logger = setup_logging(stream=io.StringIO())

        assert len(logger.handlers) == 1
        assert any(isinstance(f, SecretFilter) for f in logger.handlers[0].filters)

    def test_is_idempotent(self) -> None:
        setup_logging(stream=io.StringIO())
        logger = setup_logging(stream=io.StringIO())

        # 重复调用留下重复 handler 会让每一行日志打印两次——测试里尤其烦。
        assert len(logger.handlers) == 1

    def test_does_not_propagate_to_root(self) -> None:
        assert setup_logging(stream=io.StringIO()).propagate is False

    def test_secret_is_masked_in_rendered_output(self) -> None:
        stream = io.StringIO()
        logger = setup_logging(stream=stream)
        secret = "sk-" + "k" * 30

        logger.info("密钥是 %s", secret)

        assert secret not in stream.getvalue()
        assert "sk-k***" in stream.getvalue()

    def test_json_format_can_be_selected(self) -> None:
        stream = io.StringIO()
        logger = setup_logging(log_format="json", stream=stream)

        logger.info("hello")

        assert json.loads(stream.getvalue().strip())["message"] == "hello"

    def test_log_file_gets_created(self, tmp_path: Path) -> None:
        target = tmp_path / "logs" / "alterego.log"
        logger = setup_logging(log_file=target)

        logger.info("落盘")

        assert target.is_file()
        assert "落盘" in target.read_text(encoding="utf-8")
        assert len(logger.handlers) == 2

    def test_unknown_level_falls_back_to_info(self) -> None:
        # 配置写错不该让程序起不来，但也不能悄悄按 DEBUG 跑。
        logger = setup_logging(level="VERBOSE", stream=io.StringIO())

        assert logger.level == logging.INFO

    def test_valid_level_is_applied(self) -> None:
        assert setup_logging(level="debug", stream=io.StringIO()).level == logging.DEBUG


class TestCoerceLevel:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [("INFO", logging.INFO), ("warning", logging.WARNING), ("Debug", logging.DEBUG)],
    )
    def test_known_names(self, given: str, expected: int) -> None:
        assert _coerce_level(given) == expected

    def test_integer_passes_through(self) -> None:
        assert _coerce_level(logging.ERROR) == logging.ERROR

    def test_unknown_name_returns_info(self) -> None:
        assert _coerce_level("nonsense") == logging.INFO

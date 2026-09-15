"""日志与脱敏。

两件事必须在这里一并解决，因为它们的关系是「谁也别想绕过谁」：

1. **格式** —— ``console`` 给人看，``json`` 给机器看（``log_format`` 配置项）。
2. **脱敏** —— 密钥在**进 handler 之前**就要变成 ``***``。等到写日志时再脱敏，
   就等于已经把它写进内存快照、异常回溯和传给 Web 的那份记录里了。

依据: docs/design/05-channels.md § 11.1
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Final


__all__ = [
    "ROOT_LOGGER_NAME",
    "SENSITIVE_PATTERNS",
    "ConsoleFormatter",
    "JsonFormatter",
    "SecretFilter",
    "get_logger",
    "setup_logging",
]


#: 所有本项目的 logger 都挂在这个名字下，便于整体控制级别与日志文件归属。
ROOT_LOGGER_NAME: Final[str] = "alterego"

#: 会被脱敏的字符串形态。
#:
#: 这里覆盖的是**形态已知**的密钥。形态未知的靠 ``plugin.toml`` 里的
#: ``secret = true``——两条路都要有：前者防「不小心打出来的」，后者防「主动打印的」。
SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),  # 最常见的「短前缀 + 长随机串」密钥
    re.compile(r"(SEC[a-zA-Z0-9]{20,})"),  # 另一种常见形态
    # 注意这里是 ``\S*`` 而不是 ``[^/\s]*``：真实的 webhook 地址几乎都带路径
    # （``https://host/cgi-bin/hook/send?key=...``），而 ``[^/\s]*`` 跨不过斜杠，
    # 一条都匹配不到——安全过滤器「看起来在工作」是最糟的状态。
    re.compile(r"(https://\S*(?:webhook|hook)\S*key=)([A-Za-z0-9_-]+)"),
    re.compile(r"(bot\d{8,10}:[A-Za-z0-9_-]{35})"),  # 「bot<数字>:<长串>」形态
)

#: 脱敏后保留的前缀长度。留几个字符是为了让「是哪个密钥配错了」还能判断。
_KEEP_PREFIX: Final[int] = 4


class SecretFilter(logging.Filter):
    """把日志消息里的密钥换成 ``***``。

    注意设计文档里那段示意代码有一处真实缺陷：它对单分组的模式也会走
    ``m.group(1) + "***"``，而单分组模式的 ``group(1)`` **就是密钥本身**，
    结果是「原样打印密钥再补三个星」。这里改为：只有确实存在「安全前缀」
    分组时才保留前缀，否则整体打码。
    """

    def __init__(self, patterns: Iterable[re.Pattern[str]] = SENSITIVE_PATTERNS) -> None:
        super().__init__()
        self._patterns = tuple(patterns)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 畸形 args
            return True
        cleaned = message
        for pattern in self._patterns:
            cleaned = pattern.sub(_mask_match, cleaned)
        if cleaned != message:
            # 已经渲染过一遍，必须同时清掉 args，否则 Formatter 会再渲染一次
            # 并把占位符原样吐出来。
            record.msg = cleaned
            record.args = ()
        return True


def _mask_match(match: re.Match[str]) -> str:
    if match.lastindex is not None and match.lastindex >= 2:
        return match.group(1) + "***"
    secret = match.group(0)
    return secret[:_KEEP_PREFIX] + "***" if len(secret) > _KEEP_PREFIX else "***"


class ConsoleFormatter(logging.Formatter):
    """给人看的格式：``09:12:03 INFO  alterego.kernel.bus  消息``。"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )


class JsonFormatter(logging.Formatter):
    """给机器看的格式：一行一个 JSON 对象。

    ``extra={"alterego": {...}}`` 里的内容会平铺到 ``fields`` 下，
    用来放 ``plugin_id`` / ``tick_id`` / ``correlation_id`` 这类结构化上下文。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "alterego", None)
        if isinstance(fields, dict):
            payload["fields"] = fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str = "") -> logging.Logger:
    """取一个本项目的 logger。

    ``get_logger("kernel.bus")`` 与 ``get_logger("alterego.kernel.bus")``
    等价——省得调用方每次都要判断自己该不该带前缀。
    """
    if not name:
        return logging.getLogger(ROOT_LOGGER_NAME)
    if name == ROOT_LOGGER_NAME or name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def setup_logging(
    *,
    level: str = "INFO",
    log_format: str = "console",
    log_file: Path | None = None,
    stream: Any = None,
) -> logging.Logger:
    """配置并返回根 logger。

    重复调用是安全的：每次都先清掉自己装过的 handler，
    免得测试里留下一串重复输出的 handler。
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = JsonFormatter() if log_format == "json" else ConsoleFormatter()
    secret_filter = SecretFilter()

    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(secret_filter)
        logger.addHandler(handler)

    return logger


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if resolved is None:
        # 配置写错了不该让程序起不来——退回到 INFO 并说一声。
        logging.getLogger(ROOT_LOGGER_NAME).warning("未知的日志级别 %r，改用 INFO", level)
        return logging.INFO
    return resolved

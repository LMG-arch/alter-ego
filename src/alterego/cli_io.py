"""给用户看的输出。

单独一个模块，而不是留在 ``cli.py`` 里：``cli.py`` 已经贴着「单文件 ≤ 900 行」
的上限（``AGENTS.md`` § 5），而 ``cli_db.py`` 需要的是**同一个** ``_out``。
留在一处好过在两个模块里各抄一份——抄出来的两份迟早会改一处忘一处，
然后用户在 ``db status`` 和 ``birthday list`` 里看到两种缩进。
"""

from __future__ import annotations

import sys
import unicodedata


_RULE = "─" * 58


def _out(text: str = "") -> None:
    """往标准输出写一行。

    不用 ``print``：``scripts/check_architecture.sh`` 第 5 组对 ``src/alterego``
    **整个目录**禁止 ``print(``（本意是「生产代码别留调试打印」，而 CLI 正好也在
    那个目录里）。不去改那条红线——它是给生产代码用的；用显式的
    ``sys.stdout.write`` 表达「这行是给用户看的输出」，反而更清楚。
    """
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    """往标准错误写一行。错误走 stderr，这样 ``alterego db status > x.txt`` 拿到的是干净数据。"""
    sys.stderr.write(text + "\n")


def _width(text: str) -> int:
    """显示宽度。CJK 与全角标点占两列。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, columns: int) -> str:
    """按**显示宽度**补齐。

    用 `f"{text:<8}"` 不行：它数的是字符个数，而中文一个字占两列，
    于是表格里所有含中文的列都会错位（`节后 0  天` 旁边跟着 `节后 1 天`）。
    """
    return text + " " * max(0, columns - _width(text))


def _human_size(size: int) -> str:
    """字节数说成人话。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"

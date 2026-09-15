"""命令行入口。

完整命令树见 ``docs/design/05-channels.md`` § 8。当前只实现到「能启动、
能报版本」——后续每加一个功能就多一个子命令，而不是一次性写完再调。

退出码约定（``docs/design/01-architecture.md`` § 7）::

    0  正常
    1  通用错误
    2  配置非法
    3  插件依赖环
    4  数据库迁移失败
"""

from __future__ import annotations

import argparse

from alterego import __version__


__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    用标准库 ``argparse`` 而不是 click / typer——见设计原则 P5（标准库优先）
    与 ``docs/adr/0002-use-python-as-implementation-language.md``。
    """
    parser = argparse.ArgumentParser(
        prog="alterego",
        description="AlterEgo · 拟我 —— 一个会自己生活、自己思考、按自己的节奏找你的数字存在。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alterego {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表。``None`` 时取 ``sys.argv[1:]``（便于测试直接传列表）。

    Returns:
        进程退出码。
    """
    parser = build_parser()
    parser.parse_args(argv)
    # 还没有子命令，因此打印帮助是当前最合理的默认行为。
    parser.print_help()
    return 0

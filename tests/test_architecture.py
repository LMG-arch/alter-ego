"""架构红线的可执行版本。

``scripts/check_architecture.sh`` 是权威检查（七组 23 项，CI 里会跑），
但它是 bash——在 Windows 上不一定可用。这里用 ``ast`` 复刻其中最关键的几条，
让本地开发者在任何平台上都能立刻发现越界。

原则：**内核不知道实现**（P1）。内核里出现 ``sqlite3`` / ``httpx`` /
``fastapi`` 任何一个，都意味着「换掉一个插件就能换掉一个后端」
这句承诺已经失效了。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


pytestmark = pytest.mark.architecture

SRC = Path(__file__).resolve().parent.parent / "src" / "alterego"
KERNEL = SRC / "kernel"

#: 内核绝对不能出现的顶层模块。每一项都对应一个具体的「抽象泄漏」。
FORBIDDEN_IN_KERNEL = {
    # 存储实现
    "sqlite3",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "pymysql",
    # 网络与模型供应商
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "openai",
    "anthropic",
    # Web 框架
    "fastapi",
    "uvicorn",
    "starlette",
    # 具体渠道
    "wecom",
    "dingtalk",
    "telegram",
}

#: 内核不能 import 的上层模块（依赖方向只能向下：Interface → Sim → Domain → Kernel）。
UPPER_LAYERS = (
    "alterego.domain",
    "alterego.sim",
    "alterego.storage",
    "alterego.llm",
    "alterego.channels",
    "alterego.capabilities",
    "alterego.npc",
)


def _kernel_files() -> list[Path]:
    return sorted(p for p in KERNEL.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> set[str]:
    """收集一个文件里所有 import 的模块名（不含相对 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_kernel_directory_exists_and_has_content() -> None:
    """这条在重构时会先失败——提醒「红线没被绕过，而是被删掉了」。"""
    assert KERNEL.is_dir()
    assert _kernel_files(), "kernel/ 下没有任何 Python 文件，检查是否被误删"


@pytest.mark.parametrize("path", _kernel_files(), ids=lambda p: p.name)
def test_kernel_has_no_io_imports(path: Path) -> None:
    """内核不认识具体的存储、网络与渠道（P1 内核无知）。"""
    leaked = {m.split(".")[0] for m in _imported_modules(path)} & FORBIDDEN_IN_KERNEL
    assert not leaked, (
        f"{path.name} 引入了 {sorted(leaked)}。"
        "请改为在 protocols 里定义接口，由插件提供实现——"
        "见 docs/design/01-architecture.md § 1。"
    )


@pytest.mark.parametrize("path", _kernel_files(), ids=lambda p: p.name)
def test_kernel_does_not_import_upper_layers(path: Path) -> None:
    """内核不能反向依赖上层——依赖方向违约会让插件彻底无法替换。"""
    offenders = [module for module in _imported_modules(path) if module.startswith(UPPER_LAYERS)]
    assert not offenders, f"{path.name} 反向依赖了 {offenders}"


def test_kernel_has_no_print_calls() -> None:
    """日志必须走 ``logging``，否则日志级别、脱敏与落盘全部绕过。"""

    def prints(path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return [
            f"{path.name}:{node.lineno}"
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            )
        ]

    offenders = [hit for path in _kernel_files() for hit in prints(path)]
    assert not offenders, f"内核中不应出现 print()：{offenders}"


def test_every_package_directory_has_init() -> None:
    """没有 ``__init__.py`` 的目录不会被 ``check_architecture.sh`` 放行。

    这里同时覆盖 ``src/``，因为漏掉一个 ``__init__.py`` 只在打包时才会暴露。
    """
    missing: list[str] = []
    for directory in SRC.rglob("*"):
        if not directory.is_dir() or "__pycache__" in directory.parts:
            continue
        if not any(directory.glob("*.py")):
            continue
        if not (directory / "__init__.py").exists():
            missing.append(str(directory.relative_to(SRC.parent)))
    assert not missing, f"以下目录缺少 __init__.py：{missing}"


def test_source_files_stay_reviewable() -> None:
    """单文件超过 900 行通常意味着这里有不止一个职责。"""
    oversized = [
        f"{p.name}:{len(p.read_text(encoding='utf-8').splitlines())}"
        for p in SRC.rglob("*.py")
        if len(p.read_text(encoding="utf-8").splitlines()) > 900
    ]
    assert not oversized, f"以下文件超过 900 行：{oversized}"

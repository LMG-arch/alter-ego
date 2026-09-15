"""每个包和模块都必须能被干净地导入。

听起来像句废话，但它挡住的是最难受的一类问题：

- **循环导入**。``alterego.kernel.a`` 导入 ``alterego.kernel.b``，
  而 ``b`` 又回来导入 ``a``——在有人把 ``import`` 挪个位置之前谁也发现不了，
  因为每个单独测试都只导入自己需要的那部分。
- **占位包腐烂**。新加一个层的 ``__init__.py`` 时写错一个字，
  直到三个月后有人真的去用它才炸，而那时已经没人记得当时改了什么。
- **启动时间悄悄变胖**。``import alterego`` 必须是廉价的（CLI 目标 ≤ 1 秒）。
  这条测试会遍历全部模块，于是任何在模块级做重活的代码
  （建连接、读文件、算正则表）都会直接反映到这条测试的耗时上。

这条测试**故意**不检查返回值——它检查的是「导入这件事本身没有副作用」。
一个模块在 import 时做了 IO，就是在偷跑。

依据: docs/DESIGN.md § 11、CONTRIBUTING.md § 代码规范
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "alterego"

#: 在全新解释器里只 import 顶层包，然后把溜进来的子模块打出来。
_SUBMODULES_AFTER_A_BARE_IMPORT = """
import json
import sys

import alterego  # noqa: F401

print(json.dumps(sorted(name for name in sys.modules if name.startswith("alterego."))))
"""


def all_module_names() -> list[str]:
    """``alterego`` 下所有可导入模块的完整名字（含包本身）。"""
    found = pkgutil.walk_packages([str(PACKAGE_ROOT)], prefix="alterego.")
    return sorted(["alterego", *(info.name for info in found)])


ALL_MODULES = all_module_names()


def test_the_walker_actually_found_something() -> None:
    """防止这条测试变成永远通过的空转——它曾经差点就是。"""
    assert "alterego.kernel.bus" in ALL_MODULES
    assert "alterego.cli" in ALL_MODULES
    assert len(ALL_MODULES) > 20


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    module = importlib.import_module(module_name)

    assert module is not None
    assert module_name in sys.modules


def test_importing_the_top_level_package_stays_cheap() -> None:
    """``import alterego`` 不该顺带把内核、存储、渠道全拖进来。

    必须在**独立进程**里检查：本文件上面的参数化测试已经把各个子模块
    导进来了，在同一个进程里问「有没有被导入」永远得到「有」。
    这正是「测试之间通过 `sys.modules` 串味」的典型例子。

    这是启动时间目标（≤ 1 秒）的第一道保险：一旦有人在
    ``alterego/__init__.py`` 里加一句 ``from .kernel import Config``，
    这个断言立刻挂掉，而不是等到某天有人抱怨「启动怎么这么慢」。
    """
    env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT.parent), PYTHONIOENCODING="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", _SUBMODULES_AFTER_A_BARE_IMPORT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )

    assert json.loads(completed.stdout) == []

    import alterego

    assert alterego.__version__


def test_every_package_declares_where_its_rules_are_written() -> None:
    """每个层的 ``__init__.py`` 都要说清「这个层负责什么、不负责什么」。

    这不是格式要求，是**架构红线的入口**：新人判断一个函数该放哪一层时，
    第一眼看到的就是 ``__init__.py`` 的文档字符串。没有它，
    分层规则只存在于某人的记忆里。
    """
    offenders: list[str] = []
    for module_name in ALL_MODULES:
        if not module_name.endswith(
            (".kernel", ".domain", ".sim", ".llm", ".storage", ".channels", ".npc")
        ):
            continue
        module = importlib.import_module(module_name)
        docstring = (module.__doc__ or "").strip()
        if len(docstring) < 40:
            offenders.append(f"{module_name}（{len(docstring)} 字）")

    assert not offenders, "这些包的 __init__.py 没有说明自己的职责：" + "、".join(offenders)

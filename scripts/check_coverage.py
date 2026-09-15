#!/usr/bin/env python3
"""按包核对覆盖率下限 —— ``AGENTS.md`` § 5 那张表在这里变成真代码。

为什么要这个脚本：coverage 自带的 ``--cov-fail-under`` 只能表达**一个**全局下限，而我们要的是
``kernel ≥ 90`` / ``domain ≥ 95`` / ``sim ≥ 85`` / 全局 ``≥ 85`` 四个不同的数。为四个阈值跑四遍
pytest 太贵（本仓库一轮约 70 秒），所以复用同一次 ``--cov-report=json`` 的产物，在这里自己算。

**这不是装饰**：在此之前 ``AGENTS.md`` § 5 的「CI 阻断」三个字是假的——``pyproject.toml`` 里
没有 ``fail_under``，CI 里只有一个裸的 ``--cov=alterego``。把 ``domain/`` 的测试删光，CI 照样绿。

用法::

    python -m pytest tests -q --cov=alterego --cov-report=json
    python scripts/check_coverage.py            # 默认读 ./coverage.json
    python scripts/check_coverage.py <路径>      # 读指定报告

退出码：0 = 全部达标；1 = 有包低于下限或报告缺失。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


#: 包 → 语句覆盖率下限（百分数）。**改这里必须同步 ``AGENTS.md`` § 5 与 ``docs/DESIGN.md``。**
#: ``alterego`` 一项指的是整包合计（coverage 的 ``totals``），不是顶层那几个模块。
FLOORS: dict[str, float] = {
    "alterego": 85.0,
    "alterego.kernel": 90.0,
    "alterego.domain": 95.0,
    "alterego.sim": 85.0,
}

#: ``coverage.json`` 里的键是绝对或相对路径；用这个片段定位包根。
SOURCE_MARKER = "src/alterego/"

DEFAULT_REPORT = Path("coverage.json")


def _bucket(path: str) -> str:
    """把 ``.../src/alterego/kernel/config.py`` 归纳成 ``alterego.kernel``。"""
    normalised = path.replace("\\", "/")
    rest = normalised.split(SOURCE_MARKER, 1)[-1]
    parent = rest.rsplit("/", 1)[0] if "/" in rest else ""
    return f"alterego.{parent.replace('/', '.')}" if parent else "alterego"


def summarise(report: dict) -> dict[str, float]:
    """包名 → 语句覆盖率（百分数）。``alterego`` 是整包合计，其余按目录汇总。

    必须按 coverage 自己的口径算：``(命中行 + 命中分支) / (总行 + 总分支)``。
    只看 ``covered_lines / num_statements`` 得到的是**纯行**覆盖率，比 CLI 打印的那个数高
    一个百分点左右（本仓库 97.43% vs 96.52%），拿它去比阈值等于偷偷放宽了标准。
    ``totals["percent_covered"]`` 是 authoritative 的整包数字，直接用它，不自己重算。
    """
    totals = report["totals"]
    result = {"alterego": float(totals["percent_covered"])}

    grouped: dict[str, list[int]] = {}
    for path, info in report["files"].items():
        bucket = _bucket(path)
        if bucket == "alterego":  # 顶层单文件已计入 totals，不重复算
            continue
        summary = info["summary"]
        acc = grouped.setdefault(bucket, [0, 0, 0, 0])
        acc[0] += summary["covered_lines"]
        acc[1] += summary["num_statements"]
        acc[2] += summary["covered_branches"]
        acc[3] += summary["num_branches"]

    for bucket, (cov_lines, lines, cov_branch, branches) in grouped.items():
        denominator = lines + branches
        hit = cov_lines + cov_branch
        result[bucket] = 100.0 * hit / denominator if denominator else 100.0
    return result


def main(argv: list[str]) -> int:
    report_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_REPORT
    if not report_path.is_file():
        print(f"✗ 找不到覆盖率报告 {report_path}")
        print("  先跑：python -m pytest tests -q --cov=alterego --cov-report=json")
        return 1

    actual = summarise(json.loads(report_path.read_text(encoding="utf-8")))

    missing = [name for name in FLOORS if name not in actual]
    if missing:
        print(f"✗ 报告里没有这些包：{', '.join(missing)}")
        print("  多半是 --cov 的目标写错了，或者该包已经不存在。")
        return 1

    print(f"覆盖率下限核对（依据 {report_path}）\n")
    failed: list[str] = []
    for name, floor in FLOORS.items():
        got = actual[name]
        ok = got + 1e-9 >= floor
        if not ok:
            failed.append(name)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<20} {got:6.2f}%  下限 {floor:5.1f}%")

    print()
    if failed:
        print(f"✗ 覆盖率检查失败  {len(failed)} / {len(FLOORS)} 项不达标: {', '.join(failed)}")
        print("  依据: AGENTS.md § 5 硬性技术约束")
        print(
            "  该补测试就补测试；真要下调下限，先改 AGENTS.md § 5 与 docs/DESIGN.md，别只改这里。"
        )
        return 1

    print(f"✓ 覆盖率检查通过  共 {len(FLOORS)} 项，全部达标")
    print("  依据: AGENTS.md § 5 硬性技术约束")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

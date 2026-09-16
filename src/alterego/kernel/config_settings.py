"""``[settings]`` —— 设置中心自己的行为。

**为什么这个段不在 ``kernel/config.py`` 里。** 那个文件顶在 900/900
（``scripts/check_architecture.sh`` 第 23 项），一个字的余量都没有。
先例是 ``kernel/config_study.py`` 与 ``kernel/config_values.py``：
配置段的定义可以住在自己的模块里，由 ``config.py`` import 进来挂到
:class:`~alterego.kernel.config.Config` 上。

这四个键都是**关于设置界面自己的**，不是关于 Agent 的。它们和
``[plugins]`` / ``[web]`` 一样属于「系统级」，所以只有四个，
而且默认值都选在「不打扰你」那一边。
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = ["SettingsConfig"]


@dataclass(frozen=True)
class SettingsConfig:
    """``[settings]`` —— 设置中心自身。"""

    show_advanced: bool = False
    """设置页是否默认展开「高级」分组（直接编辑完整 TOML）。

    默认收起，但**不藏起来**：想直接改文件的人应该有一个带校验和差异预览的
    编辑器可用，否则他会去 vim 里改，然后靠重启失败来发现写错了。
    """

    auto_backup: bool = True
    """改动配置前是否自动留一份 ``alterego.toml.bak``。

    关掉它只影响「改坏了能不能一键退回去」，不影响别的。默认开着——
    配置写坏的代价是启动失败，而启动失败的人往往已经忘了自己刚改过什么。
    """

    show_unannotated: bool = True
    """是否显示推断出来的「未标注」设置项。

    你在 ``alterego.toml`` 里手写的、内核不认识的键会以「未标注」的样子出现。
    关掉它界面会干净，但你会以为自己的配置被丢了——而它其实还在，只是没显示。
    """

    confirm_diff: bool = True
    """保存前是否先给你看一遍差异。

    关掉后改一个值就是一次即时写入。之所以默认开着：设置页能改的东西里
    有 ``data_dir`` 这种一旦写错就让 Agent 找不到已有数据的项，
    而「我刚才到底改了什么」在看完差异之前是答不上来的。
    """

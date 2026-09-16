"""``alterego plugins`` —— 插件的加载、体检、查看与重载。

**这是整个程序的组装根之一**：只有这里、``cli.py`` 与未来的运行期装配
知道「插件是怎么被装起来的」。别的地方一律只认 :mod:`alterego.kernel.plugin`
里那些抽象（``Plugin`` / ``PluginContext`` / ``PluginManifest``）。

命令按「从便宜到昂贵」排：

| 命令 | 会导入插件代码吗 | 会执行 ``on_load`` / ``on_start`` 吗 |
| --- | --- | --- |
| ``list`` | 否 | 否 |
| ``info`` | 否 | 否 |
| ``reset`` | 否 | 否 |
| ``doctor`` | 是 | 是 |
| ``reload`` | 是 | 是（先卸再装一次） |

``list`` 与 ``info`` **刻意不导入插件代码**。它们最常被用来回答的正是
「这个插件为什么没生效」，而答案如果是「它的代码根本导入不了」，
那么一个必须先导入它才能跑的命令什么也证明不了——它会和插件一起坏掉。

## 一次性内核

本项目还没有常驻进程（``alterego serve`` 在下一批次），所以 ``doctor`` /
``reload`` / ``reset`` 都是**现搭一台内核、在当前进程里做一遍、然后报告**，
而不是去问某个正在跑的服务。这个选择有一个必须说清楚的后果：

* ``reload`` 仍然有意义：它真的会重新导入模块、重新走 ``on_load``，
  装不回去就是装不回去。
* ``reset`` **在一次性进程里几乎必然无事可做**。熔断状态活在
  ``PluginManager`` 实例的内存里，而这条命令每次都是一个新进程。
  它仍然保留，因为「现在没有任何插件处于熔断状态」是**真话**，
  而且等常驻进程落地后它接上去就立刻变得有用。写一个假装做成了什么的
  空操作，比不写更坏。

依据: ``docs/design/02-plugin-api.md`` § 4、§ 10、§ 16
"""

from __future__ import annotations

import argparse
import random
from collections.abc import Mapping
from typing import Any, Final

from alterego.cli_io import _RULE, _err, _out, _pad, _width
from alterego.cli_memory import _fail
from alterego.interfaces.common import HealthStatus
from alterego.kernel.bus import EventBus
from alterego.kernel.clock import RealClock, resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, PluginDependencyError
from alterego.kernel.loader import EXIT_DEPENDENCY_ERROR, DiscoveredPlugin, DiscoveryResult
from alterego.kernel.logging import get_logger
from alterego.kernel.manager import LoadReport, PluginManager
from alterego.kernel.manifest import (
    API_VERSION,
    ConfigField,
    PluginManifest,
    PluginStatus,
    is_api_version_compatible,
    resolve_config,
)
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


__all__ = [
    "add_plugins_parser",
    "cmd_plugins_doctor",
    "cmd_plugins_info",
    "cmd_plugins_list",
    "cmd_plugins_reload",
    "cmd_plugins_reset",
]


_log = get_logger("cli.plugins")

#: ``PluginStatus`` 的中文说法。插件作者看到的是状态机里的英文，用户看到的是这一列。
_STATUS_LABELS: Final[Mapping[PluginStatus, str]] = {
    PluginStatus.DISCOVERED: "已发现",
    PluginStatus.VALIDATED: "已校验",
    PluginStatus.LOADING: "加载中",
    PluginStatus.LOADED: "已加载",
    PluginStatus.STARTED: "已启动",
    PluginStatus.STOPPED: "已停止",
    PluginStatus.UNLOADED: "已卸载",
    PluginStatus.FAILED: "失败",
}


# ── 装配 ────────────────────────────────────────────────────


def _plugins_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _manager(config: Config) -> PluginManager:
    """装一台只用来观察或操作插件的内核。

    ``rng`` 显式按 ``config.core.random_seed`` 播种：不播种的话每次启动
    抖动都不一样，而 ``doctor`` 的输出是要能对得上的（设计原则 P6）。
    """
    zone = resolve_timezone(config.core.timezone)
    clock = RealClock(tz=zone)
    return PluginManager(
        config=config,
        bus=EventBus(clock, logger=_log),
        registry=ServiceRegistry(logger=_log),
        clock=clock,
        scheduler=Scheduler(clock, logger=_log),
        logger=_log,
        rng=random.Random(config.core.random_seed),
    )


def _config_label(config: Config) -> str:
    """配置文件在哪，供表头显示。

    没有配置文件时写「内置默认值」，而不是留空。空白的表头会把人引去找
    别的原因，而真实原因恰恰就是「没有配置文件，搜索路径还是默认的 ``plugins/``」。
    """
    if config.source is None:
        return "内置默认值（还没跑过 alterego init）"
    return str(config.source)


def _unknown_plugin(plugin_id: str, result: DiscoveryResult) -> int:
    """没找到这个插件。把**找到的**列出来——这是最有用的那一句。

    只写「未找到」会让人怀疑自己是不是拼错了 id，而实际原因常常是
    「插件放错目录了」，列出现有 id 能立刻分清这两种情况。
    """
    _err(f"错误：没有发现叫 {plugin_id} 的插件")
    if result.ids():
        _err(f"  发现的是：{', '.join(result.ids())}")
    else:
        _err("  一个插件都没发现——先用 `alterego plugins list` 看它在哪些目录里找。")
    return 2


def _source_text(manifest: PluginManifest) -> str:
    """插件的来源。本地插件写出目录名，好让人能直接去看。"""
    if manifest.source == "entry_point":
        return "已安装的包"
    if manifest.path is None:  # pragma: no cover - 本地插件必有 path
        return "本地"
    # ``<搜索根>/<插件目录名>``，例如 ``plugins/example_plugin``。
    # 显示完整绝对路径会把表格撑得没法看，而用户需要的信息只是「哪个目录」。
    return f"本地 {manifest.path.parent.name}/{manifest.path.name}"


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _render_value(value: Any) -> str:
    """把配置值打成一行。``None`` 说成「未设置」而不是 ``None``。"""
    if value is None:
        return "未设置"
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, str):
        return value or "（空字符串）"
    return str(value)


# ── list ────────────────────────────────────────────────────


def _print_discovery_failures(result: DiscoveryResult) -> None:
    """清单读不出来的目录。

    这一节不能省：插件「没生效」十有八九就坏在清单上，而清单坏了的插件
    不会出现在上面的表里——只打一张表，用户会以为那个目录被忽略了。
    """
    if not result.failed:
        return
    _out("")
    _out("清单读不出来的目录：")
    for path, error in result.failed:
        _out(f"  {path}")
        _out(f"    {error}")


def _print_nothing_found(config: Config) -> None:
    """一个插件都没发现。比起「空表」，说出找过哪里更有用。"""
    _out("一个插件都没发现。它会在这些目录里找：")
    _out("")
    for path in config.plugin_search_paths:
        _out(f"  {path}")
    _out("")
    _out("随包带了一个能跑的示例：plugins/example_plugin（写法见")
    _out("docs/design/02-plugin-api.md，以及它自己的 README.md）。")


def _print_disabled_hint(result: DiscoveryResult, enabled: set[str]) -> None:
    """发现了插件但一个都没启用。这里给出**可抄的那一行**，而不是「请去配置」。"""
    if enabled or not result.plugins:
        return
    _out(f"{len(result.plugins)} 个插件都被发现了，但一个都没启用。")
    _out("")
    _out("启用方式是把它们写进配置文件的 [plugins] 段，例如：")
    _out("")
    _out("  [plugins]")
    _out(f'  enabled = ["{result.ids()[0]}"]')
    _out("")
    _out("没配 enabled 时，只有清单里 enabled_by_default = true 的插件会被加载；")
    _out("随包的插件都是 false——插件不该在你没要求的时候自己跑起来。")


def _print_plugins(result: DiscoveryResult, enabled: set[str]) -> None:
    """一张表：状态、id、类型、版本、来源。

    列宽从数据算出来，不写死。写死的宽度会在插件 id 变长的那天悄悄错位，
    而错位的表格比没有表格更难看懂。
    """
    headers = ("状态", "插件", "类型", "版本", "来源")
    rows = [
        (
            "已启用" if plugin_id in enabled else "未启用",
            plugin_id,
            result.plugins[plugin_id].manifest.kind,
            result.plugins[plugin_id].manifest.version,
            _source_text(result.plugins[plugin_id].manifest),
        )
        for plugin_id in result.ids()
    ]
    widths = [
        max([_width(headers[index])] + [_width(row[index]) for row in rows])
        for index in range(len(headers))
    ]
    _out("  ".join(_pad(head, widths[index]) for index, head in enumerate(headers)).rstrip())
    _out("  ".join("─" * width for width in widths))
    for row in rows:
        _out("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(row)).rstrip())


def cmd_plugins_list(args: argparse.Namespace) -> int:
    """发现到的、以及其中哪些会被加载。不导入任何插件代码。"""
    del args  # 这个子命令没有参数
    config = _plugins_config()
    manager = _manager(config)
    result = manager.discover()
    enabled = set(manager.enabled_ids())

    _out(f"插件 · 配置 {_config_label(config)}")
    _out(_RULE)
    _out(f"发现 {len(result.plugins)} 个 · 启用 {len(enabled)} 个 · 插件 API 版本 {API_VERSION}")
    _out("")

    if result.plugins:
        _print_plugins(result, enabled)
        _out("")
        _print_disabled_hint(result, enabled)
    else:
        _print_nothing_found(config)
    _print_discovery_failures(result)
    return 0


# ── doctor ──────────────────────────────────────────────────


def _health_line(plugin_id: str, health: HealthStatus) -> list[str]:
    mark = "正常" if health.ok else "异常"
    lines = [f"{plugin_id}  {mark}"]
    if health.detail:
        lines.append(f"  {health.detail}")
    if health.hint:
        lines.append(f"  提示：{health.hint}")
    return lines


def _print_load_report(report: LoadReport) -> None:
    """加载结果里那些**不在**上面那张表里的事。

    ``auto_enabled`` 与 ``skipped_optional`` 值得单列：依赖被顺带装上来、
    可选依赖被跳过，都是「为什么和我写的不一样」的常见答案。
    """
    if report.auto_enabled:
        _out("")
        _out("因为被别人依赖而顺带启用的：" + "、".join(report.auto_enabled))
    if report.skipped_optional:
        _out("")
        _out("可选依赖缺失，已跳过（不影响加载）：")
        for plugin_id, dependencies in report.skipped_optional.items():
            _out(f"  {plugin_id} → {'、'.join(dependencies)}")
    if report.circuit_open:
        _out("")
        _out("已熔断（连续失败太多次，已停用）：" + "、".join(report.circuit_open))
        for plugin_id in report.circuit_open:
            _out(f"  运行 alterego plugins reset {plugin_id} 恢复")


def cmd_plugins_doctor(args: argparse.Namespace) -> int:
    """真的把插件装起来，报加载结果与健康状态。

    依赖成环或缺依赖时退出码是 3（``EXIT_DEPENDENCY_ERROR``），与
    ``docs/design/02-plugin-api.md`` § 8.2 一致——脚本要能把「配置写错了」
    和「插件之间接不起来」分开。
    """
    del args  # 这个子命令没有参数
    config = _plugins_config()
    manager = _manager(config)
    try:
        report = manager.load_all()
        health = manager.health_all()
    except PluginDependencyError as exc:
        _fail(exc)
        return EXIT_DEPENDENCY_ERROR
    finally:
        manager.shutdown()

    _out(f"插件体检 · 配置 {_config_label(config)}")
    _out(_RULE)
    _out(f"已启动 {len(report.loaded)} 个 · 失败 {len(report.failed)} 个")
    _out("")

    if not report.loaded and not report.failed:
        _out("没有需要体检的插件（当前配置没有启用任何插件）。")
        return 0

    for plugin_id in report.loaded:
        for line in _health_line(plugin_id, health.get(plugin_id, HealthStatus(ok=True))):
            _out(line)
    for plugin_id, reason in sorted(report.failed.items()):
        _out(f"{plugin_id}  失败")
        _out(f"  {reason}")

    _print_load_report(report)

    unhealthy = sorted(pid for pid, status in health.items() if not status.ok)
    if report.failed or unhealthy:
        _out("")
        _out(f"有 {len(report.failed)} 个插件没装上、{len(unhealthy)} 个不健康。")
        return 1
    _out("")
    _out(f"全部正常（{len(report.loaded)} 个，插件 API 版本 {API_VERSION}）。")
    return 0


# ── info ────────────────────────────────────────────────────


def _print_manifest(found: DiscoveredPlugin) -> None:
    manifest = found.manifest
    compatible = "兼容" if is_api_version_compatible(manifest.api_version) else "不兼容"
    pairs: list[tuple[str, str]] = [
        ("名称", manifest.display_name),
        ("类型", manifest.kind),
        ("版本", manifest.version),
        ("API 版本", f"{manifest.api_version}（内核 {API_VERSION}，{compatible}）"),
        ("来源", _source_text(manifest)),
        ("入口", manifest.entry),
        ("默认启用", _yes_no(manifest.enabled_by_default)),
        ("自动重载", _yes_no(manifest.auto_reload)),
        ("优先级", str(manifest.priority)),
    ]
    for label, value in (
        ("描述", manifest.description),
        ("作者", "、".join(manifest.authors)),
        ("许可", manifest.license),
        ("主页", manifest.homepage),
        ("标签", "、".join(manifest.tags)),
    ):
        if value:
            pairs.append((label, value))

    width = max(_width(label) for label, _ in pairs) + 1
    for label, value in pairs:
        _out(f"  {_pad(label, width)}{value}")

    for label, values in (
        ("需要", manifest.requires),
        ("可选", manifest.optional),
        ("提供", manifest.provides),
    ):
        if values:
            _out(f"  {_pad(label, width)}{'、'.join(values)}")


def _field_origin(field: ConfigField) -> str:
    """这一项的值从哪来。顺序与 :func:`resolve_config` 的优先级一致。"""
    if field.env:
        return f"环境变量 {field.env}"
    if field.required:
        return "必填，没有默认值"
    return "清单默认值"


def _print_config_fields(manifest: PluginManifest, config: Config) -> None:
    """清单声明的配置项，以及它们**现在**的值。

    值走 :meth:`PluginManifest.redacted_config`：``secret = true`` 的字段
    在这里是 ``***``。这一段经常被贴进 issue，而贴配置的人不会记得打码。
    """
    _out("")
    _out("配置")
    if not manifest.config:
        _out("  清单里没有声明任何配置项。")
        return

    try:
        resolved: Mapping[str, Any] | None = manifest.redacted_config(
            resolve_config(manifest, config.plugins.config_for(manifest.id), logger=None)
        )
    except AlterEgoError as exc:
        resolved = None
        _out(f"  现在解析不了：{exc}")
        _out("  （下面列出声明，值留空）")

    for name in sorted(manifest.config):
        field = manifest.config[name]
        # 显式注解成 ``list[str]``：``field.type`` 是一个 Literal 联合，
        # 让它推导会让 ``append("必填")`` 撞上类型检查。
        flags: list[str] = [field.type]
        if field.required:
            flags.append("必填")
        if field.secret:
            flags.append("密钥")
        current = "—" if resolved is None else _render_value(resolved.get(name))
        _out(f"  {name}  [{_field_origin(field)}]")
        _out(f"    {' · '.join(flags)}   当前值 {current}")
        if field.description:
            _out(f"    {field.description}")
        if field.choices:
            _out(f"    可选 {' / '.join(str(choice) for choice in field.choices)}")


def cmd_plugins_info(args: argparse.Namespace) -> int:
    """一个插件的全部细节。同样不导入它的代码。"""
    config = _plugins_config()
    manager = _manager(config)
    result = manager.discover()
    found = result.get(args.plugin_id)

    if found is None:
        return _unknown_plugin(args.plugin_id, result)

    _out(f"插件 · {found.id}")
    _out(_RULE)
    _print_manifest(found)
    _print_config_fields(found.manifest, config)

    _out("")
    _out("状态")
    # 不导入代码、也不 load，所以这里只能回答「配置上会不会加载它」。
    # 运行期的状态要去 `alterego plugins doctor` 看——那句话在两条命令里都说了，
    # 这样「info 说已启用、实际却没跑起来」就有地方可以追问。
    will_load = found.id in manager.enabled_ids()
    _out(
        f"  {'会被加载' if will_load else '不会加载'}"
        f"（[plugins] enabled {'里有它' if will_load else '里没有它'}）"
    )
    _out("  要确认它真的装得起来，运行 alterego plugins doctor。")
    _print_discovery_failures(result)
    return 0


# ── reload / reset ──────────────────────────────────────────


def cmd_plugins_reload(args: argparse.Namespace) -> int:
    """把一个本地插件卸掉再装一次。

    在这一版里它是「一次性内核里装一遍，再重载一遍」——也就是
    **验证重载这条路走得通**。重载失败不恢复旧实例（设计文档 § 10.3）。
    """
    config = _plugins_config()
    manager = _manager(config)
    result = manager.discover()
    if result.get(args.plugin_id) is None:
        return _unknown_plugin(args.plugin_id, result)

    try:
        report = manager.load_all()
        if args.plugin_id not in manager.order:
            _err(f"错误：{args.plugin_id} 不在这次加载的插件里，无法重载")
            _err("  它多半没有被启用（run alterego plugins info 看原因）。")
            return 2
        if not report.ok:
            _err(f"注意：这次加载本来就有失败项（{len(report.failed)} 个），重载结果可能被它影响。")
        ok = manager.reload(args.plugin_id)
        status = manager.status_of(args.plugin_id)
    finally:
        manager.shutdown()

    _out(f"重载 · {args.plugin_id}")
    _out(_RULE)
    _out(f"结果      {'成功' if ok else '失败'}")
    if status is not None:
        _out(f"当前状态  {_STATUS_LABELS.get(status, str(status))}")
    if not ok:
        _out("")
        _out("失败后它处于停用状态，不会恢复成旧实例——旧实例可能已经是半死的，")
        _out("把它请回来只会让问题更难查。修好代码后再跑一次本命令。")
        return 1
    return 0


def cmd_plugins_reset(args: argparse.Namespace) -> int:
    """解除熔断。

    熔断状态活在 ``PluginManager`` 实例的内存里，而本命令每次都是新进程，
    所以这里几乎总是回答「没有处于熔断状态的插件」。**这句话是真的**，
    而且是这个问题在一次性进程里唯一诚实的答案。
    """
    config = _plugins_config()
    manager = _manager(config)
    result = manager.discover()
    plugin_id = args.plugin_id

    if plugin_id is not None and result.get(plugin_id) is None:
        return _unknown_plugin(plugin_id, result)

    if plugin_id is None:
        opened = manager.circuit_open()
        _out(f"熔断检查 · 配置 {_config_label(config)}")
        _out(_RULE)
        if not opened:
            _out("没有插件处于熔断状态。")
        else:  # pragma: no cover - 一次性进程里不会有熔断的插件
            for pid in opened:
                _out(f"已重置 {pid}")
        return 0

    was_open = manager.reset(plugin_id)
    _out(f"重置 · {plugin_id}")
    _out(_RULE)
    _out("已经重置。" if was_open else "它本来就没有被熔断，没什么可重置的。")
    _out("")
    _out("熔断计数活在 PluginManager 的内存里，所以这条命令只在**同一个进程**里")
    _out("才有东西可清。常驻进程（alterego serve）落地后它会接上去问那个进程。")
    return 0


# ── 参数 ────────────────────────────────────────────────────


def add_plugins_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 ``alterego plugins`` 这一组命令。"""
    parser = commands.add_parser(
        "plugins",
        help="插件：有哪些、健不健康、装不装得起来",
        description="插件的加载与诊断。list 与 info 不导入插件代码，所以插件坏掉时它们照样能跑。",
    )
    subs = parser.add_subparsers(dest="subcommand", metavar="{list,doctor,info,reload,reset}")
    # 只敲到 `alterego plugins` 时打**这一层**的帮助，理由同 calendar / birthday。
    parser.set_defaults(subparser=parser)

    list_parser = subs.add_parser("list", help="发现到的插件，以及其中哪些会被加载")
    list_parser.set_defaults(handler=cmd_plugins_list)

    doctor_parser = subs.add_parser("doctor", help="真的装一遍，报加载结果与健康状态")
    doctor_parser.set_defaults(handler=cmd_plugins_doctor)

    info_parser = subs.add_parser("info", help="一个插件的清单与配置现状")
    info_parser.add_argument(
        "plugin_id", metavar="PLUGIN_ID", help="插件 id，例如 capability.example"
    )
    info_parser.set_defaults(handler=cmd_plugins_info)

    reload_parser = subs.add_parser("reload", help="卸掉再装一次（验证热重载走得通）")
    reload_parser.add_argument("plugin_id", metavar="PLUGIN_ID", help="插件 id")
    reload_parser.set_defaults(handler=cmd_plugins_reload)

    reset_parser = subs.add_parser("reset", help="解除熔断（不给 ID 时只报告现状）")
    reset_parser.add_argument(
        "plugin_id",
        metavar="PLUGIN_ID",
        nargs="?",
        default=None,
        help="插件 id；省略时列出当前被熔断的插件",
    )
    reset_parser.set_defaults(handler=cmd_plugins_reset)

"""``alterego config`` —— 设置中心在命令行上的那一半。

## 为什么命令行要看同一份元数据

如果 CLI 和网页各写一套说明，两份会在一周内开始不一致，而用户读到的是过时的
那一份。所以这里**不写任何一句关于配置项的话**——每一句都来自
:mod:`alterego.kernel.settings_catalog`，与（下一批次的）设置页共用同一个真源。
本模块只做三件事：取值、排版、把改动交给 :func:`save_setting` 落盘。

依据: docs/design/10-settings-center.md § 9「CLI 侧的同一份元数据」。

## 五条命令

| 命令 | 干什么 | 会写文件吗 |
| --- | --- | --- |
| ``show [分组或段]`` | 按分组列出全部设置，带「这是什么」与「改了会怎样」 | 否 |
| ``explain KEY`` | 只讲一个键：范围、选项、默认值、当前值、生效方式 | 否 |
| ``get KEY`` | 只打印当前值，给脚本用 | 否 |
| ``set KEY VALUE`` | 改一项并原子写回 | **是** |
| ``schema --json`` | 整份元数据导出成 JSON，给补全脚本和设置页用 | 否 |

只有 ``set`` 会碰文件，其余四条都是只读的——**要改东西必须先看到它会怎样**
（本分册 § 1 的全部理由）。

## 一个故意的取舍：``get`` 只打印值

``get`` 不打印标签、单位和说明，因为它的用户是 shell：
``$(alterego config get llm.routing.decision)``。字符串原样输出（不带引号），
其余类型输出 JSON 字面量——这条规则写在它的 ``--help`` 里，免得有人靠猜。
"""

from __future__ import annotations

import argparse
import difflib
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields
from datetime import time
from pathlib import Path
from typing import Any

from alterego.cli_io import _RULE, _err, _out, _pad
from alterego.cli_memory import _fail
from alterego.kernel.config import Config
from alterego.kernel.errors import ConfigError
from alterego.kernel.settings import Setting, infer_setting, is_annotated
from alterego.kernel.settings_catalog import (
    all_settings,
    get_setting_metadata,
    groups,
    section_paths,
)
from alterego.kernel.settings_write import render_value, save_setting


__all__ = [
    "add_config_parser",
    "cmd_config_explain",
    "cmd_config_get",
    "cmd_config_schema",
    "cmd_config_set",
    "cmd_config_show",
]


#: 一个键在文件里的样子。告诉用户「想让插件改它该去哪写」，也标出未标注的项。
_UNANNOTATED_MARK = "未标注"


# ── 配置 ────────────────────────────────────────────────────


def _config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


# ── 查键 ────────────────────────────────────────────────────


def _lookup(key: str) -> Setting:
    """把点分键变成元数据。

    键写错时给出「你是不是想写 X」，因为 ``llm.routing.decisoin`` 这种错法
    靠的是手指而不是理解，用户看一眼就能确认——而「没有这个配置段」不行。

    Raises:
        ConfigError: 段不存在、字段不存在，或字段的类型拼不出控件种类。
    """
    if key in section_paths:
        # ``alterego config get llm`` 这种：给的是段名，不是键。段本身没有值，
        # 所以要先说清楚「你问的东西在哪一层」，而不是报「没有这个配置段」。
        raise ConfigError(
            "这是一个配置段，不是设置项",
            key=key,
            hint=f"段本身没有值；用 `alterego config show {key}` 看它下面的项。",
        )
    parent, _, name = key.rpartition(".")
    cls = section_paths.get(parent)
    if cls is None:
        raise ConfigError(
            "没有这个配置段",
            key=key,
            section=parent,
            hint=_did_you_mean(parent, section_paths),
        )
    setting = get_setting_metadata(cls, name)
    if setting is not None:
        return setting
    declared = {info.name for info in fields(cls)}
    if name not in declared:
        raise ConfigError(
            f"{cls.__name__} 里没有这个键",
            key=key,
            available=sorted(declared),
            hint=_did_you_mean(name, declared),
        )
    inferred = infer_setting(cls, name, prefix=parent)
    if inferred is None:
        raise ConfigError(
            "这一项的类型拼不出对应的控件，所以设置页展示不了它",
            key=key,
            hint="给 kernel/settings_catalog_*.py 里补一条元数据，它就会出现在这里。",
        )
    return inferred


def _did_you_mean(needle: str, candidates: Iterable[str]) -> str:
    """拼一个「你是不是想写 X？」。找不到足够像的就不猜。"""
    close = difflib.get_close_matches(needle, list(candidates), n=1, cutoff=0.6)
    return f"你是不是想写 {close[0]}？" if close else ""


# ── 取值与排版 ──────────────────────────────────────────────


def _flatten(config: Config) -> dict[str, Any]:
    """把嵌套的配置摊成点分键 → 值。"""
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, Mapping):
            for name, value in node.items():
                path = f"{prefix}.{name}" if prefix else str(name)
                if isinstance(value, Mapping):
                    walk(path, value)
                else:
                    flat[path] = value

    walk("", config.to_dict(redact=True))
    return flat


def _show_value(value: Any) -> str:
    """值说成中文。``None`` 与空字符串要分得开——它们的含义完全不同。"""
    if value is None:
        return "未设置"
    if value == "":
        return "（空字符串）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value) if value else "（空）"
    return str(value)


def _with_unit(value: Any, setting: Setting) -> str:
    text = _show_value(value)
    return f"{text} {setting.unit}".strip() if setting.unit else text


def _marks(setting: Setting) -> str:
    """危险 / 需重启 / 高级 / 未标注。"""
    flags = []
    if setting.danger:
        flags.append("危险")
    if setting.requires_restart:
        flags.append("需重启")
    if setting.advanced:
        flags.append("高级")
    if not is_annotated(setting):
        flags.append(_UNANNOTATED_MARK)
    return "  ".join(f"[{flag}]" for flag in flags)


def _heading(text: str) -> None:
    _out()
    _out(_RULE)
    _out(text)
    _out(_RULE)


def _matches(setting: Setting, needle: str) -> bool:
    """``show`` 的筛选：认分组名（「预算」）、段路径（``llm`` / ``llm.routing``）与整键。"""
    if not needle:
        return True
    if setting.group == needle or setting.key == needle:
        return True
    return setting.key.startswith(needle.rstrip(".") + ".")


# ── show ────────────────────────────────────────────────────


def cmd_config_show(args: argparse.Namespace) -> int:
    """按分组列出设置。

    默认**全部列出**，包括高级项——CLI 的用户是来查东西的，藏起来只会让他
    以为自己要找的那一项不存在。高级项带 ``[高级]`` 标记，由他自己决定要不要看。
    """
    config = _config()
    flat = _flatten(config)
    needle = args.filter or ""

    selected = [setting for setting in all_settings() if _matches(setting, needle)]
    if not selected:
        _err(f"没有匹配 {needle!r} 的设置项。")
        _err(f"分组有：{'、'.join(groups())}")
        _err(f"段有：{'、'.join(sorted(section_paths))}")
        return 2

    if args.json:
        payload = {
            "source": str(config.source) if config.source is not None else None,
            "settings": [_as_json(setting, flat) for setting in selected],
            "unannotated": _unannotated(config),
        }
        _out(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _out(f"配置文件  {config.source if config.source is not None else '（内置默认值）'}")
    current_group = ""
    for setting in selected:
        if setting.group != current_group:
            current_group = setting.group
            _heading(setting.group or "（未分组）")
        else:
            _out()
        _out(f"  {setting.key}    {_marks(setting)}".rstrip())
        _out(f"  {'':<32}{_with_unit(flat.get(setting.key), setting)}")
        _out(f"    {setting.label}：{setting.description}")
        if is_annotated(setting):
            _out(f"    改了会怎样：{setting.effect}")
        else:
            _out("    改了会怎样：**这一项还没有人写说明**（设置页会把它标成未标注）")
        if setting.depends_on:
            _out(f"    {setting.depends_on}")

    _print_unannotated(config)
    _out()
    _out(f"共 {len(selected)} 项，来自 {len(groups())} 个分组。")
    _out("改一项：alterego config set <键> <值>；看一项：alterego config explain <键>")
    return 0


# ── explain ─────────────────────────────────────────────────


def cmd_config_explain(args: argparse.Namespace) -> int:
    """把一个键讲全：它是什么、改了会怎样、现在是什么、能填什么。"""
    try:
        setting = _lookup(args.key)
    except ConfigError as exc:
        return _fail(exc)

    flat = _flatten(_config())
    current = flat.get(setting.key)

    _out(setting.label)
    _out(_RULE)
    _out(f"键        {setting.key}")
    _out(
        f"写在哪    [{setting.key.rpartition('.')[0]}] 段里，键名 {setting.key.rpartition('.')[2]}"
    )
    _out(f"是什么    {setting.description or '（没有说明）'}")
    _out(f"改了会怎样  {setting.effect or f'（没有说明——{_UNANNOTATED_MARK}）'}")
    _out(f"当前值    {_with_unit(current, setting)}")
    _out(f"默认值    {_with_unit(setting.default, setting)}")

    bounds = _bounds(setting)
    if bounds:
        _out(f"取值范围  {bounds}")
    _out(f"生效方式  {'重启后生效' if setting.requires_restart else '下一处用到它时立即生效'}")
    if setting.danger:
        _out("危险项    改错会让 Agent 行为异常，改之前先记下原值。")
    if setting.depends_on:
        _out(f"前提      {setting.depends_on}")
    if setting.choices:
        _out()
        _out("可选值")
        for choice in setting.choices:
            _out(f"  {_pad(choice.value, 14)}{choice.label}")
            _out(f"  {'':<14}{choice.consequence or '（没有说明）'}")
    return 0


def _bounds(setting: Setting) -> str:
    if setting.minimum is None and setting.maximum is None:
        return ""
    low = "不限" if setting.minimum is None else _plain(setting.minimum)
    high = "不限" if setting.maximum is None else _plain(setting.maximum)
    return f"{low} ~ {high} {setting.unit}".strip()


def _plain(value: Any) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


# ── get ─────────────────────────────────────────────────────


def cmd_config_get(args: argparse.Namespace) -> int:
    """只打印值，给脚本用。

    字符串原样输出，不带引号——``$(alterego config get core.timezone)`` 要拿到的是
    ``Asia/Shanghai``，不是一个带引号的 JSON 字符串。其余类型输出 JSON 字面量，
    这样数组和布尔值在 shell 里也是可判断的。
    """
    try:
        setting = _lookup(args.key)
    except ConfigError as exc:
        return _fail(exc)

    value = _flatten(_config()).get(setting.key)
    if value is None:
        # 空值不打印空行：``x=$(alterego config get ...)`` 拿到空串时无法与
        # 「命令失败了」区分。给一句话，但退出码仍然是 0——这**不是**错误。
        _out("")
        _err(f"{setting.key} 现在是未设置（默认值 {_show_value(setting.default)}）")
        return 0
    if isinstance(value, str):
        _out(value)
    else:
        _out(json.dumps(_jsonable(value), ensure_ascii=False))
    return 0


# ── set ─────────────────────────────────────────────────────


def cmd_config_set(args: argparse.Namespace) -> int:
    """改一项设置并原子写回。

    写回之后**重新加载**配置并把新旧值都打出来，因为「我改的到底是哪一项」
    和「它现在真的是这个值吗」是两个问题，而只有第二个值得担心。
    """
    try:
        setting = _lookup(args.key)
    except ConfigError as exc:
        return _fail(exc)

    config = _config()
    if config.source is None:
        return _fail(
            ConfigError(
                "还没有配置文件，没有地方可以写",
                hint="先运行 `alterego init` 生成 config/alterego.toml。",
            )
        )

    before = _flatten(config).get(setting.key)
    try:
        literal = render_value(setting, args.value)
    except ConfigError as exc:
        return _fail(exc)

    if args.dry_run:
        _out(f"{setting.key}  {_show_value(before)} → {_show_value(_preview(setting, args.value))}")
        _out(
            f"会写进 [{setting.key.rpartition('.')[0]}]：{setting.key.rpartition('.')[2]} = {literal}"
        )
        _out(f"（--dry-run，{config.source} 没有被改动）")
        return 0

    backup = config.settings.auto_backup and not args.no_backup
    try:
        save_setting(config.source, setting.key, args.value, setting=setting, backup=backup)
    except ConfigError as exc:
        return _fail(exc)

    _out(f"{setting.key}")
    _out(f"  {_show_value(before)}  →  {_show_value(_preview(setting, args.value))}")
    _out(
        f"  已写入 {config.source}"
        + (f"（备份 {config.source.name}.bak）" if backup else "（未备份）")
    )
    if setting.requires_restart:
        _out("  [需重启] 这一项要重启 alterego 才会生效。")
    return 0


def _preview(setting: Setting, raw: str) -> Any:
    """把用户给的字符串说成「值」的样子，只为打印新旧对比。"""
    text = raw.strip()
    if setting.choices:
        return text
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.isdigit():
        return int(text)
    return text


# ── schema ──────────────────────────────────────────────────


def cmd_config_schema(args: argparse.Namespace) -> int:
    """导出整份元数据。

    给两种人用：补全脚本（要机器可读）和设置页（要同一份东西渲染）。
    它刻意不读配置文件——schema 回答的是「有哪些设置」，不是「你设成了什么」。
    """
    if not args.json:
        counts: dict[str, int] = dict.fromkeys(groups(), 0)
        for setting in all_settings():
            counts[setting.group] = counts.get(setting.group, 0) + 1
        _out(f"共 {len(all_settings())} 个设置项，{len(section_paths)} 个配置段。")
        _out()
        for name, count in counts.items():
            _out(f"  {_pad(name, 16)}{count} 项")
        _out()
        _out("加 --json 导出完整元数据（供补全脚本与设置页使用）。")
        return 0

    payload = {
        "version": 1,
        "sections": {path: cls.__name__ for path, cls in section_paths.items()},
        "groups": list(groups()),
        "settings": [_as_json(setting, {}) for setting in all_settings()],
    }
    _out(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ── 序列化 ──────────────────────────────────────────────────


def _as_json(setting: Setting, flat: Mapping[str, Any]) -> dict[str, Any]:
    """一条元数据 → JSON 友好的 dict。"""
    payload = asdict(setting)
    payload["kind"] = str(setting.kind)
    payload["default"] = _jsonable(setting.default)
    payload["choices"] = [
        {"value": choice.value, "label": choice.label, "consequence": choice.consequence}
        for choice in setting.choices
    ]
    payload["annotated"] = is_annotated(setting)
    payload["current"] = _jsonable(flat.get(setting.key)) if setting.key in flat else None
    return payload


def _jsonable(value: Any) -> Any:
    """``json.dumps`` 不认识 ``Path`` 与 ``time``，转掉它们。"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# ── 未标注的键 ──────────────────────────────────────────────


def _unannotated(config: Config) -> list[dict[str, Any]]:
    """配置文件里存在、内核不认识的键（本分册 § 3 的第三层）。

    它们**只告警不失败**——插件可能需要它们（P4）。所以这里做的事就一件：
    把「它还在」说出来。静默忽略会让用户以为自己的配置丢了。
    """
    if not config.unknown_keys or config.source is None:
        return []
    try:
        document = tomllib.loads(config.source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        # 报告未标注的键不值得让 show 失败：它们本来就只是「顺带一提」。
        return [{"key": key, "value": None} for key in config.unknown_keys]
    return [{"key": key, "value": _jsonable(_dig(document, key))} for key in config.unknown_keys]


def _dig(document: Mapping[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _print_unannotated(config: Config) -> None:
    entries = _unannotated(config)
    if not entries:
        return
    _heading(f"未标注（{len(entries)} 项）")
    _out("这些键在你的配置文件里，但内核不认识它们——写它们的多半是某个插件。")
    _out("它们没有被忽略，只是没有说明可显示。")
    for entry in entries:
        _out(f"  {entry['key']} = {json.dumps(entry['value'], ensure_ascii=False)}")


# ── 注册 ────────────────────────────────────────────────────


def add_config_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 ``alterego config`` 这一组命令。"""
    parser = commands.add_parser(
        "config",
        help="设置：有哪些项、每项改了会怎样、现在是什么值",
        description=(
            "设置的查看与修改。说明文字来自内核的同一份元数据，"
            "所以这里的说法与将来的设置页完全一致。只有 set 会改文件。"
        ),
    )
    subs = parser.add_subparsers(dest="subcommand", metavar="{explain,get,schema,set,show}")
    # 只敲到 `alterego config` 时打**这一层**的帮助，理由同 plugins / calendar。
    parser.set_defaults(subparser=parser)

    show_parser = subs.add_parser("show", help="按分组列出设置，带说明与影响")
    show_parser.add_argument(
        "filter",
        metavar="分组或段",
        nargs="?",
        default="",
        help="只看一部分：分组名（预算）或段路径（llm、llm.routing）；省略则全部",
    )
    show_parser.add_argument("--json", action="store_true", help="输出 JSON，供脚本使用")
    show_parser.set_defaults(handler=cmd_config_show)

    explain_parser = subs.add_parser("explain", help="讲清楚一个键：范围、选项、默认值、当前值")
    explain_parser.add_argument("key", metavar="KEY", help="点分键，例如 llm.routing.decision")
    explain_parser.set_defaults(handler=cmd_config_explain)

    get_parser = subs.add_parser("get", help="只打印当前值（字符串原样，其余输出 JSON）")
    get_parser.add_argument("key", metavar="KEY", help="点分键")
    get_parser.set_defaults(handler=cmd_config_get)

    set_parser = subs.add_parser("set", help="改一项并原子写回配置文件")
    set_parser.add_argument("key", metavar="KEY", help="点分键")
    set_parser.add_argument("value", metavar="VALUE", help="新值；列表用逗号分隔")
    set_parser.add_argument("--dry-run", action="store_true", help="只显示会写什么，不改文件")
    set_parser.add_argument("--no-backup", action="store_true", help="这次不留 .bak 备份")
    set_parser.set_defaults(handler=cmd_config_set)

    schema_parser = subs.add_parser("schema", help="导出整份元数据（不读配置文件）")
    schema_parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    schema_parser.set_defaults(handler=cmd_config_schema)

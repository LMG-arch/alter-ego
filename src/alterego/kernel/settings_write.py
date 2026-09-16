"""把设置写回 ``alterego.toml``。

## 为什么是「改一行」而不是「重写整个文件」

``docs/design/10-settings-center.md`` § 5.3 写的是 ``save_config(path, data)``：
把整棵配置树渲染成 TOML 写回去。落地的版本比它窄，只改**那一个键的那一行**，
因为重写整个文件会做一件不能接受的事：**抹掉用户的注释**。

而 ``templates/alterego.toml`` 是给人抄的，里面每一段都有中文注释说明这一项改了
会发生什么；一个用户把 ``daily_message_limit`` 从 3 改成 5，结果整个文件的注释全没了，
他不会认为「设置保存成功」，他会认为这个项目毁了他的配置。所以这一版用文本替换：

- 保留注释、空行、键的顺序、原有的缩进与对齐；
- 只碰 ``key = ...`` 那一行的**值**，连等号两边的空格都照旧；
- 多行数组（跨行的 ``[...]``）整体算一行，不会只换掉第一行；
- 键不存在时补在它所属的段末尾；段不存在时补在文件末尾。

## 写之前一定要先验，而且用**同一个**校验器

流程是「写临时文件 → 用 :meth:`Config.load` 读它 → 通过了才 ``os.replace``」。
关键在于第二步用的是**加载时同一个** ``Config.load``，也就是同一批
``__post_init__``。自己另写一套「写入前校验」一定会和加载时的规则漂移，
而漂移的后果是最坏的一种：写进去的时候通过了，下次启动却报配置非法——
用户会以为是自己改坏的（其实是他改的，但工具本该拦住）。

校验失败时临时文件被删掉，原文件**一个字都没动**。

## 为什么这些函数在 kernel 里

:func:`patch_text` / :func:`render_value` 是**纯函数**：给一段文本和一个值，
还一段新文本。不碰数据库、不调模型，CLI 与（下一批次的）Web 设置页可以共用同一份，
而「一个真源，多处渲染」正是本分册 § 9 的全部要求。
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tomllib
from pathlib import Path

from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, ConfigError
from alterego.kernel.settings import Setting, SettingKind


__all__ = ["patch_text", "render_value", "save_setting"]


#: ``[llm.routing]`` 这样的表头。右边的注释允许存在：``[core]  # 常规``。
_HEADER = re.compile(r"^\[([^\]]+)\]\s*(?:#.*)?$")

#: ``key =`` 或 ``key=``。值那一部分由 :func:`_value_end` 决定占几行。
_ASSIGN = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.]*)(\s*)=(\s*)")

_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


# ═══════════════════════════════════════════════════════════════════════
#  值 → TOML 字面量
# ═══════════════════════════════════════════════════════════════════════


def render_value(setting: Setting, raw: str) -> str:
    """把命令行给的一串字，变成能写进 TOML 的字面量。

    校验在这里做**一次**，用的是元数据里的 ``kind`` / ``choices`` /
    ``minimum`` / ``maximum``。它不替代 ``Config.load`` 的校验——
    两者都要过：这里拦住的是「一眼就不对」的输入，并且能给出比
    ``__post_init__`` 更贴题的话（比如把合法选项列出来）。

    Raises:
        ConfigError: 输入不符合这一项的 `kind`、选项或取值范围。
    """
    text = raw.strip()
    kind = setting.kind

    if kind is SettingKind.BOOL:
        return "true" if _parse_bool(setting, text) else "false"
    if kind is SettingKind.INT:
        return str(_parse_number(setting, text, integral=True))
    if kind is SettingKind.FLOAT:
        value = _parse_number(setting, text, integral=False)
        if not math.isfinite(value):
            raise ConfigError(f"{setting.key} 要一个有限的数字", value=text)
        # TOML 里 ``1`` 与 ``1.0`` 是两种类型，而这一项要的是浮点。
        # 有限浮点的 ``repr`` 一定带小数点或指数（``1.0`` / ``1e+300``），
        # 所以直接用它就够，不用手拼。
        return repr(value)
    if kind is SettingKind.ENUM:
        allowed = [choice.value for choice in setting.choices]
        if text not in allowed:
            raise ConfigError(
                f"{setting.key} 只能填 {' / '.join(allowed)}",
                value=raw,
                supported=allowed,
                hint=f"{setting.label}：{setting.description}",
            )
        return toml_literal(text)
    if kind is SettingKind.LIST:
        # 用逗号分隔。条目里带逗号的值没办法这样写，而现有的列表型配置项
        # （形状名、路径、脱敏词、时:分）没有一个是会带逗号的。
        items = [item.strip() for item in text.split(",")]
        return "[" + ", ".join(toml_literal(item) for item in items if item) + "]"
    if kind is SettingKind.SECRET:
        raise ConfigError(
            "密钥不写进配置文件",
            key=setting.key,
            hint=(
                "alterego.toml 是要提交的，写进去的密钥会进 git；"
                "请在环境变量里设置它，配置文件里用 ${VAR_NAME} 引用。"
            ),
        )
    if kind is SettingKind.MAPPING:
        raise ConfigError(
            f"{setting.key} 是一整段表，不能当成一个值来设置",
            key=setting.key,
            hint="这一段的每一行是独立的设置项；直接改 alterego.toml 里的对应段落。",
        )
    return toml_literal(text)


def toml_literal(text: str) -> str:
    """把字符串写成 TOML 基本字符串（转义反斜杠与引号）。

    单引号字面量（``'...'``）能让 Windows 路径原样通过，但它里面连一个
    单引号都放不下。统一用双引号 + 转义：任何一种值都能表示，没有例外。
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_bool(setting: Setting, text: str) -> bool:
    lowered = text.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ConfigError(
        f"{setting.key} 只能填 true 或 false",
        value=text,
        hint=f"想打开的写法是 true，想关掉的写法是 false。「{setting.label}」{setting.effect}",
    )


def _parse_number(setting: Setting, text: str, *, integral: bool) -> int | float:
    try:
        value: int | float = int(text) if integral else float(text)
    except ValueError as exc:
        what = "整数" if integral else "数字"
        raise ConfigError(f"{setting.key} 要填{what}，拿到的是 {text!r}", value=text) from exc
    if setting.minimum is not None and value < setting.minimum:
        raise ConfigError(
            f"{setting.key} 不能小于 {_number(setting.minimum)}",
            value=value,
            minimum=setting.minimum,
            hint=setting.effect,
        )
    if setting.maximum is not None and value > setting.maximum:
        raise ConfigError(
            f"{setting.key} 不能大于 {_number(setting.maximum)}",
            value=value,
            maximum=setting.maximum,
            hint=setting.effect,
        )
    return value


def _number(value: int | float) -> str:
    """整数不带小数点显示：``1.0`` 说成「1」更好读。"""
    return str(int(value)) if float(value).is_integer() else str(value)


# ═══════════════════════════════════════════════════════════════════════
#  文本 → 文本
# ═══════════════════════════════════════════════════════════════════════


def patch_text(text: str, key: str, literal: str) -> str:
    """把 ``key = literal`` 写进 TOML **文本**，其余一个字节都不动。

    纯函数：同样的输入永远得到同样的输出，不碰文件系统。测试因此可以直接
    喂一段带注释、带空行、带跨行数组的 TOML 进去，比对比对结果。

    Args:
        text: 原文件内容。
        key: 点分键，如 ``"llm.routing.decision"``。
        literal: 已经渲染好的 TOML 字面量，来自 :func:`render_value`。

    Returns:
        改好的一整段文本（换行风格与原文一致）。

    Raises:
        ConfigError: ``key`` 只有一段（没有段名），无法定位到表。
    """
    parts = key.split(".")
    if len(parts) < 2:
        raise ConfigError("设置项的键必须有段名", key=key)

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    leaf = parts[-1]
    section = ".".join(parts[:-1])
    header_at = _header_at(lines, section)
    assign_at = _assignment_at(lines, key)

    if assign_at is None:
        lines = _insert(lines, header_at, section, leaf, literal)
    else:
        end, tail = _value_span(lines, assign_at)
        # ``tail`` 是值后面剩下的东西（对齐空格 + 注释）。留着它，这个函数
        # 才配叫「只换值」——把用户写的注释吃掉是最没道理的副作用。
        lines[assign_at : end + 1] = [f"{_prefix(lines[assign_at])}{literal}{tail}"]
    joined = newline.join(lines)
    # ``splitlines`` 丢掉了末尾的换行；原文有就补回去，没有就不补：
    # 一个只有本工具会写的文件和一个用户手写的文件，末尾不应该长得不一样。
    if text.endswith(("\n", "\r")):
        return joined + newline
    return joined


def _header_at(lines: list[str], section: str) -> int | None:
    """表头所在的行号。"""
    for index, line in enumerate(lines):
        match = _HEADER.match(line.strip())
        if match and match.group(1).strip() == section:
            return index
    return None


def _section_end(lines: list[str], header_at: int) -> int:
    """段落的结束行号（不含）：下一个表头，或文件末尾。"""
    for index in range(header_at + 1, len(lines)):
        if _HEADER.match(lines[index].strip()):
            return index
    return len(lines)


def _first_header(lines: list[str]) -> int:
    """第一个表头所在的行号；整个文件没有表头就是文件末尾。"""
    for index, line in enumerate(lines):
        if _HEADER.match(line.strip()):
            return index
    return len(lines)


def _assignment_at(lines: list[str], key: str) -> int | None:
    """赋值语句所在的行号。

    同一个键在 TOML 里有三种等价写法，漏掉任何一种都会**补出第二个同名键**，
    于是同一个键在文件里出现两次，下次启动直接报 TOML 非法：

    - ``[llm.routing]`` 里的 ``decision = "strong"``；
    - ``[llm]`` 里的 ``routing.decision = "strong"``；
    - 根表（第一个表头之前）里的 ``llm.routing.decision = "strong"``。

    三种写法可以合成一条规则：表头路径与行里的名字**拼起来正好等于整键**。
    按这条规则只在这个键的祖先表里找，绝不全文找短名——``[a]`` 与 ``[b]``
    都有 ``timeout`` 时，全文找会把 b 的那一行改掉，那是一次静默的错写，
    比补一行重复键更糟。
    """
    root_end = _first_header(lines)
    found = _scan(lines, range(0, root_end), "", key)
    if found is not None:
        return found

    parts = key.split(".")
    # 从最具体的一段往上一层走：``[llm.routing]`` 要先于 ``[llm]`` 被看到。
    for depth in range(len(parts) - 1, 0, -1):
        section = ".".join(parts[:depth])
        at = _header_at(lines, section)
        if at is None:
            continue
        rows = range(at + 1, _section_end(lines, at))
        found = _scan(lines, rows, section, key)
        if found is not None:
            return found
    return None


def _scan(lines: list[str], rows: range, section: str, key: str) -> int | None:
    """在 ``rows`` 里找「拼出来等于 key」的那一行；``section`` 为空表示根表。"""
    for index in rows:
        match = _ASSIGN.match(lines[index])
        if match is None:
            continue
        name = match.group(2)
        resolved = f"{section}.{name}" if section else name
        if resolved == key:
            return index
    return None


def _prefix(line: str) -> str:
    """保留 ``key`` 原本的写法与等号两边的空格，只换值。"""
    match = _ASSIGN.match(line)
    if match is None:  # pragma: no cover - 调用方只会传 _scan 命中的行
        return ""
    return line[: match.end()]


def _value_span(lines: list[str], start: int) -> tuple[int, str]:
    """一条赋值语句占到最后一行是第几行，以及那一行在值之后剩下的东西。

    返回的行号是**绝对于整个文件**的，调用方直接拿它去切片。

    返回值里的 ``tail``（对齐空格 + 注释）必须原样留着：这个函数存在的第一
    个理由就是「改一个值不要顺手把用户写的注释删掉」。

    跨行的数组是第二个理由：``formats = [`` 之后每一项一行，最后一行才是 ``]``。
    只按「一行」替换会把数组里剩下的几行留成孤儿，于是文件变成非法的 TOML。
    """
    joined = "\n".join(lines[start:])
    depth = 0
    quote = ""
    escape = False
    comment = False
    line = start
    token_end = joined.index("=") + 1
    for index, char in enumerate(joined[token_end:], start=token_end):
        if char == "\n":
            if depth == 0 and not quote:
                return line, joined[token_end:index]
            comment = False
            line += 1
            continue
        if comment:
            continue
        if quote:
            if escape:
                escape = False
            elif quote == '"' and char == "\\":
                # 基本字符串里的转义引号 ``\"``：不认它会把字符串的边界数错，
                # 于是把后面的换行当成「还在字符串里」，一路吞掉下面几行。
                escape = True
            elif char == quote:
                quote = ""
            token_end = index + 1
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            comment = True
            continue
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char.isspace():
            # 值前后的空白不算「值的一部分」，这样尾部的对齐空格与注释才会
            # 被认成「值之后剩下的东西」。
            continue
        token_end = index + 1
    return len(lines) - 1, ""


def _insert(
    lines: list[str], header_at: int | None, section: str, leaf: str, literal: str
) -> list[str]:
    """键不在文件里时的补写位置。

    段在就补在段末尾——补在文件末尾会让它落进**最后一个**段里，于是写下去的键
    属于别的配置段，加载时会被当成那个段的未知键告警，而用户要改的那一项没变。
    """
    row = f"{leaf} = {literal}"
    if header_at is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", f"[{section}]", row] if lines else [f"[{section}]", row])
        return lines
    at = _section_end(lines, header_at)
    while at > header_at + 1 and not lines[at - 1].strip():
        at -= 1
    lines.insert(at, row)
    return lines


# ═══════════════════════════════════════════════════════════════════════
#  落盘
# ═══════════════════════════════════════════════════════════════════════


def save_setting(
    path: Path,
    key: str,
    raw: str,
    *,
    setting: Setting,
    backup: bool = True,
) -> Config:
    """改一项设置并原子地写回，返回**重新加载后**的新配置。

    步骤（``docs/design/10-settings-center.md`` § 5.3）::

        读原文件 → 确认它是合法 TOML → 文本替换 → 写同目录临时文件
        → flush + fsync → 用 Config.load 读临时文件 → 通过 → 备份 → os.replace

    每一步都在挡一类具体的失败：

    - 「确认它是合法 TOML」：文件已经被手改坏了的时候，先报「你的文件现在读不了」，
      而不是拿一段坏文本去替换，把坏的地方也一起写进去。
    - 「同目录临时文件」：跨文件系统的 ``os.replace`` 不是原子的。
    - ``flush`` + ``os.fsync``：不 fsync 时 ``replace`` 已经成功，但内容还在页缓存里，
      断电之后文件是空的。
    - ``Config.load`` 读临时文件：**验的就是将要落盘的那份内容**，不多也不少。
    - ``os.replace``：POSIX 与 Windows 上都是原子的（同目录）。

    Args:
        path: ``alterego.toml`` 的路径。
        key: 点分键。
        raw: 用户给的原始值（命令行字符串）。
        setting: 这一项的元数据。
        backup: 是否留一份 ``.bak``。由 ``settings.auto_backup`` 决定。

    Returns:
        重新加载后的配置。

    Raises:
        ConfigError: 文件不存在、读不了、不是合法 TOML，或新值没通过加载校验。
    """
    text = _read(path)
    literal = render_value(setting, raw)
    updated = patch_text(text, key, literal)

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        # 用加载时**同一个**校验器读将要落盘的那份内容
        config = Config.load(path=tmp, env=os.environ)
    except AlterEgoError:
        tmp.unlink(missing_ok=True)
        raise

    if backup:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp.replace(path)
    return config


def _read(path: Path) -> str:
    """读配置文件。读不到就直接说清楚，不要在后面某一步抛 OSError。"""
    if not path.exists():
        raise ConfigError(
            "找不到配置文件",
            path=str(path),
            hint="先运行 `alterego init` 生成 config/alterego.toml。",
        )
    if not path.is_file():
        # 目录、设备文件之类。分开报是因为「给你一个目录」和「文件不在」
        # 是两种不同的手滑，提示也该不一样。
        raise ConfigError("这个路径不是一个文件", path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("配置文件无法读取", path=str(path), error=str(exc)) from exc
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            "配置文件现在不是合法 TOML，先修好它再改设置",
            path=str(path),
            error=str(exc),
            hint="改动会先经过文本替换再加载校验，拿一段读不了的文本去替换只会把坏的地方一起写进去。",
        ) from exc
    return text

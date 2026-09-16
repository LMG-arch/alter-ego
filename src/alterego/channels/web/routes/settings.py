"""``/api/settings`` —— 设置页。

**这一页和 ``alterego config`` 是同一个东西的两种呈现，所以它们共用一份
序列化。** 这里直接复用 :mod:`alterego.cli_config` 里的 ``_as_json`` /
``_flatten`` / ``_unannotated``，而不是在 Web 层再抄一遍：抄一份的结果是
某次改动静默地只改了一边，于是命令行说默认值是 3、页面上写着 5。
两个界面显示两个默认值是所有 bug 里最难解释的一种。

**密钥永远不出浏览器。** 三条一起构成这件事：

1. ``GET /api/settings`` 里，``kind = secret`` 项只回一个 ``"set": true/false``，
   真实值一个字节都不进响应；
2. ``POST /api/settings`` 拒收任何 ``kind = secret`` 的键（403），
   理由是密钥本来就该走环境变量——能通过页面写进去的密钥，
   迟早会被写进某份被提交的配置文件；
3. 掩码本身由 :meth:`Config.to_dict` 的 ``redact`` 负责，这一层不重复实现，
   以免某天两处的判定规则不一样。

**页面不重启进程。** 改了 ``requires_restart`` 的项，响应里带
``restart_needed: ["core.data_dir", ...]``，页面上显示「待重启生效」。
从浏览器里点一下就重启进程，等于给了页面一个「随便弄死我自己」的按钮，
而用户点它的理由多半只是好奇。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, HTTPException

from alterego.channels.web.routes.base import Deps, bad_request, unavailable
from alterego.cli_config import _as_json, _flatten, _unannotated
from alterego.kernel.config import Config
from alterego.kernel.errors import ConfigError
from alterego.kernel.settings import Setting, SettingKind, infer_setting_from_value
from alterego.kernel.settings_catalog import all_settings, groups, section_paths
from alterego.kernel.settings_write import save_setting, save_settings


__all__ = ["router"]

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: 一个值最长多少字符。它挡的是「把一整份文件贴进一个输入框」，
#: 而那样的值没有一个设置项能接受。往页面上写长文本不是设置该干的事。
MAX_VALUE: Final[int] = 4000


def _require_setting(key: str) -> Setting:
    """按点分键找元数据。找不到就说清「键写错了」而不是「没有这一项」。"""
    for setting in all_settings():
        if setting.key == key:
            return setting
    known = "、".join(groups())
    raise HTTPException(
        status_code=404,
        detail={"error": "not_found", "what": f"没有这个设置项：{key}", "groups": known},
    )


@router.get("/schema")
def schema() -> dict[str, Any]:
    """有哪些设置项、分别是什么、能填什么。**不读配置文件。**

    schema 与当前值分开是有意的：补全脚本、文档生成、设置页的骨架都只要
    这一份，而它们的生命周期比某台机器上的配置长得多。``current`` 一律为
    ``null``——值走 ``GET /api/settings``。
    """
    return {
        "version": 1,
        "sections": {path: cls.__name__ for path, cls in section_paths.items()},
        "groups": list(groups()),
        "settings": [_as_json(setting, {}) for setting in all_settings()],
    }


@router.get("")
def current(deps: Deps) -> dict[str, Any]:
    """现在是什么值，以及配置文件在哪。

    ``redacted`` 这个键名不是为了好看：它在告诉前端「这里面有被替换过的值」，
    于是页面不会把 ``***`` 当成用户真的填了三个星号。
    """
    config: Config = deps.config
    #: 摊平的时候**只摊一次**，而且带脱敏。非脱敏的那份曾经在这里另摊一次，
    #: 结果是密钥的真实值会先进这个进程的内存、再靠下一行 ``bool()`` 丢掉——
    #: 而 ``bool("***")`` 与 ``bool("hunter2")`` 都是 ``True``，也就是说
    #: 读数这一步根本不需要真值。少一次摊平，也就少一条泄漏路径。
    flat = _flatten(config)

    values: dict[str, Any] = {}
    for setting in all_settings():
        if setting.kind is SettingKind.SECRET:
            # 只回「设了没有」。真实值连掩码都不要回：掩码本身会泄漏长度，
            # 而「设了没有」已经够页面决定该显示什么。
            values[setting.key] = {"set": bool(flat.get(setting.key))}
            continue
        values[setting.key] = flat.get(setting.key)

    return {
        "source": str(config.source) if config.source is not None else None,
        "values": values,
        "unannotated": _unannotated(config),
    }


@router.post("")
def write(deps: Deps, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """改一项，原子地写回，返回**重新加载后**的新值。

    返回新值而不是回显用户输入：``"8080"`` 会变成 ``8080``，``"true"`` 会变成
    ``True``，而用户想知道的是「它现在真的是什么」。回显输入会让一个没生效的
    改动看起来也成功了。
    """
    key = str(payload.get("key", "")).strip()
    if not key:
        raise bad_request("没有说要改哪一项")
    raw = str(payload.get("value", ""))
    if len(raw) > MAX_VALUE:
        raise bad_request(f"一个值最多 {MAX_VALUE} 个字符")

    setting = _require_setting(key)
    if setting.kind is SettingKind.SECRET:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "secret_not_writable",
                "what": f"{key} 是密钥，不能从这里改",
                "hint": (
                    "在 config/alterego.toml 里把它写成 ${环境变量名}，"
                    "然后在运行 alterego 的那个环境里设好它。"
                    "能在页面上填的密钥，迟早会被写进某份被提交的文件里。"
                ),
            },
        )

    config: Config = deps.config
    if config.source is None:
        raise unavailable(
            "还没有配置文件，没有地方可以写",
            hint="先跑 alterego init 生成 config/alterego.toml。",
        )

    before = _flatten(config).get(key)
    try:
        updated = save_setting(
            Path(config.source),
            key,
            raw,
            setting=setting,
            backup=config.settings.auto_backup,
        )
    except ConfigError as exc:
        # 校验失败是**用户输了东西**，不是服务器出错：400 加上它自己那句话
        # （ConfigError 的 context 里通常已经带了一条 hint，写着该怎么填）。
        # 用 ``exc.message`` 而不是 ``str(exc)``：后者会把上下文按 repr 塞进
        # 括号里，于是同一句话在页面显示两遍，第二遍还带一层转义。
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_value",
                "what": exc.message,
                "hint": exc.context.get("hint"),
            },
        ) from exc

    after = _flatten(updated).get(key)
    return {
        "key": key,
        "before": before,
        "after": after,
        "source": str(config.source),
        "backup": f"{config.source}.bak" if config.settings.auto_backup else None,
        "requires_restart": setting.requires_restart,
        "restart_needed": [key] if setting.requires_restart and before != after else [],
    }


# ═══════════════════════════════════════════════════════════════════════
#  端点：``[llm.providers.*]``
# ═══════════════════════════════════════════════════════════════════════
#
# **为什么另开两个端点，而不是让上面那个通用接口也能写这一项。**
#
# 通用接口的契约是「一个键 → 一个值」，而这里要写的是「一张表的一部分」。
# 硬塞进去只有两条路：改通用接口的语义（连带影响 CLI 的 ``alterego config set``），
# 或者把整张表序列化成一个字符串——后者注定失败，因为 ``render_value`` 认得
# MAPPING 并会直接报「这一项是一整段表，不能当成一个值来设置」。
#
# **为什么类型要靠猜。** ``llm.providers`` 是 ``Mapping[str, Any]``，里面有哪些键
# 由那个 provider 的插件解释（P4），内核无从声明，所以没有 schema 可查。这里退而
# 求其次：照**文件里现有的那一行**猜类型，猜不出来就老实说「这一行请在文件里改」。
# 页面上的字段名同样来自文件——**不是**一份内核声称「provider 该有哪些键」的清单，
# 那种清单一定会和插件漂移，然后页面开始教用户写一个插件根本不读的键。
#
# 顺序上先读后写：``GET`` 回的 ``kind`` 就是 ``POST`` 收的同一套判定，两边共用
# ``infer_setting_from_value``，所以页面上是数字框的那一行，写回来也不会变成字符串。

#: 段名。读与写都从这一份取，避免某次改动只改了读的一边。
PROVIDER_SECTION: Final[str] = "llm.providers"

#: 端点名与字段名在 TOML 里都是**裸键**（bare key）。点号必须挡掉：
#: ``llm.providers.a.b`` 在 TOML 里是「a 里面有一张叫 b 的表」，不是「一个叫
#: a.b 的端点」——放过去的结果是用户以为加了个端点，实际上加了一层嵌套。
_BARE_KEY: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

#: 一个端点最多几行。用意和 ``MAX_VALUE`` 一样：挡住「把一整份文件贴进来」，
#: 而不是防谁——这些请求都来自本机已经登录的页面。
MAX_FIELDS: Final[int] = 50


def _field_row(section: Setting, name: str, field: str, value: Any) -> dict[str, Any]:
    """一行的样子：值、它是什么类型、能不能在页面上改。

    ``editable`` 为假有两种情况，两种都要变成灰字：

    - 这一行不是标量（多半是一张嵌套的表）。把它渲染成文本框，用户改完一保存，
      写进去的是一个字符串，然后 provider 在运行时报一个和「刚才改的那一下」
      看起来毫无关系的错。
    - 字段名看起来是密钥（``_SECRET_KEY_PATTERN``）。这一层猜的是**别人的**表，
      而 provider 插件允许把 ``api_key`` 直接写在文件里——回显它就等于把密钥送进
      浏览器，和 ``GET /api/settings`` 的规矩对不上。所以只回「设了没有」。
      （以 ``_env`` 结尾的键不算：``api_key_env`` 的值是一个变量名，而页面应该
      让人看见、也应该让人改它——那个名字写错了，启动时只是安静地拿着空密钥去请求。）
    """
    key = f"{PROVIDER_SECTION}.{name}.{field}"
    setting = infer_setting_from_value(value, key=key, label=field, group=section.group)
    secret = setting is not None and setting.kind is SettingKind.SECRET
    return {
        "name": field,
        "key": key,
        "value": {"set": bool(value)} if secret else value,
        "kind": setting.kind.value if setting is not None else "",
        "editable": setting is not None and not secret,
    }


def _writable_setting(
    section: Setting, current: Mapping[str, Any], name: str, field: str
) -> Setting:
    """写这一行该用哪种类型，以及能不能写。

    规则只有一条：**已经在文件里的行照它现在的类型，新加的行就是字符串。**

    新加的行只能是字符串，是因为没有别的东西可依据——字段名归 provider 的插件
    解释（P4），这一层并不知道「model 该是字符串还是一个数组」。宁可写成字符串
    让 provider 自己报错，也不要在这里编一套只有它自己认的类型表。

    Raises:
        HTTPException: 这一行是一张嵌套的表，或者看起来是密钥（名字里带
            ``api_key`` / ``token`` / ``secret``）。两种都是「这一页改不了它」：
            前者会把一张表写成一个字符串，后者会把密钥落进一份用户可能提交的文件。
    """
    key = f"{PROVIDER_SECTION}.{name}.{field}"
    if field in current:
        setting = infer_setting_from_value(
            current[field], key=key, label=field, group=section.group
        )
        if setting is None:
            raise bad_request(
                f"{name} 的 {field} 是一张嵌套的表，这一页改不了它",
                hint=f"在 config/alterego.toml 里直接改 [{PROVIDER_SECTION}.{name}] 下的这一行。",
            )
        if setting.kind is SettingKind.SECRET:
            raise bad_request(
                f"{name} 的 {field} 看起来是密钥，这一页改不了它",
                hint=(
                    "密钥放环境变量，文件里只写变量的名字"
                    "（provider 认的键通常是 api_key_env）；改这里不等于换了密钥。"
                ),
            )
        return setting
    return Setting(
        key=key,
        label=field,
        description="",
        kind=SettingKind.STR,
        default="",
        group=section.group,
        advanced=True,
    )


@router.get("/providers")
def providers(deps: Deps) -> dict[str, Any]:
    """每个模型端点，逐行摊开，供设置页直接改。

    回的是「文件里现在有什么」，而不是「内核知道什么」——内核不认识这里的任何一个
    键（P4）。所以每一行的类型也是从**现有值**猜出来的，猜不出来就 ``editable: false``。

    这里读到的是 ``api_key_env``，也就是环境变量的**名字**，不是密钥本身；密钥一直
    只在运行它的那个环境里，页面从头到尾碰不到它。
    """
    config: Config = deps.config
    section = _require_setting(PROVIDER_SECTION)
    #: 文件里出现过的所有字段名，去重后保持出现顺序。给「加一个端点」的表单做补全。
    known: dict[str, None] = {}
    items: list[dict[str, Any]] = []
    for name, spec in sorted(config.llm.providers.items()):
        rows = (
            [_field_row(section, name, field, value) for field, value in spec.items()]
            if isinstance(spec, Mapping)
            else []
        )
        for row in rows:
            known.setdefault(str(row["name"]), None)
        items.append(
            {
                "name": name,
                "fields": rows,
                # 值不是一张表（手写出来的 ``providers.x = "..."``）。页面照样要把它
                # 显示出来，否则用户会觉得「我明明配了，页面却没看见」。
                "editable": isinstance(spec, Mapping),
                "value": None if isinstance(spec, Mapping) else spec,
            }
        )
    return {
        "key": PROVIDER_SECTION,
        "group": section.group,
        "providers": items,
        "known_fields": list(known),
        "writable": config.source is not None,
        "source": str(config.source) if config.source is not None else None,
    }


@router.post("/providers")
def write_provider(deps: Deps, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """新增或修改一个端点，把这张表里的几行**一次**写回。

    **一次写多行、一次落盘。** 一张表单里的几行在用户眼里本来就是一件事；
    分成几次落盘，中间失败一次就会留下一个改了一半的端点。

    **这里没有删除。** 删掉一个还被 ``[llm.routing]`` 指着的端点，下次 ``serve``
    直接起不来，而文本补丁删行删段做不到回滚（备份只留一份 ``.bak``，连着撤两步
    就撤不回去了）。要删就在文件里删——那里顺手改 ``[llm.routing]``，本来也是
    同一件事。

    响应里的 ``added`` 是**这次新写进去的行名**。它的用处是拦住拼错的字段名：
    ``llm.providers`` 里的键由 provider 的插件解释，写错了不会有任何提示，只会
    静默地读不到。页面据此提醒用户「这是新加的一行」。
    """
    config: Config = deps.config
    if config.source is None:
        raise unavailable(
            "还没有配置文件，没有地方可以写",
            hint="先跑 alterego init 生成 config/alterego.toml。",
        )
    section = _require_setting(PROVIDER_SECTION)

    name = str(payload.get("name", "")).strip()
    if not _BARE_KEY.fullmatch(name):
        raise bad_request(
            f"端点的名字用不了：{name!r}",
            hint=(
                "名字会成为 [llm.providers.<名字>] 这一段，所以只能是字母、数字、"
                "下划线、连字符，而且不能以数字开头。"
            ),
        )
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise bad_request("这个端点一行要写的东西都没有")
    if len(fields) > MAX_FIELDS:
        raise bad_request(f"一个端点最多 {MAX_FIELDS} 行")

    spec = config.llm.providers.get(name)
    before: dict[str, Any] = dict(spec) if isinstance(spec, Mapping) else {}
    edits: list[tuple[str, str, Setting]] = []
    for field, raw in fields.items():
        field_name = str(field).strip()
        if not _BARE_KEY.fullmatch(field_name):
            raise bad_request(
                f"字段名用不了：{field_name!r}",
                hint="字段名写在等号左边，只能是字母、数字、下划线、连字符。",
            )
        raw_text = "" if raw is None else str(raw)
        if len(raw_text) > MAX_VALUE:
            raise bad_request(f"一个值最多 {MAX_VALUE} 个字符")
        edits.append(
            (
                f"{PROVIDER_SECTION}.{name}.{field_name}",
                raw_text,
                _writable_setting(section, before, name, field_name),
            )
        )

    try:
        updated = save_settings(Path(config.source), edits, backup=config.settings.auto_backup)
    except ConfigError as exc:
        # 同 ``write``：校验失败是用户输了东西，不是服务器出错。
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_value",
                "what": exc.message,
                "hint": exc.context.get("hint"),
            },
        ) from exc

    after_spec = updated.llm.providers.get(name)
    after: dict[str, Any] = dict(after_spec) if isinstance(after_spec, Mapping) else {}
    return {
        "key": f"{PROVIDER_SECTION}.{name}",
        "name": name,
        "created": not before,
        "before": before,
        "after": after,
        "added": [
            key.rpartition(".")[2] for key, _, _ in edits if key.rpartition(".")[2] not in before
        ],
        "source": str(config.source),
        "backup": f"{config.source}.bak" if config.settings.auto_backup else None,
        "requires_restart": section.requires_restart,
        "restart_needed": (
            [PROVIDER_SECTION] if section.requires_restart and after != before else []
        ),
    }

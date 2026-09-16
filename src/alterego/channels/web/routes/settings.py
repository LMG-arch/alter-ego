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

from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, HTTPException

from alterego.channels.web.routes.base import Deps, bad_request, unavailable
from alterego.cli_config import _as_json, _flatten, _unannotated
from alterego.kernel.config import Config
from alterego.kernel.errors import ConfigError
from alterego.kernel.settings import Setting, SettingKind
from alterego.kernel.settings_catalog import all_settings, groups, section_paths
from alterego.kernel.settings_write import save_setting


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

"""``/api/plugins`` —— 后台页的插件部分。

**这一页的加载状态是真的，不是另起一个进程算出来的。** ``alterego plugins list``
每次都开一个新进程、装一遍插件、退出——所以那边看到的熔断计数永远是零
（``cmd_plugins_reset`` 的注释里写着这件事）。``serve`` 是常驻进程，
它持有的那台 ``PluginManager`` 就是插件真正跑着的那台，所以这里报的状态、
健康检查、熔断计数都是**当下真的**。这也是为什么重载在这里才有意义：
下一次 tick 就会用到重载后的那个实例。

**没有配置的插件要能看出来。** 插件的 ``[config.<键>]`` 走
``manifest.redacted_config()``——``secret = true`` 的字段不会外传，
这一点与设置页同一套规矩：浏览器永远拿不到密钥，连掩码后的值都拿不到。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from alterego.channels.web.routes.base import Deps, not_found, require, unavailable
from alterego.kernel.manifest import is_api_version_compatible


__all__ = ["router"]

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


def _plugin_row(
    found: Any,
    *,
    health: Any,
    enabled: bool,
    loaded: bool,
    status: Any,
) -> dict[str, Any]:
    """一个插件摊成一行。

    ``health`` / ``enabled`` / ``loaded`` 由调用方先算好再传进来：
    ``health_all()`` 会去问每一个插件（可能碰到网络），逐个插件各调一次
    就是 N² 次。这种错误在只有一个插件时看不出来，装到第五个就开始卡。
    """
    manifest = found.manifest
    return {
        "id": manifest.id,
        "name": manifest.display_name,
        "kind": manifest.kind,
        "version": manifest.version,
        "api_version": manifest.api_version,
        "compatible": is_api_version_compatible(manifest.api_version),
        "source": manifest.source,
        "description": manifest.description,
        "authors": list(manifest.authors),
        "tags": list(manifest.tags),
        "provides": list(manifest.provides),
        "requires": list(manifest.requires),
        "enabled": enabled,
        "loaded": loaded,
        "status": str(status) if status is not None else "",
        "health": (
            {"ok": health.ok, "detail": health.detail, "hint": health.hint}
            if health is not None
            else None
        ),
    }


@router.get("")
def plugins(deps: Deps) -> dict[str, Any]:
    """发现到了哪些插件、哪些装起来了、都还好吗。

    ``failed`` 单列一节：清单读不出来（TOML 写错、缺字段）的目录不会出现在
    插件表里，只打表会让人以为那个目录被忽略了——而那正是要修的东西。
    """
    manager = require(
        deps.manager,
        "插件管理器",
        hint="serve 起的时候没能装上插件管理器；跑 alterego plugins doctor 看是哪一步。",
    )
    result = manager.discover()
    health = manager.health_all()
    enabled = set(manager.enabled_ids())
    loaded = set(manager.order)
    items = [
        _plugin_row(
            result.plugins[plugin_id],
            health=health.get(plugin_id),
            enabled=plugin_id in enabled,
            loaded=plugin_id in loaded,
            status=manager.status_of(plugin_id),
        )
        for plugin_id in result.ids()
    ]
    return {
        "items": items,
        "loaded": list(manager.order),
        "failed": [{"path": str(path), "error": str(error)} for path, error in result.failed],
        "search_paths": [str(path) for path in deps.config.plugin_search_paths],
    }


@router.post("/{plugin_id}/reload")
def reload(deps: Deps, plugin_id: str) -> dict[str, Any]:
    """把它卸掉再装一次。

    失败**不恢复旧实例**（``02-plugin-api.md`` § 10.3）：旧实例可能已经是
    半死的，请回来只会让问题更难查。所以失败时返回 ``ok: false`` 与它现在的
    状态，而不是 500——「重载失败了，它现在是停用状态」是一个可用的回答。
    """
    manager = require(deps.manager, "插件管理器")
    found = manager.discover().get(plugin_id)
    if found is None:
        raise not_found(f"没有发现叫 {plugin_id} 的插件")
    if plugin_id not in manager.order:
        raise unavailable(
            f"{plugin_id} 没有装起来，无法重载",
            hint="它大概是没被启用：在设置页的 plugins.enabled 里加上它，或者看 plugins info。",
        )
    ok = manager.reload(plugin_id)
    status = manager.status_of(plugin_id)
    return {"id": plugin_id, "ok": ok, "status": str(status) if status is not None else ""}

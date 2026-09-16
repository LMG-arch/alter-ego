"""``alterego serve`` 那一层的接线。

**这一份测的是「它怎么组装」，不是「HTTP 答得对不对」。** 上面两层已经分别
测过路由与认证门；这里要问的是组装根自己的账：

1. ``--host`` / ``--port`` 是**盖在** ``[web]`` 上，而不是另造一个配置——
   另造就等于把 ``WebConfig.__post_init__`` 那三条检查关掉；
2. ``--port`` 敲错在 argparse 那一步就退 2，而不是装到一半才抛 ``ConfigError``；
3. ``0.0.0.0`` 不能原样打给用户看，那不是一条能打开的链接；
4. **已经存在的 token 不重复打印**——``serve`` 的 stdout 经常被重定向进日志
   文件，而日志会被贴出来。

第 4 条是这里最重要的一条：它看起来像个排版问题，实际上是一条泄漏路径。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from alterego import cli_serve
from alterego.channels.web.auth import TOKEN_FILENAME, AuthGate
from alterego.cli import build_parser, main
from alterego.kernel.config import Config, ConfigError, WebConfig


TOKEN = "sesame"


def _config(tmp_path: Path, **web: Any) -> Config:
    section: dict[str, Any] = {"host": "127.0.0.1", "port": 8765}
    section.update(web)
    return Config.load(
        env={},
        overrides={"core": {"data_dir": str(tmp_path / "data")}, "web": section},
    )


# ═══════════════════════════════════════════════════════════════════════
#  命令行参数
# ═══════════════════════════════════════════════════════════════════════


def test_serve_has_no_subcommands_of_its_own() -> None:
    """``serve`` 本身就是一个动作，和 ``chat`` 一样不多一层。"""
    args = build_parser().parse_args(["serve"])

    assert args.handler is cli_serve.cmd_serve
    assert args.host is None
    assert args.port is None
    assert args.no_web is False
    assert args.persona is None


def test_serve_flags_land_where_they_should() -> None:
    args = build_parser().parse_args(
        ["serve", "--host", "0.0.0.0", "--port", "9000", "--no-web", "--persona", "林晚"]
    )

    assert (args.host, args.port, args.no_web, args.persona) == (
        "0.0.0.0",
        9000,
        True,
        "林晚",
    )


def test_a_bad_port_is_a_usage_error_not_a_crash() -> None:
    """端口敲错了是「命令写错了」，该看到用法提示并退 2。"""
    parser = build_parser()

    for text in ("0", "65536", "abc", ""):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["serve", "--port", text])
        assert caught.value.code == 2


def test_a_port_that_is_not_a_number_says_so() -> None:
    """argparse 默认那句「invalid _port value」不认识我们的函数名。"""
    with pytest.raises(argparse.ArgumentTypeError):
        cli_serve._port("1.5")

    assert cli_serve._port("1") == 1
    assert cli_serve._port("65535") == 65535


# ═══════════════════════════════════════════════════════════════════════
#  --host / --port 盖在 [web] 上
# ═══════════════════════════════════════════════════════════════════════


def test_no_flags_means_the_very_same_object() -> None:
    """没给参数就原样返回：换一个新对象会让 ``deps.config.web`` 与它不再是同一个。"""
    web = WebConfig()

    assert cli_serve._web_config(web, host=None, port=None) is web


def test_the_flags_only_overwrite_what_they_say() -> None:
    web = WebConfig(host="127.0.0.1", port=8765, page_size=42)

    patched = cli_serve._web_config(web, host="192.168.1.9", port=None)

    assert patched.host == "192.168.1.9"
    assert patched.port == 8765
    assert patched.page_size == 42


def test_listening_on_every_interface_without_auth_is_refused_here() -> None:
    """这条检查要是被绕过，症状是「服务对着整个局域网敞着，而启动日志一切正常」。"""
    with pytest.raises(ConfigError):
        cli_serve._web_config(WebConfig(auth="none"), host="0.0.0.0", port=None)


def test_reading_every_interface_with_a_password_is_allowed() -> None:
    patched = cli_serve._web_config(
        WebConfig(auth="password", auth_password="hunter2"), host="0.0.0.0", port=None
    )

    assert patched.host == "0.0.0.0"


def test_a_port_outside_the_range_is_refused_at_construction() -> None:
    with pytest.raises(ConfigError):
        cli_serve._web_config(WebConfig(), host=None, port=70000)


# ═══════════════════════════════════════════════════════════════════════
#  那几行开场白
# ═══════════════════════════════════════════════════════════════════════


def test_wildcard_hosts_are_shown_as_something_you_can_open(tmp_path: Path) -> None:
    """``0.0.0.0`` 不是地址而是「所有网卡」；照原样打出来会让人点开一个连不上的链接。"""
    assert cli_serve._url(_config(tmp_path, host="0.0.0.0")) == "http://127.0.0.1:8765/"
    assert cli_serve._url(_config(tmp_path, host="::")) == "http://127.0.0.1:8765/"
    assert cli_serve._url(_config(tmp_path, host="localhost")) == "http://localhost:8765/"


def test_every_auth_mode_says_how_to_get_in(tmp_path: Path) -> None:
    config = _config(tmp_path)

    off = cli_serve._login_line(config, AuthGate(mode="none"), fresh_token=False)
    assert "没开认证" in off

    password = cli_serve._login_line(
        config, AuthGate(mode="password", password=TOKEN), fresh_token=False
    )
    assert "auth_password" in password


def test_a_fresh_token_is_printed_because_nothing_else_shows_it(tmp_path: Path) -> None:
    line = cli_serve._login_line(
        _config(tmp_path), AuthGate(mode="token", token=TOKEN), fresh_token=True
    )

    assert TOKEN in line
    assert "?token=" in line


def test_an_existing_token_is_not_printed_again(tmp_path: Path) -> None:
    """``serve`` 的输出常被重定向进日志，而日志会被贴出来。"""
    line = cli_serve._login_line(
        _config(tmp_path), AuthGate(mode="token", token=TOKEN), fresh_token=False
    )

    assert TOKEN not in line
    assert TOKEN_FILENAME in line


def test_the_banner_prints_the_persona_and_the_config_source(tmp_path: Path, capsys: Any) -> None:
    """起服务之前先打这几行：出问题时它们是唯一的线索。"""
    from alterego.interfaces.repository import PersonaRecord

    config = _config(tmp_path)
    cli_serve._banner(
        config,
        PersonaRecord(id="p1", name="林晚", occupation="算法工程师"),
        AuthGate(mode="none"),
        "c1",
        fresh_token=False,
    )

    text = capsys.readouterr().out
    assert "林晚" in text
    assert "algorithm" not in text.lower()
    assert "c1" in text
    assert "内置默认值" in text


# ═══════════════════════════════════════════════════════════════════════
#  从命令行走到装配
# ═══════════════════════════════════════════════════════════════════════


@contextmanager
def _fake_db() -> Iterator[Any]:
    """一个占位的「库」。这一刻要验的是接线，不是 SQL。"""
    yield object()


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seen: dict[str, Any]) -> None:
    """把组装根下面的每一层都换掉，只留下它自己那点判断。"""
    monkeypatch.setattr(cli_serve, "_serve_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli_serve, "_require_sqlite", lambda config: None)
    monkeypatch.setattr(cli_serve, "_providers", lambda config: {})
    monkeypatch.setattr(cli_serve, "_open_db", lambda config: _fake_db())

    async def fake_serve(
        config: Config,
        backend: Any,
        providers: Any,
        *,
        no_web: bool,
        persona_name: str | None,
    ) -> int:
        seen["no_web"] = no_web
        seen["persona_name"] = persona_name
        return 0

    monkeypatch.setattr(cli_serve, "_serve", fake_serve)


def test_run_reaches_the_assembly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    _patch_pipeline(monkeypatch, tmp_path, seen)

    code = main(["serve", "--no-web", "--persona", "林晚"])

    assert code == 0
    assert seen == {"no_web": True, "persona_name": "林晚"}


def test_run_warns_when_the_config_says_no_web(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """配置里关掉了界面而命令行没提这件事——要主动说一句，不然像卡住了。"""
    seen: dict[str, Any] = {}
    _patch_pipeline(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(cli_serve, "_serve_config", lambda: _config(tmp_path, enabled=False))

    assert main(["serve"]) == 0
    assert "enabled = false" in capsys.readouterr().err


def test_run_turns_a_bad_host_into_a_readable_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """非法组合要变成一句中文加一个退出码，而不是一条 traceback。"""
    seen: dict[str, Any] = {}
    _patch_pipeline(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(cli_serve, "_serve_config", lambda: _config(tmp_path, auth="none"))

    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert seen == {}
    assert "错误" in capsys.readouterr().err


def test_a_missing_database_is_reported_not_traced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    from alterego.kernel.errors import ConfigError as _ConfigError

    def boom(config: Config) -> None:
        raise _ConfigError("找不到数据库", hint="先跑 alterego init。")

    monkeypatch.setattr(cli_serve, "_serve_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli_serve, "_require_sqlite", boom)

    assert main(["serve"]) == 2
    assert "找不到数据库" in capsys.readouterr().err


def test_the_shutdown_order_is_plugins_then_channel_then_providers() -> None:
    """顺序错了不会报错，只会少一句「我先下去了」。"""
    order: list[str] = []

    class _Manager:
        def shutdown(self) -> None:
            order.append("plugins")

    class _Channel:
        async def aclose(self) -> None:
            order.append("channel")

    class _Provider:
        async def aclose(self) -> None:
            order.append("provider")

    deps = type("Deps", (), {"channel": _Channel()})()
    asyncio.run(cli_serve._shutdown(deps, _Manager(), {"main": _Provider()}))  # type: ignore[arg-type]

    assert order == ["plugins", "channel", "provider"]

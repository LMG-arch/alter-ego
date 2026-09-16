"""``channels.web.auth`` 的测试：token 落盘、常数时间比较、三种模式。

认证是那种「测试里绕过去就等于没测」的东西，所以这里一条 HTTP 都不起：
``AuthGate`` 只回答「这个凭证对不对」，而它与框架无关（``gate_for`` 的
docstring 里解释了为什么刻意不 import fastapi）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alterego.channels.web.auth import (
    TOKEN_FILENAME,
    AuthGate,
    ensure_token,
    gate_for,
    token_path,
)
from alterego.kernel.config import WebConfig
from alterego.kernel.errors import WebAuthError


# ── token 文件 ──────────────────────────────────────────────


class TestTokenFile:
    def test_the_token_lives_at_the_project_root(self, tmp_path: Path) -> None:
        """放项目根而不是 ``data/``：data 会被备份、会被同步，而 token 只是这台机器上的钥匙。"""
        assert token_path(tmp_path) == tmp_path / TOKEN_FILENAME

    def test_a_missing_token_is_generated_and_written(self, tmp_path: Path) -> None:
        path = token_path(tmp_path)

        token = ensure_token(path)

        assert token
        assert path.read_text(encoding="utf-8").strip() == token

    def test_asking_twice_gives_the_same_token(self, tmp_path: Path) -> None:
        """第二次启动必须复用同一个串，否则每个已经登录的浏览器都要重新贴一次。"""
        path = token_path(tmp_path)

        first = ensure_token(path)
        second = ensure_token(path)

        assert first == second

    def test_a_hand_written_token_is_kept(self, tmp_path: Path) -> None:
        """文件里已经有值（用户自己写的）就不覆盖。"""
        path = token_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("我自己的串\n", encoding="utf-8")

        assert ensure_token(path) == "我自己的串"

    def test_a_blank_file_counts_as_missing(self, tmp_path: Path) -> None:
        path = token_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n  \n", encoding="utf-8")

        token = ensure_token(path)

        assert token.strip() == token
        assert token

    def test_tokens_do_not_repeat(self, tmp_path: Path) -> None:
        """每一次生成都必须是新的随机串——重复的 token 等于两台机器共用一把钥匙。"""
        tokens = {ensure_token(tmp_path / f"root-{index}") for index in range(5)}

        assert len(tokens) == 5

    @pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 只影响只读位")
    def test_the_file_is_not_readable_by_others(self, tmp_path: Path) -> None:
        """在 POSIX 上这个文件只对本人可读。

        Windows 上跳过而不是断言点什么：那边的 ``chmod`` 只动只读位，
        写一条总会通过的断言等于假装验过了，而它要说明的是「默认可见范围」。
        """
        path = token_path(tmp_path)

        ensure_token(path)

        assert path.stat().st_mode & 0o077 == 0


# ── 校验 ────────────────────────────────────────────────────


class TestAuthGate:
    def test_token_mode_accepts_only_that_token(self) -> None:
        gate = AuthGate(mode="token", token="abc")

        assert gate.required is True
        assert gate.check("abc") is True
        assert gate.check("abd") is False
        assert gate.check("") is False
        assert gate.check(None) is False

    def test_password_mode_checks_the_password_not_the_token(self) -> None:
        gate = AuthGate(mode="password", token="abc", password="hunter2")

        assert gate.check("hunter2") is True
        assert gate.check("abc") is False

    def test_no_mode_lets_everything_through(self) -> None:
        """``mode="none"`` 永远通过——配置层已经保证它只可能出现在监听本机的场景里。"""
        gate = AuthGate(mode="none")

        assert gate.required is False
        assert gate.check(None) is True

    def test_an_empty_expectation_never_matches(self) -> None:
        """期望值是空串时谁都进不去。

        反过来（空就是通过）是一个**静默的开门**：token 文件被清空之后，
        服务看起来还好好的，只是不再需要认证了。
        """
        assert AuthGate(mode="token", token="").check("") is False
        assert AuthGate(mode="password", password="").check("x") is False

    def test_verify_raises_with_a_way_to_get_the_credential(self) -> None:
        """报错要给「去哪儿拿凭证」，不是「密码错误」——后者不会让人知道下一步做什么。"""
        gate = AuthGate(mode="token", token="abc")

        with pytest.raises(WebAuthError) as caught:
            gate.verify("wrong")

        assert "?token=" in caught.value.context["hint"]

    def test_verify_passes_silently_when_authentication_is_off(self) -> None:
        AuthGate(mode="none").verify(None)

    def test_the_cookie_name_is_stable(self) -> None:
        assert AuthGate().cookie_name == AuthGate(mode="password").cookie_name


# ── 按配置造表 ──────────────────────────────────────────────


class TestGateFor:
    def test_none_mode_needs_no_project_root(self) -> None:
        gate = gate_for(WebConfig(auth="none", host="127.0.0.1"))

        assert gate.mode == "none"
        assert gate.token == ""

    def test_password_mode_takes_the_value_from_config(self) -> None:
        gate = gate_for(WebConfig(auth="password", auth_password="s3cret"))

        assert gate.mode == "password"
        assert gate.check("s3cret") is True

    def test_token_mode_generates_the_file_under_the_project_root(self, tmp_path: Path) -> None:
        gate = gate_for(WebConfig(auth="token"), project_root=tmp_path)

        assert gate.mode == "token"
        assert gate.token == token_path(tmp_path).read_text(encoding="utf-8").strip()

    def test_token_mode_without_a_project_root_is_an_error(self) -> None:
        """不然它只能去猜项目根在哪，而猜错的后果是把 token 写到一个会进 git 的地方。"""
        with pytest.raises(WebAuthError) as caught:
            gate_for(WebConfig(auth="token"))

        assert "组装根" in caught.value.context["hint"]

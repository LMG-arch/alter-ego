"""``alterego.cli`` 的测试。

CLI 是用户唯一真正会碰的界面，所以哪怕是「只打印帮助」的当前状态也值得测：
``--version`` 的输出格式、退出码、以及「不认识子命令时不该静默成功」。

用 ``capsys`` 抓标准输出，不 mock ``argparse``——参数解析的行为本身就是要验证的东西。
"""

from __future__ import annotations

import pytest

from alterego import __version__
from alterego.cli import build_parser, main


class TestBuildParser:
    def test_prog_name_is_alterego(self) -> None:
        assert build_parser().prog == "alterego"

    def test_version_action_prints_the_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"alterego {__version__}"

    def test_help_mentions_the_project(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--help"])
        assert exc.value.code == 0
        assert "AlterEgo" in capsys.readouterr().out


class TestMain:
    def test_no_arguments_returns_zero_and_prints_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "usage: alterego" in out
        assert "AlterEgo" in out

    def test_unknown_argument_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 还没有子命令，所以任何多余参数都该是错误（退出码 2），而不是被忽略。
        with pytest.raises(SystemExit) as exc:
            main(["nonsense"])
        assert exc.value.code == 2
        assert "usage: alterego" in capsys.readouterr().err

    def test_explicit_none_falls_back_to_sys_argv(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.argv", ["alterego"])
        assert main() == 0
        assert "usage: alterego" in capsys.readouterr().out

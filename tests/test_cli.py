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


# ────────────────────────────────────────────────────────────
# calendar · 这一组读的是随包数据
# ────────────────────────────────────────────────────────────
#
# 这些断言确实绑在 `holidays/2026.toml` 上，年年要跟着改。
# 之所以接受：CLI 是唯一会把「数据文件」和「领域规则」拼起来的地方，
# 而这两半各自都有单元测试（`test_holiday_data.py` / `test_domain_calendar.py`）。
# 这里要证的是**接缝**：读到的数据真的变成了用户看得懂的那几行。

YEAR = 2026


class TestCalendarList:
    def test_every_holiday_is_listed(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["calendar", "list", "--year", str(YEAR)]) == 0
        out = capsys.readouterr().out
        for name in ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"):
            assert name in out

    def test_the_header_says_how_many(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["calendar", "list", "--year", str(YEAR)])
        assert f"{YEAR} 年节日" in capsys.readouterr().out

    def test_a_day_off_is_distinguishable_from_a_celebration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """情人节不放假，但锚点那天仍然落在 `span` 里。

        若用 `length_days` 判断，这一行会写成「放假 02-14」——数据没错，
        错在展示层把「兜底值」当成了「真实值」。
        """
        main(["calendar", "list", "--year", str(YEAR)])
        out = capsys.readouterr().out
        assert "情人节" in out
        assert "不放假" in out
        assert "放假 02-14" not in out

    def test_an_unconfirmed_calendar_admits_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["calendar", "list", "--year", str(YEAR)])
        assert "数据未核对" in capsys.readouterr().out

    def test_lead_and_aftermath_are_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["calendar", "list", "--year", str(YEAR)])
        assert "提前" in capsys.readouterr().out

    def test_a_year_without_data_is_not_a_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """缺一年的正确表现是「那一年不知道有什么节」，退出码 2 + 一句人话。"""
        assert main(["calendar", "list", "--year", str(YEAR + 1)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert str(YEAR + 1) in captured.err

    def test_an_absurd_year_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["calendar", "list", "--year", "1800"]) == 2
        assert "年份必须在" in capsys.readouterr().err

    def test_the_year_defaults_to_this_year(self, capsys: pytest.CaptureFixture[str]) -> None:
        """不给 --year 就该按内核时区算「今年」，而不是按进程本地时区。"""
        assert main(["calendar", "list"]) == 0
        assert "年节日" in capsys.readouterr().out


class TestCalendarToday:
    def test_a_plain_day_says_nothing_is_going_on(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["calendar", "today", "--date", "2026-09-15"]) == 0
        out = capsys.readouterr().out
        assert "2026-09-15" in out
        assert "今天没有节日安排" in out

    def test_a_plain_day_still_knows_what_is_coming(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """「提前几天就知道」是需求原文：平常日子也必须看得到节。"""
        main(["calendar", "today", "--date", "2026-09-15"])
        out = capsys.readouterr().out
        assert "未来" in out
        assert "中秋节" in out

    def test_the_day_itself_is_fully_on(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["calendar", "today", "--date", "2026-09-25"])
        out = capsys.readouterr().out
        assert "过节" in out
        assert "中秋节" in out
        assert "1.00" in out

    def test_the_eve_is_almost_but_not_quite_there(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """节前一天强度必须 < 1.0，否则 D-1 和 D0 同档，「过渡」就没了。"""
        main(["calendar", "today", "--date", "2026-09-24"])
        out = capsys.readouterr().out
        assert "节前" in out
        assert "1 天后" in out
        assert "1.00" not in out
        assert "已经开始安排了" in out

    def test_the_aftermath_survives_a_weaker_anticipation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """2026-09-26 的回归测试——本批次最重要的一条。

        这天是中秋次日（余温 0.50），也是国庆的第六天前（刚起步 0.32）。
        按阶段序排序会让国庆胜出，于是「月饼还剩一堆」凭空消失、
        它第二天就开始张罗国庆——正是需求里点名要避免的突然过渡。
        """
        main(["calendar", "today", "--date", "2026-09-26"])
        out = capsys.readouterr().out
        # 只看「今天在哪个节日里」那一段；底下的「未来 N 天」本来就该列国庆。
        block = out.split("未来")[0]
        assert "节后" in block
        assert "中秋节" in block
        assert "国庆节" not in block
        assert "10-01" not in block

    def test_the_quiet_edge_has_a_signal(self, capsys: pytest.CaptureFixture[str]) -> None:
        """节前最外沿那天：强度大于 0（所以「知道」），但还没开始安排。"""
        main(["calendar", "today", "--date", "2026-09-19"])
        out = capsys.readouterr().out
        assert "节前" in out
        assert "6 天后" in out
        assert "还只是知道" in out

    def test_the_window_size_is_configurable(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["calendar", "today", "--date", "2026-09-15", "--days", "3"])
        out = capsys.readouterr().out
        assert "未来 3 天" in out
        assert "中秋节" not in out, "3 天内不该出现 10 天后的中秋"

    def test_a_bad_date_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["calendar", "today", "--date", "下周三"])
        assert exc.value.code == 2
        assert "usage: alterego" in capsys.readouterr().err


class TestCalendarCheck:
    def test_it_names_what_still_needs_verifying(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["calendar", "check", "--year", str(YEAR)]) == 0
        out = capsys.readouterr().out
        assert "待核对" in out
        assert "中秋节" in out, "农历日期算不出来，必须出现在待核对名单里"

    def test_fixed_gregorian_dates_are_not_in_the_backlog(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["calendar", "check", "--year", str(YEAR)])
        out = capsys.readouterr().out
        backlog = out.split("已有数据")[0]
        assert "元旦" not in backlog

    def test_a_year_without_data_says_which_years_exist(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """相邻年份的合并不能让「没有这一年的数据」变成一份错答案。"""
        assert main(["calendar", "check", "--year", str(YEAR + 1)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert str(YEAR + 1) in captured.err
        assert str(YEAR) in captured.err, "得告诉用户有哪些年份可用"

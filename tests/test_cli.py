"""``alterego.cli`` 的测试。

CLI 是用户唯一真正会碰的界面，所以哪怕是「只打印帮助」的当前状态也值得测：
``--version`` 的输出格式、退出码、以及「不认识子命令时不该静默成功」。

用 ``capsys`` 抓标准输出，不 mock ``argparse``——参数解析的行为本身就是要验证的东西。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alterego import __version__
from alterego.birthdays import load_book
from alterego.cli import build_parser, main
from alterego.domain.birthday import DEFAULT_LEAD_DAYS


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


# ────────────────────────────────────────────────────────────
# birthday · 这一组读写的是用户自己的文件
# ────────────────────────────────────────────────────────────
#
# `_birthday_path()` 默认指向 `data/birthdays.toml`，也就是开发机上那份真实数据。
# 不把它换掉的话，这些测试会读开发者的私人文件，而且**会写**——
# 跑一次测试改一次真数据，是最不该有的副作用。
# 所以下面这个 autouse fixture 对整个模块生效：谁都不许碰到真实路径。


@pytest.fixture(autouse=True)
def _isolated_birthday_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把生日文件换成本次测试专属的临时文件（默认不存在）。

    返回路径给用例自己用：需要检查写进去的内容时直接 ``load_book(路径)``。
    """
    path = tmp_path / "birthdays.toml"
    monkeypatch.setattr("alterego.cli._birthday_path", lambda: path)
    return path


def _add(*args: str) -> int:
    """跑一次 ``birthday add``，参数按需给。"""
    return main(["birthday", "add", *args])


class TestBirthdayAdd:
    def test_it_writes_the_file(self, _isolated_birthday_file: Path) -> None:
        assert _add("--who", "user", "--on", "06-03") == 0
        assert _isolated_birthday_file.is_file()

    def test_it_says_what_it_recorded(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "user", "--on", "06-03")
        out = capsys.readouterr().out
        assert "已记下" in out
        assert "06-03" in out

    def test_a_default_name_is_used(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 不写 --name 时也得有个像样的称呼，否则列表里是一列空白。
        _add("--who", "user", "--on", "06-03")
        assert "你" in capsys.readouterr().out

    def test_the_lead_days_follow_the_subject(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "self", "--on", "06-03")
        out = capsys.readouterr().out
        assert str(DEFAULT_LEAD_DAYS["self"]) in out

    def test_a_custom_lead_days_wins(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "user", "--on", "06-03", "--lead-days", "20")
        assert "20" in capsys.readouterr().out

    def test_npc_needs_an_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 没有 npc_id 就没法把这条记录和某个人对上，而「对上」是它以后
        # 主动去准备礼物的前提。
        assert _add("--who", "npc", "--on", "06-03") == 2
        assert "npc_id" in capsys.readouterr().err

    def test_an_npc_is_recorded(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _add("--who", "npc", "--npc-id", "npc-1", "--name", "张三", "--on", "06-03") == 0
        assert "张三" in capsys.readouterr().out

    def test_unverified_is_marked(self, _isolated_birthday_file: Path) -> None:
        _add("--who", "npc", "--npc-id", "npc-1", "--on", "06-03", "--unverified")
        book = load_book(_isolated_birthday_file)
        assert book.birthdays[0].verified is False

    def test_a_duplicate_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "user", "--on", "06-03")
        capsys.readouterr()
        assert _add("--who", "user", "--on", "06-04") == 2
        assert "set" in capsys.readouterr().err

    def test_a_refused_duplicate_changes_nothing(
        self, capsys: pytest.CaptureFixture[str], _isolated_birthday_file: Path
    ) -> None:
        """被挡住了就不能顺手把文件改掉——「报错 + 已经改了」是最坏的一种。"""
        _add("--who", "user", "--on", "06-03")
        _add("--who", "user", "--on", "06-04")
        capsys.readouterr()
        assert load_book(_isolated_birthday_file).birthdays[0].month_day == "06-03"

    def test_a_bad_date_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _add("--who", "user", "--on", "02-30")
        assert exc.value.code == 2
        assert "02-30" in capsys.readouterr().err

    def test_a_bad_format_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _add("--who", "user", "--on", "6月3日")
        assert exc.value.code == 2
        assert "MM-DD" in capsys.readouterr().err

    def test_a_bad_subject_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _add("--who", "邻居", "--on", "06-03")
        assert exc.value.code == 2
        assert "usage: alterego" in capsys.readouterr().err


class TestBirthdaySet:
    def test_setting_an_unknown_one_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 「以为改了、其实是新加了一条」会让用户手上多出一条重复记录，
        # 而重复记录里错的那条照样会在那天生效。
        assert main(["birthday", "set", "--who", "user", "--on", "06-03"]) == 2
        assert "add" in capsys.readouterr().err

    def test_it_changes_the_date(
        self, capsys: pytest.CaptureFixture[str], _isolated_birthday_file: Path
    ) -> None:
        _add("--who", "user", "--on", "06-03")
        capsys.readouterr()
        assert main(["birthday", "set", "--who", "user", "--on", "06-04"]) == 0
        assert "已改" in capsys.readouterr().out
        assert load_book(_isolated_birthday_file).birthdays[0].month_day == "06-04"

    def test_it_keeps_the_name_and_lead_days(self, _isolated_birthday_file: Path) -> None:
        """`set --on` 只该改一天。

        顺手把称呼改回「你」、把调过的提前量改回默认值，是一次操作改了四样东西——
        而用户看不到另外三样被改了。
        """
        _add(
            "--who",
            "npc",
            "--npc-id",
            "npc-1",
            "--name",
            "张三",
            "--on",
            "06-03",
            "--lead-days",
            "20",
        )
        main(["birthday", "set", "--who", "npc", "--npc-id", "npc-1", "--on", "06-04"])
        birthday = load_book(_isolated_birthday_file).birthdays[0]
        assert birthday.month_day == "06-04"
        assert birthday.name == "张三"
        assert birthday.lead_days == 20

    def test_it_does_not_add_a_second_record(self, _isolated_birthday_file: Path) -> None:
        _add("--who", "user", "--on", "06-03")
        main(["birthday", "set", "--who", "user", "--on", "06-04"])
        assert len(load_book(_isolated_birthday_file)) == 1


class TestBirthdayList:
    def test_an_empty_book_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["birthday", "list"]) == 0
        out = capsys.readouterr().out
        assert "一条都还没有" in out
        assert "birthday add" in out

    def test_the_header_shows_the_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 用户改的是文件，不是数据库。不给路径的话下一个问题必然是「它在哪」。
        main(["birthday", "list"])
        assert "birthdays.toml" in capsys.readouterr().out

    def test_it_lists_what_was_added(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        main(["birthday", "list"])
        out = capsys.readouterr().out
        assert "小明" in out
        assert "06-03" in out

    def test_an_unverified_date_is_flagged(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "npc", "--npc-id", "npc-1", "--name", "张三", "--on", "06-03", "--unverified")
        capsys.readouterr()
        main(["birthday", "list"])
        assert "未确认" in capsys.readouterr().out

    def test_it_reminds_you_about_itself_and_you(self, capsys: pytest.CaptureFixture[str]) -> None:
        """记了别人的生日不等于它自己会过生日。

        「还要会过生日」这句话最直接的意思就是它自己也要过——所以「自己」和「用户」
        两条缺了必须说出来，否则用户看到一列生日会以为齐了。
        """
        _add("--who", "npc", "--npc-id", "npc-1", "--on", "06-03")
        capsys.readouterr()
        main(["birthday", "list"])
        out = capsys.readouterr().out
        assert "还没记" in out
        assert "--who self" in out

    def test_it_stops_reminding_once_both_are_in(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "self", "--on", "01-01")
        _add("--who", "user", "--on", "06-03")
        capsys.readouterr()
        main(["birthday", "list"])
        assert "还没记" not in capsys.readouterr().out

    def test_the_window_hides_the_far_away(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """窗口之外的不列。

        「今天」得钉住：拿真实时钟当参照的话，这个用例会在 12-30 那天失败。
        """
        monkeypatch.setattr("alterego.cli._today", lambda: date(2026, 6, 1))
        _add("--who", "user", "--on", "12-31")
        capsys.readouterr()
        assert main(["birthday", "list", "--days", "1"]) == 0
        assert "12-31" not in capsys.readouterr().out


class TestCalendarWithBirthdays:
    """生日并进节日日历之后，`calendar` 那几条命令也得认它。

    这一组是「同一个机制」的证据：生日不是另一套流程，
    它只是又一个 `personal` 种类的 `Holiday`。
    """

    def test_the_list_counts_birthdays_separately(self, capsys: pytest.CaptureFixture[str]) -> None:
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "list", "--year", str(YEAR)]) == 0
        out = capsys.readouterr().out
        assert "含 1 个生日" in out
        assert "小明" in out

    def test_a_birthday_does_not_make_the_day_a_holiday(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """生日不放假。那天原本是星期几就还是星期几。

        2026-06-03 是星期三。若 ``days_off`` 被当成「这天归我」，
        这一行会写成「放假」——而它什么都不该改。
        """
        _add("--who", "self", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "today", "--date", f"{YEAR}-06-03"]) == 0
        first = capsys.readouterr().out.splitlines()[0]
        assert "工作日" in first

    def test_the_day_itself_is_the_birthday(self, capsys: pytest.CaptureFixture[str]) -> None:
        """⭐ 2026-06-03 的接缝测试。

        这天没有法定节日，所以它证明的是「生日真的会出现在『今天』里」，
        并且用的是生日自己那套阶段名（「生日当天」而不是「过节」）。
        真正的平手（生日撞节日）在 `test_domain_birthday.py` 里测——
        CLI 手上没有能造出那两个节日的年份数据。
        """
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "today", "--date", f"{YEAR}-06-03"]) == 0
        out = capsys.readouterr().out
        assert "生日当天" in out
        assert "小明" in out

    def test_the_approach_shows_up_days_early(self, capsys: pytest.CaptureFixture[str]) -> None:
        """需求原文：「提前几天就知道」。生日也必须是这样。"""
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "today", "--date", f"{YEAR}-05-25"]) == 0
        out = capsys.readouterr().out
        assert "生日前" in out
        assert "小明" in out

    def test_the_upcoming_block_says_it_comes_every_year(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 节日的「放假 3 天」对生日没有意义——它每年都是同一天。
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "today", "--date", f"{YEAR}-05-25"]) == 0
        assert "每年这天" in capsys.readouterr().out

    def test_check_says_how_many_birthdays_are_recorded(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _add("--who", "user", "--on", "06-03")
        capsys.readouterr()
        main(["calendar", "check", "--year", str(YEAR)])
        out = capsys.readouterr().out
        assert "生日记录" in out
        assert "1 条" in out

    def test_check_does_not_confuse_birthdays_with_festivals(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """生日的「没核对」不该混进节日的待核对名单。

        两件事要去核的对象不同（一个是国务院公告，一个是本人），
        混在一行里会让人不知道该去核哪个。
        """
        _add("--who", "npc", "--npc-id", "npc-1", "--name", "张三", "--on", "06-03", "--unverified")
        capsys.readouterr()
        main(["calendar", "check", "--year", str(YEAR)])
        backlog = capsys.readouterr().out.split("生日记录")[0]
        assert "张三" not in backlog

    def test_a_birthday_alone_still_shows_a_year(self, capsys: pytest.CaptureFixture[str]) -> None:
        """没有那一年的节日数据时，生日要能自己撑起一份日历。

        否则「明年它记得我生日吗」的答案是「命令报错了」。
        """
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        assert main(["calendar", "list", "--year", str(YEAR + 2)]) == 0
        assert "小明" in capsys.readouterr().out

    def test_the_missing_festival_data_is_admitted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 只列出「小明的生日」却顶着「2028 年节日」的标题，是在骗人。
        _add("--who", "user", "--name", "小明", "--on", "06-03")
        capsys.readouterr()
        main(["calendar", "list", "--year", str(YEAR + 2)])
        assert "没有" in capsys.readouterr().err

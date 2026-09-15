"""``alterego.cli_db`` 的测试。

和 ``test_cli.py`` 一样用 ``capsys`` 抓输出，不 mock ``argparse``——
真正要验证的是「用户敲下 ``alterego db status`` 之后看到什么」。

这一组测试尤其不能碰真实数据：``db restore`` 会**覆盖**目标库。
所以下面那个 autouse fixture 把 ``core.data_dir`` 换成本次测试的临时目录，
任何一条用例都不可能走到开发机上那份 ``data/alterego.db``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.kernel.config import Config
from alterego.kernel.errors import MigrationError, StorageError
from alterego.storage.sqlite import SqliteStorageBackend
from alterego.storage.sqlite.migrator import MIGRATIONS_DIR, discover_migrations


REQUIRED_VERSION = max(item.version for item in discover_migrations(MIGRATIONS_DIR))
"""随包迁移脚本能升到的最高版本。不写死 4——加了 005 之后这里不该再改一遍。"""


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录换成本次测试专属的临时目录。

    ``_db_config()`` 之所以单独存在一个函数，就是给这里留的接口
    （见它的 docstring）。换掉它，整个 ``db`` 命令组就落在了 ``tmp_path`` 里。
    """
    monkeypatch.setattr("alterego.cli_db._db_config", lambda: _make_config(tmp_path))
    return tmp_path


def _make_config(tmp_path: Path, **storage_overrides: object) -> Config:
    """构造一份只认 ``tmp_path`` 的配置。"""
    overrides: dict[str, object] = {
        "core": {"data_dir": str(tmp_path)},
        "storage": {"backend": "sqlite", **storage_overrides},
    }
    return Config.load(path=None, env={}, overrides=overrides)


def _db(*args: str) -> int:
    """跑一次 ``db`` 子命令。"""
    return main(["db", *args])


def _database(tmp_path: Path) -> Path:
    """本程序认为的库文件位置。"""
    return tmp_path / "alterego.db"


# ────────────────────────────────────────────────────────────
# status · 只读，最不该有副作用的一条
# ────────────────────────────────────────────────────────────


class TestDbStatus:
    def test_a_missing_database_is_reported_as_such(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _db("status") == 0
        assert "还没建过" in capsys.readouterr().out

    def test_it_does_not_create_the_database(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """「查一下」不能变成「建了一个空库」。

        ``open(真路径)`` 默认就会把文件建出来，所以 ``db status`` 走的是
        内存库那条路。这条用例就是钉住它。
        """
        _db("status")
        assert not _database(tmp_path).exists()

    def test_it_shows_where_the_database_is(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("status")
        assert str(_database(tmp_path)) in capsys.readouterr().out

    def test_an_empty_database_has_pending_migrations(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("status")
        out = capsys.readouterr().out
        assert "当前版本  0" in out
        assert f"目标版本  {REQUIRED_VERSION}" in out
        assert f"待执行    {REQUIRED_VERSION} 个" in out

    def test_after_migrating_it_says_everything_is_current(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("migrate")
        capsys.readouterr()
        assert _db("status") == 0
        out = capsys.readouterr().out
        assert f"当前版本  {REQUIRED_VERSION}" in out
        assert "待执行    无" in out
        assert "已是最新" in out

    def test_it_names_the_pending_migration_files(self, capsys: pytest.CaptureFixture[str]) -> None:
        """光说「还差几个」没用，得说清楚是哪几个。"""
        _db("status")
        out = capsys.readouterr().out
        assert "001_initial" in out
        assert "非破坏性" in out

    def test_the_filename_column_is_derived_from_the_longest_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """列宽得**算出来**，不能写死。

        这条断言来自一个真实的 bug：列宽原本写死 25，理由注释是「现有最长的
        `004_observability.sql` 是 23 个字符」。`005_memory_consolidation.sql`
        （28 个字符）一进来，输出就成了
        `005_memory_consolidation.sql记忆巩固标记与活动流梳理标记……`——
        文件名和描述挤在同一行。现在按待执行文件里最长的那个算列宽。

        断言写成「对每一行都成立」，以后再加更长的迁移文件也会被拦住。
        """
        _db("status")
        out = capsys.readouterr().out

        names: list[str] = []
        columns: set[int] = set()
        for line in out.splitlines():
            if not line.startswith("  "):
                continue
            stem, marker, rest = line.partition(".sql")
            if not marker:
                continue
            name = stem.removeprefix("  ") + marker
            gap = len(rest) - len(rest.lstrip(" "))
            assert gap > 0, f"文件名和描述之间没有空隙：{line!r}"
            names.append(name)
            columns.add(len(name) + gap)

        assert names, "db status 没有列出任何待执行的迁移"
        assert len(columns) == 1, f"各行没有对齐到同一列：{sorted(columns)}"
        assert columns.pop() > max(len(name) for name in names), "列宽没有超过最长的文件名"

    def test_an_empty_database_admits_it_has_no_tables(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """文件在、表没建——这话得说出来，否则那个文件的体积是个谜。"""
        with SqliteStorageBackend.open(_database(tmp_path)):
            pass
        assert _db("status") == 0
        assert "空库，一个表都没有" in capsys.readouterr().out

    def test_an_unreadable_database_is_reported_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """库的版本比程序新（或太旧）时，这**就是**状态，不该当成崩溃。

        但要返回非零——脚本得能发现「这个库现在读不了」。
        """

        def _boom(config: Config) -> None:
            raise StorageError("库的 schema_version 比本程序新", version=99)

        monkeypatch.setattr("alterego.cli_db._peek_db", _boom)
        assert _db("status") == 2
        out = capsys.readouterr().out
        assert "当前无法读取" in out
        assert "比本程序新" in out


# ────────────────────────────────────────────────────────────
# migrate · 唯一会动手改库的一条
# ────────────────────────────────────────────────────────────


class TestDbMigrate:
    def test_it_creates_the_database(self, tmp_path: Path) -> None:
        assert _db("migrate") == 0
        assert _database(tmp_path).is_file()

    def test_it_says_what_it_moved_from_and_to(self, capsys: pytest.CaptureFixture[str]) -> None:
        _db("migrate")
        out = capsys.readouterr().out
        assert "已迁移" in out
        assert f"0 → {REQUIRED_VERSION}" in out
        assert f"应用了 {REQUIRED_VERSION} 个迁移" in out

    def test_the_second_run_is_a_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        _db("migrate")
        capsys.readouterr()
        assert _db("migrate") == 0
        assert "已是最新" in capsys.readouterr().out

    def test_dry_run_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--dry-run`` 一个字节都不该写。"""
        assert _db("migrate", "--dry-run") == 0
        assert not _database(tmp_path).exists()
        out = capsys.readouterr().out
        assert "将要应用" in out
        assert "没有改动任何东西" in out

    def test_dry_run_lists_the_files_it_would_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        _db("migrate", "--dry-run")
        out = capsys.readouterr().out
        assert "001_initial" in out

    def test_dry_run_after_migrating_says_there_is_nothing_to_do(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("migrate")
        capsys.readouterr()
        assert _db("migrate", "--dry-run") == 0
        assert "已是最新" in capsys.readouterr().out

    def test_dry_run_does_not_take_a_backup(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """预演连备份都不做——备份的时机是「真的要改数据库之前」。"""
        _db("migrate", "--dry-run")
        assert not (tmp_path / "backups").exists()

    def test_the_database_is_readable_afterwards(self, tmp_path: Path) -> None:
        """光看退出码不够：那个文件得真是个能打开的库。"""
        _db("migrate")
        with SqliteStorageBackend.open(_database(tmp_path), read_only=True) as backend:
            assert backend.current_schema_version == REQUIRED_VERSION


# ────────────────────────────────────────────────────────────
# backup · 唯一的「回滚」手段
# ────────────────────────────────────────────────────────────


class TestDbBackup:
    def test_it_lands_in_the_backups_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("migrate")
        capsys.readouterr()
        assert _db("backup") == 0
        saved = list((tmp_path / "backups").glob("*.db"))
        assert len(saved) == 1
        assert "已备份" in capsys.readouterr().out

    def test_dest_overrides_the_default_location(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("migrate")
        capsys.readouterr()
        target = tmp_path / "elsewhere" / "mine.db"
        assert _db("backup", "--dest", str(target)) == 0
        assert target.is_file()
        assert str(target) in capsys.readouterr().out

    def test_backing_up_before_the_database_exists_is_an_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _db("backup") == 2
        assert "还没有数据库可以备份" in capsys.readouterr().err

    def test_the_backup_is_a_real_database(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``VACUUM INTO`` 出来的是完整一份，不是「半个文件」。"""
        _db("migrate")
        capsys.readouterr()
        _db("backup")
        saved = next((tmp_path / "backups").glob("*.db"))
        with SqliteStorageBackend.open(saved, read_only=True) as backend:
            assert backend.current_schema_version == REQUIRED_VERSION


# ────────────────────────────────────────────────────────────
# restore · 会覆盖现在这一份，所以每一步都先校验
# ────────────────────────────────────────────────────────────


class TestDbRestore:
    def test_a_missing_file_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _db("restore", str(tmp_path / "nope.db")) == 2
        assert "找不到这个备份文件" in capsys.readouterr().err

    def test_the_database_itself_is_not_a_backup_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """从自己恢复等于什么都没做，但会先把 WAL 边车删掉——纯亏。"""
        _db("migrate")
        capsys.readouterr()
        assert _db("restore", str(_database(tmp_path))) == 2
        assert "不能把库自己当成备份来源" in capsys.readouterr().err

    def test_it_puts_the_database_back(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _db("migrate")
        capsys.readouterr()
        _db("backup")
        saved = next((tmp_path / "backups").glob("*.db"))
        capsys.readouterr()

        _database(tmp_path).unlink()
        assert _db("restore", str(saved)) == 0
        out = capsys.readouterr().out
        assert "已恢复" in out
        assert f"schema_version {REQUIRED_VERSION}" in out
        assert _database(tmp_path).is_file()

    def test_it_keeps_a_copy_of_the_database_it_overwrote(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """恢复是不可逆的，所以动手之前先把现在这份留一手。"""
        _db("migrate")
        capsys.readouterr()
        _db("backup")
        saved = next((tmp_path / "backups").glob("*.db"))
        capsys.readouterr()

        assert _db("restore", str(saved)) == 0
        out = capsys.readouterr().out
        assert "覆盖前的库已留一份" in out
        assert list((tmp_path / "backups").glob("before-restore-*.db"))

    def test_it_does_not_leave_a_safety_copy_when_there_was_nothing_to_overwrite(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """库本来就不存在，没有「覆盖前的库」可留——那行提示也不该出现。"""
        _db("migrate")
        capsys.readouterr()
        _db("backup")
        saved = next((tmp_path / "backups").glob("*.db"))
        capsys.readouterr()

        _database(tmp_path).unlink()
        assert _db("restore", str(saved)) == 0
        out = capsys.readouterr().out
        assert "覆盖前的库已留一份" not in out
        assert not list((tmp_path / "backups").glob("before-restore-*.db"))

    def test_it_clears_stale_wal_sidecars(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """主文件换了，旧的 ``-wal`` 还躺着的话，SQLite 会拿它去「重放」
        一批属于另一个库的页——库会坏得莫名其妙。
        """
        _db("migrate")
        capsys.readouterr()
        _db("backup")
        saved = next((tmp_path / "backups").glob("*.db"))
        capsys.readouterr()

        stale = _database(tmp_path).with_name(_database(tmp_path).name + "-wal")
        stale.write_bytes(b"stale pages from another database")
        assert _db("restore", str(saved)) == 0
        assert not stale.exists()


# ────────────────────────────────────────────────────────────
# 后端选择与退出码
# ────────────────────────────────────────────────────────────


class TestBackendSelection:
    def test_a_non_sqlite_backend_is_refused_rather_than_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """「改了配置却还是老样子」是最难查的那种问题。宁可明说。"""
        monkeypatch.setattr(
            "alterego.cli_db._db_config",
            lambda: _make_config(tmp_path, backend="postgres"),
        )
        assert _db("status") == 2
        assert "只随包提供了 sqlite" in capsys.readouterr().err

    def test_an_empty_backend_means_sqlite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``backend = ""`` 是配置里没写。那是默认值，不是错误。"""
        monkeypatch.setattr(
            "alterego.cli_db._db_config",
            lambda: _make_config(tmp_path, backend=""),
        )
        assert _db("status") == 0


class TestExitCodes:
    def test_the_db_group_without_a_subcommand_prints_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _db() == 0
        assert "{status,migrate,backup,restore}" in capsys.readouterr().out

    def test_the_db_group_is_listed_in_the_top_level_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        assert "db" in capsys.readouterr().out

    def test_a_failed_migration_exits_with_four(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """退出码 4 是文档承诺过的：「数据库可能停在半路」要跟「配置写错了」分开。"""

        def _boom(config: Config) -> None:
            raise MigrationError("迁移失败了", migration="001_initial")

        monkeypatch.setattr("alterego.cli_db._open_db", _boom)
        assert _db("migrate") == 4
        assert "迁移失败了" in capsys.readouterr().err

    def test_a_storage_error_exits_with_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """库还不存在的时候备份。这是「状况不对」，不是崩溃，所以是 2 不是 1。"""
        assert _db("backup") == 2
        assert "还没有数据库" in capsys.readouterr().err

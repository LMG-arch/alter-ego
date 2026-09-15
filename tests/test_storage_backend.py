"""``SqliteStorageBackend`` 的测试。

这里测的是**契约**，不是 SQLite：``StorageBackend`` 的五个方法、
双向版本兼容检查、备份与完整性检查。

全部用注入的 ``STEPS`` 迁移序列，不依赖产品里的真实迁移文件——
真实文件的断言在 ``test_storage_sqlite.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alterego.kernel.config import StorageConfig
from alterego.kernel.errors import StorageError
from alterego.storage.sqlite.backend import MIN_COMPATIBLE_VERSION, SqliteStorageBackend
from alterego.storage.sqlite.connection import PragmaSettings, SqliteConnection
from alterego.storage.sqlite.migrator import Migration


CREATE_SCHEMA_VERSION = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
"""

#: 一个自足的两步迁移。版本 1 建出 ``schema_version`` 与 ``note``，
#: 版本 2 再建一张 ``tag``——这样「只补缺失的步骤」才有东西可测。
STEPS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial",
        description="建立库结构",
        destructive=False,
        reversible=True,
        sql=CREATE_SCHEMA_VERSION + "CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT);",
        path=Path("001_initial.sql"),
    ),
    Migration(
        version=2,
        name="extra",
        description="增加标签表",
        destructive=False,
        reversible=True,
        sql="CREATE TABLE tag (id INTEGER PRIMARY KEY, label TEXT);",
        path=Path("002_extra.sql"),
    ),
)

#: ``StorageBackend`` 契约的全部方法。清单在这里再写一遍是刻意的冗余：
#: 接口加方法时这条测试会失败，提醒你「实现也要跟上」。
CONTRACT_METHODS = ("migrate", "transaction", "flush", "checkpoint", "close")


def _open(path: Path, **kwargs: object) -> SqliteStorageBackend:
    """按 ``STEPS`` 打开一个后端。"""
    return SqliteStorageBackend.open(path, migrations=STEPS, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "db" / "alterego.db"


@pytest.fixture
def backend(db_path: Path) -> SqliteStorageBackend:
    """一个已开、已授权备份目录的后端。

    备份目录必须显式传：``open()`` 不会自己派生一个（见
    ``test_open_without_a_backup_directory_has_none``）。
    """
    opened = _open(db_path, backup_dir=db_path.parent / "backups")
    yield opened
    opened.close()


def _table_names(backend: SqliteStorageBackend) -> set[str]:
    rows = backend.connection.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


# ─────────────────────────────────────────────────────────────
# 1. StorageBackend 契约
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", CONTRACT_METHODS)
def test_backend_exposes_every_contract_method(backend: SqliteStorageBackend, method: str) -> None:
    """``StorageBackend`` 是普通 ``Protocol``（运行期不可 ``isinstance``），
    所以这里逐个确认方法真的存在、真的可调用。
    """
    assert callable(getattr(backend, method))


def test_migrate_returns_the_applied_schema_version(backend: SqliteStorageBackend) -> None:
    assert backend.migrate() == "2"
    assert backend.current_schema_version == 2
    assert {"schema_version", "note", "tag"} <= _table_names(backend)


def test_migrate_is_safe_to_call_twice(backend: SqliteStorageBackend) -> None:
    backend.migrate()

    assert backend.migrate() == "2"
    assert backend.connection.scalar("SELECT COUNT(*) FROM schema_version") == 2


def test_transaction_commits_and_rolls_back(backend: SqliteStorageBackend) -> None:
    backend.migrate()

    with backend.transaction():
        backend.connection.execute("INSERT INTO note (body) VALUES ('kept')")

    with pytest.raises(RuntimeError), backend.transaction():
        backend.connection.execute("INSERT INTO note (body) VALUES ('dropped')")
        raise RuntimeError("模拟 tick 失败")

    bodies = [row["body"] for row in backend.connection.query("SELECT body FROM note")]
    assert bodies == ["kept"]


def test_transaction_is_nestable_through_the_backend(backend: SqliteStorageBackend) -> None:
    """Repository 各自 ``with backend.transaction():`` 时不得把 tick 切成几段。"""
    backend.migrate()

    with pytest.raises(RuntimeError), backend.transaction():
        with backend.transaction():
            backend.connection.execute("INSERT INTO note (body) VALUES ('a')")
        raise RuntimeError("外层失败")

    assert backend.connection.scalar("SELECT COUNT(*) FROM note") == 0


def test_flush_and_checkpoint_are_safe_without_writes(backend: SqliteStorageBackend) -> None:
    backend.flush()
    backend.checkpoint()


def test_close_is_idempotent(db_path: Path) -> None:
    opened = _open(db_path)
    opened.close()
    opened.close()


def test_context_manager_closes_the_backend(db_path: Path) -> None:
    with _open(db_path) as opened:
        opened.migrate()

    with pytest.raises(StorageError, match="closed"):
        opened.connection.execute("SELECT 1")


# ─────────────────────────────────────────────────────────────
# 2. 版本兼容（双向）
# ─────────────────────────────────────────────────────────────


def test_fresh_database_is_allowed_but_not_yet_migrated(backend: SqliteStorageBackend) -> None:
    """版本 0 不是错误——它只是「还没迁移过」。

    把「全新库」和「版本不兼容」混成同一个错误，首次启动会报「请升级程序」，
    而正确的下一步明明是「跑迁移」。
    """
    assert backend.current_schema_version == 0
    assert backend.plan().needs_migration


def test_open_refuses_a_database_newer_than_the_program(db_path: Path) -> None:
    with _open(db_path) as opened:
        opened.migrate()

    # 手动把版本号推到「未来」——模拟用户装了新版又回退到旧版程序。
    with SqliteConnection.open(db_path) as conn:
        conn.set_user_version(99)

    with pytest.raises(StorageError, match="高于当前程序支持") as caught:
        _open(db_path)

    assert caught.value.context["schema_version"] == 99
    assert caught.value.context["supported"] == 2
    assert "升级" in caught.value.context["hint"]


def test_open_refuses_a_database_older_than_min_compatible(db_path: Path) -> None:
    with SqliteConnection.open(db_path) as conn:
        conn.set_user_version(1)

    with pytest.raises(StorageError, match="版本过旧") as caught:
        _open(db_path, min_compatible_version=2)

    assert caught.value.context["min_compatible_version"] == 2
    assert caught.value.context["hint"] == "运行 alterego db migrate"


def test_min_compatible_version_defaults_to_one() -> None:
    assert MIN_COMPATIBLE_VERSION == 1


def test_required_version_is_derived_from_the_migrations(backend: SqliteStorageBackend) -> None:
    """从迁移序列推导，不写死常量——否则加了 005 却忘了改常量时毫无症状。"""
    assert backend.required_schema_version == 2
    assert backend.min_compatible_version == MIN_COMPATIBLE_VERSION


def test_error_context_names_the_database_file(db_path: Path) -> None:
    """多实例场景下「是哪个库版本不对」必须一眼看清。"""
    with SqliteConnection.open(db_path) as conn:
        conn.set_user_version(99)

    with pytest.raises(StorageError) as caught:
        _open(db_path)

    reported = Path(caught.value.context["db_path"])
    assert reported.name == "alterego.db"
    assert reported.parent.name == "db"


def test_failed_open_leaves_no_locked_files(db_path: Path) -> None:
    """构造到一半失败必须关掉连接。

    WAL 连接会一直握着 ``-wal`` / ``-shm``；泄漏几次之后连删除库文件都会失败。
    """
    with SqliteConnection.open(db_path) as conn:
        conn.set_user_version(99)

    for _ in range(3):
        with pytest.raises(StorageError):
            _open(db_path)

    # 能删干净就说明没人还握着句柄。
    for leftover in db_path.parent.glob("alterego.db*"):
        leftover.unlink()


def test_plan_reports_pending_work(backend: SqliteStorageBackend) -> None:
    assert [item.version for item in backend.plan().pending] == [1, 2]

    backend.migrate()

    assert not backend.plan().needs_migration


# ─────────────────────────────────────────────────────────────
# 3. 备份
# ─────────────────────────────────────────────────────────────


def test_open_without_a_backup_directory_has_none(db_path: Path) -> None:
    """``open()`` **不**派生备份目录。

    ``None`` 的含义必须处处一致：没有退路。如果 ``open()`` 遇到 ``None``
    就自动造一个 ``backups/``，那么 ``from_config`` 想把
    ``backup_before_destructive_migration = false`` 表达成「没有退路」时，
    这个意图会被悄悄盖掉——同一个值在两个入口含义不同，是 bug 的温床。
    """
    with _open(db_path) as opened, pytest.raises(StorageError, match="未配置备份目录"):
        opened.backup()


def test_open_honours_an_explicit_backup_directory(db_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "my-backups"

    with _open(db_path, backup_dir=target) as opened:
        opened.migrate()
        backup = opened.backup()

    assert backup.parent == target
    assert backup.exists()


def test_backup_accepts_an_explicit_destination(
    backend: SqliteStorageBackend, tmp_path: Path
) -> None:
    backend.migrate()
    destination = tmp_path / "elsewhere" / "snapshot.db"

    assert backend.backup(destination) == destination

    with SqliteConnection.open(destination, readonly=True) as restored:
        assert restored.user_version() == 2
        assert restored.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 'note'") == 1


def test_backup_is_refused_inside_a_transaction(backend: SqliteStorageBackend) -> None:
    with backend.transaction(), pytest.raises(StorageError, match="不能在事务内执行"):
        backend.backup()


def test_backup_without_a_backup_directory_is_refused(db_path: Path) -> None:
    """没有备份目录时，``backup()`` 拒绝执行并说清楚怎么补救。

    这里直接构造后端（绕开 ``from_config``），走的是「调用方自己拿主意」的路径。
    """
    conn = SqliteConnection.open(db_path)
    try:
        with (
            SqliteStorageBackend(conn, migrations=STEPS, backup_dir=None) as opened,
            pytest.raises(StorageError, match="未配置备份目录"),
        ):
            opened.backup()
    finally:
        conn.close()


def test_db_path_is_reported(db_path: Path) -> None:
    with _open(db_path) as opened:
        assert opened.db_path == db_path


# ─────────────────────────────────────────────────────────────
# 4. 完整性检查与优化
# ─────────────────────────────────────────────────────────────


def test_integrity_check_passes_on_a_healthy_database(backend: SqliteStorageBackend) -> None:
    backend.migrate()

    backend.integrity_check()


def test_integrity_check_reports_corruption(db_path: Path) -> None:
    """真的把文件写坏一次——「完整性检查能发现问题」必须有证据。

    做法是把第 2 页之后全部清零，同时**保留第 1 页**：
    SQLite 因此仍认得这是一个数据库（头部完好），但读内容页时会发现数据不可信。
    先塞够行数，保证确实存在第 2 页。
    """
    with _open(db_path) as opened:
        opened.migrate()
        with opened.transaction():
            for index in range(400):
                opened.connection.execute("INSERT INTO note (body) VALUES (?)", (f"row-{index}",))
        opened.checkpoint()
        page_size = int(opened.connection.query_one("PRAGMA page_size")[0])

    raw = bytearray(db_path.read_bytes())
    assert len(raw) > page_size * 2, "库里必须不止一页，否则这条测试没有意义"
    raw[page_size:] = bytes(len(raw) - page_size)
    db_path.write_bytes(bytes(raw))

    with pytest.raises(StorageError), _open(db_path) as opened:
        opened.integrity_check()


def test_optimize_is_safe_on_an_empty_database(backend: SqliteStorageBackend) -> None:
    backend.optimize()


# ─────────────────────────────────────────────────────────────
# 5. 从配置构造
# ─────────────────────────────────────────────────────────────


def test_from_config_applies_the_pragma_settings(tmp_path: Path) -> None:
    config = StorageConfig(
        db_path=tmp_path / "configured.db",
        journal_mode="DELETE",
        synchronous="FULL",
        busy_timeout_ms=1234,
        foreign_keys=False,
        temp_store="FILE",
    )

    with SqliteStorageBackend.from_config(config, migrations=STEPS) as opened:
        conn = opened.connection
        assert conn.query_one("PRAGMA journal_mode")[0] == "delete"
        assert conn.query_one("PRAGMA synchronous")[0] == 2  # FULL
        assert conn.query_one("PRAGMA busy_timeout")[0] == 1234
        assert conn.query_one("PRAGMA foreign_keys")[0] == 0
        assert conn.query_one("PRAGMA temp_store")[0] == 1  # FILE


def test_from_config_uses_the_configured_database_path(tmp_path: Path) -> None:
    config = StorageConfig(db_path=tmp_path / "nested" / "configured.db")

    with SqliteStorageBackend.from_config(config, migrations=STEPS) as opened:
        assert opened.db_path == tmp_path / "nested" / "configured.db"
        assert opened.db_path.exists()


def test_from_config_default_backup_directory_sits_next_to_the_database(tmp_path: Path) -> None:
    config = StorageConfig(db_path=tmp_path / "data" / "alterego.db")

    with SqliteStorageBackend.from_config(config, migrations=STEPS) as opened:
        opened.migrate()
        backup = opened.backup()

    assert backup.parent == tmp_path / "data" / "backups"
    assert backup.exists()


def test_from_config_disabling_backup_makes_destructive_migrations_a_hard_stop(
    tmp_path: Path,
) -> None:
    """``backup_before_destructive_migration = false`` 的含义是「不给退路就别改」。

    它不是「那就别备份了」——没有备份的破坏性迁移会直接毁数据。
    """
    config = StorageConfig(
        db_path=tmp_path / "configured.db",
        backup_before_destructive_migration=False,
    )

    with (
        SqliteStorageBackend.from_config(config, migrations=STEPS) as opened,
        pytest.raises(StorageError, match="未配置备份目录"),
    ):
        opened.backup()


def test_startup_checks_are_honoured(tmp_path: Path) -> None:
    """启动自检开关必须真的接上，否则配置项就是一句空话。"""
    path = tmp_path / "db.sqlite"
    with _open(path) as opened:
        opened.migrate()

    config = StorageConfig(db_path=path, checkpoint_on_start=True, integrity_check_on_start=True)

    with SqliteStorageBackend.from_config(config, migrations=STEPS) as opened:
        assert opened.current_schema_version == 2


def test_pragma_settings_accept_every_value_the_config_allows() -> None:
    """``StorageConfig`` 与 ``PragmaSettings`` 各有一份取值来源，
    最容易出现「配置里能写、连接层不认」的死角。
    """
    for mode in ("WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"):
        settings = PragmaSettings(journal_mode=mode)

        assert f"PRAGMA journal_mode = {mode}" in settings.statements()

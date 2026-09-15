"""迁移器的测试。

全部用**临时迁移目录**，不碰 ``src/alterego/storage/sqlite/migrations/``：
真实迁移文件是产品的一部分，它的断言放在 ``test_storage_sqlite.py`` 里。
这里只测「发现规则」与「原子应用」这两件事。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from alterego.kernel.errors import MigrationError
from alterego.storage.sqlite.connection import SqliteConnection
from alterego.storage.sqlite.migrator import (
    Migration,
    MigrationPlan,
    Migrator,
    discover_migrations,
)


# ─────────────────────────────────────────────────────────────
# 夹具与辅助
# ─────────────────────────────────────────────────────────────

#: 建 ``schema_version`` 表本身也是迁移 001 的活。这很重要：
#: 迁移器要往这张表里记账，所以它必须由**第一个**迁移建出来，
#: 而不能由迁移器自己在外面建——那样就出现「谁拥有这张表」的模糊地带。
CREATE_SCHEMA_VERSION = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
"""


def _write_migration(
    directory: Path,
    version: int,
    name: str,
    body: str,
    *,
    description: str = "测试迁移",
    destructive: bool = False,
    reversible: bool = True,
    header_version: int | None = None,
    extra_header: str = "",
) -> Path:
    """按规范写一个迁移文件，并允许故意写错头部用于反例。"""
    declared = version if header_version is None else header_version
    text = (
        f"-- migration: {declared:03d}\n"
        f"-- description: {description}\n"
        f"-- destructive: {str(destructive).lower()}\n"
        f"-- reversible: {str(reversible).lower()}\n"
        f"{extra_header}"
        "\n"
        f"{body}\n"
    )
    path = directory / f"{version:03d}_{name}.sql"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    """两个迁移：001 建表与记账表，002 加一列。"""
    root = tmp_path / "migrations"
    root.mkdir()
    _write_migration(
        root,
        1,
        "initial",
        CREATE_SCHEMA_VERSION + "CREATE TABLE person (id INTEGER PRIMARY KEY);",
        description="建立库结构",
    )
    _write_migration(
        root,
        2,
        "media",
        "CREATE TABLE media (id INTEGER PRIMARY KEY);",
        description="增加媒体表",
    )
    return root


@pytest.fixture
def conn(tmp_path: Path) -> SqliteConnection:
    opened = SqliteConnection.open(tmp_path / "db.sqlite")
    yield opened
    opened.close()


def _table_names(conn: SqliteConnection) -> set[str]:
    return {
        row["name"] for row in conn.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _migrate_up_to(conn: SqliteConnection, directory: Path, version: int) -> None:
    """只应用版本号 ≤ ``version`` 的那些迁移，用来伪造「老库」状态。"""
    steps = tuple(item for item in discover_migrations(directory) if item.version <= version)
    Migrator(conn, migrations=steps).migrate()


# ─────────────────────────────────────────────────────────────
# 1. 发现与校验
# ─────────────────────────────────────────────────────────────


def test_discover_returns_migrations_in_version_order(migrations_dir: Path) -> None:
    found = discover_migrations(migrations_dir)

    assert [item.version for item in found] == [1, 2]
    assert [item.name for item in found] == ["initial", "media"]
    assert found[0].description == "建立库结构"
    assert found[1].path.name == "002_media.sql"


def test_discover_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="迁移目录不存在"):
        discover_migrations(tmp_path / "nope")


def test_discover_rejects_empty_directory(tmp_path: Path) -> None:
    """空目录会伪装成「无需迁移」。宁可起不来，也不要对着空库继续跑。"""
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(MigrationError, match=r"没有 .sql 文件"):
        discover_migrations(empty)


@pytest.mark.parametrize(
    "filename", ["1_initial.sql", "001_Initial.sql", "001-initial.sql", "notes.sql"]
)
def test_discover_rejects_bad_filename(tmp_path: Path, filename: str) -> None:
    root = tmp_path / "m"
    root.mkdir()
    (root / filename).write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError, match="文件名不合规"):
        discover_migrations(root)


def test_discover_rejects_missing_header_fields(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    (root / "001_initial.sql").write_text(
        "-- migration: 001\n-- description: 只有两项\n\nSELECT 1;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="缺少必填字段") as caught:
        discover_migrations(root)

    assert caught.value.context["missing"] == ["destructive", "reversible"]


def test_discover_rejects_unknown_header_key(tmp_path: Path) -> None:
    """``destory: true`` 这种拼写错误必须报错，不能被当成正文注释忽略。"""
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "SELECT 1;", extra_header="-- destory: true\n")

    with pytest.raises(MigrationError, match="未知字段") as caught:
        discover_migrations(root)

    assert caught.value.context["key"] == "destory"


def test_discover_rejects_duplicate_header_key(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "SELECT 1;", extra_header="-- description: 又写一遍\n")

    with pytest.raises(MigrationError, match="重复"):
        discover_migrations(root)


def test_discover_rejects_version_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "SELECT 1;", header_version=7)

    with pytest.raises(MigrationError, match="版本号与文件名不一致") as caught:
        discover_migrations(root)

    assert caught.value.context == {
        "path": "001_initial.sql",
        "filename_version": 1,
        "header_version": "007",
    }


def test_discover_rejects_empty_description(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "SELECT 1;", description="   ")

    with pytest.raises(MigrationError, match="描述不能为空"):
        discover_migrations(root)


def test_discover_rejects_illegal_boolean(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    (root / "001_initial.sql").write_text(
        "-- migration: 001\n-- description: x\n-- destructive: maybe\n-- reversible: true\n\nSELECT 1;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="布尔值非法") as caught:
        discover_migrations(root)

    assert caught.value.context["value"] == "maybe"


def test_discover_rejects_gap_in_version_numbers(tmp_path: Path) -> None:
    """跳号之后 ``schema_version`` 会出现空洞，而按版本号比较的逻辑会静默跳过。"""
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "SELECT 1;")
    _write_migration(root, 3, "third", "SELECT 1;")

    with pytest.raises(MigrationError, match="版本号不连续") as caught:
        discover_migrations(root)

    assert caught.value.context["expected"] == 2
    assert caught.value.context["found"] == 3


def test_discover_rejects_empty_body(tmp_path: Path) -> None:
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", "   \n\n  ")

    with pytest.raises(MigrationError, match="没有 SQL 主体"):
        discover_migrations(root)


@pytest.mark.parametrize(
    "statement",
    [
        "COMMIT;",
        "ROLLBACK;",
        "BEGIN TRANSACTION;",
        "BEGIN IMMEDIATE;",
        "BEGIN;",
        "END TRANSACTION;",
    ],
)
def test_discover_rejects_embedded_transaction_control(tmp_path: Path, statement: str) -> None:
    """文件自带事务边界会把迁移器开的事务提前结束，「原子应用」随即失效。"""
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(root, 1, "initial", f"CREATE TABLE t (id INTEGER PRIMARY KEY);\n{statement}")

    with pytest.raises(MigrationError, match="不得出现事务控制语句"):
        discover_migrations(root)


def test_discover_allows_trigger_body_begin(tmp_path: Path) -> None:
    """触发器体的 ``BEGIN ... END;`` 不是事务控制语句，不能误伤。

    这是真会出现的情况：``001_initial.sql`` 里有三个同步 FTS5 的触发器。
    """
    root = tmp_path / "m"
    root.mkdir()
    _write_migration(
        root,
        1,
        "initial",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT);\n"
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
        "    UPDATE t SET body = body WHERE id = new.id;\n"
        "END;\n",
    )

    found = discover_migrations(root)

    assert found[0].version == 1


# ─────────────────────────────────────────────────────────────
# 2. 差异计算
# ─────────────────────────────────────────────────────────────


def test_plan_on_a_fresh_database_lists_everything(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    plan = Migrator(conn, migrations_dir=migrations_dir).plan()

    assert plan.current == 0
    assert plan.target == 2
    assert [item.version for item in plan.pending] == [1, 2]
    assert plan.needs_migration
    assert not plan.is_downgrade


def test_plan_is_empty_when_up_to_date(migrations_dir: Path, conn: SqliteConnection) -> None:
    migrator = Migrator(conn, migrations_dir=migrations_dir)
    migrator.migrate()

    plan = migrator.plan()

    assert plan.current == 2
    assert plan.pending == ()
    assert not plan.needs_migration


def test_plan_lists_destructive_pending(migrations_dir: Path, conn: SqliteConnection) -> None:
    _write_migration(
        migrations_dir,
        3,
        "drop_legacy",
        "DROP TABLE IF EXISTS nothing_here;",
        destructive=True,
    )

    plan = Migrator(conn, migrations_dir=migrations_dir).plan()

    assert [item.version for item in plan.destructive_pending] == [3]


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (2, 2, "已是最新版本（2），无需迁移"),
        (0, 2, "将从 0 升到 2"),
    ],
)
def test_plan_describe_mentions_the_numbers(
    migrations_dir: Path,
    conn: SqliteConnection,
    current: int,
    target: int,
    expected: str,
) -> None:
    conn.set_user_version(current)
    plan = Migrator(conn, migrations_dir=migrations_dir).plan()

    assert plan.target == target
    assert expected in plan.describe()


def test_plan_describe_explains_a_downgrade(migrations_dir: Path, conn: SqliteConnection) -> None:
    conn.set_user_version(9)

    plan = Migrator(conn, migrations_dir=migrations_dir).plan()

    assert plan.is_downgrade
    assert "需要升级程序" in plan.describe()


def test_plan_dataclass_is_frozen() -> None:
    plan = MigrationPlan(current=0, target=0, pending=())

    with pytest.raises(FrozenInstanceError):
        plan.current = 1  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
# 3. 应用
# ─────────────────────────────────────────────────────────────


def test_migrate_applies_everything_and_bumps_the_version(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    result = Migrator(conn, migrations_dir=migrations_dir).migrate()

    assert [item.version for item in result.pending] == [1, 2]
    assert conn.user_version() == 2
    assert {"schema_version", "person", "media"} <= _table_names(conn)


def test_migrate_records_a_schema_version_row_per_step(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    Migrator(conn, migrations_dir=migrations_dir).migrate()

    rows = conn.query(
        "SELECT version, applied_at, description FROM schema_version ORDER BY version"
    )

    assert [(row["version"], row["description"]) for row in rows] == [
        (1, "建立库结构"),
        (2, "增加媒体表"),
    ]
    # applied_at 必须是带时区的 ISO 8601，和领域层写入的时间戳同形。
    parsed = datetime.fromisoformat(rows[0]["applied_at"])
    assert parsed.tzinfo is not None


def test_migrate_is_idempotent(migrations_dir: Path, conn: SqliteConnection) -> None:
    migrator = Migrator(conn, migrations_dir=migrations_dir)
    migrator.migrate()

    second = migrator.migrate()

    assert second.pending == ()
    assert conn.scalar("SELECT COUNT(*) FROM schema_version") == 2


def test_migrate_dry_run_touches_nothing(migrations_dir: Path, conn: SqliteConnection) -> None:
    """--dry-run 必须真的什么都不做：不建表、不涨版本号。"""
    plan = Migrator(conn, migrations_dir=migrations_dir).migrate(dry_run=True)

    assert [item.version for item in plan.pending] == [1, 2]
    assert conn.user_version() == 0
    assert _table_names(conn) == set()


def test_migrate_only_applies_the_missing_steps(
    migrations_dir: Path, conn: SqliteConnection, tmp_path: Path
) -> None:
    """版本 1 已经落盘的库，只补 2。"""
    upto_one = Migrator(conn, migrations=[discover_migrations(migrations_dir)[0]])
    upto_one.migrate()
    assert conn.user_version() == 1

    plan = Migrator(conn, migrations_dir=migrations_dir).migrate()

    assert [item.version for item in plan.pending] == [2]
    assert conn.user_version() == 2
    assert conn.scalar("SELECT COUNT(*) FROM schema_version") == 2


def test_migrate_refuses_to_run_on_a_newer_database(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    conn.set_user_version(99)

    with pytest.raises(MigrationError, match="高于当前程序支持") as caught:
        Migrator(conn, migrations_dir=migrations_dir).migrate()

    assert caught.value.context["supported"] == 2


def test_failed_migration_rolls_back_completely(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    """半截状态是最坏的结果：表建了一半、版本号没涨，下次启动还会再跑一遍。

    SQLite 的 DDL 是事务性的，所以「一个迁移一个事务」真的能兜住这件事——
    但前提是迁移器在 ``executescript`` 报错后主动回滚。
    """
    _write_migration(
        migrations_dir,
        3,
        "broken",
        "CREATE TABLE ok_table (id INTEGER PRIMARY KEY);\nSELECT * FROM table_that_never_existed;",
    )
    migrator = Migrator(conn, migrations_dir=migrations_dir)

    with pytest.raises(MigrationError, match="已回滚") as caught:
        migrator.migrate()

    assert caught.value.context["version"] == 3
    assert conn.user_version() == 2, "前两个迁移应当已经落盘"
    assert "ok_table" not in _table_names(conn), "失败的那个迁移必须一点痕迹都不留"
    assert conn.raw.in_transaction is False, "失败后不能留下悬空事务"


def test_connection_is_still_usable_after_a_failed_migration(
    migrations_dir: Path, conn: SqliteConnection
) -> None:
    """回滚之后连接必须干净，否则下一个调用方会在别人的事务里写数据。"""
    _write_migration(migrations_dir, 3, "broken", "SELECT * FROM nope;")
    migrator = Migrator(conn, migrations_dir=migrations_dir)

    with pytest.raises(MigrationError):
        migrator.migrate()

    with conn.transaction():
        conn.execute("INSERT INTO person (id) VALUES (42)")

    assert conn.scalar("SELECT COUNT(*) FROM person WHERE id = 42") == 1


# ─────────────────────────────────────────────────────────────
# 4. 破坏性迁移的备份
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def destructive_dir(migrations_dir: Path) -> Path:
    _write_migration(
        migrations_dir,
        3,
        "rebuild",
        "DROP TABLE IF EXISTS media;",
        description="重建媒体表",
        destructive=True,
        reversible=False,
    )
    return migrations_dir


def test_destructive_migration_without_a_backup_dir_is_refused(
    destructive_dir: Path, conn: SqliteConnection
) -> None:
    """「忘了配备份目录」绝不能退化成「那就别备份了」。"""
    with pytest.raises(MigrationError, match="必须备份"):
        Migrator(conn, migrations_dir=destructive_dir).migrate()

    assert conn.user_version() == 0, "拒绝应当是彻底的，前两个迁移也不要跑"


def test_destructive_migration_writes_a_backup(
    destructive_dir: Path, conn: SqliteConnection, tmp_path: Path
) -> None:
    """模拟真实场景：老库已经到版本 2，新版本带来一个破坏性迁移 3。"""
    _migrate_up_to(conn, destructive_dir, 2)
    assert "media" in _table_names(conn)

    backup_dir = tmp_path / "backups"
    Migrator(conn, migrations_dir=destructive_dir, backup_dir=backup_dir).migrate()

    backups = sorted(backup_dir.glob("pre-migration-3-*.db"))
    assert len(backups) == 1

    # 备份必须是**改库之前**的快照：``media`` 表还在里面。
    with SqliteConnection.open(backups[0], readonly=True) as restored:
        assert "media" in {row["name"] for row in restored.query("SELECT name FROM sqlite_master")}

    assert "media" not in _table_names(conn)
    assert conn.user_version() == 3


def test_non_destructive_migration_does_not_write_a_backup(
    migrations_dir: Path, conn: SqliteConnection, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"

    Migrator(conn, migrations_dir=migrations_dir, backup_dir=backup_dir).migrate()

    assert not backup_dir.exists()


def test_backup_filename_carries_the_version_and_a_timestamp(
    destructive_dir: Path, conn: SqliteConnection, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"
    Migrator(conn, migrations_dir=destructive_dir, backup_dir=backup_dir).migrate()

    name = next(backup_dir.iterdir()).name

    assert name.startswith("pre-migration-3-")
    assert name.endswith(".db")
    stamp = name.removeprefix("pre-migration-3-").removesuffix(".db")
    datetime.strptime(stamp, "%Y%m%d-%H%M%S")  # 解析失败即格式错


# ─────────────────────────────────────────────────────────────
# 5. 显式传入迁移序列（不依赖文件系统）
# ─────────────────────────────────────────────────────────────


def test_migrator_accepts_explicit_migrations(conn: SqliteConnection) -> None:
    """``SqliteStorageBackend`` 之外还有测试与工具会直接构造迁移序列。"""
    steps = (
        Migration(
            version=1,
            name="initial",
            description="内联迁移",
            destructive=False,
            reversible=True,
            sql=CREATE_SCHEMA_VERSION + "CREATE TABLE inline (id INTEGER PRIMARY KEY);",
            path=Path("001_initial.sql"),
        ),
    )
    migrator = Migrator(conn, migrations=steps)

    assert migrator.target_version == 1
    assert migrator.migrations == steps

    migrator.migrate()

    assert conn.user_version() == 1
    assert "inline" in _table_names(conn)

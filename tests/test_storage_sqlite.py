"""SQLite 存储层的测试。

分三段，对应三个模块：

1. **连接**（``connection.py``）—— PRAGMA 是否真的生效、事务嵌套语义、
   版本号、备份。
2. **迁移**（``migrator.py``）—— 发现、差异、原子应用、破坏性迁移前备份。
3. **后端**（``backend.py``）—— ``StorageBackend`` 契约与版本文档兼容检查。

每个 PRAGMA 断言都是**回读**出来的，不是「我们设了所以它生效了」——
SQLite 对非法 PRAGMA 是静默忽略的，不读回来就永远发现不了写错的配置项。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alterego.kernel.errors import IntegrityError, StorageError
from alterego.storage.sqlite.connection import PragmaSettings, SqliteConnection


# ─────────────────────────────────────────────────────────────
# 夹具
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """一个位于临时目录、父目录尚不存在的库路径。"""
    return tmp_path / "nested" / "alterego.db"


@pytest.fixture
def conn(db_path: Path) -> SqliteConnection:
    """打开好的连接，测试结束自动关闭。"""
    opened = SqliteConnection.open(db_path)
    yield opened
    opened.close()


def _pragma(conn: SqliteConnection, name: str) -> object:
    row = conn.query_one(f"PRAGMA {name}")
    assert row is not None
    return row[0]


# ─────────────────────────────────────────────────────────────
# 1. PRAGMA
# ─────────────────────────────────────────────────────────────


def test_open_creates_missing_parent_directory(db_path: Path) -> None:
    """数据目录首次运行必然不存在，连接层要负责建出来。"""
    assert not db_path.parent.exists()

    with SqliteConnection.open(db_path) as opened:
        assert db_path.exists()
        assert opened.query_one("SELECT 1") is not None


def test_pragmas_actually_take_effect(conn: SqliteConnection) -> None:
    """五个 PRAGMA 全部回读校验。写错任何一个都会被静默忽略。"""
    assert _pragma(conn, "journal_mode") == "wal"
    assert _pragma(conn, "synchronous") == 1  # NORMAL
    assert _pragma(conn, "busy_timeout") == 5000
    assert _pragma(conn, "foreign_keys") == 1
    assert _pragma(conn, "temp_store") == 2  # MEMORY


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("journal_mode", "WAL2"),
        ("synchronous", "SOMETIMES"),
        ("temp_store", "DISK"),
    ],
)
def test_illegal_pragma_value_is_rejected_at_construction(field: str, value: str) -> None:
    """PRAGMA 不支持参数绑定，只能拼字符串——所以取值必须在拼之前就挡住。"""
    with pytest.raises(StorageError) as caught:
        PragmaSettings(**{field: value})  # type: ignore[arg-type]

    assert caught.value.context["setting"] == field
    assert value in caught.value.context["value"]


def test_negative_busy_timeout_is_rejected() -> None:
    with pytest.raises(StorageError, match="busy_timeout"):
        PragmaSettings(busy_timeout_ms=-1)


def test_pragma_statements_are_ordered_and_quoted() -> None:
    """顺序不能变：journal_mode 必须在任何事务开始前生效。"""
    statements = PragmaSettings().statements()

    assert statements[0] == "PRAGMA journal_mode = WAL"
    assert statements[3] == "PRAGMA foreign_keys = ON"


def test_foreign_keys_are_enforced(conn: SqliteConnection) -> None:
    """SQLite 默认不强制外键。这条测试是「开了」与「真的有用」之间的桥。"""
    conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))")

    with pytest.raises(IntegrityError):
        conn.execute("INSERT INTO child (id, parent_id) VALUES ('c1', 'missing')")


def test_foreign_keys_can_be_turned_off(db_path: Path) -> None:
    """关掉之后孤儿行应当能插进去——否则上面那条测试证明不了任何东西。"""
    with SqliteConnection.open(db_path, settings=PragmaSettings(foreign_keys=False)) as opened:
        opened.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        opened.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))"
        )

        opened.execute("INSERT INTO child (id, parent_id) VALUES ('c1', 'missing')")

        assert opened.scalar("SELECT COUNT(*) FROM child") == 1


# ─────────────────────────────────────────────────────────────
# 2. 事务
# ─────────────────────────────────────────────────────────────


def test_transaction_commits_when_body_succeeds(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    with conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('a')")

    assert conn.scalar("SELECT COUNT(*) FROM t") == 1


def test_transaction_rolls_back_when_body_raises(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    with pytest.raises(RuntimeError), conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        raise RuntimeError("模拟 tick 中途炸掉")

    assert conn.scalar("SELECT COUNT(*) FROM t") == 0
    assert not conn.in_transaction


def test_nested_transaction_joins_the_outer_one(conn: SqliteConnection) -> None:
    """内层失败必须把外层一起带走。

    这是「一个 tick 的所有写入要么全成功要么全回滚」的**唯一**保障：
    如果嵌套的 transaction() 真的各自 BEGIN，内层的 COMMIT 就会
    把外层的原子性从中间劈开。
    """
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    with pytest.raises(RuntimeError), conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('outer')")
        with conn.transaction():
            conn.execute("INSERT INTO t (value) VALUES ('inner')")
        raise RuntimeError("外层失败")

    assert conn.scalar("SELECT COUNT(*) FROM t") == 0


def test_nested_transaction_commits_once_at_the_outermost_level(conn: SqliteConnection) -> None:
    """三层嵌套全部成功，最外层退出时才落盘。"""
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    with conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('l1')")
        with conn.transaction():
            conn.execute("INSERT INTO t (value) VALUES ('l2')")
            with conn.transaction():
                conn.execute("INSERT INTO t (value) VALUES ('l3')")
            assert conn.in_transaction, "内层退出不应结束事务"
        assert conn.in_transaction, "中层退出不应结束事务"

    assert conn.scalar("SELECT COUNT(*) FROM t") == 3
    assert not conn.in_transaction


def test_inner_error_swallowed_inside_outer_keeps_the_outer_transaction_alive(
    conn: SqliteConnection,
) -> None:
    """已知边界，写成测试是为了让它**可被看见**，而不是假装它不存在。

    嵌套是「合并进外层」而不是 ``SAVEPOINT``，所以内层异常如果被外层
    ``try/except`` 吞掉，内层那一半写入会随外层一起提交。
    约定：**不要在外层事务里吞掉 Repository 的异常**。
    """
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    with conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('outer')")
        try:
            with conn.transaction():
                conn.execute("INSERT INTO t (value) VALUES ('inner')")
                raise RuntimeError("会被吞掉")
        except RuntimeError:
            pass

    assert conn.scalar("SELECT COUNT(*) FROM t") == 2


def test_in_transaction_reflects_nesting(conn: SqliteConnection) -> None:
    assert not conn.in_transaction

    with conn.transaction():
        assert conn.in_transaction
        with conn.transaction():
            assert conn.in_transaction
        assert conn.in_transaction

    assert not conn.in_transaction


# ─────────────────────────────────────────────────────────────
# 3. 错误包装
# ─────────────────────────────────────────────────────────────


def test_integrity_error_is_wrapped(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")

    with pytest.raises(IntegrityError) as caught:
        conn.execute("INSERT INTO t (id) VALUES ('x')")
        conn.execute("INSERT INTO t (id) VALUES ('x')")

    assert "UNIQUE" in str(caught.value.context["error"])


def test_sql_error_is_wrapped_with_first_line_only(conn: SqliteConnection) -> None:
    """上下文里塞整段 DDL 会让日志没法读，只留第一行。"""
    broken = "SELECT * FROM\n  definitely_not_here\n"

    with pytest.raises(StorageError) as caught:
        conn.execute(broken)

    assert caught.value.context["sql"] == "SELECT * FROM"


def test_storage_error_is_not_retryable() -> None:
    """存储错误重试没有意义：SQL 写错了，再跑一遍还是错。"""
    assert StorageError("x").retryable is False


# ─────────────────────────────────────────────────────────────
# 4. 版本号
# ─────────────────────────────────────────────────────────────


def test_user_version_defaults_to_zero(conn: SqliteConnection) -> None:
    assert conn.user_version() == 0


def test_user_version_roundtrip(conn: SqliteConnection) -> None:
    conn.set_user_version(4)
    assert conn.user_version() == 4

    conn.set_user_version(0)
    assert conn.user_version() == 0


def test_negative_user_version_is_rejected(conn: SqliteConnection) -> None:
    with pytest.raises(StorageError, match="不能为负"):
        conn.set_user_version(-1)


# ─────────────────────────────────────────────────────────────
# 5. 维护：flush / checkpoint / integrity
# ─────────────────────────────────────────────────────────────


def test_flush_outside_transaction_is_safe(conn: SqliteConnection) -> None:
    """PRAGMA wal_checkpoint 在没有 WAL 文件时也只是返回一行，不该报错。"""
    conn.flush()


def test_flush_inside_transaction_does_not_break_it(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with conn.transaction():
        conn.execute("INSERT INTO t DEFAULT VALUES")
        conn.flush()
        assert conn.in_transaction, "flush() 不得结束事务"

    assert conn.scalar("SELECT COUNT(*) FROM t") == 1


def test_checkpoint_after_writes(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with conn.transaction():
        conn.execute("INSERT INTO t DEFAULT VALUES")

    conn.checkpoint()

    assert conn.scalar("SELECT COUNT(*) FROM t") == 1


def test_integrity_check_passes_on_a_fresh_database(conn: SqliteConnection) -> None:
    conn.integrity_check()  # 不抛异常即通过


def test_foreign_key_violations_are_empty(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))")

    assert conn.foreign_key_violations() == []


# ─────────────────────────────────────────────────────────────
# 6. 备份
# ─────────────────────────────────────────────────────────────


def test_backup_produces_an_independently_openable_copy(
    conn: SqliteConnection, tmp_path: Path
) -> None:
    """备份必须能被**独立打开**——能打开且表内容一致才算真的备份。"""
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    with conn.transaction():
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        conn.execute("INSERT INTO t (value) VALUES ('b')")

    target = conn.backup_to(tmp_path / "backup.db")

    assert target.exists()
    with SqliteConnection.open(target, readonly=True) as restored:
        assert restored.scalar("SELECT COUNT(*) FROM t") == 2
        assert restored.query("SELECT value FROM t ORDER BY id")[1]["value"] == "b"


def test_backup_copies_the_schema_not_just_the_data(conn: SqliteConnection, tmp_path: Path) -> None:
    conn.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")

    with SqliteConnection.open(conn.backup_to(tmp_path / "b.db"), readonly=True) as restored:
        names = {row["name"] for row in restored.query("SELECT name FROM sqlite_master")}

    assert "kept" in names


def test_backup_overwrites_an_existing_file(conn: SqliteConnection, tmp_path: Path) -> None:
    """VACUUM INTO 拒绝覆盖已存在的文件，连接层要先删掉它。"""
    stale = tmp_path / "stale.db"
    stale.write_bytes(b"not a database")

    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.backup_to(stale)

    with SqliteConnection.open(stale, readonly=True) as restored:
        assert restored.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 't'") == 1


def test_backup_creates_missing_destination_directory(
    conn: SqliteConnection, tmp_path: Path
) -> None:
    target = conn.backup_to(tmp_path / "deep" / "down" / "backup.db")

    assert target.exists()


def test_backup_is_refused_inside_a_transaction(conn: SqliteConnection, tmp_path: Path) -> None:
    """VACUUM INTO 不能进事务。挡在这里，好过让 SQLite 抛一句没人看得懂的错。"""
    with conn.transaction(), pytest.raises(StorageError, match="不能在事务内执行"):
        conn.backup_to(tmp_path / "nope.db")


# ─────────────────────────────────────────────────────────────
# 7. 只读与内存库
# ─────────────────────────────────────────────────────────────


def test_readonly_open_does_not_create_a_missing_file(tmp_path: Path) -> None:
    """只读打开一个不存在的路径必须失败。

    如果退化成「建一个空库」，``alterego db status`` 会对着一个空库
    报告「schema 版本 0」，把「文件丢了」伪装成「还没迁移」。
    """
    missing = tmp_path / "gone.db"

    with pytest.raises(StorageError) as caught:
        SqliteConnection.open(missing, readonly=True)

    assert not missing.exists()
    assert caught.value.context["db_path"] == str(missing)


def test_readonly_open_rejects_writes(conn: SqliteConnection, tmp_path: Path) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    snapshot = conn.backup_to(tmp_path / "ro.db")

    with SqliteConnection.open(snapshot, readonly=True) as readonly:
        assert readonly.scalar("SELECT COUNT(*) FROM t") == 0
        with pytest.raises(StorageError):
            readonly.execute("INSERT INTO t DEFAULT VALUES")


def test_memory_database_opens_without_touching_the_disk() -> None:
    with SqliteConnection.open(":memory:") as opened:
        opened.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        opened.execute("INSERT INTO t DEFAULT VALUES")

        assert opened.scalar("SELECT COUNT(*) FROM t") == 1


def test_memory_database_keeps_memory_journal_mode() -> None:
    """内存库不支持 WAL，SQLite 会保留 ``memory`` 而不是报错——确认我们不会因此炸。"""
    with SqliteConnection.open(":memory:") as opened:
        assert _pragma(opened, "journal_mode") == "memory"


# ─────────────────────────────────────────────────────────────
# 8. 行工厂与关闭
# ─────────────────────────────────────────────────────────────


def test_rows_are_addressable_by_column_name(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO t (value) VALUES ('hello')")

    row = conn.query_one("SELECT id, value FROM t")

    assert row is not None
    assert row["value"] == "hello"
    assert isinstance(row, sqlite3.Row)


def test_scalar_returns_none_for_no_rows(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    assert conn.scalar("SELECT id FROM t") is None


def test_query_returns_empty_list_for_no_rows(conn: SqliteConnection) -> None:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    assert conn.query("SELECT id FROM t") == []


def test_close_is_idempotent(db_path: Path) -> None:
    opened = SqliteConnection.open(db_path)
    opened.close()
    opened.close()

    assert db_path.exists()


def test_context_manager_closes_the_connection(db_path: Path) -> None:
    with SqliteConnection.open(db_path) as opened:
        opened.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(StorageError):
        opened.execute("SELECT * FROM t")

"""真实迁移文件的测试。

``tests/test_storage_migrator.py`` 测的是迁移**机器**（用临时目录里的玩具迁移）；
这里测的是**本产品交付的 schema**：那四个 ``.sql`` 文件跑完之后，
数据库到底长什么样、约束到底灵不灵。

断言全部对着字面清单，不写成 ``>= 20`` 之类的含糊比较——
「表少了一张」是必须立刻失败的事故，而不是一个可以容忍的浮点数误差。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alterego.kernel.errors import IntegrityError, MigrationError
from alterego.storage.sqlite.backend import SqliteStorageBackend
from alterego.storage.sqlite.migrator import MIGRATIONS_DIR, discover_migrations


NOW = "2026-09-15T10:00:00+08:00"

#: 四个迁移文件建出来的全部实体表。
#:
#: 计数口径：设计文档 § 3 说「表共 26 张」，那 26 个编号里包含 ``memory_fts``
#: （第 8 个，虚拟表），但不含 ``schema_version``（迁移元数据，不挂在 persona 上）。
#: 所以这里一共 27 个名字。与其去争论「算不算 26 张」，不如把名字写全——
#: 名字写全了，多一张少一张都藏不住。
EXPECTED_TABLES = frozenset(
    {
        "schema_version",
        "persona",
        "persona_version",
        "world",
        "npc",
        "relationship",
        "emotion_log",
        "memory",
        "memory_fts",
        "conversation",
        "message",
        "social_post",
        "post_interaction",
        "schedule_block",
        "activity_log",
        "tick_log",
        "llm_usage",
        "plugin_state",
        "plugin_meta",
        "event_log",
        "budget_usage",
        "media_asset",
        "media_usage",
        "source_feed",
        "source_query",
        "source_item",
        "log_entry",
    }
)

#: SQLite 自己的记账表，不是设计的一部分。
#:
#: ``sqlite_sequence`` 是因为 ``log_entry`` 用了 ``INTEGER PRIMARY KEY AUTOINCREMENT``：
#: 日志表会被保留策略删行，而 ``AUTOINCREMENT`` 正是保证「删掉的行号不会被新日志复用」
#: 的那一句。日志页面的链结能一直有效，靠的就是这个。
SQLITE_INTERNAL_TABLES = frozenset({"sqlite_sequence"})

#: FTS5 给 `memory_fts` 自动建的影子表。它们不是设计的一部分，
#: 但会出现在 ``sqlite_master`` 里，所以必须显式列出来——
#: 否则「表数量对不上」时，排查的人会先怀疑自己数错了。
FTS_SHADOW_TABLES = frozenset(
    {
        "memory_fts_data",
        "memory_fts_idx",
        "memory_fts_content",
        "memory_fts_docsize",
        "memory_fts_config",
    }
)

EXPECTED_VIEWS = frozenset({"v_cost_daily", "v_trace"})

MIGRATION_FILES = (
    "001_initial.sql",
    "002_media.sql",
    "003_sources.sql",
    "004_observability.sql",
    "005_memory_consolidation.sql",
)

#: 随包迁移能升到的最高版本。从清单推导，加一个迁移就不用再改一遍这里的断言。
REQUIRED_VERSION = len(MIGRATION_FILES)


def _names(backend: SqliteStorageBackend, kind: str) -> set[str]:
    rows = backend.connection.query("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {row["name"] for row in rows}


def _design_tables(backend: SqliteStorageBackend) -> set[str]:
    """只要设计里写出来的表——去掉 FTS5 影子表和 SQLite 自己的记账表。"""
    return _names(backend, "table") - FTS_SHADOW_TABLES - SQLITE_INTERNAL_TABLES


@pytest.fixture
def backend(tmp_path: Path) -> SqliteStorageBackend:
    """打开一个库并把真实迁移跑到最新。"""
    opened = SqliteStorageBackend.open(tmp_path / "alterego.db")
    try:
        opened.migrate()
        yield opened
    finally:
        opened.close()


def _insert_persona(backend: SqliteStorageBackend, persona_id: str = "p1") -> None:
    backend.connection.execute(
        "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (persona_id, "小我", "{}", NOW, NOW),
    )


# ─────────────────────────────────────────────────────────────
# 1. 交付的迁移文件本身
# ─────────────────────────────────────────────────────────────


def test_the_real_migrations_are_discoverable() -> None:
    """头部四项必须齐全、文件名必须合规、版本号必须连号。

    这条测试顺带证明了「文件里没有事务控制语句」——
    发现阶段会扫一遍，扫到就抛。
    """
    migrations = discover_migrations(MIGRATIONS_DIR)

    assert [item.version for item in migrations] == list(range(1, REQUIRED_VERSION + 1))
    assert [item.path.name for item in migrations] == list(MIGRATION_FILES)
    assert all(item.description for item in migrations)
    # 本批次四个迁移都是纯新增，没有一处删表删列。
    # 将来出现 destructive=True 时这条会失败，提醒你「备份对话框要接上了」。
    assert all(not item.destructive for item in migrations)
    assert all(item.reversible for item in migrations)


@pytest.mark.parametrize("filename", MIGRATION_FILES)
def test_migration_files_leave_bookkeeping_to_the_migrator(filename: str) -> None:
    """四个文件里不许出现 PRAGMA、事务控制、版本记账与 IF NOT EXISTS。

    这是对「约定」的源码级断言：它会在有人把 ``03-data-model.md`` 里的
    完整 DDL 原样复制过来时立刻失败——那份 DDL 带着 PRAGMA 头、
    带着 ``INSERT INTO schema_version``，也带着满地的 ``IF NOT EXISTS``。
    """
    text = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    # 注释里可以**讨论** PRAGMA（文件头就在讨论），代码里不行。
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    upper = code.upper()

    for token in ("PRAGMA", "COMMIT", "ROLLBACK", "IF NOT EXISTS"):
        assert token not in upper, f"{filename} 的 SQL 里不该出现 {token}"

    # 版本记账由迁移器在同一个事务里完成。这里查的是**写语句**而不是表名——
    # 001 当然得建 schema_version，但任何文件都不该往里面写。
    assert "USER_VERSION" not in upper
    for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
        assert f"{verb} SCHEMA_VERSION" not in upper, (
            f"{filename} 不该自己记 schema_version；迁移器会在事务内补上。"
            "文件自己记就会出现「有的文件写了、有的忘了」，"
            "而忘掉的那次会留下「表建好了、版本号没涨」的状态"
        )


# ─────────────────────────────────────────────────────────────
# 2. 结构（26 张表 + 2 个视图）
# ─────────────────────────────────────────────────────────────


def test_schema_has_exactly_the_expected_tables(backend: SqliteStorageBackend) -> None:
    actual = _design_tables(backend)

    assert actual == EXPECTED_TABLES, (
        f"多出来的: {sorted(actual - EXPECTED_TABLES)} 少掉的: {sorted(EXPECTED_TABLES - actual)}"
    )


def test_fts_shadow_tables_are_the_expected_five(backend: SqliteStorageBackend) -> None:
    assert _names(backend, "table") & FTS_SHADOW_TABLES == FTS_SHADOW_TABLES


def test_schema_has_exactly_the_expected_views(backend: SqliteStorageBackend) -> None:
    assert _names(backend, "view") == EXPECTED_VIEWS


def test_observability_columns_were_added_by_the_alter_migration(
    backend: SqliteStorageBackend,
) -> None:
    """`tick_log` / `activity_log` / `llm_usage` 的 ``correlation_id`` 来自 004。

    这三条 ``ALTER TABLE`` 是全部文件里唯一**不可能幂等**的语句，
    所以它们跑没跑过必须能被验证。
    """
    for table in ("tick_log", "activity_log", "llm_usage"):
        columns = {row["name"] for row in backend.connection.query(f"PRAGMA table_info({table})")}

        assert "correlation_id" in columns, f"{table}.correlation_id 没被加上"


def test_migrations_run_from_scratch_without_the_pragma_header(
    backend: SqliteStorageBackend,
) -> None:
    """跑完四个迁移，库直接可用——PRAGMA 由连接层设，不必在文件里设。

    ``journal_mode`` 是唯一一个「设完能读回来」且必须在事务前设的值，
    所以拿它当代表。
    """
    assert backend.connection.query_one("PRAGMA journal_mode")[0] == "wal"
    assert backend.connection.query_one("PRAGMA foreign_keys")[0] == 1
    assert backend.current_schema_version == REQUIRED_VERSION


# ─────────────────────────────────────────────────────────────
# 3. 约束真的生效
# ─────────────────────────────────────────────────────────────


def test_foreign_keys_are_enforced_on_insert(backend: SqliteStorageBackend) -> None:
    with pytest.raises(IntegrityError):
        backend.connection.execute(
            "INSERT INTO world (id, persona_id, setting, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("w-orphan", "no-such-persona", "设定", NOW, NOW),
        )


def test_cascade_delete_removes_the_whole_world(backend: SqliteStorageBackend) -> None:
    conn = backend.connection
    _insert_persona(backend)
    conn.execute(
        "INSERT INTO world (id, persona_id, setting, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("w1", "p1", "设定", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO npc (id, world_id, name, relation, profile_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("n1", "w1", "老王", "friend", "{}", NOW, NOW),
    )

    conn.execute("DELETE FROM persona WHERE id = ?", ("p1",))

    assert conn.scalar("SELECT COUNT(*) FROM world") == 0
    assert conn.scalar("SELECT COUNT(*) FROM npc") == 0


def test_json_columns_reject_invalid_json(backend: SqliteStorageBackend) -> None:
    """``json_valid`` 是数据库层的最后一道闸。

    领域层当然也会校验，但一个写错的 repository 不该能把坏数据塞进去——
    等到下次读出来才发现时，已经不知道是谁写坏的了。
    """
    with pytest.raises(IntegrityError):
        backend.connection.execute(
            "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("p1", "小我", "{不是 json", NOW, NOW),
        )


def test_json_defaults_are_valid_on_their_own(backend: SqliteStorageBackend) -> None:
    """默认值 ``'[]'`` / ``'{}'`` 各自必须能过自己的 ``CHECK``。

    ``DEFAULT '[]'`` 配 ``CHECK (json_valid(x))`` 看着天经地义，
    但把两者写反（数组列给了 ``'{}'``）语法完全合法、靠 ``json_valid`` 也拦不住，
    两种值都是合法 JSON。所以只能靠这条测试逐列确认。
    """
    _insert_persona(backend)
    conn = backend.connection
    conn.execute(
        "INSERT INTO world (id, persona_id, setting, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("w1", "p1", "设定", NOW, NOW),
    )

    row = conn.query_one("SELECT locations_json, events_json, weather_json FROM world")
    assert row is not None
    assert row["locations_json"] == "[]"
    assert row["events_json"] == "[]"
    assert row["weather_json"] == "{}"


def test_canonical_media_is_unique_per_persona(backend: SqliteStorageBackend) -> None:
    """定妆照全库唯一——这是角色形象一致性的前提，靠部分唯一索引保证。"""
    _insert_persona(backend)
    conn = backend.connection

    def add(asset_id: str, role: str) -> None:
        conn.execute(
            "INSERT INTO media_asset (id, persona_id, role, file_path, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, "p1", role, f"media/{asset_id}.png", NOW, NOW),
        )

    add("a1", "canonical")

    with pytest.raises(IntegrityError):
        add("a2", "canonical")

    # 但自拍可以有第二张——部分唯一索引只约束 canonical，
    # 换成表级 UNIQUE(persona_id, role) 就会把这条也挡掉。
    add("a3", "selfie")
    add("a4", "selfie")

    assert conn.scalar("SELECT COUNT(*) FROM media_asset") == 3


def test_role_enum_is_enforced_by_a_check(backend: SqliteStorageBackend) -> None:
    _insert_persona(backend)

    with pytest.raises(IntegrityError):
        backend.connection.execute(
            "INSERT INTO media_asset (id, persona_id, role, file_path, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("a1", "p1", "not_a_role", "media/a1.png", NOW, NOW),
        )


def test_source_item_can_reference_both_parent_tables(backend: SqliteStorageBackend) -> None:
    """`003` 里把 source_feed / source_query 排到 source_item 前面就是为了这个。

    外键不能指向还不存在的表，所以三个表的建表顺序不能照抄设计文档里的编号顺序。
    """
    conn = backend.connection
    conn.execute(
        "INSERT INTO source_feed (id, url, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("f1", "https://example.com/feed.xml", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO source_query (id, query_text, created_at) VALUES (?, ?, ?)",
        ("q1", "咖啡", NOW),
    )
    conn.execute(
        "INSERT INTO source_item (id, query_id, feed_id, url, url_hash, source_kind, fetched_at,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", "q1", "f1", "https://example.com/a", "deadbeef", "feed", NOW, NOW),
    )

    # source_feed 是 SET NULL，source_query 是 CASCADE：
    # 源没了条目还能留着（它记的是「读过什么」），检索记录没了条目就不完整了。
    conn.execute("DELETE FROM source_feed WHERE id = ?", ("f1",))
    assert conn.scalar("SELECT feed_id FROM source_item WHERE id = 's1'") is None

    conn.execute("DELETE FROM source_query WHERE id = ?", ("q1",))
    assert conn.scalar("SELECT COUNT(*) FROM source_item") == 0


def test_source_items_are_deduplicated_by_url_hash(backend: SqliteStorageBackend) -> None:
    conn = backend.connection
    sql = (
        "INSERT INTO source_item (id, url, url_hash, fetched_at, created_at) VALUES (?, ?, ?, ?, ?)"
    )
    conn.execute(sql, ("s1", "https://example.com/a", "same", NOW, NOW))

    with pytest.raises(IntegrityError):
        conn.execute(sql, ("s2", "https://example.com/a?utm=x", "same", NOW, NOW))


# ─────────────────────────────────────────────────────────────
# 4. 全文索引
# ─────────────────────────────────────────────────────────────


def _add_memory(backend: SqliteStorageBackend, content: str, summary: str) -> None:
    backend.connection.execute(
        "INSERT INTO memory (id, persona_id, kind, content, summary, occurred_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("m1", "p1", "episodic", content, summary, NOW, NOW),
    )


def test_fts_triggers_follow_insert_update_and_delete(backend: SqliteStorageBackend) -> None:
    """三个触发器一起保证索引与主表不漂移。

    词汇都用空格隔开：FTS5 里的「词」由分词器给，见下一条测试。
    """
    _insert_persona(backend)
    conn = backend.connection

    _add_memory(backend, "楼下 喝了 咖啡", "喝了 咖啡")
    hits = conn.query("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("咖啡",))
    assert [row["memory_id"] for row in hits] == ["m1"]

    conn.execute(
        "UPDATE memory SET content = ?, summary = ? WHERE id = ?", ("改成 喝茶", "喝茶", "m1")
    )
    assert conn.query("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("咖啡",)) == []
    assert (
        len(conn.query("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("喝茶",))) == 1
    )

    conn.execute("DELETE FROM memory WHERE id = ?", ("m1",))
    assert conn.scalar("SELECT COUNT(*) FROM memory_fts") == 0


def test_fts_does_not_segment_unspaced_chinese(backend: SqliteStorageBackend) -> None:
    """不分词的汉字串在 FTS5 里是**一个** token，所以「喝了咖啡」搜不到「咖啡」。

    这不是配置错误，是 ``unicode61`` 的既有行为，也正是
    ``03-data-model.md § 6.4`` 要求写入前用 jieba 切词的原因。
    把这条事实钉进测试，是为了让后来者先查分词、而不是先怀疑索引坏了。
    """
    _insert_persona(backend)
    conn = backend.connection

    _add_memory(backend, "楼下喝了咖啡", "喝了咖啡")

    assert conn.query("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("咖啡",)) == []
    assert (
        len(
            conn.query(
                "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("楼下喝了咖啡",)
            )
        )
        == 1
    )


# ─────────────────────────────────────────────────────────────
# 5. 视图
# ─────────────────────────────────────────────────────────────


def test_cost_view_merges_llm_and_image_spend(backend: SqliteStorageBackend) -> None:
    """两个来源必须都能在同一个视图里看到——否则成本页要写两套聚合。"""
    conn = backend.connection
    conn.execute(
        "INSERT INTO llm_usage (id, purpose, tier, provider_id, model, total_tokens, cost_usd,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u1", "chat", "cheap", "openai_compatible", "gpt-x", 100, 0.01, NOW),
    )
    conn.execute(
        "INSERT INTO media_usage (id, purpose, provider_id, model, cost_usd, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("u2", "selfie", "openai_compatible", "dall-e", 0.04, NOW),
    )
    # 失败的调用不该进成本视图——不然「重试三次」会变成三倍花费。
    conn.execute(
        "INSERT INTO llm_usage (id, purpose, tier, provider_id, model, cost_usd, success,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u3", "chat", "cheap", "openai_compatible", "gpt-x", 0.99, 0, NOW),
    )

    rows = conn.query("SELECT cost_kind, cost_usd FROM v_cost_daily ORDER BY cost_kind")

    assert [(row["cost_kind"], row["cost_usd"]) for row in rows] == [("llm", 0.01), ("media", 0.04)]


def test_trace_view_links_rows_by_correlation_id(backend: SqliteStorageBackend) -> None:
    """六路来源都靠 correlation_id 归到同一条时间线上。"""
    conn = backend.connection
    _insert_persona(backend)
    conn.execute(
        "INSERT INTO tick_log (id, persona_id, virtual_time, real_duration_ms, status,"
        " correlation_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("t1", "p1", NOW, 42, "ok", "corr-1", NOW),
    )
    conn.execute(
        "INSERT INTO event_log (id, topic, source, correlation_id, occurred_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("e1", "tick.started", "sim", "corr-1", NOW),
    )
    conn.execute(
        "INSERT INTO llm_usage (id, purpose, tier, provider_id, model, correlation_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("u1", "chat", "cheap", "openai_compatible", "gpt-x", "corr-1", NOW),
    )
    # 没有 correlation_id 的行不进链路——它们是「没被归入任何一次推演」的噪声。
    conn.execute(
        "INSERT INTO llm_usage (id, purpose, tier, provider_id, model, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("u2", "chat", "cheap", "openai_compatible", "gpt-x", NOW),
    )

    rows = conn.query("SELECT step, tick_id, payload_json FROM v_trace ORDER BY step")

    assert [row["step"] for row in rows] == ["event", "llm", "tick"]
    # tick 那一路自己就是锚点，所以它的 tick_id 是它自己的 id。
    assert next(row for row in rows if row["step"] == "tick")["tick_id"] == "t1"
    # event 那一路没有 tick_id（event_log 就没这列），但带着可重放的 payload。
    event_row = next(row for row in rows if row["step"] == "event")
    assert event_row["tick_id"] is None
    assert event_row["payload_json"] == "{}"


# ─────────────────────────────────────────────────────────────
# 6. 失败时不留半截
# ─────────────────────────────────────────────────────────────


def test_a_failing_tail_statement_leaves_nothing_behind(tmp_path: Path) -> None:
    """最后一个文件最后一句炸了，前面几次的成果也必须原样。

    真实文件整份复制过来，只在末尾追加一个坏掉的新迁移——
    「迁移是原子的」这件事只有拿真 schema 跑过才算数。
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in MIGRATION_FILES:
        source = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        (directory / name).write_text(source, encoding="utf-8")

    version = REQUIRED_VERSION + 1
    (directory / f"{version:03d}_boom.sql").write_text(
        f"-- migration: {version:03d}\n"
        "-- description: 最后一句故意失败\n"
        "-- destructive: false\n"
        "-- reversible: true\n"
        "\n"
        "CREATE TABLE should_not_survive (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO no_such_table (id) VALUES (1);\n",
        encoding="utf-8",
    )

    with SqliteStorageBackend.open(tmp_path / "db.sqlite", migrations_dir=directory) as opened:
        with pytest.raises(MigrationError, match="迁移执行失败，已回滚"):
            opened.migrate()

        assert opened.current_schema_version == REQUIRED_VERSION
        assert "should_not_survive" not in _names(opened, "table")
        assert opened.connection.raw.in_transaction is False

        # 而且库还能继续用——回滚没把连接搞坏。
        assert _design_tables(opened) == EXPECTED_TABLES

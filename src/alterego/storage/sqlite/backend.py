"""``StorageBackend`` 的 SQLite 实现。

这是**唯一**把连接、迁移、PRAGMA 拼到一起的地方，也是其他层唯一能见到的东西：
``storage.sqlite`` 之外的代码一律通过 ``StorageBackend`` Protocol 访问，
不得 ``import alterego.storage.sqlite``（由 ``scripts/check_architecture.sh`` 第 3 组红线保证）。

两条刻意的设计选择：

1. **打开不等于迁移**。``open()`` / ``from_config()`` 只检查版本兼容性，
   绝不顺手改库结构。「连一下数据库就把它的表改了」是最不该存在的副作用，
   而且它会在备份没做成的时候发生。迁移必须显式触发（``migrate()``
   或 ``alterego db migrate``）。
2. **版本兼容是双向的**。库比程序**新**要拒绝（旧程序读不懂新结构，
   继续写会把数据写坏）；库比 ``MIN_COMPATIBLE_VERSION`` 还**旧**也要拒绝
   （老结构缺列，查询会以「字段不存在」的形式在业务代码里炸开，
   而不是在这里给出「该迁移了」这句话）。

依据: docs/design/03-data-model.md § 8.4 / § 8.5
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from alterego.interfaces.storage import StorageBackend
from alterego.kernel.config import StorageConfig
from alterego.kernel.errors import StorageError
from alterego.kernel.logging import get_logger
from alterego.storage.sqlite.connection import PragmaSettings, SqliteConnection
from alterego.storage.sqlite.migrator import MIGRATIONS_DIR, Migration, MigrationPlan, Migrator


__all__ = ["MIN_COMPATIBLE_VERSION", "SqliteStorageBackend"]


_log = get_logger("storage.sqlite.backend")

#: 本程序能读的最老库版本。
#:
#: 当某次迁移删了列、改了语义，老库就不能再被直接读了——那时把这个数字抬上去。
#: 它是一句**政策声明**，不能从迁移目录推导：目录只知道「有哪些迁移」，
#: 不知道「哪一次开始不兼容」。
MIN_COMPATIBLE_VERSION: Final[int] = 1

#: 备份文件的默认子目录名，落在库文件旁边。
_BACKUP_DIRNAME: Final[str] = "backups"


class SqliteStorageBackend:
    """SQLite 存储后端。

    不是 ``Plugin`` 的子类：本批次只做存储地基，把「插件包装」
    （``plugin.toml`` + 入口点 + ``api_version``）留到 Repository 落地时一起做，
    免得现在就定下一份没人用的清单。
    """

    __slots__ = ("_backup_dir", "_conn", "_migrator", "_min_compatible_version")

    def __init__(
        self,
        conn: SqliteConnection,
        *,
        migrations: tuple[Migration, ...] | None = None,
        migrations_dir: Path = MIGRATIONS_DIR,
        backup_dir: Path | None = None,
        min_compatible_version: int = MIN_COMPATIBLE_VERSION,
    ) -> None:
        """Args:
        conn: 已打开的连接。
        migrations: 直接给定迁移序列（测试与工具用）。省略则从 ``migrations_dir`` 发现。
        migrations_dir: 迁移目录。
        backup_dir: 备份目录。为 ``None`` 时拒绝执行破坏性迁移。
        min_compatible_version: 低于此版本的库直接拒绝打开。
        """
        self._conn = conn
        self._min_compatible_version = min_compatible_version
        self._backup_dir = backup_dir
        self._migrator = Migrator(
            conn,
            migrations=migrations,
            migrations_dir=migrations_dir,
            backup_dir=backup_dir,
        )

    # ── 构造 ────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        db_path: Path | str,
        *,
        settings: PragmaSettings | None = None,
        read_only: bool = False,
        migrations: tuple[Migration, ...] | None = None,
        migrations_dir: Path = MIGRATIONS_DIR,
        backup_dir: Path | None = None,
        min_compatible_version: int = MIN_COMPATIBLE_VERSION,
        checkpoint_on_start: bool = False,
        integrity_check_on_start: bool = False,
    ) -> SqliteStorageBackend:
        """打开（必要时创建）一个库，并检查版本兼容性。

        Args:
            db_path: 库文件路径。
            settings: PRAGMA 设置。
            read_only: 只读打开（查历史备份、``db status``）。
            migrations: 直接给定迁移序列（测试与工具用）。
            migrations_dir: 迁移目录。
            backup_dir: 备份目录。``None`` 的含义**只有**一个：没有备份目录，
                于是破坏性迁移会被拒绝。想备份就明确传一个路径，
                或走 :meth:`from_config` 让配置决定。这里刻意不派生默认值——
                同一个 ``None`` 在两个入口含义不同，是「明明传了 None 却有了备份
                目录」这类事故的温床。
            min_compatible_version: 版本下限。
            checkpoint_on_start: 启动时跑一次 ``wal_checkpoint(TRUNCATE)``。
            integrity_check_on_start: 启动时跑一次 ``integrity_check``。

        Raises:
            StorageError: 打不开、版本过高、版本过旧，或启动自检失败。
        """
        target = Path(db_path)
        conn = SqliteConnection.open(target, settings=settings, readonly=read_only)
        try:
            if not read_only and checkpoint_on_start:
                conn.checkpoint()
            if not read_only and integrity_check_on_start:
                conn.integrity_check()

            backend = cls(
                conn,
                migrations=migrations,
                migrations_dir=migrations_dir,
                backup_dir=backup_dir,
                min_compatible_version=min_compatible_version,
            )
            backend._ensure_compatible()
        except BaseException:
            # 构造到一半失败必须把连接关掉：WAL 模式下的连接会一直握着
            # ``-wal`` 与 ``-shm`` 两个文件，泄漏几次之后删除库文件都会失败。
            conn.close()
            raise
        return backend

    @classmethod
    def from_config(
        cls,
        config: StorageConfig,
        *,
        migrations: tuple[Migration, ...] | None = None,
        migrations_dir: Path = MIGRATIONS_DIR,
        backup_dir: Path | None = None,
    ) -> SqliteStorageBackend:
        """按 ``[storage]`` 配置段打开后端。

        默认备份目录落在库文件旁边的 ``backups/``（见 ``StorageConfig``）。

        ``backup_before_destructive_migration = false`` 的效果是
        **把破坏性迁移变成一次硬失败**，而不是「那就别备份了」：
        迁移器在没有备份目录时会拒绝执行。想跑破坏性迁移就必须给退路。
        """
        if not config.backup_before_destructive_migration:
            # 明确「没有退路」：不派生默认目录，让迁移器在破坏性迁移前硬失败。
            effective_backup_dir: Path | None = None
        else:
            effective_backup_dir = backup_dir or Path(config.db_path).parent / _BACKUP_DIRNAME

        return cls.open(
            config.db_path,
            settings=PragmaSettings(
                journal_mode=config.journal_mode,
                synchronous=config.synchronous,
                busy_timeout_ms=config.busy_timeout_ms,
                foreign_keys=config.foreign_keys,
                temp_store=config.temp_store,
            ),
            migrations=migrations,
            migrations_dir=migrations_dir,
            backup_dir=effective_backup_dir,
            checkpoint_on_start=config.checkpoint_on_start,
            integrity_check_on_start=config.integrity_check_on_start,
        )

    # ── 版本 ────────────────────────────────────────────────

    @property
    def current_schema_version(self) -> int:
        """库当前的 ``schema_version``。"""
        return self._conn.user_version()

    @property
    def required_schema_version(self) -> int:
        """本程序期望的 ``schema_version``。

        从迁移目录**推导**而不是写死：写死的常量会在加了 ``005_*.sql``
        之后忘记同步，而那种偏差没有任何症状——直到某天程序看到一个
        自己其实能处理的版本号，却报了「版本过高」。
        """
        return self._migrator.target_version

    @property
    def min_compatible_version(self) -> int:
        return self._min_compatible_version

    def plan(self) -> MigrationPlan:
        """当前版本与目标版本的差异，供 ``--dry-run`` 与启动日志使用。"""
        return self._migrator.plan()

    def _ensure_compatible(self) -> None:
        """双向版本检查。

        版本 0 是「全新的空库」，不是错误——它只是还没迁移过。
        """
        current = self.current_schema_version
        required = self.required_schema_version

        if current > required:
            raise StorageError(
                "数据库版本高于当前程序支持的版本",
                schema_version=current,
                supported=required,
                db_path=self._conn.path,
                hint="请升级 alterego 到最新版本，或从备份恢复",
            )

        if 0 < current < self._min_compatible_version:
            raise StorageError(
                "数据库版本过旧，无法安全读取",
                schema_version=current,
                min_compatible_version=self._min_compatible_version,
                db_path=self._conn.path,
                hint="运行 alterego db migrate",
            )

    # ── StorageBackend 契约 ─────────────────────────────────

    def migrate(self) -> str:
        """把结构升到最新版本，返回应用后的 ``schema_version``。"""
        self._migrator.migrate()
        # 回读连接里的真实值，而不是相信计划里的目标值：
        # 契约承诺的是「应用后是什么」，不是「我们打算改成什么」。
        return str(self.current_schema_version)

    def transaction(self) -> AbstractContextManager[None]:
        """开一个事务，可嵌套（内层加入外层）。"""
        return self._conn.transaction()

    def flush(self) -> None:
        """把已提交的写入从 WAL 搬回主库文件。"""
        self._conn.flush()

    def checkpoint(self) -> None:
        """收尾用：合并 WAL 并把它截断回 0 字节。"""
        self._conn.checkpoint()

    def close(self) -> None:
        """关闭后端。重复调用是安全的。"""
        self._conn.close()

    # ── SQLite 特有 ─────────────────────────────────────────

    @property
    def connection(self) -> SqliteConnection:
        """底层连接。供 ``db status`` 这类需要直接查库的维护命令使用。"""
        return self._conn

    @property
    def db_path(self) -> Path:
        """库文件路径。"""
        return Path(self._conn.path)

    def backup(self, destination: Path | None = None) -> Path:
        """导出一致性快照。

        Args:
            destination: 目标文件。省略则落在 ``backups/alterego-<时间戳>.db``。

        Raises:
            StorageError: 处于事务中（``VACUUM INTO`` 的限制）或写入失败。
        """
        if destination is None:
            if self._backup_dir is None:
                raise StorageError("未配置备份目录", hint="构造时传入 backup_dir")
            stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
            destination = Path(self._backup_dir) / f"alterego-{stamp}.db"
        return self._conn.backup_to(destination)

    def integrity_check(self) -> None:
        """跑一遍 ``PRAGMA integrity_check``；不通过抛 ``IntegrityError``。"""
        self._conn.integrity_check()

    def optimize(self) -> None:
        """跑 ``PRAGMA optimize``（SQLite 3.18+）。

        它会按需 ANALYZE 那些变化较大的索引。放在关闭前跑一次即可，
        代价很低而查询计划会明显变好。
        """
        self._conn.execute("PRAGMA optimize")

    def __enter__(self) -> SqliteStorageBackend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()


def _assert_contract(_: type[StorageBackend]) -> None:
    """只有在类型检查期才会被求值：让 mypy 验证本类真的满足 ``StorageBackend``。

    运行期永远不会调用它。没有这一行的话，「实现了协议」这件事要等到
    第一批 Repository 接上来才会第一次被检查，而那时报错的位置离出错的
    地方已经很远了。``storage/`` 目前没有别的办法在静态期建立这个连接。
    """
    raise AssertionError("仅用于类型检查，不应被调用")


if TYPE_CHECKING:
    _assert_contract(SqliteStorageBackend)

"""SQLite 连接、PRAGMA 与事务。

这一层只解决四件事，其余都不管：

1. **PRAGMA** —— 五个设置项要在**任何事务开始之前**生效。
   ``journal_mode = WAL`` 在事务里改是静默失败的，所以顺序不能反。
2. **事务** —— 必须**可嵌套**。设计文档要求「一个 tick 的所有写入要么一起成功、
   要么一起回滚」，而一个 tick 会依次调用十几个 Repository 方法，
   每个方法内部又想用 ``with transaction():`` 保护自己。
   如果嵌套时每次都真的 ``BEGIN``，第一个内层方法就会把外层的原子性切碎。
3. **版本号** —— ``PRAGMA user_version`` 是 ``schema_version`` 表的**快速路径**：
   读一个整数头字段，不必先建表再查询。
4. **备份** —— ``VACUUM INTO``（SQLite 3.27+）能在不装任何第三方库的情况下
   导出一份一致快照。它**不能在事务里执行**，调用方自己保证这一点。

依据: docs/design/03-data-model.md § 3 / § 8 / § 9.1
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from alterego.kernel.errors import IntegrityError, StorageError
from alterego.kernel.logging import get_logger


__all__ = ["PragmaSettings", "SqliteConnection"]


_log = get_logger("storage.sqlite.connection")

#: 传给 ``execute`` 的参数：位置参数用序列，命名参数用映射。
Params = Sequence[Any] | Mapping[str, Any]

#: ``:memory:`` 是 sqlite3 约定的内存库标识，不是一个文件路径。
MEMORY_PATH: Final[str] = ":memory:"

#: 允许写进 PRAGMA 的取值白名单。
#:
#: PRAGMA 不支持参数绑定，只能拼字符串。取值全部来自 ``StorageConfig``，
#: 但配置可以被用户手改，所以在这里再挡一道——拼字符串的地方都要有白名单。
_LEGAL_PRAGMA_VALUES: Final[Mapping[str, frozenset[str]]] = {
    "journal_mode": frozenset({"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}),
    "synchronous": frozenset({"OFF", "NORMAL", "FULL", "EXTRA"}),
    "temp_store": frozenset({"DEFAULT", "FILE", "MEMORY"}),
}


@dataclass(frozen=True, slots=True)
class PragmaSettings:
    """SQLite 连接级设置。默认值即 ``docs/design/03-data-model.md § 3`` 的约定。"""

    journal_mode: str = "WAL"
    """WAL 让 Web 线程与 tick 线程可以并发读写，是并发模型的前提（01-architecture.md § 7）。"""

    synchronous: str = "NORMAL"
    """WAL 下 ``NORMAL`` 已足够安全：断电最多丢最近几个事务，不会损坏数据库。"""

    busy_timeout_ms: int = 5000
    """写锁被占时先等 5 秒再报错，而不是立刻抛 ``database is locked``。"""

    foreign_keys: bool = True
    """SQLite 默认**不**强制外键。不开这一项，所有 ``REFERENCES`` 都只是注释。"""

    temp_store: str = "MEMORY"
    """排序与临时索引放内存，FTS5 检索受益明显。"""

    def __post_init__(self) -> None:
        for field, legal in _LEGAL_PRAGMA_VALUES.items():
            value = getattr(self, field)
            if value not in legal:
                raise StorageError(
                    f"PRAGMA {field} 的取值非法",
                    setting=field,
                    value=value,
                    allowed=sorted(legal),
                )
        if self.busy_timeout_ms < 0:
            raise StorageError("PRAGMA busy_timeout 不能为负", value=self.busy_timeout_ms)

    def statements(self) -> tuple[str, ...]:
        """生成要按顺序执行的 PRAGMA 语句。"""
        return (
            f"PRAGMA journal_mode = {self.journal_mode}",
            f"PRAGMA synchronous = {self.synchronous}",
            f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}",
            f"PRAGMA foreign_keys = {'ON' if self.foreign_keys else 'OFF'}",
            f"PRAGMA temp_store = {self.temp_store}",
        )


class SqliteConnection:
    """一条 sqlite3 连接，外加事务与版本号管理。

    不继承 ``sqlite3.Connection``：那样会把四十多个方法一起变成公开 API，
    而这里的意图恰恰相反——**只暴露我们想让人用的一小撮**。
    """

    __slots__ = ("_conn", "_depth", "_path", "_settings")

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: PragmaSettings,
        *,
        path: str = MEMORY_PATH,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._path = path
        self._depth = 0

    # ── 构造 ────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        settings: PragmaSettings | None = None,
        readonly: bool = False,
    ) -> SqliteConnection:
        """打开（必要时创建）一个库。

        Args:
            path: 文件路径，或 ``:memory:``。
            settings: PRAGMA 设置，省略则用设计文档的默认值。
            readonly: 以只读方式打开，用于查历史备份。

        Raises:
            StorageError: 路径不可写、文件损坏或权限不足。
        """
        resolved = str(path)
        is_memory = resolved == MEMORY_PATH

        if not readonly and not is_memory:
            try:
                Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(
                    "无法创建数据库所在目录",
                    db_path=resolved,
                    error=str(exc),
                ) from exc

        try:
            if readonly:
                # 只读必须走 URI，否则 connect() 遇到不存在的文件会直接新建一个，
                # 「打开历史备份」就变成了「悄悄造一个空库」。
                #
                # 用 as_uri() 而不是手拼 file:{path}：Windows 上 file:C:/x.db 会被
                # 当成相对路径，必须写成 file:///C:/x.db；中文与空格也要转义。
                uri = f"{Path(resolved).resolve().as_uri()}?mode=ro"
                raw = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                raw = sqlite3.connect(resolved, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StorageError("无法打开数据库", db_path=resolved, error=str(exc)) from exc

        # isolation_level=None → 交给我们自己管事务。
        #
        # 默认模式下 sqlite3 会在第一条 DML 前隐式 BEGIN，但 DDL 不会触发，
        # 于是「建表」和「插数据」落在两个事务里；而 executescript() 又会
        # 先偷偷 COMMIT 掉挂起的事务——迁移的原子性会在没人察觉的地方丢掉。
        raw.isolation_level = None
        raw.row_factory = sqlite3.Row

        instance = cls(raw, settings or PragmaSettings(), path=resolved)
        if not readonly:
            instance._apply_pragmas()
        return instance

    def _apply_pragmas(self) -> None:
        for statement in self._settings.statements():
            try:
                # 必须 fetch 一下：PRAGMA 是「执行时才生效」的语句，
                # 只 execute 不取值，某些驱动版本会把结果丢掉、设置也就不生效。
                self._conn.execute(statement).fetchone()
            except sqlite3.Error as exc:
                raise StorageError("应用 PRAGMA 失败", pragma=statement, error=str(exc)) from exc

    # ── 基础访问 ────────────────────────────────────────────

    @property
    def raw(self) -> sqlite3.Connection:
        """底层连接。仅供必须直接操作 ``sqlite3`` 的场合（如 ``executescript``）。"""
        return self._conn

    @property
    def path(self) -> str:
        """库文件路径（内存库为 ``:memory:``）。

        每个抛出去的 ``StorageError`` 都该能回答「是哪个库出的事」——
        多实例场景下少这一个字段，日志就变成了一堆无从下手的报错。
        """
        return self._path

    @property
    def in_transaction(self) -> bool:
        """当前是否处于事务中（含嵌套）。"""
        return self._depth > 0

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        """执行一条语句，返回游标。"""
        try:
            return self._conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise IntegrityError("违反数据库约束", sql=_first_line(sql), error=str(exc)) from exc
        except sqlite3.Error as exc:
            raise StorageError("SQL 执行失败", sql=_first_line(sql), error=str(exc)) from exc

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        """执行查询并取回全部行。"""
        return list(self.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        """执行查询并取回第一行。"""
        # ``fetchone()`` 在 typeshed 里标成 ``Any``（顶层 ``sqlite3.Connection``
        # 的游标是动态类型的），所以这里显式标注一下，别让 ``Any`` 漏出去。
        row: sqlite3.Row | None = self.execute(sql, params).fetchone()
        return row

    def scalar(self, sql: str, params: Params = ()) -> Any:
        """取回第一行第一列。用于 ``COUNT(*)`` 这类聚合。"""
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # ── 事务 ────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """开一个事务；**嵌套调用只加入外层，不重新开始**。

        用法::

            with conn.transaction():
                memory_repo.save(mem)
                emotion_repo.append(entry)  # 内部也有 with transaction()，合并进同一个

        只在最外层提交；任何一层抛异常都会整体回滚。

        刻意**不 yield 连接对象**：契约 ``StorageBackend.transaction()`` 声明的是
        ``AbstractContextManager[None]``，而 ``AbstractContextManager`` 对所产出的
        类型是协变的——``AbstractContextManager[SqliteConnection]`` 并不是它的子类型。
        多 yield 一个东西出来，就得在契约上开一个洞。
        """
        if self._depth == 0:
            try:
                self._conn.execute("BEGIN")
            except sqlite3.Error as exc:
                raise StorageError("无法开启事务", error=str(exc)) from exc

        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._rollback_safely()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self._commit_safely()

    def _commit_safely(self) -> None:
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StorageError("事务提交失败", error=str(exc)) from exc

    def _rollback_safely(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 连接已关或事务已被 SQLite 自己结束
            # 回滚失败时原始异常仍在向上传播，吞掉这里的次生错误才不会掩盖真正的原因。
            _log.warning("事务回滚失败，连接可能已失效")

    # ── 版本号 ──────────────────────────────────────────────

    def user_version(self) -> int:
        """读 ``PRAGMA user_version``（迁移的快速路径，无需先建表）。"""
        row = self._conn.execute("PRAGMA user_version").fetchone()
        if row is None:  # pragma: no cover - PRAGMA user_version 恒返回一行
            return 0
        return int(row[0])

    def set_user_version(self, version: int) -> None:
        """写 ``PRAGMA user_version``。整数转换是防注入的关键（PRAGMA 不能绑定参数）。"""
        target = int(version)
        if target < 0:
            raise StorageError("schema 版本号不能为负", version=target)
        try:
            self._conn.execute(f"PRAGMA user_version = {target}")
        except sqlite3.Error as exc:
            raise StorageError("写入 user_version 失败", version=target, error=str(exc)) from exc

    # ── 维护 ────────────────────────────────────────────────

    def flush(self) -> None:
        """把挂起的写入落盘。

        事务模式下写入在 ``transaction()`` 退出时就已提交，所以这里不是「提交」，
        而是让提交的数据**从 WAL 文件回到主库文件**：

        - ``PASSIVE`` 不阻塞任何读者与写者，代价小，适合频繁调用
        - ``checkpoint()`` 用 ``TRUNCATE``，会等到读者释放再截断 WAL，代价大

        两者的分工是「日常刷一下」与「关机前收尾」。
        """
        if self._depth > 0:
            # 事务未结束时 WAL 内容还不够格被搬走，等提交后再刷。
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except sqlite3.Error as exc:
            raise StorageError("WAL flush 失败", error=str(exc)) from exc

    def checkpoint(self) -> None:
        """把 WAL 文件内容合并回主库。

        可以很慢，偶尔调用即可。``TRUNCATE`` 顺带把 WAL 文件截回 0 字节，
        否则它会一直占着磁盘。
        """
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            raise StorageError("WAL checkpoint 失败", error=str(exc)) from exc

    def integrity_check(self) -> None:
        """跑一遍 ``PRAGMA integrity_check``，不通过就抛 ``IntegrityError``。

        PRAGMA 本身可能**直接报错**而不是返回一行结果：库文件被截断、
        头部被覆盖、读页失败时，SQLite 抛的是 ``DatabaseError``。
        这种失败同样是「完整性检查没通过」，必须一起包成 ``IntegrityError``——
        否则存储层就会漏出一个裸的 ``sqlite3`` 异常，而调用方按契约只捕获
        ``StorageError``，于是它会一路冒到 CLI 变成 traceback。
        """
        try:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise IntegrityError(
                "数据库完整性检查未通过",
                result=str(exc)[:400],
                sql="PRAGMA integrity_check",
            ) from exc

        result = "unknown" if row is None else str(row[0])
        if result != "ok":
            raise IntegrityError("数据库完整性检查未通过", result=result[:400])

    def foreign_key_violations(self) -> list[sqlite3.Row]:
        """列出当前的外键违规行。开了 ``foreign_keys`` 之后应当恒为空。"""
        try:
            return list(self._conn.execute("PRAGMA foreign_key_check").fetchall())
        except sqlite3.Error as exc:
            raise StorageError(
                "外键检查失败", error=str(exc), sql="PRAGMA foreign_key_check"
            ) from exc

    def backup_to(self, dest: Path) -> Path:
        """用 ``VACUUM INTO`` 导出一份一致快照。

        **不能在事务里执行**——调用方负责在无事务时调用。

        目标文件已存在时**不覆盖**，而是换一个带序号的名字。``VACUUM INTO``
        本身拒绝写一个已存在的文件，早先的做法是「先删掉再写」——但删掉的那一份
        也是一份备份，而自动生成文件名的地方（``Backend.backup()`` 与破坏性迁移前
        的自动备份）只精确到秒：一秒内跑两次就会静默丢掉第一份。

        所以返回的是**实际写出的路径**，调用方不能假定它等于 ``dest``。

        Returns:
            实际写出的文件路径。
        """
        if self._depth > 0:
            raise StorageError("备份不能在事务内执行（VACUUM INTO 的限制）", depth=self._depth)

        target = _free_path(Path(dest))
        target.parent.mkdir(parents=True, exist_ok=True)

        # 这里同样不能绑定参数，改用 sqlite3 的字符串转义。
        literal = target.resolve().as_posix().replace("'", "''")
        try:
            self._conn.execute(f"VACUUM INTO '{literal}'")
        except sqlite3.Error as exc:
            raise StorageError("备份失败", dest=str(target), error=str(exc)) from exc
        return target

    def close(self) -> None:
        """关闭连接。重复调用是安全的。"""
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - 关闭失败极罕见
            raise StorageError("关闭数据库失败", error=str(exc)) from exc

    def __enter__(self) -> SqliteConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()


def _first_line(sql: str) -> str:
    """异常上下文里只放 SQL 的第一行——整段 DDL 塞进去会让日志不可读。"""
    head = sql.strip().splitlines()[0] if sql.strip() else ""
    return head if len(head) <= 120 else head[:117] + "..."


def _free_path(target: Path) -> Path:
    """找一个还没被占用的文件名：``x.db`` → ``x-2.db`` → ``x-3.db``……

    上限 1000 只是止损：真跑到那个数，说明调用方在循环里备份，
    那时候报错比继续往磁盘里灌文件要好。
    """
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise StorageError("备份文件名冲突过多", dest=str(target))

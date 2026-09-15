"""数据库迁移。

三条规矩，都是为了「失败的迁移不留半截状态」：

1. **迁移文件只管 DDL**。它们不写 ``schema_version``，也不写 ``user_version``——
   这两笔账由迁移器在**同一个事务里**记。让文件自己记账的后果是：新加的
   迁移文件忘了写那一句，``schema_version`` 就会变成稀疏的 ``{1, 4}``，
   而没人会发现，因为没有任何一处代码会去数它。
2. **一个迁移一个事务**。SQLite 的 DDL 是事务性的，所以中途报错会整体回滚。
   幂等性靠「版本号 + 原子性」保证，而不是靠 ``PRAGMA table_info`` 预检——
   后者要在每个迁移里写一遍，而且 SQLite 的 ``ALTER TABLE ADD COLUMN``
   压根没有 ``IF NOT EXISTS``，预检逻辑会比 DDL 本身还长。
3. **迁移文件不许自带 ``BEGIN`` / ``COMMIT``**。它们会把迁移器开的事务提前结束，
   于是「原子应用」变成一句口号。

依据: docs/design/03-data-model.md § 8
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from alterego.kernel.errors import MigrationError
from alterego.kernel.logging import get_logger
from alterego.storage.sqlite.connection import SqliteConnection


__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationPlan",
    "Migrator",
    "discover_migrations",
]


_log = get_logger("storage.sqlite.migrator")

#: 内置迁移文件的所在目录。
MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "migrations"

#: 迁移文件命名：``NNN_名称.sql``，NNN 是三位十进制版本号。
_FILENAME = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")

#: 头部元数据行。四项都是**必填**。
_HEADER = re.compile(r"^--\s*(?P<key>[a-z_]+)\s*:\s*(?P<value>.*?)\s*$")

#: 迁移文件里不允许出现的语句：它们会破坏「一个迁移一个事务」。
#:
#: 只匹配行首，且 ``BEGIN`` 必须带修饰词或紧跟分号——触发器体内的
#: ``BEGIN ... END;`` 因此不会被误伤，而触发器体内的 ``BEGIN`` 也本来就不在行首。
#:
#: ``BEGIN\s*;`` 单独成一支而不是并进右边的 ``\b``：``;`` 是非单词字符，
#: 它后面再加 ``\b`` 永远匹配不上，那条规则会变成一条**永不触发**的规则。
_TRANSACTION_CONTROL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:COMMIT\b|ROLLBACK\b|END\s+TRANSACTION\b"
    r"|BEGIN\s*;"
    r"|BEGIN\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE|TRANSACTION)\b)",
    re.IGNORECASE | re.MULTILINE,
)

#: 头部里出现但不认识的键——通常是拼错（``destructive`` 写成 ``destory``）。
_KNOWN_HEADER_KEYS: Final[frozenset[str]] = frozenset(
    {"migration", "description", "destructive", "reversible"}
)


@dataclass(frozen=True, slots=True)
class Migration:
    """一个迁移文件。"""

    version: int
    """版本号。来自文件名，并与头部的 ``-- migration:`` 交叉校验。"""

    name: str
    """文件名里的短名，如 ``media``。用于日志与备份文件名。"""

    description: str
    """一句话说明这次迁移改了什么。会写进 ``schema_version.description``。"""

    destructive: bool
    """是否会丢失数据。为真时，应用前必须先备份。"""

    reversible: bool
    """是否原则上可逆。

    这是**声明性元数据**，不驱动任何代码路径：迁移器只往前走，
    没有 ``db rollback``，唯一的退路是一份 ``VACUUM INTO`` 出来的完整备份。
    它目前只给 ``alterego db status`` 展示用。
    """

    sql: str
    """迁移主体（已去掉头部注释行与首尾空白）。"""

    path: Path
    """文件路径，报错时用。"""

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """``当前版本 → 目标版本`` 的差异。"""

    current: int
    target: int
    pending: tuple[Migration, ...]

    @property
    def needs_migration(self) -> bool:
        return bool(self.pending)

    @property
    def is_downgrade(self) -> bool:
        """当前版本比目录里的最高版本还高——说明装的是旧版程序配新库。"""
        return self.current > self.target

    @property
    def destructive_pending(self) -> tuple[Migration, ...]:
        return tuple(item for item in self.pending if item.destructive)

    def describe(self) -> str:
        """给人看的一行摘要，供 ``alterego db migrate --dry-run`` 输出。"""
        if self.is_downgrade:
            return f"数据库版本 {self.current} 高于程序支持的 {self.target}，需要升级程序"
        if not self.pending:
            return f"已是最新版本（{self.current}），无需迁移"
        steps = "、".join(f"{item.version} {item.description}" for item in self.pending)
        return f"将从 {self.current} 升到 {self.target}：{steps}"


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """读取目录里的全部迁移文件，按版本号升序返回。

    Raises:
        MigrationError: 目录不存在、为空、命名不合规、头部缺项、版本号重复或断号。
    """
    root = Path(directory)
    if not root.is_dir():
        raise MigrationError("迁移目录不存在", migrations_dir=str(root))

    found: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                "迁移文件名不合规",
                path=path.name,
                hint="命名必须是 NNN_小写名.sql，例如 004_observability.sql",
            )
        found.append(_parse(path, int(match.group("version")), match.group("name")))

    if not found:
        # 空目录会让人误以为「什么都不用做」，而真相是文件没打包进来。
        raise MigrationError(
            "迁移目录里没有 .sql 文件",
            migrations_dir=str(root),
            hint="确认 migrations/ 被打包进 wheel（见 pyproject.toml）",
        )

    found.sort(key=lambda item: item.version)
    _validate_sequence(found)
    return tuple(found)


def _validate_sequence(migrations: list[Migration]) -> None:
    """版本号必须从 1 开始连续递增。"""
    for index, item in enumerate(migrations, start=1):
        if item.version != index:
            raise MigrationError(
                "迁移版本号不连续",
                expected=index,
                found=item.version,
                path=item.filename,
                hint="新增迁移请取「当前最大版本 + 1」，不要跳号也不要复用已用过的号",
            )


def _parse(path: Path, version: int, name: str) -> Migration:
    """解析单个迁移文件的头部与主体。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationError("无法读取迁移文件", path=str(path), error=str(exc)) from exc

    header: dict[str, str] = {}
    body_lines: list[str] = []
    in_header = True

    for line in text.splitlines():
        if in_header:
            stripped = line.strip()
            if not stripped:
                continue
            match = _HEADER.match(stripped)
            if match is not None:
                key = match.group("key")
                if key not in _KNOWN_HEADER_KEYS:
                    raise MigrationError(
                        "迁移头部出现未知字段",
                        path=path.name,
                        key=key,
                        allowed=sorted(_KNOWN_HEADER_KEYS),
                    )
                if key in header:
                    raise MigrationError("迁移头部字段重复", path=path.name, key=key)
                header[key] = match.group("value")
                continue
            in_header = False
        body_lines.append(line)

    _require_header(header, path, version)
    body = "\n".join(body_lines).strip()

    if not body:
        raise MigrationError("迁移文件没有 SQL 主体", path=path.name)

    offending = _TRANSACTION_CONTROL.search(body)
    if offending is not None:
        raise MigrationError(
            "迁移文件里不得出现事务控制语句",
            path=path.name,
            statement=offending.group(0).strip(),
            hint="事务边界由迁移器管理；文件自带 COMMIT 会让「原子应用」失效",
        )

    return Migration(
        version=version,
        name=name,
        description=header["description"],
        destructive=_parse_bool(header["destructive"], "destructive", path),
        reversible=_parse_bool(header["reversible"], "reversible", path),
        sql=body,
        path=path,
    )


def _require_header(header: dict[str, str], path: Path, version: int) -> None:
    """四项头部缺一不可。

    ``destructive`` 尤其不能有默认值：给它一个 ``False`` 的默认值，就等于
    「忘了写=不备份」，而忘写的成本正好落在最不该出错的那次迁移上。
    """
    missing = sorted(_KNOWN_HEADER_KEYS - header.keys())
    if missing:
        raise MigrationError(
            "迁移头部缺少必填字段",
            path=path.name,
            missing=missing,
            hint="需要 -- migration / -- description / -- destructive / -- reversible",
        )

    declared = header["migration"].strip()
    if not declared.isdigit() or int(declared) != version:
        raise MigrationError(
            "迁移头部版本号与文件名不一致",
            path=path.name,
            filename_version=version,
            header_version=declared,
        )

    if not header["description"].strip():
        raise MigrationError("迁移描述不能为空", path=path.name)


def _parse_bool(raw: str, key: str, path: Path) -> bool:
    value = raw.strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    raise MigrationError("迁移头部布尔值非法", path=path.name, key=key, value=raw)


class Migrator:
    """把库从当前版本推到最新版本。"""

    __slots__ = ("_backup_dir", "_conn", "_migrations")

    def __init__(
        self,
        conn: SqliteConnection,
        *,
        migrations: tuple[Migration, ...] | None = None,
        migrations_dir: Path = MIGRATIONS_DIR,
        backup_dir: Path | None = None,
    ) -> None:
        """Args:
        conn: 已打开的连接。
        migrations: 直接给定迁移序列（测试用）。省略则从目录发现。
        migrations_dir: 迁移目录。
        backup_dir: 破坏性迁移前的备份存放处。省略则不备份——
            **仅在测试里可以省略**，生产路径由 ``SqliteStorageBackend`` 显式传入。
        """
        self._conn = conn
        self._migrations = (
            migrations if migrations is not None else discover_migrations(migrations_dir)
        )
        self._backup_dir = backup_dir

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrations

    @property
    def target_version(self) -> int:
        return self._migrations[-1].version

    def plan(self) -> MigrationPlan:
        """算出要从哪儿补到哪儿。"""
        current = self._conn.user_version()
        pending = tuple(item for item in self._migrations if item.version > current)
        return MigrationPlan(current=current, target=self.target_version, pending=pending)

    def migrate(self, *, dry_run: bool = False) -> MigrationPlan:
        """应用全部待处理的迁移。

        Args:
            dry_run: 只算差异、不落盘。破坏性迁移在这种模式下**不会**触发备份，
                因为备份的时机是「真的要改数据库之前」。

        Returns:
            实际执行的计划。已经是最新时 ``pending`` 为空。

        Raises:
            MigrationError: 库版本高于程序、备份缺失、迁移执行失败。
        """
        plan = self.plan()

        if plan.is_downgrade:
            raise MigrationError(
                "数据库版本高于当前程序支持的版本",
                schema_version=plan.current,
                supported=plan.target,
                hint="请升级 alterego 到最新版本，或从备份恢复",
            )

        if dry_run or not plan.needs_migration:
            _log.info("迁移计划: %s", plan.describe())
            return plan

        # 备份与前置检查全部放在**改库之前**。
        #
        # 如果边迁移边检查，「第 3 步缺备份目录」会在前两步已经落盘之后才被发现，
        # 于是「配置写错了」和「库改了一半」叠在一起——这是最难收拾的组合。
        destructive = plan.destructive_pending
        if destructive:
            self._backup_before_destructive_run(destructive[0])

        applied: list[Migration] = []
        for migration in plan.pending:
            self._apply(migration)
            applied.append(migration)

        _log.info(
            "迁移完成",
            extra={
                "alterego": {
                    "from": plan.current,
                    "to": plan.target,
                    "applied": [item.version for item in applied],
                }
            },
        )
        return plan

    # ── 内部 ────────────────────────────────────────────────

    def _backup_before_destructive_run(self, first: Migration) -> None:
        """破坏性迁移之前留下退路。

        整批迁移只备份**一次**，快照取在任何 DDL 之前。按版本各备份一份的话，
        同一份字节会被复制 N 次，而它们的内容完全相同——恢复时也只需要那一份。
        """
        if self._backup_dir is None:
            raise MigrationError(
                "破坏性迁移前必须备份，但未配置备份目录",
                path=first.filename,
                version=first.version,
                hint="构造 Migrator 时传入 backup_dir，未做任何改动即可退出",
            )
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        destination = Path(self._backup_dir) / f"pre-migration-{first.version}-{stamp}.db"
        self._conn.backup_to(destination)
        _log.warning("检测到破坏性迁移，已备份到 %s", destination)

    def _apply(self, migration: Migration) -> None:
        """在一个事务里跑完 DDL 与记账。"""
        script = _compose_script(migration, _now_iso())

        try:
            self._conn.raw.executescript(script)
        except sqlite3.Error as exc:
            # executescript 在脚本中途报错时不会替你回滚，事务会挂在那里。
            # 不回滚就直接抛，下一个调用方会在「以为是干净状态」的事务里继续写。
            self._rollback_after_failure()
            raise MigrationError(
                "迁移执行失败，已回滚",
                path=migration.filename,
                version=migration.version,
                error=str(exc),
            ) from exc

        _log.info("已应用迁移 %s: %s", migration.version, migration.description)

    def _rollback_after_failure(self) -> None:
        # 脚本可能在建事务之前就失败了，那本来就没有事务可回滚；
        # 而回滚失败本身也不能盖掉原始异常（它已经把原因说清楚了）。
        with contextlib.suppress(sqlite3.Error):
            self._conn.raw.execute("ROLLBACK")


def _compose_script(migration: Migration, applied_at: str) -> str:
    """拼出「DDL + 记账 + 事务边界」的完整脚本。

    记账放在脚本里而不是 ``executescript`` 之后再写一次，是因为必须**同事务**：
    否则可能出现「表建好了但版本号没涨」，下次启动会拿同一个 DDL 再跑一遍。
    """
    description = migration.description.replace("'", "''")
    return (
        "BEGIN;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_version (version, applied_at, description) "
        f"VALUES ({migration.version}, '{applied_at}', '{description}');\n"
        f"PRAGMA user_version = {migration.version};\n"
        "COMMIT;\n"
    )


def _now_iso() -> str:
    """带时区偏移的 ISO 8601 时间戳。

    和领域层写入的其他时间戳保持同一种形状（``+08:00`` 而不是 ``Z``），
    否则同一张表里会混着两种写法，按日期分组的视图会算错。
    """
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")

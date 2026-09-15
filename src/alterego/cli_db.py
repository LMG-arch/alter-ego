"""``alterego db`` —— 数据库维护命令。

**这是整个程序的组装根**（composition root）：只有这里与 ``cli.py`` 知道
「存储用的是 SQLite」，其余代码一律只认 ``StorageBackend`` Protocol。
``scripts/check_architecture.sh`` 第 3 组红线把这件事钉住了——除本模块、
``cli.py`` 与 ``storage/`` 之外，任何地方 ``import alterego.storage.sqlite``
都会让架构检查失败。

单独成一个文件还有个很实际的原因：``cli.py`` 已经贴着「单文件 ≤ 900 行」的
上限（``AGENTS.md`` § 5）。“把数据库维护切出去”让两边都松一口气。

命令按「先看、再做、留住退路」排：``status`` 看清现状，``migrate`` 动手，
``backup`` 留退路，``restore`` 是唯一的回头路。
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from alterego.cli_io import _RULE, _human_size, _out, _pad
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config, StorageConfig
from alterego.kernel.errors import StorageError
from alterego.storage.sqlite import MigrationPlan, SqliteStorageBackend


def _db_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _storage_config(config: Config) -> StorageConfig:
    """把 ``[storage]`` 段里的库路径换成已解析的绝对路径。

    不这么做会踩一个很安静的坑：``storage.db_path`` 的默认值是
    ``Path("data/alterego.db")``，**相对于进程的工作目录**；而
    ``Config.database_path`` 已经按 ``core.data_dir`` 解析过了。
    只设了 ``data_dir`` 没设 ``db_path`` 时，两者指向的不是同一个文件——
    于是 ``db migrate`` 建了一个库，程序启动时读的却是另一个，
    而两边都不报错。统一到 ``Config.database_path`` 这个唯一真源上。
    """
    return replace(config.storage, db_path=config.database_path)


def _open_db(config: Config) -> SqliteStorageBackend:
    """读写打开。只有真的要改库的命令才用它——它会建文件、跑 PRAGMA、checkpoint。"""
    return SqliteStorageBackend.from_config(
        _storage_config(config),
        backup_dir=config.backups_dir,
    )


def _peek_db(config: Config) -> SqliteStorageBackend:
    """只读地看一眼，**并且不留下一个空库**。

    两个只读命令（``db status`` / ``db migrate --dry-run``）都用它。
    库不存在时返回一个内存库，而不是 ``open(真路径)``：后者会顺手把文件建出来，
    让「查一下」变成「建了一个空库」。空库本身合法（``user_version = 0``），
    但一个只读命令不该有这个副作用——它是 ``open()`` 的默认行为，不是巧合，
    所以要显式绕开。
    """
    if config.database_path.exists():
        return SqliteStorageBackend.from_config(
            _storage_config(config),
            backup_dir=config.backups_dir,
            read_only=True,
        )
    return SqliteStorageBackend.open(":memory:", backup_dir=None)


def _require_sqlite(config: Config) -> None:
    """确认 ``[storage] backend`` 说的是本版本真的有的那个后端。

    现在随包提供的只有 SQLite，``plugins/`` 里也还没有存储插件。
    配成别的值时，与其让人对着「改了配置却还是老样子」发呆，
    不如把这句话明确说出来。
    """
    backend = config.storage.backend or "sqlite"
    if backend != "sqlite":
        raise StorageError(
            "本版本只随包提供了 sqlite 存储后端",
            backend=backend,
            hint="把 [storage] backend 改回 sqlite；别的后端要等插件体系落地后才会出现",
        )


def _stamp(config: Config) -> str:
    """给备份文件名用的时间戳。按**配置的时区**取，理由同 ``cli._today()``。"""
    zone = resolve_timezone(config.core.timezone)
    return datetime.now(zone).strftime("%Y%m%d-%H%M%S")


def _print_migrations(plan: MigrationPlan) -> None:
    """列出待执行的迁移。

    带上 ``destructive`` / ``reversible``：它们是迁移文件头里的**声明**，
    看的时候有用（「这一步会不会丢数据」「这一步原则上能不能退」），
    但它们不驱动任何代码路径——迁移器只往前走。
    """
    for item in plan.pending:
        flags = ["破坏性" if item.destructive else "非破坏性"]
        flags.append("声明可逆" if item.reversible else "声明不可逆")
        # 25 列：现有迁移文件名最长的是 `004_observability.sql`（23 个字符）。
        # 写死一个比它大的数，否则那一行的描述会被挤到前一列里去。
        _out(f"  {_pad(item.filename, 25)}{item.description}")
        _out(f"  {'':25}{'｜'.join(flags)}")


def cmd_db_status(args: argparse.Namespace) -> int:
    """库在哪、版本多少、还差几个迁移。只读，不建库。"""
    del args  # 这个子命令没有参数
    config = _db_config()
    _require_sqlite(config)

    db_path = config.database_path
    _out(f"数据库 · {db_path}")
    _out(_RULE)

    try:
        backend = _peek_db(config)
    except StorageError as exc:
        # 版本过高或过旧。这**就是**状态，不该当成崩溃——
        # 但要返回非零，让脚本能发现「这个库现在读不了」。
        _out("状态      当前无法读取")
        _out(f"原因      {exc}")
        return 2

    with backend:
        current = backend.current_schema_version
        required = backend.required_schema_version
        plan = backend.plan()

    if not db_path.exists():
        _out("库文件    还没建过")
    elif current == 0:
        _out(f"库文件    {_human_size(db_path.stat().st_size)}（空库，一个表都没有）")
    else:
        _out(f"库文件    {_human_size(db_path.stat().st_size)}")

    _out(f"当前版本  {current}")
    _out(f"目标版本  {required}")

    if not plan.needs_migration:
        _out("待执行    无")
        _out("")
        _out(f"已是最新（schema_version {current}）。")
        return 0

    _out(f"待执行    {len(plan.pending)} 个")
    _out("")
    _print_migrations(plan)
    _out("")
    _out(f"运行 alterego db migrate 把它升到 {plan.target}。")
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    """建库，或把已有的库升到当前版本。``--dry-run`` 只说不做。"""
    config = _db_config()
    _require_sqlite(config)

    if args.dry_run:
        # 预演用只读的 `_peek_db`：一个「只看看会发生什么」的命令
        # 最不该有的副作用，就是先把库建出来。
        with _peek_db(config) as backend:
            plan = backend.plan()
        if not plan.needs_migration:
            _out(f"已是最新（schema_version {plan.current}），没有待执行的迁移。")
            return 0
        _out(f"将要应用 {len(plan.pending)} 个迁移：{plan.current} → {plan.target}")
        _out(_RULE)
        _print_migrations(plan)
        _out("")
        _out("这只是预演，没有改动任何东西。")
        _out("预演**不做备份**——备份的时机是「真的要改数据库之前」。")
        return 0

    with _open_db(config) as backend:
        plan = backend.plan()
        if not plan.needs_migration:
            _out(f"已是最新（schema_version {plan.current}），没有待执行的迁移。")
            return 0
        before = plan.current
        applied = len(plan.pending)
        target = plan.target
        # `migrate()` 返回的是**读回来的**真实版本（字符串），不是计划值。
        # 转成 int 是为了能跟 `plan.target` 比——比不了的那个不一致就看不见了。
        version = int(backend.migrate())

    _out(f"已迁移    schema_version {before} → {version}（应用了 {applied} 个迁移）")
    _out(f"数据库    {config.database_path}")
    if version != target:
        # `migrate()` 读回的是库里的真实值，不是计划值。两者不等说明有事发生，
        # 而这种事（迁移跑到一半没跑完）必须让人看见。
        _out(f"注意      实际版本 {version} 与计划目标 {target} 不一致，请检查上面是否有报错。")
    return 0


def cmd_db_backup(args: argparse.Namespace) -> int:
    """整份备份。这是唯一的「回滚」手段。"""
    config = _db_config()
    _require_sqlite(config)

    if not config.database_path.exists():
        raise StorageError(
            "还没有数据库可以备份",
            db_path=str(config.database_path),
            hint="先运行 alterego db migrate 建库",
        )

    destination = Path(args.dest) if args.dest else None
    with _open_db(config) as backend:
        saved = backend.backup(destination)

    _out(f"已备份 → {saved}")
    _out(f"大小      {_human_size(saved.stat().st_size)}")
    return 0


def cmd_db_restore(args: argparse.Namespace) -> int:
    """从备份恢复。这是唯一的「回滚」，所以每一步都先校验再动手。

    顺序是有讲究的：**先把现在这份留一手，再校验备份，最后才覆盖。**
    恢复的可怕之处在于它会盖掉现在这一份——万一备份本身是坏的，
    一次失败的「恢复」会把两份都弄没。所以任何一步失败，都必须在
    「现在这份还在」的状态下退出。
    """
    config = _db_config()
    _require_sqlite(config)

    source = Path(args.file)
    if not source.is_file():
        raise StorageError(
            "找不到这个备份文件",
            path=str(source),
            hint="备份文件由 alterego db backup 生成，默认在 data/backups/ 下",
        )
    if source.resolve() == config.database_path.resolve():
        raise StorageError(
            "不能把库自己当成备份来源",
            path=str(source),
            hint="请指向 data/backups/ 里的某一份备份",
        )

    db_path = config.database_path
    safety: Path | None = None

    if db_path.exists():
        # 1) 留一手。
        with _open_db(config) as current:
            safety = current.backup(config.backups_dir / f"before-restore-{_stamp(config)}.db")

    # 2) 校验这份备份读得出来，而且本程序认得它的版本。
    #    放在覆盖之前——坏备份必须在这里被拦住。
    with SqliteStorageBackend.open(source, read_only=True) as check:
        incoming = check.current_schema_version

    # 3) 清掉 WAL 边车文件。主文件换了，旧的 `-wal` 还躺在那里的话，
    #    SQLite 会拿它去「重放」一批属于另一个库的页——库会坏得莫名其妙。
    for suffix in ("-wal", "-shm"):
        stale = db_path.with_name(db_path.name + suffix)
        if stale.exists():
            stale.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, db_path)

    with _peek_db(config) as restored:
        version = restored.current_schema_version
        required = restored.required_schema_version

    _out(f"已恢复    {db_path}")
    _out(f"来源      {source}")
    _out(f"版本      schema_version {version}")
    if safety is not None:
        _out(f"覆盖前的库已留一份 → {safety}")
    if version != incoming:
        # 理论上不可能。真出现了说明这次复制没到位（磁盘、权限、并发），
        # 而它比看上去严重：库可能是半新半旧的。宁可现在就说出来。
        _out(f"注意      恢复后的版本 {version} 与备份里读到的 {incoming} 不一致。")
    if version < required:
        _out(f"注意      这份备份比程序旧（目标 {required}），运行 alterego db migrate 补上。")
    return 0

"""SQLite 存储后端。

选它的理由：零部署、单文件、自带事务与 FTS5 全文检索，
且 Python 标准库就能用（设计原则 P5：标准库优先）。
代价与边界见 docs/adr/0003-sqlite-as-sole-storage-backend.md。

⚠️ 本目录的代码只被两处引用：**组装根**（``cli.py`` / ``cli_db.py`` /
``cli_memory.py`` / ``cli_vault.py`` / ``cli_dataset.py``）与存储层自己。
其余代码一律通过 `StorageBackend` Protocol 访问，不得 `import alterego.storage.sqlite`——
包括 `sim/`，也包括将来会写在外面的每一个 Repository。
这条已由 `scripts/check_architecture.sh` 第 3 组红线覆盖整个 `src/`。

组装根是正当的例外：`main()` 本来就负责「挑一个具体实现装上」，
与 `01-architecture.md` § 6.1 时序图里的 `Main->>Store: migrate()` 是同一件事。
将来 `storage.sqlite` 插件落地后，这个例外会收到插件一处。

依据: docs/design/03-data-model.md
"""

from __future__ import annotations

from alterego.storage.sqlite.backend import MIN_COMPATIBLE_VERSION, SqliteStorageBackend
from alterego.storage.sqlite.connection import SqliteConnection
from alterego.storage.sqlite.migrator import MigrationPlan
from alterego.storage.sqlite.repositories import (
    SqliteActivityRepository,
    SqliteDatasetSourceRepository,
    SqliteMemoryRepository,
    SqlitePersonaRepository,
    SqliteScheduleRepository,
    SqliteSourceRepository,
    SqliteUsageRepository,
)


__all__ = [
    "MIN_COMPATIBLE_VERSION",
    "MigrationPlan",
    "SqliteActivityRepository",
    "SqliteConnection",
    "SqliteDatasetSourceRepository",
    "SqliteMemoryRepository",
    "SqlitePersonaRepository",
    "SqliteScheduleRepository",
    "SqliteSourceRepository",
    "SqliteStorageBackend",
    "SqliteUsageRepository",
]

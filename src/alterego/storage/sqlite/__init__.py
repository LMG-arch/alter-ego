"""SQLite 存储后端。

选它的理由：零部署、单文件、自带事务与 FTS5 全文检索，
且 Python 标准库就能用（设计原则 P5：标准库优先）。
代价与边界见 docs/adr/0003-sqlite-as-sole-storage-backend.md。

⚠️ 本目录的代码**只被 `storage.sqlite` 插件引用**。
其他层一律通过 `StorageBackend` Protocol 访问，不得 `import alterego.storage.sqlite`
（由 `scripts/check_architecture.sh` 第 3 组红线保证）。

依据: docs/design/03-data-model.md
"""

from __future__ import annotations


__all__: list[str] = []

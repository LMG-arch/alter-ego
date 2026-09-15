# 存储层框架 实现计划

> **规范来源（Spec）：** [`docs/design/03-data-model.md`](../design/03-data-model.md) § 3 / § 7 / § 8 / § 9、
> [`docs/design/01-architecture.md`](../design/01-architecture.md) § 1.1 依赖矩阵、
> [`docs/design/06-roadmap.md`](../design/06-roadmap.md) § 1.2 阶段 D
>
> 本计划实现路线图的 **阶段 D 前半部分**：存储层地基（连接 / 迁移 / 后端契约）。
> Repository 的具体方法（`MemoryRepository` 等 12 个接口）排在批次 2。

---

## 目标

让 `alterego` 具备一个**可迁移、可备份、可事务、可复现**的 SQLite 持久化地基：
新建库能一次建出 26 张表与 2 个视图，旧库能幂等地补上缺失迁移，
失败时**不留半截状态**，且 `StorageBackend` 契约真正有人实现。

**架构**：`storage/sqlite/` 是水平挂载的插件宿主，依赖方向 `storage → kernel`、`storage → domain`（允许）。
它不认识 `sim/`、`channels/`、`capabilities/`（红线第 4 组）。SQL 迁移是数据，不是代码，
放在 `migrations/` 下随 wheel 一起分发。

**技术栈**：Python 3.11+、标准库 `sqlite3`、SQLite 3.35+（FTS5、部分唯一索引、`json_valid`）。
不新增任何依赖（P5）。

---

## 全局约束（改动必须同时满足）

| 约束 | 值 | 出处 |
| --- | --- | --- |
| 必需依赖 | 只有 `pydantic`、`httpx` | `AGENTS.md § 5` |
| 单文件行数 | ≤ 900（`migrations/*` 豁免） | `AGENTS.md § 5` |
| 行长 | ≤ 100 | `AGENTS.md § 5` |
| 类型注解 | 全部，`mypy strict` | `AGENTS.md § 5` |
| 时间格式 | ISO 8601 **带时区偏移**（`2026-09-15T14:32:11+08:00`） | `03-data-model.md § 1.1` |
| 主键 | `TEXT`（uuid4 hex） | `03-data-model.md § 1` |
| JSON 列 | 空值用 `'{}'` / `'[]'`，且 `CHECK (json_valid(...))` | `03-data-model.md § 1.2` |
| 布尔 | `INTEGER` 0/1 | `03-data-model.md § 1` |
| SQLite PRAGMA | `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`、`temp_store=MEMORY` | `03-data-model.md § 3` |
| 可复现 | 不出现 `datetime.now()` / 全局 `random` | P6 |
| 日志 | 用 `ctx.logger` / `getLogger(__name__)`，不自建 handler | 红线 6 |

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/alterego/storage/sqlite/connection.py` | 连接工厂、PRAGMA 应用、可嵌套事务、`user_version` 读写、checkpoint |
| `src/alterego/storage/sqlite/migrator.py` | 迁移发现（文件名 + 头部元数据）、差异计算、**原子应用**、dry-run、破坏性迁移前备份 |
| `src/alterego/storage/sqlite/backend.py` | `SqliteStorageBackend` —— `interfaces.storage.StorageBackend` 的实现，含版本兼容检查与备份 |
| `src/alterego/storage/sqlite/migrations/001_initial.sql` | 表 1–20 + 索引 + FTS5 与三个同步触发器 |
| `src/alterego/storage/sqlite/migrations/002_media.sql` | 表 21–22（`media_asset` / `media_usage`）+ 定妆照部分唯一索引 |
| `src/alterego/storage/sqlite/migrations/003_sources.sql` | 表 23–25（`source_feed` → `source_query` → `source_item`）+ 索引 |
| `src/alterego/storage/sqlite/migrations/004_observability.sql` | 表 26 `log_entry`、三张表的 `correlation_id` 补列、视图 `v_cost_daily` / `v_trace` |
| `tests/test_storage_sqlite.py` | 连接、迁移、后端契约的全部测试 |

---

## 计划开始前发现的三处契约冲突（必须先解决）

| # | 冲突 | 处理 |
| --- | --- | --- |
| 1 | `check_architecture.sh` 第 4 组禁止 `storage/` import `domain`，但 `01-architecture.md § 1.1` 依赖矩阵写 `storage → domain` ✅，且 `03-data-model.md § 7` 的 Repository 返回 `Persona`/`Memory` 等 domain 类型 | **以设计文档为准**（`AGENTS.md` 开篇约定），修正脚本：从禁止列表中移除 `domain`，保留 `sim\|channels\|capabilities`。此红线此前从未被触发，因为 `storage/` 是空的 |
| 2 | `03-data-model.md § 8.3` 让每个迁移文件自己 `UPDATE schema_version`，但 `002`/`003` 没写这句 → `schema_version` 会变成稀疏的 {1, 4} | 改为**迁移器是 `schema_version` 的唯一写入点**，迁移文件只负责 DDL。同步修改 `03-data-model.md § 8.3` |
| 3 | `004` 用 `ALTER TABLE ... ADD COLUMN`，SQLite 无 `IF NOT EXISTS`，与「迁移必须幂等」冲突 | 每个迁移在**单个事务**内执行。SQLite 的 DDL 是事务性的，失败的迁移整体回滚，不留半截状态 —— 幂等性由「版本号 + 原子性」共同保证，而不是靠 `PRAGMA table_info` 预检 |

---

## 任务

### 任务 1 · 修正红线第 4 组

- 改 `scripts/check_architecture.sh`：`storage/` 的禁止模式去掉 `domain`，并写明「允许依赖 `domain` 是为了拿类型形状，不是允许把业务规则搬进存储层」。
- 验证：`bash scripts/check_architecture.sh` 仍为 22/22。

### 任务 2 · `connection.py`

- `PragmaSettings`（`journal_mode` / `synchronous` / `busy_timeout_ms` / `foreign_keys` / `temp_store`），`statements()` 生成 PRAGMA 语句。
- `SqliteConnection.open(path, *, settings, readonly=False)`：确保父目录存在、`sqlite3.connect(check_same_thread=False)`、`row_factory = sqlite3.Row`、应用 PRAGMA。
- `transaction()`：**可嵌套**——已在事务里就加入外层，不再 `BEGIN`。对应「写操作在调用方事务内被合并为单事务」。
- `user_version()` / `set_user_version(n)`、`checkpoint()`、`integrity_check()`、`close()`。
- 测试：PRAGMA 真的生效（读回 `journal_mode` 应为 `wal`）、嵌套事务只提交一次、异常时回滚。

### 任务 3 · `migrator.py`

- `Migration` 数据类（`version` / `name` / `description` / `destructive` / `reversible` / `sql`）。
- `discover_migrations(dir)`：按 `NNN_description.sql` 排序，跳过往返 0 字节外的非匹配文件，解析头部 `-- migration:` / `-- description:` / `-- destructive:` / `-- reversible:`。版本号取自文件名，与头部不一致时报错。
- `Migrator.plan()`：对比 `user_version` 与目录，算出 `pending`。
- `Migrator.migrate(dry_run=False)`：逐个在事务内 `executescript`，成功后写 `schema_version` + `set_user_version`。**破坏性迁移前自动备份**。
- 测试：空库一次建全；重复调用是空操作；版本号跳跃；头部元数据缺失或版本不一致时报 `MigrationError`；失败迁移不留半截（故意在 SQL 尾部塞一个错语句，断言表没被建出来）。

### 任务 4 · `backend.py`

- `SqliteStorageBackend` 实现 `StorageBackend`：`migrate()` 返回字符串版本、`transaction()`、`flush()`、`checkpoint()`、`close()`。
- `REQUIRED_SCHEMA_VERSION = 4` / `MIN_COMPATIBLE_VERSION = 1`，启动时双向检查（过低提示迁移，过高提示升级）。
- `backup(dest=None) -> Path`：用 `sqlite3` 的 `VACUUM INTO`（SQLite 3.27+，无需第三方），落到 `data/backups/alterego_YYYYmmdd_HHMMSS.db`。
- `integrity_check() -> bool`。
- `from_config(config) -> SqliteStorageBackend`。
- 测试：契约方法可用；版本过高/过低各抛一次 `StorageError`；备份文件能被独立打开且表数一致。

### 任务 5 · SQL 迁移文件

- 按设计文档 § 3 / § 3.1 / § 3.2 / § 3.3 逐字落地，**去掉** PRAGMA 头（由连接负责）与 `INSERT INTO schema_version`（由迁移器负责）。
- `003` 内部顺序调整为 `source_feed` → `source_query` → `source_item`，避免外键指向尚未创建的表。
- 测试：建库后 `sqlite_master` 里表数 = 26、视图数 = 2；`PRAGMA foreign_keys` 生效（插一条孤儿行应失败）；插入非法 JSON 应被 `CHECK` 拒绝。

### 任务 6 · 门禁与同步

- `ruff format` / `ruff check` / `mypy` / `pytest` / `check_architecture.sh` 全绿。
- 同步文档：`03-data-model.md § 8.3`、`CONTRIBUTING.md` 项目结构树、`docs/DESIGN.md` 目录树、`CHANGELOG.md`。
- 提交并推送，确认 CI 绿。

---

## 明确不在本批次范围内

- 12 个 Repository 的具体实现（`PersonaRepository` … `BudgetRepository`）—— 批次 2
- 把 `SqliteStorageBackend` 包成 `storage.sqlite` 插件（`plugin.toml` + 入口点）—— 批次 2
- ~~`alterego db migrate / status / backup` CLI 子命令 —— 批次 3~~
  **已完成**（批次 5，`2026-09-15-storage-cli.md`）：四条命令，另加 `restore`
- `domain/` 领域层纯函数 —— 批次 2 之后
- `jieba` 中文分词接入（`03-data-model.md § 6.4`）—— 依赖批次 2 的 Repository

# ADR-0003 · 使用 SQLite 作为唯一存储后端

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **相关**：[ADR-0002](0002-use-python-as-implementation-language.md)、[`docs/design/03-data-model.md`](../design/03-data-model.md)
- **影响范围**：`storage/`、`domain/` 的 Repository 接口、`kernel/` 的 `StorageBackend` 协议

---

## 背景

AlterEgo 需要持久化约 20 类数据：人设与其版本历史、世界设定、NPC、关系、情绪日志、长期记忆、对话与消息、动态与互动、日程、活动日志、tick 日志、LLM 用量、预算、插件状态等。

数据特征：

| 特征 | 说明 |
| --- | --- |
| **写入频率高** | `realtime` 模式每 5 分钟一个 tick，每 tick 写 3–5 张表。约 288 tick/天 |
| **持续增长** | 长期运行，估算约 515 MB/年（见 `06-roadmap.md` § 5.4）|
| **读取以近期为主** | 90% 的查询是"最近 N 天的活动/情绪" |
| **需要全文检索** | 记忆检索靠关键词匹配（中文），需要排序 |
| **需要结构化查询** | "这周发了几条主动消息"、"情绪效价的标准差" |
| **单写入者** | 单进程，无并发写；可能有少量并发读（Web 请求）|
| **本地优先** | 数据不出本机，无云同步需求 |
| **可复现性要求** | 同一 tick 重放必须得到相同结果——数据读取顺序不能随机 |
| **中文** | 分词是刚需（`03-data-model.md` § 6.4）|

不可妥协的约束：

- **C1 零运维。** 用户不应该为了用这个工具去装一个数据库服务。
- **C2 无额外依赖。** 依据 ADR-0002，必需依赖只有 2 个。引入一个数据库驱动就意味着引入一个必需依赖，或把"能存数据"变成可选功能——那样核心功能就不可用了。
- **C3 单文件 / 易备份。** 用户要能"复制一个文件就完成备份"。
- **C4 全文检索可用。** 记忆检索是核心功能，必须有可用的中文全文搜索。
- **C5 Python 3.11 内置可用。** 与 ADR-0002 的最小依赖策略一致。

---

## 决策

使用 **SQLite**（Python 标准库 `sqlite3`，要求 SQLite 3.35+）作为**唯一**存储后端。

具体配置与用法：

| 项 | 决定 | 理由 |
| --- | --- | --- |
| 模块 | 标准库 `sqlite3` | C5；不引入 `SQLAlchemy` |
| 日志模式 | `PRAGMA journal_mode=WAL` | 允许"一写多读"并发，避免 Web 请求被 tick 写入阻塞 |
| 同步级别 | `PRAGMA synchronous=NORMAL` | WAL 下的安全/性能平衡点 |
| 忙等待 | `PRAGMA busy_timeout=5000` | 避免偶发 `database is locked` 直接抛错 |
| 外键 | `PRAGMA foreign_keys=ON` | 默认关闭，必须显式开启 |
| 临时表 | `PRAGMA temp_store=MEMORY` | 排序与临时表放内存，加速 |
| 全文检索 | **FTS5** 虚拟表 + `unicode61 remove_diacritics 2` | 内置，无需额外依赖 |
| 中文分词 | 写入前在 Python 侧用 `jieba` 预分词（`preprocess_for_fts()`）| FTS5 自带分词器不切中文 |
| 复杂结构 | JSON 存为 TEXT + `CHECK (json_valid(...))` | 避免大量关联表；JSON 列约定为 `{}` / `[]` 而非 `NULL` |
| 时间 | ISO 8601 带时区偏移的 TEXT（`2026-09-15T14:32:11+08:00`）| SQLite 无原生日期类型；文本支持 BETWEEN 与排序 |
| 主键 | `uuid4().hex` 的 TEXT | 可复现性要求：不依赖自增 ID 的全局顺序 |
| 布尔 | INTEGER `0` / `1` | SQLite 无布尔类型 |
| 并发写 | `asyncio.Lock` 串行化 | SQLite 单写入者；应用层保证 |
| 同步调用 | `asyncio.to_thread` 包装 | `sqlite3` 是同步的，避免阻塞事件循环 |
| 向量检索 | **v1 不做**，留作可选插件 | 避免引入 `numpy` / `sqlite-vec` 等依赖 |
| 迁移 | 手写 SQL 脚本 + `PRAGMA user_version` | 不引入 Alembic |

同时在 `kernel/` 定义 `StorageBackend` 协议（`migrate` / `transaction` / `flush` / `checkpoint` / `close`），使存储后端可以整体替换为插件。

---

## 考虑过的方案

| 方案 | 零运维 | 零依赖 | 单文件备份 | 全文检索 | 是否采用 |
| --- | --- | --- | --- | --- | --- |
| **SQLite（标准库）** | ✅ | ✅ | ✅ | ✅ FTS5 | ✅ |
| JSON / JSONL 文件 | ✅ | ✅ | ✅ | ❌ 需自己实现 | ❌ |
| DuckDB | ✅ | ❌ 第三方包 | ✅ | ⚠️ 有但为分析场景 | ❌ |
| PostgreSQL | ❌ 需服务 | ❌ | ❌ | ✅ | ❌ |
| MongoDB | ❌ 需服务 | ❌ | ❌ | ⚠️ 中文弱 | ❌ |
| TinyDB（纯 Python） | ✅ | ❌ | ✅ | ❌ | ❌ |
| `sqlite-vec` / ChromaDB | ✅ | ❌ | ⚠️ | 向量 | ❌（可选插件）|

### 为什么否掉 JSON / JSONL 文件

最"零依赖"的方案，看起来符合"代码简单"的诉求。但：

1. **全文检索要从头写。** 记忆检索需要 BM25 排序——自己实现意味着数百行易错代码，且性能远不如 FTS5。这违反 C6"代码简单"。
2. **没有事务。** `Persist` 阶段需要在一次事务里写 6 张表（`activity_log` / `emotion_log` / `memory` / `relationship` / `schedule_block` / `tick_log`），要么全成要么全败。JSON 文件做不到。
3. **没有索引。** "最近 7 天的活动"要全量扫描。
4. **并发写会损坏文件。** 即使单进程，Web 请求与 tick 也可能交叉。

### 为什么否掉 PostgreSQL / MongoDB

需要用户装服务。直接违反 C1、C2。这是一个**本地单人工具**，不是服务端应用。

### 为什么否掉 DuckDB

DuckDB 在分析查询上很强，且也是单文件、零运维。否掉的原因：

1. **它需要安装第三方包**（`duckdb`），破例引入必需依赖（违反 C2）。
2. **写入模式不匹配。** DuckDB 的列式存储更适合"批量导入后分析"，而不是"每 5 分钟写几行"的高频小写入。它的 WAL 与并发写支持也比 SQLite 弱。
3. **经验积累成本。** 未来遇到并发或事务问题的参考资料远少于 SQLite。

### 为什么否掉引入 SQLAlchemy

ORM 会让 Repository 层更"抽象"，但：

- 它是**必需依赖**（违反 C2）
- 隐藏了 SQL，不利于性能调优与理解真实查询
- 迁移还需要另外引入 Alembic
- 本项目 SQL 只有 20 张表，手写完全可控

改为：**手写 SQL + 手写 Repository + 手写迁移脚本**，放在 `storage/sqlite/`。

### 为什么否掉在 v1 引入向量检索

记忆检索的关键词路径（FTS5 BM25 + 加权重排）已经能工作，且：

- `sqlite-vec` / `ChromaDB` / `faiss` 都是**重量级依赖**（`numpy` 起步）
- 向量检索还需要 embedding API 调用，**增加 LLM 成本**（每天多 200+ 次 embedding 请求）
- 收益不确定：记忆只有几十到几百条，关键词 + 重要性 + 新近度 + 情绪一致性的加权排序已经够用

决定：**v1 只做 FTS5，把向量检索留成 `EmbeddingProvider` 插件**。如果实测召回质量不够，再启用。

---

## 后果

### 正面

- **C1–C5 全部满足。** 零运维、零新增必需依赖、单文件、FTS5 全文检索、标准库可用。
- **备份极其简单。** `VACUUM INTO 'data/backups/alterego_20260915_143211.db'` 一条命令，或在文件系统里复制 `.db` 文件（WAL 模式建议用前者）。用户还可以直接把它丢云盘。
- **数据可检查、可手改。** 任何 SQLite 客户端（DB Browser、`sqlite3` CLI）都能打开。用户能自己查"它到底记了什么"，这对建立信任很重要。
- **性能足够。** 单写入者 + WAL，"一写多读"没有争用。20 张表、数百万行在这个量级下毫无压力。
- **中文分词方案清晰。** 在 Python 侧 `jieba` 切词后用空格连接写入 FTS5，查询时用同一函数。缺点是需要保证写入与查询用**同一个** `preprocess_for_fts()`——这一点已经写进文档与测试要求。
- **迁移机制完全可控。** 20 个 SQL 文件 + `PRAGMA user_version`，没有黑盒。
- **可复现性有保障。** uuid4 主键 + 显式 `ORDER BY`（不依赖 rowid 顺序），同一 tick 重放结果一致。

### 负面

- **并发能力有限。** 单写入者。如果未来要做多 Agent 并行推演且都要写库，会撞上 `SQLITE_BUSY`。缓解：`busy_timeout=5000` + 应用层 `asyncio.Lock`；真的需要时再考虑后端插件化（`StorageBackend` 协议已经为此准备）。
- **`sqlite3` 是同步的。** 每个调用都要 `asyncio.to_thread` 包装，有线程池切换开销。缓解：把一次 tick 的写入合并到一个事务里，减少往返。
- **没有原生 JSON/数组类型。** 复杂结构要手动 `json.dumps` / `json.loads`。缓解：`CHECK (json_valid(...))` 约束 + Repository 层统一封装。
- **没有原生日期类型。** 时间比较与排序靠 ISO 8601 文本的字典序——这**恰好**正确（ISO 8601 的设计目标之一），但不能做时区转换。故约定**所有时间都带时区偏移存储**，且 `sim/` 禁止 `datetime.now()`。
- **中文分词是手工步骤，容易漏。** 如果哪天有人写了一个新的 FTS 写入路径却忘了调 `preprocess_for_fts()`，会导致该条记录检索不到。缓解：把分词**封装进 Repository 的唯一写入方法**，而不是散落各处；并加测试。
- **数据库可能损坏。** 断电或异常退出可能损坏。缓解：WAL + `synchronous=NORMAL`、定期 `checkpoint`、启动时 `PRAGMA integrity_check`、破坏性迁移自动备份。
- **`tick_log` 会膨胀。** 估算占年度增长的 81%。缓解：`[retention]` 配置支持 `tick_log_detail = "summary"` 与保留天数（见 `06-roadmap.md` § 5.4）。

### 需要关注

**什么时候应该重新审视这个决策：**

1. 如果**多个进程**需要同时写库（例如 `serve` 与 `cli` 同时改数据）。当前设计是 `serve` 持有写权、CLI 通过 HTTP API 通信，但如果将来 CLI 直接写库就会出现问题。
2. 如果数据库**超过 2 GB**，或某个查询超过 1 秒。
3. 如果记忆条目超过数千条，FTS5 + 加权重排的召回质量实测不够——那时启用 `EmbeddingProvider` 插件，而不是换数据库。
4. 如果决定支持多用户/云端（当前是明确非目标）。

**重要**：以上任何情况都**不需要换掉 SQLite 才能解决**——前三条都可以通过配置、索引或插件解决。只有第 4 条（多用户/云端）需要换，而那是非目标。

### 需要特别注意的设计约束

WAL 模式会产生 `.db-wal` 与 `.db-shm` 两个附属文件：

- **必须在 `.gitignore` 中排除**（已做）
- **备份必须用 `VACUUM INTO` 或先 `checkpoint`**，直接复制 `.db` 可能丢失最近的写入
- **不要把数据库放在网络盘**（WAL 需要共享内存，网络文件系统不支持）

---

## 对设计原则的影响

| 原则 | 影响 |
| --- | --- |
| P1 内核无知 | **需要设计配合**。`kernel/` 只定义 `StorageBackend` 协议，`sqlite3` 只出现在 `storage/sqlite/`。`scripts/check_architecture.sh` 的第 1 组 grep 会强制检查这一点 |
| P2 显式优于隐式 | **支持**。手写 SQL 让每次查询都是显式的；JSON 列用 `CHECK` 约束显式声明形状 |
| P3 机制约束优于提示词祈祷 | **支持**。外键、`CHECK (json_valid(...))`、`NOT NULL` 都是机制约束 |
| P4 可插拔优于可配置 | **支持**。`StorageBackend` 协议允许整体替换后端（如换成 Postgres）作为插件，无需改内核 |
| P5 标准库优先 | **强支持**。`sqlite3` 是标准库，不引入任何依赖即可获得关系数据库 + 全文检索 |
| P6 可复现 | **需要显式努力**。所有查询必须显式 `ORDER BY`（否则顺序依赖实现细节）；主键用 uuid4 而非自增；时间统一带时区 |
| P7 文档与代码同生共死 | **强相关**。schema 变更必须同时改 `03-data-model.md` 的 DDL 与迁移脚本，并在 `CHANGELOG.md` 记录 |

---

## 需要同步的文档

- [x] `docs/DESIGN.md` —— § 9 数据存储，说明了 SQLite + WAL + FTS5 的选型
- [x] `docs/design/01-architecture.md` —— 内核的 `StorageBackend` 协议与第 4 组架构红线
- [x] `docs/design/03-data-model.md` —— 完整的 PRAGMA 配置、20 张表的 DDL、索引策略、检索算法、迁移机制
- [x] `docs/design/04-simulation-loop.md` —— `Persist` 阶段的单事务写入、`strength_at()` 的读取模式
- [x] `docs/design/06-roadmap.md` —— § 5.4 存储成本估算与 retention 配置
- [x] `CONTRIBUTING.md` —— 「新增依赖」说明为什么 `sqlite3` 不算依赖
- [x] `.gitignore` —— 排除了 `*.db` / `*.db-wal` / `*.db-shm` / `data/backups/`
- [x] `CHANGELOG.md` —— 已在 `[0.1.0]` 记录

---

## 验证方式

| 检查 | 方式 | 期望 |
| --- | --- | --- |
| PRAGMA 生效 | 启动后查询 `PRAGMA journal_mode` | 返回 `wal` |
| FTS5 可用 | `python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` | 无异常 |
| SQLite 版本 | `python -c "import sqlite3;print(sqlite3.sqlite_version)"` | ≥ 3.35.0 |
| JSON 约束生效 | 尝试插入非法 JSON | 抛 `IntegrityError` |
| 中文检索可用 | 写入一条含"爬山"的记忆，搜索"爬山" | 能召回（验证写入与查询都调了 `preprocess_for_fts()`）|
| 分区测试 | 写入一条记忆，用不同措辞（"登山"）搜索 | **不应**召回（确认没有意外的全表扫描）|
| 事务完整性 | 在 `Persist` 中途抛异常 | 所有表都无写入 |
| 架构红线 | `bash scripts/check_architecture.sh` | `kernel/` 无 `sqlite` 字样 |
| 无 ORM 依赖 | `python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'])"` | 列表中无 `sqlalchemy` |
| 单文件备份 | `VACUUM INTO` 后打开备份文件 | 数据完整，可查询 |
| 数据库健康 | 启动时的 `PRAGMA integrity_check` | 返回 `ok` |

**关键回归测试**（应加入 `tests/storage/`）：

```python
def test_fts_chinese_roundtrip(tmp_path):
    """写入与查询必须用同一分词函数，否则检索会静默失败"""
    db = make_db(tmp_path)
    db.add_memory(content="周末和小王去爬山，他体力不行")
    hits = db.search_memories("爬山")
    assert len(hits) == 1

def test_fts_no_false_positive(tmp_path):
    """分词后不应产生意外的子串匹配"""
    db = make_db(tmp_path)
    db.add_memory(content="周末和小王去爬山")
    assert db.search_memories("登山") == []   # 不同词，不应召回
```

---

## 备注

**核心权衡**：SQLite 不是"最好的数据库"，但它是唯一同时满足 C1–C5 的选择。而且它的限制（单写入者、同步 API、无原生 JSON/日期类型）在本项目的实际负载下都不构成问题——**单进程、本地、低并发、每年几百 MB**。

**一个容易被忽略的好处**：SQLite 让"数据透明"变得自然。用户可以用任何工具打开 `data/alterego.db` 看 Agent 到底记了什么、情绪怎么变的、忍住了哪些话。这对一个"模拟人的存在"的项目来说，是建立信任的重要部分——如果数据藏在某个云服务的黑盒里，用户很难相信它。

参考：

- [SQLite · WAL 模式](https://www.sqlite.org/wal.html)
- [SQLite · FTS5](https://www.sqlite.org/fts5.html)（含 `unicode61` 分词器说明）
- [SQLite · 何时使用](https://www.sqlite.org/whentouse.html)（"客户端应用"一节与此场景吻合）
- [`docs/design/03-data-model.md`](../design/03-data-model.md) —— 本决策的具体落地

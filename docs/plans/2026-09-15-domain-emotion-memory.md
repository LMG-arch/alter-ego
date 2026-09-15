# 实施计划 · 领域层（情绪 + 记忆纵向切片）

> 状态：**已完成**（2026-09-15）
> 依据：[`04-simulation-loop.md`](../design/04-simulation-loop.md) § 6–7、[`03-data-model.md`](../design/03-data-model.md) § 6–7、
> [`01-architecture.md`](../design/01-architecture.md) § 1.1 + § 3、[`06-roadmap.md`](../design/06-roadmap.md) § 1.2 阶段 D

## 1. 为什么先做这一片

`06-roadmap.md` 阶段 D 要求「领域层 + 存储层」。存储地基（批次 1，commit `2bf3223`）已完成。
领域层的下一层前置是**领域模型**，而侦察发现：`03-data-model.md § 7` 的 12 个 Repository 契约
引用了 20 个领域类型（`Persona` / `Memory` / …），**这些类型在全部文档里只被引用、从未定义**。

不一次性铺开 24 张表的镜像模型，而是先做一条**纵向切片**——理由：

| 判据 | 情绪 + 记忆 | 其余实体 |
| --- | --- | --- |
| 文档里有可执行的量化验收标准 | ✅ `strength_at` 验证表（4 个精确值）、`update_emotion` 四条规则 | ❌ 只有字段清单 |
| 依赖其他实体的程度 | 极低（只需 `ScheduleBlock.category` 一个字段） | 相互引用（`World` → `NPC` → `Relationship`） |
| 能被独立测试 | ✅ 全纯函数 | 需要 Repository 才能验证 |
| 是 M2 的验收项 | ✅ `06-roadmap.md` § 1.2 明确列出 | 部分 |

结论：这一片**投入产出比最高、风险最低**，且它做完后剩下的实体就是机械的字段镜像。

## 2. 范围内

| 交付物 | 依据 |
| --- | --- |
| `src/alterego/domain/schedule.py` | `01-architecture.md § 3`（`ScheduleBlock` / `current_block` / `is_interruptible`）+ `001_initial.sql` 表 13 |
| `src/alterego/domain/emotion.py` | `04-simulation-loop.md § 6.1–6.4` 逐字 |
| `src/alterego/domain/memory.py` | `04-simulation-loop.md § 7.1–7.3` + `03-data-model.md § 6.2 / § 6.4` |
| `tests/test_domain_schedule.py` / `test_domain_emotion.py` / `test_domain_memory.py` | 覆盖率门槛 `domain/ ≥ 95%`（CI 阻断） |
| 文档补缺 | 见 § 4 |

### 2.1 补定义的三个类型

`04-simulation-loop.md` 与 `03-data-model.md` 使用了但没定义的类型，本批次补齐：

- `EmotionalEvent`（`04-simulation-loop.md § 6.3` 用了 `event.valence_delta` / `arousal_delta` / `fatigue_delta` / `description`）
- `MemorySearchHit`（`03-data-model.md § 7` 的 `search_fts` 返回类型；M2 要求「Top-8 返回带分数分解」）
- `MemoryStats`（`03-data-model.md § 7` 的 `stats` 返回类型）

## 3. 四处文档冲突（本批次修正）

### 3.1 `strength_at` 有两个互相矛盾的公式

| 出处 | 公式 | 判定 |
| --- | --- | --- |
| `01-architecture.md § 3` | `importance × exp(-0.05 × days) × (1 + log1p(recall_count))` | ❌ 单一衰减率，与「三种记忆半衰期不同」矛盾 |
| `04-simulation-loop.md § 7.2` | `importance × e^{-λ_kind · Δt} × (1 + 0.35 × recall_count)`，λ 由半衰期决定 | ✅ 有推导、有参数表、有验证示例 |

**修正**：以 § 7.2 为准，`01-architecture.md § 3` 的草图改成指向 § 7.2。
`01-architecture.md § 3` 的 `Memory` 还漏了 `kind` 字段（而 `strength_at` 必须知道 kind），一并补上。

### 3.2 `Memory` 的字段名两处不一致

`01-architecture.md § 3` 的 `ScheduleBlock` 用 `start` / `end`，`001_initial.sql` 表 13 用 `start_at` / `end_at`。
**修正**：以 DDL 为准（物理列名是唯一能往返的真源），草图改名。

### 3.3 `update_emotion` 把疲劳恢复弄丢了

`04-simulation-loop.md § 6.4` 的组合函数写成 `update_fatigue(current.fatigue, elapsed, None)`
——硬编码 `None`，于是「规则 4 · 睡眠时每小时恢复 0.8」**永远不会生效**；
同时 `EmotionalEvent.fatigue_delta` 也从未被使用。

**修正**：`update_emotion` 增加 `block: ScheduleBlock | None = None` 参数并应用 `fatigue_delta`。
这不是「顺手改」——是同一份文档的 § 6.3 规则表与 § 6.4 组合函数自相矛盾，必须择一。

> 注：初稿把冲突写成了「三处」，实测过程中又碰到第四处（下文 § 3.4）。

### 3.4 `Emotion.label` 的词表两处不一致

`01-architecture.md § 3.2` 把 `label` 列为 10 个词（开心/平静/烦躁/低落/兴奋/焦虑/疲惫/感动/委屈/恼火）；
而 `04-simulation-loop.md § 6.2` 的 `infer_label()` 只能产出另一组 8 个词
（疲惫/兴奋/愉快/焦虑/低落/警觉/平静/一般）。
实现时如果按 `infer_label` 的输出做闭集校验，就会把「感动」这类合法标签拒之门外。

**修正**：两份都对，只是**角色不同**——

- `infer_label()` 是**降级路径**（LLM 不可用），输出集合封闭，就是 `EMOTION_LABELS` 那 8 个；
- LLM 路径可以给出更细的词，所以 `Emotion.label` **不做闭集校验**，只保留非空约束。

测试里加了一条不变量：「`infer_label` 在 $(valence, arousal)$ 网格上的输出一定包含于 `EMOTION_LABELS`」。

## 4. 文档同步

| 文件 | 改什么 | 状态 |
| --- | --- | --- |
| `04-simulation-loop.md` | § 6.2 补记 `infer_label` 疲惫特例的实际取舍；§ 6.3 补 `EmotionalEvent` 定义与 `update_fatigue` 的 `block` 语义；§ 6.4 修正 `update_emotion` 签名与规则 4 接线；§ 7.3 把 `maybe_resurrect` 拆成纯函数 `should_resurrect` / `resurrect` + Sim 层副作用 | ✅ |
| `03-data-model.md` | § 6.2 补 `rank_memories` / `RetrievalWeights`；新增 § 6.9「领域模型的归属」定义 `MemorySearchHit` / `MemoryStats` | ✅ |
| `01-architecture.md` | § 3.1 修正 `update_emotion` 签名；§ 3.2 列出已实现模块、修正 § 3.1 / 3.2 / 3.3 三处草图 | ✅ |
| `DESIGN.md` | § 13 目录树标注已实现的三个模块 + 变更记录 v0.2.1 | ✅ |
| `CONTRIBUTING.md` | 目录树同步 | ✅ |
| `06-roadmap.md` | 阶段 D 进度注记 | ✅ |
| `CHANGELOG.md` | 新增 / 变更 / 修复 / 文档 | ✅ |

## 5. 明确不在本批次范围内

1. **其余 21 张表的领域模型**（`Persona` / `World` / `NPC` / `Relationship` / `ScheduleBlock` 之外的表镜像、`UsageSummary` / `UsageDaily` / `LLMUsage` / `BudgetUsage` / `MediaAsset` / `SourceItem` / `LogEntry` …）→ 下一批次
2. **12 个 Repository 实现**（`storage/sqlite/repo/`）→ 下一批次
3. **`storage.sqlite` 插件打包**（`plugin.toml` + entry point）→ 下一批次
4. **`alterego db migrate / status / backup / restore` CLI 子命令** → 再后一批
5. `domain/media.py`（`build_portrait_prompt`）与 `domain/untrusted.py` → 与生图 / 外部源接线时一起做

## 6. 本批次发现的、需要后续修的结构问题

> 记录在此，避免丢失。**不在本批次修**，因为修它们要动 schema，属于 Repository 批次。

**`memory` 表没有「已巩固」标记。** `03-data-model.md § 7` 的 `MemoryRepository` 声明了
`list_recent(..., not_consolidated=True)` 与 `mark_consolidated(memory_ids)`，
但 `001_initial.sql` 表 7 **没有任何 `consolidated` 列**。
`source_ref` 的注释是「来源 id（tick_id / message_id / memory_id）」，
方向是「semantic 记忆指向它的来源 episodic」，反查只能靠 `LIKE '%id%'` —— 脆弱且无法索引。

**建议**（下一批次处理）：新增 `005_memory_consolidation.sql`，
给 `memory` 加 `consolidated_at TEXT`（`NULL` = 未巩固）+ 部分索引
`CREATE INDEX idx_memory_unconsolidated ON memory(persona_id, kind, occurred_at DESC) WHERE consolidated_at IS NULL;`
并同步 `03-data-model.md § 4.2` 的字段表。

## 7. 验收

| 项 | 标准 |
| --- | --- |
| `strength_at` | 复现 `04-simulation-loop.md § 7.2` 验证表：0 天→0.70、7 天→0.35、21 天→0.09、28 天→0.04（误差 < 0.001） |
| 遗忘阈值 | 28 天时 `0.04 < 0.05` → 判定为遗忘 |
| `infer_label` | 8 个标签各有覆盖用例 |
| `apply_with_inertia` | 同向放大（≤1.4×）、反向削弱（≥0.6×） |
| `update_emotion` | 顺序断言（先回归→再冲击→最后惯性/疲劳）；睡眠时段疲劳下降 |
| `rank_memories` | 四项分数分解与权重可验证；同分时排序稳定（按 id） |
| 覆盖率 | `domain/` ≥ 95% |
| 全部门禁 | `ruff format` / `ruff check` / `mypy` / `pytest` / `check_architecture.sh` 全绿 |

### 7.1 实测结果（2026-09-15）

| 门禁 | 结果 |
| --- | --- |
| `ruff format` / `ruff check src tests plugins` | All checks passed |
| `mypy src/alterego` | Success: no issues found in **37** source files |
| `python -m pytest tests -q` | **898 passed, 1 skipped**（本批次新增 151 → 后来含 3 条不变量） |
| `pytest --cov=alterego` | TOTAL 96.37% |
| `pytest --cov=alterego.domain` | **329 stmts / 76 分支，100%**（门槛 95%） |
| `bash scripts/check_architecture.sh` | ✓ 22 项，7 组 |

新增文件：`domain/schedule.py`（54 stmts）、`domain/emotion.py`（112）、`domain/memory.py`（160）
与三个测试文件。

### 7.2 顺手修掉的两个实现级缺陷

1. `Emotion.__post_init__` 的三维范围校验在首次落地时被写坏（`arousal` 的判据写成
   `-1 <= valence <= 1` 且错误信息互串），由 `test_rejects_out_of_range_values` 抓到。
2. `decay_toward` 的 `0.5 ** x` 在 typeshed 里返回 `Any`，触发 mypy `no-any-return`；
   改为显式标注 `factor: float`，并在 `04-simulation-loop.md § 6.3` 的示例里同步。

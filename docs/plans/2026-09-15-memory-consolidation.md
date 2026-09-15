# 批次计划 · 记忆梳理（LLM 提炼活动流 + 归纳零散记忆）

> 需求原话：**「让 llm 进行梳理记忆，梳理数据」**
>
> 本批次把这句话拆成两个方向，一次交付：
>
> | 方向 | 输入 | 输出 | 命令 |
> | --- | --- | --- | --- |
> | **梳理数据** | `activity_log`（它做过的事） | 新的 `episodic` / `emotional` 记忆 | `alterego memory distill` |
> | **梳理记忆** | 一批未巩固的 `episodic` 记忆 | 归纳出的 `semantic` 记忆 + 原始记忆降权 | `alterego memory consolidate` |
>
> 两者共用同一条流水线：**读 → 渲染提示词 → 一次 LLM 调用 → 严格解析 JSON → 落库**。
> 分别实现等于把这条流水线搭两遍，所以合在同一个批次。

依据：`docs/design/03-data-model.md` § 6.5、`docs/design/07-model-routing-and-media.md` § 2.4 / § 8、
`docs/design/05-channels.md` § 8.1、`docs/design/11-optimization-roadmap.md` § 2.3。

---

## 1. 为什么本批次必须先补 `llm/`

`src/alterego/llm/__init__.py` 至今是一个空壳（`__all__: list[str] = []`）——
没有路由、没有重试、没有计量、没有提示词装载。

而 `scripts/check_architecture.sh` **第 7 组红线**（v0.2.0 新增）规定：

> 一切 LLM 调用必须经 `ctx.llm()`。理由是三条，缺一条都会让某张账对不上：
> 1. **计量** —— 绕过的这次调用不会进 `llm_usage`，成本统计漏报；
> 2. **路由** —— 绕过了 `[llm.routing]` 的分层，用错档位的模型；
> 3. **闸门** —— 绕过了 `[llm.budget]`，预算上限形同虚设。

也就是说：**「让 LLM 梳理」这件事本身，无法在一个没有网关的仓库里合法实现。**
先做网关不是顺手加需求，而是这条路的地基。

本批次只做「够用」的那一半，刻意不做的事列在 § 5。

### 1.1 网关的形状

```
purpose("memory") ──> LLMRoutingConfig.memory ──> tier("cheap")
                                                     │
                              LLMRoutingConfig.cheap ─┘
                                        │
                              provider id("openai_compatible")
                                        │
                              ServiceRegistry.get(LLMProvider, name=…)
                                        │
                                  provider.complete(req)
                                        │
                         ┌──────────────┴───────────────┐
                    重试（只重试 retryable）        记一条 LLMUsage
```

`purpose` 是一个字符串而不是枚举：`07-model-routing-and-media.md` § 2.4 的清单还会长
（`image_prompt` / `research_query` / `research_summarize` / `reaction` 已在文档里，
配置类里还没有），加一个用途不该要改代码。

**未配置的用途怎么办**：`LLMRoutingConfig` 里查不到该用途时**不猜、不降级**，直接抛
`ConfigError` 并点名缺哪个键。猜错的代价是「用强模型跑了一万次廉价调用」，
而且要到月底看账单才发现。

---

## 2. 两个方向的合同

### 2.1 梳理数据（distill）

已存在的 `src/alterego/prompts/memory_consolidate.md` 就是为这条路径写的：
占位符 `{persona_name}` / `{activities}` / `{existing_memories}` / `{max_items}` / `{user_name}`，
输出一个**裸 JSON 数组**，元素形如

```json
{"kind": "episodic", "content": "…", "importance": 0.5, "valence": 0.0,
 "entities": ["…"], "emotional": false}
```

注意 `emotional` 这个布尔位：它表达的是「这一瞬值得长期记住」，
映射到领域层就是 `kind = "emotional"`（`HALF_LIFE_DAYS["emotional"] = 365.0`，
衰减最慢）。**不引入新字段**——领域层已经有这个区分了。

### 2.2 梳理记忆（consolidate）

`03-data-model.md` § 6.5 的伪代码与上面这份模板**不一致**（它期望
`{"memories": [{"summary", "importance", "tags"}]}`，且把 `llm: LLMProvider` 直接传进来）。
两处都以**模板为准**，改文档——理由和批次 5 修 § 8.3 时一样：

> 一份画在文档里的代码签名会被当成已实现的契约。模板文件是运行时真的会被读的那个。

改文档的同时必须修掉伪代码里的第二个问题：它绕开了 `ctx.llm()`，违反第 7 组红线。

### 2.3 巩固的副作用

已实现且已在领域层写好：`domain/memory.py::apply_consolidation(memories, factor=0.6)`。
信息已上提，原始 episodic 的重要度乘 0.6——**降的是 `importance` 而不是 `strength`**，
于是 `strength_at()` 会连带下降，两者不会互相打架。

---

## 3. 需要动 schema

`memory` 表没有「已巩固」标记，`activity_log` 没有「已梳理」标记。
没有它们，每跑一次巩固就会把同一批输入再提炼一遍，产出**重复记忆**。

新增 `005_memory_consolidation.sql`：

```sql
ALTER TABLE memory ADD COLUMN consolidated_at TEXT;          -- NULL = 未巩固
ALTER TABLE activity_log ADD COLUMN distilled_at TEXT;       -- NULL = 未梳理

CREATE INDEX idx_memory_unconsolidated
    ON memory(persona_id, kind, occurred_at DESC) WHERE consolidated_at IS NULL;

CREATE INDEX idx_activity_undistilled
    ON activity_log(persona_id, started_at DESC) WHERE distilled_at IS NULL;
```

两个部分索引的形状都和查询一一对应（「某人的、未巩固的、按时间倒序」），
而不是一个「什么都能查一点」的宽索引。

**幂等靠版本号**：`SqliteStorageBackend.required_schema_version` 从迁移目录推导
（`backend.py` 里那段注释专门解释了为什么不写死常量），所以加文件即生效，无需改代码。

**为什么不加 `consolidated_into`（指向归纳出的记忆 id）**：
反查这一个需求可以先靠 `memory show` 看来源、靠 FTS 搜内容满足。
加一列就要维护双向一致性，而它现在只服务一个展示位。要做，等它真被用到。

---

## 4. 落位

| 内容 | 住在哪 | 为什么 |
| --- | --- | --- |
| 提示词装载与渲染 | `llm/prompts.py` | 纯字符串操作，属于 LLM 层（协议适配的一部分） |
| 路由 / 重试 / 计量 | `llm/gateway.py` | 红线 4：`llm/` 只做协议适配、重试、计量 |
| 供应商 | `llm/providers/openai_compatible.py` | 一次调用，不管重试 |
| JSON → `Memory` 的解析与校验 | `domain/consolidation.py` | 纯函数、无 IO、可单测（§ 6.9 归属表） |
| SQL / 事务 / 往返映射 | `storage/sqlite/repo/` | 换后端只重写这一层 |
| 编排（读 → 调 → 写） | `sim/consolidation.py` | `07` § 8：编排不该进 domain（有 IO）也不该进 storage（红线 4） |
| 命令 | `cli_memory.py` | 组装根，与 `cli_db.py` 同级 |

`cli_memory.py` 是**第三个**组装根，而第 3 组红线的排除正则目前是 `cli(_db)?\.py:`——
要收紧成 `cli(_db|_memory)?\.py:`（批次 5 已经踩过一次同类问题：
排除正则没有覆盖到实际存在的文件，等于没有排除）。

---

## 5. 不在本批次范围内（明确记下，避免越做越大）

- **12 个 Repository**：只做 `Memory` / `Activity` / `Usage` 三个，
  其余 9 个等各自的批次（它们的输入数据现在还没人生成）。
- **`connectors/`、向量检索、`EmbeddingProvider` 实现**：降级路径本来就是关键词检索。
- **`[llm.budget]` 的闸门**：本批次把用量**记进** `llm_usage`（红线 7 要求的最小值），
  但不在调用前拒绝。预算拦截需要 `BudgetRepository` + 跨日累计，单开一批。
- **成本估算**：`llm_usage.cost_usd` 先记 0。单价表要跟着 `[llm.providers.*]` 走，
  而把「猜测的单价」写进库比写 0 更危险——0 至少是诚实的「不知道」。
- **`memory search`**：已经能用 `domain/memory.py` 的纯函数算分，
  但「让人看得懂分数分解」的展示值得单独设计，且它与「梳理」无关。
- **自动触发**：本批次只给手动命令。放进 tick 需要引擎（`sim/` 目前只有 `context.py`），
  那是另一批。

---

## 6. 新增配置项

**一个都不加。** 需要的参数（`{max_items}`、批大小、时间窗、最少条数）
全部作为函数参数写在 `domain/consolidation.py` 里，默认值取设计文档给的数字
（`03-data-model.md` § 6.5：每 6 虚拟小时一批、少于 3 条不值得归纳、输出最多 10 条）。

理由：加配置项必须同时改 `kernel/config.py` + `templates/alterego.toml`
（`test_authoritative_template_covers_every_key` 会拦），
而「一批最多归纳几条」这种数字在没有真实使用数据前调不出正确答案，
先写死、先能被测试看见，等有证据再提升为设置。

### 6.1 事后修正：确实动了两个地方

上面写「一个都不加」是我当时对「加配置项」的理解太窄——把「新增**设置项**」
当成了唯一形态。实际交付时动了两处，都记在这里：

| 动了什么 | 为什么不算违反本节的承诺 |
| --- | --- |
| `core.user_name` | 不是梳理功能要的参数，是**提示词模板的输入**。模板里不能写死「用户」，而这个值只能来自配置——没有别的来源 |
| `[llm.routing] memory = "cheap"` | 键早就存在（`LLMRoutingConfig` 的 8 个键之一），本批次只是**开始使用**它，没有新增键，也没有新增文件 |

**`distill` 与 `consolidate` 没有各自的用途键，两者共用 `memory`。**
它们是同一件事（把素材整理成文字）的两个入口，拆成两个键只会让
「换一个便宜模型来省钱」要多改一处。等真需要分别调参再加——
那时也就知道该调什么了。

**提示词模板也是共享的**：`prompts/memory_consolidate.md` 同时服务两个动词。
模板名里的 `consolidate` 是历史遗留（先写的是归纳那条路），
改名的收益抵不上让已发布的路径名变动一次。

---

## 7. 验收

1. `alterego memory distill --dry-run` 能在**不写库**的前提下打印「会提炼出哪几条」。
2. `alterego memory consolidate` 对同一批输入跑两次，第二次产出 0 条（`consolidated_at` 生效）。
3. 活动少于阈值时**不调用 LLM**（省钱且可测：网关会记一条 usage，条数为 0 即证明没调）。
4. LLM 返回的不是 JSON / 缺字段 / 越界（`importance` 不在 0~1）时，
   **不写坏数据**：整批丢弃并记 `WARNING`，退出码 1。
5. 没有配置可用供应商时退出码 2（配置非法），错误信息点名缺哪个键。
6. 全部门禁通过：`ruff format` / `ruff check` / `mypy` / `pytest` / `check_architecture.sh`。

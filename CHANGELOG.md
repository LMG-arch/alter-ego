# 变更日志

本文件的所有重要变更都会被记录。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**分类约定**：

- `新增` —— 新功能
- `变更` —— 对现有功能的调整
- `弃用` —— 即将移除的功能
- `移除` —— 已移除的功能
- `修复` —— bug 修复
- `安全` —— 安全相关
- `文档` —— 仅文档变更
- `架构` —— 分层、依赖、插件 API 相关变更（需附 ADR）

> ⚠️ **0.x 版本不保证向后兼容。** 破坏性变更会标注 `⚠️ BREAKING`。

---

## [Unreleased]

### 新增

**四类能力的设计（v0.2.0 / v0.3.0，仅设计，无实现）**

- **每一处 LLM 调用都可自定义模型**。配置模型从「一层」扩为**三层**：
  `[llm.providers.*]`（端点与密钥环境变量）→ `[llm.models.*]`（8 个字段，含
  `cost_per_1m_input/output` 让成本统计精确到分）→ `[llm.routing]`（12 个用途各自指向
  **模型别名**）。`context_window` / `max_output` 由内核用于**前置校验**，
  不再依赖供应商报错。新分册 `docs/design/07-model-routing-and-media.md`
- **生图能力与角色形象一致性**。`media_asset`（21）/ `media_usage`（22）两张新表；
  新增 `image` 插件类型。一致性靠**三层机制**：定妆照（`role='canonical'` + 部分唯一索引）
  + 每次生成都传参考图（`supports_reference=false` 的插件**被拒绝生成人物图**）
  + 固定四槽位骨架 `build_portrait_prompt(appearance_brief, *, outfit, scene, mood, lighting)`
  （**纯函数，住在 `domain/media.py`，不可挪进插件**）。验收标准量化为
  「同一份定妆照生成 20 张不同场景图，人眼判断是同一个人 ≥ 16/20（80%）」。
  见 `docs/adr/0008`
- **联网检索**。`source_item`（23）/ `source_feed`（24）/ `source_query`（25）三张新表；
  新增 `source` 插件类型与三个独立契约（`SearchProvider` / `FeedReader` / `PageFetcher`）；
  `research` 成为第 11 种意图。检索由兴趣与当前情绪驱动，读到的内容会沉淀为记忆。
  新分册 `docs/design/08-external-sources.md`
- **Token 消耗统计页面**。`log_entry`（26）表 + 两个视图 `v_cost_daily`（LLM 与生图合并计量）
  与 `v_trace`（5 路 UNION，把一次推演串成完整链路）。新增 `GET /api/stats/tokens`、
  `/api/stats/projection`、`/api/budget`、`/api/trace/{correlation_id}`。
  统计页必须显示三件事：今日进度条、外推预测、**降级状态徽章**
- **日志页面**。双写（文件轮转 14 天 / 数据库 WARNING+ 90 天 / SSE 实时不落库）；
  `GET /api/logs`、`/api/logs/stream`（SSE）、`/api/logs/export` 与
  `POST /api/logs/level`（**运行期调级别必须带 `for` 时长**，超时自动回落，
  避免「昨天开了 DEBUG 忘了关」把磁盘写满）。新分册 `docs/design/09-observability.md`
- **设置中心**。每个配置项强制携带 `Setting` 元数据（`label` / `description` / `kind` /
  `effect` / `choices[].consequence` / `danger` / `advanced` / `requires_restart` …），
  由**三条 CI 断言**卡住（每字段有元数据 / `effect` 是可验证的句子且不含「可能大概也许」 /
  枚举每个选项都写了后果）。密钥只显示「来源变量名 + 是否已设置」。新分册
  `docs/design/10-settings-center.md`，见 `docs/adr/0010`
- **优化路线分册**。`docs/design/11-optimization-roadmap.md` —— 30 个可加深方向的
  取舍分析（记忆 / 推演 / 人格 / 拟人深度 / 多模态 / 工程），附三维评分与三批落地建议

**项目规则（面向人与 AI 助手）**

- `AGENTS.md` —— 给 AI 编码助手的精简规则：开工前必读哪篇、五条硬性禁止各配一条自检命令、
  新增配置项的正确姿势、九种「必须停下来问」的情况
- `CONTRIBUTING.md` 新增「项目规则」章节：七条原则各自的**强制手段**、
  五条不可谈判的规则、以及一条元规则——**如果一条规则只能靠人记住，那它就不是规则**

**项目脚手架（已实现，v0.1.0 起）**

- `pyproject.toml`、`.gitignore`、MIT `LICENSE`、`README.md`、`.github/` 下的 CI 工作流与 issue / PR 模板
- 架构红线检查脚本 `scripts/check_architecture.sh`：七组检查强制保证分层依赖方向与两条「很久以后才会暴露」的规则

**内核层（阶段 C）**

- `kernel/errors.py` —— 异常树（`AlterEgoError` 基类 + 4 大类 13 个子类）。所有异常携带
  `context: dict`，`to_dict()` 可直接进日志；`is_retryable()` 区分「重试有用」与「重试只会更快烧钱」
- `kernel/clock.py` —— 三种时钟：`RealClock` / `VirtualClock`（倍速与重入基线）/ `FrozenClock`（测试）。
  含 IANA 时区解析与无 tzdata 环境下的固定偏移兜底
- `kernel/bus.py` —— 事件总线。`fnmatch` 通配主题、优先级 + 注册序排序、`once` 订阅、
  异常隔离（处理器炸了不影响其他处理器）、重入保护（深度上限 8）
- `kernel/registry.py` —— 服务注册表。按接口 + 名字索引、优先级解析、
  同优先级歧义时报错并给出 `name=` 建议、`unregister_owner` 支持插件整体卸载
- `kernel/config.py` —— 配置系统。11 个 `frozen=True` 配置段、`__post_init__` 全量校验、
  `${ENV_VAR}` 密钥注入、`ALTEREGO_SECTION__KEY` 环境变量映射、疑似密钥自动脱敏、
  未知键只告警不失败
- `src/alterego/defaults.toml` —— 随包分发的发行版选型（默认 provider / 存储后端 / 路由分层）
- `cli.py` —— `argparse` 骨架与退出码约定（0 正常 / 2 配置错 / 3 插件依赖错 / 4 存储错）

**测试（内核）**

- `tests/test_kernel_config.py` —— 覆盖加载优先级、键折叠、全部校验分支、
  静默时段跨零点、脱敏，并断言权威模板 `templates/alterego.toml` 的键 100% 被内核认识
- `tests/test_architecture.py` —— 用 `ast` 机械校验内核不 import 任何 IO 库、
  不 import 任何上层模块、不留 `print`

**插件体系（阶段 C 续）**

- `interfaces/` —— 跨层纯数据契约包（6 个模块）。只放 `dataclass` 与 `Protocol`，
  一个 `alterego` 的 import 都没有，也**不认识任何具体技术**（P1 在类型层面同样成立）：
  `common.py`（`HealthStatus`）/ `llm.py`（`LLMRequest` / `LLMResponse` / `LLMProvider` /
  `EmbeddingProvider`）/ `channel.py`（`OutboundMessage` / `InboundMessage` / `SendResult`）/
  `storage.py`（`StorageBackend`）/ `simulation.py`（`Stage` / `StageResult` / `Capability` /
  `CapabilityResult` / `Tool` / `IntentType` / `PromptSource`）
- `kernel/manifest.py` —— `plugin.toml` 的解析与校验。硬校验（`id` 形如 `<kind>.<name>`、
  语义化版本、`api_version` 兼容性、`kind` 取值、`entry` 形如 `<模块>:<类名>`），
  配置字段声明支持 8 种类型与 `required` / `default` / `secret` / `env` / `choices` /
  `min` / `max` / `min_length` / `max_length` / `pattern` / `item_type` 等约束；
  `resolve_config()` 走「环境变量 > 用户配置 > 清单默认值」，报错时直接给出
  可以粘贴进配置文件的 `hint`
- `kernel/context.py` —— 插件的运行期上下文：`PluginPaths`（5 个目录）、
  `PluginState`（JSON 快照 + 延迟落盘 + 批量 flush）、`PluginContext`（11 个字段的 `frozen` 容器）
- `kernel/plugin.py` —— **插件唯一需要 import 的模块**。`Plugin` 基类（九个生命周期钩子）
  + 转发上面两个模块的全部公开名字
- `kernel/loader.py` —— 插件发现与导入。本地目录（`plugins/` 与 `~/.alterego/plugins/`）
  与 pip entry points 双来源、本地优先、依赖约束解析（`>=` / `<=` / `==` / `~=`）与
  拓扑排序、循环依赖检测
- `kernel/manager.py` —— 生命周期编排。启动顺序、错误隔离、连续失败熔断、
  热重载（含失败回滚）、`tick_pre` / `tick_post` 分发、`PluginState` 批量落盘
- `kernel/logging.py` —— 日志。控制台 / JSON 两种格式 + `SecretFilter`
  （四条正则脱敏，双组模式只替换密钥本身，保留 webhook 前缀便于定位）
- `sim/context.py` —— `TickContext`（13 个字段 + `note()` / `llm()`）与 `StateSnapshot`
  （`evolve()` 式不可变更新），推演层的共享数据结构
- 七个层的 `__init__.py`（`domain` / `llm` / `storage` / `storage.sqlite` / `channels` /
  `capabilities` / `npc`）：每层都写明「负责什么、**不**负责什么」以及违反时的红线编号
- `src/alterego/prompts/` —— 七个提示词模板：`intention` / `emotion_update` /
  `memory_consolidate` / `post_compose` / `chat_reply` / `reach_out` / `persona_generate`

**示例插件**

- `plugins/example_plugin/` —— 可以照抄的最小完整 `capability` 插件。演示清单声明、
  `on_load` 注册与订阅（**不传 `owner`**）、幂等 `on_stop`、配置热更新、人话版 `summary`

**测试（插件体系与黄金测试）**

- `tests/test_kernel_scheduler.py`（29）/ `test_kernel_logging.py`（32）/
  `test_kernel_plugin.py` / `test_kernel_loader.py`（93）/ `test_kernel_manager.py`（55）/
  `test_sim_context.py`（11）/ `test_cli.py`（6）
- `tests/test_example_plugin.py`（14）—— **真的加载示例插件**：走完整的清单校验、依赖解析、
  导入、注册、执行、卸载。示例代码一旦腐烂（字段改名、钩子签名变化）CI 立刻变红
- `tests/test_packages.py`（34）—— 逐个导入 `alterego` 下的全部模块，专抓循环导入与
  占位包腐烂；另在**独立进程**里断言 `import alterego` 不会顺带拖进任何子模块（启动时间保险）
- `tests/golden/test_golden_smoke.py`（10）—— 可复现性黄金测试。配置导出在**两个独立解释器
  进程**里逐字节一致；调度器同种子同触发序列、不同种子必不同、抖动只推后不提前

**测试基础设施**

- `tests/golden/`、`tests/fixtures/` 目录就位，CI 的 `可复现性黄金测试` 作业从此真的会跑东西
  （此前是 `⊘ 黄金测试尚未创建，跳过`）

**存储地基（阶段 D 前半，已实现）**

- `storage/sqlite/connection.py` —— SQLite 连接。五个 PRAGMA 集中在一处并在建立连接后立即应用
  （`journal_mode` 必须最先，因为它不能在事务里改）；**PRAGMA 不吃绑定参数，所以三个字符串
  PRAGMA 的值走白名单**，杜绝把用户配置拼进 SQL；`transaction()` 支持嵌套（内层并入外层），
  只有最外层发 `BEGIN` / `COMMIT` / `ROLLBACK`；`sqlite3` 异常统一翻译成内核异常树
  （`IntegrityError` / `StorageError`），并带上出错的 SQL 首行；`integrity_check()` 与
  `foreign_key_violations()` 把 PRAGMA 的异常也包成 `StorageError` —— 损坏的库文件会让
  `PRAGMA integrity_check` **抛异常**而不是返回非 `ok` 行，只捕获 `StorageError` 的调用方
  否则会看到一段 CLI traceback；`backup_to()` 用 `VACUUM INTO` 且拒绝在事务中执行
- `storage/sqlite/migrator.py` —— 迁移器。发现并**零容忍**校验迁移文件（文件名格式、
  头部四个键、版本号与文件名一致、跳号、空文件体、内嵌事务控制）；`plan()` 给出
  `current → target` 与待执行清单；破坏性迁移在任何 DDL 之前**先**检查备份目录是否可用，
  拒绝执行而不是「先改了再说」；每个迁移 = 一个脚本 = 一个事务，`schema_version` 记账与
  `PRAGMA user_version` 写入都在**同一个脚本内**，因此不存在「表建好了、版本号没涨」的中间态
- `storage/sqlite/backend.py` —— `StorageBackend` 契约实现。把「打开」与「迁移」分开：
  `open()` 只做版本兼容检查、绝不偷偷改 schema；版本兼容是**双向**的（库比程序新也拒绝，
  且报错必须带上库文件路径）；`required_schema_version` 由迁移文件推导而不是写死；
  打开失败的路径会关掉连接（WAL 模式下漏关会留下 `-wal` / `-shm` 两个残留文件）
- 四个迁移文件（`migrations/001_initial.sql` ~ `004_observability.sql`）：27 张表
  （26 张设计表 + `schema_version`）、5 个 FTS5 影子表、2 个视图。
  `003_sources.sql` 里**故意把 `source_item` 放到最后**——SQLite 在 `CREATE TABLE` 时就
  解析外键目标，所以外键不能指向未来
- 测试 140 个：`test_storage_sqlite.py`（40）/ `test_storage_migrator.py`（42）/
  `test_storage_backend.py`（35）/ `test_storage_schema.py`（23，断言**交付的 schema**
  而不是迁移机器）
- `docs/plans/2026-09-15-storage-framework.md` —— 本批次的实施计划（含三处契约冲突的判定）

**领域层（阶段 D 纵向切片，已实现）**

领域层按**纵向切片**落地：只做文档里有**可执行量化验收标准**的那一片（情绪 + 记忆 + 日程），
其余 21 张表的镜像模型等 Repository 到位后再铺开。理由与对比表见
`docs/plans/2026-09-15-domain-emotion-memory.md` § 1

- `domain/schedule.py` —— `ScheduleBlock` 与两个查询纯函数。`contains()` 是**左闭右开**，
  所以 12:00 结束的块不会和 12:00 开始的块重叠；`current_block()` 在重叠时取开始时间最晚的
  那个（临时改过的日程优先）；`is_interruptible()` 在「不在任何块内」时返回 `True`——
  没有日程的人不该被当成不可打扰。字段名以 DDL（`001_initial.sql` 表 13）为准
- `domain/emotion.py` —— 二维情绪（效价 / 唤醒度 / 疲劳）+ 四条更新规则。
  `decay_toward()` 减半的是**与基线的距离**而不是数值本身（基线 +0.2、当前 -0.6，
  一个半衰期后是 -0.2 而非 -0.3）；`apply_with_inertia()` 同向放大（最高 1.4×）、
  反向削弱（最低 0.6×）；`update_fatigue()` 睡眠每小时 -0.8、清醒每小时 +0.05；
  `infer_label()` 是 LLM 不可用时的降级路径，输出封闭的 8 个标签
- `domain/memory.py` —— 记忆的强度衰减、检索重排、遗忘与激活、巩固。
  `strength_at()` 的 λ 按**记忆类型**取（episodic 7 天 / semantic 180 天 / emotional 365 天
  半衰期），复现了 `04-simulation-loop.md § 7.2` 的验证表：0 天 0.70 / 7 天 0.35 /
  21 天 0.09 / 28 天 0.04（低于 0.05 阈值 → 遗忘）；`rank_memories()` 返回
  **带分数分解**的 `MemorySearchHit`（R/I/N/E 四项 + 总分 + strength 快照）而不是只给一个分数，
  因为「为什么排到前面」是调试检索质量时唯一看得见的东西；同分按 `memory.id` 排序保证可复现
- 三个领域模型的补定义：`EmotionalEvent`、`MemorySearchHit`、`MemoryStats`
  —— 这三者在文档里被引用却从未定义。它们定义在**拥有它们的模块里**，文档随后引用
- 测试 154 个：`test_domain_schedule.py` / `test_domain_emotion.py` / `test_domain_memory.py`
- `docs/plans/2026-09-15-domain-emotion-memory.md` —— 本批次的实施计划
  （含四处文档冲突的判定与实测结果）

### 变更

- **迁移文件从此只管 DDL**。四个 `.sql` 里没有任何 `PRAGMA` 头、没有 `IF NOT EXISTS`、
  没有 `BEGIN` / `COMMIT`、不往 `schema_version` 写任何东西——这四件事全部由迁移器
  在同一个事务里补齐。理由：**幂等靠版本号，不靠 `IF NOT EXISTS`**。后者会让一个
  「建了一半」的 schema 看起来像是建好了的，而版本号 + 事务原子性要么全成、要么全不成。
  迁移器在发现阶段就把这四条列为硬校验（违反直接报错，不是警告）
- **`v_trace` 的锚点从 `event_log` 改为 `tick_log`，分支从 5 路扩为 6 路**（推演 → 事件 →
  模型调用 → 生图 → 检索 → 日志）。原因：`event_log` 默认关闭（`06-roadmap.md § 5.4`
  估算 0 行/天），拿它当锚点等于让链路视图里**一条推演记录都没有**
- **确认 `event_log` 没有也不应有 `tick_id` 列**。它的 `correlation_id` 已经唯一标识一次
  推演，再存一份 `tick_id` 就是同一个事实写两遍，而两份必然分叉。`v_trace` 里
  `event_log` 那一路的 `tick_id` 因此是 `NULL`，这是**有意为之而不是畳漏**
- 索引名统一为 `idx_<table>_correlation`（此前设计文槽里同时存在 `idx_log_corr` 与
  `idx_log_correlation` 两种写法）
- `docs/DESIGN.md § 9.1` 的版本口径修正：`PRAGMA user_version` 是版本真源
  （整数、不用建表就能读、空库上也能读），`schema_version` 表是**审计记录**

- `[llm.budget]` 更名为 `[budget]`，新增 `max_images_per_day = 20` 与 `[budget.per_purpose]`。
  更名理由：闸门现在统管 LLM 与生图，挂在 `llm` 下名不副实
- `[llm.routing]` 的值语义从「provider 名」改为「**模型别名**」。一个 provider 可以挂多个
  model，而「决策用旗舰、反思用便宜」这两件事可能在同一家。旧配置自动迁移并告警
- 插件类型 6 → **8**（新增 `image` / `source`）。不复用 `tool` 的理由：`tool` 是给 LLM
  挑选的，而 `image` / `source` 由推演循环决定——放进 `tool` 等于让 LLM 绕过预算闸门，违反 P3
- 数据库表 20 → **26**，另加 2 个视图。迁移拆为 `002_media.sql` / `003_sources.sql` /
  `004_observability.sql`，并对 `tick_log` / `activity_log` / `llm_usage` 补 `correlation_id` 列
- Web 页面 8 → **13**；API 端点 15 → **33**；SSE 事件新增 `media.created` /
  `source.ingested` / `log.entry` / `budget.exceeded`
- 成本估算从 ~$0.25/天 更新为 **~$0.39/天（~$11.6/月）**；存储从 ~1.4 MB/天
  更新为 **~1.45 MB/天 ≈ 532 MB/年**（另加图片文件 1.3–3.5 GB/年）
- **修正一处文档漂移**：`06-roadmap.md` 曾建议 `daily_usd_limit = 1.0` / `30.0`，
  与 `04-simulation-loop.md` 的 `2.0` / `40.0` 冲突。统一为 `2.0` / `40.0`，
  理由写在 `06-roadmap.md` § 5.1
- 版本规划：图片生成从 v0.5.0 **提前到 v0.2.0**；新增 v0.3.0「会自己找东西」；
  实施阶段新增 J（设置中心）/ K（可观测性）/ L（生图）/ M（联网检索）
- `README.md` 的插件类型表 6 → 8，新增五个特性小节（长相 / 自己找东西 / 成本可见 /
  详细日志 / 设置标注），文档索引补入分册 07–11
- `templates/alterego.toml` 头部注释补上第 2 层加载来源 `alterego/defaults.toml`。
  **本文件同时是「配置项权威参考」与 `alterego init` 的输入**，而
  `test_authoritative_template_covers_every_key` 断言内核认识其中每一个键 ——
  所以 v0.2.0 / v0.3.0 的新配置面只能以**注释**形式写在末尾附录里，
  「发布实现」与「取消注释」必须是同一次提交
- `pyproject.toml` 的 `[tool.hatch.build.targets.wheel.force-include]` 只保留 `templates`：
  包内数据文件由 `packages = ["src/alterego"]` 自动包含，重复声明只会让目录一挪位置就 build 失败
- ruff 忽略 `RUF001/002/003`（中文全角标点）与 `N818`（内核异常名不含 `Error` 后缀），
  两者都是既定风格而非疏忽，已在配置里写明理由
- `.gitignore` 放行 `logs/`、`instances/`、`exports/` 下的 `.gitkeep`：目录结构要能被
  clone 出来的代码感知，内容依然不进版本库
- **`update_emotion()` 新增 `block: ScheduleBlock | None` 参数**。原设计里它硬编码
  `update_fatigue(current.fatigue, elapsed, None)`，于是「规则 4 · 睡眠时每小时恢复 0.8」
  是一段**永不执行的代码**，`EmotionalEvent.fatigue_delta` 也从未被使用。
  领域层无 IO、无全局，日程块只能由调用方（`TickContext`）取当前时刻查好后传进来
- **`Emotion.label` 的词表由闭集改为开放**。原实现按 `infer_label()` 的输出做校验，
  会把「感动」「委屈」「恼火」这类合法标签拒之门外 —— 那 10 个词写在同一份文档的
  `01-architecture.md § 3.2` 里。现在：`infer_label()` 的 8 词封闭集合是
  **降级路径**的词表，`label` 本身只有非空约束。测试加了一条不变量保证两者不分叉
- **`ScheduleBlock` 的字段名以 DDL 为准**（`start_at` / `end_at` 而非 `start` / `end`）。
  物理列名是唯一能往返的真源，草图跟着改
- **`strength_at()` 的公式以 `04-simulation-loop.md § 7.2` 为准**（λ 按记忆类型取，
  半衰期 episodic 7 天 / semantic 180 天 / emotional 365 天）。
  `01-architecture.md § 3` 曾给出 `exp(-0.05 × days) × (1 + log1p(recall_count))` ——
  单一衰减率，与同一份文档里「三种记忆半衰期不同」直接矛盾，且 `log1p` 会让
  回忆 1 次就翻倍（真实的复习效应是 `1 + 0.35 × recall_count`）

### 修复

- **`kernel/plugin.py` 的 facade 转发会让 import 期直接炸掉**：`OwnedBus.subscribe`
  引用了未导入的 `Handler` / `Subscription`
- `kernel/loader.py` 有两个真 bug：`_REQUIREMENT_RE` 里比较符可选，于是把
  `"storage.x 0.1.0"`（漏写 `>=`）当成「无约束」静默放过；`import_plugin_class` 只捕获
  `ImportError`，插件文件里的 `SyntaxError` 会穿透成内核崩溃
- `kernel/logging.py` 的 webhook 脱敏正则跨不过路径里的 `/`，而真实的 webhook 地址
  几乎都带路径（`https://host/cgi-bin/hook/send?key=...`）——**安全过滤器「看起来在工作」
  是最糟的状态**；同时单组模式把整个 URL 前缀一起盖掉，导致日志里看不出是哪个 webhook 泄露的
- `kernel/manifest.py` 的 `api_version` 不兼容报错用了重复关键字参数，`TypeError` 会盖掉
  真正的错误信息
- `OwnedRegistry.__slots__` 未按字母序排列（RUF023）
- 示例插件的 `plugin.toml` 用了 `type = "choice"` 与 `type = "int"`，两者都不是合法类型
  （`choices` 是 `string` 的约束，`int` 应写作 `integer`）——被端到端探针在提交前抓出
- `tests/test_packages.py` 的 import 排序与 `list.extend` 写法（本地那次 `ruff check`
  跑在这个文件出现**之前**，之后只补跑了 pytest，于是 CI 才第一次看到它）
- **三份互相矛盾的 `v_trace` DDL**（设计分册 03、09 和真实迁移文件各一份）。
  它们已经在列名、主键类型、索引名上全部分叉，而 03 分册那份直接引用了不存在的
  `event_log.tick_id`，一旦有人真的去执行就会出现 `no such column: tick_id`。
  修法不是「把三份改一致」，而是**只留一份**：物理 schema 的唯一出处是
  `migrations/*.sql`，分册里的 DDL 全部换成「为什么是这些列」的说明。
  复制一份 DDL 的代价不是多打几行字，而是从那一刻起存在两个「真相」——而它们必然分叉
- `v_trace` 的选择列与 `ORDER BY` 不兼容（视图列名在前者里是 `step`、后者拼错成
  `tick_id`），且 `event_log` 那一路漏了 `payload_json`
- `Emotion.__post_init__` 的三维范围校验被写坏：`arousal` 那条实际在判 `valence`，
  于是 `valence=1.5` 报的是「arousal 必须在 0 ~ 1」。两个字段的区间不同（-1~1 与 0~1），
  所以这个 bug 只在**恰好落在两个区间之间**的值上暴露 —— 正好是测试挑的那几个
- `decay_toward()` 的 `0.5 ** x` 在 typeshed 里返回 `Any`，mypy strict 的
  `no-any-return` 会拦下来；改为显式标注 `factor: float` 并说明原因（否则下一个人会
  把标注当成冗余删掉）
- `infer_label()` 的「疲惫」特例判据是 `arousal < 0.25 and valence < 0.1`，
  但它**先于**其他分支执行，于是吞掉了整个低唤醒区：`(-0.5, 0.2)` 得到「疲惫」而不是
  「低落」、`(0.0, 0.2)` 也是「疲惫」而不是「平静」。测试原先按文档的标签顺序写了期望，
  失败后**没有去改规则**（改规则要重做验收表），而是改成断言可达区间 + 新增一条测试
  把这条规则的真实代价钉死，并在 `04-simulation-loop.md § 6.2` 补了「已知取舍」说明

### 安全

- **抓取的外部内容一律视为不可信输入**（`docs/adr/0009`）。五条硬规则：
  ① 绝不进 system prompt ② 绝不当指令执行 ③ 显式声明不可信 ④ 注入模式检测命中即丢弃
  ⑤ 长度截断 4000 字符。**规则 4 是「丢弃」而不是「清洗」**——清洗永远赶不上绕过，
  而丢掉一条新闻的代价是零，注入成功的代价是人格改写或隐私泄露
- 密钥只显示「来源环境变量名 + 是否已设置」，UI 不读也不写密钥值
  （`[settings] secret_write = "env_only"`）

### 文档

- 新增 `docs/adr/0008` 角色形象一致性锚定在一张定妆照上
- 新增 `docs/adr/0009` 抓取的外部内容一律视为不可信输入
- 新增 `docs/adr/0010` 每个配置项都必须携带可展示的元数据
- 新增 `docs/design/07` ~ `11` 五个分册
- 同步 `DESIGN.md`（§ 6.1 / § 9.2 / § 10.2 / § 12 / § 13 / § 15 / § 17）
- 同步 `01-architecture.md`（§ 1.1 新增模块与依赖约束；§ 1.2 红线 5 & 6；
  § 8.3 降级路径新增 5 行并补两条通用规则）
- 同步 `02-plugin-api.md`（§ 2 插件类型表 + 两个新小节的「为什么不复用 tool」）
- 同步 `03-data-model.md`（ER 图、26 表、§ 3.1/3.2/3.3 六个新表与两个视图、§ 10 容量、
  新增 § 11 数据一致性硬要求）
- 同步 `04-simulation-loop.md`（§ 4.1 第 11 种意图 `research`、§ 10.1 路由语义变更、
  § 10.4 预算更名与漂移修正）
- 同步 `05-channels.md`（33 个端点、权限分级表、13 个页面标签、
  「内心」与「日志」为什么不是一个页面）
- 同步 `06-roadmap.md`（版本、阶段 J–M、里程碑、成本、存储、风险 R17–R20、监控指标）
- `docs/adr/0006` 发行版选型写成数据文件，不写进内核代码
- `docs/adr/0007` `PluginContext.bus` / `.registry` 使用带归属的视图
- `docs/design/01-architecture.md` § 2.2 更新配置加载优先级与实现要点表
- `docs/design/01-architecture.md` § 4 的 `Stage` 补上 `depends_on`；§ 2.7 从
  「`plugin.py` / `loader.py` / `manager.py`」重写为插件体系模块表，并写明拆分理由
- `docs/design/02-plugin-api.md` § 4.1 修正接口包位置（`alterego/interfaces/`，是包不是单模块）；
  § 6 补上「为什么是包」；§ 5 新增「`bus` 与 `registry` 是带归属的视图」小节（ADR-0007）
- `docs/design/05-channels.md` § 11.1 修正 `SecretFilter` 示例的两处实现 bug
- `docs/DESIGN.md` § 13 目录树展开 `kernel/manifest.py`、`kernel/context.py` 与 `interfaces/`
- `docs/adr/0001` 引入架构决策记录机制
- `docs/guide/README.md` 说明上手指南**为什么现在还是空的**（还没有能照着敲的命令）
- `plugins/example_plugin/README.md` 说明示例插件演示的五件事，以及它为什么放在版本库里
- `docs/design/03-data-model.md` § 8 重写：§ 8.1 四个真实迁移文件与「外键不能指向未来」、
  § 8.2 迁移流程 mermaid 与「`user_version` 是真源、`schema_version` 是审计」、
  § 8.3 迁移文件**五个禁止项**（每个附理由）+「幂等靠版本号，不靠 `IF NOT EXISTS`」、
  § 8.4 版本兼容双向检查（`version == 0` 属合法）+ 报错必须带库文件路径、
  § 8.5 移除 `db rollback`（反向迁移木有设计，`reversible` 目前只是声明式元数据）
- `docs/design/08-external-sources.md § 8` 删除重复的 `source_item` / `source_feed` /
  `source_query` DDL，改为指向 `03-data-model.md § 3.2` 的「为什么这一列必须是它」表
- `docs/design/09-observability.md § 7` 删除重复的 `log_entry` / `v_trace` DDL 与 § 2.3 的
  `v_cost_daily` DDL，改为「少一列会失去什么」表，并标明完整 DDL 的唯一出处
- `docs/DESIGN.md § 12` 目录树展开 `storage/sqlite/`（`connection.py` / `migrator.py` /
  `backend.py` / `migrations/`）；`CONTRIBUTING.md` 项目结构树同步，并修正 `migrations/`
  曾被错画在 `src/alterego/` 顶层的问题
- `docs/design/01-architecture.md § 3.2` 的 `domain/` 三个模块草图对齐实现：
  `update_emotion` 的签名（`EmotionEvent` → `EmotionalEvent`、新增 `block` 参数）、
  `memory.py` 的 `strength_at(memory, now)`（去掉 `decay_rate` 参数，公式以 04 分册为准）、
  `schedule.py` 的字段名（以 DDL 为准）。并加了一条说明：**草图描述的是目标接口，
  代码才是权威**
- `docs/design/04-simulation-loop.md § 6 / § 7` 同步四处：§ 6.2 补 `infer_label` 的
  「已知取舍」（疲惫特例吞掉低唤醒区）、§ 6.3 补齐 `EmotionalEvent` 定义与
  `decay_toward` 的类型标注、§ 6.4 重写 `update_emotion` 并说明「`block` 为什么是参数」、
  § 7.3 把 `maybe_resurrect` 拆成纯函数 `should_resurrect` / `resurrect` 与
  Sim 层的副作用版本
- `docs/design/03-data-model.md` 新增 § 6.4 `RetrievalWeights` 与 § 6.9「领域模型的归属」
  （表镜像 vs 业务规则 vs SQL 的三行分工表），并补 § 6.9.1 只有查询才需要的两个类型
  （`MemorySearchHit` / `MemoryStats`）
- `docs/DESIGN.md` § 13 标注 `domain/` 三个已实现模块，变更记录加 v0.2.1 行
- `docs/design/06-roadmap.md` 阶段 D 的进度说明更新为「存储层 + 领域层第一条纵向切片已完成」

### 架构

- **新增 `domain/media.py` 与 `domain/untrusted.py`，均为纯函数且不得下沉到插件**。
  如果一致性机制住进 `image` 插件，第二个生图插件就能绕过它，保证不复存在
- **新增两条架构红线，并把脚本从六组扩到七组（20 项 → 22 项）**：
  红线 6「不得自行构造 logging handler」（否则脱敏与轮转失效）、
  红线 7「LLM 调用必须经过 `ctx.llm()`」（否则无法计量、无法路由、无法受限）。
  两条的共性是：**违反了以后问题会在很久以后才暴露**——一条在泄露那天，一条在收到账单那天。
  扩组时顺手删掉了一条**永远不可能失败**的假检查（一个 `pattern=""` 的 `check_required`），
  因为一个不会失败的检查比没有检查更糟——它让「共 20 项全部通过」失去意义
- `kernel/settings.py` 成为设置元数据的**单一真源**，Web 设置页与 CLI `config` 子命令
  渲染同一份数据，杜绝「文档写一套、前端写一套」的漂移
- **内核不再知道任何具体技术名**。`kernel/config.py` 里原本硬编码的
  `default_provider` / `backend` / 路由分层默认值移入随包数据文件 `src/alterego/defaults.toml`，
  由 `Config.load()` 作为最低优先级层合并。首次真机运行 `check_architecture.sh` 时这 6 处
  被第 1 组红线挡下——红线生效，且修的是代码而不是规则。见 ADR-0006
- **`PluginContext.bus` / `.registry` 改为带归属的视图（ADR-0007）**。原设计承诺
  「`on_load` 失败 → 回滚已注册的实现」与「热重载 → 清掉旧实例的订阅」，
  但同一份文档里的示例代码调用 `register()` / `subscribe()` 时并不传 `owner=`——
  漏填的插件会在热重载后留下**指向半死对象的悬空引用**，而且只在长时间运行后才显形。
  现在归属由 `OwnedRegistry` / `OwnedBus` 自动记账，插件作者没法写错。
  这是设计原则 P3（机制约束优于提示词祈祷）在本项目里最典型的一次应用
- **`plugin.py` 触到 900 行红线，按职责拆为三个模块**：`manifest.py`（596 行，
  甚至不知道事件总线存在）/ `context.py`（284 行）/ `plugin.py`（107 行，纯门面）。
  拆分不是为了「文件小一点」，而是让架构红线与 900 行上限能真正约束住内核。
  `tests/test_kernel_plugin.py` 从 `alterego.kernel.plugin` 导入 11 个公开名字，
  facade 的兼容性因此是被测试锁住的契约
- 架构红线第 2 / 4 / 6 组（领域层纯净、分层不越级、插件互不依赖）从
  `⊘ 目录不存在，跳过` 变为**实际生效**；连同新增的第 7 组，当前 **22/22 全部通过**

---

## [0.1.0] - 2026-09-15

### 新增

**设计文档（阶段 A）**

- `docs/DESIGN.md` —— 总设计文档（单一事实来源）
  - 七条设计原则（内核无知 / 显式优于隐式 / 机制约束优于提示词祈祷 / 可插拔优于可配置 / 标准库优先 / 可复现 / 文档与代码同生共死）
  - 四层架构 + 插件生态层
  - 三种时间维度（真实时间 / 虚拟时间 / Tick）
  - 完整目录结构
  - 成功标准 S1–S7

- `docs/design/01-architecture.md` —— 架构分册
  - 9×9 依赖矩阵
  - 四条 CI 架构红线
  - 内核模块设计（错误体系 / 配置 / 事件总线 / 服务注册表 / 时钟 / 调度器）
  - 启动与关闭时序、并发模型、三级错误隔离、降级路径

- `docs/design/02-plugin-api.md` —— 插件规范分册（`api_version = 1`）
  - 六类插件（llm / storage / channel / capability / stage / tool）
  - 完整的 `plugin.toml` 清单参考与校验规则
  - 插件生命周期与状态机、热重载机制、熔断器
  - 三个完整实战示例（钉钉渠道 / 天气心情阶段 / 拍照意图）
  - 开发 Checklist 与常见错误表

- `docs/design/03-data-model.md` —— 数据模型分册（`schema_version = 1`）
  - 20 张表的完整 DDL、ER 图、索引策略
  - 记忆检索算法（FTS5 BM25 + 加权重排，含公式）
  - 中文分词处理方案
  - 迁移机制与备份导出

- `docs/design/04-simulation-loop.md` —— 推演循环分册
  - 六阶段流水线（感知 / 反思 / 意图 / 行动 / 表达 / 持久化）
  - 10 种意图类型 + `reach_out` 的 6 种动机
  - **打扰预算与降级语义**（被拦截的意图降级为内心独白而非丢弃）
  - 情绪模型（4 条规则）、记忆模型（3 类 + 强度公式 + 巩固 + 复活）
  - 人设生成与演化、NPC 推演、成本控制、Prompt 工程、可解释性

- `docs/design/05-channels.md` —— 渠道分册
  - 单向 / 双向渠道的核心约束说明
  - Web 界面（7 个页面、SSE 实时推送、15 个 API 端点）
  - 钉钉加签、企业微信 webhook、Telegram
  - 通知路由、重试与限流、安全加固

- `docs/design/06-roadmap.md` —— 路线图分册
  - 三条主线与 A–I 分阶段推进
  - 版本规划与兼容性承诺
  - M1–M7 里程碑与可验证的验收标准
  - 成本估算（LLM ~$7.5/月、存储 ~515MB/年、开发 ~35–45 人日）
  - R1–R16 风险清单与对策

**项目治理（阶段 B）**

- `README.md` —— 项目介绍、快速开始、文档索引
- `CONTRIBUTING.md` —— 贡献指南、提交规范、PR 检查清单
- `CHANGELOG.md` —— 本文件
- `LICENSE` —— MIT
- `scripts/check_architecture.sh` —— 架构红线检查
- `.github/workflows/ci.yml` —— 持续集成
- `docs/adr/` —— 架构决策记录

### 架构

- 项目采用 Python 3.11+（选型理由见 `docs/adr/0001`）
- 依赖极简：仅 `pydantic` + `httpx` 为必需依赖

---

## 版本链接

- [Unreleased](https://github.com/LMG-arch/alter-ego/compare/v0.1.0...HEAD)
- [0.1.0](https://github.com/LMG-arch/alter-ego/releases/tag/v0.1.0)

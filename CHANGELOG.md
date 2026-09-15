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

**项目脚手架**

- `pyproject.toml`、`.gitignore`、MIT `LICENSE`、`README.md`、`.github/` 下的 CI 工作流与 issue / PR 模板
- 架构红线检查脚本 `scripts/check_architecture.sh`：六组检查强制保证分层依赖方向

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

**测试**

- `tests/test_kernel_config.py` —— 41 个用例覆盖加载优先级、键折叠、全部校验分支、
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
- `kernel/manifest.py` —— `plugin.toml` 的解析与校验。六条硬校验（`id` 形如 `<kind>.<name>`、
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

**测试**

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

### 变更

- `templates/alterego.toml` 头部注释补上第 2 层加载来源 `alterego/defaults.toml`
- `pyproject.toml` 的 `[tool.hatch.build.targets.wheel.force-include]` 只保留 `templates`：
  包内数据文件由 `packages = ["src/alterego"]` 自动包含，重复声明只会让目录一挪位置就 build 失败
- ruff 忽略 `RUF001/002/003`（中文全角标点）与 `N818`（内核异常名不含 `Error` 后缀），
  两者都是既定风格而非疏忽，已在配置里写明理由
- `.gitignore` 放行 `logs/`、`instances/`、`exports/` 下的 `.gitkeep`：目录结构要能被
  clone 出来的代码感知，内容依然不进版本库

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

### 文档

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

### 架构

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
  `⊘ 目录不存在，跳过` 变为**实际生效**，当前 **20/20 全部通过**

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

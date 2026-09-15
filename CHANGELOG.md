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

### 变更

- `templates/alterego.toml` 头部注释补上第 2 层加载来源 `alterego/defaults.toml`
- `pyproject.toml` 的 `[tool.hatch.build.targets.wheel.force-include]` 只保留 `templates`：
  包内数据文件由 `packages = ["src/alterego"]` 自动包含，重复声明只会让目录一挪位置就 build 失败
- ruff 忽略 `RUF001/002/003`（中文全角标点）与 `N818`（内核异常名不含 `Error` 后缀），
  两者都是既定风格而非疏忽，已在配置里写明理由

### 文档

- `docs/adr/0006` 发行版选型写成数据文件，不写进内核代码
- `docs/design/01-architecture.md` § 2.2 更新配置加载优先级与实现要点表
- `docs/adr/0001` 引入架构决策记录机制

### 架构

- **内核不再知道任何具体技术名**。`kernel/config.py` 里原本硬编码的
  `default_provider` / `backend` / 路由分层默认值移入随包数据文件 `src/alterego/defaults.toml`，
  由 `Config.load()` 作为最低优先级层合并。首次真机运行 `check_architecture.sh` 时这 6 处
  被第 1 组红线挡下——红线生效，且修的是代码而不是规则。见 ADR-0006

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

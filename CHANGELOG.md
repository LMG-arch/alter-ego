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

- 项目脚手架：`pyproject.toml`、`.gitignore`、MIT `LICENSE`、README、CI 工作流
- 架构红线检查脚本 `scripts/check_architecture.sh`：六组 grep 检查强制保证分层依赖方向

### 文档

- `docs/adr/0001` 引入架构决策记录机制

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

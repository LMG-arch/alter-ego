<!--
  感谢你的 PR！
  请逐项确认下面的清单。这看起来麻烦，但每一项都对应过一次真实的返工。
-->

## 变更描述

<!-- 说清楚「为什么」改，而不只是「改了什么」 -->

## 关联 Issue

<!-- 用 Closes #123 / Fixes #123 / Refs #123 -->

Closes #

## 变更类型

- [ ] `feat` 新功能
- [ ] `fix` bug 修复
- [ ] `refactor` 重构（不改变行为）
- [ ] `perf` 性能优化
- [ ] `docs` 仅文档
- [ ] `test` 测试
- [ ] `build` / `ci` / `chore`
- [ ] `arch` 架构变更（**必须在下方说明并附 ADR**）

## 影响的模块

- [ ] `kernel`（内核）
- [ ] `domain`（领域）
- [ ] `sim`（推演）
- [ ] `storage`（存储）
- [ ] `llm`
- [ ] `channels`（渠道 / 接入）
- [ ] `capabilities`（能力）
- [ ] `npc`
- [ ] `cli`
- [ ] `plugin`（插件系统）
- [ ] `docs`

---

## 测试情况

<!-- 说明你测了什么、怎么测的、结果如何。贴关键输出。 -->

**本地运行结果**：

```
$ pytest -q --cov=alterego --cov-report=json
...

$ python scripts/check_coverage.py
...

$ ruff check src tests plugins scripts
...

$ bash scripts/check_architecture.sh
...
```

- [ ] 新增功能有对应测试
- [ ] bug 修复有**先失败后通过**的复现测试
- [ ] `python scripts/check_coverage.py` 通过（`kernel/` ≥ 90%，`domain/` ≥ 95%，`sim/` ≥ 85%，全局 ≥ 85%）
- [ ] 涉及推演的改动使用了固定的 `ctx.rng` 种子（可复现）

**手动验证步骤**（如适用）：

1.
2.

---

## 文档同步 ⚠️ 必填

**本项目要求：代码行为变更必须同步更新设计文档。实现与文档冲突时，以文档为准。**

- [ ] 已检查，**无需**更新文档
- [ ] 已更新以下文档：

| 文档 | 更新内容 |
| --- | --- |
| `docs/DESIGN.md` | |
| `docs/design/01-architecture.md` | |
| `docs/design/02-plugin-api.md` | |
| `docs/design/03-data-model.md` | |
| `docs/design/04-simulation-loop.md` | |
| `docs/design/05-channels.md` | |
| `docs/design/06-roadmap.md` | |

- [ ] `CHANGELOG.md` 的 `[Unreleased]` 已按分类（新增/变更/修复/文档/架构）追加

---

## 架构红线 ⚠️ 必填

- [ ] 未触碰架构红线（`scripts/check_architecture.sh` 通过）
- [ ] 触碰了架构红线，已提交 ADR：`docs/adr/____-__________.md`

**如果触碰了，说明原因**：

<!--
  例如：
  「内核需要感知时间，因此引入了 Clock Protocol。
   这不是'内核知道具体技术'——Clock 是抽象，RealClock 在 kernel 外实现。
   ADR-0005」
-->

**依赖方向是否改变**：

- [ ] 否
- [ ] 是 → 已附 ADR 并更新 `docs/design/01-architecture.md` 的依赖矩阵

---

## 新增依赖

- [ ] 未新增依赖
- [ ] 新增了依赖，理由如下：

| 包名 | 类型 | 理由 | 标准库为何不可行 | 是否活跃维护 |
| --- | --- | --- | --- | --- |
| | 必需 / 可选 | | | |

> 提醒：必需依赖必须先写 ADR（参见 `CONTRIBUTING.md § 新增依赖`）。
> 优先考虑做成**可选插件**放在 `[project.optional-dependencies]`。

---

## 兼容性

- [ ] 向后兼容
- [ ] 有破坏性变更 → 已在提交信息中标注 `!` 与 `⚠️ BREAKING`，并说明迁移方式

**数据库变更**：

- [ ] 无 schema 变更
- [ ] 有 schema 变更 → 已提供迁移脚本 `migrations/NNN_*.sql`，并更新 `schema_version`
- [ ] 是破坏性迁移 → 已确认会自动备份

**插件 API 变更**：

- [ ] 无变更
- [ ] 有变更 → 已说明是否需要提升 `api_version`，以及旧插件的兼容策略

---

## 安全与隐私

- [ ] 无安全影响
- [ ] 涉及密钥处理 → 已确认密钥不进代码库、日志已脱敏
- [ ] 涉及 Web 暴露 → 已确认认证、CSP、CSRF 防护
- [ ] 涉及用户数据 → 已确认 `export --anonymize` 覆盖新字段

---

## 截图 / 输出示例

<!-- 涉及 UI、CLI 输出、`alterego why` 等可观测行为的改动，请贴出来 -->

---

## 检查清单回顾

- [ ] 本地 `pytest` 全绿
- [ ] 本地 `python scripts/check_coverage.py` 通过
- [ ] 本地 `ruff check src tests plugins scripts && ruff format --check src tests plugins scripts` 通过
- [ ] 本地 `bash scripts/check_architecture.sh` 通过
- [ ] 提交信息符合 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)
- [ ] 已阅读上述全部内容，没有敷衍勾选

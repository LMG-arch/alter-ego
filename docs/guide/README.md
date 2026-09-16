# 指南

本目录放「先做什么、再做什么」的步骤说明。

| 目录 | 回答的问题 | 给谁看 |
| --- | --- | --- |
| `docs/design/` | 为什么这样设计？ | 改代码的人 |
| `docs/guide/` | 我现在该敲什么？ | 用它的人 |
| `docs/adr/` | 当时为什么这么定？ | 想推翻某个决定的人 |

## 现在有什么

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| [`plugin-development.md`](plugin-development.md) | 写一个自己的 LLM / 渠道 / 推演阶段 / 能力插件，包括清单字段、九个钩子、调试与热重载 | ✅ **可照做**（v0.1.2） |
| `getting-started.md` | 安装 → 生成人格 → 首次运行 → 在浏览器里看到它的「内心」→ 收到第一条主动消息 | ⬜ 等阶段 F |

## 为什么只有一篇

因为指南的硬条件是**里面每条命令都能跑**。

- `plugin-development.md` 合格：`alterego plugins list|doctor|info|reload|reset`
  五条命令都已存在，`plugins/` 下有 4 个能跑的插件（`example_plugin` 是模板，
  `obsidian_vault` / `dataset_exporter` / `study` 是真实用途），所有字段都在代码里。
- `getting-started.md` 不合格：`alterego run` 还不存在（推演引擎在阶段 E/F，见
  [06-roadmap.md](../design/06-roadmap.md) § 3），也还没有任何一个能真实通信的渠道插件
  （阶段 G）。「从零到第一次收到它主动发来的消息」这条路**今天走不通**。

一篇描述尚不存在的命令的指南，比没有指南更糟：用户会照着敲、会失败，
然后会怀疑是自己装错了。这违反 P7（文档与代码同生共死）里
「文档描述的行为必须能被验证」这一条。

在那之前，想知道怎么上手，请读根目录的
[README.md](../../README.md)「快速开始」一节——那里只写**已经能用**的部分，
并且每加一个能用的命令就同步更新一次。

# AGENTS.md · 给 AI 编码助手的项目规则

> 人看的完整版见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。
> 本文件是**精简、可执行**版：每条规则都告诉你**怎么验证自己有没有违反**。
>
> **冲突时以 [`docs/DESIGN.md`](docs/DESIGN.md) 与 [`docs/design/`](docs/design/) 为准。**

---

## 1. 开工前必读

| 你要改的东西 | 必须先读 |
| --- | --- |
| 任何代码 | [`docs/DESIGN.md`](docs/DESIGN.md) § 3（原则）与 § 9（领域模型） |
| `kernel/` | [`docs/design/01-architecture.md`](docs/design/01-architecture.md) § 1–2 |
| `domain/` | [`docs/design/03-data-model.md`](docs/design/03-data-model.md) + `04-simulation-loop.md` § 6–8 |
| `sim/` | [`docs/design/04-simulation-loop.md`](docs/design/04-simulation-loop.md) 全文 |
| `plugins/` 或新增扩展点 | [`docs/design/02-plugin-api.md`](docs/design/02-plugin-api.md) § 16（Checklist） |
| 任何配置项 | [`docs/design/10-settings-center.md`](docs/design/10-settings-center.md) |
| 任何 LLM 调用 | [`docs/design/07-model-routing-and-media.md`](docs/design/07-model-routing-and-media.md) |
| 任何外部内容 | [`ADR-0009`](docs/adr/0009-fetched-content-is-untrusted.md) |

**不确定该读哪篇** → 看 [`docs/DESIGN.md`](docs/DESIGN.md) § 17 分册索引。

---

## 2. 七条设计原则

| 编号 | 原则 | 违反时的典型症状 |
| --- | --- | --- |
| **P1** | 内核无知 | `kernel/` 里出现了 `import sqlite3` / `openai` / `httpx` |
| **P2** | 显式优于隐式 | 用模块级单例、从全局取配置、隐式默认值 |
| **P3** | 机制约束优于提示词祈祷 | 用提示词「请不要发太频繁」代替代码硬约束 |
| **P4** | 可插拔优于可配置 | 加了 `if config.backend == "x"` 分支 |
| **P5** | 标准库优先 | 为一个小功能引入新依赖 |
| **P6** | 可复现 | 用了 `random` / `datetime.now()` / 字典序不确定的迭代 |
| **P7** | 文档与代码同生共死 | 改了行为但没改文档 |

---

## 3. 五条硬性禁止

**这五条各有自动化检查，写错了会被 CI 拒绝。**

```bash
# ✗ 禁止：在 sim/ 或 domain/ 里取当前时间
datetime.now()            # → 用 ctx.clock.now() / ctx.now()

# ✗ 禁止：在 sim/ 里用全局随机
import random             # → 用 ctx.rng

# ✗ 禁止：绕过 ctx.llm() 直接调 LLM
httpx.AsyncClient(base_url="https://api...")   # → ctx.llm().complete(...)

# ✗ 禁止：自行构造 logging handler
logging.FileHandler(...)  # → 用 ctx.logger，脱敏与轮转由内核负责

# ✗ 禁止：新增配置项不带说明
daily_limit: float = 2.0  # → 必须注册 Setting(description=..., effect=...)
```

**自行验证**：

```bash
bash scripts/check_architecture.sh --verbose   # 七组红线
python -m pytest tests/test_settings_metadata.py -q
```

---

## 4. 每次改动的固定动作

```
写代码 → 写测试 → 跑全部门禁 → 同步文档 → 更新 CHANGELOG → 提交
```

**六步缺一不可。** 完整的门禁命令：

```bash
python -m ruff format src tests plugins
python -m ruff check src tests plugins
python -m mypy src/alterego
python -m pytest tests -q
python -m pytest tests -q --cov=alterego
bash scripts/check_architecture.sh
```

**范围外的东西不要顺手改。** 发现别的问题 → 记下来，单独一次提交。

---

## 5. 硬性技术约束

| 约束 | 值 | 为什么 |
| --- | --- | --- |
| 单文件行数 | **≤ 900** | 超过就拆（`config.py` 已 656，接近上限） |
| 行长 | ≤ 100 | formatter 处理 |
| 类型注解 | 全部 | `mypy strict = true` |
| 必需依赖 | **只有两个**：`pydantic`、`httpx` | P5。新增必需依赖需写 ADR |
| `kernel/` 覆盖率 | ≥ 90% | CI 阻断 |
| `domain/` 覆盖率 | ≥ 95% | 纯函数，必须高覆盖 |
| `sim/` 覆盖率 | ≥ 85% | CI 阻断 |
| 全局覆盖率 | ≥ 85% | CI 阻断 |

**测试标记**：`@pytest.mark.slow` / `.integration` / `.golden` / `.architecture`

---

## 6. 代码风格要点

- **中文 docstring**，说清楚**做什么**与**为什么**（不是重复代码）
- 模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`
- **不要 import 单例**，通过参数或 `ctx` 拿依赖
- 生产代码不用 `print`（CLI 输出与 console 渠道例外）
- 领域层（`domain/`）**无 IO**：不开文件、不连数据库、不读环境变量
- 新函数优先写成**纯函数**——好测、可复现、无副作用

---

## 7. 新增配置项的正确姿势

```python
# 1. 配置 dataclass 加字段
@dataclass(frozen=True, slots=True)
class MediaSelfieConfig:
    daily_limit: int = 3
    """每天最多主动拍几张自拍。"""

# 2. 注册展示元数据（缺了 test_settings_metadata.py 会挂）
SETTING_METADATA[MediaSelfieConfig] = {
    "daily_limit": Setting(
        key="media.selfie.daily_limit",
        label="每天最多主动拍几张自拍",
        description="它自己决定想拍照时，一天最多拍几张。",
        kind=SettingKind.INT,
        default=3,
        minimum=0,
        maximum=20,
        unit="张",
        effect="调高后相册增长更快、生图费用同比上升；调到 0 等于关掉主动拍照，它仍可被要求拍照。",
        group="形象",
        requires_restart=False,
    ),
}
```

**`description` 与 `effect` 的分工**：

| 字段 | 回答 | 反例（会被 CI 拦） |
| --- | --- | --- |
| `description` | 这个设置**是什么** | 「自拍数量」 |
| `effect` | 改了**会发生什么** | 「影响自拍数量」/「可能会有变化」 |

---

## 8. 提交规范

```
<类型>(<范围>): <描述>
```

类型：`feat` `fix` `docs` `refactor` `perf` `test` `build` `ci` `chore` `arch`

范围：`kernel` `domain` `sim` `storage` `llm` `channels` `capabilities` `npc` `cli` `web` `plugin` `image` `source` `observability` `settings` `deps` `docs`

**破坏性变更**：类型后加 `!`，正文写清迁移方式。
**`arch` 类型必须附 ADR。**

---

## 9. 什么时候停下来问

**不要自作主张**的情况：

| 情况 | 为什么停 |
| --- | --- |
| 需要新增**必需**运行时依赖 | P5，且不可逆（依赖一旦进来就很难出去） |
| 要改分层依赖方向 | P1，影响面覆盖整个仓库 |
| 要改插件 API（`api_version`） | 破坏所有已有插件 |
| 要改数据库 schema 且无法干净迁移 | 影响用户现有数据 |
| 设计文档与需求冲突 | 先改文档或写 ADR，不要「先用代码跑起来」 |
| 需求本身有歧义 | 猜错比问一次的代价大得多 |

**可以自己决定**的情况：实现细节、命名、测试组织、内部重构（不改行为）、文档措辞。

---

## 10. 一句话总结

> **这个项目里，文档不是描述代码的，文档是代码的依据。**
>
> 任何一次「代码先跑起来，文档后面补」都会让下一句话失效。
> 如果你写了代码但不知道该更新哪篇文档——**先找文档，再写代码**。

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
# → 至少要有中文 docstring 说清楚「改了会发生什么」，并在 templates/alterego.toml 里补一行
#   带注释的默认值（tests/test_kernel_config.py::test_authoritative_template_covers_every_key 会查）
# → 还要在 kernel/settings_catalog_*.py 里补一条展示元数据（tests/test_settings_metadata.py 会查）
```

> ✅ **设置项展示元数据已落地（v0.1.2）。** `Setting(description=..., effect=...)`、
> `SettingKind` / `Choice`、`SETTING_METADATA`（95 条 / 17 个段）与
> `tests/test_settings_metadata.py` 都在代码里，骨架是 `kernel/settings.py`、
> `kernel/settings_catalog.py`，命令行入口是 `alterego config`（`cli_config.py`）。
> 写新配置项的完整姿势见 [`docs/design/10-settings-center.md`](docs/design/10-settings-center.md)。
> ✅ **展示命令**：`alterego config explain <键>` 回答「它是什么、改了会怎样、现在是多少」。
> 依据：[ADR-0010](docs/adr/0010-every-setting-carries-display-metadata.md)。

**自行验证**：

```bash
bash scripts/check_architecture.sh --verbose   # 七组红线，共 23 项
python -m pytest tests/test_kernel_config.py tests/test_packages.py -q
```

---

## 4. 每次改动的固定动作

```
写代码 → 写测试 → 跑全部门禁 → 同步文档 → 更新 CHANGELOG → 提交
```

**六步缺一不可。** 完整的门禁命令：

```bash
python -m ruff format src tests plugins scripts
python -m ruff check src tests plugins scripts
python -m mypy src/alterego
python -m pytest tests -q --cov=alterego --cov-report=json
python scripts/check_coverage.py
bash scripts/check_architecture.sh
```

⚠️ **不要漏掉 `scripts`**：CI 的 ruff 是 `git ls-files '*.py'`，范围比上面这两个命令大。
本地不检查 `scripts/`，就等于让脚本的 lint 错误留到 CI 才炸。

**范围外的东西不要顺手改。** 发现别的问题 → 记下来，单独一次提交。

---

## 5. 硬性技术约束

| 约束 | 值 | 为什么 |
| --- | --- | --- |
| 单文件行数 | **≤ 900** | 超过就拆。`kernel/config.py` 已**顶到 900**（零余量），再加配置段请开卫星模块，先例是 `kernel/config_study.py` 与 `kernel/config_values.py` |
| 行长 | ≤ 100 | formatter 处理 |
| 类型注解 | 全部 | `mypy strict = true` |
| 必需依赖 | **只有两个**：`pydantic`、`httpx` | P5。新增必需依赖需写 ADR |
| `kernel/` 覆盖率 | ≥ 90% | CI 阻断（`scripts/check_coverage.py`） |
| `domain/` 覆盖率 | ≥ 95% | 纯函数，必须高覆盖，CI 阻断 |
| `sim/` 覆盖率 | ≥ 85% | CI 阻断 |
| 全局覆盖率 | ≥ 85% | CI 阻断 |

**四个下限由 [`scripts/check_coverage.py`](scripts/check_coverage.py) 按包核对**，跑在 CI 的测试作业里。
coverage 自带的 `--cov-fail-under` 只能表达一个全局下限，所以复用同一次 `--cov-report=json` 的产物自己算。
改下限要同时改那个脚本和这张表。

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

**今天（v0.1.2）必须做的是这四步**：

1. 配置 `dataclass` 加字段，字段下面写**中文 docstring** 说清「改了会发生什么」。
2. 在 `templates/alterego.toml` 里补一行带注释的默认值——`test_authoritative_template_covers_every_key`
   会核对模板与配置类一一对应，缺了直接红。
3. 如果是新增**段**，还要在 `kernel/config.py` 的 `Config` 上挂好；嵌套的 dataclass 段
   会被 `_split_known` 递归检查，写错键名会被告警点名（见 `07-model-routing-and-media.md` § 3.1.1）。
   ⚠️ `kernel/config.py` 顶在 900 行，新段请开卫星模块（先例：`kernel/config_study.py`、
   `kernel/config_settings.py`）。
4. 在 `kernel/settings_catalog_*.py` 里注册展示元数据——`test_settings_metadata.py`
   会核对「每个配置字段都有元数据」「`effect` 说的是一句完整的话」「每个枚举选项都写了后果」。

**展示元数据的写法**（`SETTING_METADATA` / `Setting` / `SettingKind` 都已存在）：

```python
# 1. 配置 dataclass 加字段
@dataclass(frozen=True)
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

**三条约定**（`tests/test_settings_metadata.py` 逐条守着）：

- `effect` 必须是一句完整的话，不许写「可能有变化」这种什么都没说的句子；
- 枚举的每个 `Choice` 都要写 `consequence`（选了它会发生什么），而不只是 `label`；
- 元数据里的 `default` 必须与配置类里的默认值**逐字一致**（`Path("data")` 而不是 `"data"`）。

```python
# 1. 配置 dataclass 加字段
@dataclass(frozen=True)
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

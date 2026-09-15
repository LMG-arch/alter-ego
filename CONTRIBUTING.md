# 贡献指南

感谢你有兴趣改进 AlterEgo。本文件说明开发流程、代码规范与文档纪律。

---

## 目录

- [项目规则](#项目规则)
- [开发环境](#开发环境)
- [项目结构](#项目结构)
- [开发流程](#开发流程)
- [代码规范](#代码规范)
- [提交规范](#提交规范)
- [测试要求](#测试要求)
- [文档纪律](#文档纪律)
- [架构红线](#架构红线)
- [PR 检查清单](#pr-检查清单)
- [新增依赖](#新增依赖)

---

## 项目规则

本节是全文的索引。**下面每一条规则都有对应的强制手段**——
没有强制手段的规则在本项目里不算规则。

### 七条设计原则（判断一切设计问题的依据）

| 编号 | 原则 | 一句话 | 强制手段 |
| --- | --- | --- | --- |
| P1 | 内核无知 | 内核不知道任何具体技术 | 红线组 1（`kernel/` 禁 sqlite/openai/httpx） |
| P2 | 显式优于隐式 | 配置显式声明，依赖显式注入 | mypy strict + 禁止单例 import |
| P3 | 机制约束优于提示词祈祷 | 硬约束用代码，不靠「请不要」 | 打扰预算 5 项校验为纯函数并有测试 |
| P4 | 可插拔优于可配置 | 扩展靠写插件，不是加 if-else | 八类插件契约 + 红线组 6 |
| P5 | 标准库优先 | 能不用依赖就不用依赖 | 仅两个必需依赖；新增需说明理由 |
| P6 | 可复现 | 固定种子，同一 tick 重放一致 | `ctx.rng` + 红线组 3 + golden test |
| P7 | 文档与代码同生共死 | 行为变更必须同步文档 | 文档纪律三条规则 + PR 检查清单 |

### 五条不可谈判的规则

| # | 规则 | 违反的后果 | 谁拦你 |
| --- | --- | --- | --- |
| 1 | **不用 `datetime.now()`**（用 `ctx.clock` / `ctx.now()`） | 测试不可复现 | 红线组 3 |
| 2 | **不用全局 `random`**（用 `ctx.rng`） | 行为不可重现 | 红线组 3 |
| 3 | **LLM 调用必经 `ctx.llm()`** | 成本漏报、预算失效 | 红线组 7 |
| 4 | **不自行构造 logging handler** | 密钥脱敏失效 | 红线组 5 |
| 5 | **新增配置项必须写 `description` + `effect`** | 用户看到无说明的开关 | `test_settings_metadata.py` |

### 一条元规则

> **如果一条规则只能靠人记住，那它就不是规则。**

因此每当新增一条规则，同时要回答：**它会怎么被强制？** 三个选择：

1. `scripts/check_architecture.sh` 里加一条 grep/AST 检查
2. `tests/` 里加一个可执行的断言
3. 写进 `.github/PULL_REQUEST_TEMPLATE.md` 检查清单

**选择 3 是最弱的**，只适用于无法自动化的主观判断。

> 面向 AI 编码助手的精简版规则见仓库根目录的 [`AGENTS.md`](AGENTS.md)。

---

## 开发环境

### 环境要求

- Python 3.11+
- Git
- （可选）bash —— Windows 上用 Git Bash 或 WSL 跑架构检查脚本

### 安装

```bash
git clone https://github.com/LMG-arch/alter-ego.git
cd alter-ego

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装全部开发依赖
pip install -e ".[dev,web,zh]"
```

### 验证环境

```bash
pytest -q                        # 测试
ruff check .                     # 代码检查
ruff format --check .            # 格式检查
bash scripts/check_architecture.sh   # 架构红线
```

四条命令全绿即可开始开发。

---

## 项目结构

```
alter-ego/
├── src/alterego/
│   ├── kernel/          # 内核：配置、总线、注册表、插件、时钟、调度、错误
│   ├── domain/          # 领域：纯函数业务逻辑（人设/情绪/记忆/关系/日程...）
│   │   └── ...          #   schedule / emotion / memory / calendar / conversation
│   │                    #   / birthday 已实现；_toml.py 是两个加载器共用的取值助手
│   ├── holidays/        # 随包节日数据（2026.toml）+ 唯一的读盘入口
│   │                    #   数据在这里、规则在 domain/calendar.py，因为领域层不做 IO
│   ├── birthdays/       # 生日记录的唯一 IO（data/birthdays.toml）
│   │                    #   与 holidays/ 同一形状，但那边是随包公共知识、
│   │                    #   这边是用户自己告诉它的私人数据（所以数据住 data/、不进库）
│   ├── sim/             # 推演：六阶段流水线、意图、预算、TickContext
│   ├── storage/         # 存储：SQLite 实现（connection/migrator/backend）、Repository、迁移
│   │   └── sqlite/      #   └─ migrations/ 里的 .sql 是 schema 的唯一出处
│   ├── llm/             # LLM：Provider、路由、重试、计量
│   ├── channels/        # 渠道：web / file / console / dingtalk / wecom
│   ├── capabilities/    # 能力：activity / post / chat / reach_out
│   ├── npc/             # NPC 社会网络
│   ├── image/           # 生图供应商宿主（image.* 插件）
│   ├── sources/         # 外部信息来源宿主（source.* 插件）
│   ├── cli/             # 命令行
│   ├── prompts/         # 提示词模板
│   └── defaults.toml    # 随包默认配置（只放「内核不能写」的键）
├── plugins/             # 本地插件（不入库）
├── tests/               # 测试
├── docs/                # 设计文档（事实来源）
│   ├── DESIGN.md
│   ├── design/          # 分册
│   └── adr/             # 架构决策记录
├── scripts/             # 开发脚本
└── templates/           # 配置与人格模板
```

**详细分层规则见 [`docs/design/01-architecture.md`](docs/design/01-architecture.md)。**

---

## 开发流程

本项目采用 **trunk-based** 开发：小改动直接提交到 `main`，稍大的功能用短生命周期分支。

### 小改动（typo、文档、单文件 bug）

```bash
git checkout main
git pull
# 修改
pytest -q && ruff check .
git commit -m "fix(memory): 修正 strength_at 在跨时区下的计算偏差"
git push
```

### 功能开发

```bash
git checkout -b feat/npc-comment-replies
# 开发 + 测试
git commit -m "feat(npc): NPC 能回复主角动态下的评论"
git push -u origin feat/npc-comment-replies
gh pr create --fill
```

分支存活时间应 **< 1 周**。超过说明任务拆分不当。

### 每个任务的固定动作

```
写代码 → 写测试 → 跑 CI → 同步文档 → 更新 CHANGELOG → 提交
```

**这六步缺一不可。** 改动涉及行为时，同步文档不是可选项。

---

## 代码规范

### 工具

- **Ruff** —— 代码检查与格式化（配置见 `pyproject.toml`）
- **Mypy** —— 类型检查（`strict = true`）

```bash
ruff check --fix .      # 自动修复
ruff format .           # 格式化
mypy src/alterego       # 类型检查
```

### 硬性要求

| 要求 | 说明 |
| --- | --- |
| 行长 ≤ 100 | formatter 自动处理 |
| 全类型注解 | `disallow_untyped_defs = true` |
| 时区安全 | 禁止 `datetime.now()`，必须带时区。**在 `sim/` 与 `domain/` 中禁止 `datetime.now()` / `time.time()`，用 `ctx.clock` / `ctx.now()`** |
| 不用 `print` | 生产代码用 `logging`；插件用 `ctx.logger`；仅 CLI 输出与 console 渠道例外 |
| 不用全局随机 | **在 `sim/` 中禁止 `random`**，必须用 `ctx.rng`（可复现性） |
| 不使用 `open()` 于 `domain/` | 领域层无 IO |

### 命名

- 模块 / 函数 / 变量：`snake_case`
- 类 / 类型：`PascalCase`
- 常量：`UPPER_SNAKE_CASE`
- 私有：前缀 `_`
- 协议 / 接口：`XxxProtocol` 或直接抽象名（如 `Clock`、`Channel`）

### 文档字符串

公开 API 必须有 docstring，中文，说明**做什么**与**为什么**（而不是重复代码）。

```python
def decay_toward(value: float, target: float, elapsed_hours: float, half_life_hours: float) -> float:
    """指数回归到目标值。

    用于情绪的自然衰减：不施加任何事件时，情绪会缓慢回到基线。
    半衰期决定回归速度——半衰期越短，恢复越快。

    Args:
        value: 当前值
        target: 目标值（通常是基线）
        elapsed_hours: 流逝的虚拟小时数
        half_life_hours: 半衰期（小时），必须为正

    Returns:
        回归后的值，位于 value 与 target 之间
    """
```

### 依赖注入

不要 import 单例。通过参数或 `ctx` 获取依赖。

```python
# ✗ 错误
from alterego.storage.sqlite import SqliteStorage

class MyStage(Stage):
    async def run(self, ctx):
        db = SqliteStorage()          # 硬编码实现

# ✓ 正确
class MyStage(Stage):
    async def run(self, ctx):
        mems = await ctx.storage.memories.retrieve(...)   # 通过抽象
```

---

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
<类型>(<范围>): <描述>

[可选正文]

[可选脚注]
```

### 类型

| 类型 | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | bug 修复 |
| `docs` | 仅文档 |
| `refactor` | 重构（不改变行为） |
| `perf` | 性能优化 |
| `test` | 新增或修改测试 |
| `build` | 构建系统或依赖 |
| `ci` | CI 配置 |
| `chore` | 杂项 |
| `arch` | 架构变更（**必须附 ADR**）|

### 范围

用模块名：`kernel`、`domain`、`sim`、`storage`、`llm`、`channels`、`capabilities`、`npc`、`cli`、`web`、`plugin`、`deps`、`docs`。

### 示例

```
feat(sim): 实现打扰预算的 5 项校验

包含免打扰时段、熔断、日程可打扰性、每日配额、最小间隔。
被拦截的 reach_out 意图降级为 reflect_internal 并写入 inner_voice。

Closes #42
```

```
fix(storage): 修正 FTS5 查询在中文未分词时的召回为 0 的问题

写入时用 preprocess_for_fts 分词，查询时漏了同一步处理。
增加回归测试 tests/storage/test_fts_zh.py。
```

```
arch(channels): 用 Channel Protocol 替换硬编码的渠道分支

决策见 docs/adr/0007-channel-protocol.md
```

### 破坏性变更

在类型后加 `!`，正文中说明迁移方式：

```
feat(plugin)!: 插件清单必填 api_version

⚠️ BREAKING: 旧插件需在 plugin.toml 中补充 api_version = 1。

迁移方式见 docs/design/02-plugin-api.md § 2.3
```

---

## 测试要求

### 覆盖率门槛（CI 强制）

| 模块 | 最低覆盖率 |
| --- | --- |
| `kernel/` | 90% |
| `domain/` | 95%（纯函数，必须高覆盖）|
| `sim/` | 85% |
| 全局 | 85% |

### 强制测试：设置元数据（CI 阻断）

**每个配置项都必须能向用户说清楚「是什么」与「改了会怎样」。** 三条断言在 `tests/test_settings_metadata.py`：

```python
def test_every_config_field_has_metadata() -> None:
    """每个配置字段必须有展示元数据。缺一个就挂。"""
    missing = []
    for cls in CONFIG_DATACLASSES:
        for f in dataclasses.fields(cls):
            if not get_setting_metadata(cls, f.name):
                missing.append(f"{cls.__name__}.{f.name}")
    assert not missing, f"以下配置项缺少展示元数据（用户会在设置页里看到一个没有说明的开关）：{missing}"


def test_every_effect_is_a_sentence() -> None:
    """`effect` 必须是一句用户能据此做决定的话，不能是「可能」开头的模糊表述。"""
    for s in all_settings():
        assert len(s.effect) >= 8, f"{s.key} 的 effect 太短，等于没说"
        assert not any(w in s.effect for w in ("可能", "大概", "也许")), \
            f"{s.key} 的 effect 是模糊表述，用户无法据此做决定"


def test_every_enum_choice_explains_its_consequence() -> None:
    """枚举的每个选项都要写清楚选它会发生什么。"""
    for s in all_settings():
        if s.kind is SettingKind.ENUM:
            assert s.choices, f"{s.key} 是枚举但没有选项说明"
            for c in s.choices:
                assert c.consequence, f"{s.key} 的选项 {c.value} 没有说明后果"
```

**为什么用测试而不是约定**：

> 没有测试的约定不是约定，是愿望。

新增配置项的成本因此上升了——**这是故意的**。
写不出「改了会怎样」说明这个配置项本身就没想清楚，那就不应该存在。
详见 [ADR-0010](docs/adr/0010-every-setting-carries-display-metadata.md)。

### 测试组织

```
tests/
├── kernel/          # 单元测试
├── domain/          # 纯函数测试，无 IO
├── sim/             # 推演测试，用 FrozenClock + FakeLLM
├── storage/         # 用 tmp_path 建临时数据库
├── channels/        # 用 FakeChannel / mock httpx
├── plugin/          # 插件加载与隔离
├── golden/          # 可复现性黄金测试
└── architecture/    # 架构红线（Python 版）
```

### 标记

```python
@pytest.mark.slow          # > 1s，默认跳过
@pytest.mark.integration   # 需要外部服务
@pytest.mark.golden        # 固定种子，输出必须逐字节一致
@pytest.mark.architecture  # 依赖方向检查
```

### 原则

1. **bug 修复必须先写复现测试**，确认测试红了，再修代码让它变绿。
2. **纯函数必须有边界测试**（0、负值、极值、NaN）。
3. **推演测试必须用固定的 `ctx.rng` 种子**，否则不可复现。
4. **不要 mock 被测对象自身**。要 mock 就 mock 它的依赖。
5. **黄金测试**用于保护可复现性：同一 tick 重放，`activity_log` 内容必须完全一致。

### 测试工具

内核提供 `alterego.testing`：

```python
from alterego.testing import (
    FakeRegistry, FakeBus, FrozenClock, FakeLLM, FakeChannel, make_context,
)

async def test_reach_out_respects_budget():
    ctx = make_context(
        clock=FrozenClock("2026-09-15T14:30:00+08:00"),
        llm=FakeLLM(responses=[...]),
        budget={"messages_today": 3},   # 已达上限
    )
    await IntentionStage().run(ctx)
    assert ctx.chosen_intent.name == "reflect_internal"
    assert ctx.suppressed[0].reason == "daily_message_limit_reached"
    assert ctx.suppressed[0].inner_voice        # 必须记录了内心话
```

---

## 文档纪律

**这是本项目最重要的约定。**

### 三条规则

1. **代码行为变更必须同步更新对应设计文档。**
   - 改了打扰预算算法 → 更新 `docs/design/04-simulation-loop.md`
   - 加了数据库字段 → 更新 `docs/design/03-data-model.md`
   - 改了插件清单格式 → 更新 `docs/design/02-plugin-api.md`
   - 加了配置项 → 更新 `docs/design/10-settings-center.md` 与 `templates/alterego.toml`
   - 加了可观测字段 → 更新 `docs/design/09-observability.md`
   - 加了生图/检索能力 → 更新 `docs/design/07-` / `08-`

2. **实现与文档冲突时，以文档为准。**
   - 如果你认为文档写错了，先改文档（或写 ADR），再改代码。
   - 不允许"代码先跑起来，文档后面补"。

3. **偏离设计必须写 ADR。**
   - 放在 `docs/adr/NNNN-<短横线描述>.md`
   - 格式见 [`docs/adr/0001-record-architecture-decisions.md`](docs/adr/0001-record-architecture-decisions.md)
   - ADR 一旦 Accepted 就不可修改（只能被后续 ADR 取代）

### 什么时候写 ADR

| 写 | 不写 |
| --- | --- |
| 改变分层依赖方向 | 改一个函数内部实现 |
| 更换数据库 / LLM / Web 框架 | 加一个插件 |
| 破坏插件 API 兼容性 | 修复 bug |
| 新增必需依赖 | 重命名变量 |
| 改变核心设计决策（如降级语义）| 调整提示词措辞 |

### 文档更新检查

```bash
git diff --name-only main...HEAD | grep '^src/'   # 有源码变更？
git diff --name-only main...HEAD | grep '^docs/'  # 有文档变更？
```

如果第一个有输出而第二个没有，PR 会被要求补充文档。

---

## 架构红线

CI 会跑 [`scripts/check_architecture.sh`](scripts/check_architecture.sh)，**七组共 23 项**检查：

| 组 | 内容 |
| --- | --- |
| 1 | 内核无知 —— `kernel/` 不引用 sqlite / openai / wecom / fastapi / httpx / tavily / comfyui |
| 2 | 领域层纯净 —— `domain/` 无 IO、无数据库、无网络、无环境变量 |
| 3 | 推演层抽象 —— `sim/` 不 import 具体实现，不用全局 `random`，不用 `datetime.now()` |
| 4 | 分层不越级 —— `storage/` 与 `llm/` 不含业务逻辑 |
| 5 | 代码卫生 —— 无 `print`、目录有 `__init__.py`、单文件 ≤ 900 行、**不得自行构造 logging handler** |
| 6 | 插件自包含 —— 插件之间不互相 import |
| **7** | **LLM 调用必经 `ctx.llm()`** —— 不得在 `sim/`、`capabilities/` 里直接 `httpx.Client(base_url=...)`；否则无法计量、无法路由、无法受限 |

**这些不是"建议"，是硬性约束。** 想突破就写 ADR。

### 红线为什么长这样

每一条红线都对应一个「**如果违反了，问题会在很久以后才暴露**」的场景：

| 红线 | 违反以后会发生什么 | 多久后发现 |
| --- | --- | --- |
| 内核无知 | 换掉 SQLite 时发现内核里埋了 `sqlite3` | 数月 |
| 领域层纯净 | 纯函数测试要开数据库，覆盖率掉下去 | 数周 |
| 推演层抽象 | golden test 开始随机失败（用了全局 `random`） | 第二天 |
| 不经 `ctx.llm()` | **成本统计漏报**，预算闸门失效 | 收到账单的那天 |
| 自建 logging handler | **密钥脱敏失效**（`SecretFilter` 挂在 root logger 上） | 泄露的那天 |

**最后两条是 v0.2.0 新增的**，因为 Token 统计页与日志页把这两件事从「建议」变成了「必须」。

本地运行：

```bash
bash scripts/check_architecture.sh --verbose
```

### Python 版红线测试

同时提供 pytest 版本（`tests/architecture/test_dependencies.py`），用 AST 解析 import 语句，比 grep 更精确：

```python
@pytest.mark.architecture
def test_kernel_has_no_io_imports():
    forbidden = {"sqlite3", "httpx", "requests", "fastapi"}
    for mod in imports_in("src/alterego/kernel"):
        assert mod.split(".")[0] not in forbidden, \
            f"kernel 引用了 {mod}。依据 docs/design/01-architecture.md § 1"
```

---

## PR 检查清单

创建 PR 前请逐项确认（模板见 [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)）：

- [ ] 变更描述清晰，说明了**为什么**改
- [ ] 本地 `pytest` 全绿
- [ ] 本地 `ruff check . && ruff format --check .` 通过
- [ ] 本地 `bash scripts/check_architecture.sh` 通过
- [ ] 新功能有测试；bug 修复有复现测试
- [ ] 覆盖率未下降
- [ ] **相关设计文档已同步更新**
- [ ] **新增配置项已补 `description` 与 `effect`**（`tests/test_settings_metadata.py` 会拦）
- [ ] **新增可观测记录已带 `correlation_id`**
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 已更新
- [ ] 提交信息符合 Conventional Commits
- [ ] 未新增依赖，或已说明理由
- [ ] 未触碰架构红线，或已附 ADR
- [ ] **未把 LLM 调用写到 `ctx.llm()` 之外**

---

## 新增依赖

本项目的原则是 **P5 · 标准库优先**。当前必需依赖只有两个：

| 依赖 | 为什么无法避免 |
| --- | --- |
| `pydantic` | 配置与消息的结构化校验，手写会引入大量易错代码 |
| `httpx` | 异步 HTTP，标准库的 `urllib` 不支持 async |

**新增依赖前请自问**：

1. 标准库真的做不到吗？（`tomllib` / `sqlite3` / `argparse` / `asyncio` / `logging` 都很强）
2. 这个功能能不能做成**可选插件**而不是核心依赖？
3. 这个包是否维护活跃（近 1 年有提交）？
4. 它的依赖树有多大？
5. 是否只在开发时需要（→ 放 `[dev]`）？

可选功能应放 `[project.optional-dependencies]`，并在文档中标注安装方式：

```bash
pip install "alterego[web]"    # Web 界面
pip install "alterego[zh]"     # 中文分词
pip install "alterego[wecom]"  # 企业微信双向消息
```

如果新增必需的运行时依赖，**必须写 ADR** 说明理由。

---

## 遇到问题

- **设计不清楚** → 先读 [`docs/DESIGN.md`](docs/DESIGN.md) 与相关分册
- **不确定该不该写 ADR** → 写了不亏
- **不确定改动是否破坏架构** → 跑 `scripts/check_architecture.sh`，或者想想"内核会不会因此知道一个具体技术"
- **还是不清楚** → 开一个 issue 讨论，不要猜

---

再次感谢你的贡献。这个项目的目标很小也很具体：**让一个数字存在看起来真的在生活**。

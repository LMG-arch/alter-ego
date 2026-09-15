# ADR-0006 · 发行版选型写成数据文件，不写进内核代码

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **相关**：[`docs/DESIGN.md`](../DESIGN.md) § 4 设计原则 P1、[`docs/design/01-architecture.md`](../design/01-architecture.md) § 2.2 `config.py`、`scripts/check_architecture.sh` 第 1 组
- **影响范围**：`src/alterego/defaults.toml`、`kernel/config.py` 的 `LLMConfig.default_provider` / `LLMRoutingConfig.strong`/`cheap` / `StorageConfig.backend`、`Config.load()` 的装配顺序、`templates/alterego.toml` 的头部注释

---

## 背景

`docs/DESIGN.md` § 4 的第一条设计原则是 **P1「内核无知」**：

> 内核知道**机制**，不知道**实现**。

并且给出了一个机械可验证的红线（`docs/design/01-architecture.md` § 1）：

```bash
grep -r "sqlite\|openai\|wecom" src/alterego/kernel/     # 必须无输出
```

第一版 `kernel/config.py` 是这样写的：

```python
@dataclass(frozen=True)
class LLMRoutingConfig:
    strong: str = "openai_compatible"
    cheap: str = "openai_compatible"

@dataclass(frozen=True)
class LLMConfig:
    default_provider: str = "openai_compatible"

@dataclass(frozen=True)
class StorageConfig:
    backend: str = "sqlite"
```

设计时没人觉得有问题——这些只是**默认值**，用户当然可以改。等到第一次真正跑

```bash
bash scripts/check_architecture.sh --verbose
```

结果是 **3 / 20 项违规**，全部来自这两个文件。

更微妙的是 `kernel/__init__.py` 里那段说明红线的 docstring 自己也被 grep 命中了：

```
kernel/ 下不得出现 sqlite / openai / httpx / wecom / fastapi 等任何具体技术名词
```

——**一条用 grep 实现的规则，会匹配到禁止它自己那段文字。**

---

## 决策

**把「默认挑选哪个实现」从内核代码移到随包分发的数据文件
`src/alterego/defaults.toml`。**

```toml
[llm]
default_provider = "openai_compatible"

[llm.routing]
strong = "openai_compatible"
cheap = "openai_compatible"
decision = "strong"
expression = "strong"
reflection = "cheap"
npc = "cheap"
persona = "strong"
memory = "cheap"

[storage]
backend = "sqlite"
```

内核里对应的字段默认值改为空字符串，由 `Config.load()` 把该文件作为**最低优先级**层合并进来：

```
dataclass 默认值  →  alterego/defaults.toml  →  config/alterego.toml
                  →  ALTEREGO_* 环境变量  →  CLI 参数
```

`defaults.toml` 不在 `kernel/` 目录下，因此不受第 1 组红线约束——它本来就
**应该**包含具体技术名。

---

## 备选方案

| 方案 | 优点 | 为什么没选 |
| --- | --- | --- |
| **保持现状**：dataclass 默认值里写死 | 零成本，最直白 | 内核里出现具体技术名，P1 变成一句空话；实测已被 `check_architecture.sh` 挡下 |
| **把默认值改成 `None`，强制用户必填** | 内核彻底干净 | 违背 S1「`alterego init && alterego serve` 五分钟内跑起来」；不给默认值就不是「开箱可用」 |
| **放宽 grep 规则，加白名单** | 改动最小 | 一旦允许例外，例外就会越来越多；红线必须无例外才有价值 |
| **拆成 `alterego-core` + `alterego-default` 两个包** | 分层最彻底 | 为一个默认值拆包，代价远超收益；`defaults.toml` 已经达到了同样的隔离效果 |
| **运行时从 entry_points 里挑第一个 LLM provider** | 没有硬编码 | 多个 provider 并存时结果不确定，且「没装任何 provider」会变成神秘的启动失败（P2 显式优于隐式） |
| ✅ **随包数据文件 `defaults.toml`** | 内核零技术名；换发行版只需换一个文件；用户仍可覆盖 | —— |

---

## 代价

1. **多一次文件读取**。`Config.load()` 每次启动多读一个约 20 行的 TOML，
   实测可忽略（`tomllib` 解析微秒级）。已按类缓存 `get_type_hints`，不重复解析。
2. **多一个必须打进 wheel 的文件**。已在 `tests/test_kernel_config.py` 里
   加断言 `DEFAULT_CONFIG_PATH.is_file()` 看住——打包漏了会当场测试失败。
3. **默认值不再一眼可见**。读 `config.py` 只能看到 `str = ""`。
   代价通过 docstring 里指向 `alterego/defaults.toml` 来补偿；
   而且这恰好是**想要**的效果：读内核时本就不该看到具体厂商。
4. **文件缺失时必须优雅降级**。非 editable 安装若漏了数据文件，
   `_packaged_defaults()` 返回空 dict，由 dataclass 默认值兜底，
   绝不因此启动失败，更不用代码里的技术名去补。

---

## 后续约束

- **新增任何「默认挑哪个实现」的配置项，一律加进 `defaults.toml`，
  不加进 dataclass 默认值。**
- `scripts/check_architecture.sh` 第 1 组**不加白名单**。若某天它又红了，
  说明有新的具体技术名漏进内核，应该改代码而不是改规则。
- 写「禁止出现 X」这类注释时，**不要直接写出 X 的字面量**，
  用描述代替（"具体技术名词——存储引擎、模型厂商、HTTP 客户端库、IM 平台的名字"）。

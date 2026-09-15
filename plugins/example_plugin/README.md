# 示例插件

这个目录是**照抄的起点**，也是 CI 的一部分。

它的作用不是「演示功能」——它几乎什么都不做。它演示的是**正确的写法**：

1. `plugin.toml` 里该声明什么（`id` 必须形如 `<kind>.<name>`，每个配置字段都要有
   `description`，因为它会被 `alterego plugins config` 原样打给用户看）；
2. `on_load` 里注册能力、订阅事件时**不传 `owner`**——归属由
   `ctx.registry` / `ctx.bus` 自动记账（原因见
   [ADR-0007](../../docs/adr/0007-auto-owning-plugin-context-views.md)）；
3. `on_stop` 必须幂等（内核可能因为失败回滚或热重载而重复调用）；
4. `execute()` 返回的 `summary` 是**给人看的**，不是给日志看的；
5. 配置从 `ctx.config` 读，不从环境变量也不从文件读。

## 跑起来

```bash
alterego plugins list                    # 应该能看到 capability.example
alterego plugins enable capability.example
alterego plugins config capability.example
```

把 `punctuation` 改成 `~`、`repeat` 改成 `2`，再看 `alterego plugins list`
里的问候语变化——这就是「改配置不用改代码」。

## 它是怎么被验证的

`tests/test_example_plugin.py` 会**真的加载它**：走一遍清单校验、依赖解析、
导入、注册、执行、卸载。所以这个目录里的代码一旦腐烂（字段改名、
钩子签名变化），CI 立刻变红，而不是等到有人照着抄才发现。

这就是为什么示例插件放在版本库里、而不是贴在文档里的一段代码块：
贴出来的代码没人测，放进来的代码每次提交都在被测。

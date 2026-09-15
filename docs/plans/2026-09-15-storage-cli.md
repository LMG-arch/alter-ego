# 实施计划 · 存储维护命令行（`alterego db`）

> 状态：**已完成**（2026-09-15）
> 依据：[`03-data-model.md`](../design/03-data-model.md) § 8.5 + § 9、[`05-channels.md`](../design/05-channels.md) § 8、
> [`01-architecture.md`](../design/01-architecture.md) § 1.2（架构红线）+ § 6.3（退出码）

## 1. 为什么先做这一片

批次 1（commit `2bf3223`）把存储地基做完了：`SqliteStorageBackend`、迁移器、
四个迁移脚本、`VACUUM INTO` 备份、事务深度计数、完整性检查——
每一项都有测试，覆盖率 95% 以上。

**但用户手上没有一个能用的命令。**

一个 100% 覆盖、100% 正确的迁移器，如果只能从 Python 里
`SqliteStorageBackend.from_config(...).migrate()` 调用，那么第一次跑起来的时候，
谁也不知道库在哪、版本是多少、还差几个迁移。这一批就是把 § 8.5 的调用接到命令行上。

排序不是随意定的，是**用户遇到问题的顺序**：

| 顺序 | 命令 | 回答的问题 |
| --- | --- | --- |
| 1 | `db status` | 库在哪？版本多少？还差什么？ |
| 2 | `db migrate` | 把它升上去 |
| 3 | `db backup` | 升之前留一份 |
| 4 | `db restore <file>` | 升坏了，退回去 |

（`03-data-model.md` § 8.5 的清单顺序是 `migrate` 在前，因为它按「设计意图」写；
CLI 的子命令列表按「先看、再做、留住退路」排，因为它按「人的动作顺序」写。
两处不一致没有害处——但**值得说明为什么**，否则下一个人会去把它们改成一致。）

## 2. 范围内

| 交付物 | 依据 |
| --- | --- |
| `src/alterego/cli_db.py` —— `db {status,migrate,backup,restore}` | `03-data-model.md` § 8.5 五条调用 |
| `src/alterego/cli_io.py` —— `_out` / `_err` / `_width` / `_pad` / `_human_size` | `AGENTS.md` § 3 禁止 `print(`（第 5 组红线）；`cli.py` 与 `cli_db.py` 都需要 |
| `src/alterego/cli.py` 收紧 —— 参数树、`set_defaults(handler=…)`、退出码 `4` | `01-architecture.md` § 6.3 退出码表 |
| `scripts/check_architecture.sh` 第 3 组红线收紧 | `01-architecture.md` § 1.1 组装根 |
| `tests/test_cli_db.py`（32）/ `test_cli.py`（+4）/ `test_storage_backend.py`（+2）/ `test_storage_sqlite.py`（改写 1） | `AGENTS.md` § 5 覆盖率门槛 |
| 一次手工实跑（`db` 全部命令 + 版本不匹配分支） | 见 § 4 |

### 2.1 为什么拆成三个文件

`cli.py` 一度是 **937 行**，而 `AGENTS.md` § 5 的上限是 900。触发点很具体：
加上 `db` 命令组的参数定义就超了。

值得记的是**拆的边界怎么选的**。第一直觉是按「职责」拆——把「数据库相关」切出去。
这个理由不够，因为下一个人加 `memory` 命令组时会再问一遍同样的问题。
实际用的判据是：**哪一部分会因为「再加一个功能」而再次增长？**

| 部分 | 会随功能增长吗 | 结论 |
| --- | --- | --- |
| 参数树（`build_parser`） | 会，每个新命令都加几行 | 留下 |
| 输出助手（`_out` / 宽度计算 / 大小格式化） | 几乎不会 | 独立成 `cli_io.py` |
| 某个命令组的实现 | 会，但只在**本组**内 | 每组一个文件 |

`_out` 必须独立，还有一个硬理由：`cli.py` 与 `cli_db.py` 都要用它。
放在任一边，另一边就得 `from alterego.cli import` 或 `from alterego.cli_db import`——
一条没有意义的依赖边。

## 3. 三处「文档与实现不一致」（本批次修正）

### 3.1 `05-channels.md` § 8.1 有两个不存在的命令

命令树里写着：

```
│   ├── vacuum
│   └── rollback --to <n>
```

两者都不实现，而且**理由不同**：

- `rollback --to N` **是设计上有意不做的**——`03-data-model.md` § 8.5 已经写明了理由
  （反向迁移的正确性几乎从不会被测试，而真正的回滚发生在生产事故里）。
  同一份文档的一个分册说「没有这个命令」，另一个分册的命令树里却列着它。
- `vacuum` 只是**还没做**，而且很可能永远不做：`optimize()` 已经能手工调用，
  `03-data-model.md` § 10 把它列为「优化建议」而不是命令。

**修正**：命令树改成实际存在的四条，`restore`（树里原本缺失）补上，
`vacuum` 与 `rollback` 改成一行指向 § 10 与 § 8.5 的说明。

> 一个命令树里同时存在「有意不做」和「还没做」两种缺失，
> 而它们**长得一模一样**。这本身就是问题：读者无法区分「别等了」与「等着」。

### 3.2 `03-data-model.md` § 9.1 的备份文件名是错的

文档写 `alterego_20260915_143211.db`（下划线），
代码输出 `alterego-20260915-223422.db`（连字符）。

这类不一致的伤害不在「照抄会失败」——两边都是合法文件名——
而在于它让人**怀疑其他数字也是编的**。§ 9.1 里有 backup 的实际样例输出，
那是文档在说「你可以照这个预期」。既然样例本身是第一手的，就应该是真的。

### 3.3 `05-channels.md` § 8.3 的实现是设计期伪代码

原文是一段 50 行的 `build_parser()` 草图，里面有两处与最终实现相反的做法：

| 伪代码 | 实际 | 为什么 |
| --- | --- | --- |
| `print(...)` | `cli_io._out`（`sys.stdout.write`） | 第 5 组红线对**整个** `src/alterego` 禁止 `print(`，CLI 没有豁免 |
| `return exc.exit_code` | `main()` 按异常类型分派 | 异常类只描述「发生了什么」；「该以几退出」是调用方的判断 |

伪代码里的 `exit_code` 属性**从来不存在**，于是「迁移失败返回 4」这句话
在文档里存在了两个批次，而代码里所有错误都返回 `2`。
这是这一批最值得记的教训：**一段画在文档里的签名，会被当成已经实现的契约。**

**修正**：§ 8.3 换成真实实现的四条约定 + 退出码表。

## 4. 手工实跑抓到的 bug（本批次的主要收获）

**备份会互相抵消。**

`SqliteConnection.backup_to()` 里有一句 `if target.exists(): target.unlink()`。
引入它的理由听起来完全合理：`VACUUM INTO` 拒绝写一个已存在的文件，
而撞名的情况是「一个陈旧文件占着位置」，删掉是自然的处理。

但它同时也在给**真备份**让路：自动生成的文件名只精确到秒

```
data/backups/alterego-20260915-223422.db
```

一秒内备份两次，第二次就把第一次删掉了，**一声不吭**。
而 `db restore` 每次留的那份 `before-restore-*.db` 走的是同一条路径——
也就是说，「唯一的回滚手段」在快速连续操作下会吃掉自己的退路。

修法是 `_free_path()`：撞名时让路（`x.db` → `x-2.db` → …），
并**返回实际写出的路径**，调用方不得假定它等于入参。

**为什么单元测试没抓到**：每个用例都指向自己的 `tmp_path`，
「目标已经存在」这条分支在备份语境下没有任何用例走过
（只有一个用例走了它，而那个用例断言的正是这个错误行为）。

**为什么手工实跑抓到了**：连敲两次命令，间隔不到一秒。
这是第七次「自己把自己的 CLI 跑一遍，找到了单测找不到的东西」。

## 5. 文档同步

| 文件 | 改什么 | 状态 |
| --- | --- | --- |
| `03-data-model.md` | § 8.5 五条命令标注为已实现 + 四条使用约定（不建库 / 预演不备份 / restore 顺序 / 退出码）；§ 9.1 文件名改连字符 + 补「不覆盖已有备份」 | ✅ |
| `05-channels.md` | § 8.1 命令树对齐实现；§ 8.2 补 `db status` 实测输出；§ 8.3 换成真实实现 | ✅ |
| `06-roadmap.md` | M2 迁移行的验收标准 | ✅ |
| `CHANGELOG.md` | 新增 / 修复 / 文档 | ✅ |
| `docs/plans/2026-09-15-storage-framework.md`、`...-domain-emotion-memory.md` | 把「`db` CLI 子命令」从待办改为已完成 | ✅ |

## 6. 明确不在本批次范围内

1. **`storage.sqlite` 插件壳**（`plugin.toml` + `alterego.plugins` entry point + `api_version = 1`）
   → 与 Repositories 一起做（`storage/sqlite/backend.py` 的 docstring 已经这么写了）
2. **12 个 Repository 实现**（`storage/sqlite/repo/`）——`interfaces/` 下的 Repository Protocol 也还没有
3. **`db vacuum` / `db archive` / `export` / `import`** —— § 10 的「优化建议」，不是命令
4. **`db status` 显示磁盘占用与碎片率** —— `optimize()` 已经能算，但输出格式没定

### 6.1 两处已知的命名不一致（未修）

- `templates/defaults.toml` 写 `[storage] backend = "sqlite"`，
  而 `02-plugin-api.md` § 2 把内置存储插件的 id 定为 **`storage.sqlite`**。
  `cli_db._require_sqlite()` 目前接受 `"sqlite"` 与 `""`，两者都当 sqlite 处理。
  等插件壳落地时统一——**在那之前，改配置默认值会让现有用户的配置文件失效**。
- `storage/sqlite/__init__.py`、`backend.py`、`cli.py` 的模块 docstring 仍称
  `cli.py` 是「组装根」。`db` 那部分的组装根现在是 `cli_db.py`。
  措辞问题，不影响行为，但 P7 要求这类措辞跟得上。

### 6.2 一件确认过「不会冲突」的事

`db migrate` 不开事务（迁移器自己在每步之间提交），
`backend.backup()` 用的 `VACUUM INTO` 要求 `_depth == 0`。
两者互不嵌套，因此「迁移前的自动备份」不会自己把自己卡住。

同理，`01-architecture.md` § 6.1 把 tick 的事务交给 `PersistStage` 持有，
**存储插件自身从不主动开启事务**——所以 `db` 命令组与未来的插件壳
在事务这件事上没有交叠。

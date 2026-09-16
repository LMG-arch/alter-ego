"""元数据 · 它花多少钱、想得多深。

模型路由（哪一类调用走哪一档）、成本天花板、训练数据集的导出。

这一组里的 ``default_provider`` / ``strong`` / ``cheap`` 三个值**默认是空的**，
不是漏写：填哪个 provider 是**发行版**的选择，不是内核的（P1）。运行时由随包
分发的 ``alterego/defaults.toml`` 填入，所以设置页显示它们时要说明这一点，
而不是显示一个空白输入框——用户会以为配置坏了。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alterego.kernel.settings import Choice, Setting, SettingKind


__all__ = ["SETTINGS"]


#: 七个「用哪档模型」的字段共用这两个选项：一档就是一份 provider 绑定。
#: 抽成常量不是因为懒得打字，而是因为七处各写一遍必然会在某一次改动里只改掉六处。
_TIER_CHOICES: Final[tuple[Choice, ...]] = (
    Choice("strong", "强档（[llm.routing] strong）", "换成它之后这一类调用改用那一档绑定的模型。"),
    Choice(
        "cheap",
        "便宜档（[llm.routing] cheap）",
        "换成它之后这一类调用改用便宜档，省钱但判断会变粗。",
    ),
)


SETTINGS: Final[tuple[Setting, ...]] = (
    # ── 模型 ────────────────────────────────────────────────
    Setting(
        key="llm.default_provider",
        label="默认用哪个 provider",
        description="路由表没覆盖到的调用走哪个 provider；空值表示跟着随包默认配置走。",
        kind=SettingKind.STR,
        default="",
        group="模型",
        effect="换掉它之后所有没在 [llm.routing] 里点名的调用都改走新的 provider，名字必须和 [llm.providers] 里的段名对得上。",
        requires_restart=True,
    ),
    Setting(
        key="llm.timeout_seconds",
        label="单次调用超时",
        description="一次模型调用最多等多久。",
        kind=SettingKind.INT,
        default=60,
        group="模型",
        unit="秒",
        minimum=1,
        effect="调到 5 秒会让稍慢的模型直接被判超时，它开始频繁地重试或降级；调到 600 秒则网络卡住时整个推演会僵在那里。",
    ),
    Setting(
        key="llm.max_retries",
        label="失败后重试几次",
        description="一次调用失败后最多再试几次，含在日调用上限里。",
        kind=SettingKind.INT,
        default=3,
        group="模型",
        unit="次",
        minimum=0,
        effect="调到 0 之后一次网络抖动就够让这次推演失败；调得太高会在接口持续报错时把当日调用额度烧光。",
    ),
    Setting(
        key="llm.routing",
        label="模型路由表",
        description="哪一类调用走哪一档模型；这一节的每一行在下面单独列出。",
        kind=SettingKind.MAPPING,
        default=None,
        group="模型",
        effect="改动其中任何一行只影响那一类调用，其余用途照旧——这正是分层路由能把成本压到全强模型六分之一的原因。",
    ),
    Setting(
        key="llm.routing.strong",
        label="强档指向哪个 provider",
        description="[llm.routing] 里的「strong」这个名字绑定到哪个 provider。",
        kind=SettingKind.STR,
        default="",
        group="模型",
        effect="改掉它会让所有指向 strong 的用途一起换模型，包括意图决策和聊天表达——这两个最影响它像不像人。",
        requires_restart=True,
    ),
    Setting(
        key="llm.routing.cheap",
        label="便宜档指向哪个 provider",
        description="[llm.routing] 里的「cheap」这个名字绑定到哪个 provider。",
        kind=SettingKind.STR,
        default="",
        group="模型",
        effect="改掉它会让反思、记忆整理、知识库这些后台工作一起换模型，省下的钱和变差的质量都出在这些地方。",
        requires_restart=True,
    ),
    Setting(
        key="llm.routing.decision",
        label="意图决策用哪档模型",
        description="在一个 tick 里选出「现在该做什么」用哪一档模型。",
        kind=SettingKind.ENUM,
        default="strong",
        group="模型",
        effect="换成便宜档后每小时的决策成本降到约三分之一，代价是它选出来的事更常是「什么也不做」。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.expression",
        label="说话用哪档模型",
        description="把意图写成真正发给你的那句话用哪一档模型。",
        kind=SettingKind.ENUM,
        default="strong",
        group="模型",
        effect="换成便宜档后句子会明显变短变平，错别字和复读也会多起来——这是最容易看出降级的一项。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.reflection",
        label="反思用哪档模型",
        description="每天收尾时回头整理今天发生了什么用哪一档模型。",
        kind=SettingKind.ENUM,
        default="cheap",
        group="模型",
        effect="换成强档会让它对自己的总结更有想法，但这是一天一次的后台调用，省下的钱从这里拿最少。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.npc",
        label="其他角色用哪档模型",
        description="身边那些角色说话时用哪一档模型。",
        kind=SettingKind.ENUM,
        default="cheap",
        group="模型",
        effect="换成强档后配角的台词会更有细节，但它们数量多，这一项是全表里最贵的改动之一。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.persona",
        label="生成人设用哪档模型",
        description="生成或演化人设时用哪一档模型。",
        kind=SettingKind.ENUM,
        default="strong",
        group="模型",
        effect="换成便宜档后生成的人设描述会退化成模板填空，而这份人设是后面所有对话的地基。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.memory",
        label="整理记忆用哪档模型",
        description="把原始对话压成长期记忆时用哪一档模型。",
        kind=SettingKind.ENUM,
        default="cheap",
        group="模型",
        effect="换成强档后它记住的细节更准，但每次整理都更贵，而整理是一天里跑得最勤的后台任务。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.routing.vault",
        label="整理知识库用哪档模型",
        description="把学到的内容归档进知识库时用哪一档模型。",
        kind=SettingKind.ENUM,
        default="cheap",
        group="模型",
        effect="换成强档后归档的分类和摘要更靠谱，检索时更容易翻到对的那一篇。",
        choices=_TIER_CHOICES,
    ),
    Setting(
        key="llm.providers",
        label="provider 配置",
        description="每个 provider 自己那段配置，键由那个 provider 的插件解释。",
        kind=SettingKind.MAPPING,
        default=None,
        group="模型",
        effect="这里没有内核认识的键，写错只会让对应的 provider 读不到自己的配置并报错，不会影响别的 provider。",
        advanced=True,
    ),
    # ── 预算 ────────────────────────────────────────────────
    Setting(
        key="llm.budget",
        label="成本天花板",
        description="每天/每月最多花多少钱、多少次调用、多少 token；这一节的每一行在下面单独列出。",
        kind=SettingKind.MAPPING,
        default=None,
        group="预算",
        effect="四个上限里任何一个先到都会触发 on_exceed 的处置，所以调低最紧的那一项才是真正在控制花费。",
    ),
    Setting(
        key="llm.budget.daily_usd_limit",
        label="每天花费上限",
        description="一天之内模型调用累计花到多少美元就停手。",
        kind=SettingKind.FLOAT,
        default=2.0,
        group="预算",
        unit="美元",
        minimum=0.01,
        effect="调到 0.1 之后它每天中午前后就进入超限状态，剩下的时间里不再主动说话。",
    ),
    Setting(
        key="llm.budget.monthly_usd_limit",
        label="每月花费上限",
        description="一个月之内模型调用累计花到多少美元就停手。",
        kind=SettingKind.FLOAT,
        default=40.0,
        group="预算",
        unit="美元",
        minimum=0.01,
        effect="调低到低于日上限会让日上限永远轮不到生效，账单由月线一口气截断。",
    ),
    Setting(
        key="llm.budget.max_calls_per_day",
        label="每天调用次数上限",
        description="一天最多调多少次模型，重试也算一次。",
        kind=SettingKind.INT,
        default=800,
        group="预算",
        unit="次",
        minimum=1,
        effect="调到 50 之后一次「学一格」就能吃掉大半额度，日常聊天会先一步耗完。",
    ),
    Setting(
        key="llm.budget.max_tokens_per_day",
        label="每天 token 上限",
        description="一天最多消耗多少 token（输入加输出）。",
        kind=SettingKind.INT,
        default=2_000_000,
        group="预算",
        unit="token",
        minimum=1,
        effect="调小之后长对话会先撞线，因为它每次都要把上下文重新发一遍，token 消耗比调用次数更早见底。",
    ),
    Setting(
        key="llm.budget.on_exceed",
        label="超出预算时怎么办",
        description="四个上限里任何一个到了之后的处置方式。",
        kind=SettingKind.ENUM,
        default="degrade",
        group="预算",
        effect="换掉它改变的不是花多少钱，而是额度用完之后它对你来说还剩多少存在感。",
        choices=(
            Choice(
                "degrade",
                "降级但不停止",
                "超出预算后改用便宜模型，Agent 继续生活，只是变笨。",
            ),
            Choice(
                "stop",
                "完全停止",
                "超出预算后不再发起任何模型调用，它这一天彻底安静，日记和动态也都停在那里。",
            ),
            Choice(
                "warn",
                "只警告",
                "超出预算后只在日志里记一条，调用照常进行，账单会继续涨。",
            ),
        ),
    ),
    # ── 数据集 ──────────────────────────────────────────────
    Setting(
        key="dataset.export_dir",
        label="训练数据导出到哪",
        description="导出根目录，实际落盘在它下面再套一层角色名。",
        kind=SettingKind.PATH,
        default=Path("exports/datasets"),
        group="数据集",
        effect="换到这里之后旧的导出文件不会跟过去，dataset list 会认为你从来没导出过。",
    ),
    Setting(
        key="dataset.formats",
        label="导出哪几种形状",
        description="chat / sharegpt / alpaca，可以同时选多个。",
        kind=SettingKind.LIST,
        default=("chat",),
        group="数据集",
        effect="多选一种就多一份同内容的文件，纯粹占磁盘；选错形状只会在训练脚本里报字段缺失。",
    ),
    Setting(
        key="dataset.redact_terms",
        label="额外要摘掉的字面量",
        description="在内置的八条脱敏规则之外，再加一份你自己的名单；按字面量匹配，不是正则。",
        kind=SettingKind.LIST,
        default=(),
        group="数据集",
        effect="改了它之后要重跑 alterego dataset build，dataset list 会提示哪一批是旧规则脱的，但不会自动重跑。",
    ),
    Setting(
        key="dataset.lookback_days",
        label="往回看多少天",
        description="导出时取最近多少天的消息与推演记录。",
        kind=SettingKind.INT,
        default=30,
        group="数据集",
        unit="天",
        minimum=1,
        effect="调小会让样本变少、导出变快；调到 1 基本只剩当天那几十条，训出来什么也学不到。",
    ),
)

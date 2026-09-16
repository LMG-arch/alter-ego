"""配置系统。

加载优先级（后者覆盖前者，``docs/design/01-architecture.md`` § 2.2）::

    随包默认值 → config/alterego.toml → ALTEREGO_* 环境变量 → CLI 参数 → 测试注入

「随包默认值」是 ``alterego/defaults.toml``——它只回答一个问题：
**这个发行版默认挑选了哪些实现**。内核自己不回答这个问题（P1）。

三条刻意的设计约束：

1. **全部 ``frozen=True``**。配置在启动时解析一次，之后任何地方改它都是 bug。
   需要运行时可变的东西属于 ``PluginState``，不属于配置。
2. **校验在 ``__post_init__`` 里做**。宁可启动时退出码 2，也不要跑了一半才发现
   时区写错了、限额是负数。
3. **密钥只从环境变量来**。TOML 里写 ``"${ALTEREGO_LLM_API_KEY}"``，
   解析时替换；变量没设置就直接失败并点名。这样 ``alterego config``
   打印出来的东西天然是安全的（仍有 :meth:`Config.redacted` 兜底）。

环境变量命名：双下划线表示层级，
``ALTEREGO_CORE__LOG_LEVEL=DEBUG`` ≡ ``[core] log_level = "DEBUG"``。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import time
from pathlib import Path
from typing import Any, Literal, cast, get_args

from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config_settings import SettingsConfig
from alterego.kernel.config_study import StudyConfig
from alterego.kernel.config_values import DATASET_FORMATS, build_section, type_hints
from alterego.kernel.config_web import WebConfig
from alterego.kernel.errors import ConfigError
from alterego.kernel.logging import get_logger


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_CONFIG_PATHS",
    "ENV_PREFIX",
    "ChannelsConfig",
    "Config",
    "CoreConfig",
    "DisturbBudgetConfig",
    "LLMBudgetConfig",
    "LLMConfig",
    "LLMRoutingConfig",
    "PersonaConfig",
    "PluginsConfig",
    "RetentionConfig",
    "RoutingConfig",
    "SettingsConfig",
    "SimulationConfig",
    "StorageConfig",
    "WebConfig",
]

ENV_PREFIX = "ALTEREGO_"

_log = get_logger("kernel.config")

#: 按顺序查找的默认配置文件位置。
DEFAULT_CONFIG_PATHS: tuple[Path, ...] = (
    Path("config/alterego.toml"),
    Path("alterego.toml"),
)

#: 随包分发的最低优先级默认值。
#:
#: 这里刻意放**数据**而不是**代码**：像「默认用哪个 LLM provider」「默认用哪个
#: 存储后端」这类选择属于**发行版**，不属于内核。写死在 Python 默认值里，
#: 内核就变成了「知道具体技术的内核」，违反设计原则 P1。
#: 换成 ``defaults.toml`` 后，换个发行版只要换这个文件，内核一行都不用动。
DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parent.parent / "defaults.toml"

#: 出现在键名里就认为「打印出来之前要遮掉」。
_SECRET_KEY_PATTERN = re.compile(r"(api_key|token|secret|password|webhook|credential)", re.I)

#: TOML 里的 ``${VAR_NAME}``。
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MASK = "***"

SimulationMode = Literal["realtime", "fast", "turbo"]
BudgetExceedAction = Literal["degrade", "stop", "warn"]
StorageJournalMode = Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"]


# ═══════════════════════════════════════════════════════════════════════
#  各配置段
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CoreConfig:
    """``[core]`` —— 路径、日志、时区、随机种子。"""

    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    locale: str = "zh_CN"
    timezone: str = "Asia/Shanghai"
    #: 它对你的称呼。提示词里用它，而不是「用户」——
    #: 「用户」是产品经理的词，不是一个人称呼另一个人的词。
    user_name: str = "你"
    #: 固定种子 = 可复现推演（P6）。``None`` 表示每次启动随机。
    random_seed: int | None = 42

    def __post_init__(self) -> None:
        level = self.log_level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(
                "log_level 非法",
                log_level=self.log_level,
                supported=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            )
        if not self.user_name.strip():
            raise ConfigError(
                "user_name 不能是空白",
                hint="它是它对你的称呼；写「你」也行，但不能空着。",
            )
        # 提前解析一次：时区写错是最常见的配置错误，必须在启动时就暴露
        resolve_timezone(self.timezone)


@dataclass(frozen=True)
class SimulationConfig:
    """``[simulation]`` —— 推演节奏。"""

    mode: SimulationMode = "realtime"
    tick_interval_minutes: int = 5
    speed_multiplier: float = 1.0
    npc_tick_interval_minutes: int = 30
    enable_npc_conversations: bool = True
    max_consecutive_tick_failures: int = 5

    def __post_init__(self) -> None:
        if self.mode not in get_args(SimulationMode):
            raise ConfigError(
                "推演模式非法", mode=self.mode, supported=list(get_args(SimulationMode))
            )
        _require_positive("tick_interval_minutes", self.tick_interval_minutes)
        _require_positive("speed_multiplier", self.speed_multiplier)
        _require_positive("npc_tick_interval_minutes", self.npc_tick_interval_minutes)
        _require_positive("max_consecutive_tick_failures", self.max_consecutive_tick_failures)
        if self.mode == "turbo" and self.tick_interval_minutes < 15:
            # turbo 下用量按 tick 数放大，粒度过细会在几小时内烧完当日预算
            raise ConfigError(
                "turbo 模式的 tick 粒度不应低于 15 分钟",
                tick_interval_minutes=self.tick_interval_minutes,
                hint="见 docs/design/04-simulation-loop.md § 1.2",
            )


@dataclass(frozen=True)
class DisturbBudgetConfig:
    """``[disturb_budget]`` —— 它有多克制。

    被拦下的意图不会消失，而是降级为内心活动（ADR-0005）。
    调高这些数字会让它变烦人，调低会让它变冷淡。
    """

    daily_message_limit: int = 3
    daily_message_limit_urgent: int = 5
    daily_post_limit: int = 4
    quiet_hours: tuple[time, time] = (time(23, 30), time(8, 0))
    min_interval_minutes: int = 90
    consecutive_no_reply_limit: int = 3
    pending_topic_ttl_hours: int = 12

    def __post_init__(self) -> None:
        _require_non_negative("daily_message_limit", self.daily_message_limit)
        _require_positive("daily_post_limit", self.daily_post_limit)
        _require_positive("min_interval_minutes", self.min_interval_minutes)
        _require_positive("consecutive_no_reply_limit", self.consecutive_no_reply_limit)
        if self.daily_message_limit_urgent < self.daily_message_limit:
            raise ConfigError(
                "紧急上限不能低于普通上限（否则紧急情况反而发得更少）",
                daily_message_limit=self.daily_message_limit,
                daily_message_limit_urgent=self.daily_message_limit_urgent,
            )
        start, end = self.quiet_hours
        if start == end:
            raise ConfigError(
                "quiet_hours 的两端相同，会变成「全天静默」或「完全不静默」，语义不明",
                quiet_hours=[start.isoformat(), end.isoformat()],
            )

    def in_quiet_hours(self, when: time) -> bool:
        """判断某个时刻是否处于免打扰时段（支持跨零点）。"""
        start, end = self.quiet_hours
        if start < end:
            return start <= when < end
        return when >= start or when < end


@dataclass(frozen=True)
class LLMRoutingConfig:
    """``[llm.routing]`` —— 哪一类调用走哪档模型。

    这个分层让成本降到全强模型的 1/6.6（``06-roadmap.md`` § 5.3）。

    字段默认值刻意留空：具体用哪个 provider 是**发行版**的选择，不是内核的
    选择（设计原则 P1）。运行时这些值由随包分发的 ``alterego/defaults.toml``
    填入，见 :func:`Config.load`。
    """

    strong: str = ""
    cheap: str = ""
    decision: str = "strong"
    expression: str = "strong"
    reflection: str = "cheap"
    npc: str = "cheap"
    persona: str = "strong"
    memory: str = "cheap"
    vault: str = "cheap"

    def resolve(self, purpose: str) -> str:
        """把用途（如 ``"decision"``）解析成实际的 provider 名。

        允许一层间接（``decision = "strong"``）。不存在的用途直接报错，
        而不是悄悄回落到默认值——静默回落会让「我明明配了没生效」变成玄学。
        """
        tier = getattr(self, purpose, None)
        if tier is None:
            raise ConfigError(
                "未知的 LLM 用途",
                purpose=purpose,
                supported=[f.name for f in fields(self)],
            )
        resolved = getattr(self, tier, None)
        return resolved if isinstance(resolved, str) else tier


@dataclass(frozen=True)
class LLMBudgetConfig:
    """``[llm.budget]`` —— 成本天花板与超限行为。"""

    daily_usd_limit: float = 2.0
    monthly_usd_limit: float = 40.0
    max_calls_per_day: int = 800
    max_tokens_per_day: int = 2_000_000
    on_exceed: BudgetExceedAction = "degrade"

    def __post_init__(self) -> None:
        _require_positive("daily_usd_limit", self.daily_usd_limit)
        _require_positive("monthly_usd_limit", self.monthly_usd_limit)
        _require_positive("max_calls_per_day", self.max_calls_per_day)
        _require_positive("max_tokens_per_day", self.max_tokens_per_day)
        if self.monthly_usd_limit < self.daily_usd_limit:
            raise ConfigError(
                "月上限低于日上限，日上限永远不会生效",
                daily_usd_limit=self.daily_usd_limit,
                monthly_usd_limit=self.monthly_usd_limit,
            )
        if self.on_exceed not in get_args(BudgetExceedAction):
            raise ConfigError(
                "on_exceed 非法",
                on_exceed=self.on_exceed,
                supported=list(get_args(BudgetExceedAction)),
            )


@dataclass(frozen=True)
class LLMConfig:
    """``[llm]`` —— 模型调用。

    ``default_provider`` 的默认值来自随包分发的 ``alterego/defaults.toml``
    而不是这里：内核不该知道任何 provider 的名字（设计原则 P1）。
    """

    default_provider: str = ""
    timeout_seconds: int = 60
    max_retries: int = 3
    routing: LLMRoutingConfig = field(default_factory=LLMRoutingConfig)
    budget: LLMBudgetConfig = field(default_factory=LLMBudgetConfig)
    #: ``[llm.providers.<name>]`` 的原样内容，由各 provider 插件自己解析。
    providers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_positive("timeout_seconds", self.timeout_seconds)
        _require_non_negative("max_retries", self.max_retries)


@dataclass(frozen=True)
class PersonaConfig:
    """``[persona]`` —— 能否被改写。

    演化默认关闭：人格漂移是真实风险（``06-roadmap.md`` R2）。
    """

    evolution_enabled: bool = False
    evolution_interval_days: int = 30
    evolution_max_field_delta: float = 0.15
    generation_model: str = "persona"

    def __post_init__(self) -> None:
        _require_positive("evolution_interval_days", self.evolution_interval_days)
        delta = self.evolution_max_field_delta
        if not 0.0 < delta <= 1.0:
            raise ConfigError(
                "evolution_max_field_delta 必须在 (0, 1] 之间",
                evolution_max_field_delta=delta,
            )


@dataclass(frozen=True)
class ChannelsConfig:
    """``[channels]`` —— 渠道开关与各自的配置。"""

    enabled: tuple[str, ...] = ("channel.file", "channel.web")
    #: ``[channels.<id>]`` 的原样内容，由渠道插件自己解析。
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(set(self.enabled)) != len(self.enabled):
            raise ConfigError("channels.enabled 中有重复项", enabled=list(self.enabled))

    def options_for(self, channel_id: str) -> dict[str, Any]:
        """取某个渠道的配置。缺失时返回空 dict（插件应自己给出默认值）。"""
        value = self.options.get(channel_id, {})
        return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class RoutingFiltersConfig:
    """``[routing.filters]`` —— 发送前的最后一道过滤。"""

    suppress_quiet_hours: bool = True
    suppress_when_web_active: bool = False


@dataclass(frozen=True)
class RoutingConfig:
    """``[routing]`` —— 消息往哪个渠道发。"""

    primary: str = "web"
    outbound: tuple[str, ...] = ()
    level: Literal["all", "important", "urgent"] = "important"
    interactive_dedup_hours: int = 24
    filters: RoutingFiltersConfig = field(default_factory=RoutingFiltersConfig)

    def __post_init__(self) -> None:
        _require_non_negative("interactive_dedup_hours", self.interactive_dedup_hours)
        if self.level not in {"all", "important", "urgent"}:
            raise ConfigError("routing.level 非法", level=self.level)


@dataclass(frozen=True)
class StorageConfig:
    """``[storage]`` —— 持久化。

    ``backend`` 指向一个 ``StorageBackend`` 插件 id，默认值来自随包分发的
    ``alterego/defaults.toml``；内核只要求「必须有一个存储后端」，
    不知道它具体是什么（设计原则 P1，选型见 ADR-0003）。

    ``journal_mode`` 等属于 SQLite 的调优项保留在这里，是因为它们是
    *配置结构*而非*实现选型*——实现不认识这些键时直接忽略即可。
    """

    backend: str = ""
    db_path: Path = Path("data/alterego.db")
    journal_mode: StorageJournalMode = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    busy_timeout_ms: int = 5000
    foreign_keys: bool = True
    temp_store: Literal["DEFAULT", "FILE", "MEMORY"] = "MEMORY"
    checkpoint_on_start: bool = True
    integrity_check_on_start: bool = False
    backup_before_destructive_migration: bool = True
    """破坏性迁移（会删表、删列、重建表）之前是否自动备份，默认备份。

    关掉它**不等于「那就别备份了」**，而是「不给退路就别改」：
    存储层在没有备份目录时会直接拒绝执行破坏性迁移。
    这样写是为了让关掉开关的后果落在「迁移没跑成」而不是「数据没了」——
    前者重跑一次就好，后者不可逆。
    """

    def __post_init__(self) -> None:
        _require_positive("busy_timeout_ms", self.busy_timeout_ms)
        if self.journal_mode not in get_args(StorageJournalMode):
            raise ConfigError("journal_mode 非法", journal_mode=self.journal_mode)


@dataclass(frozen=True)
class RetentionConfig:
    """``[retention]`` —— 保留策略。

    ``tick_log`` 占每年存储的约 81%（``06-roadmap.md`` § 5.4），
    因此它是唯一一个「值得调低」的表。
    """

    tick_log_keep_days: int = 90
    tick_log_detail: Literal["full", "summary"] = "full"
    activity_log_keep_days: int = 365
    memory_keep_forever: bool = True
    event_log_enabled: bool = False
    auto_vacuum: bool = True

    def __post_init__(self) -> None:
        _require_positive("tick_log_keep_days", self.tick_log_keep_days)
        _require_positive("activity_log_keep_days", self.activity_log_keep_days)


#: ``domain.dataset.FORMATS`` 的镜像，住在 ``config_values.py``：
#: 本文件顶在 900/900（``scripts/check_architecture.sh`` 第 23 项），一个字的余量都没有。
#: 用法见 :class:`DatasetConfig` 的 ``__post_init__``。
@dataclass(frozen=True)
class DatasetConfig:
    """``[dataset]`` —— 训练数据集的导出。

    数据集是库的**派生产物**：只读 ``message`` / ``tick_log`` / ``activity_log``
    三张已有的表，不建任何新表，也不往回写（ADR-0011）。

    所以这里没有任何开关式字段。没有 ``enabled``——不想要这个功能，
    不跑那条命令就行；加一个 ``enabled`` 只会让「为什么没数据」多一种可能。
    也没有 LLM 相关字段——导出一步都不调模型。
    """

    export_dir: Path = Path("exports/datasets")
    """导出根目录。相对路径按**当前工作目录**解析，与知识库的 ``exports/`` 一致。

    实际落盘在 ``<export_dir>/<角色名>/``。多套一层角色名，
    是为了将来多角色共用一份配置时互不覆盖。
    """

    formats: tuple[str, ...] = ("chat",)
    """这一批要产出哪几种形状：``chat`` / ``sharegpt`` / ``alpaca``。

    可以同时选多个。同一批样本渲染成多份，直接拿去比哪份训出来的好——
    渲染不调任何模型，代价只有磁盘。

    默认只出 ``chat``：多出一个用不上的文件，比少一个你以为存在的文件好。
    """

    redact_terms: tuple[str, ...] = ()
    """额外要摘掉的**字面量**（不是正则）。

    在八个内置规则之外再加一份你自己的名单：真名、公司名、住址里的门牌。
    按字面量处理，所以填 ``红中`` 不会被当成量词；留空就只用内置规则。

    ⚠️ 改了它之后要重跑 ``alterego dataset build``。``dataset list`` 会比对
    规则指纹并提示哪一批是旧规则脱的，但**不会自动重跑**——什么时候花这份
    时间该由你定。
    """

    lookback_days: int = 30
    """往回看多少天。

    比知识库默认的 7 天长：知识库要的是「最近发生了什么」，
    训练集要的是「够不够多」。调小会让样本变少、导出变快。
    """

    def __post_init__(self) -> None:
        _require_positive("lookback_days", self.lookback_days)
        if not self.formats:
            raise ConfigError(
                "formats 不能为空",
                hint="至少选一个：chat / sharegpt / alpaca",
            )
        unknown = sorted(set(self.formats) - DATASET_FORMATS)
        if unknown:
            raise ConfigError(
                "formats 里有不认识的形状",
                unknown=unknown,
                supported=sorted(DATASET_FORMATS),
            )
        if any(not term.strip() for term in self.redact_terms):
            # 静默忽略一条用户明确要求的脱敏词 = 隐私事故。这里必须响。
            raise ConfigError(
                "redact_terms 里有空条目",
                hint="空条目会被规则表跳过，等于你要求的脱敏根本没发生；把它删掉",
            )


@dataclass(frozen=True)
class PluginsConfig:
    """``[plugins]`` —— 插件体系自身的行为。"""

    enabled: tuple[str, ...] = ()
    search_paths: tuple[Path, ...] = (Path("plugins"),)
    auto_reload: bool = False
    auto_reload_interval_seconds: float = 1.0
    circuit_breaker_threshold: int = 5
    isolate_failures: bool = True
    #: ``[plugins.config."<plugin.id>"]`` 的原样内容。
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_positive("auto_reload_interval_seconds", self.auto_reload_interval_seconds)
        _require_positive("circuit_breaker_threshold", self.circuit_breaker_threshold)
        if self.auto_reload and self.auto_reload_interval_seconds < 0.2:
            # 轮询 mtime 太频繁会让空闲时的 CPU 占用肉眼可见
            raise ConfigError(
                "auto_reload_interval_seconds 过小（建议 ≥ 1）",
                auto_reload_interval_seconds=self.auto_reload_interval_seconds,
            )

    def config_for(self, plugin_id: str) -> dict[str, Any]:
        """取某个插件的配置。"""
        value = self.config.get(plugin_id, {})
        return dict(value) if isinstance(value, Mapping) else {}


# ═══════════════════════════════════════════════════════════════════════
#  顶层
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Config:
    """全部配置。由 :meth:`load` 构造，不要手工 new。"""

    core: CoreConfig = field(default_factory=CoreConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    disturb_budget: DisturbBudgetConfig = field(default_factory=DisturbBudgetConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    study: StudyConfig = field(default_factory=StudyConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    settings: SettingsConfig = field(default_factory=SettingsConfig)
    #: 实际读到的配置文件；全部使用内置默认值时为 ``None``。
    source: Path | None = None
    #: 配置文件里存在但内核不认识的键，用点分路径表示（``"llm.routing.decisionn"``）。
    #: 只告警，不失败——插件可能需要它们（P4 可插拔优于可配置）。
    unknown_keys: tuple[str, ...] = ()

    # ── 加载 ────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        overrides: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        require_file: bool = False,
    ) -> Config:
        """按优先级装配配置。

        优先级（``docs/design/01-architecture.md`` § 2.2）::

            随包默认值 → 用户配置文件 → ALTEREGO_* 环境变量 → overrides

        Args:
            path: 配置文件路径。``None`` 时依次尝试 :data:`DEFAULT_CONFIG_PATHS`。
            overrides: 最高优先级的覆盖（CLI 参数或测试注入），嵌套 dict。
            env: 环境变量映射。``None`` 时取 ``os.environ``。
            require_file: 为 ``True`` 时找不到配置文件就报错（``alterego init``
                之外的所有命令都应该用默认值静默继续）。

        Returns:
            校验完毕的 :class:`Config`。

        Raises:
            ConfigError: 配置文件语法错误、缺少被引用的环境变量、或任何校验失败。
        """
        environ = os.environ if env is None else env
        raw: dict[str, Any] = {}
        source: Path | None = None
        found = _find_config_file(path)
        if found is None:
            if require_file:
                raise ConfigError(
                    "找不到配置文件",
                    searched=[str(p) for p in DEFAULT_CONFIG_PATHS],
                    hint="先运行 `alterego init` 生成 config/alterego.toml",
                )
        else:
            source = found
            raw = _read_toml(found)

        merged = _deep_merge(_packaged_defaults(), raw)
        merged = _deep_merge(merged, _env_overrides(environ))
        if overrides:
            merged = _deep_merge(merged, dict(overrides))
        merged = _normalize(merged)
        merged = _resolve_env_references(merged, environ)

        known, unknown = _split_known(merged, cls)
        if unknown:
            _log.warning(
                "配置里有内核不认识的键，已忽略: %s（%s）",
                ", ".join(unknown),
                source if source is not None else "内置默认值",
            )
        config = build_section(cls, known)
        return _replace(config, source=source, unknown_keys=tuple(unknown))

    # ── 派生路径 ────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        """数据目录（已解析为绝对路径）。"""
        return _absolute(self.core.data_dir)

    @property
    def database_path(self) -> Path:
        """数据库文件路径。相对路径解析到 :attr:`data_dir` 之下。"""
        db = self.storage.db_path
        return db if db.is_absolute() else self.data_dir / db.name

    @property
    def backups_dir(self) -> Path:
        """备份目录。"""
        return self.data_dir / "backups"

    @property
    def outbox_dir(self) -> Path:
        """文件渠道的离线兜底目录。"""
        return self.data_dir / "outbox"

    @property
    def birthdays_path(self) -> Path:
        """生日记录文件（`data/birthdays.toml`）。

        和上面几个一样是**派生路径**而不是配置项：能被配置的只有
        「数据目录在哪」，文件名跟着它走。见 ``alterego.birthdays``。
        """
        return self.data_dir / "birthdays.toml"

    @property
    def plugin_search_paths(self) -> tuple[Path, ...]:
        """插件搜索路径（已解析为绝对路径）。"""
        return tuple(_absolute(p) for p in self.plugins.search_paths)

    def ensure_directories(self) -> None:
        """创建必要的目录。启动时调用一次。"""
        for directory in (self.data_dir, self.backups_dir, self.outbox_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ── 展示 ────────────────────────────────────────────────

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """转成嵌套 dict，供 ``alterego config`` 打印。

        Args:
            redact: 是否把疑似密钥的值替换为 ``***``。默认开启——
                配置经常被贴到 issue 里。
        """
        # ``_dump`` 是「任意值 → JSON 友好值」的递归函数，返回类型只能是 Any；
        # 但 Config 本身是 dataclass，走到这里必然是 dict。
        return cast("dict[str, Any]", _dump(self, redact=redact))

    def redacted(self) -> dict[str, Any]:
        """``to_dict(redact=True)`` 的别名，强调用途。"""
        return self.to_dict(redact=True)


# ═══════════════════════════════════════════════════════════════════════
#  内部实现
# ═══════════════════════════════════════════════════════════════════════


def _require_positive(name: str, value: float) -> None:
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
        return
    raise ConfigError("配置项必须为正数", key=name, value=value)


def _require_non_negative(name: str, value: float) -> None:
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
        return
    raise ConfigError("配置项不能为负数", key=name, value=value)


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _find_config_file(path: Path | None) -> Path | None:
    if path is not None:
        if not path.exists():
            raise ConfigError("指定的配置文件不存在", path=str(path))
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _normalize(merged: Mapping[str, Any]) -> dict[str, Any]:
    """把「面向用户的 TOML 形状」整理成「面向 dataclass 的形状」。

    配置文件里写的是 ``[channels.file]``、``[llm.providers.<名字>]``——
    这类「以用户起的名字为键」的表无法预先声明成字段，因此统一收进
    一个容器字段：

    - ``[channels.*]``  → ``channels.options``
    - ``[llm.*]``       → ``llm.providers``
    - ``[plugins.*]``   → ``plugins.config``

    这一步是显式的而不是靠 ``**kwargs``：写错名字的后果应该是能预料到的。
    """
    result = dict(merged)
    _collapse_extras(result, "channels", ChannelsConfig, "options")
    _collapse_extras(result, "llm", LLMConfig, "providers")
    _collapse_extras(result, "plugins", PluginsConfig, "config")
    return result


def _collapse_extras(
    root: dict[str, Any],
    section: str,
    cls: type[Any],
    container: str,
) -> None:
    """把 ``root[section]`` 里不认识的键挪进 ``root[section][container]``。"""
    data = root.get(section)
    if not isinstance(data, Mapping):
        return
    # 容器字段本身也算「认识的键」，这样 `[llm.providers.x]` 这种写法同样可用
    declared = _field_names(cls)
    extras = {key: value for key, value in data.items() if key not in declared}
    if not extras:
        return
    existing = data.get(container)
    merged_extras = _deep_merge(existing if isinstance(existing, Mapping) else {}, extras)
    root[section] = {key: value for key, value in data.items() if key in declared}
    root[section][container] = merged_extras


def _read_toml(path: Path) -> dict[str, Any]:
    import tomllib  # 3.11+ 内置；放在函数里是为了让 `import alterego` 保持廉价

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("配置文件不是合法 TOML", path=str(path), error=str(exc)) from exc
    except OSError as exc:
        raise ConfigError("配置文件无法读取", path=str(path), error=str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是表（table）", path=str(path))
    return data


def _packaged_defaults() -> dict[str, Any]:
    """读随包分发的 :data:`DEFAULT_CONFIG_PATH`，作为最低优先级的默认值。

    文件不在（例如非 editable 安装时打包漏了数据文件）就返回空 dict，
    让代码里的 dataclass 默认值兜底——**不能**因此启动失败，
    更不能用代码里的具体技术名去补（那正是本文件要避免的事）。
    """
    if not DEFAULT_CONFIG_PATH.is_file():
        return {}
    return _read_toml(DEFAULT_CONFIG_PATH)


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并，``patch`` 覆盖 ``base``。不修改入参。"""
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """把 ``ALTEREGO_CORE__LOG_LEVEL=DEBUG`` 变成 ``{"core": {"log_level": "DEBUG"}}``。"""
    result: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        if len(path) < 2:
            # 只有一级的 ALTEREGO_XXX 无法定位到配置段，忽略而不是猜
            continue
        cursor = result
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value
    return result


def _resolve_env_references(value: Any, environ: Mapping[str, str]) -> Any:
    """递归替换 ``${VAR}``。缺变量直接失败并点名。"""
    if isinstance(value, str):
        missing: set[str] = set()

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = environ.get(name)
            if resolved is None:
                missing.add(name)
                return ""
            return resolved

        replaced = _ENV_REFERENCE.sub(substitute, value)
        if missing:
            raise ConfigError(
                "配置引用了未设置的环境变量",
                variables=sorted(missing),
                hint="在 .env 或 shell 里设置它们；.env 已被 .gitignore 排除",
            )
        return replaced
    if isinstance(value, Mapping):
        return {k: _resolve_env_references(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_references(item, environ) for item in value]
    return value


def _split_known(merged: Mapping[str, Any], cls: type[Any]) -> tuple[dict[str, Any], list[str]]:
    """分出内核认识的键与多余的键，**递归到嵌套配置段**。多余的只告警——插件可能要用。

    只看顶层是不够的：``[llm.routing]`` 里把 ``decision`` 敲成 ``decisionn``，键落在已知的
    ``llm`` 段内，于是既不进 ``unknown_keys``，也不会被 :func:`build_section` 认领（后者只遍历
    dataclass 自己的字段名）——**静默消失**。设计文档把这种失效列为不可接受，见
    ``docs/design/07-model-routing-and-media.md`` § 3.1.1。

    ``Mapping[str, Any]`` 类型的字段是**开放的**（``llm.providers`` / ``channels.options`` /
    ``plugins.config`` 里的键由插件自己解释），内核无从判断，因此不往里递归。注解取不到时
    同样不递归——猜错比不猜更糟。
    """
    declared = _field_names(cls)
    hints = type_hints(cls)
    known: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in merged.items():
        if key not in declared:
            unknown.append(key)
            continue
        known[key] = value
        annotation = hints.get(key)
        if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, Mapping):
            _, nested = _split_known(value, annotation)
            unknown.extend(f"{key}.{path}" for path in nested)
    return known, unknown


def _field_names(cls: type[Any]) -> frozenset[str]:
    return frozenset(f.name for f in fields(cls))


def _replace(config: Config, **changes: Any) -> Config:
    """``dataclasses.replace`` 的类型友好包装。"""
    return replace(config, **changes)


def _dump(value: Any, *, redact: bool, key: str = "") -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _dump(getattr(value, f.name), redact=redact, key=f.name) for f in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, Mapping):
        return {k: _dump(v, redact=redact, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item, redact=redact, key=key) for item in value]
    if redact and key and _SECRET_KEY_PATTERN.search(key) and value:
        return _MASK
    return value

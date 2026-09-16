"""仓储契约。

上层（`sim/`、CLI）需要读写记忆与行为日志，但**不该知道它们躺在 SQLite 里**。
``scripts/check_architecture.sh`` 第 3 组红线只允许组装根（``cli*.py``）与
``storage/`` 提到具体存储实现，所以形状必须有一个中立的地方放——就是这里。

三条约定（与 `docs/design/03-data-model.md` § 7 一致）：

1. 所有方法都可能抛 :class:`~alterego.kernel.errors.StorageError`；
2. 调用方在 ``with backend.transaction():`` 里连着调几个写方法时，它们会合并成
   一个事务——**仓储自己不 BEGIN**，事务边界属于调用方；
3. **仓储不含业务逻辑**。「重要度怎么衰减」「什么时候该降权」属于 `domain/`，
   仓储只负责「一行 ↔ 一个对象」。

依据: docs/design/03-data-model.md § 6.9、§ 7
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol


if TYPE_CHECKING:
    from alterego.domain.dataset import ActivityRow, MessageRow, TickRow
    from alterego.domain.emotion import Emotion
    from alterego.domain.memory import Memory, MemoryKind


__all__ = [
    "ActivityRecord",
    "ActivityRepository",
    "BudgetRepository",
    "BudgetUsage",
    "ConversationRecord",
    "ConversationRepository",
    "DatasetSourceRepository",
    "EmotionRepository",
    "MemoryRepository",
    "MessageRecord",
    "PersonaRecord",
    "PersonaRepository",
    "ScheduleRecord",
    "ScheduleRepository",
    "SocialPostRecord",
    "SocialPostRepository",
    "SourceRecord",
    "SourceRepository",
    "TickLogDraft",
    "TickLogRepository",
    "UsageGroup",
    "UsageRepository",
    "UsageTotal",
]


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """``activity_log`` 的一行。

    它暂时住在这里而不是 ``domain/``：目前只有一个用途——渲染进提示词。
    还没有需要纯函数去处理的规则，为它单开一个领域模型是空壳。

    Attributes:
        id: 行为 id。
        intent: 当时选中的意图，取自 `docs/DESIGN.md` 的意图清单。
        description: 一句话描述做了什么。
        started_at: 开始时间（**虚拟时间**，由 ``ctx.clock`` 决定）。
        category: ``internal`` / ``social`` / ``outbound``。
        location: 地点，可能为空。
        inner_voice: 内心独白，含「想说但没说」的那些。梳理记忆时它比
            ``description`` 更有信息量——一个人记住的往往是自己当时怎么想，
            而不是自己当时做了什么。
        duration_minutes: 持续分钟数，可能为 0（未知）。
        detail: 结构化补充，落 ``detail_json``。放的是「这次行为带回来什么」——
            能力的产物、失败原因。空的时候列里仍是合法的 ``{}``，
            所以「没有细节」与「细节是空对象」在库里长得一样，读的人不必区分。
    """

    id: str
    intent: str
    description: str
    started_at: datetime
    category: str = ""
    location: str = ""
    inner_voice: str = ""
    duration_minutes: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: 下面这些只在**写**的时候有意义：读回来的 ``ActivityRecord`` 用不到它们，
    #: 因为调用方拿到的己经是某个角色的行。同样一个类承担两个方向，
    #: 是因为再拆一个 ``ActivityDraft`` 只会让两份字段列表各自漂移。
    persona_id: str = ""
    actor_kind: str = "persona"
    actor_id: str = ""
    outbound: bool = False
    outbound_ref: str = ""
    suppressed_intent: str = ""
    suppress_reason: str = ""
    tick_id: str = ""


class MemoryRepository(Protocol):
    """记忆的读写。"""

    def save(self, memory: Memory) -> None:
        """写入一条记忆。id 相同时覆盖。"""
        ...

    def save_many(self, memories: Sequence[Memory]) -> None:
        """批量写入。一次巩固会产出好几条，逐条写会开好几个事务。"""
        ...

    def get(self, memory_id: str) -> Memory | None:
        """按 id 取一条。不存在返回 `None`。"""
        ...

    def list_recent(
        self,
        persona_id: str,
        *,
        since: datetime,
        kind: MemoryKind | None = None,
        not_consolidated: bool = False,
        limit: int = 100,
    ) -> list[Memory]:
        """按时间列出 ``since`` 之后的记忆，最早的在前面。

        ``not_consolidated=True`` 时只给还没被巩固过的那些。两个方向共用
        这一个方法：梳理要「最近发生的」，巩固要「还没归纳过的」。
        """
        ...

    def mark_consolidated(self, memory_ids: Sequence[str], at: datetime) -> None:
        """标记这批记忆已被巩固。

        标记而不是删除：巩固只是「细节变模糊」，不是「没发生过」。
        ``at`` 是**虚拟时间**，不是墙上时间。
        """
        ...

    def set_importance(self, updates: Sequence[tuple[str, float]]) -> None:
        """批量改重要度，值为 ``(memory_id, importance)``。

        ``apply_consolidation()`` 算出来的新值由调用方传进来——
        仓储不重复一遍衰减公式。
        """
        ...


class ActivityRepository(Protocol):
    """行为日志的读写。"""

    def list_undistilled(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 200,
    ) -> list[ActivityRecord]:
        """列出 ``since`` 之后发生的、**还没被梳理过**的行为，按时间正序。

        正序而不是倒序：梳理是「读一遍这一天」，倒着读会把因果读反。
        """
        ...

    def mark_distilled(self, activity_ids: Sequence[str], at: datetime) -> None:
        """标记这批行为已被梳理进记忆。"""
        ...

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ActivityRecord]:
        """``since`` 到 ``until`` 之间的**全部**行为，按时间正序。

        和 :meth:`list_undistilled` 的区别是**不看 ``distilled_at``**：
        写日记要的是「这一天真实发生过什么」，而不是「还有什么没被梳理进记忆」。
        被梳理过的行为同样是那天的一部分——恰恰是被梳理过的那些往往最要紧。
        """
        ...

    def append(self, records: Sequence[ActivityRecord]) -> None:
        """追写一批行为。

        批次而不是单条：一次 tick 可能同时做成一件事、拦下三件想做没做的事，
        单条接口会让那三件被拦下的事要么各开一个事务、要么干脆不记。
        """
        ...


class EmotionRepository(Protocol):
    """情绪轨迹的读写。

    **它的存在是为了让上一轮的它接得上下一次。** ``ReflectStage`` 从
    ``state.emotion`` 出发算回归，而那份快照只能来自库里：不读旧情绪，
    每一轮都从基线重新开始——表现出来就是「它永远是同一个心情」，
    而这是个很难从日志里看出来的 bug（每一轮的 ``reason`` 都自洽）。

    一次 tick 一行是**故意的**，不是浪费：``emotion_log`` 就是一条时间序列
    （``03-data-model.md`` 按每天 288 行估算它的体量），而「曲线在这一天怎么走」
    只能从点里看出来。
    """

    def latest(self, persona_id: str) -> Emotion | None:
        """最近一条情绪。没有记录返回 ``None``。

        ``None`` 是「还不知道它现在心情如何」，不是「一条平静的情绪」：
        两者在 ``reflect`` 里走的是同一段兼容代码，但在文档与调试里
        是两个完全不同的起点（新装好的它 vs 一个真的平静的它）。
        """
        ...

    def append(
        self,
        persona_id: str,
        emotion: Emotion,
        *,
        reason: str = "",
        causes: Sequence[str] = (),
        tick_id: str = "",
    ) -> None:
        """记一条。时间取 ``emotion.updated_at``——

        那个字段就是「这份情绪算到哪一刻为止」，另传一个 ``recorded_at``
        会让两者有机会不一致，而它们一旦不一致，回归曲线就会从错的地方重算。

        ``reason`` 与 ``causes`` 是 ``alterego why`` 的全部原料
        （见 ADR-0004）。
        """
        ...


# ── 知识库要读的三样东西 ────────────────────────────────────
#
# 下面三个契约是「把库里的东西写成笔记」用的。它们**只读**：知识库是
# 库的**下游**，从不往回写。这一条很要紧——一旦允许它回写，
# 「用户手改了一个 Markdown 文件」就得有冲突解决策略，
# 那是另一个量级的事（docs/plans/2026-09-16-obsidian-vault.md § 9）。


@dataclass(frozen=True, slots=True)
class PersonaRecord:
    """``persona`` 的一行，只取写索引页开头要用的那几个字段。

    刻意**不带** ``persona_json``：整份人格数据有性格、表达、偏好、背景、
    情绪基线，而这里只要一句「我是谁」。全量传过来会诱使调用方
    把整段塞进笔记，那既不是用户想看的，也会随人格演化和文件内容脱节。
    """

    id: str
    name: str
    age: int | None = None
    gender: str = ""
    city: str = ""
    occupation: str = ""
    version: int = 1


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """``schedule_block`` 的一行：计划做什么，以及实际几点做的。

    计划和实际**都要**带出来。只写计划，笔记就成了一张没人兑现的时间表；
    而 ``deviation_note`` 是「为什么没按计划来」，那通常是这一天里
    最有意思的一句话。
    """

    id: str
    day: date
    start_at: datetime
    end_at: datetime
    activity: str
    category: str = "other"
    #: 是否允许被主动联系打断。**必须带出来**：打扰预算的第一道检查就是它
    #: （``sim/budget.check_budget``），而它一旦缺失，唯一安全的默认值是
    #: 「可以打断」——那等于把「开会时别发消息」变成一条永远不会生效的规则。
    interruptible: bool = True
    location: str = ""
    actual_start_at: datetime | None = None
    actual_end_at: datetime | None = None
    deviation_note: str = ""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """``source_item`` 的一行：它搜集到的一条信息。

    ``dropped=True`` 的那些**不该**出现在这里——被判定为提示词注入、
    过长或重复的内容是过滤掉的中间产物，不是它「搜集到的东西」。
    """

    id: str
    url: str
    fetched_at: datetime
    title: str = ""
    summary: str = ""
    source_kind: str = "search"
    published_at: datetime | None = None
    lang: str = ""
    memory_id: str | None = None


class PersonaRepository(Protocol):
    """人设的只读访问。"""

    def get(self, persona_id: str) -> PersonaRecord | None:
        """按 id 取。不存在返回 `None`。"""
        ...

    def find_by_name(self, name: str) -> PersonaRecord | None:
        """按**名字**找。敲命令的人手上有名字，id 是他从没见过的一串字符。"""
        ...

    def list_all(self) -> list[PersonaRecord]:
        """全部人设，按创建时间。用来在「有多个」时报出候选。"""
        ...

    def document(self, persona_id: str) -> Mapping[str, Any]:
        """取整份人格文档（``persona_json``）。不存在返回空字典。

        :class:`PersonaRecord` 故意不带它——索引页只需要一句「我是谁」。
        但**写提示词的地方必须要它**：语气、口头禅、表达习惯都在这一份里，
        而它们是 ``prompts/chat_reply.md`` 那十几个占位符的真正来源。

        返回的是**原样的 JSON 对象**：schema 由 ``persona_generate`` 提示词
        决定，现在把它序列化成一个 dataclass 会把「人设能长成什么样」锁死在
        代码里，而它本来应该由提示词与用户输入决定。读的人自己宽容取键。
        """
        ...


class ScheduleRepository(Protocol):
    """日程的只读访问。"""

    def list_day(self, persona_id: str, *, day: date) -> list[ScheduleRecord]:
        """某一天的全部日程块，按开始时间正序。"""
        ...

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ScheduleRecord]:
        """一段时间的日程块。用来补写过去几天漏掉的日记。"""
        ...


class SourceRepository(Protocol):
    """搜集到的信息的只读访问。"""

    def list_kept(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 100,
    ) -> list[SourceRecord]:
        """``since`` 之后**没被丢掉**的条目，按抓取时间正序。

        ``dropped`` 的那些不出现：它们是过滤掉的中间产物
        （提示词注入、过长、重复），不是它「搜集到的东西」。
        """
        ...


@dataclass(frozen=True, slots=True)
class SocialPostRecord:
    """``social_post`` 的一行：它自己发的一条动态。

    动态与消息是两条不同的产品线：消息有收件人，动态没有。所以它不共用
    :class:`MessageRecord`——「发给谁」在动态上是空值，而把空值塞进一个
    必填字段会让每个读的人都要先回答「这里的空是什么意思」。

    ``mood_*`` 三个字段是**发的时候**的情绪快照，不是外键：情绪曲线会继续
    演化，而「这条动态是在什么心情下发的」必须停在这一刻。
    """

    id: str
    persona_id: str
    content: str
    posted_at: datetime
    image_paths: tuple[str, ...] = ()
    location: str = ""
    mood_label: str = ""
    mood_valence: float | None = None
    mood_arousal: float | None = None
    intent_motivation: str = ""
    trigger_note: str = ""
    activity_ref: str = ""
    like_count: int = 0
    comment_count: int = 0
    visible: bool = True
    tick_id: str = ""


class SocialPostRepository(Protocol):
    """动态的读写。"""

    def append(self, post: SocialPostRecord) -> None:
        """写一条动态。同一个 id 再来一次是覆盖，而不是多出一条。"""
        ...

    def list_recent(
        self,
        persona_id: str,
        *,
        limit: int = 30,
        visible_only: bool = True,
    ) -> list[SocialPostRecord]:
        """最近的动态，按发布时间倒序。"""
        ...


# ────────────────────────────────────────────────────────────
# 训练数据集的源。**下面这个契约是只读的**：数据集是库的派生产物，
# 从不往回写（ADR-0011）。这也是它不需要任何迁移的原因。
# ────────────────────────────────────────────────────────────


class DatasetSourceRepository(Protocol):
    """训练数据集要用到的三张源表的只读访问。

    **返回的是 ``domain.dataset`` 里的行，不是本模块的 ``*Record``。**
    看起来不一致，但另一条路更糟：那三个行类型与库里的列一一对应，
    而 ``domain/dataset.py`` 的纯函数直接吃它们——中间再放一层 DTO，
    只会让时间字段在「库里的 TEXT → ``datetime`` → 又变回 TEXT」之间来回转两趟，
    而每一趟都是可能出错的转换点。

    这也正是本模块文件头对 :class:`ActivityRecord` 的解释里说的那种情况：
    「还没有需要纯函数去处理的规则」的行住在这里；**有纯函数要吃它们的行，
    就住在 ``domain/``**。
    """

    def list_conversation_messages(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[MessageRow]:
        """**和用户的**会话里的消息，按 ``(conversation_id, created_at, id)`` 正序。

        只取 ``counterpart_kind = 'user'`` 的会话。和 NPC 的来往不在这一批里：
        那批数据训的是「它怎么和别的角色相处」，与「它怎么和你说话」
        是两件事，混在一起会稀释掉后者。

        正序是有意的——``domain`` 那边合并相邻同向的消息，
        顺序错了合出来的话也就错了。
        """
        ...

    def list_ticks(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 5000,
    ) -> list[TickRow]:
        """``since`` 之后的推演日志，按 ``(virtual_time, id)`` 正序。

        ``status`` 不在这里筛。失败的轨迹也要取出来——由 ``domain`` 决定
        哪些能用，因为「哪些算成功」是业务判断，不是取数的事。
        """
        ...

    def list_activities(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[ActivityRow]:
        """``since`` 之后的行为日志，按 ``(started_at, id)`` 正序。"""
        ...

    def map_tick_intents(
        self,
        persona_id: str,
        *,
        since: datetime,
    ) -> dict[str, str]:
        """``tick_id → chosen_intent``，给工具调用数据集补「它想做的是什么」。

        单独的查询而不是让调用方从 ``list_ticks`` 里自己挖：行为日志里
        只有被规范化过的 ``intent``，而两者之差恰恰是「它想做」与「它实际做的」。
        """
        ...


# ── 推演要写的东西 ──────────────────────────────────────────
#
# 上面那几个契约是给「读」用的——知识库、数据集、CLI 都在读。下面这四个是给
# 「写」用的，而且**只有推演引擎在写**。读写分开不是洁癖：读要的是「把它渲染
# 进提示词」，写要的是「这一次它做了什么」，中间差的字段强行合成一个类，
# 结果是每一侧都背着一半用不上的字段。


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """``conversation`` 的一行。

    ``counterpart_id`` 对用户会话恒为 ``"user"``，对 NPC 会话是 ``npc.id``——
    真正的区分靠 ``counterpart_kind``，因为一个 NPC 的 id 完全可能就叫 ``user``。
    """

    id: str
    persona_id: str
    counterpart_id: str
    counterpart_kind: str
    title: str = ""
    message_count: int = 0
    last_message_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """``message`` 的一行（写方向）。

    ``initiative`` 与 ``motivation`` 是**主动消息**才有的：区分「你说话它回」
    与「它突然找你」是这套数据最要紧的一列——前者谁都会，后者才是这个项目
    要模拟的东西。``delivered_channels`` 为空的出站消息表示「生成了但还没发出去」。
    """

    id: str
    conversation_id: str
    direction: str
    sender_id: str
    content: str
    content_type: str = "text"
    initiative: bool = False
    motivation: str = ""
    trigger_note: str = ""
    delivered_channels: tuple[str, ...] = ()
    read_at: datetime | None = None
    replied_at: datetime | None = None
    tick_id: str = ""
    created_at: datetime | None = None


class ConversationRepository(Protocol):
    """会话与消息的读写。

    ``ensure`` / ``append`` 两个写方法都**幂等**：同一个 id 再来一次是更新而不是
    报错。推演会在失败后重跑同一段时间，而「重跑一次就多出一条重复消息」
    会让用户看到自己说过两遍。
    """

    def ensure(self, record: ConversationRecord) -> ConversationRecord:
        """按 ``(persona_id, counterpart_id)`` 取出会话；没有就建一个。

        返回的是**库里真实的那一行**（消息数、最后消息时间都是最新的）。
        """
        ...

    def append(self, message: MessageRecord) -> None:
        """写一条消息，并把所在会话的 ``message_count`` 与 ``last_message_at`` 推上去。"""
        ...

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[MessageRecord]:
        """按时间**正序**取消息；给了 ``before`` 就取它之前的。

        正序是因为调用方（组装对话上下文）要的是「从早到晚读一遍」；
        要最新的 N 条时取完再自己切——比让仓储提供两种排序简单，
        也让「倒序取 N 条再翻回来」这种典型的 off-by-one 不会发生。
        """
        ...

    def count_unread(self, conversation_id: str) -> int:
        """有多少条入站消息还没回过。

        「已回」的判据是**后面已有出站消息**，而不是 ``read_at`` 不为空：
        已读不回也是一种状态，而它这里关心的是「有人在等它说话」。
        """
        ...

    def last_inbound_at(self, conversation_id: str) -> datetime | None:
        """最后一条入站消息的时间。没有就返回 ``None``。

        ``None`` 与「很久以前」是两件事：前者是「刚装好，还没说过话」，
        后者是「被冷落了」。混为一谈会让新装好的它在第一天就摆出一副
        被冷落的样子。
        """
        ...


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """某一天的打扰预算用量。对应 ``budget_usage`` 表的一行。

    **它为什么住在这里而不是 ``sim/budget.py``。** 它是「一行 ↔ 一个对象」里
    的那个对象，而 ``scripts/check_architecture.sh`` 第 16 组红线禁止 ``storage/``
    引用 ``sim/``——仓储不能为了描述一行而反向依赖推演层。**规则**
    （:func:`alterego.sim.budget.check_budget`）留在 ``sim/``：形状是数据，
    「什么时候该拦」是业务。

    所有时间字段都是**虚拟时间**：推演可以 60 倍速跑，「今天」指的是它自己的今天。
    """

    day: date
    messages_sent: int = 0
    posts_sent: int = 0
    messages_suppressed: int = 0
    posts_suppressed: int = 0
    #: 连续多少次主动消息没等到回复。到阈值就熔断。
    consecutive_no_reply: int = 0
    #: 熔断截止时间。``None`` 表示没熔断。
    circuit_until: datetime | None = None
    last_message_at: datetime | None = None
    last_post_at: datetime | None = None


class BudgetRepository(Protocol):
    """打扰预算的当日用量。"""

    def load(self, persona_id: str, *, day: date) -> BudgetUsage:
        """取某一天的用量。没有记录就返回一条空的。

        **不返回 ``None``。** 让调用方处理三种状态（没有记录 / 有记录但为空 /
        有记录）是在制造 bug：第一次跑的那天正好是最容易被忘记处理的那种。
        """
        ...

    def save(self, persona_id: str, usage: BudgetUsage) -> None:
        """整条覆盖。``(persona_id, day)`` 是主键，所以这是一次 UPSERT。"""
        ...


#: 用量按什么分组。三选一，不给第四个：
#:
#: - ``day``     —— 「哪天用得多」，趋势；
#: - ``purpose`` —— 「钱花在哪了」，最常用的那一栏；
#: - ``model``   —— 「换模型到底便宜了多少」，验证分层路由有没有生效。
#:
#: 它同时是**接口约束**与**HTTP 参数**：``channels/web`` 直接把它当查询参数
#: 的类型，于是不认识的取值在 FastAPI 那一层就被拒掉（422），
#: 而不是变成一条拼进 SQL 的字符串。SQL 里能放进 ``GROUP BY`` 的东西
#: 必须是白名单，而 ``Literal`` 就是那个白名单。
UsageGroup = Literal["day", "purpose", "model"]


@dataclass(frozen=True, slots=True)
class UsageTotal:
    """一段窗口里、按一个维度分好的一格用量。

    **它不是 ``llm_usage`` 的一行。** 一行回答「这一次调用花了多少」，
    它回答「这段时间里，这个用途（/模型/日子）一共花了多少」。所以它没有
    ``tier`` / ``latency_ms`` / ``error`` —— 那些是单次调用的属性，
    加起来没有意义（延迟相加得不到「平均延迟」，那是另一个问题）。

    ``total_tokens`` 是算出来的而不是存下来的：库里那一列本来就是
    ``prompt + completion``（见 :attr:`alterego.interfaces.llm.LLMUsage.total_tokens`），
    两处各算一遍迟早会分叉。
    """

    #: 分组值：日期（``2026-09-16``）、用途（``decision``）或模型名。
    #: 模型名可能为空串——路由没定模型时就是空，前端负责把它画成「—」。
    key: str
    #: 这一段里一共调了几次（**含失败的那几次**，账本对每一次尝试都记一条）。
    calls: int = 0
    #: 其中失败的次数。``calls - failed`` 就是成功次数。
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入 + 输出。"""
        return self.prompt_tokens + self.completion_tokens


class UsageRepository(Protocol):
    """``llm_usage`` 的只读聚合。

    写的那一端是 :class:`~alterego.interfaces.llm.UsageSink`（网关只认识那个形状），
    而统计页要的是这个形状。两者分开是因为**方向不同、生命周期也不同**：
    写端一次 tick 造一个，读端是「这一整段窗口里花了多少」。

    **聚合落在 SQL 里，不在这一层。** ``/api/stats`` 那一页现有的做法是把行
    拉回内存再数（仓储没有 ``count()``，见 ``routes/stats.py`` 的模块文档），
    那是因为它数的是几十上百条。``llm_usage`` 是**每次调用一行**：一天跑下来
    就是几百上千行，一个月上万行，而这一页默认看 30 天——把行搬进 Python
    只为求三个和，是这张表唯一会真正长起来的地方。
    """

    def totals(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        group: UsageGroup = "purpose",
    ) -> list[UsageTotal]:
        """窗口内按 ``group`` 分组的用量。

        Args:
            persona_id: 哪个人设。预算与账本都是**按 persona 算**的。
            since: 窗口起点（含）。
            until: 窗口终点（不含）。半开区间，与 ``activity_log`` 的区间查询一致：
                相邻两个窗口接起来不重不漏。
            group: 见 :data:`UsageGroup`。

        Returns:
            分组结果。``day`` 按日期升序（趋势从左到右读），
            ``purpose`` / ``model`` 按用量从大到小（最花钱的排在最上面）。
            同量时按键名排序——**顺序不确定的迭代是 P6 禁止的**，
            而它在这里的症状是「刷新一下表格的行就换了位置」。

        Raises:
            ValueError: ``group`` 不在 :data:`UsageGroup` 里。它不是「没数据」，
                而是调用方写错了——静默回落成按用途分组会让统计页安静地答错。
        """
        ...


@dataclass(frozen=True, slots=True)
class TickLogDraft:
    """``tick_log`` 的一行。

    它是 ``alterego why`` 的唯一数据来源，所以 ``notes`` / ``candidates`` /
    ``suppressed`` 三个字段**不是调试用的废料，是产品的一部分**：
    「它为什么没回我」这个问题必须能不看代码就回答出来。

    各种 ``*_json`` 列在这里是真正的 Python 对象，序列化由存储层负责——
    让调用方自己 ``json.dumps`` 会把「能不能序列化」变成一个运行期惊喜。
    """

    id: str
    persona_id: str
    virtual_time: datetime
    status: str
    created_at: datetime
    real_duration_ms: int = 0
    state_snapshot: Mapping[str, object] = field(default_factory=dict)
    percepts: Mapping[str, object] = field(default_factory=dict)
    candidates: Sequence[Mapping[str, object]] = ()
    chosen_intent: str | None = None
    motivation: str = ""
    trigger_note: str = ""
    suppressed: Sequence[Mapping[str, object]] = ()
    memories: Sequence[str] = ()
    notes: Sequence[str] = ()
    stage_results: Mapping[str, object] = field(default_factory=dict)
    llm_calls: int = 0
    llm_tokens: int = 0
    error: str | None = None


class TickLogRepository(Protocol):
    """推演日志的写入。"""

    def append(self, draft: TickLogDraft) -> None:
        """写一条 tick 记录。

        保留策略（``[retention] tick_log_keep_days``）不在这里做——
        仓储不清扫自己，因为「什么时候可以删」是运维判断，不是数据形状。
        """
        ...

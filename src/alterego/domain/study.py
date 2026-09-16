"""专项学习：学什么、学到哪了、什么时候该翻出来用。

这个模块是 ``alterego study`` 的**纯内核**——不碰文件、不调模型、不看时钟。
三件事全是函数：把一个领域展开成主题表、按一句话从笔记里挑出相关的、推进进度。

**它解决的问题是「角色会的东西从哪来」。** 人设里只有一句 ``occupation``
（「产品经理」），而一个产品经理该懂的东西不是一句话能装下的。于是有
:func:`curriculum`：把一个领域展开成一串**有序、固定**的主题，每学一次消耗一个。
顺序固定所以可复现（P6）——同样的进度跑两次，学的是同一个主题。

**「懂了才会用它」不能靠提示词求。** 靠 :func:`select`：它是从一句话到
「该翻哪几篇笔记」的**唯一**通道，而且它是纯函数，所以「它怎么突然说起这个」
永远答得出来（命中了哪些词、每篇各得几分）。提示词里写「请记得你学过的东西」，
模型高兴就记、不高兴就不记，那不叫功能（P3）。

**为什么中文用二字组而不是分词器。** ``jieba`` 是可选的
（``plugins/tokenizer_jieba``），而这一层的输出必须**在任何环境下逐字节一致**
（P6）：换一版词典、换一台机器没装词典，召回结果就变了。二字组不需要词典，
代价是「神经网络」会被切成「神经 / 经网 / 网络」——对召回来说够用。

但虚词必须单独挡掉（:data:`_STOPWORDS`），因为第 1 轮的题面全都是
``X · 是什么``：漏掉一个 ``什么``，「今晚吃什么」就能把整个第一轮翻出来，
而 :data:`DEFAULT_MIN_SCORE` 拦不住它——那个门槛是一把公用的刀，
会把「标题真的命中一次」一起切掉。**噪声靠词表，不靠调高门槛。**

依据: docs/plans/2026-09-16-specialized-study.md § 2–4
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from alterego.domain.knowledge import Note


__all__ = [
    "ASPECTS",
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_RECALL_LIMIT",
    "MISSING_FIELD_HINT",
    "STATE_FILENAME",
    "Field",
    "Match",
    "Progress",
    "Recall",
    "StudyDraft",
    "Topic",
    "advance",
    "curriculum",
    "field_hint",
    "field_key",
    "next_topics",
    "parse_progress",
    "parse_study_note",
    "render_context",
    "render_note_body",
    "resolve_field",
    "score_note",
    "select",
    "terms",
    "to_payload",
]


# ── 常量 ────────────────────────────────────────────────────

ASPECTS: Final[tuple[str, ...]] = (
    "是什么",
    "怎么做",
    "容易踩的坑",
    "和什么容易混",
    "我还不服的",
)
"""一个领域的五个切入面。**顺序即学习顺序**，改了它等于让进度失去意义。

为什么是这五个：前两个是「能讲清楚」，后三个是「知道边界」。
只会讲定义的人不算懂，而「我还不服的」逼它留一条自己的判断——
这一条是它以后会不会主动提这个话题的关键。
"""

DEFAULT_RECALL_LIMIT: Final[int] = 3
"""一句话最多翻出几篇笔记。多了会盖过当前对话本身。"""

DEFAULT_MIN_SCORE: Final[float] = 2.0
"""低于这个分就不算命中。**门槛必须比 0 高**：否则每次聊天都会塞进一堆
碰巧共用一个字的专业笔记，那比不调用更糟。"""

DEFAULT_CONTEXT_BUDGET: Final[int] = 900
"""拼出来的上下文块最多多少字符。位子是借来的，不是争来的。"""

STATE_FILENAME: Final[str] = ".study-state.json"
"""学习进度记在这儿（放在 ``00-索引/`` 里）。以点开头，Obsidian 不显示。"""

MISSING_FIELD_HINT: Final[str] = (
    "不知道该学什么：[study] field 是空的，也没能从人设的 occupation 认出来。"
)

#: 领域识别表：``(领域名, 关键词, 稳定的键)``。**顺序即优先级**，
#: 所以「数据分析师」会落在「数据与算法」而不是「计算机软件」。
_FIELDS: Final[tuple[tuple[str, tuple[str, ...], str], ...]] = (
    (
        "数据与算法",
        ("算法", "数据科学", "数据分析", "机器学习", "人工智能", "推荐系统"),
        "data",
    ),
    (
        "计算机软件",
        ("程序员", "软件开发", "软件工程", "后端", "前端", "全栈", "架构师", "开发工程师"),
        "software",
    ),
    (
        "产品与设计",
        ("产品经理", "产品设计", "交互设计", "用户体验", "设计师", "ui", "ux"),
        "product",
    ),
    ("运营与增长", ("运营", "市场", "品牌", "增长", "新媒体", "电商"), "growth"),
    ("财务与会计", ("会计", "财务", "审计", "税务", "出纳", "理财"), "finance"),
    ("法律", ("律师", "法务", "法律", "合规"), "law"),
    ("医学与健康", ("医生", "医师", "护士", "临床", "药师", "心理咨询"), "medical"),
    ("教育与科研", ("老师", "教师", "教授", "讲师", "教研", "辅导员"), "teaching"),
    ("建筑与土木", ("建筑师", "土木", "结构工程", "施工", "造价", "监理"), "civil"),
    ("制造与硬件", ("机械", "电子", "硬件", "嵌入式", "电气", "自动化", "工艺"), "hardware"),
    ("传媒与内容", ("记者", "编辑", "文案", "编导", "主播", "媒体"), "media"),
    ("人力资源", ("hr", "人力", "招聘", "人事", "培训"), "hr"),
    ("销售与客户", ("销售", "客户经理", "商务", "客服"), "sales"),
    ("艺术与表演", ("演员", "音乐", "绘画", "舞者", "摄影", "戏剧"), "art"),
)

#: 看得出「在读书」但看不出「读的什么」。这类必须让人去填 ``field``：
#: 专业名只有当事人知道，猜一个出来等于让它学一门我们不认识的学科。
_STUDENT_WORDS: Final[tuple[str, ...]] = (
    "研究生",
    "硕士",
    "博士",
    "本科生",
    "大学生",
    "学生",
    "在读",
    "学员",
    "实习生",
)

_WEIGHT_TITLE: Final[float] = 3.0
_WEIGHT_TAG: Final[float] = 2.0
_WEIGHT_BODY: Final[float] = 1.0

#: 正文规模（不同词个数）到这个量级，阻尼就明显了。
_BODY_SATURATION: Final[float] = 200.0

_EXCERPT_LIMIT: Final[int] = 120

_LATIN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9+#._-]*")
_CJK: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]+")
_KEY_CLEAN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_MIN_LATIN: Final[int] = 2

#: 切词时丢掉的虚词二字组。
#:
#: 没有它，字组切词的噪声会淹掉真实信号：第 1 轮的题面全都是
#: ``X · 是什么``，而 ``是什么`` 的二字组里有 ``什么``——于是
#: 「今晚吃什么」「你说什么」这类句子都会把全部第 1 轮的笔记翻出来，
#: ``min_score`` 那个门槛形同虚设。
#:
#: **丢的是噪声那一侧，不是把门槛调高。** 调高门槛会连
#: 「标题命中一次」（3.0 分）一起杀掉——那是真信号。
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "一个",
        "一下",
        "一点",
        "不过",
        "东西",
        "为了",
        "为什",
        "什么",
        "今天",
        "他们",
        "你们",
        "但是",
        "其实",
        "刚才",
        "别的",
        "到底",
        "因为",
        "如果",
        "就是",
        "已经",
        "我们",
        "所以",
        "所有",
        "有点",
        "反正",
        "只是",
        "可是",
        "可以",
        "哪个",
        "哪些",
        "哪天",
        "怎么",
        "总是",
        "感觉",
        "应该",
        "觉得",
        "不是",
        "不能",
        "不要",
        "还是",
        "那个",
        "那么",
        "那些",
        "那样",
        "这么",
        "这个",
        "这些",
        "这样",
        "这种",
        "的话",
        "时候",
        "明天",
        "昨天",
        "现在",
        "然后",
        "而且",
        "没有",
        "事情",
        "嗯嗯",
        "好吧",
    }
)

_HEADING: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}#")

FieldSource = Literal["config", "occupation"]


# ── 学什么 ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Field:
    """要深耕的方向。

    Attributes:
        key: 稳定的键。进度按它记，所以**改一个字就是换一门学问**。
        label: 显示给人看的名字（进笔记标题、进输出）。
        source: 这个名字是哪儿来的——用户填的，还是从 occupation 认出来的。
        evidence: 认出来的时候凭的是哪句话，给用户看，好让它判断认没认错。
    """

    key: str
    label: str
    source: FieldSource
    evidence: str = ""


def field_key(label: str) -> str:
    """领域名 → 稳定的键。

    中文原样留着：进度文件是 JSON，而「computer-vision」和「计算机视觉」
    对用户来说是同一个东西的两种写法，把中文转成拼音只会让它认不出来。
    """
    text = unicodedata.normalize("NFC", label).strip().lower()
    return _KEY_CLEAN.sub("-", text).strip("-") or "unnamed"


def resolve_field(configured: str, *, occupation: str = "") -> Field | None:
    """定下学什么。

    配置优先——用户说了算；其次按 ``occupation`` 认。

    **认不出来就返回 None，不编一个领域出来。** 让角色去深耕一门我们瞎猜的
    专业，比让它明确地说「我不知道我该学什么」糟得多：前者会写出一堆
    像模像样的空话，而用户根本不会想到要去检查。

    Args:
        configured: ``[study] field`` 或 ``--field``。
        occupation: 人设里的职业，可能是一句自由发挥的话。

    Returns:
        认出来了的领域；认不出来返回 `None`。
    """
    label = configured.strip()
    if label:
        return Field(key=field_key(label), label=label, source="config")

    text = occupation.strip()
    if not text:
        return None
    lowered = text.lower()
    for name, keywords, key in _FIELDS:
        if any(word in lowered for word in keywords):
            return Field(key=key, label=name, source="occupation", evidence=text)
    return None


def field_hint(occupation: str = "") -> str:
    """认不出来时给人看的那句话。

    要说清**去哪儿填**而不只是「不知道」——否则用户看到的是一句
    「它没在学习」，然后不知道下一步该干什么。
    """
    text = occupation.strip()
    if not text:
        return f"{MISSING_FIELD_HINT}人设里连 occupation 都没有。"
    if any(word in text for word in _STUDENT_WORDS):
        return (
            f"occupation 是「{text}」——看得出是在读书，但**读的是什么**库里没有。"
            '填 `[study] field = "你在学的那个专业"`，它才知道该学什么。'
        )
    return (
        f"occupation 是「{text}」，对不上任何内置领域。"
        '填 `[study] field = "你要它学的方向"` 就行，不必是标准职业名。'
    )


# ── 学什么，按顺序 ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Topic:
    """一次学习的题目。"""

    key: str
    title: str
    aspect: str
    round: int


def curriculum(field: Field, *, rounds: int) -> tuple[Topic, ...]:
    """把领域展开成 ``rounds`` 轮的主题表。

    第 1 轮把 :data:`ASPECTS` 这五格填满，第 2 轮再来一遍，题面带上轮次。
    为什么不让模型自己决定今天学什么：那样「学了多久」就没有刻度，
    也没法回答「上次学到哪」，而那正是这个功能唯一能证明自己的东西。

    Args:
        field: 方向。
        rounds: 展开几轮。小于 1 返回空表。

    Returns:
        顺序固定的主题表。``rounds`` 大时表也大，调用方自己截。
    """
    if rounds <= 0:
        return ()
    out: list[Topic] = []
    for index in range(rounds):
        number = index + 1
        suffix = f"（第 {number} 轮）" if number > 1 else ""
        out.extend(
            Topic(
                key=f"{field.key}/{number}/{aspect}",
                title=f"{field.label} · {aspect}{suffix}",
                aspect=aspect,
                round=number,
            )
            for aspect in ASPECTS
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Progress:
    """学到哪了。

    Attributes:
        field_key: 这份进度属于哪个方向。和当前的 :class:`Field` 对不上时，
            进度等于空——**这是故意的**：两个专业的主题混在一个进度里，
            「学到第几个了」这句话就没意义了。
        done: 学过的主题键。
        last_at: 上一次学习的时间（ISO 字符串）。这里不解析它：
            `domain/` 不做时间运算，格式化是调用方的事。
    """

    field_key: str = ""
    done: tuple[str, ...] = ()
    last_at: str = ""

    @property
    def rounds_done(self) -> int:
        """学满了几轮（非整除的余数不算一轮）。"""
        return len(self.done) // len(ASPECTS)


def parse_progress(payload: Mapping[str, Any], *, field: Field) -> Progress:
    """把状态文件读回对象。

    坏数据**不报错**：进度只是个缓存，最坏的结果是重学一遍一个主题，
    而那是「最坏」，不是「灾难」。为了它让 ``study status`` 跑不起来，
    正好发生在这个文件被改坏的时侯——那是最需要看到输出的时候。

    属于别的方向的进度按空处理（见 :class:`Progress`)。
    """
    stored = payload.get("field_key")
    if not isinstance(stored, str) or stored != field.key:
        return Progress(field_key=field.key)
    stamped = payload.get("last_at")
    return Progress(
        field_key=field.key,
        done=_text_tuple(payload.get("done")),
        last_at=stamped if isinstance(stamped, str) else "",
    )


def next_topics(field: Field, *, progress: Progress, rounds: int) -> tuple[Topic, ...]:
    """接下来该学的 ``rounds`` 个主题。

    表要**展开得足够远**再筛：进度走到第 3 轮时，只展开 1 轮的表里
    一个都没剩，直接返回空表——而那时候真正该学的是第 4 轮。
    """
    if rounds <= 0:
        return ()
    need = progress.rounds_done + rounds + 1
    pending = [topic for topic in curriculum(field, rounds=need) if topic.key not in progress.done]
    return tuple(pending[:rounds])


def advance(
    progress: Progress,
    *,
    field: Field,
    topics: Sequence[Topic],
    at: str = "",
) -> Progress:
    """把这一轮学过的主题记进进度。**幂等**：同一个主题记两次还是那一条。"""
    if progress.field_key != field.key:
        progress = Progress(field_key=field.key)
    seen: dict[str, None] = {}
    for key in progress.done:
        seen.setdefault(key, None)
    for topic in topics:
        seen.setdefault(topic.key, None)
    return Progress(field_key=field.key, done=tuple(seen), last_at=at or progress.last_at)


def to_payload(progress: Progress) -> dict[str, Any]:
    """进度 → 可落盘的字典。键按字母序写出去（见 `sim/study.py`）。"""
    return {
        "done": list(progress.done),
        "field_key": progress.field_key,
        "last_at": progress.last_at,
    }


# ── 学到了什么 ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StudyDraft:
    """模型交回来的一篇学习笔记。"""

    summary: str
    points: tuple[str, ...] = ()
    unsure: tuple[str, ...] = ()


def parse_study_note(text: str) -> StudyDraft | None:
    """从模型的回复里抠出一篇笔记。

    只接受**结构完整**的：一句话总结加至少一条要点。凑不齐就返回 `None`，
    让上层记一条失败。为什么不退回「把整段回复当正文」：
    那样会写下一篇只有标题和一堆散文的笔记，而笔记标题上写着
    「第 2 轮 · 怎么做」——**它会变成一份它其实没有的学历**。

    取的是**第一个** ``{`` 到**最后一个** ``}``：中间夹了第二段话就整段
    读不出来，那时宁可不成。因为切片必然以 ``{`` 开头、以 ``}`` 结尾，
    能读出来的只能是个对象——所以不需要再判一次类型。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    points = _text_tuple(payload.get("points"))
    if not points:
        return None
    return StudyDraft(
        summary=summary.strip(),
        points=points,
        unsure=_text_tuple(payload.get("unsure")),
    )


def render_note_body(draft: StudyDraft, *, field: Field, topic: Topic) -> str:
    """笔记正文。

    骨架是**机制**不是格式：每一篇都要有「我还不确定的」这一节。
    靠提示词写「请注明你不确定的地方」，它就不写——而一篇什么都确定的
    学习笔记，只能说明它没真的学过。
    """
    lines = ["## 一句话", "", draft.summary, "", "## 记下来的", ""]
    lines.extend(f"- {point}" for point in draft.points)
    if draft.unsure:
        lines.extend(["", "## 我还不确定的", ""])
        lines.extend(f"- {item}" for item in draft.unsure)
    lines.extend(
        [
            "",
            "## 出处",
            "",
            f"- 领域：{field.label}",
            f"- 这一轮：第 {topic.round} 轮 · {topic.aspect}",
            "- 这是我自己学的，不是谁教我的。",
        ]
    )
    return "\n".join(lines)


# ── 什么时候该翻出来用 ──────────────────────────────────────


def terms(text: str) -> tuple[str, ...]:
    """切词：拉丁词原样，中文取**二字组**，按首次出现的顺序去重。

    二字组而不是分词器：``jieba`` 是可选的插件，而这一层的输出必须在
    任何环境下逐字节一致（P6）。粗一点没关系，命中数会补上来。

    虚词（见 :data:`_STOPWORDS`）不进结果——它们是噪声，不是信号。

    >>> terms("卷积神经网络 CNN")
    ('cnn', '卷积', '积神', '神经', '经网', '网络')
    """
    lowered = text.lower()
    found: list[str] = []
    for raw in _LATIN.findall(lowered):
        word = raw.strip("._-")
        if len(word) >= _MIN_LATIN:
            found.append(word)
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            found.append(run)
        else:
            found.extend(run[index : index + 2] for index in range(len(run) - 1))
    seen: dict[str, None] = {}
    for term in found:
        if term and term not in _STOPWORDS:
            seen.setdefault(term, None)
    return tuple(seen)


def _excerpt(body: str, *, limit: int = _EXCERPT_LIMIT) -> str:
    """正文里第一句像话的话，压到 ``limit`` 个字符。

    跳过标题行与标点：**摘要进上下文，全文不进**。把整篇笔记塞进提示词，
    等于用一篇自己写的文章把当前对话挤出去。
    """
    for raw in body.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line or _HEADING.match(raw):
            continue
        return line if len(line) <= limit else f"{line[:limit]}…"
    return ""


def score_note(note: Note, *, query_terms: Sequence[str]) -> tuple[float, tuple[str, ...]]:
    """一句话 × 一篇笔记 → 分数与命中的词。

    标题里的词权重最高，其次是标签，最后才是正文。再加一道**阻尼**：
    正文规模越大，同样的命中越不值钱——长文里的偶发同词不该赢过
    短文里的主题词，否则「记得最多」的那篇会永远占据全部位置。

    Returns:
        ``(分数, 命中的词)``。命中的词按 ``query_terms`` 的顺序，
        所以同样的输入永远得到同样的输出（P6）。
    """
    title = set(terms(note.title))
    tags = set(terms(" ".join(note.tags)))
    body = set(terms(note.body))
    matched = title | tags | body

    hit = tuple(term for term in query_terms if term in matched)
    if not hit:
        return 0.0, ()

    score = 0.0
    for term in hit:
        if term in title:
            score += _WEIGHT_TITLE
        elif term in tags:
            score += _WEIGHT_TAG
        else:
            score += _WEIGHT_BODY
    damp = 1.0 + math.log10(1.0 + len(body) / _BODY_SATURATION)
    return score / damp, hit


@dataclass(frozen=True, slots=True)
class Match:
    """一篇被翻出来的笔记。"""

    path: str
    title: str
    score: float
    terms: tuple[str, ...]
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class Recall:
    """一次召回的结果。"""

    query: str
    matches: tuple[Match, ...] = ()
    considered: int = 0
    threshold: float = DEFAULT_MIN_SCORE

    @property
    def hit(self) -> bool:
        """有没有翻出东西。空召回是**正常状态**，不是错误。"""
        return bool(self.matches)


def select(
    notes: Iterable[Note],
    *,
    query: str,
    limit: int = DEFAULT_RECALL_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Recall:
    """一句话 → 该翻出来的那几篇笔记。

    这是「讨论到专业话题就自动用上」的**唯一**入口。它只在调用方递进来的
    笔记里挑，所以想让它别乱翻，改调用方递什么就行——不必去改一段
    「请只在你真的懂的时候引用」的提示词。

    排序按 ``(-分数, 路径)``：分数一样时按路径，所以**结果与输入顺序无关**
    （P6）。否则文件扫描顺序一变，同一句话翻出来的笔记就换了。
    """
    if limit <= 0:
        return Recall(query=query, considered=len(list(notes)), threshold=min_score)

    query_terms = terms(query)
    considered = 0
    scored: list[Match] = []
    if query_terms:
        for note in notes:
            considered += 1
            score, hit = score_note(note, query_terms=query_terms)
            if score >= min_score:
                scored.append(
                    Match(
                        path=note.path,
                        title=note.title,
                        score=score,
                        terms=hit,
                        excerpt=_excerpt(note.body),
                    )
                )
    scored.sort(key=lambda item: (-item.score, item.path))
    return Recall(
        query=query,
        matches=tuple(scored[:limit]),
        considered=considered,
        threshold=min_score,
    )


def render_context(recall: Recall, *, budget: int = DEFAULT_CONTEXT_BUDGET) -> str:
    """召回结果 → 一段能直接拼进提示词的文本。

    末尾那句不是修辞。不写它，模型会把这些句子当成**对方刚说的话**接着答，
    于是变成它在复述用户的观点——比不调用更糟，因为看起来像懂了。

    超出 ``budget`` 就整篇丢掉、不截断：半篇笔记会变成一句像结论的错话。
    """
    if not recall.matches:
        return ""
    lines = ["【我学过、可能和这次有关的】", ""]
    used = 0
    for match in recall.matches:
        block = [f"- {match.title}（{match.path}）"]
        if match.excerpt:
            block.append(f"  {match.excerpt}")
        text = "\n".join(block)
        if used + len(text) > budget:
            break
        lines.extend(block)
        used += len(text)
    lines.extend(["", "（以上是我自己在知识库里记过的，不是这次对话里对方说的话。）"])
    return "\n".join(lines)


def _text_tuple(value: Any) -> tuple[str, ...]:
    """任意值 → 一串非空字符串。缺字段、给了个字符串、给了个数字，都不炸。"""
    if isinstance(value, str):
        items: Sequence[Any] = [value]
    elif isinstance(value, Sequence):
        items = value
    else:
        return ()
    seen: dict[str, None] = {}
    for item in items:
        text = item.strip() if isinstance(item, str) else ""
        if text:
            seen.setdefault(text, None)
    return tuple(seen)

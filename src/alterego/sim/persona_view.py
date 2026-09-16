"""人设的运行期视图：写提示词时真正会读到的那几个字段。

**为什么不直接用 :class:`~alterego.interfaces.repository.PersonaRecord`。**
那张表的一行只有「我是谁」（名字、年龄、城市、职业），而 ``chat_reply`` /
``reach_out`` / ``post_compose`` 三份模板要的是「我说话什么调子」——
语气、口头禅、常用表情、啰嗦程度。它们住在 ``persona_json`` 里，那是一份
由 ``prompts/persona_generate.md`` 生成、形状还没有定论的文档。

于是这里做两件事：

1. **把两个来源拼成一个对象**（表里的固定几列 + 文档里的自由字段），
   这样 ``state.persona`` 就只有一个类型，读的人不必再问「这个字段在哪」。
2. **宽容取键**。形状没定论意味着它会变，而一个读不到语气的人是能说话的
   （只是没个性），一个因为少了 ``tone`` 键就抛异常的人是彻底说不话的。
   所以每个字段都有兜底，兜底值写在下面每一处的理由里。

**视图是只读的。** 它是快照的一部分（``StateSnapshot`` 是 frozen 的），
一次 tick 里的人设不应该在 tick 中间被别人改掉。

依据: docs/design/03-data-model.md § 3、docs/design/04-simulation-loop.md § 7
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alterego.interfaces.repository import PersonaRecord


__all__ = ["EXPRESSION_KEYS", "SUMMARY_KEYS", "PersonaView"]


#: ``persona_json`` 里可能装「一句话自我介绍」的键，按可信度排序。
#:
#: 这是一份**猜测清单**，不是 schema。写成列表而不是取某一个键，是因为
#: 提示词每改一次这份文档就可能换一个键名，而换键名不该让所有人生成的文本
#: 里那句话突然消失。找不到任何一个时才退回下面那句由表字段拼出来的话。
SUMMARY_KEYS: tuple[str, ...] = ("summary", "description", "self_intro", "bio", "intro")

#: ``persona_json`` 里"表达风格"那一段可能用的键。
EXPRESSION_KEYS: tuple[str, ...] = ("expression", "expressions", "style", "voice")


@dataclass(frozen=True, slots=True)
class PersonaView:
    """写提示词时能看到的那个人。

    ``document`` 留着整份原始文档**不是偷懒**：``alterego why`` 与
    ``--show-prompt`` 要能回答「这句话是根据什么生成的」，而那个问题的答案
    在文档里那些还没被命名成字段的部分。字段是给代码用的，文档是给人看的。
    """

    id: str
    name: str
    user_name: str = ""
    age: int | None = None
    gender: str = ""
    city: str = ""
    occupation: str = ""
    #: 一句话自我介绍。空字符串表示「文档里没写」，调用方自己决定怎么兜底。
    summary: str = ""
    #: 表达风格。键名由 ``persona_json`` 决定，见
    #: :meth:`~alterego.sim.stages.express.ExpressionStyle.from_persona`。
    expression: Mapping[str, Any] = field(default_factory=dict)
    document: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        record: PersonaRecord,
        *,
        user_name: str = "",
        document: Mapping[str, Any] | None = None,
    ) -> PersonaView:
        """把「表里那几列」与「文档里那些自由字段」拼成一份视图。

        ``document`` 为 ``None`` 或空字典是**正常情况**：人格还没生成过文档
        （``alterego persona`` 没跑过），这时它仍然应该能说话，只是没写进
        文档的那部分个性全是兜底值。
        """
        doc: Mapping[str, Any] = document or {}
        return cls(
            id=record.id,
            name=record.name,
            user_name=user_name,
            age=record.age,
            gender=record.gender,
            city=record.city,
            occupation=record.occupation,
            summary=_first_text(doc, SUMMARY_KEYS) or cls._fallback_summary(record),
            expression=_first_mapping(doc, EXPRESSION_KEYS),
            document=doc,
        )

    def text(self) -> str:
        """给提示词用的一段自我介绍。

        **没有性格设定也返回一句话**，而不是空串：模板里 ``{persona}`` 那一行
        空掉，模型会自己编一个人格出来，而编出来的那个每一轮都不一样——
        那比「一个只有名字和职业的人」糟糕得多，因为它不可解释。
        """
        parts = [self.summary]
        if self.age is not None:
            parts.append(f"{self.age} 岁")
        if self.gender:
            parts.append(self.gender)
        if self.city:
            parts.append(f"在{self.city}")
        if self.occupation:
            parts.append(f"职业是{self.occupation}")
        return "，".join(part for part in parts if part)

    @staticmethod
    def _fallback_summary(record: PersonaRecord) -> str:
        """文档里没写自我介绍时，用表里那几列拼一句。

        拼出来的这句**只有事实、没有性格**，那是故意的：事实来自数据库，
        性格来自文档。用一句编出来的性格填空，会让「它的语气变了」这件事
        永远查不出原因。
        """
        detail = [part for part in (record.occupation, record.city) if part]
        if not detail:
            return f"我是{record.name}。"
        return f"我是{record.name}，{'，'.join(detail)}。"


def _first_text(document: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序找第一个非空字符串。找不到返回空串。

    只认 ``str``：文档里 ``summary`` 有可能是一段列表或一个对象（生成时模型
    自作主张），而把 ``['a','b']`` 字符串化塞进提示词会给它一段带引号的垃圾。
    """
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_mapping(document: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """按顺序找第一个字典。找不到返回空字典。"""
    for key in keys:
        value = document.get(key)
        if isinstance(value, Mapping) and value:
            return {str(k): v for k, v in value.items()}
    return {}

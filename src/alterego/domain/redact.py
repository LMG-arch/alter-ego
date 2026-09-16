"""训练数据的脱敏：把**真实身份**从文本里摘出去。

## 它和仓库里已有的「脱敏」不是一回事

``kernel/logging.py`` 的 ``SecretFilter`` 与 ``kernel/config.py`` 的 ``to_dict(redact=True)``
管的是**密钥**——防的是「API key 被写进日志、被贴进 issue」。
本模块管的是**隐私**——防的是「用户自己的身份被烘进一个要上传去微调的数据集」。

两套规则的服务对象与失效代价都不同，所以它们不共用开关也不共用规则表。
**唯一共享的是动机**：有些东西不该离开这台机器。

## 三条不变量

1. **纯函数、无 IO、无配置读取。** ``domain/`` 的红线本来就禁止这些；
   这里额外要的是「给同一段文本就还同一段文本」，与运行环境无关（P6）。
2. **规则表是模块级常量，顺序固定。** 顺序不是风格问题：``url_credentials``
   必须早于 ``email``（``https://a@b.com:pw@host`` 里嵌着一个邮箱），
   ``id_card_cn`` 必须早于 ``phone_cn``（18 位里含 11 位数字子串）。
3. **占位符不会被任何规则二次命中。** 做法是所有规则先把命中处换成含 NUL 的哨兵、
   跑完统一还原（见 :func:`redact_with_report`）。于是 ``redact`` 对**内置规则表**
   是幂等的——对输出再跑一遍结果不变。这条有测试钉住。

## 为什么虚构角色的名字**不**脱

这一点和直觉相反，所以说明白：数据集的目标是「训出一个会以这个名字自称、
会称呼你的模型」。把所有人名统一换成 ``[人名]`` 会得到一个**没有称呼能力**的模型。

脱敏要摘掉的是**可识别的真实身份**，不是专有名词。所以：

===========================  ========  ==================================================
内容                          脱不脱    为什么
===========================  ========  ==================================================
``core.user_name``            脱        用户可能填了真名
角色名、NPC 名                不脱      虚构的，是训练目标本身
邮箱 / 手机 / 证件号          脱        真实身份
API key / token / 密码        脱        与 ``SecretFilter`` 同源
绝对路径                      脱        本项目的真实情况：路径里带用户名
内网 IP / 带凭据的 URL        脱        真实基础设施
===========================  ========  ==================================================

依据: ``docs/adr/0011-training-datasets-are-derived-and-redacted.md``
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from re import Pattern
from typing import Final


__all__ = [
    "PRONOUNS",
    "REDACTION_RULES",
    "RedactionReport",
    "RedactionRule",
    "apply_rules",
    "build_rules",
    "digest",
    "redact",
    "redact_with_report",
]


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """一条脱敏规则。

    Attributes:
        name: 规则标识，进 ``manifest.json`` 与报告，方便回答「哪些规则命中过」。
        label: 命中的内容被替换成什么。占位符就是 ``[label]``。
        pattern: 匹配模式。
        replacement: 替换文本。默认占位符是 ``[label]``；
            个别规则要保留结构（凭据规则留协议头），所以单独指定。
    """

    name: str
    label: str
    pattern: Pattern[str]
    replacement: str = ""


#: 这些词**不能**当身份去替换。
#:
#: ``core.user_name`` 的默认值是「你」。若不加这一步，规则会把每一个「你」
#: 换成 ``[我]``——整份数据集当场报废，而且是**静默**报废（文件照样生成、
#: 条数照样对，只是每一句话都读不通）。同类词一并列出。
PRONOUNS: Final[frozenset[str]] = frozenset(
    {
        "你",
        "您",
        "我",
        "他",
        "她",
        "它",
        "咱",
        "俺",
        "you",
        "me",
        "i",
        "user",
        "assistant",
        "system",
        "用户",
        "助手",
        "角色",
    }
)

#: 内置规则表。**顺序即应用顺序**，见模块文档第三条不变量。
REDACTION_RULES: Final[tuple[RedactionRule, ...]] = (
    RedactionRule(
        name="url_credentials",
        label="凭据",
        # 保留协议头：`[凭据]@host` 比一整坨 `[凭据]` 更好读，也不泄露更多。
        pattern=re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@"),
        replacement="[凭据]@",
    ),
    RedactionRule(
        name="api_key",
        label="密钥",
        pattern=re.compile(
            r"(?:"
            r"\bsk-[A-Za-z0-9_\-]{16,}"
            r"|\bgh[a-z]_[A-Za-z0-9]{20,}"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
            r"|(?i:\bbearer)\s+[A-Za-z0-9._\-]{12,}"
            r"|(?i:\b(?:api[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret"
            r"|secret|password|passwd|pwd)\b)\s*[:=]\s*[\"']?[^\s\"',;]{6,}"
            r")"
        ),
    ),
    RedactionRule(
        name="email",
        label="邮箱",
        pattern=re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b"
        ),
    ),
    RedactionRule(
        name="id_card_cn",
        label="证件号",
        pattern=re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ),
    RedactionRule(
        name="phone_cn",
        label="手机号",
        pattern=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    RedactionRule(
        name="ipv4",
        label="IP",
        pattern=re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    ),
    RedactionRule(
        name="win_path",
        label="路径",
        pattern=re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n\s,;)]+\\?)+"),
    ),
    RedactionRule(
        name="posix_home",
        label="路径",
        # 只认确实带用户名的几个前缀。`/var/log` 这类不该脱——
        # 它是系统路径，不指向任何具体的人。
        pattern=re.compile(r"/(?:home|Users|root)/[^\s\"',;:)\]<>]+"),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionReport:
    """脱敏结果与命中统计。

    ``hits`` 只记**命中过的**规则，按规则表顺序。用途是让
    ``alterego dataset build`` 能报一句「脱敏 37 处（邮箱 2、路径 35）」——
    用户据此能看出规则是不是真的在跑，以及自己是不是该补 ``redact_terms``。
    """

    text: str
    hits: tuple[tuple[str, int], ...] = ()

    @property
    def total(self) -> int:
        """总共替换了多少处。"""
        return sum(count for _, count in self.hits)


#: 一个字面量至少要这么长才值得当身份去替换。
#:
#: 单字太宽：`user_name = "高"` 会把每一个「高」都换掉。
#: 两个汉字的名字（张三）是常见情况，所以下限取 2。
_MIN_NAME_LENGTH: Final[int] = 2


def _normalize_name(raw: str) -> str:
    """把一个候选字面量收拾干净；不值得脱就返回空串。"""
    name = raw.strip()
    if len(name) < _MIN_NAME_LENGTH:
        return ""
    if name.casefold() in PRONOUNS:
        return ""
    return name


def _literal_rule(name: str, label: str, term: str) -> RedactionRule:
    """把一个**字面量**包成规则。

    刻意不做成正则：用户填进来的东西不该被当成模式解释
    （``红中`` 里的 ``中`` 是字面量，不是量词；写错一个 ``[`` 就是一个
    能卡住整个命令的正则）。``re.escape`` 把这件事一次性解决。
    """
    return RedactionRule(name=name, label=label, pattern=re.compile(re.escape(term)))


def _build_rules(
    user_name: str,
    extra_terms: Sequence[str],
) -> tuple[RedactionRule, ...]:
    """内置规则表 + 这次运行才有的字面量规则。

    字面量规则排在**最后**：先让具体形状（邮箱、证件号）先摘走，
    剩下的裸词再替换。反过来做的话，``user_name = "13800138000"``
    会先被当成名字换掉，规则表里那条手机号就再也命中不了。
    """
    rules = list(REDACTION_RULES)
    name = _normalize_name(user_name)
    if name:
        rules.append(_literal_rule("user_name", "我", name))
    seen: set[str] = set()
    for raw in extra_terms:
        term = raw.strip()
        if not term or term in seen:
            continue
        seen.add(term)
        rules.append(_literal_rule(f"extra:{term}", "自定义", term))
    return tuple(rules)


def build_rules(
    *,
    user_name: str = "",
    extra_terms: Sequence[str] = (),
) -> tuple[RedactionRule, ...]:
    """把这次要用的规则表建出来。

    拆出来是为了 **批量脱敏**：一次导出要过几万个字段，而每条字段
    重新组装一遍规则表（含每一条字面量的 ``re.escape`` 与 ``compile``）
    是纯粹浪费。先建一次、反复用，见 :func:`apply_rules`。
    """
    return _build_rules(user_name, extra_terms)


def apply_rules(text: str, rules: Sequence[RedactionRule]) -> RedactionReport:
    """用一张**已经建好**的规则表脱敏。

    规则按给定顺序依次应用，每次命中先换成含 NUL 的哨兵，
    最后统一还原成占位符。

    哨兵这一步不是防手滑。不这么做的话，后一条规则能命中前一条刚插进来的
    占位符：用户填 ``redact_terms = ["自定义"]``，某个规则先插进 ``[自定义]``，
    这条字面量规则就把它再吃一遍，输出变成 ``[[自定义]]``。
    哨兵含 NUL，内置规则的模式全都需要一个字母或数字才可能起步，命中不了。
    """
    if not text:
        return RedactionReport(text=text)

    current = text
    hits: list[tuple[str, int]] = []
    slots: dict[str, str] = {}
    for index, rule in enumerate(rules):
        token = f"\x00{index}\x00"
        slots[token] = rule.replacement or f"[{rule.label}]"
        current, count = rule.pattern.subn(token, current)
        if count:
            hits.append((rule.name, count))
    for token, placeholder in slots.items():
        current = current.replace(token, placeholder)
    return RedactionReport(text=current, hits=tuple(hits))


def redact_with_report(
    text: str,
    *,
    user_name: str = "",
    extra_terms: Sequence[str] = (),
) -> RedactionReport:
    """脱敏，并报告每条规则各命中多少次。

    Args:
        text: 原文。
        user_name: 用户显示名（``config.core.user_name``）。空格串、
            单个字、以及常见人称代词会被忽略——见 :data:`PRONOUNS`。
        extra_terms: 用户额外要摘掉的字面量（``[dataset] redact_terms``）。
            按字面量处理，不是正则。

    Returns:
        处理后的文本与命中统计。

    一次只处理一段文本时用这个。一次处理很多段时先调 :func:`build_rules`
    再用 :func:`apply_rules`，别在循环里重复组装规则表。
    """
    return apply_rules(text, _build_rules(user_name, extra_terms))


def redact(
    text: str,
    *,
    user_name: str = "",
    extra_terms: Sequence[str] = (),
) -> str:
    """脱敏，只要结果。绝大多数调用方要的是这个。"""
    return redact_with_report(text, user_name=user_name, extra_terms=extra_terms).text


def digest(*, extra_terms: Sequence[str] = ()) -> str:
    """规则表的指纹（sha256 前 12 位十六进制）。

    写进 ``manifest.json``。用途只有一个：磁盘上的数据集是用哪一版规则脱的。
    ``alterego dataset list`` 拿它和当前代码比，不一致就提示重跑——
    这是「脱敏必须可回溯重做」那条决策的**兑现机制**（ADR-0011）。

    刻意**不含** ``user_name``：它是内容，不是规则；换个显示名不该让
    几万个已导出的样本集体标记为过期。
    """
    parts = [
        f"{rule.name}\x1f{rule.pattern.pattern}\x1f{rule.replacement}" for rule in REDACTION_RULES
    ]
    terms = sorted({term.strip() for term in extra_terms if term.strip()})
    parts.extend(f"extra\x1f{term}\x1f[自定义]" for term in terms)
    payload = "\x1e".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]

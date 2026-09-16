"""``domain/redact.py`` 的规矩：**原值不许出现在输出里**。

这个文件里最重要的一个测试是 ``test_no_rule_leaks_the_original_value``。
它不是「检查函数返回值等于某个字符串」那种测试——它断言的是
**敏感原值不出现在输出文本里**。区别在于：前者会在有人把占位符
从 ``[手机号]`` 改成 ``[phone]`` 时无辜地失败，而后者只在真正泄漏时失败。

脱敏是安全边界，所以判定标准必须是「东西没了」，不是「东西变成了我以为的样子」。
"""

from __future__ import annotations

import inspect

import pytest

from alterego.domain.redact import (
    PRONOUNS,
    REDACTION_RULES,
    digest,
    redact,
    redact_with_report,
)


# ────────────────────────────────────────────────────────────
# 逐条规则：命中形状 → 原值消失
# ────────────────────────────────────────────────────────────
#
# 每条 = (说明, 原文, 必须消失的子串)
# 用「必须消失的子串」而不是占位符，理由见模块文档。
_LEAK_CASES: list[tuple[str, str, str]] = [
    (
        "带凭据的 URL",
        "数据库在 postgres://admin:hunter2@10.0.0.8:5432/main",
        "hunter2",
    ),
    (
        "API key 前缀",
        "export KEY=sk-abcdefghijklmnopqrstuvwxyz012345",
        "sk-abcdefghijklmnopqrstuvwxyz012345",
    ),
    (
        "GitHub token",
        "token 是 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    ),
    ("AWS access key", "AKIAIOSFODNN7EXAMPLE 是访问密钥", "AKIAIOSFODNN7EXAMPLE"),
    (
        "Bearer 头",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6",
    ),
    (
        "key=value 形态",
        "api_key = 8f3a2b1c9d4e5f60718293a4b5c6d7e8",
        "8f3a2b1c9d4e5f60718293a4b5c6d7e8",
    ),
    ("邮箱", "发到 zhang.san+work@example.co.uk 就行", "zhang.san+work@example.co.uk"),
    ("手机号", "我的号码是 13800138000，微信同号", "13800138000"),
    ("18 位证件号", "身份证 110101199003078888 已核验", "110101199003078888"),
    ("内网 IP", "服务器 192.168.31.107 上的服务挂了", "192.168.31.107"),
    ("Windows 绝对路径", r"日志在 C:\Users\lmg\AppData\Local\alterego\app.log", r"C:\Users\lmg"),
    ("POSIX 家目录", "配置放在 /home/lmg/.alterego/alterego.toml", "/home/lmg"),
    ("用户显示名", "林墨今天说她想去看海", "林墨"),
]


@pytest.mark.parametrize(("label", "raw", "secret"), _LEAK_CASES, ids=[c[0] for c in _LEAK_CASES])
def test_no_rule_leaks_the_original_value(label: str, raw: str, secret: str) -> None:
    """每一条规则：原值不许出现在输出里。"""
    out = redact(raw, user_name="林墨")
    assert secret not in out, f"{label} 没脱干净：{out!r}"


@pytest.mark.parametrize(("label", "raw", "secret"), _LEAK_CASES, ids=[c[0] for c in _LEAK_CASES])
def test_every_leak_case_actually_had_something_to_redact(
    label: str, raw: str, secret: str
) -> None:
    """反向检查：上面的用例本身是有效的。

    没有这一条的话，一条写错了的用例（原文里根本没有敏感串）会永远绿，
    而 ``test_no_rule_leaks_the_original_value`` 也就跟着成了摆设。
    """
    assert secret in raw, f"{label} 的用例原文里没有 {secret!r}，这条测试是空的"


def test_rule_names_are_unique_and_cover_every_placeholder() -> None:
    """规则名唯一，且每条都有名字——报告靠名字说话。"""
    names = [rule.name for rule in REDACTION_RULES]
    assert len(names) == len(set(names))
    assert all(name.strip() for name in names)
    assert all(rule.label.strip() for rule in REDACTION_RULES)


def test_user_name_does_not_touch_other_people() -> None:
    """只脱用户自己的名字，NPC 与角色名留着——脱光了就没称呼能力了。"""
    out = redact("林墨问苏晚：你今晚有空吗", user_name="林墨")
    assert "苏晚" in out
    assert "林墨" not in out


# ────────────────────────────────────────────────────────────
# 顺序
# ────────────────────────────────────────────────────────────
def test_credentials_win_over_email_inside_the_same_url() -> None:
    """``https://a@b.com:pw@host`` 里嵌着一个邮箱，凭据规则要先动手。"""
    out = redact("https://ops@example.com:s3cret@git.internal/repo")
    assert "s3cret" not in out
    assert "ops@example.com" not in out


def test_id_card_wins_over_phone_inside_the_same_digits() -> None:
    """18 位证件号里含 11 位数字子串，证件号规则要先动手。"""
    out = redact("110101199003078888")
    assert out == "[证件号]"


def test_literal_name_loses_to_structured_patterns() -> None:
    """用户把手机号填成显示名时，结构化规则仍要优先。"""
    out = redact("打 13800138000 找我", user_name="13800138000", extra_terms=["13800138000"])
    assert out == "打 [手机号] 找我"


# ────────────────────────────────────────────────────────────
# 幂等
# ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("label", "raw", "secret"), _LEAK_CASES, ids=[c[0] for c in _LEAK_CASES])
def test_redact_is_idempotent(label: str, raw: str, secret: str) -> None:
    """再跑一遍结果不变——占位符不会被任何规则二次命中。

    这条不成立的话，``build`` 重跑两次会得到不同的文件，
    而 ``manifest.json`` 里的 sha256 也就失去意义了。
    """
    once = redact(raw, user_name="林墨")
    assert redact(once, user_name="林墨") == once


# ────────────────────────────────────────────────────────────
# 人称代词与短名：不脱
# ────────────────────────────────────────────────────────────
def test_default_user_name_is_not_redacted() -> None:
    """``core.user_name`` 默认是「你」。把它当身份会毁掉整份数据集。"""
    out = redact("你说你想要去看海，我记住了", user_name="你")
    assert out == "你说你想要去看海，我记住了"


@pytest.mark.parametrize("pronoun", sorted(PRONOUNS))
def test_every_pronoun_is_protected(pronoun: str) -> None:
    """代词表里的每一个都不许被替换。"""
    out = redact(f"今天{pronoun}在家", user_name=pronoun)
    assert out == f"今天{pronoun}在家"


@pytest.mark.parametrize("raw", ["", "  ", "高", "a"])
def test_short_or_blank_names_are_ignored(raw: str) -> None:
    """单字太宽，不值得当身份去替换。"""
    assert redact("高山流水", user_name=raw) == "高山流水"


# ────────────────────────────────────────────────────────────
# 自定义词
# ────────────────────────────────────────────────────────────
def test_extra_terms_are_literal_not_regex() -> None:
    """用户填进来的东西是字面量。填了个 ``[`` 不该让命令崩掉。"""
    out = redact("红中碰了[括号", extra_terms=["[", "红中"])
    assert "红中" not in out
    assert "[括号" not in out


def test_blank_and_duplicate_extra_terms_are_skipped() -> None:
    """空串与重复项不产生规则。"""
    report = redact_with_report("公司在北京", extra_terms=["", "  ", "北京", "北京"])
    assert dict(report.hits) == {"extra:北京": 1}


# ────────────────────────────────────────────────────────────
# 报告
# ────────────────────────────────────────────────────────────
def test_report_counts_only_rules_that_fired() -> None:
    """报告只列命中过的规则，按规则表顺序。"""
    report = redact_with_report("发到 a@b.com，路径 C:\\Users\\lmg\\x.log")
    assert dict(report.hits) == {"email": 1, "win_path": 1}
    assert report.total == 2


def test_report_on_clean_text_is_empty() -> None:
    """干净的文本不该报告任何命中。"""
    report = redact_with_report("今天天气不错，我们去公园吧")
    assert report.hits == ()
    assert report.total == 0
    assert report.text == "今天天气不错，我们去公园吧"


def test_empty_text_short_circuits() -> None:
    """空串直接返回，不构造规则表。"""
    assert redact("") == ""
    assert redact_with_report("").hits == ()


# ────────────────────────────────────────────────────────────
# 指纹
# ────────────────────────────────────────────────────────────
def test_digest_is_stable() -> None:
    """同一版规则表，指纹不变。"""
    assert digest() == digest()
    assert len(digest()) == 12


def test_digest_changes_when_extra_terms_change() -> None:
    """加了自定义词，磁盘上的旧数据集就过期了——指纹必须跟着变。"""
    assert digest(extra_terms=["北京"]) != digest()


def test_digest_ignores_user_name() -> None:
    """换显示名不该让已导出的样本集体标记为过期。

    实现方式上钉住这条决策：``digest`` 的参数表里**根本不该有** ``user_name``。
    只要它出现，就说明有人把内容混进了规则指纹。
    """
    assert "user_name" not in inspect.signature(digest).parameters


def test_placeholder_is_never_eaten_by_a_later_rule() -> None:
    """用户自定义词撞上占位符词汇时，输出不该出现嵌套方括号。

    这条钉住的是实现里的哨兵机制：占位符是在所有规则跑完之后才还原的。
    """
    out = redact("自定义一下", extra_terms=["自定义"])
    assert out == "[自定义]一下"
    assert "[[" not in out

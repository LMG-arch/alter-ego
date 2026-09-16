"""设置元数据的硬性约束。

来源：``docs/design/10-settings-center.md`` § 4「用测试强制标注」。

前三条是从那份文档逐字抄下来的三条 CI 断言。它们存在的理由只有一句：
**一个没有说明的设置项，用户看到它时无法做决定**——所以「缺说明」不是风格问题，
是功能没做完。第四条（默认值一致）是同一件事的另一面：说明对了但默认值写错了，
点「恢复默认」会恢复成另一个值，而用户不会去翻源码对照。

这一组测试用到的都是公开接口，唯一例外是 `:func:`_register`——
它必须在导入期拒绝坏数据，而那是无法从外面观察到的行为，只能直接调。
"""

from __future__ import annotations

import dataclasses
import dataclasses as dc
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest

from alterego.kernel import settings_catalog as catalog
from alterego.kernel.config import Config
from alterego.kernel.config_values import type_hints
from alterego.kernel.settings import (
    Choice,
    Setting,
    SettingKind,
    infer_setting,
    is_annotated,
)
from alterego.kernel.settings_catalog import (
    CONFIG_DATACLASSES,
    SETTING_METADATA,
    all_settings,
    get_setting_metadata,
    groups,
    section_paths,
    settings_for,
)


# ═══════════════════════════════════════════════════════════════
#  § 4 的三条 CI 断言
# ═══════════════════════════════════════════════════════════════


def test_every_config_field_has_metadata() -> None:
    """每个内核配置字段都必须有 Setting 元数据，否则设置页会出现无法解释的项。"""
    missing = [
        f"{cls.__name__}.{info.name}"
        for cls in CONFIG_DATACLASSES
        for info in dataclasses.fields(cls)
        if not get_setting_metadata(cls, info.name)
    ]
    assert not missing, f"以下配置项缺少展示元数据：{missing}"


def test_every_effect_is_a_sentence() -> None:
    """effect 必须是可验证的因果陈述，不是空话。"""
    for setting in all_settings():
        assert len(setting.effect) >= 8, f"{setting.key} 的 effect 太短，等于没说"
        assert not any(word in setting.effect for word in ("可能", "大概", "也许")), (
            f"{setting.key} 的 effect 是模糊表述，用户无法据此做决定"
        )


def test_every_enum_choice_explains_its_consequence() -> None:
    """枚举的每一个选项都要说清「选它会发生什么」。"""
    for setting in all_settings():
        if setting.kind is SettingKind.ENUM:
            assert setting.choices, f"{setting.key} 是枚举但没有选项说明"
            for choice in setting.choices:
                assert choice.consequence, f"{setting.key} 的选项 {choice.value} 没有说明后果"


def test_every_setting_has_a_label_and_a_group() -> None:
    """label 是设置页上那一行的标题，group 决定它落在哪个 tab 下。"""
    for setting in all_settings():
        assert setting.label.strip(), f"{setting.key} 没有 label"
        assert setting.group.strip(), f"{setting.key} 没有 group"


def test_every_description_answers_what_this_is() -> None:
    """description 回答「这是什么」。有 label 就不该再靠 key 猜。"""
    for setting in all_settings():
        assert len(setting.description) >= 4, f"{setting.key} 的 description 说不清它是什么"


# ═══════════════════════════════════════════════════════════════
#  目录自身的内在一致性
# ═══════════════════════════════════════════════════════════════


def test_keys_are_unique() -> None:
    """一个键只能有一个来源，否则「当前值」会取决于注册顺序。"""
    keys = [setting.key for setting in all_settings()]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicated, f"这些键被注册了多次：{duplicated}"


def test_every_default_matches_the_dataclass() -> None:
    """元数据里的 default 必须与配置类里的默认值逐字一致。

    不一致的后果不显眼但很脏：设置页点「恢复默认」会恢复成**另一个**值。
    用户不会去翻源码对照，他会以为这个项目的默认值就是那个。

    元数据写 ``default=None`` 表示「这一项没有有意义的单项默认值」（容器型字段
    就是这种），不算不一致；但配置类里有默认值时它不能缺席。
    """
    mismatched = []
    for cls in CONFIG_DATACLASSES:
        for field in dc.fields(cls):
            setting = get_setting_metadata(cls, field.name)
            assert setting is not None
            expected = (
                field.default if field.default is not dc.MISSING else field.default_factory()  # type: ignore[misc]
            )
            if expected in (None, (), {}):
                continue
            if dc.is_dataclass(expected):
                # 这一项本身就是一「段」（``llm.routing`` 这种），它的默认值是整段
                # 配置的实例，不是能展示、能比较的单个值。
                continue
            if setting.default != expected:
                mismatched.append(
                    f"{setting.key}：元数据 {setting.default!r} != 配置类默认值 {expected!r}"
                )
    assert not mismatched, "元数据的默认值与配置类对不上：" + "；".join(mismatched)


def test_every_enum_matches_its_literal() -> None:
    """标注成 ``Literal`` 的字段，元数据里的选项与那个 Literal 必须一一对应。

    对应的是**清单**，不是顺序：设置页按元数据的顺序列出选项，而那个顺序是
    作者权衡过的（``storage.temp_store`` 把默认值排在第一个）。顺序不同是
    有意的，少一个选项才是缺陷——用户会发现配置文件里合法的值在界面上选不到。
    """
    for cls in CONFIG_DATACLASSES:
        for name, annotation in type_hints(cls).items():
            if get_origin(annotation) is not Literal:
                continue
            setting = get_setting_metadata(cls, name)
            assert setting is not None
            assert setting.kind is SettingKind.ENUM, (
                f"{setting.key} 的类型是 Literal，元数据却不是枚举"
            )
            values = [choice.value for choice in setting.choices]
            missing = [arg for arg in get_args(annotation) if arg not in values]
            extra = [value for value in values if value not in get_args(annotation)]
            assert not missing, f"{setting.key} 少了这些选项：{missing}"
            assert not extra, f"{setting.key} 多了这些选项：{extra}"


def test_sections_cover_every_config_dataclass() -> None:
    """每个配置段都能被路径找到——设置页要按段渲染，找不到就渲染不出来。"""
    assert CONFIG_DATACLASSES
    assert set(section_paths.values()) == set(CONFIG_DATACLASSES)
    # 根节点（``Config``）不在里面：它自己不是一段，没有值可以展示。
    assert "" not in section_paths
    assert section_paths["llm.routing"].__name__ == "LLMRoutingConfig"
    assert section_paths["llm"].__name__ == "LLMConfig"


def test_groups_keep_the_order_of_first_appearance() -> None:
    """分组顺序 = 首次出现的顺序，不是 sorted。

    ``sorted`` 会让中文分组名按 Unicode 码点排，「模型」与「Web 界面」的相对
    位置会变得没道理；设置页的 tab 顺序跟着元数据走，所以这里不能排。
    """
    seen: list[str] = []
    for setting in all_settings():
        if setting.group not in seen:
            seen.append(setting.group)
    assert tuple(seen) == groups()


def test_settings_for_is_a_view_of_the_registry() -> None:
    """``settings_for`` 与 ``get_setting_metadata`` 必须说同一件事。"""
    for cls, bucket in SETTING_METADATA.items():
        assert settings_for(cls) == bucket
        for name, setting in bucket.items():
            assert get_setting_metadata(cls, name) is setting
    assert get_setting_metadata(Config, "llm") is None
    assert settings_for(Config) == {}


# ═══════════════════════════════════════════════════════════════
#  导入期校验（设置写错了要在导入时就炸，不是等到设置页渲染时）
# ═══════════════════════════════════════════════════════════════


def _dummy(key: str) -> Setting:
    return Setting(
        key=key,
        label="假的",
        description="这条只是拿来测校验的。",
        kind=SettingKind.BOOL,
        default=False,
        group="测试",
        effect="它只存在于这个测试里，改它不会有任何事发生。",
    )


def test_register_rejects_an_unknown_section() -> None:
    """段名写错：报错要把它认识的全部段列出来，不然人只能靠猜。"""
    with pytest.raises(ValueError, match="不存在"):
        catalog._register([_dummy("nowhere.at_all")])


def test_register_rejects_an_unknown_field() -> None:
    """段对但字段不对：报错要把那个类真的有哪些字段列出来。"""
    with pytest.raises(ValueError, match="没有字段"):
        catalog._register([_dummy("core.no_such_key")])


def test_register_rejects_a_duplicate() -> None:
    """同一个键注册两次：后一次会让「当前值」取决于注册顺序，必须直接拒绝。"""
    existing = get_setting_metadata(catalog.section_paths["core"], "log_level")
    assert existing is not None
    with pytest.raises(ValueError, match="两次"):
        catalog._register([_dummy(existing.key)])
    # 拒绝是**先检查后写入**，所以注册表没有被这次失败的注册污染。
    assert get_setting_metadata(catalog.section_paths["core"], "log_level") is existing


def test_setting_rejects_bad_keys() -> None:
    """键必须是点分路径：没有段名的键没办法定位到它该写进哪一段。"""
    for bad in ("", ".core", "core", "core."):
        with pytest.raises(ValueError):
            _dummy(bad)


def test_setting_rejects_an_enum_without_choices() -> None:
    with pytest.raises(ValueError, match="choices"):
        Setting(
            key="core.log_level",
            label="日志级别",
            description="日志打多详细。",
            kind=SettingKind.ENUM,
            group="常规",
            effect="调到 DEBUG 会看到每一次模型调用的原文。",
        )


def test_setting_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError, match="minimum"):
        Setting(
            key="llm.timeout_seconds",
            label="超时",
            description="一次模型调用最多等多久。",
            kind=SettingKind.INT,
            default=60,
            group="模型",
            minimum=10,
            maximum=1,
            effect="调小会让慢模型更容易超时失败。",
        )


def test_is_annotated_means_effect_is_written() -> None:
    """有 effect 才叫「有说明」。空字符串不等于「不需要说明」。"""
    assert is_annotated(all_settings()[0])
    assert not is_annotated(
        Setting(
            key="core.timezone",
            label="时区",
            description="按哪个时区算今天。",
            kind=SettingKind.STR,
            default="Asia/Shanghai",
            group="常规",
        )
    )


# ═══════════════════════════════════════════════════════════════
#  infer_setting —— 元数据的第三层：从类型推断
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("cls", "name", "kind"),
    [
        (catalog.section_paths["core"], "log_level", SettingKind.STR),
        (catalog.section_paths["core"], "log_format", SettingKind.ENUM),
        (catalog.section_paths["core"], "data_dir", SettingKind.PATH),
        (catalog.section_paths["core"], "random_seed", SettingKind.INT),
        (catalog.section_paths["simulation"], "mode", SettingKind.ENUM),
        (catalog.section_paths["simulation"], "speed_multiplier", SettingKind.FLOAT),
        (catalog.section_paths["simulation"], "enable_npc_conversations", SettingKind.BOOL),
        (catalog.section_paths["disturb_budget"], "quiet_hours", SettingKind.LIST),
        (catalog.section_paths["llm"], "providers", SettingKind.MAPPING),
        (catalog.section_paths["llm"], "routing", SettingKind.MAPPING),
    ],
)
def test_infer_setting_reads_the_type(cls: type[Any], name: str, kind: SettingKind) -> None:
    """第三层：类型本身就说了它该怎么被编辑。

    这一层不是给内核配置用的（那 95 项都有手写元数据），是给**插件**用的——
    插件作者只写一个 dataclass，设置页就能把它渲染出来。
    """
    inferred = infer_setting(cls, name, prefix="core")
    assert inferred is not None
    assert inferred.kind is kind
    assert inferred.key == f"core.{name}"
    assert inferred.advanced, "推断出来的项一律算高级：没人替它写过说明"
    assert not is_annotated(inferred), "推断出来的项没有 effect，要显示成「未标注」"


def test_infer_setting_takes_the_default_from_the_dataclass() -> None:
    """推断时不给值，就用配置类自己的默认值。"""
    cls = catalog.section_paths["disturb_budget"]
    inferred = infer_setting(cls, "daily_message_limit", prefix="disturb_budget")
    assert inferred is not None
    assert inferred.default == 3
    explicit = infer_setting(cls, "daily_message_limit", prefix="disturb_budget", value=7)
    assert explicit is not None
    assert explicit.default == 7


def test_infer_setting_refuses_what_it_cannot_express() -> None:
    """认不出来的类型返回 ``None``，**不回落成字符串**。

    回落成字符串的表现是：设置页给一个「真的不认识的类型」渲染出文本框，
    用户填进去一个字符串，加载时才炸——错误发生在离现场很远的地方。
    """
    cls = catalog.section_paths["core"]

    @dataclasses.dataclass(frozen=True)
    class Weird:
        payload: bytes = b""

    assert infer_setting(Weird, "payload", prefix="core") is None
    assert infer_setting(cls, "no_such_field", prefix="core") is None


def test_infer_setting_marks_secrets() -> None:
    """名字像密钥的字段一律按密钥处理：密钥不进配置文件（§ 5.2）。"""

    @dataclasses.dataclass(frozen=True)
    class Provider:
        api_key: str = ""
        base_url: str = ""

    key_setting = infer_setting(Provider, "api_key", prefix="llm.providers.deepseek")
    url_setting = infer_setting(Provider, "base_url", prefix="llm.providers.deepseek")
    assert key_setting is not None
    assert key_setting.kind is SettingKind.SECRET
    assert url_setting is not None
    assert url_setting.kind is SettingKind.STR


def test_infer_setting_labels_the_last_segment() -> None:
    """推断出来的 label 只能是键名本身：它没有人写的说明。"""
    inferred = infer_setting(catalog.section_paths["study"], "field", prefix="study")
    assert inferred is not None
    assert inferred.label == "field"
    assert inferred.default == ""


def test_choices_of_a_string_enum_are_empty() -> None:
    """非 ``Literal`` 的字符串字段没有可枚举的选项，而不是「一个叫 str 的选项」。"""
    inferred = infer_setting(catalog.section_paths["core"], "log_level", prefix="core")
    assert inferred is not None
    assert inferred.choices == ()


def test_choice_is_frozen() -> None:
    choice = Choice(value="smart", label="智能", consequence="默认档，成本居中。")
    with pytest.raises(dataclasses.FrozenInstanceError):
        choice.value = "cheap"  # type: ignore[misc]


def test_default_paths_in_metadata_are_paths() -> None:
    """路径型默认值必须是 ``Path``。

    写成字符串会在「恢复默认」时把 ``Path("data")`` 变成 ``"data"``——
    值的形状变了，`__post_init__` 里的校验也就跟着换了对象。
    """
    setting = get_setting_metadata(catalog.section_paths["core"], "data_dir")
    assert setting is not None
    assert isinstance(setting.default, Path)

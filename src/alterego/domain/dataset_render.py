"""训练数据集的**渲染**：把已经算好的结果摆成人看得懂的文本。

``domain/dataset.py`` 负责「哪些行能变成样本、变成什么形状」，这里只负责
「这批文件叫什么、多少条、脱了几处」——写成 ``manifest.json`` 与 ``README.md``。

两边分家是因为它们**改动的理由不同**：样本形状变了要动 ``dataset.py``，
README 的措辞或表格变了只动这里。而 ``dataset.py`` 已经顶到 900 行的硬上限
（AGENTS.md § 5），再往里加一段说明文字就会红。

**没有 IO**：这里的函数只返回字符串与字典，落盘由 ``sim/dataset.py`` 做。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from alterego.domain.dataset import (
    REASONING_OPEN,
    DatasetName,
    ExportedFile,
    FormatName,
    spec_for,
)
from alterego.domain.redact import REDACTION_RULES


__all__ = [
    "render_manifest",
    "render_readme",
]


#: 各格式「一行样本长什么样」的示例，只用于生成 README。
#:
#: 写成现成字符串而不是现场 ``json.dumps``：形状与包裹键都不同，
#: 用字典拼就分不清哪些花括号是格式、哪些是数据。
_EXAMPLES: Final[dict[str, str]] = {
    "chat": '{"messages": [{"role": "user", "content": "……"}, '
    '{"role": "assistant", "content": "……"}]}',
    "sharegpt": '{"conversations": [{"from": "human", "value": "……"}, '
    '{"from": "gpt", "value": "……"}]}',
    "alpaca": '{"instruction": "……", "input": "……", "output": "……"}',
}


def _format_bytes(size: int) -> str:
    """字节数说成人话。README 里 ``1234567`` 不如 ``1.2 MB`` 好读。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def render_manifest(
    *,
    persona_name: str,
    generated_at: str,
    formats: Sequence[FormatName],
    redact_digest: str,
    redact_terms: Sequence[str],
    files: Sequence[ExportedFile],
    redactions: Sequence[tuple[str, int]],
    skipped: Sequence[tuple[DatasetName, str]] = (),
) -> dict[str, Any]:
    """``manifest.json`` 的内容。

    这个文件唯一的机器可读用途是 ``redact_digest``：它记着**磁盘上的数据集
    是用哪一版脱敏规则脱的**。脱敏规则改了之后 ``dataset list`` 一比对就知道
    该重跑——这是「脱敏必须可回溯重做」那条决策的兑现机制（ADR-0011）。

    ``formats`` 是**这个目录里出现过的全部形状**，不是一个：一次 ``build``
    可以同时产出 ``chat`` 与 ``sharegpt``，而说明文件只有一份，
    只记最后一个形状会让另一份文件在文档里凭空消失。
    """
    return {
        "persona": persona_name,
        "generated_at": generated_at,
        "formats": list(formats),
        "redact_digest": redact_digest,
        "redact_terms": list(redact_terms),
        "totals": {
            "samples": sum(item.samples for item in files),
            "bytes": sum(item.byte_count for item in files),
            "redactions": sum(count for _, count in redactions),
        },
        "files": [
            {
                "dataset": item.kind,
                "label": spec_for(item.kind).label,
                "file": item.filename,
                "samples": item.samples,
                "bytes": item.byte_count,
                "sha256": item.sha256,
                "source": spec_for(item.kind).source,
            }
            for item in files
        ],
        "redactions": [{"rule": name, "count": count} for name, count in redactions],
        "skipped": [{"dataset": kind, "reason": reason} for kind, reason in skipped],
    }


def render_readme(
    *,
    persona_name: str,
    generated_at: str,
    formats: Sequence[FormatName],
    redact_digest: str,
    redact_terms: Sequence[str],
    files: Sequence[ExportedFile],
    redactions: Sequence[tuple[str, int]],
    skipped: Sequence[tuple[DatasetName, str]] = (),
) -> str:
    """``README.md`` 的内容——这就是「数据页面」当前的形态。

    用行列表拼而不是一个大 f-string：正文里有 JSON 示例，
    里面全是花括号，塞进 f-string 只会让人分不清哪些括号是格式、哪些是数据。
    """
    primary = formats[0] if formats else "chat"

    lines: list[str] = [
        f"# {persona_name} 的训练数据集",
        "",
        f"> 由 `alterego dataset build` 生成于 {generated_at}。",
        ">",
        "> **整个目录都是派生产物。** 手改的内容会在下次 `build` 时被覆盖——",
        "> 要改脱敏规则，改配置里的 `[dataset] redact_terms` 再重跑。",
        "",
        "## 怎么用",
        "",
        f"每个 `.jsonl` 一行一条样本，UTF-8，一行一个 JSON 对象。形状看文件名"
        f"（本次：{'、'.join(f'`{item}`' for item in formats)}）：",
        "",
        "```json",
        _EXAMPLES[primary],
        "```",
        "",
        f"上面是 `{primary}` 的形状。文件名里写着哪一种，就读哪一种——",
        "**不要按 `*.jsonl` 整体读**，配方换过之后目录里可能不止一种形状。",
        "",
        "直接交给任何支持这套形状的训练框架即可。**本项目只产出数据，不做训练**",
        "（见 `docs/adr/0011-training-datasets-are-derived-and-redacted.md`）。",
        "",
        "## 有什么",
        "",
        "| 文件 | 数据集 | 条数 | 大小 | 来自 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    if files:
        lines.extend(
            f"| `{item.filename}` | {spec_for(item.kind).label} | {item.samples} | "
            f"{_format_bytes(item.byte_count)} | {spec_for(item.kind).source} |"
            for item in files
        )
    else:
        lines.append("| — | — | 0 | — | — |")

    for kind, reason in skipped:
        spec = spec_for(kind)
        lines.extend(["", f"> ⏸ **{spec.label}这一批是空的**：{reason}"])

    lines.extend(
        [
            "",
            "### 思考写在哪儿",
            "",
            f"推理与工具调用样本的助手正文里，思考被包在 `<{REASONING_OPEN}>` 与 "
            f"`</{REASONING_OPEN}>` 之间：",
            "",
            "```",
            f"<{REASONING_OPEN}>",
            "……它考虑过的选项、压下的念头、想起的事……",
            f"</{REASONING_OPEN}>",
            "",
            "……它最后说的话……",
            "```",
            "",
            "去掉这两个标记，微调出来的模型会把内心独白当成正文一起说出来。",
            "",
            "## 脱了什么",
            "",
            f"规则表指纹：`{redact_digest}`。条目与替换结果：",
            "",
            "| 规则 | 替换为 |",
            "| --- | --- |",
        ]
    )
    lines.extend(f"| `{rule.name}` | `[{rule.label}]` |" for rule in REDACTION_RULES)
    if redact_terms:
        joined = "、".join(f"`{term}`" for term in redact_terms)
        lines.append(f"| `extra`（来自 `[dataset] redact_terms`） | `[自定义]`（{joined}） |")
    lines.extend(
        [
            "",
            f"这一批总共替换了 **{sum(count for _, count in redactions)}** 处。",
            "",
            "**虚构角色的名字没有脱。** 这是有意的：训练集的目标是「训出一个会以",
            "这个名字自称、会称呼你的模型」，把所有人名统一换掉会让它失去称呼能力。",
            "脱敏摘的是**可识别的真实身份**，不是专有名词。",
            "",
            "## 已知局限",
            "",
            "- **正则挡不住自然语言里的身份信息。** 「我住在某某小区」这类句子，",
            "  规则表看不见。这一层不假装完备。",
            "- **脱敏规则改过之后要重跑 `build`。** `alterego dataset list` 会比对",
            "  指纹并提示，但它不会自动重跑——重跑要花时间，该由你决定什么时候。",
            "- **`tooluse` 与 `reasoning` 依赖上游的账。** 这两类的源表",
            "  （`tick_log` / `activity_log`）由推演引擎写；库里没有对应的行，",
            "  它们就是 0 条。此时**不会写出空文件**（一个 0 字节的 `.jsonl`",
            "  看起来像坏了），上面那张表会说清是哪一种空：时间范围里真没记录，",
            "  还是取到行但拼不出样本。",
            "- **不带系统提示。** 样本里现在只有对话本身。正式的人设提示词还没接进来",
            "  （它要由 `persona_json` 渲染出来），先编一份塞进训练集，只会让模型",
            "  学会一套运行时根本不会发给它的前言——比不带更糟。位置已经留好了。",
        ]
    )
    return "\n".join(lines) + "\n"

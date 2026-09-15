"""``alterego.sim.context`` 的测试。

这个模块本身没有逻辑，测试它的价值在于**约束住那两条刻意的设计**：

1. ``StateSnapshot`` 在 tick 内不可变，改状态只能通过 ``evolve``。
2. ``TickContext`` 的默认值必须是「每个 tick 各自一份」，不能是共享的类属性——
   否则上一个 tick 的 ``notes`` 会漏进下一个 tick，而那种 bug 极难发现。

依据: docs/design/04-simulation-loop.md § 2.2
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from alterego.sim.context import StateSnapshot, TickContext


def make_snapshot(**overrides: object) -> StateSnapshot:
    values: dict[str, object] = {"persona": object(), "emotion": object()}
    values.update(overrides)
    return StateSnapshot(**values)  # type: ignore[arg-type]


def make_context(**overrides: object) -> TickContext:
    values: dict[str, object] = {
        "tick_id": "tick-1",
        "virtual_now": datetime(2026, 9, 15, 9, 0),
        "correlation_id": "corr-1",
        "state": make_snapshot(),
        "rng": random.Random(42),
    }
    values.update(overrides)
    return TickContext(**values)  # type: ignore[arg-type]


class TestStateSnapshot:
    def test_frozen(self) -> None:
        snapshot = make_snapshot()
        with pytest.raises(Exception, match="cannot assign to field"):
            snapshot.persona = object()

    def test_evolve_returns_a_new_object(self) -> None:
        snapshot = make_snapshot()
        newer = snapshot.evolve(emotion="平静")
        assert newer is not snapshot
        assert newer.emotion == "平静"
        # 原来的快照一个字段都没动——这正是「可追溯」的前提。
        assert snapshot.emotion != "平静"

    def test_evolve_keeps_untouched_fields(self) -> None:
        snapshot = make_snapshot(relationships={"npc.a": 0.5})
        newer = snapshot.evolve(emotion="愉悦")
        assert newer.relationships == {"npc.a": 0.5}
        assert newer.persona is snapshot.persona

    def test_evolve_rejects_an_unknown_field(self) -> None:
        snapshot = make_snapshot()
        # 写错字段名时当场炸掉，而不是「改了但没生效」。
        with pytest.raises(TypeError):
            snapshot.evolve(emotioon="愉悦")

    def test_defaults_are_immutable_friendly(self) -> None:
        first = make_snapshot()
        second = make_snapshot()
        assert first.today_activity == ()
        assert first.recent_memories == ()
        assert first.current_block is None
        assert first.budget is None
        assert first.world is None
        assert first.relationships == {}
        assert first.relationships is not second.relationships


class TestTickContext:
    def test_note_appends(self) -> None:
        ctx = make_context()
        ctx.note("没什么特别的")
        ctx.note("它在发呆")
        assert ctx.notes == ["没什么特别的", "它在发呆"]

    def test_note_ignores_empty_text(self) -> None:
        ctx = make_context()
        ctx.note("")
        assert ctx.notes == []

    def test_lists_are_not_shared_between_ticks(self) -> None:
        first = make_context()
        second = make_context()
        first.note("只属于第一个 tick")
        first.candidates.append("work")
        first.suppressed.append("reach_out")

        assert second.notes == []
        assert second.candidates == []
        assert second.suppressed == []

    def test_has_chosen_intent(self) -> None:
        ctx = make_context()
        assert ctx.has_chosen_intent is False
        ctx.chosen_intent = "rest"
        assert ctx.has_chosen_intent is True

    def test_suppressed_is_kept_not_dropped(self) -> None:
        """被预算拦下的意图留在这里，不会消失（ADR-0005）。"""
        ctx = make_context()
        ctx.suppressed.append({"intent": "reach_out", "reason": "今日消息额度用完"})
        assert len(ctx.suppressed) == 1

    def test_percepts_default_to_none_not_empty(self) -> None:
        # ``None`` 与「感知到了但什么都没有」是两件事，前者表示 Sense 还没跑。
        assert make_context().percepts is None

"""``sim/budget.py`` 的测试：打扰预算的五条规则。

这个模块是设计原则 P3「机制约束优于提示词祈祷」的落点——「一天最多主动找
你三次」如果写在提示词里，模型完全可以不理；写成 :func:`check_budget`
之后，拦截发生在「意图已选中、行为还没执行」之间，模型没有绕过的机会。

所以这里要盯死两件事：

1. **五条检查的顺序**。顺序错了不会报错，只会让某一种失败模式悄悄溜过去
   （比如「凌晨三点但额度还富裕」应该被拦下，而先查额度就会放行）。
2. **被拦下的意图不能消失**。``downgrade_to`` 是接口：``None`` 也有含义，
   它表示「不换行为，但这条意图仍进 ``suppressed``」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest

from alterego.kernel.config import DisturbBudgetConfig
from alterego.kernel.errors import ConfigError
from alterego.sim.budget import (
    HIGH_URGENCY_THRESHOLD,
    NO_REPLY_CIRCUIT_HOURS,
    BudgetDecision,
    BudgetUsage,
    check_budget,
    record_no_reply,
    record_reply,
    record_sent,
    roll_over,
)


NOW = datetime(2026, 9, 15, 14, 0)
DAY = date(2026, 9, 15)
CONFIG = DisturbBudgetConfig()


@dataclass(frozen=True)
class FakeBlock:
    """只满足 :class:`~alterego.sim.budget.ScheduleBlockLike` 的替身。

    只要求 ``activity`` 与 ``interruptible`` 两个属性，所以不必为了调一次
    预算检查而拼出完整的 ``ScheduleBlock``（那要编 id、persona_id、day……）。
    """

    activity: str = "开会"
    interruptible: bool = True


def usage(**overrides: object) -> BudgetUsage:
    values: dict[str, object] = {"day": DAY}
    values.update(overrides)
    return BudgetUsage(**values)  # type: ignore[arg-type]


def check(
    kind: str | None = "message",
    *,
    at: datetime = NOW,
    config: DisturbBudgetConfig = CONFIG,
    block: FakeBlock | None = None,
    urgency: float = 0.0,
    **overrides: object,
) -> BudgetDecision:
    return check_budget(
        kind,
        usage=usage(**overrides),
        now=at,
        config=config,
        urgency=urgency,
        block=block,
    )


# ── 不需要预算约束的行为 ────────────────────────────────────


class TestKindsThatDoNotDisturb:
    """生成一张图不会弹通知，所以它不受打扰预算约束。"""

    @pytest.mark.parametrize("kind", [None, "media", "internal"])
    def test_unknown_kinds_are_always_allowed(self, kind: str | None) -> None:
        assert check(kind).allowed

    def test_the_reason_says_why_it_was_allowed(self) -> None:
        assert check(None).reason == "这类行为不打扰别人"

    def test_a_quiet_hours_time_still_allows_it(self) -> None:
        """免打扰管的是「打扰」，不是「什么都不许做」。"""
        midnight = datetime(2026, 9, 15, 3, 0)
        assert check(None, at=midnight).allowed

    def test_an_exhausted_quota_still_allows_it(self) -> None:
        assert check(None, messages_sent=99).allowed


# ── ① 免打扰 ────────────────────────────────────────────────


class TestQuietHours:
    def test_three_in_the_morning_is_blocked(self) -> None:
        midnight = datetime(2026, 9, 15, 3, 0)
        assert not check(at=midnight).allowed

    def test_the_reason_names_the_window(self) -> None:
        midnight = datetime(2026, 9, 15, 3, 0)
        assert "23:30–08:00" in check(at=midnight).reason

    def test_a_fresh_quota_does_not_buy_a_way_through(self) -> None:
        """额度再富裕也不该在凌晨三点开口。这是最容易让人反感的失败模式。"""
        midnight = datetime(2026, 9, 15, 3, 0)
        assert not check(at=midnight, messages_sent=0).allowed

    def test_maximum_urgency_does_not_buy_a_way_through(self) -> None:
        midnight = datetime(2026, 9, 15, 3, 0)
        assert not check(at=midnight, urgency=1.0).allowed

    def test_the_downgrade_becomes_inner_voice(self) -> None:
        midnight = datetime(2026, 9, 15, 3, 0)
        assert check(at=midnight).downgrade_to == "reflect_internal"

    def test_the_boundary_belongs_to_the_quiet_window(self) -> None:
        start = datetime(2026, 9, 15, 23, 30)
        assert not check(at=start).allowed

    def test_the_moment_it_ends_is_allowed(self) -> None:
        end = datetime(2026, 9, 15, 8, 0)
        assert check(at=end).allowed

    def test_a_window_that_wraps_midnight_still_works(self) -> None:
        """``(23:30, 08:00)`` 这一段跨了零点，比较方向不能想当然。"""
        assert not check(at=datetime(2026, 9, 15, 1, 0)).allowed
        assert check(at=datetime(2026, 9, 15, 12, 0)).allowed

    def test_a_window_that_does_not_wrap_also_works(self) -> None:
        daytime = DisturbBudgetConfig(quiet_hours=(time(9, 0), time(18, 0)))
        assert not check(at=datetime(2026, 9, 15, 12, 0), config=daytime).allowed
        assert check(at=datetime(2026, 9, 15, 20, 0), config=daytime).allowed


# ── ② 熔断 ──────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_it_stays_silent_until_the_circuit_expires(self) -> None:
        until = NOW + timedelta(hours=1)
        assert not check(circuit_until=until, consecutive_no_reply=2).allowed

    def test_the_reason_says_when_it_will_speak_again(self) -> None:
        until = NOW + timedelta(hours=6)
        assert "09-15 20:00" in check(circuit_until=until, consecutive_no_reply=3).reason

    def test_the_circuit_also_becomes_inner_voice(self) -> None:
        """熔断是它自己状态的问题，把冲动转成内心活动是合理的。"""
        until = NOW + timedelta(hours=1)
        assert check(circuit_until=until).downgrade_to == "reflect_internal"

    def test_an_expired_circuit_is_not_a_block(self) -> None:
        past = NOW - timedelta(seconds=1)
        assert check(circuit_until=past).allowed

    def test_the_exact_expiry_moment_is_allowed(self) -> None:
        assert check(circuit_until=NOW).allowed


# ── ③ 不可打断的日程 ────────────────────────────────────────


class TestUninterruptibleBlocks:
    def test_a_meeting_blocks_outbound_messages(self) -> None:
        assert not check(block=FakeBlock(interruptible=False)).allowed

    def test_the_reason_quotes_the_activity(self) -> None:
        blocked = FakeBlock(activity="和同事对需求", interruptible=False)
        assert "和同事对需求" in check(block=blocked).reason
        assert "不可打断" in check(block=blocked).reason

    def test_an_interruptible_block_is_fine(self) -> None:
        assert check(block=FakeBlock()).allowed

    def test_no_block_is_fine(self) -> None:
        """没有排定日程的时段是自由的，不该被当成「不可打断」。"""
        assert check(block=None).allowed

    def test_a_busy_block_also_becomes_inner_voice(self) -> None:
        assert check(block=FakeBlock(interruptible=False)).downgrade_to == "reflect_internal"


# ── ④ 日配额 ────────────────────────────────────────────────


class TestQuota:
    def test_the_last_allowed_message_goes_through(self) -> None:
        assert check(messages_sent=CONFIG.daily_message_limit - 1).allowed

    def test_the_next_one_is_blocked(self) -> None:
        assert not check(messages_sent=CONFIG.daily_message_limit).allowed

    def test_the_reason_states_the_limit_in_chinese(self) -> None:
        """用户读到的是「今天的主动消息配额（3 条）用完了」。"""
        decision = check(messages_sent=CONFIG.daily_message_limit)
        assert f"今天的主动消息配额（{CONFIG.daily_message_limit} 条）用完了" == decision.reason

    def test_the_quota_is_gone_becomes_inner_voice(self) -> None:
        """额度用完是「今天不能再说了」，把冲动转成内心活动正好。"""
        assert check(messages_sent=CONFIG.daily_message_limit).downgrade_to == "reflect_internal"

    def test_posts_have_their_own_quota(self) -> None:
        assert not check("post", posts_sent=CONFIG.daily_post_limit).allowed
        assert check("post", posts_sent=CONFIG.daily_post_limit - 1).allowed

    def test_posts_do_not_consume_the_message_quota(self) -> None:
        assert check("post", messages_sent=99).allowed

    def test_messages_do_not_consume_the_post_quota(self) -> None:
        assert check("message", posts_sent=99).allowed

    def test_urgency_raises_the_message_limit(self) -> None:
        over = CONFIG.daily_message_limit
        assert not check(messages_sent=over).allowed
        assert check(messages_sent=over, urgency=1.0).allowed

    def test_urgency_does_not_raise_the_post_limit(self) -> None:
        """一条动态没有「紧急」这种说法——急也还是得等明天。"""
        over = CONFIG.daily_post_limit
        assert not check("post", posts_sent=over, urgency=1.0).allowed

    def test_the_threshold_is_a_strict_comparison(self) -> None:
        decision = check(urgency=HIGH_URGENCY_THRESHOLD)
        assert "主动消息配额" in decision.reason or decision.allowed

    def test_an_urgent_message_is_labelled_as_such(self) -> None:
        decision = check(messages_sent=CONFIG.daily_message_limit_urgent, urgency=1.0)
        assert "紧急消息配额" in decision.reason


# ── ⑤ 最小间隔 ──────────────────────────────────────────────


class TestMinimumInterval:
    def test_two_messages_too_close_together_are_blocked(self) -> None:
        recent = NOW - timedelta(minutes=CONFIG.min_interval_minutes - 1)
        assert not check(last_message_at=recent, messages_sent=1).allowed

    def test_the_reason_counts_down_in_minutes(self) -> None:
        recent = NOW - timedelta(minutes=CONFIG.min_interval_minutes - 1)
        decision = check(last_message_at=recent, messages_sent=1)
        assert "才 89 分钟" in decision.reason
        assert "再等" in decision.reason

    def test_waiting_exactly_the_interval_is_enough(self) -> None:
        assert check(last_message_at=NOW - timedelta(minutes=CONFIG.min_interval_minutes)).allowed

    def test_no_previous_message_is_not_a_gap(self) -> None:
        assert check(last_message_at=None, messages_sent=0).allowed

    def test_the_interval_block_does_not_downgrade(self) -> None:
        """时间不对，转成内心活动纯属废动作——等一会儿再发就是了。"""
        recent = NOW - timedelta(minutes=1)
        assert check(last_message_at=recent, messages_sent=1).downgrade_to is None

    def test_posts_use_the_post_clock(self) -> None:
        recent = NOW - timedelta(minutes=1)
        assert not check("post", last_post_at=recent).allowed
        assert check("post", last_post_at=None).allowed

    def test_the_message_clock_does_not_block_a_post(self) -> None:
        recent = NOW - timedelta(minutes=1)
        assert check("post", last_message_at=recent).allowed

    def test_a_short_interval_can_be_configured(self) -> None:
        loose = DisturbBudgetConfig(min_interval_minutes=1)
        recent = NOW - timedelta(minutes=1)
        assert check(last_message_at=recent, config=loose, messages_sent=1).allowed

    def test_the_config_refuses_a_zero_interval(self) -> None:
        """每一分钟都能发一条不算「克制」，那只是一种没用的配置。"""
        with pytest.raises(ConfigError):
            DisturbBudgetConfig(min_interval_minutes=0)


# ── 顺序 ────────────────────────────────────────────────────


class TestOrderOfChecks:
    """五条检查的顺序不可交换。顺序错了不会报错，只会让某种模式溜过去。"""

    def test_quiet_hours_is_checked_before_the_quota(self) -> None:
        midnight = datetime(2026, 9, 15, 3, 0)
        assert "免打扰" in check(at=midnight, messages_sent=0).reason

    def test_the_circuit_is_checked_before_the_quota(self) -> None:
        until = NOW + timedelta(hours=2)
        decision = check(circuit_until=until, consecutive_no_reply=3, messages_sent=0)
        assert "没等到回音" in decision.reason

    def test_the_schedule_is_checked_before_the_quota(self) -> None:
        decision = check(block=FakeBlock(interruptible=False), messages_sent=0)
        assert "不可打断" in decision.reason

    def test_the_quota_is_checked_before_the_interval(self) -> None:
        """额度用完时给的理由该是「今天说够了」而不是「再等 90 分钟」——
        后者会让用户以为等一会儿就能发。"""
        decision = check(messages_sent=CONFIG.daily_message_limit, last_message_at=NOW)
        assert "配额" in decision.reason


# ── 记账 ────────────────────────────────────────────────────


class TestRecordSent:
    def test_a_message_bumps_the_counter_and_the_clock(self) -> None:
        updated = record_sent(usage(), kind="message", at=NOW)
        assert updated.messages_sent == 1
        assert updated.last_message_at == NOW
        assert updated.posts_sent == 0

    def test_a_post_bumps_its_own_counter(self) -> None:
        updated = record_sent(usage(), kind="post", at=NOW)
        assert updated.posts_sent == 1
        assert updated.last_post_at == NOW
        assert updated.messages_sent == 0

    def test_an_unknown_kind_changes_nothing(self) -> None:
        before = usage(messages_sent=2)
        assert record_sent(before, kind="media", at=NOW) == before

    def test_counting_does_not_touch_the_no_reply_streak(self) -> None:
        """发出去不等于被回复。把两件事混在一起会让熔断提前触发。"""
        assert (
            record_sent(usage(consecutive_no_reply=2), kind="message", at=NOW).consecutive_no_reply
            == 2
        )


class TestRecordNoReply:
    def test_an_unanswered_message_starts_a_streak(self) -> None:
        assert record_no_reply(usage(), config=CONFIG, at=NOW).consecutive_no_reply == 1

    def test_one_below_the_limit_is_still_just_a_streak(self) -> None:
        before = usage(consecutive_no_reply=CONFIG.consecutive_no_reply_limit - 2)
        updated = record_no_reply(before, config=CONFIG, at=NOW)
        assert updated.consecutive_no_reply == CONFIG.consecutive_no_reply_limit - 1
        assert updated.circuit_until is None

    def test_reaching_the_limit_trips_the_circuit(self) -> None:
        before = usage(consecutive_no_reply=CONFIG.consecutive_no_reply_limit - 1)
        updated = record_no_reply(before, config=CONFIG, at=NOW)
        assert updated.circuit_until == NOW + timedelta(hours=NO_REPLY_CIRCUIT_HOURS)

    def test_tripping_the_circuit_resets_the_streak(self) -> None:
        """不归零的话熔断一解除会立刻再次触发，变成永久静默。"""
        before = usage(consecutive_no_reply=CONFIG.consecutive_no_reply_limit - 1)
        assert record_no_reply(before, config=CONFIG, at=NOW).consecutive_no_reply == 0

    def test_a_limit_of_one_trips_immediately(self) -> None:
        strict = DisturbBudgetConfig(consecutive_no_reply_limit=1)
        updated = record_no_reply(usage(), config=strict, at=NOW)
        assert updated.circuit_until == NOW + timedelta(hours=NO_REPLY_CIRCUIT_HOURS)

    def test_a_tripped_circuit_leaves_the_other_counters_alone(self) -> None:
        """熔断是「暂停主动」，不是「忘掉今天已经说过几句话」。"""
        before = usage(messages_sent=2, posts_sent=1)
        before = record_no_reply(
            before, config=DisturbBudgetConfig(consecutive_no_reply_limit=1), at=NOW
        )
        assert before.messages_sent == 2
        assert before.posts_sent == 1


class TestRecordReply:
    def test_the_streak_resets(self) -> None:
        assert record_reply(usage(consecutive_no_reply=2)).consecutive_no_reply == 0

    def test_an_active_circuit_is_lifted(self) -> None:
        """用户主动说话了却还被熔断挡着，是「它在赌气」——而它不该赌气。"""
        assert record_reply(usage(circuit_until=NOW + timedelta(hours=5))).circuit_until is None

    def test_nothing_to_do_returns_the_same_object(self) -> None:
        """没有变化时返回原对象：调用方可以靠 ``is`` 判断「这一轮什么都没改」。"""
        before = usage()
        assert record_reply(before) is before

    def test_it_does_not_touch_the_sent_counters(self) -> None:
        before = usage(messages_sent=2, posts_sent=1)
        updated = record_reply(before)
        assert updated.messages_sent == 2
        assert updated.posts_sent == 1


# ── 跨天 ────────────────────────────────────────────────────


class TestRollOver:
    def test_a_new_day_clears_the_counters(self) -> None:
        """计数器不清零，第二天它会以为「今天已经发过 3 条」而永远不再开口。"""
        stale = usage(day=date(2026, 9, 14), messages_sent=3, posts_sent=2)
        fresh = roll_over(stale, today=DAY)
        assert fresh.day == DAY
        assert fresh.messages_sent == 0
        assert fresh.posts_sent == 0

    def test_the_same_day_returns_the_same_object(self) -> None:
        today = usage(messages_sent=2)
        assert roll_over(today, today=DAY) is today

    def test_roll_over_keeps_the_no_reply_streak(self) -> None:
        """「连续三次没回音」不因为过了零点就不算数。"""
        stale = usage(day=date(2026, 9, 14), consecutive_no_reply=2)
        assert roll_over(stale, today=DAY).consecutive_no_reply == 2

    def test_roll_over_keeps_the_circuit(self) -> None:
        """熔断是绝对时刻，本来就跨天。"""
        until = datetime(2026, 9, 16, 10, 0)
        stale = usage(day=date(2026, 9, 14), circuit_until=until)
        assert roll_over(stale, today=DAY).circuit_until == until

    def test_a_check_on_a_stale_usage_still_applies(self) -> None:
        """``check_budget`` 内部会先 roll_over，所以调用方不必自己记得清。"""
        stale = usage(day=date(2020, 1, 1), messages_sent=99)
        decision = check_budget(
            "message", usage=stale, now=NOW, config=CONFIG, urgency=0.0, block=None
        )
        assert decision.allowed

    def test_a_stale_usage_does_not_get_a_time_travelling_clock(self) -> None:
        stale = usage(day=date(2020, 1, 1), last_message_at=datetime(2020, 1, 1, 9, 0))
        decision = check_budget(
            "message", usage=stale, now=NOW, config=CONFIG, urgency=0.0, block=None
        )
        assert decision.allowed
        assert "今天第 1 条" in decision.reason


# ── 放行时的理由 ────────────────────────────────────────────


class TestAllowanceReason:
    def test_it_says_which_one_this_is(self) -> None:
        assert "今天第 1 条主动消息" in check(messages_sent=0).reason

    def test_it_counts_from_the_current_usage(self) -> None:
        assert "今天第 3 条主动消息" in check(messages_sent=2).reason

    def test_posts_are_labelled_posts(self) -> None:
        assert "今天第 1 条动态" in check("post").reason

    def test_the_reason_is_readable_not_a_payload(self) -> None:
        """理由最终会走到用户眼前，所以它不能是 ``limit=3 used=3`` 这种东西。"""
        for decision in (check(messages_sent=99), check(at=datetime(2026, 9, 15, 3, 0))):
            assert "=" not in decision.reason

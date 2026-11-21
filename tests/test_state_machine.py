from __future__ import annotations

import pytest

from app.exceptions import StatusTransitionInvalid
from app.models import BookingStatus as S
from app.services.state_machine import (
    TERMINAL,
    TRANSITIONS,
    can_transition,
    ensure_transition,
    is_terminal,
)

P = S.PENDING_PAYMENT.value
C = S.CONFIRMED.value
CI = S.CHECKED_IN.value
D = S.COMPLETED.value
X = S.CANCELLED.value
E = S.EXPIRED.value
N = S.NO_SHOW.value


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (P, C),  # 支付成功
        (P, E),  # 15 分钟超时
        (P, X),  # 未支付取消
        (C, CI),  # 到店核销
        (C, X),  # 已支付后取消（走退款）
        (C, N),  # 开始 15 分钟未核销
        (CI, D),  # 时段结束
    ],
)
def test_legal_transitions(src, dst):
    assert can_transition(src, dst)
    ensure_transition(src, dst)  # 不抛异常


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (E, C),  # 超时释放的单不能被支付「复活」——最容易写错的一条
        (E, X),
        (N, C),  # 爽约后不能复活
        (X, C),
        (X, E),
        (C, E),  # 已支付不会超时
        (C, P),  # 不能倒流
        (CI, X),  # 已核销不能取消（服务已经消费）
        (CI, C),
        (D, CI),  # 完成是终态
        (P, P),
    ],
)
def test_illegal_transitions(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(StatusTransitionInvalid):
        ensure_transition(src, dst)


def test_terminal_states_have_no_outgoing_edges():
    assert TERMINAL == {D, X, E, N}
    for status in TERMINAL:
        assert is_terminal(status)
        assert all(not can_transition(status, other) for other in (P, C, CI, D, X, E, N))


def test_occupying_matches_migration_where_clause():
    """迁移里的 EXCLUDE ... WHERE (status IN (...)) 必须和这里完全一致。

    两边一旦漂移：约束会漏掉某个占位状态（导致超卖），
    或者把终态也算进去（导致取消后时段永远卖不出去）。

    checked_in 必须在白名单里：核销后时段仍在被使用，不能被二次卖出。
    """
    assert set(S.occupying()) == {"pending_payment", "confirmed", "checked_in"}
    assert set(S.occupying()).isdisjoint(TERMINAL)


def test_every_status_is_reachable_or_terminal():
    """防止加了新状态却忘记接进转移表（那样它会变成孤岛，永远进不去）。"""
    all_statuses = {P, C, CI, D, X, E, N}
    reachable = {dst for dsts in TRANSITIONS.values() for dst in dsts}
    assert all_statuses == reachable | {P}

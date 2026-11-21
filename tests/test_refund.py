from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.refund import TIERS, compute_refund, refund_ratio

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 12, 1, 10, 0, tzinfo=TZ)


def _now_before(delta: timedelta) -> datetime:
    return START - delta


@pytest.mark.parametrize(
    ("lead", "expected_ratio", "expected_cents"),
    [
        (timedelta(days=3), 1.0, 20_000),  # 提前 3 天 → 全退
        (timedelta(hours=24), 1.0, 20_000),  # 正好 24h，闭区间左端算「以上」
        (timedelta(hours=24) - timedelta(seconds=1), 0.5, 10_000),  # 差 1 秒掉档
        (timedelta(hours=2), 0.5, 10_000),  # 正好 2h
        (timedelta(hours=2) - timedelta(seconds=1), 0.0, 0),
        (timedelta(minutes=1), 0.0, 0),
        (timedelta(0), 0.0, 0),  # 刚好开始
        (timedelta(hours=-3), 0.0, 0),  # 已开始
    ],
)
def test_tier_boundaries(lead, expected_ratio, expected_cents):
    ratio, _label = refund_ratio(START, _now_before(lead))
    refund, ratio2, _ = compute_refund(20_000, START, _now_before(lead))
    assert ratio == expected_ratio
    assert ratio2 == expected_ratio
    assert refund == expected_cents


def test_refund_rounds_down_to_cent():
    """1333 分的 50% = 666.5 → 666。宁可少退，也不要因为浮点多退。"""
    refund, _, _ = compute_refund(1333, START, _now_before(timedelta(hours=5)))
    assert refund == 666


def test_checked_in_gets_no_refund():
    """已核销 = 服务已消费，哪怕提前 10 天取消也不退。"""
    refund, ratio, label = compute_refund(
        20_000, START, _now_before(timedelta(days=10)), checked_in=True
    )
    assert (refund, ratio) == (0, 0.0)
    assert "核销" in label


def test_refund_never_exceeds_amount():
    for amount in (0, 1, 99, 100_000):
        refund, _, _ = compute_refund(amount, START, _now_before(timedelta(days=10)))
        assert 0 <= refund <= amount


def test_tiers_are_ordered_from_generous_to_strict():
    """档位必须是「从宽到严」的降序，否则 refund_ratio 的首次命中逻辑会取错档。"""
    leads = [t.min_lead for t in TIERS]
    assert leads == sorted(leads, reverse=True)

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.booking import quote_amount
from app.services.idempotency import hash_request

TZ = timezone(timedelta(hours=8))


def _range(minutes: int) -> tuple[datetime, datetime]:
    start = datetime(2026, 12, 1, 9, 0, tzinfo=TZ)
    return start, start + timedelta(minutes=minutes)


def test_hour_price_scales_linearly():
    assert quote_amount(10_000, *_range(60)) == 10_000
    assert quote_amount(10_000, *_range(30)) == 5_000
    assert quote_amount(10_000, *_range(120)) == 20_000


def test_rounds_up_to_cent():
    """每小时 10001 分，半小时 = 5000.5 → 5001，不能出现 0 元或漏分的单。"""
    assert quote_amount(10_001, *_range(30)) == 5_001


def test_zero_price_resource():
    assert quote_amount(0, *_range(60)) == 0


def test_hash_ignores_dict_order():
    a = {"resource_id": 1, "start_at": "2026-12-01T09:00:00", "end_at": "2026-12-01T10:00:00"}
    b = {"end_at": "2026-12-01T10:00:00", "start_at": "2026-12-01T09:00:00", "resource_id": 1}
    assert hash_request(a) == hash_request(b)


def test_hash_detects_payload_change():
    a = {"resource_id": 1, "start_at": "2026-12-01T09:00:00"}
    b = {"resource_id": 1, "start_at": "2026-12-01T09:30:00"}
    assert hash_request(a) != hash_request(b)

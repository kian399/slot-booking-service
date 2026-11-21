"""对账的四类差异必须都能被查出来。

这些用例的存在意义：对账脚本本身也会写错。
只跑一次 `--demo` 看到两条差异，不能证明它覆盖了 D3、D4；
更不能证明"账平"的时候它真的会报 0 条（false negative 才是最危险的）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import (
    Booking,
    BookingStatus,
    BusinessHour,
    Payment,
    PaymentKind,
    Resource,
)
from app.services import booking as booking_service
from app.services import lifecycle
from scripts.reconcile import D1, D2, D3, D4, _cleanup_demo_data, reconcile

TZ = timezone(timedelta(hours=8))
pytestmark = pytest.mark.integration


@pytest.fixture()
def resource(schema):  # schema 来自 test_integration_booking.py 的同名 fixture
    db = SessionLocal()
    res = Resource(
        name=f"recon-{uuid.uuid4().hex[:8]}",
        kind="room",
        timezone="Asia/Shanghai",
        slot_minutes=30,
        min_minutes=30,
        max_minutes=240,
        price_cents=20_000,
    )
    db.add(res)
    db.flush()
    for wd in range(7):
        db.add(
            BusinessHour(
                resource_id=res.id,
                weekday=wd,
                open_time=time(8, 0),
                close_time=time(20, 0),
            )
        )
    db.commit()
    db.refresh(res)
    yield res
    _cleanup_demo_data(db, res.id)
    db.close()


def _booked_and_cancelled(resource_id: int, *, offset_hours: int = 0, user_id: int = 1) -> int:
    """建单 → 支付 → 取消，返回 booking_id。默认提前 5 天，走到 100% 退款档。"""
    db = SessionLocal()
    try:
        base = (datetime.now(TZ) + timedelta(days=5)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        b, _ = booking_service.create_booking(
            db,
            user_id=user_id,
            resource_id=resource_id,
            start_at=base + timedelta(hours=offset_hours),
            end_at=base + timedelta(hours=offset_hours + 1),
        )
        lifecycle.pay(db, b.id)
        lifecycle.cancel(db, b.id)
        return b.id
    finally:
        db.close()


def test_clean_books_report_backslash_zero(resource):
    """正常取消并足额退款 → 必须报 0 差异。这一条防的是"对账永远在报警"。"""
    _booked_and_cancelled(resource.id, user_id=11)
    db = SessionLocal()
    report = reconcile(db)
    db.close()
    assert report.balanced, [d.detail for d in report.discrepancies]
    assert report.expected_refund_total == report.actual_refund_total


def test_d1_missing_refund_is_detected(resource):
    bid = _booked_and_cancelled(resource.id, user_id=12)
    db = SessionLocal()
    db.execute(delete(Payment).where(Payment.booking_id == bid, Payment.kind == "refund"))
    db.commit()
    report = reconcile(db)
    db.close()
    kinds = [d.kind for d in report.discrepancies]
    assert D1 in kinds


def test_d2_excess_refund_is_detected(resource):
    bid = _booked_and_cancelled(resource.id, user_id=13)
    db = SessionLocal()
    db.add(
        Payment(
            booking_id=bid,
            trade_no=f"extra-{uuid.uuid4().hex[:10]}",
            kind=PaymentKind.REFUND.value,
            amount_cents=5_000,
            status="success",
        )
    )
    db.commit()
    report = reconcile(db)
    db.close()
    kinds = [d.kind for d in report.discrepancies]
    assert D2 in kinds


def test_d3_paid_but_status_lost_is_detected(resource):
    """有支付流水但订单还停在 pending_payment：支付回调丢了 / 状态被改回去了。"""
    db = SessionLocal()
    base = (datetime.now(TZ) + timedelta(days=6)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    b, _ = booking_service.create_booking(
        db,
        user_id=14,
        resource_id=resource.id,
        start_at=base,
        end_at=base + timedelta(hours=1),
    )
    bid = b.id
    db.add(
        Payment(
            booking_id=bid,
            trade_no=f"pay-{uuid.uuid4().hex[:10]}",
            kind=PaymentKind.PAY.value,
            amount_cents=b.amount_cents,
            status="success",
        )
    )
    db.commit()
    report = reconcile(db)
    db.close()
    assert D3 in [d.kind for d in report.discrepancies]


def test_d4_phantom_refund_is_detected(resource):
    """订单是 confirmed，却有退款流水：钱退了但单没关。"""
    db = SessionLocal()
    base = (datetime.now(TZ) + timedelta(days=7)).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    b, _ = booking_service.create_booking(
        db,
        user_id=15,
        resource_id=resource.id,
        start_at=base,
        end_at=base + timedelta(hours=1),
    )
    bid = b.id
    lifecycle.pay(db, bid)
    db.add(
        Payment(
            booking_id=bid,
            trade_no=f"refund-{uuid.uuid4().hex[:10]}",
            kind=PaymentKind.REFUND.value,
            amount_cents=3_000,
            status="success",
        )
    )
    db.commit()
    status = db.execute(select(Booking.status).where(Booking.id == bid)).scalar_one()
    db.close()
    assert status == BookingStatus.CONFIRMED.value

    db = SessionLocal()
    report = reconcile(db)
    db.close()
    assert D4 in [d.kind for d in report.discrepancies]

"""集成测试：需要真实 PostgreSQL（排他约束在 SQLite 上不存在，必须用 PG 验证）。

跑法：
    docker compose up -d
    uv run alembic upgrade head
    uv run pytest -m integration -v

这里的每一条 fail 都对应一个真实会发生的线上事故：
- 并发抢同一时段 → 超卖（一槽两卖）
- 相邻时段被误判冲突 → 每个整点浪费一个槽位
- 取消/超时后时段不释放 → 卖不出去
- 幂等键失效 → 重复下单、重复扣款
- 超时后还能支付 → 用户付了钱但没有位置
- 同一用户同时占两个资源 → 业务规则被绕过
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text

from app.db import SessionLocal
from app.exceptions import (
    BookingNotPayable,
    IdempotencyInFlight,
    IdempotencyMismatch,
    SlotTaken,
    StatusTransitionInvalid,
)
from app.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    BusinessHour,
    Payment,
    Resource,
    Waitlist,
)
from app.services import booking as booking_service
from app.services import lifecycle, refund

TZ = timezone(timedelta(hours=8))
pytestmark = pytest.mark.integration


def _future_weekday() -> datetime:
    """取一个「未来 7~13 天的 10:00」，避开边界，也方便反复跑。"""
    base = datetime.now(TZ) + timedelta(days=7)
    return base.replace(hour=10, minute=0, second=0, microsecond=0)


def _make_resource(db, name: str, price_cents: int = 20_000) -> Resource:
    res = Resource(
        name=name,
        kind="room",
        timezone="Asia/Shanghai",
        slot_minutes=30,
        min_minutes=30,
        max_minutes=240,
        price_cents=price_cents,
    )
    db.add(res)
    db.flush()
    for weekday in range(7):
        db.add(
            BusinessHour(
                resource_id=res.id,
                weekday=weekday,
                open_time=time(8, 0),
                close_time=time(20, 0),
            )
        )
    db.commit()
    db.refresh(res)
    return res


def _drop_resource(db, resource_id: int) -> None:
    """按外键顺序拆掉一个资源。

    payments 对 bookings 是 RESTRICT，所以必须先删流水；
    直接 delete(resources) 会报 ForeignKeyViolation。
    """
    booking_ids = select(Booking.id).where(Booking.resource_id == resource_id)
    db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
    db.execute(delete(BookingEvent).where(BookingEvent.booking_id.in_(booking_ids)))
    db.execute(delete(Booking).where(Booking.resource_id == resource_id))
    db.execute(delete(Waitlist).where(Waitlist.resource_id == resource_id))
    db.execute(delete(BusinessHour).where(BusinessHour.resource_id == resource_id))
    db.execute(delete(Resource).where(Resource.id == resource_id))
    db.commit()


@pytest.fixture()
def resource(schema):
    db = SessionLocal()
    res = _make_resource(db, f"球场-{datetime.now().timestamp()}")
    yield res
    _drop_resource(db, res.id)
    db.close()


def _create(
    resource_id: int,
    start: datetime,
    end: datetime,
    user_id: int,
    key: str | None = None,
):
    db = SessionLocal()
    try:
        return booking_service.create_booking(
            db,
            user_id=user_id,
            resource_id=resource_id,
            start_at=start,
            end_at=end,
            idempotency_key=key,
        )
    finally:
        db.close()


# ---------------------------------------------------------------- 区间语义


def test_adjacent_slots_both_succeed(resource):
    """08:00-09:00 与 09:00-10:00 不冲突——证明区间用的是 '[)' 而不是 '[]'。"""
    base = _future_weekday().replace(hour=8)
    a, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)
    b, _ = _create(resource.id, base + timedelta(hours=1), base + timedelta(hours=2), user_id=2)
    assert a.id != b.id


def test_partial_overlap_is_rejected(resource):
    """09:00-10:00 与 09:30-10:30 起点不同但相交——这是 UNIQUE(start_at) 会漏掉的超卖。"""
    base = _future_weekday().replace(hour=9)
    _create(resource.id, base, base + timedelta(hours=1), user_id=1)
    with pytest.raises(SlotTaken) as exc:
        _create(resource.id, base + timedelta(minutes=30), base + timedelta(minutes=90), user_id=2)
    assert exc.value.status_code == 409
    assert exc.value.code == "SLOT_TAKEN"


def test_contained_range_is_rejected(resource):
    """10:00-12:00 被占后，10:30-11:00 这种「完全包含」的也得挡。"""
    base = _future_weekday().replace(hour=10)
    _create(resource.id, base, base + timedelta(hours=2), user_id=1)
    with pytest.raises(SlotTaken):
        _create(resource.id, base + timedelta(minutes=30), base + timedelta(hours=1), user_id=2)


def test_different_resources_do_not_conflict(resource):
    """约束的第一列是 resource_id：不同资源同一时段必须互不影响。"""
    db = SessionLocal()
    other = _make_resource(db, f"球场B-{datetime.now().timestamp()}")
    other_id = other.id
    db.close()

    base = _future_weekday().replace(hour=14)
    _create(resource.id, base, base + timedelta(hours=1), user_id=1)
    _create(other_id, base, base + timedelta(hours=1), user_id=2)

    db = SessionLocal()
    _drop_resource(db, other_id)
    db.close()


def test_same_user_cannot_hold_two_resources(resource):
    """第二条约束：同一用户不能同时占两个资源（纯业务规则，只能靠约束保证）。"""
    db = SessionLocal()
    other = _make_resource(db, f"球场C-{datetime.now().timestamp()}")
    other_id = other.id
    db.close()

    base = _future_weekday().replace(hour=15)
    _create(resource.id, base, base + timedelta(hours=1), user_id=777)
    with pytest.raises(SlotTaken) as exc:
        _create(other_id, base, base + timedelta(hours=1), user_id=777)
    assert "已有其它预约" in exc.value.message

    db = SessionLocal()
    _drop_resource(db, other_id)
    db.close()


# ---------------------------------------------------------------- 并发


def test_concurrent_booking_exactly_one_wins(resource):
    """核心用例：16 个线程同时抢同一个时段，只能有 1 个成功。

    这才是「应用层 SELECT 判断」永远做不到的事：
    所有线程的 SELECT 都会看到「没人占」，然后一起 INSERT。
    """
    base = _future_weekday().replace(hour=15, minute=30)
    end = base + timedelta(hours=1)
    n = 16
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(i: int):
        barrier.wait()  # 尽量同时开跑，放大竞争窗口
        try:
            _create(resource.id, base, end, user_id=1000 + i)
            with lock:
                outcomes.append("ok")
        except SlotTaken:
            with lock:
                outcomes.append("taken")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(attempt, range(n)))

    assert outcomes.count("ok") == 1, f"超卖了：{outcomes.count('ok')} 个成功"
    assert outcomes.count("taken") == n - 1

    db = SessionLocal()
    rows = db.execute(
        text(
            "SELECT count(*) FROM bookings "
            "WHERE resource_id = :r AND status IN ('pending_payment','confirmed','checked_in')"
        ),
        {"r": resource.id},
    ).scalar_one()
    db.close()
    assert rows == 1


# ---------------------------------------------------------------- 状态流转


def test_cancel_frees_the_slot(resource):
    """取消后同一时段必须能立刻再卖——靠的是约束的 WHERE status IN (...)。"""
    base = _future_weekday().replace(hour=11)
    end = base + timedelta(hours=1)
    booking, _ = _create(resource.id, base, end, user_id=1)
    with pytest.raises(SlotTaken):
        _create(resource.id, base, end, user_id=2)

    db = SessionLocal()
    lifecycle.cancel(db, booking.id)
    db.close()

    again, _ = _create(resource.id, base, end, user_id=3)
    assert again.id != booking.id


def test_expired_frees_the_slot(resource):
    base = _future_weekday().replace(hour=16)
    end = base + timedelta(hours=1)
    booking, _ = _create(resource.id, base, end, user_id=1)

    db = SessionLocal()
    released = lifecycle.expire_one(db, booking.id, now=booking.expires_at + timedelta(seconds=1))
    db.close()
    assert released is True

    again, _ = _create(resource.id, base, end, user_id=3)
    assert again.id != booking.id


def test_expire_is_idempotent_and_loses_to_payment(resource):
    """已支付的单不能被 worker 释放：CAS 的 WHERE status='pending_payment' 挡住它。"""
    base = _future_weekday().replace(hour=17)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    late = booking.expires_at + timedelta(hours=1)
    assert lifecycle.expire_one(db, booking.id, now=late) is False
    db.close()


def test_payment_after_expiry_is_rejected(resource):
    """「已超时但 worker 还没跑到」的窗口里，支付必须失败。"""
    base = _future_weekday().replace(hour=13)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    late = booking.expires_at + timedelta(seconds=1)
    with pytest.raises(BookingNotPayable):
        lifecycle.pay(db, booking.id, now=late)
    db.close()


def test_checkin_then_complete(resource):
    """confirmed → checked_in → completed。核销后仍占位，完成后才归档。"""
    base = _future_weekday().replace(hour=9, minute=30)
    end = base + timedelta(hours=1)
    booking, _ = _create(resource.id, base, end, user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    with pytest.raises(StatusTransitionInvalid, match="还未到核销时间"):
        lifecycle.checkin(db, booking.id, now=base - timedelta(hours=2))

    checked = lifecycle.checkin(db, booking.id, now=base)
    assert checked.status == BookingStatus.CHECKED_IN.value

    # 核销后仍占位：同资源同时段不能再卖
    with pytest.raises(SlotTaken):
        _create(resource.id, base, end, user_id=2)

    assert lifecycle.complete(db, booking.id, now=end + timedelta(minutes=1)) is True
    db.close()


def test_cannot_cancel_checked_in(resource):
    base = _future_weekday().replace(hour=12)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    lifecycle.checkin(db, booking.id, now=base)
    with pytest.raises(StatusTransitionInvalid, match="核销"):
        lifecycle.cancel(db, booking.id, now=base)
    db.close()


def test_no_show_releases_slot(resource):
    """confirmed 后 15 分钟未核销 → no_show，时段回到可售。"""
    base = _future_weekday().replace(hour=18)
    end = base + timedelta(hours=1)
    booking, _ = _create(resource.id, base, end, user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    assert lifecycle.mark_no_show(db, booking.id, now=base + timedelta(minutes=5)) is False
    assert lifecycle.mark_no_show(db, booking.id, now=base + timedelta(minutes=16)) is True
    db.close()

    again, _ = _create(resource.id, base, end, user_id=2)
    assert again.id != booking.id


# ---------------------------------------------------------------- 幂等


def test_idempotent_replay_returns_same_booking(resource):
    base = _future_weekday().replace(hour=9)
    end = base + timedelta(hours=1)
    first, created1 = _create(resource.id, base, end, user_id=1, key="k-1")
    second, created2 = _create(resource.id, base, end, user_id=1, key="k-1")
    assert created1 is True
    assert created2 is False
    assert first.id == second.id


def test_idempotency_key_reuse_with_different_payload_conflicts(resource):
    base = _future_weekday().replace(hour=9)
    _create(resource.id, base, base + timedelta(hours=1), user_id=1, key="k-2")
    with pytest.raises(IdempotencyMismatch):
        _create(
            resource.id, base + timedelta(hours=1), base + timedelta(hours=2), user_id=1, key="k-2"
        )


def test_idempotent_concurrent_submissions_create_one_booking(resource):
    """同一 key 并发提交 8 次：只落 1 单，其余要么回放、要么被告知稍后重试。"""
    base = _future_weekday().replace(hour=12, minute=30)
    end = base + timedelta(hours=1)
    barrier = threading.Barrier(8)
    ids: list[int] = []
    lock = threading.Lock()

    def attempt(_i: int):
        barrier.wait()
        try:
            booking, _created = _create(resource.id, base, end, user_id=1, key="k-concurrent")
            with lock:
                ids.append(booking.id)
        except IdempotencyInFlight:
            pass  # 首请求还没提交，允许客户端重试；不算错误

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(8)))

    assert len(set(ids)) == 1, f"同一个幂等键产生了多张单：{set(ids)}"

    db = SessionLocal()
    count = db.execute(
        text("SELECT count(*) FROM bookings WHERE idem_key = 'k-concurrent'")
    ).scalar_one()
    db.close()
    assert count == 1


# ---------------------------------------------------------------- 退款与对账


def test_cancel_refund_follows_tiers(resource):
    base = _future_weekday() + timedelta(days=3)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    amount = booking.amount_cents

    cancelled = lifecycle.cancel(db, booking.id)
    expected, _ratio, _label = refund.compute_refund(amount, booking.start_at, datetime.now(TZ))
    assert cancelled.status == BookingStatus.CANCELLED.value
    assert cancelled.refunded_cents == expected

    refunds = db.execute(
        text(
            "SELECT coalesce(sum(amount_cents),0) FROM payments "
            "WHERE booking_id=:b AND kind='refund'"
        ),
        {"b": booking.id},
    ).scalar_one()
    db.close()
    assert refunds == expected


def test_cancel_is_idempotent_no_double_refund(resource):
    base = _future_weekday() + timedelta(days=3)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    lifecycle.pay(db, booking.id)
    lifecycle.cancel(db, booking.id)
    lifecycle.cancel(db, booking.id)  # 重复取消：不应再记一笔退款

    refund_count = db.execute(
        text("SELECT count(*) FROM payments WHERE booking_id=:b AND kind='refund'"),
        {"b": booking.id},
    ).scalar_one()
    db.close()
    assert refund_count == 1


def test_unpaid_cancel_has_no_refund(resource):
    base = _future_weekday() + timedelta(days=2)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)
    db = SessionLocal()
    cancelled = lifecycle.cancel(db, booking.id)
    db.close()
    assert cancelled.refunded_cents == 0


def test_sweep_catches_what_redis_lost(resource):
    """兜底扫描：Redis 里没有 id，DB 里超期的单也必须被释放。"""
    base = _future_weekday().replace(hour=19)
    booking, _ = _create(resource.id, base, base + timedelta(hours=1), user_id=1)

    db = SessionLocal()
    ids = lifecycle.sweep_expired(db, now=booking.expires_at + timedelta(seconds=1))
    db.close()
    assert booking.id in ids

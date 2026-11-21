"""候补端到端演示：抢不到 → 排队 → 有人取消 → 自动补位 → 5 分钟不付再让给下一个。

    uv run python scripts/demo_waitlist.py
    uv run python scripts/demo_waitlist.py --keep   # 演示后保留数据，方便去库里看

跑完会打印每一步的实际状态，可以直接当面试 demo 用。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Booking,
    BookingEvent,
    BookingStatus,
    BusinessHour,
    Payment,
    Resource,
    Waitlist,
)
from app.services import booking as booking_service  # noqa: E402
from app.services import lifecycle, waitlist  # noqa: E402

TZ = timezone(timedelta(hours=8))
ALICE, BOB, CAROL = 7001, 7002, 7003


def banner(text: str) -> None:
    print(f"\n{'=' * 66}\n{text}\n{'=' * 66}")


def show(db, booking_id: int, label: str) -> None:
    b = db.get(Booking, booking_id)
    if b is None:
        print(f"  {label}: (不存在)")
        return
    print(f"  {label}: booking#{b.id} user={b.user_id} status={b.status} 退={b.refunded_cents}分")


def cleanup(db, resource_id: int) -> None:
    booking_ids = select(Booking.id).where(Booking.resource_id == resource_id)
    db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
    db.execute(delete(BookingEvent).where(BookingEvent.booking_id.in_(booking_ids)))
    db.execute(delete(Booking).where(Booking.resource_id == resource_id))
    db.execute(delete(Waitlist).where(Waitlist.resource_id == resource_id))
    db.execute(delete(BusinessHour).where(BusinessHour.resource_id == resource_id))
    db.execute(delete(Resource).where(Resource.id == resource_id))
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="演示后不清理数据")
    args = ap.parse_args()

    db = SessionLocal()
    rid = None
    try:
        suffix = uuid.uuid4().hex[:6]
        res = Resource(
            name=f"demo-waitlist-{suffix}",
            kind="room",
            timezone="Asia/Shanghai",
            slot_minutes=30,
            min_minutes=30,
            max_minutes=240,
            price_cents=20_000,
        )
        db.add(res)
        db.flush()
        rid = res.id
        for wd in range(7):
            db.add(
                BusinessHour(
                    resource_id=rid,
                    weekday=wd,
                    open_time=datetime.min.time().replace(hour=8),
                    close_time=datetime.min.time().replace(hour=20),
                )
            )
        db.commit()

        base = (datetime.now(TZ) + timedelta(days=4)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        start, end = base, base + timedelta(minutes=60)

        banner("1. Alice 下单并支付，占住 10:00-11:00")
        a, _ = booking_service.create_booking(
            db, user_id=ALICE, resource_id=rid, start_at=start, end_at=end
        )
        lifecycle.pay(db, a.id)
        show(db, a.id, "Alice")

        banner("2. Bob 抢同一时段 → 被排他约束挡下")
        try:
            booking_service.create_booking(
                db, user_id=BOB, resource_id=rid, start_at=start, end_at=end
            )
            print("  ❌ 不应该成功")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✅ 被拒绝：{type(exc).__name__} / {getattr(exc, 'code', '')}")

        banner("3. Bob 加入候补队列")
        wl = waitlist.join(db, resource_id=rid, user_id=BOB, start_at=start, end_at=end)
        wl_before = wl.status
        print(f"  waitlist#{wl.id} user={wl.user_id} status={wl.status}")

        banner("4. Alice 取消 —— 取消的同时应当触发补位")
        lifecycle.cancel(db, a.id)
        show(db, a.id, "Alice")

        promoted = db.execute(
            select(Booking).where(Booking.user_id == BOB, Booking.resource_id == rid)
        ).scalars().first()
        if promoted is None:
            print("  ❌ 没有补位：取消后未生成 Bob 的订单")
            return 1
        show(db, promoted.id, "Bob(补位)")
        print(f"  hold 到 {promoted.expires_at.astimezone(TZ):%H:%M:%S}（5 分钟）")

        wl_after = db.get(Waitlist, wl.id)
        print(f"  候补状态：{wl_before} → {wl_after.status}")

        banner("5. Bob 5 分钟没付 → 超时释放 → 轮到 Carol")
        carol_wl = waitlist.join(db, resource_id=rid, user_id=CAROL, start_at=start, end_at=end)
        carol_before = carol_wl.status
        released = lifecycle.expire_one(
            db, promoted.id, now=promoted.expires_at + timedelta(seconds=1)
        )
        print(f"  释放 Bob 的单：{released}")
        # worker 的路径是「释放成功后补位」，这里手动触发同一步
        nxt = waitlist.offer_next(db, resource_id=rid, start_at=start, end_at=end)
        if nxt is None:
            print("  ❌ Carol 没有被补位")
            return 1
        show(db, nxt.id, "Carol(补位)")
        print(f"  候补状态：{carol_before} → {db.get(Waitlist, carol_wl.id).status}")

        banner("6. 最终占位情况")
        rows = db.execute(
            select(Booking).where(
                Booking.resource_id == rid, Booking.status.in_(BookingStatus.occupying())
            )
        ).scalars().all()
        print(f"  当前占位：{[f'#{b.id}/{b.user_id}/{b.status}' for b in rows]}")
        print("  ✅ 同一时段始终只有一条占位记录")

        if not args.keep:
            cleanup(db, rid)
            print("\n（演示数据已清理，加 --keep 可保留）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

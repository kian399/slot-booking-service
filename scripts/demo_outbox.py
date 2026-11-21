"""事务性发件箱演示：事件不丢、只会重复、消费端按 id 去重。

    uv run python scripts/demo_outbox.py

核心要讲清三件事：
1. 事件是在**业务事务内**写进 outbox 表的，不是"提交后再发消息"，所以不会丢；
2. 投递是**至少一次**：XADD 成功、标记 published_at 前崩了 → 重启会重投；
3. 因此消费端必须按 outbox_id 幂等去重——这就是 at-least-once 的代价。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, update  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Booking,
    BookingEvent,
    BusinessHour,
    Outbox,
    Payment,
    Resource,
)
from app.redis_client import redis_client  # noqa: E402
from app.services import booking as booking_service  # noqa: E402
from app.services import lifecycle  # noqa: E402
from app.services.outbox_worker import STREAM_KEY, publish_once  # noqa: E402

TZ = timezone(timedelta(hours=8))


def banner(text: str) -> None:
    print(f"\n{'=' * 66}\n{text}\n{'=' * 66}")


def pending_count(db) -> int:
    return db.execute(
        select(func.count()).select_from(Outbox).where(Outbox.published_at.is_(None))
    ).scalar_one()


def drain_stream() -> None:
    try:
        redis_client.delete(STREAM_KEY)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    drain_stream()
    db = SessionLocal()
    rid = None
    try:
        res = Resource(
            name=f"demo-outbox-{uuid.uuid4().hex[:6]}",
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

        base = (datetime.now(TZ) + timedelta(days=5)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )

        banner("1. 下单 + 支付 —— 事件随业务事务一起写入 outbox")
        b, _ = booking_service.create_booking(
            db, user_id=8001, resource_id=rid,
            start_at=base, end_at=base + timedelta(minutes=60),
        )
        lifecycle.pay(db, b.id)
        print(f"  订单 #{b.id} 已支付；outbox 未发布：{pending_count(db)} 条")
        types = db.execute(
            select(Outbox.event_type).where(Outbox.aggregate_id == b.id).order_by(Outbox.id)
        ).scalars().all()
        print(f"  事件类型：{types}")

        banner("2. 投递到 Redis Stream")
        # 库里可能躺着以前测试没投递的积压，先记下 Stream 当前末尾，
        # 只展示本次新增的事件，免得把历史噪音当成演示结果
        tail = redis_client.xrevrange(STREAM_KEY, count=1)
        since = f"({tail[0][0]}" if tail else "-"
        n = publish_once(db)
        print(f"  本次投递 {n} 条，剩余未发布：{pending_count(db)}")
        events = redis_client.xrange(STREAM_KEY, since, "+")
        for eid, fields in events:
            print(f"  {eid}  type={fields['event_type']}  agg={fields['aggregate_id']}")

        banner("3. 模拟崩溃：XADD 成功但没来得及标记 published_at")
        last = db.execute(select(Outbox).order_by(Outbox.id.desc()).limit(1)).scalar_one()
        db.execute(update(Outbox).where(Outbox.id == last.id).values(published_at=None))
        db.commit()
        print(f"  把 outbox#{last.id} 的 published_at 改回 NULL，模拟「投递后崩溃」")
        n2 = publish_once(db)
        total = len(redis_client.xrange(STREAM_KEY, since, "+"))
        print(f"  重启后又投递 {n2} 条；Stream 里现在共 {total} 条（重复的是正常现象）")

        banner("4. 消费端按 outbox_id 去重（at-least-once 的正确用法）")
        seen: set[str] = set()
        handled = duplicated = 0
        for _eid, fields in redis_client.xrange(STREAM_KEY, since, "+"):
            key = str(fields["outbox_id"])
            if key in seen:
                duplicated += 1
                continue
            seen.add(key)
            handled += 1
        print(f"  实际处理 {handled} 条，跳过重复 {duplicated} 条")
        print("  → 事件不丢（业务事务内写入），代价是可能重复，由消费端幂等兜住")

        if not args.keep:
            booking_ids = select(Booking.id).where(Booking.resource_id == rid)
            db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
            db.execute(delete(BookingEvent).where(BookingEvent.booking_id.in_(booking_ids)))
            db.execute(delete(Booking).where(Booking.resource_id == rid))
            db.execute(delete(BusinessHour).where(BusinessHour.resource_id == rid))
            db.execute(delete(Resource).where(Resource.id == rid))
            db.execute(delete(Outbox).where(Outbox.aggregate_id == b.id))
            db.commit()
            drain_stream()
            print("\n（演示数据已清理，加 --keep 可保留）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

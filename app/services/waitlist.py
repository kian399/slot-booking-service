"""候补队列（加分项 10）。

语义：抢不到的人排队；有人取消/超时释放时，按 (优先级 desc, 加入时间 asc) 补位，
给候补者建一张 hold 5 分钟的待支付单。他 5 分钟内不付，单子超时释放，再轮到下一个。

为什么补位要"直接建单"而不是"发通知让他自己来抢"：
通知链路（短信/推送）有延迟，而槽位是稀缺资源。直接建单 + 短 hold 是把
"补位"变成一次事务，而不是一次依赖外部系统的协作。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.config import settings
from app.exceptions import ResourceNotFound
from app.models import Booking, BookingEvent, BookingStatus, Waitlist

logger = structlog.get_logger(__name__)


def join(
    db: Session,
    *,
    resource_id: int,
    user_id: int,
    start_at: datetime,
    end_at: datetime,
    priority: int = 0,
) -> Waitlist:
    row = Waitlist(
        resource_id=resource_id,
        user_id=user_id,
        start_at=start_at,
        end_at=end_at,
        priority=priority,
        status="waiting",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    metrics.inc("waitlist_joined_total")
    return row


def offer_next(
    db: Session,
    *,
    resource_id: int,
    start_at: datetime,
    end_at: datetime,
    now: datetime | None = None,
) -> Booking | None:
    """为刚释放的时段找下一个候补者，给他建一张 5 分钟 hold 的单。

    返回 None 表示没人排队，或者排到的人建单时被并发抢走了（时段冲突）。
    """
    now = now or datetime.now(timezone.utc)
    entry = (
        db.execute(
            select(Waitlist)
            .where(
                Waitlist.resource_id == resource_id,
                Waitlist.status == "waiting",
                Waitlist.start_at < end_at,
                Waitlist.end_at > start_at,
            )
            .order_by(Waitlist.priority.desc(), Waitlist.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .first()
    )
    if entry is None:
        return None

    booking = Booking(
        resource_id=resource_id,
        user_id=entry.user_id,
        start_at=start_at,
        end_at=end_at,
        status=BookingStatus.PENDING_PAYMENT.value,
        amount_cents=_quote(db, resource_id, start_at, end_at),
        expires_at=now + timedelta(minutes=settings.WAITLIST_HOLD_MINUTES),
    )
    db.add(booking)
    try:
        db.flush()
    except Exception:  # noqa: BLE001 约束冲突 = 已经被别人抢走，本轮放弃
        db.rollback()
        logger.info("waitlist_offer_lost", resource_id=resource_id, waitlist_id=entry.id)
        return None

    entry.status = "converted"
    entry.offered_until = booking.expires_at
    db.add(
        BookingEvent(
            booking_id=booking.id,
            from_status=None,
            to_status=booking.status,
            actor="system",
            reason="waitlist_promoted",
        )
    )
    db.commit()
    metrics.inc("waitlist_promoted_total")
    logger.info(
        "waitlist_promoted", booking_id=booking.id, user_id=entry.user_id, waitlist_id=entry.id
    )
    return booking


def _quote(db: Session, resource_id: int, start_at: datetime, end_at: datetime) -> int:
    from app.models import Resource
    from app.services.booking import quote_amount

    resource = db.get(Resource, resource_id)
    if resource is None:
        raise ResourceNotFound("资源不存在", resource_id=resource_id)
    return quote_amount(resource.price_cents, start_at, end_at)

"""订单生命周期：支付 / 核销 / 取消 / 超时释放。

四条路径的共同纪律：**状态变化一律用 CAS（WHERE status = 期望值）**。
理由：支付回调、核销、延迟队列 worker 是三条独立的时间线，随时可能同时碰到同一张单。
先读状态再写回（read-modify-write）在并发下必然出错，这里一行都不这么写。

例外是 cancel：它需要读金额和 start_at 算退款，还要同时写 booking + payments 两张表，
多步写必须用 SELECT ... FOR UPDATE 锁住这一行，否则两个并发取消会各退一次。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app import metrics
from app.exceptions import BookingNotFound, BookingNotPayable, StatusTransitionInvalid
from app.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    Outbox,
    Payment,
    PaymentKind,
    PaymentStatus,
)
from app.services import delay_queue, refund

logger = structlog.get_logger(__name__)

P = BookingStatus.PENDING_PAYMENT.value
C = BookingStatus.CONFIRMED.value
CI = BookingStatus.CHECKED_IN.value
D = BookingStatus.COMPLETED.value
X = BookingStatus.CANCELLED.value
E = BookingStatus.EXPIRED.value
N = BookingStatus.NO_SHOW.value

# 到店核销的开放窗口：开始前 15 分钟 ~ 结束时刻
CHECKIN_LEAD = timedelta(minutes=15)
# 开始后多久未核销判 no_show
NO_SHOW_AFTER = timedelta(minutes=15)


def _event(
    db: Session, booking_id: int, src: str | None, dst: str, reason: str, actor: str
) -> None:
    db.add(
        BookingEvent(
            booking_id=booking_id,
            from_status=src,
            to_status=dst,
            actor=actor,
            reason=reason,
        )
    )


def _outbox(db: Session, event_type: str, booking: Booking, status: str) -> None:
    """status 显式传入：CAS 更新用的是 synchronize_session=False，
    内存里的 booking.status 还是旧值，直接读会写出错误的事件体。"""
    db.add(
        Outbox(
            event_type=event_type,
            aggregate_id=booking.id,
            payload={
                "booking_id": booking.id,
                "resource_id": booking.resource_id,
                "user_id": booking.user_id,
                "status": status,
            },
        )
    )


def pay(
    db: Session,
    booking_id: int,
    *,
    now: datetime | None = None,
    trade_no: str | None = None,
) -> Booking:
    """模拟支付成功回调。

    两个条件必须同时成立才允许流转：
      1) status = 'pending_payment'
      2) expires_at > now()
    第 2 条很关键：延迟队列有延迟（worker 积压、Redis 短暂不可用），
    只看状态就会出现「已经超过 15 分钟、但 worker 还没跑，用户还能付钱」的窗口，
    而这时场地可能已经被别人约走了。
    """
    now = now or datetime.now(timezone.utc)
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFound("订单不存在", booking_id=booking_id)

    # 支付回调天然会重试：已经 confirmed 就直接返回成功，不要报错，也不要重复记账
    if booking.status == C:
        metrics.inc("payment_duplicate_total")
        return booking

    if booking.status != P:
        raise BookingNotPayable(f"订单当前状态为 {booking.status}，无法支付")

    result = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == P, Booking.expires_at > now)
        .values(status=C, confirmed_at=now, updated_at=now, version=Booking.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        current = db.get(Booking, booking_id)
        metrics.inc("payment_rejected_total")
        raise BookingNotPayable(
            "订单已超时释放或状态已变化，无法支付",
            status=current.status if current else None,
        )

    db.add(
        Payment(
            booking_id=booking_id,
            trade_no=trade_no or f"pay-{uuid.uuid4().hex[:16]}",
            kind=PaymentKind.PAY.value,
            amount_cents=booking.amount_cents,
            status=PaymentStatus.SUCCESS.value,
        )
    )
    _event(db, booking_id, P, C, "payment_succeeded", "user")
    _outbox(db, "BookingConfirmed", booking, C)
    db.commit()
    db.expire_all()

    # 单已成交，队列里的悬空 id 可以清掉（不清也能被 CAS 挡掉，只是空转）
    delay_queue.remove(booking_id)
    metrics.inc("booking_confirmed_total")
    logger.info("booking_confirmed", booking_id=booking_id)
    return db.get(Booking, booking_id)


def checkin(db: Session, booking_id: int, *, now: datetime | None = None) -> Booking:
    """到店核销。只有 confirmed 且在窗口内才能核销。"""
    now = now or datetime.now(timezone.utc)
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFound("订单不存在", booking_id=booking_id)
    if booking.status == CI:
        return booking  # 幂等
    if booking.status != C:
        raise StatusTransitionInvalid(f"订单当前状态为 {booking.status}，无法核销")

    if now < booking.start_at - CHECKIN_LEAD:
        raise StatusTransitionInvalid("还未到核销时间", start_at=booking.start_at.isoformat())
    if now > booking.end_at:
        raise StatusTransitionInvalid("时段已结束，无法核销")

    result = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == C)
        .values(status=CI, checked_in_at=now, updated_at=now, version=Booking.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        raise StatusTransitionInvalid("状态已变化，核销失败")

    _event(db, booking_id, C, CI, "checked_in", "staff")
    db.commit()
    db.expire_all()
    metrics.inc("booking_checked_in_total")
    return db.get(Booking, booking_id)


def cancel(
    db: Session,
    booking_id: int,
    *,
    now: datetime | None = None,
    reason: str = "user_cancel",
) -> Booking:
    """取消 + 阶梯退款。

    用 SELECT ... FOR UPDATE 串行化同一张单的取消请求：
    两个并发取消如果都读到 confirmed，就会各写一条退款流水——退两次。
    锁的是单行，不影响其它订单。
    """
    now = now or datetime.now(timezone.utc)
    booking = db.execute(
        select(Booking).where(Booking.id == booking_id).with_for_update()
    ).scalar_one_or_none()
    if booking is None:
        raise BookingNotFound("订单不存在", booking_id=booking_id)

    if booking.status in BookingStatus.terminal():
        # 已是终态：幂等返回，不报错（客户端可能重试）
        metrics.inc("cancel_noop_total")
        db.commit()
        return booking

    if booking.status == CI:
        raise StatusTransitionInvalid("已核销的订单不能取消")

    src_status = booking.status
    if src_status == P:
        refund_cents = 0
        tier = "未支付"
    else:
        refund_cents, _ratio, tier = refund.compute_refund(
            booking.amount_cents, booking.start_at, now
        )

    booking.status = X
    booking.refunded_cents = refund_cents
    booking.closed_at = now
    booking.updated_at = now
    booking.version += 1

    if refund_cents > 0:
        db.add(
            Payment(
                booking_id=booking.id,
                trade_no=f"refund-{uuid.uuid4().hex[:16]}",
                kind=PaymentKind.REFUND.value,
                amount_cents=refund_cents,
                status=PaymentStatus.SUCCESS.value,
            )
        )
    _event(db, booking.id, src_status, X, reason, "user")
    _outbox(db, "BookingCancelled", booking, X)
    db.commit()
    db.expire_all()

    delay_queue.remove(booking_id)
    metrics.inc("booking_cancelled_total")
    metrics.inc("refund_total_cents", value=float(refund_cents))
    logger.info(
        "booking_cancelled",
        booking_id=booking_id,
        refund_cents=refund_cents,
        tier=tier,
        reason=reason,
    )

    # 释放即补位：从档期里退出来的时段应该立刻给队首候补者，
    # 而不是等他自己发现再抢一次。补位失败（没人排队/已被抢走）不影响取消本身。
    from app.services import waitlist  # 局部导入避免循环依赖

    resource_id, start_at, end_at = booking.resource_id, booking.start_at, booking.end_at
    try:
        promoted = waitlist.offer_next(
            db,
            resource_id=resource_id,
            start_at=start_at,
            end_at=end_at,
            now=now,
        )
        if promoted is not None:
            logger.info("waitlist_promoted_after_cancel", new_booking_id=promoted.id)
    except Exception:  # noqa: BLE001 补位是增值动作，不能因为它失败而回滚取消
        db.rollback()
        logger.warning("waitlist_promote_failed", booking_id=booking_id, exc_info=True)

    return db.get(Booking, booking_id)


def mark_no_show(db: Session, booking_id: int, *, now: datetime | None = None) -> bool:
    """开始 15 分钟后仍未核销 → no_show，释放时段。"""
    now = now or datetime.now(timezone.utc)
    booking = db.get(Booking, booking_id)
    if booking is None or booking.status != C:
        return False
    if now < booking.start_at + NO_SHOW_AFTER:
        return False

    result = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == C)
        .values(status=N, closed_at=now, updated_at=now, version=Booking.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        return False
    _event(db, booking_id, C, N, "no_show", "system")
    db.commit()
    metrics.inc("booking_no_show_total")
    return True


def complete(db: Session, booking_id: int, *, now: datetime | None = None) -> bool:
    """时段结束且已核销 → completed（归档态，不释放槽位也不需要释放）。"""
    now = now or datetime.now(timezone.utc)
    booking = db.get(Booking, booking_id)
    if booking is None or booking.status != CI or now < booking.end_at:
        return False

    result = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == CI)
        .values(status=D, closed_at=now, updated_at=now, version=Booking.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        return False
    _event(db, booking_id, CI, D, "completed", "system")
    db.commit()
    metrics.inc("booking_completed_total")
    return True


def expire_one(db: Session, booking_id: int, *, now: datetime | None = None) -> bool:
    """释放一张超时未支付的单。返回是否真的释放了（false = 已被支付/取消抢先）。"""
    now = now or datetime.now(timezone.utc)
    result = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.status == P, Booking.expires_at <= now)
        .values(status=E, closed_at=now, updated_at=now, version=Booking.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        return False

    booking = db.get(Booking, booking_id)
    _event(db, booking_id, P, E, "hold_timeout", "system")
    db.commit()

    if booking is not None and booking.expires_at is not None:
        delay = max((now - booking.expires_at).total_seconds(), 0)
        metrics.observe("hold_release_delay_seconds", delay)
    metrics.inc("booking_expired_total")
    logger.info("booking_expired", booking_id=booking_id)
    return True


# 兜底扫描：Redis 丢事件（ZADD 失败 / 队列被误删 / 换机器没持久化）时唯一能救命的手段。
# SKIP LOCKED 让多实例同时扫不会互相等锁。
_SWEEP_SQL = text(
    """
    WITH due AS (
        SELECT id FROM bookings
        WHERE status = 'pending_payment' AND expires_at <= :now
        ORDER BY expires_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE bookings b
    SET status = 'expired', closed_at = :now, updated_at = :now
    FROM due
    WHERE b.id = due.id
    RETURNING b.id
    """
)


def sweep_expired(db: Session, *, limit: int = 500, now: datetime | None = None) -> list[int]:
    now = now or datetime.now(timezone.utc)
    ids = [row[0] for row in db.execute(_SWEEP_SQL, {"now": now, "limit": limit}).all()]
    if ids:
        for booking_id in ids:
            _event(db, booking_id, P, E, "sweep_timeout", "system")
        db.commit()
        metrics.inc("booking_swept_total", value=float(len(ids)))
        logger.info("booking_swept", count=len(ids))
    else:
        db.rollback()
    return ids

"""预约相关接口。业务判断全在 services 里，这里只做协议层的事。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.exceptions import BookingNotFound
from app.models import Booking, BookingEvent
from app.schemas import BookingCreate, BookingEventOut, BookingOut
from app.services import booking as booking_service
from app.services import lifecycle

router = APIRouter(prefix="/bookings", tags=["bookings"])

IDEMPOTENCY_HEADER = Header(
    default=None,
    alias="Idempotency-Key",
    description="客户端生成，同一业务请求重复提交必须复用同一个值",
)


def _out(booking: Booking) -> BookingOut:
    return BookingOut(**booking_service.snapshot(booking))


@router.post("", response_model=BookingOut, status_code=201)
def create_booking(
    payload: BookingCreate,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_HEADER,
    db: Session = Depends(get_db),
):
    booking, created = booking_service.create_booking(
        db,
        user_id=payload.user_id,
        resource_id=payload.resource_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        idempotency_key=idempotency_key,
    )
    if not created:
        # 幂等重放：语义上「这次没创建任何东西」，所以 200 而不是 201。
        response.status_code = 200
    return _out(booking)


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: int, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFound("订单不存在", booking_id=booking_id)
    return _out(booking)


@router.get("/{booking_id}/events", response_model=list[BookingEventOut])
def get_events(booking_id: int, db: Session = Depends(get_db)):
    rows = (
        db.execute(
            select(BookingEvent)
            .where(BookingEvent.booking_id == booking_id)
            .order_by(BookingEvent.created_at)
        )
        .scalars()
        .all()
    )
    return [BookingEventOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/{booking_id}/pay", response_model=BookingOut)
def pay_booking(booking_id: int, db: Session = Depends(get_db)):
    """模拟支付回调。真实场景由支付网关异步调用，这里同步返回方便演示。"""
    return _out(lifecycle.pay(db, booking_id, now=datetime.now(timezone.utc)))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    booking_id: int,
    reason: str = Query(default="user_cancel", max_length=64),
    db: Session = Depends(get_db),
):
    return _out(lifecycle.cancel(db, booking_id, now=datetime.now(timezone.utc), reason=reason))


@router.post("/{booking_id}/checkin", response_model=BookingOut)
def checkin_booking(booking_id: int, db: Session = Depends(get_db)):
    """到店核销（真实场景由店员扫码调用）。"""
    return _out(lifecycle.checkin(db, booking_id, now=datetime.now(timezone.utc)))

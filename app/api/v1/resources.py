"""资源与营业时间。

写操作直接用 SQLAlchemy 同步会话——这个项目的并发压力在「抢时段」上，
资源管理是低频操作，不需要为它做任何优化。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.exceptions import ResourceNotFound
from app.models import BusinessHour, Resource
from app.schemas import AvailabilityOut, BusinessHoursPut, ResourceCreate, ResourceOut
from app.services import slots

router = APIRouter(prefix="/resources", tags=["resources"])


@router.post("", response_model=ResourceOut, status_code=201)
def create_resource(payload: ResourceCreate, db: Session = Depends(get_db)):
    if payload.min_minutes > payload.max_minutes:
        raise HTTPException(status_code=422, detail="min_minutes 不能大于 max_minutes")
    resource = Resource(
        name=payload.name,
        kind=payload.kind,
        price_cents=payload.price_cents,
        slot_minutes=payload.slot_minutes,
        min_minutes=payload.min_minutes,
        max_minutes=payload.max_minutes,
        timezone=payload.timezone,
    )
    db.add(resource)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="资源名已存在") from None
    db.refresh(resource)
    return resource


@router.get("", response_model=list[ResourceOut])
def list_resources(active_only: bool = True, db: Session = Depends(get_db)):
    stmt = select(Resource).order_by(Resource.id)
    if active_only:
        stmt = stmt.where(Resource.active.is_(True))
    return db.execute(stmt).scalars().all()


@router.put("/{resource_id}/business-hours", status_code=204)
def put_business_hours(resource_id: int, payload: BusinessHoursPut, db: Session = Depends(get_db)):
    if db.get(Resource, resource_id) is None:
        raise ResourceNotFound("资源不存在", resource_id=resource_id)
    payload.validate_order()

    # 整体替换而不是增量 PATCH：营业时间是「一张周表」，
    # 增量改很容易留下重叠的两段（09-18 和 10-12），后面的可售计算就乱套。
    db.execute(delete(BusinessHour).where(BusinessHour.resource_id == resource_id))
    for h in payload.hours:
        db.add(
            BusinessHour(
                resource_id=resource_id,
                weekday=h.weekday,
                open_time=h.open_time,
                close_time=h.close_time,
            )
        )
    db.commit()


@router.get("/{resource_id}/availability", response_model=AvailabilityOut)
def get_availability(resource_id: int, day: date, db: Session = Depends(get_db)):
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise ResourceNotFound("资源不存在", resource_id=resource_id)
    hours = slots.to_business_hours(
        db.execute(select(BusinessHour).where(BusinessHour.resource_id == resource_id))
        .scalars()
        .all()
    )
    return AvailabilityOut(
        resource_id=resource_id,
        day=day,
        slot_minutes=resource.slot_minutes,
        slots=slots.available_slots(db, resource=resource, day=day, hours=hours),
    )

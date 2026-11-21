"""候补队列接口（加分项 10）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import WaitlistCreate, WaitlistOut
from app.services import waitlist as waitlist_service

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


@router.post("", response_model=WaitlistOut, status_code=201)
def join_waitlist(payload: WaitlistCreate, db: Session = Depends(get_db)):
    row = waitlist_service.join(
        db,
        resource_id=payload.resource_id,
        user_id=payload.user_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        priority=payload.priority,
    )
    return WaitlistOut.model_validate(row, from_attributes=True)

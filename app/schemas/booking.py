from datetime import date, datetime, time

from pydantic import BaseModel, Field


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="room", max_length=16, description="studio / desk / room")
    price_cents: int = Field(default=0, ge=0, description="每小时单价（分）")
    slot_minutes: int = Field(default=30, ge=5, le=240)
    min_minutes: int = Field(default=30, ge=5, le=1440)
    max_minutes: int = Field(default=240, ge=5, le=1440)
    timezone: str = "Asia/Shanghai"


class ResourceOut(BaseModel):
    id: int
    name: str
    kind: str
    price_cents: int
    slot_minutes: int
    min_minutes: int
    max_minutes: int
    timezone: str
    active: bool


class BusinessHourIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=周一 … 6=周日")
    open_time: time
    close_time: time


class BusinessHoursPut(BaseModel):
    """整体替换某资源的每周营业时段：先删后插，避免增量改出错。"""

    hours: list[BusinessHourIn]

    def validate_order(self) -> None:
        for h in self.hours:
            if h.open_time >= h.close_time:
                raise ValueError(f"weekday={h.weekday} 的 open_time 必须早于 close_time")


class BookingCreate(BaseModel):
    resource_id: int
    user_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime


class BookingOut(BaseModel):
    id: int
    resource_id: int
    user_id: int
    start_at: datetime
    end_at: datetime
    status: str
    amount_cents: int
    refunded_cents: int
    expires_at: datetime
    created_at: datetime


class BookingEventOut(BaseModel):
    from_status: str | None
    to_status: str
    actor: str
    reason: str
    created_at: datetime


class AvailabilityOut(BaseModel):
    resource_id: int
    day: date
    slot_minutes: int
    slots: list[datetime]


class WaitlistCreate(BaseModel):
    resource_id: int
    user_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    priority: int = Field(default=0, ge=0, le=10)


class WaitlistOut(BaseModel):
    id: int
    resource_id: int
    user_id: int
    start_at: datetime
    end_at: datetime
    priority: int
    status: str
    created_at: datetime

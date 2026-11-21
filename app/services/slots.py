"""时段规则：对齐、时长、营业时间、可预约窗口，以及 availability 查询。

这些都是「业务规则」，DB 约束管不到（也不该管）：
- 排他约束保证不重叠，但不会管你是不是 09:15 这种没对齐的时间；
- 营业时间是每周模板，展开成具体日期需要时区。
分工：**正确性靠约束，合法性靠这里**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.exceptions import DurationInvalid, OutOfBusinessHours
from app.models import BookingStatus, Resource


@dataclass(frozen=True)
class BusinessHourSpec:
    weekday: int  # 0=周一
    open_time: time
    close_time: time


def _as_local(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        raise DurationInvalid("时间必须带时区（ISO 8601，例如 2026-11-20T09:00:00+08:00）")
    return dt.astimezone(tz)


def validate_slot(
    *,
    resource: Resource,
    hours: list[BusinessHourSpec],
    start_at: datetime,
    end_at: datetime,
    now: datetime,
    max_advance_days: int,
) -> None:
    """不合法就抛 422。注意这里**不查库判断冲突**，冲突交给约束。"""
    if end_at <= start_at:
        raise DurationInvalid("结束时间必须晚于开始时间")

    tz = ZoneInfo(resource.timezone)
    start_local = _as_local(start_at, tz)
    end_local = _as_local(end_at, tz)
    minutes = int((end_local - start_local).total_seconds() // 60)

    # 1) 时长约束
    if minutes < resource.min_minutes:
        raise DurationInvalid(
            f"最短预约 {resource.min_minutes} 分钟", min_minutes=resource.min_minutes
        )
    if minutes > resource.max_minutes:
        raise DurationInvalid(
            f"最长预约 {resource.max_minutes} 分钟", max_minutes=resource.max_minutes
        )

    # 2) 对齐：不对齐的时段会让排他约束的判定失去业务意义
    #    （14:07 这种起点既无法定价，也无法和排班表对上）
    if start_local.second or start_local.microsecond or end_local.second or end_local.microsecond:
        raise DurationInvalid("时间不能带秒或微秒")
    if start_local.minute % resource.slot_minutes or end_local.minute % resource.slot_minutes:
        raise DurationInvalid(f"时间必须按 {resource.slot_minutes} 分钟对齐")
    if minutes % resource.slot_minutes:
        raise DurationInvalid(f"时长必须是 {resource.slot_minutes} 分钟的整数倍")

    # 3) 窗口：不能约过去，也不能约太远
    if start_local < now.astimezone(tz):
        raise DurationInvalid("不能预约过去的时间")
    if start_local > (now.astimezone(tz) + timedelta(days=max_advance_days)):
        raise DurationInvalid(f"最多只能提前 {max_advance_days} 天预约")

    # 4) 营业时间：整个区间必须被**同一段**营业时间完整覆盖。
    #    跨午休（11:00-14:00 中间 12-14 关门）要拆成两单，否则等于卖了不存在的时段。
    if start_local.date() != end_local.date():
        raise OutOfBusinessHours("暂不支持跨天预约，请拆成两段")
    weekday = start_local.weekday()
    for spec in hours:
        if spec.weekday != weekday:
            continue
        if start_local.time() >= spec.open_time and end_local.time() <= spec.close_time:
            return
    raise OutOfBusinessHours("所选时段不在营业时间内", weekday=weekday)


def to_business_hours(rows: list) -> list[BusinessHourSpec]:
    return [BusinessHourSpec(r.weekday, r.open_time, r.close_time) for r in rows]


AVAILABILITY_SQL = text(
    """
    WITH params AS (
        SELECT CAST(:day_start AS timestamptz) AS day_start,
               CAST(:slot AS interval) AS slot
    ),
    grid AS (
        -- 唯一能「无中生有」出时间序列的方式：generate_series 生成整天的槽位起点
        SELECT gs AS slot_start, gs + p.slot AS slot_end
        FROM params p,
             generate_series(p.day_start, p.day_start + interval '1 day' - p.slot, p.slot) AS gs
    )
    SELECT g.slot_start
    FROM grid g
    WHERE NOT EXISTS (
        SELECT 1 FROM bookings b
        WHERE b.resource_id = :resource_id
          AND b.status = ANY(:occupying)
          -- 用同一种区间语义（左闭右开）做差集，边界才不会重合
          AND tstzrange(b.start_at, b.end_at, '[)') && tstzrange(g.slot_start, g.slot_end, '[)')
    )
    ORDER BY g.slot_start
    """
)


def available_slots(
    db: Session, *, resource: Resource, day: date, hours: list[BusinessHourSpec]
) -> list[datetime]:
    """某个资源某一天的可预约槽位起点。

    先按营业时间算出「可卖格口」，再和已占用区间做差集。
    只返回「完整落在同一段营业时间内」的格口，和 validate_slot 的规则保持一致。
    """
    tz = ZoneInfo(resource.timezone)
    slot = timedelta(minutes=resource.slot_minutes)
    day_hours = [h for h in hours if h.weekday == day.weekday()]

    rows = db.execute(
        AVAILABILITY_SQL,
        {
            "day_start": datetime.combine(day, time(0, 0), tzinfo=tz),
            "slot": slot,
            "resource_id": resource.id,
            "occupying": list(BookingStatus.occupying()),
        },
    ).all()
    free = {row[0] for row in rows}

    result: list[datetime] = []
    for spec in day_hours:
        cursor = datetime.combine(day, spec.open_time, tzinfo=tz)
        limit = datetime.combine(day, spec.close_time, tzinfo=tz)
        while cursor + slot <= limit:
            if cursor in free:
                result.append(cursor)
            cursor += slot
    return sorted(result)

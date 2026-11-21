from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.exceptions import DurationInvalid, OutOfBusinessHours
from app.models import Resource
from app.services.slots import BusinessHourSpec, validate_slot

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 20, 8, 0, tzinfo=TZ)  # 2026-11-20 是周五（weekday=4）


def _resource(slot_minutes: int = 30, min_minutes: int = 30, max_minutes: int = 240) -> Resource:
    return Resource(
        name="球场A",
        kind="room",
        timezone="Asia/Shanghai",
        slot_minutes=slot_minutes,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        price_cents=10_000,
    )


def _hours(*specs: tuple[int, time, time]) -> list[BusinessHourSpec]:
    return [BusinessHourSpec(w, o, c) for w, o, c in specs]


FULL_DAY = _hours((4, time(9, 0), time(18, 0)))
LUNCH_BREAK = _hours((4, time(9, 0), time(12, 0)), (4, time(14, 0), time(18, 0)))


def _at(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def _validate(
    start, end, *, hours=FULL_DAY, slot_minutes=30, now=NOW, min_minutes=30, max_minutes=240
):
    validate_slot(
        resource=_resource(slot_minutes, min_minutes=min_minutes, max_minutes=max_minutes),
        hours=hours,
        start_at=start,
        end_at=end,
        now=now,
        max_advance_days=30,
    )


def test_valid_slot_passes():
    _validate(_at(10), _at(11))


def test_rejects_backwards_range():
    with pytest.raises(DurationInvalid, match="结束时间"):
        _validate(_at(11), _at(10))
    with pytest.raises(DurationInvalid, match="结束时间"):
        _validate(_at(11), _at(11))


def test_rejects_too_short_and_too_long():
    """min/max 时长是 resources 上的配置，不是全局常量。"""
    with pytest.raises(DurationInvalid, match="最短"):
        _validate(_at(10), _at(10, 15))  # 15 分钟 < 30 分钟下限
    with pytest.raises(DurationInvalid, match="最长"):
        _validate(_at(10), _at(15), max_minutes=120)  # 5 小时 > 2 小时上限


@pytest.mark.parametrize("start,end", [(_at(10, 15), _at(11, 15)), (_at(10, 0), _at(10, 45))])
def test_rejects_unaligned_slots(start, end):
    """不对齐的时段会让「同一时段」失去定义：14:07 起算的 53 分钟该收多少钱？
    更重要的是排班表按粒度切分，不对齐的订单无法和班次对上。"""
    with pytest.raises(DurationInvalid, match="对齐|整数倍"):
        _validate(start, end)


def test_rejects_seconds_precision():
    with pytest.raises(DurationInvalid, match="秒"):
        _validate(_at(10).replace(second=30), _at(11))


def test_rejects_past_slot():
    with pytest.raises(DurationInvalid, match="过去"):
        _validate(_at(7), _at(8))


def test_rejects_too_far_ahead():
    # 选的日期是周五（有营业时间），只有「超过 30 天」这一条能拦住它
    with pytest.raises(DurationInvalid, match="最多"):
        _validate(_at(10), _at(11), now=datetime(2026, 10, 1, 0, 0, tzinfo=TZ))


def test_rejects_outside_business_hours():
    with pytest.raises(OutOfBusinessHours, match="营业时间"):
        _validate(_at(8), _at(9))  # 9 点才开门
    with pytest.raises(OutOfBusinessHours, match="营业时间"):
        _validate(_at(17, 30), _at(18, 30))


def test_rejects_range_spanning_lunch_break():
    """11:30-14:30 中间隔着 12:00-14:00 的休息段：必须拆成两单。"""
    with pytest.raises(OutOfBusinessHours, match="营业时间"):
        _validate(_at(11, 30), _at(14, 30), hours=LUNCH_BREAK)
    _validate(_at(11, 00), _at(12, 00), hours=LUNCH_BREAK)  # 单段内合法
    _validate(_at(14, 00), _at(15, 00), hours=LUNCH_BREAK)


def test_rejects_other_weekday():
    with pytest.raises(OutOfBusinessHours, match="营业时间"):
        _validate(_at(10, day=21), _at(11, day=21))  # 周六没排班


def test_rejects_cross_day():
    with pytest.raises(OutOfBusinessHours, match="跨天"):
        _validate(_at(23), _at(1, day=21))


def test_rejects_naive_datetime():
    with pytest.raises(DurationInvalid, match="时区"):
        _validate(datetime(2026, 11, 20, 10, 0), datetime(2026, 11, 20, 11, 0))


def test_accepts_same_instant_in_other_timezone():
    """UTC 表达同一时刻也必须通过——时区只影响「营业时间」的解释，不影响时刻本身。"""
    _validate(_at(10).astimezone(timezone.utc), _at(11).astimezone(timezone.utc))


def test_slot_minutes_is_respected():
    """15 分钟粒度的资源：15/30 分钟合法，20 分钟既不整除粒度也不对齐分钟数。"""
    hours = _hours((4, time(9, 0), time(18, 0)))
    _validate(_at(10), _at(10, 15), hours=hours, slot_minutes=15, min_minutes=15)
    _validate(_at(10), _at(10, 30), hours=hours, slot_minutes=15, min_minutes=15)
    with pytest.raises(DurationInvalid):
        _validate(_at(10), _at(10, 20), hours=hours, slot_minutes=15, min_minutes=15)


def test_boundary_exactly_at_open_and_close():
    _validate(_at(9), _at(9, 30))  # 正好开门
    _validate(_at(17, 30), _at(18, 0))  # 正好关门


def test_dst_note_asia_shanghai_has_no_dst():
    """选 Asia/Shanghai 是有意的：没有夏令时，营业时间展开不用处理 23/25 小时的那一天。
    换到 America/New_York 就要处理 DST 切换日——记在 README 的已知限制里。"""
    assert ZoneInfo("Asia/Shanghai").utcoffset(datetime(2026, 7, 1)) == timedelta(hours=8)

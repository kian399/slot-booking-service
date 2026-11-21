"""创建预约。

这个函数是本项目的立论所在，所以把「谁保证互斥」写死在这里：

    INSERT 直接交给数据库，不做 SELECT 冲突检查。

`SELECT ... 有没有重叠` + `INSERT` 这两步之间，别的请求可以插进来（TOCTOU）。
真正挡住第二次插入的是 bookings 表上的排他约束：

    EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at,end_at,'[)') WITH &&)
    WHERE (status IN ('pending_payment','confirmed','checked_in'))

冲突时 PG 抛 SQLSTATE 23P01（exclusion_violation），这里翻译成 409 SLOT_TAKEN。

`CONFLICT_STRATEGY` 是为了做 A~E 五组对照实验（实施方案 3.11）而留的开关，
默认 exclude 就是生产实现。其余取值只在压测里用，**不要**在生产打开：

    naive       先查后插（无保护）        → 复现超卖
    row_lock    锁资源行（SELECT FOR UPDATE） → 正确但串行
    optimistic  资源行 version CAS + 重试    → 保护的是"同一行"，管不了区间
    exclude     排他约束（默认）
    redis_lock  Redis 锁 + 排他约束         → 锁只削峰，正确性仍靠约束
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import metrics
from app.config import settings
from app.exceptions import ResourceInactive, ResourceNotFound, SlotTaken
from app.models import Booking, BookingEvent, BookingStatus, BusinessHour, Outbox, Resource
from app.redis_client import redis_client
from app.services import delay_queue, idempotency, slots

logger = structlog.get_logger(__name__)

OVERLAP_RESOURCE_CONSTRAINT = "bookings_no_overlap_resource"
OVERLAP_USER_CONSTRAINT = "bookings_no_overlap_user"


def quote_amount(price_cents_per_hour: int, start_at: datetime, end_at: datetime) -> int:
    """按小时单价折算，向上取整到分（宁可多收一分也不要出现 0 元订单）。"""
    minutes = (end_at - start_at).total_seconds() / 60
    return math.ceil(price_cents_per_hour * minutes / 60)


def snapshot(booking: Booking) -> dict:
    """对外响应体 = 幂等快照，用同一个函数生成，避免两处格式漂移。"""
    return {
        "id": booking.id,
        "resource_id": booking.resource_id,
        "user_id": booking.user_id,
        "start_at": _iso(booking.start_at),
        "end_at": _iso(booking.end_at),
        "status": booking.status,
        "amount_cents": booking.amount_cents,
        "refunded_cents": booking.refunded_cents,
        "expires_at": _iso(booking.expires_at),
        "created_at": _iso(booking.created_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _constraint_name(exc: IntegrityError) -> str:
    """从 psycopg2 的异常里取出约束名。

    diag.constraint_name 是最可靠的来源；不是所有驱动版本都会填，
    所以再退一步匹配报错文本（PG 的文案很稳定：
    'conflicting key value violates exclusion constraint "bookings_no_overlap_resource"'）。
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", "") or ""
    if not name:
        raw = str(orig)
        for candidate in (OVERLAP_RESOURCE_CONSTRAINT, OVERLAP_USER_CONSTRAINT):
            if candidate in raw:
                return candidate
    return name


def _has_overlap(db: Session, resource_id: int, start_at: datetime, end_at: datetime) -> bool:
    """对照组 A 的那段「看起来没问题」的代码。生产中禁用（TOCTOU）。"""
    return (
        db.execute(
            select(Booking.id)
            .where(
                Booking.resource_id == resource_id,
                Booking.status.in_(BookingStatus.occupying()),
                Booking.start_at < end_at,
                Booking.end_at > start_at,
            )
            .limit(1)
        ).first()
        is not None
    )


def _optimistic_bump(db: Session, resource: Resource) -> None:
    """对照组 C：对资源行做 version CAS。

    注意它保护的是 resources 这一行，而真正的冲突发生在两条 booking 之间。
    所以即使 CAS 成功，两个并发事务仍然都能插入重叠的 booking——这个实验的意义
    就是证明「乐观锁解决不了区间互斥」。
    """
    for attempt in range(3):
        with db.begin_nested():
            result = db.execute(
                text("UPDATE resources SET version = version + 1 WHERE id = :id AND version = :v"),
                {"id": resource.id, "v": resource.version},
            )
        if result.rowcount:
            resource.version += 1
            return
        db.refresh(resource)
        time.sleep(0.001 * (attempt + 1))
    raise SlotTaken("资源版本冲突，请重试")


_LOCK_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@contextmanager
def _strategy_guard(
    db: Session, resource: Resource, start_at: datetime, end_at: datetime
) -> Iterator[None]:
    """按 CONFLICT_STRATEGY 在 INSERT 前后加不同保护，供五组对照实验复用同一份插入逻辑。"""
    strategy = settings.CONFLICT_STRATEGY
    lock_token: str | None = None

    if strategy == "naive":
        if _has_overlap(db, resource.id, start_at, end_at):
            metrics.inc("booking_conflict_total")
            raise SlotTaken("该时段已被占用", resource_id=resource.id)
        # 模拟真实业务在「判断」和「写入」之间的耗时（算价/校验/写审计）
        if settings.NAIVE_WINDOW_MS > 0:
            time.sleep(settings.NAIVE_WINDOW_MS / 1000)
    elif strategy == "row_lock":
        # 锁的目的是让「查冲突」和「插入」成为一个原子动作。
        # 只锁不查 = 锁了个寂寞：并发的第二个事务拿到锁之后照样插入，
        # 因为它根本没做任何判断（实测 384 笔全部成交，比 A 组还惨）。
        db.execute(text("SELECT id FROM resources WHERE id = :id FOR UPDATE"), {"id": resource.id})
        if _has_overlap(db, resource.id, start_at, end_at):
            metrics.inc("booking_conflict_total")
            raise SlotTaken("该时段已被占用", resource_id=resource.id)
    elif strategy == "optimistic":
        _optimistic_bump(db, resource)
    elif strategy == "redis_lock":
        key = f"lock:resource:{resource.id}"
        lock_token = uuid.uuid4().hex
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if redis_client.set(key, lock_token, nx=True, px=3000):
                break
            time.sleep(0.002)
        else:
            raise SlotTaken("资源繁忙，请重试", resource_id=resource.id)

    try:
        yield
    finally:
        if lock_token is not None:
            try:
                redis_client.eval(_LOCK_RELEASE_LUA, 1, f"lock:resource:{resource.id}", lock_token)
            except Exception:  # noqa: BLE001 锁到期自动释放，释放失败不影响正确性
                logger.warning("lock_release_failed", resource_id=resource.id)


def create_booking(
    db: Session,
    *,
    user_id: int,
    resource_id: int,
    start_at: datetime,
    end_at: datetime,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Booking, bool]:
    """返回 (订单, 是否新建)。False 表示这是一次幂等重放。"""
    now = now or datetime.now(timezone.utc)

    resource = db.get(Resource, resource_id)
    if resource is None:
        raise ResourceNotFound("资源不存在", resource_id=resource_id)
    if not resource.active:
        raise ResourceInactive("资源已下架，无法预约", resource_id=resource_id)

    hours = slots.to_business_hours(
        db.execute(select(BusinessHour).where(BusinessHour.resource_id == resource_id))
        .scalars()
        .all()
    )
    slots.validate_slot(
        resource=resource,
        hours=hours,
        start_at=start_at,
        end_at=end_at,
        now=now,
        max_advance_days=settings.MAX_ADVANCE_DAYS,
    )

    request_hash = idempotency.hash_request(
        {
            "resource_id": resource_id,
            "start_at": start_at.astimezone(timezone.utc).isoformat(),
            "end_at": end_at.astimezone(timezone.utc).isoformat(),
        }
    )
    idem = None
    if idempotency_key:
        idem = idempotency.begin(
            db, user_id=user_id, key=idempotency_key, request_hash=request_hash
        )
        if idem.replay:
            metrics.inc("booking_idempotent_replay_total")
            booking = db.get(Booking, idem.record.booking_id)
            if booking is None:  # 理论上不可能，除非有人手工删了单
                raise SlotTaken("订单已不存在，请换一个幂等键重试")
            return booking, False

    booking = Booking(
        resource_id=resource_id,
        user_id=user_id,
        start_at=start_at,
        end_at=end_at,
        status=BookingStatus.PENDING_PAYMENT.value,
        amount_cents=quote_amount(resource.price_cents, start_at, end_at),
        idem_key=idempotency_key,
        expires_at=now + timedelta(minutes=settings.HOLD_MINUTES),
    )

    with _strategy_guard(db, resource, start_at, end_at):
        db.add(booking)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            name = _constraint_name(exc)
            if name == OVERLAP_RESOURCE_CONSTRAINT:
                metrics.inc("booking_conflict_total")
                raise SlotTaken(
                    "该时段已被占用",
                    resource_id=resource_id,
                    start_at=_iso(start_at),
                    end_at=_iso(end_at),
                ) from exc
            if name == OVERLAP_USER_CONSTRAINT:
                metrics.inc("user_conflict_total")
                raise SlotTaken("你在该时段已有其它预约", user_id=user_id) from exc
            raise

    db.add(
        BookingEvent(
            booking_id=booking.id,
            from_status=None,
            to_status=booking.status,
            actor="user",
            reason="created",
        )
    )
    # 事务性发件箱：和 booking 同一事务写入，事件不会因为"先提交后发消息"而丢
    db.add(
        Outbox(
            event_type="BookingCreated",
            aggregate_id=booking.id,
            payload=snapshot(booking),
        )
    )
    if idem is not None:
        idempotency.finish(
            db,
            idem.record,
            booking_id=booking.id,
            response_status=201,
            response_body=snapshot(booking),
        )

    # DB 提交之后才写延迟队列：先写队列再提交，一旦提交失败，队列里就留了一个
    # 指向已回滚订单的幽灵 id（worker 每次都会空转）。
    db.commit()
    metrics.inc("booking_created_total")
    delay_queue.enqueue(booking.id, booking.expires_at)
    logger.info(
        "booking_created",
        booking_id=booking.id,
        resource_id=resource_id,
        user_id=user_id,
        strategy=settings.CONFLICT_STRATEGY,
    )
    return booking, True

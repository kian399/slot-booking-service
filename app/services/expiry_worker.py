"""超时释放 worker。

两层循环：
- 快循环（默认 1s）：从 Redis ZSET 取到期 id，逐个在 DB 做 CAS，成功释放后触发候补补位；
- 慢循环（默认 60s）：直接扫 DB 里超期但仍是 pending_payment 的单，兜住 Redis 丢事件。

为什么两层都要：Redis 只是调度器。只要 DB 兜底在，即使整个 ZSET 被清空、
或者换台机器没带持久化，订单也不会永远卡在 pending_payment 里占着槽位。
（兜底扫描只做释放，不触发补位——补位要靠精确的时段信息，适合走快循环。）
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import structlog

from app import metrics
from app.config import settings
from app.db import SessionLocal
from app.models import Booking
from app.services import delay_queue, lifecycle, outbox_worker, waitlist

logger = structlog.get_logger(__name__)

outbox_loop = outbox_worker.outbox_loop


def run_expiry_once(batch_size: int | None = None) -> int:
    """同步执行一批释放。放线程池里跑，避免同步 IO 卡住事件循环。"""
    batch = batch_size or settings.EXPIRY_BATCH_SIZE
    due = delay_queue.pop_due(batch)
    if not due:
        return 0

    released = 0
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        for booking_id in due:
            try:
                booking = db.get(Booking, booking_id)
                if booking is None:
                    continue
                resource_id, start_at, end_at = (
                    booking.resource_id,
                    booking.start_at,
                    booking.end_at,
                )
                if lifecycle.expire_one(db, booking_id, now=now):
                    released += 1
                    # 释放即补位：给队首候补者建一张 5 分钟 hold 的单
                    waitlist.offer_next(
                        db,
                        resource_id=resource_id,
                        start_at=start_at,
                        end_at=end_at,
                        now=now,
                    )
            except Exception:  # noqa: BLE001 单个 id 出错不能拖垮整批
                db.rollback()
                logger.warning("expire_one_failed", booking_id=booking_id, exc_info=True)
    finally:
        db.close()

    if released:
        logger.info("expiry_batch_done", scanned=len(due), released=released)
    # 扫描了但没释放 = 大部分已被支付/取消，是正常现象（ZSET 里的悬空 id）
    metrics.inc("expiry_scanned_total", value=float(len(due)))
    return released


def run_sweep_once() -> int:
    db = SessionLocal()
    try:
        return len(lifecycle.sweep_expired(db, limit=500))
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("expiry_sweep_failed", exc_info=True)
        return 0
    finally:
        db.close()


async def expiry_loop() -> None:
    while True:
        await asyncio.sleep(settings.EXPIRY_POLL_INTERVAL)
        try:
            await asyncio.to_thread(run_expiry_once)
        except Exception:  # noqa: BLE001 后台任务不能因为一次失败就退出
            logger.exception("expiry_loop_failed")


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(settings.EXPIRY_SWEEP_INTERVAL)
        try:
            await asyncio.to_thread(run_sweep_once)
        except Exception:  # noqa: BLE001
            logger.exception("sweep_loop_failed")


@contextlib.asynccontextmanager
async def lifespan_tasks(app):  # noqa: ARG001 FastAPI 会把 app 实例传进来
    """FastAPI 的 lifespan 签名是 `async (app) -> AsyncIterator`。

    少写这个参数时 uvicorn 会在启动阶段直接抛
    `TypeError: lifespan_tasks() takes 0 positional arguments but 1 was given`，
    服务起不来——而 TestClient 不进 `with` 是不会触发 lifespan 的，
    所以集成测试全绿也发现不了。这条教训记在 docs 同级的问题台账里。
    """
    tasks: list[asyncio.Task] = []
    if settings.EXPIRY_WORKER_ENABLED:
        tasks.append(asyncio.create_task(expiry_loop(), name="expiry_loop"))
    if settings.EXPIRY_SWEEP_ENABLED:
        tasks.append(asyncio.create_task(sweep_loop(), name="sweep_loop"))
    if settings.OUTBOX_WORKER_ENABLED:
        # 事务性发件箱的投递器：把 outbox 表里未发布的事件推到 Redis Stream
        tasks.append(asyncio.create_task(outbox_loop(), name="outbox_loop"))
    logger.info("background_tasks_started", count=len(tasks))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

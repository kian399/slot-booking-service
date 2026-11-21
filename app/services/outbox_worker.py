"""事务性发件箱（Outbox）投递。

要解决的问题：业务事务写库成功、但发消息失败，事件就永久丢了。
「先写库再 XADD」不行——中间崩了消息就没了；「先 XADD 再写库」更糟，库里没这笔单却发了事件。

做法：事件在**业务事务内**写进 outbox 表（见 booking.create_booking / lifecycle 里的 _outbox），
再由这个小 worker 轮询 outbox 投递到 Redis Stream，投完标记 published_at。

投递语义是**至少一次**（at-least-once），不是恰好一次：
XADD 成功、更新 published_at 前崩了 → 重启后会重投同一条。
所以消费端必须按事件 id 幂等去重——这是这套方案的代价，也是它成立的前提。

多实例安全：`FOR UPDATE SKIP LOCKED` 让多个 worker 同时跑不会抢同一条。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.db import SessionLocal
from app.models import Outbox
from app.redis_client import redis_client

logger = structlog.get_logger(__name__)

STREAM_KEY = "events:booking"
OUTBOX_POLL_INTERVAL = 1.0
OUTBOX_BATCH_SIZE = 200


def publish_once(db: Session, *, batch: int = OUTBOX_BATCH_SIZE) -> int:
    """投递一批未发布事件，返回成功条数。

    顺序是「先 XADD，再标 published_at」——反过来的话，标记成功但投递失败，
    这条事件就永远不会再被捞到（静默丢失）。
    """
    rows = (
        db.execute(
            select(Outbox)
            .where(Outbox.published_at.is_(None))
            .order_by(Outbox.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not rows:
        db.rollback()
        return 0

    published = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            redis_client.xadd(
                STREAM_KEY,
                {
                    "outbox_id": row.id,
                    "event_type": row.event_type,
                    "aggregate_id": row.aggregate_id,
                    "payload": json.dumps(row.payload, ensure_ascii=False),
                    "occurred_at": row.created_at.isoformat() if row.created_at else "",
                },
            )
        except Exception:  # noqa: BLE001 Redis 抖动就整批不标记，下一轮重试
            logger.warning("outbox_publish_failed", outbox_id=row.id, exc_info=True)
            db.rollback()
            break
        row.published_at = now
        published += 1

    db.commit()
    if published:
        metrics.inc("outbox_published_total", value=float(published))
        logger.info("outbox_batch_published", count=published)
    return published


async def outbox_loop() -> None:
    while True:
        await asyncio.sleep(OUTBOX_POLL_INTERVAL)
        try:
            await asyncio.to_thread(_run_once)
        except Exception:  # noqa: BLE001 后台任务不能因一次失败退出
            logger.exception("outbox_loop_failed")


def _run_once() -> int:
    db = SessionLocal()
    try:
        return publish_once(db)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("outbox_run_failed", exc_info=True)
        return 0
    finally:
        db.close()


@contextlib.asynccontextmanager
async def outbox_lifespan():
    task = asyncio.create_task(outbox_loop(), name="outbox_loop")
    logger.info("outbox_loop_started")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

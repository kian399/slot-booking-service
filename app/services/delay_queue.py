"""延迟队列（Redis ZSET）。

为什么是 ZSET 而不是「key 过期通知」：
- Redis 的 keyspace notification 是 fire-and-forget，订阅端掉线就永久丢；
- 过期 key 的删除本身是惰性+采样的，到期时刻不精确，也不能批量取；
- ZSET 可以按 score 范围批量取、可以查积压深度（ZCARD）、可以手动重新入队。

但 Redis 在这套设计里**只是调度器，不是真相**：
每个到期事件最终都要在 PG 上做一次 CAS（WHERE status='pending_payment'），
Redis 里多出来的 id 最多造成一次空转，不会造成错误释放；
Redis 丢了的 id 由 DB 兜底扫描补上（EXPIRY_SWEEP_ENABLED）。
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from app import metrics
from app.redis_client import redis_client

logger = structlog.get_logger(__name__)

HOLD_ZSET = "hold:expiry"

# 取一批到期的 id 并从 ZSET 里摘掉，两个动作必须原子：
# 否则两个 worker 会拿到同一批 id 重复处理（虽然 DB 的 CAS 能兜住，但白跑一遍）。
_POP_DUE_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
if #due > 0 then
  redis.call('ZREM', KEYS[1], unpack(due))
end
return due
"""


def enqueue(booking_id: int, expires_at: datetime) -> bool:
    """把订单挂到延迟队列。失败只记日志不抛：DB 才是真相，兜底扫描会补。"""
    try:
        redis_client.zadd(HOLD_ZSET, {str(booking_id): expires_at.timestamp()})
        return True
    except Exception:  # noqa: BLE001
        logger.warning("hold_enqueue_failed", booking_id=booking_id, exc_info=True)
        metrics.inc("hold_enqueue_failed_total")
        return False


def remove(booking_id: int) -> None:
    """支付/取消之后把残留 id 摘掉，减少 worker 的空转。"""
    try:
        redis_client.zrem(HOLD_ZSET, str(booking_id))
    except Exception:  # noqa: BLE001
        logger.warning("hold_remove_failed", booking_id=booking_id, exc_info=True)


def pop_due(limit: int) -> list[int]:
    now = time.time()
    try:
        raw = redis_client.eval(_POP_DUE_LUA, 1, HOLD_ZSET, now, limit)
    except Exception:  # noqa: BLE001
        logger.warning("hold_pop_failed", exc_info=True)
        return []
    return [int(x) for x in raw]


def depth() -> int:
    try:
        return int(redis_client.zcard(HOLD_ZSET))
    except Exception:  # noqa: BLE001
        return -1

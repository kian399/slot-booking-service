"""幂等键。

语义（对齐 Stripe 的 Idempotency-Key）：
- 同一 (user_id, key) + 同一请求体 → 返回**第一次**的结果，不重复下单；
- 同一 key + 不同请求体 → 409 IDEMPOTENCY_CONFLICT；
- 并发两个同 key 请求 → 一个建单，另一个被唯一索引挡住，回读后返回同一订单。

相对实施方案 3.9 的改动：方案用的是 bookings 上的
`UNIQUE(user_id, idem_key) WHERE idem_key IS NOT NULL` 部分索引。
那只能"挡住重复"，挡不住"重试时要拿到原响应"，也挡不住"同 key 换 payload"。
所以单独一张表存请求指纹 + 响应快照。

实现上的两个关键点：
1) 用 SAVEPOINT（begin_nested）捕获唯一冲突。直接 flush 失败会把外层事务打成 aborted，
   整个请求都得重来；SAVEPOINT 只回滚这一小段，外层还能继续读。
2) 并发时 PG 的唯一索引会让后到的 INSERT 等前一个事务结束——这正好是我们要的：
   等它提交，然后冲突回读；而不是两边都以为自己是第一个。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import IdempotencyInFlight, IdempotencyMismatch
from app.models import IdempotencyRecord


def hash_request(payload: dict) -> str:
    """稳定指纹：键排序 + 紧凑分隔符，避免 dict 顺序不同导致同一个请求算出不同哈希。"""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class IdemBegin:
    record: IdempotencyRecord
    replay: bool


def begin(db: Session, *, user_id: int, key: str, request_hash: str) -> IdemBegin:
    """占坑或拿回放。返回 replay=True 表示这是重复请求，调用方直接回响应快照。"""
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.user_id == user_id,
        IdempotencyRecord.key == key,
    )
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return _replay_or_conflict(existing, request_hash)

    record = IdempotencyRecord(user_id=user_id, key=key, request_hash=request_hash)
    # 顺序很关键：**先建 SAVEPOINT，再 add，再 flush**。
    # 如果先 db.add() 再 with db.begin_nested()，begin_nested() 在发出 SAVEPOINT 之前
    # 会先把 pending 对象 flush 掉——那条 INSERT 就跑在 SAVEPOINT 外面，
    # 唯一冲突会把整个 session 打成 aborted，后面连 SELECT 都做不了
    # （实测报 PendingRollbackError: This Session's transaction has been rolled back）。
    savepoint = db.begin_nested()
    try:
        db.add(record)
        db.flush()
        savepoint.commit()
    except IntegrityError:
        # 并发同 key：另一个请求先插进去了。回滚到 SAVEPOINT，外层事务还能用。
        savepoint.rollback()
        row = db.execute(stmt).scalar_one_or_none()
        if row is None:
            # 极端情况：对方回滚了，这一行又消失了。让客户端重试一次比瞎猜安全。
            raise IdempotencyInFlight("相同幂等键的请求正在处理中，请稍后重试") from None
        return _replay_or_conflict(row, request_hash)
    return IdemBegin(record=record, replay=False)


def _replay_or_conflict(record: IdempotencyRecord, request_hash: str) -> IdemBegin:
    if record.request_hash != request_hash:
        raise IdempotencyMismatch("同一个 Idempotency-Key 不能用于不同的请求内容")
    if record.response_status is None:
        # 首个请求还在事务中（或已失败但记录被复用），拿不到可回放的响应
        raise IdempotencyInFlight("相同幂等键的请求正在处理中，请稍后重试")
    return IdemBegin(record=record, replay=True)


def finish(
    db: Session,
    record: IdempotencyRecord,
    *,
    booking_id: int,
    response_status: int,
    response_body: dict,
) -> None:
    record.booking_id = booking_id
    record.response_status = response_status
    record.response_body = response_body
    db.flush()

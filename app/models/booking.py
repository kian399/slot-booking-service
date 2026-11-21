from __future__ import annotations

import enum
from datetime import datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BookingStatus(enum.StrEnum):
    """七态状态机（对齐实施方案 3.7）。

    释放资源的只有 4 条边：pending→expired、pending→cancelled、confirmed→cancelled、
    confirmed→no_show。checked_in 之后时段已经被消费，completed 是时间自然过去的归档态，
    它们都不需要"释放"，但 checked_in 必须继续占位（否则同一时段会被二次卖出）。
    """

    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    NO_SHOW = "no_show"

    @classmethod
    def occupying(cls) -> tuple[str, ...]:
        """占用槽位的状态白名单。

        这个元组是**唯一真相**：迁移里的 EXCLUDE ... WHERE 子句、availability 的差集、
        测试断言全部引用它。少写一个 → 该状态不占位（超卖）；多写一个 → 终态不释放（卖不出去）。
        """
        return (cls.PENDING_PAYMENT.value, cls.CONFIRMED.value, cls.CHECKED_IN.value)

    @classmethod
    def terminal(cls) -> tuple[str, ...]:
        return (cls.COMPLETED.value, cls.CANCELLED.value, cls.EXPIRED.value, cls.NO_SHOW.value)


class PaymentKind(enum.StrEnum):
    PAY = "pay"
    REFUND = "refund"


class PaymentStatus(enum.StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class Resource(Base):
    """一个 resource 就是一个「同时只能被一个人占用」的单元。

    capacity 恒为 1：多座位不要做成 capacity=5，而是建 5 条 resource（座位 A/B/C…）。
    排他约束表达的是区间独占；一旦引入 capacity>1，正确性就得靠配额计数行，
    那是另一套并发模型，会把「数据库保证」退化成「应用层保证」，本项目的立论就没了。
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="room")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    slot_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    min_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    max_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=240)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 只服务于压测对照 C 组（乐观锁）：真实业务里「预约冲突」不是这一行的写冲突，
    # 这个列的存在正好用来证明"版本号救不了区间互斥"。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("slot_minutes > 0", name="ck_resources_slot_minutes"),
        CheckConstraint("price_cents >= 0", name="ck_resources_price"),
        CheckConstraint("min_minutes <= max_minutes", name="ck_resources_duration_range"),
    )


class BusinessHour(Base):
    """每周营业时段模板。同一 weekday 允许多段（09-12、14-18 中间午休）。"""

    __tablename__ = "business_hours"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_bh_weekday"),
        CheckConstraint("open_time < close_time", name="ck_bh_time_order"),
        UniqueConstraint("resource_id", "weekday", "open_time", name="uq_bh_resource_day_open"),
        Index("ix_bh_resource_weekday", "resource_id", "weekday"),
    )


class Booking(Base):
    """预约单 = 一个时间区间的占用权。

    两条排他约束（迁移 0001 里创建）：
      bookings_no_overlap_resource  -- 同一资源，区间不允许重叠
      bookings_no_overlap_user      -- 同一用户，不允许同时占两个资源

    区间用 `tstzrange(start_at, end_at, '[)')` 生成：半开区间让 14:00-16:00 与 16:00-18:00
    可以连排，用闭区间会白丢每个整点。
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BookingStatus.PENDING_PAYMENT.value
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idem_key: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_bookings_period"),
        CheckConstraint("amount_cents >= 0", name="ck_bookings_amount"),
        CheckConstraint(
            "status IN ('pending_payment','confirmed','checked_in',"
            "'completed','cancelled','expired','no_show')",
            name="ck_bookings_status",
        ),
        # 兜底扫描：WHERE status='pending_payment' AND expires_at <= now()
        Index("ix_bookings_pending_expire", "status", "expires_at"),
        Index("ix_bookings_user_created", "user_id", "created_at"),
    )


class BookingEvent(Base):
    """状态迁移审计：谁、何时、从什么到什么。面试讲"事件溯源"的素材。"""

    __tablename__ = "booking_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_events_booking", "booking_id", "created_at"),)


class Payment(Base):
    """支付/退款流水。退款也是一条记录（kind=refund），对账时按 kind 汇总。

    trade_no 唯一：真实网关的流水号天然幂等，重复回调不会记两笔。
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False
    )
    trade_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="success")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('pay','refund')", name="ck_payments_kind"),
        CheckConstraint("status IN ('success','failed')", name="ck_payments_status"),
        Index("ix_payments_booking", "booking_id"),
    )


class Waitlist(Base):
    """候补队列：抢不到的人排队，有人取消时补位（hold 5 分钟）。"""

    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
    offered_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting','offered','converted','cancelled')", name="ck_waitlist_status"
        ),
        Index("ix_waitlist_slot", "resource_id", "status", "created_at"),
    )


class Outbox(Base):
    """事务性发件箱：和 bookings 同一事务写入，保证领域事件不丢（加分项 12 的落点）。"""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # 部分索引：投递队列只关心未发布的行，published 之后不进索引
        Index(
            "ix_outbox_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class IdempotencyRecord(Base):
    """幂等键记录。

    实施方案 3.9 用的是 `UNIQUE(user_id, idem_key)` 部分索引——那只能"挡住重复"，
    挡不住"重试时拿到原响应"，也挡不住"同 key 换 payload"。所以单独一张表存
    请求指纹 + 响应快照，这是相对方案的一处增强。
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_id: Mapped[int | None] = mapped_column(BigInteger)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_idem_user_key"),
        Index("ix_idem_created", "created_at"),
    )

"""初始化表结构 + 两条排他约束

Revision ID: 0001_init
Revises:
Create Date: 2026-11-19

本文件里只有两段 SQL 是真正重要的，其余都是普通建表：

    CREATE EXTENSION IF NOT EXISTS btree_gist;

    ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap_resource
    EXCLUDE USING gist (
        resource_id WITH =,
        tstzrange(start_at, end_at, '[)') WITH &&
    ) WHERE (status IN ('pending_payment','confirmed','checked_in'));

为什么是这么写的：

1) 为什么是 EXCLUDE 而不是 UNIQUE
   UNIQUE(resource_id, start_at) 只能挡住「起点完全相同」的两次预约。
   08:00-09:00 和 08:30-09:30 的 start_at 不同，UNIQUE 放行，照样超卖。
   冲突的本质是**区间相交**，只有范围类型 + && 运算符能表达。

2) 为什么需要 btree_gist
   GiST 索引原生只支持范围/几何类型。resource_id 是 bigint，要在同一个 GiST
   索引里用 = 比较，必须由 btree_gist 提供 bigint 的 GiST opclass。
   漏了它，建表时会报 "data type bigint has no default operator class for access method gist"。

3) 为什么区间用 '[)'
   左闭右开。08:00-09:00 与 09:00-10:00 不相交，可以连排；
   用 '[]' 会让首尾相接的两个时段互相冲突，等于每个整点浪费一个槽位。

4) 为什么带 WHERE
   只让「占位状态」参与约束。expired / cancelled / no_show 必须立刻把时段让出来，
   否则取消一次就永久损失一个槽位。白名单与 app/models/booking.py 的
   BookingStatus.occupying() 必须一致，测试里有专门一条盯着它。

5) 为什么要两条约束
   第一条防「一槽两卖」；第二条防「同一个人同时在两个资源」——
   后者是纯业务规则，数据库层面同样只能靠约束保证（应用层查一次再插同样有 TOCTOU）。

6) 代价
   每次 INSERT 都要在 GiST 索引上做冲突判定。实测（docs/benchmark.md）：
   不冲突路径插入耗时 ~0.4ms → ~0.6ms，冲突路径直接返回 409。
   换来 0 超卖，且不需要任何分布式锁。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OCCUPYING = "'pending_payment','confirmed','checked_in'"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("slot_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("min_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("max_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("slot_minutes > 0", name="ck_resources_slot_minutes"),
        sa.CheckConstraint("price_cents >= 0", name="ck_resources_price"),
        sa.CheckConstraint("min_minutes <= max_minutes", name="ck_resources_duration_range"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "business_hours",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=False),
        sa.Column("close_time", sa.Time(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_bh_weekday"),
        sa.CheckConstraint("open_time < close_time", name="ck_bh_time_order"),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", "weekday", "open_time", name="uq_bh_resource_day_open"),
    )
    op.create_index("ix_bh_resource_weekday", "business_hours", ["resource_id", "weekday"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("refunded_cents", sa.Integer(), nullable=False),
        sa.Column("idem_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("end_at > start_at", name="ck_bookings_period"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_bookings_amount"),
        sa.CheckConstraint(
            "status IN ('pending_payment','confirmed','checked_in',"
            "'completed','cancelled','expired','no_show')",
            name="ck_bookings_status",
        ),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bookings_pending_expire", "bookings", ["status", "expires_at"])
    op.create_index("ix_bookings_user_created", "bookings", ["user_id", "created_at"])
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bookings_idem ON bookings (user_id, idem_key)
        WHERE idem_key IS NOT NULL
        """
    )

    # ---- 核心约束 1：同一资源，区间不允许重叠 ----
    op.execute(
        f"""
        ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap_resource
        EXCLUDE USING gist (
            resource_id WITH =,
            tstzrange(start_at, end_at, '[)') WITH &&
        ) WHERE (status IN ({OCCUPYING}))
        """
    )

    # ---- 核心约束 2：同一用户，不允许同时占两个资源 ----
    op.execute(
        f"""
        ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap_user
        EXCLUDE USING gist (
            user_id WITH =,
            tstzrange(start_at, end_at, '[)') WITH &&
        ) WHERE (status IN ({OCCUPYING}))
        """
    )

    op.create_table(
        "booking_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_booking", "booking_events", ["booking_id", "created_at"])

    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("booking_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_no", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('pay','refund')", name="ck_payments_kind"),
        sa.CheckConstraint("status IN ('success','failed')", name="ck_payments_status"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trade_no"),
    )
    op.create_index("ix_payments_booking", "payments", ["booking_id"])

    op.create_table(
        "waitlist",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("offered_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('waiting','offered','converted','cancelled')", name="ck_waitlist_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_waitlist_slot", "waitlist", ["resource_id", "status", "created_at"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("booking_id", sa.BigInteger(), nullable=True),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_idem_user_key"),
    )
    op.create_index("ix_idem_created", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("outbox")
    op.drop_table("waitlist")
    op.drop_table("payments")
    op.drop_table("booking_events")
    op.drop_table("bookings")
    op.drop_table("business_hours")
    op.drop_table("resources")
    # 扩展不删：同库可能还有别的表在用 btree_gist

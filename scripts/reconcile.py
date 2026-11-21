"""订单 / 支付流水对账。

为什么这件事值得单独写一个脚本：
「不超卖」只证明没卖重，不证明**钱没算错**。退多少是按时间阶梯算出来的，
只要退款规则、状态流转、支付流水三者有任何一处不一致，账就对不上，
而这种错在功能测试里完全看不出来（用户不会告诉你多退了他 20 块）。

对账的核心思路：**应退金额是可以重算的**。
退款策略被写成纯函数（app/services/refund.py），任何时刻都能用
「订单金额 + 开始时间 + 取消时间」把"应该退多少"重新算一遍，
再和 payments 表里"实际退了多少"比。所以对账不需要依赖当时的历史规则。

四类差异：
    D1 已取消且有支付，但退款不足        → 用户的钱没退（投诉来源）
    D2 退款金额超过应退                  → 多退（直接资损）
    D3 有支付流水，但订单不在已支付状态   → 状态丢失（支付回调丢了？）
    D4 有退款流水，但订单没取消          → 幽灵退款

用法：
    uv run python scripts/reconcile.py                 # 全量
    uv run python scripts/reconcile.py --since 2026-11-01
    uv run python scripts/reconcile.py --json          # 机器可读，方便接告警
    uv run python scripts/reconcile.py --demo          # 制造两条差异，验证对账能查出来

退出码：0 = 账平；1 = 有差异（可以直接放进 cron / CI 当告警）。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Booking,
    BookingEvent,
    BookingStatus,
    BusinessHour,
    Payment,
    PaymentKind,
    Resource,
)
from app.services import refund as refund_rules  # noqa: E402

TZ = timezone(timedelta(hours=8))

D1 = "D1_REFUND_MISSING"
D2 = "D2_REFUND_EXCESS"
D3 = "D3_STATUS_LOST"
D4 = "D4_PHANTOM_REFUND"

KIND_LABEL = {
    D1: "已取消但退款不足",
    D2: "退款超过应退金额",
    D3: "有支付流水但订单状态不符",
    D4: "有退款流水但订单未取消",
}


@dataclass
class Discrepancy:
    kind: str
    booking_id: int
    expected_cents: int
    actual_cents: int
    detail: str


@dataclass
class Report:
    scanned: int = 0
    with_payment: int = 0
    with_refund: int = 0
    expected_refund_total: int = 0
    actual_refund_total: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        return not self.discrepancies

    def by_kind(self) -> dict[str, list[Discrepancy]]:
        out: dict[str, list[Discrepancy]] = {}
        for d in self.discrepancies:
            out.setdefault(d.kind, []).append(d)
        return out


def _load_rows(db: Session, since: datetime | None):
    stmt = select(Booking).order_by(Booking.id)
    if since is not None:
        stmt = stmt.where(Booking.created_at >= since)
    bookings = db.execute(stmt).scalars().all()

    # 一次把所有流水读进内存再聚合：对账是离线任务，几万行不值得为它写复杂 SQL
    payments: dict[int, list[Payment]] = {}
    for p in db.execute(select(Payment)).scalars():
        payments.setdefault(p.booking_id, []).append(p)
    return bookings, payments


def reconcile(db: Session, *, since: datetime | None = None) -> Report:
    bookings, payments = _load_rows(db, since)
    report = Report(scanned=len(bookings))

    for b in bookings:
        rows = payments.get(b.id, [])
        paid = sum(
            p.amount_cents
            for p in rows
            if p.kind == PaymentKind.PAY.value and p.status == "success"
        )
        refunded = sum(
            p.amount_cents
            for p in rows
            if p.kind == PaymentKind.REFUND.value and p.status == "success"
        )
        if paid:
            report.with_payment += 1
        if refunded:
            report.with_refund += 1

        # 应退：只有「真的收过钱、且最终取消」的单才有应退
        if b.status == BookingStatus.CANCELLED.value and paid > 0:
            closed_at = b.closed_at or datetime.now(timezone.utc)
            expected, _ratio, _label = refund_rules.compute_refund(
                b.amount_cents, b.start_at, closed_at
            )
        else:
            expected = 0

        report.expected_refund_total += expected
        report.actual_refund_total += refunded

        if expected > refunded:
            report.discrepancies.append(
                Discrepancy(
                    kind=D1,
                    booking_id=b.id,
                    expected_cents=expected,
                    actual_cents=refunded,
                    detail=f"应退 {expected} 分，实退 {refunded} 分，差 {expected - refunded} 分",
                )
            )
        elif refunded > expected:
            report.discrepancies.append(
                Discrepancy(
                    kind=D2,
                    booking_id=b.id,
                    expected_cents=expected,
                    actual_cents=refunded,
                    detail=f"应退 {expected} 分，实退 {refunded} 分，多退 {refunded - expected} 分",
                )
            )

        if paid > 0 and b.status in (
            BookingStatus.PENDING_PAYMENT.value,
            BookingStatus.EXPIRED.value,
        ):
            report.discrepancies.append(
                Discrepancy(
                    kind=D3,
                    booking_id=b.id,
                    expected_cents=paid,
                    actual_cents=0,
                    detail=f"状态为 {b.status} 却有 {paid} 分支付流水",
                )
            )

        if refunded > 0 and b.status != BookingStatus.CANCELLED.value:
            report.discrepancies.append(
                Discrepancy(
                    kind=D4,
                    booking_id=b.id,
                    expected_cents=0,
                    actual_cents=refunded,
                    detail=f"状态为 {b.status} 却有 {refunded} 分退款流水",
                )
            )

    return report


def render_text(report: Report) -> str:
    lines = [
        "=" * 72,
        "订单 / 支付流水对账报告",
        "=" * 72,
        f"扫描订单      : {report.scanned}",
        f"有支付流水    : {report.with_payment}",
        f"有退款流水    : {report.with_refund}",
        f"应退合计(分)  : {report.expected_refund_total}",
        f"实退合计(分)  : {report.actual_refund_total}",
        f"差异条数      : {len(report.discrepancies)}",
        "-" * 72,
    ]
    if report.balanced:
        lines.append("账平：没有发现差异。")
    else:
        for kind, items in report.by_kind().items():
            lines.append(f"[{kind}] {KIND_LABEL[kind]}  {len(items)} 条")
            for d in items:
                lines.append(f"  - booking #{d.booking_id}: {d.detail}")
        lines.append("-" * 72)
        delta = report.actual_refund_total - report.expected_refund_total
        lines.append(f"合计差异金额: {delta} 分")
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- demo


def _make_demo_data(db: Session) -> dict[str, int]:
    """造两张"本来正常"的单，然后分别破坏退款流水，用于验证对账能查出来。"""
    from app.services import booking as booking_service
    from app.services import lifecycle

    suffix = uuid.uuid4().hex[:6]
    res = Resource(
        name=f"recon-{suffix}",
        kind="room",
        timezone="Asia/Shanghai",
        slot_minutes=30,
        min_minutes=30,
        max_minutes=240,
        price_cents=20_000,
    )
    db.add(res)
    db.flush()
    for wd in range(7):
        db.add(
            BusinessHour(
                resource_id=res.id,
                weekday=wd,
                open_time=datetime.min.time().replace(hour=8),
                close_time=datetime.min.time().replace(hour=20),
            )
        )
    db.commit()

    base = (datetime.now(TZ) + timedelta(days=5)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    ids = {}
    for tag, offset in (("missing", 0), ("excess", 2)):
        b, _ = booking_service.create_booking(
            db,
            user_id=900_000 + offset,
            resource_id=res.id,
            start_at=base + timedelta(hours=offset),
            end_at=base + timedelta(hours=offset + 1),
        )
        lifecycle.pay(db, b.id)
        lifecycle.cancel(db, b.id)
        ids[tag] = b.id

    # 破坏 1：删掉退款流水 → 应退未退（D1）
    db.execute(
        delete(Payment).where(
            Payment.booking_id == ids["missing"], Payment.kind == "refund"
        )
    )
    # 破坏 2：多插一笔退款 → 多退（D2）
    db.add(
        Payment(
            booking_id=ids["excess"],
            trade_no=f"demo-extra-{suffix}",
            kind=PaymentKind.REFUND.value,
            amount_cents=5000,
            status="success",
        )
    )
    db.commit()
    ids["resource"] = res.id
    return ids


def _cleanup_demo_data(db: Session, resource_id: int) -> None:
    """demo 只负责证明"能查出来"，跑完必须把自己造的脏数据收走。

    不收走的话，下一次全量对账会一直报这两条差异，人就会开始忽略告警——
    对账最怕的不是漏报，而是误报多了之后没人看。
    """
    booking_ids = select(Booking.id).where(Booking.resource_id == resource_id)
    db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
    db.execute(delete(BookingEvent).where(BookingEvent.booking_id.in_(booking_ids)))
    db.execute(delete(Booking).where(Booking.resource_id == resource_id))
    db.execute(delete(BusinessHour).where(BusinessHour.resource_id == resource_id))
    db.execute(delete(Resource).where(Resource.id == resource_id))
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="只查该日期之后创建的订单，YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--demo", action="store_true", help="先制造两条差异再对账，用于验证")
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=TZ)

    db = SessionLocal()
    try:
        ids = None
        if args.demo:
            ids = _make_demo_data(db)
            print(
                f"已制造差异：booking #{ids['missing']}（删了退款流水）、"
                f"#{ids['excess']}（多记一笔退款）"
            )
        report = reconcile(db, since=since)
        if ids is not None:
            _cleanup_demo_data(db, ids["resource"])
            print("demo 数据已清理，下次全量对账不会因此误报。")
    finally:
        db.close()

    if args.json:
        payload = {
            "scanned": report.scanned,
            "with_payment": report.with_payment,
            "with_refund": report.with_refund,
            "expected_refund_total": report.expected_refund_total,
            "actual_refund_total": report.actual_refund_total,
            "balanced": report.balanced,
            "discrepancies": [asdict(d) for d in report.discrepancies],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))

    return 0 if report.balanced else 1


if __name__ == "__main__":
    sys.exit(main())

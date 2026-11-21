"""应用装配层的单测：不连库，只验"能不能起来"。

这一组存在的原因很具体：`lifespan_tasks()` 少写了一个 `app` 参数，
uvicorn 启动时抛 TypeError、服务根本起不来，但 `TestClient(app).get(...)`
不进 `with` 就不触发 lifespan，所以 20 条集成测试全绿也没发现。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.main import app
from app.services.expiry_worker import lifespan_tasks


def test_lifespan_accepts_the_app_argument():
    """FastAPI 会以 `lifespan(app)` 的形式调用，签名必须收得下这个参数。"""

    async def _enter_once() -> bool:
        async with lifespan_tasks(app):
            return True

    assert asyncio.run(_enter_once()) is True


def test_routes_are_all_registered():
    """路由少挂一个（比如 waitlist）在测试里很难被发现，这里直接对路径清单。"""
    from fastapi.testclient import TestClient as C

    spec = C(app).get("/openapi.json").json()
    paths = set(spec["paths"])
    assert {
        "/api/v1/resources",
        "/api/v1/resources/{resource_id}/business-hours",
        "/api/v1/resources/{resource_id}/availability",
        "/api/v1/bookings",
        "/api/v1/bookings/{booking_id}",
        "/api/v1/bookings/{booking_id}/pay",
        "/api/v1/bookings/{booking_id}/cancel",
        "/api/v1/bookings/{booking_id}/checkin",
        "/api/v1/bookings/{booking_id}/events",
        "/api/v1/waitlist",
    } <= paths


def test_healthz_does_not_touch_dependencies():
    """/healthz 只回 ok，不查 DB 也不查 Redis——否则容器探针会被下游故障拖死。"""
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}

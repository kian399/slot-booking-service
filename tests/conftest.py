"""测试全局配置：库健康检查 + 建表 fixture。

`pytest_collection_modifyitems` 负责"环境没起来时别甩 20 条一样的报错"；
`schema` 负责"用 alembic 建库，而不是 metadata.create_all"——
因为两条 EXCLUDE 约束只写在迁移里，`create_all` 建出来的库测不出真实行为。
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import pytest

from app.config import settings


def _db_endpoint() -> tuple[str, int]:
    url = settings.DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://")
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 5432


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def pytest_collection_modifyitems(config, items):
    """只在本次确实要跑 integration 用例时才检查，单测不受影响。"""
    need_db = any("integration" in item.keywords for item in items)
    if not need_db:
        return

    host, port = _db_endpoint()
    if _port_open(host, port):
        return

    pytest.exit(
        f"\n数据库连不上：{host}:{port}\n"
        "\n先用三条命令把依赖拉起来，再重跑：\n"
        "    docker compose up -d\n"
        "    docker compose ps            # 等到 healthy\n"
        "    uv run alembic upgrade head\n"
        "\n如果是 Docker Desktop 被关掉了，容器会是 Exited 状态，\n"
        "启动 Docker Desktop 后执行 `docker start slot-booking-db slot-booking-redis` 也行。\n",
        returncode=2,
    )


@pytest.fixture(scope="session")
def schema():
    """整轮集成测试共用一个干净库。

    故意不用 autouse：单测不碰数据库，不能让它们触发 `DROP SCHEMA`。
    """
    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(Config("alembic.ini"), "head")
    yield
    engine.dispose()

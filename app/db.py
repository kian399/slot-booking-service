from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：一个请求一个 Session，异常时回滚。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台任务用：脱离请求生命周期的独立事务。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_conn, _record):
    # 排他约束冲突时 PG 会等锁；没有超时的话一个慢事务能把整条队列拖住。
    # 2s 对上层的 409 语义没有影响（冲突通常在毫秒级就判完），但能挡住锁等待雪崩。
    with dbapi_conn.cursor() as cur:
        cur.execute("SET statement_timeout = '2s'")
        cur.execute("SET idle_in_transaction_session_timeout = '10s'")

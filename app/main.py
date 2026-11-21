import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import metrics
from app.api.v1.bookings import router as bookings_router
from app.api.v1.resources import router as resources_router
from app.api.v1.waitlist import router as waitlist_router
from app.exceptions import AppError
from app.logging_config import configure_logging
from app.middleware import MetricsMiddleware, RequestIDMiddleware
from app.services import delay_queue
from app.services.expiry_worker import lifespan_tasks

configure_logging()
logger = structlog.get_logger(__name__)


app = FastAPI(title="时段预约与排期服务", version="0.1.0", lifespan=lifespan_tasks)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(MetricsMiddleware)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """所有业务异常的出口。code 比 HTTP 状态码更能表达「到底哪一条规则没过」。"""
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = structlog.contextvars.get_contextvars().get("request_id", "")
    logger.exception("unhandled_exception")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
                 "request_id": request_id},
    )


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    from app.db import engine

    try:
        metrics.set_gauge("db_pool_size", engine.pool.size())
        metrics.set_gauge("db_pool_checked_out", engine.pool.checkedout())
    except Exception:  # noqa: BLE001 某些池实现没有这些方法
        pass
    # 积压深度是 Grafana 上最该盯的指标：ZSET 一直涨说明 worker 干不过来或 worker 死了
    metrics.set_gauge("hold_queue_depth", delay_queue.depth())
    return Response(
        content=metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


app.include_router(resources_router, prefix="/api/v1")
app.include_router(bookings_router, prefix="/api/v1")
app.include_router(waitlist_router, prefix="/api/v1")

"""业务异常与统一错误体。

错误体格式（实施方案 3.10）：

    {"error": {"code": "SLOT_TAKEN", "message": "...", "details": {...}}}

HTTP 状态码表达「哪一类问题」，code 表达「具体哪一条规则没过」。
客户端和压测脚本按 code 分支，比匹配中文文案可靠。
"""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class ResourceNotFound(AppError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class BookingNotFound(AppError):
    status_code = 404
    code = "BOOKING_NOT_FOUND"


class SlotTaken(AppError):
    """时段已被占用。由数据库排他约束 violation 翻译而来，不是应用层判断出来的。"""

    status_code = 409
    code = "SLOT_TAKEN"


class IdempotencyConflict(AppError):
    """同一 Idempotency-Key 配了不同的请求体。"""

    status_code = 409
    code = "IDEMPOTENCY_CONFLICT"


class IdempotencyInFlight(AppError):
    """同 key 的首个请求还没落库完成（极少见），让客户端稍后重试。"""

    status_code = 409
    code = "IDEMPOTENCY_IN_FLIGHT"


class StatusTransitionInvalid(AppError):
    status_code = 409
    code = "STATUS_TRANSITION_INVALID"


class BookingNotPayable(AppError):
    status_code = 409
    code = "BOOKING_NOT_PAYABLE"


class DurationInvalid(AppError):
    """时长为空/反向/超出租期范围/不对齐。"""

    status_code = 422
    code = "DURATION_INVALID"


class OutOfBusinessHours(AppError):
    status_code = 422
    code = "OUT_OF_BUSINESS_HOURS"


class ResourceInactive(AppError):
    status_code = 409
    code = "RESOURCE_INACTIVE"


# 内部沿用的短名字
InvalidStateTransition = StatusTransitionInvalid
IdempotencyMismatch = IdempotencyConflict
DurationOrWindowInvalid = DurationInvalid

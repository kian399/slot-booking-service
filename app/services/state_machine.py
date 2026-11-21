"""七态状态机。

为什么把转移表写成数据而不是散落的 if：
1) 任何一条 UPDATE 之前都能用 `ensure_transition` 断言，非法转移在服务端直接 409，
   而不是悄悄把一个 expired 的单改成 confirmed；
2) 并发下真正防住「重复流转」的是 SQL 的 CAS（WHERE status = 期望值），
   状态机负责的是「语义合法」，两者分工不能混。

只有 4 条边会释放资源：
    pending_payment → expired / cancelled
    confirmed       → cancelled / no_show
checked_in 必须继续占位（时段已经被消费，但同资源同时段仍然不能被二次卖出），
completed 是时间自然过去的归档态，不参与约束白名单。
"""

from __future__ import annotations

from app.exceptions import InvalidStateTransition
from app.models import BookingStatus as S

P = S.PENDING_PAYMENT.value
C = S.CONFIRMED.value
CI = S.CHECKED_IN.value
D = S.COMPLETED.value
X = S.CANCELLED.value
E = S.EXPIRED.value
N = S.NO_SHOW.value

TRANSITIONS: dict[str, frozenset[str]] = {
    P: frozenset({C, E, X}),
    C: frozenset({CI, X, N}),
    CI: frozenset({D}),
    D: frozenset(),
    X: frozenset(),
    E: frozenset(),
    N: frozenset(),
}

TERMINAL: frozenset[str] = frozenset({D, X, E, N})


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())


def ensure_transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise InvalidStateTransition(f"订单状态不能从 {src} 变为 {dst}", src=src, dst=dst)


def is_terminal(status: str) -> bool:
    return status in TERMINAL

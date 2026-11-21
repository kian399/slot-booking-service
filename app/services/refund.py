"""阶梯退款。

规则按「距开始还有多久」分档，越晚取消退得越少。写成纯函数：
不碰 DB、不碰时钟，`now` 由调用方传入，单测可以直接钉住边界值。

边界取「闭区间左端」：正好提前 24 小时取消算 100%，正好提前 2 小时算 50%。
这个选择必须写死在测试里，否则某个版本改成 `>` 会造成对账差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Tier:
    min_lead: timedelta
    ratio: float
    label: str


# 从宽到严，顺序即优先级
TIERS: tuple[Tier, ...] = (
    Tier(timedelta(hours=24), 1.0, "提前24小时以上"),
    Tier(timedelta(hours=2), 0.5, "提前2小时以上"),
    Tier(timedelta(0), 0.0, "开始前2小时内"),
)


def refund_ratio(start_at: datetime, now: datetime) -> tuple[float, str]:
    lead = start_at - now
    for tier in TIERS:
        if lead >= tier.min_lead:
            return tier.ratio, tier.label
    # lead < 0：已经开始（甚至结束）才来取消
    return 0.0, "已开始或已过期"


def compute_refund(
    amount_cents: int, start_at: datetime, now: datetime, *, checked_in: bool = False
) -> tuple[int, float, str]:
    """返回 (退款金额, 比例, 档位说明)。

    已核销的单不退（服务已经被消费掉了），这条在状态机之外单独判：
    checked_in → cancelled 不是合法边，正常走不到，防御性保留。
    向下取整到分：宁可少退一分，也不要因为浮点误差多退——多退在对账里是资金损失。
    """
    if checked_in:
        return 0, 0.0, "已核销，不退款"
    ratio, label = refund_ratio(start_at, now)
    refund = int(amount_cents * ratio)  # int() 对正数即向下取整
    if refund > amount_cents:  # 防御：配置被改坏也不能超额退
        refund = amount_cents
    return refund, ratio, label

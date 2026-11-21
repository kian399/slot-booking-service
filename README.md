# slot-booking-service

[![ci](https://github.com/kian399/slot-booking-service/actions/workflows/ci.yml/badge.svg)](https://github.com/kian399/slot-booking-service/actions/workflows/ci.yml)

多资源时段预约服务。**解决的问题是并发正确性：「同一资源同一时段只能成交一次」由数据库约束保证，不是应用层判断。**

## 为什么

「先查有没有冲突，再插入」在并发下必然超卖——两条请求可以同时查到「空」，然后一起插入。
本仓库的对照组实测：32 并发抢同一时段，**12 轮里 12 轮超卖**。

修法不是加锁，是让数据库拒绝第二次插入：

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap_resource
EXCLUDE USING gist (
    resource_id WITH =,
    tstzrange(start_at, end_at, '[)') WITH &&
) WHERE (status IN ('pending_payment', 'confirmed', 'checked_in'));
```

- `&&` 判断的是**区间相交**，不是值相等。`UNIQUE(resource_id, start_at)` 挡不住 08:00-09:00 与 08:30-09:30。
- `'[)'` 左闭右开，08:00-09:00 与 09:00-10:00 可以连排。
- `WHERE` 只让占位状态参与互斥，订单取消后时段自动可售。

第二条约束防「同一个人同时占两个资源」：结构相同，第一列换成 `user_id`。

## 对照数据

`bench/run_all_groups.py`，32 并发 × 12 轮，每轮抢同一时段：

| 实现 | 超卖轮次 | QPS | P99 |
| --- | --- | --- | --- |
| 先查后插 | **12/12** | 234.6 | 169 ms |
| 行锁 `FOR UPDATE` | 0 | 219.4 | 222 ms |
| 乐观锁 `version` + 重试 | **12/12** | 70.1 | 532 ms |
| **排他约束（本实现）** | **0** | **244.1** | **166 ms** |
| Redis 锁 + 约束 | 0 | 116.9 | 492 ms |

乐观锁那一行是重点：它保护的是 `resources` 这一行的写冲突，而预约冲突发生在两条 `bookings` 之间，**没有共同的行可以比版本号**。所以它不但没解决问题，吞吐还只有约束方案的 1/7。

## 跑起来

```bash
docker compose up -d              # PostgreSQL 5544 / Redis 6390
uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8001 --reload
```

```bash
uv run pytest                    # 58 条单测，不需要数据库
uv run pytest -m integration -v  # 25 条，跑在真实 PostgreSQL 上
uv run python scripts/reconcile.py        # 订单/流水对账，账平退出码 0
uv run python scripts/demo_waitlist.py    # 候补端到端：抢不到 → 排队 → 取消 → 自动补位
uv run python scripts/demo_outbox.py      # 发件箱：事件不丢、可能重复、消费端去重
uv run python scripts/demo_waitlist.py    # 候补端到端：抢不到 → 排队 → 取消 → 自动补位
uv run python scripts/demo_outbox.py      # 发件箱：事件不丢、可能重复、消费端去重
uv run python bench/run_all_groups.py     # A~E 五组对照压测
```

观测栈是可选的：`docker compose --profile observability up -d`，Grafana `localhost:3000`（admin/admin）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/bookings` | 创建预约，支持 `Idempotency-Key` |
| POST | `/api/v1/bookings/{id}/pay` | 模拟支付 |
| POST | `/api/v1/bookings/{id}/checkin` | 到店核销 |
| POST | `/api/v1/bookings/{id}/cancel` | 取消 + 阶梯退款 |
| GET | `/api/v1/bookings/{id}/events` | 状态迁移历史 |
| GET | `/api/v1/resources/{id}/availability?day=` | 可售时段 |
| POST | `/api/v1/waitlist` | 候补（释放后自动补位，hold 5 分钟） |

错误体统一为 `{"error": {"code", "message", "details"}}`，冲突返回 `409 SLOT_TAKEN`。

## 生命周期

```
pending_payment ──支付──> confirmed ──核销──> checked_in ──结束──> completed
      │                       │
      ├──15min 未支付──> expired   ├──提前取消──> cancelled ──> 退款流水
      └──用户取消──────> cancelled   └──开始 15min 未核销──> no_show
```

超时释放用 Redis ZSET 做调度，但**真相在 PG**：每次释放都是
`UPDATE ... WHERE status='pending_payment' AND expires_at <= now()` 的 CAS。
Redis 多出来的 id 只是空转，Redis 丢了的 id 由 60 秒一次的兜底扫描补上。

## 限制

- 单资源独占（capacity=1），多座位请建多条 resource
- 不支持跨天预约
- 未处理夏令时（`Asia/Shanghai` 无 DST）
- 无鉴权，`user_id` 从 body 传入

更细的实测环境、踩坑记录和后续排期见 [`PLAN.md`](PLAN.md)。

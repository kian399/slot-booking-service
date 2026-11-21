# 实验手册

主线功能已闭环（约束 / 状态机 / 幂等 / 延迟队列 / 对账 / 观测 / 候补 / outbox）。
下面 5 个实验是为了回答面试官的追问——每个实验都对应一个他们一定会问的问题。

| # | 实验 | 回答什么追问 |
| --- | --- | --- |
| 1 | EXPLAIN 看约束的索引 | 「零超卖是不是靠全表扫换来的？」 |
| 2 | 10 万订单下的性能 | 「数据量大了还快吗？」 |
| 3 | Redis 挂掉 | 「Redis 挂了业务还能用吗？」 |
| 4 | worker 挂掉再重启 | 「消息丢了怎么办？」 |
| 5 | 并发时的锁等待 | 「排他约束会不会把库锁死？」 |

前置：`docker compose up -d && uv run alembic upgrade head`，以下命令都在项目根目录执行。

---

## 实验 1：EXPLAIN 证明约束走的是 GiST 索引

**作用**：说明「32 并发零超卖」的成本不是全表扫描，而是 O(log n) 的索引判定。
面试官听到"数据库约束"第一反应是"那得多慢"，这组数据就是答案。

### 步骤

```bash
# 1. 看约束生成了哪些索引
docker exec slot-booking-db psql -U slot -d slot_booking -c \
  "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='bookings' ORDER BY indexname;"
```

预期输出里必须有：
```
bookings_no_overlap_resource | CREATE INDEX ... USING gist (resource_id, tstzrange(start_at, end_at, '[)'::text)) WHERE (status = ANY (...))
bookings_no_overlap_user     | CREATE INDEX ... USING gist (user_id, tstzrange(...)) WHERE (...)
```

```bash
# 2. 看「找冲突行」这个查询怎么执行（约束内部就是跑这个）
docker exec slot-booking-db psql -U slot -d slot_booking -c \
"EXPLAIN (ANALYZE, BUFFERS)
 SELECT 1 FROM bookings
 WHERE resource_id = 1
   AND status IN ('pending_payment','confirmed','checked_in')
   AND tstzrange(start_at, end_at, '[)') && tstzrange('2026-12-01 10:00+08','2026-12-01 11:00+08','[)');"
```

### 预期

`Bitmap Index Scan on bookings_no_overlap_resource`（或 `Index Scan`），`rows=` 很小，`Buffers: shared hit=` 个位数。

### 怎么讲

> 冲突判定走 GiST 索引，代价和已占用区间的数量对数相关，不随表增长线性变差。
> 实测插入耗时从 0.4ms 涨到 0.6ms（`docs` 里五组对照的 D 组数据），这 0.2ms 就是约束的成本。

---

## 实验 2：10 万订单下的插入与查询

**作用**：面试必问「数据量大了怎么办」。空库压测的数字不可信，这个实验把库灌到 10 万级再看。

### 步骤

```python
# scripts/bench_scale.py  （新建后 uv run python scripts/bench_scale.py）
import time
import psycopg2

DSN = "postgresql://slot:slot@127.0.0.1:5544/slot_booking"

def seed(conn, n=100_000):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bookings")
        if cur.fetchone()[0] > 50_000:
            print("已有数据，跳过造数"); return
        t0 = time.perf_counter()
        # 每条订单占 30 分钟且互不重叠，user_id 各不相同，避开两条排他约束
        cur.execute("""
            INSERT INTO bookings
                (resource_id, user_id, start_at, end_at, status,
                 amount_cents, refunded_cents, version, expires_at)
            SELECT (g %% 20) + 1,
                   1000000 + g,
                   now() + (g * interval '30 min'),
                   now() + (g * interval '30 min') + interval '30 min',
                   'confirmed', 10000, 0, 0, now() + interval '15 min'
            FROM generate_series(1, %s) g
        """, (n,))
        conn.commit()
        print(f"造数 {n} 条，耗时 {time.perf_counter() - t0:.2f}s")

def timing(conn, sql, params=None, rounds=20):
    """跑 rounds 次取 P50，排除首次冷启动的影响。"""
    lat = []
    with conn.cursor() as cur:
        for _ in range(rounds):
            t0 = time.perf_counter()
            cur.execute(sql, params)
            cur.fetchall()
            lat.append((time.perf_counter() - t0) * 1000)
    conn.rollback()
    lat.sort()
    return lat[len(lat) // 2]

if __name__ == "__main__":
    conn = psycopg2.connect(DSN)
    seed(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bookings")
        total = cur.fetchone()[0]
    print(f"当前订单数：{total}")

    p50 = timing(conn, """
        SELECT 1 FROM bookings
        WHERE resource_id = 1
          AND status IN ('pending_payment','confirmed','checked_in')
          AND tstzrange(start_at, end_at, '[)')
              && tstzrange(now() + interval '3 days', now() + interval '3 days 1 hour', '[)')
    """)
    print(f"冲突判定 P50：{p50:.2f} ms")

    p50 = timing(conn, """
        SELECT gs FROM generate_series(
            date_trunc('day', now() + interval '3 days'),
            date_trunc('day', now() + interval '4 days') - interval '30 min',
            interval '30 min') gs
        WHERE NOT EXISTS (
            SELECT 1 FROM bookings b
            WHERE b.resource_id = 1
              AND b.status = ANY(ARRAY['pending_payment','confirmed','checked_in'])
              AND tstzrange(b.start_at, b.end_at, '[)') && tstzrange(gs, gs + interval '30 min', '[)'))
    """)
    print(f"某天可售时段查询 P50：{p50:.2f} ms")
    conn.close()
```

### 执行

```bash
uv run python scripts/bench_scale.py
```

### 预期与解读

| 数据量 | 冲突判定 P50 | 可售查询 P50 |
| --- | --- | --- |
| 1 千 | ~0.2ms | ~1ms |
| 10 万 | 应该仍在 1ms 量级 | 应该 < 10ms |

如果可售查询明显变慢，说明 `bookings` 上缺「资源 + 时间区间」的 GiST 索引用于反查——
这正好暴露一个真实问题，也是下一步优化的落点（按 `resource_id` + `tstzrange` 建 GiST 索引）。

**面试怎么说**：「约束的判定成本和已占用区间数是对数关系；真正会先慢下来的是可售查询，
所以我在 ×× 上加了 GiST 索引，把 ×× ms 降到 ×× ms。」——有数字，就比背概念强。

---

## 实验 3：Redis 挂掉，业务还能不能用

**作用**：证明「Redis 只是调度器，不是真相」这句话不是嘴上说说。

### 步骤

```bash
# 1. 起服务
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001

# 2. 造资源
uv run python - <<'PY'
import httpx
c = httpx.Client(base_url="http://127.0.0.1:8001", trust_env=False)
rid = c.post("/api/v1/resources", json={"name":"fail-test","price_cents":20000}).json()["id"]
c.put(f"/api/v1/resources/{rid}/business-hours", json={"hours":[
    {"weekday":w,"open_time":"08:00:00","close_time":"20:00:00"} for w in range(7)]})
print("resource", rid)
PY

# 3. 停掉 Redis
docker stop slot-booking-redis

# 4. 再下单（把 resource_id 换成上一步打印的）
curl -s -X POST localhost:8001/api/v1/bookings -H 'content-type: application/json' \
  -d '{"resource_id":1,"user_id":1,"start_at":"2026-12-02T10:00:00+08:00","end_at":"2026-12-02T11:00:00+08:00"}'

# 5. 查指标里的入队失败计数
curl -s localhost:8001/metrics | grep hold_enqueue_failed

# 6. 恢复
docker start slot-booking-redis
```

### 预期

- 第 4 步仍然返回 **201**（下单成功）。因为 `delay_queue.enqueue` 捕获异常只记日志，DB 提交是独立的。
- `hold_enqueue_failed_total` 会增加。
- 这张单不会进 ZSET，超时后由 **60 秒兜底扫描**释放（`EXPIRY_SWEEP_ENABLED=true`）。
- 如果想验证自愈：等 15 分钟（`HOLD_MINUTES`），查 `bookings.status` 会变成 `expired`。

### 怎么讲

> 双写（DB + Redis）一定会不一致，我的处理是让不一致**只能往安全的方向偏**：
> Redis 多了 id → CAS 命中 0 行，空转；Redis 少了 id → 兜底扫描补。
> 实测停掉 Redis 后下单接口仍返回 201，代价只是这张单的超时释放会晚最多 60 秒。

---

## 实验 4：worker 挂掉，积压能不能补回来

**作用**：回答「消息丢了怎么办」。这是延迟队列最容易被质疑的点。

### 步骤

```bash
# 1. 故意用「不启 worker」的方式启动
EXPIRY_WORKER_ENABLED=false EXPIRY_SWEEP_ENABLED=false \
  uv run uvicorn app.main:app --port 8002

# 2. 灌 200 张已到期未支付的单（直接写库 + ZSET）
uv run python - <<'PY'
import psycopg2, redis
from datetime import datetime, timedelta, timezone
TZ = timezone(timedelta(hours=8))
r = redis.Redis.from_url("redis://127.0.0.1:6390/0")
conn = psycopg2.connect("postgresql://slot:slot@127.0.0.1:5544/slot_booking"); conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT id FROM resources ORDER BY id LIMIT 1"); rid = cur.fetchone()[0]
cur.execute("DELETE FROM bookings WHERE user_id = 888888")
expired = datetime.now(TZ) - timedelta(seconds=1)
cur.execute("""
    INSERT INTO bookings (resource_id, user_id, start_at, end_at, status,
                          amount_cents, refunded_cents, version, expires_at)
    SELECT %s, 888888 + g,
           now() + (g * interval '1 hour'), now() + (g * interval '1 hour') + interval '30 min',
           'pending_payment', 10000, 0, 0, %s
    FROM generate_series(1, 200) g RETURNING id""", (rid, expired))
ids = [x[0] for x in cur.fetchall()]
r.zadd("hold:expiry", {str(i): expired.timestamp() for i in ids})
print("灌入", len(ids), "张，ZSET 深度", r.zcard("hold:expiry"))
PY

# 3. 等 30 秒，确认没人处理（worker 是关的）
docker exec slot-booking-db psql -U slot -d slot_booking -t -c \
  "SELECT count(*) FROM bookings WHERE user_id >= 888888 AND status='pending_payment';"

# 4. 再起一个「只开兜底扫描」的实例 —— 模拟恢复
EXPIRY_WORKER_ENABLED=false EXPIRY_SWEEP_ENABLED=true EXPIRY_SWEEP_INTERVAL=5 \
  uv run uvicorn app.main:app --port 8003

# 5. 等 10 秒后再查
docker exec slot-booking-db psql -U slot -d slot_booking -t -c \
  "SELECT count(*) FROM bookings WHERE user_id >= 888888 AND status='pending_payment';"
```

### 预期

- 第 3 步：200（一张都没释放，因为 worker 和扫描都关了）。
- 第 5 步：**0**。兜底扫描用 `FOR UPDATE SKIP LOCKED` 直接扫库，不依赖 ZSET。

### 怎么讲

> Redis 里的 id 是"提醒"，DB 里的 `expires_at` 才是"事实"。
> 我把 worker 完全关掉再重启，200 张超期单在 10 秒内全部释放——
> 这条路径不经过 Redis，所以 Redis 丢多少都不影响正确性。

---

## 实验 5：并发时到底锁住了什么

**作用**：回答「排他约束会不会把库锁死」。很多人以为约束 = 表锁，其实不是。

### 步骤

```bash
# 1. 起服务（正常配置）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001

# 2. 开一个终端持续观察锁
docker exec -it slot-booking-db psql -U slot -d slot_booking -c \
"SELECT locktype, relation::regclass AS rel, mode, granted, count(*)
 FROM pg_locks
 WHERE relation = 'bookings'::regclass
 GROUP BY 1,2,3,4 ORDER BY granted, mode;"

# 3. 另一个终端跑压测（只跑 D 组，32 并发抢同一时段）
uv run python bench/run_all_groups.py --groups d --rounds 6 --concurrency 32
```

### 预期

你会看到大量 `RowExclusiveLock`（INSERT 的行级/表级意向锁），**不会出现 `AccessExclusiveLock`**。
等待中的锁（`granted=false`）最多在峰值出现，且很快消失——它们等的是 GiST 索引页上的短锁，
不是等整张表。

### 怎么讲

> 排他约束的判定是索引级的，INSERT 拿的是 `RowExclusive`（意向锁），不会阻塞读，
> 也不会让整张表排队。真正被串行化的是"同一资源同一时段"这一小片区间。

---

## 做完这些之后

数据记到 `PLAN.md` 第 1 节或者单独开一节，格式和已有的五组对照保持一致（表 + 一句话结论）。

下一步真正值得做的（按性价比）：

1. **实验 2 如果发现可售查询慢** → 加 GiST 索引，把优化前后的数据写进简历（这才是"性能优化"的真实故事）
2. **多 worker 部署压测** → 4 进程 × 16 并发，证明约束跨进程有效、吞吐线性增长
3. **压测报告成文** → 把五组对照 + 这 5 个实验合成一页，面试时直接投屏

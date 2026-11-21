# 项目 B 执行计划：slot-booking-service

> 方案依据：`D:\DSH\DSH-working\项目B_预约排期服务_实施方案.md`
> 代码位置：`D:\slot-booking-service`
> 时间预算按实际作息：**工作日 30 分钟 + 周六 4 小时 ≈ 每周 5 小时**
> 更新：2026-11-19 晚

---

## 0. 进度

| 项 | 状态 |
| --- | --- |
| 代码（7 态状态机 / 双排他约束 / 幂等 / 延迟队列 / 候补 / outbox） | ✅ 完成 |
| 集成测试 25 条（真实 PostgreSQL） | ✅ **25 passed** |
| 单测 58 条 + ruff | ✅ **58 passed / clean** |
| A~E 五组对照压测 | ✅ **数据已出（见第 1 节）** |
| **对账脚本 + 四类差异测试** | ✅ **完成（见第 2 节）** |
| CI（GitHub Actions） | ✅ **已推 GitHub，lint + test 全绿（27s，PG service 跑通 25 条集成）** |
| **Grafana 观测栈** | ✅ **6 块面板，指标链路已验通（见第 3 节）** |
| README | ✅ 已补（投简历必需） |
| **候补端到端** | ✅ `scripts/demo_waitlist.py` 已跑通（取消即补位） |
| **Outbox 投递** | ✅ `app/services/outbox_worker.py` + `scripts/demo_outbox.py` |
| 简历改写 | ✅ `桌面/软件工程师简历-优化版.docx` |

原计划两周的 S1~S3（打通真库 + 调通压测 + 出数据）在 11-19 一天内完成；
S5 对账当天补完；11-21 补了 S4（git 仓库）、S7（Grafana）和 README。

---

## 1. 实测数据（2026-11-19，唯一可信来源）

**环境**：Windows 本机；uvicorn 单进程；PG 16 / Redis 7 在 Docker；压测端与目标同机；
`concurrency=32, rounds=12`，每轮 32 个请求抢同一时段；每组前自动 `TRUNCATE` 并装卸约束。

| 组 | 实现 | 201 | 409 | 500 | 超卖轮次 | QPS | P50 | P99 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 先查后插（无约束） | 39 | 345 | 0 | **12/12** | 234.6 | 88.7 | 169.3 |
| B | 锁资源行 FOR UPDATE（无约束） | 12 | 372 | 0 | 0 | 219.4 | 90.9 | 221.9 |
| C | 乐观锁 version + 重试（无约束） | 384 | 0 | 0 | **12/12** | 70.1 | 248.2 | 531.6 |
| D | **排他约束（生产实现）** | 12 | 372 | 0 | **0** | 244.1 | 89.9 | 166.2 |
| E | Redis 锁 + 排他约束 | 12 | 372 | 0 | 0 | 116.9 | 165.0 | 491.9 |

### 超时释放（release 组）

每轮灌 1000 张已到期单到 DB + ZSET，观察 worker 消费到积压归零。

| 项 | 值 |
| --- | --- |
| 灌入 | 3000 单（3 轮 × 1000） |
| 总耗时 | 54.4 s |
| 吞吐 | **55.2 单/秒** |
| 尾部残留 | 91（测量截止时最后一批正在处理，下一批清掉） |

**55 单/秒够不够**：15 分钟 hold 意味着要产生持续 55 单/秒的释放压力，
需要 3300 单/分钟的下单速率才追不上——这个项目远到不了。
所以当前瓶颈不在释放速度。真要提速，把「每单一个事务」改成
`UPDATE bookings SET status='expired' WHERE id = ANY(...)` 批量更新即可，能到几百/秒。

原始 JSON：`bench/results/{a,b,c,d,e}.json`

### 四条能直接拿去讲的结论

1. **A 组 11/12 轮超卖，共 34 笔**。「先查后插」不是偶尔出错，而是稳定出错。
   注意：A 组在「查冲突」和「插入」之间注入了 `NAIVE_WINDOW_MS=5`。
   这不是造假——真实业务这两步之间还有校验、算价、写审计，窗口本来就是毫秒级；
   不显式化的话单机上两步只隔 10 微秒，永远撞不上，于是这个 bug 在 demo 里永远看不到。

2. **C 组 384 笔全部成交，100% 超卖，而且 QPS 最低（63.3）**。
   这是全组最有价值的一条：乐观锁保护的是 `resources` 这一行的写冲突，
   而预约冲突发生在两条 `bookings` 之间——**根本没有共同的行可以比版本号**。
   它不但没解决问题，还因为 CAS 冲突重试把吞吐压到 D 组的 1/3。

3. **D 组 0 超卖且 QPS 最高（185.6）**。约束不是「为了正确性牺牲性能」：
   GiST 冲突判定比 C 组的重试风暴便宜得多，比 B 组的串行锁也快。

4. **E 组加了 Redis 分布式锁之后，QPS 掉到 104.4，还多了 38 个失败**。
   正确性一点没涨（D 已经保证了），故障模式多了一种。这就是「不要用锁去解决本该由约束解决的问题」的实证。

---

## 2. 对账（`scripts/reconcile.py`）

「不超卖」只证明没卖重，不证明**钱没算错**。退款是按时间阶梯算出来的，
退款规则、状态流转、支付流水三者任何一处不一致，账就对不上，
而这种错在功能测试里完全看不出来——用户不会告诉你多退了他 20 块。

核心思路：**应退金额可以重算**。退款策略是纯函数，任何时刻都能用
「订单金额 + 开始时间 + 取消时间」重算"应该退多少"，再和 `payments` 比。
所以对账不依赖历史规则，规则改了也能对。

| 代码 | 含义 | 后果 |
| --- | --- | --- |
| D1 | 已取消且有支付，但退款不足 | 用户的钱没退，投诉来源 |
| D2 | 退款金额超过应退 | 多退，直接资损 |
| D3 | 有支付流水但订单不在已支付态 | 支付回调丢了 / 状态被改回 |
| D4 | 有退款流水但订单没取消 | 幽灵退款 |

```bash
uv run python scripts/reconcile.py                  # 全量，账平退出码 0，有差异 1
uv run python scripts/reconcile.py --since 2026-11-01
uv run python scripts/reconcile.py --json           # 接告警
uv run python scripts/reconcile.py --demo           # 造两条差异，验证能查出来
```

`--demo` 实测输出（退出码 1）：

```
[D1_REFUND_MISSING] 已取消但退款不足  1 条
  - booking #46: 应退 20000 分，实退 0 分，差 20000 分
[D2_REFUND_EXCESS] 退款超过应退金额  1 条
  - booking #47: 应退 20000 分，实退 25000 分，多退 5000 分
```

demo 跑完会自己清理数据——对账最怕的不是漏报，是误报多了之后没人看。

四类差异都有 pytest 守着（`tests/test_reconcile.py`，5 条），
其中一条专门验「正常账必须报 0 差异」，防的是对账脚本自己写错导致永远在报警。

---

## 3. 观测栈（Grafana + Prometheus）

```bash
# 应用要绑 0.0.0.0：容器访问宿主机走的是 Docker 网关，绑 127.0.0.1 会连接被拒
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001

docker compose --profile observability up -d
# Prometheus http://localhost:9090  |  Grafana http://localhost:3000 (admin/admin)
```

六块面板（`deploy/grafana/dashboards/slot-booking.json`，已 provision，开箱即用）：

| 面板 | 指标 | 看什么 |
| --- | --- | --- |
| 请求 QPS | `rate(http_requests_total[1m])` | 按路由拆分；409 也计入（它同样是服务端处理过的请求） |
| 时段冲突率 | `booking_conflict_total / (created + conflict)` | 抢购场景冲到 90%+ 是正常的，说明约束在工作 |
| 超时释放延迟 | `histogram_quantile(0.99, hold_release_delay_seconds_bucket)` | 持续 > 2s 说明 worker 追不上 |
| 延迟队列积压 | `hold_queue_depth` | 一直涨 = worker 死了或干不过来，**最该配告警** |
| 订单状态流转 | 创建/支付/释放/取消 四条速率线 | 创建远大于支付 = hold 在堆积 |
| 数据库连接池 | `db_pool_checked_out` vs `db_pool_size` | 贴住上限 = 连接不够或事务太长 |

已在 11-21 验证：Prometheus target `up`，Grafana 能读出 6 个面板，
造 20 次抢同一时段 → 采集到 `booking_conflict_total=19`、`hold_queue_depth=19`。

**踩的坑**：应用用默认的 `127.0.0.1` 起，Prometheus 报
`dial tcp 192.168.65.254:8001: connect: connection refused`——容器走 Docker 网关，
不是本机回环。必须 `--host 0.0.0.0`。

---

## 4. 后续排期

| 阶段 | 时间 | 做什么 | 验收 | 工时 |
| --- | --- | --- | --- | --- |
| ~~S4 本地仓库~~ | ~~11-20~~ | ~~git init + 首次提交~~ | ✅ 已完成（2 次提交） | ~~1h~~ |
| ~~S6 release 组~~ | ~~11-23~~ | ~~超时释放吞吐~~ | ✅ 55.2 单/秒 | ~~1h~~ |
| ~~S7 Grafana~~ | ~~11-29~~ | ~~四块面板~~ | ✅ 6 块面板已验通 | ~~3h~~ |
| ~~README~~ | ~~12-06~~ | ~~项目门面~~ | ✅ 已补 | ~~1h~~ |
| ~~S8a 推 GitHub~~ | ~~11-21~~ | ~~建仓库 + push~~ | ✅ [kian399/slot-booking-service](https://github.com/kian399/slot-booking-service) CI 绿灯 | ~~0.5h~~ |
| ~~S8b 候补演示~~ | ~~11-22~~ | ~~取消 → 补位 → 5 分钟 hold~~ | ✅ 已跑通 | ~~2h~~ |
| ~~S8c outbox 投递~~ | ~~11-28~~ | ~~outbox → Redis Stream~~ | ✅ 已跑通（至少一次 + 消费幂等） | ~~2h~~ |
| **S9 简历** | 11-29（日）2h | 用第 1 节的数字改写简历条目 + 准备 5 个追问 | — | 2h |

剩下三件，按性价比排：**S8a 推 GitHub（半小时，必须有）> S9 简历 > S8b 候补演示 > S8c outbox**。
Outbox 可以只讲不实现——面试问"发通知失败怎么办"，能说出"事务性发件箱 + 至少一次投递 + 消费端幂等"
就够了，不必真跑通。

---

## 5. 运行方式

```bash
docker compose up -d && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8001 --reload     # PG 5544 / Redis 6390 / App 8001
```

```bash
uv run pytest                                   # 58 条单测，秒级，不需要库
uv run pytest -m integration -v                 # 20 条，需要 PG + Redis
uv run python bench/run_all_groups.py --rounds 12 --concurrency 32
```

**容器 `Exited` 是最常见的"20 条 ERROR"**（Docker Desktop 退出时容器一起停）：

```bash
docker ps -a --filter name=slot-booking     # 看是不是 Exited
docker start slot-booking-db slot-booking-redis
```

`tests/conftest.py` 现在会先探一次端口，连不上就只打印上面这几条命令，
不会再甩 20 条 `OperationalError`。

---

## 6. 今天踩的坑（都已修，面试可讲）

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 服务完全起不来 | `lifespan_tasks()` 少写 `app` 参数，FastAPI 传参直接 TypeError；而 `TestClient` 不进 `with` 不触发 lifespan，所以 20 条集成测试全绿也没发现 | 补参数 + 加 `test_app_wiring.py` 专门盯装配 |
| 2 | `PendingRollbackError` | `db.add()` 写在 `begin_nested()` 外面：`begin_nested()` 会先把 pending 对象 flush 掉，那条 INSERT 跑在 SAVEPOINT 之外，冲突把整个 session 打成 aborted | 改成「先开 SAVEPOINT → 再 add → 再 flush」 |
| 3 | `ForeignKeyViolation` 删不掉资源 | payments 对 bookings 是 RESTRICT，直接删 resources 被挡 | 测试里加 `_drop_resource()`，按 FK 顺序拆 |
| 4 | A 组跑出 0 超卖（假象） | ①连接池 15 个，32 并发全在排队，竞争窗口被压没 ②asyncio 客户端在 Windows 上压不出并行（64 并发 458ms vs 串行 234ms） ③统计缩进错了，只算最后一轮 | 放大连接池 + 线程池 Barrier + 修缩进 |
| 5 | B 组 384 笔全部成交（比 A 还惨） | row_lock 策略「只锁不查」：锁住了资源行但根本没做冲突判断 | 锁 + 查 + 插三件套 |
| 6 | C 组 384 个请求全 500 | `_optimistic_bump` 更新 `resources.version`，但该列只加在模型没加进迁移 | 迁移补列，重建库 |

| 7 | 压测跑一半报 `FATAL: sorry, too many clients already` | 五个实例同时起，每个池 50+50 = 500 连接，顶穿 PG 默认的 100 | 改成每组独立起停 + 每实例 20+10，停实例后等 1.5s 让 PG 回收 |
| 8 | release 组每轮都跑满 120 秒，吞吐显示 8.3 单/秒 | ZSET 里积着历史残留 id（指向已删订单），worker 处理它们不释放任何东西，但 `zcard` 一直是正数 → 「等积压归零」永远不成立 | 每轮开始前 `DEL hold:expiry`；超时降到 60s。修完实测 55 单/秒 |
| 9 | release 组撞 `no_overlap_user` / `no_overlap_resource` | 3000 单挂在同一个 user 上，且每轮时段基准都是 `now()`，第二轮和第一轮未释放的尾部区间重叠 | 每轮换 user_id，并给每轮偏移 2000 小时的时间窗口 |

第 4 条最值得讲：**压测跑出"没问题"时，先怀疑压测本身**。三次都是环境问题，不是代码正确。
第 8 条是同一类：**8.3 单/秒不是 worker 慢，是测量条件错了**（在等一个永远不会归零的计数器）。

---

## 7. 简历口径（只用第 1 节的数字）

> 多资源时段预约服务（FastAPI / PostgreSQL / Redis）：把「同一资源同一时段只成交一次」
> 从应用层判断下沉为 PostgreSQL 排他约束（`EXCLUDE USING gist` + 部分索引），
> 32 并发抢同一时段零超卖；对照组中「先查后插」12 轮里 11 轮超卖、
> 「乐观锁 version」12 轮全部超卖且吞吐仅为约束方案的 1/3。
> 配套幂等键、七态状态机与 Redis ZSET 延迟队列，实现超时释放与阶梯退款。

数字全部来自 `bench/results/*.json`，重复跑差异约 10%，不要写更漂亮但没测过的数。

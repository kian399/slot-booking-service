"""五组对照压测（A~E），对应实施方案 3.11 难点 1。

每组只验证一个命题，跑完打出来的数字要能直接贴进 docs/benchmark.md：

    A  naive      先 SELECT 查冲突再 INSERT，且库里**没有**约束  → 期望测出超卖（复现 bug）
    B  row_lock   SELECT ... FOR UPDATE 锁资源行，无约束        → 0 超卖，但串行
    C  optimistic 资源行 version CAS + 重试，无约束             → 仍有超卖（乐观锁管不了区间）
    D  exclude    排他约束（生产实现）                          → 0 超卖
    E  redis_lock Redis 锁 + 排他约束                            → 0 超卖，看锁对 DB 冲突的削峰

    release       超时释放吞吐：不同 batch 下的释放延迟与残留     → 难点 3 的指标

为什么 A/B/C 组要 `DROP CONSTRAINT`：约束在库里时，再烂的应用层代码也超卖不了。
必须先把"裁判"撤掉，才能看到应用层方案的真实表现——这就是"先证明它会坏"。

用法（脚本会自己起服务实例，不用手工开五个终端）：

    # 先确保依赖在跑
    docker compose up -d
    uv run alembic upgrade head

    # 跑全部五组（约 5~8 分钟，取决于 rounds）
    uv run python bench/run_all_groups.py --rounds 20 --concurrency 32

    # 只跑某几组
    uv run python bench/run_all_groups.py --groups a,d

Windows 上如果 shell 里有 HTTP_PROXY，先 export NO_PROXY=127.0.0.1,localhost，
否则本地请求会被塞进代理（成批 status 0，数据作废）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TZ = timezone(timedelta(hours=8))
RESULTS_DIR = Path(__file__).parent / "results"
DB_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://slot:slot@127.0.0.1:5544/slot_booking")

# 组名 → (策略, 端口, 是否需要约束, 说明)
GROUPS: dict[str, tuple[str, int, bool, str]] = {
    "a": ("naive", 8011, False, "先查后插（无保护）"),
    "b": ("row_lock", 8012, False, "锁资源行 FOR UPDATE"),
    "c": ("optimistic", 8013, False, "乐观锁 version + 重试"),
    "d": ("exclude", 8014, True, "排他约束"),
    "e": ("redis_lock", 8015, True, "Redis 锁 + 排他约束"),
}

OCCUPYING = "'pending_payment','confirmed','checked_in'"


@dataclass
class GroupResult:
    group: str
    note: str
    rounds: int
    concurrency: int
    ok: int = 0
    conflict: int = 0
    error: int = 0
    oversold_rounds: int = 0
    latencies: list[float] = field(default_factory=list)
    wall_time: float = 0.0

    def summary(self) -> dict:
        lat = sorted(self.latencies)
        if not lat:
            p50 = p95 = p99 = 0.0
        else:
            def q(r: float) -> float:
                return round(lat[min(int(len(lat) * r), len(lat) - 1)] * 1000, 1)

            p50, p95, p99 = q(0.50), q(0.95), q(0.99)
        total = self.ok + self.conflict + self.error
        return {
            "group": self.group,
            "strategy": self.note,
            "rounds": self.rounds,
            "concurrency": self.concurrency,
            "requests": total,
            "ok_201": self.ok,
            "conflict_409": self.conflict,
            "errors": self.error,
            "qps": round(total / self.wall_time, 1) if self.wall_time else 0.0,
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "oversold_rounds": self.oversold_rounds,
        }


# ---------------------------------------------------------------- 环境准备


def _psycopg_dsn() -> str:
    return DB_URL.replace("postgresql+psycopg2://", "postgresql://")


def set_constraint(on: bool) -> None:
    """装卸排他约束。A/B/C 组必须先卸掉，否则测不出应用层方案的真实行为。"""
    import psycopg2

    conn = psycopg2.connect(_psycopg_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE bookings "
                "DROP CONSTRAINT IF EXISTS bookings_no_overlap_resource"
            )
            if on:
                cur.execute(
                    f"""
                    ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap_resource
                    EXCLUDE USING gist (
                        resource_id WITH =,
                        tstzrange(start_at, end_at, '[)') WITH &&
                    ) WHERE (status IN ({OCCUPYING}))
                    """
                )
    finally:
        conn.close()


def truncate_bookings() -> None:
    """组间清理。A/B/C 会留下重叠数据，不清掉的话加约束那一步会直接失败。"""
    import psycopg2

    conn = psycopg2.connect(_psycopg_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE bookings, booking_events, payments, outbox CASCADE")
    finally:
        conn.close()


def start_server(strategy: str, port: int, *, worker: bool = False) -> subprocess.Popen:
    """起一个压测实例。

    worker=True 时启用超时释放 worker（release 组需要它消费 ZSET，
    否则队列永远不归零、只能等到超时）。
    """
    env = dict(os.environ)
    env.update(
        {
            "CONFLICT_STRATEGY": strategy,
            # 压测实例必须把池子放大：默认 10+5 在 32 并发下会让请求在
            # 应用层排队，把真正的竞争窗口（SELECT 与 INSERT 之间）压没了，
            # 结果就是 A 组"看起来没超卖"——那是环境问题，不是代码正确。
            # 池子按「同一时刻只有一个实例在跑」来配：
            # 早先是 50+50，五个实例同时起 = 500 连接，直接把 PG 的默认上限 100 顶穿，
            # E 组前的 truncate 报 "FATAL: sorry, too many clients already"。
            # 现在改成每组独立起停 + 每实例 20+10，单实例 30 条连 32 并发都够用。
            "DB_POOL_SIZE": "20",
            "DB_MAX_OVERFLOW": "10",
            # A 组：把「判断」到「写入」的窗口拉到真实业务量级（5ms：校验+算价+写审计）
            "NAIVE_WINDOW_MS": "5" if strategy == "naive" else "0",
            # 默认关掉 worker：它会释放超时的单，干扰"同一时段只能一个人成交"的判定
            "EXPIRY_WORKER_ENABLED": "true" if worker else "false",
            # 兜底扫描永远关（release 组要测的是 ZSET 这条路径，不能让 sweep 代劳）
            "EXPIRY_SWEEP_ENABLED": "false",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5, trust_env=False)
            if resp.status_code == 200:
                return proc
        except Exception:  # noqa: BLE001 还没起来
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"服务没有在 20 秒内起来（port={port}, strategy={strategy}）")


def stop_server(proc: subprocess.Popen, *, grace: float = 1.5) -> None:
    """停实例并等 PG 回收连接。

    不等的话，下一个实例 / 下一条 psycopg2 连接可能正好撞上还没回收的 backend，
    又变成 too many clients——这个坑在压测里特别隐蔽，因为它是"上一组留下的"。
    """
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    time.sleep(grace)


# ---------------------------------------------------------------- 压测主体


class Seed:
    """资源与时段规划：保证每组压测的 (resource, day, slot) 互不重复。

    如果两轮拿到同一个时段，第二轮的 409 就不是并发冲突，而是上一轮的脏数据。
    """

    def __init__(self, base_url: str, resources: int = 4) -> None:
        self.base_url = base_url
        self.resource_count = resources
        self.resource_ids: list[int] = []
        self._cursor = 0
        self._base_day = (datetime.now(TZ) + timedelta(days=3)).date()
        self._hours = list(range(8, 20))

    def next_slot(self) -> tuple[int, datetime, datetime]:
        per_day = self.resource_count * len(self._hours)
        idx = self._cursor
        self._cursor += 1
        day = self._base_day + timedelta(days=idx // per_day)
        inner = idx % per_day
        resource = self.resource_ids[inner % self.resource_count]
        hour = self._hours[(inner // self.resource_count) % len(self._hours)]
        start = datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(hour=hour)
        return resource, start, start + timedelta(minutes=30)

    def prepare(self, client: httpx.Client) -> None:
        for i in range(self.resource_count):
            name = f"bench-{uuid.uuid4().hex[:8]}-{i}"
            r = client.post(
                "/api/v1/resources",
                json={
                    "name": name,
                    "kind": "room",
                    "price_cents": 20_000,
                    "slot_minutes": 30,
                    "min_minutes": 30,
                    "max_minutes": 240,
                },
            )
            r.raise_for_status()
            rid = r.json()["id"]
            self.resource_ids.append(rid)
            hours = [
                {"weekday": wd, "open_time": "08:00:00", "close_time": "20:00:00"}
                for wd in range(7)
            ]
            r = client.put(f"/api/v1/resources/{rid}/business-hours", json={"hours": hours})
            r.raise_for_status()


def _post(client: httpx.Client, path: str, body: dict, headers: dict | None = None):
    start = time.perf_counter()
    try:
        resp = client.post(path, json=body, headers=headers or {})
        return resp.status_code, time.perf_counter() - start
    except Exception:  # noqa: BLE001 连接失败按 0 记，避免把网络问题算成业务结果
        return 0, time.perf_counter() - start


def run_group(key: str, rounds: int, concurrency: int) -> GroupResult:
    """线程池 + Barrier：所有线程在同一瞬间放行，才叫并发。

    为什么不用 asyncio.gather：实测在 Windows 上单进程 uvicorn 跑 asyncio 客户端，
    64 个并发请求打 /healthz 要 458ms，而**串行**只要 234ms——并发比串行还慢，
    等于完全没压出并行，A 组因此永远复现不出超卖。
    换成 N 个 OS 线程 + Barrier 之后才能真正同时打到服务端。
    """
    strategy, port, needs_constraint, note = GROUPS[key]
    base_url = f"http://127.0.0.1:{port}"
    result = GroupResult(group=key.upper(), note=note, rounds=rounds, concurrency=concurrency)

    # trust_env=False：不让 shell 里的 HTTP_PROXY 把本地请求塞进代理
    with httpx.Client(
        base_url=base_url,
        timeout=60,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=concurrency + 20,
            max_keepalive_connections=concurrency + 20,
        ),
    ) as client:
        seed = Seed(base_url)
        seed.prepare(client)
        # 预热连接池：否则每轮都要现场做 TCP 握手，请求到达时间被摊开
        with ThreadPoolExecutor(max_workers=concurrency) as warm_pool:
            list(warm_pool.map(lambda _: client.get("/healthz"), range(concurrency)))

        barrier = threading.Barrier(concurrency)

        def attempt(_i: int):
            barrier.wait()  # 卡在这里，直到 concurrency 个线程全部就位
            rid, start, end = slot
            body = {
                "resource_id": rid,
                "user_id": int(uuid.uuid4().int % 100_000) + 1,
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
            }
            return _post(client, "/api/v1/bookings", body)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for _ in range(rounds):
                slot = seed.next_slot()
                results = list(pool.map(attempt, range(concurrency)))

                created = 0
                for status, elapsed in results:
                    result.latencies.append(elapsed)
                    if status == 201:
                        created += 1
                        result.ok += 1
                    elif status == 409:
                        result.conflict += 1
                    else:
                        result.error += 1
                # 一轮里出现 2 个以上 201 = 两笔都插入成功 = 超卖
                # 注意缩进：这行必须在 for 轮次循环体内，否则只统计最后一轮
                if created > 1:
                    result.oversold_rounds += 1
        result.wall_time = time.perf_counter() - started

    return result


def run_release(rounds: int) -> dict:
    """难点 3：超时释放的吞吐与残留。

    直接写 DB + ZSET（不走 HTTP，5000 次请求太慢），状态与线上一致：
    bookings 里 pending_payment + expires_at 已过，Redis 里 score 也已到期。
    """
    import psycopg2
    import redis

    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6390/0"))
    conn = psycopg2.connect(_psycopg_dsn())
    expired = datetime.now(TZ) - timedelta(seconds=1)
    total = 0

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM resources WHERE name LIKE 'bench-%' ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row is None:
                return {"error": "没有 bench 资源，请先跑一次 d 组"}
            rid = row[0]
            # 每轮用不同的 user_id：所有单都挂在同一个 user 上时，
            # 第二轮的时段（now()+g 小时，now 已经往后走了 17 秒）会和第一轮
            # 尚未释放的尾部区间重叠，直接撞 bookings_no_overlap_user。
            cur.execute(
                "DELETE FROM bookings WHERE user_id BETWEEN 999999 AND %s",
                (999_999 + rounds,),
            )
            conn.commit()
            # 必须清空 ZSET：历史残留的 id 指向已被删掉的订单，worker 处理它们
            # 不会释放任何东西（CAS 命中 0 行），但 zcard 会一直是正数，
            # 于是「等到积压归零」这个条件永远不成立，每轮都白等满超时。
            r.delete("hold:expiry")

            started = time.perf_counter()
            for round_index in range(rounds):
                uid = 999_999 + round_index
                cur.execute(
                    """
                    INSERT INTO bookings (resource_id, user_id, start_at, end_at, status,
                                          amount_cents, refunded_cents, version, expires_at)
                    SELECT %s, %s,
                           now() + ((g + %s) * interval '1 hour'),
                           now() + ((g + %s) * interval '1 hour') + interval '30 min',
                           'pending_payment', 10000, 0, 0, %s
                    FROM generate_series(1, 1000) g
                    RETURNING id
                    """,
                    (rid, uid, round_index * 2000, round_index * 2000, expired),
                )
                ids = [x[0] for x in cur.fetchall()]
                conn.commit()
                r.zadd("hold:expiry", {str(i): expired.timestamp() for i in ids})
                total += len(ids)

                # 1000 单按实测 ~60 单/秒，30 秒足够；给一倍余量
                deadline = time.time() + 60
                while time.time() < deadline and r.zcard("hold:expiry") > 0:
                    time.sleep(0.2)

            elapsed = time.perf_counter() - started
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM bookings "
                    "WHERE status='pending_payment' AND expires_at < now()"
                )
                leftover = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "group": "RELEASE",
        "inserted": total,
        "elapsed_s": round(elapsed, 2),
        "throughput_per_s": round(total / elapsed, 1) if elapsed else 0,
        "leftover": leftover,
        "note": f"rounds={rounds} 每轮 1000 单；leftover=0 表示没有卡在 pending_payment 的残留",
    }


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", default="a,b,c,d,e", help="逗号分隔，可选 a/b/c/d/e/release")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--release-rounds", type=int, default=3, help="release 组：每轮灌 1000 单")
    args = ap.parse_args()

    # 本机 shell 里常年挂着 HTTP_PROXY，本地压测必须排除
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    wanted = [g.strip().lower() for g in args.groups.split(",") if g.strip()]
    RESULTS_DIR.mkdir(exist_ok=True)

    results: list[dict] = []
    for key in wanted:
        if key == "release":
            continue
        strategy, port, needs_constraint, note = GROUPS[key]
        print(f"\n=== {key.upper()} 组：{note} ===")

        # 顺序很重要：先清库/装卸约束（此刻没有任何应用实例，连接最宽裕），
        # 再起实例，跑完立刻停。早先是五个实例一起起，500 条连接直接把 PG 顶穿。
        truncate_bookings()
        set_constraint(needs_constraint)
        print(f"排他约束: {'已启用' if needs_constraint else '已卸载（对照组）'}")

        proc = start_server(strategy, port)
        try:
            res = run_group(key, args.rounds, args.concurrency)
        finally:
            stop_server(proc)

        payload = res.summary()
        results.append(payload)
        (RESULTS_DIR / f"{key}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    if "release" in wanted:
        print("\n=== RELEASE 组：超时释放吞吐 ===")
        truncate_bookings()
        set_constraint(True)
        # release 组测的是「ZSET 到期 → worker 消费 → DB CAS 释放」这条链路，
        # 所以这里必须起一个开了 worker 的实例；前面的组都故意关掉 worker。
        proc = start_server("exclude", 8016, worker=True)
        try:
            rel = run_release(args.release_rounds)
        finally:
            stop_server(proc)
        results.append(rel)
        (RESULTS_DIR / "release.json").write_text(
            json.dumps(rel, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps(rel, indent=2, ensure_ascii=False))

    # 收尾：恢复生产配置（约束必须在）。
    # 加约束前必须清掉对照组留下的重叠数据，否则 PG 会拒绝创建：
    # "could not create exclusion constraint ... because of split in range"
    truncate_bookings()
    set_constraint(True)
    (RESULTS_DIR / "all.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n结果写入 {RESULTS_DIR}")

    # 判定：A 组必须测出超卖，D/E 必须 0 超卖，否则说明对照没做对
    by_group = {r["group"]: r for r in results if "oversold_rounds" in r}
    exit_code = 0
    if "A" in by_group and by_group["A"]["oversold_rounds"] == 0:
        print("⚠️  A 组没测出超卖：检查约束是不是真的卸掉了")
        exit_code = 2
    for key in ("D", "E"):
        if key in by_group and by_group[key]["oversold_rounds"] > 0:
            print(f"❌ {key} 组出现超卖：约束没生效！")
            exit_code = 3
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

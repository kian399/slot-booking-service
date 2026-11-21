from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: str = "dev"
    LOG_LEVEL: str = "INFO"

    # 端口错开项目 A（短链 5432/6379/8000）
    DATABASE_URL: str = "postgresql+psycopg2://slot:slot@127.0.0.1:5544/slot_booking"
    REDIS_URL: str = "redis://127.0.0.1:6390/0"

    # 连接池按「每进程」算。多 worker 时要自己乘一下，别顶穿 PG 的 max_connections。
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 5

    # ---- 业务规则（放配置，压测/演示时可调，避免改代码）----
    HOLD_MINUTES: int = 15
    MAX_ADVANCE_DAYS: int = 30
    SLOT_MINUTES: int = 30
    # 候补补位后的 hold 时长
    WAITLIST_HOLD_MINUTES: int = 5

    # ---- 并发策略开关（实施方案 3.11 的 A~E 五组对照）----
    # exclude 是生产实现，其余只在压测里用：
    # naive / row_lock / optimistic / exclude / redis_lock
    CONFLICT_STRATEGY: str = "exclude"
    # 仅 naive 策略用：在「查完冲突」和「插入」之间插入的业务耗时（毫秒）。
    # 真实系统这两步之间还有算价、校验、写审计，窗口是毫秒级的；
    # 不显式化的话，单机上 SELECT→INSERT 只隔 10 微秒，永远撞不上，
    # 于是「先查后插」看起来毫无问题——这正是这类 bug 在 demo 里查不出来的原因。
    NAIVE_WINDOW_MS: float = 0.0

    # ---- 延迟队列 ----
    EXPIRY_WORKER_ENABLED: bool = True
    EXPIRY_POLL_INTERVAL: float = 1.0
    EXPIRY_BATCH_SIZE: int = 100
    # DB 兜底扫描：Redis ZADD 失败（双写不一致）时靠它捞回来
    EXPIRY_SWEEP_ENABLED: bool = True
    EXPIRY_SWEEP_INTERVAL: float = 60.0

    METRICS_ENABLED: bool = True


settings = Settings()

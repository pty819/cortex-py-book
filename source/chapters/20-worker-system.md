# 第20章 Worker 系统

## 概述

cortex 的 Worker 系统基于 **Postgres-as-queue** 模式，无需 Redis 等外部队列组件。所有任务通过 `jobs` 表管理，worker 通过 `SKIP LOCKED` 原子抢任务。

```{mermaid}
graph TB
    subgraph 写入
        A[append_event] --> B[enqueue_job]
    end
    
    subgraph Worker 循环
        C[claim_next_job] --> D{执行任务}
        D -->|成功| E[complete_job]
        D -->|失败| F[exponential backoff]
        F -->|未超限| C
        F -->|超限| G[dead letter]
    end
    
    subgraph 守护
        H[reap_zombies] --> C
    end
    
    B --> C
```

## Jobs 表结构

```sql
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,       -- 'extract' | 'consolidate' | ...
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    priority        INT NOT NULL DEFAULT 0,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),   -- 退避时间
    locked_by       TEXT,                -- worker ID
    locked_at       TIMESTAMPTZ,
    payload         JSONB,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_jobs_queue ON jobs 
    (priority DESC, run_after, created_at) WHERE status = 'queued';
```

## 原子抢任务

```python
def claim_next_job(conn, worker_id):
    """SKIP LOCKED 原子抢一个到期 job"""
    row = conn.execute(text("""
        UPDATE jobs SET status='running', locked_by=:w, locked_at=now(), 
                        started_at=now(), attempts=attempts+1
        WHERE job_id = (SELECT job_id FROM jobs
                        WHERE status='queued' AND run_after <= now()
                        ORDER BY priority DESC, run_after, created_at
                        FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING job_id, job_type, scope, event_id, attempts, max_attempts, payload
    """), {"w": worker_id}).fetchone()
    if not row:
        return None
    return {"job_id": str(row.job_id), "job_type": row.job_type, ...}
```

**SKIP LOCKED** 是 PostgreSQL 9.5+ 的特性，允许多个 worker 并发抢任务而不互相阻塞。

## 完成与失败处理

### 成功完成

```python
def complete_job(conn, job_id, result=None):
    conn.execute(text("""
        UPDATE jobs SET status='completed', completed_at=now(), 
                        result=CAST(:r AS jsonb)
        WHERE job_id=CAST(:j AS uuid)
    """), {"r": json.dumps(result) if result else None, "j": job_id})
```

### 失败 + 退避重试

```python
def fail_job(conn, job_id, error, backoff_base=4, terminal=False):
    info = conn.execute(text("""
        SELECT attempts, max_attempts FROM jobs WHERE job_id=CAST(:j AS uuid)
    """), {"j": job_id}).fetchone()
    
    if terminal or (info and info.attempts >= info.max_attempts):
        # 死信：标记为 failed
        conn.execute(text("""
            UPDATE jobs SET status='failed', error=:e, completed_at=now()
            WHERE job_id=CAST(:j AS uuid)
        """), {"e": error[:500], "j": job_id})
    else:
        # 指数退避：下次执行时间 = now + 4^attempts 秒
        conn.execute(text("""
            UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL,
                            run_after=now() + make_interval(secs => :backoff), error=:e
            WHERE job_id=CAST(:j AS uuid)
        """), {"backoff": float(backoff_base ** info.attempts), "e": error[:500], "j": job_id})
```

**退避策略**：
- 第 1 次失败：4 秒后重试
- 第 2 次失败：16 秒后重试
- 第 3 次失败（超限）：死信

## 僵尸回收

当 worker 崩溃时，正在 running 的 job 会变成僵尸：

```python
def reap_zombies(conn, visibility_secs=300):
    """回收超时的 running jobs"""
    r = conn.execute(text("""
        UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL
        WHERE status='running' 
          AND locked_at < now() - make_interval(secs => :v)
    """), {"v": float(visibility_secs)})
    return r.rowcount or 0
```

默认 visibility timeout 来自 `cfg.worker.visibility_timeout_secs`（默认 300 秒）。reaper 的触发间隔来自 `cfg.worker.reaper_interval_secs`（默认 60 秒）。超过 `visibility_timeout_secs` 未完成的任务自动回到队列。

## Worker 主循环

```python
def run_worker(*, max_iterations=0):
    cfg = load_config()
    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    poll = cfg.worker.poll_interval_secs
    vis = cfg.worker.visibility_timeout_secs
    while max_iterations == 0 or it < max_iterations:
        cfg = load_config()  # 按 YAML mtime 自动刷新
        poll = cfg.worker.poll_interval_secs
        vis = cfg.worker.visibility_timeout_secs
        with session_scope() as conn:
            job = claim_next_job(conn, worker_id)
        if not job:
            if now - last_reap > cfg.worker.reaper_interval_secs:
                with session_scope() as conn:
                    reap_zombies(conn, vis)              # 用 vis 而非硬编码
                    _maybe_schedule_dreaming(conn, ...)  # 复用同一 session
                last_reap = now
            time.sleep(poll)
            continue
        heartbeat_interval = max(1.0, min(60.0, vis / 3.0))
        with _JobExecutionLock(job["job_id"]):
            with _JobHeartbeat(job["job_id"], worker_id, heartbeat_interval):
                result = _dispatch(job)
                with session_scope() as conn:
                    complete_job(conn, job["job_id"], worker_id, result)
```

### `_dispatch`：8 种 job 类型

`runner.py` 的 `_dispatch` 是 worker 的分派核心，按 `job_type` 路由到对应处理器。共 8 种：

| job_type | 处理函数 | 说明 |
|----------|----------|------|
| `extract` | `extract_event(event_id)` | 同步抽取三元组 + 实体 + 索引 |
| `segment` | `segment_event(event_id)` | 对长文本 event 做分段切片 |
| `methylation` | `methylation_run(scope, older_than_days=...)` | 软剪枝长期不召回的 events；`older_than_days` 从 payload 取（默认 30） |
| `consolidate` | `consolidation_run(scope)` | 完整语义身份相同的 legacy duplicates 去重 |
| `enrich` | 内联三段短事务 | 扫无 embedding 实体 → 会话外批量 `embed_texts` → 写回短事务 |
| `synthesize` | `synthesize_scope(scope, topics=...)` | 跨 facts 合成摘要/结论；`topics` 从 payload 透传 |
| `dream` | `dream_run(scope, **payload)` | 离线巩固：relation_detect + action_plan 两阶段（见下文调度器） |
| `higher_order` | `generate_higher_order(entity_id, new_fact_id=...)` | 高阶归纳：基于证据摘要生成抽象概念节点；无 scope 参数 |

```python
def _dispatch(job):
    jt = job["job_type"]
    scope = job.get("scope")
    if jt == "extract" and job.get("event_id"):
        return extract_event(job["event_id"])
    if jt == "segment" and scope:
        return segment_scope(scope)
    if jt == "methylation" and scope:
        older = (job.get("payload") or {}).get("older_than_days", 30)
        return methylation_run(scope, older_than_days=older)
    if jt == "consolidate" and scope:
        return consolidation_run(scope)
    if jt == "enrich" and scope:
        ...  # 内联三段短事务
    if jt == "synthesize" and scope:
        payload = job.get("payload") or {}
        return synthesize_scope(scope, topics=payload.get("topics"))
    if jt == "dream" and scope:
        payload = job.get("payload") or {}
        return dream_run(scope, **payload)
    if jt == "higher_order" and scope:
        payload = job.get("payload") or {}
        return generate_higher_order(
            payload.get("entity_id", ""),
            new_fact_id=payload.get("new_fact_id"))
    raise ValueError(f"unsupported job_type: {jt}")
```

## 生命周期事件通知

每次 job 状态变化都触发 lifecycle event，同时通过 `pg_notify` 推送给等待者：

```python
def emit_lifecycle(conn, *, kind, scope, event_id=None, job_id=None, ...):
    row = conn.execute(text("""...""")).fetchone()
    # NOTIFY 让 ?wait= 的 listener 能立刻收到
    conn.execute(text("SELECT pg_notify('cortex_lc', :msg)"),
                 {"msg": f"{kind}|{event_id or ''}"})
```

### ?wait= 同步等待

API 支持 `?wait=indexed` 参数，阻塞直到该 event 的抽取完成。`wait_for_stage` 用独立连接 LISTEN/NOTIFY，cortex 已从 psycopg2 迁移到 psycopg3——后者提供原生的 `conn.notifies(timeout=1.0)` 生成器，替代了 psycopg2 时代的手动 `select.select` + `conn.poll()` + `notifies.pop()` 轮询：

```python
import psycopg  # psycopg3(不再是 psycopg2)

def wait_for_stage(event_id, target_stage, timeout=30.0):
    """LISTEN + 轮询,等待目标 stage。用独立连接绕过 SQLAlchemy 池。"""
    # 先查已有(可能已处理完)
    with session_scope() as c:
        done = [r[0] for r in c.execute(text(
            "SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
            {"e": event_id}).fetchall()]
        if _stage_reached(done, target_stage):
            return {"reached": True, ...}

    # LISTEN 独立连接(autocommit,绕过 SQLAlchemy 池)
    # URL 需从 SQLAlchemy 格式(postgresql+psycopg://)剥成裸串(postgresql://),
    # psycopg3 直连接受裸 libpq 连接串。
    raw_url = cfg.database.url.replace("+psycopg", "")
    conn = psycopg.connect(raw_url, autocommit=True)
    try:
        conn.execute("LISTEN cortex_lc")
        remaining = timeout - (time.time() - t0)
        while remaining > 0:
            # psycopg3 的 notifies() 是 generator,自带 timeout 参数
            for notify in conn.notifies(timeout=1.0):
                if notify.payload and "|" in notify.payload:
                    kind, eid = notify.payload.split("|", 1)
                    if eid == event_id and _stage_reached([kind], target_stage):
                        return {"reached": True, ...}
                break  # notifies timeout=1.0 到时也会退出 for
            # 超时后也查一次表(notify 可能丢失或积压)
            with session_scope() as c:
                ...
            remaining = timeout - (time.time() - t0)
        return {"reached": False, "note": "timeout, downgraded to async"}
    finally:
        conn.close()
```

psycopg3 的优势：`notifies(timeout=1.0)` 是阻塞式 generator，超时自动返回，不需要手动管理文件描述符的 select/poll。配合 `autocommit=True` 绕过 SQLAlchemy 连接池（LISTEN 需要长持连接，不适合池化复用）。

**stage 顺序**：
```
captured(0) → extracted(1) → indexed(2) → consolidated(3)
```

## Dreaming 调度器与心跳

Dreaming（离线巩固）是耗时最长的 job 类型——它跑两阶段 LLM 管线（relation_detect + action_plan），单次执行常远超 reap_zombies 的 `visibility_timeout`（300s）。为此 worker 内建两个机制：轻量调度器和心跳续约。

### `_maybe_schedule_dreaming`：in-worker 轻量调度器

worker 主循环每轮调用一次，检查每个 scope 上一次完成的 dreaming 运行是否已超过 `schedule_interval_hours`，若是则入队一个 `dream` job。

**去重守卫**：入队前先查 `jobs` 表，确认该 scope 不存在 `status IN ('queued','running')` 的 dream job，避免重复插入。

```python
def _maybe_schedule_dreaming(conn, cfg, last_check, now):
    """每个 scope 按 schedule_interval_hours 周期性触发 dream job。
    查 dreaming_runs 表(不是 jobs 表)获取上次完成时间——dreaming 有独立运行记录表。"""
    if not cfg.dreaming.enabled:
        return last_check
    interval = cfg.dreaming.schedule_interval_hours * 3600
    if now - last_check < interval:
        return last_check
    scopes = conn.execute(text(
        "SELECT DISTINCT scope FROM facts WHERE recorded_to IS NULL")).fetchall()
    for (sc,) in scopes:
        last = conn.execute(text(
            "SELECT completed_at FROM dreaming_runs WHERE scope=:s AND status='completed' "
            "ORDER BY started_at DESC LIMIT 1"), {"s": sc}).fetchone()
        should_enqueue = (not last or not last[0]) or (now - last[0].timestamp() > interval)
        if should_enqueue:
            # 去重守卫:已有 queued/running 的 dream job 则跳过
            already = conn.execute(text(
                "SELECT 1 FROM jobs WHERE job_type='dream' AND scope=:s "
                "AND status IN ('queued','running') LIMIT 1"), {"s": sc}).fetchone()
            if not already:
                # 内联 INSERT(此时已在 session 内,enqueue_job 会另开 session)
                conn.execute(text("""INSERT INTO jobs (job_type, scope, priority, payload)
                    VALUES ('dream', :s, -1, '{"min_age_hours": 0}'::jsonb)"""), {"s": sc})
    return now
```

> 查 `dreaming_runs` 而非 `jobs`——dreaming 有独立的运行记录表（含 `started_at`/`completed_at`/`status`），比从 `jobs` 表推断更准确。入队用内联 `INSERT INTO jobs` 而非 `enqueue_job()`，因为此处已在 `session_scope` 内，`enqueue_job` 内部会另开 session 导致嵌套。

### `_JobHeartbeat`：心跳续约线程

所有 job 共用的 `_JobHeartbeat`（带 `worker_id` owner-fencing），在 `run_worker` 的 job 循环外层包裹每个 job，不是 dream 专用、也不在 `_dispatch` 内部。后台线程每 `interval` 秒刷新该 job 的 `locked_at = now()`，使 `reap_zombies`（visibility_timeout）不会把正在跑的长任务误判为僵尸而重新入队。

心跳间隔是动态的，基于 `cfg.worker.visibility_timeout_secs`(`vis`) 计算：`max(1.0, min(60.0, vis/3.0))`——不是固定 60s。默认 `vis=300` 时，心跳间隔为 `min(60.0, 100.0) = 60.0`s；若 `vis` 调小，心跳会自动加快以保持"三次心跳超时才回收"的安全余量。

心跳通过 `@contextmanager` 包装整个 `_dispatch` + `complete_job`——进入时起线程，退出时停线程；续约时走 `heartbeat_job(conn, job_id, worker_id)`，lease 丢失（owner 不再是自己）则自动停止续租：

```python
from contextlib import contextmanager

@contextmanager
def _JobHeartbeat(job_id: str, worker_id: str, interval: float = 60.0):
    """所有 job 共用 owner-fenced heartbeat；lease 丢失后停止续租。"""
    stop = threading.Event()

    def _beat():
        while not stop.wait(interval):
            try:
                with session_scope() as c:
                    if not heartbeat_job(c, job_id, worker_id):
                        stop.set()
            except Exception:
                pass  # 心跳失败不阻断主任务

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=min(interval, 1.0))

# 在 run_worker 的 job 循环中（_dispatch 外层）：
heartbeat_interval = max(1.0, min(60.0, vis / 3.0))
with _JobExecutionLock(job["job_id"]):
    with _JobHeartbeat(job["job_id"], worker_id, heartbeat_interval):
        result = _dispatch(job)
        with session_scope() as conn:
            if not complete_job(conn, job["job_id"], worker_id, result):
                log.warning("lost lease before completing")
                continue
            emit_lifecycle(conn, ...)
```

> `_dispatch` 内部不再有 `_DreamHeartbeat`——心跳在 `run_worker` 外层统一包裹所有 job 类型，dream 与其他 job 一视同仁。`@contextmanager` 比手写 `__enter__`/`__exit__` class 更简洁；心跳内部自行 `session_scope()`，不需要调用方传入 `conn_factory`。

> 设计要点：心跳走 `heartbeat_job` 带 owner-fencing（只有持有 lease 的 worker_id 能续租），续不上或被抢占就自动停止；任务真正结束仍由 `complete_job` / `fail_job` 处理。心跳线程是 daemon，worker 进程崩溃时不会卡住退出。

## 启动 Worker

```bash
# 启动 worker 循环
uv run python -m cortex.interfaces.cli worker

# 启动后端（另一个终端）
uv run uvicorn cortex.interfaces.api.app:app --port 8002
```

## 关键设计决策

| 决策 | 方案 | 理由 |
|------|------|------|
| 队列实现 | Postgres SKIP LOCKED | 无 Redis 依赖，减少运维复杂度 |
| 退避策略 | 指数退避 (4^n s) | 避免瞬时失败风暴 |
| 超限处理 | 死信 (failed) | 不自动丢弃，可人工介入 |
| 僵尸回收 | 5 分钟 visibility timeout | 防止 worker 崩溃后任务永久丢失 |
| 通知机制 | pg_notify | 零额外组件，Postgres 原生支持 |

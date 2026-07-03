# 第16章 Worker 系统

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

默认 visibility timeout = 300 秒。超过 5 分钟未完成的任务自动回到队列。

## Worker 主循环

```python
def worker_loop():
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    while True:
        with session_scope() as conn:
            # 1. 回收僵尸
            reaped = reap_zombies(conn, visibility_secs=300)
            
            # 2. 抢任务
            job = claim_next_job(conn, worker_id)
        
        if not job:
            time.sleep(1)  # 空转等待
            continue
        
        try:
            if job["job_type"] == "extract":
                result = extract_event(job["event_id"])
            elif job["job_type"] == "consolidate":
                result = consolidate_scope(job["scope"])
            
            with session_scope() as conn:
                complete_job(conn, job["job_id"], result)
        except Exception as e:
            with session_scope() as conn:
                fail_job(conn, job["job_id"], str(e))
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

API 支持 `?wait=indexed` 参数，阻塞直到该 event 的抽取完成：

```python
def wait_for_stage(event_id, target_stage, timeout=30.0):
    """LISTEN pg_notify + 轮询，等待目标 stage"""
    conn = psycopg2.connect(cfg.database.url)
    conn.autocommit = True
    conn.execute("LISTEN cortex_lc")
    while time.time() - t0 < timeout:
        # 查表（通知可能已积压）
        # 等 notify（最多 1s）
        select.select([conn], [], [], 1.0)
        conn.poll()
        # 检查通知内容
        if payload matches target_stage:
            return {"reached": True, ...}
    return {"reached": False, "note": "timeout, downgraded to async"}
```

**stage 顺序**：
```
captured(0) → extracted(1) → indexed(2) → consolidated(3)
```

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

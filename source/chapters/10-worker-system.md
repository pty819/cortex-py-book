# 第10章 Worker 系统

## Postgres-as-Queue 设计

### 为什么不用 Redis?

```{admonition} 设计决策
1. **简单** —— 少一个依赖，少一个运维负担
2. **事务一致** —— 写 WAL 和入队在同一事务
3. **足够快** —— 个人/小团队规模，Postgres 完全够用
4. **SKIP LOCKED** —— 高效的任务抢占，避免锁竞争
```

## 队列表设计

### Schema

```sql
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    payload         JSONB,
    
    -- 状态机
    status          TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    
    -- 时间跟踪
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at       TIMESTAMPTZ,
    locked_by       TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    
    -- 错误信息
    error           TEXT,
    
    -- 约束
    CONSTRAINT valid_status CHECK (status IN ('queued', 'running', 'completed', 'failed'))
);
```

### 状态机

```{mermaid}
stateDiagram-v2
    [*] --> queued: 创建任务
    queued --> running: Worker 抢锁
    running --> completed: 成功
    running --> failed: 失败
    running --> queued: 超时 (reaper)
    failed --> queued: 重试 (attempts < max)
    failed --> [*]: 最终失败 (attempts >= max)
    completed --> [*]: 完成
```

## 任务抢占

### SKIP LOCKED 原理

```{mermaid}
sequenceDiagram
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as PostgreSQL
    participant Q as Jobs Table
    
    Note over Q: status=queued, job_id=J1
    
    W1->>DB: BEGIN
    W2->>DB: BEGIN
    
    W1->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    W2->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    
    Note over DB: W1 锁定 J1
    Note over DB: W2 跳过 J1 (SKIP LOCKED)
    
    DB-->>W1: job_id=J1
    DB-->>W2: null
    
    W1->>DB: UPDATE status='running'
    W2->>DB: ROLLBACK
    
    W1->>W1: 执行任务...
```

### 实现

```python
# core.py
def claim_next_job(conn, worker_id: str) -> Optional[dict]:
    """抢占下一个 queued 任务"""
    row = conn.execute(text("""
        UPDATE jobs 
        SET 
            status = 'running',
            locked_at = now(),
            locked_by = :wid,
            started_at = now(),
            attempts = attempts + 1
        WHERE job_id = (
            SELECT job_id 
            FROM jobs
            WHERE status = 'queued'
              AND attempts < max_attempts
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
    """), {"wid": worker_id}).fetchone()
    
    return dict(row) if row else None
```

## Worker 主循环

### 流程图

```{mermaid}
flowchart TD
    A[Worker 启动] --> B[初始化]
    B --> C[主循环]
    
    C --> D{claim_next_job}
    D -->|有任务| E{job_type?}
    D -->|无任务| F[sleep poll_interval]
    F --> C
    
    E -->|extract| G[extraction_pipeline]
    E -->|segment| H[segment_scope]
    E -->|synthesize| I[synthesize_scope]
    E -->|methylation| J[methylation_run]
    E -->|consolidate| K[consolidation_run]
    E -->|enrich| L[enrich_entities]
    
    G --> M[complete_job]
    H --> M
    I --> M
    J --> M
    K --> M
    L --> M
    
    G -->|失败| N[fail_job]
    H -->|失败| N
    I -->|失败| N
    
    M --> C
    N --> C
    
    C -->|Ctrl-C| O[退出]
```

### 完整实现

```python
# worker/runner.py
def run_worker(*, max_iterations: int = 0) -> None:
    """阻塞跑 worker"""
    cfg = load_config()
    worker_id = f"worker-{int(time.time()) % 100000}"
    poll = cfg.worker.poll_interval_secs
    vis = cfg.worker.visibility_timeout_secs
    last_reap = 0.0
    it = 0
    
    log.info("worker %s started (poll=%.2fs vis=%ss)", 
             worker_id, poll, vis)
    
    while max_iterations == 0 or it < max_iterations:
        it += 1
        try:
            with session_scope() as conn:
                # 定期回收僵尸任务
                if time.time() - last_reap > cfg.worker.reaper_interval_secs:
                    reaped = reap_zombies(conn, vis)
                    if reaped:
                        log.info("reaped %d zombie jobs", reaped)
                    last_reap = time.time()
                
                # 抢任务
                job = claim_next_job(conn, worker_id)
                
                if not job:
                    # 没任务，sleep
                    conn.execute(text("SELECT pg_sleep(:s)"), {"s": poll})
                    continue
                
                log.info("worker %s claimed job %s (%s)", 
                        worker_id, job["job_id"], job["job_type"])
                
                # 分发执行
                try:
                    result = _dispatch(job)
                    complete_job(conn, job["job_id"], result)
                    log.info("job %s completed: %s", 
                            job["job_id"], result)
                except Exception as e:
                    log.exception("job %s failed", job["job_id"])
                    fail_job(conn, job["job_id"], str(e))
                    
        except KeyboardInterrupt:
            log.info("worker %s interrupted", worker_id)
            break
        except Exception:
            log.exception("worker loop error")
            time.sleep(1)
    
    log.info("worker %s exiting", worker_id)
```

## 任务分发

### 分发器实现

```python
def _dispatch(job: dict) -> dict:
    """按 job_type 跑对应 handler"""
    jt = job["job_type"]
    scope = job.get("scope")
    
    if jt == "extract" and job.get("event_id"):
        from ..extraction.pipeline import extract_event
        return extract_event(job["event_id"])
    
    if jt == "segment" and scope:
        from ..episodes import segment_scope
        return segment_scope(scope)
    
    if jt == "synthesize" and scope:
        from ..understanding import synthesize_scope
        payload = job.get("payload") or {}
        return synthesize_scope(scope, topics=payload.get("topics"))
    
    if jt == "methylation" and scope:
        from ..maintenance import methylation_run
        older = (job.get("payload") or {}).get("older_than_days", 30)
        return methylation_run(scope, older_than_days=older)
    
    if jt == "consolidate" and scope:
        from ..maintenance import consolidation_run
        return consolidation_run(scope)
    
    if jt == "enrich" and scope:
        # 异步 KG 增强
        return _enrich_entities(scope)
    
    return {"ok": True, "note": f"no handler for {jt}"}
```

### 任务类型

| job_type | 处理器 | 说明 |
|----------|--------|------|
| `extract` | `extract_event` | 从 Event 抽取实体/事实 |
| `segment` | `segment_scope` | 事件分段为 Episodes |
| `synthesize` | `synthesize_scope` | 从 Beliefs 合成 Understanding |
| `methylation` | `methylation_run` | 老化处理 |
| `consolidate` | `consolidation_run` | 合并处理 |
| `enrich` | `_enrich_entities` | 异步计算 embedding |

## 任务完成与失败

### 完成任务

```python
def complete_job(conn, job_id: str, result: dict):
    """标记任务完成"""
    conn.execute(text("""
        UPDATE jobs 
        SET 
            status = 'completed',
            completed_at = now(),
            payload = payload || :result
        WHERE job_id = :jid
    """), {
        "jid": job_id,
        "result": json.dumps({"result": result})
    })
```

### 失败任务

```python
def fail_job(conn, job_id: str, error: str):
    """标记任务失败"""
    conn.execute(text("""
        UPDATE jobs 
        SET 
            status = CASE 
                WHEN attempts < max_attempts THEN 'queued'
                ELSE 'failed'
            END,
            locked_at = NULL,
            locked_by = NULL,
            error = :err
        WHERE job_id = :jid
    """), {
        "jid": job_id,
        "err": error
    })
```

## Visibility Timeout & Reaper

### 问题

```{mermaid}
sequenceDiagram
    participant W as Worker
    participant DB as DB
    
    W->>DB: claim job (status=running)
    Note over W: 执行中...
    Note over W: Worker 崩溃!
    Note over DB: job 卡在 running
```

### 解决方案

```{mermaid}
flowchart TD
    A[Reaper 定时检查] --> B{"locked_at < now - timeout"}
    B -->|是| C[重置为 queued]
    B -->|否| D[跳过]
    C --> E[其他 Worker 可抢]
```

### 实现

```python
def reap_zombies(conn, visibility_timeout: int = 300):
    """回收超时的 running 任务"""
    rows = conn.execute(text("""
        UPDATE jobs 
        SET 
            status = 'queued',
            locked_at = NULL,
            locked_by = NULL
        WHERE status = 'running'
          AND locked_at < now() - make_interval(secs => :timeout)
        RETURNING job_id
    """), {"timeout": visibility_timeout}).fetchall()
    
    return len(rows)
```

## Worker 配置

```python
# config.py
class WorkerCfg(BaseModel):
    poll_interval_secs: float = 1.0      # 轮询间隔
    visibility_timeout_secs: int = 300   # 可见性超时 (5分钟)
    reaper_interval_secs: int = 60       # Reaper 检查间隔
    max_attempts: int = 3                # 最大重试次数
    backoff_base_secs: int = 4           # 退避基数
```

## 多 Worker 协作

```{mermaid}
graph TB
    subgraph "Worker Pool"
        W1[Worker 1]
        W2[Worker 2]
        W3[Worker 3]
    end
    
    subgraph "Jobs Table"
        J1["job1 (queued)"]
        J2["job2 (queued)"]
        J3["job3 (queued)"]
    end
    
    W1 -->|claim| J1
    W2 -->|claim| J2
    W3 -->|claim| J3
    
    J1 -->|complete| D1[done]
    J2 -->|fail| D2[retry]
    J3 -->|timeout| D3[reaper]
```

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
            result = _dispatch(job)
            
            with session_scope() as conn:
                complete_job(conn, job["job_id"], result)
        except Exception as e:
            with session_scope() as conn:
                fail_job(conn, job["job_id"], str(e))
```

### `_dispatch`：8 种 job 类型

`runner.py` 的 `_dispatch` 是 worker 的分派核心，按 `job_type` 路由到对应处理器。共 8 种：

| job_type | 处理函数 | 说明 |
|----------|----------|------|
| `extract` | `extract_event(event_id)` | 同步抽取三元组 + 实体 + 索引 |
| `segment` | `segment_event(event_id)` | 对长文本 event 做分段切片 |
| `methylation` | `methylation_run(scope)` | 软剪枝长期不召回的 events |
| `consolidate` | `consolidate_scope(scope)` | 同 S/P/O 重复 facts 去重 |
| `enrich` | `enrich_scope(scope)` | 补充实体/事实的属性与元数据 |
| `synthesize` | `synthesize_scope(scope)` | 跨 facts 合成摘要/结论 |
| `dream` | `dreaming_run(scope)` | 离线巩固：relation_detect + action_plan 两阶段（见下文调度器） |
| `higher_order` | `higher_order_generate(entity_id, scope)` | 高阶归纳：基于证据摘要生成抽象概念节点 |

```python
def _dispatch(job):
    jt = job["job_type"]
    if jt == "extract":
        return extract_event(job["event_id"])
    elif jt == "segment":
        return segment_event(job["event_id"])
    elif jt == "methylation":
        return methylation_run(job["scope"])
    elif jt == "consolidate":
        return consolidate_scope(job["scope"])
    elif jt == "enrich":
        return enrich_scope(job["scope"])
    elif jt == "synthesize":
        return synthesize_scope(job["scope"])
    elif jt == "dream":
        return dreaming_run(job["scope"])
    elif jt == "higher_order":
        return higher_order_generate(
            entity_id=job["payload"].get("entity_id"),
            scope=job["scope"])
    raise ValueError(f"unknown job_type: {jt}")
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

## Dreaming 调度器与心跳

Dreaming（离线巩固）是耗时最长的 job 类型——它跑两阶段 LLM 管线（relation_detect + action_plan），单次执行常远超 reap_zombies 的 `visibility_timeout`（300s）。为此 worker 内建两个机制：轻量调度器和心跳续约。

### `_maybe_schedule_dreaming`：in-worker 轻量调度器

worker 主循环每轮调用一次，检查每个 scope 上一次完成的 dreaming 运行是否已超过 `schedule_interval_hours`，若是则入队一个 `dream` job。

**去重守卫**：入队前先查 `jobs` 表，确认该 scope 不存在 `status IN ('queued','running')` 的 dream job，避免重复插入。

```python
def _maybe_schedule_dreaming(conn):
    """每个 scope 按 schedule_interval_hours 周期性触发 dream job"""
    rows = conn.execute(text("""
        SELECT scope, MAX(completed_at) AS last_done
        FROM jobs WHERE job_type='dream' AND status='completed'
        GROUP BY scope
    """)).fetchall()
    interval = cfg.dreaming.schedule_interval_hours * 3600
    for r in rows:
        last = r.last_done or datetime.min.replace(tzinfo=UTC)
        if (datetime.now(UTC) - last).total_seconds() < interval:
            continue
        # 去重守卫：已有 queued/running 的 dream job 则跳过
        exists = conn.execute(text("""
            SELECT 1 FROM jobs
            WHERE scope=:s AND job_type='dream'
              AND status IN ('queued','running') LIMIT 1
        """), {"s": r.scope}).fetchone()
        if not exists:
            enqueue_job(conn, job_type="dream", scope=r.scope, payload={})
```

### `_DreamHeartbeat`：心跳续约线程

Dream job 执行期间，后台线程每 60 秒刷新该 job 的 `jobs.locked_at = now()`，使 `reap_zombies`（visibility_timeout 300s）不会把正在跑的长任务误判为僵尸而重新入队。

心跳通过 context manager 包装 dream 的 dispatch 调用——进入时起线程，退出时停线程：

```python
class _DreamHeartbeat:
    """每 60s 刷新 jobs.locked_at，防止 reaper 抢走长跑 dream job"""
    def __init__(self, conn_factory, job_id, interval_secs=60):
        self.conn_factory = conn_factory
        self.job_id = job_id
        self.interval = interval_secs
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                with self.conn_factory() as c:
                    c.execute(text("""
                        UPDATE jobs SET locked_at=now()
                        WHERE job_id=CAST(:j AS uuid) AND status='running'
                    """), {"j": self.job_id})
            except Exception:
                pass  # 心跳失败不阻断主任务

# 在 _dispatch 的 dream 分支中：
with _DreamHeartbeat(session_scope, job["job_id"]):
    result = dreaming_run(job["scope"])
```

> 设计要点：心跳只续 `locked_at`，不改 status / attempts；任务真正结束仍由 `complete_job` / `fail_job` 处理。心跳线程是 daemon，worker 进程崩溃时不会卡住退出。

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

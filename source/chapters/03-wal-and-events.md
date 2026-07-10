# 第3章 WAL 与事件系统

## 写入路径概述

```{mermaid}
flowchart LR
    A[用户请求] --> B[Experience API]
    B --> C[WAL Append]
    C --> D[Postgres Queue]
    D --> E[Worker]
    E --> F[Extraction]
```

## WAL (Write-Ahead Log)

### 设计原则

```{admonition} WAL 核心约束
1. **不可变** —— 一旦写入，永不修改
2. **Append-only** —— 只能追加，不能删除（除非 erasure）
3. **唯一真相源** —— 所有派生层从此重建
4. **幂等写入** —— 同 key 同 body 返回既有，不同 body 报错
```

### 幂等机制

```{mermaid}
sequenceDiagram
    participant C as Client
    participant API as Experience API
    participant DB as PostgreSQL
    
    C->>API: POST /experience (key=abc, body=X)
    API->>DB: INSERT event (key=abc, body=X)
    DB-->>API: event_id=123
    API-->>C: 200 {event_id: 123}

    Note over C,API: 重试相同请求

    C->>API: POST /experience (key=abc, body=X)
    API->>DB: SELECT WHERE key=abc
    DB-->>API: event_id=123, body=X
    API-->>C: 200 {event_id: 123} (幂等返回,同 status)
    
    Note over C,API: 不同 body 的请求
    
    C->>API: POST /experience (key=abc, body=Y)
    API->>DB: SELECT WHERE key=abc
    DB-->>API: event_id=123, body=X
    Note over API: X != Y, 冲突!
    API-->>C: 409 Conflict
```

### 实现

```python
def append_event(*, scope, modality, content, context, caller, idempotency_key, **kwargs):
    """append 一个 event。幂等:同 key+同 body → 返回既有;同 key+异 body → raise IdempotencyConflict。"""
    with session_scope() as c:
        # 先查幂等
        existing = c.execute(text(
            "SELECT event_id, wal_offset FROM events WHERE scope=:s AND idempotency_key=:k"
        ), {"s": scope, "k": idempotency_key}).fetchone()
        if existing:
            # 对比 body hash
            ex_body = _body_hash(existing.modality, existing.content, existing.context)
            if ex_body == _body_hash(modality, content, context):
                return str(existing.event_id), existing.wal_offset
            raise IdempotencyConflict(...)
        
        row = c.execute(text("""
            INSERT INTO events (scope, modality, content, context, caller, observed_actor, subject,
                                observed_at, directives, idempotency_key)
            VALUES (...) RETURNING event_id, wal_offset
        """), {...}).fetchone()
        _auto_provision_scope(c, scope)
        emit_lifecycle(c, kind="captured", scope=scope, event_id=row.event_id)
        return str(row.event_id), row.wal_offset
```

## Experience Envelope

每个 Event 遵循 Experience Envelope 格式：

```python
{
    "scope": "org:acme/user:alice",
    "modality": "conversation",
    "content": {
        "kind": "message",
        "role": "user",
        "text": "Alice works at Acme Corp"
    },
    "context": {
        "observed_at": "2024-01-15T10:00:00Z",
        "labels": ["work", "introduction"],
        "intent": "statement"
    },
    "observed_actor": "alice",
    "subject": "alice",
    "directives": {"skip_extraction": False},
    "idempotency_key": "unique-key-123"
}
```

### Schema

```sql
CREATE TABLE events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset          BIGSERIAL UNIQUE NOT NULL,
    scope               TEXT NOT NULL,
    modality            TEXT NOT NULL,
    content             JSONB NOT NULL,
    context             JSONB NOT NULL,
    caller              TEXT NOT NULL,
    observed_actor      TEXT NOT NULL,
    subject             TEXT,
    observed_at         TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    directives          JSONB,
    idempotency_key     TEXT NOT NULL,
    excluded_from_recall BOOLEAN NOT NULL DEFAULT false,
    embed_status        TEXT,
    extraction_diagnostics JSONB NOT NULL DEFAULT '[]',
    access_count        INT NOT NULL DEFAULT 0,
    case_id             TEXT,
    methylated_at       TIMESTAMPTZ,
    feedback_processed  BOOLEAN NOT NULL DEFAULT false,  -- 反馈是否已被处理入权重
    last_recalled_at    TIMESTAMPTZ,                     -- 最近一次召回时间
    
    UNIQUE (scope, idempotency_key)
);
```

## Postgres-as-Queue

### 为什么不用 Redis？

```{admonition} 设计决策
1. **简单** —— 少一个依赖，少一个运维负担
2. **事务一致** —— 写 WAL 和入队在同一事务
3. **足够快** —— 个人/小团队规模，Postgres 完全够用
4. **SKIP LOCKED** —— 高效的任务抢占，避免锁竞争
```

### 队列表

```sql
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    priority        INT NOT NULL DEFAULT 0,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,
    payload         JSONB,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
```

### 状态机

```{mermaid}
stateDiagram-v2
    [*] --> queued: 创建任务
    queued --> running: worker 锁定
    running --> completed: 成功
    running --> failed: 失败
    running --> queued: 重试 (< 3次)
    failed --> [*]: 超过重试次数
    queued --> cancelled: 手动取消
```

### SKIP LOCKED 实现

```python
def claim_next_job(conn, worker_id):
    """原子抢一个到期 job (SKIP LOCKED)。"""
    row = conn.execute(text("""
        UPDATE jobs SET status='running', locked_by=:w, locked_at=now(), 
                        started_at=now(), attempts=attempts+1
        WHERE job_id = (SELECT job_id FROM jobs
                        WHERE status='queued' AND run_after <= now()
                        ORDER BY priority DESC, run_after, created_at
                        LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING job_id, job_type, scope, event_id, payload
    """), {"w": worker_id}).fetchone()
    return dict(row) if row else None
```

## Scope 自动注册

```python
def _auto_provision_scope(conn, scope):
    parts = scope.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        parent = "/".join(parts[:i-1]) if i > 1 else None
        conn.execute(text("""
            INSERT INTO scopes (scope_path, parent_path, auto_provisioned)
            VALUES (:p, :parent, true) ON CONFLICT (scope_path) DO NOTHING
        """), {"p": p, "parent": parent})
```

## Lifecycle Events

```sql
CREATE TABLE lifecycle_events (
    lifecycle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,
    job_id          UUID REFERENCES jobs(job_id),
    payload         JSONB NOT NULL DEFAULT '{}'
);
```

支持的 lifecycle kinds:

| Kind | 含义 |
|------|------|
| `captured` | Event 已写入 WAL（append 成功） |
| `extracted` | 抽取管线完成，已产出 facts/entities |
| `entity_linked` | 实体链接（B over C）完成 |
| `belief_synthesized` | Belief 聚合完成 |
| `import_progress` | bulk 导入进度上报 |
| `import_complete` | bulk 导入全部完成 |
| `feedback_received` | 收到一条用户反馈（正/负投票、采纳信号），写入 `feedback_signals` |
| `dreamed` | Dreaming 离线巩固完成一次 run，知识被合并/去重/抽象 |
| `higher_order_generated` | Higher-Order 模块合成出一条高阶事实（`is_higher_order=true`） |
| `forgotten` | 记忆被遗忘（salience 长期过低或 methylation 升级为真删前的标记） |
| `failed` | 某个 job（抽取/归纳/巩固等）最终失败，超 过重试上限 |
| `indexed` | 向量/全文索引构建完成（embed_status='done'） |
| `erasure_complete` | GDPR 擦除 4 阶段全部完成，相关引用已真删 |

## 完整写入流程

```{mermaid}
sequenceDiagram
    participant C as Client
    participant API as API
    participant DB as PostgreSQL
    participant W as Worker
    
    C->>API: POST /experience
    API->>DB: INSERT event
    API->>DB: INSERT job (extract)
    DB-->>API: event_id, job_id
    API-->>C: 200 {event_id, status}
    
    loop Worker Loop
        W->>DB: SKIP LOCKED claim
        DB-->>W: job
        W->>DB: UPDATE status=running
        W->>W: Extract triples
        W->>DB: INSERT facts
        W->>DB: UPDATE status=completed
    end
```

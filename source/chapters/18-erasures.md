# 第18章 Erasures 系统

## 概述

Erasures 实现了 GDPR 风格的**引用计数真删**——不是软标记，而是物理删除或内容擦除，同时维护引用完整性。

```{mermaid}
graph LR
    subgraph 4 阶段流程
        A[enumerate] --> B[refcount]
        B --> C[delete]
        C --> D[cleanup]
    end
    
    D -->|完成| E[completed]
    A -->|预览| F[preview manifest]
```

## 4 阶段执行

### 阶段 1：Enumerate（列举）

根据 selector 收集命中的 event_id：

```python
def _select_event_ids(conn, scope, selector):
    """memory_ids 直接；about_entity/predicate 走 facts.supports 反查"""
    ids = []
    if selector.get("memory_ids"):
        ids.extend(selector["memory_ids"])
    if selector.get("about_entity") or selector.get("predicate"):
        rows = conn.execute(text("""
            SELECT DISTINCT unnest(f.supports)::text FROM facts f
            WHERE f.scope=:s AND (:a IS NULL OR f.subject_id=CAST(:a AS uuid))
        """), params).fetchall()
        ids.extend(r[0] for r in rows)
    return list(dict.fromkeys(ids))  # 去重
```

### 阶段 2：Refcount（引用计数）

对每个 event 计算引用数——有多少 facts/beliefs 的 `supports` 数组指向它：

```python
def _event_refcount(conn, scope, event_id):
    return conn.execute(text("""
        SELECT (SELECT count(*) FROM facts   WHERE scope=:s AND CAST(:e AS uuid) = ANY(supports))
             + (SELECT count(*) FROM beliefs WHERE scope=:s AND CAST(:e AS uuid) = ANY(supports))
    """), {"s": scope, "e": event_id}).scalar() or 0
```

### 阶段 3：Delete（删除/擦除）

根据引用计数决定：

| 引用计数 | 操作 |
|---------|------|
| `refcount = 0` | **物理删除**整行 |
| `refcount > 0` | **擦除内容**：清空 `content`，设 `excluded_from_recall=true`，保留 event_id + wal_offset |

```python
# refcount = 0：物理删
DELETE FROM events WHERE event_id = :e

# refcount > 0：擦除（保留行，清内容）
UPDATE events SET content='{}', excluded_from_recall=true WHERE event_id = :e
```

### 阶段 4：Cleanup（清理引用）

清理所有指向被删/擦除事件的 supports 引用：

```python
# 从 facts.supports 移除引用
UPDATE facts SET supports = array_remove(supports, :e) 
WHERE scope=:s AND :e = ANY(supports)

# 从 beliefs.supports 移除引用
UPDATE beliefs SET supports = array_remove(supports, :e) 
WHERE scope=:s AND :e = ANY(supports)

# 如果 blob 引用计数归零 → 删除 blob
DELETE FROM blobs WHERE refcount = 0
```

## 事务策略

每个 event 独立事务，避免 PG 事务中毒：

```python
for event_id in manifest["to_delete"]:
    with session_scope() as conn:
        conn.execute(text("DELETE FROM events WHERE event_id=CAST(:e AS uuid)"), ...)
        conn.execute(text("UPDATE facts SET supports=array_remove(supports,:e) ..."), ...)

for event_id in manifest["to_redact"]:
    with session_scope() as conn:
        conn.execute(text("UPDATE events SET content='{}', excluded_from_recall=true ..."), ...)
```

## 预览与执行

### Preview（干跑，不做修改）

```python
def preview_erasure(scope, selector):
    """返回 manifest：哪些删、哪些擦"""
    with session_scope() as conn:
        eids = _select_event_ids(conn, scope, selector)
        to_delete = []
        to_redact = []
        for eid in eids:
            rc = _event_refcount(conn, scope, eid)
            if rc == 0:
                to_delete.append(eid)
            else:
                to_redact.append({"event_id": eid, "refcount": rc})
    return {"to_delete": to_delete, "to_redact": to_redact, ...}
```

### Execute（执行 + 审计）

```python
def execute_erasure(scope, selector, from_preview_id=None):
    # 1. 创建 erasure_job（含 idempotency_key）
    # 2. 枚举 event_ids
    # 3. 逐 event 处理（每 event 独立事务）
    # 4. 记录审计日志
    return {"deleted": n, "redacted": m, "audit_id": audit_id}
```

## 完整 API

```
POST /v1/erasures/preview    → 干跑预览
POST /v1/erasures/execute    → 执行删除
GET  /v1/erasures/status     → 查询任务状态
```

MCP 工具：
```
erasure_preview(about_entity, predicate, scope)  → 预览
erasure_execute(scope, about_entity, predicate)  → 执行
```

## 设计原则

1. **引用计数保安全**：有下游依赖的 event 不物理删，只擦除内容
2. **可审计**：每次删除记录到 erasure_jobs 表 + audit_log
3. **幂等**：同 erasure_job 重复执行安全
4. **逐 event 事务**：防止大批量删除导致的事务膨胀
5. **WAL 保留**：擦除后的 event 保留 wal_offset，不破坏 wal_offset 序列的连续性

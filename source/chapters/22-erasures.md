# 第22章 Erasures 系统

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
    """memory_ids 直接；about_entity 双向匹配(subject_id 或 object_entity_id)/predicate 走 facts.supports 反查"""
    ids = []
    if selector.get("memory_ids"):
        ids.extend(selector["memory_ids"])
    cond = ""
    params = {"s": scope}
    if selector.get("about_entity"):
        # 双向匹配:subject 或 object 任一命中都算
        cond += " AND (f.subject_id=CAST(:a AS uuid) OR f.object_entity_id=CAST(:a AS uuid))"
        params["a"] = selector["about_entity"]
    if selector.get("predicate"):
        cond += " AND f.predicate=:p"
        params["p"] = selector["predicate"]
    if cond:
        rows = conn.execute(text(f"""
            SELECT DISTINCT unnest(f.supports)::text FROM facts f
            WHERE f.scope=:s{cond}
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
# refcount > 0：擦除（保留行，清内容）
UPDATE events SET content='{}'::jsonb, excluded_from_recall=true WHERE event_id = :e

# refcount = 0：物理删前先置空指向该 event 的 FK(jobs/lifecycle_events),
# 否则 FK 约束挡 DELETE
UPDATE jobs SET event_id=NULL WHERE event_id = :e
UPDATE lifecycle_events SET event_id=NULL WHERE event_id = :e
DELETE FROM events WHERE event_id = :e
```

### 阶段 4：Cleanup（清理引用）

清理所有指向被删/擦除事件的 supports 引用（只清 `facts.supports` 和 `beliefs.supports`）：

```python
# 从 facts.supports 移除引用
UPDATE facts SET supports = array_remove(supports, CAST(:e AS uuid))
WHERE scope=:s

# 从 beliefs.supports 移除引用
UPDATE beliefs SET supports = array_remove(supports, CAST(:e AS uuid))
WHERE scope=:s
```

> MVP 不处理 blob 清理——`erasures.py` 无 blob 删除逻辑，blob 的引用计数与回收由其他子系统负责。旧文档中"DELETE FROM blobs WHERE refcount=0"不属此模块。

## 事务策略

每个 event 独立事务，避免 PG 事务中毒：

```python
for event_id in manifest["to_delete"]:
    with session_scope() as conn:
        # 先置空 FK,再 DELETE
        conn.execute(text("UPDATE jobs SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), ...)
        conn.execute(text("UPDATE lifecycle_events SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), ...)
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
def execute_erasure(*, scope, selector=None, from_preview_id=None):
    # 1. preview_erasure（或复用 from_preview_id 的 manifest）
    # 2. 逐 event 处理（每 event 独立事务）
    # 3. 写 erasure_jobs.phase='completed' + progress
    # 4. 审计走 emit_lifecycle(kind="erasure_complete")
    progress = {"deleted": 0, "redacted": 0, "demoted": 0}
    ...
    emit_lifecycle(conn, kind="erasure_complete", scope=scope,
                   payload={"erasure_id": str(erasure_id), "progress": progress})
    return {"erasure_id": str(erasure_id), "phase": "completed", "progress": progress}
```

> `progress` 含三个键：`deleted`/`redacted`/`demoted`（MVP 中 `demoted` 恒为 0，保留键位供未来软降级路径）。审计走 `emit_lifecycle(kind="erasure_complete")`，**无** `audit_id` 返回字段。

## 完整 API

```
POST /v1/erasures                            → 执行删除（创建 erasure_job）
POST /v1/erasures/preview                    → 干跑预览，返回 preview_id（不落库改动）
GET  /v1/erasures/preview/{preview_id}/manifest  → 取预览 manifest（preview 过期返回 409）
GET  /v1/erasures/{erasure_id}               → 查询 erasure 任务状态
POST /v1/erasures/{erasure_id}/cancel        → 取消正在运行的 erasure
```

> 注意路径形态：执行用 `POST /v1/erasures`（不是 `/execute`），状态查询用 `GET /v1/erasures/{erasure_id}`（不是 `/status/{id}`）。preview 与 execute 是两个独立入口——preview 返回的 `preview_id` 可换 manifest，但 execute 本身可不依赖 preview 直接跑。manifest 24 小时后过期（`MANIFEST_TTL_HOURS=24`），过期后取 manifest 返回 `{"expired": True}`、execute 带 `from_preview_id` 会 409。

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

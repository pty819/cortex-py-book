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
    # 去重 + 只保留真实存在于 events 表的(event_id 可能因 FK 置空/残留而失效)
    if not ids:
        return []
    rows = conn.execute(text(
        "SELECT event_id::text FROM events WHERE scope=:s AND event_id = ANY(CAST(:ids AS uuid[]))"
    ), {"s": scope, "ids": "{" + ",".join(ids) + "}"}).fetchall()
    return list({r[0] for r in rows})
```

收集到的 `event_id` 会先按 `events` 表过滤——只保留该 scope 下真实存在的行，避免 `facts.supports` 里残留的失效引用（如已被擦除/删除的 event）进入后续 refcount 阶段。

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
for ent in manifest["events"]:   # 逐 event
    with session_scope() as conn:
        rc = _event_refcount(conn, scope, ent["event_id"])
        if rc > 0 or ent["action"] == "redact":
            # 擦除:清 content,设 excluded_from_recall
            conn.execute(text("UPDATE events SET content='{}', excluded_from_recall=true ..."), ...)
        else:
            # 物理删:先置空 FK,再 DELETE
            conn.execute(text("UPDATE jobs SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), ...)
            conn.execute(text("UPDATE lifecycle_events SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), ...)
            conn.execute(text("DELETE FROM events WHERE event_id=CAST(:e AS uuid)"), ...)
        # 每行都清 supports 引用
        conn.execute(text("UPDATE facts SET supports=array_remove(supports,:e) ..."), ...)
        conn.execute(text("UPDATE beliefs SET supports=array_remove(supports,:e) ..."), ...)
```

`execute_erasure` 遍历 `manifest["events"]`（而非旧的 `to_delete`/`to_redact` 两组），对每个 `{event_id, action, refcount}` 条目独立开事务：`action == "redact"` 或当场 refcount>0 走擦除，否则物理删。执行后回写 `erasure_jobs.phase='completed'` 与 `progress`。

## 预览与执行

### Preview（干跑，不做修改）

`preview_erasure` 枚举 + 引用计数，产出 manifest，并把 `phase='enumerate'` 的 erasure_job（含 preview_id 与 manifest）落库：

```python
def preview_erasure(*, scope, selector):
    """enumerate + refcount → manifest。落 erasure_jobs(phase=enumerate)。"""
    with session_scope() as conn:
        eids = _select_event_ids(conn, scope, selector)
        manifest_entries = []
        n_del = n_red = 0
        for eid in eids:
            rc = _event_refcount(conn, scope, eid)
            action = "redact" if rc > 0 else "delete"
            if action == "redact":
                n_red += 1
            else:
                n_del += 1
            manifest_entries.append({"event_id": eid, "action": action, "refcount": rc})
        preview_id = uuid.uuid4()
        manifest = {"events": manifest_entries,
                    "expires_at": ...}
        row = conn.execute(text("""
            INSERT INTO erasure_jobs (scope, selector, phase, preview_id, manifest, refcount_breakdown)
            VALUES (:s, CAST(:sel AS jsonb), 'enumerate', :pid, CAST(:m AS jsonb), CAST(:rb AS jsonb))
            RETURNING erasure_id
        """), ...).fetchone()
        eid = str(row.erasure_id)
    return {"erasure_id": eid, "preview_id": str(preview_id),
            "estimated_affected": {"events": len(eids)},
            "refcount_breakdown": {"events_to_delete": n_del, "events_to_redact": n_red},
            "manifest": manifest}
```

返回结构不再是简单的 `{to_delete, to_redact}` 两个列表，而是带元信息的五件套：`erasure_id`（本次 enumerate 落库的 erasure_job id）、`preview_id`（供后续取 manifest / execute 复用）、`estimated_affected`、`refcount_breakdown`（`events_to_delete`/`events_to_redact` 计数）以及 `manifest`（逐 event 的 `{event_id, action, refcount}`）。manifest 24 小时后过期（`MANIFEST_TTL_HOURS=24`），过期后 `get_manifest` 返回 `{"expired": True}`。

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

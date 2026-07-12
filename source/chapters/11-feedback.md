# 第11章 Feedback 回灌 — 用修正信号优化召回

## 1. 概述

召回不是一次性的单向管道。当用户在召回结果上做出 "这条相关 / 这条无关 / 这条错了" 的判断时，这些修正信号必须回灌到记忆层，调整下次召回的排序权重。这一闭环就是 **Feedback 回灌**。

cortex-py 的 Feedback 模块位于 `src/cortex/memory/feedback.py`，核心是 `submit_feedback` / `_apply_to_fact` / `_check_methylation` 三个函数。它采用**双轨设计**：

- **软轨（salience）**：上调或下调 `facts.salience`，影响召回排序但不删数据。
- **硬轨（recorded_to）**：对明确错误且属一次性问题的事实，设 `recorded_to=now()` 软关闭，保留可溯源历史。

```{mermaid}
graph LR
    U[用户/Agent 判断] -->|relevant/irrelevant/wrong/partial| S[submit_feedback]
    S --> R1[feedback_signals 记录]
    S --> F{_apply_to_fact}
    F -->|软轨| SL[salience ±]
    F -->|硬轨| RT[recorded_to=now]
    F -->|累积负反馈| ME[_check_methylation]
    ME --> EV[events.excluded_from_recall=true]
    SL --> NX[下次召回权重改变]
    RT --> NX
    EV --> NX
```

设计借鉴 MindMemOS 的三级 durable 分类法（`task_temporary` / `scenario_specific` / `long_term`），以及 "归档而非删除" 的软关原则。**关键差异**：cortex-py 补上了 MindMemOS 漏掉的正反馈通道——`relevant` 信号会写 `facts.retrieval_usefulness` 并上调 `salience`（不再递增 `access_count`，避免显式反馈伪装成被动召回次数的双重加权），让被反复确认的事实持续上浮，而非只做单向惩罚。

## 2. 信号分类

### 2.1 四种 signal_type

| signal_type    | 极性 | 即时动作                                                       | 用途                                 |
|----------------|------|----------------------------------------------------------------|--------------------------------------|
| `relevant`     | 正   | retrieval_usefulness+positive_weight, salience+positive_weight, positive_feedback_count+1 | 表扬：这条召回有用，提升下次排序      |
| `irrelevant`   | 负   | retrieval_usefulness−negative_weight, salience−negative_weight, negative_feedback_count+1 | 轻惩：跑题，降权；累积触发 methylation |
| `wrong`        | 强负 | 软关旧 fact + INSERT ruled_out 负版本(workspace tier, diagnostic_correctness=0.0) + 派生反驳 evidence（task_temporary 再软关新版本） | 纠错：结论错了，版本化推翻并可能归档 |
| `partial`      | 中性 | 仅记录                                                         | 不调整，作为 Dreaming/Higher-Order 的离线输入信号 |

### 2.2 三种 signal_durable

来自 MindMemOS 的持久化分级，决定信号在系统中存活的方式：

- **`task_temporary`**：当前任务的一次性问题，不该持久化。`wrong + task_temporary` 触发硬轨 `recorded_to=now()`。
- **`scenario_specific`**：仅在特定场景下不适用（如某用户上下文），带条件地保留。
- **`long_term`**（默认）：长期持久，正常调整 salience。

### 2.3 signal_type × signal_durable 动作矩阵

|                       | `task_temporary`                            | `scenario_specific`                  | `long_term`（默认）                   |
|-----------------------|---------------------------------------------|--------------------------------------|--------------------------------------|
| **relevant**          | retrieval_usefulness↑, salience↑            | retrieval_usefulness↑, salience↑     | retrieval_usefulness↑, salience↑     |
| **irrelevant**        | salience↓, retrieval_usefulness↓, 累积触发 methylation | salience↓, retrieval_usefulness↓, 累积触发 methylation | salience↓, retrieval_usefulness↓, 累积触发 methylation |
| **wrong**             | 版本化 ruled_out + 软关新版本 + methylation 检查 | 版本化 ruled_out + methylation 检查 | 版本化 ruled_out + methylation 检查 |
| **partial**           | 仅记录                                      | 仅记录                               | 仅记录                               |

注意 `signal_durable` 只在 `wrong` 分支产生差异化动作：唯有 `task_temporary` 才会走硬轨归档。其他 durable 等级对同类型信号的处理一致——它们的价值在于为后续的 Dreaming 离线巩固提供分级输入，而非改变即时路径。

## 3. 双轨设计

### 3.1 软轨：salience 浮动

salience 是 `facts` 表上的召回权重浮点字段，反馈在 `[salience_floor, salience_ceiling]` 区间内做**钳位增减**：

- **正反馈上调**（`feedback.py` L130-134）：

```python
UPDATE facts SET positive_feedback_count = positive_feedback_count + 1,
                 salience = least(salience + :w, :ceil)
WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND recorded_to IS NULL AND valid_to IS NULL
```

用 `least(...)` 钳顶，默认 `:w=0.5`、`:ceil=2.0`，保证 salience 不会无限膨胀。

- **负反馈下调**（`feedback.py` L140-144、L151-156）：

```python
UPDATE facts SET negative_feedback_count = negative_feedback_count + 1,
                 salience = greatest(salience - :w, :floor)
WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND recorded_to IS NULL AND valid_to IS NULL
```

用 `greatest(...)` 钳底，默认 `:w=0.3`、`:floor=0.1`。注意 `floor` 不为 0——保留可恢复性，一旦后续有正反馈仍能爬升。

### 3.2 硬轨：recorded_to 软关

只有 `wrong + task_temporary` 才触发硬轨（`feedback.py` L160-163）：

```python
UPDATE facts SET recorded_to=now()
WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND recorded_to IS NULL AND valid_to IS NULL
```

这是**软关闭**：事实行仍在表中，历史可溯源，但被 `_LIVE_FACT` 守卫排除出活跃集合，不再参与召回。相比物理删除，软关支持审计与回滚（手动清空 `recorded_to` 即恢复）。

## 4. `_apply_to_fact` 详解

`_apply_to_fact` 是即时反馈的核心调度器，按 `signal_type` 分四个分支。所有写操作前先做 `SELECT ... FOR UPDATE` 锁住该活跃 fact（见第 6 节），然后进入分支：

### 4.1 relevant 分支

```python
if signal_type == "relevant":
    # 显式 usefulness 不再写入被动 retrieval/access 计数，避免同一信号双重加权。
    conn.execute(text(f"""
        UPDATE facts SET positive_feedback_count = positive_feedback_count + 1,
                         salience = least(salience + :w, :ceil),
                         retrieval_usefulness = least(retrieval_usefulness + :uw, 1.0)
        WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND {_LIVE_FACT}
    """), {"f": fact_id, "s": scope, "w": fb.positive_weight,
           "uw": min(fb.positive_weight, 0.25), "ceil": fb.salience_ceiling})
    actions.append("retrieval_usefulness_boosted")
    actions.append("salience_boosted")
```

一条 UPDATE(全在 `facts` 表):
1. `positive_feedback_count` +1。
2. `salience` 钳顶上调(`least(..., salience_ceiling)`)。
3. `retrieval_usefulness` 钳顶上调(`least(..., 1.0)`,每次最多 +`positive_weight` 上限 0.25)。

```{note}
关键设计:**正反馈不再递增 `events.access_count`**。旧版把"用户说有用"伪装成"被动召回次数",导致同一信号在 access_count 和 salience 上双重加权。新版把显式反馈值独立写入 `facts.retrieval_usefulness`(检索加权单独读它),与被动召回次数 `retrieval_count` 分轨。被动召回只由 recall 自己递增(见第10章 §3)。
```

### 4.2 irrelevant 分支

```python
elif signal_type == "irrelevant":
    conn.execute(text(f"""
        UPDATE facts SET negative_feedback_count = negative_feedback_count + 1,
                         salience = greatest(salience - :w, :floor),
                         retrieval_usefulness = greatest(retrieval_usefulness - :uw, -1.0)
        WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND {_LIVE_FACT}
    """), {"f": fact_id, "s": scope, "w": fb.negative_weight,
           "uw": min(fb.negative_weight, 0.25), "floor": fb.salience_floor})
    actions.append("salience_demoted")
    _check_methylation(conn, scope, fact_id, cfg, actions)
```

只动 facts 表，不碰 events。salience 钳底下调后立即调用 `_check_methylation` 检查是否已累积到阈值。

### 4.3 wrong 分支(版本化修订,非 in-place 改)

````{admonition} 关键设计变更
:class: important
wrong 反馈**不再 in-place 改 `assertion_status='ruled_out'`**。遵循双时态原则("Epistemic changes are recorded-time revisions, never in-place history edits"),它走**版本化 INSERT**:软关旧 fact → 插入一条 `polarity='negative' / assertion_status='ruled_out' / knowledge_tier='workspace'` 的新版本,保留历史可溯源。
````

```python
elif signal_type == "wrong":
    # 1) 软关旧 fact(recorded_to=now())
    conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"),
                 {"f": fact_id})
    # 2) INSERT 一条 ruled_out 负版本(从旧 fact 复制大部分列,改 polarity/assertion/tier/salience)
    revised_id = conn.execute(text("""
        INSERT INTO facts(...,polarity,assertion_status,...,knowledge_tier,...,
                           salience,negative_feedback_count,diagnostic_correctness,...)
        SELECT ...,'negative','ruled_out',...,'workspace',...,
               greatest(salience-:weight,:floor),negative_feedback_count+1,0.0,...
        FROM facts WHERE fact_id=CAST(:f AS uuid) RETURNING fact_id::text
    """), {"f": fact_id, "weight": fb.negative_weight, "floor": fb.salience_floor}).scalar()
    # 3) 复制 claim_evidence 到新版本(role='context')
    # 4) 若有 feedback_id:派生一条 evidence_artifacts('derived','feedback')+
    #    两条 claim_evidence(旧 fact role='refutes', 新 fact role='supports')
    actions.append("assertion_ruled_out")
    actions.append(f"assertion_versioned:{revised_id}")
    actions.append("salience_demoted")
    # task_temporary:软关的是新版本 revised_id(不是原 fact_id)
    if signal_durable == "task_temporary":
        conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"),
                     {"f": revised_id})
        actions.append("fact_archived(task_temporary)")
    _check_methylation(conn, scope, revised_id, cfg, actions)
```

与 irrelevant 的关键差异:

- **版本化而非 in-place**:软关旧 fact + INSERT 负版本,而非直接 `UPDATE assertion_status`。旧 fact 留在历史里,可溯源"曾经被认为真"。
- **新版本字段**:`polarity='negative'`、`assertion_status='ruled_out'`、`knowledge_tier='workspace'`、`diagnostic_correctness=0.0`(标记为诊断上不正确)、`salience` 下调、`negative_feedback_count+1`。
- **证据链迁移**:旧 fact 的 `claim_evidence` 复制到新版本(role='context');若有 feedback_id,额外派生一条 `evidence_artifacts(evidence_kind='derived', source_system='feedback')`,并用两条 claim_evidence 把它分别以 `refutes`(指向旧)、`supports`(指向新)关联——形成可溯源的反驳证据。
- **task_temporary 软关的是 `revised_id`**(新版本),不是原 fact_id——因为原 fact 已经在第 1 步被软关了。

无论是否归档,最后都对 `revised_id` 走 `_check_methylation`。

### 4.4 partial 分支（L166-168）

```python
elif signal_type == "partial":
    actions.append("recorded_only")
```

**零写操作**（除 `feedback_signals` 表本身的记录外）。`partial` 信号仅作为 Dreaming / Higher-Order 离线巩固的输入，不触碰任何记忆字段，保证离线层有完整信号可分析而不污染即时排序。

## 5. `_check_methylation` — 累积负反馈触发软剪枝

当一个 fact 的 `negative_feedback_count` 累积达到 `demote_threshold`（默认 3），对其支撑 events 触发甲基化——设 `excluded_from_recall=true`，使其不再被召回。

### 5.1 阈值检查

```python
def _check_methylation(conn, scope: str, fact_id: str, cfg, actions: List[str]) -> None:
    fb = cfg.feedback
    row = conn.execute(text(f"""
        SELECT negative_feedback_count FROM facts
        WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND {_LIVE_FACT}
    """), {"f": fact_id, "s": scope}).fetchone()
    if row and row[0] >= fb.demote_threshold:
        ...
```

只对活跃 fact 检查；已归档的 fact（`recorded_to` 非空）即使负反馈计数超阈值也不再处理——它已经被硬轨关掉了。

### 5.2 共享证据守卫（CRITICAL）

甲基化 UPDATE 的核心是 `NOT EXISTS` 子查询，确保**只剪 "仅被本活跃 fact 支撑、不被任何其他活跃 fact 支撑" 的 event**：

```python
conn.execute(text("""
    UPDATE events e SET excluded_from_recall=true, methylated_at=now()
    WHERE e.event_id = ANY(SELECT unnest(supports) FROM facts
                           WHERE fact_id=CAST(:f AS uuid) AND scope=:s
                             AND recorded_to IS NULL AND valid_to IS NULL)
      AND e.excluded_from_recall=false
      AND NOT EXISTS (
          SELECT 1 FROM facts f2
          WHERE e.event_id = ANY(f2.supports)
            AND f2.fact_id <> CAST(:f AS uuid)
            AND f2.scope=:s AND f2.recorded_to IS NULL AND f2.valid_to IS NULL)
"""), {"f": fact_id, "s": scope})
actions.append(f"methylation_triggered(count={row[0]})")
```

三层过滤条件：

1. `e.event_id = ANY(SELECT unnest(supports) ...)` —— 只动本 fact 的支撑 events。
2. `e.excluded_from_recall=false` —— 跳过已甲基化的，避免重复写。
3. `NOT EXISTS (... f2.fact_id <> :f ...)` —— **关键守卫**：该 event 不能同时被任何其他活跃 fact 引用为支撑。

为什么必须这层守卫？一个 event 可能同时支撑多个 fact。若 fact A 被判错而甲基化了它共享给 fact B 的 evidence event，fact B 就会变成无支撑孤儿，导致正确事实被连带降级。这是 cortex-py 在 C3 一致性约束下的硬性安全要求。

### 5.3 甲基化级联

```{mermaid}
graph TB
    F[fact: neg_count≥3] --> Q1{哪些 event 支撑本 fact?}
    Q1 -->|supports| E1[候选 events]
    E1 --> Q2{已被甲基化?}
    Q2 -->|是| SKIP[跳过]
    Q2 -->|否| Q3{被其他活跃 fact 共享?}
    Q3 -->|是| KEEP[保留, 防孤儿]
    Q3 -->|否| METH[excluded_from_recall=true]
    METH --> NX[下次召回不再出现]
```

注意：甲基化是**软剪枝**，只设标志不删行。手工把 `excluded_from_recall` 设回 `false` 即可恢复召回——与第 17 章 Maintenance 的甲基化保持同样的可逆语义。

## 6. 并发安全

反馈可能在同一 fact 上并发到达（例如 agent 同时收到多个用户的纠正）。`_apply_to_fact` 用三道机制防竞态：

### 6.1 行锁：`SELECT ... FOR UPDATE`

函数开头（L114-118）锁住目标活跃 fact：

```python
locked = conn.execute(text(f"""
    SELECT fact_id::text FROM facts
    WHERE fact_id=CAST(:f AS uuid) AND scope=:s AND {_LIVE_FACT}
    FOR UPDATE
"""), {"f": fact_id, "s": scope}).fetchone()
if not locked:
    return ["skipped_fact_not_live"]
```

`FOR UPDATE` 取得行锁，Postgres 会把其他尝试锁同一行的并发事务阻塞到本事务提交。这把同一 fact 上的 `salience / negative_feedback_count` 读改写序列化，避免经典的 lost update（A、B 都读到 count=2，各自写 3，丢一次）。

若 fact 不活跃（已归档），`locked` 为空，直接返回 `skipped_fact_not_live` 不做任何写操作。

### 6.2 原子幂等：`INSERT ... ON CONFLICT DO NOTHING`

`submit_feedback` 在写 `feedback_signals` 时用幂等键原子去重（L52-72）：

```python
row = conn.execute(text("""
    INSERT INTO feedback_signals (scope, pack_id, target_layer, target_id, signal_type,
                                  signal_durable, strength, reason, actor, idempotency_key,
                                  applied, applied_at)
    VALUES (:s,:p,:tl,CAST(:tid AS uuid),:st,:sd,1.0,:r,:a,:k,true,now())
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING feedback_id::text, applied
"""), {...}).fetchone()
if row:
    fid, applied_already = row[0], row[1]
else:
    # 冲突:并发请求已插入,回查既有记录
    ex = conn.execute(text(
        "SELECT feedback_id::text, applied FROM feedback_signals WHERE idempotency_key=:k"
    ), {"k": idempotency_key}).fetchone()
    if ex:
        return {"feedback_id": ex[0], "signal_type": signal_type,
                "applied": ex[1], "note": "idempotent (already applied)"}
```

关键点：`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING ...` 是 Postgres 的**单条原子语句**。冲突时 `RETURNING` 返回空集，于是回查既有记录。这避免了 "先 SELECT 再 INSERT" 的 TOCTOU（time-of-check-to-time-of-use）竞态——那种写法在两个并发请求都查不到记录后各自 INSERT，其中一个会因 UNIQUE 约束抛错退化为 500。

随后的 `applied_already is not False` 守卫（L87）确保：命中既有记录时**不再重复应用即时动作**，防止 salience 被同一信号多次叠加。

### 6.3 `_LIVE_FACT` 守卫

模块顶部定义（L31）：

```python
_LIVE_FACT = "recorded_to IS NULL AND valid_to IS NULL"
```

所有针对 facts 的写操作（salience 调整、归档、计数递增、methylation 阈值检查）都带这个守卫。它保证：

- 不会篡改已归档（`recorded_to` 非空）的历史事实。
- 不会动已版本失效（`valid_to` 非空）的旧版本。

这是与第 18 章 Erasures / 第 21 章 Versioning 协同的硬性约束：反馈只作用于"当前活跃版本"，历史链保留原样供审计。

## 7. `feedback_signals` 表

DDL 来自 `src/cortex/schema.sql`（L394-414）：

```sql
CREATE TABLE IF NOT EXISTS cortex.feedback_signals (
    feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    pack_id          TEXT,                          -- 引用 recall_packs.pack_id(可选,溯源哪次召回)
    target_layer     TEXT NOT NULL CHECK (target_layer IN ('fact','belief','event')),
    target_id        UUID NOT NULL,
    signal_type      TEXT NOT NULL CHECK (signal_type IN ('relevant','irrelevant','wrong','partial')),
    signal_durable   TEXT NOT NULL DEFAULT 'long_term'
                     CHECK (signal_durable IN ('task_temporary','scenario_specific','long_term')),
    strength         FLOAT NOT NULL DEFAULT 1.0,
    reason           TEXT,
    actor            TEXT,
    idempotency_key  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied          BOOLEAN NOT NULL DEFAULT false,
    applied_at       TIMESTAMPTZ,
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON cortex.feedback_signals (scope, target_id);
CREATE INDEX IF NOT EXISTS idx_feedback_pack ON cortex.feedback_signals (pack_id);
```

字段说明：

| 字段             | 类型        | 说明                                                          |
|------------------|-------------|---------------------------------------------------------------|
| `feedback_id`    | UUID        | 主键，`gen_random_uuid()` 自动生成                            |
| `scope`          | TEXT        | 命名空间隔离，必填                                            |
| `pack_id`        | TEXT        | 可选，引用 `recall_packs.pack_id`，溯源是哪次召回引发的反馈   |
| `target_layer`   | TEXT        | 目标层级：`fact` / `belief` / `event`（CHECK 约束）           |
| `target_id`      | UUID        | 目标实体 ID                                                   |
| `signal_type`    | TEXT        | 信号类型：四值之一（CHECK 约束）                              |
| `signal_durable` | TEXT        | 持久化分级：三值之一，默认 `long_term`                        |
| `strength`       | FLOAT       | 信号强度，默认 1.0（当前未参与加权，预留扩展）                |
| `reason`         | TEXT        | 可选的修正理由                                                |
| `actor`          | TEXT        | 提交者标识（API 层注入 `actor=Depends(auth)`）                |
| `idempotency_key`| TEXT        | 幂等键，UNIQUE 约束，用于原子去重                             |
| `created_at`     | TIMESTAMPTZ | 创建时间                                                      |
| `applied`        | BOOLEAN     | 是否已应用即时动作，默认 false（submit_feedback 写入时设 true）|
| `applied_at`     | TIMESTAMPTZ | 应用时间                                                      |

索引：`idx_feedback_target` 支持按 scope+target 查询反馈历史；`idx_feedback_pack` 支持按召回包溯源。

注意：当前实现中 `target_layer` 仅在 `fact` 时触发即时动作（`feedback.py` L87）。`belief` / `event` 层的信号只入表记录，留给离线巩固处理。

## 8. API 与 MCP

### 8.1 REST API

**POST /v1/feedback** — 提交反馈（`src/cortex/interfaces/api/app.py` L797-803）：

```python
@app.post("/v1/feedback")
def submit_feedback(body: schemas.FeedbackRequest, actor: str = Depends(auth)):
    from ...memory.feedback import submit_feedback as _submit
    return _submit(scope=body.scope, target_layer=body.target_layer, target_id=body.target_id,
                   signal_type=body.signal_type, signal_durable=body.signal_durable,
                   reason=body.reason, actor=actor, pack_id=body.pack_id,
                   idempotency_key=body.idempotency_key)
```

请求体 `FeedbackRequest`（`src/cortex/interfaces/api/schemas.py` L332-340）：

```python
class FeedbackRequest(BaseModel):
    scope: str
    target_layer: Literal["fact", "belief", "event"]
    target_id: str
    signal_type: Literal["relevant", "irrelevant", "wrong", "partial"]
    signal_durable: Literal["task_temporary", "scenario_specific", "long_term"] = "long_term"
    reason: Optional[str] = None
    pack_id: Optional[str] = None
    idempotency_key: Optional[str] = None
```

`actor` 从 `Depends(auth)` 注入，记录是哪个用户提交的反馈。`Literal` 类型校验保证 `signal_type` / `signal_durable` 取值合法，非法值在 Pydantic 层即被拒。

请求示例：

```bash
curl -X POST http://localhost:8000/v1/feedback \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "proj_a",
    "target_layer": "fact",
    "target_id": "3f9c1e2a-...",
    "signal_type": "wrong",
    "signal_durable": "task_temporary",
    "reason": "事实已过期",
    "idempotency_key": "fb-2026-07-09-3f9c1e2a-wrong"
  }'
```

返回：

```json
{
  "feedback_id": "8a7b...",
  "signal_type": "wrong",
  "applied_actions": [
    "assertion_ruled_out",
    "salience_demoted",
    "fact_archived(task_temporary)",
    "methylation_triggered(count=4)",
    "cache_invalidated"
  ]
}
```

**GET /v1/feedback** — 查询反馈（L806-810）：

```python
@app.get("/v1/feedback")
def list_feedback(scope: str, target_id: Optional[str] = Query(None),
                  limit: int = Query(50, le=200), actor: str = Depends(auth)):
    from ...memory.feedback import list_feedback as _list
    return {"items": _list(scope=scope, target_id=target_id, limit=limit)}
```

支持 `scope`（必填）、`target_id`（可选）、`limit`（≤200）三个查询参数。

### 8.2 MCP 工具

两个工具位于 `src/cortex/interfaces/mcp_server.py`：

**feedback_submit**（L357-376）：

```python
@mcp.tool()
def feedback_submit(target_id: str, signal_type: str,
                    target_layer: str = "fact",
                    signal_durable: str = "long_term",
                    reason: Optional[str] = None,
                    scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Submit feedback on a recalled memory to optimize future recall.

    Call this after memory_search/answer when you find a recalled fact is
    relevant, irrelevant, wrong, or only partially correct. The system adjusts
    ranking salience and may archive clearly-wrong memories.

    signal_type: relevant | irrelevant | wrong | partial
    signal_durable: task_temporary (one-off, may delete) | scenario_specific | long_term
    target_id: the fact_id (or belief_id/event_id) from the recall result.
    """
    from ...memory.feedback import submit_feedback
    return submit_feedback(scope=_eff_scope(ctx, scope), target_layer=target_layer,
                           target_id=target_id, signal_type=signal_type,
                           signal_durable=signal_durable, reason=reason)
```

**feedback_list**（L379-384）：

```python
@mcp.tool()
def feedback_list(target_id: Optional[str] = None, limit: int = 50,
                  scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """List feedback signals for a scope (optionally filtered by target_id)."""
    from ...memory.feedback import list_feedback
    return {"items": list_feedback(scope=_eff_scope(ctx, scope), target_id=target_id, limit=limit)}
```

MCP 工具与 REST API 的差异：MCP 的 `scope` 可选，缺省时由 `_eff_scope(ctx, scope)` 从上下文推断；MCP 入口不暴露 `pack_id` / `idempotency_key`，适合 agent 在一次对话内即时回灌，无需关心幂等去重的场景。

典型 agent 调用模式：先 `memory_search` 拿到 `fact_id`，检查结果是否合用，随即调 `feedback_submit` 回灌。这一闭环让 agent 的判断直接转化为下次召回的优化信号。

## 9. 配置

`FeedbackCfg` 位于 `src/cortex/infra/config.py`（L124-132）：

```python
class FeedbackCfg(BaseModel):
    """Feedback 回灌:修正信号优化召回(双轨--软降权 salience + 硬归档 recorded_to)。"""
    enabled: bool = True
    positive_weight: float = 0.5       # 正反馈 salience 提升量
    negative_weight: float = 0.3       # 负反馈 salience 降低量
    demote_threshold: int = 3          # 累积负反馈达此值触发 methylation
    salience_floor: float = 0.1        # salience 最低值(不归零,保留可恢复)
    salience_ceiling: float = 2.0      # salience 最高值
    cache_invalidate: bool = True      # 提交反馈后失效 recall_packs
```

| 字段               | 类型   | 默认 | 作用                                                              |
|--------------------|--------|------|-------------------------------------------------------------------|
| `enabled`          | bool   | True | 总开关。False 时 `submit_feedback` 直接返回 `feedback disabled`   |
| `positive_weight`  | float  | 0.5  | 每次 `relevant` 信号 salience 上调量                              |
| `negative_weight`  | float  | 0.3  | 每次 `irrelevant` / `wrong` 信号 salience 下调量                  |
| `demote_threshold` | int    | 3    | `negative_feedback_count` 达此值触发 `_check_methylation`        |
| `salience_floor`   | float  | 0.1  | salience 钳底。不为 0，保留可恢复性                               |
| `salience_ceiling` | float  | 2.0  | salience 钳顶，防正反馈无限膨胀                                   |
| `cache_invalidate` | bool   | True | 提交反馈后 `DELETE FROM recall_packs WHERE scope=:s`，强制下次重算 |

### 9.1 权重不对称的设计

注意 `positive_weight (0.5) > negative_weight (0.3)`。这是有意的非对称：正反馈比负反馈更"重"。一次正反馈需要 ~1.7 次负反馈才能抵消。理由是**正反馈的成本更低、信号更可信**——用户主动确认某条有用，比抱怨某条无用更接近真实判断。而负反馈可能是上下文误判、查询歧义等噪声，应累积多次（`demote_threshold=3`）才触发硬剪枝。

### 9.2 cache_invalidate 的取舍

`cache_invalidate=True` 意味着每次反馈都会清空该 scope 的所有 `recall_packs` 缓存。这保证反馈立即生效——下次召回一定看到新的 salience。代价是高频反馈场景下缓存命中率下降。若你的工作负载是"召回多、反馈少"，默认值合适；若是"反馈密集型"（如批量标注），可考虑关闭并依赖 TTL 过期。

### 9.3 配置示例

```yaml
feedback:
  enabled: true
  positive_weight: 0.5
  negative_weight: 0.3
  demote_threshold: 3
  salience_floor: 0.1
  salience_ceiling: 2.0
  cache_invalidate: true
```

调参建议：
- **保守场景**（数据珍贵、误判代价高）：调高 `demote_threshold` 到 5，降低 `negative_weight` 到 0.2，让负反馈累积更慢。
- **激进场景**（噪声数据多、要快速清洗）：调低 `demote_threshold` 到 2，调高 `negative_weight` 到 0.5，但注意 `salience_floor` 不要降到 0，否则失去可恢复性。
- **离线巩固为主**：把 `partial` 信号作为 Dreaming 的主输入，即时反馈权重可整体调低，让离线 LLM 综合判断后再批量调整。

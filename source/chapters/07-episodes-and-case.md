# 第7章 Episodes 与诊断 Case

## 概述

Episodes 是五层记忆模型的第二层——**有界事件序列**。它有两种模式：

1. **自动分段**：按 30 分钟时间窗口或 `case_id` 自动分组
2. **显式 Case**：下游 agent 创建诊断案例，手动关联 events，跟踪全生命周期

```{mermaid}
graph TB
    subgraph 自动分段
        E1[Event 1] --> EP1[Episode A<br/>30min 窗口]
        E2[Event 2] --> EP1
        E3[Event 3] --> EP2[Episode B<br/>下一窗口]
    end
    
    subgraph 诊断 Case
        C1[Case: 密封失效<br/>open]
        E4[Event 4<br/>case_id=C001] --> C1
        E5[Event 5<br/>case_id=C001] --> C1
        C1 -->|update phase| C2[investigating]
        C2 -->|update| C3[resolved]
    end
```

## Episodes 表结构

```sql
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL,
    title           TEXT,
    event_ids       UUID[] NOT NULL DEFAULT '{}',
    actors          TEXT[] NOT NULL DEFAULT '{}',
    causal_chain    JSONB,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    
    -- 双时态
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ,
    sealed          BOOLEAN NOT NULL DEFAULT false,
    
    -- 诊断 Case 扩展
    case_id         TEXT,           -- 案例编号
    equipment       TEXT,           -- 设备标识
    lot             TEXT,           -- 批次号
    recipe          TEXT,           -- 配方
    phase           TEXT,           -- 诊断阶段
    root_cause      TEXT,           -- 根因结论
    resolution      TEXT,           -- 修复措施
    status          TEXT DEFAULT 'open',  -- open/investigating/resolved/closed
    metadata        JSONB DEFAULT '{}'
);
```

## 模式1：自动分段

`segment_scope` 函数自动将 events 按以下规则分组：

```python
WINDOW_MIN = 30  # 30 分钟窗口

def segment_scope(scope, since=None):
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT event_id, observed_at, observed_actor, context, case_id
            FROM events WHERE scope=:s AND excluded_from_recall=false
            ORDER BY observed_at
        """), {"s": scope}).fetchall()
        
        # 分组规则：
        # 1. 有 case_id 的 events → 按 case_id 分组（不论时间间隔）
        # 2. 无 case_id 的 events → 30 分钟时间窗口
        groups = []
        case_groups = {}
        time_group = []
        last_t = None
        
        for r in fresh:
            cid = r.case_id
            if cid:
                case_groups.setdefault(cid, []).append(r)
            else:
                if last_t and (r.observed_at - last_t).total_seconds() > WINDOW_MIN * 60:
                    groups.append(time_group)
                    time_group = []
                time_group.append(r)
                last_t = r.observed_at
```

```{mermaid}
flowchart TD
    A[Events 按 observed_at 排序] --> B{有 case_id?}
    B -->|是| C[按 case_id 分组]
    B -->|否| D{与前一个 event<br/>间隔 > 30min?}
    D -->|是| E[新 episode]
    D -->|否| F[加入当前 episode]
    C --> G[写入 episodes 表]
    E --> G
    F --> G
```

## 模式2：诊断 Case

Case 是一个诊断事件的**全生命周期管理**，从发现到根因确认到修复。

### Case 生命周期

```
open → investigating → resolved → closed
```

状态迁移受**白名单校验**约束（`_STATUS_TRANSITIONS`），不允许跳级回退：

| 当前状态 | 可迁移到 |
|----------|----------|
| `open` | `open`, `investigating` |
| `investigating` | `investigating`, `resolved` |
| `resolved` | `resolved`, `closed` |
| `closed` | `closed`（终态） |

### 诊断阶段

```
observation → scoping → investigation → correlation → root_cause → remediation → regression
```

phase 同样按 `_PHASE_ORDER` **单调推进**，不允许回退到更早阶段。状态与阶段相互约束：

- `status = resolved/closed` 时**必须**已有 `root_cause` 与 `resolution`
- `status = closed` 时 phase 必须推进到 `regression`，且 `metadata.regression_evidence` 需提供**本 scope 的 measurement/external 证据 ID**（写入 `episode_evidence`，role=`regression`）

### API

```python
# 创建 Case
@mcp.tool()
def case_create(title=None, equipment=None, lot=None, recipe=None, scope=None):
    """创建一个诊断 case"""
    return episodes.create_case(scope=scope, title=title, 
                                equipment=equipment, lot=lot, recipe=recipe)

# 更新 Case
@mcp.tool()
def case_update(episode_id, phase=None, status=None, root_cause=None, resolution=None):
    """更新 case 的阶段/状态/根因；迁移与阶段推进受校验约束"""
    return episodes.update_case(episode_id, phase=phase, status=status,
                               root_cause=root_cause, resolution=resolution)

# 查询 Case
@mcp.tool()
def case_get(episode_id):
    """获取 case 详情（含 events + facts + beliefs）"""

@mcp.tool()
def case_list(status=None, equipment=None, scope=None):
    """按状态或设备筛选"""

@mcp.tool()
def case_search(query, scope=None):
    """按标题/根因/设备/修复措施模糊搜索"""
```

### Case 创建与更新

```python
def create_case(*, scope, title=None, case_id=None, equipment=None, lot=None,
                recipe=None, metadata=None):
    """创建一个诊断 case(空 episode,待关联 events)。返回 {episode_id, case_id}。"""
    with session_scope() as conn:
        row = conn.execute(text("""
            INSERT INTO episodes (scope, title, case_id, equipment, lot, recipe, metadata,
                                  started_at, valid_from, sealed, status, phase)
            VALUES (:s, :t, :cid, :eq, :lot, :rec, CAST(:meta AS jsonb),
                    now(), now(), false, 'open', 'observation')
            RETURNING episode_id
        """), {"s": scope, "t": title, "cid": case_id, "eq": equipment, "lot": lot,
               "rec": recipe, "meta": json.dumps(metadata or {})}).fetchone()
    return {"episode_id": str(row.episode_id), "case_id": case_id,
            "scope": scope, "status": "open", "phase": "observation"}


def update_case(episode_id, **fields):
    """更新 case 的 phase/status/root_cause/resolution/equipment/lot/recipe/title。
    校验:phase 合法、status 迁移白名单、phase 单调推进、
    resolved/closed 需 root_cause+resolution、closed 需 regression 证据。"""
    with session_scope() as conn:
        current = conn.execute(text("""
            SELECT status, phase, root_cause, resolution, scope FROM episodes
            WHERE episode_id=CAST(:e AS uuid) AND recorded_to IS NULL FOR UPDATE
        """), {"e": episode_id}).fetchone()
        if not current:
            return {"error": "case not found"}
        if status and status not in _STATUS_TRANSITIONS.get(current.status, set()):
            return {"error": f"invalid status transition: {current.status} -> {status}"}
        if phase:
            cur = _PHASE_ORDER.index(current.phase or "observation")
            nxt = _PHASE_ORDER.index(phase)
            if nxt < cur:
                return {"error": f"invalid phase transition: {current.phase} -> {phase}"}
        if status in {"resolved", "closed"} and (not root_cause or not resolution):
            return {"error": "resolved/closed case requires root_cause and resolution"}
        if status == "closed" and phase != "regression":
            return {"error": "closed case requires regression phase"}
        # status=='closed' 需 scope 内 measurement/external 回归证据 → episode_evidence
        ...
        conn.execute(text(f"UPDATE episodes SET {', '.join(sets)} WHERE episode_id=CAST(:e AS uuid)"), params)
    return {"updated": True}
```

### Case 搜索

```python
def search_cases(scope, query, limit=20):
    """按标题/根因/设备/修复措施 ILIKE 模糊搜 cases（不含 case_id）。"""
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT episode_id::text, title, equipment, phase, root_cause,
                   resolution, status, started_at::text
            FROM episodes WHERE scope=:s AND recorded_to IS NULL
              AND (title ILIKE :q OR root_cause ILIKE :q
                   OR equipment ILIKE :q OR resolution ILIKE :q)
            ORDER BY started_at DESC LIMIT :lim
        """), {"s": scope, "q": f"%{query}%", "lim": limit}).fetchall()
    return [dict(...) for r in rows]
```

## Case 完整数据流

```{mermaid}
sequenceDiagram
    participant A as 诊断 Agent
    participant C as cortex
    participant DB as PostgreSQL
    
    A->>C: case_create(equipment="XXX-v1")
    C->>DB: INSERT episodes (status=open, phase=observation)
    C-->>A: {episode_id, case_id}
    
    A->>C: POST /v1/cases/{id}/events (memory_store 自动携带 case_id)
    C->>DB: 回写 events.case_id + 追加 episodes.event_ids
    
    A->>C: case_update(phase="investigation")
    A->>C: memory_store(text="检查MFC-1...")
    A->>C: case_update(phase="root_cause", root_cause="密封圈老化")
    
    A->>C: GET /v1/cases/{id}/workspace-graph
    C-->>A: case-local 图（候选/反证，与 verified 分层）
    
    A->>C: case_update(status="resolved", phase="regression", regression_evidence=[...])
    A->>C: POST /v1/cases/{id}/promote (reviewer=...)
    C->>DB: workspace fact → verified tier 晋升（claim_evidence/assertion_case_links 复制）
    
    A->>C: case_search(query="密封圈")
    C-->>A: [case_id=C001, root_cause="密封圈老化"]
```

## MCP 工具一览

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `episodes_build` | 自动分段 | scope, async_enqueue |
| `episodes_list` | 列出已封存的 episodes | scope |
| `case_create` | 创建诊断 case | title, equipment, lot, recipe, scope |
| `case_update` | 更新 case（含状态/阶段迁移校验） | episode_id, phase, status, root_cause, resolution |
| `case_get` | 获取 case 详情（events + facts + beliefs） | episode_id |
| `case_list` | 筛选 cases | status, equipment, scope |
| `case_search` | 模糊搜索（标题/根因/设备/修复） | query, scope |

此外，Case 的 HTTP 端点还提供三个 MCP 未直接暴露、但 REST 层完整实现的流程：

- `POST /v1/cases/{episode_id}/events` → `add_event_to_case`：把 event 关联进 case（回写 `events.case_id` + 追加 `episodes.event_ids`，要求同 scope）
- `GET /v1/cases/{episode_id}/workspace-graph` → `get_case_workspace_graph`：返回 Case-local 的 workspace 图（候选/反证可遍历，但与 verified graph 明确分层）
- `POST /v1/cases/{episode_id}/promote` → `promote_case_assertions`：把闭环 Case 中经确认且有证据的 assertion 版本化晋升到 verified graph（需 `closed` + `regression` 阶段 + 回归证据 + reviewer）

### Case-local 图与结构边收敛

Case 的 workspace 图（`get_case_workspace_graph`）只取属于该 case 的 facts（`case_id` 命中或 `assertion_case_links` 关联），供诊断 agent 在**未晋升前**探索候选与反证，与 verified 全局图隔离。

**结构边收敛**：结构谓词（`has_component` / `installed_on` / `located_in` / `monitored_by` / `controlled_by` / `regulates` / `configured_as` / `depends_on`）的 identity 仅由**图的边本身**决定——`scope + subject + predicate + object + polarity`。case、工况、事件时间只描述观测/证据，不会产生重复的拓扑边：同一结构三元组始终收敛为**一条 live edge**，多次观测通过合并 `supports`（并集）、`confidence=max`、`assertion_case_links` 复制来累积证据。更强确认（`hypothesized→confirmed`、`workspace→verified`）会生成单一 live recorded revision。

非结构谓词（因果/诊断/promote 出的 workspace 断言）则保留 case/事件时间维度，同身份的变化按 recorded-time revision 处理，不做拓扑去重。

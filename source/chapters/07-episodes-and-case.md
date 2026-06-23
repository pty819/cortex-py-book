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

### 诊断阶段

```
observation → scoping → investigation → correlation → root_cause → remediation → regression
```

### API

```python
# 创建 Case
@mcp.tool()
def case_create(title, equipment=None, lot=None, recipe=None, scope=None):
    """创建一个诊断 case"""
    return episodes.create_case(scope=scope, title=title, 
                                equipment=equipment, lot=lot, recipe=recipe)

# 更新 Case
@mcp.tool()
def case_update(episode_id, phase=None, status=None, root_cause=None, resolution=None):
    """更新 case 的阶段/状态/根因"""
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
    """按根因/标题/设备模糊搜索"""
```

### Case 创建与更新

```python
def create_case(scope, title=None, equipment=None, lot=None, recipe=None):
    with session_scope() as conn:
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        row = conn.execute(text("""
            INSERT INTO episodes (scope, title, event_ids, actors, started_at,
                case_id, equipment, lot, recipe, status, phase)
            VALUES (:s, :t, '{}', '{}', now(), :c, :e, :l, :r, 'open', 'observation')
            RETURNING episode_id
        """), {"s": scope, "t": title or case_id, "c": case_id,
               "e": equipment, "l": lot, "r": recipe}).fetchone()
    return {"episode_id": str(row.episode_id), "case_id": case_id}


def update_case(episode_id, phase=None, status=None, 
                root_cause=None, resolution=None):
    with session_scope() as conn:
        updates = []
        params = {"e": episode_id}
        if phase:
            updates.append("phase=:p"); params["p"] = phase
        if status:
            updates.append("status=:s"); params["s"] = status
        if root_cause:
            updates.append("root_cause=:rc"); params["rc"] = root_cause
        if resolution:
            updates.append("resolution=:res"); params["res"] = resolution
        conn.execute(text(f"""
            UPDATE episodes SET {', '.join(updates)} 
            WHERE episode_id=CAST(:e AS uuid)
        """), params)
    return {"updated": True}
```

### Case 搜索

```python
def search_cases(scope, query):
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT episode_id::text, case_id, title, status, phase,
                   equipment, root_cause, resolution
            FROM episodes WHERE scope=:s AND recorded_to IS NULL
              AND (title ILIKE :q OR root_cause ILIKE :q 
                   OR equipment ILIKE :q OR case_id ILIKE :q)
            ORDER BY started_at DESC LIMIT 20
        """), {"s": scope, "q": f"%{query}%"}).fetchall()
    return [dict(zip(cols, r)) for r in rows]
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
    
    A->>C: memory_store(text="发现腔体压力异常...")
    Note over A,C: 自动携带 case_id
    
    A->>C: case_update(phase="investigation")
    A->>C: memory_store(text="检查MFC-1...")
    A->>C: case_update(phase="root_cause", root_cause="密封圈老化")
    
    A->>C: case_search(query="密封圈")
    C-->>A: [case_id=C001, root_cause="密封圈老化"]
```

## MCP 工具一览

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `episodes_build` | 自动分段 | scope |
| `episodes_list` | 列出已封存的 episodes | scope |
| `case_create` | 创建诊断 case | title, equipment, lot, recipe, scope |
| `case_update` | 更新 case | episode_id, phase, status, root_cause, resolution |
| `case_get` | 获取 case 详情 | episode_id |
| `case_list` | 筛选 cases | status, equipment, scope |
| `case_search` | 模糊搜索 | query, scope |

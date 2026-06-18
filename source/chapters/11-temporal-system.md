# 第11章 时间系统

## 双时态设计

### 概述

Cortex-PY 的每条记录有**4 个时间字段**，支持双时态查询。

```{mermaid}
graph LR
    subgraph "记录时间 (Transaction Time)"
        RT1["recorded_from<br/>何时记录"]
        RT2["recorded_to<br/>何时被取代"]
    end
    
    subgraph "有效时间 (Valid Time)"
        VT1["valid_from<br/>何时开始为真"]
        VT2["valid_to<br/>何时不再为真"]
    end
    
    F[Fact] --> RT1
    F --> RT2
    F --> VT1
    F --> VT2
```

### 时间字段含义

| 字段 | 含义 | 示例 |
|------|------|------|
| `recorded_from` | 系统何时获知此 fact | "2024-01-15 我们知道这件事" |
| `recorded_to` | 此 fact 何时被新版本取代 | "2024-06-01 这个信息过时了" |
| `valid_from` | 此 fact 在世界中何时开始为真 | "2023-06-01 Alice 加入 Acme" |
| `valid_to` | 此 fact 在世界中何时不再为真 | "2024-03-01 Alice 离开 Acme" |

### 查询场景

```{mermaid}
flowchart TD
    Q1["问：现在 Alice 在哪工作?"]
    Q1 --> A1["SELECT * FROM facts WHERE valid_to IS NULL AND recorded_to IS NULL"]
    
    Q2["问：2024-03 时我们以为 Alice 在哪?"]
    Q2 --> A2["SELECT * FROM facts WHERE recorded_from <= '2024-03' AND (recorded_to IS NULL OR recorded_to > '2024-03')"]
    
    Q3["问：Alice 在 Acme 的完整历史?"]
    Q3 --> A3["SELECT * FROM facts WHERE subject='Alice' AND predicate='works_at' ORDER BY valid_from"]
```

## NL 时间短语解析

### 支持的时间短语

```python
# temporal.py
_DEFAULTS = [
    ("last week", "-P7D..P0D"),
    ("last month", "-P30D..P0D"),
    ("yesterday", "-P1D..P0D"),
    ("last quarter", "-P90D..P0D"),
    ("last year", "-P365D..P0D"),
]
```

### ISO 8601 Duration

```{mermaid}
graph LR
    A["-P7D..P0D"] --> B["-7天 .. 今天"]
    C["-P1M..P0D"] --> D["-1月 .. 今天"]
    E["-P1Y..P0D"] --> F["-1年 .. 今天"]
```

### 解析实现

```python
# temporal.py
_DUR_RE = re.compile(
    r"^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)

def _parse_dur(s: str) -> timedelta:
    """解析 ISO 8601 duration"""
    m = _DUR_RE.match(s.strip())
    if not m:
        raise ValueError(f"bad ISO8601 duration: {s!r}")
    
    sign = -1 if m.group(1) else 1
    y, mo, w, d, h, mi, se = (
        int(x) if x else 0 for x in m.groups()[1:]
    )
    
    return sign * timedelta(
        days=y * 365 + mo * 30 + w * 7 + d,
        hours=h, minutes=mi, seconds=se
    )

def parse_expression(expr: str, anchor: datetime) -> Tuple[datetime, datetime]:
    """解析表达式如 '-P7D..P0D'"""
    parts = expr.split("..")
    if len(parts) != 2:
        raise ValueError(f"expression must be 'dur..dur': {expr!r}")
    
    return anchor + _parse_dur(parts[0]), anchor + _parse_dur(parts[1])
```

### 注册自定义短语

```python
def register_phrase(name: str, expression: str, 
                    anchor: Optional[datetime] = None) -> str:
    """注册自定义时间短语"""
    with session_scope() as conn:
        row = conn.execute(text("""
            INSERT INTO temporal_phrases (name, anchor, expression, is_default)
            VALUES (:n, COALESCE(:a, now()), :e, false)
            ON CONFLICT (name) DO UPDATE 
            SET expression = :e, anchor = COALESCE(:a, now())
            RETURNING phrase_id::text
        """), {
            "n": name.lower(),
            "a": anchor,
            "e": expression
        }).fetchone()
        
        return row[0]
```

### 使用示例

```python
# 注册短语
register_phrase("recent", "-P7D..P0D")

# 解析短语
from_dt, to_dt = parse_temporal("recent")
# → (7天前, 现在)

# 在 recall 中使用
result = recall(scope, query, temporal="last week")
```

## Temporal 通道

### 时间衰减

```{mermaid}
graph LR
    subgraph "时间衰减曲线"
        T1["1天前: weight=0.37"]
        T2["7天前: weight=0.0009"]
        T3["30天前: weight≈0"]
    end
```

### 实现

```python
def _chan_temporal(conn, scope, view, top_k):
    """Temporal 通道: 近期优先"""
    frag, p = _scope_filter(scope, view)
    p["k"] = top_k
    
    sql = f"""
        SELECT 
            fact_id::text,
            EXP(
                -EXTRACT(EPOCH FROM (now() - recorded_from)) / 86400.0
            ) as temporal_weight
        FROM facts
        WHERE {frag}
          AND valid_to IS NULL
          AND recorded_to IS NULL
        ORDER BY temporal_weight DESC, recorded_from DESC
        LIMIT :k
    """
    
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]
```

## 双时态查询

### 当前有效

```sql
-- 问"现在什么是真的"
SELECT * FROM facts 
WHERE valid_to IS NULL       -- 仍然为真
  AND recorded_to IS NULL;   -- 当前版本
```

### 历史快照

```sql
-- 问"2024-03-01 时我们以为的"
SELECT * FROM facts 
WHERE recorded_from <= '2024-03-01'
  AND (recorded_to IS NULL OR recorded_to > '2024-03-01');
```

### 时间范围

```sql
-- 问"2024 年有效的 facts"
SELECT * FROM facts 
WHERE valid_from < '2025-01-01'
  AND (valid_to IS NULL OR valid_to >= '2024-01-01');
```

## 时间字段的写入

### Event 写入

```python
def append_event(..., observed_at=None, ...):
    """写入 Event"""
    with session_scope() as c:
        c.execute(text("""
            INSERT INTO events (..., observed_at, recorded_at, ...)
            VALUES (..., COALESCE(:observed_at, now()), now(), ...)
        """), {
            "observed_at": observed_at,
            ...
        })
```

### Fact 写入

```python
def create_fact(..., valid_from=None, valid_to=None, ...):
    """写入 Fact"""
    with session_scope() as conn:
        conn.execute(text("""
            INSERT INTO facts (
                ..., valid_from, valid_to, 
                recorded_from, recorded_to, ...
            ) VALUES (
                ..., :vf, :vt, 
                now(), NULL, ...
            )
        """), {
            "vf": valid_from,
            "vt": valid_to,
            ...
        })
```

## Fact 更新（版本化）

### 原理

不修改原记录，而是插入新版本。

```{mermaid}
sequenceDiagram
    participant O as Old Fact
    participant N as New Fact
    
    Note over O: recorded_from: 2024-01<br/>recorded_to: NULL<br/>valid_to: NULL
    
    Note over N: 新信息: Alice 离开 Acme
    
    O->>O: UPDATE recorded_to = now()
    N->>N: INSERT recorded_from = now()<br/>recorded_to = NULL<br/>valid_to = 2024-06
    
    Note over O: recorded_from: 2024-01<br/>recorded_to: 2024-06<br/>valid_to: NULL
    
    Note over N: recorded_from: 2024-06<br/>recorded_to: NULL<br/>valid_to: 2024-06
```

### 实现

```python
def supersede_fact(conn, old_fact_id, new_valid_to=None):
    """标记旧 fact 被取代"""
    conn.execute(text("""
        UPDATE facts 
        SET recorded_to = now()
        WHERE fact_id = :id AND recorded_to IS NULL
    """), {"id": old_fact_id})
    
    # 如果指定了 valid_to，也更新
    if new_valid_to:
        conn.execute(text("""
            UPDATE facts 
            SET valid_to = :vt
            WHERE fact_id = :id
        """), {"id": old_fact_id, "vt": new_valid_to})
```

## 生命周期中的时间

### 时间线可视化

```{mermaid}
gantt
    title Fact 时间线
    dateFormat YYYY-MM-DD
    section 记录时间
    Old Version (recorded)  :2024-01-01, 2024-06-01
    New Version (recorded)  :2024-06-01, 2024-12-31
    section 有效时间
    Fact 有效              :2023-06-01, 2024-03-01
```

## 时间索引

### 索引策略

```sql
-- 按记录时间查询
CREATE INDEX idx_facts_recorded ON facts (recorded_from, recorded_to);

-- 按有效时间查询
CREATE INDEX idx_facts_valid ON facts (valid_from, valid_to);

-- 复合索引 (scope + 时间)
CREATE INDEX idx_facts_scope_recorded ON facts (scope, recorded_from);
```

### 查询优化

```sql
-- 使用索引的查询
EXPLAIN ANALYZE
SELECT * FROM facts 
WHERE scope = 'user:alice'
  AND valid_to IS NULL
  AND recorded_to IS NULL;
```

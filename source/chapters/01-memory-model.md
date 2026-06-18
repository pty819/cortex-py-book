# 第1章 五层记忆模型

## 设计理念

Cortex-PY 的核心是**五层记忆模型**，灵感来源于人类记忆系统：

```{mermaid}
graph TB
    subgraph 感觉记忆
        E[Events<br/>WAL, 不可变]
    end
    
    subgraph 短期记忆
        EP[Episodes<br/>有界事件序列]
    end
    
    subgraph 长期记忆
        F[Facts<br/>双时态三元组]
        B[Beliefs<br/>概率断言]
        U[Understanding<br/>概念合成]
    end
    
    E -->|抽取| F
    E -->|分段| EP
    EP -->|聚合| F
    F -->|推理| B
    B -->|合成| U
```

## 第1层：Events（事件层）

### 定义

Events 是系统的**唯一真相源**，采用 WAL (Write-Ahead Log) 模式，**不可变 append-only**。

### Schema

```sql
CREATE TABLE events (
    event_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset        BIGSERIAL UNIQUE NOT NULL,
    
    -- 寻址
    scope             TEXT NOT NULL,  -- 'org:acme/dept:eng/user:alice'
    
    -- 内容 (Experience Envelope)
    modality          TEXT NOT NULL,  -- conversation/document/tool_result
    content           JSONB NOT NULL, -- {kind, role, text, ...}
    context           JSONB NOT NULL, -- {observed_at, labels, intent, ...}
    
    -- 身份槽
    caller            TEXT NOT NULL,
    observed_actor    TEXT NOT NULL,
    subject           TEXT,
    
    -- 双时态 (Events 只有 2 字段)
    observed_at       TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    
    -- 幂等
    idempotency_key   TEXT NOT NULL,
    
    -- 修订约束
    UNIQUE (scope, idempotency_key)
);
```

### 幂等写入

```{mermaid}
flowchart TD
    A[写入请求] --> B{检查 idempotency_key}
    B -->|不存在| C[写入 Events]
    B -->|存在| D{Body 一致?}
    D -->|是| E[返回已有 event_id]
    D -->|否| F[抛出 409 Conflict]
    
    C --> G[Enqueue Job]
    C --> H[emit lifecycle]
```

**实现代码** (`core.py`):

```python
def append_event(*, scope, modality, content, context, 
                 caller, idempotency_key, ...):
    # 1. 幂等检查
    existing = c.execute(
        "SELECT event_id, wal_offset FROM events "
        "WHERE scope=:s AND idempotency_key=:k",
        {"s": scope, "k": idempotency_key}
    ).fetchone()
    
    if existing:
        # 计算 body hash 比对
        if body_hash_match(existing.event_id, content):
            return existing.event_id, existing.wal_offset
        raise IdempotencyConflict("同 key 不同 body")
    
    # 2. 写入 WAL
    row = c.execute("""
        INSERT INTO events (scope, modality, content, context, 
                           caller, idempotency_key, ...)
        VALUES (...)
        RETURNING event_id, wal_offset
    """).fetchone()
    
    # 3. 自动 provision scope
    _auto_provision_scope(c, scope)
    
    # 4. 发送生命周期事件
    emit_lifecycle(c, kind="captured", scope=scope, event_id=row.event_id)
    
    return row.event_id, row.wal_offset
```

### Experience Envelope

```python
# schemas.py
class Content(BaseModel):
    kind: str = "message"      # message|text|json|blob_ref|triple
    role: Optional[str] = None # user|assistant|tool|system
    text: Optional[str] = None
    data: Optional[Dict] = None
    blob_id: Optional[str] = None

class Context(BaseModel):
    observed_at: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    intent: Optional[str] = None
    preceded_by: List[str] = Field(default_factory=list)
```

## 第2层：Episodes（情节层）

### 定义

Episodes 是有界事件序列，按时间间隔自动分段。

### 分段逻辑

```{mermaid}
flowchart TD
    A[Events 序列] --> B{时间间隔 > 30min?}
    B -->|否| C[加入当前 Episode]
    B -->|是| D[封存当前 Episode]
    D --> E[创建新 Episode]
    E --> C
```

**实现** (`episodes.py`):

```python
def segment_scope(scope: str):
    """扫描 scope 下的 events，按时间间隔分段"""
    events = fetch_events_ordered(scope)
    
    current_episode = None
    last_time = None
    
    for event in events:
        if last_time and (event.observed_at - last_time).seconds > 1800:
            # 超过 30 分钟，封存当前 episode
            seal_episode(current_episode)
            current_episode = None
        
        if not current_episode:
            current_episode = create_episode(scope)
        
        add_event_to_episode(current_episode, event.event_id)
        last_time = event.observed_at
    
    if current_episode:
        seal_episode(current_episode)
```

## 第3层：Facts（事实层）

### 定义

Facts 是**双时态三元组**，同时承担：
1. 知识图谱的边 (subject-predicate-object)
2. 双时态记录 (当时 vs 现在)

### 双时态设计

```{mermaid}
graph LR
    subgraph 记录时间
        RT[recorded_from<br/>何时记录]
        RT2[recorded_to<br/>何时被取代]
    end
    
    subgraph 有效时间
        VT[valid_from<br/>何时开始为真]
        VT2[valid_to<br/>何时不再为真]
    end
    
    F[Fact] --> RT
    F --> RT2
    F --> VT
    F --> VT2
```

**4 个时间字段**：

| 字段 | 含义 |
|------|------|
| `recorded_from` | 系统何时获知此 fact |
| `recorded_to` | 此 fact 何时被新版本取代 (NULL=当前有效) |
| `valid_from` | 此 fact 在世界中何时开始为真 |
| `valid_to` | 此 fact 在世界中何时不再为真 (NULL=仍然为真) |

**查询示例**：

```sql
-- 问"现在什么是真的"
SELECT * FROM facts 
WHERE valid_to IS NULL AND recorded_to IS NULL;

-- 问"当时我们怎么以为的"
SELECT * FROM facts 
WHERE recorded_from < '2024-01-01' AND recorded_to > '2024-01-01';
```

### Schema

```sql
CREATE TABLE facts (
    fact_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope             TEXT NOT NULL,
    
    -- 三元组
    subject_id        UUID NOT NULL REFERENCES entities(entity_id),
    predicate         TEXT NOT NULL,
    object_entity_id  UUID REFERENCES entities(entity_id),
    object_value      JSONB,  -- {datatype, value}
    
    -- 置信度
    confidence        REAL NOT NULL DEFAULT 0.5,
    
    -- 双时态
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    recorded_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to       TIMESTAMPTZ,
    
    -- 证据链
    supports          UUID[] NOT NULL DEFAULT '{}',
    
    -- 向量
    embedding         vector(1024),
    
    -- 全文检索
    content_tsv       tsvector GENERATED ALWAYS AS (
        to_tsvector('english', predicate || ' ' || 
                    COALESCE(object_value->>'value', ''))
    ) STORED
);
```

## 第4层：Beliefs（信念层）

### 定义

Beliefs 是**概率断言**，从多个 facts 推理得出，带置信度和证据链。

### 结构

```{mermaid}
graph TB
    F1[Fact 1: Alice works at Acme] --> B[Belief: Alice 是 Acme 员工]
    F2[Fact 2: Acme 是公司] --> B
    F3[Fact 3: Alice 有 Acme 邮箱] --> B
    
    B --> C[confidence: 0.95]
    B --> D[supports: fact1_id, fact2_id, fact3_id]
```

### Schema

```sql
CREATE TABLE beliefs (
    belief_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope             TEXT NOT NULL,
    
    -- 关于谁
    about_entity_id   UUID NOT NULL REFERENCES entities(entity_id),
    canonical_name    TEXT NOT NULL,
    
    -- 断言
    stance            TEXT NOT NULL,  -- supports/refutes/neutral
    claim             TEXT NOT NULL,
    
    -- 置信度
    confidence        REAL NOT NULL,
    confidence_interval REAL[],
    
    -- 证据链
    supports          UUID[] NOT NULL DEFAULT '{}',
    
    -- 双时态
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    recorded_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to       TIMESTAMPTZ
);
```

## 第5层：Understanding（理解层）

### 定义

Understanding 是**概念合成**，从多个 beliefs 聚合得出高级概念。

### 合成流程

```{mermaid}
flowchart TD
    A[Beliefs 关于同一实体] --> B{LLM 合成}
    B --> C[生成 concept]
    C --> D[name: 概念名]
    C --> E[summary: 摘要]
    C --> F[confidence: 置信度]
    C --> G[related: 关联概念]
```

**实现** (`understanding.py`):

```python
def synthesize_scope(scope, topics=None):
    """从 beliefs 合成 understanding"""
    beliefs = fetch_beliefs_for_synthesis(scope)
    
    if not topics:
        # 自动按实体名分 topic
        topics = {b.canonical_name for b in beliefs}
    
    for topic in topics:
        relevant = [b for b in beliefs if b.canonical_name == topic]
        
        # 调 LLM 合成
        if llm_configured("synthesis"):
            result = llm_chat("synthesis", 
                "为给定主题合成一个概念...",
                material)
            concept = parse_json(result)
        else:
            # Mock: 简单聚合
            concept = {
                "name": topic,
                "summary": "; ".join(b.claim for b in relevant[:3]),
                "confidence": 0.6
            }
        
        # 存入 understanding 表
        insert_understanding(scope, concept)
```

## 层间关系

```{mermaid}
graph TB
    subgraph 写入
        E[Event] -->|1. append| WAL[WAL]
        WAL -->|2. enqueue| Q[Job Queue]
    end
    
    subgraph 异步处理
        Q -->|3. claim| W[Worker]
        W -->|4. extract| EXT[Extraction]
        EXT -->|5. link| LINK[Entity Linking]
        LINK -->|6. fact| F[Facts]
        LINK -->|7. entity| ENT[Entities]
        W -->|8. segment| EP[Episodes]
        W -->|9. aggregate| B[Beliefs]
        W -->|10. synthesize| U[Understanding]
    end
    
    subgraph 读取
        R[Recall] -->|query| RET[Retrieval]
        RET -->|6 channels| MIX[Mix]
        MIX -->|RRF| PACK[StratifiedPack]
    end
    
    F --> RET
    B --> RET
    U --> RET
```

# 第8章 Beliefs 与 Understanding

## 概述

Beliefs 和 Understanding 是五层记忆模型的**第4、5层**，负责从结构化事实中提炼更高阶的知识。

```{mermaid}
graph TB
    subgraph Layer 3
        F[Facts<br/>双时态三元组]
    end
    
    subgraph Layer 4
        B[Beliefs<br/>概率断言<br/>带证据链]
    end
    
    subgraph Layer 5
        U[Understanding<br/>概念合成<br/>Related 图]
    end
    
    F -->|aggregate| B
    B -->|synthesize| U
    U -->|relate| U2[Concept 关联]
```

## 第4层：Beliefs（信念层）

Beliefs 是 Facts 聚合而成的**概率断言**，回答"我们目前怎么看 X"。每个 Belief 带证据链（supports → facts → events）。

### Schema

```sql
CREATE TABLE beliefs (
    belief_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    about_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    stance         TEXT NOT NULL,  
        -- supports|likely_true|uncertain|likely_false|contradicts
    claim          TEXT NOT NULL,
    confidence     FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_interval JSONB,        -- [lower, upper]
    supports       UUID[] NOT NULL DEFAULT '{}',  -- → fact_id / event_id
    
    -- 双时态
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,
    recorded_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to    TIMESTAMPTZ,
    last_revised_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 5 种立场

| 立场 | 含义 | 示例 |
|------|------|------|
| `supports` | 证据支持 | "MFC-1流量偏差与刻蚀速率漂移正相关(r=0.85)" |
| `likely_true` | 很可能为真 | "腔体压力异常的根因很可能是密封圈老化" |
| `uncertain` | 不确定 | "温度漂移是否由冷却系统引起尚不明确" |
| `likely_false` | 很可能为假 | "MFC-1校准漂移的假设可能性低" |
| `contradicts` | 反驳/矛盾 | "MFC-1校准在公差内，反驳了MFC漂移假设" |

### 证据链

每个 Belief 的 `supports` 数组指向支持它的 facts/events，形成完整的溯源链：

```{mermaid}
graph TB
    E1[Event: 故障报告<br/>"压力异常，怀疑密封圈"] --> F1[Fact: 压力异常<br/>caused_by 密封圈老化]
    E2[Event: 排查记录<br/>"更换密封圈后恢复"] --> F2[Fact: 密封失效<br/>repaired_by 更换密封圈]
    
    F1 --> B1[Belief: supports<br/>"密封圈老化是根因<br/>置信度0.85"]
    F2 --> B1
    
    B1 --> U1[Concept: 密封类故障<br/>典型演化路径]
```

## 第5层：Understanding（理解层）

Understanding 层对 Beliefs 做**高阶概念合成**，产生主题级概括。下游 agent 召回这些 concept 做主题级理解。

### Schema

```sql
CREATE TABLE concepts (
    concept_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope        TEXT NOT NULL,
    name         TEXT NOT NULL,
    topic        TEXT,              -- 主题分类
    version      INT NOT NULL DEFAULT 1,
    summary      TEXT,              -- LLM 合成的概括
    supports     UUID[] NOT NULL DEFAULT '{}',  -- → fact_id / belief_id / episode_id
    related      JSONB NOT NULL DEFAULT '[]',    -- [{name, relation}]
    confidence   FLOAT NOT NULL DEFAULT 0.5,
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 概念合成

`synthesize_scope` 对 scope 内所有 beliefs 做 LLM 合成：

```python
def synthesize_scope(scope, topics=None):
    with session_scope() as conn:
        # 取该 scope 的 beliefs
        beliefs = conn.execute(text("""
            SELECT b.claim, b.confidence, e.canonical_name FROM beliefs b
            JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL
        """), {"s": scope}).fetchall()
        
        # 自动分 topic
        if not topics:
            topics = sorted({b.canonical_name for b in beliefs})[:5]
        
        for topic in topics:
            relevant = [b for b in beliefs if b.canonical_name == topic]
            material = json.dumps({"topic": topic, "beliefs": [...]})
            
            # LLM 合成
            if services.llm_configured("synthesis"):
                raw = services.llm_chat("synthesis", 
                    UNDERSTANDING_SYNTHESIZE, material)
                data = services.parse_llm_json(raw)
            else:
                data = {"name": topic, "summary": "mock", 
                        "confidence": 0.6, "related": []}
            
            # 写入 concepts 表
            conn.execute(text("""
                INSERT INTO concepts (scope, name, topic, summary, 
                    supports, related, confidence)
                VALUES (:s, :n, :t, :sum, CAST(:sup AS uuid[]), 
                        CAST(:rel AS jsonb), :c)
            """), {...})
```

### Related 图

Concepts 之间通过 5 种关系关联：

| 关系 | 含义 | 示例 |
|------|------|------|
| `specializes` | 特化 | "密封失效" → "真空系统故障" |
| `generalizes` | 泛化 | "真空系统故障" → "密封失效" |
| `contrasts` | 对比 | "温度漂移" vs "压力漂移" |
| `co_occurs` | 共现 | "等离子不稳定" + "刻蚀速率漂移" |
| `causes` | 因果 | "密封失效" → "真空度下降" |

```{mermaid}
graph TB
    C1[真空系统故障] -->|generalizes| C2[密封失效]
    C1 -->|generalizes| C3[MFC 故障]
    C2 -->|causes| C4[真空度下降]
    C4 -->|co_occurs| C5[等离子不稳定]
    C5 -->|causes| C6[刻蚀速率漂移]
    C6 -->|co_occurs| C7[均匀性偏差]
    C3 -->|contrasts| C2
```

### Related 图遍历

```python
def related_concepts(concept_id, relation=None, depth=2, limit=20):
    """BFS 遍历 related 图"""
    with session_scope() as conn:
        visited = {concept_id}
        result = []
        frontier = [concept_id]
        
        for _ in range(depth):
            nxt = []
            for cid in frontier:
                c = get_concept(cid)
                for rel in (c["related"] or []):
                    row = conn.execute(text("""
                        SELECT concept_id::text FROM concepts 
                        WHERE scope=:s AND name=:n LIMIT 1
                    """), {"s": c["scope"], "n": rel["name"]}).fetchone()
                    if row and row[0] not in visited:
                        visited.add(row[0])
                        full = get_concept(row[0])
                        if full:
                            result.append(full)
                            nxt.append(row[0])
            frontier = nxt
            if not frontier:
                break
    return result
```

## Beliefs → Understanding 的完整流程

```{mermaid}
sequenceDiagram
    participant DB as PostgreSQL
    participant LLM as LLM Service
    
    Note over DB: Facts 已存在
    
    DB->>LLM: 取 scope 的 facts
    LLM-->>DB: aggregate → beliefs
    
    DB->>LLM: 取 scope 的 beliefs
    LLM-->>DB: synthesize → concepts
    
    DB->>LLM: 取 concepts 做 related 图
    LLM-->>DB: related [{name, relation}]
    
    Note over DB: Understanding 层就绪
```

## API

**FastAPI**：
```
GET  /v1/layers/beliefs        → 列出 beliefs
GET  /v1/layers/concepts       → 列出 concepts
GET  /v1/layers/concepts/{id}  → 单个 concept
POST /v1/understanding/synthesize → 手动触发合成
GET  /v1/understanding/coverage   → 覆盖率
```

**MCP 工具**：
```
list_beliefs(scope, about)    → 列出 beliefs
memory_search(query, scope)   → 检索结果含 beliefs
entity_edges(entity_id)       → 实体的所有 facts
```

## 使用场景

1. **Belief 检索**：agent 问"对密封圈老化有多大把握？"→ 召回 related beliefs
2. **概念理解**：agent 问"密封类故障的典型模式"→ 召回 concept summary
3. **类比推理**：related 图找到类似概念（co_occurs / causes）
4. **知识覆盖审计**：coverage 查询显示哪些 topic 已合成、哪些缺 belief
5. **溯源验证**：每个 belief 的 supports → fact → event 可完整追溯

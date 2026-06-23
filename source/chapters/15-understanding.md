# 第15章 Understanding 层

## 概述

Understanding 层是五层记忆模型的最高层，对 Beliefs 做**高阶概念合成**，产生主题级概括。下游 agent 召回这些 concept 做主题级理解（如"真空系统密封类故障的典型演化路径是什么"）。

```{mermaid}
graph TB
    F[Facts] -->|aggregate| B[Beliefs]
    B -->|synthesize| U[Concepts]
    U -->|related 图| U2[Concept 关联]
    U -->|coverage| R[Coverage Report]
```

## Concepts 表

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
    confidence   FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 概念合成

`synthesize_scope` 函数对 scope 内所有 beliefs 做合成：

```python
def synthesize_scope(scope, topics=None):
    """对 scope 做 Understanding 合成"""
    with session_scope() as conn:
        # 取该 scope 的 beliefs + 高 confidence facts
        beliefs = conn.execute(text("""
            SELECT b.claim, b.confidence, e.canonical_name FROM beliefs b
            JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL
        """), {"s": scope}).fetchall()
        
        # 自动分 topic：按实体 canonical_name 分
        if not topics:
            topics = sorted({b.canonical_name for b in beliefs})[:5]
        
        for topic in topics:
            # LLM 合成
            if services.llm_configured("synthesis"):
                raw = services.llm_chat("synthesis",
                    UNDERSTANDING_SYNTHESIZE, material)
                data = services.parse_llm_json(raw)
            else:
                data = {"name": topic, "summary": f"{topic}: ...",
                        "confidence": 0.6, "related": []}
            
            # 写入 concepts 表
            conn.execute(text("""
                INSERT INTO concepts (scope, name, topic, summary, supports, related, confidence)
                VALUES (:s, :n, :t, :sum, CAST(:sup AS uuid[]), CAST(:rel AS jsonb), :c)
            """), {...})
```

## Related 图

Concepts 之间通过 5 种关系关联：

| 关系 | 含义 | 示例 |
|------|------|------|
| `specializes` | 特化 | "密封失效" specializes "真空系统故障" |
| `generalizes` | 泛化 | "真空系统故障" generalizes "密封失效" |
| `contrasts` | 对比 | "温度漂移" contrasts "压力漂移" |
| `co_occurs` | 共现 | "等离子不稳定" co_occurs "刻蚀速率漂移" |
| `causes` | 因果 | "密封失效" causes "真空度下降" |

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
                    # 按 name 找关联 concept
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

## 覆盖率查询

```python
def coverage(scope):
    """查询 scope 的 Understanding 覆盖率"""
    with session_scope() as conn:
        total = conn.execute(text("""
            SELECT count(*) FROM concepts WHERE scope=:s
        """), {"s": scope}).scalar() or 0
        
        rows = conn.execute(text("""
            SELECT topic, count(*), avg(confidence) 
            FROM concepts WHERE scope=:s GROUP BY topic
        """), {"s": scope}).fetchall()
    
    return {
        "concept_count": total,
        "by_topic": [{"topic": r[0], "concepts": r[1], 
                      "avg_confidence": float(r[2])} for r in rows]
    }
```

## API 与 MCP

**FastAPI**：
```
GET  /v1/layers/concepts        → 列出 concepts
GET  /v1/layers/concepts/{id}   → 单个 concept
POST /v1/understanding/synthesize → 手动触发合成
GET  /v1/understanding/coverage  → 覆盖率
```

**MCP 工具**（通过 `entity_list` 等间接使用）：
```
memory_search → StratifiedPack 含 beliefs
```

## 使用场景

1. **主题级检索**：agent 问"真空系统密封类故障的典型模式"→ 召回相关 concept 的 summary
2. **类比推理**：related 图找到类似概念（co_occurs / causes）
3. **知识覆盖审计**：coverage 查询显示哪些 topic 已合成、哪些缺 belief
4. **概念演化**：version 字段跟踪概念随新 belief 加入而更新

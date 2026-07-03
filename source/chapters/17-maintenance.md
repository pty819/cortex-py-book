# 第17章 Maintenance 系统

## 概述

Maintenance 系统负责记忆的**演化管理**，包括甲基化（软剪枝）、去重（consolidation）和词表预置。

```{mermaid}
graph TB
    subgraph Methylation
        E[Events] -->|access_count=0 + 超时| M[excluded_from_recall=true]
    end
    
    subgraph Consolidation
        F1[Fact v1] -->|同 subject/predicate/object| D[保留最新, 关闭旧]
        F2[Fact v2] --> D
    end
    
    subgraph Vocab Seed
        V[vocabularies] -->|预置| P[诊断谓词词表]
    end
```

## Methylation（甲基化）

**目的**：长期不召回的事件自动标记为 excluded_from_recall，减少检索噪声。

```python
def methylation_run(scope, older_than_days=30):
    """标记 access_count=0 且超过阈值的 events"""
    with session_scope() as conn:
        r = conn.execute(text("""
            UPDATE events SET excluded_from_recall=true, methylated_at=now()
            WHERE scope=:s AND excluded_from_recall=false AND access_count=0
              AND observed_at < now() - make_interval(secs => :secs)
        """), {"s": scope, "secs": float(older_than_days * 86400)})
        n = r.rowcount or 0
    
    with session_scope() as conn:
        emit_lifecycle(conn, kind="methylated", scope=scope,
                       payload={"methylated": n})
    return {"methylated": n, "older_than_days": older_than_days}
```

**触发条件**：
- `excluded_from_recall = false`（尚未被甲基化）
- `access_count = 0`（从未被召回）
- `observed_at < now() - 30 天`（超过 30 天）

**可逆操作**：甲基化只设标志，不删数据。手动设回 `excluded_from_recall=false` 即可恢复。

```{mermaid}
flowchart LR
    A[Events] --> B{access_count > 0?}
    B -->|是| C[活跃, 保留]
    B -->|否| D{超过 30 天?}
    D -->|是| E[甲基化<br/>excluded_from_recall=true]
    D -->|否| F[等待]
    E -->|可逆| G[手动恢复]
```

## Consolidation（去重）

**目的**：同 (subject, predicate, object) 的重复 live facts → 保留最新版本，旧版本 closed。

```python
def consolidation_run(scope):
    """同 subject/predicate/object 的重复 live facts → 保留最新"""
    with session_scope() as conn:
        # 找到需要合并的重复组
        r = conn.execute(text("""
            UPDATE facts f SET recorded_to = now()
            WHERE recorded_to IS NULL AND valid_to IS NULL
              AND f.fact_id NOT IN (
                SELECT fact_id FROM (
                  SELECT fact_id, ROW_NUMBER() OVER (
                    PARTITION BY scope, subject_id, predicate, 
                                 COALESCE(object_entity_id::text, object_value->>'value')
                    ORDER BY confidence DESC, extracted_at DESC
                  ) AS rn FROM facts
                  WHERE recorded_to IS NULL AND valid_to IS NULL
                    AND scope=:s
                ) sub WHERE rn = 1
              )
        """), {"s": scope})
        n = r.rowcount or 0
    
    return {"consolidated": n, "scope": scope}
```

**选择规则**：每个重复组保留 `confidence DESC, extracted_at DESC` 第一条。

**超替语义**：旧 fact 的 `recorded_to = now()`（认知上已过时），`valid_to` 保持不变（保留历史上为真的时间窗口）。

## 诊断谓词词表预置

```python
def seed_diagnosis_vocab(scope):
    """预置诊断场景的因果谓词闭合词表"""
    n = 0
    with session_scope() as conn:
        # 创建 'predicate' 词表（closed, multi）
        row = conn.execute(text("""
            INSERT INTO vocabularies (scope, name, kind, description, cardinality)
            VALUES (:s, 'predicate', 'closed', 'Diagnosis causal predicates', 'multi')
            ON CONFLICT (scope, name) DO UPDATE SET cardinality='multi'
            RETURNING vocab_id
        """), {"s": scope}).fetchone()
        
        # 预置所有诊断谓词
        for pred, desc, card in DIAGNOSIS_PREDICATES:
            r = conn.execute(text("""
                INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases, cardinality)
                VALUES (:v, :c, '{}', :card)
                ON CONFLICT (vocab_id, canonical_value) DO UPDATE SET cardinality=:card
            """), {"v": str(row.vocab_id), "c": pred, "card": card})
            n += r.rowcount or 0
    return n
```

## 启动命令

```bash
# 甲基化
uv run python -m cortex.interfaces.cli maintenance --action methylation --scope equip:XXX-v1

# 去重
uv run python -m cortex.interfaces.cli maintenance --action consolidation --scope equip:XXX-v1

# 预置诊断词表
uv run python -m cortex.interfaces.cli maintenance --action seed-vocab --scope equip:XXX-v1
```

## API 端点

```
POST /v1/maintenance/methylation    → 触发甲基化
POST /v1/maintenance/consolidation  → 触发去重
POST /v1/maintenance/vocab/seed     → 预置词表
```

## 最佳实践

| 操作 | 频率 | 说明 |
|------|------|------|
| Methylation | 每日/每周 | 自动清理长期不召回的历史事件 |
| Consolidation | 每次大量写入后 | 消除重复 facts，保持图谱干净 |
| Vocab Seed | 新 scope 创建时 | 预置诊断谓词约束 |

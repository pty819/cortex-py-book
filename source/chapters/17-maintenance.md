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

**触发方式的演化**：甲基化最初是纯批处理 job（由 maintenance 定时触发）。现在它**还**会被 Feedback 流程内联触发——`feedback._check_methylation` 在每次反馈提交后检查该 scope 的 `negative_feedback_count`，一旦越过 `demote_threshold` 就立即对该 scope 跑一次 methylation。也就是说甲基化不再只靠人工 maintenance 触发，负反馈累积到阈值会自动软剪枝对应记忆。

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

**与 Dreaming 的关系**：Dreaming 流程的 Phase 0 直接复用 `consolidation_run` 作为前置去重——先跑一遍 consolidation 把同 S/P/O 的重复 live facts 收敛到最新版本，Phase 1/2 才在干净的图上做 relation_detect + action_plan。详见第 23 章。

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

## 谓词定义预置（seed_predicate_definitions）

第 4 个 maintenance 动作：把 `ontology.py` 中定义的谓词 upsert 到 `predicate_definitions` 表，`prop_order=1`，使本体从纯代码常量升级为 DB 支撑的元数据表（带 category / order 字段），支持运行时查询与约束校验。

```python
def seed_predicate_definitions(scope=None):
    """upsert ontology.py 谓词到 predicate_definitions 表（prop_order=1）"""
    n = 0
    with session_scope() as conn:
        for pred in ONTOLOGY_PREDICATES:  # 来自 ontology.py
            r = conn.execute(text("""
                INSERT INTO predicate_definitions
                    (predicate, category, prop_order, description)
                VALUES (:p, :cat, 1, :d)
                ON CONFLICT (predicate) DO UPDATE
                SET category=:cat, prop_order=1, description=:d
            """), {"p": pred.name, "cat": pred.category,
                   "d": pred.description})
            n += r.rowcount or 0
    return n
```

**作用**：开启 DB-backed ontology——谓词不再只是代码里的 `Enum`，而是表里带 `category`（因果/观测/诊断/状态…）和 `prop_order` 的可查元数据。extract 环节可据此做闭集校验，未命中的谓词走 quarantine 而非直接入图。

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

maintenance 操作统一走单一端点，通过 body.action 区分：

```
POST /v1/admin/maintenance
  body.action ∈ {methylation, consolidation}
  body.scope  → 目标 scope
```

词表预置走专用端点：

```
POST /v1/admin/maintenance/vocab/seed   → 预置词表（scope）
```

> 注意：不存在 `/v1/maintenance/methylation`、`/v1/maintenance/consolidation` 这类按动作拆分的路径——旧文档中的写法已废弃，实际实现是单端点 + action 字段。

## 最佳实践

| 操作 | 频率 | 说明 |
|------|------|------|
| Methylation | 每日/每周 | 自动清理长期不召回的历史事件 |
| Consolidation | 每次大量写入后 | 消除重复 facts，保持图谱干净 |
| Vocab Seed | 新 scope 创建时 | 预置诊断谓词约束 |

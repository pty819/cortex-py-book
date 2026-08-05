# 第21章 Maintenance 系统

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
    return {"action": "methylation", "scope": scope,
            "methylated": n, "older_than_days": older_than_days}
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

**目的**：合并**完整语义身份**相同的 legacy duplicates——不同工况/Case/状态/层级永不折叠。

**分组维度**（20 列完整语义身份）：`subject_id, predicate, object_type, object_entity_id/object_value, polarity, assertion_status, knowledge_tier, operating_regime, case_id, valid_from, valid_to, confidence, salience, positive_feedback_count, negative_feedback_count, retrieval_usefulness, evidence_quality, diagnostic_correctness, population_prevalence, retrieval_count`。任一列不同即视为不同 fact，不合并。这与早期"同 S/P/O 三列去重"的简化描述完全不同——三列分组会把不同工况/状态的正负反馈混成一组，破坏双时态语义。

```python
def consolidation_run(scope, min_age_hours=24):
    """合并完整语义身份相同的 legacy duplicates；不同工况/Case/状态/层级永不折叠。"""
    with session_scope() as conn:
        # 1. 找重复组(>1 条 live fact 同完整语义身份)
        # min_age_hours 守卫:extracted_at < now() - interval(默认24h),避免合并刚抽取的
        dups = conn.execute(text("""
            SELECT subject_id, predicate, object_type,
                   coalesce(object_entity_id,''), coalesce(object_value->>'value',''),
                   polarity, assertion_status, knowledge_tier, operating_regime, case_id,
                   valid_from, valid_to, confidence, salience, positive_feedback_count,
                   negative_feedback_count, retrieval_usefulness, evidence_quality,
                   diagnostic_correctness, population_prevalence, retrieval_count
            FROM facts
            WHERE scope=:s AND recorded_to IS NULL
              AND extracted_at < now() - make_interval(secs => :secs)
            GROUP BY subject_id, predicate, object_type, oe, ov, polarity, assertion_status,
                     knowledge_tier, operating_regime, case_id, valid_from, valid_to,
                     confidence, salience, positive_feedback_count, negative_feedback_count,
                     retrieval_usefulness, evidence_quality, diagnostic_correctness,
                     population_prevalence, retrieval_count
            HAVING count(*) > 1
        """), {"s": scope, "secs": float(min_age_hours * 3600)}).fetchall()
        closed = 0
        for d in dups:
            # 2. survivor 选择:ORDER BY recorded_from DESC, fact_id(非 confidence DESC)
            fact_ids = [...ORDER BY recorded_from DESC, fact_id]
            survivor, redundant = fact_ids[0], fact_ids[1:]
            # 3. 合并三类引用到 survivor:
            #    - facts.supports(聚合并去重 event_id)
            #    - claim_evidence(ON CONFLICT DO NOTHING)
            #    - assertion_case_links(ON CONFLICT DO NOTHING)
            ...
            # 4. 软关 redundant facts
            closed += conn.execute(text(
                "UPDATE facts SET recorded_to=now() WHERE fact_id=ANY(:ids)"
            ), ...).rowcount or 0
    return {"action": "consolidation", "scope": scope,
            "facts_closed": closed, "groups": len(dups)}
```

**选择规则**：survivor = `ORDER BY recorded_from DESC, fact_id` 第一条（最早入库的优先保留，不是 confidence 最高的）。

**引用合并**：把 redundant facts 的三类引用迁到 survivor——`facts.supports`（聚合并去重 `event_id`）、`claim_evidence`、`assertion_case_links`，用 `ON CONFLICT DO NOTHING` 防重复。

**`min_age_hours` 守卫**：`extracted_at < now() - interval`（默认 24h），不合并刚抽取的 fact——给抽取管线时间稳定。

**返回键**：`action="consolidation"`, `scope`, `facts_closed`(软关条数), `groups`(重复组数)。注意是 `facts_closed`/`groups`，不是 `consolidated`。

**超替语义**：redundant fact 的 `recorded_to = now()`（认知上已过时），`valid_to` 保持不变（保留历史上为真的时间窗口）。

**与 Dreaming 的关系**：Dreaming 流程的 Phase 0 直接复用 `consolidation_run(scope, min_age_hours=0)` 作为前置去重——先跑一遍 consolidation 把完整语义身份重复的 live facts 收敛，Phase 1/2 才在干净的图上做 relation_detect + action_plan。详见第 12 章。

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
def seed_predicate_definitions() -> int:
    """把 ontology.PREDICATE_DICTIONARY 预置到 predicate_definitions 表(一阶,order=1)。幂等。"""
    n = 0
    with session_scope() as conn:
        for pred, d in PREDICATE_DICTIONARY.items():
            card = PREDICATE_CARDINALITY.get(pred, "multi")
            r = conn.execute(text("""
                INSERT INTO predicate_definitions (predicate, category, prop_order, cardinality, description, example)
                VALUES (:p, :c, 1, :card, :desc, :ex)
                ON CONFLICT (predicate) DO UPDATE
                SET category=:c, cardinality=:card, description=:desc, example=:ex
            """), {"p": pred, "c": d.category, "card": card, "desc": d.meaning, "ex": d.example})
            n += r.rowcount or 0
    return n
```

**不再是手工 cat_map**：早期实现从 `STRUCTURAL_PREDICATES`/`CAUSAL_PREDICATES`/`DIAGNOSTIC_PREDICATES`/`STATE_PREDICATES` 四个独立集合手工构建 `cat_map`。现在 `seed_predicate_definitions` 直接遍历单一真相源 `PREDICATE_DICTIONARY.items()`——`category` 取自 `PredicateDef.category`，并顺带把 `description`（`meaning`）和 `example` 一并 upsert 进 `predicate_definitions` 表。这套设计下四个分类集合（`STRUCTURAL_PREDICATES` 等）只是从词典 `frozenset` 派生，不再需要手工同步；prompt 中的谓词表格也由 `render_predicate_table` 从同一词典动态生成，消灭手工漂移。

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

> 注意：不存在 `/v1/maintenance/methylation`、`/v1/maintenance/consolidation` 这类按动作拆分的路径，也不存在 `POST /v1/admin/maintenance/vocab/seed` 端点。`seed_diagnosis_vocab` 是内部函数（由 CLI / 测试调用），不暴露为 HTTP 端点；consolidation 端点本身也不支持 vocab seed，seed_predicates 走的是 `/v1/admin/higher-order?seed_predicates=true`。旧文档中的写法已废弃，实际实现是单端点 + action 字段。

## 最佳实践

| 操作 | 频率 | 说明 |
|------|------|------|
| Methylation | 每日/每周 | 自动清理长期不召回的历史事件 |
| Consolidation | 每次大量写入后 | 消除重复 facts，保持图谱干净 |
| Vocab Seed | 新 scope 创建时 | 预置诊断谓词约束 |

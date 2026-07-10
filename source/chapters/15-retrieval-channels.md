# 第15章 检索通道详解

## 概述

cortex 的检索系统由 **6 个并行通道** 组成，每个通道从不同角度召回相关 facts，然后通过 RRF 融合。每个通道返回 fact_id 列表，统一时态过滤。

```{mermaid}
flowchart TB
    Q[用户查询] --> VEC[通道1: 向量]
    Q --> BM25[通道2: BM25]
    Q --> GRAPH[通道3: 图遍历]
    Q --> ENT[通道4: Entity Name]
    Q --> SYN[通道5: Synonym]
    Q --> TD[通道6: Temporal-decay]
    
    VEC --> RRF[RRF 融合]
    BM25 --> RRF
    GRAPH --> RRF
    ENT --> RRF
    SYN --> RRF
    TD --> RRF
    
    RRF --> RERANK[Prism Rerank]
    RERANK --> PACK[StratifiedPack]
```

## 通用时态过滤

所有通道共享统一的时态过滤逻辑（`_temporal_clause`）：

```python
def _temporal_clause(as_of, include_superseded):
    # 默认（无 as_of）：valid_to IS NULL AND recorded_to IS NULL（当前 live facts）
    # as_of（不含 include_superseded）：valid_from<=t<valid_to AND recorded_to IS NULL
    # as_of + include_superseded：包含历史认知
    ...
```

## 通道1：向量检索（_chan_vector）

**原理**：query → embedding → 实体近邻 → 近邻实体的 facts

```python
def _chan_vector(conn, scope, view, q_emb, top_k, as_of=None, include_superseded=False):
    frag, p = _scope_filter(scope, view)
    p["q"] = str(q_emb); p["k"] = top_k
    tc = _temporal_clause(as_of, include_superseded)
    sql = f"""
        WITH near AS (
          SELECT entity_id FROM entities
          WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
          ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k
        )
        SELECT DISTINCT f.fact_id::text FROM facts f
        WHERE f.{frag} AND f.{tc}
          AND (f.subject_id IN (SELECT entity_id FROM near)
               OR f.object_entity_id IN (SELECT entity_id FROM near))
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]
```

**执行流程**：
```
query → jina-v5 embedding(1024d) → pgvector HNSW 近邻搜索
→ 最近 N 个实体 → 这些实体的所有 live facts
```

## 通道2：BM25 全文检索（_chan_bm25）

**原理**：PostgreSQL tsvector 全文索引 + ILIKE 模糊匹配

```python
def _chan_bm25(conn, scope, view, query, top_k, as_of=None, include_superseded=False):
    frag, p = _scope_filter(scope, view)
    p["q"] = query; p["k"] = top_k; p["likeq"] = f"%{query.strip()}%"
    tc = _temporal_clause(as_of, include_superseded)
    sql = f"""
        SELECT fact_id::text FROM facts
        WHERE {frag} AND {tc}
          AND (to_tsvector('simple', 
               coalesce(predicate,'')||' '||coalesce(object_value->>'value','')
               ||' '||coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.subject_id),''))
               @@ plainto_tsquery(:q)
               OR coalesce(object_value->>'value','') ILIKE :likeq
               OR coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.subject_id),'') ILIKE :likeq)
        ORDER BY ts_rank(...) DESC
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]
```

**索引支持**：
- `idx_facts_text_fts`：facts 表 `(predicate || object_value)` 的 GIN tsvector 索引
- `idx_events_content_fts`：events 表 `content->>'text'` 的 GIN tsvector 索引

## 通道3：图遍历（_chan_graph）

**原理**：种子实体 → 递归 CTE BFS 沿 facts 边遍历 2-3 跳

```python
def _chan_graph(conn, scope, view, q_emb, max_hops, top_k, as_of=None, include_superseded=False):
    sql = """
      WITH RECURSIVE seeds AS (
        SELECT entity_id FROM entities WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
      ),
      graph_walk AS (
        SELECT f.object_entity_id AS node, f.fact_id, 1 AS hop
          FROM facts f, seeds s
         WHERE f.subject_id = s.entity_id AND {frag} AND {tc} AND {graph_eligible}
           AND f.object_entity_id IS NOT NULL
        UNION ALL
        SELECT f.object_entity_id, f.fact_id, gw.hop + 1
          FROM facts f JOIN graph_walk gw ON f.subject_id = gw.node
         WHERE {frag} AND {tc} AND {graph_eligible}
           AND f.object_entity_id IS NOT NULL
           AND gw.hop < :h
           AND NOT f.object_entity_id = ANY(gw.visited)
      )
      SELECT DISTINCT fact_id::text FROM graph_walk LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]
```

**关键设计**：
- 只走 `graph_eligible` 的边（排除 `no_correlation`/`contradicts`/`ruled_out`）
- 因果谓词边要求 `assertion_status='confirmed'`（未确认的假设不进图）
- 带 visited 环检测
- 默认 2 跳（`max_hops=2`），可配置到 3

## 通道4：Entity Name 匹配（_chan_entity_name）

**原理**：从查询中提取实体名 → pg_trgm 模糊匹配 → 匹配实体的 facts

```python
def _chan_entity_name(conn, scope, view, query, top_k, as_of=None, include_superseded=False):
    names = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", query)
    if not names:
        names = [w for w in re.findall(r"\w+", query) if len(w) > 3][:5]
    for nm in names:
        rows = conn.execute(text(f"""
            SELECT entity_id::text FROM entities
            WHERE {frag} AND merged_into IS NULL
              AND (canonical_name ILIKE :nm
                   OR EXISTS (SELECT 1 FROM entity_aliases a 
                     WHERE a.entity_id=entities.entity_id AND a.alias ILIKE :nm)
                   OR similarity(canonical_name, :nm) > 0.3)
        """), {**p, "nm": f"%{nm}%"}).fetchall()
    # 从匹配实体取 facts
    ...
```

**支持**：`pg_trgm` 扩展的 `similarity()` 函数 + ILIKE + 别名表

## 通道5：Synonym 同义词扩展（_chan_synonym）

**原理**：查询词 → synonyms 表查找同义词 → 扩展后重新 BM25 检索

```python
def _chan_synonym(conn, scope, query, as_of=None, include_superseded=False):
    words = re.findall(r"\w+", query.lower())
    terms = set(words)
    for w in words:
        rows = conn.execute(text("""
            SELECT term, aliases FROM synonyms 
            WHERE scope=:s AND (term=:w OR :w = ANY(aliases))
        """), {"s": scope, "w": w}).fetchall()
        for r in rows:
            terms.add(r[0]); terms.update(r[1] or [])
    if terms == set(words):
        return []  # 无同义词扩展
    expanded = " ".join(sorted(terms))
    # 用扩展后的查询做 BM25
    ...
```

**同义词表结构**：
```sql
CREATE TABLE synonyms (
    synonym_id  UUID PRIMARY KEY,
    scope       TEXT NOT NULL,
    term        TEXT NOT NULL,         -- 规范词, 如 "own"
    aliases     TEXT[] NOT NULL DEFAULT '{}',  -- 同义: "possess"/"has"/"owns"
    UNIQUE (scope, term)
);
```

## 通道6：Temporal-decay 时间衰减

**原理**：按 access_count（访问热度）+ 时间衰减加权，热数据优先

该通道使用 `events.access_count` 字段统计每个 event 被召回次数。access_count=0 且超过阈值的 events 会被 methylation 标记为 `excluded_from_recall`。

> **信号总线加权补充**：除上述 6 通道融合外,RRF 后还有一步**信号总线加权**(salience + access_count),详见第14章和第10章。temporal-decay 通道的 `access_count` 因子与信号总线的 `access_count` 是**同一字段**——召回频率高的记忆在 temporal-decay 通道得分更高(通道内排序),同时又在信号总线加权阶段叠加 `salience_weight * ac/10.0`(融合后加权),形成**双重加权**效应,使这类记忆在最终排序中更靠前。

## HyDE 假设性文档嵌入

在检索前，先用 LLM 生成一段"假设知识库里有完美答案"的文本，将其嵌入后用向量检索：

```python
HYDE_SYSTEM = """【本次任务：查询 → 假设性文本（用于向量检索召回）】

针对下游 agent 的查询，写一段"假设知识库里有完美匹配答案"的文本。
包含可能的关键实体名（故障/部件/传感器/控制器/征兆/参数/步骤）。
200-500字。纯文本，不输出 JSON/think。"""
```

> **并行化**：HyDE 的 N 次 LLM 调用已由检索 Phase 0 的第一波 `parallel_call` 与 query embed、multihop LLM 同时发起；第二波 `parallel_map` 对生成的 HyDE 文本并行 embed。详见[第14章 检索系统概述](14-retrieval-system)。

## Multihop 子问题分解

将复杂查询拆解为多个子查询，分别检索后融合结果：

```python
MULTIHOP_SYSTEM = """【本次任务：查询 → 多个子查询（用于多跳检索）】

将下游 agent 的诊断查询拆解为多个子查询。
每个子查询聚焦一个方面：根因层/征兆层/传感器特征/控制逻辑/
工艺参数/级联影响/历史案例/相关性分析/排除项。
输出 JSON {"queries": ["子查询1", ...]}"""
```

> **并行化**：Multihop 的 LLM 调用已并入检索 Phase 0 的第一波 `parallel_call`（与 HyDE 同时发起），不再串行。解析出的子查询在 Phase 1 的 BM25 通道内追加检索。详见[第14章](14-retrieval-system)。

## scope 过滤视图

所有通道都支持 4 种 scope 视图：

| 视图 | 含义 | SQL |
|------|------|-----|
| `local` | 精确 scope 匹配 | `scope = :scope` |
| `holistic` | 祖先链回溯 | `scope = ANY(前缀列表)` |
| `descend` | scope + 子 scope | `(scope = :s OR scope LIKE :s || '/%')` |
| `structured` | 精确匹配 | 同 local |

## 通道对比

| 通道 | 召回类型 | 适合场景 | 依赖 |
|------|---------|---------|------|
| 向量 | 语义相似 | "类似XX的问题" | embedding service |
| BM25 | 关键词精确 | 查具体参数/编号 | tsvector index |
| 图遍历 | 关联推理 | "XX的根因是什么" | graph_eligible facts |
| Entity Name | 模糊名称 | 记不全的名字 | pg_trgm |
| Synonym | 同义表达 | "owns vs has" | synonyms 表 |
| Temporal | 热数据 | "最近的问题" | access_count |

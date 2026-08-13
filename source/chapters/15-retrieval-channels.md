# 第15章 检索通道详解

## 概述

cortex 的检索系统由 **6 个通道** 组成，每个通道从不同角度召回相关 facts，然后通过 RRF 融合。每个通道返回 fact_id 列表，统一时态过滤。6 个通道在同一个 `session_scope()` 内**串行执行**(都是 DB 查询,无 HTTP I/O,并行化收益有限)。

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
    fragment, params = _scope_filter(scope, view)
    params.update({"q": str(q_emb), "k": top_k, **_temporal_params(as_of)})
    temporal = _temporal_clause(as_of, include_superseded)
    sql = f"""
        WITH near AS (
          SELECT entity_id, embedding <=> CAST(:q AS vector) AS distance FROM entities
          WHERE merged_into IS NULL AND deleted_at IS NULL AND embedding IS NOT NULL AND {fragment}
          ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k
        )
        SELECT f.fact_id::text FROM facts f
        JOIN near ON near.entity_id=f.subject_id OR near.entity_id=f.object_entity_id
        WHERE f.{fragment} AND f.{temporal}
        GROUP BY f.fact_id
        ORDER BY min(near.distance), f.fact_id
        LIMIT :k
    """
    return [row[0] for row in conn.execute(text(sql), params).fetchall()]
```

**执行流程**：
```
query → jina-embeddings-v5-text-small embedding(1024d) → pgvector HNSW 近邻搜索
→ 最近 N 个实体 → 这些实体的所有 live facts
```

## 通道2：BM25 全文检索（_chan_bm25）

**原理**：优先走 `pg_textsearch` 扩展的**真 BM25 索引**；未安装该扩展时回退到 PostgreSQL tsvector 全文索引 + ILIKE 模糊匹配

```python
def _chan_bm25(conn, scope, view, query, top_k, as_of=None, include_superseded=False, details=None):
    query = unicodedata.normalize("NFKC", query).strip()
    if not query:
        return []
    refreshed = refresh_search_documents(conn, scope, view)   # 读时修复 BM25 投影
    if bm25_index_ready(conn):                                 # pg_textsearch + 索引就绪
        lexemes, dictionary_revision = query_lexemes(conn, scope, view, query)
        unique_lexemes = list(dict.fromkeys(lexemes.split()))
        minimum_matches = (1 if len(unique_lexemes) <= 2
                           else min(3, max(2, (len(unique_lexemes) + 3) // 4)))
        if details is not None:
            details.update({"engine": "pg_textsearch", "true_bm25": True,
                            "query_lexemes": unique_lexemes,
                            "minimum_lexeme_matches": minimum_matches,
                            "dictionary_revision": dictionary_revision,
                            "documents_refreshed": refreshed})
        if not lexemes:
            return []
        # 在 fact_search_documents 投影上跑真 BM25(算子 <@> to_bm25query)
        rows = conn.execute(text("""
            WITH ranked AS MATERIALIZED (
                SELECT fact_id, tokenized_text,
                       tokenized_text <@> to_bm25query(:q, :index_name) AS distance
                FROM fact_search_documents
                WHERE {fragment} AND {temporal}
                  AND (tokenized_text <@> to_bm25query(:q, :index_name)) < 0
                ORDER BY tokenized_text <@> to_bm25query(:q, :index_name), fact_id
                LIMIT :candidate_k
            )
            SELECT fact_id::text FROM ranked
            WHERE (SELECT count(DISTINCT query_term)
                   FROM unnest(CAST(:query_lexemes AS text[])) AS query_terms(query_term)
                   WHERE query_term = ANY(string_to_array(ranked.tokenized_text, ' ')))
                  >= :minimum_matches
            ORDER BY distance, fact_id LIMIT :k
        """), params).fetchall()
        return [row[0] for row in rows]
    # 回退:PostgreSQL legacy tsvector + ILIKE + 中文 n-gram(见下文)
    ...
```

**真 BM25 索引机制**：中文文本先在 Python 侧用 jieba 按 `text_config='simple'` 切词,持久化到 `fact_search_documents` 投影表(`tokenized_text` 空格分隔的 lexeme),由 `pg_textsearch` 扩展的 `<@>` / `to_bm25query()` 算子做真正的 BM25 距离排序。`refresh_search_documents()` 在检索前按 `fact_search_dirty_scopes` 脏标记**事务性增量修复**投影(不变文档按 hash 跳过),保证多条 Fact 写入路径都不会让索引 stale。索引名 `idx_fact_search_documents_bm25`。

**回退路径**(未装 `pg_textsearch`):用 `to_tsvector('simple', predicate + object_value + subject/object canonical_name)` 的 tsvector 全文 + ILIKE 模糊匹配,并叠加中文 CJK 2/3-gram 命中计数(`_cjk_search_terms`)提升中文召回。相关索引:`idx_facts_text_fts`(facts `(predicate || object_value)` GIN)、`idx_events_content_fts`(events `content->>'text'` GIN)。

## 通道3：图遍历（_chan_graph）

**原理**：种子实体 → 递归 CTE BFS 沿 facts 边遍历（默认 2 跳）

```python
def _chan_graph(conn, scope, view, q_emb, max_hops, top_k, as_of=None, include_superseded=False,
                seed_limit=5, max_edges_per_node=50, max_paths=2000):
    fragment, params = _scope_filter(scope, view)
    fact_fragment = re.sub(r"\bscope\b", "f.scope", fragment)
    params.update({"q": str(q_emb), "k": top_k, "h": max_hops,
                   "seed_limit": seed_limit, "fanout": max_edges_per_node,
                   "path_limit": max_paths, **_temporal_params(as_of)})
    temporal = _temporal_clause(as_of, include_superseded)
    sql = """
      WITH RECURSIVE seeds AS (
        SELECT entity_id FROM entities WHERE merged_into IS NULL AND deleted_at IS NULL AND embedding IS NOT NULL AND {fragment}
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT :seed_limit
      ),
      eligible_edges AS MATERIALIZED (
        SELECT from_node, to_node, fact_id FROM (
          SELECT from_node, to_node, fact_id,
                 row_number() OVER (PARTITION BY from_node ORDER BY fact_id) AS edge_rank
          FROM (
            SELECT f.subject_id AS from_node, f.object_entity_id AS to_node, f.fact_id
              FROM facts f
             WHERE {fact_fragment} AND f.{temporal}
               AND {_graph_eligible_sql('f')}
               AND f.object_entity_id IS NOT NULL
            UNION ALL
            SELECT f.object_entity_id AS from_node, f.subject_id AS to_node, f.fact_id
              FROM facts f
             WHERE {fact_fragment} AND f.{temporal}
               AND {_graph_eligible_sql('f')}
               AND f.object_entity_id IS NOT NULL
          ) bidir
        ) ranked
        WHERE ranked.edge_rank <= :fanout
      ),
      graph_walk AS (
        SELECT edge.to_node AS node, edge.fact_id, 1 AS hop,
               ARRAY[s.entity_id, edge.to_node]::uuid[] AS visited
          FROM seeds s JOIN eligible_edges edge ON edge.from_node = s.entity_id
        UNION ALL
        SELECT edge.to_node, edge.fact_id, gw.hop + 1,
               gw.visited || edge.to_node
          FROM graph_walk gw
          JOIN eligible_edges edge ON edge.from_node = gw.node
         WHERE gw.hop < :h AND NOT edge.to_node = ANY(gw.visited)
      ),
      graph_walk_limited AS MATERIALIZED (
        SELECT * FROM graph_walk LIMIT :path_limit
      )
      SELECT fact_id::text FROM graph_walk_limited WHERE hop <= :h
      GROUP BY fact_id
      ORDER BY min(hop), fact_id
      LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), params).fetchall()]
```

**关键设计**：
- 只走 `graph_eligible` 的边（**要求 `knowledge_tier='verified'`**，排除 `no_correlation`/`contradicts`/`ruled_out`）
- 因果谓词边要求 `assertion_status='confirmed'`（未确认的假设不进图）
- seeds 限制 `seed_limit`（默认 5）、每节点出边扇出 `fanout`（默认 50）、全图路径上限 `path_limit`（默认 2000），双向遍历，带 visited 环检测
- 默认 2 跳（`graph_max_hops=2`），可配置到 10

> **传感器解析**：`POST /v1/sensors/resolve` 走独立的 **LLM 解析 → 向量检索 top-1 → 沿 `STRUCTURAL_PREDICATES` 出边 BFS（最多 5 跳）** 流程收集 `entity_type='sensor'` 节点，用于把自然语言查询解析成关联传感器名。它复用本通道的"种子实体向量检索 + 图遍历"思路，但只沿结构谓词（`has_component`/`installed_on`/`monitored_by` 等）单向出边，不经过 RRF 融合（详见 API 章节）。

## 通道4：Entity Name 匹配（_chan_entity_name）

**原理**：从查询中提取实体名 → pg_trgm 模糊匹配 → 匹配实体的 facts

```python
def _chan_entity_name(conn, scope, view, query, top_k, as_of=None, include_superseded=False):
    fragment, params = _scope_filter(scope, view)
    params.update({"k": top_k, **_temporal_params(as_of)})
    temporal = _temporal_clause(as_of, include_superseded)
    names = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", query)
    if not names:
        names = [w for w in re.findall(r"\w+", query) if len(w) > 3][:5]
    if not names:
        return []
    entity_ids = []
    for name in names:
        rows = conn.execute(text(f"""
            SELECT entity_id::text FROM entities
            WHERE {fragment} AND merged_into IS NULL AND deleted_at IS NULL
              AND (canonical_name ILIKE :nm
                   OR EXISTS (SELECT 1 FROM entity_aliases a
                              WHERE a.entity_id=entities.entity_id AND a.alias ILIKE :nm)
                   OR similarity(canonical_name, :raw_name) > 0.3)
            ORDER BY CASE
                WHEN lower(canonical_name)=lower(:raw_name) THEN 0
                WHEN canonical_name ILIKE :nm THEN 1
                WHEN EXISTS (SELECT 1 FROM entity_aliases a
                             WHERE a.entity_id=entities.entity_id AND a.alias ILIKE :nm) THEN 2
                ELSE 3 END,
                similarity(canonical_name, :raw_name) DESC, entity_id
            LIMIT 5
        """), {**params, "nm": f"%{name}%", "raw_name": name}).fetchall()
        entity_ids.extend(r[0] for r in rows)
    entity_ids = list(dict.fromkeys(entity_ids))
    if not entity_ids:
        return []
    # 从匹配实体(保序)取 facts
    rows = conn.execute(text(f"""
        WITH ranked_entities AS (
          SELECT entity_id, rank FROM unnest(CAST(:eids AS uuid[]))
          WITH ORDINALITY AS ranked(entity_id, rank)
        )
        SELECT facts.fact_id::text FROM facts
        JOIN ranked_entities ON ranked_entities.entity_id=facts.subject_id
                             OR ranked_entities.entity_id=facts.object_entity_id
        WHERE {fragment} AND {temporal}
        GROUP BY facts.fact_id
        ORDER BY min(ranked_entities.rank), facts.fact_id
        LIMIT :k
    """), {**params, "eids": "{" + ",".join(entity_ids) + "}"}).fetchall()
    return [r[0] for r in rows]
```

**支持**：`pg_trgm` 扩展的 `similarity()` 函数 + ILIKE + 别名表

## 通道5：Synonym 同义词扩展（_expand_synonyms）

**原理**：查询词 → synonyms 表查找**受管控(governed)的同义词组** → 扩展后重新 BM25 检索

```python
def _expand_synonyms(conn, scope, view, query, top_k=40, as_of=None, include_superseded=False, details=None):
    # 只匹配 status='active' 的同义词组;命中 canonical term 或任一 alias 即加入
    terms, groups = terminology.expanded_terms(conn, scope=scope, view=view, query=query)
    if details is not None:
        details["expanded_terms"] = terms
        details["matched_groups"] = [
            {"synonym_id": g["synonym_id"], "scope": g["scope"],
             "term": g["term"], "matched_members": g["matched_members"]}
            for g in groups
        ]
    if not terms:
        return []                                             # 无同义词命中 → 空
    # 用扩展后的 terms 做候选:任一 term 命中 tsvector 或 ILIKE 即入选
    sql = """
        WITH candidates AS (
            SELECT fact_id,
                   to_tsvector('simple',coalesce(predicate,'')||' '||
                                        coalesce(object_value->>'value','')) AS document,
                   coalesce(predicate,'')||' '||coalesce(object_value->>'value','') AS raw_text
            FROM facts WHERE {fragment} AND {temporal}
        )
        SELECT fact_id::text FROM candidates
        WHERE EXISTS (
            SELECT 1 FROM unnest(CAST(:terms AS text[])) AS expansion(term)
            WHERE document @@ plainto_tsquery('simple', expansion.term)
               OR raw_text ILIKE ('%' || expansion.term || '%')
        )
        ORDER BY coalesce((SELECT max(ts_rank(document, plainto_tsquery('simple', expansion.term)))
            FROM unnest(CAST(:terms AS text[])) AS expansion(term)), 0) DESC, fact_id
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), params).fetchall()]
```

命中语义是 **OR 式**：只要扩展后的任一 term(includes 各命中的 canonical + 全部 aliases)匹配就入选，而非要求全部词同时出现。同义词组由 `terminology` 模块管理（`create_synonym` / `import_synonyms`），支持 `status`（`draft`/`active`/`retired`）——**只有 `active` 组参与扩展**，draft 存入不影响线上行为。

**同义词表结构**：
```sql
CREATE TABLE synonyms (
    synonym_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    term        TEXT NOT NULL,         -- 规范词, 如 "own"
    aliases     TEXT[] NOT NULL DEFAULT '{}',  -- 同义: "possess"/"has"/"owns"
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','retired')),
    locale      TEXT NOT NULL DEFAULT 'und',
    domain      TEXT NOT NULL DEFAULT 'general',
    source      TEXT NOT NULL DEFAULT 'manual',
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_by  TEXT, reviewed_by TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, term)
);
```

## 通道6：Temporal-decay 时间衰减

**原理**：按 `valid_from` 近因窗内 facts，纯时间衰减排序（越新越靠前），不依赖 `access_count`。通道内排序只看时间新近度，不看热度。

真实实现（`channels.py:_chan_temporal_decay`）：scope 过滤 + 时态过滤 + `valid_from >= anchor - decay_days` 近因窗 + `ORDER BY valid_from DESC LIMIT :k`。**完全不读 `access_count`，不 JOIN events**。

```python
def _chan_temporal_decay(conn, scope, view, top_k, decay_days=30,
                         as_of=None, include_superseded=False):
    fragment, params = _scope_filter(scope, view)
    params.update({"k": top_k, "d": decay_days, **_temporal_params(as_of)})
    temporal = _temporal_clause(as_of, include_superseded)
    anchor = "CAST(:ao AS timestamptz)" if as_of else "now()"
    sql = f"""
        SELECT fact_id::text FROM facts
        WHERE {fragment} AND {temporal}
          AND valid_from >= {anchor} - make_interval(secs => :d * 86400)
        ORDER BY valid_from DESC LIMIT :k
    """
    return [row[0] for row in conn.execute(text(sql), params).fetchall()]
```

> **信号总线加权补充**：除上述 6 通道融合外,融合后还有一步**四信号加权**(salience / usage / usefulness / exploration),详见第14章和第10章。注意:temporal-decay 通道内排序只看时间新近度(不看热度),Usage(被动召回次数)只在**融合后的信号总线加权**阶段叠加加分,不会在通道内排序阶段介入。

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

所有通道都支持 3 种 scope 视图（`_scope_filter` 只有 `holistic` / `descend` / 其他=local 三路，无 `structured` 分支）：

| 视图 | 含义 | SQL |
|------|------|-----|
| `local`（默认） | 精确 scope 匹配 | `scope = :scope` |
| `holistic` | 祖先链回溯 | `scope = ANY(前缀列表)` |
| `descend` | scope + 子 scope | `(scope = :s OR scope LIKE :s || '/%')` |

## 通道对比

| 通道 | 召回类型 | 适合场景 | 依赖 |
|------|---------|---------|------|
| 向量 | 语义相似 | "类似XX的问题" | embedding service |
| BM25 | 关键词精确 | 查具体参数/编号 | pg_textsearch BM25 索引 / tsvector |
| 图遍历 | 关联推理 | "XX的根因是什么" | graph_eligible facts |
| Entity Name | 模糊名称 | 记不全的名字 | pg_trgm |
| Synonym | 同义表达 | "owns vs has" | synonyms 表 |
| Temporal | 最近 facts | "最近的问题" | valid_from |

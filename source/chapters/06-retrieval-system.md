# 第6章 检索系统概述

## 设计目标

检索系统的核心目标是**从记忆中找到最相关的信息**，支持 6 通道混合检索。

```{mermaid}
graph TB
    subgraph 输入
        Q[用户查询]
    end
    
    subgraph 6通道
        V[向量通道]
        B[BM25 通道]
        G[图遍历通道]
        N[Entity Name 通道]
        S[Synonym 通道]
        T[Temporal-Decay 通道]
    end
    
    subgraph 融合
        RRF[RRF 融合]
        R[Rerank]
    end
    
    subgraph 输出
        P[StratifiedPack]
    end
    
    Q --> V
    Q --> B
    Q --> G
    Q --> N
    Q --> S
    Q --> T
    
    V --> RRF
    B --> RRF
    G --> RRF
    N --> RRF
    S --> RRF
    T --> RRF
    
    RRF --> R
    R --> P
```

## 6 通道详解

| 通道 | 方法 | 来源 | 最适合 |
|------|------|------|--------|
| **Vector** | 实体 embedding 近邻 → 其 facts | `entities.embedding` | 语义匹配 |
| **BM25** | 全文检索 facts 文本 | `facts` tsvector | 关键词匹配 |
| **Graph** | 种子实体 BFS 图遍历 | `facts` 图边 | 关系发现 |
| **Entity Name** | 查询中的人名/实体名精确匹配 | `entities.canonical_name` | 命名实体 |
| **Synonym** | 同义词扩展后再 BM25 | `synonyms` 表 | 词义泛化 |
| **Temporal-Decay** | 近因窗内 facts，按时间衰减 | `facts.valid_from` | 时效性 |

## Scope 过滤

```python
def _scope_filter(scope, view):
    """返回 (SQL fragment, params)。"""
    if view == "holistic":
        prefixes = ["/".join(scope.split("/")[:i]) for i in range(1, len(scope.split("/")) + 1)]
        return "scope = ANY(:scopes)", {"scopes": prefixes}
    if view == "descend":
        return "(scope = :scope0 OR scope LIKE :scopep)", {"scope0": scope, "scopep": scope + "/%"}
    return "scope = :scope0", {"scope0": scope}
```

## 时态过滤 (`_temporal_clause`)

所有通道统一使用时态过滤：

```python
def _temporal_clause(as_of, include_superseded):
    """返回通道 SQL 的时间过滤片段。
    
    默认(无 as_of): valid_to IS NULL AND recorded_to IS NULL (当前 live facts)
    as_of: valid_from<=t<valid_to AND recorded_to IS NULL (当时为真+当前认知)
    as_of + include_superseded: 含历史认知版本
    """
    if as_of:
        base = "valid_from <= CAST(:ao AS timestamptz) AND (valid_to IS NULL OR CAST(:ao AS timestamptz) < valid_to)"
        if include_superseded:
            return (base + " AND recorded_from <= CAST(:ao AS timestamptz) "
                    "AND (recorded_to IS NULL OR CAST(:ao AS timestamptz) < recorded_to)")
        return base + " AND recorded_to IS NULL"
    if not include_superseded:
        return "valid_to IS NULL AND recorded_to IS NULL"
    return "valid_to IS NULL"
```

## 图准入 SQL

```python
def _graph_eligible_sql(alias="f"):
    """生成图遍历的 eligibility 条件"""
    causal = ",".join(f"'{p}'" for p in sorted(CAUSAL_PREDICATES))
    excluded = ",".join(f"'{p}'" for p in sorted(GRAPH_EXCLUDED_PREDICATES))
    return (f"{alias}.polarity='positive' AND {alias}.predicate NOT IN ({excluded}) "
            f"AND (({alias}.predicate IN ({causal}) AND {alias}.assertion_status='confirmed') "
            f"OR ({alias}.predicate NOT IN ({causal}) AND {alias}.assertion_status IN ('observed','confirmed')))")
```

## 检索主流程

```{mermaid}
flowchart TB
    Q[用户查询] --> P0[Phase 0: Embedding]
    P0 --> P0a[HyDE 假设性文本?]
    P0a -->|yes| LLM1[LLM 生成假设文本]
    LLM1 --> EMB[embed_one]
    P0a -->|no| EMB
    EMB --> P1[Phase 1: 6通道]
    
    subgraph Phase 1
        V[Vector]
        B[BM25]
        G[Graph]
        N[Entity Name]
        S[Synonym]
        T[Temporal]
    end
    
    P1 --> RRF[RRF Fusion]
    RRF --> P2[Phase 2: Rerank]
    P2 --> P3[Phase 3: Pack 装配]
    P3 --> CACHE[Cache to recall_packs]
    CACHE --> OUT[StratifiedPack]
```

## 问题类型路由

```python
def _question_type(query):
    """规则版路由:多 session 信号→multi,否则 single"""
    multi_signals = sum(1 for w in ("last", "previous", "earlier", "before", "yesterday", "history") 
                        if w in query.lower())
    if multi_signals >= 1 or query.lower().count(" ") >= 8:
        return "multi-session"
    return "single-session"
```

## HyDE (Hypothetical Document Embedding)

```python
if adv.hyde_enabled and services.llm_configured("synthesis"):
    for _ in range(adv.hyde_passages):
        raw = services.llm_chat("synthesis",
            "写一段假设性回答(假设记忆里有答案),纯文本无前缀。", query)
        extra_embs.append(services.embed_one(services.strip_think(raw)))
```

## Multihop 子问题分解

```python
if adv.multihop_enabled and services.llm_configured("synthesis"):
    raw = services.llm_chat("synthesis", MULTIHOP_SYSTEM,
        json.dumps({"query": query, "n": adv.multihop_count}))
    subs = services.parse_llm_json(raw)
    for sq in (subs.get("queries") or [])[:adv.multihop_count]:
        c_bm25 = list(dict.fromkeys(c_bm25 + _chan_bm25(conn, scope, view, sq, top_k, ...)))
```

## StratifiedPack 缓存

```python
def _cache_pack(conn, pack):
    qh = hashlib.sha256((pack["scope"] + json.dumps(pack["layers"], sort_keys=True)).encode()).hexdigest()[:16]
    conn.execute(text("""
        INSERT INTO recall_packs (pack_id, scope, query_hash, pack_json, expires_at)
        VALUES (:id,:s,:h,CAST(:j AS jsonb), now() + interval '60 second')
    """), {"id": pack["pack_id"], "s": pack["scope"], "h": qh, "j": json.dumps(pack)})
```

## 完整 recall 调用

```python
def recall(*, scope, query=None, view="local", top_k=None, as_of=None,
           valid_during=None, recorded_during=None, include_superseded=False,
           budgets=None, citation_mode="inline_with_markers",
           exclude_content=False) -> Dict[str, Any]:
    """混合检索主入口"""
    cfg = load_config()
    top_k = top_k or cfg.retrieval.top_k
    
    # Phase 0: Embedding
    q_emb = services.embed_one(query)
    
    # Phase 1: 6通道 + RRF
    with session_scope() as conn:
        c_vec = _chan_vector(conn, scope, view, q_emb, top_k, as_of, include_superseded)
        c_bm25 = _chan_bm25(conn, scope, view, query, top_k, as_of, include_superseded)
        c_graph = _chan_graph(conn, scope, view, q_emb, cfg.retrieval.graph_max_hops, top_k, as_of, include_superseded)
        c_ent = _chan_entity_name(conn, scope, view, query, top_k, as_of, include_superseded)
        c_syn = _expand_synonyms(conn, scope, query, as_of, include_superseded)
        c_tmp = _chan_temporal_decay(conn, scope, view, top_k, as_of=as_of, include_superseded=include_superseded)
        
        scores = _rrf([c_vec, c_bm25, c_graph, c_ent, c_syn, c_tmp], cfg.retrieval.rrf_k)
        ranked = sorted(scores, key=lambda fid: scores[fid], reverse=True)[:top_k]
    
    # Phase 2: Rerank
    reranked_rows = _rerank(query, ordered_rows)
    
    # Phase 3: Pack 装配
    pack = _assemble_pack(conn, scope, view, query, reranked_rows, t, ch_counts)
    return pack
```

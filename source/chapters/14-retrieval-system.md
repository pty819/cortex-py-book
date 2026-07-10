# 第14章 检索系统概述

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

Phase 0 的 Embedding + HyDE + Multihop 采用**两波并行**——第一波 `parallel_call` 同时跑 query embed + N×HyDE LLM + multihop LLM，第二波 `parallel_map` 对 HyDE 文本并行 embed。全部在 DB session 之外执行（纯 HTTP I/O）。

```{mermaid}
flowchart TB
    Q[用户查询] --> W1[第一波 parallel_call 会话外]
    W1 --> EMB[embed_one query]
    W1 --> H1[llm_chat HyDE x N]
    W1 --> M1[llm_chat Multihop]
    H1 --> W2[第二波 parallel_map 会话外]
    W2 --> HE[embed_one 每段 HyDE]
    EMB --> P1[Phase 1: 6通道 session_scope]
    HE --> P1
    M1 --> P1

    subgraph Phase 1
        V[Vector]
        B[BM25]
        G[Graph]
        N[Entity Name]
        S[Synonym]
        T[Temporal]
    end

    P1 --> RRF[RRF Fusion 会话内]
    RRF --> SIG[信号总线加权 会话内]
    SIG --> P2[Phase 2: Rerank 会话外]
    P2 --> P3[Phase 3: _assemble_pack 独立短事务 x3 重试]
    P3 --> CB[context_block LLM 会话外]
    CB --> CACHE[access_count 递增 + Cache 独立短事务]
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

HyDE 的 N 次 LLM 调用与 query embed、multihop LLM 一起由 Phase 0 的第一波 `parallel_call` 并行发起；随后第二波 `parallel_map` 对清洗后的 HyDE 文本并行 embed：

```python
from cortex.infra.concurrency import parallel_call, parallel_map

# 第一波:embed query + N×HyDE LLM + multihop LLM 同时跑(会话外)
first_wave = [
    (services.embed_one, (query, "query"), {}),
]
if adv.hyde_enabled and services.llm_configured("synthesis"):
    first_wave += [
        (services.llm_chat, ("synthesis", HYDE_SYSTEM, query), {})
        for _ in range(adv.hyde_passages)
    ]
if adv.multihop_enabled and services.llm_configured("synthesis"):
    first_wave.append((services.llm_chat, ("synthesis", MULTIHOP_SYSTEM,
        json.dumps({"query": query, "n": adv.multihop_count})), {}))
first_results = parallel_call(*first_wave)  # 一次性并发

q_emb = first_results[0]
hyde_raws = first_results[1 : 1 + adv.hyde_passages]
multihop_raw = first_results[-1] if adv.multihop_enabled else None

# 第二波:HyDE 文本并行 embed(会话外)
hyde_texts = [services.strip_think(r) for r in hyde_raws if r]
hyde_embs = parallel_map(lambda txt: services.embed_one(txt, role="query"), hyde_texts)
extra_embs = [e for e in hyde_embs if e]
```

两波之间有数据依赖（第二波需要第一波产出的 HyDE 文本），但第一波内部的三类调用、第二波内部的 N 个 embed 各自完全独立，所以分别用 `parallel_call`（异构函数并行）和 `parallel_map`（同构保序）并发。

## Multihop 子问题分解

Multihop 的 LLM 调用已并入 Phase 0 第一波 `parallel_call`（见上节），与 HyDE 同时发起。解析出的子查询在 Phase 1 的 BM25 通道内追加检索：

```python
# Phase 0 第一波已并发拿到 multihop_raw(见 HyDE 节)
if multihop_raw:
    subs = services.parse_llm_json(multihop_raw)
    # Phase 1 session 内:子查询追加到 BM25 候选
    for sq in (subs.get("queries") or [])[:adv.multihop_count]:
        c_bm25 = list(dict.fromkeys(
            c_bm25 + _chan_bm25(conn, scope, view, sq, top_k, ...)))
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

`recall()` 的 session 边界经过精心设计：Phase 0 在 session 外（纯 HTTP）；Phase 1 用一个 session 跑 6 通道 + RRF + 信号总线加权；rerank 在 session 外；`_assemble_pack` 在独立短事务里（带 3 次重试）；`context_block` 的 LLM 调用在 session 外。

```python
def recall(*, scope, query=None, view="local", top_k=None, as_of=None, ...):
    cfg = load_config()

    # Phase 0: 两波并行(会话外,纯 HTTP)
    #   第一波 parallel_call: embed query + N×HyDE + multihop
    #   第二波 parallel_map: HyDE 文本并行 embed
    # → 产出 q_emb + extra_embs + multihop 子查询

    # Phase 1: 6通道 + RRF + 信号总线加权(单个 session_scope)
    with session_scope() as conn:
        c_vec = _chan_vector(conn, ...)
        c_bm25 = _chan_bm25(conn, ...)
        c_graph = _chan_graph(conn, ...)
        c_ent = _chan_entity_name(conn, ...)
        c_syn = _expand_synonyms(conn, ...)
        c_tmp = _chan_temporal_decay(conn, ...)
        scores = _rrf([...], cfg.retrieval.rrf_k)
        # 信号总线加权(见下文专节)
        ranked = sorted(scores, key=lambda fid: scores[fid], reverse=True)[:top_k]
    # ← session 关闭——下面 rerank 不持有 DB 连接

    # Phase 2: Rerank(会话外,纯 HTTP)
    reranked_rows = _rerank(query, ordered_rows)

    # Phase 3: Pack 装配(独立短事务,3 次重试)
    pack = None
    for _attempt in range(3):
        try:
            with session_scope() as conn:
                pack = _assemble_pack(conn, scope, view, query, reranked_rows, ...)
            break
        except Exception:
            if _attempt < 2:
                time.sleep(0.3)
    # context_block 的 LLM 调用在 session 外(不再占住连接等 HTTP 返回)
    pack["context_block"] = _context_block(query, pack["layers"]["facts"], ...)
    return pack
```

Rerank 与 Pack 装配均移出主 `session_scope`，避免 LLM/rerank HTTP 调用占用 DB 连接。`_assemble_pack` 在独立短事务里执行，失败重试 3 次；三次全失败则兜底构造最小 pack。

## 信号总线加权 (salience + access_count)

RRF 融合产出 `scores` 之后、rerank 之前，pipeline 还插入了一步**信号总线再加权**——把记忆的"重要性信号"叠加到 RRF 分数上。这一步只在 `adv.salience_weight > 0` 时触发：

```python
# retrieval/pipeline.py — Phase 1 session 内,RRF 之后
if adv.salience_weight > 0 and scores:
    # H4:批量查询(单条 SQL 取全部候选的 ac+sal),消除 N+1
    all_fids = list(scores.keys())
    sig_rows = conn.execute(text("""
        SELECT f.fact_id::text, coalesce(max(e.access_count),0) AS ac,
               coalesce(f.salience,1.0) AS sal
        FROM facts f LEFT JOIN events e ON e.event_id = ANY(f.supports)
        WHERE f.fact_id = ANY(CAST(:ids AS uuid[]))
        GROUP BY f.fact_id
    """), {"ids": "{" + ",".join(all_fids) + "}"}).fetchall()
    sig = {r[0]: ((r[1] or 0), (r[2] or 1.0)) for r in sig_rows}
    for fid in all_fids:
        ac, sal = sig.get(fid, (0, 1.0))
        scores[fid] = scores[fid] * sal + adv.salience_weight * (ac / 10.0)
```

加权公式：

```
scores[fid] = scores[fid] * sal + adv.salience_weight * (ac / 10.0)
```

两个信号因子：

| 因子 | 来源 | 含义 |
|------|------|------|
| `sal` | `facts.salience`（默认 1.0） | Feedback 双向调整：正向反馈提升 salience（`salience += positive_weight`，上限 `salience_ceiling`），负向反馈降低 salience（如降到 0.7）。`sal < 1.0` 时压低分数,`sal > 1.0` 时放大分数 |
| `ac` | `max(events.access_count)` of supporting events | 隐式正反馈：该 fact 被召回的累计次数。除以 10.0 归一化后,乘以 `salience_weight` 作为加分项 |

**排序效果**：

- **高 salience 的 fact**（被正向反馈强化,或天生重要）——RRF 分数被 `sal` 放大,排名更靠前
- **低 salience 的 fact**（被负向反馈降权,如错误结论/过时推断）——RRF 分数被 `sal < 1.0` 压缩,排名下沉
- **频繁被召回的 fact**（高 `access_count`）——额外加 `salience_weight * ac/10` 的分,热门记忆自然浮现

这一步把第10章的"信号总线"(`salience` + `access_count`)与第11章的"反馈循环"(Feedback 改写 salience)接入了检索排序——记忆不再是静态召回,而是"被用得越多越强,被否定得越多越弱"的动态权重。详见 **第10章 信号总线** 和 **第11章 反馈系统**。

### recall → access_count 隐式反馈环

Pack 装配成功后,pipeline 还做了一步**隐式正反馈**——对本次命中的 fact 的 supporting events 批量递增 `access_count`：

```python
# retrieval/pipeline.py — recall() 返回路径,Pack 装配成功后
if pack and pack.get("layers", {}).get("facts"):
    _hit_ids = [f["fact_id"] for f in pack["layers"]["facts"] if f.get("fact_id")]
    if _hit_ids:
        with session_scope() as conn:
            conn.execute(text("""
                UPDATE events SET access_count = access_count + 1, last_recalled_at = now()
                WHERE event_id = ANY(SELECT unnest(supports) FROM facts
                                     WHERE fact_id = ANY(CAST(:ids AS uuid[])))
            """), {"ids": "{" + ",".join(_hit_ids) + "}"})
```

注意这步**写在 `recall()` 的返回路径上**——也就是说 **recall 不再是一个纯读操作**：每次成功召回都会写回 `events.access_count` 和 `events.last_recalled_at`,影响下一次 temporal-decay 通道和信号总线加权的分数。被频繁召回的记忆因此进入"越用越好召回"的正循环;长期未被召回的 event 则由 methylation 机制标记 `excluded_from_recall`(详见第10章)。

异常容错:这步递增用 `try/except` 包裹,**信号采集失败不阻塞召回**——读路径的可靠性优先于反馈环的完整性。

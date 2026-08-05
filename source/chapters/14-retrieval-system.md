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
    
    默认(无 as_of): valid_from<=now() AND valid_to IS NULL AND recorded_to IS NULL (当前 live facts)
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
        return "valid_from <= now() AND (valid_to IS NULL OR now() < valid_to) AND recorded_to IS NULL"
    return "valid_from <= now() AND (valid_to IS NULL OR now() < valid_to)"
```

## 图准入 SQL

```python
def _graph_eligible_sql(alias="f"):
    """生成图遍历的 eligibility 条件"""
    causal = ",".join(f"'{p}'" for p in sorted(CAUSAL_PREDICATES))
    excluded = ",".join(f"'{p}'" for p in sorted(GRAPH_EXCLUDED_PREDICATES))
    return (f"{alias}.knowledge_tier='verified' AND {alias}.polarity='positive' "
            f"AND {alias}.predicate NOT IN ({excluded}) "
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

    P1 --> FUSE[Fuse 会话内<br/>rrf/weighted_rrf/priority]
    FUSE --> SIG[信号总线加权 会话内<br/>salience/usage/usefulness/exploration]
    SIG --> P2[Phase 2: Rerank 会话外]
    P2 --> P3[Phase 3: _assemble_pack 独立短事务 x3 重试]
    P3 --> CB[context_block LLM 会话外]
    CB --> CACHE[retrieval_count 递增 + Cache 独立短事务]
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
        scores = _fuse([...], tuning)          # 三策略:rrf/weighted_rrf/priority
        # 信号总线加权(见下文专节)
        ranked = sorted(scores, key=lambda fid: scores[fid], reverse=True)[:top_k]
    # ← session 关闭——下面 rerank 不持有 DB 连接

    # Phase 2: Rerank(会话外,纯 HTTP)
    #   rerank_cfg.enabled=false 或服务未就绪 → 跳过;服务调用抛异常 → 离线兜底退回融合顺序
    reranked_rows = services.rerank(query, docs, cfg=rerank_cfg)  # 失败时 pipeline 兜底为 ordered_rows

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

## 信号总线加权 (四信号独立模型)

融合产出 `scores` 之后、rerank 之前，pipeline 插入一步**信号总线再加权**——把记忆的四个重要性信号叠加到融合分数上。与旧版"单一 salience 公式"不同,这四个信号现在**相互独立、各自有开关**,可单独启停(`AdvancedRetrievalCfg`):

```python
class AdvancedRetrievalCfg(BaseModel):
    salience_enabled: bool = False      # 乘数混合 salience
    usage_enabled: bool = True          # 被动召回次数(饱和)
    usefulness_enabled: bool = True     # 显式反馈累积值
    exploration_enabled: bool = True    # 为新 fact 保留候选位
    # 各自的权重 / 参数
    salience_weight: float = 0.0
    usage_weight: float = 0.02
    usage_saturation: float = 5.0
    usefulness_weight: float = 0.05
    exploration_ratio: float = 0.10
```

### 批量取信号(消除 N+1)

```python
# retrieval/pipeline.py — Phase 1 session 内,融合之后
if scores:
    all_fids = list(scores.keys())
    sig_rows = conn.execute(text("""
        SELECT f.fact_id::text, coalesce(f.retrieval_count,0) AS retrievals,
               coalesce(f.salience,1.0) AS sal,
               coalesce(f.retrieval_usefulness,0.0) AS usefulness
        FROM facts f
        WHERE f.fact_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": "{" + ",".join(all_fids) + "}"}).fetchall()
    sig = {r[0]: (int(r[1] or 0), float(r[2] or 1.0), float(r[3] or 0.0))
           for r in sig_rows}
```

注意信号来源已从 `events.access_count`(需 JOIN events)改为 `facts` 表上的冗余列 `retrieval_count` / `retrieval_usefulness` —— 单表查询,无 JOIN,更快。

### 四信号加权公式

```python
for fid in all_fids:
    retrievals, sal, usefulness = sig.get(fid, (0, 1.0, 0.0))
    saturation = max(float(adv.usage_saturation), 0.001)
    # (1) Usage:饱和加法,防止高频 fact 无限加分
    usage_bonus = (float(adv.usage_weight) * (1.0 - math.exp(-retrievals / saturation))
                   if adv.usage_enabled else 0.0)
    # (2) Usefulness:显式反馈累积,线性加法
    usefulness_bonus = (float(adv.usefulness_weight) * usefulness
                         if adv.usefulness_enabled else 0.0)
    # (3) Salience:乘数混合(默认关),salience_enabled 才生效
    salience_multiplier = 1.0
    if adv.salience_enabled:
        weight = min(max(float(adv.salience_weight), 0.0), 1.0)
        salience_multiplier = (1.0 - weight) + weight * sal
    scores[fid] = scores[fid] * salience_multiplier + usage_bonus + usefulness_bonus
```

汇总:

```
scores[fid] = scores[fid] * salience_multiplier + usage_bonus + usefulness_bonus
```

| 信号 | 来源 | 加权方式 | 开关 | 默认权重 |
|------|------|----------|------|----------|
| **Salience** | `facts.salience`(默认 1.0,Feedback 调整) | 乘数混合:`(1-w) + w·sal` | `salience_enabled`(**默认关**) | `salience_weight=0.0` |
| **Usage** | `facts.retrieval_count`(被动召回次数) | 饱和加法:`w·(1-e^(-n/saturation))` | `usage_enabled`(**默认开**) | `usage_weight=0.02`,`saturation=5.0` |
| **Usefulness** | `facts.retrieval_usefulness`(显式 relevant/irrelevant 反馈累积) | 线性加法:`w·usefulness` | `usefulness_enabled`(**默认开**) | `usefulness_weight=0.05` |

### Usage 的饱和设计

被动召回次数(retrieval_count)若线性加分,高频 fact 会无限累积优势、霸占结果。Usage 信号用**指数饱和**封顶:

```
usage_bonus = usage_weight · (1 - e^(-retrievals / saturation))
```

`retrievals → ∞` 时 bonus 趋近 `usage_weight`(默认 0.02)——无论被召回多少次,Usage 最多只能贡献一个有界的固定加分。`saturation`(默认 5.0)控制达到饱和的速度:约被召回 5 次后接近上限。这让"热门"记忆获得适度加成,但不会淹没从未被召回但高度相关的新 fact。

### Salience 乘数混合(默认关)

Salience 信号默认**关闭**(`salience_enabled=false`)。开启后用乘数混合而非直接相乘,避免极端 salience 值主导分数:

```
salience_multiplier = (1 - weight) + weight · sal
```

`weight = salience_weight`(裁剪到 [0,1])。`weight=0` 时乘数恒为 1(等于关闭);`weight=1` 时乘数就是 `sal` 本身。这让 salience 的影响是**可控渐变**的,而非全有或全无。

### Exploration 探索槽(exploit/exploit 分配)

第四个信号 `exploration` 不改分数,而是改**最终选哪些 fact**。融合 + 三信号加权排好序后,pipeline 按 `exploration_ratio`(默认 0.10)把 top_k 个名额拆成 exploit/exploit 两段:

```python
ranked_all = sorted(scores, key=lambda fid: scores[fid], reverse=True)
explore_slots = (min(top_k, max(0, math.ceil(top_k * float(adv.exploration_ratio))))
                 if adv.exploration_enabled else 0)
exploit_slots = max(0, top_k - explore_slots)
exploit = ranked_all[:exploit_slots]                          # 高分热门 fact
explore_pool = sorted(                                        # 从未被召回(retrieval_count==0)的新 fact
    (fid for fid in ranked_all if fid not in exploit
     and sig.get(fid, (0,))[0] == 0),
    key=lambda fid: base_scores.get(fid, 0.0), reverse=True,
)
ranked = exploit + explore_pool[:explore_slots]               # 拼 final top_k
if len(ranked) < top_k:
    ranked.extend(fid for fid in ranked_all if fid not in ranked)  # 名额不足时补齐
ranked = ranked[:top_k]
```

效果:默认 10% 的名额预留给"从未被召回过的新 assertion",保证新鲜记忆有曝光机会,而不是被热门 fact 永远压在下面。`exploration_enabled=false` 时退化为纯 exploit(全按分数排序)。

**排序效果总览**:

- **高 salience 的 fact**(开启时)——分数被乘数放大,排名更靠前
- **频繁被召回的 fact**(Usage)——获得有界饱和加分(封顶 0.02),适度上浮
- **获正向反馈的 fact**(Usefulness)——usefulness 累积值线性加分
- **全新的 fact**(Exploration)——保留 10% 候选位,不被热门压制

这一步把第10章的"信号总线"与第11章的"反馈循环"(Feedback 改写 salience/usefulness)接入了检索排序——记忆不再是静态召回,而是"被用得越多越强、被否定得越多越弱、全新的也有曝光"的动态权重。详见 **第10章 信号总线** 和 **第11章 反馈系统**。

### recall → retrieval_count 隐式反馈环

Pack 装配成功后,pipeline 还做了一步**隐式正反馈**——对本次命中的 fact 批量递增 `retrieval_count`(并刷新 `last_recalled_at`):

注意这步**写在 `recall()` 的返回路径上**——也就是说 **recall 不再是一个纯读操作**(当 `track_usage=True` 时):每次成功召回都会写回 `facts.retrieval_count`,影响下一次 Usage 信号加权的分数。被频繁召回的记忆因此进入"越用越容易召回"的饱和正循环。

```{warning}
A/B Preview(`POST /v1/admin/retrieval/preview`)和带 `track_usage=False` 的 recall **不递增** retrieval_count、不写 recall_packs 缓存——这是无副作用调参的前提。详见第16章「命名 Profile 与 A/B Preview」与第18章。
```

异常容错:反馈环写入用 `try/except` 包裹,**信号采集失败不阻塞召回**——读路径的可靠性优先于反馈环的完整性。

# 第16章 RRF 融合

## 概述

RRF（Reciprocal Rank Fusion）将多个通道的召回结果合并为一个有序列表。

## RRF 公式

```
RRF score = Σ(1 / (k + rank(d, c)))
```

其中：
- `rank(d, c)` = 文档 d 在通道 c 中的排名
- `k` = 融合常数（默认 60）
- 所有通道结果等权融合

## 实现

```python
def rrf_merge(channel_results: List[List[str]], k: int = 60) -> List[str]:
    """RRF 融合多个通道的 fact_id 列表"""
    scores = {}
    for fact_ids in channel_results:
        for rank, fid in enumerate(fact_ids):
            scores[fid] = scores.get(fid, 0) + 1.0 / (k + rank + 1)
    
    # 按 score 降序排列
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [fid for fid, score in ranked]
```

```{mermaid}
flowchart LR
    subgraph 通道结果
        V[向量: A B C D]
        B[BM25: B E F G]
        G[图: A C H I]
        E[Entity: D F J]
    end
    
    subgraph RRF
        R1[A: 1/61 + 1/63 = 0.032]
        R2[B: 1/62 + 1/61 = 0.033]
        R3[C: 1/63 + 1/63 = 0.032]
        R4[D: 1/64 + 1/64 = 0.031]
        R5[E: 1/62 = 0.016]
        R6[F: 1/62 + 1/62 = 0.032]
    end
    
    V --> R1
    V --> R2
    V --> R3
    B --> R2
    B --> R5
    G --> R1
    G --> R3
    
    R1 --> SORT[排序: B A C F D E ...]
    R2 --> SORT
    R3 --> SORT
```

## Rerank 后处理

RRF 融合后，top-N（默认 40）走 Prism rerank：

```python
def rerank(query, documents):
    """Prism rerank：语义重排序"""
    cfg = load_config().rerank
    url = cfg.api_base.rstrip("/") + "/rerank"
    with httpx.Client(timeout=cfg.timeout) as cli:
        r = cli.post(url, json={
            "model": cfg.model,
            "query": query,
            "documents": documents
        }, headers={"Authorization": f"Bearer {cfg.api_key}"})
        r.raise_for_status()
        return r.json()["data"]  # [{index, relevance_score, document}]
```

rerank 后按 `relevance_score` 降序排列，取 top_k（默认 20）。

## StratifiedPack 组装

最终结果组装为 StratifiedPack：

```python
class StratifiedPack(BaseModel):
    pack_id: str
    scope: str
    view: str
    layers: Layers            # events + facts + beliefs
    context_block: str        # LLM 合成的综述文本
    provenance: ProvenanceTrail  # 证据链
    diagnostics: Diagnostics  # 时间 + 通道统计
```

**pack_id** 是 `"pack_" + uuid4().hex[:24]`（随机 UUID），**query_hash** 是 `sha256(scope + layers_json)[:16]`，用于缓存命中：

```sql
CREATE TABLE recall_packs (
    pack_id    TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    pack_json  JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
```

缓存 60 秒内有效，相同查询直接返回。

> **缓存失效与权重一致性**：Feedback(第11章)改写 `facts.salience` 或关闭 fact、Dreaming(见相关章节)改动 salience 后,会主动失效相关 scope 的 recall_packs 缓存——`DELETE FROM recall_packs WHERE scope=:s`,确保下次召回重新走 RRF + salience 再加权,反映最新权重。否则 60 秒 TTL 内的缓存命中会返回旧的排序结果。

## 完整检索流程

```{mermaid}
sequenceDiagram
    participant C as 调用方
    participant R as recall()
    participant DB as PostgreSQL
    participant LLM as LLM Service
    
    C->>R: recall(scope, query)
    
    rect rgb(240,248,255)
        Note over R,DB: 6 通道串行查询(同一 session_scope 内)
        R->>DB: _chan_vector (pgvector HNSW)
        R->>DB: _chan_bm25 (tsvector)
        R->>DB: _chan_graph (递归 CTE)
        R->>DB: _chan_entity_name (pg_trgm)
        R->>DB: _chan_synonym (synonyms 表)
        R->>DB: _chan_temporal_decay
    end
    
    Note over R: RRF 融合 (k=60)

    R->>DB: salience 再加权 (scores * sal + w * ac/10)
    Note over R: 信号总线加权:见第14章/第10章

    R->>LLM: Prism rerank (top-40)
    LLM-->>R: reranked top-20
    
    R->>LLM: Synthesis context block
    LLM-->>R: context_block
    
    Note over R: 组装 StratifiedPack
    
    R-->>C: StratifiedPack
```

## salience 再加权(信号总线)

上图流程中,RRF 融合与 Prism rerank 之间还有一步**salience 再加权**,因它与 RRF 的衔接关系而放在本章说明。RRF 输出 `scores` 后,pipeline 按下式改写每个候选 fact 的分数:

```
scores[fid] = scores[fid] * sal + adv.salience_weight * (ac / 10.0)
```

其中 `sal = facts.salience`(默认 1.0,由 Feedback 软降权),`ac = max(events.access_count)`(被召回次数)。效果是:RRF 排名并非最终排名——高 salience / 高 access_count 的 fact 在送入 rerank 之前就被向上抬,低 salience 的 fact 被向下压。完整的加权语义、反馈环与 `access_count` 递增细节见 **第14章「信号总线加权」** 与 **第10章 信号总线**。

## 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `rrf_k` | 60 | RRF 融合常数 |
| `rerank_top_n` | 40 | 送入 rerank 的文档数 |
| `top_k` | 20 | 最终返回的文档数 |
| `pack_ttl` | 60s | 缓存有效期 |

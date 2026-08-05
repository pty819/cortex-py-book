# 第16章 RRF 融合

## 概述

融合层(`_fuse`)将 6 个通道的召回结果合并为一个有序列表。cortex-py 支持三种可切换的融合策略,由 `retrieval.fusion_strategy` 控制。

## 三种融合策略

```python
def _fuse(rank_lists: List[List[str]], tuning: RetrievalTuningCfg) -> Dict[str, float]:
    if tuning.fusion_strategy == "priority":
        # 按通道优先级(声明顺序)直接拼接,先出现的 fact 排名更高
        ordered = list(dict.fromkeys(fid for items in rank_lists for fid in items))
        total = max(len(ordered), 1)
        return {fid: (total - index) / total for index, fid in enumerate(ordered)}
    weights = None
    if tuning.fusion_strategy == "weighted_rrf":
        # 每通道独立 weight,从 channels.<name>.weight 取
        weights = [float(getattr(tuning.channels, name).weight) for name in CHANNEL_NAMES]
    return _rrf(rank_lists, tuning.rrf_k, weights)
```

| 策略 | 公式 | 适用 |
|------|------|------|
| **`rrf`** | `Σ 1/(k+rank)`(等权) | 各通道质量相近,经典稳健 |
| **`weighted_rrf`**(默认) | `Σ w_c · 1/(k+rank)`,权重来自 `channels.<name>.weight` | 想压低/放大某通道(如 graph 默认 weight=0.20) |
| **`priority`** | 按通道声明顺序拼接,先出现者优先 | 需要确定性的通道优先级(如 vector 先于 graph) |

### weighted_rrf 的 RRF 基础公式

```
weighted RRF score(d) = Σ_c  w_c / (k + rank(d, c))
```

其中:
- `rank(d, c)` = 文档 d 在通道 c 中的排名
- `k` = 融合常数(默认 60,`retrieval.rrf_k`)
- `w_c` = 通道 c 的权重(默认全 1.0,graph 通道默认 0.20)

等权 RRF 是 weighted RRF 在所有 `w_c = 1` 时的特例。

## 每通道独立控制

每个通道(`RetrievalChannelCfg`)有三项独立配置,使六通道成为真正可独立调音的输入:

| 字段 | 默认 | 作用 |
|------|------|------|
| `enabled` | `true` | 关闭则该通道完全不参与召回与融合 |
| `top_k` | `None`(回退全局 `retrieval.top_k`) | 限制该通道返回的候选数上限 |
| `weight` | `1.0`(graph 通道 `0.20`) | `weighted_rrf` 策略下该通道的融合权重 |

```python
class RetrievalChannelCfg(BaseModel):
    enabled: bool = True
    top_k: Optional[int] = Field(default=None, ge=1, le=1000)
    weight: float = Field(default=1.0, ge=0.0, le=10.0)

class RetrievalChannelsCfg(BaseModel):
    vector: RetrievalChannelCfg = Field(default_factory=RetrievalChannelCfg)
    bm25: RetrievalChannelCfg = Field(default_factory=RetrievalChannelCfg)
    graph: RetrievalChannelCfg = Field(default_factory=lambda: RetrievalChannelCfg(weight=0.20))
    entity_name: RetrievalChannelCfg = Field(default_factory=RetrievalChannelCfg)
    synonym: RetrievalChannelCfg = Field(default_factory=RetrievalChannelCfg)
    temporal: TemporalChannelCfg = Field(default_factory=TemporalChannelCfg)  # 多 decay_days
```

```{note}
`graph_weight`(旧顶层字段)被保留为兼容别名,`model_validator` 会把它同步到 `channels.graph.weight`。两个方向都生效:改 `channels.graph.weight` 会回写 `graph_weight`,反之亦然。新代码应直接用 `channels.graph.weight`。
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

RRF 融合后,取 `rerank.top_n`(默认 25)个候选走 Prism rerank。rerank 现在有正式的运行时开关与预算控制(`RerankRuntimeCfg`):

```python
class RerankRuntimeCfg(BaseModel):
    enabled: bool = True       # 关闭则跳过 rerank,直接用融合分数
    threshold: float = 0.1     # 低于此 relevance_score 的候选丢弃
    top_n: int = 25            # 送入 rerank 的候选池大小
    timeout: int = 60          # 超时秒数
```

`rerank.enabled=false` 时 pipeline 直接用融合 + 信号加权后的分数排序,不经 HTTP rerank 调用。这对无 rerank 服务的部署是硬性开关。每个命名 Profile 还可以有自己的 `rerank` 覆盖(见下文)。

rerank 调用本身（`services.rerank`，走 `_cached_http_client`，`top_n` 由 `RerankCfg` 控制）：

```python
def rerank(query, documents, cfg=None):
    """调 prism rerank → 返回 [{"index","relevance_score","document"}, ...] 按 score 降序。"""
    cfg = cfg or load_config().rerank
    url = cfg.api_base.rstrip("/") + "/rerank"
    cli = _cached_http_client("rerank", api_base=cfg.api_base,
                              api_key=cfg.api_key, timeout=cfg.timeout)
    body = {"model": cfg.model, "query": query,
            "documents": documents, "top_n": cfg.top_n}
    if cfg.extra_body:
        body.update(cfg.extra_body)          # extra_body 透传合并进请求体
    r = cli.post(url, json=body,
                 headers={"Authorization": f"Bearer {cfg.api_key}"})
    r.raise_for_status()
    out = r.json()
    results = out.get("results") or out.get("data") or []   # 兼容 results/data 两种返回
    results.sort(key=lambda d: d.get("relevance_score", 0), reverse=True)
    return results
```

pipeline 侧 rerank 的**运行时决策**（`recall` Phase 2，会话外）：
- `rerank_cfg.enabled=false` → 跳过 HTTP 调用，直接用融合 + 信号加权后的顺序；
- `rerank` 服务未就绪（缺 `api_base`/`model`/`api_key`）→ 同样跳过，`status="dependency_not_ready"`；
- 调用成功 → 按 `relevance_score` 保留下限 `threshold` 之上的候选（`keep_idx` 为空时兜底取前 10），`status="completed"`；
- 调用抛异常 → **离线兜底**：退回融合顺序，`status="failed_open"`，不阻断召回。

即 rerank 层是**尽力而为**的：除了 `enabled=false` 是硬开关，其余情况下 rerank 服务的缺失或失败都不会让召回挂掉，而是退回融合排序。

rerank 后按 `relevance_score` 降序排列，低于 `threshold` 的丢弃，取最终 top_k。

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
        R->>DB: _expand_synonyms (synonyms 表)
        R->>DB: _chan_temporal_decay
    end
    
    Note over R: 融合 (rrf/weighted_rrf/priority)

    R->>DB: 信号总线再加权 (4 信号独立)
    Note over R: salience·usage·usefulness·exploration,见第14章

    R->>LLM: Prism rerank (top_n=25, 会话外/尽力而为)
    LLM-->>R: reranked top-20 (缺失/失败则退回融合顺序)
    
    R->>LLM: Synthesis context block
    LLM-->>R: context_block
    
    Note over R: 组装 StratifiedPack
    
    R-->>C: StratifiedPack
```

## 信号总线再加权(融合后、rerank 前)

上图流程中,融合与 Prism rerank 之间还有一步**信号总线再加权**——把记忆的四个重要性信号叠加到融合分数上。四个信号各自有独立开关,可单独启停:

| 信号 | 来源 | 加权方式 | 开关(默认) |
|------|------|----------|------------|
| **Salience** | `facts.salience`(默认 1.0,Feedback 调整) | 乘数混合:`score · ((1-w) + w·sal)` | `salience_enabled`(默认 **关**) |
| **Usage** | `facts.retrieval_count`(被动召回次数) | 饱和加法:`usage_weight·(1-e^(-n/saturation))` | `usage_enabled`(默认 **开**) |
| **Usefulness** | `facts.retrieval_usefulness`(显式反馈累积) | 线性加法:`usefulness_weight·usefulness` | `usefulness_enabled`(默认 **开**) |
| **Exploration** | `retrieval_count == 0` 的新 fact | 为新 fact 保留候选位(explore/exploit 分配) | `exploration_enabled`(默认 **开**) |

完整加权公式与 explore/exploit 机制见 **第14章「信号总线加权」**。这里只点明:融合分数 `scores[fid]` 并非最终排名——开启的信号会在送入 rerank 前改写它,使"被用得越多越强、被否定得越多越弱、全新的 fact 也有曝光机会"。

## 命名 Profile 与 A/B Preview

检索配置支持**命名 Profile**(`retrieval.profiles`):每个 Profile 是一份完整的 `RetrievalTuningCfg`(可含自己的 `rerank` 覆盖),`active_profile` 指定当前生效的那份。recall 时也可临时指定 `profile` 参数,不切换全局激活态。

`POST /v1/admin/retrieval/preview` 提供**无副作用的 Active-vs-Draft A/B 预览**:对同一 query 跑多个配置变体,比较它们的 fact 排名与每通道候选数/耗时,但 `track_usage=False`(不递增 retrieval_count、不写 recall_packs 缓存)。这让调参可在不影响线上计数与缓存的前提下进行。详见第18章。

## 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `fusion_strategy` | `weighted_rrf` | 融合策略(rrf / weighted_rrf / priority) |
| `rrf_k` | 60 | RRF 融合常数 |
| `channels.<name>.enabled` | `true` | 通道开关 |
| `channels.<name>.weight` | `1.0`(graph `0.20`) | weighted_rrf 下的通道权重 |
| `top_k` | 40 | 全局每通道候选上限(通道未设 top_k 时回退) |
| `rerank.enabled` | `true` | rerank 开关 |
| `rerank.threshold` / `.top_n` / `.timeout` | `0.1` / `25` / `60` | rerank 阈值/候选池/超时 |
| `salience_enabled` / `usage_enabled` / `usefulness_enabled` / `exploration_enabled` | 关/开/开/开 | 四信号开关 |
| `pack_ttl` | 60s | 缓存有效期 |

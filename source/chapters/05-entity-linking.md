# 第5章 实体链接详解

## B-over-C 三层策略

实体链接是**图谱质量的命门**。Cortex-PY 采用三层策略：

```{mermaid}
graph TB
    subgraph "A 层: 别名精确匹配"
        A1[查 entity_aliases]
        A2[匹配 identity_context]
        A3[命中 → 直接返回]
    end
    
    subgraph "B 层: 向量近邻"
        B1[pgvector 近邻查询]
        B2[context_key 过滤]
        B3{身份敏感兼容性}
        B4{相似度判断}
        B5[高: 直接合并]
        B6[中: LLM 判定]
        B7[低: 创建新实体]
    end
    
    subgraph "C 层: 创建新实体"
        C1[生成 UUID]
        C2[计算 embedding]
        C3[存入 entities + aliases]
    end
    
    A1 --> A2
    A2 -->|context_key 匹配| A3
    A2 -->|不匹配| B1
    A1 -->|未命中| B1
    B1 --> B2
    B2 --> B3
    B3 -->|兼容| B4
    B3 -->|不兼容| C1
    B4 -->|> 0.85| B5
    B4 -->|0.30-0.85| B6
    B4 -->|< 0.30| C1
    B6 -->|是同一实体| B5
    B6 -->|不是| C1
```

## A 层: 别名匹配

### 精确匹配 + 身份上下文

为支持灰区 LLM 裁决的并行化，B-over-C 链接逻辑已拆分为查询阶段（`_resolve_lookup`，只读）和写入阶段（`_resolve_write`）。`extract_event` 主流程走三阶段并行路径（见本章末尾），`_resolve_or_create` 保留为单步兼容包装供 triple 直写路径使用。

`_resolve_lookup` 的 A 层逻辑——别名精确命中 + 身份上下文匹配——返回三态之一：

```python
# extraction/pipeline.py
def _resolve_lookup(conn, scope, name, etype, description, thresholds,
                    identity_context=None, precomputed_emb=None):
    """DB 查询阶段(只读)。返回:
      ("resolved", entity_id) / ("grey", candidates) / ("new", None)
    """
    canonical_ctx = _identity_context_for_type(identity_context, etype)
    ctx_key = json.dumps(canonical_ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # A 层: 别名精确命中
    exact = conn.execute(text("""
        SELECT DISTINCT e.entity_id, e.context_key FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
        WHERE e.scope=:s AND e.merged_into IS NULL
          AND (lower(e.canonical_name)=lower(:n) OR lower(a.alias)=lower(:n))
          AND ((CAST(:t AS text) IS NULL AND e.entity_type IS NULL) OR e.entity_type=:t)
        ORDER BY e.entity_id
    """), {"s": scope, "n": name, "t": etype}).fetchall()

    # 身份上下文匹配
    if canonical_ctx:
        matches = [r for r in exact if r.context_key == ctx_key]
        if len(matches) == 1:
            return "resolved", str(matches[0].entity_id)
        # 兼容旧数据:无上下文的单条记录可升级
        legacy = [r for r in exact if r.context_key == "{}"]
        if not matches and len(exact) == 1 and len(legacy) == 1:
            conn.execute(text("""UPDATE entities SET identity_context=CAST(:ctx AS jsonb),
                                 context_key=:ck, updated_at=now() WHERE entity_id=:e"""),
                         {"ctx": json.dumps(canonical_ctx), "ck": ctx_key, "e": legacy[0].entity_id})
            return "resolved", str(legacy[0].entity_id)
    elif len(exact) == 1:
        return "resolved", str(exact[0].entity_id)

    # 同名但上下文不同必须保守分离 → 不进向量层
    # ... 继续到 B 层向量召回
```

> `CAST(:t AS text) IS NULL` 是 psycopg3 的写法——psycopg3 对参数类型推断更严格，裸 `:t IS NULL` 会报 `AmbiguousParameter`。详见[第20章](20-worker-system)。

## B 层: 向量近邻

### 身份上下文过滤

向量查询时强制 `context_key` 过滤：

```python
# _resolve_lookup 的 B 层(仍在同一函数内,紧接 A 层之后)
cands = conn.execute(text("""
    SELECT entity_id, canonical_name, description, entity_type, context_key,
           1-(embedding <=> CAST(:q AS vector)) AS cos
    FROM entities WHERE scope=:s AND merged_into IS NULL AND embedding IS NOT NULL
      AND ((CAST(:t AS text) IS NULL AND entity_type IS NULL) OR entity_type=:t)
      AND context_key=:ck
    ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
"""), {"q": str(emb), "s": scope, "t": etype, "ck": ctx_key}).fetchall()
```

### 身份敏感匹配 (`_identity_candidate_compatible`)

```python
_IDENTITY_SENSITIVE_TYPES = {
    "component", "sensor", "controller", "process_param", "measurement",
    "metrology_result", "recipe", "recipe_revision",
}

def _critical_identity_tokens(value):
    """提取关键身份标识符（编号+量纲）"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    identifiers = re.findall(r"[a-z]+(?:[-_][a-z]+)*[-_]?\d+(?:\.\d+)?[a-z%°μ]*", normalized)
    quantities = re.findall(r"\d+(?:\.\d+)?\s*(?:kw|w|v|a|ma|pa|torr|mtorr|sccm|slm|°?c|nm|um|μm|%|hz|khz|mhz)", normalized)
    return tuple(sorted(set(identifiers + quantities)))

def _identity_candidate_compatible(name, candidate_name, entity_type):
    """身份敏感类型:关键标识符必须一致"""
    if _canonical_text(entity_type or "") not in _IDENTITY_SENSITIVE_TYPES:
        return True
    incoming = _critical_identity_tokens(name)
    existing = _critical_identity_tokens(candidate_name)
    return not (incoming or existing) or incoming == existing
```

### 阈值策略

| 余弦相似度 | 策略 | 说明 |
|------------|------|------|
| >= 0.85 | 直接合并 | 高置信度，省 LLM |
| 0.30 - 0.85 | LLM 灰区判定 | 传入候选实体 + 原文上下文 |
| < 0.30 | 创建新实体 | 低置信度，保守新建 |

## C 层: LLM 灰区判定

当相似度在灰区时，调用 LLM 判定：

```python
def _llm_entity_link(name, etype, description, candidates, context_text):
    """LLM 灰区判定:决定复用哪个候选还是新建"""
    from ..prompts import ENTITY_LINK_SYSTEM
    payload = json.dumps({
        "new_entity": {"name": name, "type": etype, "description": description},
        "candidates": candidates,
        "context": context_text[:2000],
    }, ensure_ascii=False)
    raw = services.llm_chat("extraction", ENTITY_LINK_SYSTEM, payload, max_tokens=1024)
    data = services.parse_llm_json(raw)
    return {"reuse": data.get("reuse", False),
            "entity_name": data.get("entity_name"),
            "reason": data.get("reason", "")}
```

### LLM 判定 Prompt

```python
ENTITY_LINK_SYSTEM = PROJECT_CONTEXT + """【本次任务:实体灰区判定 — 新实体是复用已有还是新建?】

## 判断准则
1. **看上下文**:原文上下文里这个实体出现在什么设备/子系统/工艺背景下?
2. **看类型**:类型不同(如一个是 sensor 一个是 component)→ 大概率不同实体
3. **看描述**:描述里提到的位置/编号/型号是否一致?
4. **保守原则**:如果不确定,倾向于新建(宁可重复也不错误合并)

## 输出格式
```json
{"reuse": true/false, "entity_name": "...", "reason": "..."}
```"""
```

## 三阶段并行:extract_event 的实体链接

上面描述的是单个实体的链接逻辑。在 `extract_event` 主流程中，N 个实体的链接如果逐个串行执行，灰区实体的 LLM 裁决会累积成 `N × 1-3s` 的串行等待。为此，实体链接被组织为**三阶段**，把 LLM 裁决从 session 内串行改为 session 外并行：

```{mermaid}
flowchart TD
    subgraph Phase1["Phase 1: lookup(会话内,只读短事务)"]
        L1[逐 entity 调 _resolve_lookup] --> L2{分类}
        L2 -->|resolved| L3[直接记下 entity_id]
        L2 -->|grey| L4[收集待裁决]
        L2 -->|new| L5[标记待新建]
    end

    subgraph Phase2["Phase 2: 灰区 LLM 并行(会话外)"]
        P1[parallel_map _decide_grey] --> P2[N 个灰区 entity<br/>同时调 _llm_entity_link]
        P2 --> P3[裁决:reuse 或新建]
    end

    subgraph Phase3["Phase 3: write(会话内,短事务)"]
        W1[_resolve_write 新建] --> W2[插入 facts]
        W2 --> W3[超替闭合]
        W3 --> W4[Belief 聚合]
    end

    L4 --> P1
    P3 --> W1
    L3 --> W2
    L5 --> W1
```

```python
# extraction/pipeline.py — extract_event Step 3c(简化)
from cortex.infra.concurrency import parallel_map

# Phase 1: 会话内只读,对每个 entity 跑 _resolve_lookup,按三态分类
with session_scope() as conn:
    for ent, emb in zip(ents_to_resolve, ent_embeddings):
        category, payload = _resolve_lookup(conn, ...)
        if category == "resolved":
            ent_map[ent["name"].lower()] = payload
        elif category == "grey":
            grey_entities.append((idx, ent, emb, payload))  # payload = candidates

# Phase 2: 会话外并行,N 个灰区 entity 同时调 LLM
def _decide_grey(item):
    idx, ent, emb, cands = item
    decision = _llm_entity_link(ent["name"], ...)
    if decision.get("reuse"):
        return (idx, find_reuse_id(cands, decision))
    return (idx, None)

results = parallel_map(_decide_grey, grey_entities)  # N 路并发,保序

# Phase 3: 会话内短事务,_resolve_write + facts + belief
with session_scope() as conn:
    for idx, (ent, emb) in enumerate(zip(ents_to_resolve, ent_embeddings)):
        reuse_id = grey_decisions.get(idx)
        if reuse_id:
            ent_map[ent["name"].lower()] = reuse_id
        elif ent["name"].lower() not in ent_map:
            ent_map[ent["name"].lower()] = _resolve_write(conn, ...)
    # 接着插入 facts + 超替 + belief 聚合...
```

关键设计点：

1. **LLM 移出 session**：`_decide_grey` 在 `session_scope()` 外执行。持着 DB 连接等 LLM HTTP 响应（1-3s）是典型的 session 占用浪费——N 个灰区实体会串行占用连接。移出后，Phase 1 和 Phase 3 各自是只读/写入的短事务，连接持有时间降到毫秒级。
2. **`parallel_map` 保序**：来自 `cortex.infra.concurrency`，基于 `ThreadPoolExecutor`。GIL 在网络 I/O 的 `recv()` 处释放，所以 LLM/embed/rerank 这类 HTTP 调用能实现真正的并行。结果按输入顺序返回，单项异常返回 `None` 不阻断其余。
3. **写入仍串行**：Phase 3 的 `_resolve_write` + fact 插入 + belief 聚合在单个事务内串行执行，保证事务一致性。`_resolve_write` 的签名是纯写入，不回查不判定。

## Fact 超替机制

单值谓词的新事实到达时，超替旧值：

```python
def _close_superseded(conn, scope, subject_id, predicate, valid_from):
    """超替:仅对单值谓词,把同 (subject,predicate) 的当前活 fact 闭合"""
    if not _is_single_value(conn, scope, predicate):
        return None
    
    rows = conn.execute(text("""
        SELECT fact_id::text, valid_from::text FROM facts 
        WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p
          AND recorded_to IS NULL AND polarity='positive'
          AND assertion_status IN ('observed','confirmed')
        ORDER BY valid_from FOR UPDATE
    """), {"s": scope, "sub": subject_id, "p": predicate}).fetchall()
    
    target = _parse_timestamp(valid_from)
    for row in rows:
        point = _parse_timestamp(row.valid_from)
        if point == target:
            conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"),
                        {"f": row.fact_id})
            return row.valid_to
    # 闭合前驱
    ...
```

## 完整实体链接流程

下图展示**单个实体**在 `_resolve_lookup` 内的决策树（Phase 1 的逐 entity 逻辑）。N 个实体的编排（三阶段并行）见上一节。

```{mermaid}
flowchart TD
    EXT[抽取管线 extract_event] -->|Phase 1| LINK[_resolve_lookup]
    LINK --> A[别名查询 entity_aliases]
    A --> HIT{精确命中?}
    HIT -->|是| CTX{context_key 匹配?}
    CTX -->|匹配| RES[返回 resolved + entity_id]
    CTX -->|不匹配| B[向量近邻]
    HIT -->|否| B

    B --> CAND[top-5 候选<br/>context_key 过滤]
    CAND --> COMPAT{身份敏感<br/>兼容?}
    COMPAT -->|不兼容| NEW[返回 new]
    COMPAT -->|兼容| SIM{余弦相似度}
    SIM -->|>= 0.85| RES2[返回 resolved<br/>高分直接合并]
    SIM -->|0.30 - 0.85| GREY[返回 grey + candidates]
    SIM -->|< 0.30| NEW

    GREY -->|Phase 2 会话外并行| LLM[_decide_grey → _llm_entity_link]
    LLM --> DECIDE{LLM 判定}
    DECIDE -->|同一实体| REUSE[复用 entity_id]
    DECIDE -->|不同/无key| NEW

    RES --> W[Phase 3: _resolve_write / 直接记下]
    RES2 --> W
    REUSE --> W
    NEW --> W
```

## 关键原则

1. **保守合并**：宁可重复也不错误合并——重复可以后续 consolidate，错误合并会污染因果链
2. **身份隔离**：同名实体在不同设备/腔体中保持独立
3. **灰区 LLM**：中间阈值走 LLM 判定，利用上下文信息
4. **自动别名**：新实体创建时自动注册 canonical_name 为别名

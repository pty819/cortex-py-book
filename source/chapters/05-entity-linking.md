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

```python
def _resolve_or_create(conn, scope, name, etype, description, thresholds, model, context_text="", identity_context=None):
    """返回 entity_id。A 层别名→C 层向量召回→阈值→新建。"""
    canonical_ctx = _identity_context_for_type(identity_context, etype)
    ctx_key = json.dumps(canonical_ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    
    # A 层: 别名精确命中
    exact = conn.execute(text("""
        SELECT DISTINCT e.entity_id, e.context_key FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
        WHERE e.scope=:s AND e.merged_into IS NULL
          AND (lower(e.canonical_name)=lower(:n) OR lower(a.alias)=lower(:n))
          AND ((:t IS NULL AND e.entity_type IS NULL) OR e.entity_type=:t)
        ORDER BY e.entity_id
    """), {"s": scope, "n": name, "t": etype}).fetchall()
    
    # 身份上下文匹配
    if canonical_ctx:
        matches = [r for r in exact if r.context_key == ctx_key]
        if len(matches) == 1:
            return str(matches[0].entity_id)
        # 兼容旧数据:无上下文的单条记录可升级
        legacy = [r for r in exact if r.context_key == "{}"]
        if not matches and len(exact) == 1 and len(legacy) == 1:
            conn.execute(text("""UPDATE entities SET identity_context=CAST(:ctx AS jsonb), 
                                 context_key=:ck, updated_at=now() WHERE entity_id=:e"""),
                         {"ctx": json.dumps(canonical_ctx), "ck": ctx_key, "e": legacy[0].entity_id})
            return str(legacy[0].entity_id)
    elif len(exact) == 1:
        return str(exact[0].entity_id)
    
    # 同名但上下文不同必须保守分离
    if exact:
        cands = []
    else:
        cands = None
    
    # C 层: 向量召回
    # ... 继续到向量层
```

## B 层: 向量近邻

### 身份上下文过滤

向量查询时强制 `context_key` 过滤：

```python
cands = conn.execute(text("""
    SELECT entity_id, canonical_name, description, entity_type, context_key,
           1-(embedding <=> CAST(:q AS vector)) AS cos
    FROM entities WHERE scope=:s AND merged_into IS NULL AND embedding IS NOT NULL
      AND ((:t IS NULL AND entity_type IS NULL) OR entity_type=:t)
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

```{mermaid}
sequenceDiagram
    participant EXT as 抽取管线
    participant LINK as 实体链接
    participant DB as PostgreSQL
    participant LLM as LLM 灰区
    
    EXT->>LINK: _resolve_or_create(name, type, ctx)
    LINK->>DB: A层: 别名查询
    DB-->>LINK: 精确命中?
    
    alt A层命中
        LINK->>DB: 检查 context_key
        DB-->>LINK: 匹配→返回 entity_id
    else A层未命中
        LINK->>DB: B层: 向量近邻 (context_key 过滤)
        DB-->>LINK: top-5 候选
        
        LINK->>LINK: _identity_candidate_compatible
        alt 高相似度 >= 0.85
            LINK->>DB: 直接合并
        else 灰区 0.30-0.85
            LINK->>LLM: 灰区判定
            LLM-->>LINK: 复用/新建
            alt LLM 复用
                LINK->>DB: 合并到候选
            else LLM 新建
                LINK->>DB: 创建新实体
            end
        else 低相似度 < 0.30
            LINK->>DB: 创建新实体
        end
    end
    LINK-->>EXT: entity_id
```

## 关键原则

1. **保守合并**：宁可重复也不错误合并——重复可以后续 consolidate，错误合并会污染因果链
2. **身份隔离**：同名实体在不同设备/腔体中保持独立
3. **灰区 LLM**：中间阈值走 LLM 判定，利用上下文信息
4. **自动别名**：新实体创建时自动注册 canonical_name 为别名

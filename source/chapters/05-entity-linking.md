# 第5章 实体链接详解

## B over C 策略

实体链接是**图谱质量的命门**。Cortex-PY 采用三层策略：

```{mermaid}
graph TB
    subgraph "A 层: 别名精确匹配"
        A1[查 entity_aliases]
        A2[命中 → 直接返回]
    end
    
    subgraph "B 层: 向量近邻"
        B1[pgvector 近邻查询]
        B2{相似度判断}
        B3[高: 直接合并]
        B4[中: LLM 判定]
        B5[低: 创建新实体]
    end
    
    subgraph "C 层: 创建新实体"
        C1[生成 UUID]
        C2[计算 embedding]
        C3[存入 entities]
    end
    
    A1 --> A2
    A1 -->|未命中| B1
    B1 --> B2
    B2 -->|> 0.85| B3
    B2 -->|0.30-0.85| B4
    B2 -->|< 0.30| C1
    B4 -->|是同一实体| B3
    B4 -->|不是| C1
```

## A 层: 别名匹配

### 最快路径

```python
def match_by_alias(conn, scope, name):
    """A 层: 别名精确匹配"""
    row = conn.execute(text("""
        SELECT e.entity_id, e.canonical_name
        FROM entity_aliases a
        JOIN entities e ON e.entity_id = a.entity_id
        WHERE a.alias = :name 
          AND e.scope = :scope
          AND e.merged_into IS NULL
        LIMIT 1
    """), {"name": name, "scope": scope}).fetchone()
    
    return row
```

### 为什么先查别名？

- **速度**：索引查询，O(1)
- **确定性**：精确匹配，不需要相似度判断
- **常见场景**：别名已经积累了大量映射

## B 层: 向量近邻

### pgvector 查询

```python
def vector_recall(conn, scope, name, top_k=5):
    """B 层: 向量近邻查询"""
    # 计算查询向量
    query_emb = services.embed_one(name)
    
    # pgvector cosine distance
    rows = conn.execute(text("""
        SELECT 
            entity_id,
            canonical_name,
            entity_type,
            description,
            1 - (embedding <=> CAST(:q AS vector)) as similarity
        FROM entities
        WHERE scope = :s 
          AND merged_into IS NULL
          AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:q AS vector)
        LIMIT :k
    """), {
        "s": scope, 
        "q": str(query_emb), 
        "k": top_k
    }).fetchall()
    
    return rows
```

### 阈值判断

```{mermaid}
flowchart TD
    A[相似度 score] --> B{score > 0.85?}
    B -->|是| C[高置信: 直接合并]
    B -->|否| D{score > 0.30?}
    D -->|是| E[灰区: LLM 判定]
    D -->|否| F[低置信: 新建实体]
    
    E --> G{LLM 判断?}
    G -->|是同一实体| C
    G -->|不是| F
```

**配置** (`config.py`):

```python
class LinkThresholds(BaseModel):
    merge: float = 0.85   # 高于此直接合并
    new: float = 0.30     # 低于此新建实体
```

### 直接合并

```python
def merge_entity(conn, source_id, target_id):
    """合并实体: source → target"""
    conn.execute(text("""
        UPDATE entities 
        SET merged_into = :target
        WHERE entity_id = :source
    """), {"source": source_id, "target": target_id})
    
    # 把 source 的 facts 转移到 target
    conn.execute(text("""
        UPDATE facts 
        SET subject_id = :target
        WHERE subject_id = :source
    """), {"source": source_id, "target": target_id})
    
    conn.execute(text("""
        UPDATE facts 
        SET object_entity_id = :target
        WHERE object_entity_id = :source
    """), {"source": source_id, "target": target_id})
    
    # 更新 target 的统计
    conn.execute(text("""
        UPDATE entities 
        SET fact_count = (
            SELECT COUNT(*) FROM facts 
            WHERE subject_id = :target
        )
        WHERE entity_id = :target
    """), {"target": target_id})
```

## 灰区: LLM 判定

### Prompt 设计

```python
JUDGE_PROMPT = """
判断以下两个实体是否指代同一事物:

实体 A: {name_a}
实体 B: {name_b}
实体 B 描述: {description_b}

考虑:
1. 名称相似度 (包含、缩写、别名)
2. 描述语义相似度
3. 实体类型一致性

输出 JSON: {{"is_same": boolean, "reason": "判断理由"}}
"""
```

### LLM 判定实现

```python
def llm_judge_linkage(name, candidate_name, candidate_desc):
    """LLM 判定两个实体是否相同"""
    if not llm_configured("extraction"):
        # 无 LLM key 时的默认行为
        return {"is_same": False, "reason": "no LLM"}
    
    prompt = JUDGE_PROMPT.format(
        name_a=name,
        name_b=candidate_name,
        description_b=candidate_desc or "无描述"
    )
    
    result = services.llm_chat("extraction", prompt, "")
    return services.parse_llm_json(result)
```

## C 层: 创建新实体

### 创建流程

```{mermaid}
flowchart TD
    A[创建新实体] --> B[生成 UUID]
    B --> C[设置基本信息]
    C --> D[计算 embedding]
    D --> E[存入 entities]
    E --> F[添加别名]
    F --> G[更新统计]
```

### 实现

```python
def create_entity(conn, scope, name, entity_type=None, description=None):
    """创建新实体"""
    entity_id = uuid.uuid4()
    
    # 1. 插入实体
    conn.execute(text("""
        INSERT INTO entities (
            entity_id, scope, canonical_name, 
            entity_type, description
        ) VALUES (
            :id, :scope, :name, :type, :desc
        )
    """), {
        "id": entity_id,
        "scope": scope,
        "name": name,
        "type": entity_type,
        "desc": description
    })
    
    # 2. 添加别名
    conn.execute(text("""
        INSERT INTO entity_aliases (entity_id, alias)
        VALUES (:id, :alias)
        ON CONFLICT DO NOTHING
    """), {"id": entity_id, "alias": name})
    
    # 3. 异步计算 embedding (enrich job)
    # 不阻塞当前流程
    
    return entity_id
```

### Embedding 计算时机

```{mermaid}
sequenceDiagram
    participant C as Client
    participant API as API
    participant DB as DB
    participant W as Worker
    
    C->>API: 写入 Event
    API->>DB: INSERT event
    API->>DB: INSERT job (extract)
    API-->>C: 202
    
    Note over W: 异步处理
    
    W->>DB: claim extract job
    W->>W: LLM 抽取
    W->>DB: INSERT entity (无 embedding)
    W->>DB: INSERT job (enrich)
    
    Note over W: 再次异步
    
    W->>DB: claim enrich job
    W->>W: 计算 embedding
    W->>DB: UPDATE entity SET embedding = ...
```

## 别名管理

### 别名来源

1. **创建时**：canonical_name 自动成为别名
2. **合并时**：被合并实体的名称成为别名
3. **手动添加**：API 调用添加别名

```python
def add_alias(conn, entity_id, alias):
    """添加别名"""
    conn.execute(text("""
        INSERT INTO entity_aliases (entity_id, alias)
        VALUES (:id, :alias)
        ON CONFLICT DO NOTHING
    """), {"id": entity_id, "alias": alias})
```

### 别名索引

```sql
-- 用于快速查找和模糊匹配
CREATE INDEX idx_aliases_alias ON entity_aliases 
    USING gin (alias gin_trgm_ops);
```

## 实体分裂

### 场景

当发现两个被合并的实体实际上是不同事物时，需要分裂。

```{mermaid}
flowchart TD
    A[发现误合并] --> B[创建新实体]
    B --> C[重新分配 facts]
    C --> D[更新 embedding]
    D --> E[添加别名]
```

### 实现

```python
def split_entity(conn, original_id, new_name, new_type, new_desc):
    """分裂实体"""
    # 1. 创建新实体
    new_id = create_entity(conn, scope, new_name, new_type, new_desc)
    
    # 2. 重新分配部分 facts (需要人工判断哪些分配给新实体)
    # 这里简化: 把所有 object 是原实体的 facts 分配给新实体
    conn.execute(text("""
        UPDATE facts 
        SET object_entity_id = :new
        WHERE object_entity_id = :original
    """), {"original": original_id, "new": new_id})
    
    # 3. 更新统计
    update_entity_stats(conn, original_id)
    update_entity_stats(conn, new_id)
    
    return new_id
```

## 质量监控

### 实体质量指标

| 指标 | 计算方式 | 目标 |
|------|----------|------|
| 唯一实体率 | unique / total | > 90% |
| 平均别名数 | aliases / entities | 2-5 |
| 灰区命中率 | gray_zone / total | < 20% |
| 合并准确率 | correct_merges / total_merges | > 95% |

### 监控查询

```sql
-- 唯一实体率
SELECT 
    COUNT(DISTINCT canonical_name) * 1.0 / COUNT(*) as unique_rate
FROM entities 
WHERE merged_into IS NULL;

-- 平均别名数
SELECT 
    COUNT(*) * 1.0 / COUNT(DISTINCT entity_id) as avg_aliases
FROM entity_aliases;

-- 最近合并操作
SELECT 
    e1.canonical_name as source,
    e2.canonical_name as target,
    e1.updated_at
FROM entities e1
JOIN entities e2 ON e1.merged_into = e2.entity_id
ORDER BY e1.updated_at DESC
LIMIT 10;
```

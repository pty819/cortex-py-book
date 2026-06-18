# 第2章 数据模型

## 设计哲学

```{admonition} 核心原则
1. **Facts 表是图谱的核心** —— 同时承担"双时态三元组存储"和"图遍历的边表"
2. **Entity 表是实体链接的载体** —— 支持向量召回 + 别名 + 合并/分裂
3. **scope 过滤是 SQL 层强制** —— 所有查询和 CTE 都带 scope 条件
4. **所有派生记录可从 Events 重建** —— WAL 是唯一真相源
5. **Postgres 原生类型优先** —— JSONB / ARRAY / tsvector / pgvector
```

## 表清单

| 表 | 角色 | 核心字段 |
|----|------|----------|
| `events` | WAL, 唯一真相源 | scope, content, context, idempotency_key |
| `entities` | 实体表, B over C 载体 | canonical_name, entity_type, embedding |
| `entity_aliases` | 别名表 | entity_id, alias |
| `facts` | **双时态三元组 + 图边** | subject_id, predicate, object, 双时态4字段 |
| `beliefs` | 概率断言 + supports 链 | about_entity_id, claim, confidence, supports |
| `episodes` | 有界事件序列 | scope, started_at, ended_at, event_ids |
| `jobs` | Postgres-as-queue | job_type, status, payload |
| `scopes` | scope 注册表 | scope, created_at |

## 实体表 (entities)

### Schema

```sql
CREATE TABLE entities (
    entity_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope             TEXT NOT NULL,
    
    -- 基本信息
    canonical_name    TEXT NOT NULL,
    entity_type       TEXT,  -- person/org/concept/...
    description       TEXT,
    
    -- 向量 (用于 B over C 实体链接)
    embedding         vector(1024),
    
    -- 合并 (B over C 的 C 策略)
    merged_into       UUID REFERENCES entities(entity_id),
    
    -- 统计
    fact_count        INTEGER NOT NULL DEFAULT 0,
    
    -- 时间
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    
    -- 约束
    UNIQUE (scope, canonical_name)
);

-- 向量索引 (HNSW)
CREATE INDEX idx_entities_embedding ON entities 
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

### Entity Linking: B over C 策略

```{mermaid}
flowchart TD
    A[新提及的实体] --> B{向量召回<br/>cosine similarity}
    B -->|score > 0.85| C[直接合并<br/>merged_into]
    B -->|0.30 < score < 0.85| D[LLM 判定<br/>灰区]
    B -->|score < 0.30| E[创建新实体]
    
    C --> F[复用已有实体 ID]
    D -->|是同一实体| C
    D -->|不是同一实体| E
    
    E --> G[计算 embedding]
    F --> G
```

**配置阈值** (`config.py`):

```python
class LinkThresholds(BaseModel):
    merge: float = 0.85   # 高于此阈值直接合并
    new: float = 0.30     # 低于此阈值创建新实体
    # 0.30-0.85 是灰区，调 LLM 判定
```

## 别名表 (entity_aliases)

```sql
CREATE TABLE entity_aliases (
    alias_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id   UUID NOT NULL REFERENCES entities(entity_id),
    alias       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    
    UNIQUE (entity_id, alias)
);

-- 用于 synonym 通道检索
CREATE INDEX idx_aliases_alias ON entity_aliases (alias gin_trgm_ops);
```

**用途**：
- 实体链接：新提到的名称 → 查别名 → 直接命中
- Synonym 通道：recall 时匹配别名

## 双时态设计详解

### 为什么需要双时态？

```{mermaid}
graph LR
    subgraph "场景1：当时以为的"
        T1[2024-01: Alice 在 Acme] 
        T2[2024-06: Alice 离开 Acme]
    end
    
    subgraph "场景2：现在查询"
        Q1["问：2024-03 时，Alice 在哪？"]
        A1["答：Acme 当时记录"]
    end
    
    subgraph "场景3：现在查询"
        Q2["问：现在 Alice 在哪？"]
        A2["答：不在 Acme 已 invalid"]
    end
```

### 4 个时间字段的含义

| 字段 | 含义 | 查询场景 |
|------|------|----------|
| `valid_from` | 在世界中何时开始为真 | "Alice 何时加入 Acme?" |
| `valid_to` | 在世界中何时不再为真 | "Alice 何时离开 Acme?" |
| `recorded_from` | 系统何时获知 | "我们何时知道这件事?" |
| `recorded_to` | 何时被新版本取代 | "这个信息何时过时?" |

### 查询示例

```sql
-- 问"现在什么是真的"
SELECT * FROM facts 
WHERE valid_to IS NULL    -- 仍然为真
  AND recorded_to IS NULL; -- 当前版本

-- 问"2024-03 时我们怎么以为的"
SELECT * FROM facts 
WHERE recorded_from <= '2024-03-01' 
  AND (recorded_to IS NULL OR recorded_to > '2024-03-01');

-- 问"Alice 在 Acme 的完整历史"
SELECT * FROM facts 
WHERE subject_name = 'Alice' 
  AND predicate = 'works_at'
ORDER BY valid_from;
```

## 图遍历

### Facts 作为图边

```{mermaid}
graph LR
    A[Alice] -->|works_at| B[Acme]
    B[Acme] -->|is_a| C[Company]
    D[Bob] -->|works_at| B
    A -->|manages| D
    
    style A fill:#e1f5fe
    style B fill:#fff3e0
    style C fill:#e8f5e8
    style D fill:#e1f5fe
```

### 递归 CTE 图遍历

```sql
-- 从种子实体出发，2-3 跳 BFS
WITH RECURSIVE graph_walk AS (
    -- 种子层 (0 跳)
    SELECT 
        f.fact_id,
        f.subject_id,
        f.object_entity_id,
        0 as depth
    FROM facts f
    WHERE f.subject_id = :seed_entity_id
      AND f.valid_to IS NULL
    
    UNION ALL
    
    -- 递归 (1-N 跳)
    SELECT 
        f.fact_id,
        f.subject_id,
        f.object_entity_id,
        gw.depth + 1
    FROM facts f
    JOIN graph_walk gw ON (
        f.subject_id = gw.object_entity_id 
        OR f.object_entity_id = gw.subject_id
    )
    WHERE gw.depth < :max_hops  -- 通常 2-3
      AND f.valid_to IS NULL
)
SELECT DISTINCT fact_id FROM graph_walk;
```

## 索引策略

| 表 | 索引 | 用途 |
|----|------|------|
| entities | `hnsw (embedding)` | 向量近邻查询 |
| entities | `gin (canonical_name gin_trgm_ops)` | 模糊匹配 |
| facts | `btree (subject_id)` | 图遍历起点 |
| facts | `btree (object_entity_id)` | 图遍历终点 |
| facts | `gin (content_tsv)` | BM25 全文检索 |
| facts | `gin (supports)` | 证据链查询 |
| events | `btree (scope, observed_at)` | 范围查询 |
| events | `gin (content->'text' gin_trgm_ops)` | 全文检索 |

## Scope 隔离

### 层级 Scope

```{mermaid}
graph TB
    R["/ (root)"] --> O1["org:acme"]
    R --> O2["org:other"]
    O1 --> D1["dept:eng"]
    O1 --> D2["dept:sales"]
    D1 --> U1["user:alice"]
    D1 --> U2["user:bob"]
```

### Scope 过滤

```python
# retrieval/pipeline.py
def _scope_filter(scope, view):
    if view == "holistic":
        # 向上聚合: /org → /org/dept → /org/dept/user
        prefixes = scope_to_prefixes(scope)
        return "scope = ANY(:scopes)", {"scopes": prefixes}
    elif view == "descend":
        # 向下展开: /org/dept → 所有子 scope
        return "(scope = :s OR scope LIKE :sp)", {"s": scope, "sp": scope + "/%"}
    else:
        # 精确匹配
        return "scope = :s", {"s": scope}
```

## 数据完整性

### WAL 重建

所有派生层 (Facts, Beliefs, Understanding) 都可以**从 Events 重建**：

```{mermaid}
flowchart TD
    E[Events] -->|extract| F[Facts]
    E -->|segment| EP[Episodes]
    F -->|aggregate| B[Beliefs]
    B -->|synthesize| U[Understanding]
    
    U -.->|drop & rebuild| U
    B -.->|drop & rebuild| B
    F -.->|drop & rebuild| F
    EP -.->|drop & rebuild| EP
```

**保证**：丢掉任何派生层，重新跑 pipeline 即可恢复。

# 第2章 数据模型

## 设计哲学

```{admonition} 核心原则
1. **Facts 表是图谱的核心** —— 同时承担"双时态三元组存储"和"图遍历的边表"
2. **Entity 表是实体链接的载体** —— 支持向量召回 + 别名 + identity_context
3. **scope 过滤是 SQL 层强制** —— 所有查询和 CTE 都带 scope 条件
4. **所有派生记录可从 Events 重建** —— WAL 是唯一真相源
5. **Postgres 原生类型优先** —— JSONB / ARRAY / tsvector / pgvector
6. **断言语义** —— polarity + assertion_status 双轴描述事实
7. **身份上下文** —— context_key + identity_context 实现多设备/多腔体隔离
```

## 完整表清单

| 表 | 角色 | 核心字段 |
|----|------|----------|
| `events` | WAL, 唯一真相源 | scope, content, context, idempotency_key |
| `entities` | 实体表, B-over-C 载体 | canonical_name, entity_type, embedding, identity_context |
| `entity_aliases` | 别名表 | entity_id, alias |
| `facts` | **双时态三元组 + 图边** | subject_id, predicate, object, 双时态4字段, polarity, assertion_status |
| `beliefs` | 概率断言 + supports 链 | about_entity_id, claim, confidence, supports |
| `episodes` | 有界事件序列 + Case | scope, event_ids, case_id, equipment, root_cause |
| `concepts` | Understanding 概念 | name, topic, summary, supports, related |
| `jobs` | Postgres-as-queue | job_type, status, payload |
| `scopes` | scope 注册表 | scope_path, parent_path, policies |
| `lifecycle_events` | 生命周期事件 | kind, scope, event_id, payload |
| `audit_log` | 审计日志 | actor, scope, endpoint, action |
| `blobs` | 大对象存储 | sha256, content_type, storage, refcount |
| `vocabularies` | 词表定义 | name, kind (closed/open), cardinality |
| `vocabulary_values` | 词表值 | canonical_value, aliases, cardinality |
| `synonyms` | 同义词扩展 | term, aliases |
| `temporal_phrases` | 时间短语 | name, anchor, expression |
| `import_jobs` | 导入任务 | source, status, accepted, failed |
| `erasure_jobs` | 擦除任务 | selector, phase, manifest |
| `recall_packs` | 检索结果缓存 | pack_id, query_hash, pack_json, expires_at |

## 实体表 (entities)

### Schema

```sql
CREATE TABLE entities (
    entity_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope             TEXT NOT NULL,
    
    -- 基本信息
    canonical_name    TEXT NOT NULL,
    entity_type       TEXT,  -- equipment/sensor/component/fault/...
    description       TEXT,
    
    -- 身份上下文（多设备/多腔体隔离）
    identity_context  JSONB NOT NULL DEFAULT '{}',
    context_key       TEXT NOT NULL DEFAULT '{}',
    
    -- 向量 (用于 B-over-C 实体链接)
    embedding         vector(1024),
    
    -- 合并 (B-over-C 的 C 策略)
    merged_into       UUID REFERENCES entities(entity_id),
    merge_confidence  FLOAT,
    
    -- 时间
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 身份上下文 (Identity Context)

用于区分同名实体在不同设备/腔体中的实例：

```python
_CONTEXT_FIELDS = ("fab", "equipment", "module", "chamber", "recipe", "recipe_revision")

def canonical_identity_context(context):
    raw = context or {}
    values = {
        "fab": raw.get("fab"),
        "equipment": raw.get("tool") or raw.get("equipment"),
        "module": raw.get("module"),
        "chamber": raw.get("chamber"),
        "recipe": raw.get("recipe"),
        "recipe_revision": raw.get("recipe_revision") or raw.get("recipe_rev"),
    }
    return {key: canon for key in _CONTEXT_FIELDS if (canon := _canonical_text(values.get(key) or ""))}
```

身份上下文按实体类型分层：

| 实体类型 | 包含的上下文字段 |
|----------|------------------|
| equipment / tool | fab, equipment |
| module / chamber / component / sensor / subsystem | fab, equipment, module, chamber |
| recipe / process_step / process_param | 全部 6 个字段 |
| 其他（fault / material / phenomenon 等） | 无 |

## Facts 表

### Schema

```sql
CREATE TABLE facts (
    fact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    subject_id       UUID NOT NULL REFERENCES entities(entity_id),
    predicate        TEXT NOT NULL,
    object_type      TEXT NOT NULL,     -- 'entity' | 'literal'
    object_entity_id UUID REFERENCES entities(entity_id),
    object_value     JSONB,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    recorded_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to      TIMESTAMPTZ,
    confidence       FLOAT NOT NULL DEFAULT 0.5,
    polarity         TEXT NOT NULL DEFAULT 'positive',
    assertion_status TEXT NOT NULL DEFAULT 'observed',
    evidence_span    TEXT,
    supports         UUID[] NOT NULL DEFAULT '{}',
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    extraction_model TEXT
);
```

### 断言语义规则

```python
def _assertion_semantics(predicate, fact, trusted=False, source_text=None):
    polarity = "negative" if fact.get("negation") else fact.get("polarity", "positive")
    requested = fact.get("assertion_status")
    
    # ruled_out → 固定 negative + ruled_out
    if predicate in OPPOSING_PREDICATES:
        return "negative", "ruled_out"
    
    # 因果谓词默认 hypothesized
    if predicate in CAUSAL_PREDICATES:
        if polarity == "negative":
            return polarity, "ruled_out"
        evidence = str(fact.get("evidence_span") or "").strip()
        grounded = bool(source_text and evidence and evidence in source_text)
        if requested == "confirmed" and evidence and (trusted or grounded):
            return polarity, "confirmed"
        return polarity, "hypothesized"
    
    return polarity, requested or "observed"
```

### 图准入规则

```{mermaid}
flowchart TD
    F[Fact] --> P{polarity?}
    P -->|negative| EXCLUDE[排除出图]
    P -->|positive| E{predicate?}
    E -->|因果谓词| S{assertion_status?}
    E -->|非因果谓词| S2{assertion_status?}
    S -->|confirmed| INCLUDE[入图]
    S -->|其他| EXCLUDE
    S2 -->|observed/confirmed| INCLUDE
    S2 -->|其他| EXCLUDE
```

## Ontology 模块

`cortex/ontology.py` 是**谓词本体的单一真相源**：

```python
STRUCTURAL_PREDICATES = {
    "part_of", "has_component", "installed_on", "located_in", "monitored_by",
    "controlled_by", "regulates", "configured_as", "depends_on",
}

CAUSAL_PREDICATES = {
    "caused_by", "led_to", "cascades_to", "affects", "triggers", "contributes_to",
    "correlates_with", "suggests", "symptom_of", "has_symptom",
}

DIAGNOSTIC_PREDICATES = {
    "detected_by", "investigates", "investigated_by", "checked", "found", "normal",
    "ruled_out", "no_correlation", "supports", "contradicts", "refines_to",
    "alternative_to", "confirmed_by", "repaired_by", "observed_by", "references",
    "preceded_by", "drifts_from", "measured_as", "deviates_from", "feedback_to",
}
```

### Cardinality

```python
PREDICATE_CARDINALITY = {
    "has_status": "single",      # 新值超替旧值
    "deal_stage": "single",
    "part_of": "multi",          # 多值共存
    "has_component": "multi",
    "caused_by": "multi",
    # ...
}
```

## Vocabularies 词表系统

### 词表定义

```sql
CREATE TABLE vocabularies (
    vocab_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('closed','open')),
    description TEXT,
    cardinality TEXT DEFAULT 'multi',
    UNIQUE (scope, name)
);
```

### 词表值

```sql
CREATE TABLE vocabulary_values (
    value_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vocab_id        UUID NOT NULL REFERENCES vocabularies(vocab_id) ON DELETE CASCADE,
    canonical_value TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    sort_order      INT NOT NULL DEFAULT 0,
    cardinality     TEXT DEFAULT 'multi',
    UNIQUE (vocab_id, canonical_value)
);
```

### Coerce 逻辑

```python
def coerce_value(conn, scope, vocab_name, raw):
    """closed:未命中→null; open:未命中→保留; 命中别名→canonical"""
    row = conn.execute("SELECT vocab_id, kind FROM vocabularies WHERE scope=:s AND name=:n",
                       {"s": scope, "n": vocab_name}).fetchone()
    if not row:
        return raw
    hit = conn.execute("""
        SELECT vv.canonical_value FROM vocabulary_values vv WHERE vv.vocab_id=:v
        AND (vv.canonical_value=:r OR :r = ANY(vv.aliases)) LIMIT 1
    """, {"v": row.vocab_id, "r": raw}).fetchone()
    if hit:
        return hit.canonical_value
    return raw if row.kind == "open" else None
```

详见第17章 词表系统详解。

## Synonyms 同义词表

```sql
CREATE TABLE synonyms (
    synonym_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    term        TEXT NOT NULL,         -- 规范词
    aliases     TEXT[] NOT NULL DEFAULT '{}',  -- 同义扩展
    UNIQUE (scope, term)
);
```

用于检索的 synonym 通道扩展查询词。

## Concepts 概念表

Understanding 层的概念存储：

```sql
CREATE TABLE concepts (
    concept_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope        TEXT NOT NULL,
    name         TEXT NOT NULL,
    topic        TEXT,
    version      INT NOT NULL DEFAULT 1,
    summary      TEXT,
    supports     UUID[] NOT NULL DEFAULT '{}',
    related      JSONB NOT NULL DEFAULT '[]',
    confidence   FLOAT NOT NULL DEFAULT 0.5,
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Temporal Phrases 时间短语表

```sql
CREATE TABLE temporal_phrases (
    phrase_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,
    anchor       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expression   TEXT NOT NULL,         -- 两 ISO8601 duration 以 '..' 隔
    is_default   BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

内置默认短语：

| 名称 | 表达式 | 含义 |
|------|--------|------|
| last week | `-P7D..P0D` | 最近 7 天 |
| last month | `-P30D..P0D` | 最近 30 天 |
| yesterday | `-P1D..P0D` | 昨天 |
| last quarter | `-P90D..P0D` | 最近 90 天 |
| last year | `-P365D..P0D` | 最近 365 天 |

## Blobs 大对象表

```sql
CREATE TABLE blobs (
    blob_id        TEXT PRIMARY KEY,
    sha256         TEXT NOT NULL UNIQUE,
    content_type   TEXT NOT NULL,
    size_bytes     BIGINT NOT NULL,
    storage        TEXT NOT NULL DEFAULT 'inline',
    data           BYTEA,
    external_path  TEXT,
    scope          TEXT NOT NULL,
    uploader_actor TEXT NOT NULL,
    refcount       BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Import Jobs 导入任务表

```sql
CREATE TABLE import_jobs (
    import_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    source         TEXT NOT NULL,
    scope_template TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    accepted       INT NOT NULL DEFAULT 0,
    failed         INT NOT NULL DEFAULT 0,
    total          INT NOT NULL DEFAULT 0,
    ordering       TEXT,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);
```

## Erasure Jobs 擦除任务表

```sql
CREATE TABLE erasure_jobs (
    erasure_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         TEXT NOT NULL,
    selector      JSONB NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'enumerate',
    preview_id    UUID,
    manifest      JSONB,
    refcount_breakdown JSONB,
    progress      JSONB NOT NULL DEFAULT '{}',
    audit_id      UUID,
    idempotency_key TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
```

## 实体关系全景图

```{mermaid}
erDiagram
    EVENTS ||--o{ FACTS : supports
    ENTITIES ||--o{ FACTS : subject
    ENTITIES ||--o{ FACTS : object
    ENTITIES ||--o{ ENTITY_ALIASES : has
    ENTITIES ||--o{ BELIEFS : about
    ENTITIES ||--o{ EPISODES : actors
    EVENTS ||--o{ EPISODES : contains
    EVENTS ||--o{ JOBS : triggers
    VOCABULARIES ||--o{ VOCABULARY_VALUES : contains
    ENTITIES ||--o{ CONCEPTS : related
    BLOBS }o--|| EVENTS : referenced
    
    EVENTS {
        uuid event_id PK
        bigint wal_offset
        text scope
        text modality
        jsonb content
        jsonb context
        text idempotency_key
        text case_id
    }
    FACTS {
        uuid fact_id PK
        text predicate
        text polarity
        text assertion_status
        float confidence
        uuid[] supports
    }
    ENTITIES {
        uuid entity_id PK
        text canonical_name
        text entity_type
        vector embedding
        jsonb identity_context
    }
    VOCABULARIES {
        text name
        text kind
        text cardinality
    }
```

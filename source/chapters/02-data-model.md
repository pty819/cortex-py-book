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

cortex schema 共 **27 张表**(全部幂等 `CREATE TABLE IF NOT EXISTS`,部分列以 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 增量补加)。下表按逻辑层分组:

| 表 | 角色 | 核心字段 |
|----|------|----------|
| `events` | WAL, 唯一真相源 | scope, content, context, idempotency_key, access_count, feedback_processed, last_recalled_at |
| `entities` | 实体表, B-over-C 载体 | canonical_name, entity_type, embedding, identity_context |
| `entity_aliases` | 别名表 | entity_id, alias |
| `facts` | **双时态三元组 + 图边** | subject_id, predicate, object, 双时态4字段, polarity, assertion_status, salience, 正/负反馈计数, is_higher_order, evidence_fact_ids |
| `beliefs` | 概率断言 + supports 链 | about_entity_id, claim, confidence, supports |
| `episodes` | 有界事件序列 + Case | scope, event_ids, case_id, equipment, root_cause |
| `concepts` | Understanding 概念 | name, topic, summary, supports, related |
| `evidence_artifacts` | **外部证据目录**(payload 留权威系统) | evidence_kind, uri/source_record_id, content_hash, source_system, observed_from/to, query_spec, quality |
| `claim_evidence` | fact ↔ evidence 引用 | fact_id, evidence_id, role(supports/refutes), weight, span |
| `assertion_case_links` | fact ↔ Case 关联 | fact_id, episode_id, relation(hypothesis/counter/regime) |
| `episode_evidence` | Case ↔ evidence 关联 | episode_id, evidence_id, role(regression) |
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
| `feedback_signals` | **反馈信号总线**(反馈回灌) | scope, target_layer, target_id, signal_type, signal_durable, strength, idempotency_key, applied |
| `dreaming_runs` | **离线巩固运行记录**(Dreaming) | run_id, scope, status, phase0_closed, phase_a_clusters, phase_b_issues, phase_c_actions |
| `evolution_candidates` | **人工审批门**(Dreaming/Higher-Order 候选) | source_type, proposed_action, subject_id, payload, source_fact_ids, status(pending/approved/rejected), reviewer, reasoning |
| `predicate_definitions` | **谓词本体表**(从 ontology.py 迁入 DB) | predicate, category, prop_order, cardinality, example |

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

#### 记忆自演化的增量列(ALTER TABLE 增量补加)

以下 6 列在 `schema.sql` 中以 `ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS ...` 形式追加(位于主 `CREATE TABLE` 之后),用于支持 salience 软降权、反馈计数与高阶归纳:

```sql
-- 信号总线:facts 软降权(salience)+ 反馈计数(冗余加速查询)
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS salience FLOAT NOT NULL DEFAULT 1.0
    CHECK (salience >= 0 AND salience <= 2);
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS positive_feedback_count INT NOT NULL DEFAULT 0;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS negative_feedback_count INT NOT NULL DEFAULT 0;

-- Higher-Order 高阶归纳:一阶事实 LLM 归纳高阶结论
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS is_higher_order BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS higher_order_reasoning TEXT;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS evidence_fact_ids UUID[] NOT NULL DEFAULT '{}';
```

| 列 | 类型 | 用途 |
|----|------|------|
| `salience` | FLOAT ∈ [0,2],默认 1.0 | 信号总线软降权;负反馈累积会把它拉低,影响召回排序 |
| `positive_feedback_count` | INT,默认 0 | 正反馈计数(冗余加速查询,避免每次聚合 feedback_signals) |
| `negative_feedback_count` | INT,默认 0 | 负反馈计数 |
| `is_higher_order` | BOOLEAN,默认 false | 标记该 fact 是由一阶事实经 LLM 归纳出的高阶结论 |
| `higher_order_reasoning` | TEXT | 高阶归纳的推理过程(可空) |
| `evidence_fact_ids` | UUID[],默认 '{}' | 支撑本高阶结论的一阶 fact_id 列表 |

### Events 表的增量列

`events` 同样以 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 增量补加了两列,服务于反馈幂等与召回热度追踪:

```sql
-- 信号总线:events 隐式反馈幂等标记 + 最近被召回时间
ALTER TABLE cortex.events ADD COLUMN IF NOT EXISTS feedback_processed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cortex.events ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ;
```

| 列 | 类型 | 用途 |
|----|------|------|
| `access_count` | INT,默认 0 | 已在主 `CREATE TABLE` 中;事件被召回次数,用于 methylation 冷数据扫描(`access_count = 0` 且长期未被召回) |
| `feedback_processed` | BOOLEAN,默认 false | 隐式反馈已处理标记,避免对同一事件重复应用反馈信号 |
| `last_recalled_at` | TIMESTAMPTZ | 最近一次被召回的时间戳;配合 `access_count` 判断热度与甲基化 |

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

`src/cortex/infra/ontology.py` 是**谓词本体的单一真相源**(其内容由 `predicate_definitions` 表镜像,DB 版本支持 `prop_order`/`cardinality` 可配):

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

详见第21章 词表系统详解。

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

## 记忆自演化四表

这四张表支撑 cortex 的"记忆自演化"能力:反馈信号回灌修正召回、离线 Dreaming 巩固去重归纳、Dreaming/Higher-Order 候选的人工审批门、谓词本体从硬编码迁入 DB 可配。

### feedback_signals — 反馈信号总线

用户/系统显式反馈的落地表。`applied`/`applied_at` 跟踪是否已被吸收回 facts 的 salience 与正负反馈计数;`idempotency_key` 唯一保证重复投递幂等。

```sql
CREATE TABLE IF NOT EXISTS cortex.feedback_signals (
    feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    pack_id          TEXT,                          -- 引用 recall_packs.pack_id(可选,溯源哪次召回)
    target_layer     TEXT NOT NULL CHECK (target_layer IN ('fact','belief','event')),
    target_id        UUID NOT NULL,
    signal_type      TEXT NOT NULL CHECK (signal_type IN ('relevant','irrelevant','wrong','partial')),
    signal_durable   TEXT NOT NULL DEFAULT 'long_term'
                     CHECK (signal_durable IN ('task_temporary','scenario_specific','long_term')),
    strength         FLOAT NOT NULL DEFAULT 1.0,
    reason           TEXT,
    actor            TEXT,
    idempotency_key  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied          BOOLEAN NOT NULL DEFAULT false,
    applied_at       TIMESTAMPTZ,
    UNIQUE(idempotency_key)
);
```

### dreaming_runs — 离线巩固运行记录

每次离线 Dreaming(巩固)运行写一行,记录各阶段产出量,便于回溯与运营监控:

```sql
CREATE TABLE IF NOT EXISTS cortex.dreaming_runs (
    run_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
    phase0_closed    INT NOT NULL DEFAULT 0,    -- 精确去重关了多少 fact
    phase_a_clusters INT NOT NULL DEFAULT 0,    -- 发现多少候选簇
    phase_b_issues   INT NOT NULL DEFAULT 0,    -- LLM 发现多少 issue
    phase_c_actions  INT NOT NULL DEFAULT 0,    -- 执行多少动作
    summary          JSONB                       -- 详细统计
);
```

### predicate_definitions — 谓词本体表

把原本硬编码在 `src/cortex/infra/ontology.py` 的谓词分类迁入 DB,新增 `prop_order` 区分一阶(1)/高阶(2)谓词,`cardinality` 标记单值超替或多值共存:

```sql
CREATE TABLE IF NOT EXISTS cortex.predicate_definitions (
    predicate       TEXT PRIMARY KEY,
    category        TEXT NOT NULL CHECK (category IN ('structural','causal','diagnostic','state','higher_order')),
    prop_order      INT NOT NULL DEFAULT 1 CHECK (prop_order IN (1,2)),  -- 1=一阶, 2=高阶
    description     TEXT,
    cardinality     TEXT NOT NULL DEFAULT 'multi' CHECK (cardinality IN ('single','multi')),
    example         TEXT
);
```

### evolution_candidates — 人工审批门

Dreaming 和 Higher-Order 都**不直接改 verified graph**:它们先在此表落一条 `status=pending` 的候选,等人工(或规则)审批后才执行变更。`source_type` 标记来源,`proposed_action` 是受控动作集,`source_fact_ids` 指向支撑该候选的一阶事实:

```sql
CREATE TABLE IF NOT EXISTS cortex.evolution_candidates (
    candidate_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope              TEXT NOT NULL,
    source_type        TEXT NOT NULL CHECK (source_type IN ('dreaming','higher_order')),
    proposed_action    TEXT NOT NULL CHECK (proposed_action IN ('archive','merge','create','update_quality','promote')),
    subject_id         UUID REFERENCES cortex.entities(entity_id),
    predicate          TEXT,
    payload            JSONB NOT NULL DEFAULT '{}',
    source_fact_ids    UUID[] NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','applied','failed')),
    proposed_confidence FLOAT CHECK (proposed_confidence IS NULL OR (proposed_confidence >= 0 AND proposed_confidence <= 1)),
    reasoning          TEXT,
    reviewer           TEXT,
    reviewed_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

审批走 `POST /v1/admin/evolution-candidates/{id}/review`(`memory.evolution.review_candidate`),`decision=approve` 后才落地为真实图变更,`reject` 则标注 `reviewer`+`reasoning` 留痕。这是自演化子系统"不污染 verified graph"的关键护栏。

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
    FEEDBACK_SIGNALS }o--o| FACTS : targets
    FEEDBACK_SIGNALS }o--o| BELIEFS : targets
    FEEDBACK_SIGNALS }o--o| EVENTS : targets
    RECALL_PACKS ||--o{ FEEDBACK_SIGNALS : sourced_from
    SCOPES ||--o{ DREAMING_RUNS : scoped
    PREDICATE_DEFINITIONS }o..|| FACTS : "predicate 字典"

    EVENTS {
        uuid event_id PK
        bigint wal_offset
        text scope
        text modality
        jsonb content
        jsonb context
        text idempotency_key
        text case_id
        int access_count
        bool feedback_processed
        timestamptz last_recalled_at
    }
    FACTS {
        uuid fact_id PK
        text predicate
        text polarity
        text assertion_status
        float confidence
        float salience
        int positive_feedback_count
        int negative_feedback_count
        bool is_higher_order
        uuid[] evidence_fact_ids
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
    FEEDBACK_SIGNALS {
        uuid feedback_id PK
        text scope
        text target_layer
        uuid target_id
        text signal_type
        text signal_durable
        float strength
        text idempotency_key
        bool applied
    }
    DREAMING_RUNS {
        uuid run_id PK
        text scope
        text status
        int phase0_closed
        int phase_a_clusters
        int phase_b_issues
        int phase_c_actions
    }
    PREDICATE_DEFINITIONS {
        text predicate PK
        text category
        int prop_order
        text cardinality
    }
```

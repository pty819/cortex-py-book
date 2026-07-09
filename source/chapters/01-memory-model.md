# 第1章 五层记忆模型

## 设计理念

Cortex-PY 的核心是**五层记忆模型**，灵感来源于人类记忆系统：

```{mermaid}
graph TB
    subgraph 感觉记忆
        E[Events<br/>WAL, 不可变]
    end
    
    subgraph 短期记忆
        EP[Episodes<br/>有界事件序列<br/>Case 诊断案例]
    end
    
    subgraph 长期记忆
        F[Facts<br/>双时态三元组<br/>polarity + assertion_status]
        B[Beliefs<br/>概率断言]
        U[Understanding<br/>概念合成]
    end
    
    E -->|抽取| F
    E -->|分段| EP
    EP -->|聚合| F
    F -->|推理| B
    B -->|合成| U
```

## 第1层：Events（事件层）

### 定义

Events 是系统的**唯一真相源**，采用 WAL (Write-Ahead Log) 模式，**不可变 append-only**。

### Schema

```sql
CREATE TABLE events (
    event_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset        BIGSERIAL UNIQUE NOT NULL,
    
    -- 寻址
    scope             TEXT NOT NULL,  -- 'org:acme/dept:eng/user:alice'
    
    -- 内容 (Experience Envelope)
    modality          TEXT NOT NULL,  -- conversation/document/tool_result
    content           JSONB NOT NULL, -- {kind, role, text, ...}
    context           JSONB NOT NULL, -- {observed_at, labels, intent, ...}
    
    -- 身份槽
    caller            TEXT NOT NULL,
    observed_actor    TEXT NOT NULL,
    subject           TEXT,
    
    -- 双时态 (Events 只有 2 字段)
    observed_at       TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    
    -- 幂等
    idempotency_key   TEXT NOT NULL,
    
    -- 自演化信号
    access_count      INT NOT NULL DEFAULT 0,        -- 召回计数(隐式正向反馈)
    feedback_processed BOOLEAN NOT NULL DEFAULT false, -- 反馈是否已被处理入权重
    last_recalled_at  TIMESTAMPTZ,                    -- 最近一次召回时间
    
    -- 修订约束
    UNIQUE (scope, idempotency_key)
);
```

### 关键字段说明

- **`scope`**：命名空间路径，隔离不同设备/产线的知识图谱
- **`idempotency_key`**：幂等键，同 key + 同 body → 返回已有；同 key + 异 body → 409
- **`excluded_from_recall`**：软删除/甲基化标记（由 maintenance 自动管理）
- **`extraction_diagnostics`**：抽取管线诊断信息 JSON
- **`methylated_at`**：甲基化时间戳（长期不召回的事件被标记）
- **`case_id`**：关联的诊断案例 ID（诊断场景）
- **`access_count`**：该事件被召回的次数，作为隐式正向反馈信号（自演化信号总线的一部分）
- **`feedback_processed`**：标记反馈是否已被处理并入权重，避免重复计算
- **`last_recalled_at`**：最近一次被召回的时间戳，用于时间衰减与冷热判定

### 幂等写入

```{mermaid}
flowchart TD
    A[写入请求] --> B{检查 idempotency_key}
    B -->|不存在| C[写入 Events]
    B -->|存在| D{Body 一致?}
    D -->|是| E[返回已有 event_id]
    D -->|否| F[抛出 409 Conflict]
    
    C --> G[Enqueue Job]
    C --> H[emit lifecycle]
```

**实现代码** (`core.py`)：

```python
def append_event(*, scope, modality, content, context, 
                 caller, idempotency_key, ...):
    # 1. 幂等检查
    existing = c.execute(
        "SELECT event_id, wal_offset FROM events "
        "WHERE scope=:s AND idempotency_key=:k",
        {"s": scope, "k": idempotency_key}
    ).fetchone()
    
    if existing:
        # 计算 body hash 比对
        if body_hash_match(existing.event_id, content):
            return existing.event_id, existing.wal_offset
        raise IdempotencyConflict("同 key 不同 body")
    
    # 2. 写入 WAL
    row = c.execute("""INSERT INTO events (...) 
        VALUES (...) RETURNING event_id, wal_offset""", ...)
    
    # 3. 自动注册 scope
    _auto_provision_scope(c, scope)
    
    # 4. 发送生命周期事件
    emit_lifecycle(c, kind="captured", scope=scope, event_id=row.event_id)
    
    return str(row.event_id), row.wal_offset
```

## 第2层：Episodes（事件分段层）

### 定义

Episodes 是按时间窗口或诊断 Case 分组的"有界事件序列"。

```sql
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL,
    title           TEXT,
    event_ids       UUID[] NOT NULL DEFAULT '{}',
    actors          TEXT[] NOT NULL DEFAULT '{}',
    causal_chain    JSONB,
    
    -- 双时态
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ,
    sealed          BOOLEAN NOT NULL DEFAULT false,
    
    -- 诊断 Case 扩展（新增）
    case_id         TEXT,          -- 案例编号
    equipment       TEXT,          -- 设备标识
    lot             TEXT,          -- 批次号
    recipe          TEXT,          -- 配方
    phase           TEXT,          -- 诊断阶段
    root_cause      TEXT,          -- 根因结论
    resolution      TEXT,          -- 修复措施
    status          TEXT DEFAULT 'open',  -- open/investigating/resolved/closed
    metadata        JSONB DEFAULT '{}'
);
```

### 两种分段模式

1. **自动分段**（`segment_scope`）：按 30 分钟时间窗口或 `case_id` 分组
2. **显式 Case**（`create_case`）：下游 agent 创建诊断 Case，手动关联 events

### Case 生命周期

```
open → investigating → resolved → closed
```

诊断阶段（phase）：
```
observation → scoping → investigation → correlation → root_cause → remediation → regression
```

## 第3层：Facts（事实层）

Facts 是系统的**核心知识表示**——subject-predicate-object 三元组，同时作为知识图谱的边。

```sql
CREATE TABLE facts (
    fact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    subject_id       UUID NOT NULL REFERENCES entities(entity_id),
    predicate        TEXT NOT NULL,
    object_type      TEXT NOT NULL,        -- 'entity' | 'literal'
    object_entity_id UUID REFERENCES entities(entity_id),
    object_value     JSONB,                -- {value: "..."} for literals
    
    -- 双时态
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    recorded_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to      TIMESTAMPTZ,
    
    -- 语义字段
    confidence       FLOAT NOT NULL DEFAULT 0.5,
    polarity         TEXT NOT NULL DEFAULT 'positive',  -- positive | negative
    assertion_status TEXT NOT NULL DEFAULT 'observed',  -- observed|hypothesized|confirmed|ruled_out|rejected
    evidence_span    TEXT,
    supports         UUID[] NOT NULL DEFAULT '{}',      -- → event_id
    extraction_model TEXT,
    
    -- 自演化 / 高阶事实
    salience                 FLOAT NOT NULL DEFAULT 1.0 CHECK (salience BETWEEN 0 AND 2),  -- 显著性权重
    positive_feedback_count  INT NOT NULL DEFAULT 0,   -- 显式正向反馈数
    negative_feedback_count  INT NOT NULL DEFAULT 0,   -- 显式负向反馈数
    is_higher_order          BOOLEAN NOT NULL DEFAULT false,  -- 是否为高阶事实(LLM 合成结论)
    higher_order_reasoning   TEXT,                      -- 高阶事实的推理过程
    evidence_fact_ids        UUID[] NOT NULL DEFAULT '{}',  -- → 支撑此高阶事实的一阶 fact_id
    
    CHECK (object_type = 'entity' AND object_entity_id IS NOT NULL
        OR object_type = 'literal' AND object_value IS NOT NULL)
);
```

### 断言语义

**Polarity**（极性）：
| 值 | 含义 | 示例 |
|----|------|------|
| `positive` | 肯定断言 | "腔体压力异常" |
| `negative` | 否定/排除 | "腔体压力未异常" |

**Assertion Status**（断言状态）：
| 值 | 含义 | 适用谓词 |
|----|------|----------|
| `observed` | 观察到的，确认无误 | 结构/配置/传感器等非因果 |
| `hypothesized` | 假设/推断（未确认） | 因果谓词默认 |
| `confirmed` | 有证据确认 | 因果谓词 + 证据支撑 |
| `ruled_out` | 被排除了 | 对立谓词自动 |
| `rejected` | 被驳回 | 明确否定 |

**自动规则**（`_assertion_semantics` 函数）：
- 对立谓词（`ruled_out`）→ 自动 `negative` + `ruled_out`
- 因果谓词 + 无证据 → `hypothesized`
- 因果谓词 + 证据支撑 + 来源可信 → `confirmed`
- 非因果谓词 → 保留 LLM 指定的值

### 谓词本体（Ontology）

所有谓词在 `ontology.py` 中集中定义，三大类：

**结构/配置关系**（静态拓扑）：
```
part_of, has_component, installed_on, located_in,
monitored_by, controlled_by, regulates, configured_as, depends_on
```

**因果/级联关系**（故障传播）：
```
caused_by, led_to, cascades_to, affects, triggers,
contributes_to, correlates_with, suggests, symptom_of, has_symptom
```

**诊断推理关系**（排查过程）：
```
investigates, checked, found, normal, ruled_out,
supports, contradicts, refines_to, alternative_to,
confirmed_by, repaired_by, references, drifts_from,
measured_as, deviates_from, feedback_to, preceded_by
```

**状态谓词**（单值超替）：
```
has_status, deal_stage
```

### 图准入规则

不是所有 facts 都进图遍历。`graph_eligible()` 函数定义准入条件：
```
1. polarity = 'positive'
2. predicate 不是排除谓词（no_correlation, contradicts, ruled_out）
3. 因果谓词 → 必须 assertion_status = 'confirmed'
4. 非因果谓词 → assertion_status ∈ {'observed', 'confirmed'}
```

### Higher-Order Facts（高阶事实）

Facts 表内部其实分**两层**——这是 Facts 层内的一个抽象子层，而不是一个独立的新层：

| 层级 | `is_higher_order` | 来源 | `evidence_fact_ids` |
|------|-------------------|------|---------------------|
| **一阶事实（first-order）** | `false` | 抽取管线从原始 Event 直接抽出的三元组 | 空或指向 event |
| **高阶事实（higher-order）** | `true` | LLM 对多条一阶事实做归纳/合成得到的抽象结论 | 指向支撑它的一阶 fact_id 列表 |

**一阶事实**就是前面描述的标准三元组：从某个 Event 抽出的 `subject-predicate-object`，`supports` 指向产生它的 Event。它们是知识图谱的基础边。

**高阶事实**是在抽取完成后由 Higher-Order 模块（详见[第24章](24-higher-order)）异步合成的：系统收集同一 `subject_id` 下的多条相关一阶事实，调用 LLM 归纳出更抽象/概括性的结论，再以一条新的 fact 行写入，`is_higher_order=true` 并通过 `evidence_fact_ids` 反向指向支撑它的一阶事实。`higher_order_reasoning` 字段记录 LLM 的推理过程，便于溯源与审计。

这样设计的好处是：高阶结论与一阶事实共用同一张表、同一套检索与图遍历路径，但通过 `is_higher_order` 与 `evidence_fact_ids` 维持了清晰的溯源链——可以随时回到它所依据的一阶证据。检索/图遍历可按需包含或排除高阶事实。

## 第4层：Beliefs（信念层）

Beliefs 是 Facts 聚合而成的**概率断言**，回答"我们目前怎么看 X"。

```sql
CREATE TABLE beliefs (
    belief_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    about_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    stance         TEXT NOT NULL,  -- supports|likely_true|uncertain|likely_false|contradicts
    claim          TEXT NOT NULL,
    confidence     FLOAT NOT NULL,
    confidence_interval JSONB,       -- [lower, upper]
    supports       UUID[] NOT NULL DEFAULT '{}',  -- → fact_id / event_id
    
    -- 双时态
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,
    recorded_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to    TIMESTAMPTZ,
    last_revised_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Belief 的 5 种立场：

| 立场 | 含义 |
|------|------|
| `supports` | 证据支持 |
| `likely_true` | 很可能为真 |
| `uncertain` | 不确定 |
| `likely_false` | 很可能为假 |
| `contradicts` | 反驳/矛盾 |

## 第5层：Understanding（理解层）

Understanding 层对 Beliefs 做**高阶概念合成**，产生主题级概括。

```sql
CREATE TABLE concepts (
    concept_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope        TEXT NOT NULL,
    name         TEXT NOT NULL,
    topic        TEXT,              -- 主题分类
    version      INT NOT NULL DEFAULT 1,
    summary      TEXT,              -- LLM 合成的概括
    supports     UUID[] NOT NULL DEFAULT '{}',  -- → fact_id / belief_id / episode_id
    related      JSONB NOT NULL DEFAULT '[]',    -- [{name, relation}]
    confidence   FLOAT NOT NULL DEFAULT 0.5,
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

related 图支持 5 种关系：
```
specializes, generalizes, contrasts, co_occurs, causes
```

## 数据派生关系

```{mermaid}
graph TB
    subgraph 写入
        E[Events] -->|extract| F[Facts]
        E -->|segment| EP[Episodes]
    end
    
    subgraph 推理
        F -->|aggregate| B[Beliefs]
        B -->|synthesize| U[Understanding]
    end
    
    subgraph 读取
        F -->|6通道检索| R[Recall]
        EP -->|Case 检索| R
        B -->|信念检索| R
        U -->|概念检索| R
    end
    
    R -->|StratifiedPack| A[Answer / Agent]
```

## 分层架构的优势

1. **可溯源**：每个 Fact 的 `supports` 指向产生它的 Event（"这个结论来自哪条记录"）
2. **双视角**：同时回答"现在什么是真的"和"当时我们怎么以为的"
3. **渐进合成**：原始事件 → 结构化事实 → 概率断言 → 概念概括，每层都是下层的高阶抽象
4. **按需存储**：不需要的层可以不生成（如关闭 Understanding 合成不影响检索）

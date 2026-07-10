# 第13章 Higher-Order 高阶归纳 — 从一阶事实到抽象结论

## 13.1 概述

Dreaming 与 Higher-Order 是记忆自演化的两条互补热路径：

| 机制 | 方向 | 操作 | 典型产物 |
|------|------|------|----------|
| Dreaming（第12章） | 减法 | 去重 / 合并 / 归档 | 三元组收敛、冷数据下沉 |
| **Higher-Order** | **加法** | **归纳 / 抽象** | **从一阶事实合成高阶结论** |

Dreaming 做的是"压缩"——把语义重复的三元组合并；Higher-Order 做的是"升华"——把零散的一阶观察（first-order facts）抽象成行为模式、故障规律、性格特质等高阶结论。设计借鉴自 MindMemOS 的 `_schema_higher_order.py`：以 evidence-driven LLM summarization 为核心，用一阶事实作为证据，让 LLM 归纳出 `order=2` 谓词描述的抽象断言。

一个直观的例子：

> 某设备在过去半年累积了 10 条一阶事实 `(device_X, caused_by, "PSU_aging")`、`(device_X, caused_by, "capacitor_leak")` ……散落在多次 incident 事件里。召回时返回 10 条平铺事件既冗余又难以诊断。Higher-Order 在后台用 LLM 把它们归纳成一条高阶事实：
>
> `(device_X, failure_mode, "recurrent PSU/capacitor hardware fault pattern")`
>
> 一条抽象结论替代 10 条零散事件，诊断效率显著提升。

高阶事实与一阶事实**同表共存**（`facts` 表），用 `is_higher_order` 列区分层级；高阶事实额外携带 `higher_order_reasoning`（LLM 归纳推理过程）和 `evidence_fact_ids`（指向一阶证据的 UUID 数组），形成可追溯的证据链。

```{mermaid}
graph LR
    subgraph 一阶层[一阶事实 order=1]
        F1["(device_X, caused_by, PSU_aging)"]
        F2["(device_X, caused_by, capacitor_leak)"]
        F3["(device_X, occurred_at, 2026-03)"]
    end

    subgraph 高阶层[高阶事实 order=2]
        H["(device_X, failure_mode,<br/>recurrent PSU/capacitor fault)<br/>reasoning: ...<br/>evidence: [F1, F2, F3]"]
    end

    F1 -.evidence.-> H
    F2 -.evidence.-> H
    F3 -.evidence.-> H
```

## 13.2 一阶 vs 高阶事实

`facts` 表通过三列扩展实现**同表两级结构**（`schema.sql:431-435`）：

```sql
-- facts 表加高阶标记
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS is_higher_order BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS higher_order_reasoning TEXT;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS evidence_fact_ids UUID[] NOT NULL DEFAULT '{}';
```

| 列 | 一阶事实 | 高阶事实 |
|----|----------|----------|
| `is_higher_order` | `false`（默认） | `true` |
| `higher_order_reasoning` | NULL | LLM 归纳推理过程（一句话解释"为什么从这些 evidence 能得出此结论"） |
| `evidence_fact_ids` | `{}`（空数组） | 指向一阶证据 `fact_id` 的 UUID 数组 |
| `extraction_model` | LLM 模型名 | `'higher-order'`（固定标记，标识由高阶归纳产生） |
| `predicate` | `order=1` 谓词（`caused_by`、`located_in` …） | `order=2` 谓词（`failure_mode`、`behavior_pattern` …） |

高阶事实的写入见 `higher_order.py` 的 generate_higher_order 写入段的 INSERT 语句——`confidence=0.7`、`assertion_status='confirmed'`、`object_type='literal'`，并显式填入 `is_higher_order=true`、`higher_order_reasoning`、`evidence_fact_ids`。版本化更新时，旧高阶 fact 不会被 DELETE，而是 `recorded_to=now()` 软关闭，新版本 INSERT，保留完整演化历史（见 13.4）。

为加速热路径检索，`schema.sql:448-450` 专门建了部分索引：

```sql
CREATE INDEX IF NOT EXISTS idx_facts_higher_order
    ON cortex.facts (scope, subject_id) WHERE is_higher_order = true AND recorded_to IS NULL;
```

## 13.3 predicate_definitions 表

高阶归纳需要一个**谓词本体**告诉 LLM"哪些是 `order=2` 的高阶谓词、它们的语义和示例"。这个本体从 `ontology.py` 的硬编码迁移到了 DB 可配表（`schema.sql:437-445`）：

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

关键列：

- **`prop_order`**：`1` = 一阶谓词（从事件/文档直接抽取），`2` = 高阶谓词（只能由 Higher-Order 归纳产生）。`generate_higher_order` 第一步就查 `WHERE prop_order=2` 决定 LLM 能用哪些谓词（`higher_order.py` 的谓词加载段）。
- **`category`**：谓词类别枚举。一阶侧覆盖 `structural`/`causal`/`diagnostic`/`state`，高阶侧用 `higher_order`。这是多维度本体分类，不与 `prop_order` 冗余——一个 `order=2` 谓词的 `category` 既可以是 `higher_order` 也可以归到 `causal` 表示它是关于因果规律的抽象。
- **`cardinality`**：`single`（单值，如 `failure_mode` 一个实体一个主导模式）或 `multi`（多值）。
- **`description` / `example`**：注入 LLM prompt，让模型理解谓词语义。

### seed_predicate_definitions()

`maintenance.py` 的 `seed_predicate_definitions()` 把 `ontology.py` 硬编码谓词**幂等 upsert** 到该表：

```python
def seed_predicate_definitions() -> int:
    """把 ontology.py 的硬编码谓词预置到 predicate_definitions 表(一阶,order=1)。幂等。返回 upsert 数。"""
    cat_map = {}
    for p in STRUCTURAL_PREDICATES: cat_map[p] = "structural"
    for p in CAUSAL_PREDICATES:     cat_map[p] = "causal"
    for p in DIAGNOSTIC_PREDICATES: cat_map[p] = "diagnostic"
    for p in STATE_PREDICATES:      cat_map[p] = "state"
    n = 0
    with session_scope() as conn:
        for pred, cat in cat_map.items():
            card = PREDICATE_CARDINALITY.get(pred, "multi")
            r = conn.execute(text("""
                INSERT INTO predicate_definitions (predicate, category, prop_order, cardinality)
                VALUES (:p, :c, 1, :card)
                ON CONFLICT (predicate) DO UPDATE SET category=:c, cardinality=:card
            """), {"p": pred, "c": cat, "card": card})
            n += r.rowcount or 0
    return n
```

注意：seed 只预置 **`prop_order=1`** 的一阶谓词。`order=2` 的高阶谓词（`failure_mode`、`behavior_pattern`、`personality_trait` 等）需要管理员手动 INSERT 或后续迁移脚本补入。这是有意为之——高阶谓词代表"系统想抽象出什么样的结论"，属于业务语义决策，不应与抽取本体自动绑定。

## 13.4 generate_higher_order 流程

`higher_order.py:29-134` 的 `generate_higher_order(entity_id, *, new_fact_id=None)` 是整个机制的核心。它接受一个实体 ID（可选传入触发本次归纳的新 `fact_id`），返回 `{"synthesized": n, ...}` 或带 `skipped` 原因。

### 流程图

```{mermaid}
flowchart TD
    A([generate_higher_order entity_id]) --> B{enabled?}
    B -- no --> S1["skip: disabled"]
    B -- yes --> C{entity 存在<br/>且未 merged_into?}
    C -- no --> S2["skip: not found/merged"]
    C -- yes --> D["加载一阶 live facts<br/>ORDER BY new_fact_id DESC,<br/>access_count DESC, valid_from DESC<br/>LIMIT lookback_facts"]
    D --> E{len ≥ min_evidence_count?<br/>默认 ≥2}
    E -- no --> S3["skip: insufficient evidence"]
    E -- yes --> F{Σ access_count<br/>≥ min_access_count?<br/>默认 ≥2}
    F -- no --> S4["skip: insufficient access_count"]
    F -- yes --> G{order=2 谓词存在?<br/>predicate_definitions}
    G -- no --> S5["skip: no order=2 predicates"]
    G -- yes --> H{LLM tier 配置?<br/>最后检查}
    H -- no --> S6["skip: LLM not configured"]
    H -- yes --> I["加载已有高阶 facts<br/>新 session"]
    I --> J["LLM synthesis<br/>HIGHER_ORDER_SYNTHESIZE"]
    J --> K{解析 updates?}
    K -- 空/异常 --> S7["skip: no updates / LLM error"]
    K -- 有 updates --> L["遍历 updates:<br/>create = INSERT 新高阶 fact<br/>update = 旧 fact recorded_to=now + INSERT 新版本"]
    L --> M{n > 0?}
    M -- yes --> N["emit_lifecycle<br/>higher_order_generated"]
    N --> O([return synthesized=n])
    M -- no --> O
```

### 13.4.1 检查门序列（gate ordering）

设计上的关键细节是**检查顺序**——所有 DB 检查先于 LLM key 检查（`higher_order.py` 的门控序列）：

```python
# LLM key 检查放在最后(DB 检查之后)
if not services.llm_configured(cfg.higher_order.llm_tier):
    return {"synthesized": 0, "skipped": f"LLM tier '{cfg.higher_order.llm_tier}' not configured"}
```

理由：LLM key 检查涉及配置/网络，应放在最便宜的 DB 检查之后，避免每次 cold-start skip 都无谓地触碰 LLM 配置层。

### 13.4.2 证据窗加载（evidence window）

`higher_order.py` 加载该实体的一阶 live facts 作为 evidence：

```python
# 取该实体的一阶 live facts(按 access_count + valid_from DESC,信号总线:高频优先)
# M4:若有 new_fact_id(触发本归纳的新 fact),强制它排在证据窗首位(即便 access_count=0)
nf_order = "(f.fact_id = CAST(:nf AS uuid)) DESC, " if new_fact_id else ""
first_order = conn.execute(text(f"""SELECT fact_id::text, predicate,
    coalesce(o.canonical_name, f.object_value->>'value') AS object_text,
    f.valid_from::text, f.assertion_status,
    coalesce((SELECT max(e.access_count) FROM events e WHERE e.event_id = ANY(f.supports)), 0) AS access_count
    FROM facts f LEFT JOIN entities o ON o.entity_id=f.object_entity_id
    WHERE f.subject_id=CAST(:e AS uuid) AND f.scope=:s
      AND f.is_higher_order=false AND f.recorded_to IS NULL AND f.valid_to IS NULL
    ORDER BY {nf_order} access_count DESC, f.valid_from DESC
    LIMIT :n"""), ...)
```

两个要点：

1. **`access_count` 来自信号总线**——通过子查询 `SELECT max(e.access_count) FROM events e WHERE e.event_id = ANY(f.supports)` 聚合该 fact 支撑事件的最大访问计数。高访问计数意味着该事实在召回中被反复命中，是"值得归纳的高价值事实"。这让高阶归纳与召回热度联动（详见 13.7 冷启动保护）。
2. **M4 fix：`new_fact_id` 强制进证据窗首位**——刚抽取触发本次归纳的新事实 `access_count=0`，正常排序会排在末尾甚至被 `LIMIT` 截掉。`(f.fact_id = CAST(:nf AS uuid)) DESC` 把它强制顶到首位，保证"触发源"一定进入 LLM 视野。这修正了早期版本"新事实触发归纳却没参与归纳"的 bug。

### 13.4.3 冷启动双重门槛

```python
if len(first_order) < cfg.higher_order.min_evidence_count:      # 默认 2
    return {"synthesized": 0, "skipped": f"insufficient evidence(...)"}
total_ac = sum(r[5] for r in first_order)
if total_ac < cfg.higher_order.min_access_count:                 # 默认 2
    return {"synthesized": 0, "skipped": f"insufficient access_count(...)"}
```

两道门槛缺一不可：**证据数量**（至少 2 条一阶事实）+ **证据热度**（累计访问计数至少 2）。后者对齐信号总线——只有被召回系统真正"用过"的事实才有资格被抽象。

### 13.4.4 LLM synthesis

通过所有门后，构造 `material` JSON（实体信息 + evidence + 已有高阶值 + `order=2` 谓词定义）调用 `HIGHER_ORDER_SYNTHESIZE` prompt（`prompts.py:633-663`）。Prompt 核心规则：

- **只从 evidence 归纳**——不编造、不引入 evidence 没有的信息。
- **用 `order=2` 谓词**——`behavior_pattern` / `failure_mode` / `personality_trait` 等。
- **`action=create|update`**——新建或修正/强化已有结论。
- **必须给 `reasoning`**——归纳推理过程一句话，写入 `higher_order_reasoning` 列。

输出格式：

```json
{
  "updates": [
    {
      "predicate": "failure_mode",
      "action": "update",
      "value": "recurrent PSU/capacitor hardware fault pattern",
      "reasoning": "3 次 incident 均指向电源/电容老化，故障模式一致"
    }
  ]
}
```

### 13.4.5 版本化 apply updates

`higher_order.py` 遍历 updates 写库，采用**软关 + 新版本**策略（而非 in-place UPDATE）：

```python
for upd in updates:
    action = upd.get("action", "create")
    predicate = upd.get("predicate")
    ...
    if action == "update":
        # 找已有的同 predicate 高阶 fact,软关 + 新版本
        old = conn.execute(text("""SELECT fact_id::text FROM facts
            WHERE subject_id=CAST(:e AS uuid) AND scope=:s AND predicate=:p
              AND is_higher_order=true AND recorded_to IS NULL LIMIT 1"""), ...)
        if old:
            conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"), {"f": old[0]})
    # INSERT 新高阶 fact
    conn.execute(text("""INSERT INTO facts (scope, subject_id, predicate, object_type, object_value,
        valid_from, confidence, assertion_status, evidence_span, supports, extraction_model,
        is_higher_order, higher_order_reasoning, evidence_fact_ids)
        VALUES (:s, CAST(:e AS uuid), :p, 'literal', CAST(:ov AS jsonb),
        now(), 0.7, 'confirmed', :es, '{}', 'higher-order',
        true, :reasoning, CAST(:evid AS uuid[]))"""), ...)
    n += 1
```

`update` 动作会把旧高阶 fact 的 `recorded_to=now()` 软关闭（recension 层关闭），再 INSERT 一条新版本——保留完整的高阶结论演化轨迹，可回溯任何时刻的结论版本。这与 Facts 表的双时态语义完全一致（见第17章）。

成功归纳后 `emit_lifecycle(kind="higher_order_generated", ...)` 发生命周期事件（`higher_order.py` 收尾段）。

## 13.5 触发机制：extract 后异步 enqueue

Higher-Order 不由用户同步调用触发，而是**抽取管线 extract 完成后异步 enqueue** 一个 `higher_order` job。抽取管线有两条路径，都内置了这个触发点：

### 13.5.1 LLM 抽取路径（`extraction/pipeline.py` 的 `extract_event` 收尾阶段）

```python
# ── Higher-Order 异步触发:extract 后对该 event 涉及的实体 enqueue 高阶归纳 ──
if cfg.higher_order.enabled and fact_ids:
    try:
        subj_ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT subject_id::text FROM facts WHERE fact_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": "{" + ",".join(fact_ids) + "}"}).fetchall()]
        for sid in subj_ids:
            conn.execute(text("""INSERT INTO jobs (job_type, scope, priority, payload)
                VALUES ('higher_order', :s, -1, CAST(:p AS jsonb))"""),
                {"s": scope, "p": json.dumps({"entity_id": sid, "new_fact_id": fact_ids[0] if fact_ids else None})})
    except Exception:  # noqa: BLE001  高阶触发不阻塞 extract
        pass
```

发生在 `embed_status='done'` 之后（`extraction/pipeline.py` 的 `extract_event` 收尾）。对本次抽取产生的每个 `subject_id` 各 enqueue 一个 job，payload 携带 `entity_id` 和 `new_fact_id`（首个新 fact，用于 13.4.2 的 M4 强制进证据窗）。

### 13.5.2 三元组直写路径（`extraction/pipeline.py` 的 `_direct_write_triple`）

```python
# Higher-Order 异步触发(triple 直写路径)
if cfg.higher_order.enabled and res.get("fact_ids"):
    try:
        fids = res["fact_ids"]
        subj_ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT subject_id::text FROM facts WHERE fact_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": "{" + ",".join(fids) + "}"}).fetchall()]
        for sid in subj_ids:
            conn.execute(text("""INSERT INTO jobs (job_type, scope, priority, payload)
                VALUES ('higher_order', :s, -1, CAST(:p AS jsonb))"""),
                {"s": ev.scope, "p": json.dumps({"entity_id": sid, "new_fact_id": fids[0]})})
    except Exception:  # noqa: BLE001
        pass
```

逻辑与 LLM 路径完全对称，只是触发点在 `emit_lifecycle(..., model="triple-direct")` 之后。

### 13.5.3 为什么用 inline INSERT 而非 enqueue_job

两处都用 `INSERT INTO jobs ... VALUES ('higher_order', :s, -1, ...)` 而非调 `enqueue_job()`——因为 extract 本身已在一个 `session_scope` 内，直接同事务写 job 表避免嵌套事务开销，且 extract 失败回滚时 job 也一起回滚（不会留下"无对应 extract 的孤儿高阶 job"）。`priority=-1` 表示低优先级，不与用户路径的 extract/erase job 抢资源。

Worker 端（`worker/runner.py` 的 `_dispatch` higher_order 分支）处理 `higher_order` job：

```python
if jt == "higher_order" and scope:
    from ...memory.higher_order import generate_higher_order
    payload = job.get("payload") or {}
    return generate_higher_order(payload.get("entity_id", ""), new_fact_id=payload.get("new_fact_id"))
```

Worker 系统详见第20章。

## 13.6 召回集成：higher_order 层

`retrieval/pipeline.py` 的 `_assemble_pack` 在 `StratifiedPack` 里新增了 `higher_order` 层，与 `events`/`facts`/`beliefs` 并列：

```python
# higher_order 层:命中实体的 is_higher_order=true facts(归纳性结论 + 证据链)
higher_order: List[Dict[str, Any]] = []
if subj_ids:
    # H5:与其他层契约一致——用 _scope_filter(支持 holistic/descend)+ temporal_where + valid_to IS NULL
    # _scope_filter 返回形如 "scope = :scope0" / "scope = ANY(:scopes)" / "(scope = :scope0 OR scope LIKE :scopep)"
    # 用正则给列名 scope 加 f. 前缀,但不碰 bind param 名(:scope0 / :scopes / :scopep)
    import re as _re
    sf_raw = scope_frag_sql or "scope = :s"
    sf = _re.sub(r'(?<![.\w])scope(?=\s*[=L])', 'f.scope', sf_raw)
    sp = dict(scope_params) if scope_params else {"s": scope}
    tw = temporal_where or ""
    tp = dict(temporal_params) if temporal_params else {}
    sp.update(tp)
    sp["a"] = "{" + ",".join(subj_ids) + "}"
    horows = conn.execute(text(f"""SELECT f.fact_id::text, f.predicate, f.object_value->>'value' AS value,
        f.higher_order_reasoning, f.evidence_fact_ids::text[], f.confidence,
        s.canonical_name AS subject_name, f.valid_from::text
        FROM facts f JOIN entities s ON s.entity_id=f.subject_id
        WHERE {sf} AND f.is_higher_order=true AND f.recorded_to IS NULL AND f.valid_to IS NULL
          {tw} AND f.subject_id = ANY(CAST(:a AS uuid[])) LIMIT 10
    """), sp).fetchall()
    higher_order = [{"fact_id": r[0], "predicate": r[1], "value": r[2],
                     "reasoning": r[3], "evidence_fact_ids": list(r[4] or []),
                     "confidence": r[5], "subject_name": r[6], "valid_from": r[7]} for r in horows]
```

返回的 `StratifiedPack.layers` 结构（`pipeline.py:544`）：

```python
"layers": {"events": events, "facts": facts_out, "beliefs": beliefs, "higher_order": higher_order}
```

三个设计要点：

1. **H5 一致性修正**——早期版本该层没用 `_scope_filter` 和 `temporal_where`，导致 holistic/descend scope 和时间旅行查询时高阶层行为与其他层不一致。现版用正则 `_re.sub(r'(?<![.\w])scope(?=\s*[=L])', 'f.scope', sf_raw)` 给 scope 列名加 `f.` 前缀（因 SQL 里 facts 表别名为 `f`），同时保留 bind param 名不变，让 scope 片段和时间片段与其他层共享同一组参数。
2. **过滤契约**——`is_higher_order=true AND recorded_to IS NULL AND valid_to IS NULL`，与一阶 facts 层的"只返回活跃 fact"契约一致（recension 未关闭 + 时间未失效）。
3. **LIMIT 10**——高阶结论本身是浓缩产物，每个实体通常只有少数几条，10 条足够覆盖；过多会挤占召回 token 预算。

召回系统的分层结构与 RRF 融合详见第10-12章。

## 13.7 冷启动保护

Higher-Order 默认**禁用**（`HigherOrderCfg.enabled=False`），并由 `min_access_count` 门禁。原因在于 evidence 的质量依赖信号总线的成熟度：

- **`access_count` 的来源**——来自 `events` 表的 `access_count` 列（`schema.sql:26`），由召回系统每次命中该事件时递增。它反映"这条一阶事实在真实查询中被验证过多少次"。
- **冷启动期的问题**——系统刚部署时，事件 `access_count` 全为 0，此时若强行归纳，LLM 只能基于"未被任何查询验证过"的原始抽取结果做抽象，极易产生**幻觉式高阶结论**（把偶发的抽取噪声归纳成"模式"）。
- **双重门槛的语义**——`min_evidence_count=2` 保证证据数量，`min_access_count=2` 保证证据质量（至少被召回系统验证过）。两者都满足才允许 LLM 介入。
- **`min_access_count` 对齐信号总线**——这与 Dreaming 的"只合并高频访问的相似三元组"、Methylation 的"冷数据下沉"是同一套热度信号体系。整个记忆自演化系统共享 `access_count` 作为"数据价值"的统一度量。

因此启用顺序通常是：先跑系统积累召回数据 → 观察 `events.access_count` 分布 → 确认有足够热度后 `higher_order.enabled=true` + 手动 INSERT 几条 `order=2` 谓词定义 → 让抽取管线开始 enqueue 高阶 job。

## 13.8 API 与 MCP

### 13.8.1 管理接口 `POST /v1/admin/higher-order`

`app.py` 的 higher-order admin 端点，需要 `admin_auth`，承担两种职责：

```python
@app.post("/v1/admin/higher-order")
def admin_higher_order(body: dict, actor: str = Depends(admin_auth)):
    from ...memory.higher_order import generate_higher_order, list_higher_order_facts
    from ...memory.maintenance import seed_predicate_definitions
    scope = body.get("scope")
    entity_id = body.get("entity_id")
    if not scope:
        raise HTTPException(422, "scope required")
    if body.get("seed_predicates"):
        n = seed_predicate_definitions()
        return {"seeded": n}
    if not entity_id:
        raise HTTPException(422, "entity_id required (or seed_predicates=true)")
    return generate_higher_order(entity_id)
```

- **`seed_predicates=true`**：调用 `seed_predicate_definitions()` 把 `ontology.py` 一阶谓词 upsert 到 `predicate_definitions` 表。启用 Higher-Order 前的初始化步骤。
- **`entity_id=...`**：同步触发某实体的 `generate_higher_order`（通常用于调试或手动补归纳，生产路径走异步 job）。

请求示例：

```bash
# 初始化谓词本体
curl -X POST http://localhost:8000/v1/admin/higher-order \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"scope":"default","seed_predicates":true}'
# {"seeded":42}

# 手动触发某实体高阶归纳
curl -X POST http://localhost:8000/v1/admin/higher-order \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"scope":"default","entity_id":"<uuid>"}'
# {"synthesized":1,"entity_id":"...","entity_name":"device_X"}
```

### 13.8.2 查询接口 `GET /v1/higher-order`

`app.py` 的 higher-order 查询端点，普通 `auth` 即可，委托 `list_higher_order_facts`（`higher_order.py`）：

```python
@app.get("/v1/higher-order")
def higher_order_list(scope: str, entity_id: Optional[str] = Query(None),
                      limit: int = Query(50, le=200), actor: str = Depends(auth)):
    from ...memory.higher_order import list_higher_order_facts
    return {"items": list_higher_order_facts(scope, entity_id, limit)}
```

可选按 `entity_id` 过滤，返回该实体或全 scope 的高阶事实（含 `reasoning` 和 `evidence_fact_ids`，便于追溯归纳依据）。

### 13.8.3 MCP 工具 `higher_order_generate`

`mcp_server.py` 的 higher-order MCP 工具：

```python
@mcp.tool()
def higher_order_generate(entity_id: str,
                          scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Generate higher-order conclusions for an entity from its first-order facts.

    Triggers LLM-based evidence-driven summarization: gathers the entity's recent
    first-order facts as evidence, then induces higher-order conclusions (e.g.
    behavior patterns, failure modes) using order=2 predicate definitions.
    Requires higher_order.enabled=true and LLM key configured.
    """
    from ...memory.higher_order import generate_higher_order
    return generate_higher_order(entity_id)
```

MCP 工具语义与 admin API 的 `entity_id` 路径一致，供 agent 主动触发归纳。MCP server 详见第19章。

## 13.9 配置

`HigherOrderCfg`（`config.py:146-152`）：

```python
class HigherOrderCfg(BaseModel):
    """高阶归纳:从一阶 fact LLM 归纳高阶结论(order=2 谓词)。"""
    enabled: bool = False              # 默认关
    min_evidence_count: int = 2        # 至少 N 条一阶 fact 才归纳
    min_access_count: int = 2          # 一阶 fact 累计 access_count 达此值才归纳
    lookback_facts: int = 10           # 取最近 N 条一阶 fact 作 evidence
    llm_tier: str = "synthesis"
```

| 字段 | 默认 | 含义 |
|------|------|------|
| `enabled` | `False` | 全局开关。关时 extract 不 enqueue、`generate_higher_order` 直接 skip |
| `min_evidence_count` | `2` | 证据数量下限，证据窗 facts 数 < 此值则 skip |
| `min_access_count` | `2` | 证据热度下限，证据窗 facts 的 `access_count` 之和 < 此值则 skip |
| `lookback_facts` | `10` | 证据窗大小，`LIMIT` 取最近 N 条一阶事实 |
| `llm_tier` | `"synthesis"` | LLM tier 名（见 LLM 配置），走 `services.llm_chat` |

YAML 配置示例：

```yaml
higher_order:
  enabled: true
  min_evidence_count: 3      # 提高证据门槛，减少噪声归纳
  min_access_count: 5        # 要求证据被反复验证
  lookback_facts: 20         # 更大证据窗，捕捉长周期模式
  llm_tier: "synthesis"
```

调参直觉：

- **想更保守**（生产、防幻觉）→ 抬高 `min_access_count`（如 5-10），让只有高频验证过的事实才进归纳。
- **想更激进**（实验、快速抽象）→ 降低 `min_evidence_count` 到 2、`min_access_count` 到 0，但注意会放大抽取噪声。
- **长周期模式**（月度故障规律）→ 抬高 `lookback_facts` 到 30-50，让证据窗覆盖更长时间跨度。

---

至此，记忆自演化的"减法"（Dreaming，第12章）与"加法"（Higher-Order，本章）闭环完成。前者收敛冗余、后者升华抽象，两者共同维护一个既精简又富有洞察力的知识图谱。

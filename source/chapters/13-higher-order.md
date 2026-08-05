# 第13章 Higher-Order 高阶归纳 — 从一阶事实到抽象结论

````{admonition} 重大重构:认知晋升门 + evidence_quality + candidate tier
:class: important
本章曾以"`access_count` 热度门控 + 直接写 `confirmed` 高阶 fact"为核心。两项均已过时,请以下述为准:
1. **不再读 `access_count`**:证据窗排序与门控改用 `facts.evidence_quality`(`min_access_count` 已 `deprecated=0`,使用热度不参与认知晋升)。唯一门控是证据数量 `min_evidence_count`。
2. **不再直接写 `confirmed`**:LLM 归纳只能生成 `assertion_status='hypothesized' / knowledge_tier='candidate'` 的待审 fact + 同步落 `evolution_candidates(status='pending', proposed_action='promote')`。需人工 `review_candidate` 审批后才晋升 `verified`。召回层只返回 `knowledge_tier='verified'` 的高阶 fact。
3. 认知晋升质量由**人工审批门**(检查 confirmed + 独立闭环 Case + 回归证据)保证,不再由召回热度保证。
````

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

`facts` 表通过三列扩展实现**同表两级结构**（`schema.sql:891-893`）：

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

高阶事实的写入见 `higher_order.py` 的 generate_higher_order 写入段的 INSERT 语句——`confidence=candidate_confidence`(默认 0.5)、`assertion_status='hypothesized'`、`knowledge_tier='candidate'`、`object_type='literal'`，并显式填入 `is_higher_order=true`、`higher_order_reasoning`、`evidence_fact_ids`。注意生成阶段只产 candidate tier 待审 fact,需人工审批(`evolution.review_candidate`)通过后才晋升 `verified`(见 13.4.5)。

为加速热路径检索，`schema.sql:907-908` 专门建了部分索引：

```sql
CREATE INDEX IF NOT EXISTS idx_facts_higher_order
    ON cortex.facts (scope, subject_id) WHERE is_higher_order = true AND recorded_to IS NULL;
```

## 13.3 predicate_definitions 表

高阶归纳需要一个**谓词本体**告诉 LLM"哪些是 `order=2` 的高阶谓词、它们的语义和示例"。这个本体从 `ontology.py` 的硬编码迁移到了 DB 可配表（`schema.sql:896-903`）：

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
    C -- yes --> D["加载一阶正向 live facts<br/>ORDER BY new_fact_id DESC,<br/>evidence_quality DESC, valid_from DESC<br/>LIMIT lookback_facts"]
    D --> E{len ≥ min_evidence_count?<br/>默认 ≥2}
    E -- no --> S3["skip: insufficient evidence"]
    E -- yes --> G{order=2 谓词存在?<br/>predicate_definitions}
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
# 取该实体的一阶正向 live facts(按 evidence_quality + valid_from DESC;使用热度不参与晋升)
# 只取 polarity='positive' AND assertion_status IN ('observed','confirmed');ruled_out/rejected/negative 不得作归纳依据
# M4:若有 new_fact_id(触发本归纳的新 fact),强制它排在证据窗首位(即便 evidence_quality=0)
nf_order = "(f.fact_id = CAST(:nf AS uuid)) DESC, " if new_fact_id else ""
first_order = conn.execute(text(f"""SELECT fact_id::text, predicate,
    coalesce(o.canonical_name, f.object_value->>'value') AS object_text,
    f.valid_from::text, f.assertion_status,
    coalesce(f.evidence_quality,0.0) AS evidence_quality,
    coalesce(f.case_id,(SELECT max(e.case_id) FROM events e WHERE e.event_id = ANY(f.supports))) AS case_id
    FROM facts f LEFT JOIN entities o ON o.entity_id=f.object_entity_id
    WHERE f.subject_id=CAST(:e AS uuid) AND f.scope=:s
      AND f.is_higher_order=false AND f.recorded_to IS NULL AND f.valid_to IS NULL
      AND f.polarity='positive' AND f.assertion_status IN ('observed','confirmed')
    ORDER BY {nf_order} evidence_quality DESC, f.valid_from DESC
    LIMIT :n"""), ...)
```

三个要点：

1. **按 `evidence_quality` 排序(非 access_count)**——证据质量高的 fact 优先进入证据窗。`access_count` 热度不再参与认知晋升门控(`min_access_count` 已 `deprecated=0`)。
2. **正向证据过滤**:只取 `polarity='positive' AND assertion_status IN ('observed','confirmed')`。被 `wrong` 反馈推翻(`ruled_out`)或负向(`negative`)的 fact 不得作为归纳依据——这是质量护栏。
3. **M4 fix:`new_fact_id` 强制进证据窗首位**——刚抽取触发本次归纳的新事实 `evidence_quality=0`,正常排序会排在末尾甚至被 `LIMIT` 截掉。`(f.fact_id = CAST(:nf AS uuid)) DESC` 把它强制顶到首位,保证"触发源"一定进入 LLM 视野。

### 13.4.3 证据数量门控(单一门槛)

```python
if len(first_order) < cfg.higher_order.min_evidence_count:      # 默认 2
    return {"synthesized": 0, "skipped": f"insufficient evidence({len(first_order)} < {min_evidence_count})"}
# 注意:已无 access_count 热度门槛;min_access_count deprecated=0,使用热度不参与认知晋升
```

单一门槛:**证据数量**(至少 `min_evidence_count` 条一阶正向事实)。使用热度(`access_count`/`retrieval_count`)不参与认知晋升——晋升质量由后续人工审批门保证(见 13.4.5)。

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

### 13.4.5 生成 candidate tier fact + evolution_candidates(不直接改 verified graph)

````{admonition} 关键设计变更
:class: important
LLM 归纳**不再直接写 `assertion_status='confirmed'` 高阶 fact**。现在只生成 `assertion_status='hypothesized' / knowledge_tier='candidate'` 的待审 fact,并同步落一条 `evolution_candidates(status='pending', proposed_action='promote')`。旧 fact 的软关也推迟到审批阶段。需人工 `review_candidate` 审批通过后才晋升 `verified`。
````

```python
allowed_predicates = {row[0] for row in ho_predicates}  # order=2 谓词白名单
for upd in updates:
    predicate = upd.get("predicate")
    if not predicate or predicate not in allowed_predicates:
        log.warning("higher_order rejected unapproved predicate: %s", predicate)
        continue   # LLM 输出的 predicate 必须在 order=2 定义里,否则 reject
    supersedes_fact_id = None
    if action == "update":
        # 找已有的同 predicate 高阶 fact;仅记录 proposed supersession
        # 生成阶段绝不关闭现有 verified assertion
        old = conn.execute(text("""SELECT fact_id::text FROM facts
            WHERE subject_id=... AND predicate=:p AND is_higher_order=true
              AND recorded_to IS NULL LIMIT 1"""), ...)
        if old:
            supersedes_fact_id = old[0]   # 只记下,不软关
    # INSERT candidate tier fact(LLM 只能生成 candidate)
    new_fact_id = conn.execute(text("""INSERT INTO facts (...,assertion_status,
        extraction_model, knowledge_tier, is_higher_order, higher_order_reasoning, evidence_fact_ids)
        VALUES (...,'hypothesized', 'higher-order', 'candidate', true, :reasoning, ...)"""), ...)
    # 同步落 evolution_candidates 待审
    conn.execute(text("""INSERT INTO evolution_candidates(
        scope,source_type,proposed_action,subject_id,predicate,payload,
        source_fact_ids,status,proposed_confidence,reasoning)
        VALUES(:s,'higher_order','promote',...,CAST(:payload AS jsonb),...,'pending',:confidence,...)"""),
        {"payload": json.dumps({"fact_id": new_fact_id, "value": value,
                                "supersedes_fact_id": supersedes_fact_id})})
    n += 1
```

设计要点:

- **`assertion_status='hypothesized'` + `knowledge_tier='candidate'`**:LLM 归纳只能生成待审候选,不直接进 verified graph。`confidence` 读 `cfg.higher_order.candidate_confidence`(默认 0.5),非硬编 0.7。
- **predicate 白名单**:LLM 输出的 predicate 必须在 `predicate_definitions` 的 `order=2` 集合里,否则 reject。防止 LLM 编造未定义的高阶谓词。
- **update 只记录 `supersedes_fact_id`,不软关旧 fact**:旧 fact 的软关推迟到审批通过后由 `evolution._approve_higher_order` 执行。生成阶段绝不关闭现有 verified assertion。
- **同步落 `evolution_candidates(proposed_action='promote', status='pending')`**:与 Dreaming(第12章)共用同一套审批门。人工 `review_candidate` 通过后才晋升 `confirmed/verified` 并(若有)软关被取代的旧 fact。

成功归纳后 `emit_lifecycle(kind="higher_order_generated", ...)` 发生命周期事件。注意:此时只是"候选已生成",尚未晋升 verified——召回层不会返回 candidate tier 的高阶 fact(见 13.6)。

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
        WHERE {sf} AND f.is_higher_order=true AND f.knowledge_tier='verified'
          AND f.recorded_to IS NULL AND f.valid_to IS NULL
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
2. **过滤契约**——`is_higher_order=true AND knowledge_tier='verified' AND recorded_to IS NULL AND valid_to IS NULL`。注意多一个 `knowledge_tier='verified'`:**召回只返回已审批晋升 verified 的高阶 fact**,candidate tier 的不返回。这与 13.4.5 的"只生成 candidate 待审"配套——未经人工审批的高阶结论不会进入召回结果。若要查看所有高阶 fact(含 candidate),用 `list_higher_order_facts` 接口(`higher_order.py`)。
3. **LIMIT 10**——高阶结论本身是浓缩产物，每个实体通常只有少数几条，10 条足够覆盖；过多会挤占召回 token 预算。

召回系统的分层结构与 RRF 融合详见第10-12章。

## 13.7 质量保护:candidate tier + 人工审批门

Higher-Order 默认**禁用**(`HigherOrderCfg.enabled=False`)。质量保护不再依赖召回热度门(`min_access_count` 已 `deprecated=0`),而是由两层护栏:

- **生成层:candidate tier**。LLM 归纳只能生成 `assertion_status='hypothesized' / knowledge_tier='candidate'` 的待审 fact(见 13.4.5),**不直接进 verified graph**,召回层不返回(见 13.6)。即使 LLM 产生幻觉式高阶结论(把偶发抽取噪声归纳成"模式"),它也只是候选,污染不了知识。
- **晋升层:人工审批门**。candidate 要晋升 `verified` 必须经 `evolution.review_candidate` 人工审批(`evolution.py`)。`_approve_higher_order` 在执行晋升前会检查:断言须 `confirmed`、需至少两个独立闭环 Case 支撑、有回归证据——这是"认知晋升"的硬质量门槛,比召回热度可靠得多。

`min_evidence_count=2`(证据数量门槛)仍保留:至少 2 条一阶正向事实才允许 LLM 介入归纳,防止单条孤证被抽象。

启用顺序通常是:配 LLM key → 手动 INSERT 几条 `order=2` 谓词定义 → `higher_order.enabled=true` → 抽取管线开始 enqueue 高阶 job → 候选落 `evolution_candidates` → 人工审批通过后晋升 verified、进入召回。`min_access_count` 字段保留但 deprecated(=0),使用热度不参与认知晋升。

## 13.8 API 与 MCP

### 13.8.1 管理接口 `POST /v1/admin/higher-order`

`routes/operations.py` 的 higher-order admin 端点（`operations.py:69-82`），挂载于 `app.include_router(operations_router)`，**无鉴权**（actor 固定 `"api"`），承担两种职责：

```python
@router.post("/v1/admin/higher-order")
def admin_higher_order(body: dict):
    scope = body.get("scope")
    entity_id = body.get("entity_id")
    if not scope:
        raise HTTPException(422, "scope required")
    if body.get("seed_predicates"):
        return {"seeded": seed_predicate_definitions()}
    if not entity_id:
        raise HTTPException(422, "entity_id required (or seed_predicates=true)")
    entity_scope = resource_scope("entity", entity_id)
    if entity_scope != scope:
        raise HTTPException(409, "entity and requested scope do not match")
    return generate_higher_order(entity_id)
```

- **`seed_predicates=true`**：调用 `seed_predicate_definitions()` 把 `ontology.py` 的 36 个一阶谓词 upsert 到 `predicate_definitions` 表。启用 Higher-Order 前的初始化步骤。
- **`entity_id=...`**：`resource_scope("entity", entity_id)` 校验实体归属的 scope 与请求 scope 一致（不一致返回 409），再同步触发 `generate_higher_order`（通常用于调试或手动补归纳，生产路径走异步 job）。

请求示例：

```bash
# 初始化谓词本体
curl -X POST http://localhost:8000/v1/admin/higher-order \
  -d '{"scope":"default","seed_predicates":true}'
# {"seeded":36}

# 手动触发某实体高阶归纳
curl -X POST http://localhost:8000/v1/admin/higher-order \
  -d '{"scope":"default","entity_id":"<uuid>"}'
# {"synthesized":1,"entity_id":"...","entity_name":"device_X"}
```

### 13.8.2 查询接口 `GET /v1/higher-order`

`routes/operations.py` 的 higher-order 查询端点（`operations.py:85-91`），同样无鉴权，委托 `list_higher_order_facts`（`higher_order.py`）：

```python
@router.get("/v1/higher-order")
def higher_order_list(
    scope: str,
    entity_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    return {"items": list_higher_order_facts(scope, entity_id, limit)}
```

可选按 `entity_id` 过滤，返回该实体或全 scope 的高阶事实（含 `reasoning` 和 `evidence_fact_ids`，便于追溯归纳依据）。

### 13.8.3 MCP 工具 `higher_order_generate`

`mcp_server.py` 的 higher-order MCP 工具（`mcp_server.py:710-721`）：

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
    _resource_scope(ctx, scope, "entities", "entity_id", entity_id)
    return generate_higher_order(entity_id)
```

MCP 工具语义与 admin API 的 `entity_id` 路径一致，多了一步 `_resource_scope` 校验实体归属 scope，供 agent 主动触发归纳。MCP server 详见第19章。

## 13.9 配置

`HigherOrderCfg`（`config.py:240-247`）：

```python
class HigherOrderCfg(BaseModel):
    """高阶归纳:从一阶 fact LLM 归纳高阶结论(order=2 谓词)。"""
    enabled: bool = False              # 默认关
    min_evidence_count: int = 2        # 至少 N 条一阶 fact 才归纳
    min_access_count: int = 0          # deprecated:使用热度不再作为认知晋升门槛
    lookback_facts: int = 10           # 取最近 N 条一阶 fact 作 evidence
    llm_tier: str = "synthesis"
    candidate_confidence: float = 0.5  # LLM 归纳只能生成待审核候选
```

| 字段 | 默认 | 含义 |
|------|------|------|
| `enabled` | `False` | 全局开关。关时 extract 不 enqueue、`generate_higher_order` 直接 skip |
| `min_evidence_count` | `2` | 证据数量下限，证据窗 facts 数 < 此值则 skip |
| `min_access_count` | `0` | **deprecated**,使用热度不参与认知晋升(晋升由人工审批门保证) |
| `lookback_facts` | `10` | 证据窗大小，`LIMIT` 取最近 N 条一阶事实 |
| `llm_tier` | `"synthesis"` | LLM tier 名（见 LLM 配置），走 `services.llm_chat` |
| `candidate_confidence` | `0.5` | 生成的 candidate tier fact 的 confidence 值 |

YAML 配置示例：

```yaml
higher_order:
  enabled: true
  min_evidence_count: 3      # 提高证据门槛，减少噪声归纳
  lookback_facts: 20         # 更大证据窗，捕捉长周期模式
  llm_tier: "synthesis"
  candidate_confidence: 0.5  # 候选置信度(审批前)
```

调参直觉：

- **想更保守**（生产、防幻觉）→ 抬高 `min_evidence_count`（如 3-5），只有更多独立证据支撑的事实才进归纳。
- **想更激进**（实验、快速抽象）→ 降低 `min_evidence_count` 到 2，但注意会放大抽取噪声。
- **长周期模式**（月度故障规律）→ 抬高 `lookback_facts` 到 30-50，让证据窗覆盖更长时间跨度。

---

至此，记忆自演化的"减法"（Dreaming，第12章）与"加法"（Higher-Order，本章）闭环完成。前者收敛冗余、后者升华抽象，两者共同维护一个既精简又富有洞察力的知识图谱。

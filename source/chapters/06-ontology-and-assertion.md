# 第6章 谓词本体与断言语义

## 概述

cortex 的谓词体系是整个知识图谱的**语言基础**。所有抽取三元组必须使用预定义的谓词，确保图谱关系的一致性和可遍历性。

谓词体系经过 `0008_predicate_cleanup` 迁移清理，从早期 40+ 个谓词收敛为 **36 个核心谓词**，分为**四大类**（结构 / 因果 / 诊断 / 状态），消除了互逆冗余（如 `part_of` ↔ `has_component`、`symptom_of` ↔ `has_symptom` 不再并存，统一方向为父→子 / 结果→原因）。

```{mermaid}
graph TB
    subgraph 谓词四大类（36 个）
        S[结构/配置关系<br/>8 个，静态拓扑]
        C[因果/级联关系<br/>5 个，故障传播]
        D[诊断推理关系<br/>22 个，排查过程]
        ST[状态关系<br/>1 个，单值超替]
    end
    
    subgraph 断言语义
        P[Polarity: positive/negative]
        A[Assertion Status<br/>observed/hypothesized<br/>confirmed/ruled_out/rejected]
    end
    
    subgraph 图准入
        G[graph_eligible]
    end
    
    S --> G
    C --> G
    D --> G
    ST --> G
    P --> G
    A --> G
```

## Ontology 模块

所有谓词在 `ontology.py` 的 `PREDICATE_DICTIONARY` 中集中定义，是单一真相源。分类集合（`STRUCTURAL_PREDICATES` 等）从词典动态派生，通过模块加载时的 assert 强校验保证一致性。

```python
PREDICATE_DICTIONARY = {
    "has_component": PredicateDef("structural", "...", "...", "..."),
    "caused_by":     PredicateDef("causal",     "...", "...", "..."),
    "investigates":  PredicateDef("diagnostic", "...", "...", "..."),
    "has_status":    PredicateDef("state",      "...", "...", "..."),
    # ... 共 36 个
}

# 分类集合从词典派生（不再独立硬编码）
STRUCTURAL_PREDICATES = frozenset(p for p, d in PREDICATE_DICTIONARY.items()
                                    if d.category == "structural")  # 8 个
CAUSAL_PREDICATES = frozenset(p for p, d in PREDICATE_DICTIONARY.items()
                                 if d.category == "causal")       # 5 个
DIAGNOSTIC_PREDICATES = frozenset(p for p, d in PREDICATE_DICTIONARY.items()
                                     if d.category == "diagnostic") # 22 个
STATE_PREDICATES = frozenset(p for p, d in PREDICATE_DICTIONARY.items()
                                if d.category == "state")         # 1 个

# 排除类谓词（自动不入因果图）
OPPOSING_PREDICATES = frozenset({"ruled_out"})
RELATIONAL_EXCLUSION_PREDICATES = frozenset({"no_correlation", "contradicts"})
GRAPH_EXCLUDED_PREDICATES = OPPOSING_PREDICATES | RELATIONAL_EXCLUSION_PREDICATES

# 基数：state 类一律 single，其余 multi
PREDICATE_CARDINALITY = {
    predicate: ("single" if predicate in STATE_PREDICATES else "multi")
    for predicate in DIAGNOSIS_PREDICATE_NAMES
}
```

> **谓词清理变更（0008 迁移）**：
> - 移除互逆冗余：`part_of` → 统一用 `has_component`（方向：父→子）；`symptom_of` → 统一用 `has_symptom`（方向：故障→征兆）；`investigated_by` → 统一用 `investigates`（方向：假设→排查对象）；`led_to` → 统一用 `caused_by`（方向：结果→原因）
> - 移除语义重叠：`contributes_to` 并入 `affects`；`deal_stage` 移除（用 state 表达即可）
> - 方向统一原则：subject 是**整体/上层/结果**，object 是**部分/下层/原因**。遍历从故障出发向下游找原因、向上游找子系统

## 四大类谓词详解

### 1. 结构/配置关系（8 个，静态拓扑）

描述设备的结构层级、传感器布局和控制链路。**方向统一**：父→子、整体→部分。

| 谓词 | 含义 | subject → object | 示例 |
|------|------|-------------------|------|
| `has_component` | A包含B（整体→部分） | equipment/subsystem → component/subsystem | 气体输送系统 → MFC-1 |
| `installed_on` | A安装在B上 | sensor/component → component/chamber | T-101 → 腔体壁 |
| `located_in` | A位于B内 | component/subsystem → chamber/equipment | 匹配网络 → 射频系统腔体 |
| `monitored_by` | A被B监测 | param/fault/component → sensor | 腔体压力 → P-02 |
| `controlled_by` | A被B控制 | component/param → **controller** | 加热器H-1 → 温度PID |
| `regulates` | A调节B | controller → param | 温度PID → 基底温度 |
| `configured_as` | A（步骤/配方）配置为B | process_step/recipe → param | 主工艺步骤 → 射频功率1500W |
| `depends_on` | A依赖B | step/param/subsystem → step/param/subsystem | 主工艺步骤 → 预真空步骤 |

```{admonition} controlled_by 的 object 必须是 controller
部件/参数"属于"哪个系统 → 用 `has_component` / `installed_on`
部件/参数"被谁控制" → 用 `controlled_by`，且 object 必须是 `controller` 类型
**禁止** `controlled_by` 指向 equipment（整机）——整机不直接控制单个传感器，控制经由局部 controller 实现
```

```{mermaid}
graph LR
    SYS[温控系统] -->|has_component| PID[温度PID<br/>controller]
    SYS -->|has_component| HEATER[加热器H-1<br/>component]
    PID -->|regulates| TEMP[基底温度<br/>process_param]
    TEMP -->|monitored_by| T101[T-101<br/>sensor]
    T101 -->|installed_on| WALL[腔体壁<br/>component]
    HEATER -->|controlled_by| PID
```

### 2. 因果/级联关系（5 个，故障传播）

描述故障如何产生、传播和表现。**方向统一**：结果→原因、故障→征兆。

| 谓词 | 含义 | subject → object | 示例 |
|------|------|-------------------|------|
| `caused_by` | A的故障由B引起（核心因果谓词） | fault → component/fault/phenomenon | 腔体压力异常 → 密封圈老化 |
| `cascades_to` | A故障级联传播到B（跨子系统） | fault → fault | 压力异常 → 等离子不稳定 |
| `has_symptom` | A故障表现为B征兆 | fault → symptom/fault | MFC响应延迟 → 流量阶梯式偏差 |
| `affects` | A影响了B（宽泛因果） | fault/param → param/fault | 温度漂移 → 刻蚀速率 |
| `triggers` | A触发了B（互锁/告警/自动动作） | fault/condition → event/action | 压力超限 → 互锁停机 |

> **设计决策**：所有因果关系统一用 `caused_by` 作为主谓词（subject 为结果、object 为原因），不再区分 `led_to` / `causes` / `results_in` 等方向变体。这大幅简化了图遍历逻辑——找根因一律沿 `caused_by` 出边向下走，找影响一律沿入边向上走。

### 3. 诊断推理关系（22 个，排查过程）

描述工程师/agent 的排查过程和推理链，是诊断知识图谱中最丰富的一类。

| 谓词 | 含义 | 示例 |
|------|------|------|
| `investigates` | A（假设）排查了B（子系统/传感器/部件） | 怀疑真空泄漏 → 真空系统 |
| `checked` | A（排查动作）检查了B | 对比MFC设定值 → MFC-1实测流量 |
| `found` | A（排查动作）发现了B（发现/异常） | 拆检密封圈 → 密封圈磨损痕迹 |
| `normal` | A排查时正常（排除项） | 检查射频系统 → 射频系统(正常) |
| `ruled_out` | A（假设）被排除了 | 假设射频故障 → 射频系统 |
| `no_correlation` | A与B无相关性（排除项） | 水温波动 → 刻蚀速率漂移 |
| `contradicts` | A（证据）反驳了B（假设） | MFC-1校准合格 → 假设MFC校准漂移 |
| `supports` | A（证据）支持B（假设/结论） | 压力历史数据 → 密封圈老化假设 |
| `confirmed_by` | A（结论）被B（证据/案例）确认 | MFC-1是根因 → 参考案例-007 |
| `refines_to` | A（宽泛假设）细化为B（更具体的假设） | 气体系统问题 → MFC-1校准漂移 |
| `alternative_to` | A和B是互斥的替代假设 | MFC校准漂移 vs 密封圈老化 |
| `detected_by` | A（征兆）被B（传感器）检测到 | 压力波动 → P-02 |
| `observed_by` | A（现象/故障）被B（人）发现 | 刻蚀不均 → 工程师A |
| `references` | A引用了历史案例B | 当前排查 → 案例-007 |
| `repaired_by` | A（故障）被B（措施）修复 | 密封失效 → 更换O-ring |
| `preceded_by` | A发生在B之后（时序） | 压力异常 → 更换气体瓶之后 |
| `drifts_from` | A（状态/参数）偏离B（基准/正常状态） | 腔壁温度 → 设定值偏差3度 |
| `deviates_from` | A（量测结果）偏离B（规格/基准） | CD均匀性 → 规格±2nm |
| `measured_as` | A（工艺/批次）的量测结果是B | 批次A123 → CD均匀性偏差3% |
| `correlates_with` | A与B有相关性 | MFC-1流量波动 → 刻蚀速率漂移 |
| `suggests` | A（信号/数据模式）暗示B（假设/故障） | T-101周期性振荡 → PID参数失调 |
| `feedback_to` | A（量测结果）反馈到B（工艺步骤/参数） | CD偏差 → 主刻蚀步骤(需调补偿) |

### 4. 状态关系（1 个，单值互斥）

描述设备/腔室的运行状态，**single 基数**——新值到达自动超替旧值（recorded_to=now()）。

| 谓词 | 含义 | subject → object | 示例 |
|------|------|-------------------|------|
| `has_status` | A的运行状态是B（单值互斥） | equipment/chamber/subsystem → status_value | 主腔体 → 正常运行 / 故障停机 / 维护中 |

## 断言语义

每个 Fact 有两个语义轴：

### Polarity（极性）

| 值 | 含义 | 示例 |
|----|------|------|
| `positive` | 肯定断言 | "腔体压力异常 caused_by 密封圈老化" |
| `negative` | 否定/排除 | "检查射频系统 normal 射频系统" |

### Assertion Status（断言状态）

| 值 | 含义 | 适用场景 |
|----|------|----------|
| `observed` | 观察到的，确认无误 | 结构/配置/传感器关系 |
| `hypothesized` | 假设/推断，未确认 | 因果谓词默认 |
| `confirmed` | 有证据确认 | 因果谓词 + 证据支撑 |
| `ruled_out` | 被排除 | 对立谓词自动 |
| `rejected` | 被驳回 | 明确否定 |

### 自动规则

`assertion_semantics` 函数在抽取管线中自动应用规则：

```python
def assertion_semantics(predicate, fact, *, trusted=False, source_text=None):
    polarity = "negative" if fact.get("negation") else fact.get("polarity", "positive")
    requested = fact.get("assertion_status")
    # 规则1：对立谓词 → 自动 negative + ruled_out
    if predicate in OPPOSING_PREDICATES:
        return "negative", "ruled_out"

    # 规则2：关系排除谓词（无相关性/反驳）→ 正常注入，但 GRAPH_EXCLUDED 不入图
    if predicate in RELATIONAL_EXCLUSION_PREDICATES:
        return "positive", requested or "observed"

    # 规则3：因果谓词
    if predicate in CAUSAL_PREDICATES:
        if polarity == "negative" or requested in {"ruled_out", "rejected"}:
            return polarity, "ruled_out"
        # 有证据且可信 → confirmed
        evidence = str(fact.get("evidence_span") or "").strip()
        grounded = bool(source_text and evidence and evidence in source_text)
        if requested == "confirmed" and evidence and (trusted or grounded):
            return polarity, "confirmed"
        return polarity, "hypothesized"  # 默认假设

    # 规则4：非因果谓词 → 保留 LLM 指定值
    return polarity, requested or "observed"
```

## 图准入规则

不是所有 facts 都进知识图谱遍历。`graph_eligible()` 函数定义准入条件：

```python
def graph_eligible(predicate, polarity, assertion_status):
    """是否可入图遍历"""
    if polarity != "positive" or predicate in GRAPH_EXCLUDED_PREDICATES:
        return False
    if predicate in CAUSAL_PREDICATES:
        return assertion_status == "confirmed"  # 因果必须 confirmed
    return assertion_status in {"observed", "confirmed"}
```

```{mermaid}
flowchart TD
    F[Fact] --> P{polarity?}
    P -->|negative| X[不进图]
    P -->|positive| E{predicate in<br/>excluded?}
    E -->|ruled_out/no_correlation/contradicts| X
    E -->|否| C{predicate in<br/>causal?}
    C -->|是| S{assertion_status?}
    S -->|confirmed| IN[进图 ✅]
    S -->|hypothesized| X
    C -->|否| O{assertion_status?}
    O -->|observed/confirmed| IN
    O -->|hypothesized/ruled_out| X
```

## Cardinality（谓词基数）

`PREDICATE_CARDINALITY` 只区分**状态类谓词**（单值，新值超替旧值）与**其他谓词**（多值，多值共存）：

```python
PREDICATE_CARDINALITY = {
    predicate: ("single" if predicate in STATE_PREDICATES else "multi")
    for predicate in DIAGNOSIS_PREDICATE_NAMES
}
```

需要注意：**基数（cardinality）与结构边收敛是两个不同维度**。基数描述"同一 subject+谓词 下可同时存在多少条有效事实"，而结构边收敛描述的是图中**拓扑边的身份如何确定**。多值共存 ≠ 结构谓词产生多条重复拓扑边——见下一节。

## 结构边收敛（idempotent 写入）

结构谓词（8 个 structural）描述的是设备的**静态拓扑**，同一条边本质上是"同一客观事实"，不应随 case/事件重复出现而堆积成多条等价边。因此结构边采用**按身份收敛**的写路径，而非普通的多值追加。

### 边的身份

`fact_store.py` 的 `lock_and_find_structural_fact()` 把结构边的身份定义为：

```
identity = scope + subject_id + predicate + object_type + object_identity + polarity
```

identity 中**不包含** `case_id`、`operating_regime`、`valid_from`/`valid_to`。也就是说，无论来自哪个 case、哪个运行工况、哪个时间区间，只要 `scope+subject+predicate+object+polarity` 相同，都视为**同一条结构边**。

```python
def lock_and_find_structural_fact(conn, *, scope, subject_id, predicate,
                                  object_type, object_entity_id, object_value,
                                  polarity="positive", exclude_fact_id=None):
    if predicate not in STRUCTURAL_PREDICATES:
        return None  # 非结构谓词不走收敛路径
    object_identity = str(object_entity_id) if object_entity_id else \
        json.dumps(object_value, ensure_ascii=False, sort_keys=True)
    identity = json.dumps(
        ["structural", scope, subject_id, predicate, object_type, object_identity, polarity],
        ensure_ascii=False, sort_keys=True)
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))"),
        {"identity": identity})
    # ... 在 recorded_to IS NULL（当前有效）中查找同身份边
```

### 并发串行化

写入前先取 `pg_advisory_xact_lock(hashtextextended(:identity,0))`，把对同一条结构边的并发写入串行化——同一事务内多个来源同时写入同一结构边时，只有一个能真正插入，其余走合并路径。这与实体创建（`resolve_write`）的身份锁是同一套思想。

### 证据/置信度合并

写路径 `_insert_fact` 在命中既有结构边时**不新增重复行**，而是合并：

- `supports`（支撑证据）：并集合并 `combined_supports = existing ∪ incoming`
- `confidence`（置信度）：取 `max`
- `assertion_case_links`：复制到合并后的记录

### 更强确认 → recorded 版本更新

当新来源的确认强度更高时（`assertion_status` 或 `knowledge_tier` 更强，按 `_ASSERTION_STATUS_RANK` / `_KNOWLEDGE_TIER_RANK` 比较），把旧记录 `recorded_to=now()` 关闭，插入一条带更强确认的新 recorded 版本（同一 valid 区间、合并后的 supports/confidence）。若新来源并不更强，则保留既有更强状态，仅补入新增 supports。

### 与多值/状态谓词的差异

| 谓词类别 | 边身份 | 写入行为 |
|----------|--------|----------|
| **structural**（8 个） | `scope+subject+predicate+object+polarity`，**忽略** case/regime/valid 区间 | 同身份收敛：合并 evidence，更强确认才出新 recorded 版本 |
| **state**（1 个） | 单值，新值超替旧值 | valid 区间超替 |
| **diagnostic / causal** | 保留 case_id / event/regime 感知的身份 | 多值共存，保留多值历史 |

换句话说，**多值共存适用于诊断/因果类谓词**（不同 case、不同事件时间点本就该并存各自的历史），而**结构边收敛**保证静态拓扑不因 case 重复而退化出等价冗余边。高频 `ingest` 同一台机台的结构时，收敛路径天然具备幂等性——重复写入同一结构边不会产生重复拓扑边。

## DB-backed 本体: predicate_definitions 表

`ontology.py`（`src/cortex/infra/ontology.py`）**仍然是所有一阶谓词的单一真相源**。在此基础上，cortex 增加了一张 DB 配套表 `predicate_definitions`，承载 `ontology.py` 本身不携带的 `prop_order`（一阶/高阶）和 `category`（类别）元数据，并使本体可查询、可扩展到高阶谓词。

表结构定义在 `schema.sql`：

```sql
-- 谓词本体表(从 ontology.py 硬编码迁移到 DB 可配,支持 order 标记)
CREATE TABLE IF NOT EXISTS cortex.predicate_definitions (
    predicate       TEXT PRIMARY KEY,
    category        TEXT NOT NULL CHECK (category IN ('structural','causal','diagnostic','state','higher_order')),
    prop_order      INT NOT NULL DEFAULT 1 CHECK (prop_order IN (1,2)),  -- 1=一阶, 2=高阶
    description     TEXT,
    cardinality     TEXT NOT NULL DEFAULT 'multi' CHECK (cardinality IN ('single','multi')),
    example         TEXT
);
```

关键字段：

| 字段 | 说明 |
|------|------|
| `predicate` | 主键，谓词名 |
| `category` | 类别，`CHECK IN ('structural','causal','diagnostic','state','higher_order')`。前四类与 `ontology.py` 的四组谓词一一对应 |
| `prop_order` | 阶数，`CHECK IN (1,2)`。`1`=一阶谓词，`2`=高阶谓词 |
| `description` | 谓词的自然语言描述 |
| `cardinality` | 基数 `'single'`/`'multi'`，与 `ontology.py` 的 `PREDICATE_CARDINALITY` 对齐 |
| `example` | 示例三元组 |

### seed_predicate_definitions()

`maintenance.py` 的 `seed_predicate_definitions()` 把 `ontology.py` 的一阶谓词按类别 upsert 进 `predicate_definitions`，统一标记为 `prop_order=1`。该函数幂等：

```python
def seed_predicate_definitions() -> int:
    """把 ontology.PREDICATE_DICTIONARY 预置到 predicate_definitions 表(一阶,order=1)。幂等。返回 upsert 数。"""
    with session_scope() as conn:
        for pred, d in PREDICATE_DICTIONARY.items():
            card = PREDICATE_CARDINALITY.get(pred, "multi")
            conn.execute(text("""
                INSERT INTO predicate_definitions (predicate, category, prop_order, cardinality, description, example)
                VALUES (:p, :c, 1, :card, :desc, :ex)
                ON CONFLICT (predicate) DO UPDATE
                SET category=:c, cardinality=:card, description=:desc, example=:ex
            """), {"p": pred, "c": d.category, "card": card, "desc": d.meaning, "ex": d.example})
```

实现直接遍历 `PREDICATE_DICTIONARY.items()` 取每个 `PredicateDef` 的 `category`，连同 `meaning`/`example` 一起写入 6 列，`ON CONFLICT (predicate) DO UPDATE` 保证幂等。

### higher_order 类别与高阶归纳

`category='higher_order'` 是 DB 表新引入的类别，**在 `ontology.py` 中没有对应谓词集**。这类谓词必须同时满足 `prop_order=2`，由第 13 章的 **Higher-Order 归纳** 特征消费：

```python
# higher_order.py
ho_predicates = conn.execute(text("""
    SELECT predicate, description, example
    FROM predicate_definitions WHERE prop_order=2
""")).fetchall()
if not ho_predicates:
    return {"synthesized": 0, "skipped": "no order=2 predicates defined"}
```

换句话说：一阶谓词的真相仍在 `ontology.py`，而高阶谓词（`prop_order=2`、`category='higher_order'`）只存在于 DB 表中，需要运维人员/管理员通过该表显式注册后才能驱动高阶归纳。

### 真相源分工

| 内容 | 真相源 |
|------|--------|
| 一阶谓词集合（structural / causal / diagnostic / state） | `ontology.py` |
| 一阶谓词的 cardinality | `ontology.py` 的 `PREDICATE_CARDINALITY`（被 `seed_predicate_definitions()` 同步进 DB） |
| `category`、`prop_order` 元数据 | `predicate_definitions` 表（`ontology.py` 不携带） |
| 高阶谓词（`category='higher_order'`、`prop_order=2`） | 仅 `predicate_definitions` 表 |

## 在抽取管线中的应用

抽取管线在写入 facts 前，通过 `coerce_value` 将 LLM 输出的谓词约束到预定义词表（如果 scope 有 predicate 词表），并通过 `assertion_semantics` 自动设置 polarity 和 assertion_status；结构谓词再经 `lock_and_find_structural_fact` 走结构边收敛写路径：

```python
# 1. 谓词词表约束
predicate = coerce_value(conn, scope, "predicate", raw_predicate)
if predicate is None:
    skip  # 闭集未命中 → 跳过该 fact

# 2. 断言语义自动推断
polarity, assertion_status = assertion_semantics(predicate, fact, ...)

# 3. 结构边收敛（structural 谓词幂等写入）
if predicate in STRUCTURAL_PREDICATES:
    existing = lock_and_find_structural_fact(conn, scope=scope, subject_id=..., 
                                             predicate=predicate, object_type=...,
                                             object_entity_id=..., object_value=...)
    # existing 命中 → 合并 evidence / 更强确认出新 recorded；未命中 → 新插入

# 4. 图准入检查
if not graph_eligible(predicate, polarity, assertion_status):
    # 该 fact 只存不进图（检索仍可命中 BM25）
    ...
```

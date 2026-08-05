# 第4章 抽取管线

## 概述

抽取管线是 cortex 的**核心处理层**，负责从原始 Event 中提取结构化知识。一条 Event 进入系统后，抽取管线将其转化为实体（Entities）和事实（Facts），经过实体链接（B over C）去重合并后存入知识图谱，并聚合为 Beliefs。

```{mermaid}
flowchart TB
    subgraph 写入
        E[原始 Event] --> EXT[抽取管线]
    end
    
    subgraph 抽取
        EXT --> LLM[LLM 结构化抽取]
        LLM --> ENT[实体列表]
        LLM --> FACT[事实三元组]
    end
    
    subgraph 实体链接
        ENT --> LINK[B over C 链接]
        LINK --> MERGE[合并/新建实体]
    end
    
    subgraph 存储
        MERGE --> DB[(entities)]
        FACT --> DB2[(facts)]
        DB2 --> AGG[Belief 聚合]
        AGG --> DB3[(beliefs)]
    end
```

## LLM Prompts 体系

cortex 的 prompt 体系采用**分层前缀设计**：所有 prompt 共享 `PROJECT_CONTEXT` 前缀，然后按场景叠加专属指令。每个 prompt 回答三个问题："我在什么系统里"、"这次产出的东西给谁用"、"应该怎么产"。

### PROJECT_CONTEXT

所有 prompt 的公共 system 前缀，约 500 字，描述 cortex 系统的五层记忆模型、scope 隔离、双时态等核心概念。它确保每次 LLM 调用都理解自己的上下文。

### 三种抽取 Prompt

根据 Event 的 `context.intent` 字段，选择不同的抽取 prompt：

| Prompt | 触发条件 | 适用场景 |
|--------|----------|----------|
| `EXTRACTION_SYSTEM_DIAGNOSIS` | `intent="diagnosis"` 或 `"incident_retrospective"` | 故障诊断文本、排查记录、故障分析 |
| `EXTRACTION_SYSTEM_STRUCTURE` | `intent="structure"` | 机械结构文档、传感器布局、控制架构 |
| `EXTRACTION_SYSTEM_GENERAL` | 其他 | 通用文本、对话记录 |

```python
# extraction/pipeline.py
def _llm_extract(text_body: str, is_diagnosis: bool = False,
                 intent: str = None) -> Dict[str, Any]:
    from ..prompts import (EXTRACTION_SYSTEM_DIAGNOSIS, EXTRACTION_SYSTEM_STRUCTURE,
                           EXTRACTION_SYSTEM_GENERAL)
    if intent == "structure":
        sys_msg = EXTRACTION_SYSTEM_STRUCTURE
    elif intent in ("diagnosis", "incident_retrospective") or is_diagnosis:
        sys_msg = EXTRACTION_SYSTEM_DIAGNOSIS
    else:
        sys_msg = EXTRACTION_SYSTEM_GENERAL
    sys_msg += ("\n\nMachine-enforced predicate vocabulary: "
                + ", ".join(sorted(DIAGNOSIS_PREDICATE_NAMES)))
    # ... 调用 LLM
```

## 21 种实体类型

抽取 prompt 定义了**21 种实体类型**，按语义层分为三组。实体类型是字符串而非枚举——抽取时自由标注，B-over-C 实体链接时 `entity_type` 作为匹配维度之一。

### A. 物理/工程层实体（11 种）

| 类型 | 说明 | 命名规范 | 示例 |
|------|------|----------|------|
| `equipment` | 设备/整机 | 型号/代号 | "处理单元A"、"工艺模块PM-3" |
| `subsystem` | 子系统/功能模块 | 功能名+系统 | "温控系统"、"真空系统"、"气体输送系统"、"射频系统"、"传输系统"、"排气系统" |
| `component` | 具体部件 | 规格+部件名 | "质量流量控制器MFC-1"、"截止阀V-3"、"加热器H-1"、"静电卡盘ESC"、"匹配网络"、"喷淋头" |
| `sensor` | 传感器/仪表 | 编号+类型 | "温度传感器T-101"、"压力传感器P-02"、"光学端点检测器EPD-1"、"振动传感器V-1"、"电压传感器"、"流量计" |
| `controller` | 控制单元 | 层级+功能 | "PLC主控"、"腔体温度PID"、"安全互锁SIS"、"射频匹配控制器"、"MCU固件v2.1"、"节流阀压力控制器" |
| `process_param` | 工艺参数 | 参数名+单位 | "腔体压力3mTorr"、"射频功率1500W"、"气体流量A:50sccm"、"基底温度80度"、"刻蚀速率1200A/min"、"DC偏压200V"、"沉积速率" |
| `process_step` | 工艺步骤/阶段 | 阶段名 | "预真空步骤"、"稳定步骤"、"主工艺步骤"、"吹扫步骤"、"升温步骤"、"除气步骤"、"预沉积步骤" |
| `material` | 材料/介质 | 规范名 | "工艺气体A"、"前驱体B"、"密封O-ring"、"冷却液"、"靶材C"、"反应副产物" |
| `phenomenon` | 物理/化学现象 | 现象描述 | "等离子体点火"、"沉积反应"、"反应副产物累积"、"热膨胀"、"气体击穿"、"离子轰击"、"溅射"、"等离子体模式转换" |
| `chamber_state` | 腔体状态/条件 | 状态描述 | "腔体积碳(seasoning漂移)"、"腔壁残留污染"、"腔体conditioning未完成"、"腔体清洁后首次工艺"、"等离子体清洗状态" |
| `metrology_result` | 量测/计量结果 | 指标+偏差 | "CD均匀性±2.1nm"、"套刻精度overlay偏差3nm"、"缺陷密度15个/wafer"、"薄膜厚度偏差"、"刻蚀深度偏差" |

### B. 故障层实体（3 种）

| 类型 | 说明 | 示例 |
|------|------|------|
| `fault` | 故障/异常状态 | "腔体压力异常"、"MFC响应延迟"、"温控超调"、"等离子不稳定"、"均匀性偏差"、"刻蚀速率漂移" |
| `symptom` | 可观测征兆 | "压力读数波动±0.5mTorr"、"RF反射功率升高"、"温度振荡幅度±3度"、"信号基线漂移" |
| `signal_pattern` | 信号特征模式 | "T-101温度呈周期性振荡(周期约30s)"、"P-02压力有阶跃式下降"、"EPD信号斜率偏离基准15%" |

### C. 诊断推理层实体（7 种）

诊断推理的**载体实体**（不只是谓词）——假设、证据、排查动作等作为图谱节点，让排查历史可被图遍历追溯：

| 类型 | 说明 | 示例 |
|------|------|------|
| `hypothesis` | 诊断假设/嫌疑方向 | "怀疑真空系统泄漏"、"假设MFC校准漂移"、"气体输送系统嫌疑" |
| `evidence` | 诊断证据 | "T-101趋势显示72小时内缓慢漂移5度"、"P-02与EPD信号呈0.82相关" |
| `diagnostic_action` | 排查动作 | "检查真空系统密封性"、"对比MFC-1设定值与实测值"、"执行腔体烘烤除气" |
| `correlation` | 相关性发现 | "MFC-1流量偏差与刻蚀速率漂移相关系数0.85" |
| `measure` | 维修/处理措施 | "更换密封O-ring"、"重新校准MFC-1"、"修改PID增益参数" |
| `person` | 相关人员 | "工程师李某"、"维护班组" |
| `historical_ref` | 历史案例引用 | "参考2025-11类似事件(案例-007)" |

### 关于"假设/证据/动作"的双重表达

```{admonition} 关键设计：诊断推理既用实体也用谓词承载
早期设计中，`hypothesis` / `evidence` / `diagnostic_action` / `measure` 等只作为谓词语义存在；后来它们**同时作为实体类型回归**（C 组 7 种），让排查产物成为图谱中的可追溯节点：

- **实体维度**：假设、证据、排查动作、相关性、措施各为一个节点，命名即其语义（"怀疑真空系统泄漏"）
- **谓词维度**：节点之间用 `investigates` / `refines_to` / `suggests`（假设）、`supports` / `contradicts` / `confirmed_by`（证据）、`checked` / `found` / `repaired_by`（动作）、`references`（历史）等连接，配合 `assertion_status=hypothesized` 表达确认状态

这样，一条排查记录既保留"做了什么"（C 组实体节点），又保留"证明了什么"（诊断谓词 + 断言语义），下游 agent 沿图即可重放完整排查链。
```

## 36 个谓词四大类

所有谓词定义在 `ontology.py` 的 `PREDICATE_DICTIONARY` 中，分为**四大类**（结构 8 + 因果 5 + 诊断 22 + 状态 1 = 36 个）。经 0008_predicate_cleanup 迁移清理，消除了互逆冗余，方向统一。

| 大类 | 数量 | 核心谓词示例 | 详见 |
|------|------|-------------|------|
| 结构谓词 | 8 | has_component, installed_on, monitored_by, controlled_by, regulates | 第6章 第1节 |
| 因果谓词 | 5 | caused_by, cascades_to, has_symptom, affects, triggers | 第6章 第2节 |
| 诊断谓词 | 22 | investigates, checked, found, ruled_out, supports, contradicts, refines_to | 第6章 第3节 |
| 状态谓词 | 1 | has_status（单值互斥） | 第6章 第4节 |

> **谓词清理变更要点**：
> - 移除 `part_of` → 统一用 `has_component`（父→子方向）
> - 移除 `led_to` / `symptom_of` / `investigated_by` → 方向统一：结果→原因、故障→征兆、假设→排查对象
> - 移除 `contributes_to` → 并入 `affects`
> - `correlates_with` 和 `suggests` 从因果类移入诊断类（它们是排查发现，不是物理因果）
> - 新增 `confirmed_by` 归为诊断类，`feedback_to` 归为诊断类
> - 总数量从 40+ 收敛到 36，分类从 3 类变为 4 类（新增 state 类）

## 8 条连接准则

抽取 prompt 中定义了 8 条关键连接准则，指导 LLM 提取特定关系链：

### 准则 1：监测链（传感器 ↔ 部件 ↔ 参数）

每个传感器必须连接安装在什么上、监测什么。

```
T-101 --installed_on--> 腔体壁
腔体温度 --monitored_by--> T-101
加热器H-1 --controlled_by--> 温度PID
温度PID --regulates--> 腔体温度
```

### 准则 2：检测链（征兆 ↔ 传感器 ↔ 故障）

每个征兆连接到检测它的传感器和它指向的故障。

```
压力波动 --detected_by--> P-02
压力波动 --symptom_of--> 腔体压力异常
```

### 准则 2b：腔体状态链（Seasoning/漂移链）

腔体状态漂移是半导体设备的"隐性根因"。

```
腔体积碳 --drifts_from--> 正常seasoning状态
腔体积碳 --affects--> 刻蚀速率
```

### 准则 2c：量测反馈链（计量联动链）

工艺质量通过下游量测验证，量测结果反馈到工艺参数。

```
主刻蚀步骤 --measured_as--> CD均匀性±2.1nm
CD均匀性 --deviates_from--> CD规格±1.5nm
CD偏差 --feedback_to--> 主刻蚀步骤
```

### 准则 3：因果链（多层级根因追溯）

故障根因跨多个层面，逐层追溯到底。

```
均匀性偏差 --caused_by--> 等离子不稳定
  --caused_by--> 腔体压力异常 --caused_by--> 密封圈老化
```

### 准则 4：级联链（跨子系统传播）

```
密封失效 --cascades_to--> 真空度下降
  --cascades_to--> 压力波动 --cascades_to--> 等离子不稳定
  --cascades_to--> 刻蚀速率漂移 --cascades_to--> 均匀性偏差
```

### 准则 5：诊断推理链（多轮迭代）

每一轮包含假设→排查→发现→排除/细化→再假设。排除项也必须提取！

```
刻蚀速率漂移 --investigated_by--> 怀疑温控系统
检查温控系统 --found--> T-101缓慢漂移
怀疑温控系统 --refines_to--> T-101可能漂移
怀疑射频故障 --ruled_out--> 射频系统
```

### 准则 6：触发链（互锁/告警）

```
腔体压力超上限 --triggers--> 互锁停机
MFC偏差>10% --triggers--> 工艺中断告警
```

### 准则 7：依赖链（工艺步骤依赖）

```
主工艺步骤 --depends_on--> 预真空步骤(压力<1mTorr)
沉积速率 --depends_on--> 前驱体流量
```

### 准则 8：数值参数 → literal

具体数值作为 literal object，不创建实体。

```
压力波动 --has_symptom--> {literal: "±0.5mTorr"}
MFC-1偏差 --correlates_with--> {literal: "r=0.85 with 刻蚀速率漂移"}
```

## Assertion Semantics 断言语义规则

`assertion_semantics` 函数根据谓词类型和 fact 属性，自动推导断言的**极性（polarity）**和**认知状态（assertion_status）**：

```python
# extraction/pipeline.py
def assertion_semantics(predicate: str, fact: Dict[str, Any], *,
                        trusted: bool = False,
                        source_text: Optional[str] = None) -> Tuple[str, str]:
    polarity = "negative" if fact.get("negation") else fact.get("polarity", "positive")
    requested = fact.get("assertion_status")

    if predicate in OPPOSING_PREDICATES:        # ruled_out
        return "negative", "ruled_out"
    if predicate in RELATIONAL_EXCLUSION_PREDICATES:  # no_correlation, contradicts
        return "positive", requested or "observed"
    if predicate in CAUSAL_PREDICATES:           # 因果谓词需要 evidence
        if polarity == "negative" or requested in {"ruled_out", "rejected"}:
            return polarity, "ruled_out"
        evidence = str(fact.get("evidence_span") or "").strip()
        grounded_in_source = bool(source_text and evidence and evidence in source_text)
        if requested == "confirmed" and evidence and (trusted or grounded_in_source):
            return polarity, "confirmed"
        return polarity, "hypothesized"
    return polarity, requested or "observed"
```

| 条件 | polarity | assertion_status | 含义 |
|------|----------|------------------|------|
| 默认 | positive | observed | 观察到的事实 |
| 因果谓词 + 无证据 | positive | hypothesized | 假设性推断 |
| 因果谓词 + 有证据 | positive | confirmed | 已确认的因果 |
| negation=true | negative | observed | 否定的断言 |
| ruled_out/否定的因果 | negative | ruled_out | 已被排除 |
| contradicts | positive | observed | 反驳关系 |

## Vocab Coerce 词表约束

抽取过程中，谓词和字面值会经过词表约束（vocab coerce），确保入库的值在可控范围内：

```python
# extraction/pipeline.py
def coerce_value(conn, scope: str, vocab_name: str, raw: str) -> Optional[str]:
    """closed:未命中→null; open:未命中→保留; 命中别名→canonical。无词表→原样。"""
    row = conn.execute(text(
        "SELECT vocab_id, kind FROM vocabularies WHERE scope=:s AND name=:n"),
        {"s": scope, "n": vocab_name}).fetchone()
    if not row:
        return raw
    hit = conn.execute(text("""
        SELECT vv.canonical_value FROM vocabulary_values vv WHERE vv.vocab_id=:v
        AND (vv.canonical_value=:r OR :r = ANY(vv.aliases)) LIMIT 1
    """), {"v": row.vocab_id, "r": raw}).fetchone()
    if hit:
        return hit.canonical_value
    return raw if row.kind == "open" else None
```

词表约束在抽取中的两个关键点：
1. **谓词约束**：所有 predicate 必须命中"predicate"词表（closed），未命中的进入 quarantine
2. **字面值约束**：literal object 按 `_guess_vocab(predicate)` 猜对应词表，未命中 closed 词表则拒绝

## Identity Context 身份上下文传递

物理实体（传感器、部件、控制器）的身份上下文（fab/equipment/module/chamber）决定了它们在知识图谱中的唯一性：

```python
# extraction/pipeline.py
_CONTEXT_FIELDS = ("fab", "equipment", "module", "chamber", "recipe", "recipe_revision")

def canonical_identity_context(context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    raw = context or {}
    values = {
        "fab": raw.get("fab"),
        "equipment": raw.get("tool") or raw.get("equipment"),
        "module": raw.get("module"),
        "chamber": raw.get("chamber"),
        "recipe": raw.get("recipe"),
        "recipe_revision": raw.get("recipe_revision") or raw.get("recipe_rev"),
    }
    return {key: canon for key in _CONTEXT_FIELDS 
            if (canon := _canonical_text(values.get(key) or ""))}
```

不同类型的实体使用不同的身份上下文字段：

| 实体类型 | 身份字段 |
|----------|----------|
| equipment, tool | fab, equipment |
| module, chamber, component, sensor, subsystem | fab, equipment, module, chamber |
| recipe, process_step, process_param | 全部 6 个字段 |
| fault, symptom, material 等 | 无身份上下文 |

```python
def _identity_context_for_type(context, entity_type):
    canonical = canonical_identity_context(context)
    etype = _canonical_text(entity_type or "")
    if etype in {"equipment", "tool"}:
        allowed = {"fab", "equipment"}
    elif etype in {"module", "chamber", "component", "sensor", "subsystem"}:
        allowed = {"fab", "equipment", "module", "chamber"}
    elif etype in {"recipe", "process_step", "process_param"}:
        allowed = set(_CONTEXT_FIELDS)
    else:
        allowed = set()
    return {k: v for k, v in canonical.items() if k in allowed}
```

## B-over-C 实体链接调用

抽取管线的实体链接阶段实现了 B-over-C 三层策略（详见[第5章](05-entity-linking)）。为了支持灰区 LLM 裁决的并行化，`resolve_or_create` 已拆分为**查询阶段**和**写入阶段**两个函数：

```python
# extraction/entity_resolution.py
def resolve_lookup(conn, scope, name, etype, description, thresholds,
                   identity_context=None, precomputed_emb=None):
    """DB 查询阶段(只读):别名精确匹配 + 向量召回 + 阈值分类。

    返回三态之一:
      ("resolved", entity_id)  — 别名精确命中或高分直接合并,无需 LLM
      ("grey", candidates)     — 灰区(cos ∈ [new_thr, merge_thr)),需 LLM 判定
      ("new", None)            — 低分或无候选,直接新建
    """
    # A层：别名精确命中
    # C层：向量召回 top-5
    # 阈值判断：merge_thr(0.85)直接合并 / new_thr(0.30)灰区LLM / 以下新建
    # 身份敏感匹配：传感器/部件编号必须一致(identity_candidate_compatible)

def resolve_write(conn, scope, name, etype, description, emb, identity_context=None):
    """写入阶段:新建 entity + alias,返回 entity_id。仅在 Phase 3 调用。"""
```

`resolve_or_create` 仍保留，内部调 `resolve_lookup` + `resolve_write`，供 triple 直写等**单步路径**复用。`extract_event` 主流程不走它——而是走下面的三阶段并行路径。

```{mermaid}
sequenceDiagram
    participant E as Event
    participant LLM as LLM 抽取
    participant DB as 数据库(会话内)
    participant PL as parallel_map(会话外)

    E->>LLM: 文本内容
    LLM->>LLM: 按 intent 选 prompt
    LLM-->>LLM: 结构化输出 JSON

    Note over DB: Phase 1 — lookup(只读短事务)<br/>逐 entity 跑 resolve_lookup<br/>分类:resolved / grey / new
    DB->>DB: A层别名 + C层向量召回

    Note over PL: Phase 2 — 灰区 LLM 并行(会话外)<br/>N 个 grey entity 并发调 _llm_entity_link
    PL->>PL: parallel_map(_decide_grey)

    Note over DB: Phase 3 — write(写入短事务)<br/>resolve_write + 插入 facts<br/>超替闭合 + Belief 聚合
    DB->>DB: 落库 entity + facts + beliefs
```

## 主流程：extract_event

```{mermaid}
flowchart TD
    A[extract_event] --> B{content.kind?}
    B -->|triple| C[直写实体+fact]
    B -->|message/text| D{有 LLM key?}
    
    D -->|是| E[LLM 抽取]
    D -->|否| F[测试模式?]
    F -->|是| G[Mock 抽取]
    F -->|否| H[报错:无 LLM key]
    
    E --> I[实体链接]
    G --> I
    C --> I
    
    I --> J[词表约束谓词]
    J --> K{命中 closed?}
    K -->|否| L[进入 quarantine]
    K -->|是| M[断言语义分析]
    
    M --> N[单值超替闭合]
    N --> O[插入 facts]
    O --> P[冲突检测]
    P --> Q[Belief 聚合]
    Q --> R[发出 lifecycle]
    R --> S[返回结果]
```

```python
# extraction/pipeline.py - 主入口
def extract_event(event_id: str) -> Dict[str, Any]:
    cfg = load_config()
    thresholds = (cfg.extraction.link_thresholds.merge,
                  cfg.extraction.link_thresholds.new)

    # Step 1: 加载 event（短事务）
    with session_scope() as conn:
        ev = conn.execute(text("""
            SELECT scope, modality, content, context, observed_at, caller
            FROM events WHERE event_id=CAST(:e AS uuid)
        """), {"e": event_id}).fetchone()
        if not ev:
            return {"error": "event not found"}

        # triple 直写：不经 LLM
        if ev.content.get("kind") == "triple":
            return _direct_write_triple(conn, ...)

    # Step 2: LLM 抽取（无 DB session，防超时断连）
    extraction = _llm_extract(text_body, is_diagnosis=is_diagnosis, intent=intent)

    # Step 3a: 谓词校验（短事务）— quarantine 不合格谓词,确定 accepted facts
    with session_scope() as conn:
        accepted = [f for f in extraction["facts"]
                    if coerce_value(conn, scope, "predicate", f["predicate"]) is not None]

    # Step 3b: 预计算 entity embedding（session 外,纯 HTTP 批量）
    # 只为 accepted facts 引用的 entity 算 embedding(quarantined 的不创建)
    ent_embeddings = services.embed_texts([format_embedding_text(e) for e in ents_to_resolve])

    # Step 3c: 三阶段实体链接 + 建 facts + belief 聚合
    # Phase 1: lookup(会话内,只读)→ 分类 resolved/grey/new
    # Phase 2: 灰区 LLM 并发(会话外)→ parallel_map(_decide_grey)
    # Phase 3: write(会话内,短事务)→ resolve_write + facts + belief
```

Step 3c 的三阶段是并行化的核心——把灰区 LLM 裁决从 session 内串行改为 session 外并行，详见[第5章](05-entity-linking)。

## 完整抽取流程图

```{mermaid}
flowchart LR
    subgraph 写入路径
        A[用户输入] --> B[append_event]
        B --> C[enqueue extract job]
    end
    
    subgraph 抽取管线
        C --> D[claim extract job]
        D --> E[extract_event]
        E --> F{kind=triple?}
        F -->|是| G[direct_write_triple]
        F -->|否| H[LLM_extract]
        H --> P1[Phase1: resolve_lookup<br/>会话内只读分类]
        P1 --> P2{有 grey entity?}
        P2 -->|是| P2L[Phase2: parallel_map LLM<br/>会话外并发]
        P2 -->|否| P3
        P2L --> P3[Phase3: resolve_write<br/>+ facts + belief]
        P3 --> J[insert_fact]
        J --> K[close_superseded]
        K --> L[aggregate_belief]
        G --> M[complete_job]
        L --> M
    end
    
    subgraph 输出
        M --> N[entities]
        M --> O[facts]
        M --> P[beliefs]
        M --> Q[lifecycle: extracted/indexed]
    end
    
    subgraph 高阶归纳触发
        M --> HO{cfg.higher_order.enabled?}
        HO -->|是| HOJ[enqueue higher_order job<br/>per subject_id]
        HO -->|否| END[结束]
        HOJ --> HOS[(higher_order 队列)]
    end
```

## Higher-Order 异步触发

抽取完成（`embed_status='done'`）后，若 `cfg.higher_order.enabled` 开启，系统会遍历本次新写入 facts 涉及的每个 `subject_id`，分别为其 enqueue 一个 `higher_order` 归纳任务。Worker 后续消费这些任务时，收集该实体下相关的一阶事实，调用 LLM 合成高阶结论并写回 facts 表（`is_higher_order=true`，`evidence_fact_ids` 指向支撑它的一阶事实）。这是 Higher-Order 合成的异步入口，与在线抽取解耦，不会阻塞抽取主流程的返回。详见[第13章](13-higher-order)。

```python
# extraction/pipeline.py - extract_event 收尾阶段(pseudo)
if cfg.higher_order.enabled:
    for subject_id in extracted_subject_ids:
        enqueue_job("higher_order", scope=scope,
                    payload={"subject_id": str(subject_id),
                             "trigger_event_id": event_id})
```

## 结构边收敛（idempotent 写入）

抽取落库时，**结构谓词**（8 个：has_component, installed_on, located_in, monitored_by, controlled_by, regulates, configured_as, depends_on）与诊断/因果谓词遵循不同的写入语义：

- **结构谓词**：按 `scope + subject + predicate + object + polarity` 折叠为**单条活跃图边**，忽略 case_id、operating_regime、valid_from/valid_to。重复写入会合并证据（`supports` 并集、`confidence` 取 max），更强的确认（更高 assertion_status / knowledge_tier）产生新的 recorded-time 修订，否则保留既有更强状态。`POST /v1/facts/batch` 与 `create_fact` 都走这条幂等路径——结构边已存在则返回 `reuse`/`existing`。
- **诊断/因果/时间谓词**：**保留** case / event-time 感知身份，多值历史共存（如多次排查的 `checked` / `found` 记录不会被覆盖）。

> 这是旧"多值谓词共存"规则的刻意细化：多条值 ≠ 结构谓词的重复拓扑边。物理拓扑（"A 安装在 B 上"）是唯一真相，不随每次事件重复；诊断推理是历史记录，逐条保留。详见[第6章 §结构边收敛](06-ontology-and-assertion)。

## 关键代码结构

| 函数 | 位置 | 职责 |
|------|------|------|
| `extract_event` | `extraction/pipeline.py` | 主入口，编排整个抽取流程(含三阶段实体链接) |
| `assertion_semantics` | `extraction/semantics.py` | 断言语义规则 |
| `_llm_extract` | `extraction/llm_extraction.py` | LLM 调用 + R1 fallback 链 |
| `resolve_lookup` | `extraction/entity_resolution.py` | B-over-C 实体链接·查询阶段(只读,返回三态) |
| `resolve_write` | `extraction/entity_resolution.py` | B-over-C 实体链接·写入阶段(新建 entity+alias) |
| `resolve_or_create` | `extraction/entity_resolution.py` | 单步兼容包装(lookup+write),供 triple 直写路径 |
| `lock_and_find_structural_fact` | `extraction/fact_store.py` | 结构边幂等查找(advisory lock 收敛) |
| `_insert_fact` | `extraction/fact_store.py` | 插入事实三元组(结构边合并证据) |
| `_close_superseded` | `extraction/fact_store.py` | 超替闭合(含 operating_regime/case 过滤) |
| `coerce_value` | `extraction/pipeline.py` | 词表约束(closed 强制收窄) |
| `canonical_identity_context` / `identity_context_for_type` | `extraction/semantics.py` | 身份上下文规范化与按类型收窄 |
| `_aggregate_belief` | `extraction/beliefs.py` | Belief 聚合 |
| `_direct_write_triple` | `extraction/pipeline.py` | 三元组直写（不经 LLM） |
| `_quarantine` | `extraction/pipeline.py` | 不合格数据隔离 |

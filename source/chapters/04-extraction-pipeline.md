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

## 10+ 实体类型

抽取 prompt 定义了三大类、10+ 种实体类型：

### A. 物理层实体

| 类型 | 说明 | 命名规范 | 示例 |
|------|------|----------|------|
| `equipment` | 设备/整机 | 型号/代号 | "处理单元A"、"工艺模块PM-3" |
| `subsystem` | 子系统 | 功能名+系统 | "温控系统"、"真空系统"、"气体输送系统" |
| `component` | 具体部件 | 规格+部件名 | "质量流量控制器MFC-1"、"静电卡盘ESC" |
| `sensor` | 传感器/仪表 | 编号+类型 | "温度传感器T-101"、"压力传感器P-02" |
| `controller` | 控制单元 | 层级+功能 | "PLC主控"、"腔体温度PID" |
| `process_param` | 工艺参数 | 参数名+单位 | "腔体压力3mTorr"、"射频功率1500W" |
| `process_step` | 工艺步骤 | 阶段名 | "预真空步骤"、"主工艺步骤" |
| `material` | 材料/介质 | 规范名 | "工艺气体A"、"密封O-ring" |
| `phenomenon` | 物理/化学现象 | 现象描述 | "等离子体点火"、"沉积反应" |
| `chamber_state` | 腔体状态 | 状态描述 | "腔体积碳"、"腔壁残留污染" |
| `metrology_result` | 量测结果 | 指标+偏差 | "CD均匀性±2.1nm"、"缺陷密度15个/wafer" |

### B. 故障层实体

| 类型 | 说明 | 示例 |
|------|------|------|
| `fault` | 故障/异常状态 | "腔体压力异常"、"MFC响应延迟" |
| `symptom` | 可观测征兆 | "压力读数波动±0.5mTorr"、"RF反射功率升高" |
| `signal_pattern` | 信号特征模式 | "T-101温度呈周期性振荡(周期约30s)" |

### C. 诊断推理层实体

| 类型 | 说明 | 示例 |
|------|------|------|
| `hypothesis` | 诊断假设/嫌疑方向 | "怀疑真空系统泄漏" |
| `evidence` | 诊断证据 | "T-101趋势显示72小时内缓慢漂移5度" |
| `diagnostic_action` | 排查动作 | "检查真空系统密封性" |
| `correlation` | 相关性发现 | "MFC-1流量偏差与刻蚀速率漂移相关系数0.85" |
| `measure` | 维修/处理措施 | "更换密封O-ring"、"重新校准MFC-1" |
| `historical_ref` | 历史案例引用 | "参考2025-11类似事件(案例-007)" |

## 40+ 谓词三大类

所有谓词定义在 `ontology.py` 中，分为三大类：

### A. 结构谓词（STRUCTURAL_PREDICATES）

```python
# ontology.py
STRUCTURAL_PREDICATES = {
    "part_of", "has_component", "installed_on", "located_in", "monitored_by",
    "controlled_by", "regulates", "configured_as", "depends_on",
}
```

### B. 因果谓词（CAUSAL_PREDICATES）

```python
CAUSAL_PREDICATES = {
    "caused_by", "led_to", "cascades_to", "affects", "triggers", "contributes_to",
    "correlates_with", "suggests", "symptom_of", "has_symptom",
}
```

### C. 诊断谓词（DIAGNOSTIC_PREDICATES）

```python
DIAGNOSTIC_PREDICATES = {
    "detected_by", "investigates", "investigated_by", "checked", "found", "normal",
    "ruled_out", "no_correlation", "supports", "contradicts", "refines_to",
    "alternative_to", "confirmed_by", "repaired_by", "observed_by", "references",
    "preceded_by", "drifts_from", "measured_as", "deviates_from", "feedback_to",
}
```

| 大类 | 数量 | 核心谓词示例 |
|------|------|-------------|
| 结构谓词 | 9 | part_of, has_component, installed_on, monitored_by, controlled_by |
| 因果谓词 | 10 | caused_by, led_to, cascades_to, affects, triggers, correlates_with |
| 诊断谓词 | 21 | investigated_by, checked, found, normal, ruled_out, supports, contradicts |

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

`_assertion_semantics` 函数根据谓词类型和 fact 属性，自动推导断言的**极性（polarity）**和**认知状态（assertion_status）**：

```python
# extraction/pipeline.py
def _assertion_semantics(predicate: str, fact: Dict[str, Any], *,
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

抽取管线的实体链接阶段，调用 `_resolve_or_create` 函数实现 B-over-C 三层策略（详见第5章）：

```python
# extraction/pipeline.py
def _resolve_or_create(conn, scope, name, etype, description, thresholds,
                       model, context_text="", identity_context=None):
    # A层：别名精确命中
    # C层：向量召回 top-5
    # 阈值判断：merge_thr(0.85)直接合并 / new_thr(0.30)灰区LLM / 以下新建
    # 身份敏感匹配：传感器/部件编号必须一致
    # 灰区走 LLM 判定
```

```{mermaid}
sequenceDiagram
    participant E as Event
    participant LLM as LLM 抽取
    participant L as 实体链接
    participant DB as 数据库
    
    E->>LLM: 文本内容
    LLM->>LLM: 按 intent 选 prompt
    LLM->>LLM: 结构化输出 JSON
    LLM-->>L: entities + facts
    
    Note over L: 实体链接 B-over-C<br/>A层别名→C层向量→阈值/LLM
    
    L->>DB: 插入 facts
    L->>DB: 超替闭合
    L->>DB: Belief 聚合
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
    
    # Step 3: 实体链接 + facts + belief（短事务）
    with session_scope() as conn:
        for raw_fact in extraction["facts"]:
            pred = coerce_value(conn, scope, "predicate", raw_fact["predicate"])
            if pred is None:
                _quarantine(conn, event_id, raw_fact["predicate"], ...)
                continue
            # 实体链接 + 插入 fact + 超替 + belief 聚合
```

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
        H --> I[resolve_or_create]
        I --> J[insert_fact]
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

抽取完成（`embed_status='done'`）后，若 `cfg.higher_order.enabled` 开启，系统会遍历本次新写入 facts 涉及的每个 `subject_id`，分别为其 enqueue 一个 `higher_order` 归纳任务。Worker 后续消费这些任务时，收集该实体下相关的一阶事实，调用 LLM 合成高阶结论并写回 facts 表（`is_higher_order=true`，`evidence_fact_ids` 指向支撑它的一阶事实）。这是 Higher-Order 合成的异步入口，与在线抽取解耦，不会阻塞抽取主流程的返回。详见[第24章](24-higher-order)。

```python
# extraction/pipeline.py - extract_event 收尾阶段(pseudo)
if cfg.higher_order.enabled:
    for subject_id in extracted_subject_ids:
        enqueue_job("higher_order", scope=scope,
                    payload={"subject_id": str(subject_id),
                             "trigger_event_id": event_id})
```

## 关键代码结构

| 函数 | 位置 | 职责 |
|------|------|------|
| `extract_event` | pipeline.py:354 | 主入口，编排整个抽取流程 |
| `_llm_extract` | pipeline.py:774 | LLM 调用 + R1 fallback 链 |
| `_resolve_or_create` | pipeline.py:147 | B-over-C 实体链接 |
| `_insert_fact` | pipeline.py:312 | 插入事实三元组 |
| `_close_superseded` | pipeline.py:269 | 单值谓词超替闭合 |
| `_assertion_semantics` | pipeline.py:80 | 断言语义规则 |
| `coerce_value` | pipeline.py:131 | 词表约束 |
| `_aggregate_belief` | pipeline.py:627 | Belief 聚合 |
| `_direct_write_triple` | pipeline.py:734 | 三元组直写（不经 LLM） |
| `_quarantine` | pipeline.py:499 | 不合格数据隔离 |

# 第6章 谓词本体与断言语义

## 概述

cortex 的谓词体系是整个知识图谱的**语言基础**。所有抽取三元组必须使用预定义的谓词，确保图谱关系的一致性和可遍历性。

```{mermaid}
graph TB
    subgraph 谓词三大类
        S[结构/配置关系<br/>静态拓扑]
        C[因果/级联关系<br/>故障传播]
        D[诊断推理关系<br/>排查过程]
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
    P --> G
    A --> G
```

## Ontology 模块

所有谓词在 `ontology.py` 中集中定义，是单一真相源：

```python
# 结构/配置关系（静态拓扑）
STRUCTURAL_PREDICATES = {
    "part_of", "has_component", "installed_on", "located_in", 
    "monitored_by", "controlled_by", "regulates", "configured_as", "depends_on",
}

# 因果/级联关系（故障传播）
CAUSAL_PREDICATES = {
    "caused_by", "led_to", "cascades_to", "affects", "triggers", 
    "contributes_to", "correlates_with", "suggests", "symptom_of", "has_symptom",
}

# 诊断推理关系（排查过程）
DIAGNOSTIC_PREDICATES = {
    "detected_by", "investigates", "investigated_by", "checked", "found", "normal",
    "ruled_out", "no_correlation", "supports", "contradicts", "refines_to",
    "alternative_to", "confirmed_by", "repaired_by", "observed_by", "references",
    "preceded_by", "drifts_from", "measured_as", "deviates_from", "feedback_to",
}

# 状态关系（单值超替）
STATE_PREDICATES = {"has_status", "deal_stage"}

# 排除类（不进图）
OPPOSING_PREDICATES = {"ruled_out"}
RELATIONAL_EXCLUSION_PREDICATES = {"no_correlation", "contradicts"}
GRAPH_EXCLUDED_PREDICATES = OPPOSING_PREDICATES | RELATIONAL_EXCLUSION_PREDICATES

# 全部诊断谓词名
DIAGNOSIS_PREDICATE_NAMES = (
    STRUCTURAL_PREDICATES | CAUSAL_PREDICATES | DIAGNOSTIC_PREDICATES | STATE_PREDICATES
)
```

## 三大类谓词详解

### 1. 结构/配置关系（静态拓扑）

描述设备的结构层级和传感器-控制链路。

| 谓词 | 含义 | subject → object | 示例 |
|------|------|-------------------|------|
| `part_of` | A是B的组成部分 | component → subsystem | MFC-1 → 气体输送系统 |
| `has_component` | A包含B | equipment → subsystem | 温控系统 → 加热器H-1 |
| `installed_on` | A安装在B上 | sensor → component | T-101 → 腔体壁 |
| `located_in` | A位于B | component → subsystem | 匹配网络 → 射频系统 |
| `monitored_by` | A被B监测 | param/fault → sensor | 腔体温度 → T-101 |
| `controlled_by` | A被B控制 | component → controller | 加热器功率 → 温度PID |
| `regulates` | A调节B | controller → param | 温度PID → 基底温度 |
| `configured_as` | A配置为B | step → param | 主工艺步骤 → 射频功率1500W |
| `depends_on` | A依赖B | step/param → step/param | 主工艺步骤 → 预真空步骤 |

```{mermaid}
graph LR
    PID[温度PID] -->|regulates| TEMP[基底温度]
    TEMP -->|monitored_by| T101[T-101传感器]
    T101 -->|installed_on| WALL[腔体壁]
    HEATER[加热器H-1] -->|controlled_by| PID
    HEATER -->|part_of| SYS[温控系统]
```

### 2. 因果/级联关系（故障传播）

描述故障如何产生、传播和表现。

| 谓词 | 含义 | 示例 |
|------|------|------|
| `caused_by` | A的故障由B引起 | 腔体压力异常 → 密封圈老化 |
| `led_to` | A导致B | 密封圈老化 → 气体泄漏 |
| `cascades_to` | 级联传播（跨子系统） | 压力异常 → 等离子不稳定 |
| `has_symptom` | A故障表现为B | MFC响应延迟 → 流量阶梯式偏差 |
| `symptom_of` | A是B的征兆 | RF反射升高 → 匹配网络失调 |
| `affects` | A影响了B | 温度漂移 → 刻蚀速率 |
| `triggers` | A触发了B | 压力超限 → 互锁停机 |
| `correlates_with` | A与B有相关性 | MFC-1偏差 → 刻蚀速率漂移(r=0.85) |
| `suggests` | A暗示B | T-101周期性振荡 → PID参数失调 |

### 3. 诊断推理关系（排查过程）

描述工程师/agent 的排查过程和推理链。

| 谓词 | 含义 | 示例 |
|------|------|------|
| `investigates` | A排查了B | 怀疑真空泄漏 → 真空系统 |
| `checked` | A检查了B | 对比MFC设定值 → MFC-1实测流量 |
| `found` | A发现了B | 检查密封性 → T-101缓慢漂移5度 |
| `normal` | A正常（排除项） | 检查射频系统 → 射频系统(正常) |
| `ruled_out` | A被排除 | 假设射频故障 → 射频系统 |
| `supports` | A支持B | 相关性r=0.85 → MFC-1是根因 |
| `contradicts` | A反驳B | MFC-1校准合格 → 假设MFC校准漂移 |
| `confirmed_by` | A被B确认 | MFC-1是根因 → 参考案例-007 |
| `repaired_by` | A被B修复 | 密封失效 → 更换密封O-ring |

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

`_assertion_semantics` 函数在抽取管线中自动应用规则：

```python
def _assertion_semantics(predicate, fact, *, trusted=False, source_text=None):
    # 规则1：ruled_out 谓词 → 自动 negative + ruled_out
    if predicate in OPPOSING_PREDICATES:
        return "negative", "ruled_out"
    
    # 规则2：因果谓词
    if predicate in CAUSAL_PREDICATES:
        if polarity == "negative":
            return polarity, "ruled_out"
        # 有证据且可信 → confirmed
        if requested == "confirmed" and evidence and (trusted or grounded):
            return polarity, "confirmed"
        return polarity, "hypothesized"  # 默认假设
    
    # 规则3：非因果谓词 → 保留 LLM 指定值
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

状态类谓词是单值（新值超替旧值），其他是多值（多值共存）：

```python
PREDICATE_CARDINALITY = {
    predicate: ("single" if predicate in STATE_PREDICATES else "multi")
    for predicate in DIAGNOSIS_PREDICATE_NAMES
}
```

## 在抽取管线中的应用

抽取管线在写入 facts 前，通过 `coerce_value` 将 LLM 输出的谓词约束到预定义词表（如果 scope 有 predicate 词表），并通过 `_assertion_semantics` 自动设置 polarity 和 assertion_status：

```python
# 1. 谓词词表约束
predicate = coerce_value(conn, scope, "predicate", raw_predicate)
if predicate is None:
    skip  # 闭集未命中 → 跳过该 fact

# 2. 断言语义自动推断
polarity, assertion_status = _assertion_semantics(predicate, fact, ...)

# 3. 图准入检查
if not graph_eligible(predicate, polarity, assertion_status):
    # 该 fact 只存不进图（检索仍可命中 BM25）
    ...
```

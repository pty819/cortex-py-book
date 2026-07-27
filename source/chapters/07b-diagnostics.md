# 第7b章 诊断推理：Playbook DAG 与正向推理

## 为什么需要独立的 Diagnostics 模块

在第 6 章我们看到，cortex-py 的 facts 图有 22 个诊断谓词，可以表达"假设→排查→确认/排除"的排查过程。那为什么还需要一个独立的 `diagnostics` 模块？

核心原因：**facts 图是描述性的，diagnostics 是程序性的**。

| 维度 | Facts 图（诊断谓词） | Diagnostics Playbook |
|------|---------------------|---------------------|
| 内容本质 | "发生了什么"——历史排查记录 | "该怎么做"——审定的诊断规程 |
| 产生方式 | LLM 自动抽取 + 人工修正 | 领域专家创建 + 评审发布 |
| 确定性 | 有 assertion_status（hypothesis/confirmed/...） | 100% 确定性（DAG 遍历无 LLM 参与） |
| 修改方式 | 随新事件持续增长 | 版本化不可变追加（draft → active → retired） |
| 作用 | 历史检索、案例对比、因果推理 | 实时诊断决策支持、下一步建议 |

可以这样理解：**Facts 图是诊断案例库，Diagnostics 是诊断操作手册**。操作手册（playbook）指导工程师/agent 下一步该查什么，查完的结果再写回 facts 图积累为案例。两者形成闭环，但数据结构和生命周期完全独立。

```{mermaid}
graph LR
    subgraph Diagnostics 程序性知识
        PB[Playbook DAG<br/>审定的诊断流程] --> FR[Forward Reasoning<br/>确定性遍历]
        FR --> NA[Next Actions<br/>下一步查什么]
        FR --> REC[Recommendations<br/>可能的结论]
    end

    subgraph Facts 描述性知识
        EV[排查结果<br/>写入 events] --> EXT[抽取管线]
        EXT --> F[诊断谓词 facts<br/>investigates/checked/ruled_out/...]
        F --> CASE[Case 工作区<br/>断言-案例链接]
    end

    NA -->|工程师执行检查| EV
    CASE -->|积累案例| PB
    PB -.->|人工更新新版本| PB
```

---

## 模块架构

`diagnostics/` 包分为两层：

```
diagnostics/
├── engine.py              # 纯函数 DAG 引擎（无 DB，无状态）
│   ├── validate_graph()   # 校验 DAG 合法性（节点类型、边引用、无环）
│   ├── evaluate_condition()  # 条件表达式求值
│   ├── is_applicable()    # Playbook 适用性判断
│   └── walk_graph()       # 正向推理：BFS 遍历 DAG
└── forward_reasoning.py   # 持久化 + 版本管理（DB 访问层）
    ├── create_playbook()  # 创建 playbook + 首个版本
    ├── update_playbook()  # 追加新版本（不可变）
    ├── get_playbook()     # 读取 playbook（指定版本 or active）
    ├── list_playbooks()   # 列出 playbooks
    ├── forward_reason()   # 执行正向推理 + 持久化 run 记录
    ├── get_reasoning_run() # 查询推理 run 结果
    ├── export_playbook()  # 导出为 JSON（可迁移）
    └── import_playbook()  # 从 JSON 导入
```

**分层原则**：`engine.py` 完全纯函数，不依赖数据库——DAG 验证和遍历逻辑可以单测、可以离线使用。`forward_reasoning.py` 负责持久化，把纯函数的结果存进 `diagnostic_*` 表。

---

## Playbook DAG 数据模型

### 节点类型（6 种）

| 节点类型 | 含义 | 遍历行为 |
|---------|------|---------|
| `symptom` | 起始症状节点 | 入口节点，匹配症状后沿边继续 |
| `condition` | 条件判断节点 | 求值 condition，按 outcome 走不同边 |
| `test` | 待执行的检查/测试 | 匹配时加入 `next_actions`，沿边继续 |
| `action` | 待执行的操作 | 匹配时加入 `next_actions`，沿边继续 |
| `recommendation` | 推荐结论 | 匹配时加入 `recommendations`，可继续 |
| `terminal` | 终止节点 | 匹配后停止该分支遍历 |

### 边的 Outcome 类型

每条边有一个 `outcome` 字段，决定从源节点出发时，什么情况下走这条边：

| outcome | 触发条件 |
|---------|---------|
| `matched` | 节点 condition 求值为"匹配" |
| `not_matched` | 节点 condition 求值为"不匹配" |
| `unknown` | 节点 condition 有缺失输入，无法判断 |
| `always` | 无论什么状态都走（无条件边） |
| `default` | 没有匹配的特定 outcome 时走（兜底边） |

边的优先级：`always` > 特定 outcome > `default`。同 outcome 内的多条边按 `priority` 降序排列。

### 条件表达式（Condition JSON）

节点和边都可以带 `condition` 字段，是一个 JSON 对象，支持五种条件模式：

```json
{
  "all_symptoms": ["压力波动", "RF反射升高"],
  "any_symptoms": ["温度异常", "压力异常"],
  "none_symptoms": ["真空系统报警"],
  "observations": {
    "equipment_model": "E-301",
    "chamber": "C3"
  },
  "context": {
    "phase": "investigating",
    "day_shift": true
  }
}
```

| 模式 | 语义 |
|------|------|
| `all_symptoms` | 所有列出的症状都必须出现 |
| `any_symptoms` | 至少一个列出的症状出现 |
| `none_symptoms` | 列出的症状都不能出现 |
| `observations` | 观测数据字典必须包含指定键值（精确匹配，经 `normalize_term` 归一化） |
| `context` | 上下文字典必须包含指定键值 |

**症状匹配规则**：使用 `terminology.normalize_term()` 归一化后比较。支持子串匹配——如果 query term 包含在症状项中或症状项包含在 query term 中，都算命中。

**求值结果**：
- `matched` — 所有条件满足，无缺失
- `not_matched` — 至少一个条件不满足
- `unknown` — 有缺失的 key（observations/context 中缺少）

---

## DAG 验证（validate_graph）

创建或更新 playbook 时，`validate_graph()` 会做完整的合法性检查：

1. **节点校验**：至少 1 个节点、key 唯一非空、类型合法、title 非空
2. **边校验**：引用的 from/to 节点必须存在、禁止自环
3. **无环校验**：Kahn 拓扑排序（BFS 入度递减法），若遍历节点数 ≠ 总结点数则报错
4. **入口节点**：`entry_nodes` 指定的节点必须存在；若未指定，自动取所有入度为 0 的节点

```python
def validate_graph(nodes, edges, entry_nodes=None):
    # 返回 (node_list, edge_list, entry_nodes)
    # 不合法则 raise ValueError
```

---

## 正向推理（walk_graph）

`walk_graph()` 是核心的 BFS 遍历引擎，从 entry nodes 出发逐层推进。

### 遍历算法

```
输入：graph（节点+边+入口）, symptom_terms, observations, context
输出：{trace, next_actions, recommendations, unresolved_inputs}

队列 = [entry_nodes]
已访问 = {}
trace = []
next_actions = []
recommendations = []
unresolved = []

while 队列非空 and 已访问 < 200:
    node = 队列出队
    if node 已访问: continue
    标记已访问

    result = evaluate_condition(node.condition, symptom_terms, observations, context)
    trace.append({node_key, node_type, title, evaluation=result})
    unresolved += result.missing

    if result.state == "matched":
        if node.type in {test, action}:
            next_actions.append(node)
        if node.type == "recommendation":
            recommendations.append(node.recommendation)

    # 选出边：根据 result.state 选 outcome 匹配的边
    outgoing_edges = select_edges(node.outgoing, result.state)
    过滤出 condition 为 matched 的边
    按 priority 排序
    所有目标节点入队
```

### 输出结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `trace` | list | 完整遍历轨迹，每个节点的求值结果 |
| `next_actions` | list | 下一步应执行的检查/操作（按 priority 降序） |
| `recommendations` | list | 推荐结论（按 priority 降序） |
| `unresolved_inputs` | list | 缺失的观测/上下文输入（需要补充信息） |

`next_actions` 的每个元素包含：
```python
{
    "node_key": "check_mfc_calibration",
    "type": "test",              # test 或 action
    "title": "检查 MFC-1 校准状态",
    "description": "对比 MFC-1 设定值与实测流量...",
    "priority": 10,
    "recommendation": {...}      # 操作建议详情
}
```

### 200 节点安全边界

遍历设置了 200 节点上限防止病态 DAG 无限循环（虽然 DAG 不会有环，但超大图也可能导致遍历过深）。正常的诊断 playbook 一般在 20-50 个节点范围内。

---

## Playbook 版本管理

每个 Playbook 可以有多个版本，版本号从 1 开始递增。**版本不可变**——一旦创建，节点和边就不能修改，只能追加新版本。

### 版本状态

| 状态 | 含义 | 正向推理可用？ |
|------|------|:-------------:|
| `draft` | 草稿，编辑中 | ❌ |
| `active` | 已发布，当前生效版本 | ✅ |
| `retired` | 已退役，历史保留 | ❌ |

### 生命周期

```
创建 → v1 (draft)
  ↓ 激活
v1 (active) ← 正向推理使用此版本
  ↓ 更新（追加新版本，不修改 v1）
v2 (draft)
  ↓ 激活
v1 (retired)  ← 历史保留，推理 run 仍可引用
v2 (active)   ← 切换为当前生效版本
  ↓ ...
vN (retired)
```

### 为什么不可变

1. **可追溯性**：每次正向推理 run 都记录了 `version`，永远知道是用哪个版本的规程得出的结论
2. **安全性**：不会出现"改了 playbook 导致历史 run 结果对不上"的情况
3. **审计合规**：诊断规程的每一次变更都有明确版本号和变更记录

---

## 传感器解析（Sensor Resolve）

虽然 `sensor_resolve` 在 API 路由层（`routes/sensor_resolve.py`）而非 `diagnostics/` 包，但它是诊断场景下非常实用的辅助功能——把自然语言查询翻译成具体的传感器列表。

### 四步管线

```
自然语言查询
  ↓ Step 1: LLM 解析
查询项列表（如 ["腔体压力", "RF功率"]）
  ↓ Step 2: 向量检索（每项 top-1 entity）
匹配到的实体（entity_id + name + type）
  ↓ Step 3: BFS 沿结构谓词出边（最多 5 跳）
收集所有 entity_type='sensor' 的节点
  ↓ Step 4: 去重排序
关联传感器名称列表
```

### 关键设计

- **LLM 解析**：有 LLM 时用结构化输出提取查询项；无 LLM 时 fallback 到按分隔符切分（中文逗号、顿号、英文逗号、分号、空格）
- **向量检索 top-1**：每项只取最相似的实体，用余弦距离 `<=>`
- **结构谓词 BFS**：只沿 `STRUCTURAL_PREDICATES`（8 个结构谓词）出边遍历，sensor 节点是终止符（收集并停止该分支）
- **5 跳上限**：防止结构太深导致 BFS 爆炸，一般设备层级 3-4 跳足够

### 端点

```
POST /v1/sensors/resolve
  body: { scope, query }
  返回: { query, parsed_items, matched_entities, sensors }
```

MCP 工具：`sensor_resolve(scope, query)`（如果暴露的话，实际通过 `diagnostic_forward_reason` 间接使用）

---

## 与 Facts 图的协同

Diagnostics 和 Facts 图虽然数据独立，但在工作流中紧密协同：

### 场景：从 Playbook 到 Facts

```
工程师收到"压力异常"告警
  → diagnostic_forward_reason(symptoms=["压力异常"])
  → 返回 next_actions: ["检查 MFC-1 流量", "检查真空泵电流"]
  → 工程师执行检查，结果写入事件（append_event）
  → 抽取管线从事件中提取诊断谓词 facts：
       MFC-1 checked 正常
       真空泵电流 found 偏高
       假设密封失效 investigates 真空系统
  → facts 写入图谱，关联到当前 Case
```

### 场景：从 Facts 到 Playbook

```
积累了足够多的同类故障 Case
  → 领域专家分析案例模式
  → 将模式固化为 Playbook DAG
  → create_playbook / update_playbook 发布新版本
  → 新故障发生时，正向推理直接复用专家经验
```

### 推荐结论的实体引用

`recommendation` 节点的 `targets` 字段可以包含 `entity_id`，指向 facts 图中的具体实体（如 "MFC-1"、"密封圈"）。这样正向推理的推荐结论可以和知识图谱实体直接关联，支持点击跳转查看实体详情和历史故障。

---

## 数据持久化（5 张表）

| 表 | 内容 |
|----|------|
| `diagnostic_playbooks` | playbook 元数据（名称、描述、当前状态、active_version、适用性） |
| `diagnostic_playbook_versions` | 版本记录（版本号、状态、entry_nodes、创建人） |
| `diagnostic_playbook_nodes` | 各版本的节点 DAG（node_key、node_type、title、condition、recommendation、priority） |
| `diagnostic_playbook_edges` | 各版本的边（from/to 节点、outcome、condition、priority） |
| `diagnostic_reasoning_runs` | 每次正向推理的运行记录（输入症状、观测数据、trace、next_actions、recommendations） |

所有表都有 `scope` 字段，支持 scope 级隔离。

---

## 设计哲学总结

1. **程序知识与描述知识分离**：Facts 图描述世界是什么样，Playbook 描述该怎么做。前者 LLM 抽取自动增长，后者人工审定版本发布。
2. **确定性推理**：正向推理是纯 DAG 遍历，不调用 LLM，不产生幻觉，结果可重复。
3. **不可变版本**：Playbook 版本追加而非覆盖，保证推理结果的可追溯性。
4. **渐进式诊断**：输出 `next_actions` 而非直接给出答案——引导工程师/agent 一步步排查，通过 `unresolved_inputs` 明确指出还缺什么信息。
5. **与事实图协同但解耦**：Playbook 的推荐可以指向 facts 图实体，但不修改图谱——两边各做各的，通过工作流闭环。

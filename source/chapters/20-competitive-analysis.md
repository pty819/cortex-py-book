# 第20章 竞品分析：Agent 记忆/知识图谱系统在复杂故障诊断场景的能力边界

## 为什么要做这个对比

本章不是"我的项目比别人的好"式的宣传——而是**针对复杂机械故障诊断这个特定场景**，逐条分析现有主流记忆/知识图谱系统的实际能力边界。

为什么要做？因为当你在构建一个**面向设备诊断的知识图谱**时，你会发现：
- **Mem0** 排名第一时 59k star——但它能区分"怀疑"和"确认"吗？
- **Graphiti** 有 Neo4j 时态图——但它能处理"腔体C1的MFC-1"和"腔体C3的MFC-1"是不同实体吗？
- **agentmemory** R@5 达到 95.2%——但它的"知识图谱"是搜索增强用的，不是因果推理用的
- **OpenViking** 卖了"上下文文件系统"的概念——但文件路径能替代谓词约束吗？

答案需要进入**实际源码和架构层面**才能给出。

---

## 1. Mem0：59k star 的通用记忆层，但它的"记忆"是扁平的

### 1.1 实际架构

论文（arXiv 2504.19413）描述了两条架构线：

**Mem0（密集记忆）——两阶段管线：**

```
add() 调用
  │
  ├── 阶段1：抽取（Extraction）
  │    输入：当前对话 (m_{t-1}, m_t) + 最近的 m=10 条消息 + 对话摘要 S
  │    LLM 调用：GPT-4o-mini 做函数调用，抽出事实 Ω = {ω₁, ..., ωₙ}
  │
  └── 阶段2：更新（Update）
        对每个 ωᵢ:
          1. 检索 top-s=10 条语义最相似的历史记忆
          2. LLM 选一个操作：ADD / UPDATE / DELETE / NOOP
          3. 执行——写向量库
```

**Mem0^g（图记忆变体，2026年4月v3重构后已移除）——专属的三元组图管线：**

```
add() 调用
  ├── 阶段1：实体提取器（Entity Extractor）
  │    从对话中识别实体（人物、地点、物品等）
  ├── 阶段2：关系生成器（Relationship Generator）
  │    创建三元组 (subject, predicate, object) 和类型标签
  ├── 冲突检测（Conflict Detection）
  │    LLM 判断新旧三元组是否冲突，标记旧关系为 invalid（不物理删除）
  │    注：存在 Neo4j 中
  └── 检索：双通道
      ├── 实体中心：定位查询中的实体 → 相似度匹配 → 探索连接 → 构建子图
      └── 语义三元组：编码查询 → 匹配所有三元组 → 阈值过滤
```

关键细节：`Update` 阶段的 ADD/UPDATE/DELETE/NOOP 决策**完全由 LLM 判断**——Mem0 没有在产品层添加额外的编排逻辑。

**2026年4月重构后的 v3 算法（当前版本）：**

```
add() 调用（新算法）
  ├── 检索 top-10 相关记忆（用于去重上下文）
  ├── 单次 LLM 调用：从输入+上下文中抽取出所有新事实
  ├── 批量 embedding 所有新记忆
  ├── 基于 MD5 hash 的去重（防止精确重复）
  ├── 批量写入向量库
  └── Entity Linking：从每条记忆提取实体（专有名词、引号文本、复合名词短语）
      存入 {collection}_entities 集合（同一个向量库的平行集合）
      检索时：从查询中提取实体 → 和 {collection}_entities 匹配 → 提升命中实体的记忆分数
```

**移除点**：v3 删除了 ~4000 行的外部图存储代码（包括 Neo4j 集成、关系检索等）。Mem0 团队的官方解释是"实体连接现在通过检索排名应用，而不是作为单独的、可直接遍历的结构暴露"。

### 1.2 抽取能力的实际边界

```python
# Mem0 的 add() 核心逻辑（伪代码，基于论文和源码结构）
def add(self, messages, user_id):
    # step 1: 抽取
    summary = self._get_summary(user_id)  # 对话摘要
    recent = self._get_recent(user_id, m=10)  # 最近 10 条
    extracted_facts = self.llm.extract(messages, summary, recent)
    
    # step 2: 更新（每个事实走 LLM 判断）
    for fact in extracted_facts:
        similar = self.vector_store.search(fact, top_k=10)
        operation = self.llm.decide_operation(fact, similar)
        if operation == "ADD":
            self.vector_store.insert(fact)
        elif operation == "UPDATE":
            self.vector_store.update(similar[0], fact)
        elif operation == "DELETE":
            self.vector_store.delete(similar[0])
        # NOOP = 不做
```

**关键限制分析：**

| 能力 | Mem0 能做到吗 | 为什么 |
|------|:-----------:|--------|
| 区分"猜测"和"确认" | ❌ | 所有事实平等存储。`add()` 不接收语义状态参数，存储层无对应的列或属性 |
| 跨设备同名实体隔离 | ❌ | 隔离基于 `user_id` 字符串。没有"设备身份上下文"的概念 |
| 因果链多跳遍历 | ❌ v2/v3 / ⚠️ Mem0^g | v3 已经移除了图存；Mem0^g 的图在 Neo4j 里可以做 Cypher 查询，但节点没有认知状态字段，遍历时无法区分 hypothesis/confirmed |
| 谓词约束 | ❌ | predicate 是自由文本。没有预定义词表、没有 quarantined 机制 |
| 数值参数提取 | ❌ | 没有量纲感知。`"功率1500W"` 是一个文本 token，不会和 `"功率1600W"` 自动区分 |
| 属性组合查询 | ❌ | 检索只靠语义相似度 + 实体匹配。不能做"predicate=caused_by AND assertion_status=confirmed" 的结构化过滤 |

**对诊断场景的判断**：Mem0 设计目标是**用户偏好记忆**——记住"Alice 喜欢意大利菜"。它的抽取管线是为对话场景调优的，能够提取事实"Alice likes Italian food"，并通过四个操作（ADD/UPDATE/DELETE/NOOP）管理事实演变。但对于复杂设备诊断，它没有：
- 实体类型体系（不知道 MFC-1 是 component 还是 sensor）
- 身份上下文（不知道腔体 C1 和 C3 是两个隔离域）
- 断言语义（不知道"怀疑"和"已排除"是两个不同的认知状态）

|Mem0 论文中 Mem0^g 在 LOCOMO 上比基本 Mem0 整体提升约 2%（68.44 vs 66.88），但在单跳和多跳问题中并没有显著优势，反而检索慢了约 3 倍、token 消耗约 2 倍。这个数据本身已经说明：**它当前的图记忆层的推理能力不足以支撑复杂的关系推理。**

---

## 2. Graphiti (Zep)：真正的时态知识图谱，但实体是"通用"的

### 2.1 实际架构

Graphiti 的核心是一个**基于 Neo4j 的时态知识图谱引擎**。它的管线深度远大于 Mem0：

```
add_episode() 调用
  │
  ├── 1. extract_nodes()
  │    LLM 从 episode 文本中提取实体节点
  │    → 返回 [EntityNode, ...]（含 name, summary, embedding）
  │
  ├── 2. resolve_extracted_nodes()
  │    LLM 判断每个新节点是否匹配已有节点
  │    → 去重/合并决策（不是简单向量阈值，而是 LLM 语义判断）
  │
  ├── 3. extract_edges()
  │    LLM 提取实体之间的关系边
  │    → 返回 [EntityEdge, ...]（含 fact, valid_at, invalid_at）
  │
  ├── 4. resolve_extracted_edges()
  │    LLM 判断每条边是否匹配已有边
  │    → 去重/识别属性
  │
  ├── 5. extract_timestamps()（2026年重构后拆出）
  │    专门提取 valid_at / invalid_at 时间戳
  │
  ├── 6. 写入 Neo4j
  │    节点：EntityNode (labels, summary, embedding)
  │    边：EntityEdge —RELATES_TO— (fact, valid_at, invalid_at, embedding)
  │    溯源：EpisodicNode (原始文本，指向产生的节点和边)
  │
  └── 7. 可选：build_communities()
       社区检测 → 生成社区摘要
```

**节点和边的数据结构（基于源码）：**

```python
# graphiti_core/nodes/entity_node.py
class EntityNode(BaseNode):
    name: str                      # 实体名
    summary: str                   # LLM 生成的摘要，随时间更新
    embedding: Optional[list]      # 向量
    entity_type: str               # 实体类型（自定义 Pydantic 模型定义）
    created_at: datetime
    # 无 identity_context / context_key 字段！
    
# graphiti_core/nodes/entity_edge.py  
class EntityEdge(BaseNode):
    fact: str                      # 事实文本
    valid_at: Optional[datetime]   # 有效开始时间
    invalid_at: Optional[datetime] # 有效结束时间
    embedding: Optional[list]
    # 有 temporal 字段，但无 polarity/assertion_status！
```

### 2.2 核心能力边界

| 能力 | Graphiti 能做到吗 | 为什么 |
|------|:--------------:|--------|
| **双时态跟踪** | ✅ 完整 | `valid_at` / `invalid_at` 明确记录时间窗口，支持 time-point 查询 |
| **因果链多跳遍历** | ✅ Cypher 递归查询 | Neo4j 原生支持的递归 CTE 可以任意深度 |
| **自定义实体类型** | ✅ Pydantic 模型 | 可以用 `class MyEntity(EntityNode): ...` 扩展字段 |
| **自定义谓词/边类型** | ✅ `custom_edge_types` | 可以用 `edge_type_map` 控制哪些实体类型之间允许哪些边 |
| **溯源到原始 episode** | ✅ `EpisodicNode` | 每条边可追溯到产生它的原始文本 |
| ****跨设备同名实体隔离** | ❌ | EntityNode 没有身份上下文字段。两个腔体的 'MFC-1' 在 Neo4j 中被判为同一个节点 |
| **断言语义（hypothesis vs confirmed）** | ❌ | EntityEdge 没有 assertion_status 字段。新旧边通过时间窗口区分（新边覆盖旧边），但"假设"和"确认"的认知差异没有表达方式 |
| **谓词闭集约束** | ❌ | Edge 的 fact 文本是 LLM 自由生成的。语义上可以约束类型名，但无法禁止 LLM 生成 `caused_by` 和 `led_to` 混用的边 |
| **排除链保留不污染图** | ❌ | `invalid_at` 标记的边只是"在时间上失效"，不是"被排除"。"已排除的假设"和"已过时的结论"在图中用同一个机制表达 |
| **不依赖 Neo4j** | ❌ 强依赖 | 需要 Neo4j（或 FalkorDB/Neptune）实例运行 |
| **不依赖 LLM** | ❌ 强依赖 | 所有抽取和去重决策依赖 LLM structured output。官方文档明确说不适用于小模型 {"entity_name": "Alice", "reason": "Works at OpenAI from conversation context"} → 一次 LLM 调用 |

### 2.3 一个真实的对比场景

输入："检查MFC-1发现流量偏差5%，怀疑是MFC校准漂移。检查MFC-2发现正常，已排除MFC-2。"

```
Graphiti 处理结果：
  节点：MFC-1, MFC-2, 流量偏差5%, MFC校准漂移
  边：  MFC-1 --RELATES_TO--> 流量偏差5%  
        MFC-1 --RELATES_TO--> MFC校准漂移
        MFC-2 --RELATES_TO--> 正常
        
  问题：两条边缺乏"这是假设"还是"这是确认"的标记。
       "MFC校准漂移"只有一个有效性时间窗口。
       检索时无法区分"这是一个被验证的结论"和"这是一个尚未验证的猜想"。

Cortex-PY 处理结果：
  实体：MFC-1(component, identity_context={chamber:C3})
        MFC-2(component, identity_context={chamber:C3})
        流量偏差5%(symptom)
        MFC校准漂移(hypothesis)          ← 类型不同！
  事实：MFC-1 monitored_by 流量偏差5%    ← observed, 入图
        流量偏差5% investigated_by 怀疑MFC校准漂移 ← diagnostic, 入图
        MFC-1 caused_by MFC校准漂移      ← hypothesized, **不入图**
        MFC-2 checked 正常              ← observed, 入图
        MFC校准漂移 ruled_out MFC-2     ← negative+ruled_out, **不入图**
  
  图遍历时的结果：
    BFS 从"流量偏差5%"出发 2 跳 → MFC-1, MFC校准漂移(但因为是hypothesized所以不走它的因果边)
    Cypher 查询"所有被排除了的嫌疑" → MFC-2正常 → MFC校准漂移(被排除)
```

---

## 3. agentmemory (rohitg00)：编程 agent 记忆的天才设计，但和诊断无关

### 3.1 实际架构

agentmemory 不是一个知识图谱系统——它是一个**编程 agent 会话记忆系统**。构建在 iii-engine 的 Worker/Function/Trigger 三个原语之上。

**核心：4 层记忆整合管线**

```
PostToolUse 钩子触发
  │
  ├── 1. SHA-256 去重（5 分钟窗口） → 防止同一信息重复存储
  ├── 2. 隐私过滤器 → 剥离 secrets、API keys、<private> 标签内容
  ├── 3. 存储原始观察 → Working 层级
  ├── 4. LLM 压缩 → 结构化事实 + 概念 + 叙述 → Semantic 层级
  ├── 5. 向量嵌入（6 个 provider + 本地 all-MiniLM-L6-v2）
  └── 6. BM25 + 向量索引

SessionEnd 钩子触发 → 知识图谱提取（可选 GRAPH_EXTRACTION_ENABLED=true）
SessionStart 钩子触发 → 加载项目画像 + 混合检索（BM25+向量+图）+ token budget 2000

4 层整合（Tiered Consolidation）：
  Working  → 原始观察（短期）
  Episodic → 压缩后的会话摘要（"发生了什么"）
  Semantic → 提取的事实和模式（"我知道了什么"）
  Procedural → 工作流和决策模式（"怎么做"）
```

**混合检索（Triple-Stream Hybrid Search）：**

```
memory_smart_search(query)
  ├── BM25 全文检索（关键词匹配）
  ├── 向量检索（语义相似度，local all-MiniLM-L6-v2 零成本）
  ├── 图检索（BFS → Dijkstra 带权边遍历，0.1-1.0 边权重）
  └── RRF 融合 → 排序结果
```

注意：这里的"图"不是关系型知识图谱的图——它是由记忆之间的共现/引用关系构成的**关联图**（"这段记忆引用了那段记忆"），而不是"MFC-1 安装在腔体 C3 上"这样的语义边。

### 3.2 为什么它和诊断场景不相关

| 能力 | agentmemory | 诊断是否需要 |
|------|:----------:|:----------:|
| **实体类型区分** | ❌ 无（entity 只是文本片段） | ✅ 核心 |
| **谓词约束** | ❌ 无 | ✅ 核心 |
| **身份上下文隔离** | ❌ 无（按 agent_id 隔离） | ✅ 核心 |
| **因果链图遍历** | ⚠️ 关联图，不是语义图 | ❌ 不满足 |
| **双时态** | ❌ 无 | ✅ |
| **零外部依赖** | ✅ SQLite + iii-engine 全部内建 | ✅ 加分项 |
| **低延迟、高 R@5(95.2%)** | ✅ LongMemEval 最优之一 | ❌ 需求不匹配 |

agentmemory 的设计目标是**让编码 agent 记住会话上下文**——"这个项目的 JWT 认证用的是什么库"、"之前修复过什么 bug"。它的 95.2% R@5 在 LongMemEval 上证明了对这种任务的搜索有效性。但它的"知识图谱"本质上是**记忆之间的引用关系图**，而不是**实体之间的语义关系图**——这两个"图"在检索行为上有本质区别。

---

## 4. OpenViking (字节跳动)：上下文文件系统，不是知识图谱

### 4.1 实际架构

OpenViking 的核心是将上下文组织为**虚拟文件系统**，通过 `viking://` URI 访问。

**架构分层（从外到内）：**

```
Agent 层
  │  viking:// 协议命令 (ls/find/tree/add-resource)
  ↓
OpenViking Server 层
  │  身份验证 + 任务调度 + 并发控制
  ↓
AGFS / RAGFS 层 (Abstract Graph File System)
  │  上下文管理核心——文件树遍历、递归检索、路径定位
  ↓
存储层
  ├── VikingDB（向量存储，用于语义搜索）
  └── 本地文件系统 + S3（原文存储）
```

**三级内容分级（L0/L1/L2）：**

```
L0 (Abstract) - ~100 tokens
  "E-301 刻蚀机 腔体C3 压力异常事件"
  
L1 (Overview) - ~2000 tokens
  "2026-06-18 E-301 C3 发现刻蚀速率漂移15.1%，怀疑MFC..."
  
L2 (Detail) - 全文
  "完整的故障排查报告..."
```

**记忆提取：session 结束后的异步后处理：**
```
Session 结束
  → 分析任务执行结果和用户反馈
  → 压缩对话内容、工具调用、资源引用
  → 提取长期记忆写入 viking://user/{id}/memories/
  → 提取操作经验写入 viking://agent/skills/
```

### 4.2 能力边界

| 能力 | OpenViking | 诊断是否需要 |
|------|:---------:|:----------:|
| **层次化上下文管理** | ✅ 核心设计——URI 树+L0/L1/L2 三级 | ✅ 加分（控制token） |
| **多类型数据源** | ✅ resource 目录统一管理代码/文档/网页 | ✅ |
| **目录递归检索** | ✅ intent→定位目录→dir内检索→下钻→聚合 | ⚠️ 适合但不够精确 |
| **实体关系推理** | ❌ 无 | ✅ 核心 |
| **因果链遍历** | ❌ 文件树不是因果图 | ✅ 核心 |
| **身份上下文隔离** | ✅ 通过 URI 路径天然隔离 (`viking://user/{id}/...`) | ✅ |
| **谓词约束/断言语义** | ❌ 无 | ✅ 核心 |
| **双时态** | ❌ 有版本快照但无精细化时间窗口 | ✅ |

**关键认识**：OpenViking 的设计哲学和 Cortex-PY 是**正交互补**的。它回答的是"上下文在哪、怎么按需加载"，Cortex-PY 回答的是"知识是什么、怎么推理"。两者可以分层共存——OpenViking 管理原始文档，Cortex-PY 从文档中构建可推理的因果图。

---

## 6. 诊断场景的六个"不可妥协"的技术需求

综合以上分析，复杂机械故障诊断的知识图谱系统在技术上有六个**不可妥协**的需求。每个需求对应一个设计决策：

### 需求 1：实体必须有类型体系，且类型之间有关系约束

```
为什么：
  诊断推理不是"从文本中找名词"——而是 
  "sensor monitored_by process_param" 和 
  "fault caused_by component" 是不同层面的关系。
  
  如果 LLM 把 sensor 和 fault 混在一起当"名词"处理，
  "温度传感器"和"温度异常"在语义上很接近，
  但在诊断意义上完全不同。
```

**谁做到了**：Cortex-PY（16 种预定义类型 + 40+ 谓词约束）、Graphiti（可自定义 Pydantic 实体/边类型，但无约束机制）

### 需求 2：同名实体在不同设备中必须是不同实体

```
为什么：
  "MFC-1" 在腔体 C1 中和腔体 C3 中连接不同的传感器、
  控制不同的参数、关联不同的故障历史。
  如果合并为同一个人实体，因果链交叉污染，推理结果毫无意义。
```

**谁做到了**：Cortex-PY（6 字段 identity_context）、OpenViking（URI 路径天然隔离）

### 需求 3：断言必须有认知状态——不仅仅是时间

```
为什么：
  "怀疑密封圈老化" (hypothesis)
  "密封圈老化已确认" (confirmed)  
  "密封圈老化已被排除" (ruled_out)
  
  这三条不是时间先后关系——它们是对同一个命题的不同认知。
  时间窗口无法表达这种差异（三个的时间窗口可以完全重叠）。
```

**谁做到了**：Cortex-PY（5 种 assertion_status + polarity）——**唯一**。

### 需求 4：被排除的嫌疑不能进因果图

```
为什么：
  如果你排除了 MFC-1 校准漂移，这条排除信息必须在历史中可查
  （让以后的人知道"我们已经查过 MFC-1 了"），
  但不能出现在因果推理路径中
  （否则图遍历会把"MFC-1 校准漂移"当成"压力异常"的根因）。
```

**谁做到了**：Cortex-PY（graph_eligible + ruled_out 谓词自动 negative）——**唯一**。

### 需求 5：检索必须能组合"事实属性"条件

```
为什么：
  诊断检索不是"找语义相似的段落"——而是：
  "找所有 predicate='caused_by' AND assertion_status='confirmed' AND 
   subject 的类型是 'fault' 的边"
  
  语义相似度只能回答"这个文本像什么"，
  不能回答"这条关系的认知状态是什么"。
```

**谁做到了**：Cortex-PY（facts 表结构化字段可直接 SQL WHERE 过滤）、Graphiti/neo4j-agent-memory（Cypher 可拼属性条件）

### 需求 6：排查时序必须可重建

```
为什么：
  诊断不是多篇文档的时间排序——而是一个迭代收敛过程：
  
  第1轮：怀疑气体系统 → 检查MFC → MFC正常 → 排除气体系统
  第2轮：怀疑真空系统 → RGA发现聚合物 → 细化到腔体积碳
  第3轮：干法清洗 → 恢复 → 根因确认
  
  每轮之间的"假设→验证→排除/细化"关系不是时间顺序，
  而是推理逻辑。
```

**谁做到了**：Cortex-PY（diagnostic predicates + case/phase 系统）——**唯一**。

---

## 7. 五系统综合对比总表

| 诊断需求 | **Cortex-PY** | **Mem0** | **Graphiti** | **OpenViking** | **agentmemory** |
|---------|:-----------:|:-------:|:-----------:|:-------------:|:--------------:|
| **实体类型体系**（16种专用+命名规范） | ✅✅ | ❌ | ⚠️ 自定义 Pydantic 但无约束 | ❌ | ❌ |
| **身份上下文隔离**（跨设备同名隔离） | ✅✅ 6字段 | ❌ user_id仅 | ❌ | ✅ URI路径 | ❌ |
| **断言认知状态**（hypothesis≠confirmed≠ruled_out） | ✅✅ **唯一实现** | ❌ | ❌ | ❌ | ❌ |
| **排除项不入因果图**（graph_eligible 过滤） | ✅✅ **唯一实现** | ❌ | ❌ | ❌ | ❌ |
| **因果链多跳遍历** | ✅ 递归CTE BFS | ❌ | ✅✅ Neo4j Cypher | ❌ | ⚠️ 关联图非语义图 |
| **谓词闭集约束**（40+标准谓词，未命中隔离） | ✅✅ **唯一实现** | ❌ | ⚠️ 自定义但无强制执行 | ❌ | ❌ |
| **双时态**（valid+recorded 4字段） | ✅✅ | ❌ | ✅✅ valid_at+invalid_at | ❌ | ❌ |
| **排查时序重建**（case+phase+诊断谓词链） | ✅✅ **唯一实现** | ❌ | ❌ | ❌ | ❌ |
| **属性组合检索**（predicate+status 结构化过滤） | ✅✅ SQL WHERE | ❌ | ✅ Cypher | ❌ | ❌ |
| **数值参数量纲识别**（1500W≠1600W自动区分） | ✅✅ | ❌ | ❌ | ❌ | ❌ |
| **多类型数据源融合**（3入口+5导入器+triple直写） | ✅✅ | ❌ | ❌ | ✅ FS统一管 | ❌ |
| **轻量部署**（不需要额外图数据库） | ✅ PostgreSQL | ✅✅ SaaS/Redis | ❌ 需Neo4j | ✅ FS | ✅✅ SQLite |
| **零 LLM 依赖运行**（mock抽取+本地embedding） | ⚠️ 基本可用 | ❌ | ❌ | ❌ | ✅✅ 全本地 |
| **图准入规则**（因果confirmed才进BFS） | ✅✅ **唯一实现** | ❌ | ❌ | ❌ | ❌ |
| **RAG 检索（搜索相关文档）** | ✅ 6通道+RRF+rerank | ✅✅ 多信号融合 | ✅ 混合融合 | ✅ 目录递归 | ✅✅ BM25+向量+图 |
| **token 节省（按需加载）** | ❌ 无 | ✅ 提取+摘要 | ❌ | ✅✅ L0/L1/L2 | ✅✅ 2000t budget |
| **SaaS 版本** | ❌ | ✅ Mem0 Cloud | ✅ Zep | ❌ | ❌ |

> ✅✅ = 强支持、且该场景下唯此家有；✅ = 支持但有局限；⚠️ = 有但不够；❌ = 不支持

---

## 8. 决策树

```
你的场景需要？
│
├── 记住用户偏好和对话历史
│   └── Mem0 / agentmemory（轻量快速）
│
├── 管理 agent 上下文窗口、按需加载文档
│   └── OpenViking（viking:// 文件系统）
│
├── 理解实体关系随时间的变化（CRM/客户画像）
│   └── Graphiti（时态图）
│
├── 编程 session 跨会话记忆
│   └── agentmemory（零外部依赖 + 自动钩子）
│
└── 复杂机械设备的故障诊断因果推理
    └── Cortex-PY（唯一满足6项不可妥协需求的系统）
        │
        ├── 需要管理原始文档上下文？
        │   可以配合 OpenViking
        ├── 需要记用户偏好？
        │   可以配合 Mem0
        └── 需要快速上手原型？
            先把架构消化——Cortex-PY 的领域深度换来的是更高的理解门槛
```

---

## 9. 附：各系统的"不可"清单

| 系统 | 它**不能**做到什么 |
|------|-----------------|
| **Mem0** | 不能区分"怀疑"和"确认"；不能处理跨设备同名实体；不能做因果推理图遍历（v3已移除图存储） |
| **Graphiti** | 不能区分假设和确认（只有时间窗口没有认知状态）；不能隔离跨设备同名字体（EntityNode 无身份上下文）；不能同时跑于无 Neo4j 环境 |
| **agentmemory** | 不是知识图谱系统——它的"图"是记忆引用图，不是语义关系图；不能做谓词约束；没有实体类型体系 |
| **OpenViking** | 没有实体关系推理；没有谓词/断言概念；没有因果图——它是文件系统不是图谱 |
| **Cortex-PY** | 不能做通用 agent 偏好记忆（没设计这个）；不能做上下文窗口管理（不是 OpenViking）；不能做 streaming 低延迟记忆 |

---

> **各系统的本质差异一句话**：
> - Mem0 是**记忆**（记住用户说过什么）
> - Graphiti 是**时间机器**（事实怎么变化）
> - agentmemory 是**备忘**（编程 agent 的跨会话上下文）
> - OpenViking 是**文件柜**（上下文按路径组织）
> - **Cortex-PY 是故障诊断推理机**（结构化的因果图 + 每一步该信多少）

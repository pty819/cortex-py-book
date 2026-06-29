# 第20章 竞品分析：Agent 记忆/上下文系统全面对比

## 为什么要做这个对比

Cortex-PY 不是一个诞生在真空中项目。当决定构建它时（而不是直接使用已有方案），有必要理解它和其他系统的本质差异——它解决什么问题、不解决什么问题、在什么场景下是不可替代的。

本章从**技术架构**、**设计哲学**、**数据模型**、**检索策略**、**部署取舍**五个维度，对比四个有代表性的系统：

| 系统 | 版本/调研时间 | 定位 | 开发方 |
|------|-------------|------|--------|
| **Cortex-PY** | v0.1 (Jun 2026) | 精密设备故障诊断专用知识图谱+记忆 | 个人项目 |
| **Mem0** | v2.0.10 (Apr 2026) | 通用 Agent 记忆层 | Mem0 AI |
| **Graphiti (Zep)** | main (2026) | 时态知识图谱引擎 | Zep AI |
| **OpenViking** | v0.4.5 (Jun 2026) | Agent 上下文文件系统 | 字节跳动/火山引擎 |

---

## 1. 核心问题：它们各自在解决什么

### Cortex-PY

```{admonition} 核心问题
半导体刻蚀/CVD 设备的故障排查经验如何**结构化、可溯源、可推理**？
```

工程师写了一份故障排查报告（如"腔体压力异常——怀疑密封圈老化——检查了 MFC 正常——更换 O-ring 后恢复"）。Cortex-PY 把这篇文章变成一张图：

- 每个实体（传感器/故障/怀疑/证据）有唯一身份
- 每条因果关系有认知状态（假设 vs 已确认）
- 排除的嫌疑不污染因果推理路径
- 每条事实能追溯到原始事件

**输出**：下游诊断 agent 可以通过图遍历回答"密封圈老化导致了什么"、"还有哪些故障表现为压力异常"。

### Mem0

```{admonition} 核心问题
AI agent 如何在多轮对话、多 session 中**记住用户偏好和历史**？
```

用户今天说"我喜欢简洁的回答"，明天说"请用 Python 实现"。Mem0 把关键偏好提取为 memory 条目（"用户偏好简洁回答"、"用户技术栈是 Python"），下次对话自动召回。

**输出**：prompt 里注入相关记忆条目，让 LLM 产生个性化回答。

### Graphiti (Zep)

```{admonition} 核心问题
Agent 如何理解**实体关系随时间的变化**——什么变了、什么时候变的、变之前是什么？
```

Alice 先喜欢 Nike 鞋，后改喜欢 Adidas。这个变化不是"覆盖"——原来的偏好还在历史上可查。Graphiti 用**时态知识图谱**（neo4j）记录每个事实的有效时间窗口。

**输出**：支持点-in-time 查询（"2026 年 1 月 Alice 喜欢什么鞋"）和历史状态重建。

### OpenViking

```{admonition} 核心问题
Agent 的**上下文（记忆+资源+技能）如何统一管理、按需精确加载**，而不是一股脑塞进 prompt？
```

代码库、PDF 文档、历史对话、用户偏好——传统做法是全部向量化后倒进一个"向量汤"里。OpenViking 用**虚拟文件系统**（`viking://` URI 树）组织所有上下文，按三级粒度(L0/L1/L2)按需加载。

**输出**：一个 agent 在写代码时能精确定位到 `viking://resources/codebase/auth/middleware.py`，而不是从向量库里捞出 5 段不相关的碎片。

---

## 2. 架构深度对比

### 2.1 存储模型

| 维度 | Cortex-PY | Mem0 | Graphiti | OpenViking |
|------|-----------|------|----------|------------|
| **主存储** | PostgreSQL + pgvector | Valkey/Redis + Qdrant | **Neo4j** / FalkorDB | 本地文件系统 + VikingDB |
| **数据层次** | **5 层记忆模型** | 平面"memory"条目 | 4 组件(实体+边+episode+社区) | **3 级树** (L0摘要/L1概述/L2原文) |
| **双时态** | ✅ 完整4字段: `valid_from/to` + `recorded_from/to` | ❌ 仅有创建时间戳 | ✅ 完整的 valid + transaction time | ❌ 版本快照(非精细化时态) |
| **知识图谱** | 内建: facts 表即图边, 递归 CTE BFS | 可选: Mem0g 版用图后端 | **核心架构**: Neo4j 原生图 | ❌ 无内建图谱(层次文件树) |
| **向量** | pgvector (1024d, HNSW 索引) | Qdrant / 多种可插拔 | Neo4j 向量索引 | VikingDB (可插拔) |
| **关系约束** | **40+ 预定义谓词闭集**: 结构/因果/诊断三类 | ❌ 无谓词概念 | ✅ 可自定义 Pydantic ontology | ❌ 无谓词约束 |

### 2.2 抽取与实体链接

Cortex-PY 的抽取管线是**领域深度最大的**：

```{mermaid}
flowchart TB
    subgraph Cortex-PY 抽取管线
        A[Event] --> B{intent?}
        B -->|diagnosis| C[DIAGNOSIS prompt<br/>16实体类型+40谓词+8准则]
        B -->|structure| D[STRUCTURE prompt<br/>8实体类型]
        B -->|其他| E[GENERAL prompt<br/>无类型定义]
        
        C --> F[LLM 结构化输出]
        F --> G[B-over-C 实体链接]
        G --> H[谓词词表约束 closed set]
        H --> I[断言语义分析<br/>polarity + assertion_status]
        I --> J[图准入检查]
        J --> K[单值超替闭合]
        K --> L[Belief 聚合]
    end
```

```{mermaid}
flowchart TB
    subgraph Mem0 抽取
        M1[输入文本] --> M2[LLM 单次抽取事实]
        M2 --> M3[ADD-only: 无更新/删除]
        M3 --> M4[实体嵌入+索引]
    end
    
    subgraph Graphiti 抽取
        G1[Episode] --> G2[LLM 提取实体+关系]
        G2 --> G3[实体解析/合并于neo4j]
        G3 --> G4[社区检测+摘要]
        G4 --> G5[更新时态窗口]
    end
    
    subgraph OpenViking 抽取
        O1[Session 结束] --> O2[异步后处理]
        O2 --> O3[提取摘要→L0]
        O2 --> O4[提取概述→L1]
        O2 --> O5[保留原文→L2]
    end
```

**关键差异**：

| 维度 | Cortex-PY | Mem0 | Graphiti | OpenViking |
|------|-----------|------|----------|------------|
| 抽取 prompt 数 | **3 套定制系统** + 实体链接 prompt | 1 套通用 prompt | 1 套通用 + 自定义类型 | ASync 后处理 |
| 实体类型 | **16 种**半导体领域专有 | 无约束 | 自定义 Pydantic 模型 | 无约束 |
| 谓词约束 | **40+ 闭集**，未命中→quarantine | 无 | 可自定义 | 无 |
| 实体链接策略 | **B-over-C 三层**: 别名→向量→LLM灰区 | embedding 近邻 | LLM 解析+社区合并 | 路径定位 |
| 身份上下文隔离 | **强**: fab/equipment/chamber 6 字段 | 弱: user_id 字符串 | 弱: user/entity | **强**: URI 路径树 |
| 断言语义 | **polarity + assertion_status**(5状态) | 无 | 时间窗(valid/invalid) | 无 |

> **Cortex-PY 的 identity_context 设计在半导体的价值**：`MFC-1` 安装在腔体 C1 和腔体 C3 上是**两个不同的实体**——因为它们在知识图谱中连接的传感器、控制的参数、关联的故障都不同。通用系统(如 Mem0)基于 user_id 做隔离，无法处理"同名设备不同位置"的语义区分。

### 2.3 检索策略

```{mermaid}
graph TB
    subgraph Cortex-PY 6通道+融合
        CP1[用户查询] --> CP2[embedding]
        CP2 --> CP3[6通道并行]
        CP3 --> CP4[RRF 融合 k=60]
        CP4 --> CP5[Prism Rerank]
        CP5 --> CP6[StratifiedPack]
    end
    
    subgraph Mem0 多信号
        M1[查询] --> M2[语义+BM25+实体 并行]
        M2 --> M3[分数融合]
        M3 --> M4[top-k 记忆]
    end
    
    subgraph Graphiti 混合
        G1[查询] --> G2[语义搜索]
        G1 --> G3[关键词搜索]
        G1 --> G4[图遍历]
        G2 --> G5[混合融合]
        G3 --> G5
        G4 --> G5
    end
    
    subgraph OpenViking 目录递归
        O1[查询] --> O2[intent 分析]
        O2 --> O3[向量定位到目录]
        O3 --> O4[目录内精搜]
        O4 --> O5[递归下钻]
        O5 --> O6[聚合结果]
    end
```

| 维度 | Cortex-PY | Mem0 | Graphiti | OpenViking |
|------|-----------|------|----------|------------|
| **检索通道** | **6 个**: 向量+BM25+图+实体名+同义词+时间衰减 | 3 个: 语义+BM25+实体 | 3 个: 语义+关键词+图 | **目录递归**: intent→vector→dir→drill→aggregate |
| **融合策略** | RRF + Rerank + StratifiedPack | 并行评分融合 | 混合融合 | 路径遍历+向量排序 |
| **HyDE** | ✅ | ❌ | ❌ | ❌ |
| **MultiHop** | ✅ 子问题分解 | ❌ | ❌ | ❌ |
| **时态查询** | ✅ 完整: as_of/超替链/NL时间短语 | ❌ 基础过滤 | ✅ 完整: valid time 窗口 | ❌ |
| **缓存** | recall_packs(60s TTL) | Valkey 缓存 | ❌ | BM25 缓存(RAGFS) |
| **召回对象** | **facts**(结构化的 subject-predicate-object) | 文本片段 | 实体+边+社区 | 文件/目录块 |

---

## 3. 设计哲学：核心决策对比

### 3.1 数据质量 vs 检索速度

```{mermaid}
graph LR
    subgraph 质量优先极
        CP[Cortex-PY<br/>谓词闭集+断言状态+身份上下文<br/>入库慢但图是准的]
    end
    subgraph 平衡区
        GR[Graphiti<br/>时态窗口+LLM解析<br/>质量与速度的折中]
    end
    subgraph 速度优先极
        M[Mem0<br/>ADD-only 无更新<br/>快但无结构化约束]
        OV[OpenViking<br/>文件系统路径定位<br/>快但无关系推理]
    end
    
    CP --- GR --- M
    GR --- OV
```

**Cortex-PY 的选择**：宁可抽取慢 1-2 秒，也要保证入库的事实是可被信任推理的。**图准入规则**确保只有 `confirmed` 的因果边和 `observed/confirmed` 的非因果边进入图遍历——`hypothesized` 的怀疑虽然入库但被排除在图推理之外。这不是过度设计，而是领域需求：如果"我怀疑密封圈老化"和"密封圈老化已被确认"在图里地位相同，因果推理链就会断裂。

### 3.2 层次化 vs 平面化

OpenViking 和 Cortex-PY 在**反平面化**上有共识，但方式完全相反：

| | OpenViking | Cortex-PY |
|---|-----------|-----------|
| **反对什么** | 向量汤（所有文本切块后平铺到向量空间） | 通用知识图谱（所有实体不加区分地混合） |
| **解决方案** | **空间层次化**：`viking://` URI 树，上下文通过路径导航 | **语义层次化**：5 层记忆 + 身份上下文 + 谓词分类，上下文通过语义隔离 |
| **对"层次"的理解** | where（在哪） | what + how sure（是什么 + 确信程度） |

两者可以互补：OpenViking 管理"这个故障案例的原始报告存在哪里"，Cortex-PY 管理"这个故障案例的结构化推理图是什么"。

### 3.3 记忆 vs 知识

这是四条产品线最根本的分歧：

```
Mem0:        用户说了什么 → 记住偏好（记忆）
Graphiti:    事实如何变化 → 追踪关系（知识 + 时间）
OpenViking:  上下文在哪 → 组织资源（管理）
Cortex-PY:   故障怎么发生 → 结构化经验（专业知识）
```

```{admonition} 记忆≠知识
**记忆**是"用户喜欢 Python"——一段事实性陈述，不需要推理，记住就行。
**知识**是"密封圈老化导致腔体压力异常，进而引起刻蚀速率漂移"——一条因果链，需要结构化和可推理。

Mem0 擅长前者，Cortex-PY 擅长后者。不是谁替代谁的关系。
```

---

## 4. 部署与运维

| 维度 | Cortex-PY | Mem0 | Graphiti | OpenViking |
|------|-----------|------|----------|------------|
| **主语言** | Python (FastAPI) | Python (核心) + TypeScript(SDK) | Python (graphiti-core) | Python (73%) + Rust (15%) |
| **数据库依赖** | PostgreSQL 14+ (含 pgvector) | Valkey/Redis + Qdrant | **Neo4j** (强依赖) | 本地 FS + VikingDB |
| **必配外部服务** | LLM API + Embedding API | LLM API + Embedding API | LLM API (强依赖 structured output) | LLM API + Embedding API |
| **启动步骤** | 5 步: DB→API→Worker→MCP→前端 | 1 步: `pip install mem0ai` | 2 步: Neo4j docker → pip | 2 步: pip → `openviking-server` |
| **运维复杂度** | 中 (PG 维护) | **低** (SaaS 优先) | **高** (Neo4j 集群管理) | 中 (文件系统 + 向量库) |
| **SaaS 版本** | ❌ | ✅ (mem0.ai) | ✅ (Zep) | ❌ |
| **开源协议** | 未明确 | Apache 2.0 | Apache 2.0 | AGPL-3.0 (主库) |

---

## 5. 场景匹配矩阵

| 场景 | 最佳选择 | 理由 |
|------|---------|------|
| **半导体刻蚀/CVD 设备故障诊断知识图谱** | **Cortex-PY** | 唯一具备身份上下文隔离 + 断言语义 + 领域谓词闭集的系统 |
| **轻量 Agent 记忆（用户偏好/习惯）** | **Mem0** | 安装即用，SaaS 免运维，ADD-only 零维护 |
| **企业客户画像（关系随时间变化）** | **Graphiti / Zep** | 时态图天然适合 CRM 场景；Neo4j 企业生态成熟 |
| **Agent 上下文窗口管理（token 节省）** | **OpenViking** | L0/L1/L2 三级按需加载；文件路径即上下文 |
| **精密设备结构文档管理** | **Cortex-PY + OpenViking** | 互补：Cortex-PY 做结构化入库，OpenViking 做原文管理 |
| **多 session 跨周对话检索** | **Mem0 或 Graphiti** | 通用记忆场景，不需要领域谓词约束 |
| **故障案例分析库（跨设备）** | **Cortex-PY** | Cortex-PY 的 episodes/case 系统专为此设计 |
| **代码库级 Agent 上下文** | **OpenViking** | 可将整个代码仓库挂载为 `viking://resources/`，支持目录级检索 |
| **实时 streaming agent 记忆** | **Mem0 (low latency)** | 论文报告 p95 < 1s，适合语音/实时对话 |

---

## 6. Cortex-PY 的不可替代性

### 6.1 竞品做不到的事

```{mermaid}
graph TB
    subgraph Cortex-PY 独占能力
        A1["identity_context 隔离<br/>MFC-1 in chamber C1 ≠ MFC-1 in chamber C3"]
        A2["assertion_status 认知状态<br/>hypothesized ≠ confirmed ≠ ruled_out"]
        A3["谓词闭集约束<br/>caused_by ≠ led_to ≠ cascades_to<br/>语义不同、图遍历的行为也不同"]
        A4["图准入规则<br/>只有 confirmed 的因果边进 BFS"]
        A5["排除链完整保留<br/>检查了MFC→MFC正常→MFC被排除<br/>但排除信息不进图"]
    end
    
    A1 --> B["Cortex-PY<br/>可推理的知识图谱"]
    A2 --> B
    A3 --> B
    A4 --> B
    A5 --> B
```

**具体来说：**

1. **身份上下文隔离** — 其他系统最多能做到 `user_id` 级别隔离。Cortex-PY 的 6 字段身份上下文（fab/equipment/module/chamber/recipe/revision）是专门为**多腔体/多设备产线**设计的。没有它，从两个腔体抽取的"MFC-1"会被当成同一个实体，因果链交叉污染。

2. **断言状态** — 这是**认知上的双时态**。普通双时态（valid time + transaction time）回答"什么时间什么是真的"；Cortex-PY 的 assertion_status 回答"我们有多大把握这是真的"。没有这个区分，假设和结论在图里地位相同，推理链失去可信度。

3. **谓词闭集约束** — 40+ 谓词的分词表设计不是限制，而是**推理的保证**。`caused_by`、`cascades_to`、`suggests` 三个词的语义不同，在图遍历时行为也不同（`cascades_to` 沿着传播方向走，`suggests` 不传播）。没有这个约束，LLM 可能用 `caused_by` 和 `led_to` 混用，图遍历就失去了语义一致性。

### 6.2 Cortex-PY 不做的事

```
❌ 不做用户偏好记忆（这是 Mem0 的领域）
❌ 不做上下文窗口管理（这是 OpenViking 的领域）
❌ 不做 streaming 低延迟记忆（这是 Mem0/Zep SaaS 的领域）
❌ 不做大规模实体关系随时间追踪（这是 Graphiti 的领域）
```

这不是短板，是设计取舍。Cortex-PY 专注于：**把精密设备的故障诊断经验变成可推理的结构化知识**。

---

## 7. 协同比拼：如果放在一起用

这些系统不是非此即彼的关系。一个完整的企业级 Agent 记忆方案可能是：

```{mermaid}
graph TB
    subgraph Agent 运行时
        AGT[诊断 Agent]
    end
    
    subgraph 上下文管理层
        OV[OpenViking<br/>viking://user/diag/memories/<br/>存储原始文本+工作区]
    end
    
    subgraph 知识图谱层
        CP[Cortex-PY<br/>从文本抽取实体+因果><br/>结构化+可推理+可溯源]
    end
    
    subgraph 偏好记忆层
        M0[Mem0<br/>记住用户的偏好<br/>(语言/风格/格式习惯)]
    end
    
    AGT -->|对话| OV
    AGT -->|查询因果| CP
    AGT -->|注入偏好| M0
    
    OV -->|原始文本| CP
    CP -->|结构化结果| OV
```

这种分层架构让每层各司其职：

- **OpenViking**：管理原始文档、代码、报告的层次化存储，三级按需加载控制 token
- **Cortex-PY**：从原始文本抽取结构化知识，构建可推理的因果图
- **Mem0**：记住 agent 操作者的个人偏好（语言、报告风格、优先级排序）

---

## 8. 总结

| 系统 | 一句话 | 强项 | 弱项 | 最佳场景 |
|------|--------|------|------|---------|
| **Cortex-PY** | 精密设备故障诊断专用知识图谱 | 领域深度、断言语义、身份隔离、可溯源 | 通用性窄、部署略复杂、不支持 SaaS | 半导体 fab 诊断 agent |
| **Mem0** | Agent 通用记忆层 | 简单快速、SaaS 免运维、低延迟 | 无结构化、无谓词约束、无多设备隔离 | 消费级 agent 偏好记忆 |
| **Graphiti** | 时态知识图谱引擎 | 时间维度丰富、关系推理强、Neo4j 生态 | Neo4j 运维成本高、强依赖 LLM structured output | 客户画像/CRM/时态关系分析 |
| **OpenViking** | Agent 上下文文件系统 | 层次化组织、token 节省、URI 路径导航 | 无关系推理、无结构化知识、无语义约束 | Agent 上下文窗口管理、代码库理解 |

---

> **Cortex-PY 和其他系统的关系不是替代，是补全。** 如果行业标准方案（Mem0/Graphiti/OpenViking）能覆盖你的全部需求，不需要 Cortex-PY。但如果你在做半导体设备故障诊断，需要一个"知道 MFC-1 在 C1 和 C3 上不是同一个实体"、知道"怀疑"和"确认"在图里应该有不同地位的系统——Cortex-PY 就是这个专用工具。

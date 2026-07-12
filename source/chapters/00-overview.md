# 第0章 项目概览

## 项目定位

**Cortex-PY** 是 CortexDB 记忆系统的 Python 复刻实现。它不是一个生产级系统，而是面向个人/小团队可用的 Agent 长期记忆层。

```{admonition} 设计哲学
- **重点投入知识图谱质量**
- 不做集群/企业安全/benchmark 复现
- 五层记忆模型是核心
- 双时态三元组是灵魂
```

## 系统架构

```{mermaid}
graph TB
    subgraph 写入路径
        A[用户输入] --> B[Experience API]
        B --> C[WAL Append]
        C --> D[Enqueue Job]
    end
    
    subgraph 异步处理
        D --> E[Worker Loop]
        E --> F[Extraction Pipeline]
        F --> G[Entity Linking]
        G --> H[Fact/Belief 生成]
    end
    
    subgraph 读取路径
        I[Recall API] --> J[6通道检索]
        J --> K[RRF 融合]
        K --> L[Rerank]
        L --> M[StratifiedPack]
    end
    
    subgraph 存储
        N[(PostgreSQL)]
        O[pgvector]
        P[ltree]
    end
    
    H --> N
    J --> N
    N --> O
    N --> P
```

## 五层记忆模型

| 层 | 存储 | 职责 |
|----|------|------|
| **Events** | `events` 表 | WAL，唯一真相源，不可变 append-only |
| **Episodes** | `episodes` 表 | 有界事件序列，按时间分段 |
| **Facts** | `facts` 表 | 双时态三元组，知识图谱的边 |
| **Beliefs** | `beliefs` 表 | 概率断言，带 supports 证据链 |
| **Understanding** | `understanding` 表 | 概念合成，从 beliefs 聚合 |

```{mermaid}
graph LR
    E1[Event 1] --> E2[Event 2]
    E2 --> E3[Event 3]
    
    E1 --> EP1[Episode 1]
    E2 --> EP1
    E3 --> EP2[Episode 2]
    
    EP1 --> F1[Fact: subject-predicate-object]
    EP1 --> F2[Fact: ...]
    
    F1 --> B1[Belief: claim + confidence]
    F2 --> B1
    
    B1 --> U1[Understanding: concept]
```

## 核心组件

### 1. 写入路径 (infra/core.py)

```python
# infra/core.py - 核心写入逻辑
def append_event(*, scope, modality, content, context, 
                 caller, idempotency_key, ...):
    """幂等写入：同 key+同 body → 返回既有; 同 key+异 body → 409"""
    # 1. 幂等检查
    existing = check_idempotency(scope, idempotency_key)
    if existing:
        if body_hash_match(existing, content):
            return existing  # 幂等返回
        raise IdempotencyConflict
    
    # 2. 写入 WAL
    event_id = insert_event(scope, modality, content, ...)
    
    # 3. 入队抽取任务
    enqueue_job("extract", event_id)
    
    # 4. 发送生命周期事件
    emit_lifecycle("captured", event_id)
    
    return event_id
```

### 2. 抽取管线 (graph/extraction/pipeline.py)

```{mermaid}
flowchart TD
    A[原始 Event] --> B[LLM 抽取]
    B --> C{抽取结果}
    C -->|entities| D[实体链接 B over C]
    C -->|facts| E[Fact 生成]
    
    D --> D1{向量召回}
    D1 -->|命中| D2[复用已有实体]
    D1 -->|未命中| D3[创建新实体]
    D1 -->|灰区| D4[LLM 判定]
    
    E --> F[存入 facts 表]
    D2 --> G[更新实体 embedding]
    D3 --> G
    
    F --> H[触发下游]
    H --> I[Belief 聚合]
    H --> J[Episode 分段]
    H --> K[Understanding 合成]
```

### 3. 检索系统 (graph/retrieval/pipeline.py)

6 通道混合检索：

| 通道 | 实现 | 说明 |
|------|------|------|
| 向量 | pgvector `<=>` | 实体 embedding 近邻 → 其 facts |
| BM25 | tsvector | facts/events 全文检索 |
| 图遍历 | 递归 CTE | 种子实体 BFS 2-3 跳 |
| Entity Name | pg_trgm | 模糊实体名匹配 |
| Synonym | synonyms 表 | 同义词扩展 |
| Temporal-decay | access_count + 衰减 | 热数据优先 |
| Salience 软降权 | signal bus（retrieval_count + salience + retrieval_usefulness，四信号独立开关） | 负反馈多的事实按 salience 软降权，避免噪声召回 |

### 4. 功能模块

| 模块 | 文件 | 职责 |
|------|------|------|
| **Ontology** | `infra/ontology.py` | 谓词本体（结构/因果/诊断/状态）、图准入规则 |
| **Prompt 体系** | `infra/prompts.py` | 半导体级诊断 prompts，10+ 实体类型，40+ 谓词 |
| **Understanding** | `memory/understanding.py` | 概念合成层，related 图遍历 |
| **Maintenance** | `memory/maintenance.py` | methylation（软剪枝）+ consolidation（去重）+ `seed_predicate_definitions` |
| **Erasures** | `memory/erasures.py` | GDPR 4 阶段引用计数真删 |
| **Ingest** | `memory/ingest.py` | bulk 写入 + 5 导入器 |
| **Episodes/Case** | `memory/episodes.py` | 诊断 Case 全生命周期管理 |
| **Temporal** | `memory/temporal.py` | NL 时间短语解析 |
| **Schemas** | `interfaces/api/schemas.py` | Pydantic API 契约 |
| **Feedback** | `memory/feedback.py` | 反馈信号采集（salience/正负反馈/retrieval_usefulness），写入 `feedback_signals` 表 |
| **Dreaming** | `memory/dreaming.py` | 离线巩固（Dreaming），周期性归纳 `dreaming_runs` |
| **Higher-Order** | `memory/higher_order.py` | 高阶事实合成，从一阶 facts 产出抽象结论（`is_higher_order=true`） |
| **Concurrency** | `infra/concurrency.py` | ThreadPoolExecutor 并行 I/O 工具:`parallel_map`(保序、异常→None)、`parallel_call`(异构函数并行)、`get_executor`(惰性单例) |

## 并行 I/O 优化

cortex-py 的 LLM/embed/rerank 调用都是 HTTP I/O。Python 的 GIL 在网络 `recv()` 处释放，因此用 `ThreadPoolExecutor` 能实现真正的并行。`infra/concurrency.py` 提供 `parallel_map`（保序、异常返回 None 不阻断）和 `parallel_call`（异构函数并行）两个工具，已应用于 6 处串行 I/O 等待点：

| 位置 | 并行内容 | 效果 |
|------|----------|------|
| 检索 Phase 0 | query embed + N×HyDE LLM + multihop LLM（第一波）；N×HyDE 文本 embed（第二波） | 从串行 `Σ` 降到并行 `max` |
| 抽取 Step 3c | 灰区 entity link 的 N 路 LLM 裁决 | N 个灰区实体并发判定 |
| 抽取 Step 3b | entity embedding 批量化 | N 逐条 → 1 batch |
| Dreaming Phase B/C | 跨 cluster 的 relation_detect + action_plan | N cluster 并发 |
| Understanding 合成 | 跨 topic 的 synthesis LLM | N topic 并发 |
| Worker enrich | 缺 embedding 实体的批量补算 | batch embed 会话外 |

所有并行 LLM 调用都在 `session_scope()` 外执行——持着 DB 连接等 HTTP 响应会浪费连接（QueuePool 下可能 pool_timeout）。模式统一为：session 内只做短事务读写，HTTP I/O 在 session 外并行。

## 记忆自演化（Feedback / Dreaming / Higher-Order）

Cortex-PY 在五层记忆模型之上新增了**自演化能力**，使记忆层不再是一次性写入的静态图，而是会随使用被持续"回炉"的活系统。三条演化链路共享一条**信号总线**（核心信号：`retrieval_count`、`salience`、`retrieval_usefulness`，各自有独立开关）：

- **Feedback（反馈）**：用户召回结果时的隐式/显式反馈（点击、采纳、正/负投票）被写入 `feedback_signals` 表，并实时调整相关 fact 的 `salience` 与 `retrieval_usefulness`。正向反馈提升召回优先级，负向反馈触发软降权（salience 软衰减，而非立即删除）。
- **Dreaming（做梦/离线巩固）**：系统在低负载时段运行 `dreaming_runs`，对一段时间内的 facts 做去重、合并、冲突消解与抽象，把零散三元组凝聚成更紧凑的知识结构。这是与在线抽取解耦的"睡眠巩固"。
- **Higher-Order（高阶合成）**：在抽取触发或定时任务驱动下，对同一实体的多条一阶 facts 调用 LLM 合成**高阶事实**（`is_higher_order=true`，带 `evidence_fact_ids` 指向支撑它的一阶事实），相当于在 Facts 层内开了一个"抽象子层"。

三条链路统一读写信号总线字段：Feedback 负责采集信号（写 salience/retrieval_usefulness）、Dreaming 负责巩固、Higher-Order 负责提升抽象层级。检索融合后的四信号加权（Salience/Usage/Usefulness/Exploration 各自可开关）让记忆系统可以通过 salience 软降权抑制噪声、用 usage 饱和加分放大高频记忆、用 exploration 槽位保证新记忆曝光。详细设计与配置见[第10章 信号总线](10-signal-bus)、[第11章 Feedback 信号](11-feedback)、[第12章 Dreaming 离线巩固](12-dreaming)与[第13章 Higher-Order 高阶事实](13-higher-order)。

## 代码结构

> 4 子包分层（`infra` → `memory` → `graph` → `interfaces`，依赖单向无环）。完整架构说明见[第23章 架构视图](23-architecture-diagrams)。

```
src/cortex/
├── schema.sql              # 全表 DDL（单一真相源,27 张表）
├── infra/                  # 基础设施（10 模块）
│   ├── config.py           # YAML 配置 + 维度强校验 + 热更新白名单
│   ├── db.py               # engine / session / schema 初始化（psycopg3 + QueuePool 连接池）
│   ├── core.py             # WAL append(幂等) + 队列 + lifecycle + ?wait=
│   ├── services.py         # embedding / rerank / LLM + think 剥离 + 流式
│   ├── concurrency.py      # parallel_map / parallel_call(ThreadPoolExecutor 并行 I/O)
│   ├── prompts.py          # 半导体级诊断 prompts（10+ 实体, 40+ 谓词）
│   ├── ontology.py         # 谓词本体（结构/因果/诊断/状态）
│   ├── chunking.py         # 长文档分块
│   ├── token_budget.py     # token 预算估算
│   └── think_stream.py     # think 标签边界状态机
├── memory/                 # 记忆写入与生命周期(12 模块)
│   ├── ingest.py           # 批量 + 5 导入器
│   ├── episodes.py         # Episodes + 诊断 Case 管理
│   ├── erasures.py         # GDPR 引用计数真删（4 阶段）
│   ├── temporal.py         # NL 时间短语解析
│   ├── export_data.py      # 导出 JSONL
│   ├── maintenance.py      # methylation / consolidation / seed_predicate_definitions
│   ├── understanding.py    # 概念合成层
│   ├── evidence.py         # 外部证据目录(URI/hash/query/version/quality)
│   ├── evolution.py        # Dreaming/Higher-Order 人工审批门(evolution_candidates)
│   ├── feedback.py         # Feedback 回灌(双轨软降权+硬归档)
│   ├── dreaming.py         # Dreaming proposal 生成(不直接改 verified graph)
│   └── higher_order.py     # Higher-Order candidate 生成
├── graph/                  # 知识图谱
│   ├── extraction/         # 抽取管线 + 实体链接 B over C(三阶段并行)
│   └── retrieval/          # 6 通道 + RRF + rerank + StratifiedPack
└── interfaces/             # 对外入口
    ├── api/                # FastAPI 全端点(72 个)+ Pydantic schemas
    ├── mcp_server.py       # MCP server(32 工具,双传输)
    ├── cli.py              # CLI 入口
    ├── smoke.py            # 端到端冒烟
    └── worker/             # Postgres-as-queue worker 循环
```

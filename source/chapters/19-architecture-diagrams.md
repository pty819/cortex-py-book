# 第19章 架构视图(4+1 视图模型)

> 本章用 Kruchten 的 4+1 视图模型完整描述 cortex-py 的架构:逻辑视图(Logical)、进程视图(Process)、开发视图(Development)、物理视图(Physical),加上贯穿它们的场景视图(Scenarios)。
>
> 所有模块路径基于 `infra` / `memory` / `graph` / `interfaces` 四子包分层(重构 commit `347afb1` 之后)。
>
> ```{admonition} 自演化子系统(后置增量)
> :class: important
> 四子包重构落地后,在 `memory` 包内追加了**自演化子系统**:三个新模块 `feedback` / `dreaming` / `higher_order`,以及两个新 worker job 类型 `dream` / `higher_order`。三者通过 `access_count` + `salience` 这条**共享信号总线**耦合(而非各自为政),刻意避免 MindMemOS 把反馈/巩固/归纳解耦成三个互不知情的孤岛。本架每次涉及的计数(模块数、job 类型数、表数、端点数、工具数)均已同步到本版。```

```{admonition} 视图总览
| 视图 | 回答的问题 | 主要制品 |
|------|-----------|---------|
| **逻辑视图** | 系统提供哪些功能职责?如何分层? | 功能分包、分层依赖、核心抽象 |
| **进程视图** | 运行时有哪些进程/并发单元?如何通信同步? | 进程拓扑、队列模型、SSE/notify |
| **开发视图** | 代码如何组织成模块/包?分层规则? | 4 子包目录、依赖方向、构建入口 |
| **物理视图** | 部署到哪些机器/进程?外部依赖? | 部署拓扑、端口、外部服务 |
| **场景视图** | 关键用例如何穿越上述四层? | 写入/读取/遗忘三条路径 |
```

---

## 19.1 逻辑视图

### 19.1.1 功能分层

系统按职责分为四层子包,依赖**严格单向向下**,无环:

```{mermaid}
graph TB
    subgraph interfaces ["interfaces · 对外入口"]
        API["FastAPI 端点(62)"]
        MCP["MCP Server(32 工具)"]
        CLI["CLI"]
        WK["Worker 循环"]
    end
    subgraph graph_ ["graph · 知识图谱"]
        EXT["extraction<br/>LLM 抽取 + 实体链接"]
        RET["retrieval<br/>6 通道 + RRF + rerank"]
    end
    subgraph memory ["memory · 记忆生命周期"]
        INGEST["ingest · 批量导入"]
        EPI["episodes · Case 管理"]
        ERASE["erasures · GDPR 真删"]
        MAINT["maintenance · 演化"]
        UNDER["understanding · 概念合成"]
        FB["feedback · 反馈回灌"]
        DREAM["dreaming · 离线巩固"]
        HO["higher_order · 高阶归纳"]
    end
    subgraph infra ["infra · 基础设施"]
        CFG["config"]
        DB["db"]
        CORE["core<br/>WAL/队列/lifecycle"]
        SVC["services<br/>LLM/embed/rerank"]
        PROMPT["prompts"]
        ONTO["ontology"]
    end

    API --> graph_
    API --> memory
    API --> infra
    MCP --> graph_
    MCP --> memory
    MCP --> infra
    WK --> graph_
    WK --> memory
    WK --> infra
    graph_ --> infra
    memory --> infra
```

```{admonition} 分层规则
:class: note
- 上层可依赖下层,同层可互相依赖
- 下层**不得**反向依赖上层(`infra.ontology` 不得 import `memory`/`graph`)
- `graph` 不依赖 `memory`(已验证)
- 唯一例外:`memory.ingest` 通过**函数内 lazy import** 调 `graph.extraction`(避免循环)
```

### 19.1.2 各层职责

**infra —— 基础设施(9 模块)**

| 模块 | 职责 | 核心抽象 |
|------|------|---------|
| `config` | YAML 配置 + 维度强校验 | `load_config()` · `llm_configured()` |
| `db` | engine / session / schema 初始化 | `session_scope()` · `init_schema()` |
| `core` | WAL(幂等) + 队列 + lifecycle + `?wait=` | `append_event()` · `claim_next_job()` · `wait_for_stage()` |
| `services` | embedding / rerank / LLM + think 剥离 + 流式 | `embed_one()` · `llm_chat()` · `llm_chat_stream()` |
| `prompts` | 全部 LLM prompt 常量 | `EXTRACTION_SYSTEM_*` · `ANSWER_SYSTEM` |
| `ontology` | 谓词本体单一真相源 + 图准入规则 | `CAUSAL_PREDICATES` · `graph_eligible()` |
| `chunking` | 长文档按标题分块 | `chunk_document()` |
| `token_budget` | token 预算估算 + 裁剪 | `fit_to_budget()` |
| `think_stream` | think 标签边界状态机(跨 chunk 缓冲) | `split_think_stream()` |

**memory —— 记忆写入与生命周期(10 模块)**

| 模块 | 职责 |
|------|------|
| `ingest` | 批量写入 + 5 导入器(jsonl/mem0/zep/letta/openai) |
| `episodes` | 事件分段 + 诊断 Case 全生命周期(open→investigating→resolved→closed) |
| `erasures` | GDPR 4 阶段引用计数真删(enumerate→refcount→delete→cleanup) |
| `temporal` | NL 时间短语注册 + 解析(`last_week` → `-P7D..P0D`) |
| `export_data` | 导出 JSONL(可回灌) |
| `maintenance` | methylation(软剪枝)+ consolidation(去重) |
| `understanding` | 概念合成(per topic)+ related 图 + coverage |
| `feedback` | 反馈回灌(双轨:软降权 `salience` + 硬归档 `recorded_to`),正反馈递增 `access_count` |
| `dreaming` | 离线巩固(两阶段 LLM:`relation_detect` → `action_plan`),scheduler 定时触发 + heartbeat 续命 |
| `higher_order` | 高阶归纳(evidence-driven LLM 归纳 `order=2` 谓词),extract 后异步触发 |

**graph —— 知识图谱(2 子包)**

| 模块 | 职责 |
|------|------|
| `graph.extraction.pipeline` | LLM 抽取三元组 + 实体链接 B over C + 事实校验 |
| `graph.retrieval.pipeline` | 6 通道 + RRF(k=60)+ prism rerank + StratifiedPack |

**interfaces —— 对外入口(6 模块)**

| 模块 | 职责 |
|------|------|
| `interfaces.api.app` | FastAPI 全端点(62 个) |
| `interfaces.api.schemas` | Pydantic 请求/响应契约 |
| `interfaces.mcp_server` | MCP server(32 工具,双传输) |
| `interfaces.cli` | CLI 入口(db/worker/serve/probe-llm/smoke/mcp) |
| `interfaces.smoke` | 端到端冒烟 |
| `interfaces.worker.runner` | 队列 worker 循环 + reaper |

### 19.1.3 五层记忆模型(领域核心)

```{mermaid}
graph LR
    E["Events<br/>WAL append-only"] -->|extract| F["Facts<br/>双时态三元组<br/>图谱边"]
    E -->|segment| EP["Episodes<br/>Case 管理"]
    EP -->|attach| F
    F -->|aggregate| B["Beliefs<br/>概率断言<br/>证据链"]
    B -->|synthesize| U["Understanding<br/>概念合成<br/>related 图"]
    F -->|graph walk<br/>递归 CTE| F
```

每条派生记录带 **4 个时间字段**:`valid_from`/`valid_to`(业务时间)+ `recorded_at`/`recorded_to`(系统时间),同时支持"现在什么是真的"和"当时我们怎么以为"。

---

## 19.2 进程视图

### 19.2.1 进程拓扑

```{mermaid}
graph TB
    subgraph proc ["cortex-py 进程(无共享内存)"]
        API["FastAPI<br/>:8002<br/>uvicorn"]
        MCP["MCP HTTP<br/>:8001<br/>streamable"]
        WK1["Worker 1<br/>poll=1s"]
        WK2["Worker N<br/>可多实例"]
    end

    PG[("PostgreSQL<br/>共享 DB<br/>SKIP LOCKED 协调")]

    API --> PG
    MCP --> PG
    WK1 --> PG
    WK2 --> PG

    API -.->|pg_notify| API
    WK1 -.->|pg_notify lifecycle| API
```

典型部署 3 类进程:API(1)、MCP HTTP(1)、Worker(1~N)。也可单进程跑 stdio MCP(本地 agent)。所有进程**无共享内存**,完全通过 PostgreSQL 协调。

### 19.2.2 并发与同步模型

```{mermaid}
sequenceDiagram
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as PostgreSQL
    participant API as FastAPI

    Note over W1,W2: SKIP LOCKED 原子抢 job,无冲突
    par
        W1->>DB: claim_next_job (FOR UPDATE SKIP LOCKED)
        DB-->>W1: job A (locked_by=W1)
    and
        W2->>DB: claim_next_job (跳过 job A)
        DB-->>W2: job B (locked_by=W2)
    end

    W1->>DB: 处理中... (locked_at)
    Note over W1: 若崩溃,locked_at 停留
    Note over DB: reaper 每 60s 扫<br/>locked_at 超 300s → 重置 queued
    DB->>DB: reap_zombies (visibility timeout)

    W1->>DB: complete_job / fail_job
    W1->>DB: emit_lifecycle + pg_notify
    DB-->>API: NOTIFY cortex_lc
    API->>API: ?wait= 的 LISTEN 收到,返回
```

| 机制 | 位置 | 作用 |
|------|------|------|
| **Postgres-as-queue** | `infra.core.claim_next_job` | `SKIP LOCKED` 原子抢 job,多 worker 无冲突;无 Redis |
| **visibility timeout** | `infra.core.reap_zombies` | running 且 `locked_at` 超 300s → 重置 queued |
| **reaper** | `worker.runner` 每 60s | 扫僵尸 job,防 worker 崩溃后任务卡死 |
| **退避重试** | `infra.core.fail_job` | 未超 `max_attempts` → queued + 指数退避;超限 → failed 死信 |
| **pg_notify** | `infra.core.emit_lifecycle` | lifecycle 事件推送,`?wait=` 的 LISTEN 立即收到 |
| **SSE 长连接** | `api.app` stream 端点 | `EventSourceResponse` + `is_disconnected()` 探活 |
| **幂等 WAL** | `infra.core.append_event` | 同 key + 同 body hash → 返回既有;异 body → 409 |

### 19.2.3 队列任务类型

worker 按 `job_type` 分发(`worker.runner._dispatch`):

| job_type | 处理函数 | 触发 |
|----------|---------|------|
| `extract` | `graph.extraction.extract_event` | 每次 experience 入库 |
| `segment` | `memory.episodes.segment_scope` | /v1/episodes/build |
| `methylation` | `memory.maintenance.methylation_run` | 定时 / admin |
| `consolidate` | `memory.maintenance.consolidation_run` | 定时 / admin |
| `enrich` | 跨 event 实体消歧,补 embedding | 定时 / admin |
| `synthesize` | `memory.understanding.synthesize_scope` | /v1/understanding/synthesize |
| `dream` | `memory.dreaming.dream_run` | scheduler 定时触发(无 queued/running dream 时入队)+ heartbeat 续命 `locked_at` |
| `higher_order` | `memory.higher_order.generate_higher_order` | extract 后异步触发(对新增 fact 做 `order=2` 归纳) |

---

## 19.3 开发视图

### 19.3.1 目录结构(4 子包)

```{mermaid}
graph LR
    subgraph src ["src/cortex/"]
        INFRA["infra/<br/>config·db·core<br/>services·prompts·ontology<br/>chunking·token_budget·think_stream"]
        MEM["memory/<br/>ingest·episodes·erasures<br/>temporal·export_data<br/>maintenance·understanding<br/>feedback·dreaming·higher_order"]
        GRAPH["graph/<br/>extraction/<br/>retrieval/"]
        IF["interfaces/<br/>api/·mcp_server<br/>cli·smoke·worker/"]
    end

    INFRA --> MEM
    INFRA --> GRAPH
    INFRA --> IF
    MEM --> IF
    GRAPH --> IF
```

```
src/cortex/
├── __init__.py                 # __version__
├── schema.sql                  # 全表 DDL(22 张表,单一真相源)
│
├── infra/                      # 基础设施(9 模块)
│   ├── config.py  db.py  core.py  services.py
│   ├── prompts.py  ontology.py  chunking.py
│   └── token_budget.py  think_stream.py
│
├── memory/                     # 记忆写入与生命周期(10 模块)
│   ├── ingest.py  episodes.py  erasures.py  temporal.py
│   ├── export_data.py  maintenance.py  understanding.py
│   └── feedback.py  dreaming.py  higher_order.py    # 自演化子系统
│
├── graph/                      # 知识图谱
│   ├── extraction/             # pipeline.py + probe.py
│   └── retrieval/              # pipeline.py
│
└── interfaces/                 # 对外入口
    ├── api/                    # app.py + schemas.py
    ├── mcp_server.py  cli.py  smoke.py
    └── worker/                 # runner.py
```

### 19.3.2 分层依赖矩阵(无环)

| 依赖方 → | infra | memory | graph | interfaces |
|----------|:-----:|:------:|:-----:|:----------:|
| **infra** | — | ✗ | ✗ | ✗ |
| **memory** | ✓ | — | ✗\* | ✗ |
| **graph** | ✓ | ✗ | — | ✗ |
| **interfaces** | ✓ | ✓ | ✓ | — |

> ✗\* = `memory.ingest` 仅通过函数内 lazy import 调 `graph.extraction`,非 import 顶层依赖。

### 19.3.3 构建入口

| 入口 | 命令 | 模块路径 |
|------|------|---------|
| console script | `cortex <cmd>` | `cortex.interfaces.cli:main` |
| 模块运行 | `python -m cortex.interfaces.cli` | 同上 |
| API | `uvicorn cortex.interfaces.api.app:app` | `interfaces.api.app:app` |
| MCP stdio | `cortex mcp` | `interfaces.mcp_server.main_stdio` |
| MCP HTTP | `cortex mcp-http` | `interfaces.mcp_server.main_http` |
| Worker | `cortex worker` | `interfaces.worker.runner.run_worker` |

### 19.3.4 测试组织(101 测试)

测试按被测层级组织,与 4 子包对应:

| 测试文件 | 被测层 |
|---------|--------|
| `test_core.py` / `test_wait_for_stage.py` | infra.core |
| `test_think_stream.py` | infra.think_stream |
| `test_llm_chat_stream.py` / `test_llm_max_tokens.py` | infra.services |
| `test_extraction_shape.py` / `test_extraction_retrieval.py` | graph |
| `test_assertion_semantics.py` | graph + infra.ontology |
| `test_case_retrieval_operational.py` | memory.episodes + graph |
| `test_temporal_identity_belief.py` | memory.temporal + 双时态 |
| `test_api.py` / `test_answer_stream.py` | interfaces.api |

---

## 19.4 物理视图

### 19.4.1 部署拓扑

```{mermaid}
graph TB
    subgraph host ["单机 / 局域网"]
        API["FastAPI :8002"]
        MCP["MCP HTTP :8001"]
        WK["Worker ×1~N"]
        PG[("PostgreSQL 18.4")]
    end

    subgraph ext ["外部 LLM 服务"]
        LLM["Minimax-M3<br/>(OpenAI 兼容)"]
        EMB["jina-embeddings-v5<br/>1024d"]
        RR["Prism Rerank"]
    end

    subgraph client ["客户端"]
        VUE["Vue 3 :5173"]
        CC["Claude Code<br/>MCP stdio"]
        RA["远程 Agent<br/>MCP HTTP"]
    end

    VUE --> API
    CC -.->|stdio| MCP
    RA --> MCP

    API --> LLM
    API --> EMB
    API --> RR
    MCP --> LLM
    MCP --> EMB

    API --> PG
    MCP --> PG
    WK --> PG

    subgraph pgext ["PG 扩展"]
        HNSW["pgvector HNSW"]
        LTREE["ltree scope 层级"]
        TRGM["pg_trgm 模糊匹配"]
    end
    PG --> HNSW
    PG --> LTREE
    PG --> TRGM
```

### 19.4.2 端口与配置

| 服务 | 默认端口 | 配置项 |
|------|---------|--------|
| FastAPI | 8002 | `cortex serve --port` |
| MCP HTTP | 8001 | `cortex mcp-http --port` |
| Vue 前端 | 5173 | `config.api.cors_origins` |
| PostgreSQL | 5432 | `config.database.url` |

### 19.4.3 外部依赖与可替换性

| 依赖 | 用途 | 可替换 |
|------|------|--------|
| PostgreSQL 18.4 | 存储 + 队列 + 向量 + 全文 + 图 | 不可替换(深度依赖 PG 扩展) |
| Minimax-M3 | LLM 抽取/回答/合成/校验 | 可替换(OpenAI 兼容接口) |
| jina-embeddings-v5 | embedding(1024d) | 可替换,但维度变更需重算全量 |
| Prism Rerank | 检索重排 | 可替换为其他 reranker |

```{admonition} 扩展性边界
:class: warning
- **垂直扩展**:单 PG + 读副本即可支撑小团队
- **水平扩展**:Worker 可多实例(SKIP LOCKED 无冲突);API/MCP 无状态可多实例(需负载均衡)
- **不做**:集群、分片、企业安全、多租户强隔离(定位个人/小团队)
```

---

## 19.5 场景视图(Scenarios)

四个核心场景穿越全部四层,验证架构一致性。

### 19.5.1 场景一:Agent 写入记忆

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as interfaces.api
    participant CORE as infra.core
    participant WK as Worker
    participant EXT as graph.extraction
    participant SVC as infra.services
    participant DB as PostgreSQL

    A->>API: POST /v1/experience
    API->>CORE: append_event (WAL 幂等)
    CORE->>DB: INSERT event + emit_lifecycle(captured)
    CORE->>DB: pg_notify(cortex_lc)
    CORE->>DB: enqueue_job(extract)
    API-->>A: 200 {event_id} (?wait=indexed 则阻塞)

    WK->>DB: claim_next_job (SKIP LOCKED)
    WK->>EXT: extract_event
    EXT->>SVC: llm_chat (抽取三元组)
    SVC-->>EXT: entities + facts
    EXT->>DB: 实体链接 B over C (向量→阈值→LLM灰区)
    EXT->>DB: 写 facts + beliefs (双时态)
    EXT->>DB: emit_lifecycle(extracted/indexed)
    DB-->>API: pg_notify (若 ?wait= 在等)
```

**穿越层级**:interfaces → infra → graph → infra.services/core。

### 19.5.2 场景二:Agent 检索 + 流式 answer

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as interfaces.api
    participant RET as graph.retrieval
    participant SVC as infra.services
    participant TS as infra.think_stream
    participant DB as PostgreSQL
    participant LLM as LLM

    A->>API: POST /v1/recall {query}
    API->>RET: recall
    RET->>SVC: embed_one(query)
    par 6 通道并行
        RET->>DB: _chan_vector (pgvector)
        RET->>DB: _chan_bm25 (tsvector)
        RET->>DB: _chan_graph (递归 CTE)
        RET->>DB: _chan_entity_name (pg_trgm)
        RET->>DB: _chan_synonym
        RET->>DB: _chan_temporal_decay
    end
    Note over RET: RRF 融合 (k=60) → top-40
    RET->>SVC: rerank (top-40 → top-20)
    RET->>SVC: llm_chat (合成 context_block)
    RET-->>API: StratifiedPack

    A->>API: GET /v1/answer/stream (SSE)
    API->>SVC: llm_chat_stream (stream=True)
    loop 逐 chunk
        LLM-->>SVC: delta (含 think 标签)
        SVC-->>TS: chunk
        TS-->>API: (reasoning|answer, text)
        API-->>A: SSE event: reasoning / answer
    end
    API-->>A: SSE event: done (citations + pack_id)
```

**穿越层级**:interfaces → graph → infra。think 标签在后端状态机解析,前端按 event 类型分别渲染推理过程与回答。

### 19.5.3 场景三:GDPR 遗忘

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as interfaces.api
    participant ERASE as memory.erasures
    participant DB as PostgreSQL

    A->>API: POST /v1/erasures/preview {scope}
    API->>ERASE: preview
    ERASE->>DB: enumerate (扫 scope 内 events)
    ERASE->>DB: refcount (算 blob/event 引用计数)
    ERASE-->>API: manifest (逐 event: delete vs redact)
    API-->>A: preview_id

    A->>API: POST /v1/erasures {preview_id}
    API->>ERASE: execute
    alt refcount > 0
        ERASE->>DB: redact (清 content,保 id+wal_offset)
    else refcount = 0
        ERASE->>DB: 物理删
    end
    ERASE->>DB: array_remove 清 facts/beliefs.supports
    ERASE->>DB: blob refcount=0 → 删 blob
    ERASE-->>API: erasure_id
    API-->>A: 200 {erasure_id}
```

**穿越层级**:interfaces → memory → infra.db。

### 19.5.4 场景四:反馈回灌闭环

用户/Agent 对某条召回结果打反馈,系统在**共享信号总线**(`access_count` + `salience`)上即时调整,后续召回自然重排;负反馈累积到阈值则触发 `methylation` 级联软剪枝。这条路径是自演化子系统对外可见的主入口。

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as interfaces.api
    participant FB as memory.feedback
    participant DB as PostgreSQL
    participant RET as graph.retrieval
    participant MAINT as memory.maintenance

    A->>API: POST /v1/feedback {target_id, signal_type}
    API->>FB: submit_feedback
    FB->>DB: SELECT ... FOR UPDATE(序列化同 fact 并发反馈)

    alt signal_type=relevant(正)
        FB->>DB: access_count += 1, salience 上调(ceil 封顶)
        FB-->>API: actions=[salience_boosted]
    else signal_type=irrelevant(负)
        FB->>DB: salience 下调(floor 兜底), negative_feedback_count += 1
        FB->>FB: _check_methylation(累积阈值?)
        FB-->>API: actions=[salience_demoted]
    else signal_type=wrong(强负)
        FB->>DB: salience 下调 + assertion_status → ruled_out
        FB->>DB: task_temporary 则软关 recorded_to(版本化归档)
        FB-->>API: actions=[ruled_out / archived]
    end

    FB->>DB: cache invalidate(若 cfg.feedback.cache_invalidate)
    API-->>A: 200 {feedback_id, actions}

    Note over A,RET: 下一次召回
    A->>API: POST /v1/recall
    API->>RET: recall
    RET->>DB: ORDER BY 综合分(含 salience 权重)
    Note over RET: 正反馈项排序上升 / 负反馈项下沉
    RET-->>API: 重排后的 pack

    Note over DB,MAINT: 负反馈累积达阈值(后台)
    DB->>MAINT: 触发 methylation cascade
    MAINT->>DB: 跳过仍被其他活跃 fact 支撑的 event(不剪共享 evidence)
```

**穿越层级**:interfaces → memory.feedback → infra.db;副作用经信号总线回流到 graph.retrieval 的排序,并在阈值处触发 memory.maintenance 的级联。

**关键不变量**:所有 fact 写操作带 `recorded_to IS NULL AND valid_to IS NULL` 守卫,不篡改已归档历史;`methylation` 跳过仍被其他活跃 fact 支撑的 event,避免误剪共享 evidence。

---

## 19.6 架构决策记录(关键 ADR 摘要)

| 决策 | 选择 | 理由 |
|------|------|------|
| 队列实现 | Postgres-as-queue(`SKIP LOCKED`) | 不引入 Redis,小团队运维简单;PG 已是必选依赖 |
| LLM think | 强制开启 + 后端状态机解析 | 推理准确度依赖 think;状态机跨 chunk 缓冲保证标签完整 |
| 流式 answer 传输 | GET + EventSource(SSE) | query 短;dev 态无 auth;浏览器原生;agent 用 MCP 同步接口 |
| 实体链接 | B over C(向量召回 + 阈值 + LLM 灰区) | 图谱质量命门;纯向量误并,纯 LLM 太贵 |
| 双时态 | 4 字段(valid/recorded × from/to) | 同时支持"现在什么是真的"和"当时我们怎么以为" |
| 分层 | 4 子包(infra/memory/graph/interfaces) | 职责清晰;依赖单向无环;便于维护演进 |
| 信号总线 | `access_count` + `salience` 作为共享信号层 | 单一信号层耦合 Feedback/Dreaming/Higher-Order,避免 MindMemOS 把三功能解耦成互不知情的孤岛;反馈即时回流召回排序 |

```{admonition} 与重构前的差异
:class: important
重构前(commit `347afb1` 之前)为 30 个 py 文件平铺在 `src/cortex/` 根下。重构后按职责归入 4 子包,依赖关系从隐式变为显式分层,便于维护与演进。旧路径(如 `cortex.infra.core`、`cortex.infra.db`)已**不再可用**,全部迁移到新路径(如 `cortex.infra.core`)。
```

# 第23章 架构视图（4+1 视图模型）

> 本章依据 Philippe Kruchten 1995 年提出的 4+1 视图模型（*Architectural Blueprints — The "4+1" View Model of Software Architecture*, IEEE Software 12(6), pp. 42–50）描述 cortex-py 的软件架构：**逻辑视图**（功能的对象分解）、**进程视图**（并发与同步）、**开发视图**（源码的静态组织）、**物理视图**（软件到硬件节点的映射），再加上把四个视图缝合在一起的**场景视图**（+1）。
>
> 4+1 的要义不是"画五张图"，而是让不同的干系人各自拿到回答自己问题的那一刀；五个视图描述的是**同一套架构的五个投影**。因此本章对每个视图都明确规定两件事——**本视图回答什么问题、禁止出现什么内容**——并在 23.7 节用显式的"视图对应关系"把各投影重新对齐。领域细节见第 0–2 章，各子系统实现细节见第 3–22 章，本章只保留**架构显著**的内容。

## 23.1 视图模型与隔离规则

Kruchten 用 Perry & Wolf 的公式逐视图展开：**架构 = {元素, 形式, 理由/约束}**——每个视图有自己的元素（构件与连接件）、自己的风格，以及自己的干系人：

| 视图 | 干系人 | 关注点 | 构件 | 连接件 | 常用 UML 图 |
|------|--------|--------|------|--------|------------|
| **逻辑** | 最终用户、领域与数据专家 | 功能：系统向用户提供什么服务 | 类（关键抽象） | 关联、组合、继承 | 类图、状态图 |
| **进程** | 系统集成者、性能工程师 | 非功能：并发、同步、容错、完整性、可伸缩性 | 进程、任务 | 消息、RPC、事件广播、锁 | 时序图、活动图、通信图 |
| **开发** | 程序员、软件配置管理 | 组织：模块划分、复用、发布策略 | 模块、子系统 | 编译 / import 依赖 | 包图、组件图 |
| **物理** | 系统工程师 | 拓扑：软件映射到哪些节点 | 节点（硬件） | 通信介质（LAN、HTTP、stdio） | 部署图 |
| **场景（+1）** | 全体干系人 | 验证：少数关键用例把四个视图缝合起来 | 参与者 · 用例 · 脚本步骤 | 对象间与进程间交互 | 用例图、时序图 |

```{mermaid}
graph TB
    LOG["逻辑视图<br/>功能 · 对象模型"]
    PROC["进程视图<br/>并发 · 同步"]
    DEV["开发视图<br/>模块 · 分层"]
    PHYS["物理视图<br/>节点 · 部署"]
    SCEN(("场景<br/>+1"))
    SCEN --- LOG
    SCEN --- PROC
    SCEN --- DEV
    SCEN --- PHYS
```

```{admonition} 为什么每个视图要写"本视图不包含"
:class: tip
4+1 最常见的退化，是把同一张"子系统方框图"在五个视图里各贴一遍：用包结构冒充逻辑视图、用部署图冒充进程视图。结果是五个视图互相重叠、谁也回答不了自己的问题。本章各节末尾都有一条**本视图不包含**清单，用来守住视图边界——例如"五子包分层"只出现在开发视图（23.4），"端口与机器"只出现在物理视图（23.5）。
```

**读法建议**：想理解系统"记住什么、能回答什么"从 23.2 进入；想理解"怎么跑起来、怎么容错"从 23.3 进入；想改代码从 23.4 进入；想部署从 23.5 进入；想验证自己理解是否自洽，读 23.6 的场景。

---

## 23.2 逻辑视图：功能的对象分解

逻辑视图回答最终用户（Agent、诊断工程师）的问题：**cortex-py 记住了什么、能回答什么**。系统被分解为一组取自问题域的关键抽象，以及它们之间的关系与不变量。这里不出现任何包名、进程名或机器名。

### 23.2.1 关键抽象

| 抽象 | 职责（一句话） | 为什么架构显著 |
|------|---------------|---------------|
| **Scope** | 层级命名空间（如 `org:acme/dept:eng/user:alice`），一切对象的隔离键 | 所有读写以 scope 为前提；它是命名空间而非权限边界（AuthZ 归上游） |
| **Event** | 不可变的事实记录单元，WAL 式只追加，系统**唯一真相源** | 一切派生层（Facts/Beliefs/Understanding）均可由 Events 重放重建 |
| **Episode / Case** | 有界事件序列；Case 是带生命周期的诊断工作单元（设备/批次/根因） | 给散乱事件定界，是"诊断 Case 检索"的组织单位 |
| **Entity** | 现实对象（设备/子系统/部件/人/批次），图谱节点，多别名同一身份 | 实体链接的载体，图谱质量的命门 |
| **Predicate** | 受控关系词表（36 个，结构 8 / 因果 5 / 诊断 22 / 状态 1） | 图谱的"语言"；预定义谓词保证图可遍历、语义一致 |
| **Fact** | 双时态三元组断言 `(subject, predicate, object)`，既是知识单元又是**图的边** | 整个系统的中心抽象：检索、推理、演化都发生在 Fact 图上 |
| **Belief** | 概率断言（claim + confidence + stance），带 supports 证据链 | 从 Facts 聚合出"我们目前怎么看 X" |
| **Understanding** | 概念合成层（per topic），related 概念图 | 从 Beliefs 提炼跨案例的理解 |
| **Playbook** | 人审定的诊断规程 DAG，版本化不可变追加 | 程序性知识：在 Fact 图上做确定性前向推理，**不修改** Fact 图 |
| **Evidence** | 外部证据目录（URI / source_record_id / hash / quality），payload 留在权威系统 | 把"引用"与"拷贝"分开，支撑可信度与追溯 |

> **信号总线不是对象，而是 Fact 上的共享信号属性**：`salience`（Feedback 软降权）、`retrieval_count`（被动召回次数）、`retrieval_usefulness`(显式反馈累积)。反馈 / 巩固（Dreaming）/ 高阶归纳（Higher-Order）三个自演化机制都读写同一组信号列，因而耦合为一个子系统，而不是三个互不知情的孤岛（对比 MindMemOS 的解耦设计，见第 25 章）。

### 23.2.2 领域对象图

```{mermaid}
classDiagram
    direction LR
    class Scope {
        +scope_path
        +parent_path
    }
    class Event {
        +wal_offset
        +observed_at
        +recorded_at
        +idempotency_key
    }
    class Episode {
        +event_ids
        +started_at
    }
    class Case {
        +case_id
        +status
        +phase
    }
    class Entity {
        +canonical_name
        +entity_type
        +aliases
    }
    class Predicate {
        +category
        +cardinality
    }
    class Fact {
        +subject_id
        +predicate
        +polarity
        +assertion_status
    }
    class Belief {
        +claim
        +confidence
        +supports
    }
    class Understanding {
        +topic
        +summary
        +related
    }
    class Playbook {
        +name
        +version
        +nodes
        +edges
    }
    class Evidence {
        +uri
        +content_hash
        +quality
    }

    Scope "1" o-- "0..*" Event : 隔离
    Scope "1" o-- "0..*" Fact : 隔离
    Event ..> Fact : 抽取
    Event ..> Episode : 分段
    Episode <|-- Case
    Case "1" o-- "0..*" Event : 显式关联
    Fact "1" --> "1..2" Entity : subject / object
    Fact --> Predicate
    Fact ..> Fact : 图遍历 2-3 跳
    Fact ..> Belief : 聚合
    Belief ..> Understanding : 合成
    Understanding ..> Understanding : related
    Playbook ..> Fact : 前向推理·只读
    Evidence "0..*" ..> Fact : claim-evidence
```

```{admonition} Fact 的完整语义
:class: note
Fact 除图中字段外还携带：**双时态四字段** `valid_from/valid_to`（业务时间）+ `recorded_at/recorded_to`（系统时间）；**断言双轴** `polarity`（positive/negative）× `assertion_status`（observed/hypothesized/confirmed/ruled_out/rejected）；以及信号总线三列（见上文）。object 端可以是 Entity，也可以是受控词表的字面值（value 型谓词）。详见第 2、6 章。
```

### 23.2.3 架构显著状态机

只有跨多个模块、影响行为一致性的生命周期才进逻辑视图：

**Case 状态**（正交的 `phase` 轴：observation → scoping → investigation → correlation → root_cause → remediation → regression）：

```{mermaid}
stateDiagram-v2
    direction LR
    [*] --> open : 创建
    open --> investigating
    investigating --> resolved : 定位根因
    resolved --> closed : 归档
    closed --> [*]
```

**Playbook 版本状态**（版本化不可变追加，LLM 不自动改写）：

```{mermaid}
stateDiagram-v2
    direction LR
    [*] --> draft
    draft --> active : 评审通过
    active --> retired : 停用
    draft --> retired
```

### 23.2.4 领域不变量

不变量是逻辑视图的"形式与约束"，任何实现都必须维持：

1. **WAL 不可变**：Event 只追加、不修改不删除（擦除走 redact 保 id）。
2. **双时态四字段**：同时回答"现在什么是真的"（valid + recorded 当前值）与"当时我们怎么以为"（历史区间）。
3. **结构谓词收敛**：同 `(scope, subject, predicate, object, polarity)` 的结构性 Fact 只保留一条活跃边，重复断言累积为证据而非多值并存。
4. **派生可重建**：Facts / Beliefs / Understanding 均可从 Events 重放得出。
5. **Scope 隔离**：所有对象与查询以 scope 为前提；跨 scope 只经显式的祖先视图（local / holistic / descend）。
6. **归档守卫**：活跃写操作带 `recorded_to IS NULL AND valid_to IS NULL` 守卫，不篡改已归档历史。

**本视图不包含**：包结构与目录树（→ 23.4 开发视图）；进程、队列与锁（→ 23.3 进程视图）；机器、端口与部署配置（→ 23.5 物理视图）。

---

## 23.3 进程视图：并发与同步

进程视图回答集成者与性能工程师的问题：**运行时有哪些可独立启动/恢复/停止的执行单元，它们如何通信、同步、容错**。

### 23.3.1 进程清单

| 进程 | 实例数 | 承载的工作 |
|------|--------|-----------|
| **API 进程**（FastAPI / uvicorn） | 1 | 同步 REST 端点、SSE 流（lifecycle / answer stream）、召回与回答 |
| **MCP HTTP 进程** | 1 | 面向远程 Agent 的 MCP 工具调用（streamable-http） |
| **MCP stdio 进程** | 每 Agent 1 个 | 面向本地 Agent（如 Claude Code）的子进程模式 |
| **Worker 进程** | 1~N | 异步任务循环：抽取、分段、维护、巩固、归纳 |
| PostgreSQL | （外部设施） | **唯一共享状态**：数据 + 队列 + 通知 |

**核心声明：所有进程无共享内存**，进程间的一切协调——任务分发、生命周期通知、互斥、幂等——都经 PostgreSQL 完成。不引入 Redis 或独立消息中间件。

```{mermaid}
graph TB
    subgraph procs["cortex-py 进程"]
        API["API 进程"]
        MCPH["MCP HTTP 进程"]
        MCPS["MCP stdio 进程<br/>（每 Agent 一个）"]
        WK["Worker 进程 ×1~N"]
    end
    PG[("PostgreSQL<br/>数据 · 队列 · NOTIFY")]
    AGT["Agent / 浏览器（外部刺激）"]

    AGT -->|HTTP / SSE| API
    AGT -->|stdio| MCPS
    AGT -->|HTTP| MCPH
    API -->|SQL · LISTEN| PG
    MCPH -->|SQL| PG
    MCPS -->|SQL| PG
    WK -->|SQL · SKIP LOCKED| PG
```

### 23.3.2 进程内任务

| 进程 | 任务（线程级） |
|------|---------------|
| API | 同步端点跑在内建线程池；SSE 生成器经 `asyncio.to_thread` 逐项拉取（单项拉取形成**自然背压**）；外部 I/O（LLM/embed/rerank）经共享 `ThreadPoolExecutor` 并行 |
| Worker | 主循环 poll 1s；heartbeat 后台线程每 60s 续命；reaper 每 60s 扫僵尸 job；dreaming 调度器挂在 reaper 周期 |
| MCP | 每个工具调用同步处理（全同步模型，无 async DB） |

### 23.3.3 通信与同步机制

| 机制 | 位置 | 作用 |
|------|------|------|
| **Postgres-as-queue** | `claim_next_job` | `FOR UPDATE SKIP LOCKED` 原子抢 job，多 Worker 无冲突 |
| **visibility timeout** | `reap_zombies` | running 且 `locked_at` 超 300s → 按 attempts 分流：未超限重置 queued，超限判死信；两分支均用事务级 advisory lock 与在飞 Worker 互斥 |
| **指数退避重试** | `fail_job` | 瞬态失败按 `backoff_base^attempts` 退避；超 `max_attempts` 进 failed 死信 |
| **heartbeat** | Worker 后台线程 | 每 60s 刷新 job 的 `locked_at`（owner-fencing 带 `worker_id`），防长任务被误重排 |
| **advisory lock** | dreaming | `pg_try_advisory_lock(dream:{scope})` 序列化同 scope 的巩固运行 |
| **pg_notify → LISTEN** | `emit_lifecycle` | lifecycle 事件推送；`?wait=` 语义用专用连接 LISTEN 阻塞等待 |
| **SSE 长连接** | API stream 端点 | `EventSourceResponse` + 断连探活；同步生成器逐项输出 |
| **幂等 WAL** | `append_event` | 同 `idempotency_key` + 同 body hash → 返回既有；异 body → 409 |
| **反馈幂等与串行** | `submit_feedback` | `ON CONFLICT (idempotency_key) DO NOTHING` 原子去重；`FOR UPDATE` 行锁串行化同 Fact 并发反馈 |

**容错分级**（失败纪律）：检索各通道与 rerank **单独 fail-open**（坏一路不影响整包）；瞬态 LLM 失败 raise 给 Worker 退避重试；抽取**配置**错误是终端的（job 直接 failed，不重试）。

### 23.3.4 Worker 任务类型

| job_type | 触发 |
|----------|------|
| `extract` | 每次 experience 入库 |
| `segment` | 显式 `/v1/episodes/build` |
| `methylation` / `consolidate` / `enrich` | 定时 / admin |
| `synthesize` | 显式 `/v1/understanding/synthesize` |
| `dream` | scheduler 定时（同 scope 无 queued/running dream 时入队） |
| `higher_order` | extract 后异步触发 |

**本视图不包含**：机器、端口与部署变体（→ 23.5）；包与模块结构（→ 23.4）；领域语义与不变量（→ 23.2）。

---

## 23.4 开发视图：源码静态组织

开发视图回答程序员与配置管理者的问题：**代码怎么拆成可独立开发的子系统，依赖朝哪个方向，怎么构建**。五子包分层**只属于本视图**。

### 23.4.1 子系统分层与依赖矩阵

```
             infra   memory   graph   diagnostics   interfaces
infra          -       ✗        ✗          ✗             ✗
memory         ✓       -        ✗*         ✗             ✗
graph          ✓       ✗        -          ✗             ✗
diagnostics    ✓       ✓        ✗          -             ✗
interfaces     ✓       ✓        ✓          ✓             -
```

规则：上层可依赖下层，下层**不得**反向依赖；`graph` 不依赖 `memory`；`diagnostics` 只依赖 `infra`/`memory`。唯一记录在案的例外（✗\*）：`memory.ingest` 通过**函数内 lazy import** 调 `graph.extraction`，避免循环导入——严格意义上 memory 顶层不依赖 graph。

```
src/cortex/
├── infra/          # 基础设施（10 模块）：config · db · core(WAL+队列) · services
│                   #   · concurrency · prompts · ontology · chunking
│                   #   · token_budget · think_stream
├── memory/         # 记忆写入与生命周期（14 模块）：ingest · episodes · erasures
│                   #   · temporal · export_data · maintenance · understanding
│                   #   · evidence · evolution · feedback · dreaming
│                   #   · higher_order · graph_mutations · terminology
├── graph/          # 知识图谱：extraction/（抽取+实体链接） · retrieval/（6通道+融合+重排）
├── diagnostics/    # 诊断推理：engine（纯函数 DAG 引擎） · forward_reasoning（持久化+版本）
└── interfaces/     # 对外入口：api/ · mcp_server · cli · smoke · worker/
```

```{admonition} 与重构前的差异
:class: note
重构（commit `347afb1`）前约 30 个 py 文件平铺在 `src/cortex/` 根下；重构后按职责归入子包（`diagnostics` 随后追加），依赖从隐式变为显式分层。旧平铺路径已不可用。
```

### 23.4.2 模块地图（一句话级）

各模块的完整职责见对应章节，此处只给导航：

- **infra** —— 无业务语义的基础设施：配置（含热更新白名单）、DB 会话、WAL+队列、外部服务客户端、并行 I/O 线程池、prompt 常量、谓词本体单一真相源、分块、token 预算、think 流状态机。
- **memory** —— 记忆的写入与生命周期治理：批量导入、Episode/Case、GDPR 擦除、时间短语、导出、维护（甲基化/巩固）、概念合成、证据目录、演化审批、反馈、巩固、高阶归纳、受控图写、词表。
- **graph** —— 抽取管线（LLM 三元组 + 实体链接 B over C）与检索管线（6 通道 + RRF + 信号加权 + rerank + StratifiedPack）。详见第 4–5、14–16 章。
- **diagnostics** —— 纯函数 DAG 引擎 + 持久化版本管理。详见第 7b 章。
- **interfaces** —— FastAPI（实测 97 端点：app 29 + routes/ 68，10 个领域路由模块）、MCP server（53 工具，双传输）、CLI、冒烟、Worker 循环。详见第 18–20 章。

### 23.4.3 schema 真相源

schema 变更以 **Alembic 迁移**为真相源（`0001_current_schema` → `0008_predicate_cleanup`，共 8 个 revision，34 张表）；`schema.sql` 已降级为审阅用基线参考。`init_schema()` 即 `upgrade_database("head")`；生产降级默认拒绝。

### 23.4.4 构建入口与工具链

| 入口 | 命令 |
|------|------|
| console script | `cortex <cmd>`（serve / worker / mcp / mcp-http / db / probe-llm / smoke） |
| 模块运行 | `python -m cortex.interfaces.cli` |
| API | `uvicorn cortex.interfaces.api.app:app` |
| Worker | `cortex worker` |

工具链：Python 3.12 + `uv` 管理（hatchling 打包），ruff（line-length 110）。测试按被测层组织，与五包对应；全量回归 `scripts/run_regression.sh`（pytest + stage0 SQL + stage6/7 + Case + MCP 双传输验收），用例数以 `pytest --co -q` 实时为准。

**本视图不包含**：运行时锁、队列与通知行为（→ 23.3）；机器与端口（→ 23.5）；领域不变量（→ 23.2）。

---

## 23.5 物理视图：部署拓扑

物理视图回答系统工程师的问题：**软件（进程）映射到哪些硬件/网络节点上，有哪几种部署配置**。

### 23.5.1 节点模型

```{mermaid}
graph TB
    subgraph clients["客户端节点"]
        VUE["浏览器<br/>Vue 3 控制平面"]
        CC["本地 Agent<br/>（Claude Code 等）"]
        RA["远程 Agent"]
    end
    subgraph apphost["应用主机（1~N 台）"]
        API["API 进程 :8002"]
        MCPH["MCP HTTP 进程 :8001"]
        MCPS["MCP stdio 进程"]
        WK["Worker 进程 ×1~N"]
    end
    subgraph dbnode["数据库节点"]
        PG[("PostgreSQL 18<br/>pgvector · pg_textsearch · pg_trgm")]
    end
    subgraph saas["外部模型服务（SaaS）"]
        LLM["Minimax-M3"]
        EMB["jina-embeddings-v5 1024d"]
        RR["Prism Rerank"]
    end

    VUE -->|HTTP / SSE| API
    CC -->|stdio| MCPS
    RA -->|HTTP| MCPH
    API --> PG
    MCPH --> PG
    MCPS --> PG
    WK --> PG
    API --> LLM
    API --> EMB
    API --> RR
    WK --> LLM
    WK --> EMB
```

### 23.5.2 进程 → 节点映射与部署变体

| 进程 | A. 本地单进程 | B. 单机多进程（典型） | C. 局域网分布 |
|------|--------------|---------------------|--------------|
| API | —（不需要） | 应用主机 | 应用主机 ×1~N（无状态，可前置负载均衡） |
| MCP stdio | 与 Agent 同机子进程 | 应用主机 | 各 Agent 本机 |
| MCP HTTP | — | 应用主机 :8001 | 应用主机 :8001 |
| Worker | — | 应用主机 ×1~N | 独立主机 ×1~N（`SKIP LOCKED` 天然无冲突） |
| PostgreSQL | 本机 Docker | 应用主机或旁路 | 独立 DB 主机（标准端口 5432） |
| 适用场景 | 个人 Agent / 开发 | 小团队生产 | 团队增长后拆分 |

变体 B、C 是**同一套进程模型的两种节点摆放**，不改代码。开发态 PG 用 Docker（`postgres:18-bookworm` + pgvector v0.8.2 + pg_textsearch v0.2.0，容器 `cortex-db`）。

### 23.5.3 端口与外部依赖

| 项 | 值 | 说明 |
|----|----|------|
| API / MCP HTTP / 前端 / PG | 8002 / 8001 / 5173 / 5432 | 前端 Vite 代理 `/v1` → API |
| PostgreSQL 18 | **不可替换** | 深度依赖 pgvector（HNSW，1024d）、pg_textsearch（BM25，需 `shared_preload_libraries`）、pg_trgm |
| Minimax-M3 / jina-embeddings / Prism Rerank | 可替换 | OpenAI 兼容接口；embedding 换型号需重算全量（维度锁 1024） |

**扩展边界**：垂直——单 PG + 读副本即可支撑小团队；水平——Worker 与 API/MCP 多实例；**不做**——集群、分片、多租户强隔离、企业安全（产品定位个人/小团队，见第 0 章）。

**本视图不包含**：类与领域关系（→ 23.2）；锁与队列机制（→ 23.3）；包结构（→ 23.4）。

---

## 23.6 场景视图（用例视图，+1）

场景视图又称**用例视图**：从**参与者**（actor）的视角收拢系统的功能需求（用例），再挑出少数关键用例做成**场景**（用例的一次具体执行实例）去缝合其余四个视图。它在 Kruchten 的模型里是**冗余**的（故称 +1），作用有二：架构设计期**驱动发现**架构元素；架构完成后**验证与说明**四个视图描述的是同一套系统。

三个术语要分清：**用例**是功能需求——"某类参与者要系统能做什么"；**场景**是用例的一次执行实例；**时序图**只是场景脚本的记法，不是视图本身。本节先给参与者与用例总览（23.6.1），再把架构关键场景作为用例实例展开（23.6.2–23.6.5），每个场景配一张**四视图对照表**——这是缝合的关键；实现级细节回链对应章节。

### 23.6.1 参与者与用例总览（功能需求）

cortex-py 的功能需求围绕三条业务主线：**记忆读写**（Agent 的长期记忆）、**根因诊断（RCA）**（诊断工程师的 Case 工作台与 Playbook 推理）、**记忆治理与运维**（反馈、演化审批、遗忘、配置）。

```{mermaid}
graph LR
    AG["Agent<br/>（Claude Code / 远程 Agent · MCP）"]
    ENG["诊断工程师<br/>（Vue 控制平面）"]
    OPS["运维人员<br/>（Vue 控制平面）"]

    subgraph SYS["cortex-py 用例（功能需求）"]
        UC1(("写入经验"))
        UC2(("批量导入 / 文档喂入"))
        UC3(("召回记忆"))
        UC4(("提问 · 流式回答"))
        UC5(("管理诊断 Case"))
        UC6(("Similar Case 检索"))
        UC7(("Playbook 正向推理"))
        UC8(("反馈信号"))
        UC9(("演化候选审批"))
        UC10(("GDPR 遗忘"))
        UC11(("配置热更新 / 队列监控"))
    end

    AG --> UC1
    AG --> UC2
    AG --> UC3
    AG --> UC4
    AG --> UC10
    ENG --> UC5
    ENG --> UC6
    ENG --> UC7
    ENG --> UC8
    OPS --> UC9
    OPS --> UC11
    UC4 -.->|include| UC3
    UC7 -.->|include| UC3
```

| 用例 | 参与者 | 需求要点 |
|------|--------|---------|
| UC1 写入经验 | Agent | 幂等 WAL 入库；`?wait=` 同步语义 |
| UC2 批量导入 / 文档喂入 | Agent | 5 种导入器（jsonl / mem0 / zep / letta / openai）；长文档按标题分块 |
| UC3 召回记忆 | Agent | 6 通道混合检索 + 融合重排，返回分层证据包（StratifiedPack） |
| UC4 提问 · 流式回答 | Agent | 强制 think；SSE 分流 reasoning / answer；引用与 pack 可追溯 |
| UC5 管理诊断 Case | 诊断工程师 | open → investigating → resolved → closed 全生命周期；事件挂接 |
| UC6 Similar Case 检索 | 诊断工程师 | 按设备 / 症状 / 根因特征检索同构历史案例 |
| UC7 Playbook 正向推理 | 诊断工程师 | 确定性 DAG 遍历，给出下一步检查与结论建议 |
| UC8 反馈信号 | 诊断工程师 | relevant / irrelevant / wrong / partial；即时影响召回排序 |
| UC9 演化候选审批 | 运维人员 | Dreaming / Higher-Order 候选 approve / reject |
| UC10 GDPR 遗忘 | Agent | 两阶段 preview / execute；引用计数真删 |
| UC11 配置热更新 / 队列监控 | 运维人员 | 白名单热更新即时生效；jobs 队列可视化 |

```{admonition} 用例视图与逻辑视图的分工
:class: note
两者都涉及"功能"，但视角不同：**逻辑视图**（23.2）站在设计者一侧，回答"系统内部用什么对象与机制实现功能"；**用例视图**站在参与者一侧，回答"从外部要系统做什么"，是功能需求的总目录。场景是其中关键用例的执行实例，用来缝合其余四个视图。
```

选出的架构关键场景：UC1（最核心——系统存在的理由）、UC4（最高频）、UC10（风险最大——不可逆操作），外加 UC8 / UC11 两个次级场景。

### 23.6.2 场景一：写入记忆（UC1 的实例 · 最核心）

Agent 提交一段经验，系统把它变成可召回的知识。

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as API 进程
    participant DB as PostgreSQL
    participant WK as Worker 进程
    A->>API: POST /v1/experience
    API->>DB: Event 追加（幂等键去重）
    API->>DB: Job(extract) 入队
    API-->>A: event_id（?wait= 时 LISTEN 阻塞至 NOTIFY）
    WK->>DB: SKIP LOCKED 抢 job
    Note over WK: LLM 抽取候选三元组 →<br/>实体链接（向量→阈值→LLM 灰区）→<br/>双时态写入 + 结构收敛
    WK->>DB: Fact / Belief 落库
    WK->>DB: lifecycle NOTIFY
```

| 视图 | 本场景动用的元素 |
|------|-----------------|
| 逻辑 | `Event.append`（幂等）；`Entity` 身份合并；`Fact` 写入（双时态 + 结构谓词收敛）；`Belief` upsert；`Predicate` 图准入 |
| 进程 | API 进程请求线程 → PG 队列 → Worker job 线程；`pg_notify` 回连 `?wait=`；瞬态失败退避重试 |
| 开发 | `interfaces.api` → `infra.core` → `graph.extraction` → `infra.services` / `infra.ontology` |
| 物理 | 客户端节点 → 应用主机 → DB 节点；LLM SaaS（抽取调用） |

细节见第 3–5 章。

### 23.6.3 场景二：召回 + 流式回答（UC4 的实例 · 最高频）

Agent 提问，系统召回分层证据并流式生成带推理过程的回答。

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as API 进程
    participant RET as 检索管线
    participant DB as PostgreSQL
    participant LLM as LLM SaaS
    A->>API: GET answer/stream（SSE）
    API->>RET: recall(scope, query)
    RET->>LLM: 并行 embed / HyDE / 多跳分解
    RET->>DB: 6 通道并行检索（各通道 fail-open）
    RET->>RET: RRF 融合 → 信号总线加权 → rerank
    RET->>DB: 命中写回 retrieval_count（隐式反馈环）
    RET-->>API: StratifiedPack（events/facts/beliefs/higher_order）
    API-->>A: SSE phase recall_done
    API->>LLM: 流式生成（强制 think）
    API-->>A: SSE reasoning / answer 分流输出
```

| 视图 | 本场景动用的元素 |
|------|-----------------|
| 逻辑 | `Fact` 图遍历与多通道命中；信号总线读取（salience / count / usefulness）；`Belief` / `Understanding` 分层装包 |
| 进程 | API 进程内：线程池并行外部 I/O；SSE 逐项拉取背压；think 状态机跨 chunk 分流 |
| 开发 | `interfaces.api` → `graph.retrieval`（channels / pipeline）→ `infra.services` / `infra.think_stream` |
| 物理 | 客户端节点 → 应用主机 → DB 节点 + embed/rerank/LLM 三个 SaaS |

细节见第 14–16 章。

### 23.6.4 场景三：GDPR 遗忘（UC10 的实例 · 风险最大）

按 scope 擦除个人数据，同时不破坏 WAL 完整性与共享证据。

```{mermaid}
sequenceDiagram
    participant A as Agent
    participant API as API 进程
    participant ER as 擦除子系统
    participant DB as PostgreSQL
    A->>API: POST /v1/erasures/preview
    API->>ER: enumerate + refcount
    ER-->>A: manifest（逐 event：删 or redact）
    A->>API: POST /v1/erasures
    API->>ER: 执行 manifest
    ER->>DB: 引用>0 → redact（清 content 保 id）；引用=0 → 物理删
    ER->>DB: 清 facts/beliefs.supports；blob 引用计数回收
    ER-->>A: erasure_id
```

| 视图 | 本场景动用的元素 |
|------|-----------------|
| 逻辑 | `Event` 不可变性的例外路径（redact）；`Fact.supports` 证据链拆链；blob 引用计数不变量 |
| 进程 | 纯 API 进程内同步完成（无 Worker 参与）；两阶段 preview/execute |
| 开发 | `interfaces.api` → `memory.erasures` → `infra.db` |
| 物理 | 客户端节点 → 应用主机 → DB 节点 |

细节见第 22 章。

### 23.6.5 次级场景（对照表）

**反馈回灌闭环**（UC8 的实例，自演化主入口）：

| 视图 | 本场景动用的元素 |
|------|-----------------|
| 逻辑 | `Fact` 信号列调整（软降权 salience / 硬归档 recorded_to）；负反馈累积触发甲基化级联（跳过仍被活跃 Fact 支撑的 Event） |
| 进程 | API 进程内：幂等去重 + `FOR UPDATE` 串行；级联在 Worker 的 methylation job |
| 开发 | `interfaces.api` → `memory.feedback` →（阈值处）`memory.maintenance` |
| 物理 | 客户端节点 → 应用主机 → DB 节点 |

**配置热更新**（UC11 的实例）：

| 视图 | 本场景动用的元素 |
|------|-----------------|
| 逻辑 | 无领域对象变更——纯运行时开关（dreaming.enabled 等） |
| 进程 | API 进程内白名单深合并、原地改单例，即时生效无重启 |
| 开发 | `interfaces.api`（admin 路由）→ `infra.config`（白名单禁改 database / embedding.dimension） |
| 物理 | 浏览器（Settings 页）→ 应用主机 |

---

## 23.7 视图对应关系

Kruchten 明确指出：**各视图不正交、不独立**，元素按设计规则跨视图连接；且项目越大，逻辑与开发、进程与物理两两距离越大——**禁止期待 1:1 映射**。下面三张表是 cortex-py 的显式缝合。

### 23.7.1 逻辑 → 进程（每个抽象在哪条控制线上执行）

| 逻辑抽象 | 主要操作 | 执行位置 |
|----------|---------|---------|
| Event | append（幂等） | API / MCP 进程，请求线程 |
| Event → Fact | 抽取 + 实体链接 | Worker 进程，extract job |
| Fact / Belief / Understanding | 检索、融合、装包 | API 进程，recall 调用链 |
| Fact | 信号调整（反馈） | API 进程（即时）+ Worker（级联） |
| Fact | 巩固 / 高阶归纳 | Worker 进程，dream / higher_order job |
| Playbook | 前向推理 | API / MCP 进程（engine 为纯函数，无 DB） |
| Event | 擦除 redact | API 进程，同步两阶段 |

`?wait=` 语义把 API 线程与 Worker 线程用 `LISTEN/NOTIFY` 接起来——这是逻辑视图"一条 Event 变成可召回 Fact"在进程视图里的**跨进程缝合点**。

### 23.7.2 逻辑 → 开发（每个抽象落在哪些子系统；刻意非 1:1）

| 逻辑抽象 | 实现子系统 | 备注 |
|----------|-----------|------|
| Event（WAL 语义） | `infra.core` | WAL 是基础设施职责，不是领域包职责 |
| Fact 语义（谓词/断言/准入） | `infra.ontology` + `graph.extraction` | 本体的单一真相源在 infra |
| Fact 的读 | `graph.retrieval` | 读写分离在包级别 |
| Fact 的治理写 | `memory.graph_mutations` / `memory.maintenance` | 同一抽象的写路径横跨两包 |
| Episode / Case | `memory.episodes` | |
| Playbook | `diagnostics.engine`（纯函数）+ `diagnostics.forward_reasoning`（持久化） | 逻辑上一个抽象，开发上按"纯/不纯"切两半 |
| Scope | 无独立模块——由约定 + DB 行 + 全函数显式参数实现 | "无处不在所以无处安放"的横切抽象 |

最后一行正是"逻辑 ≠ 开发"的最好例证：Scope 在逻辑视图是一等抽象，在开发视图里却是贯穿所有包的横切约定。

### 23.7.3 进程 → 物理（每种进程部署到哪类节点）

见 23.5.2 的映射表与三种部署变体。要点：变体之间**只改节点摆放，不改进程模型**——这正是进程视图与物理视图分离的回报。

---

## 23.8 关键约束与架构决策（按视图归类）

每条 ADR 约束的是特定视图，按视图归档后更容易查：

**逻辑视图约束**

| 决策 | 理由 |
|------|------|
| 双时态 4 字段（valid/recorded × from/to） | 同时支持"现在什么是真的"与"当时我们怎么以为" |
| 实体链接 B over C（向量召回→阈值→LLM 灰区） | 图谱质量命门；纯向量误并、纯 LLM 太贵 |
| 结构谓词收敛为单一活跃边、证据累积 | 防止多值并存污染图遍历 |
| 信号总线作为三机制共享耦合层 | 避免反馈/巩固/归纳退化为互不知情的孤岛 |
| Scope 不做 AuthZ | 纯数据服务边界，授权归上游应用 |

**进程视图约束**

| 决策 | 理由 |
|------|------|
| Postgres-as-queue（不引入 Redis） | PG 已是必选依赖；小团队运维简单 |
| 全同步 + 线程池（无 async DB） | 模型简单；外部 I/O 并行已足够 |
| LLM 强制 think + 后端状态机解析 | 推理准确度依赖 think；跨 chunk 缓冲保证标签完整 |
| 流式 answer 用 GET + SSE | query 短；浏览器原生 EventSource；Agent 走 MCP 同步接口 |

**开发视图约束**

| 决策 | 理由 |
|------|------|
| 5 子包分层 + 无环依赖矩阵 | 职责清晰；显式分层便于演进 |
| Alembic 作为 schema 真相源 | 迁移可重放、可降级保护 |

**物理视图约束**

| 决策 | 理由 |
|------|------|
| 深度绑定 PostgreSQL 扩展（pgvector/pg_textsearch/pg_trgm） | 一库承担向量/全文/模糊/图四种负载；不追求可替换 |
| 不做集群/分片/多租户强隔离 | 产品定位个人/小团队 |

**跨视图**

| 决策 | 理由 |
|------|------|
| 反馈幂等（ON CONFLICT）+ 行锁串行 | 防并发 TOCTOU 与读改写竞态 |
| 配置热更新白名单深合并 | 开关即时生效；白名单锁死 database / embedding.dimension |

---

```{admonition} 一页总结
:class: tip
**逻辑**：Event 是唯一真相源，Fact 是中心抽象（双时态 + 图边 + 信号），Playbook 在图上只读推理，六条不变量兜底。
**进程**：四类进程无共享内存，一切协调经 PostgreSQL（队列 / NOTIFY / 锁），全同步 + 线程池。
**开发**：五子包单向依赖（唯一 lazy import 例外），Alembic 是 schema 真相源。
**物理**：同一进程模型支持本地单进程 → 单机多进程 → 局域网分布三种摆放。
**场景**：用例总览（3 类参与者 × 11 个用例）承载功能需求；写入 / 召回 / 遗忘三条关键路径各配四视图对照表，验证五个投影说的是同一套系统。
```

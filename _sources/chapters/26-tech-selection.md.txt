# 第26章 后端选型论证：最小依赖约束下的 PG+扩展生态

## 1. 选型原则：低三方件引入

cortex-py 面向**个人/小团队的 Agent 长期记忆层**，部署形态是 Docker 单机（或 Podman），数据必须持久化。这个定位决定了选型的第一原则：

> **能不引入的三方件就不引入。** 每多一个独立服务，就多一个容器、一份内存、一套备份策略、一个一致性窗口。

把这个原则量化，就是一条预算线：

| 预算项 | 约束 |
|--------|------|
| 独立进程数 | 存储层 ≤ 1 个（不算应用自身） |
| 中间件 | 能用数据库内置能力解决的，不引入独立中间件 |
| 扩展 | 只接受"装进同一个 PG 进程"的扩展，不接受需要独立服务的能力 |
| 备份 | 必须一条 `pg_dump` / 一个数据卷搞定全部状态 |

最终结果：**1 个 PostgreSQL 18 进程 + 3 个扩展（pgvector / pg_textsearch / pg_trgm）**，承担了关系存储、向量检索、BM25 全文检索、模糊匹配、任务队列、事件总线、分布式锁、图遍历共 **8 种角色**。

本章的任务就是论证：在这条预算线下，当前这批三方件不是"妥协的次优解"，而是**最优解**。论证方式是把每个位置上的候选方案逐一摆出来，对照系统的真实需求逐项打分。

---

## 2. 需求画像：这个系统到底需要什么样的数据库

在对比之前，先从代码反推需求。cortex-py 对存储层提出了 9 条硬性要求，每一条都有源码出处：

| # | 需求 | 源码证据 |
|---|------|----------|
| 1 | 并发 OLTP 写入 | WAL append（`infra/core.py` 的 `append_event`）+ worker 池并发抽取 |
| 2 | 跨能力事务一致性 | fact 与 embedding 同事务写入（`graph/extraction/fact_store.py`） |
| 3 | 图遍历 | `WITH RECURSIVE` 2-3 跳 BFS（`graph/retrieval/channels.py`） |
| 4 | 任务队列 | `FOR UPDATE SKIP LOCKED LIMIT 1` 抢 job（`infra/core.py`） |
| 5 | 事件总线 | `pg_notify('cortex_lc')` + `LISTEN cortex_lc`（`infra/core.py`） |
| 6 | 分布式锁 | `pg_advisory_xact_lock` 防并发实体合并（`entity_resolution.py` 等 7 处） |
| 7 | 双时态 + JSONB | facts 表 4 个时间字段 + `operating_regime` GIN 索引 |
| 8 | 向量 / BM25 / 模糊匹配 | 6 通道检索中的三个通道 |
| 9 | Docker 内持久化 | 数据卷挂载，容器重建不丢数据 |

````{note}
这张表是全文的评分基准。后文每个候选方案的命运，都取决于它能满足这 9 条中的几条——尤其是第 2、4、5、6 条，它们正是"专用数据库"们普遍缺失的能力。
````

---

## 3. 数据库选型：逐一对比

### 3.1 候选名单

- **SQLite**：嵌入式关系库，零运维的代表
- **DuckDB**：嵌入式列存 OLAP 引擎
- **LanceDB**：面向 AI 的嵌入式向量数据库
- **ClickHouse**：列存 OLAP 服务
- **Neo4j**：专用图数据库（常见质疑："你不是知识图谱系统吗？"）
- **PostgreSQL 18 + 扩展**：当前选择

### 3.2 SQLite：单机嵌入很香，但扛不住多写者

SQLite 的诱惑力在于"零部署"，但它在本系统有三个致命伤：

1. **单写者模型**。SQLite 同一时刻只允许一个写事务，写锁是库级（WAL 模式下写者仍互斥）。本系统的写入方有：API 网关收 experience、worker 池并发跑抽取/链接/建 fact、Dreaming 离线巩固。多 worker 场景下 SQLite 会退化成串行写 + `database is locked` 重试风暴。
2. **需求 4/5/6 全部缺失**。没有 `SKIP LOCKED`（`SELECT ... FOR UPDATE` 语义不存在），没有 `LISTEN/NOTIFY`，没有 advisory lock。这三样如果用应用层模拟，等于把 PG 内置能力换成自己维护的轮子——与"低三方件"原则背道而驰。
3. **向量生态不成熟**。sqlite-vec 尚不提供生产级 HNSW 索引，且与 BM25（FTS5 只有朴素排序）、模糊匹配的能力组合仍需自己缝合。

SQLite 适合"单进程、读多写少"的场景；本系统是"多写者、事务密集"的管线，方向性不匹配。

### 3.3 DuckDB：分析利器，OLTP 不是它的主场

DuckDB 是优秀的嵌入式分析引擎，但设计目标是**查询密集的分析负载**：

- MVCC 语义面向批量追加写，点更新（本系统的核心操作：fact 状态流转、assertion_status 变更、双时态超替）不是它的优势路径；
- 同样没有服务端协议、`SKIP LOCKED`、`NOTIFY`、advisory lock；
- 向量检索（vss 扩展）仍是实验性质，无 HNSW 生产级支持。

如果 cortex-py 是一个离线分析工具，DuckDB 会是首选；但它是**在线事务系统**。

### 3.4 LanceDB：向量是它的唯一语言

LanceDB 面向"AI 数据湖"，列式 Lance 格式对向量+张量存储很高效，但：

- 没有 SQL 事务语义，facts（关系数据）和向量必须拆两个存储，回到双写一致性问题；
- 没有 JOIN、递归查询、锁、队列——需求 3~7 全部为零；
- 实体链接需要在"向量相似 + 结构化属性（entity_type、context_key、scope）"上做联合过滤，嵌入式向量库的过滤表达式能力远不如 SQL。

向量库当主库，等于让锤子做所有事。

### 3.5 ClickHouse：OLAP 巨兽装不进这个场景

ClickHouse 的强项是海量数据的聚合分析，与本系统的需求正好错开：

- **不擅长高频点更新**。`ALTER TABLE ... UPDATE`（mutation）是异步重写 parts，而 assertion_status 状态机、双时态 recorded_to 关窗都要求**同步、行级、事务性**的更新；
- 弱事务：不支持跨语句的 ACID 事务语义，需求 2 直接出局；
- 用 MergeTree 做 point lookup + 队列抢锁，是对引擎的错误使用；
- 部署重量级（单节点也需要独立镜像与配置），违反预算线。

### 3.6 Neo4j：图是能力之一，不是全部

"知识图谱系统为什么不用图数据库？"这是最常见的质疑。答案在需求清单里：

- 本系统的图查询是**受限遍历**——2-3 跳 BFS 且带 scope/predicate 过滤，`WITH RECURSIVE` 在 facts 表 B-tree 索引上足够高效（stage0 冒烟已验证）；
- Neo4j 只解决需求 3，不解决需求 2（向量同事务）、4、5、7、8。引入 Neo4j 意味着：PG 仍要保留（做其余 8 条），**依赖数 +1、一致性窗口 +1**；
- 谓词本体约束（封闭词表 + 准入规则）用 PG 的 CHECK 约束 + 词表 JOIN 表达即可，不需要图数据库的 schema 能力。

一句话：**图遍历是 PG 能力集的子集时，就不要为这个子集引入一个新系统。**

### 3.7 评分汇总

| 需求 | PG+扩展 | SQLite | DuckDB | LanceDB | ClickHouse | Neo4j |
|------|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 并发 OLTP 写 | ✅ | ⚠️ 单写者 | ⚠️ | ⚠️ | ⚠️ | ✅ |
| 2 跨能力事务 | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ |
| 3 图遍历 | ✅ 递归 CTE | ✅ | ✅ | ❌ | ❌ | ✅ |
| 4 SKIP LOCKED 队列 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 5 LISTEN/NOTIFY | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 6 advisory lock | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 7 双时态+JSONB | ✅ | ⚠️ | ⚠️ | ❌ | ⚠️ | ❌ |
| 8 向量/BM25/模糊 | ✅ 3 扩展 | ⚠️ 缝合 | ⚠️ | ⚠️ 仅向量 | ❌ | ❌ |
| 9 Docker 持久化 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **合计** | **9/9** | 3.5 | 3 | 2.5 | 1.5 | 3 |

**结论：PostgreSQL 是唯一一个 9/9 全满足的候选**——不是因为它每一项都最强，而是因为它是唯一一个"全都够用"的。

---

## 4. 向量检索：pgvector vs 专用向量库

### 4.1 候选

- **pgvector**（HNSW，当前选择）
- **Milvus**：分布式向量库旗舰
- **Qdrant**：Rust 高性能向量库
- **Weaviate**：带 GraphQL 接口的向量库
- **Chroma**：轻量嵌入式向量库

### 4.2 决定性论据：事务一致性

看实体链接的写入路径（`graph/extraction/fact_store.py`）：

```python
conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), ...)
# 同一事务内：
#   1) INSERT/UPDATE entities（含 embedding vector(1024)）
#   2) INSERT facts（引用 entity_id）
#   3) 更新 beliefs
# 提交 → 向量索引（HNSW）随事务自动更新
```

**实体 embedding 和引用它的 facts 在同一个事务里落盘**。如果向量放在独立的 Milvus/Qdrant：

- 写入变成"PG 事务 + 向量库 RPC"两步，必须引入 outbox 模式或双写补偿；
- 实体合并（merge）时要同步删改向量库里的条目，崩溃窗口内两边视图不一致；
- GDPR 真删（erasures 引用计数）要求**物理删除**向量和关联记录，跨库真删的一致性成本更高。

这些问题专用向量库都有标准答案（outbox、对账任务），但每个答案都是一个新组件或一套新代码——**预算线不允许**。

### 4.3 规模论据

专用向量库的优势在**亿级向量 + 高并发 ANN**。本系统的定位（个人/小团队记忆）：

- 向量规模：万级到百万级。pgvector 0.8 的 HNSW 在百万级 1024 维上召回延迟在个位数到几十毫秒，与专用库差距不构成体感差异；
- 检索模式：向量通道只是 6 通道之一，召回后立即进入 scope 过滤 + RRF 融合，**ANN 精度要求被下游流程放宽**（HNSW 的 recall@10 > 0.99 已足够）；
- 真正的排序质量来自 rerank 阶段，而不是 ANN 引擎本身。

### 4.4 运维论据

| 维度 | pgvector | Milvus/Qdrant |
|------|----------|---------------|
| 独立进程 | 0 | 1（Milvus 分布式版更多） |
| 内存开销 | 复用 PG shared_buffers | 独立一份 |
| 备份 | 随 `pg_dump` / 数据卷 | 独立快照机制 |
| SDK | psycopg 直连，零新增依赖 | 各家独立 SDK |
| 过滤 | 原生 SQL（scope/时态/谓词任意组合） | 有限的过滤表达式 |

````{note}
Chroma 单独说明：它和 pgvector 一样轻量，但它是"库"不是"服务"，嵌入应用进程意味着 API 进程和 worker 进程各持一份索引，多进程写入直接冲突。本系统 API/worker 分离的架构天然排除嵌入式向量库。
````

---

## 5. 全文检索（BM25）：pg_textsearch vs 搜索引擎

### 5.1 候选

- **pg_textsearch**（真 Okapi BM25 扩展，当前选择）
- **Elasticsearch / OpenSearch**：全文检索事实标准
- **Meilisearch / Typesense**：轻量搜索服务
- **PG 内置 tsvector/ts_rank**：零扩展方案

### 5.2 为什么 PG 内置不够

PG 自带的 `to_tsvector` + `ts_rank` 不是真 BM25——`ts_rank` 是基于词频/密度的启发式排序，没有逆文档频率项，长文档会被系统性高估。schema 里的内置 FTS 索引（`idx_facts_text_fts`）明确注释为"pg_textsearch 不可用时的兼容回退"。排序质量是召回质量的底线，这个位置不能将就。

### 5.3 为什么不是 Elasticsearch

ES 是全文检索的天花板，但对照预算线：

1. **资源重量级**：JVM 进程，生产建议 2GB+ 堆内存，比整个 PG 实例还重；
2. **中文分词外挂**：需要 IK 分词插件；本系统用 jieba-py 在应用层预分词写入 `fact_search_documents.tokenized_text`，分词策略（含自定义词典热更）完全自控；
3. **一致性窗口**：facts 在 PG，索引在 ES，写入链路变成双写。本系统的 BM25 投影表 `fact_search_documents` 与 facts **同库**，用触发器标脏（`mark_fact_search_scope_dirty`）+ 按 scope 增量重建，投影与源数据的一致性由同一个事务系统保证；
4. **ERASURE 联动**：GDPR 真删要求索引与源数据同步物理删除，同库方案是 `ON DELETE CASCADE`，跨库方案是一套同步协议。

### 5.4 为什么不是 Meilisearch / Typesense

两者都是优秀的 typo-tolerant 搜索服务，但：

- BM25 语义不完整（Meilisearch 的排序以相关性规则集表达，不是经典 Okapi BM25），而本系统的检索评估是按 BM25 通道设计的；
- 仍是独立服务 + 双写问题；
- 它们的差异化能力（即拼即搜、facets）在本系统没有消费者。

### 5.5 pg_textsearch 的代价与边界

公平起见，pg_textsearch 也有代价：需要独立安装二进制、`shared_preload_libraries` 配置、重启生效。项目为此提供了 `Dockerfile.postgres` 把这些步骤固化进镜像。**用一次性的镜像构建成本，换运行期零额外进程**——这笔账在 Docker 部署形态下是划算的。

---

## 6. 模糊匹配：pg_trgm vs 替代方案

模糊匹配在本系统有两个消费者：

- **实体名召回通道**（`graph/retrieval/channels.py`）：`similarity(canonical_name, :raw_name) > 0.3` + GIN trigram 索引；
- **Dreaming 聚类预筛**（`memory/dreaming.py`）：`similarity(body_a, body_b) >= threshold`，把疑似重复 fact 对预筛后再送 LLM 判定，省 token。

### 6.1 候选对比

| 方案 | 评价 |
|------|------|
| **pg_trgm**（✅ 当前） | contrib 内置，官方镜像自带，零安装成本；GIN 索引支持 `similarity()` 走索引；与 scope 过滤在同一 SQL 里组合 |
| Elasticsearch fuzzy query | 能力足够，但为了一个 `similarity()` 引入整个 ES，违反预算线 |
| Tantivy / tantivy-py | Rust 全文引擎，需要独立索引目录与同步逻辑，且 Python 绑定生态薄 |
| rapidfuzz / python-Levenshtein | 纯 Python 内存计算，无法建索引，实体数一多就是全表扫描 |
| PG 内置 ILIKE | 无索引加速（前缀除外），且没有相似度排序语义 |

### 6.2 结论

pg_trgm 是**零边际成本**的选择：它随 `postgres:18-bookworm` 镜像内置，一条 `CREATE EXTENSION` 即可。当模糊匹配的需求就是"带索引的三元组相似度"时，没有任何独立组件能给出比"数据库自带"更低的引入成本。

---

## 7. PG 的一身七职：中间件替代全景

第 3~6 节论证了检索层。最后把 PG 承担的非检索角色也列清楚——这些位置本可以引入独立中间件，但都被 PG 内置能力替代了：

| 角色 | 独立中间件方案 | PG 方案 | 源码 |
|------|----------------|---------|------|
| 任务队列 | Redis + RQ / RabbitMQ + Celery | `jobs` 表 + `FOR UPDATE SKIP LOCKED` | `infra/core.py` `claim_next_job` |
| 事件总线 | Redis Pub/Sub / Kafka | `pg_notify` + `LISTEN`（channel `cortex_lc`） | `infra/core.py` `emit_lifecycle` / `wait_for_stage` |
| 分布式锁 | Redis SETNX / etcd / ZooKeeper | `pg_advisory_xact_lock`（事务结束自动释放） | 实体链接/图变更/证据注册共 7 处 |
| 图遍历 | Neo4j / JanusGraph | `WITH RECURSIVE` | `channels.py` 图遍历通道 |
| 幂等写入 | 应用层去重缓存 | `idempotency_key` UNIQUE + `ON CONFLICT DO NOTHING` | `append_event` |
| 配置/词表存储 | 独立配置中心 | `vocabularies` / `predicate_definitions` 表 | schema.sql |
| 文档存储 | 对象存储 | `blobs` 表（inline BYTEA + SHA-256 去重） | schema.sql |

````{note}
Postgres-as-queue 有一个常被忽略的红利：**队列本身就是可查询的业务数据**。job 的状态、重试次数、错误信息都在 `jobs` 表里，运维页面直接 SQL 聚合，不需要给 Redis/RabbitMQ 再配一套监控。死信也不是新机制，只是 `status='failed' AND attempts >= max_attempts` 的一个查询条件。
````

advisory lock 对比 Redis 锁还有一个结构性优势：**锁的生命周期绑定事务**。实体合并持锁期间崩溃，事务回滚的同时锁自动释放，不存在 Redis 锁"持有者崩溃后等 TTL 过期"的窗口。

---

## 8. 应用层 Python 生态选型

数据库定了之后，应用层的选型同样遵循最小依赖原则：

| 位置 | 选择 | 对比与否决理由 |
|------|------|----------------|
| Web 框架 | **FastAPI** | vs Flask：原生 async + Pydantic 契约一体；vs Django：无 admin/ORM 负担，本项目不需要 |
| ASGI 服务器 | **uvicorn** | FastAPI 事实标准，不引入 gunicorn 多进程层（worker 并发由 Postgres 队列承担） |
| 数据契约 | **Pydantic v2** | vs 手写 dataclass：校验/序列化/配置三合一，`AppConfig` 启动即强校验 |
| DB 访问 | **psycopg3 + SQLAlchemy Engine（仅连接池）+ `text()` 裸 SQL** | vs SQLAlchemy ORM：本系统的 SQL 充满递归 CTE/窗口函数/advisory lock/`ON CONFLICT`，ORM 表达力不足反而增加翻译层；保留 Engine 只为 QueuePool 连接池与 `pool_pre_ping` |
| 迁移 | **Alembic** | 与 SQLAlchemy Engine 同源，无新增生态 |
| SSE | **sse-starlette** | 手写 SSE 要处理心跳/断连/`<think>` 分流边界，不值得 |
| HTTP 客户端 | **httpx2 + openai SDK** | LLM/Embedding/Rerank 全是 OpenAI 兼容接口，一个 SDK 覆盖三路外部服务 |
| 分词 | **jieba-py** | 纯 Python 依赖，替代"给 ES 装 IK 插件"的重方案 |
| MCP | **mcp (FastMCP)** | 官方 SDK 同时提供 stdio + streamable-http 双传输 |
| 配置 | **PyYAML + python-dotenv** | YAML 结构化 + 环境变量覆盖，不引入 consul/etcd |
| 测试/lint | **pytest + ruff** | 两个开发期工具，不进运行镜像 |

注意一个取舍：**没有引入 Celery、Redis、任何 ORM、任何配置中心**。worker 就是对 `jobs` 表的轮询消费循环——当队列是数据库表时，消费队列的 worker 不需要框架。

---

## 9. 依赖总账：两个架构的对照

把"行业惯例组合"与本项目的选择放在一起，差距一目了然：

| 维度 | 行业惯例组合 | cortex-py 选型 |
|------|--------------|----------------|
| 组件清单 | PG + Redis + Elasticsearch + Milvus + RabbitMQ | PG 18 + pgvector + pg_textsearch + pg_trgm |
| 独立进程数 | 5 | 1 |
| 容器数（最小部署） | 5 | 1（`Dockerfile.postgres`） |
| 内存预算 | PG 1G + Redis 0.5G + ES 2G + Milvus 2G + MQ 0.5G ≈ **6GB** | PG 单实例 ≈ **1GB** 量级 |
| 跨存储一致性 | 3 处双写窗口（向量/全文/队列） | **0**（同库同事务） |
| 备份策略 | pg_dump + Redis RDB + ES snapshot + Milvus backup + MQ 镜像 | **一个数据卷 / 一条 pg_dump** |
| 新增 SDK 依赖 | redis-py、elasticsearch-py、pymilvus、kombu | 0（psycopg 一个驱动打天下） |

````{note}
这张表不是否认 Redis/ES/Milvus 的价值——它们在各自的规模化场景都是正确答案。选型论证的前提永远是**约束条件**：本项目的约束是"个人/小团队 + Docker 单机 + 低三方件"，在这个约束下，PG+扩展生态是帕累托前沿上唯一的点。
````

---

## 10. 边界与逃生门

诚实的选型文档要写清楚**什么时候这个选择会失效**：

1. **向量规模破千万级**。pgvector HNSW 的构建时间和内存占用会先到瓶颈。逃生门：检索是 6 通道抽象（`graph/retrieval/channels.py`），向量通道可以整体替换为外部向量库，其余通道不动；
2. **BM25 检索 QPS 要求超过 PG 单机承载**。逃生门：`fact_search_documents` 是投影表，天然可以整体重放到 ES；
3. **需要多读副本/多区域部署**。Postgres 流复制可以解决读扩展，但队列消费需要额外分区设计；
4. **图遍历深度超过 3-4 跳**。递归 CTE 的性能随深度指数下降，届时才值得考虑专用图引擎。

当前系统离这四条线都有数量级的距离。在越过任何一条之前，**多引入一个三方件的边际收益是负的**——这正是本章结论的完整表述：

> 在低三方件引入的前提下，PostgreSQL 18 + pgvector + pg_textsearch + pg_trgm 不是"够用就行"，而是同时满足事务一致性、运维成本、能力覆盖三个维度的**唯一最优解**。

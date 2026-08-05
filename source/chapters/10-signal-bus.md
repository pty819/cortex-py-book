# 第10章 信号总线 — 记忆自演化的基础设施

````{admonition} 检索控制面重构后的信号模型变化
:class: important
本章描述的是信号总线的**物理字段与跨特性共享协议**,这些设计仍然成立。但检索加权阶段(§4)的信号消费方式已重构:**检索 pipeline 现在读 `facts` 表的 `retrieval_count` / `retrieval_usefulness`(而非旧的 `events.access_count`)**,且拆成四个可独立开关的信号(Salience / Usage / Usefulness / Exploration)。本章 §3/§4 已按新模型更新;四个信号的加权公式与 explore/exploit 机制详见 **第14章「信号总线加权」**。
````

## 1. 概述

Feedback(反馈回灌)、Dreaming(离线巩固)、Higher-Order(高阶归纳)这三个自演化特性,表面上各自独立,实际上共享同一套底层数据通路——**信号总线**(signal bus)。它不是一个新的模块或服务,而是 `events` 与 `facts` 两张表上一组被刻意设计出来的共享列:`access_count`、`retrieval_count`、`last_recalled_at`、`salience`、`retrieval_usefulness`、`feedback_processed`,以及 `positive_feedback_count` / `negative_feedback_count` 这两个冗余计数。

信号总线的本质是一份**跨特性可读写的运行时状态**:

- **recall** 在每次命中时向 `facts.retrieval_count`(+ `events` 冗余计数)写入隐式正反馈;
- **Feedback** 写 `facts.salience`(软降权)、`facts.retrieval_usefulness`(显式反馈值)与两个计数列;
- **Dreaming** 读 `access_count` 与 `salience`,决定哪些簇值得巩固、哪些 evidence 已冷;
- **Higher-Order** 读 `events.access_count`,只有累计被召回过的事实才会被归纳为高阶结论。

如果把这组列拆掉,三个特性就会退化为彼此隔离的孤岛——这正是 MindMemOS 原型的设计缺陷:它的反馈通道、巩固通道、归纳通道各自维护私有的热度统计,互相看不见对方的信号,导致"用户反复召回的记忆"和"系统决定巩固的记忆"之间出现系统性偏差。cortex-py 把信号总线下沉到 schema 层,让所有特性读写同一份物理状态,从而把记忆从静态存储变成一个自调整系统。

```{mermaid}
graph LR
    R[recall 命中] -->|writes| RC[facts.retrieval_count++ / events 冗余]
    RC --> SRW[四信号加权]
    FB[Feedback] -->|reads salience<br/>writes salience| SRW
    SRW -->|re-weighted score| RR[rerank]
    RC --> DR[Dreaming<br/>读 retrieval_count 选簇]
    RC --> HO[Higher-Order<br/>读 evidence_quality 门控]
    FB -->|writes| SAL[facts.salience]
    SAL --> SRW
    DR -->|失效 cache| CACHE[(recall_packs)]
    FB -->|失效 cache| CACHE
```

上图揭示了信号总线的双向性:recall 既是消费者也是生产者,Feedback 既是读者也是作者。本章逐字段拆解这条总线。

## 2. 信号字段

信号总线的物理定义集中在 `schema.sql` 第 821–838 行的"记忆自演化"段落。`events.access_count` 是建表时就有的原生列(`schema.sql:26`),其余列都是后续 `ALTER TABLE ADD COLUMN IF NOT EXISTS` 增量加上去的,因此老库可以平滑升级。

**`events` 表上的隐式信号:**

| 列 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `access_count` | `INT NOT NULL` | `0` | 召回计数(保留兼容,主要给 methylation 冷数据扫描的 partial index `access_count=0` 使用;检索加权已改读 `facts.retrieval_count`) |
| `retrieval_count` | `INT NOT NULL` | `0` | 新增:events 级召回计数,与 `facts.retrieval_count` 同步递增 |
| `last_recalled_at` | `TIMESTAMPTZ` | `NULL` | 最近一次被召回的时间戳 |
| `feedback_processed` | `BOOLEAN NOT NULL` | `false` | 是否已被反馈流水线处理过(幂等标记) |

**`facts` 表上的显式信号:**

| 列 | 类型 | 默认 / 约束 | 语义 |
|---|---|---|---|
| `salience` | `FLOAT NOT NULL` | `1.0`,`CHECK (0 <= salience <= 2)` | 显式反馈权重,`<1` 被负反馈降权,`>1` 被正反馈加权 |
| `retrieval_count` | `BIGINT NOT NULL` | `0` | **检索加权直接读**:被动召回次数,recall 每次命中 +1 |
| `retrieval_usefulness` | `FLOAT NOT NULL` | `0.0`,`CHECK (-1 <= x <= 1)` | **检索加权直接读**:显式 relevant/irrelevant 反馈累积值 |
| `positive_feedback_count` | `INT NOT NULL` | `0` | 正反馈累计次数(冗余,加速查询) |
| `negative_feedback_count` | `INT NOT NULL` | `0` | 负反馈累计次数,达阈值触发 methylation |

`salience` 的取值范围被 `CHECK` 约束钉死在 `[0, 2]`:`1.0` 是中性起点,正反馈向 `2.0` 上探,负反馈向 `0.1`(配置项 `salience_floor`)下探但不归零——保留可恢复性,避免一次误判永久抹掉一条记忆。两个 `_count` 列在 `feedback_signals` 表里其实也能 `COUNT` 出来,但这里冗余存储是为了让召回热路径上的 salience 重加权能用一次 `SELECT` 取齐所有信号,不必 join 反馈表。

完整的 `ALTER TABLE` 语句如下(`schema.sql:821-838`):

```sql
-- 信号总线:facts 软降权(salience)+ 反馈计数(冗余加速查询)
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS salience FLOAT NOT NULL DEFAULT 1.0
    CHECK (salience >= 0 AND salience <= 2);
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS positive_feedback_count INT NOT NULL DEFAULT 0;
ALTER TABLE cortex.facts ADD COLUMN IF NOT EXISTS negative_feedback_count INT NOT NULL DEFAULT 0;

-- 信号总线:events 隐式反馈幂等标记 + 最近被召回时间
ALTER TABLE cortex.events ADD COLUMN IF NOT EXISTS feedback_processed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cortex.events ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ;
```

注意 `IF NOT EXISTS`:这是信号总线能向后兼容的关键。老库迁移时这几条语句幂等执行,不会破坏既有数据。

## 3. 隐式反馈环:recall → retrieval_count

信号总线的写入端不止 Feedback。recall 本身在每次成功返回 pack 后(`track_usage=True` 时),会对命中的 fact 批量递增 `retrieval_count`。代码在 `src/cortex/graph/retrieval/pipeline.py` 的 `recall()` 返回路径(Pack 装配成功后):

```python
# 被动召回只更新 retrieval_count;access_count 仅保留为兼容统计,不再参与评分/知识晋升。
if track_usage and result_pack.get("layers", {}).get("facts"):
    _hit_ids = [f["fact_id"] for f in result_pack["layers"]["facts"] if f.get("fact_id")]
    if _hit_ids:
        try:
            with session_scope() as conn:
                conn.execute(text("""
                    UPDATE facts SET retrieval_count=retrieval_count+1
                    WHERE fact_id = ANY(CAST(:ids AS uuid[]))
                """), {"ids": "{" + ",".join(_hit_ids) + "}"})
                # events 冗余计数(access_count 仍写,但仅供 methylation 冷数据扫描;Higher-Order/Dreaming 已改读 facts.retrieval_count/evidence_quality)
                conn.execute(text("""
                    UPDATE events SET retrieval_count = retrieval_count + 1,
                                      access_count = access_count + 1,
                                      last_recalled_at = now()
                    WHERE event_id = ANY(SELECT unnest(supports) FROM facts
                                         WHERE fact_id = ANY(CAST(:ids AS uuid[])))
                """), {"ids": "{" + ",".join(_hit_ids) + "}"})
        except Exception:  # noqa: BLE001  信号采集不阻塞召回
            pass
```

这条语句有几个值得注意的设计点:

1. **主写 `facts.retrieval_count`,events 冗余同步。** 检索加权阶段直接读 `facts` 表(单表查询,无 JOIN),更快;`events` 上的 `access_count`/`retrieval_count` 仍写,供 Higher-Order/Dreaming 的门控沿用。
2. **`track_usage` 开关。** A/B preview 和显式 `track_usage=False` 的 recall **不写计数**——这是无副作用调参的前提(见第14章、第18章 `/v1/admin/retrieval/preview`)。
3. **`last_recalled_at = now()` 顺带刷新。** 这个时间戳是 Dreaming 冷热分区的依据之一。
4. **异常吞掉,不阻塞召回。** 信号采集是 best-effort:写失败不影响用户拿到 pack。这是"读优先于写"的取舍——召回的正确性高于信号的完整性。

**核心权衡:recall 不再是纯读操作。** 传统检索系统里 `query → result` 是无副作用的,但 cortex-py 的 recall(当 `track_usage=True`)每次都会触发一次 `UPDATE`。这带来隐式信号:无需用户显式点赞/踩,系统就能从"被召回的频率"中推断出记忆的价值。这是 MindMemOS 完全缺失的一环——它只有显式反馈通道,而真实用户极少主动反馈。

这个取舍是值得的:隐式信号的数据量比显式反馈大几个数量级,且不受用户惰性影响。Dreaming 和 Higher-Order 的门控阈值(见 §6、第 12 章和第 13 章)都建立在召回计数稳定增长的前提上。

## 4. 信号注入:融合后的四信号加权

信号总线的读取端最关键的一环,是检索 pipeline 在融合之后、rerank 之前,把记忆的"重要性信号"注入到候选分数里。检索控制面重构后,这从旧版的"双因子(salience + access_count)"升级为**四个可独立开关的信号**:Salience / Usage / Usefulness / Exploration。

读取的数据源也从"JOIN events 取 access_count"改为**直接读 facts 表的冗余列**(单表查询,无 JOIN):

```python
scores = _fuse([c_vec, c_bm25, c_graph, c_ent, c_syn, c_tmp], tuning)
if scores:
    # 批量取三个信号列(全部在 facts 表,无需 JOIN events)
    all_fids = list(scores.keys())
    sig_rows = conn.execute(text("""
        SELECT f.fact_id::text, coalesce(f.retrieval_count,0) AS retrievals,
               coalesce(f.salience,1.0) AS sal,
               coalesce(f.retrieval_usefulness,0.0) AS usefulness
        FROM facts f
        WHERE f.fact_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": "{" + ",".join(all_fids) + "}"}).fetchall()
    sig = {r[0]: (int(r[1] or 0), float(r[2] or 1.0), float(r[3] or 0.0))
           for r in sig_rows}
    for fid in all_fids:
        retrievals, sal, usefulness = sig.get(fid, (0, 1.0, 0.0))
        # 四信号各自有开关,详见第14章
        # usage: 饱和加法(防止高频 fact 无限加分)
        # usefulness: 显式反馈累积,线性加法
        # salience: 乘数混合(默认关)
        # exploration: 改候选选择而非改分数(explore/exploit 分配)
        ...
```

新的加权公式:

```
scores[fid] = scores[fid] * salience_multiplier + usage_bonus + usefulness_bonus
# (exploration 在此之后改 explore/exploit 名额分配,不改分数)
```

四个信号的语义、开关、默认权重与公式见 **第14章「信号总线加权」**。这里只点明信号总线在融合后的接入位置与数据源迁移:从 `events.access_count`(需 JOIN)改为 `facts.retrieval_count`/`retrieval_usefulness`(单表),更快且把"被动召回次数"与"显式反馈值"拆成独立信号,避免旧版"正反馈伪装成被动召回"的双重加权问题。

`coalesce` 兜底是新 fact 的常见情况:`salience` 默认 `1.0`、`retrieval_count` 默认 `0`、`retrieval_usefulness` 默认 `0.0`,可能产生 NULL,`coalesce` 保证不会因为缺信号而把分数算成 NULL。

```{mermaid}
sequenceDiagram
    participant Q as 查询
    participant CH as 6 通道
    participant RRF as RRF 融合
    participant SRW as 信号总线加权(4 信号)
    participant RR as rerank
    participant SP as StratifiedPack
    Q->>CH: embed + 检索
    CH->>RRF: 候选 fact_ids
    RRF->>SRW: scores[fid]
    SRW->>SRW: SELECT retrieval_count, salience, retrieval_usefulness FROM facts
    SRW->>SRW: scores = scores·sal_mult + usage_bonus + usefulness_bonus
    SRW->>RR: re-weighted scores
    RR->>SP: keep_idx
    SP->>SP: 写 retrieval_count++ (隐式反馈, track_usage=true 时)
```

注意时序:salience 重加权发生在 RRF **之后**、rerank **之前**。这是因为 RRF 的输入是各通道的 rank,与分数绝对值无关,不能掺入信号;而 rerank 是基于文本相关性的 LLM/模型打分,信号注入必须在它之前完成,否则 rerank 的语义判断会被信号噪声污染。重加权只调整候选池的排序和截断(`top_k`),不改变 rerank 阶段的输入文档内容。

## 5. 缓存失效

召回结果会被缓存到 `recall_packs` 表(默认 60 秒 TTL,详见第14章检索系统)。一旦 Feedback 或 Dreaming 修改了 `salience` / `access_count` / `excluded_from_recall`,这些缓存就变成了脏数据——如果不失效,用户下次召回会拿到基于旧信号的排序。

Feedback 的失效逻辑在 `src/cortex/memory/feedback.py`(信号写入后):

```python
# 失效 recall_packs 缓存(反馈应立即反映到下次召回)
if cfg.feedback.cache_invalidate:
    conn.execute(text("DELETE FROM recall_packs WHERE scope=:s"), {"s": scope})
    actions.append("cache_invalidated")
```

Dreaming 执行动作后做同样的失效(`src/cortex/memory/dreaming.py` 的 `_execute_actions` 和收尾阶段):

```python
conn.execute(text("DELETE FROM recall_packs WHERE scope=:s"), {"s": scope})
```

两处用的是同一条 SQL:`DELETE FROM recall_packs WHERE scope=:s`。按 scope 整段删除而非按 `pack_id` 精确删除,是因为 Feedback/Dreaming 对信号字段的修改影响的是该 scope 下**所有**查询的排序,无法预知哪些 pack 会受波及。scope 粒度的粗粒度失效换来的是实现简单和正确性保证——宁可多删,不可漏删。

`cache_invalidate` 是可配置项(`FeedbackCfg.cache_invalidate`,默认 `True`)。在反馈极高频的场景下可以关掉它,改由 TTL 自然过期,代价是反馈生效延迟最多 60 秒。

## 6. 热路径索引

信号总线引入了两种新的查询模式,需要专门的局部索引支撑。它们都建在 `schema.sql:905-911`:

```sql
-- ── 记忆自演化热路径索引(评审 H6)──────────────────────────────────────────────
-- higher_order 召回层 + 高阶模块按 (scope, subject_id) 查 is_higher_order=true 活跃 fact
CREATE INDEX IF NOT EXISTS idx_facts_higher_order
    ON cortex.facts (scope, subject_id) WHERE is_higher_order = true AND recorded_to IS NULL;
-- methylation 扫描(maintenance + feedback._check_methylation)冷数据按 scope+observed_at
CREATE INDEX IF NOT EXISTS idx_events_methylation
    ON cortex.events (scope, observed_at) WHERE excluded_from_recall = false AND access_count = 0;
```

**`idx_facts_higher_order`** 服务于 Higher-Order 模块和召回层的高阶 fact 检索。典型查询是"在某个 scope 下、某个 subject 的高阶活跃事实",即 `WHERE scope=:s AND subject_id=:sid AND is_higher_order=true AND recorded_to IS NULL`。这是一个**部分索引**(partial index):`WHERE is_higher_order = true AND recorded_to IS NULL` 把索引体积限制在"高阶且未归档"的子集上,绝大多数 fact 是一阶的,不会进入这个索引,既省空间又加速查询。

**`idx_events_methylation`** 服务于 methylation(甲基化/软剪枝)扫描。`feedback._check_methylation` 和 maintenance 任务需要找"还没被剪枝、且从未被召回过"的冷事件,即 `WHERE scope=:s AND excluded_from_recall=false AND access_count=0`。同样用部分索引:`excluded_from_recall = false AND access_count = 0` 只覆盖"仍可召回但零热度"的 event,这正是 methylation 候选集。一旦某 event 被 methylated(`excluded_from_recall=true`)或被召回过(`access_count>0`),它就自动移出这个索引。

两个索引的共同设计哲学:**用 `WHERE` 谓词把索引限定在"热查询真正关心的子集"上**。Postgres 的部分索引不会为不满足谓词的行建索引项,因此体积远小于全表索引,且查询规划器能识别谓词匹配后直接走索引扫描。这是信号总线从"加列"到"加索引"的自然延伸——列定义了信号,索引让信号可查。

## 7. 配置

信号总线的开关现在拆成四个独立字段(`AdvancedRetrievalCfg`,`src/cortex/infra/config.py`):

```python
class AdvancedRetrievalCfg(BaseModel):
    hyde_enabled: bool = False
    hyde_passages: int = 1
    multihop_enabled: bool = False
    multihop_count: int = 4
    # 四信号(各自独立开关 + 权重)
    salience_enabled: bool = False      # 乘数混合 salience(默认关)
    salience_weight: float = 0.0
    usage_enabled: bool = True          # 被动召回次数(饱和,默认开)
    usage_weight: float = 0.02
    usage_saturation: float = 5.0
    usefulness_enabled: bool = True     # 显式反馈累积(默认开)
    usefulness_weight: float = 0.05
    exploration_enabled: bool = True    # 新 fact 候选位(默认开)
    exploration_ratio: float = 0.10
    entity_vector_seed: bool = False
    question_routing: bool = False
```

四信号的取值语义:

| 信号 | 默认 | 关闭后行为 |
|---|---|---|
| **Salience** | 关(`weight=0.0`) | 不做乘数混合,salience 只供 Feedback/Dreaming 读,不进排序 |
| **Usage** | 开(`weight=0.02`,`saturation=5.0`) | 被动召回次数不影响分数(但仍被 recall 递增) |
| **Usefulness** | 开(`weight=0.05`) | 显式反馈累积值不影响分数(但 Feedback 仍写它) |
| **Exploration** | 开(`ratio=0.10`) | 不为新 fact 保留候选位,纯按分数排序(exploit-only) |

各信号权重与 Feedback 的 `positive_weight` / `negative_weight`(见 `FeedbackCfg`)是配套的:前者控制信号对排序的影响强度,后者控制信号本身的产生速率与边界。完整的运行配置(含环境变量覆盖、热更新白名单)见{doc}`24-config-and-frontend`,四信号加权公式与 explore/exploit 机制见{doc}`14-retrieval-system`。

值得注意:整个 `retrieval` 子树在运行时配置热更新的白名单内(`_CONFIG_PATCH_WHITELIST` 含 `retrieval`),因此可以在不重启服务的前提下动态调整四信号增益——配合 `/v1/admin/retrieval/preview` 的无副作用 A/B 预览(第18章),可以反复试不同开关组合找到最优配置再保存生效。

## 8. 小结

信号总线不是一个新的特性,而是 Feedback、Dreaming、Higher-Order 三个特性得以协同的**底层协议**。它的物理形态是 `events` 与 `facts` 上的一组共享列(`access_count`、`retrieval_count`、`last_recalled_at`、`salience`、`retrieval_usefulness`、`feedback_processed`、两个 `_count`),逻辑形态是 recall → 四信号加权 → Feedback/Dreaming/Higher-Order 的双向数据流。

关键设计决策回顾:

1. **隐式信号优先于显式信号。** recall 每次命中都递增 `access_count`,无需用户主动反馈——这弥补了 MindMemOS 只有显式反馈通道的缺陷。
2. **recall 不是纯读操作。** 写放大换来的是系统性自调整能力,异常被吞掉以保证读优先。
3. **salience 软降权而非硬删除。** `CHECK (0 <= salience <= 2)` + `salience_floor = 0.1` 保证记忆可恢复,不因一次误判永久丢失。
4. **部分索引限定热子集。** `idx_facts_higher_order` 和 `idx_events_methylation` 用 `WHERE` 谓词把索引体积压到最小,同时精确匹配两种热查询模式。
5. **缓存按 scope 粗粒度失效。** Feedback 和 Dreaming 都用 `DELETE FROM recall_packs WHERE scope=:s`,宁可多删不可漏删。

信号总线是记忆系统的"神经系统":recall 是感觉输入,access_count 是神经冲动,salience 是突触权重,Feedback 是意识层面的修正,Dreaming 是睡眠期的巩固,Higher-Order 是抽象推理。没有这套共享状态,这些功能各自为政;有了它,记忆才从静态存储变成一个自调整、自演化的系统。后续三章(第 11 章 Feedback、第 12 章 Dreaming、第 13 章 Higher-Order)都建立在本章描述的信号字段与数据流之上。

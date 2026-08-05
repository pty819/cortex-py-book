# 第12章 Dreaming 离线巩固 — 两阶段 LLM + 精确去重

## 概述

`dreaming.py` 实现 cortex-py 的**离线记忆巩固**(offline memory consolidation):在系统空闲期扫描同一主体下累积的事实,完成精确去重、冲突仲裁与高阶信息提炼,让长期运行的图谱不至于被重复/冲突/低价值 fact 污染而拖垮召回质量。

设计上借鉴了 MindMemOS 的**两阶段 LLM 分离**思路——把"发现问题"(relation_detect)与"决定动作"(action_plan)拆成两次独立的 LLM 调用,各自拥有专属 prompt 与约束,避免单次调用既要分类又要规划导致输出失控。在此基础上,cortex-py 做了三点关键适配:

1. **双时态软关**:不使用 `status='archived'` 这类状态标记,而是写 `recorded_to=now()` 把事实软关,保留历史可回溯(见 `facts.recorded_to`)。合并产生的新 fact 的 `valid_from` 取源 fact 中最早的一个,保证时间链不断裂。
2. **代码层 Subject 守卫**:聚簇与 LLM 输入的边界由 SQL `GROUP BY subject_id` 在代码层强制,不依赖 prompt 约束 LLM"只比较同一主体"——prompt 不可靠,代码可靠。
3. **精确去重先行**:Phase 0 复用 `consolidation_run`(纯 SQL、无 LLM),先清掉完全相同的重复项,再让昂贵的 LLM 只处理语义层面的疑似关系。

核心源码位于 `src/cortex/memory/dreaming.py`(~487 行),配置在 `src/cortex/infra/config.py` 的 `DreamingCfg`,调度与心跳在 `src/cortex/interfaces/worker/runner.py`。

## 四阶段管线

```{mermaid}
graph TB
    subgraph Phase0["Phase 0: 精确去重(无 LLM)"]
        P0["consolidation_run<br/>min_age_hours=0<br/>纯 SQL 完全重复消除"]
    end
    subgraph PhaseA["Phase A: 候选发现(纯 SQL)"]
        PA["GROUP BY subject_id<br/>HAVING count >= min_cluster_size<br/>时间窗 lookback_days<br/>可选 pg_trgm 预筛"]
    end
    subgraph PhaseBC["Phase B + C: 两阶段 LLM(per 簇)"]
        PB["Phase B: _relation_detect<br/>LLM #1 分类 7 种 issue_type"]
        PC["Phase C: _action_plan<br/>LLM #2 规划 5 种动作"]
        PB --> PC
    end
    subgraph PhaseD["Phase D: 异步重合成"]
        PD["enqueue_job synthesize<br/>priority=-1"]
    end
    P0 --> PA --> PB --> PC --> PD

    LLM{"services.llm_configured<br/>(synthesis tier)?"}
    PA --> LLM
    LLM -->|未配置| SKIP["跳过 B/C<br/>仅 Phase 0 增强版"]
    LLM -->|已配置| PB
```

整个管线由 `dream_run()`(`dreaming.py` 第 32-54 行,内容在 `_dream_run_impl` 第 54-175 行)编排。每个阶段的结果都汇总进 `summary["phases"]`,最终写入 `dreaming_runs` 表。

## Phase 0: 精确去重

Phase 0 直接复用第 21 章介绍的 `consolidation_run`,但有一个关键参数差异:

```python
# dreaming.py 第 89-94 行
if dry_run:
    p0 = {"action": "consolidation", "scope": scope, "facts_closed": 0,
          "groups": 0, "note": "dry_run skipped"}
else:
    p0 = consolidation_run(scope, min_age_hours=0)
```

`consolidation_run` 的默认签名是 `min_age_hours=24`(见 `maintenance.py` 第 56 行),意为常规后台合并只处理入库满 24 小时的事实,避免对刚抽取的事实操作。但 Dreaming 是**手动或定时触发**的离线任务,操作员主动调用时不希望再等 24 小时,因此这里显式传 `min_age_hours=0`,让精确去重覆盖所有 live fact。

`dry_run=True` 时 Phase 0 被跳过(不写库),仅做候选发现以供预览,随后直接返回(`dreaming.py` 第 66-76 行)。把 Phase 0 放在最前的价值在于:完全相同的重复项用 SQL 一次清掉成本极低,不应消耗 LLM token,只有 SQL 无法判定的语义关系才进入后续阶段。

## Phase A: 候选发现

Phase A 由 `_discover_clusters()` 实现(`dreaming.py` 第 177-222 行),纯 SQL 聚簇,无 LLM 参与。核心查询按 `subject_id` 分组,只取 live fact(`recorded_to IS NULL AND valid_to IS NULL`),并施加时间窗:

```sql
-- dreaming.py 第 193-207 行
SELECT subject_id::text, array_agg(fact_id::text) AS fids, count(*) AS n
FROM facts
WHERE scope=:s AND recorded_to IS NULL AND valid_to IS NULL
  AND extracted_at < now() - make_interval(secs => :secs)    -- 下界:至少 min_age_hours 前
  AND extracted_at >= now() - make_interval(days => :lb)      -- 上界:lookback_days 内
GROUP BY subject_id
HAVING count(*) >= :min    -- min_cluster_size
ORDER BY count(*) DESC
LIMIT :max                 -- max_scopes_per_run
```

时间窗是**双界**的:`extracted_at ∈ [now-lookback_days, now-min_age_hours]`。下界 `min_age_hours` 避免对刚抽取、尚可能被后续事件修正的事实下手;上界 `lookback_days` 限制扫描范围,防止对超大 scope 全表回溯。

**Subject 守卫**是这一阶段最重要的设计决策:聚簇键就是 `subject_id`,代码层保证送入 LLM 的一组 fact 必然属于同一主体。`_discover_clusters` 注释明确写道"代码层 Subject 守卫:只把同 subject 的 fact 送 LLM(不靠 prompt)"。这把"不要跨主体比较"这条约束从不可靠的 prompt 提示下沉到 SQL `GROUP BY`,从根上杜绝了 LLM 误判跨主体关系的可能。

### 可选的 pg_trgm 预筛

当 `similarity_threshold` 落在 `(0, 1]` 区间时(默认 0.85 即满足),Phase A 会额外调用 `_similarity_prefilter()`(`dreaming.py`),用 PostgreSQL 的 `pg_trgm` 扩展做三元组相似度预筛:

```python
# 0 表示显式关闭;正常的 (0,1] 阈值必须实际参与候选发现。
# complementary 的广域分析应使用较低阈值或 0,同时仍受单簇硬上限保护。
if 0.0 < similarity_threshold <= 1.0:
    facts = _similarity_prefilter(conn, scope, facts, similarity_threshold)
```

```python
sim = conn.execute(text("SELECT similarity(:a, :b)"),
                   {"a": ta, "b": tb}).scalar()
if sim is not None and sim >= threshold:
    keep.add(fa["fact_id"])
    keep.add(fb_["fact_id"])
    break
```

阈值语义:

| `similarity_threshold` | 行为 |
|---|---|
| `0.0` | **显式关闭**预筛(广域 complementary 分析用,簇可能很大,仍受 `max_facts_per_cluster` 硬上限保护) |
| `(0, 1]`(默认 `0.85`) | **启用** pg_trgm 预筛,只保留相似度 >= 阈值的 fact 对 |
| `> 1.0` | 配置层 `le=1.0` 校验拒绝,无法配出 |

注意默认 `0.85` **会触发预筛**。若想关闭以做广域 complementary 分析(簇内可能包含文本相似度低但语义互补的 fact,如"温度过高"与"散热风扇停转"),应显式设为 `0`。配置项有 `ge=0.0, le=1.0` 约束。

聚簇完成后,`_load_facts_detail()` 加载每条 fact 的详情(predicate、object_text、effective_time、assertion_status、salience),供后续 LLM 阶段使用。其中 `effective_time` 已在 SQL 里预计算为 `coalesce(valid_from, extracted_at)`,供 Phase C 的 latest-wins 仲裁直接取用。

## Phase B+C: 两阶段 LLM（跨簇并发）

Phase B 与 Phase C 各是一次 LLM 调用，簇间互相独立。Phase B 对所有簇**并发**跑关系检测，收集全部 issue 后，Phase C 对所有 (cluster, issue) 对**并发**跑行动规划，最后串行执行写入（事务安全）。这种分离设计的核心收益是**职责单一、约束可校验**:Phase B 只负责分类,输出是受控的 7 选 1 枚举;Phase C 只负责规划,输入是已分类的 issue + 相关 fact,输出是受约束的 5 种动作。两次调用各自可以独立做异常处理与降级。

```python
from cortex.infra.concurrency import parallel_map

valid_clusters = [c for c in clusters[:max_clusters] if len(c["facts"]) >= min_cluster_size]

# Phase B:所有簇的 relation_detect 并发(N 簇 → N 路 LLM 同时跑)
phase_b_results = parallel_map(
    lambda c: _relation_detect(c["facts"]), valid_clusters
)
# 收集 (cluster, issue) 对,跨簇跨 issue 全部独立
issue_pairs = []
for cluster, issues in zip(valid_clusters, phase_b_results):
    if issues:
        for issue in issues:
            issue_pairs.append((cluster, issue))

# Phase C:所有 (cluster, issue) 的 action_plan 并发(M 对 → M 路 LLM 同时跑)
phase_c_results = parallel_map(
    lambda pair: _action_plan(pair[0]["facts"], pair[1]), issue_pairs
) if issue_pairs else []

# 写入串行执行(事务安全 + advisory lock 语义)
if not dry_run:
    for (cluster, _issue), actions in zip(issue_pairs, phase_c_results):
        if actions:
            _execute_actions(scope, actions, cluster["facts"])
```

`parallel_map` 基于 `ThreadPoolExecutor`，GIL 在网络 `recv()` 处释放，N 个簇的 LLM 调用实现真并行。结果保序，单项异常返回 None 不阻断其余。写入（`_execute_actions`）保持串行以保证事务顺序和 advisory lock 语义。这把 N 个簇的串行 LLM 延迟从 O(N) 降到 O(N / max_workers)。

### Phase B: 关系检测(LLM #1)

`_relation_detect()`(`dreaming.py` 第 302-323 行)把整簇 fact 序列化为 JSON 喂给 LLM,prompt 为 `DREAMING_RELATION_DETECT`(`prompts.py` 第 546-580 行)。该 prompt 定义了 **7 种 issue_type**:

| issue_type | 含义 | 后续典型动作 |
|---|---|---|
| `duplicate` | 完全重复(同 subject+predicate+object,仅来源不同) | archive 冗余项 |
| `conflict` | 同 subject+predicate 但 object 矛盾 | latest-wins 仲裁 |
| `near_duplicate` | 语义近似但不完全相同 | merge |
| `complementary` | 同主题非冲突碎片,合并更完整 | merge / create |
| `low_value` | 低价值/无信息量(如"设备存在") | archive |
| `ambiguous` | 关系不确定,无法判定 | update_quality 降权 |
| `other` | 不属于以上类别 | 不操作 |

prompt 中特别强调了**负面规则**(什么不是冲突/重复),这是抑制 LLM 过度归并的关键:

```
- subject 相同但 predicate 不同 -> 不是重复(是不同维度的信息)
- predicate 相同但 subject 不同 -> 不是冲突(是不同主体)
- object 相同但 (subject, predicate) 不同 -> 两条都可保留
- 仅共享一个宽泛类别(如"都是故障")不构成重复
```

LLM 返回的 JSON 经 `services.parse_llm_json()` 解析,取 `issues` 数组;解析或调用异常时返回空列表(降级,不阻断后续簇处理),见 `dreaming.py` 第 315-323 行的 try/except 包裹。

### Phase C: 行动规划(LLM #2)

`_action_plan()`(`dreaming.py` 第 324-350 行)接收 Phase B 产出的单个 issue 及其 `fact_ids` 对应的 fact 详情,调用 `DREAMING_ACTION_PLAN` prompt(`prompts.py` 第 587-626 行)规划具体动作。该 prompt 定义 **5 种动作**:

1. **archive**:软关(`recorded_to=now()`)。适用 duplicate 冗余项、low_value 事实。
2. **merge**:合并多条为新 fact 并归档所有源 fact,新 fact 的 `valid_from` 取最早源 fact。
3. **create**:从多条 fact 提炼高阶结论,**不归档**源 fact。
4. **update_quality**:调整 `salience`,不归档。适用 ambiguous 或不确定时,只降权不删。
5. **link**:在 fact 间建立 supports 关联。

prompt 中两条最重要的规则:

- **latest-wins**:对 `conflict`,保留 `effective_time` 最新的 fact,归档旧的。`effective_time` 优先级 `valid_from > extracted_at`,已在 Phase A 的 SQL 中预计算。
- **False archival is worse than leaving both active**:不确定时宁可不操作。这条规则贯穿整个 Dreaming 的保守哲学——错误归档会丢失信息且难以恢复,而保留两条活跃 fact 的代价只是召回时多一份噪声。

对 `new_salience`,prompt 明确约束 **必须在 `[0, 2]` 范围内**(0=完全降权,1=默认,2=最高)。即便 LLM 输出越界,代码层也会 clamp(见下一节)。对 `effective_time` 相同的冲突,prompt 要求双方都保留,最多标 `ambiguous` 降权,不归档任何一方。

## _execute_actions — 写 pending 候选,不直接改知识图

````{admonition} 关键设计变更
:class: important
Dreaming **不再直接修改 facts 表**(不再用 SAVEPOINT 隔离地执行 archive/merge/create)。现在 `_execute_actions` 把每个 LLM action 写成一条 `evolution_candidates(status='pending')` 候选,等人工审批(`evolution.review_candidate`)通过后才由 `_approve_dreaming` 执行真实变更。这是"不污染 verified graph"护栏的核心。
````

```python
# dreaming.py — _execute_actions
"""把 LLM action 写成 pending review candidate;不直接修改知识。"""
def _execute_actions(scope, actions, cluster_facts):
    fact_map = {f["fact_id"]: f for f in cluster_facts}
    with session_scope() as conn:
        for act in actions:
            try:
                atype = act.get("action")
                fids = act.get("fact_ids", [])
                # 只接受 4 种落地动作;link 动作被丢弃
                if not fids or atype not in {"archive", "merge", "create", "update_quality"}:
                    continue
                source = next((fact_map[fid] for fid in fids if fid in fact_map), None)
                if not source:
                    continue
                conn.execute(text("""
                    INSERT INTO evolution_candidates(
                        scope,source_type,proposed_action,subject_id,predicate,payload,
                        source_fact_ids,status,proposed_confidence,reasoning)
                    VALUES(:s,'dreaming',:action,CAST(:subject AS uuid),:predicate,
                           CAST(:payload AS jsonb),CAST(:ids AS uuid[]),
                           'pending',:confidence,:reasoning)
                """), {...})
            except Exception as e:  # 单个坏 proposal 不阻断整轮
                log.warning("dreaming proposal failed (action=%s), skipped: %s", atype, e)
```

设计要点:

- **不碰 facts 表**:每个 action 落一条 `evolution_candidates(status='pending', source_type='dreaming')`,`payload` 存完整 LLM action JSON,`source_fact_ids` 存涉及的 fact。真实 archive/merge/create/update_quality 只在审批通过后由 `evolution._approve_dreaming` 执行。
- **try/except 隔离**:单个坏 proposal(如不合法 fact_id)只 `log.warning` 跳过,不阻断整轮。旧版的 SAVEPOINT 不再需要——因为这里不再有"部分写入已落到 facts"的风险,候选写入要么成功要么跳过。
- **4 种落地动作**:`archive`/`merge`/`create`/`update_quality`。第 5 种 `link`(关系建议)被直接 `continue` 丢弃。
- **salience clamp 移到审批阶段**:`_approve_dreaming` 执行 `update_quality` 时才 clamp salience 到 `[0,2]`,生成阶段不碰。

这与 Higher-Order(第13章)的"只生成 candidate tier 待审"是同一套审批门设计,统一由 `evolution.review_candidate` 把关(见第2章 evolution_candidates 表)。审批通过才改 verified graph,避免 LLM 幻觉直接污染知识。

## 并发与调度

Dreaming 的并发控制分三层:数据库 advisory lock、worker scheduler、heartbeat。三者共同保证同一 scope 的 dream job 不会被重复执行或并发执行。

```{mermaid}
graph LR
    subgraph Scheduler["Worker scheduler"]
        S1["_maybe_schedule_dreaming<br/>每 schedule_interval_hours 检查一次"]
        S2["查 dreaming_runs 最后完成时间"]
        S3{"已有 queued/running<br/>dream job?"}
        S4["enqueue dream job<br/>priority=-1, min_age_hours=0"]
        S1 --> S2 --> S3
        S3 -->|是| SKIP["跳过,不重复排程"]
        S3 -->|否| S4
    end
    subgraph Dispatch["Worker dispatch"]
        D1["claim dream job"]
        D2["_JobHeartbeat 启动<br/>(所有 job 共用,vis/3 间隔)"]
        D3["dream_run"]
        D4["advisory lock<br/>pg_try_advisory_lock (session 级)"]
        D1 --> D2 --> D3
        D3 --> D4
        D4 -.->|finally unlock| D3
    end
    S4 --> D1
```

### advisory lock

`dream_run()` 入口处用 PostgreSQL **会话级** advisory lock 序列化同一 scope 的并发 run(`dreaming.py`):

```python
# dreaming.py — dream_run 入口
def dream_run(scope, ...):
    """Hold a session advisory lock for the complete Dreaming run, not only setup."""
    lock_conn = get_engine().connect()   # 独立 connection,覆盖完整 run
    lock_key = f"dream:{scope}"
    try:
        locked = lock_conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                                   {"key": lock_key}).scalar()
        if not locked:
            return {"scope": scope, "skipped": "another dream run in progress for this scope"}
        return _dream_run_impl(scope, ...)
    finally:
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_key})
        finally:
            lock_conn.close()
```

关键设计:

- **会话级 `pg_try_advisory_lock`(非事务级 `pg_try_advisory_xact_lock`)**。因为 dream run 会进出多个 `session_scope`(Phase 0/A/B/C 各自的事务),事务级锁会在第一个 session 提交时释放,无法覆盖完整 run。会话级锁绑定一个**独立 connection**(`lock_conn`),覆盖从入口到 `_dream_run_impl` 返回的整个生命周期,`finally` 里显式 `pg_advisory_unlock` 释放。
- **lock key 是 `f"dream:{scope}"`**(非裸 scope),`hashtext` 映射为 32 位整数。命名空间前缀避免与其他子系统的 advisory lock 冲突。
- 非阻塞:拿不到锁立即返回 false,本次 run 跳过(返回 `skipped`)而非等待。这是最后一道防线——即使 scheduler 的 H2 去重因竞态漏过,advisory lock 也能阻止两个 worker 同时跑同一 scope 的 dream。

### worker scheduler

`_maybe_schedule_dreaming()`(`runner.py` 第 61-101 行)是嵌入 worker 主循环的轻量调度器,每次 reaper 周期触发一次检查:

```python
# runner.py 第 69-71 行
interval = cfg.dreaming.schedule_interval_hours * 3600
if now - last_check < interval:
    return last_check
```

它扫描所有有 live fact 的 scope,查 `dreaming_runs` 表中该 scope 最后一次 `status='completed'` 的时间,若超过 `schedule_interval_hours`(默认 24h)则触发。触发前有 **H2 去重守卫**:

```python
# runner.py 第 89-93 行
# H2:仅在无 queued/running dream job 时插入(防多 worker 重复排程)
already = conn.execute(text(
    "SELECT 1 FROM jobs WHERE job_type='dream' AND scope=:s "
    "AND status IN ('queued','running') LIMIT 1"), {"s": sc}).fetchone()
if already:
    continue
```

插入的 dream job 携带 `payload={"min_age_hours": 0}`,让 dream_run 跳过常规的 24h 年龄下界(见 Phase 0)。`priority=-1` 是低优先级,避免抢占 extract 等实时任务。

### heartbeat

Dream job 运行时间长(一次可能处理数十个聚簇、两次 LLM 调用 × 聚簇数),极易超过 worker 的 `visibility_timeout_secs`(默认 300s)。若不加保护,reaper 会在 300s 后误判 dream job 为僵尸并重置其状态,导致另一个 worker 重新 claim 并**并发执行同一 dream job**。

````{admonition} 设计变更
:class: important
旧版有一个 dream 专用的 `_DreamHeartbeat` 类。现已重构为**所有 job 共用的 `_JobHeartbeat`**(`runner.py`),在 `run_worker` 外层包裹**每个** job(不只是 dream),带 **owner-fencing**:lease 丢失(被别的 worker 抢走)则 `stop.set()` 自停。
````

`_JobHeartbeat`(`runner.py`)是所有 job 共用的上下文管理器,不再为 dream 专用:

```python
# runner.py — 所有 job 共用的心跳(含 owner fencing)
@contextmanager
def _JobHeartbeat(job_id, worker_id, interval=60.0):
    stop = threading.Event()
    def _beat():
        while not stop.wait(interval):
            with session_scope() as c:
                # heartbeat_job 带 owner fencing:lease 丢失则返回 False
                if not heartbeat_job(c, job_id, worker_id):
                    stop.set()   # 别的 worker 抢走了,停止续租
                    return
    threading.Thread(target=_beat, daemon=True).start()
    try:
        yield
    finally:
        stop.set()
```

关键设计:

- **所有 job 共用**,在 `run_worker` 的 job 循环外层包裹(`with _JobExecutionLock(job["job_id"]): with _JobHeartbeat(job["job_id"], worker_id, heartbeat_interval):`),dream job 不再有专用心跳,也不在 `_dispatch` 的 dream 分支内单独包裹。
- **心跳外套一层 `_JobExecutionLock`**:`_JobHeartbeat` 外面还有 `_JobExecutionLock(job_id)`(基于 `pg_advisory_lock(hashtext('job:<id>'))` 的独立连接锁),防止 handler 的副作用仍在执行时被 reaper 回收/重新分配——即使 heartbeat 已停,执行锁也保证同一个 job 不会被并发跑两次。
- **心跳间隔随 visibility_timeout 动态算**:`heartbeat_interval = max(1.0, min(60.0, vis / 3.0))`(`vis = cfg.worker.visibility_timeout_secs`),非固定 60s。
- **owner fencing**:`heartbeat_job(c, job_id, worker_id)` 内部用 `WHERE job_id=:j AND locked_by=:w` 条件更新——如果 lease 已被别的 worker 抢走(reaper 重排后),更新影响 0 行返回 False,心跳线程 `stop.set()` 自停,避免为不属于自己的 job 续命。

## dreaming_runs 表

每次 `dream_run` 在入口创建一条 `dreaming_runs` 记录,结束时回写统计。DDL 位于 `schema.sql` 第 875-887 行:

```sql
CREATE TABLE IF NOT EXISTS cortex.dreaming_runs (
    run_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','completed','failed')),
    phase0_closed    INT NOT NULL DEFAULT 0,    -- 精确去重关了多少 fact
    phase_a_clusters INT NOT NULL DEFAULT 0,    -- 发现多少候选簇
    phase_b_issues   INT NOT NULL DEFAULT 0,    -- LLM 发现多少 issue
    phase_c_actions  INT NOT NULL DEFAULT 0,    -- 执行多少动作
    summary          JSONB                       -- 详细统计
);
CREATE INDEX IF NOT EXISTS idx_dreaming_runs_scope
    ON cortex.dreaming_runs (scope, started_at DESC);
```

各列含义:

| 列 | 说明 |
|---|---|
| `run_id` | 主键,`dream_run` 返回的 `summary` 不含 run_id,需通过 API 查询 |
| `scope` | 巩固的作用域 |
| `started_at` | 创建记录时(`INSERT ... DEFAULT now()`) |
| `completed_at` | `_complete_run` 或失败分支回写 |
| `status` | `running` → `completed` / `failed`。异常时(`dreaming.py` 第 165-174 行)回写 `failed` 并存 `summary.error` |
| `phase0_closed` | Phase 0 精确去重关闭的 fact 数 |
| `phase_a_clusters` | Phase A 发现的候选聚簇数 |
| `phase_b_issues` | Phase B LLM 发现的 issue 总数 |
| `phase_c_actions` | Phase C 执行的动作总数 |
| `summary` | 完整 `summary` dict 的 JSON,含各阶段明细 |

`_complete_run()`(`dreaming.py` 第 461-475 行)在写完统计后还做两件事:失效该 scope 的 `recall_packs` 缓存(`DELETE FROM recall_packs WHERE scope=:s`),确保下次召回能看到 dreaming 期间的 archive/merge 改动;并 `emit_lifecycle(kind="dreamed")` 发出生命周期事件。失败分支同样会失效 `recall_packs`。

## 无 LLM key 降级

Dreaming 的 LLM 阶段依赖 `synthesis` tier(默认)。若该 tier 未配置 API key,`dream_run` 会优雅降级——只运行 Phase 0,跳过 Phase B/C:

```python
# dreaming.py 第 107-109 行
if not services.llm_configured(cfg.dreaming.llm_tier):
    summary["note"] = f"LLM tier '{cfg.dreaming.llm_tier}' not configured, skipping Phase B/C"
    _complete_run(run_id, scope, p0["facts_closed"], len(clusters), 0, 0, summary)
    return summary
```

此时 Dreaming 等价于一次**增强版 consolidation**:Phase 0 精确去重照常执行(`min_age_hours=0`),Phase A 候选发现照常运行(但不送 LLM),`phase_b_issues` 和 `phase_c_actions` 记为 0。`summary.note` 字段记录降级原因,供运维排查。

这一设计意味着 `DreamingCfg.enabled=true` 但未配 LLM key 的部署不会报错,只是巩固能力退化为纯精确去重——对初期试运行或成本敏感场景很友好。

## API

### POST /v1/admin/dreaming

触发一次 dreaming 巩固,需 admin 权限。请求体 `DreamingRequest`(`schemas.py` 第 629-632 行):

```python
class DreamingRequest(BaseModel):
    scope: str
    dry_run: bool = False
    async_enqueue: bool = False
```

实现位于 `operations.py` 第 48-58 行:

```python
@router.post("/v1/admin/dreaming")
def admin_dreaming(body: schemas.DreamingRequest):
    if body.async_enqueue:
        job_id = enqueue_job(
            job_type="dream",
            scope=body.scope,
            payload={"dry_run": body.dry_run},
            priority=-1,
        )
        return {"status": "queued", "job_id": job_id, "scope": body.scope}
    return dream_run(body.scope, dry_run=body.dry_run)
```

`async_enqueue=True` 时入队 dream job 立即返回 `job_id`,由 worker 异步执行(会触发前述的 heartbeat 机制);`False` 时同步阻塞执行,直接返回 `dream_run` 的 `summary`。`dry_run=True` 仅发现候选不执行任何写操作,用于预览巩固范围。该端点挂在 `operations` 路由上,未套鉴权依赖(与第13章 admin 端点一致)。

### GET /v1/admin/dreaming/{run_id}

查询某次运行的结果(`operations.py` 第 61-66 行),同样挂在 operations 路由上,无鉴权依赖(原 `auth` 依赖已移除):

```python
@router.get("/v1/admin/dreaming/{run_id}")
def admin_dreaming_get(run_id: str):
    result = get_dreaming_run(run_id)
    if not result:
        raise HTTPException(404, "dreaming run not found")
    return result
```

`get_dreaming_run()`(`dreaming.py` 第 477-487 行)从 `dreaming_runs` 表读取一行并组装为 dict 返回,字段对应表中各列。

### MCP

Dreaming 通过 `dreaming_run` MCP 工具暴露给 agent,封装 `POST /v1/admin/dreaming` 的能力,使外部 agent 能以标准协议触发离线巩固。

## 配置

`DreamingCfg`(`config.py` 第 224-237 行)集中管理所有 dreaming 参数:

```python
class DreamingCfg(BaseModel):
    """Dreaming 离线巩固:两阶段 LLM(relation_detect + action_plan)+ 精确去重先行。"""
    enabled: bool = False              # 默认关,需配 LLM key 后开
    lookback_days: int = 7             # 候选发现时间窗
    max_scopes_per_run: int = 50       # 单次最多处理聚簇数
    min_cluster_size: int = Field(default=2, ge=2)          # <2 跳过 LLM
    similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    max_facts_per_cluster: int = Field(default=40, ge=2, le=500)
    max_input_chars: int = Field(default=30000, ge=1000, le=500000)
    max_issues_per_cluster: int = Field(default=10, ge=1, le=100)
    max_issues_per_run: int = Field(default=100, ge=1, le=10000)
    max_actions_per_issue: int = Field(default=20, ge=1, le=100)
    schedule_interval_hours: int = 24  # worker scheduler 触发间隔
    llm_tier: str = "synthesis"        # Dreaming LLM 用的 tier
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `False` | 全局开关。默认关,因为 Dreaming 依赖 LLM key,未配置时不应自动触发 |
| `lookback_days` | `7` | Phase A 候选发现的时间窗上界,只扫近 7 天入库的 fact |
| `max_scopes_per_run` | `50` | 单次 run 最多处理的聚簇数(`clusters[:max_clusters]`),控成本 |
| `min_cluster_size` | `2` | 聚簇最小成员数,`<2` 不送 LLM(单条 fact 无关系可言)。`ge=2` 约束 | 
| `similarity_threshold` | `0.85` | pg_trgm 预筛阈值。注意:**落在 `(0, 1]` 区间就会触发预筛**,默认 0.85 即启用;`0` 显式关闭 |
| `max_facts_per_cluster` | `40` | 单簇送入 LLM 的 fact 上限(硬上限,防超大簇超 token) |
| `max_input_chars` | `30000` | 单次 LLM 输入的最大字符数 |
| `max_issues_per_cluster` | `10` | 单簇最多产出的 issue 数 |
| `max_issues_per_run` | `100` | 单次 run 最多处理的 issue 总数 |
| `max_actions_per_issue` | `20` | 单 issue 最多规划的动作数 |
| `schedule_interval_hours` | `24` | worker scheduler 的检查间隔,每 24h 检查各 scope 是否该触发 dreaming |
| `llm_tier` | `"synthesis"` | Phase B/C 使用的 LLM tier,默认复用 synthesis tier |

`dream_run()` 的所有可选参数默认 `None`,意为回落到 `DreamingCfg`(`_dream_run_impl`,`dreaming.py` 第 70-75 行),操作员调参即时生效。唯一例外是 `min_age_hours`,函数内硬编码默认 `24`(用于候选发现的 fact 年龄下界),但 scheduler 入队的 dream job payload 显式传 `{"min_age_hours": 0}`,API 同步调用时不传该参数故用默认 24h 下界——这一差异是有意的:手动触发可以保守一点等 24h,定时触发则覆盖全部 live fact。

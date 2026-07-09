# 第23章 Dreaming 离线巩固 — 两阶段 LLM + 精确去重

## 概述

`dreaming.py` 实现 cortex-py 的**离线记忆巩固**(offline memory consolidation):在系统空闲期扫描同一主体下累积的事实,完成精确去重、冲突仲裁与高阶信息提炼,让长期运行的图谱不至于被重复/冲突/低价值 fact 污染而拖垮召回质量。

设计上借鉴了 MindMemOS 的**两阶段 LLM 分离**思路——把"发现问题"(relation_detect)与"决定动作"(action_plan)拆成两次独立的 LLM 调用,各自拥有专属 prompt 与约束,避免单次调用既要分类又要规划导致输出失控。在此基础上,cortex-py 做了三点关键适配:

1. **双时态软关**:不使用 `status='archived'` 这类状态标记,而是写 `recorded_to=now()` 把事实软关,保留历史可回溯(见 `facts.recorded_to`)。合并产生的新 fact 的 `valid_from` 取源 fact 中最早的一个,保证时间链不断裂。
2. **代码层 Subject 守卫**:聚簇与 LLM 输入的边界由 SQL `GROUP BY subject_id` 在代码层强制,不依赖 prompt 约束 LLM"只比较同一主体"——prompt 不可靠,代码可靠。
3. **精确去重先行**:Phase 0 复用 `consolidation_run`(纯 SQL、无 LLM),先清掉完全相同的重复项,再让昂贵的 LLM 只处理语义层面的疑似关系。

核心源码位于 `src/cortex/memory/dreaming.py`(~380 行),配置在 `src/cortex/infra/config.py` 的 `DreamingCfg`,调度与心跳在 `src/cortex/interfaces/worker/runner.py`。

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

整个管线由 `dream_run()` 编排(`dreaming.py` 第 31-127 行)。每个阶段的结果都汇总进 `summary["phases"]`,最终写入 `dreaming_runs` 表。

## Phase 0: 精确去重

Phase 0 直接复用第 22 章介绍的 `consolidation_run`,但有一个关键参数差异:

```python
# dreaming.py 第 63-70 行
if dry_run:
    p0 = {"action": "consolidation", "scope": scope, "facts_closed": 0,
          "groups": 0, "note": "dry_run skipped"}
else:
    p0 = consolidation_run(scope, min_age_hours=0)
```

`consolidation_run` 的默认签名是 `min_age_hours=24`(见 `maintenance.py` 第 56 行),意为常规后台合并只处理入库满 24 小时的事实,避免对刚抽取的事实操作。但 Dreaming 是**手动或定时触发**的离线任务,操作员主动调用时不希望再等 24 小时,因此这里显式传 `min_age_hours=0`,让精确去重覆盖所有 live fact。

`dry_run=True` 时 Phase 0 被跳过(不写库),仅做候选发现以供预览,随后直接返回(`dreaming.py` 第 66-76 行)。把 Phase 0 放在最前的价值在于:完全相同的重复项用 SQL 一次清掉成本极低,不应消耗 LLM token,只有 SQL 无法判定的语义关系才进入后续阶段。

## Phase A: 候选发现

Phase A 由 `_discover_clusters()` 实现(`dreaming.py` 第 130-169 行),纯 SQL 聚簇,无 LLM 参与。核心查询按 `subject_id` 分组,只取 live fact(`recorded_to IS NULL AND valid_to IS NULL`),并施加时间窗:

```sql
-- dreaming.py 第 142-154 行
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

当操作员显式把 `similarity_threshold` 设为 `>= 1.0` 时,Phase A 会额外调用 `_similarity_prefilter()`(`dreaming.py` 第 172-198 行),用 PostgreSQL 的 `pg_trgm` 扩展做三元组相似度预筛:

```python
# dreaming.py 第 165-168 行
if similarity_threshold and similarity_threshold >= 1.0:
    facts = _similarity_prefilter(conn, scope, facts, similarity_threshold)
```

```python
# dreaming.py 第 190-195 行
sim = conn.execute(text("SELECT similarity(:a, :b)"),
                   {"a": ta, "b": tb}).scalar()
if sim is not None and sim >= threshold:
    keep.add(fa["fact_id"])
    keep.add(fb_["fact_id"])
    break
```

注意默认配置 `similarity_threshold=0.85` **不会**触发预筛——阈值 `>= 1.0` 才启用。这是一个有意为之的保守默认:簇内可能包含互补但文本相似度较低的 fact(如"温度过高"与"散热风扇停转"),LLM 需要看到全貌才能判定 `complementary`。若默认开启预筛,这类互补对会被提前剔除,损害巩固效果。预筛仅供操作员在 token 成本敏感、且能接受丢失部分互补对时显式开启。

聚簇完成后,`_load_facts_detail()`(`dreaming.py` 第 201-218 行)加载每条 fact 的详情(predicate、object_text、effective_time、assertion_status、salience、access_count),供后续 LLM 阶段使用。其中 `effective_time` 已在 SQL 里预计算为 `coalesce(valid_from, extracted_at)`,供 Phase C 的 latest-wins 仲裁直接取用。

## Phase B+C: 两阶段 LLM

对每个聚簇,Phase B 与 Phase C 串行执行两次 LLM 调用。这种分离设计的核心收益是**职责单一、约束可校验**:Phase B 只负责分类,输出是受控的 7 选 1 枚举;Phase C 只负责规划,输入是已分类的 issue + 相关 fact,输出是受约束的 5 种动作。两次调用各自可以独立做异常处理与降级。

```python
# dreaming.py 第 88-102 行
for cluster in clusters[:max_clusters]:
    if len(cluster["facts"]) < min_cluster_size:
        continue
    issues = _relation_detect(cluster["facts"])        # Phase B
    total_issues += len(issues)
    for issue in issues:
        actions = _action_plan(cluster["facts"], issue)  # Phase C
        if not dry_run:
            _execute_actions(scope, actions, cluster["facts"])
        total_actions += len(actions)
```

### Phase B: 关系检测(LLM #1)

`_relation_detect()`(`dreaming.py` 第 221-236 行)把整簇 fact 序列化为 JSON 喂给 LLM,prompt 为 `DREAMING_RELATION_DETECT`(`prompts.py` 第 546-580 行)。该 prompt 定义了 **7 种 issue_type**:

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

LLM 返回的 JSON 经 `services.parse_llm_json()` 解析,取 `issues` 数组;解析或调用异常时返回空列表(降级,不阻断后续簇处理),见 `dreaming.py` 第 230-236 行的 try/except 包裹。

### Phase C: 行动规划(LLM #2)

`_action_plan()`(`dreaming.py` 第 239-259 行)接收 Phase B 产出的单个 issue 及其 `fact_ids` 对应的 fact 详情,调用 `DREAMING_ACTION_PLAN` prompt(`prompts.py` 第 587-626 行)规划具体动作。该 prompt 定义 **5 种动作**:

1. **archive**:软关(`recorded_to=now()`)。适用 duplicate 冗余项、low_value 事实。
2. **merge**:合并多条为新 fact 并归档所有源 fact,新 fact 的 `valid_from` 取最早源 fact。
3. **create**:从多条 fact 提炼高阶结论,**不归档**源 fact。
4. **update_quality**:调整 `salience`,不归档。适用 ambiguous 或不确定时,只降权不删。
5. **link**:在 fact 间建立 supports 关联。

prompt 中两条最重要的规则:

- **latest-wins**:对 `conflict`,保留 `effective_time` 最新的 fact,归档旧的。`effective_time` 优先级 `valid_from > extracted_at`,已在 Phase A 的 SQL 中预计算。
- **False archival is worse than leaving both active**:不确定时宁可不操作。这条规则贯穿整个 Dreaming 的保守哲学——错误归档会丢失信息且难以恢复,而保留两条活跃 fact 的代价只是召回时多一份噪声。

对 `new_salience`,prompt 明确约束 **必须在 `[0, 2]` 范围内**(0=完全降权,1=默认,2=最高)。即便 LLM 输出越界,代码层也会 clamp(见下一节)。对 `effective_time` 相同的冲突,prompt 要求双方都保留,最多标 `ambiguous` 降权,不归档任何一方。

## _execute_actions — SAVEPOINT 隔离

`_execute_actions()`(`dreaming.py` 第 262-292 行)是 LLM 输出落地为数据库变更的唯一入口。其核心设计是:**每个 action 用 SAVEPOINT(嵌套事务)隔离**。

```python
# dreaming.py 第 269-292 行
with session_scope() as conn:
    for act in actions:
        sp = conn.begin_nested()  # SAVEPOINT
        try:
            atype = act.get("action")
            fids = act.get("fact_ids", [])
            if not fids:
                continue
            if atype == "archive":
                conn.execute(text("UPDATE facts SET recorded_to=now() "
                                  "WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s"),
                             {"ids": "{" + ",".join(fids) + "}", "s": scope})
            elif atype == "merge":
                _do_merge(conn, scope, fids, act.get("merged_object_value"), fact_map)
            elif atype == "create":
                _do_create(conn, scope, fids, act.get("merged_object_value"), fact_map)
            elif atype == "update_quality":
                _do_update_quality(conn, scope, fids, act.get("new_salience", 0.5))
            sp.commit()
        except Exception as e:  # noqa: BLE001  单个 action 失败:回滚到 SAVEPOINT,继续下一个
            try:
                sp.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.warning("dreaming action failed (action=%s), skipped: %s", act.get("action"), e)
```

`conn.begin_nested()` 在 PostgreSQL 上对应 `SAVEPOINT`,`sp.commit()` 释放保存点,`sp.rollback()` 回滚到保存点而不影响外层事务。这样设计的目的是:**单个坏 LLM 输出(如不合法的 fact_id、越界的 salience、格式错误的 merged_object_value)只回滚自身,不拖垮整轮 run,也不影响其他 action 已提交的写入**。Dreaming 一次可能处理数十个 action,若没有 SAVEPOINT 隔离,一个 action 失败就会让整轮 run 中途夭折,已执行的 archive/merge 全部回滚,浪费大量 LLM token。

### 各动作的执行细节

- **archive**:`UPDATE facts SET recorded_to=now()`。纯软关,不删除任何行,历史可回溯。
- **merge**(`_do_merge`,第 295-325 行):取源 fact 中最早的 `valid_from` 作为新 fact 的 `valid_from`,`supports` 取所有源 fact supports 的并集(`array_agg(DISTINCT e.event_id)`),插入新 fact 后归档所有源 fact。新 fact 的 `extraction_model` 标记为 `'dreaming-merge'`,`evidence_span` 记录来源 fact_id 列表。
- **create**(`_do_create`,第 328-345 行):与 merge 类似但**不归档源 fact**,`supports` 直接指向源 fact_id,`extraction_model='dreaming-create'`,用于高阶结论提炼。
- **update_quality**(`_do_update_quality`,第 348-356 行):**salience clamp** 是这里的关键防御:

```python
# dreaming.py 第 350-354 行
try:
    sal = max(0.0, min(2.0, float(new_salience)))
except (TypeError, ValueError):
    log.warning("dreaming update_quality: unparseable new_salience=%r, skipped", new_salience)
    return
```

`max(0.0, min(2.0, float(new_salience)))` 把 LLM 输出强制 clamp 到 `[0, 2]`,防止违反 `facts` 表的 CHECK 约束(`salience_floor=0.1`、`salience_ceiling=2.0`,见第 21 章 FeedbackCfg)。若 `new_salience` 根本无法解析为 float(如 LLM 返回了字符串 `"high"`),则跳过该 action 而非抛异常。

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
        D2["_DreamHeartbeat 启动<br/>每 60s 刷 locked_at"]
        D3["dream_run"]
        D4["advisory lock<br/>pg_try_advisory_xact_lock"]
        D1 --> D2 --> D3 --> D4
    end
    S4 --> D1
```

### advisory lock

`dream_run()` 入口处用 PostgreSQL 事务级 advisory lock 序列化同一 scope 的并发 run(`dreaming.py` 第 53-56 行):

```python
# dreaming.py 第 53-56 行
with session_scope() as conn:
    locked = conn.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:s))"),
                          {"s": scope}).scalar()
    if not locked:
        return {"scope": scope, "skipped": "another dream run in progress for this scope"}
```

`pg_try_advisory_xact_lock` 是非阻塞的:拿不到锁立即返回 false 而非等待。锁的生命周期绑定当前事务,`session_scope` 提交后自动释放。`hashtext(scope)` 把 scope 字符串映射为 32 位整数作为 lock key。这是最后一道防线——即使 scheduler 的 H2 去重因竞态漏过,advisory lock 也能阻止两个 worker 同时跑同一 scope 的 dream。

### worker scheduler

`_maybe_schedule_dreaming()`(`runner.py` 第 43-82 行)是嵌入 worker 主循环的轻量调度器,每次 reaper 周期触发一次检查:

```python
# runner.py 第 51-53 行
interval = cfg.dreaming.schedule_interval_hours * 3600
if now - last_check < interval:
    return last_check
```

它扫描所有有 live fact 的 scope,查 `dreaming_runs` 表中该 scope 最后一次 `status='completed'` 的时间,若超过 `schedule_interval_hours`(默认 24h)则触发。触发前有 **H2 去重守卫**:

```python
# runner.py 第 70-75 行
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

`_DreamHeartbeat`(`runner.py` 第 20-40 行)是为此设计的后台心跳线程:

```python
# runner.py 第 24-36 行
stop = threading.Event()

def _beat():
    while not stop.wait(interval):   # interval=60s
        try:
            with session_scope() as c:
                c.execute(text("UPDATE jobs SET locked_at=now() "
                               "WHERE job_id=CAST(:j AS uuid) AND status='running'"),
                          {"j": job_id})
        except Exception:  # noqa: BLE001  心跳失败不影响主流程
            pass

t = threading.Thread(target=_beat, daemon=True)
t.start()
```

心跳每 60 秒刷新 `jobs.locked_at`,让 reaper 始终看到该 job"最近被访问过",不会误判为僵尸。心跳线程是 daemon,`dream_run` 返回或抛异常时 `stop.set()` 停止心跳。dispatch 分支用 `with _DreamHeartbeat(...)` 包裹 `dream_run`:

```python
# runner.py 第 138-144 行
if jt == "dream" and scope:
    from ...memory.dreaming import dream_run
    payload = job.get("payload") or {}
    with _DreamHeartbeat(job["job_id"]):
        return dream_run(scope, **payload)
```

## dreaming_runs 表

每次 `dream_run` 在入口创建一条 `dreaming_runs` 记录,结束时回写统计。DDL 位于 `schema.sql` 第 417-428 行:

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
| `status` | `running` → `completed` / `failed`。异常时(`dreaming.py` 第 118-127 行)回写 `failed` 并存 `summary.error` |
| `phase0_closed` | Phase 0 精确去重关闭的 fact 数 |
| `phase_a_clusters` | Phase A 发现的候选聚簇数 |
| `phase_b_issues` | Phase B LLM 发现的 issue 总数 |
| `phase_c_actions` | Phase C 执行的动作总数 |
| `summary` | 完整 `summary` dict 的 JSON,含各阶段明细 |

`_complete_run()`(`dreaming.py` 第 359-375 行)在写完统计后还做两件事:失效该 scope 的 `recall_packs` 缓存(`DELETE FROM recall_packs WHERE scope=:s`),确保下次召回能看到 dreaming 期间的 archive/merge 改动;并 `emit_lifecycle(kind="dreamed")` 发出生命周期事件。失败分支同样会失效 `recall_packs`。

## 无 LLM key 降级

Dreaming 的 LLM 阶段依赖 `synthesis` tier(默认)。若该 tier 未配置 API key,`dream_run` 会优雅降级——只运行 Phase 0,跳过 Phase B/C:

```python
# dreaming.py 第 82-86 行
if not services.llm_configured(cfg.dreaming.llm_tier):
    summary["note"] = f"LLM tier '{cfg.dreaming.llm_tier}' not configured, skipping Phase B/C"
    _complete_run(run_id, scope, p0["facts_closed"], len(clusters), 0, 0, summary)
    return summary
```

此时 Dreaming 等价于一次**增强版 consolidation**:Phase 0 精确去重照常执行(`min_age_hours=0`),Phase A 候选发现照常运行(但不送 LLM),`phase_b_issues` 和 `phase_c_actions` 记为 0。`summary.note` 字段记录降级原因,供运维排查。

这一设计意味着 `DreamingCfg.enabled=true` 但未配 LLM key 的部署不会报错,只是巩固能力退化为纯精确去重——对初期试运行或成本敏感场景很友好。

## API

### POST /v1/admin/dreaming

触发一次 dreaming 巩固,需 admin 权限。请求体 `DreamingRequest`(`schemas.py` 第 343-346 行):

```python
class DreamingRequest(BaseModel):
    scope: str
    dry_run: bool = False
    async_enqueue: bool = False
```

实现位于 `app.py` 第 814-821 行:

```python
@app.post("/v1/admin/dreaming")
def admin_dreaming(body: schemas.DreamingRequest, actor: str = Depends(admin_auth)):
    from ...memory.dreaming import dream_run, get_dreaming_run
    if body.async_enqueue:
        jid = enqueue_job(job_type="dream", scope=body.scope,
                          payload={"dry_run": body.dry_run}, priority=-1)
        return {"status": "queued", "job_id": jid, "scope": body.scope}
    res = dream_run(body.scope, dry_run=body.dry_run)
    return res
```

`async_enqueue=True` 时入队 dream job 立即返回 `job_id`,由 worker 异步执行(会触发前述的 heartbeat 机制);`False` 时同步阻塞执行,直接返回 `dream_run` 的 `summary`。`dry_run=True` 仅发现候选不执行任何写操作,用于预览巩固范围。该端点受 `admin_auth` 保护,普通用户无法触发。

### GET /v1/admin/dreaming/{run_id}

查询某次运行的结果(`app.py` 第 824-830 行),受普通 `auth` 保护(非 admin 也可查):

```python
@app.get("/v1/admin/dreaming/{run_id}")
def admin_dreaming_get(run_id: str, actor: str = Depends(auth)):
    from ...memory.dreaming import get_dreaming_run
    r = get_dreaming_run(run_id)
    if not r:
        raise HTTPException(404, "dreaming run not found")
    return r
```

`get_dreaming_run()`(`dreaming.py` 第 378-388 行)从 `dreaming_runs` 表读取一行并组装为 dict 返回,字段对应表中各列。

### MCP

Dreaming 通过 `dreaming_run` MCP 工具暴露给 agent,封装 `POST /v1/admin/dreaming` 的能力,使外部 agent 能以标准协议触发离线巩固。

## 配置

`DreamingCfg`(`config.py` 第 135-143 行)集中管理所有 dreaming 参数:

```python
class DreamingCfg(BaseModel):
    """Dreaming 离线巩固:两阶段 LLM(relation_detect + action_plan)+ 精确去重先行。"""
    enabled: bool = False              # 默认关,需配 LLM key 后开
    lookback_days: int = 7             # 候选发现时间窗
    max_scopes_per_run: int = 50       # 单次最多处理聚簇数
    min_cluster_size: int = 2          # <2 跳过 LLM
    similarity_threshold: float = 0.85  # LLM 前的 SQL 预筛相似度
    schedule_interval_hours: int = 24  # worker scheduler 触发间隔
    llm_tier: str = "synthesis"        # Dreaming LLM 用的 tier
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `False` | 全局开关。默认关,因为 Dreaming 依赖 LLM key,未配置时不应自动触发 |
| `lookback_days` | `7` | Phase A 候选发现的时间窗上界,只扫近 7 天入库的 fact |
| `max_scopes_per_run` | `50` | 单次 run 最多处理的聚簇数(`clusters[:max_clusters]`),控成本 |
| `min_cluster_size` | `2` | 聚簇最小成员数,`<2` 不送 LLM(单条 fact 无关系可言) |
| `similarity_threshold` | `0.85` | pg_trgm 预筛阈值。注意:**仅当 `>= 1.0` 才启用预筛**,默认 0.85 不预筛 |
| `schedule_interval_hours` | `24` | worker scheduler 的检查间隔,每 24h 检查各 scope 是否该触发 dreaming |
| `llm_tier` | `"synthesis"` | Phase B/C 使用的 LLM tier,默认复用 synthesis tier |

`dream_run()` 的所有可选参数默认 `None`,意为回落到 `DreamingCfg`(`dreaming.py` 第 43-48 行),操作员调参即时生效。唯一例外是 `min_age_hours`,函数内硬编码默认 `24`(用于候选发现的 fact 年龄下界),但 scheduler 入队的 dream job payload 显式传 `{"min_age_hours": 0}`,API 同步调用时不传该参数故用默认 24h 下界——这一差异是有意的:手动触发可以保守一点等 24h,定时触发则覆盖全部 live fact。

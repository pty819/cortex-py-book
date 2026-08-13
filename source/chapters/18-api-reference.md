# 第18章 API 参考

本章是 cortex-py HTTP API 的完整参考,直接对应 `src/cortex/interfaces/api/app.py` 中注册的全部路由(不含 MCP 工具)。仅作为速查手册,具体用法与示例请查阅对应专题章节。

## 1. 概述

所有端点统一前缀 `/v1`,由 `FastAPI(title="cortex")` 暴露。

### 鉴权

cortex 自身**不实现访问控制**——认证与授权由上游应用(承载本服务的网关 / 反向代理)负责。API 层不校验 token、不解析 principal,`ApiCfg` 仅保留 `cors_origins`(跨域来源)一个字段。因此下文所有路由表的 `Auth` 列已省略;若表格中出现 `auth`/`admin` 字样,均为旧版残留,应视为「由上游决定」。

> 同理,MCP 的 streamable-http 模式也是开放访问(`upstream controls authorization`),如需隔离请在上游网关层做鉴权。

### 双时态与 scope

- 所有读写都带 `scope`(强制过滤在查询层)。
- `as_of` / `valid_from`/`valid_to` / `recorded_from`/`recorded_to` 双时态语义见第6章。
- POST 体字段名取自 `src/cortex/interfaces/api/schemas.py` 的 Pydantic 模型。

### 统一约定

- 幂等:写端点通过 `idempotency_key` 去重,冲突返回 `409`。
- 长时任务:`lifecycle_stream` 返回 SSE 地址供前端追踪抽取进度。
- 抽取不在写路径跑:写入只入 WAL + 入队 `extract` job,异步完成。

---

## 2. 核心写入

| Endpoint | Method | Description | 关键 body/query 字段 |
|----------|--------|-------------|----------------------|
| `/v1/experience` | POST | 单条 event 写入(唯一同步写路径) | `scope`, `modality`, `content{kind,text,role,...}`, `context{observed_at,labels,intent,preceded_by}`, `observed_actor`, `subject`, `directives`, `idempotency_key` |
| `/v1/experience/bulk` | POST | 批量写入(可指定排序策略) | `scope`, `items[]`(同上), `ordering=strict_temporal|batch_throughput` |
| `/v1/ingest/document` | POST | 长文档按标题切块入库,每块一条 experience | `scope`, `text`, `intent=structure|diagnosis|general`, `min_chars=200`, `max_chars=2000` |
| `/v1/forget` | POST | 软遗忘(`recorded_to=now()`);`cascade=redact_events` 会抹除关联 events 内容 | `scope`, `layers=[facts,beliefs]`, `predicate`, `about_entity`, `cascade=derived_only|redact_events`, `confirm_all` |

返回 `ExperienceResponse`/`ImportResponse`/`ForgetResponse`(见第13节)。

---

## 3. 核心读取

| Endpoint | Method | Description | 关键 body/query 字段 |
|----------|--------|-------------|----------------------|
| `/v1/recall` | POST | 6 通道混合检索,返回 `StratifiedPack` | `scope`, `query`, `view=local|holistic|descend|structured`, `top_k`, `as_of`, `include_superseded`, `recorded_during{from,to}`, `budgets`, `citation_mode`, `exclude_content`, `temporal{natural,reference_date}` |
| `/v1/answer` | POST | 检索 + LLM 回答(可复用 pack) | `scope`, `query`, `use_pack_id` |

---

## 4. 流式端点 (SSE)

所有 SSE 端点返回 `text/event-stream`,使用 `sse_starlette.EventSourceResponse`。cortex 不做鉴权,由上游网关负责。

### 4.1 `/v1/lifecycle/stream` — GET

生命周期事件流,用于追踪抽取进度。`event_id` 或 `scope` 任选其一传入。

| 事件类型 | 说明 |
|----------|------|
| `lifecycle` | 单条生命周期事件(`kind=captured|extracted|indexed|consolidated|failed|forgotten`) |
| `done` | 终止事件(`event_id` 模式下,达到 `indexed`/`failed` 后 emit) |

**Query**: `event_id`, `scope`。

### 4.2 `/v1/recall/stream` — POST

`StratifiedPack` 逐层推送,先同步算 pack 再按层 emit。body 同 `/v1/recall`。

| 事件类型 | 说明 |
|----------|------|
| `plan` | 检索规划:`{scope, channels}` |
| `layer` | 层数据:`{layer: facts|beliefs|events, items: [...]}` |
| `context_block` | 拼好的上下文文本 |
| `provenance` | 溯源 trail + citations |
| `diagnostics` | `{time_ms, channels}` |
| `done` | `{pack_id}` |

### 4.3 `/v1/answer/stream` — GET

流式回答,LLM 边生成边推。`<think>` 段走 `reasoning`,正文走 `answer`。

| 事件类型 | 说明 |
|----------|------|
| `phase` | 阶段事件:`phase=recall_done|llm_start|llm_end`,携带 `pack_id`/`model`/`time_ms` |
| `reasoning` | 思维链文本段 |
| `answer` | 正文文本段 |
| `done` | `{model_used, pack_id, citations[]}` |
| `error` | `{message}` |

**Query**: `scope`, `query`, `use_pack_id`。

---

## 5. 层直读与图谱编辑

| Endpoint | Method | Description | 关键 query/body 字段 |
|----------|--------|-------------|---------------------|
| `/v1/entities` | GET | 列出实体(`merged_into` 为空) | `scope`, `q`, `limit`(≤0 表示全量) |
| `/v1/facts` | GET | 列出 facts,支持双时态裁剪 | `scope`, `subject`, `predicate`, `as_of`, `include_superseded`, `limit` |
| `/v1/facts/timeline` | GET | 某 (subject,predicate) 的超替版本链 | `scope`, `subject`, `predicate` |
| `/v1/beliefs` | GET | 列出当前有效 beliefs(上限 50) | `scope`, `about` |
| `/v1/beliefs/why` | GET | belief → facts → events 支持图 + LLM 生成 narrative | `belief_id` |
| `/v1/beliefs/build` | POST | 手动触发某 scope 的 belief 聚合 | `{scope}` |

### 5.1 批量入库（结构化数据直灌）

面向结构化第三方数据的高吞吐入口,单条失败不影响其他条目,逐条返回 `status=created|existing|failed`。

| Endpoint | Method | Description | 关键 body 字段 |
|----------|--------|-------------|----------------|
| `/v1/entities/batch` | POST | 批量 entity 入库:生成 embedding → 幂等 upsert。同名同 type 返回既有 `entity_id`(不报错) | `scope`, `entities[]{name, type, description}` |
| `/v1/facts/batch` | POST | 批量 fact 入库:校验 subject/object 存在且同 scope → 幂等插入。默认 `knowledge_tier='verified'`、`assertion_status='confirmed'`、`confidence=1.0`、`valid_from=now()` | `scope`, `facts[]{subject_id, predicate, object_entity_id, confidence, evidence_span}` |

> 幂等语义（结构边收敛）:`/v1/facts/batch` 对每条 fact 调用 `lock_and_find_structural_fact`,按 `scope+subject+predicate+object+极性`（忽略大小写 / 时间窗）去重;若已存在则返回既有 `fact_id`(`status='existing'`),不新开历史。**诊断 / 因果边保留事件历史**,不参与该结构去重。`/v1/facts/batch` 还会校验谓词是否在 scope 的谓词库 / 谓词定义中注册,未注册谓词整体返回 `422` 并列出 `rejected_predicates`。

### 5.2 手工图谱编辑（governed graph）

受治理的图谱编辑端点,全部走 `graph_mutations` 的审计路径(`audit_log` 记录 `actor/endpoint/action`)。

| Endpoint | Method | Description | 关键 body/query 字段 |
|----------|--------|-------------|---------------------|
| `/v1/entities` | POST | 创建实体 | `scope`, `name`, `type`, `description`, `identity_context` |
| `/v1/entities/{entity_id}` | PATCH | 更新实体字段 | path, `name`/`type`/`description`/`identity_context` |
| `/v1/entities/{entity_id}` | DELETE | 删除实体(可选级联) | path, `cascade`, `note` |
| `/v1/facts` | POST | 创建事实三元组 | `scope`, `subject_id`, `predicate`, `object_type=entity|literal`, `object_entity_id`/`object_value`, `polarity`, `confidence`, `valid_from`/`valid_to`, `assertion_status`, `knowledge_tier`, `note` |
| `/v1/facts/{fact_id}` | PATCH | 更新事实(双时态:旧版本 `recorded_to=now()` 关闭,新版本插入) | path, 可编辑字段同上 |
| `/v1/facts/{fact_id}` | DELETE | 删除事实(软关 `recorded_to`) | path, `note` |

> `POST /v1/facts` 复用结构边:若该结构三元组已存在,`create_fact` 直接返回既有 fact(`audit action='reuse'`)而不新开历史。`PATCH /v1/facts/{fact_id}` 若会把某条 fact 改成与另一条形成结构冲突,返回 `409 GraphConflict`(结构三元组已存在)。

---

## 6. 记忆自演化

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/feedback` | POST | 提交对某 fact/belief/event 的反馈信号 | `scope`, `target_layer=fact|belief|event`, `target_id`, `signal_type=relevant|irrelevant|wrong|partial`, `signal_durable=task_temporary|scenario_specific|long_term`, `reason`, `pack_id`, `idempotency_key` |
| `/v1/feedback` | GET | 列出反馈(按 scope,可按 target_id 过滤) | `scope`, `target_id`, `limit` |
| `/v1/admin/dreaming` | POST | 触发 dreaming 离线巩固(可同步跑或入队) | `scope`, `dry_run`, `async_enqueue`(true 返回 `{status:queued, job_id}`) |
| `/v1/admin/dreaming/{run_id}` | GET | 查询某次 dreaming 运行结果 | path: `run_id` |
| `/v1/admin/higher-order` | POST | 触发高阶归纳;或 `seed_predicates=true` 预置谓词定义 | `{scope, entity_id, seed_predicates}` |
| `/v1/higher-order` | GET | 列出已生成的高阶 facts | `scope`, `entity_id`, `limit` |

---

## 7. 导入导出

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/import/jsonl` | POST | 从 JSONL 文本导入 | `scope`, `scope_template`(`{field}` 占位), `lines` |
| `/v1/import/mem0` | POST | 从 Mem0 导入 | `scope`, `scope_template`, `memories[]` |
| `/v1/import/zep` | POST | 从 Zep 导入(facts 三元组) | `scope`, `facts[]{subject,predicate,object,valid_from,valid_to,confidence}` |
| `/v1/import/letta` | POST | 从 Letta blocks 导入 | `scope`, `scope_template`, `blocks[]{label,text}` |
| `/v1/import/openai` | POST | 从 OpenAI Memory 导入 | `scope`, `scope_template`, `memories[]{id,content}` |
| `/v1/import/{import_id}` | GET | 查询导入任务状态 | path: `import_id` |
| `/v1/export` | POST | 导出整个 scope 为 JSONL(内联返回) | `scope`, `format=jsonl` |

返回 `ImportResponse`/`ImportStatus`/`ExportResponse`。

---

## 8. Erasures

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/erasures/preview` | POST | 干跑预览(不改库,返回 preview_id) | `scope`, `selector{memory_ids[],about_entity,predicate}` |
| `/v1/erasures/preview/{preview_id}/manifest` | GET | 取预览清单(过期返回 409) | path: `preview_id` |
| `/v1/erasures` | POST | 执行擦除(可用 `from_preview_id` 或现传 `selector`) | `scope`, `selector`, `from_preview_id` |
| `/v1/erasures/{erasure_id}` | GET | 查询擦除任务状态 | path: `erasure_id` |
| `/v1/erasures/{erasure_id}/cancel` | POST | 取消运行中的擦除 | path: `erasure_id` |

---

## 9. Episodes 与 Cases

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/episodes` | GET | 列出 episodes | `scope` |
| `/v1/episodes/build` | POST | 对某 scope 触发 episode 切分 | `{scope}` |
| `/v1/cases` | POST | 创建诊断 case | `scope`, `title`, `case_id`, `equipment`, `lot`, `recipe`, `metadata` |
| `/v1/cases` | GET | 列出 cases(可过滤) | `scope`, `status`, `equipment`, `limit` |
| `/v1/cases/{episode_id}` | GET | 获取单个 case | path: `episode_id` |
| `/v1/cases/{episode_id}` | PATCH | 更新 case 字段 | `title`, `phase`, `status`, `root_cause`, `resolution`, `equipment`, `lot`, `recipe`, `metadata` |
| `/v1/cases/{episode_id}/events` | POST | 把已有 event 挂到 case 上 | `{event_id}` |
| `/v1/cases/{episode_id}/workspace-graph` | GET | 返回 case 工作区图谱(facts/beliefs/events/关联证据),供前端 workspace 渲染 | path: `episode_id` |
| `/v1/cases/{episode_id}/promote` | POST | 把 case 推导出的 fact 提升为正式断言（`reviewer` 为可选 body 字段，缺省 `api`） | `fact_ids[]`, `reviewer`, `note` |
| `/v1/cases/search` | POST | 按 query 搜索 cases | `scope`, `query` |

`phase ∈ observation|scoping|investigation|correlation|root_cause|remediation|regression`;`status ∈ open|investigating|resolved|closed`。

### 9.1 诊断召回

诊断专用检索入口,接受资产/腔体/配方/批号/症状等结构化约束,内部走与通用 recall 相同的 6 通道融合但按 `goal` 裁剪结果:

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/diagnosis/recall` | POST | 诊断召回(按资产/症状/时间窗检索相关事实与机制) | `scope`, `query`, `asset`, `chamber`, `recipe`, `lot`, `time_from`, `time_to`, `symptoms[]`, `actions_taken[]`, `goal=history|mechanism|root_cause|next_test|full`, `applicability_mode=strict|allow_unknown`, `case_id`, `top_k` |

### 9.2 Evidence(外部证据)

外部证据目录:登记指向权威系统(记录 id / URI)的证据,再把它作为 `supports`/`refutes`/`context`/`causal_direction`/`applicability` 关系挂到某条 fact 上。payload 始终留在权威系统,本系统只存引用与质量元数据。

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/evidence` | POST | 登记外部证据引用 | `scope`, `reference{uri, source_record_id, hash, query, version, quality}` |
| `/v1/evidence/{evidence_id}` | GET | 取单个证据记录(scope 校验) | path: `evidence_id` |
| `/v1/evidence/{evidence_id}/claims` | POST | 把证据挂到某 fact 上(证据与 fact 必须同 scope) | `fact_id`, `role=supports|refutes|context|causal_direction|applicability`, `weight`, `span`, `quality` |

### 9.3 Diagnostic Playbooks（诊断剧本）

诊断剧本的 CRUD 与版本管理（详见第 7b 章）。每个 playbook 是一个 DAG，有多个不可变版本（draft/active/retired）。

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/diagnostic-playbooks` | POST | 创建 playbook + v1 版本 | `scope`, `name`, `description`, `nodes[]{key,type,title,condition,recommendation,priority}`, `edges[]{from,to,outcome,condition,priority}`, `entry_nodes[]`, `status`, `applicability{}` |
| `/v1/diagnostic-playbooks` | GET | 列出 playbooks | `scope`, `status`, `view=local\|descend`, `limit`, `offset` |
| `/v1/diagnostic-playbooks/{playbook_id}` | GET | 获取单个 playbook（默认 active 版本） | `version`(可选,指定版本号) |
| `/v1/diagnostic-playbooks/{playbook_id}` | PUT | 追加新版本（不可变），可切换状态 | `nodes[]`, `edges[]`, `entry_nodes[]`, `status`, `description`, `applicability{}` |
| `/v1/diagnostic-playbooks/{playbook_id}` | DELETE | 退役 playbook（软删除，历史版本保留） | `note` |
| `/v1/diagnostic-playbooks/{playbook_id}/export` | GET | 导出 playbook 为 JSON（可迁移） | `version`(可选) |
| `/v1/diagnostic-playbooks/import` | POST | 从 JSON 导入 playbook | `playbook{...}`, `scope` |

节点类型 ∈ {symptom, condition, test, action, recommendation, terminal}；边 outcome ∈ {matched, not_matched, unknown, always, default}。

### 9.4 Forward Reasoning（正向推理）

输入症状 + 观测数据，沿 playbook DAG 确定性遍历，返回下一步动作和推荐结论。

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/forward-reasoning/query` | POST | 执行正向推理 | `scope`, `symptoms[]`, `observations{}`, `context{}`, `view=local\|holistic\|descend`, `applicability_mode=strict\|allow_unknown`, `playbook_ids[]`(可选), `case_id`, `persist_run=true` |
| `/v1/forward-reasoning/runs/{run_id}` | GET | 查询推理 run 结果（完整 trace + next_actions + recommendations） | path: `run_id` |

若未指定 `playbook_ids`，自动选择所有 `active` 状态且适用性匹配的 playbooks 并行推理，合并结果。`persist_run=false` 时不落 `forward_reasoning_runs` 记录（纯试探）。返回 `{run_id, playbook_id, version, trace[], next_actions[], recommendations[], unresolved_inputs[]}`。

### 9.5 Sensor Resolve（传感器解析）

自然语言查询 → 向量检索匹配实体 → 沿结构谓词 BFS → 收集关联传感器。

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/sensors/resolve` | POST | 解析自然语言查询,返回关联传感器列表 | `scope`, `query` |

返回 `{query, parsed_items[], matched_entities[], sensors[]}`。BFS 沿 8 个结构谓词出边最多 5 跳，`entity_type='sensor'` 的节点为终止符。

---

## 10. Understanding 层

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/understanding` | GET | 列出 concepts(可按 topic 过滤) | `scope`, `topic`, `limit` |
| `/v1/understanding/coverage` | GET | 概念覆盖度统计 | `scope` |
| `/v1/understanding/{concept_id}` | GET | 取单个 concept | path: `concept_id` |
| `/v1/understanding/{concept_id}/related` | GET | 相邻 concepts(BFS) | path, `relation`, `depth=2`, `limit=20` |
| `/v1/understanding/synthesize` | POST | 同步合成某 scope 的 understanding | `{scope, topics}` |

---

## 11. Vocabularies 与 Temporal

### Vocabularies

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/vocabularies` | POST | 创建/更新词表(`ON CONFLICT` upsert) | `scope`, `name`, `kind=closed|open`, `values[]{canonical,aliases[]}` |
| `/v1/vocabularies` | GET | 列出 scope 内所有词表 | `scope` |
| `/v1/vocabularies/{name}` | GET | 取单个词表 | `scope`, path: `name` |
| `/v1/vocabularies/{name}` | PUT | 整体替换词表值(先删后插) | `scope`, `kind`, `values[]` |
| `/v1/vocabularies/{name}` | DELETE | 删除词表 | `scope`, path: `name` |

### Synonyms（同义词）

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/synonyms` | POST | 创建同义词组 | `scope`, `term`, `aliases[]`, `status`, `locale`, `domain`, `source` |
| `/v1/synonyms` | GET | 列出同义词组 | `scope`, `status` |
| `/v1/synonyms/import` | POST | 批量导入同义词 | `scope`, `items[]`, `default_status` |
| `/v1/synonyms/export` | GET | 导出同义词 | `scope` |
| `/v1/synonyms/{synonym_id}` | GET | 取单个同义词组 | path: `synonym_id` |
| `/v1/synonyms/{synonym_id}` | PUT | 更新同义词组(`aliases`/状态等) | path, `aliases[]`, `status`, `locale`, `domain`, `source` |
| `/v1/synonyms/{synonym_id}` | DELETE | 删除同义词组 | path: `synonym_id` |

### Temporal Phrases

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/temporal/phrases` | POST | 注册时间短语(自动 seed 默认值) | `name`, `expression`(ISO8601 dur..dur,如 `-P7D..P0D`), `anchor` |
| `/v1/temporal/phrases` | GET | 列出所有时间短语 | — |
| `/v1/temporal/phrases/{name}` | DELETE | 删除时间短语 | path: `name` |

---

## 12. Admin

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/admin/config` | GET | 读取运行配置(脱敏:`api_key`/`url` 替换为 `***`,加 `has_key`) | — |
| `/v1/admin/config` | POST | 白名单深合并修改配置;`persist=true` 写回 YAML | body=patch dict, query: `persist` |
| `/v1/admin/jobs` | GET | 任务队列明细(不返回 payload) | `scope`, `status`, `job_type`, `limit` |
| `/v1/admin/metrics` | GET | 存储指标(各表行数 + jobs 按 status 计数) | `scope` |
| `/v1/admin/version` | GET | cortex 版本 + schema 表数 | — |
| `/v1/admin/maintenance` | POST | 维护操作(取代旧 `maintenance/*` 三端点) | `action=methylation|consolidation|enrich`, `scope`, `older_than_days=30`, `async_enqueue` |
| `/v1/admin/retrieval/effective` | GET | 配置值 + 依赖就绪状态 + 预测生效态(每通道 configured/effective enabled、weight、top_k) | `profile` |
| `/v1/admin/retrieval/preview` | POST | 无副作用 Active-vs-Draft A/B 预览(不递增 retrieval_count、不写缓存) | body:`scope`/`query`/`variants[]` |
| `/v1/admin/evolution-candidates` | GET | 列出 Dreaming/Higher-Order 产出的演化候选(facts/谓词定义等,待人工审批) | `scope`, `status=pending`, `limit` |
| `/v1/admin/evolution-candidates/{candidate_id}/review` | POST | 审批单个候选（`approve`/`reject`，`reviewer` 为可选 body 字段，缺省 `api`） | `decision=approve|reject`, `reviewer`, `note` |

> `POST /v1/admin/dreaming` 与 `POST /v1/admin/higher-order` 见第6节(记忆自演化)。

### 12.1 `/v1/admin/retrieval/effective` — GET

返回当前(或指定 Profile)检索配置的**三层视图**:configured(配置值)、dependencies(依赖就绪态)、effective(预测生效态)。这是前端检索控制面板展示"配置值 vs 有效值 vs 依赖就绪"的依据。

```python
@app.get("/v1/admin/retrieval/effective")
def admin_retrieval_effective(profile: Optional[str] = Query(None)):
    tuning = resolve_retrieval_config(load_config().retrieval, profile=profile)
    rerank_cfg = resolve_rerank_config(load_config(), profile=profile)
    return {"profile": profile or load_config().retrieval.active_profile,
            **describe_retrieval_runtime(tuning, rerank_cfg)}
```

`describe_retrieval_runtime` 对每个通道算出 `configured_enabled`(配置里写的)与 `effective_enabled`(配置开关 AND 依赖就绪,如 vector/graph 依赖 embedding 服务)。这让 UI 能区分"我关了它"和"依赖没就绪所以它没生效":

```json
{
  "configured": { "channels": { "vector": {"enabled": true, "weight": 1.0} } },
  "dependencies": {
    "embedding":        {"ready": true,  "kind": "configured"},
    "synthesis_llm":    {"ready": true,  "kind": "configured"},
    "rerank_service":   {"ready": false, "kind": "configured"}
  },
  "effective": {
    "channels": {
      "vector": {"configured_enabled": true, "effective_enabled": true,
                 "dependency": "embedding", "dependency_ready": true, "weight": 1.0, "top_k": 40},
      "graph":  {"configured_enabled": true, "effective_enabled": true, "weight": 0.20, "top_k": 40}
    },
    "fusion_strategy": "weighted_rrf",
    "hyde":   {"configured_enabled": false, "effective_enabled": false},
    "rerank": {"configured_enabled": true,  "effective_enabled": false}
  }
}
```

### 12.2 `/v1/admin/retrieval/preview` — POST

无副作用的检索变体预览:对同一 `{scope, query}` 跑 1–4 个配置变体,比较它们的 fact 排名、每通道候选数、耗时。**关键:`track_usage=False`,不递增 `retrieval_count`、不写 `recall_packs` 缓存** —— 这是调参可反复执行的前提。

```python
class RetrievalPreviewVariant(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile: Optional[str] = None          # 用哪个命名 Profile
    overrides: Optional[Dict[str, Any]] = None    # 临时覆盖检索 tuning 字段
    rerank_overrides: Optional[Dict[str, Any]] = None  # 临时覆盖 rerank 字段

class RetrievalPreviewRequest(BaseModel):
    scope: str
    query: str = Field(min_length=1)
    view: Literal["local", "holistic", "descend"] = "local"
    top_k: int = Field(default=20, ge=1, le=200)
    variants: List[RetrievalPreviewVariant] = Field(min_length=1, max_length=4)
```

每个 variant 跑一次 `recall(...)`,返回 `fact_ids` + `diagnostics`(含每通道候选数 `channels` 与耗时 `channel_time_ms`)。第一个 variant 视为 baseline,后续变体与之对比排名差异。

```{note}
`overrides` 只接受 `RetrievalTuningCfg` 字段(不能改 `profiles`/`active_profile`),`rerank_overrides` 只接受 `RerankRuntimeCfg` 字段。未知字段返回 422。Profile 的 `rerank` 覆盖(若有)先生效,`rerank_overrides` 再叠加。
```

---

## 13. Scopes 与 Health

| Endpoint | Method | Description | 关键字段 |
|----------|--------|-------------|---------|
| `/v1/scopes/list` | GET | 列出 DB 内已注册 + 数据中出现的 scope(供前端下拉框) | `prefix`, `limit` |
| `/v1/health` | GET | 健康检查(DB/向量库等依赖连通性) | — |

---

## Pydantic Schemas

所有 schema 定义于 `src/cortex/interfaces/api/schemas.py`。

### 请求 Schema

| Schema | 用途 | 关键字段 |
|--------|------|---------|
| `Content` | event 内容信封 | `kind=message|text|json|blob_ref|triple`, `role`, `text`, `data`, `blob_id` |
| `Context` | event 上下文 | `observed_at`, `labels[]`, `intent`, `preceded_by[]` |
| `ExperienceRequest` | 单条写入 | `scope`, `modality`, `content`, `context`, `observed_actor`, `subject`, `directives`, `idempotency_key` |
| `BulkItem` | 批量写入的单项 | 同上,无 `scope` |
| `BulkExperienceRequest` | 批量写入 | `scope`, `items[]`, `ordering`, `directives` |
| `RecallRequest` | 检索请求 | `scope`, `query`, `view`, `include[]`, `top_k`, `as_of`, `include_superseded`, `recorded_during`, `budgets`, `citation_mode`, `exclude_content`, `temporal`, `retrieval_profile`, `retrieval_overrides`, `rerank_overrides` |
| `DiagnosisRecallRequest` | 诊断召回请求 | `scope`, `query`, `asset`, `chamber`, `recipe`, `lot`, `time_from`, `time_to`, `symptoms[]`, `actions_taken[]`, `goal`, `applicability_mode`, `case_id`, `top_k` |
| `AnswerRequest` | 问答请求 | `scope`, `query`, `use_pack_id` |
| `ForgetRequest` | 遗忘请求 | `scope`, `layers`, `predicate`, `about_entity`, `cascade`, `confirm_all` |
| `IngestDocumentRequest` | 文档切块入库 | `scope`, `text`, `intent`, `min_chars`, `max_chars` |
| `FeedbackRequest` | 反馈回灌 | `scope`, `target_layer`, `target_id`, `signal_type`, `signal_durable`, `reason`, `pack_id`, `idempotency_key` |
| `DreamingRequest` | 离线巩固 | `scope`, `dry_run`, `async_enqueue` |
| `ErasureSelector` | 擦除选择器 | `memory_ids[]`, `about_entity`, `predicate` |
| `ErasurePreviewRequest` | 擦除预览 | `scope`, `selector` |
| `ErasureExecuteRequest` | 擦除执行 | `scope`, `selector`, `from_preview_id` |
| `EvidenceRegisterRequest` | 外部证据登记 | `scope`, `reference{uri, source_record_id, hash, query, version, quality}` |
| `EvidenceAttachmentRequest` | 证据挂到 fact | `fact_id`, `role`, `weight`, `span`, `quality` |
| `CaseCreateRequest` | 创建 case | `scope`, `title`, `case_id`, `equipment`, `lot`, `recipe`, `metadata` |
| `CaseUpdateRequest` | 更新 case | `title`, `phase`, `status`, `root_cause`, `resolution`, `equipment`, `lot`, `recipe`, `metadata` |
| `CaseAddEventRequest` | case 加 event | `event_id` |
| `CasePromotionRequest` | case 推导断言提升 | `fact_ids[]`, `reviewer`, `note` |
| `CaseSearchRequest` | 搜索 case | `scope`, `query` |
| `VocabValueIn` | 词表单项 | `canonical`, `aliases[]` |
| `VocabCreateRequest` | 创建词表 | `scope`, `name`, `kind`, `values[]` |
| `VocabReplaceRequest` | 替换词表 | `scope`, `kind`, `values[]` |
| `TemporalPhraseRequest` | 时间短语 | `name`, `expression`, `anchor` |
| `MaintenanceRequest` | 维护操作 | `action`, `scope`, `older_than_days` |
| `EvolutionReviewRequest` | 演化候选审批 | `decision=approve|reject`, `reviewer`, `note` |
| `ForwardReasoningRequest` | 正向推理 | `scope`, `symptoms[]`, `observations{}`, `context{}`, `view`, `applicability_mode`, `playbook_ids[]`, `case_id`, `persist_run` |
| `EntityBatchRequest` | 批量实体入库 | `scope`, `entities[]{name, type, description}` |
| `FactBatchRequest` | 批量事实入库 | `scope`, `facts[]{subject_id, predicate, object_entity_id, confidence, evidence_span}` |
| `EntityCreateRequest` | 创建实体 | `scope`, `canonical_name`, `entity_type`, `description`, `identity_context`, `note` |
| `EntityUpdateRequest` | 更新实体 | `canonical_name`, `entity_type`, `description`, `identity_context`, `note` |
| `FactCreateRequest` | 创建事实 | `scope`, `subject_id`, `predicate`, `object_type=entity|literal`, `object_entity_id`/`object_value`, `confidence`, `polarity`, `assertion_status`, `knowledge_tier`, `valid_from`/`valid_to`, `evidence_span`, `operating_regime`, `case_id`, `note` |
| `FactUpdateRequest` | 更新事实 | 可编辑字段同 `FactCreateRequest`(均可选) |
| `SensorResolveRequest` | 传感器解析 | `scope`, `query` |
| `ImportJsonlRequest` | JSONL 导入 | `scope`, `scope_template`, `lines` |
| `ImportMem0Request` | Mem0 导入 | `scope`, `scope_template`, `memories[]` |
| `ImportZepRequest` | Zep 导入 | `scope`, `facts[]` |
| `ImportLettaRequest` | Letta 导入 | `scope`, `scope_template`, `blocks[]` |
| `ImportOpenAIRequest` | OpenAI 导入 | `scope`, `scope_template`, `memories[]` |
| `ExportRequest` | 导出 | `scope`, `format` |

### 响应 Schema

| Schema | 用途 |
|--------|------|
| `ExperienceResponse` | 写入响应(`event_id`, `wal_offset`, `status`, `lifecycle_stream`) |
| `StratifiedPack` | 检索响应(`pack_id`, `layers{events,facts,beliefs}`, `context_block`, `provenance`, `diagnostics`) |
| `AnswerResponse` | 问答响应(`answer`, `citations[]`, `model_used`, `pack_id`) |
| `Citation` | 单条引用(`marker`, `layer`, `id`) |
| `TimelineVersion` / `TimelineResponse` | 双时态版本链 |
| `ForgetResponse` | 遗忘响应(`deleted{facts,beliefs}`, `audit_id`) |
| `ImportResponse` | 导入响应(`import_id`, `source`, `accepted`, `failed`, `lifecycle_stream`) |
| `ImportStatus` | 导入状态(`status`, `accepted`, `failed`, `total`) |
| `ExportResponse` | 导出响应(`export_id`, `bytes`, `data`) |
| `EntityOut` | 实体输出 |
| `FactOut` | 事实输出(`subject{Ref}`, `predicate`, `object{datatype,value}`, `confidence`, `valid_from`, `valid_to`, `supports[]`) |
| `BeliefOut` | 信念输出(`about{Ref}`, `stance`, `claim`, `confidence`, `confidence_interval[]`, `supports[]`) |
| `EventOut` | 事件输出 |
| `Ref` | 轻量引用(`id`, `name`) |
| `EntityBatchResponse` | 批量实体响应(`scope`, `created`, `existing`, `failed`, `results[]{entity_id,status}`) |
| `FactBatchResponse` | 批量事实响应(`scope`, `created`, `existing`, `failed`, `results[]{fact_id,status}`) |
| `SensorResolveResponse` | 传感器解析响应(`query`, `parsed_items[]`, `matched_entities[]`, `sensors[]`) |

---

## 变更说明(相对旧版章节)

删除的失效端点:`GET /v1/context`、`GET /v1/timeline`(改为 `/v1/facts/timeline`)、所有 `/v1/layers/*` 路由、`/v1/erasures/execute`+`/v1/erasures/status`(改为 `/v1/erasures`+`/v1/erasures/{id}`)、`/v1/vocab`(改为 `/v1/vocabularies`)、`/v1/temporal`(改为 `/v1/temporal/phrases`)、`/v1/maintenance/*` 三端点(合并为 `POST /v1/admin/maintenance`)、`/v1/lifecycle`(改为 `/v1/lifecycle/stream`)、`GET /v1/experience/{id}`。

新增端点:`POST/GET /v1/feedback`、`POST /v1/admin/dreaming`+`GET /v1/admin/dreaming/{run_id}`、`POST /v1/admin/higher-order`+`GET /v1/higher-order`、`GET/POST /v1/admin/config`、`GET /v1/admin/jobs`、`GET /v1/admin/version`、`POST /v1/recall/stream`、`GET /v1/answer/stream`、`GET /v1/beliefs/why`+`POST /v1/beliefs/build`、`POST /v1/cases/{episode_id}/events`、`GET /v1/import/{import_id}`、`GET /v1/health`(无鉴权)、`GET /v1/admin/retrieval/effective`+`POST /v1/admin/retrieval/preview`(检索控制面:有效态预览 + 无副作用 A/B)、`POST /v1/evidence`+`GET /v1/evidence/{id}`+`POST /v1/evidence/{id}/claims`(外部证据目录)、`POST /v1/diagnosis/recall`(诊断召回)、`GET /v1/cases/{episode_id}/workspace-graph`+`POST /v1/cases/{episode_id}/promote`(case 工作区 + 断言提升)、`GET /v1/admin/evolution-candidates`+`POST /v1/admin/evolution-candidates/{id}/review`(演化审批门)、`POST/GET/PUT/DELETE /v1/diagnostic-playbooks` + import/export(诊断剧本 CRUD)、`POST /v1/forward-reasoning/query` + `GET /v1/forward-reasoning/runs/{run_id}`(正向推理)、`POST /v1/sensors/resolve`(传感器解析)、`POST /v1/synonyms` + synonym CRUD + import(同义词管理)、`POST /v1/ingest/document`(文档切块入库)。

最新一轮新增(结构化批量入库 + 手工图谱编辑):`POST /v1/entities/batch`+`POST /v1/facts/batch`(批量实体/事实入库,幂等 upsert,结构边收敛)、`POST /v1/entities`+`PATCH/DELETE /v1/entities/{entity_id}`+`POST /v1/facts`+`PATCH/DELETE /v1/facts/{fact_id}`(governed graph 编辑,走审计路径)。

> 端点总数：约 **90+** 个（含 REST + 流式），分布在 `app.py`（核心读写/检索）+ 10 个路由文件中。按功能域 13 节分类索引。精确数量以 FastAPI `/openapi.json` 为准。

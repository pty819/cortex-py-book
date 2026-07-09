# 第18章 API 参考

本章是 cortex-py HTTP API 的完整参考,直接对应 `src/cortex/interfaces/api/app.py` 中注册的全部路由(不含 MCP 工具)。仅作为速查手册,具体用法与示例请查阅对应专题章节。

## 1. 概述

所有端点统一前缀 `/v1`,由 `FastAPI(title="cortex")` 暴露。

### 鉴权

| 级别 | 要求 | 说明 |
|------|------|------|
| 无鉴权 | 无 | 仅 `GET /v1/health` |
| 普通鉴权 (`auth`) | `Authorization: Bearer <api.key>` + 可选 `X-Cortex-Actor` | 若配置 `api.key` 非空则校验;`X-Cortex-Actor` 默认 `user:alice` |
| 管理员鉴权 (`admin_auth`) | 普通 auth + `X-Cortex-Admin-Key` | 若配置了 `api.admin_key`,该头必须匹配;未配置则退化为普通 auth |

> 开发态 `api.key=""` 时不做 token 校验,便于 `EventSource`(无法发自定义头)直连 SSE 端点。

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

| Endpoint | Method | Auth | Description | 关键 body/query 字段 |
|----------|--------|------|-------------|----------------------|
| `/v1/experience` | POST | auth | 单条 event 写入(唯一同步写路径) | `scope`, `modality`, `content{kind,text,role,...}`, `context{observed_at,labels,intent,preceded_by}`, `observed_actor`, `subject`, `directives`, `idempotency_key` |
| `/v1/experience/bulk` | POST | auth | 批量写入(可指定排序策略) | `scope`, `items[]`(同上), `ordering=strict_temporal|batch_throughput` |
| `/v1/ingest/document` | POST | auth | 长文档按标题切块入库,每块一条 experience | `scope`, `text`, `intent=structure|diagnosis|general`, `min_chars=200`, `max_chars=2000` |
| `/v1/forget` | POST | auth | 软遗忘(`recorded_to=now()`);`cascade=redact_events` 会抹除关联 events 内容 | `scope`, `layers=[facts,beliefs]`, `predicate`, `about_entity`, `cascade=derived_only|redact_events`, `confirm_all` |

返回 `ExperienceResponse`/`ImportResponse`/`ForgetResponse`(见第13节)。

---

## 3. 核心读取

| Endpoint | Method | Auth | Description | 关键 body/query 字段 |
|----------|--------|------|-------------|----------------------|
| `/v1/recall` | POST | auth | 6 通道混合检索,返回 `StratifiedPack` | `scope`, `query`, `view=local|holistic|descend|structured`, `top_k`, `as_of`, `include_superseded`, `recorded_during{from,to}`, `budgets`, `citation_mode`, `exclude_content`, `temporal{natural,reference_date}` |
| `/v1/answer` | POST | auth | 检索 + LLM 回答(可复用 pack) | `scope`, `query`, `use_pack_id` |

---

## 4. 流式端点 (SSE)

所有 SSE 端点返回 `text/event-stream`,使用 `sse_starlette.EventSourceResponse`。鉴权同 `auth`(开发态 `api.key=""` 以便 `EventSource` 直连)。

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

## 5. 层直读

| Endpoint | Method | Auth | Description | 关键 query 字段 |
|----------|--------|------|-------------|-----------------|
| `/v1/entities` | GET | auth | 列出实体(`merged_into` 为空) | `scope`, `q`, `limit`(≤0 表示全量) |
| `/v1/facts` | GET | auth | 列出 facts,支持双时态裁剪 | `scope`, `subject`, `predicate`, `as_of`, `include_superseded`, `limit` |
| `/v1/facts/timeline` | GET | auth | 某 (subject,predicate) 的超替版本链 | `scope`, `subject`, `predicate` |
| `/v1/beliefs` | GET | auth | 列出当前有效 beliefs(上限 50) | `scope`, `about` |
| `/v1/beliefs/why` | GET | auth | belief → facts → events 支持图 + LLM 生成 narrative | `belief_id` |
| `/v1/beliefs/build` | POST | auth | 手动触发某 scope 的 belief 聚合 | `{scope}` |

---

## 6. 记忆自演化

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/feedback` | POST | auth | 提交对某 fact/belief/event 的反馈信号 | `scope`, `target_layer=fact|belief|event`, `target_id`, `signal_type=relevant|irrelevant|wrong|partial`, `signal_durable=task_temporary|scenario_specific|long_term`, `reason`, `pack_id`, `idempotency_key` |
| `/v1/feedback` | GET | auth | 列出反馈(按 scope,可按 target_id 过滤) | `scope`, `target_id`, `limit` |
| `/v1/admin/dreaming` | POST | admin | 触发 dreaming 离线巩固(可同步跑或入队) | `scope`, `dry_run`, `async_enqueue`(true 返回 `{status:queued, job_id}`) |
| `/v1/admin/dreaming/{run_id}` | GET | auth | 查询某次 dreaming 运行结果 | path: `run_id` |
| `/v1/admin/higher-order` | POST | admin | 触发高阶归纳;或 `seed_predicates=true` 预置谓词定义 | `{scope, entity_id, seed_predicates}` |
| `/v1/higher-order` | GET | auth | 列出已生成的高阶 facts | `scope`, `entity_id`, `limit` |

---

## 7. 导入导出

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/import/jsonl` | POST | auth | 从 JSONL 文本导入 | `scope`, `scope_template`(`{field}` 占位), `lines` |
| `/v1/import/mem0` | POST | auth | 从 Mem0 导入 | `scope`, `scope_template`, `memories[]` |
| `/v1/import/zep` | POST | auth | 从 Zep 导入(facts 三元组) | `scope`, `facts[]{subject,predicate,object,valid_from,valid_to,confidence}` |
| `/v1/import/letta` | POST | auth | 从 Letta blocks 导入 | `scope`, `scope_template`, `blocks[]{label,text}` |
| `/v1/import/openai` | POST | auth | 从 OpenAI Memory 导入 | `scope`, `scope_template`, `memories[]{id,content}` |
| `/v1/import/{import_id}` | GET | auth | 查询导入任务状态 | path: `import_id` |
| `/v1/export` | POST | auth | 导出整个 scope 为 JSONL(内联返回) | `scope`, `format=jsonl` |

返回 `ImportResponse`/`ImportStatus`/`ExportResponse`。

---

## 8. Erasures

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/erasures/preview` | POST | auth | 干跑预览(不改库,返回 preview_id) | `scope`, `selector{memory_ids[],about_entity,predicate}` |
| `/v1/erasures/preview/{preview_id}/manifest` | GET | auth | 取预览清单(过期返回 409) | path: `preview_id` |
| `/v1/erasures` | POST | auth | 执行擦除(可用 `from_preview_id` 或现传 `selector`) | `scope`, `selector`, `from_preview_id` |
| `/v1/erasures/{erasure_id}` | GET | auth | 查询擦除任务状态 | path: `erasure_id` |
| `/v1/erasures/{erasure_id}/cancel` | POST | auth | 取消运行中的擦除 | path: `erasure_id` |

---

## 9. Episodes 与 Cases

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/episodes` | GET | auth | 列出 episodes | `scope` |
| `/v1/episodes/build` | POST | auth | 对某 scope 触发 episode 切分 | `{scope}` |
| `/v1/cases` | POST | auth | 创建诊断 case | `scope`, `title`, `case_id`, `equipment`, `lot`, `recipe`, `metadata` |
| `/v1/cases` | GET | auth | 列出 cases(可过滤) | `scope`, `status`, `equipment`, `limit` |
| `/v1/cases/{episode_id}` | GET | auth | 获取单个 case | path: `episode_id` |
| `/v1/cases/{episode_id}` | PATCH | auth | 更新 case 字段 | `title`, `phase`, `status`, `root_cause`, `resolution`, `equipment`, `lot`, `recipe`, `metadata` |
| `/v1/cases/{episode_id}/events` | POST | auth | 把已有 event 挂到 case 上 | `{event_id}` |
| `/v1/cases/search` | POST | auth | 按 query 搜索 cases | `scope`, `query` |

`phase ∈ observation|scoping|investigation|correlation|root_cause|remediation|regression`;`status ∈ open|investigating|resolved|closed`。

---

## 10. Understanding 层

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/understanding` | GET | auth | 列出 concepts(可按 topic 过滤) | `scope`, `topic`, `limit` |
| `/v1/understanding/coverage` | GET | auth | 概念覆盖度统计 | `scope` |
| `/v1/understanding/{concept_id}` | GET | auth | 取单个 concept | path: `concept_id` |
| `/v1/understanding/{concept_id}/related` | GET | auth | 相邻 concepts(BFS) | path, `relation`, `depth=2`, `limit=20` |
| `/v1/understanding/synthesize` | POST | auth | 同步合成某 scope 的 understanding | `{scope, topics}` |

---

## 11. Vocabularies 与 Temporal

### Vocabularies

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/vocabularies` | POST | auth | 创建/更新词表(`ON CONFLICT` upsert) | `scope`, `name`, `kind=closed|open`, `values[]{canonical,aliases[]}` |
| `/v1/vocabularies` | GET | auth | 列出 scope 内所有词表 | `scope` |
| `/v1/vocabularies/{name}` | GET | auth | 取单个词表 | `scope`, path: `name` |
| `/v1/vocabularies/{name}` | PUT | auth | 整体替换词表值(先删后插) | `scope`, `kind`, `values[]` |
| `/v1/vocabularies/{name}` | DELETE | auth | 删除词表 | `scope`, path: `name` |

### Temporal Phrases

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/temporal/phrases` | POST | auth | 注册时间短语(自动 seed 默认值) | `name`, `expression`(ISO8601 dur..dur,如 `-P7D..P0D`), `anchor` |
| `/v1/temporal/phrases` | GET | auth | 列出所有时间短语 | — |
| `/v1/temporal/phrases/{name}` | DELETE | auth | 删除时间短语 | path: `name` |

---

## 12. Admin

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/admin/config` | GET | auth | 读取运行配置(脱敏:`api_key`/`url` 替换为 `***`,加 `has_key`) | — |
| `/v1/admin/config` | POST | admin | 白名单深合并修改配置;`persist=true` 写回 YAML | body=patch dict, query: `persist` |
| `/v1/admin/jobs` | GET | auth | 任务队列明细(不返回 payload) | `scope`, `status`, `job_type`, `limit` |
| `/v1/admin/metrics` | GET | auth | 存储指标(各表行数 + jobs 按 status 计数) | `scope` |
| `/v1/admin/version` | GET | auth | cortex 版本 + schema 表数 | — |
| `/v1/admin/maintenance` | POST | auth | 维护操作(取代旧 `maintenance/*` 三端点) | `action=methylation|consolidation`, `scope`, `older_than_days=30` |

> `POST /v1/admin/dreaming` 与 `POST /v1/admin/higher-order` 见第6节(记忆自演化)。

---

## 13. Scopes 与 Health

| Endpoint | Method | Auth | Description | 关键字段 |
|----------|--------|------|-------------|---------|
| `/v1/scopes/list` | GET | auth | 列出 DB 内已注册 + 数据中出现的 scope(供前端下拉框) | `prefix`, `limit` |
| `/v1/health` | GET | 无 | 健康检查(DB/向量库等依赖连通性) | — |

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
| `RecallRequest` | 检索请求 | `scope`, `query`, `view`, `top_k`, `as_of`, `include_superseded`, `recorded_during`, `budgets`, `citation_mode`, `exclude_content`, `temporal` |
| `AnswerRequest` | 问答请求 | `scope`, `query`, `use_pack_id` |
| `ForgetRequest` | 遗忘请求 | `scope`, `layers`, `predicate`, `about_entity`, `cascade`, `confirm_all` |
| `IngestDocumentRequest` | 文档切块入库 | `scope`, `text`, `intent`, `min_chars`, `max_chars` |
| `FeedbackRequest` | 反馈回灌 | `scope`, `target_layer`, `target_id`, `signal_type`, `signal_durable`, `reason`, `pack_id`, `idempotency_key` |
| `DreamingRequest` | 离线巩固 | `scope`, `dry_run`, `async_enqueue` |
| `ErasureSelector` | 擦除选择器 | `memory_ids[]`, `about_entity`, `predicate` |
| `ErasurePreviewRequest` | 擦除预览 | `scope`, `selector` |
| `ErasureExecuteRequest` | 擦除执行 | `scope`, `selector`, `from_preview_id` |
| `CaseCreateRequest` | 创建 case | `scope`, `title`, `case_id`, `equipment`, `lot`, `recipe`, `metadata` |
| `CaseUpdateRequest` | 更新 case | `title`, `phase`, `status`, `root_cause`, `resolution`, `equipment`, `lot`, `recipe`, `metadata` |
| `CaseAddEventRequest` | case 加 event | `event_id` |
| `CaseSearchRequest` | 搜索 case | `scope`, `query` |
| `VocabValueIn` | 词表单项 | `canonical`, `aliases[]` |
| `VocabCreateRequest` | 创建词表 | `scope`, `name`, `kind`, `values[]` |
| `VocabReplaceRequest` | 替换词表 | `scope`, `kind`, `values[]` |
| `TemporalPhraseRequest` | 时间短语 | `name`, `expression`, `anchor` |
| `MaintenanceRequest` | 维护操作 | `action`, `scope`, `older_than_days` |
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

---

## 变更说明(相对旧版章节)

删除的失效端点:`GET /v1/context`、`GET /v1/timeline`(改为 `/v1/facts/timeline`)、所有 `/v1/layers/*` 路由、`/v1/erasures/execute`+`/v1/erasures/status`(改为 `/v1/erasures`+`/v1/erasures/{id}`)、`/v1/vocab`(改为 `/v1/vocabularies`)、`/v1/temporal`(改为 `/v1/temporal/phrases`)、`/v1/maintenance/*` 三端点(合并为 `POST /v1/admin/maintenance`)、`/v1/lifecycle`(改为 `/v1/lifecycle/stream`)、`GET /v1/experience/{id}`。

新增端点:`POST/GET /v1/feedback`、`POST /v1/admin/dreaming`+`GET /v1/admin/dreaming/{run_id}`、`POST /v1/admin/higher-order`+`GET /v1/higher-order`、`GET/POST /v1/admin/config`、`GET /v1/admin/jobs`、`GET /v1/admin/version`、`POST /v1/recall/stream`、`GET /v1/answer/stream`、`GET /v1/beliefs/why`+`POST /v1/beliefs/build`、`POST /v1/cases/{episode_id}/events`、`GET /v1/import/{import_id}`、`GET /v1/health`(无鉴权)。

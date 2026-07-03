# 第14章 API 参考

## FastAPI 端点

所有端点前缀 `/v1`，需带 `Authorization: Bearer <key>`（如配置了 api.key）。

### 核心写入

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/experience` | POST | 写入单个 event（幂等） |
| `/v1/experience/bulk` | POST | 批量写入 |
| `/v1/experience/{event_id}/?wait=` | GET | 等待指定 stage |
| `/v1/forget` | POST | 软遗忘 |

### 核心读取

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/recall` | POST | 6 通道混合检索 |
| `/v1/answer` | POST | 检索 + LLM 回答 |
| `/v1/context` | GET | holistic 上下文 |
| `/v1/timeline` | GET | 双时态超替链 |

### 层直读

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/layers/events` | GET | 列出 events |
| `/v1/layers/events/{event_id}` | GET | 单个 event |
| `/v1/layers/entities` | GET | 列出实体 |
| `/v1/layers/entities/{entity_id}/edges` | GET | 实体的 facts |
| `/v1/layers/facts` | GET | 列出 facts |
| `/v1/layers/beliefs` | GET | 列出 beliefs |
| `/v1/layers/concepts` | GET | 列出 concepts |

### 导入导出

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/import/jsonl` | POST | 从 JSONL 导入 |
| `/v1/import/mem0` | POST | 从 Mem0 导入 |
| `/v1/import/letta` | POST | 从 Letta 导入 |
| `/v1/import/openai` | POST | 从 OpenAI 导入 |
| `/v1/import/zep` | POST | 从 Zep 导入 |
| `/v1/ingest/document` | POST | 文档切块入库 |
| `/v1/export` | POST | 导出 JSONL |

### Erasures

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/erasures/preview` | POST | 干跑预览 |
| `/v1/erasures/execute` | POST | 执行删除 |
| `/v1/erasures/status/{erasure_id}` | GET | 任务状态 |

### Case 管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/cases` | POST | 创建 case |
| `/v1/cases/{episode_id}` | PATCH | 更新 case |
| `/v1/cases/{episode_id}` | GET | 获取 case |
| `/v1/cases` | GET | 列出 cases |
| `/v1/cases/search` | POST | 搜索 cases |

### Vocab

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/vocab` | POST | 创建词表 |
| `/v1/vocab` | GET | 列出词表 |

### Temporal

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/temporal` | POST | 注册时间短语 |
| `/v1/temporal` | GET | 列出时间短语 |

### 维护

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/maintenance/methylation` | POST | 甲基化 |
| `/v1/maintenance/consolidation` | POST | 去重 |
| `/v1/maintenance/vocab/seed` | POST | 预置诊断词表 |

### Admin

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/health` | GET | 健康检查 |
| `/v1/scopes/list` | GET | 列出 scopes |
| `/v1/admin/metrics` | GET | 存储指标 |
| `/v1/lifecycle` | GET | 生命周期事件（SSE） |

## MCP 工具

共 28 个工具，全部通过 `cortex.interfaces.mcp_server` 注册。

### 核心记忆

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `health_check` | DB 连通性 + 行数 | - |
| `memory_store` | 存文本 + 同步抽取 | text, scope, modality |
| `memory_search` | 6 通道混合检索 | query, scope, view, top_k |
| `answer` | 检索 + LLM 回答 | query, scope |
| `get_context` | holistic 上下文 | scope, query |

### 查询

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `memory_list` | 列出 events | scope, limit |
| `memory_get` | 单个 event | event_id |
| `entity_list` | 列出实体 | scope, q, limit |
| `entity_edges` | 实体的 facts | entity_id, scope |
| `facts_timeline` | 双时态链 | subject, predicate, scope |
| `list_beliefs` | 列出 beliefs | scope, about |

### 写入

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `bulk_ingest` | 批量存文本 | texts[], scope, modality |
| `memory_forget` | 软遗忘 | predicate, about_entity, scope |

### Erasure

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `erasure_preview` | 干跑 | about_entity, predicate, scope |
| `erasure_execute` | 执行 | scope, from_preview_id |

### Case

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `case_create` | 创建 case | title, equipment, lot, recipe, scope |
| `case_update` | 更新 case | episode_id, phase, status, root_cause |
| `case_get` | 获取 case | episode_id |
| `case_list` | 列出 cases | status, equipment, scope |
| `case_search` | 搜索 cases | query, scope |

### Vocab

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `vocab_create` | 创建词表 | name, kind, values[], scope |
| `vocab_list` | 列出词表 | scope |

### Temporal

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `temporal_register` | 注册短语 | name, expression |
| `temporal_list` | 列出短语 | - |

### Admin

| 工具 | 说明 | 关键参数 |
|------|------|----------|
| `admin_metrics` | 存储指标 | scope |
| `export_scope` | 导出 JSONL | scope |
| `episodes_build` | 构建 episodes | scope |
| `episodes_list` | 列出 episodes | scope |

## Pydantic Schemas

### 请求 Schema

| Schema | 用途 |
|--------|------|
| `ExperienceRequest` | 单条写入 |
| `BulkExperienceRequest` | 批量写入 |
| `RecallRequest` | 检索请求 |
| `AnswerRequest` | 问答请求 |
| `ForgetRequest` | 遗忘请求 |
| `ErasurePreviewRequest` | 擦除预览 |
| `ErasureExecuteRequest` | 擦除执行 |
| `CaseCreateRequest` | 创建案例 |
| `CaseUpdateRequest` | 更新案例 |
| `CaseSearchRequest` | 搜索案例 |
| `VocabCreateRequest` | 创建词表 |
| `VocabReplaceRequest` | 替换词表 |
| `TemporalPhraseRequest` | 时间短语 |
| `IngestDocumentRequest` | 文档入库 |
| `ImportJsonlRequest` | JSONL 导入 |
| `ImportMem0Request` | Mem0 导入 |
| `ImportLettaRequest` | Letta 导入 |
| `ImportOpenAIRequest` | OpenAI 导入 |
| `ImportZepRequest` | Zep 导入 |
| `ExportRequest` | 导出请求 |
| `MaintenanceRequest` | 维护请求 |

### 响应 Schema

| Schema | 用途 |
|--------|------|
| `ExperienceResponse` | 写入响应 |
| `StratifiedPack` | 检索响应 |
| `AnswerResponse` | 问答响应 |
| `TimelineResponse` | 时间线响应 |
| `ForgetResponse` | 遗忘响应 |
| `ImportResponse` | 导入响应 |
| `ExportResponse` | 导出响应 |
| `EntityOut` | 实体输出 |
| `FactOut` | 事实输出 |
| `BeliefOut` | 信念输出 |
| `EventOut` | 事件输出 |

# 第14章 API 参考

## REST API 端点

### Experience API

**端点**: `POST /v1/experience`

记录一条经验到记忆系统。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "modality": "conversation",
    "content": {
        "kind": "message",
        "role": "user",
        "text": "Alice works at Acme Corp"
    },
    "context": {
        "observed_at": "2024-01-15T10:00:00Z",
        "labels": ["work", "introduction"]
    },
    "idempotency_key": "unique-key-123"
}

# 响应 (202 Accepted)
{
    "event_id": "uuid",
    "wal_offset": 42,
    "status": "accepted",
    "lifecycle_stream": "/v1/lifecycle/stream?event_id=uuid"
}
```

**幂等性**:
- 同 `idempotency_key` + 同 body → 返回既有 (200)
- 同 `idempotency_key` + 不同 body → 409 Conflict

### Recall API

**端点**: `POST /v1/recall`

从记忆中检索相关信息。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "query": "Alice 在哪工作?",
    "view": "local",
    "include": ["facts", "beliefs", "understanding"],
    "top_k": 40,
    "temporal": "last month",
    "citation_mode": "inline_with_markers"
}

# 响应
{
    "pack": {
        "facts": [...],
        "beliefs": [...],
        "understanding": [...]
    },
    "stats": {
        "total_results": 15,
        "channels_used": 6,
        "query_time_ms": 150
    }
}
```

**视图模式**:

| 视图 | 说明 |
|------|------|
| `local` | 只看当前 scope |
| `holistic` | 向上聚合所有父 scope |
| `descend` | 向下展开所有子 scope |

### Answer API

**端点**: `POST /v1/answer`

基于记忆回答问题。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "question": "Alice 什么时候加入 Acme 的?",
    "view": "holistic",
    "top_k": 20
}

# 响应
{
    "answer": "Alice 于 2023 年 6 月加入 Acme Corp。",
    "sources": [
        {"fact_id": "uuid", "summary": "Alice works_at Acme (2023-06)"},
        ...
    ],
    "confidence": 0.95
}
```

### Forget API

**端点**: `POST /v1/forget`

软删除记忆（标记为 excluded_from_recall）。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "memory_ids": ["event_id_1", "event_id_2"]
}

# 响应
{
    "forgotten": 2,
    "status": "ok"
}
```

### Erasures API

**端点**: `POST /v1/erasures/preview`

预览 GDPR 删除。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "selector": {
        "about_entity": "entity_id",
        "predicate": "works_at"
    }
}

# 响应
{
    "preview_id": "uuid",
    "total_events": 10,
    "to_delete": 7,
    "to_redact": 3,
    "manifest": {...}
}
```

**端点**: `POST /v1/erasures/execute`

执行 GDPR 删除。

```python
# 请求
{
    "scope": "org:acme/user:alice",
    "preview_id": "uuid"
}

# 响应
{
    "executed": 10,
    "deleted": 7,
    "redacted": 3
}
```

### Layer Read API

**端点**: `GET /v1/{layer}`

直接读取各层数据。

| 端点 | 说明 |
|------|------|
| `GET /v1/events?scope=...` | 读取 Events |
| `GET /v1/entities?scope=...` | 读取 Entities |
| `GET /v1/facts?scope=...` | 读取 Facts |
| `GET /v1/beliefs?scope=...` | 读取 Beliefs |
| `GET /v1/episodes?scope=...` | 读取 Episodes |
| `GET /v1/understanding?scope=...` | 读取 Understanding |

### Bulk API

**端点**: `POST /v1/bulk/experience`

批量写入。

```python
# 请求
{
    "items": [
        {"scope": "...", "content": {...}, "idempotency_key": "key1"},
        {"scope": "...", "content": {...}, "idempotency_key": "key2"}
    ]
}

# 响应
{
    "accepted": 2,
    "event_ids": ["uuid1", "uuid2"]
}
```

### Import API

**端点**: `POST /v1/import/{format}`

支持多种导入格式。

| 格式 | 说明 |
|------|------|
| `jsonl` | JSON Lines |
| `csv` | CSV |
| `markdown` | Markdown |
| `text` | 纯文本 |
| `conversation` | 对话格式 |

### Export API

**端点**: `GET /v1/export`

导出数据为 JSONL。

```python
# 响应 (JSONL 流)
{"event_id": "uuid1", "scope": "...", "content": {...}}
{"event_id": "uuid2", "scope": "...", "content": {...}}
```

### Lifecycle API

**端点**: `GET /v1/lifecycle/stream`

SSE 流，实时推送任务进度。

```
event: captured
data: {"event_id": "uuid", "scope": "..."}

event: extracting
data: {"event_id": "uuid", "scope": "..."}

event: extracted
data: {"event_id": "uuid", "scope": "...", "facts": 3, "entities": 2}
```

### Health API

**端点**: `GET /v1/health`

健康检查。

```json
{
    "ok": true,
    "version": "0.1.0",
    "database": "ok",
    "embedding": "ok",
    "rerank": "ok",
    "llm": "configured"
}
```

## MCP 工具

### experience

记录经验。

```python
{
    "name": "experience",
    "arguments": {
        "scope": "org:acme/user:alice",
        "modality": "conversation",
        "content": {"kind": "message", "text": "..."},
        "idempotency_key": "unique-key"
    }
}
```

### recall

检索记忆。

```python
{
    "name": "recall",
    "arguments": {
        "scope": "org:acme/user:alice",
        "query": "Alice 在哪工作?",
        "view": "local",
        "top_k": 40
    }
}
```

### answer

回答问题。

```python
{
    "name": "answer",
    "arguments": {
        "scope": "org:acme/user:alice",
        "question": "Alice 什么时候加入 Acme 的?"
    }
}
```

### list_entities

列出实体。

```python
{
    "name": "list_entities",
    "arguments": {
        "scope": "org:acme/user:alice",
        "limit": 100
    }
}
```

### search_entities

搜索实体。

```python
{
    "name": "search_entities",
    "arguments": {
        "scope": "org:acme/user:alice",
        "query": "Alice",
        "limit": 10
    }
}
```

### merge_entities

合并实体。

```python
{
    "name": "merge_entities",
    "arguments": {
        "source_id": "uuid1",
        "target_id": "uuid2"
    }
}
```

### add_alias

添加别名。

```python
{
    "name": "add_alias",
    "arguments": {
        "entity_id": "uuid",
        "alias": "Google"
    }
}
```

### register_phrase

注册时间短语。

```python
{
    "name": "register_phrase",
    "arguments": {
        "name": "recent",
        "expression": "-P7D..P0D"
    }
}
```

### get_stats

获取统计。

```python
# 响应
{
    "entities": 150,
    "facts": 420,
    "beliefs": 85,
    "events": 1000,
    "episodes": 45
}
```

## 错误码

| HTTP | 说明 |
|------|------|
| 200 | 成功 |
| 202 | 已接受 (异步处理) |
| 400 | 请求错误 |
| 404 | 未找到 |
| 409 | 冲突 (幂等键) |
| 500 | 服务器错误 |

## 认证

### API Key

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
     -X POST http://localhost:8000/v1/recall \
     -d '{"scope": "...", "query": "..."}'
```

### MCP

MCP 通过 scope 隔离，不需要额外认证。

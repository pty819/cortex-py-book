# 第17章 时态系统

## 概述

cortex 的时态系统支持自然语言时间短语解析，让检索可以按时间范围过滤。

## Temporal Phrases 表

```sql
CREATE TABLE temporal_phrases (
    phrase_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,           -- "last week"
    anchor       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expression   TEXT NOT NULL,                  -- "-P7D..P0D" (ISO 8601 duration)
    is_default   BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**expression 格式**：两个 ISO 8601 duration 以 `..` 分隔
- `-P7D..P0D`：7 天前到现在
- `-P30D..P0D`：30 天前到现在
- `-P1M..P0D`：1 个月前到现在

## 预置短语

`temporal.py:18-24` 的 `_DEFAULTS` 在 `seed_defaults()` 时写入（`is_default=true`）。注意短语名是**自然语言带空格**（不是 `recent_week` 这种标识符风格），注册和查询都按小写匹配（`register_phrase` / `delete_phrase` / `parse_temporal` 均对 name 做 `.lower()`）：

| 名称 | 表达式 | 含义 |
|------|--------|------|
| `last week` | `-P7D..P0D` | 最近 7 天 |
| `last month` | `-P30D..P0D` | 最近 30 天 |
| `yesterday` | `-P1D..P0D` | 昨天 |
| `last quarter` | `-P90D..P0D` | 最近 90 天 |
| `last year` | `-P365D..P0D` | 最近 365 天 |

## 注册自定义短语

```python
@mcp.tool()
def temporal_register(name, expression):
    """注册一个时间短语。expression = 'dur..dur' (e.g. -P7D..P0D)"""
    return temporal.register_phrase(name, expression)
```

```bash
# MCP 工具调用
temporal_register(name="this_quarter", expression="-P90D..P0D")
```

## 检索中的时态过滤

recall 请求支持 `temporal` 参数：

```python
class RecallRequest(BaseModel):
    query: str
    temporal: Optional[Dict] = None  # {natural: "last week", reference_date: "2026-06-01"}
```

系统将 NL 短语解析为 ISO 时间范围后，应用到所有检索通道的时态过滤中。

## REST API

除 MCP 工具外，时态短语也通过 REST 端点管理（**只有 `POST /v1/temporal/phrases` 会先调用 `temporal.seed_defaults()` 确保预置短语就位；`GET` / `DELETE` 不调，因此首次对空库 `GET` 可能返回空列表，直到有人先 POST 一条或显式调用 MCP `temporal_register`/`seed_defaults`**）：

| 端点 | 说明 |
|------|------|
| `POST /v1/temporal/phrases` | 注册一个时间短语，body: `{name, expression, anchor?}` |
| `GET /v1/temporal/phrases` | 列出所有已注册的时间短语 |
| `DELETE /v1/temporal/phrases/{name}` | 按名称删除时间短语 |

请求体 schema：

```python
class TemporalPhraseRequest(BaseModel):
    name: str
    expression: str    # dur..dur，例如 -P7D..P0D
    anchor: Optional[str] = None
```

```bash
# 注册自定义短语
curl -X POST /v1/temporal/phrases \
  -H "Content-Type: application/json" \
  -d '{"name":"this_quarter","expression":"-P90D..P0D"}'
# → {"phrase_id":"...","name":"this_quarter","expression":"-P90D..P0D"}

# 列出短语
curl /v1/temporal/phrases
# → {"items":[...]}

# 删除短语
curl -X DELETE /v1/temporal/phrases/this_quarter
# → {"deleted":"this_quarter"}
```

> 注意：MCP 工具 `temporal_register(name, expression)` 当前签名不含 `anchor`；REST 端点通过 `TemporalPhraseRequest` 额外接受可选 `anchor` 字段。

## MCP 工具

```bash
temporal_list()      → 列出所有已注册的时间短语
temporal_register()  → 注册新的时间短语
```

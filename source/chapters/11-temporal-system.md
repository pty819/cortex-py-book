# 第11章 时态系统

## 概述

cortex 的时态系统支持自然语言时间短语解析，让检索可以按时间范围过滤。

## Temporal Phrases 表

```sql
CREATE TABLE temporal_phrases (
    phrase_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,           -- "recent_week"
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

| 名称 | 表达式 | 含义 |
|------|--------|------|
| `recent_day` | `-P1D..P0D` | 最近 1 天 |
| `recent_week` | `-P7D..P0D` | 最近 7 天 |
| `recent_month` | `-P30D..P0D` | 最近 30 天 |
| `recent_quarter` | `-P90D..P0D` | 最近 90 天 |
| `recent_year` | `-P365D..P0D` | 最近 1 年 |

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
    temporal: Optional[Dict] = None  # {natural: "recent_week", reference_date: "2026-06-01"}
```

系统将 NL 短语解析为 ISO 时间范围后，应用到所有检索通道的时态过滤中。

## MCP 工具

```bash
temporal_list()      → 列出所有已注册的时间短语
temporal_register()  → 注册新的时间短语
```

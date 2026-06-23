# 第9章 MCP Server

## MCP 概述

MCP (Model Context Protocol) 是 Anthropic 推出的协议，用于 LLM 与外部工具交互。cortex 通过 MCP 暴露全部能力，支持两种传输模式。

```{mermaid}
graph TB
    subgraph "MCP Client"
        LLM[Agent / LLM]
    end
    
    subgraph "cortex MCP Server (23 工具)"
        T1[memory_store]
        T2[memory_search]
        T3[answer]
        T4[memory_list/get]
        T5[entity_list/entity_edges]
        T6[facts_timeline]
        T7[list_beliefs]
        T8[bulk_ingest]
        T9[memory_forget]
        T10[erasure_preview/execute]
        T11[episodes_build/list]
        T12[case_create/update/get/list/search]
        T13[vocab_create/list]
        T14[temporal_register/list]
        T15[health_check]
        T16[admin_metrics]
        T17[export_scope]
    end
    
    subgraph "Transport"
        STDIO[stdio - 本地单 agent]
        HTTP[streamable-http - 多人共享]
    end
    
    LLM --> STDIO
    LLM --> HTTP
    STDIO --> T1
    HTTP --> T2
```

## 23 工具一览

| 类别 | 工具 | 说明 |
|------|------|------|
| **核心** | `memory_store` | 存文本 + 同步抽取三元组 |
| | `memory_search` | 6 通道混合检索 |
| | `answer` | 检索 + LLM 回答 |
| | `get_context` | holistic 检索（含祖先 scope） |
| **查询** | `memory_list` | 列出 scope 的 events |
| | `memory_get` | 获取单个 event 详情 |
| | `entity_list` | 列出实体（图节点） |
| | `entity_edges` | 实体的所有 live facts |
| | `facts_timeline` | 双时态超替链 |
| | `list_beliefs` | 概率断言列表 |
| **写入** | `bulk_ingest` | 批量存文本 |
| | `memory_forget` | 软遗忘（close recorded_to） |
| **Erasure** | `erasure_preview` | GDPR 预演 |
| | `erasure_execute` | GDPR 执行 |
| **Case** | `case_create` | 创建诊断 case |
| | `case_update` | 更新 case 阶段/状态 |
| | `case_get` | 获取 case 详情 |
| | `case_list` | 列出 cases |
| | `case_search` | 搜索 cases |
| **Vocab** | `vocab_create` | 创建词表 |
| | `vocab_list` | 列出词表 |
| **Temporal** | `temporal_register` | 注册时间短语 |
| | `temporal_list` | 列出时间短语 |
| **Admin** | `health_check` | 健康检查 |
| | `admin_metrics` | 存储指标 |
| | `export_scope` | 导出 JSONL |

## 双传输模式

### stdio 模式

本地单 agent，每 agent 一个子进程：

```bash
uv run python -m cortex.cli mcp
```

```{mermaid}
sequenceDiagram
    participant C as MCP Client
    participant S as cortex-mcp (stdio)
    participant DB as PostgreSQL
    
    C->>S: 启动进程 (stdin/stdout)
    C->>S: JSON-RPC: tools/list
    S-->>C: [memory_store, memory_search, ...]
    
    C->>S: JSON-RPC: tools/call
    Note over C,S: name: "memory_search"
    Note over C,S: args: {query: "..."}
    
    S->>DB: 6通道检索
    DB-->>S: 结果
    S-->>C: StratifiedPack
```

**.mcp.json 注册**（给 Claude Code 自动发现）：

```json
{
  "mcpServers": {
    "cortex": {
      "command": "uv",
      "args": ["run", "--directory", ".", "python", "-m", "cortex.cli", "mcp"],
      "env": {
        "CORTEX_SCOPE": "org:acme/dept:sales/user:alice"
      }
    }
  }
}
```

### streamable-http 模式

多人共享，按 `X-Cortex-Scope` 请求头隔离：

```bash
uv run python -m cortex.cli mcp-http --port 8001
```

```{mermaid}
sequenceDiagram
    participant C1 as Agent A (Alice)
    participant C2 as Agent B (Bob)
    participant S as cortex-mcp (HTTP)
    participant DB as PostgreSQL
    
    C1->>S: POST /mcp (X-Cortex-Scope: equip:XXX-v1)
    C2->>S: POST /mcp (X-Cortex-Scope: equip:YYY-v2)
    
    S->>DB: 查询 scope=equip:XXX-v1
    S->>DB: 查询 scope=equip:YYY-v2
    
    DB-->>S: Alice 的数据
    DB-->>S: Bob 的数据
    
    S-->>C1: Alice 的结果
    S-->>C2: Bob 的结果
```

## Scope 解析规则

`_eff_scope` 函数决定每次调用的 scope：

```python
def _eff_scope(ctx, scope_arg):
    """显式 arg > HTTP 头 X-Cortex-Scope > 环境变量 CORTEX_SCOPE"""
    if scope_arg:
        return scope_arg
    try:
        req = ctx.request_context.request
        h = req.headers.get("x-cortex-scope")
        if h:
            return h
    except Exception:
        pass
    return DEFAULT_SCOPE  # "org:local/user:default"
```

## 关键工具详解

### memory_store

核心写入工具，存文本 + 同步抽取（存完立即可搜）：

```python
@mcp.tool()
def memory_store(text, scope=None, modality="conversation", ctx=None):
    sc = _eff_scope(ctx, scope)
    eid, off = append_event(scope=sc, modality=modality,
        content={"kind": "message", "role": "user", "text": text},
        context={}, caller="mcp",
        idempotency_key=f"mcp-{uuid.uuid4().hex[:16]}")
    res = extract_event(eid)  # 同步抽取！
    return {"event_id": eid, "wal_offset": off,
            "facts_extracted": res["facts_extracted"],
            "entities": res["entities"]}
```

**关键设计**：MCP 内同步抽取（不通过 worker 队列），保证存完立即可搜。

### memory_search

核心读取工具，6 通道混合检索：

```python
@mcp.tool()
def memory_search(query, scope=None, view="local", top_k=20, ctx=None):
    pack = recall(scope=_eff_scope(ctx, scope), query=query,
                  view=view, top_k=top_k)
    return {"pack_id": pack["pack_id"], "scope": pack["scope"],
            "facts": pack["layers"]["facts"],
            "beliefs": pack["layers"]["beliefs"],
            "context_block": pack["context_block"]}
```

### answer

检索 + LLM 回答（带 `[n]` 引用标记）：

```python
@mcp.tool()
def answer(query, scope=None, ctx=None):
    pack = recall(scope=sc, query=query)
    if llm_configured("answer"):
        raw = services.llm_chat("answer", prompt, json.dumps(pack))
        ans = services.strip_think(raw)
    else:
        ans = services.mock_answer(query, json.dumps(pack))
    return {"answer": ans, "model_used": model, "citations": [...]}
```

## Auth 中间件

streamable-http 模式下可选静态 key 鉴权：

```python
class _AuthASGI:
    def __init__(self, app, key):
        self.app = app
        self.key = (key or "").strip()
    
    async def __call__(self, scope, receive, send):
        if self.key and scope["type"] == "http":
            # 检查 Authorization: Bearer <key>
            ...
            if auth != f"Bearer {self.key}":
                return 401
        await self.app(scope, receive, send)
```

## 启动命令速查

```bash
# stdio（本地 agent）
uv run python -m cortex.cli mcp

# streamable-http（多人共享）
uv run python -m cortex.cli mcp-http --port 8001
# → http://host:8001/mcp

# 带 key 鉴权（config.api.key 非空时自动启用）
```

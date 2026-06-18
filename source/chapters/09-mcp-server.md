# 第9章 MCP Server

## MCP 概述

MCP (Model Context Protocol) 是 Anthropic 推出的协议，用于 LLM 与外部工具交互。

```{mermaid}
graph TB
    subgraph "MCP Client"
        LLM[LLM]
    end
    
    subgraph "MCP Server"
        T1[experience]
        T2[recall]
        T3[answer]
        T4[forget]
        T5[...]
    end
    
    subgraph "Transport"
        STDIO[stdio]
        HTTP[streamable-http]
    end
    
    LLM --> STDIO
    LLM --> HTTP
    STDIO --> T1
    STDIO --> T2
    HTTP --> T3
    HTTP --> T4
```

## 双传输模式

### stdio 模式

```{mermaid}
sequenceDiagram
    participant C as MCP Client (Claude Desktop)
    participant S as cortex-mcp (stdio)
    participant DB as PostgreSQL
    
    C->>S: 启动进程 (stdin/stdout)
    C->>S: JSON-RPC: tools/list
    S-->>C: [experience, recall, answer, ...]
    
    C->>S: JSON-RPC: tools/call
    Note over C,S: name: "recall"
    Note over C,S: arguments: {scope: "...", query: "..."}
    
    S->>DB: 查询
    DB-->>S: 结果
    S-->>C: JSON-RPC response
```

**特点**：
- 单用户
- 本地运行
- 简单直接

### streamable-http 模式

```{mermaid}
sequenceDiagram
    participant C1 as Client 1 (Alice)
    participant C2 as Client 2 (Bob)
    participant S as cortex-mcp (HTTP)
    participant DB as PostgreSQL
    
    C1->>S: POST /mcp (scope=alice)
    C2->>S: POST /mcp (scope=bob)
    
    S->>DB: 查询 scope=alice
    S->>DB: 查询 scope=bob
    
    DB-->>S: Alice 的数据
    DB-->>S: Bob 的数据
    
    S-->>C1: Alice 的结果
    S-->>C2: Bob 的结果
```

**特点**：
- 多用户共享
- 按 scope 隔离
- 支持远程访问

## 23 个 MCP 工具

### 工具清单

| 工具 | 类型 | 说明 |
|------|------|------|
| `experience` | 写入 | 记录经验 |
| `recall` | 读取 | 检索记忆 |
| `answer` | 读取 | 回答问题 |
| `forget` | 删除 | 遗忘记忆 |
| `erasures` | 删除 | GDPR 删除 |
| `list_entities` | 读取 | 列出实体 |
| `list_facts` | 读取 | 列出事实 |
| `list_beliefs` | 读取 | 列出信念 |
| `list_episodes` | 读取 | 列出情节 |
| `get_entity` | 读取 | 获取实体详情 |
| `get_fact` | 读取 | 获取事实详情 |
| `get_belief` | 读取 | 获取信念详情 |
| `search_entities` | 读取 | 搜索实体 |
| `search_facts` | 读取 | 搜索事实 |
| `add_alias` | 写入 | 添加别名 |
| `remove_alias` | 删除 | 移除别名 |
| `merge_entities` | 写入 | 合并实体 |
| `register_phrase` | 写入 | 注册时间短语 |
| `list_phrases` | 读取 | 列出时间短语 |
| `delete_phrase` | 删除 | 删除时间短语 |
| `get_stats` | 读取 | 获取统计 |
| `lifecycle_stream` | 订阅 | 生命周期流 |
| `health` | 读取 | 健康检查 |

## 实现架构

```python
# mcp_server.py
from fastmcp import FastMCP

mcp = FastMCP("cortex-py", 
              description="CortexDB 记忆系统")

@mcp.tool()
def experience(scope: str, modality: str, content: dict, 
               idempotency_key: str, ...):
    """记录一条经验"""
    from .core import append_event
    event_id, wal_offset = append_event(
        scope=scope, modality=modality, 
        content=content, idempotency_key=idempotency_key, ...
    )
    return {"event_id": event_id, "wal_offset": wal_offset}

@mcp.tool()
def recall(scope: str, query: str, view: str = "local", 
           top_k: int = 40, ...):
    """检索记忆"""
    from .retrieval.pipeline import recall as do_recall
    result = do_recall(scope, query, view, top_k, ...)
    return result

@mcp.tool()
def answer(scope: str, question: str, ...):
    """回答问题"""
    from .retrieval.pipeline import recall
    from .services import llm_chat
    
    # 1. 检索相关记忆
    context = recall(scope, question, ...)
    
    # 2. LLM 回答
    answer = llm_chat("answer", 
        "基于以下上下文回答问题...",
        f"上下文: {context}\n问题: {question}"
    )
    
    return {"answer": answer, "sources": context}
```

## scope 隔离

### HTTP 模式的 scope

```{mermaid}
flowchart TD
    A[请求] --> B{解析 scope}
    B --> C[scope = user:alice]
    C --> D[查询 WHERE scope = 'user:alice']
    D --> E[只返回 Alice 的数据]
```

### 实现

```python
# HTTP 传输时，从请求中提取 scope
@mcp.tool()
def recall(scope: str, query: str, ...):
    """scope 从请求参数传入"""
    # scope 由调用方提供
    # Server 不做鉴权，只做隔离
    return do_recall(scope, query, ...)
```

## FastMCP 配置

### stdio 模式启动

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### HTTP 模式启动

```python
if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000
    )
```

## CLI 集成

### 启动命令

```bash
# stdio 模式
uv run python -m cortex.mcp_server

# HTTP 模式
uv run python -m cortex.mcp_server --transport http --port 8000
```

### Claude Desktop 配置

```json
{
  "mcpServers": {
    "cortex": {
      "command": "uv",
      "args": ["run", "python", "-m", "cortex.mcp_server"],
      "cwd": "/path/to/cortex-py"
    }
  }
}
```

## 工具实现示例

### experience 工具

```python
@mcp.tool()
def experience(
    scope: str,
    modality: str = "conversation",
    content: dict = None,
    context: dict = None,
    observed_actor: str = None,
    subject: str = None,
    directives: dict = None,
    idempotency_key: str = None
) -> dict:
    """
    记录一条经验到记忆系统。
    
    Args:
        scope: 记忆作用域，如 "org:acme/user:alice"
        modality: 模态类型 (conversation/document/tool_result/...)
        content: 内容对象 {kind, role, text, data, blob_id}
        context: 上下文 {observed_at, labels, intent, preceded_by}
        observed_actor: 观察者 (默认=caller)
        subject: 主题 (默认=observed_actor)
        directives: 指令 {extract, consolidate_into, ...}
        idempotency_key: 幂等键 (必须唯一)
    
    Returns:
        {event_id, wal_offset, status}
    """
    from .core import append_event
    
    # 参数验证
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
    
    if content is None:
        content = {"kind": "message", "text": ""}
    
    # 调用核心写入
    event_id, wal_offset = append_event(
        scope=scope,
        modality=modality,
        content=content,
        context=context or {},
        caller="mcp",
        observed_actor=observed_actor,
        subject=subject,
        directives=directives,
        idempotency_key=idempotency_key
    )
    
    return {
        "event_id": event_id,
        "wal_offset": wal_offset,
        "status": "accepted"
    }
```

### recall 工具

```python
@mcp.tool()
def recall(
    scope: str,
    query: str = None,
    view: str = "local",
    include: list = None,
    top_k: int = 40,
    as_of: str = None,
    include_superseded: bool = False,
    temporal: str = None,
    budgets: dict = None,
    citation_mode: str = "inline_with_markers"
) -> dict:
    """
    从记忆中检索相关信息。
    
    Args:
        scope: 记忆作用域
        query: 查询文本
        view: 视图模式 (local/holistic/descend)
        include: 包含的层 [events, facts, beliefs, understanding]
        top_k: 返回数量
        as_of: 时间点 (ISO 8601)
        include_superseded: 是否包含被取代的
        temporal: 时间短语 (如 "last week")
        budgets: token 预算 {max_tokens, per_layer_limits}
        citation_mode: 引用模式
    
    Returns:
        {pack, stats}
    """
    from .retrieval.pipeline import recall as do_recall
    
    result = do_recall(
        scope=scope,
        query=query,
        view=view,
        include=include,
        top_k=top_k,
        as_of=as_of,
        include_superseded=include_superseded,
        temporal=temporal,
        budgets=budgets,
        citation_mode=citation_mode
    )
    
    return result
```

## 错误处理

```python
@mcp.tool()
def experience(...):
    try:
        # ... 正常逻辑
        return {"event_id": event_id, ...}
    except IdempotencyConflict as e:
        return {
            "error": "idempotency_conflict",
            "message": str(e)
        }
    except Exception as e:
        return {
            "error": "internal_error",
            "message": str(e)
        }
```

# 第19章 MCP Server

## MCP 概述

MCP (Model Context Protocol) 是 Anthropic 推出的协议，用于 LLM 与外部工具交互。cortex 通过 MCP 暴露全部能力，支持两种传输模式。

```{mermaid}
graph TB
    subgraph "MCP Client"
        LLM[Agent / LLM]
    end
    
    subgraph "cortex MCP Server (53 工具)"
        T1[memory_store/search/answer]
        T2[entity CRUD + edges]
        T3[fact CRUD + timeline]
        T4[bulk_ingest + forget]
        T5[erasure_preview/execute]
        T6[case CRUD + search]
        T7[vocab CRUD + synonym CRUD]
        T8[playbook CRUD + import/export]
        T9[forward_reason + run_get]
        T10[feedback + dreaming + higher-order]
        T11[temporal + admin + export]
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

## 53 工具一览

按功能域分组，共 **53 个工具**。

### 核心读写（6 个）

| 工具 | 说明 |
|------|------|
| `memory_store` | 存文本 + 同步抽取三元组 |
| `memory_search` | 6 通道混合检索 |
| `answer` | 检索 + LLM 回答 |
| `get_context` | holistic 检索（含祖先 scope） |
| `memory_list` | 列出 scope 的 events |
| `memory_get` | 获取单个 event 详情 |

### 实体与事实 CRUD（9 个）

| 工具 | 说明 |
|------|------|
| `entity_list` | 列出实体（图节点），支持 q 模糊搜索 |
| `entity_create` | 手动创建实体 |
| `entity_update` | 更新实体字段（name/type/description/identity_context） |
| `entity_delete` | 删除实体（可选级联） |
| `entity_edges` | 实体的所有 live facts（出边） |
| `fact_create` | 手动创建事实三元组 |
| `fact_update` | 更新事实（predicate/object/status 等） |
| `fact_delete` | 删除事实（软关 recorded_to） |
| `facts_timeline` | 双时态超替链（同 S/P 的历史版本） |

### 批量操作与擦除（5 个）

| 工具 | 说明 |
|------|------|
| `bulk_ingest` | 批量存文本 + 入队抽取 |
| `memory_forget` | 软遗忘（close recorded_to） |
| `erasure_preview` | GDPR 擦除预演（返回 to_delete/to_redact） |
| `erasure_execute` | GDPR 擦除执行（4 阶段引用计数真删） |
| `export_scope` | 导出 scope 数据为 JSONL |

### Episodes 与 Cases（8 个）

| 工具 | 说明 |
|------|------|
| `episodes_build` | 触发 episode 自动分段 |
| `episodes_list` | 列出 episodes |
| `case_create` | 创建诊断 case |
| `case_update` | 更新 case 阶段/状态/根因 |
| `case_get` | 获取 case 详情（含 workspace graph） |
| `case_list` | 列出 cases（可按 status/equipment 过滤） |
| `case_search` | 语义搜索 cases |
| `list_beliefs` | 列出 beliefs（可按 about_entity 过滤） |

### 词表与同义词（7 个）

| 工具 | 说明 |
|------|------|
| `vocab_list` | 列出词表 |
| `vocab_create` | 创建/更新词表（含 values） |
| `synonym_list` | 列出同义词组 |
| `synonym_create` | 创建同义词组 |
| `synonym_update` | 更新同义词组（aliases/status） |
| `synonym_delete` | 删除同义词组 |
| `synonym_import` | 批量导入同义词 |

### 诊断 Playbook 与推理（9 个）

| 工具 | 说明 |
|------|------|
| `diagnostic_playbook_create` | 创建诊断剧本（DAG + v1） |
| `diagnostic_playbook_list` | 列出 playbooks（可按 status/view 过滤） |
| `diagnostic_playbook_get` | 获取 playbook 详情（节点/边/版本） |
| `diagnostic_playbook_update` | 追加新版本（不可变）+ 切换状态 |
| `diagnostic_playbook_retire` | 退役 playbook（软删除） |
| `diagnostic_playbook_export` | 导出 playbook 为 JSON（可迁移） |
| `diagnostic_playbook_import` | 从 JSON 导入 playbook |
| `diagnostic_forward_reason` | 正向推理：输入症状 → next_actions + recommendations |
| `diagnostic_reasoning_run_get` | 查询推理 run 结果（完整 trace） |

### 时间与运维（6 个）

| 工具 | 说明 |
|------|------|
| `temporal_list` | 列出时间短语 |
| `temporal_register` | 注册时间短语 |
| `health_check` | 健康检查 |
| `admin_metrics` | 存储指标（各表行数 + jobs 状态） |
| `maintenance_enqueue` | 触发 maintenance 任务（methylation/consolidation） |
| `feedback_submit` | 提交反馈信号（正/负反馈，调 salience/usefulness） |

### 自演化（3 个）

| 工具 | 说明 |
|------|------|
| `feedback_list` | 查询反馈信号历史 |
| `dreaming_run` | 触发 Dreaming 离线巩固（dry_run 模式可预览） |
| `higher_order_generate` | 触发高阶归纳（指定 entity_id） |

> **注**：自演化的人工审批门（evolution_candidates 列表/审批）走 HTTP Admin API，未暴露为 MCP 工具——审批属于运维操作，不适合 LLM agent 直接调用。

## 双传输模式

### stdio 模式

本地单 agent，每 agent 一个子进程：

```bash
uv run python -m cortex.interfaces.cli mcp
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
      "args": ["run", "--directory", ".", "python", "-m", "cortex.interfaces.cli", "mcp"],
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
uv run python -m cortex.interfaces.cli mcp-http --port 8001
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
    return DEFAULT_SCOPE  # os.environ["CORTEX_SCOPE"] 或 "org:local"
```

## 关键工具详解

### memory_store

核心写入工具，存文本 + 同步抽取（存完立即可搜）：

```python
@mcp.tool()
def memory_store(text, scope=None, modality="conversation", ctx=None):
    if not llm_configured("extraction"):
        raise RuntimeError("memory_store requires a configured extraction LLM")
    sc = _eff_scope(ctx, scope)
    eid, off = append_event(scope=sc, modality=modality,
        content={"kind": "message", "role": "user", "text": text},
        context={}, caller="mcp",
        idempotency_key=f"mcp-{uuid.uuid4().hex[:16]}")
    res = extract_event(eid)  # 同步抽取！
    return {"event_id": eid, "wal_offset": off, "scope": sc,
            "facts_extracted": res["facts_extracted"],
            "entities": res["entities"], "model": res["model"]}
```

**关键设计**：MCP 内同步抽取（不通过 worker 队列），保证存完立即可搜。

### memory_search

核心读取工具，6 通道混合检索：

```python
@mcp.tool()
def memory_search(query, scope=None, view="local", top_k=20, ctx=None):
    sc = _eff_scope(ctx, scope)
    pack = recall(scope=sc, query=query, view=view, top_k=top_k)
    return {"pack_id": pack["pack_id"], "scope": sc,
            "channels": pack["diagnostics"].get("channels", {}),
            "facts": pack["layers"]["facts"],
            "beliefs": pack["layers"]["beliefs"],
            "context_block": pack["context_block"]}
```

### answer

检索 + LLM 回答（带 `[n]` 引用标记）：

```python
@mcp.tool()
def answer(query, scope=None, ctx=None):
    sc = _eff_scope(ctx, scope)
    pack = recall(scope=sc, query=query)
    if llm_configured("answer"):
        raw = services.llm_chat("answer", prompt, json.dumps(pack))
        ans = services.strip_think(raw)
    else:
        ans = services.mock_answer(query, json.dumps(pack))
    return {"answer": ans, "model_used": model, "citations": [...]}
```

## 访问控制

cortex 自身不做鉴权。streamable-http 模式是**开放访问**,docstring 明确 `upstream owns access control`(上游负责授权)。如需隔离,请在承载服务的网关 / 反向代理层做鉴权,例如校验 `Authorization: Bearer <key>`:cortex 只负责把 `X-Cortex-Scope` 当作调用方默认 scope 的请求头透传。

```python
def http_app():
    """Return the open streamable-http ASGI app; upstream owns access control."""
    return mcp.streamable_http_app()
```

## 启动命令速查

```bash
# stdio（本地 agent）
uv run python -m cortex.interfaces.cli mcp

# streamable-http（多人共享）
uv run python -m cortex.interfaces.cli mcp-http --port 8001
# → http://host:8001/mcp

# 鉴权由上游网关负责（cortex 自身开放访问）
```

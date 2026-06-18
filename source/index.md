# Cortex-PY 架构解析

> 一本深入解析 [cortex-py](https://github.com/pty819/cortex-py) 项目实现的技术书

```{toctree}
:maxdepth: 2
:caption: 目录

chapters/00-overview
chapters/01-memory-model
chapters/02-data-model
chapters/03-wal-and-events
chapters/04-extraction-pipeline
chapters/05-entity-linking
chapters/06-retrieval-system
chapters/07-retrieval-channels
chapters/08-rrf-fusion
chapters/09-mcp-server
chapters/10-worker-system
chapters/11-temporal-system
chapters/12-erasures
chapters/13-architecture-diagrams
chapters/14-api-reference
```

## 项目概览

**Cortex-PY** 是 [CortexDB](https://cortexdb.ai/docs/) 记忆系统的 Python 复刻实现，面向个人/小团队的 Agent 长期记忆层。

### 核心特性

| 特性 | 说明 |
|------|------|
| 五层记忆 | Events → Episodes → Facts → Beliefs → Understanding |
| 双时态 | 每条记录 4 个时间字段，支持"当时"和"现在"双视角 |
| 知识图谱 | Facts 当图边，递归 CTE 做 2-3 跳 BFS |
| 6 通道检索 | 向量 + BM25 + 图遍历 + entity-name + synonym + temporal-decay |
| 实体链接 | B over C 策略：向量召回 + 阈值 + LLM 灰区判定 |
| MCP | 23 个工具，stdio + streamable-http 双传输 |

### 技术栈

```{mermaid}
graph TB
    subgraph 应用层
        A[FastAPI] --> B[Vue 3 前端]
        A --> C[MCP Server]
    end
    
    subgraph 业务层
        D[Extraction Pipeline] --> E[Retrieval Pipeline]
        D --> F[Entity Linking]
        E --> G[RRF Fusion]
    end
    
    subgraph 存储层
        H[(PostgreSQL)] --> I[pgvector]
        H --> J[ltree]
        H --> K[pg_trgm]
    end
    
    A --> D
    E --> H
    D --> H
```

## 索引

- {ref}`genindex`
- {ref}`search`

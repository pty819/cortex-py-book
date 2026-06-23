# 第13章 架构图

## 系统整体架构

```{mermaid}
graph TB
    subgraph 用户层
        UI[Vue 3 前端]
        MCP[MCP Client/Agent]
        CLI[CLI 命令行]
    end
    
    subgraph API 层
        FA[FastAPI :8002]
        MCP_SRV[MCP Server<br/>stdio / streamable-http]
    end
    
    subgraph 业务层
        EXT[Extraction Pipeline<br/>LLM 抽取 + 实体链接]
        RET[Retrieval Pipeline<br/>6 通道 + RRF + rerank]
        WK[Worker Loop<br/>Postgres-as-queue]
        UNDER[Understanding<br/>概念合成]
        MAINT[Maintenance<br/>methylation + consolidation]
        CASE[Episodes / Case<br/>诊断案例管理]
        ERASE[Erasures<br/>GDPR 真删]
    end
    
    subgraph 存储层
        PG[(PostgreSQL)]
        VEC[pgvector HNSW]
        TS[tsvector / pg_trgm]
    end
    
    UI --> FA
    MCP --> MCP_SRV
    CLI --> FA
    
    FA --> EXT
    FA --> RET
    FA --> CASE
    FA --> ERASE
    MCP_SRV --> EXT
    MCP_SRV --> RET
    
    WK --> EXT
    WK --> MAINT
    
    EXT --> PG
    RET --> PG
    CASE --> PG
    ERASE --> PG
    UNDER --> PG
    MAINT --> PG
    
    PG --> VEC
    PG --> TS
```

## 数据流图

### 写入路径

```{mermaid}
sequenceDiagram
    participant U as 用户/Agent
    participant API as FastAPI / MCP
    participant DB as PostgreSQL
    participant WK as Worker
    participant LLM as LLM Service
    
    U->>API: POST /v1/experience
    API->>DB: append_event (WAL)
    API->>DB: enqueue_job (extract)
    API-->>U: 200 {event_id, ?wait=}
    
    WK->>DB: claim_next_job (SKIP LOCKED)
    WK->>DB: 读取 Event
    WK->>LLM: 抽取三元组
    LLM-->>WK: entities + facts
    WK->>DB: 实体链接 (B over C)
    WK->>DB: 写入 facts + beliefs
    WK->>DB: complete_job
    DB-->>U: pg_notify (lifecycle)
```

### 读取路径

```{mermaid}
sequenceDiagram
    participant U as 用户/Agent
    participant API as FastAPI / MCP
    participant DB as PostgreSQL
    participant EMB as Embedding (jina-v5)
    participant LLM as LLM Service
    
    U->>API: POST /v1/recall {query}
    API->>EMB: embed(query)
    
    par 6 通道
        API->>DB: _chan_vector (pgvector)
        API->>DB: _chan_bm25 (tsvector)
        API->>DB: _chan_graph (递归 CTE)
        API->>DB: _chan_entity_name
        API->>DB: _chan_synonym
        API->>DB: _chan_temporal_decay
    end
    
    Note over API: RRF 融合 (k=60)
    
    API->>LLM: Prism rerank (top-40)
    LLM-->>API: reranked top-20
    
    API->>LLM: 合成 context_block
    LLM-->>API: context_block
    
    Note over API: 组装 StratifiedPack
    
    API-->>U: StratifiedPack
```

## 五层记忆架构

```{mermaid}
graph LR
    subgraph Layer 1
        E[Events<br/>WAL Append-Only]
    end
    subgraph Layer 2
        EP[Episodes<br/>Case 管理]
    end
    subgraph Layer 3
        F[Facts<br/>双时态三元组<br/>知识图谱边]
    end
    subgraph Layer 4
        B[Beliefs<br/>概率断言<br/>证据链]
    end
    subgraph Layer 5
        U[Understanding<br/>概念合成<br/>Related 图]
    end
    
    E -->|extract| F
    E -->|segment| EP
    EP -->|attach| F
    F -->|aggregate| B
    B -->|synthesize| U
    F -->|graph walk| F
```

## 部署架构

```{mermaid}
graph TB
    subgraph 单机部署
        direction TB
        API[cortex FastAPI<br/>:8002]
        MCP_SRV[cortex MCP HTTP<br/>:8001]
        WK1[Worker 1]
        WK2[Worker 2]
        
        subgraph Python 进程
            API
            MCP_SRV
            WK1
            WK2
        end
        
        PG[(PostgreSQL<br/>局域网/云)]
    end
    
    subgraph 外部服务
        JINA[jina-v5 Embedding<br/>1024d]
        PRISM[Prism Rerank]
        LLM[Minimax-M3 LLM]
    end
    
    subgraph 客户端
        VUE[Vue 3 Frontend<br/>:5173]
        CLAUDE[Claude Code / Agent<br/>MCP stdio]
        HTTP_CLI[Remote Agent<br/>MCP HTTP]
    end
    
    VUE --> API
    CLAUDE --> MCP_SRV
    HTTP_CLI --> MCP_SRV
    
    API --> JINA
    API --> PRISM
    API --> LLM
    MCP_SRV --> JINA
    MCP_SRV --> PRISM
    MCP_SRV --> LLM
    
    API --> PG
    MCP_SRV --> PG
    WK1 --> PG
    WK2 --> PG
```

## 模块依赖关系

```{mermaid}
graph TB
    CORE[core.py<br/>WAL / Queue / Lifecycle]
    DB[db.py<br/>Engine / Session]
    CFG[config.py<br/>YAML 配置]
    SVC[services.py<br/>Embedding / LLM / Rerank]
    ONTO[ontology.py<br/>谓词本体]
    PROMPT[prompts.py<br/>LLM Prompts]
    SCHEMA[schema.sql<br/>DDL]
    
    EXT[extraction/pipeline<br/>抽取管线]
    EL[extraction/pipeline<br/>实体链接]
    RET[retrieval/pipeline<br/>6通道检索]
    
    WK[worker/runner<br/>Worker 循环]
    API[api/app<br/>FastAPI 端点]
    MCP[mcp_server<br/>23 MCP 工具]
    
    EPI[episodes<br/>Case 管理]
    UNDER[understanding<br/>概念合成]
    INGEST[ingest<br/>批量导入]
    ERASE[erasures<br/>GDPR 真删]
    MAINT[maintenance<br/>维护]
    TEMP[temporal<br/>时间短语]
    SCHEMAS[schemas<br/>Pydantic]
    
    CORE --> DB
    EXT --> SVC
    EXT --> ONTO
    EXT --> PROMPT
    EL --> SVC
    RET --> SVC
    RET --> ONTO
    RET --> PROMPT
    
    WK --> CORE
    WK --> EXT
    
    API --> CORE
    API --> RET
    API --> EPI
    API --> UNDER
    API --> INGEST
    API --> ERASE
    API --> SCHEMAS
    
    MCP --> CORE
    MCP --> EXT
    MCP --> RET
    MCP --> EPI
    MCP --> UNDER
    MCP --> INGEST
    MCP --> ERASE
    MCP --> TEMP
    MCP --> MAINT
    
    DB --> SCHEMA
    CFG --> CORE
    CFG --> SVC
    CFG --> API
```

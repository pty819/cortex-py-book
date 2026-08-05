# 第24章 运行配置与前端运维

## 1. 概述

cortex-py 的配置最初是只读的:启动时由 `load_config()` 从 `config/config.yaml` 读取,加上环境变量覆盖,校验后缓存进模块级 `_CACHE` 单例,进程生命周期内不再变化。任何调整都要改文件并重启进程,对长时间运行的抽取/回答服务很不方便。

本章描述两块新能力:

- **配置热更新**:通过 `POST /v1/admin/config` 在运行时深合并补丁到缓存的 `AppConfig` 单例,功能开关、上游 API key、检索参数等即时生效,无需重启;可选写回 YAML。
- **前端运维页面**:三个新视图 —— `SettingsView`(配置中心)、`OpsView`(运维监控)、`QaView` 的 Ask 诊断面板,加上 SSE 阶段事件,把黑盒的 recall/LLM 流程变成可观测的瀑布图。

````{note}
热更新只覆盖**白名单内**的字段。`database` 与 `embedding.dimension` 明确禁止运行时修改 —— 前者影响连接池,后者与 `schema.sql` 的 `vector(N)` 强绑定,改了会静默召回失败。
````

## 2. 配置热更新机制

### 2.1 原地深合并

`apply_config_patch()` 的关键设计是**不替换 `_CACHE` 实例**,而是递归地把补丁写进现有 `AppConfig` 对象的字段:

```python
# src/cortex/infra/config.py
_CONFIG_PATCH_WHITELIST = {
    "llm", "rerank", "embedding.api_base", "embedding.model", "embedding.provider",
    "worker", "retrieval", "extraction", "feedback", "dreaming", "higher_order",
}

def apply_config_patch(patch: dict) -> AppConfig:
    """原地深合并 patch 到缓存的 AppConfig(不替换 _CACHE 实例,
    避免 app.py 的 cfg 绑定失效)。"""
    cfg = load_config()
    candidate = cfg.model_copy(deep=True)                 # 1) 深拷贝,避免半成品污染 _CACHE
    _deep_merge(candidate, patch, root_path="")           # 2) 白名单深合并
    validated = AppConfig.model_validate(                 # 3) 重跑 Pydantic 校验(含 dimension 强约束)
        candidate.model_dump(by_alias=True))
    for field_name in AppConfig.model_fields:             # 4) 把校验通过的值逐字段回填到原 cfg
        setattr(cfg, field_name, getattr(validated, field_name))
    return cfg
```

白名单**不含 `api`**:`ApiCfg` 现在只承载传输层设置 `cors_origins`(认证已归属上游应用,见 `config.py` 的 `ApiCfg` 注释),运行中改它会让 CORS 与部署不一致。注释明确"传输配置 `api` 与 `database`/`embedding.dimension` 禁止运行时修改",必须通过受控部署更新并重启。

为什么不替换 `_CACHE`?因为 `app.py` 等模块在启动时通过 `cfg = load_config()` 拿到单例引用,并在闭包/装饰器里长期持有。如果热更新走 `load_config(reload=True)` 路径重建实例,旧的 `cfg` 引用就指向了一个孤儿对象,新配置对它们永远不可见。原地改字段让所有持有引用的代码立刻看到新值。

但"原地改字段"不能跳过校验:补丁可能引入非法值(如 `embedding.dimension` 与 schema 的 `vector(N)` 不一致)。所以 `apply_config_patch` 先深拷贝一份候选对象,合并补丁后用 `AppConfig.model_validate(...)` 重跑完整 Pydantic 校验,校验通过才把每个字段 `setattr` 回真正的 `_CACHE` 实例。校验失败 `_CACHE` 不变。

### 2.2 白名单校验

`_deep_merge()` 在递归时维护 `root_path`(如 `llm.extraction.api_key`),对每个字段做两层校验:

```python
def _deep_merge(target, patch: dict, root_path: str) -> None:
    for key, value in patch.items():
        path = f"{root_path}.{key}" if root_path else key
        top = path.split(".")[0]
        # 三条白名单匹配规则:精确路径、顶层、父路径在白名单
        allowed = (path in _CONFIG_PATCH_WHITELIST or top in _CONFIG_PATCH_WHITELIST
                   or any(item.startswith(path + ".") for item in _CONFIG_PATCH_WHITELIST))
        if not allowed:
            raise ValueError(f"Field '{path}' is not patchable at runtime (not in whitelist)")
        if top == "database" or path == "embedding.dimension":
            raise ValueError(f"Field '{path}' cannot be changed at runtime (requires restart)")
        # 特判:retrieval.graph_weight 落到 channels.graph.weight
        if path == "retrieval.graph_weight":
            target.channels.graph.weight = value
        # 递归或赋值;retrieval.profiles 整表替换并逐项 model_validate
        if isinstance(value, dict) and hasattr(current, "model_dump"):
            _deep_merge(current, value, path)
        elif isinstance(value, dict) and isinstance(current, dict):
            if path == "retrieval.profiles":
                current.clear()
            for k2, v2 in value.items():
                current[k2] = (RetrievalProfileCfg.model_validate(v2)
                               if path == "retrieval.profiles" else v2)
        else:
            setattr(target, key, value)
```

白名单匹配有**三条**规则:① 精确路径命中(如 `embedding.api_base`);② 顶层整棵子树命中(如 `llm`,覆盖 `llm.extraction.api_key` 等任意下钻);③ **父路径**已在白名单(如 `embedding.model` 在白名单,允许 `embedding.model` 本身)。另有两条 `retrieval` 特判:`retrieval.graph_weight` 实际写入 `target.channels.graph.weight`(检索通道权重,非顶层标量);`retrieval.profiles` 走"整表 clear + 逐项 `RetrievalProfileCfg.model_validate`",而非原地合并。

规则:

| 字段 | 可热更新 | 说明 |
|------|----------|------|
| `llm` / `rerank` | 是 | 整棵子树,含 `api_key` |
| `embedding.api_base` / `.model` / `.provider` | 是 | 仅这三项 |
| `embedding.dimension` | **否** | 与 `vector(N)` 强绑定 |
| `embedding.api_key` | 否(不在白名单) | 走环境变量 |
| `database.*` | **否** | 整棵子树禁 |
| `api` | **否** | 传输设置(`cors_origins`),禁止运行时修改,需重启 |
| `worker` / `retrieval` / `extraction` | 是 | |
| `feedback` / `dreaming` / `higher_order` | 是 | 含 `enabled` 开关 |

### 2.3 持久化

`save_config()` 的真实行为与直觉相反:**它不会把 env 注入的 secret 明文落盘**。docstring 直写"原子写回 YAML;环境变量注入的 secret 永不落盘"。流程是先 `model_dump()` 拿到当前内存配置(其中已被 env 覆盖成真实 key 的字段),再调 `_restore_persisted_secrets(data, raw, persist_secret_paths)` 把这些字段替换回**磁盘 YAML 里的原值或占位符**:

```python
def save_config(path: Path | str | None = None, *,
                persist_secret_paths: Optional[set[tuple[str, ...]]] = None) -> None:
    """原子写回 YAML；环境变量注入的 secret 永不落盘。"""
    cfg = load_config()
    p = Path(path or os.environ.get("CORTEX_CONFIG", _DEFAULT_CONFIG_PATH)).resolve()
    data = cfg.model_dump(by_alias=True)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}   # 磁盘当前原值
    _restore_persisted_secrets(data, raw, persist_secret_paths=persist_secret_paths or set())
    # …原子写:tempfile + fsync + os.replace…
```

`save_config` 多了一个 `persist_secret_paths` 参数:由 `admin_config_post` 用 `secret_paths_in_patch(body)` 算出本次补丁**显式携带**的 secret 路径,持久化时这些被用户主动改写的 key 保留明文写入,其余 env secret 仍被盖回。`_restore_persisted_secrets` 只对两类键做替换:`api_key`(每个 secret 字段)与 `database.url`(**整段**)。若该键在磁盘原 YAML 里有值就写回原值;否则按类型给空串(字符串)或空列表:

```python
def _is_secret_path(path: tuple[str, ...], key: str) -> bool:
    return key == "api_key" or path == ("database", "url")

def _restore_persisted_secrets(data, raw, path=(), *, persist_secret_paths) -> None:
    """把当前模型里的 env secret 替换回磁盘原值/占位符。"""
    if not isinstance(data, dict):
        return
    raw_dict = raw if isinstance(raw, dict) else {}
    for key, value in list(data.items()):
        current_path = path + (key,)
        secret = _is_secret_path(current_path, key)
        if secret:
            if current_path in persist_secret_paths:
                continue                        # 显式改写的 secret 保留明文
            data[key] = raw_dict[key] if key in raw_dict else ([] if isinstance(value, list) else "")
            continue
        _restore_persisted_secrets(value, raw_dict.get(key), current_path, persist_secret_paths=persist_secret_paths)
```

结果:env 覆盖的 secret 只活在进程内存里,持久化后的 YAML 与重启前看到的内容一致,不泄漏明文。写入采用**原子替换**(tempfile + `os.fsync` + `os.replace`,保留原文件权限),中途崩溃不会留下半截损坏的 `config.yaml`。

### 2.4 典型用法

```python
from cortex.infra.config import apply_config_patch, save_config

# 开启 Dreaming(默认关,配 LLM key 后开)
apply_config_patch({"dreaming": {"enabled": True}})

# 轮换抽取 LLM 的 api_key
apply_config_patch({"llm": {"extraction": {"api_key": "sk-xxx"}}})

# 调检索参数
apply_config_patch({"retrieval": {"top_k": 80, "graph_weight": 0.3}})

# 持久化到 config.yaml
save_config()
```

```{warning}
`apply_config_patch` 是进程内的。多进程部署(uvicorn workers > 1)时,补丁只对当前进程生效;要全量生效需 `persist=true` 写回文件后逐个 reload 或重启。
```

### 2.5 热更新流程

```{mermaid}
sequenceDiagram
    participant FE as 前端 SettingsView
    participant API as FastAPI /v1/admin/config
    participant CFG as config.py _CACHE
    participant FS as config.yaml

    FE->>API: POST patch + ?persist=true
    API->>CFG: apply_config_patch(patch)
    CFG->>CFG: _deep_merge 白名单校验
    alt 非法字段
        CFG-->>API: ValueError
        API-->>FE: 422
    else 合法
        CFG-->>API: AppConfig(原地改)
        opt persist=true
            API->>FS: save_config() yaml.safe_dump
        end
        API->>CFG: model_dump()
        API->>API: _mask_secrets
        API-->>FE: 200 脱敏后的全量配置
    end
```

## 3. 配置 API

### 3.1 读取:GET /v1/admin/config

返回 `AppConfig.model_dump()`,但密钥被 `_mask_secrets()` 原地脱敏:

```python
def _mask_secrets(value) -> None:
    """原地遮蔽 DSN 与 API key。"""
    if isinstance(value, list):
        for item in value:
            _mask_secrets(item)
        return
    if not isinstance(value, dict):
        return
    for key, nested in list(value.items()):
        if key == "api_key" and isinstance(nested, str):
            value[key] = "***" if nested else ""
            value[f"has_{key}"] = bool(nested)        # 注入布尔状态
        elif key == "url" and isinstance(nested, str):
            value[key] = "***"                        # database.url
        else:
            _mask_secrets(nested)
```

脱敏规则覆盖 **2 个键**：

| 原字段 | 脱敏后 | 附带 |
|--------|--------|------|
| `*.api_key`(有值) | `"***"` | `has_api_key: true` |
| `*.api_key`(空) | `""` | `has_api_key: false` |
| `database.url` | `"***"` | — |

前端据此在 LLM/Rerank 卡片上显示「已配置 / 未配置」徽标,而无需拿到真实 key。

### 3.2 修改:POST /v1/admin/config

```python
@app.post("/v1/admin/config")
def admin_config_post(body: dict, request: Request,
                      persist: bool = Query(False)):
    """修改运行配置(白名单深合并)。persist=true 时写回 YAML。"""
    from ...infra.config import apply_config_patch, save_config
    try:
        apply_config_patch(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if persist:
        try:
            save_config(persist_secret_paths=secret_paths_in_patch(body))
        except Exception as e:
            raise HTTPException(500, f"failed to persist config: {e}")
    d = _load_runtime_config(request).model_dump()
    _mask_secrets(d)
    return d
```

要点:

- **鉴权**:不再内嵌 `Depends(admin_auth)` —— 认证已整体上移给上游应用(见 `ApiCfg` 注释),端点只暴露 `body` / `persist` / `request`。
- **Body**:任意层级的补丁 dict,只含想改的字段即可(深合并)。
- **持久化与 secret**:`persist=true` 时 `save_config(persist_secret_paths=secret_paths_in_patch(body))` —— 本次补丁显式改写的 `api_key` 会明文落盘,其余 env secret 仍被盖回为占位符。
- **返回**:脱敏后的全量配置(同 GET)。
- **错误**:`422` 白名单违例(`ValueError` 透传);`500` 持久化失败。

请求示例:

```bash
curl -X POST http://localhost:8000/v1/admin/config \
  -H "Content-Type: application/json" \
  -d '{"dreaming": {"enabled": true}, "retrieval": {"top_k": 80}}' \
  '?persist=true'
```

## 4. 任务队列 API

### 4.1 GET /v1/admin/jobs

```python
@app.get("/v1/admin/jobs")
def admin_jobs(scope: Optional[str] = Query(None),
               status: Optional[str] = Query(None),
               job_type: Optional[str] = Query(None),
               limit: int = Query(50, le=500)):
    """查看任务队列明细(不返回 payload,可能含敏感数据)。"""
```

查询参数:

| 参数 | 说明 |
|------|------|
| `scope` | 按 scope 过滤 |
| `status` | `queued` / `running` / `completed` / `failed` / `cancelled` |
| `job_type` | `extract` / `segment` / `methylation` / `consolidate` / `synthesize` / `dream` / `higher_order` / `enrich` |
| `limit` | 默认 50,上限 500 |

返回字段: `job_id, job_type, scope, status, attempts, max_attempts, priority, locked_by, locked_at, run_after, created_at, started_at, completed_at, error, result`。

````{important}
该端点**故意不返回 `payload`**。job 的 payload 可能含原始 event 文本或 LLM 中间产物,属于敏感数据;运维只需看状态、重试次数、错误信息即可定位问题。
````

## 5. answer/stream SSE phase 事件

为了让前端诊断 recall 与 LLM 的耗时分布,`/v1/answer/stream` 在原有的 `reasoning` / `answer` / `done` 事件之外,新增三个 `phase` 事件:

```python
# src/cortex/interfaces/api/app.py  (answer 流式生成器内)
# 1) recall 完成
yield {"event": "phase", "data": _json.dumps(
    {"phase": "recall_done", "pack_id": pack["pack_id"],
     "time_ms": (pack.get("diagnostics") or {}).get("time_ms")})}

# 2) LLM 调用开始
yield {"event": "phase", "data": _json.dumps(
    {"phase": "llm_start", "model": model_use})}

# …reasoning / answer 文本流…

# 3) LLM 流结束
yield {"event": "phase", "data": _json.dumps({"phase": "llm_end", "model": model_use})}
```

| 事件 | data 字段 | 触发时机 |
|------|-----------|----------|
| `recall_done` | `pack_id`, `time_ms`(recall 内部各阶段耗时 dict) | 6 通道检索 + rerank 完成后 |
| `llm_start` | `model`(answer tier 模型名) | 首次调用 LLM 前 |
| `llm_end` | `model` | LLM 流彻底结束(answer 已全部 yield) |

SSE 帧示例:

```
event: phase
data: {"phase":"recall_done","pack_id":"pk_abc","time_ms":{"fetch":120,"fuse_rrf":8,"rerank":340}}

event: phase
data: {"phase":"llm_start","model":"gpt-4o-mini"}

event: reasoning
data: {"text":"..."}

event: answer
data: {"text":"..."}

event: phase
data: {"phase":"llm_end","model":"gpt-4o-mini"}

event: done
data: {"model_used":"gpt-4o-mini","pack_id":"pk_abc","citations":[...]}
```

前端用浏览器收到每个 `phase` 事件时的 `Date.now()` 时间戳,减去请求开始时间,即可算出 recall 与 LLM 两段的墙钟耗时,无需依赖后端时钟。这是分布式可观测性的常见技巧 —— 避免客户端/服务端时钟漂移。

## 6. 前端:控制平面架构

````{admonition} 重构背景
:class: important
前端曾是扁平的 demo 导航(Ingest/Graph/Ask/Browse/Ops/Settings 六个平级链接),且 API 客户端硬编码 `dev-key` / `user:alice`。随着后端能力扩展到 Cases、诊断召回、证据、演化审批、词表、时间短语、Understanding、导入导出、维护、Higher-Order、Playbooks,扁平导航不再能覆盖。重构(commit `e2645de`,PR #13)把前端升级为**控制平面**:左侧持久化分组导航 + 13 个视图。
````

### 6.1 信息架构:左侧分组导航

`frontend/src/App.vue` 用固定左侧栏(`.control-rail`)把全部能力按职责分四组,上下文标题栏(`.context-bar`)显示当前页标题与健康状态:

| 分组 | 路由 | 视图 |
|------|------|------|
| **Observe** | `/overview` `/ops` | Overview(健康/版本/存储/队列/特性概览)、Operations(jobs/worker/Dreaming/Higher-Order) |
| **Operate** | `/ingest` `/data` `/cases` `/playbooks` `/qa` | Ingest、Data operations(文档/批量/导入导出/Evidence)、Cases & Diagnosis、Playbooks、Ask |
| **Inspect** | `/graph` `/browse` `/understanding` | Knowledge Graph、Memory Browser、Understanding |
| **Govern** | `/governance` `/api-console` `/settings` | Governance(演化审批/反馈/词表/时间短语/Erasure)、API Console、Settings |

默认落地页从 `/ingest` 改为 `/overview`。移动端(`@media max-width: 980px`)左侧栏折叠为抽屉,由上下文栏的 ☰ 按钮唤出。

### 6.2 Pinia store:scope 与 settings

接入认证上移到上游应用后,前端**不再需要连接凭据 store**——旧的 `connection.ts`(token/actor/admin-key 的 localStorage 持久化)已移除。现在 `frontend/src/stores/` 只保留:

- `scope.ts`:`useScopeStore` 维护全局 scope 选择(localStorage 持久化),启动时从 `/v1/scopes/list` 动态拉取候选,供 Ingest/检索等需要 scope 的调用读取。
- `settings.ts`:`useSettingsStore` 是 mock 模式开关的遗留(Mock 已移除,`useMock` 固定 `false`、不可切换)——前端永远走 Live API。

请求头不再由前端注入 `Authorization` / `X-Cortex-Actor` / admin-key;需要认证时由上游网关统一附加,前端按无状态 SPA 处理。

### 6.3 SSE 从 EventSource 改为 fetch

原 SSE 订阅用 `EventSource`,但 `EventSource` **无法附加自定义请求头**,因此流式端点在带鉴权时无法直连。重构把 SSE 改为 `fetch` + `AbortController` + 手写 SSE 帧解析(通用 `subscribeApiStream`)。认证头上移后前端不再注入自定义头,`fetch` 直接打相对路径、由上游网关统一鉴权:

```typescript
// frontend/src/api/index.ts
export function subscribeApiStream(path, onFrame, onError): () => void {
  const controller = new AbortController()
  fetch(`/v1${path}`, { headers: { Accept: 'text/event-stream' },
                        signal: controller.signal })
    .then(/* reader.read() 循环 + 按 \n\n 切块 + parseSseBlock */ )
  return () => controller.abort()   // 返回 unsubscribe
}
```

`subscribeLifecycle` 等老封装改为内部调 `subscribeApiStream`,对外 API 不变。返回值仍是"取消订阅"函数,组件 unmount 时调用即可中断流。

### 6.4 全量端点封装

`api/index.ts` 从只覆盖点状需求扩展到**完整后端能力面**,统一走 `requestApi<T>(method, path, {params, data})`:

| 领域 | 函数 |
|------|------|
| 健康 | `getHealth` |
| 文档/批量/导入导出 | `ingestDocument` `bulkExperience` `runImport` `getImportStatus` `exportScope` |
| 证据 | `registerEvidence` `attachEvidenceClaim` |
| Cases/诊断 | `listCases` `getCase` `createCase` `updateCase` `addCaseEvent` `getCaseWorkspace` `promoteCaseFacts` `diagnosisRecall` |
| 演化审批/反馈 | `listEvolutionCandidates` `reviewEvolutionCandidate` `listFeedback` `submitFeedback` |
| 词表/时间短语 | `listVocabularies` `createVocabulary` `deleteVocabulary` `listTemporalPhrases` `createTemporalPhrase` `deleteTemporalPhrase` |
| Erasure | `previewErasure` `executeErasure` |
| Understanding | `listUnderstanding` `understandingCoverage` `synthesizeUnderstanding` `getUnderstanding` |
| 维护/高阶 | `runMaintenance` `runHigherOrder` `listHigherOrder` |

### 6.5 复用组件

| 组件 | 作用 |
|------|------|
| `PageHeader.vue` | 页内标题 + 说明 + 操作槽,统一各视图顶部样式 |
| `JsonResult.vue` | 深色 `<pre>` 渲染任意 JSON 响应,API Console / 调试视图复用 |
| `RetrievalControlPanel.vue` | 检索控制面(六通道调音 + 三融合策略 + 四信号开关 + A/B 预览) |
| `ScopeSelector.vue` | 全局 scope 选择(已存在,接入上下文栏) |

### 6.6 检索控制面(RetrievalControlPanel)

`frontend/src/components/RetrievalControlPanel.vue` 是检索调参的核心 UI,嵌入 SettingsView 的检索 Tab。它把后端 `/v1/admin/retrieval/effective` 返回的**三层视图**(配置值 / 有效值 / 依赖就绪态)分开呈现,让运维清楚区分"我配的"和"实际生效的":

- **六通道独立行**:每通道(vector/bm25/graph/entity_name/synonym/temporal)一行,各带 `enabled` 开关、`weight` 滑块、`top_k` 输入。通道关闭或其依赖(如 vector/graph 依赖 embedding)未就绪时,显示降级徽标而非静默失效。
- **融合策略选择**:`rrf` / `weighted_rrf` / `priority` 三选一。选 `weighted_rrf` 时各通道 weight 生效;选 `priority` 时 weight 失效(按通道顺序拼接)。
- **四信号独立开关**:Salience / Usage / Usefulness / Exploration 各自一个 `NSwitch`,配各自的权重/参数输入。开关状态直接映射 `AdvancedRetrievalCfg` 的 `*_enabled` 字段。
- **Rerank 控制**:`enabled` 开关 + `threshold` / `top_n` / `timeout`。
- **命名 Profile**:可创建/切换 Profile,每个 Profile 是一份完整 tuning + 专属 rerank 覆盖。`active_profile` 标记当前激活。

#### A/B Preview(无副作用预览)

面板内置 **Active-vs-Draft A/B 预览**:输入一个 query,选择 1–4 个配置变体(不同 profile 或临时 overrides),点"预览"调 `POST /v1/admin/retrieval/preview`。返回每个变体的 fact 排名 + 每通道候选数 + 耗时,并标注与 baseline(第一个变体)的排名差异。

```{important}
预览请求后端以 `track_usage=False` 执行:**不递增 `retrieval_count`、不写 `recall_packs` 缓存**。这是反复调参的前提——否则每次预览都会污染线上计数与缓存。运维可以放心地反复试不同 weight/通道开关,找到最优配置再保存生效。
```

## 7. 前端:SettingsView 配置中心

`frontend/src/views/SettingsView.vue` 是 4 个 Tab 的配置编辑器。核心设计:

- `original`(后端返回的脱敏配置)与 `working`(深拷贝工作副本)双份。`dirty` computed 比较两者 JSON(剔除被脱敏的 `api_key`)。
- LLM/Rerank 卡片里 api_key 用独立的 `newApiKeys` reactive 对象收集;后端只返回 `***` + `has_key`,用户重新输入才视为变更。
- `buildPatch()` 只发变更字段,最小化补丁体积。
- 保存调 `patchConfig(patch, persist)`,返回值覆盖 `original`,完成闭环。

### 7.1 Tab 1:功能开关

三个 `NCard`:Feedback / Dreaming / Higher-Order。每张卡片:

- 头部一个 `NSwitch` 绑定 `working.<feat>.enabled`,即时切换状态徽标(已启用/已停用)。
- 折叠面板内是该功能的数值参数,用 `NSlider`(连续值,如 `positive_weight`、`similarity_threshold`)或 `NInputNumber`(整数,如 `lookback_days`、`min_cluster_size`)。

### 7.2 Tab 2:上游 API

4 张 `NCard`:Extraction / Answer / Synthesis LLM + Rerank。每张:

- Provider 只读展示。
- Model / API Base / Temperature 可编辑。
- API Key 是 `type="password"` 输入,placeholder 显示 `*** (已配置)` 或 `输入新的 API Key`;输入值进入 `newApiKeys[tier]`,不在 `working` 里。
- 头部徽标根据 `has_key` 显示绿色「已配置」或红色「未配置」。
- **`extra_body` 透传**:`EmbeddingCfg` / `RerankCfg` / `LLMTierCfg` 都带 `extra_body: Optional[Dict]`,运行时配置支持把额外的请求体字段(如 provider 专属参数)原样透传给上游,无需改代码。此字段在 `working` 里可直接编辑。

### 7.3 Tab 3:检索调参

一个 `NCard` 内的 `NForm`,扁平检索参数与 `advanced` 子对象:

- `top_k`(NInputNumber,5–500)
- `rrf_k` / `graph_weight` / `salience_weight`(`NSlider`)
- `hyde_enabled` / `multihop_enabled` / `question_routing` / `entity_vector_seed`(`NSwitch`)

读写通过 `getRetrievalNumber` / `getRetrievalAdvanced` / `setRetrievalField` 等辅助函数,绕过 Vue 对动态键的可选链限制。

### 7.4 Tab 4:系统

- **版本信息**:Cortex 版本、schema 表数量(由独立的 `getVersion()` 拉取,不随 config 重置)、数据库 schema、数据库 URL(脱敏)。
- **Worker 配置**:`visibility_timeout_secs` / `reaper_interval_secs` / `max_attempts`。
- **API 管理**:`cors_origins`(多选 tag 输入)。(`API Console` 视图承载对上游认证的原始调用,见第 10 节。)

### 7.5 顶栏操作

```
[开关即时生效;API密钥需保存后才持久化]  [☑ 持久化到文件]  [重置]  [保存]
```

- **保存**:调 `patchConfig(buildPatch(), persist)`。成功后 `original = updated`,显示 `已保存并持久化到文件` 或 `配置已保存`。
- **持久化到文件** 复选框:控制 `persist` 查询参数。
- **重置**:`working = structuredClone(original)`,清空 `newApiKeys`。

## 8. 前端:OpsView 运维监控

`frontend/src/views/OpsView.vue` 是实时运维仪表盘,每 5 秒 `refreshAll()` 并发拉 `getMetrics()` 与 `getJobs()`。

### 8.1 统计卡片

两行 `NStatistic`:

- **存储指标**(6 项):Events / Facts / Beliefs / Entities / Episodes / Blobs。
- **任务队列概览**(4 项):Queued(info) / Running(warning) / Completed(success) / Failed(error),用彩色 `NTag` 渲染数字。

队列计数从 `metrics.jobs_by_status` 读取,存储计数从 `metrics` 顶层字段读取。

### 8.2 活跃 Worker

```{important}
活跃 worker 不是独立端点,而是前端从 `jobs` 列表推导:
筛选 `status === 'running' && locked_by` 的 job,映射出 `{worker_id, job_type, scope}`。
这等价于 `SELECT DISTINCT locked_by FROM jobs WHERE status='running'`,但复用了已拉取的 job 数据,省一次请求。空闲 worker 不可见。
```

每个活跃 worker 渲染为一个 warning 色 `NTag`:`worker_id · job_type (scope)`,带 Tooltip。

### 8.3 任务队列表

`NDataTable` 列定义(均用 `render` 函数定制):

| 列 | 渲染 |
|----|------|
| `job_type` | 彩色 `NTag`(`extract=info` / `dream=purple` / `higher_order=cyan` / `synthesize=success` / `methylation=warning` / `consolidate=teal`) |
| `scope` | 截断 + Tooltip |
| `status` | `NTag`,`running` 加 `pulse-tag` 1.4s 脉冲动画 |
| `attempts` | `attempts/max_attempts`,重试 >1 时红色加粗 |
| `worker` | `locked_by` 用 `<code>` 渲染 |
| `耗时` | `fmtDuration(started_at, completed_at)`,running 时用 `Date.now()` 持续计时 |
| `error` | 截断到 60 字符 + 省略号,Tooltip 显示全文 |
| `created_at` | 本地化时间 |

表头两个 `NSelect` 过滤器(`status` / `job_type`),通过 `filteredJobs` computed 应用。分页 15/30/50。

### 8.4 Dreaming 控制

- 「立即运行」按钮调 `runDreaming(scope, dryRun, asyncEnqueue=true)`。
- `dry_run` 开关:预演不落库。
- 返回 `run_id` 后,每 3 秒轮询 `getDreamingRun(runId)`,直到 `status` 为 `completed`/`failed`。
- 结果展示 5 个指标:`status` / `phase0_closed` / `phase_a_clusters` / `phase_b_issues` / `phase_c_actions` / `timing_ms`。

## 9. 前端:QaView Ask 诊断面板

`frontend/src/views/QaView.vue` 在回答区与 Raw pack 之间插入了一个可折叠的诊断面板,含四个维度。数据来自 SSE `phase` 事件与最终 pack 的 `diagnostics` / `provenance` 字段。

### 9.1 监听 phase 事件

```typescript
const phaseEvents = ref<Record<string, number>>({})  // phase → 收到时的 Date.now()
const recallTimeMs = ref<number | null>(null)        // recall_done 携带的耗时
const llmModel = ref<string | null>(null)            // llm_start 携带的模型名

es.addEventListener('phase', (ev) => {
  const d = JSON.parse((ev as MessageEvent).data)
  phaseEvents.value[d.phase] = Date.now()
  if (d.phase === 'recall_done' && typeof d.time_ms === 'number') {
    recallTimeMs.value = d.time_ms
  }
  if (d.phase === 'llm_start') llmModel.value = d.model || null
})
```

### 9.2 阶段耗时瀑布图

`stageTimings` computed 从 `phaseEvents` 推导三段耗时:

```typescript
const stageTimings = computed(() => {
  const p = phaseEvents.value
  const recallMs = recallTimeMs.value
    ?? (p.recall_done ? p.recall_done - askStartTime.value : null)
  const llmMs = (p.llm_start != null && p.llm_end != null)
    ? p.llm_end - p.llm_start : null
  const totalMs = p.done ? p.done - askStartTime.value : null
  return { recallMs, llmMs, totalMs }
})
```

渲染为三行横向进度条,宽度按 `ms / stageMax * 100%`。**瓶颈检测**:任一阶段 `> 5000ms` 时进度条加 `is-bottleneck` class,触发脉冲动画,提示用户哪一段拖慢了响应。

### 9.3 6 通道命中数

```typescript
const CHANNEL_META = {
  vector:      { color: '#2080f0', label: '向量' },
  bm25:        { color: '#18a058', label: 'BM25' },
  graph:       { color: '#f0a020', label: '图谱' },
  entity_name: { color: '#d03050', label: '实体名' },
  synonym:     { color: '#7c3aed', label: '同义词' },
  temporal:    { color: '#2080f0', label: '时序' },
}
```

从 `pack.diagnostics.channels` 读取每通道命中候选数,渲染为 6 条彩色条形图。若全为 0,显示警告「所有通道命中均为 0,召回可能未命中任何候选」—— 这是召回调优的关键信号(通常是 embedding 维度不匹配或 scope 写错)。

### 9.4 provenance trail(数据漏斗)

从 `pack.provenance.trail` 读取召回管线每步的 `kept` 计数,渲染为顺序 `NTag` 链:

```
1. fetch → 240   2. fuse_rrf → 60   3. rerank → 25
```

`trailKeptTotal()` 兼容 `kept` 是数字(fetch 步可能是各通道计数的 dict)的情况,dict 时求和。这条漏斗直观展示了候选从 6 通道抓取 → RRF 融合 → rerank 精排的逐级收敛。

### 9.5 LLM 状态指示器

```typescript
const llmStatus = computed<LlmStatus>(() => {
  const p = phaseEvents.value
  if (p.llm_end != null) return { text: '✅ 生成完成', color: '#18a058', cls: 'llm-st-done' }
  if (answerText.value)  return { text: '✍️ 正在生成回答...', color: '#2080f0', cls: 'llm-st-answer' }
  if (p.llm_start != null) return { text: '🧠 正在思考...', color: '#7c3aed', cls: 'llm-st-thinking' }
  return { text: '⏳ 等待 Recall...', color: '#909399', cls: 'llm-st-wait' }
})
```

四态:⏳等待 Recall → 🧠思考(reasoning 阶段)→ ✍️生成(answer 文本流入)→ ✅完成。旁边显示模型名 `NTag` 与已生成字符数。

**卡住检测**:`stuckDetected` computed 在 `llm_start` 已触发但 10 秒内无 reasoning/answer 文本时返回 true(`nowTick` 每秒刷新驱动重算),触发警告「LLM 正在响应但尚无输出,可能模型正在思考或上游 API 超时」。这对推理模型(长 think)尤其有用 —— 区分「正常长思考」与「上游卡死」。

## 10. 新增视图速览

控制平面重构后共 **13 个视图**,覆盖后端全部能力面:

| 视图 | 分组 | 路由 | 职责 |
|------|------|------|------|
| **OverviewView** | Observe | `/overview` | health/version/storage/queue/active features/pending governance 概览 + 快捷动作 |
| **OpsView** | Observe | `/ops` | jobs/worker 状态、Dreaming/Higher-Order 运行记录、维护操作入口 |
| **IngestView** | Operate | `/ingest` | 单条/批量文档录入,modality/intent 选择 |
| **DataOpsView** | Operate | `/data` | 文档 ingest、批量 experience、import/export、Evidence 登记 |
| **CasesView** | Operate | `/cases` | Case CRUD/lifecycle、workspace graph、promotion、diagnosis recall |
| **PlaybooksView** | Operate | `/playbooks` | 诊断剧本 DAG 管理:创建/版本/可视化/导入导出/正向推理调试 |
| **QaView** | Operate | `/qa` | Ask 诊断面板 + 阶段事件瀑布流 + 卡住检测 |
| **GraphView** | Inspect | `/graph` | 知识图谱可视化(vis-network),实体/事实浏览 |
| **BrowseView** | Inspect | `/browse` | Memory Browser,分层级浏览事件/事实/信念 |
| **UnderstandingView** | Inspect | `/understanding` | 概念合成、coverage、detail、related concepts |
| **GovernanceView** | Govern | `/governance` | evolution 审批、feedback、vocabularies、synonyms、temporal phrases、Erasure |
| **SettingsView** | Govern | `/settings` | 配置中心(检索调参/信号总线/feature flag)、检索控制面板 |
| **ApiConsoleView** | Govern | `/api-console` | 全端点目录 + 认证原始调用(回退兜底) |

**设计意图**:Observe/Operate/Inspect/Govern 四组对应运维生命周期 —— 观察 → 操作 → 审视 → 治理。API Console 作为"原始回退",保证任何后端能力(含尚未建专用 UI 的新端点)都能通过控制平面调用,不再需要 curl。

### 10.1 PlaybooksView（诊断剧本管理）

PlaybooksView 是诊断 playbook 的可视化编辑器,对应后端 `diagnostic_playbooks` + `forward_reasoning` 两套 API。

**核心功能**:

| 功能 | 说明 |
|------|------|
| **DAG 可视化** | 用 vis-network 渲染 playbook 节点和边,节点按类型着色(symptom=蓝/test=绿/action=橙/recommendation=紫/terminal=灰) |
| **节点编辑** | 节点 CRUD、类型切换、条件表达式编辑器(JSON Schema 表单) |
| **边编辑** | 拖拽连线创建边、outcome 选择、边条件编辑 |
| **版本管理** | 版本列表、版本对比(diff)、激活/退役操作 |
| **测试运行** | 内置 forward reasoning 调试面板——输入症状+观测数据,实时看到遍历 trace 和 next_actions |
| **导入导出** | JSON 格式导入导出,便于跨环境迁移 playbook |

**与 CasesView 的关系**:CasesView 处理"具体一次故障怎么查"(案例),PlaybooksView 处理"这类故障应该怎么查"(规程)。两者在 UI 中通过 Operate 分组下的 Tab 切换,数据层完全独立。

```{seealso}
配置项的语义与默认值见 `src/cortex/infra/config.py` 的 Pydantic 模型定义(`RetrievalCfg` / `DreamingCfg` / `HigherOrderCfg` 等)。API 端点完整列表见第18章。前端控制平面的完整设计文档见主仓 `DESIGN.md`。
```

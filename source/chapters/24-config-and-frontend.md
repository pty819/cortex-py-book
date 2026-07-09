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
    "api", "worker", "retrieval", "extraction", "feedback", "dreaming", "higher_order",
}

def apply_config_patch(patch: dict) -> AppConfig:
    """原地深合并 patch 到缓存的 AppConfig(不替换 _CACHE 实例,
    避免 app.py 的 cfg 绑定失效)。"""
    cfg = load_config()
    _deep_merge(cfg, patch, root_path="")
    return cfg
```

为什么不替换 `_CACHE`?因为 `app.py` 等模块在启动时通过 `cfg = load_config()` 拿到单例引用,并在闭包/装饰器里长期持有。如果热更新走 `load_config(reload=True)` 路径重建实例,旧的 `cfg` 引用就指向了一个孤儿对象,新配置对它们永远不可见。原地改字段让所有持有引用的代码立刻看到新值。

### 2.2 白名单校验

`_deep_merge()` 在递归时维护 `root_path`(如 `llm.extraction.api_key`),对每个字段做两层校验:

```python
def _deep_merge(target, patch: dict, root_path: str) -> None:
    for key, value in patch.items():
        path = f"{root_path}.{key}" if root_path else key
        top = path.split(".")[0]
        allowed = path in _CONFIG_PATCH_WHITELIST or top in _CONFIG_PATCH_WHITELIST
        if not allowed:
            raise ValueError(f"Field '{path}' is not patchable at runtime (not in whitelist)")
        if top == "database" or path == "embedding.dimension":
            raise ValueError(f"Field '{path}' cannot be changed at runtime (requires restart)")
        # …递归或赋值
```

规则:

| 字段 | 可热更新 | 说明 |
|------|----------|------|
| `llm` / `rerank` | 是 | 整棵子树,含 `api_key` |
| `embedding.api_base` / `.model` / `.provider` | 是 | 仅这三项 |
| `embedding.dimension` | **否** | 与 `vector(N)` 强绑定 |
| `embedding.api_key` | 否(不在白名单) | 走环境变量 |
| `database.*` | **否** | 整棵子树禁 |
| `api` / `worker` / `retrieval` / `extraction` | 是 | |
| `feedback` / `dreaming` / `higher_order` | 是 | 含 `enabled` 开关 |

### 2.3 持久化

`save_config()` 把当前缓存 `model_dump()` 后用 `yaml.safe_dump` 写回 YAML:

```python
def save_config(path: Path | str | None = None) -> None:
    cfg = load_config()
    p = Path(path or os.environ.get("CORTEX_CONFIG", _DEFAULT_CONFIG_PATH))
    data = cfg.model_dump()
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
```

注意 `model_dump()` 会把通过环境变量注入的 key 也写进文件 —— 若不希望明文落盘,就用环境变量管理敏感 key 而非热更新接口。

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

    FE->>API: POST patch + ?persist=true (X-Cortex-Admin-Key)
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
def _mask_secrets(d: dict) -> None:
    """原地脱敏:api_key -> '***'(标记 has_key),database.url -> '***'。"""
    for k, v in list(d.items()):
        if k == "api_key" and isinstance(v, str):
            d[k] = "***" if v else ""
            d["has_key"] = bool(v)          # 注入布尔状态
        elif k == "url" and isinstance(v, str):
            d[k] = "***"                    # database.url
        elif isinstance(v, dict):
            _mask_secrets(v)
```

脱敏规则:

| 原字段 | 脱敏后 | 附带 |
|--------|--------|------|
| `*.api_key`(有值) | `"***"` | `has_key: true` |
| `*.api_key`(空) | `""` | `has_key: false` |
| `database.url` | `"***"` | — |

前端据此在 LLM/Rerank 卡片上显示「已配置 / 未配置」徽标,而无需拿到真实 key。

### 3.2 修改:POST /v1/admin/config

```python
@app.post("/v1/admin/config")
def admin_config_post(body: dict, persist: bool = Query(False),
                      actor: str = Depends(admin_auth)):
    """修改运行配置(白名单深合并)。persist=true 时写回 YAML。"""
    from ...infra.config import apply_config_patch, save_config
    try:
        apply_config_patch(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if persist:
        try:
            save_config()
        except Exception as e:
            raise HTTPException(500, f"failed to persist config: {e}")
    d = load_config().model_dump()
    _mask_secrets(d)
    return d
```

要点:

- **鉴权**:`Depends(admin_auth)`。若配置了 `api.admin_key`,请求必须带 `X-Cortex-Admin-Key` 头;否则该端点 403。
- **Body**:任意层级的补丁 dict,只含想改的字段即可(深合并)。
- **查询参数**:`?persist=true` 触发 `save_config()` 写回 YAML;缺省只改内存。
- **返回**:脱敏后的全量配置(同 GET)。
- **错误**:`422` 白名单违例(`ValueError` 透传);`500` 持久化失败。

请求示例:

```bash
curl -X POST http://localhost:8000/v1/admin/config \
  -H "Authorization: Bearer $CORTEX_API_KEY" \
  -H "X-Cortex-Admin-Key: $CORTEX_ADMIN_KEY" \
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
               limit: int = Query(50, le=500),
               actor: str = Depends(auth)):
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

## 6. 前端:SettingsView 配置中心

`frontend/src/views/SettingsView.vue` 是 4 个 Tab 的配置编辑器。核心设计:

- `original`(后端返回的脱敏配置)与 `working`(深拷贝工作副本)双份。`dirty` computed 比较两者 JSON(剔除被脱敏的 `api_key`)。
- LLM/Rerank 卡片里 api_key 用独立的 `newApiKeys` reactive 对象收集;后端只返回 `***` + `has_key`,用户重新输入才视为变更。
- `buildPatch()` 只发变更字段,最小化补丁体积。
- 保存调 `patchConfig(patch, persist)`,返回值覆盖 `original`,完成闭环。

### 6.1 Tab 1:功能开关

三个 `NCard`:Feedback / Dreaming / Higher-Order。每张卡片:

- 头部一个 `NSwitch` 绑定 `working.<feat>.enabled`,即时切换状态徽标(已启用/已停用)。
- 折叠面板内是该功能的数值参数,用 `NSlider`(连续值,如 `positive_weight`、`similarity_threshold`)或 `NInputNumber`(整数,如 `lookback_days`、`min_cluster_size`)。

### 6.2 Tab 2:上游 API

4 张 `NCard`:Extraction / Answer / Synthesis LLM + Rerank。每张:

- Provider 只读展示。
- Model / API Base / Temperature 可编辑。
- API Key 是 `type="password"` 输入,placeholder 显示 `*** (已配置)` 或 `输入新的 API Key`;输入值进入 `newApiKeys[tier]`,不在 `working` 里。
- 头部徽标根据 `has_key` 显示绿色「已配置」或红色「未配置」。

### 6.3 Tab 3:检索调参

一个 `NCard` 内的 `NForm`,扁平检索参数与 `advanced` 子对象:

- `top_k`(NInputNumber,5–500)
- `rrf_k` / `graph_weight` / `salience_weight`(`NSlider`)
- `hyde_enabled` / `multihop_enabled` / `question_routing` / `entity_vector_seed`(`NSwitch`)

读写通过 `getRetrievalNumber` / `getRetrievalAdvanced` / `setRetrievalField` 等辅助函数,绕过 Vue 对动态键的可选链限制。

### 6.4 Tab 4:系统

- **版本信息**:Cortex 版本、schema 表数量(由独立的 `getVersion()` 拉取,不随 config 重置)、数据库 schema、数据库 URL(脱敏)。
- **Worker 配置**:`visibility_timeout_secs` / `reaper_interval_secs` / `max_attempts`。
- **API 管理**:`admin_key`(密码框)、`cors_origins`(多选 tag 输入)。

### 6.5 顶栏操作

```
[开关即时生效;API密钥需保存后才持久化]  [☑ 持久化到文件]  [重置]  [保存]
```

- **保存**:调 `patchConfig(buildPatch(), persist)`。成功后 `original = updated`,显示 `已保存并持久化到文件` 或 `配置已保存`。
- **持久化到文件** 复选框:控制 `persist` 查询参数。
- **重置**:`working = structuredClone(original)`,清空 `newApiKeys`。

## 7. 前端:OpsView 运维监控

`frontend/src/views/OpsView.vue` 是实时运维仪表盘,每 5 秒 `refreshAll()` 并发拉 `getMetrics()` 与 `getJobs()`。

### 7.1 统计卡片

两行 `NStatistic`:

- **存储指标**(6 项):Events / Facts / Beliefs / Entities / Episodes / Blobs。
- **任务队列概览**(4 项):Queued(info) / Running(warning) / Completed(success) / Failed(error),用彩色 `NTag` 渲染数字。

队列计数从 `metrics.jobs_by_status` 读取,存储计数从 `metrics` 顶层字段读取。

### 7.2 活跃 Worker

```{important}
活跃 worker 不是独立端点,而是前端从 `jobs` 列表推导:
筛选 `status === 'running' && locked_by` 的 job,映射出 `{worker_id, job_type, scope}`。
这等价于 `SELECT DISTINCT locked_by FROM jobs WHERE status='running'`,但复用了已拉取的 job 数据,省一次请求。空闲 worker 不可见。
```

每个活跃 worker 渲染为一个 warning 色 `NTag`:`worker_id · job_type (scope)`,带 Tooltip。

### 7.3 任务队列表

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

### 7.4 Dreaming 控制

- 「立即运行」按钮调 `runDreaming(scope, dryRun, asyncEnqueue=true)`。
- `dry_run` 开关:预演不落库。
- 返回 `run_id` 后,每 3 秒轮询 `getDreamingRun(runId)`,直到 `status` 为 `completed`/`failed`。
- 结果展示 5 个指标:`status` / `phase0_closed` / `phase_a_clusters` / `phase_b_issues` / `phase_c_actions` / `timing_ms`。

## 8. 前端:QaView Ask 诊断面板

`frontend/src/views/QaView.vue` 在回答区与 Raw pack 之间插入了一个可折叠的诊断面板,含四个维度。数据来自 SSE `phase` 事件与最终 pack 的 `diagnostics` / `provenance` 字段。

### 8.1 监听 phase 事件

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

### 8.2 阶段耗时瀑布图

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

### 8.3 6 通道命中数

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

### 8.4 provenance trail(数据漏斗)

从 `pack.provenance.trail` 读取召回管线每步的 `kept` 计数,渲染为顺序 `NTag` 链:

```
1. fetch → 240   2. fuse_rrf → 60   3. rerank → 25
```

`trailKeptTotal()` 兼容 `kept` 是数字(fetch 步可能是各通道计数的 dict)的情况,dict 时求和。这条漏斗直观展示了候选从 6 通道抓取 → RRF 融合 → rerank 精排的逐级收敛。

### 8.5 LLM 状态指示器

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

## 9. 导航

`frontend/src/App.vue` 的 `navItems` 现在是 6 项,覆盖完整工作流:

```typescript
const navItems = [
  { to: '/ingest',   label: 'Ingest' },          // 写入事件
  { to: '/graph',    label: 'Knowledge Graph' }, // 图谱可视化
  { to: '/qa',       label: 'Ask' },             // 问答 + 诊断面板
  { to: '/browse',   label: 'Browse' },          // 层直读
  { to: '/ops',      label: 'Ops' },             // 运维监控(本章)
  { to: '/settings', label: 'Settings' },        // 配置中心(本章)
]
```

Ingest / Knowledge Graph / Ask / Browse 是数据面,Ops / Settings 是控制面。两者分离让普通用户只接触数据面,运维才进 Ops/Settings —— 后者受 `admin_auth` 保护。

```{seealso}
配置项的语义与默认值见 `src/cortex/infra/config.py` 的 Pydantic 模型定义(`RetrievalCfg` / `DreamingCfg` / `HigherOrderCfg` 等)。API 端点完整列表见第18章。
```

# 第9章 Vocabularies 系统

## 概述

Vocabularies（词表系统）提供**受控词汇**约束，确保抽取的三元组使用标准化的谓词和值。分为 closed（闭集）和 open（开集）两种模式。

```{mermaid}
graph TB
    subgraph Vocab 类型
        C[Closed 闭集<br/>必须命中词表]
        O[Open 开集<br/>未命中保留原值]
    end
    
    subgraph Cardinality
        S[Single 单值<br/>新值超替旧值]
        M[Multi 多值<br/>多值共存]
    end
    
    subgraph 应用
        P[Predicate 谓词约束<br/>诊断关系标准化]
        V[Value 值约束<br/>状态/阶段标准化]
    end
```

## 表结构

### Vocabularies

```sql
CREATE TABLE vocabularies (
    vocab_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('closed', 'open')),
    description TEXT,
    cardinality TEXT DEFAULT 'multi',  -- 'single' | 'multi'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, name)
);
```

### Vocabulary Values

```sql
CREATE TABLE vocabulary_values (
    value_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vocab_id        UUID NOT NULL REFERENCES vocabularies(vocab_id) ON DELETE CASCADE,
    canonical_value TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    sort_order      INT NOT NULL DEFAULT 0,
    cardinality     TEXT DEFAULT 'multi',  -- per-value override
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vocab_id, canonical_value)
);

CREATE INDEX idx_vocab_values_aliases ON vocabulary_values USING gin (aliases);
```

## 两种模式

### Closed（闭集）

**规则**：抽取的值必须命中词表。未命中 → 返回 `null`（值被拒绝）。

**适用**：谓词（predicate）——所有诊断关系必须从预定义列表中选。

### Open（开集）

**规则**：抽取的值尽量匹配词表。未命中 → 保留原值（不拒绝）。

**适用**：值标签、设备名称等——不能限制太死，但鼓励标准化。

## Cardinality（基数）

| 基数 | 行为 | 适用 |
|------|------|------|
| `single` | 新值超替旧值（最后写入的 wins） | 状态字段（"设备状态：运行中"） |
| `multi` | 多值共存 | 多标签、多属性 |

## 谓词 Cardinality 表

诊断谓词的 cardinality 在 `ontology.py` 中统一定义：

```python
# 唯一 single 的来源是 STATE_PREDICATES(当前仅 has_status);
# 其余全部 multi。cardinality 不是手写字典,而是由类别推导。
STATE_PREDICATES = frozenset(
    p for p, d in PREDICATE_DICTIONARY.items() if d.category == "state"
)

PREDICATE_CARDINALITY = {
    predicate: ("single" if predicate in STATE_PREDICATES else "multi")
    for predicate in DIAGNOSIS_PREDICATE_NAMES
}
```

> **DB 镜像**：cardinality 现在也同时存在于 `predicate_definitions` 表中（与 `category`、`prop_order` 并列）。`maintenance.py` 的 `seed_predicate_definitions()` 在 upsert 一阶谓词时会把 `ontology.py` 的 `PREDICATE_CARDINALITY` 同步写入该表的 `cardinality` 列。运行时 cardinality 仍以 `ontology.py` 为单一真相源，`predicate_definitions` 提供可查询/可审计的镜像并承载高阶谓词元数据。详见第 6 章「DB-backed 本体: predicate_definitions 表」。

## Coerce 函数

抽取管线的 `coerce_value` 函数将 LLM 输出的原始值约束到词表：

```python
def coerce_value(conn, scope, vocab_name, raw):
    """closed：未命中→null；open：未命中→保留；命中别名→canonical"""
    row = conn.execute(text("""
        SELECT vocab_id, kind FROM vocabularies 
        WHERE scope=:s AND name=:n
    """), {"s": scope, "n": vocab_name}).fetchone()
    if not row:
        return raw  # 无词表 → 原样
    
    hit = conn.execute(text("""
        SELECT vv.canonical_value FROM vocabulary_values vv 
        WHERE vv.vocab_id=:v
        AND (vv.canonical_value=:r OR :r = ANY(vv.aliases)) LIMIT 1
    """), {"v": row.vocab_id, "r": raw}).fetchone()
    
    if hit:
        return hit.canonical_value  # 命中 → 标准化
    
    return raw if row.kind == "open" else None  # 未命中
```

## 预置诊断词表

`maintenance.py` 的 `seed_diagnosis_vocab` 为新 scope 预置诊断谓词词表：

```bash
# 新 scope 初始化时执行一次
uv run python -m cortex.interfaces.cli maintenance --action seed-vocab --scope equip:XXX-v1
```

预置后，该 scope 的抽取管线会自动约束谓词为 36 个预定义值（8 结构 + 5 因果 + 22 诊断 + 1 状态）。

> **配套函数**：`maintenance.py` 还提供 `seed_predicate_definitions()`，把 `ontology.py` 的一阶谓词 upsert 到 `predicate_definitions` 表（统一标记 `prop_order=1` 并写入 `category`、`cardinality`）。两者互补：`seed_diagnosis_vocab` 预置抽取期的 closed 词表约束，`seed_predicate_definitions` 则填充可查询的本体元数据。详见第 6 章。

## API

| 端点 | 说明 |
|------|------|
| `POST /v1/vocabularies` | 创建词表（body: `scope`、`name`、`kind`、`values[]`） |
| `GET /v1/vocabularies?scope=` | 列出某 scope 下所有词表（含 values） |
| `GET /v1/vocabularies/{name}?scope=` | 取单个词表详情 |
| `PUT /v1/vocabularies/{name}` | 替换词表内容（body: `scope`、可选 `kind`、`values[]`，整体覆盖 values） |
| `DELETE /v1/vocabularies/{name}?scope=` | 删除词表 |

```bash
# 创建词表（closed，多值）
curl -X POST /v1/vocabularies \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "equip:XXX-v1",
    "name": "failure_mode",
    "kind": "closed",
    "values": [
      {"canonical": "密封失效", "aliases": ["密封圈老化", "密封损坏"]},
      {"canonical": "MFC漂移", "aliases": ["MFC校准偏差", "流量计漂移"]}
    ]
  }'

# 列出词表
curl "/v1/vocabularies?scope=equip:XXX-v1"

# 替换词表（整体覆盖 values）
curl -X PUT /v1/vocabularies/failure_mode \
  -H "Content-Type: application/json" \
  -d '{"scope":"equip:XXX-v1","kind":"closed","values":[{"canonical":"密封失效","aliases":[]}] }'

# 删除词表
curl -X DELETE "/v1/vocabularies/failure_mode?scope=equip:XXX-v1"
```

## MCP 工具

```bash
vocab_create(name="failure_mode", kind="closed", 
             values=[{canonical: "密封失效", aliases: ["密封圈老化"]}],
             scope="equip:XXX-v1")

vocab_list(scope="equip:XXX-v1")
```

## Synonyms（同义词扩展）

Vocabularies 解决的是"值标准化"（所有对同一概念的表述归一为 canonical form）。**Synonyms** 解决的是反向问题——当用户用任意别名查询时，都能命中同一条记忆。

### 与 Vocabularies 的区别

| 维度 | Vocabularies（词表） | Synonyms（同义词） |
|------|---------------------|-------------------|
| 方向 | 写入时归一化（多 → 一） | 读取时扩展（一 → 多） |
| 作用层 | 抽取/写入路径 | 检索/读取路径 |
| 粒度 | 按词表分,每个词表一组值 | 全局 term → aliases 映射 |
| 状态 | 只有 active/存在 | draft / active / retired |
| 触发 | 写入时 coerce 函数 | 检索时 synonym 通道自动扩展 |

### 表结构

```sql
CREATE TABLE synonyms (
    synonym_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    term        TEXT NOT NULL,           -- 主词（规范名）
    aliases     TEXT[] NOT NULL DEFAULT '{}',  -- 别名数组
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft','active','retired')),
    locale      TEXT NOT NULL DEFAULT 'und',   -- 语言/区域限定
    domain      TEXT NOT NULL DEFAULT 'general',  -- 领域限定
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | imported | ...
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_by  TEXT,
    reviewed_by TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, term)
);
```

### 归一化与重叠检测

所有 term 和 alias 都经 `normalize_term()` 归一化（小写、去空白、全角转半角）。创建/更新时做**重叠检测**——如果新加入的 aliases 和已有同义词组的成员（term 或任一 alias）重叠，报错拒绝，防止同一个词出现在两个不同的同义词组里导致歧义。

```python
def _assert_no_active_overlap(conn, *, scope, members, exclude_synonym_id=None):
    """检查 members 中任意一个是否已在其他同义词组里"""
    # 对每个 member 做归一化,然后查 synonyms 表
    # 有重叠则 raise ValueError
```

### 检索中的同义词扩展

第 14 章的 **Synonym 通道**（6 通道之一）在检索时自动扩展查询词：

```python
def expanded_terms(conn, *, scope, view, query):
    """返回 (expanded_terms_list, matched_groups)
    expanded_terms = 原 query term + 所有命中同义词组的 aliases
    matched_groups = 命中了哪些同义词组（用于调试/解释）
    """
```

匹配方式是 `_query_contains`——子串双向匹配（query 包含 term 或 term 包含 query 都算命中），不是精确相等。这让"压力不稳"能命中"压力波动"这个同义词组。

### API 与 MCP

| 操作 | HTTP 端点 | MCP 工具 |
|------|----------|----------|
| 列出 | `GET /v1/synonyms` | `synonym_list` |
| 创建 | `POST /v1/synonyms` | `synonym_create` |
| 取单个 | `GET /v1/synonyms/{id}` | `synonym_get` |
| 更新 | `PUT /v1/synonyms/{id}` | `synonym_update` |
| 删除 | `DELETE /v1/synonyms/{id}` | `synonym_delete` |
| 批量导入 | `POST /v1/synonyms/import` | `synonym_import` |
| 导出 | `GET /v1/synonyms/export` | `synonym_export` |

### 典型用法

```python
# 创建同义词组
synonym_create(
    scope="equip:XXX-v1",
    term="压力波动",
    aliases=["压力不稳", "压力振荡", "压力跳动", "pressure oscillation"],
    status="active",
)

# 检索时自动扩展:用户搜"压力不稳" → 扩展为 ["压力不稳", "压力波动", "压力振荡", ...]
# → 提高召回率
```

## 最佳实践

| 场景 | 推荐模式 | 说明 |
|------|---------|------|
| 谓词约束 | closed + multi | 限制所有诊断关系为预定义 36 个谓词 |
| 故障类型 | closed + multi | 标准化故障分类 |
| 设备状态 | closed + single | 每个设备只有一个当前状态 |
| 设备名称 | open + multi | 不能限制太死 |
| 阶段/步骤 | closed + single | 诊断阶段依次推进 |
| 同义词扩展 | active + aliases 3-5 个 | 每个同义词组别太多,避免扩展过宽引入噪声 |

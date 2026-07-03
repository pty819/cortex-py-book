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
PREDICATE_CARDINALITY = {
    # 结构关系（multi）
    "part_of": "multi", "has_component": "multi", 
    "installed_on": "multi", "located_in": "multi",
    
    # 因果关系（multi）
    "caused_by": "multi", "led_to": "multi", 
    "cascades_to": "multi", "affects": "multi",
    
    # 状态关系（single — 超替）
    "has_status": "single", "deal_stage": "single",
    
    # 默认 multi
    ...: "multi" for all other predicates
}
```

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

预置后，该 scope 的抽取管线会自动约束谓词为 40+ 个预定义值。

## API

```bash
# 创建词表（closed，多值）
curl -X POST /v1/vocab \
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
curl /v1/vocab?scope=equip:XXX-v1
```

## MCP 工具

```bash
vocab_create(name="failure_mode", kind="closed", 
             values=[{canonical: "密封失效", aliases: ["密封圈老化"]}],
             scope="equip:XXX-v1")

vocab_list(scope="equip:XXX-v1")
```

## 最佳实践

| 场景 | 推荐模式 | 说明 |
|------|---------|------|
| 谓词约束 | closed + multi | 限制所有诊断关系为预定义 40+ 谓词 |
| 故障类型 | closed + multi | 标准化故障分类 |
| 设备状态 | closed + single | 每个设备只有一个当前状态 |
| 设备名称 | open + multi | 不能限制太死 |
| 阶段/步骤 | closed + single | 诊断阶段依次推进 |

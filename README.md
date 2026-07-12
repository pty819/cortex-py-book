# Cortex-PY 架构解析

> 一本深入解析 [cortex-py](https://github.com/pty819/cortex-py) 项目实现的技术书

## 📖 在线阅读

**https://pty819.github.io/cortex-py-book/**

## 📚 内容概览

全书 26 章(00–25),按上篇(架构总览)/ 中篇(写入路径 + 自演化)/ 下篇(读取路径)/ 接口篇 / 运维篇 / 对比篇组织。完整目录见[在线版](https://pty819.github.io/cortex-py-book/)与 `source/index.md`。

### 上篇:架构总览

| 章节 | 内容 |
|------|------|
| 第0章 | 项目概览 — 系统架构、五层记忆模型、4 子包分层、技术栈 |
| 第1章 | 五层记忆模型 — Events/Episodes/Facts/Beliefs/Understanding |
| 第2章 | 数据模型 — 27 张表、双时态、图遍历、索引策略 |

### 中篇:写入路径(从原始事件到结构化知识)

| 章节 | 内容 |
|------|------|
| 第3章 | WAL 与事件系统 — 幂等写入、Postgres-as-Queue、Lifecycle |
| 第4章 | 抽取管线 — LLM 抽取、并行 I/O(concurrency)、实体链接 |
| 第5章 | 实体链接详解 — B over C 三阶段(lookup → parallel LLM → write) |
| 第6章 | 本体与断言 — 谓词受控词表、assertion_status 生命周期 |
| 第7章 | Episodes 与 Case — 诊断工作区、闭环晋升 |
| 第8章 | Beliefs 与 Understanding — 概念合成、证据链 |
| 第9章 | 词表系统 — 受控词表接入校验 |

### 中篇:记忆自演化(信号总线与三功能)

| 章节 | 内容 |
|------|------|
| 第10章 | 信号总线 — access_count + salience 共享信号层 |
| 第11章 | Feedback 回灌 — 双轨软降权 + 硬归档 |
| 第12章 | Dreaming 离线巩固 — 两阶段 LLM + 人工审批门 |
| 第13章 | Higher-Order 高阶归纳 — evidence-driven 候选生成 |

### 下篇:读取路径(精准召回)

| 章节 | 内容 |
|------|------|
| 第14章 | 检索系统概述 — 6 通道混合检索、Scope 视图 |
| 第15章 | 检索通道详解 — 向量/BM25/图遍历/Entity Name/Synonym/Temporal |
| 第16章 | RRF 融合与 Rerank — RRF 算法、Prism Rerank |
| 第17章 | 时间系统 — 双时态设计、NL 时间短语、时间衰减 |

### 接口篇

| 章节 | 内容 |
|------|------|
| 第18章 | API 参考 — 70 个 REST 端点完整文档 |
| 第19章 | MCP Server — 双传输模式、32 个工具、scope 隔离 |

### 运维篇

| 章节 | 内容 |
|------|------|
| 第20章 | Worker 系统 — Postgres-as-Queue、SKIP LOCKED、Visibility Timeout |
| 第21章 | Maintenance — methylation / consolidation |
| 第22章 | Erasure 系统 — GDPR 删除、4 阶段流程、引用计数 |
| 第23章 | 架构视图 — 4+1 视图模型(Logical/Process/Development/Physical/Scenarios) |
| 第24章 | 运行配置与前端运维 — 配置热更新 + 控制平面重构 |

### 对比篇

| 章节 | 内容 |
|------|------|
| 第25章 | 竞品分析 — vs Mem0/Graphiti/OpenViking,诊断场景聚焦 |

## 🛠 技术栈

| 组件 | 版本 |
|------|------|
| Python | >= 3.12(与 cortex-py 主仓一致) |
| Sphinx | >= 9.1.0 |
| sphinxcontrib-mermaid | >= 2.0.2 |
| sphinx-rtd-theme | >= 3.1.0 |
| myst-parser | >= 5.1.0 |

## 🚀 本地构建

```bash
# 安装依赖
uv sync

# 构建 HTML
uv run sphinx-build -b html source build/html

# 打开 build/html/index.html
```

## 📦 自动部署

推送到 `main` 分支后，GitHub Actions 自动构建并部署到 GitHub Pages。

Workflow 使用最新版本：
- `actions/checkout@v4`
- `astral-sh/setup-uv@v6`
- `actions/setup-python@v5`
- `peaceiris/actions-gh-pages@v4`


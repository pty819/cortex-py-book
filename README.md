# Cortex-PY 架构解析

> 一本深入解析 [cortex-py](https://github.com/pty819/cortex-py) 项目实现的技术书

## 📖 在线阅读

**https://pty819.github.io/cortex-py-book/**

## 📚 内容概览

| 章节 | 内容 |
|------|------|
| 第0章 | 项目概览 - 系统架构、五层记忆模型、技术栈 |
| 第1章 | 五层记忆模型 - Events/Episodes/Facts/Beliefs/Understanding |
| 第2章 | 数据模型 - 表设计、双时态、图遍历、索引策略 |
| 第3章 | WAL 与事件系统 - 幂等写入、Postgres-as-Queue、Lifecycle |
| 第4章 | 抽取管线 - LLM 抽取、Mock 抽取器、实体链接 |
| 第5章 | 实体链接详解 - B over C 三层策略、向量召回、灰区判定 |
| 第6章 | 检索系统概述 - 6 通道混合检索、Scope 视图 |
| 第7章 | 检索通道详解 - 向量/BM25/图遍历/Entity Name/Synonym/Temporal |
| 第8章 | RRF 融合与 Rerank - RRF 算法、Prism Rerank |
| 第9章 | MCP Server - 双传输模式、23 个工具、scope 隔离 |
| 第10章 | Worker 系统 - Postgres-as-Queue、SKIP LOCKED、Visibility Timeout |
| 第11章 | 时间系统 - 双时态设计、NL 时间短语、时间衰减 |
| 第12章 | Erasure 系统 - GDPR 删除、4 阶段流程、引用计数 |
| 第13章 | 架构图汇总 - 全部 Mermaid 图集中展示 |
| 第14章 | API 参考 - REST API + MCP 工具完整文档 |

## 🛠 技术栈

| 组件 | 版本 |
|------|------|
| Python | >= 3.13 |
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


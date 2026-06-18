"""Sphinx configuration for Cortex-PY Architecture Book."""

project = 'Cortex-PY 架构解析'
copyright = '2026, Technical Deep Dive'
author = 'Hermes Agent'

# -- General configuration ---------------------------------------------------
extensions = [
    'myst_parser',
    'sphinxcontrib.mermaid',
]

templates_path = ['_templates']
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

# -- MyST configuration ------------------------------------------------------
source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}

myst_enable_extensions = [
    'colon_fence',
    'deflist',
    'dollarmath',
    'fieldlist',
    'html_admonition',
    'html_image',
    'linkify',
    'replacements',
    'smartquotes',
    'substitution',
    'tasklist',
]

# -- Mermaid configuration ---------------------------------------------------
# sphinxcontrib-mermaid 2.0.2 自带 mermaid CDN 加载和渲染生命周期管理。
# 必须 startOnLoad:false —— 扩展通过 load 事件自行调用 mermaid.run()，
# 如果设 true 会导致 mermaid 被处理两次（自动渲染 + runMermaid），第二次解析 SVG 报错。
mermaid_version = '10.9.3'

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
pygments_style = 'sphinx'

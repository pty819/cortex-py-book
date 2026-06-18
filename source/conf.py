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
# 使用 mermaid 10.9.3 (稳定版)
mermaid_version = '10.9.3'
# 配置 mermaid 初始化参数
mermaid_init_config = {
    "startOnLoad": True,
    "theme": "default",
}

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
pygments_style = 'sphinx'

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
# 构建时渲染：用 mmdc (mermaid-cli) 在构建阶段把 mermaid 代码渲染成 SVG 图片，
# 嵌入 HTML。不依赖浏览器端 JS，彻底避免客户端渲染的双重转义和 startOnLoad 问题。
mermaid_cmd = '/tmp/node_modules/.bin/mmdc'
mermaid_cmd_shell = False
mermaid_output_format = 'svg'
mermaid_params = ['--configFile', '/tmp/cortex-book/mermaid-config.json']
# 去掉 mermaid_version —— 构建时渲染不需要加载客户端 JS

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
pygments_style = 'sphinx'

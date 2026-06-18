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
# sphinxcontrib-mermaid 0.9.x: client-side rendering via <script> tag (no ESM).
# Avoids the double-encoding bug in 2.0.x. Mermaid JS loaded from CDN.
mermaid_version = '11'

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
pygments_style = 'sphinx'

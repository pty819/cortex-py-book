"""Sphinx configuration for Cortex-PY Architecture Book."""

project = 'Cortex-PY 架构解析'
copyright = '2026, Technical Deep Dive'
author = 'Hermes Agent'

# -- General configuration ---------------------------------------------------
extensions = [
    'myst_parser',
    'sphinxcontrib.mermaid',
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
    'sphinx.ext.graphviz',
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
mermaid_version = 'latest'
mermaid_init_js = "mermaid.initialize({startOnLoad:true});"

# -- Options for LaTeX output ------------------------------------------------
latex_documents = [
    ('index', 'CortexPY.tex', 'Cortex-PY 架构解析',
     'Technical Deep Dive', 'manual'),
]

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
exclude_patterns = []
pygments_style = 'sphinx'

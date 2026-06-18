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
# Use CDN mermaid for client-side rendering.
mermaid_version = '11'

# -- Fix sphinxcontrib-mermaid 2.0.x double-encoding bug ---------------------
# The extension stores mermaid source via innerHTML into data-original-code.
# On re-render it reads it back — but HTML entities get double-encoded.
# Fix: monkey-patch setAttribute to decode entities for data-original-code.
_MERMAID_FIX_JS = """
(function() {
    var orig = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {
        if (name === 'data-original-code' && typeof value === 'string') {
            var ta = document.createElement('textarea');
            ta.innerHTML = value;
            value = ta.value;
        }
        return orig.call(this, name, value);
    };
})();
"""

def _inject_fix(app):
    app.add_js_file(None, body=_MERMAID_FIX_JS, priority=0)

def setup(app):
    app.connect('builder-inited', _inject_fix)
    return {'version': '0.1.0', 'parallel_read_safe': True}

# -- Other configuration -----------------------------------------------------
master_doc = 'index'
language = 'zh_CN'
pygments_style = 'sphinx'

"""Every class a template uses has a rule in app.css (or is a `js-` hook), and `[hidden]` always hides."""

import re
from pathlib import Path

import autonome_context

_ROOT = Path(autonome_context.__file__).parent
TEMPLATES = _ROOT / "templates"
CSS = (_ROOT / "static" / "app.css").read_text()

_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_TEMPLATE_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def used_classes(html: str) -> set[str]:
    out: set[str] = set()
    for attr in _CLASS_ATTR.findall(html):
        tokens = _TEMPLATE_CODE.sub(lambda m: "\0" if m.group().startswith("{{") else " ", attr).split()
        out |= {c for c in tokens if "\0" not in c and not c.startswith("js-")}  # `st--{{ x }}` is built at render
    return out


def css_classes(css: str) -> set[str]:
    return set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", re.sub(r"/\*.*?\*/", "", css, flags=re.S)))


def missing(html: str, css: str = CSS) -> set[str]:
    return used_classes(html) - css_classes(css)


def test_every_class_in_a_template_has_a_rule_in_app_css():
    gaps = {t.name: sorted(m) for t in sorted(TEMPLATES.glob("*.html")) if (m := missing(t.read_text()))}
    assert gaps == {}


def test_an_invented_class_in_a_template_is_caught():
    assert missing('<div class="btn totally-made-up">') == {"totally-made-up"}


def test_template_code_dynamic_parts_and_js_hooks_are_ignored():
    assert missing('<div class="btn{% if x %} is-on{% endif %} {{ y }} st--{{ z }} js-hook">') == set()
    assert missing('<div class="btn{% if x %} not-a-class{% endif %}">') == {"not-a-class"}


def test_every_input_in_a_template_has_a_type():
    gaps = {t.name: tags for t in sorted(TEMPLATES.glob("*.html"))
            if (tags := [m for m in re.findall(r"<input\b[^>]*>", t.read_text()) if not re.search(r"\btype=", m)])}
    assert gaps == {}


def test_settings_rows_style_input_select_and_textarea():
    rule = re.search(r"\.srow input\[type=text\][^{]*\{([^}]*)\}", CSS).group(1)
    assert ".srow select" in re.search(r"\.srow input\[type=text\][^{]*\{", CSS).group()
    assert ".srow textarea" in re.search(r"\.srow input\[type=text\][^{]*\{", CSS).group()
    assert "height: 32px" in rule and "padding: 0 10px" in rule and "border-radius: 6px" in rule


def test_hidden_always_hides():
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS)

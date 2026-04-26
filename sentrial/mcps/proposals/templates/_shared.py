"""Shared rendering helpers between brand templates.

Keeps the brand modules focused on aesthetics — palette, type, layout — and
factors out the boring escape/section/list rendering so we don't drift
between brands.
"""
from __future__ import annotations

import html as _html
from typing import Any


def esc(s: Any) -> str:
    """HTML-escape a value for safe insertion into templates. None → ''."""
    if s is None:
        return ""
    return _html.escape(str(s), quote=True)


def paragraphs(text: Any) -> str:
    """Split text on blank lines into <p> blocks. Single-line text becomes
    one <p>. Empty input returns ''."""
    if text is None:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{esc(p)}</p>" for p in parts)


def bullets(items: Any) -> str:
    """Render a <ul> from an iterable of strings. Empty/missing → ''."""
    if not items:
        return ""
    if not isinstance(items, (list, tuple)):
        return ""
    li = "\n".join(f"      <li>{esc(x)}</li>" for x in items if x)
    if not li:
        return ""
    return f"    <ul>\n{li}\n    </ul>"


def render_sections(sections: Any) -> str:
    """Sections are a list of {heading, body, bullets?} dicts. Render each as
    a <section> with optional list. Returns concatenated HTML."""
    if not isinstance(sections, list):
        return ""
    out = []
    for s in sections:
        if not isinstance(s, dict):
            continue
        heading = esc(s.get("heading") or "")
        body = paragraphs(s.get("body"))
        ul = bullets(s.get("bullets"))
        if not (heading or body or ul):
            continue
        out.append(
            f'  <section class="prop-section">\n'
            f'    <h2>{heading}</h2>\n'
            f'{body}\n'
            f'{ul}\n'
            f'  </section>'
        )
    return "\n".join(out)

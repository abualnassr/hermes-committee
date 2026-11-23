"""Model-maker logos for the PDF report.

Read from the Desktop page's own table (``desktop/plugin.js``: ``LOGOS`` and ``MAKERS``), so the page and the
report always show the same mark for the same model, and a maker is added in one place. The marks are
single-colour logos from lobe-icons (MIT; notice in ``desktop/THIRD_PARTY_NOTICES.md``). A model whose maker is
not known gets no logo.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Optional

JS = Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"


@functools.lru_cache(maxsize=1)
def _tables() -> tuple[dict, list]:
    try:
        js = JS.read_text(encoding="utf-8")
        logo_block = js.split("const LOGOS = {", 1)[1].split("\n}\n", 1)[0]
        maker_block = js.split("const MAKERS = [", 1)[1].split("]]\n", 1)[0] + "]"
    except (OSError, IndexError):
        return {}, []
    logos = {m.group(1): (m.group(2), re.findall(r'"([^"]+)"', m.group(3)))
             for m in re.finditer(r'^\s+(\w+): \["([^"]+)", \[(.*?)\]\],?\s*$', logo_block, re.M)}
    makers = []
    for key, rx in re.findall(r"\['(\w+)', /((?:\\/|[^/])+)/\]", maker_block):
        try:
            makers.append((key, re.compile(rx)))
        except re.error:
            continue
    return logos, makers


def maker_of(model: Optional[str], provider: Optional[str] = "") -> Optional[str]:
    """The maker read from the model id (never the route: GLM through OpenRouter is Z.ai). The provider is
    consulted only when no model is set, as on the Desktop page."""
    _, makers = _tables()

    def find(text: str) -> Optional[str]:
        return next((k for k, rx in makers if rx.search(text)), None)

    m = str(model or "").lower()
    return find(m) if m else find(str(provider or "").lower())


def maker_name(key: Optional[str]) -> str:
    return (_tables()[0].get(key) or ("", []))[0]


def logo_svg(key: Optional[str], rgb: tuple = (28, 28, 32), size: float = 10) -> Optional[bytes]:
    """The maker's mark as a small SVG in one colour, or None when there is no logo for it."""
    paths = (_tables()[0].get(key) or ("", []))[1] if key else []
    if not paths:
        return None
    fill = "#%02x%02x%02x" % tuple(rgb)
    body = "".join(f'<path fill="{fill}" fill-rule="evenodd" d="{d}"/>' for d in paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" height="{size}">'
            f"{body}</svg>").encode("utf-8")

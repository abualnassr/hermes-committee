"""hermes-committee core: pure logic with no Hermes imports (unit-testable with a fake LLM).

The Hermes-facing glue (tools, hooks, ctx.llm adapter, progress messages) lives in the
plugin's top-level ``__init__.py``.
"""

import re as _re
from pathlib import Path as _Path


def _read_version() -> str:
    """The version from ``plugin.yaml``, the one file a release bumps. A constant here was left at 0.4.0 through
    0.4.7, so every report said it came from 0.4.0."""
    try:
        text = (_Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        m = _re.search(r"^version:\s*['\"]?([0-9][^'\"\s#]*)", text, _re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


__version__ = _read_version()

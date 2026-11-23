"""One version everywhere: the reports print __version__, the catalog reads plugin.yaml, pip reads pyproject."""
import re
from pathlib import Path

import committee

ROOT = Path(__file__).resolve().parent.parent


def _field(path: str, pattern: str) -> str:
    m = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.M)
    assert m, path
    return m.group(1)


def test_one_version_everywhere():
    yaml_v = _field("plugin.yaml", r"^version:\s*['\"]?([0-9][^'\"\s#]*)")
    toml_v = _field("pyproject.toml", r'^version\s*=\s*"([^"]+)"')
    assert committee.__version__ == yaml_v == toml_v
    assert f"## {yaml_v}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

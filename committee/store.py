"""Run records on disk: <data>/runs/<run_id>/{run.json, report.md, report.pdf}."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def run_dir(base: Path, run_id: str) -> Path:
    if not _ID_RE.match(run_id or ""):
        raise ValueError(f"Invalid run id {run_id!r}.")
    return Path(base) / "runs" / run_id


def save(base: Path, run: dict[str, Any]) -> Path:
    d = run_dir(base, run["id"])
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".run.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, d / "run.json")
    return d / "run.json"


def load(base: Path, run_id: str) -> Optional[dict[str, Any]]:
    p = run_dir(base, run_id) / "run.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def recent(base: Path, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(base) / "runs"
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and _ID_RE.match(p.name)), reverse=True)[:limit]:
        try:
            r = json.loads((d / "run.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a damaged record must not hide the others
            continue
        out.append({"id": r["id"], "status": r.get("status"), "template": r.get("template"),
                    "question": (r.get("question") or "")[:120], "reports": r.get("reports") or {}})
    return out

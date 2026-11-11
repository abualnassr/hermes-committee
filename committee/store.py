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
        # "_" keys are transient (evidence text and picture bytes loaded for a stage) and never stored here.
        json.dump({k: v for k, v in run.items() if not k.startswith("_")}, f, ensure_ascii=False, indent=1,
                  default=str)
    os.replace(tmp, d / "run.json")
    return d / "run.json"


_DRAFT_RE = re.compile(r"^d[0-9a-f]{10}$")
DRAFT_FIELDS = ("question", "brief", "evidence", "template", "preset", "seats", "chairman", "review", "research", "research_secs",
                "deliberate",
                "chairman_guidance", "decision_focus",
                "deliberate_rounds", "max_minutes",
                "evidence_pack", "evidence_files", "max_usd", "allow_unknown_price", "title", "follow_of",
                "follow_context", "follow_evidence")


def save_draft(base: Path, draft: dict[str, Any]) -> str:
    """Store a committee set up on the Desktop page; the chat message carries only the id."""
    import secrets
    did = "d" + secrets.token_hex(5)
    d = Path(base) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    body = {k: draft.get(k) for k in DRAFT_FIELDS if draft.get(k) is not None}
    (d / f"{did}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return did


def load_draft(base: Path, draft_id: str) -> Optional[dict[str, Any]]:
    if not _DRAFT_RE.match(draft_id or ""):
        return None
    p = Path(base) / "drafts" / f"{draft_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


CANCEL_FLAG = "cancel.flag"


def request_cancel(base: Path, run_id: str) -> bool:
    """Ask a run to stop from outside the agent turn (the Desktop page). The engine sees the flag
    within half a second; no new model calls start after that."""
    d = run_dir(base, run_id)
    if not (d / "run.json").is_file():
        return False
    (d / CANCEL_FLAG).write_text("stop", encoding="utf-8")
    return True


REQUESTS_FILE = "requests.jsonl"


def add_request(base: Path, run_id: str, req: dict[str, Any]) -> bool:
    """An answer from the page to a limit warning (extend time or budget, or decline). One JSON line each;
    the engine applies the ones it has not seen, between stages and while it waits on seats."""
    d = run_dir(base, run_id)
    if not (d / "run.json").is_file():
        return False
    with open(d / REQUESTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")
    return True


def read_requests(base: Path, run_id: str) -> list[dict[str, Any]]:
    try:
        p = run_dir(base, run_id) / REQUESTS_FILE
    except ValueError:
        return []
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict):
            out.append(x)
    return out


def cancel_requested(base: Path, run_id: str) -> bool:
    try:
        return (run_dir(base, run_id) / CANCEL_FLAG).is_file()
    except ValueError:
        return False


def load(base: Path, run_id: str) -> Optional[dict[str, Any]]:
    p = run_dir(base, run_id) / "run.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def all_runs(base: Path, limit: int = 500) -> list[dict[str, Any]]:
    """Full run records, newest first. A damaged record is skipped, never fatal."""
    root = Path(base) / "runs"
    if not root.is_dir():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and _ID_RE.match(p.name)), reverse=True)[:limit]:
        try:
            out.append(json.loads((d / "run.json").read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - a damaged record must not hide the others
            continue
    return out


def recent(base: Path, limit: int = 20) -> list[dict[str, Any]]:
    return [{"id": r["id"], "status": r.get("status"), "template": r.get("template"),
             "question": (r.get("question") or "")[:120], "reports": r.get("reports") or {}}
            for r in all_runs(base, limit)]


# ── evidence packs: files added on the page, read once ──────────────────────
# <data>/evidence/<pack_id>/<fid>.json (+ <fid>-<n>.jpg for pictures). A run copies the files it
# uses into runs/<run_id>/evidence/, so later edits to the pack never change a run.

_PACK_RE = re.compile(r"^e[0-9a-f]{10}$")
_FID_RE = re.compile(r"^f[0-9a-f]{8}$")
PACK_TTL_DAYS = 7


def _pack_dir(base: Path, pack_id: str) -> Path:
    if not _PACK_RE.match(pack_id or ""):
        raise ValueError(f"Invalid evidence pack id {pack_id!r}.")
    return Path(base) / "evidence" / pack_id


def new_pack(base: Path) -> str:
    import secrets
    pid = "e" + secrets.token_hex(5)
    _pack_dir(base, pid).mkdir(parents=True, exist_ok=True)
    prune_packs(base)
    return pid


def _write_json(path: Path, obj: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def pack_files(base: Path, pack_id: str) -> list[dict[str, Any]]:
    """File summaries (no text, no bytes), oldest first."""
    d = _pack_dir(base, pack_id)
    out = []
    for f in sorted(d.glob("f*.json"), key=lambda x: x.stat().st_mtime) if d.is_dir() else []:
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append(meta["summary"])
    return out


def add_to_pack(base: Path, pack_id: str, name: str, data: bytes) -> dict[str, Any]:
    """Read one file into the pack. Returns its summary; raises evidence.EvidenceError."""
    import secrets
    from . import evidence as ev
    d = _pack_dir(base, pack_id)
    if not d.is_dir():
        raise ev.EvidenceError("This evidence list has expired. Add the files again.")
    have = pack_files(base, pack_id)
    if len(have) >= ev.MAX_FILES:
        raise ev.EvidenceError(f"A committee can read at most {ev.MAX_FILES} files.")
    if sum(h["size"] for h in have) + len(data) > ev.MAX_PACK_BYTES:
        raise ev.EvidenceError(f"All files together may be at most {ev.MAX_PACK_BYTES // (1024 * 1024)} MB.")
    f = ev.extract(name, data)
    if f["images"] and sum(h["images"] for h in have) + f["images"] > ev.MAX_IMAGES:
        raise ev.EvidenceError(f"A committee can look at most {ev.MAX_IMAGES} pictures (photos and scanned pages).")
    fid = "f" + secrets.token_hex(4)
    items = []
    for n, it in enumerate(f["items"]):
        if it["type"] == "image":
            img = d / f"{fid}-{n}.jpg"
            img.write_bytes(it["data"])
            items.append({k: v for k, v in it.items() if k != "data"} | {"file": img.name})
        else:
            items.append(it)
    summ = ev.summary({**f, "fid": fid})
    _write_json(d / f"{fid}.json", {"summary": summ, "items": items})
    return summ


def remove_from_pack(base: Path, pack_id: str, fid: str) -> bool:
    if not _FID_RE.match(fid or ""):
        raise ValueError(f"Invalid file id {fid!r}.")
    d = _pack_dir(base, pack_id)
    hit = False
    for p in [d / f"{fid}.json", *d.glob(f"{fid}-*.jpg")]:
        if p.exists():
            p.unlink()
            hit = True
    return hit


def _load_pack_file(d: Path, fid: str) -> dict[str, Any]:
    meta = json.loads((d / f"{fid}.json").read_text(encoding="utf-8"))
    items = []
    for it in meta["items"]:
        if it["type"] == "image":
            it = {**it, "data": (d / it["file"]).read_bytes()}
        items.append(it)
    return {**meta["summary"], "items": items}


def load_pack(base: Path, pack_id: str, fids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """The chosen files of a pack with their text and picture bytes, in the order they were added."""
    d = _pack_dir(base, pack_id)
    if not d.is_dir():
        raise FileNotFoundError("This evidence list has expired. Add the files again on the Committee page.")
    chosen = [s["fid"] for s in pack_files(base, pack_id)]
    if fids is not None:
        want = set(fids)
        chosen = [f for f in chosen if f in want]
    return [_load_pack_file(d, f) for f in chosen]


def attach_pack_to_run(base: Path, run: dict[str, Any], pack_id: str, fids: Optional[list[str]] = None) -> None:
    """Copy the chosen files into the run folder and index them (E1…, I1…) on the run."""
    d = _pack_dir(base, pack_id)
    chosen = [s["fid"] for s in pack_files(base, pack_id)]
    if fids is not None:
        want = set(fids)
        chosen = [f for f in chosen if f in want]
    attach_files_to_run(base, run, [_load_pack_file(d, f) for f in chosen])


def attach_files_to_run(base: Path, run: dict[str, Any], files: list[dict[str, Any]]) -> None:
    from . import evidence as ev
    if not files:
        return
    rd = run_dir(base, run["id"]) / "evidence"
    rd.mkdir(parents=True, exist_ok=True)
    keep = [it.get("_keep") or {} for f in files for it in f["items"] if it["type"] == "image"]
    secs, imgs = ev.sections(files)
    for n, i in enumerate(imgs):
        (rd / f"{i['id']}.jpg").write_bytes(i.pop("data"))
        i["file"] = f"{i['id']}.jpg"
        i["description"] = None
        i.update(keep[n] if n < len(keep) else {})      # a follow-up keeps the earlier descriptions
    _write_json(rd / "sections.json", secs)
    run["files"] = [{k: f.get(k) for k in ("fid", "name", "kind", "size", "tokens", "images", "notes", "pages", "chars")}
                    for f in files]
    run["evidence_index"] = {"sections": [{k: s[k] for k in ("id", "source", "locator")} | {"chars": len(s["text"])}
                                          for s in secs],
                             "images": imgs,
                             "tokens": sum(f.get("tokens") or 0 for f in files)}


def load_run_evidence(base: Path, run: dict[str, Any]) -> None:
    """Load the run's evidence text and picture bytes into the transient ``_sections``/``_images``."""
    idx = run.get("evidence_index") or {}
    if not idx:
        return
    rd = run_dir(base, run["id"]) / "evidence"
    try:
        run["_sections"] = json.loads((rd / "sections.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        run["_sections"] = []
    run["_images"] = {}
    for i in idx.get("images") or []:
        p = rd / i["file"]
        if p.exists():
            run["_images"][i["id"]] = {"data": p.read_bytes(), "mime": i.get("mime") or "image/jpeg"}


def prune_packs(base: Path, days: int = PACK_TTL_DAYS) -> int:
    """Delete evidence packs untouched for ``days``. Runs keep their own copies."""
    import shutil
    import time as _t
    root = Path(base) / "evidence"
    if not root.is_dir():
        return 0
    cutoff, n = _t.time() - days * 86400, 0
    for d in root.iterdir():
        if d.is_dir() and _PACK_RE.match(d.name):
            newest = max([p.stat().st_mtime for p in d.iterdir()] + [d.stat().st_mtime])
            if newest < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n



def clear_pack(base: Path, pack_id: str) -> int:
    """Remove every file of a working pack (the page's "Clear all"). Runs keep their own copies."""
    d = _pack_dir(base, pack_id)
    n = 0
    for p in list(d.glob("f*")) if d.is_dir() else []:
        if p.name.endswith(".json"):
            n += 1
        p.unlink(missing_ok=True)
    return n


def reuse_run_files(base: Path, prev: dict[str, Any]) -> list[dict[str, Any]]:
    """An earlier run's evidence as loadable files (text, picture bytes and the pictures' descriptions),
    so a follow-up can add new files to it and the old pictures are not described again."""
    idx = prev.get("evidence_index") or {}
    if not idx:
        return []
    rd = run_dir(base, prev["id"]) / "evidence"
    try:
        secs = json.loads((rd / "sections.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    out = []
    for f in prev.get("files") or []:
        items = [{"type": "text", "locator": x.get("locator", ""), "text": x["text"]}
                 for x in secs if x.get("source") == f["name"]]
        for i in idx.get("images") or []:
            p = rd / i.get("file", "")
            if i.get("source") == f["name"] and p.is_file():
                items.append({"type": "image", "locator": i.get("locator", ""), "mime": i.get("mime") or "image/jpeg",
                              "data": p.read_bytes(), "_keep": {k: i[k] for k in ("description", "described_by")
                                                                 if i.get(k)}})
        out.append({**f, "items": items})
    return out


# ── presets saved from the page ─────────────────────────────────────────────
# <data>/presets.json = {"presets": {name: preset}, "hidden": [names]}. Hermes's settings writer
# can add keys but not remove them, so presets the page saves live here, where Delete works.
# Presets in config.yaml are still read; deleting one from the page hides it ("hidden").

PRESET_RE = re.compile(r"^[^\W_][\w .()&+\-]{0,39}$")


def _presets_path(base: Path) -> Path:
    return Path(base) / "presets.json"


def page_presets(base: Path) -> dict[str, Any]:
    try:
        d = json.loads(_presets_path(base).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        d = {}
    return {"presets": d.get("presets") if isinstance(d.get("presets"), dict) else {},
            "hidden": [h for h in d.get("hidden") or [] if isinstance(h, str)]}


def _save_presets(base: Path, d: dict[str, Any]) -> None:
    Path(base).mkdir(parents=True, exist_ok=True)
    _write_json(_presets_path(base), d)


def save_preset(base: Path, name: str, preset: dict[str, Any]) -> None:
    if not PRESET_RE.match(name or ""):
        raise ValueError("A committee name starts with a letter or digit and has at most 40 characters.")
    d = page_presets(base)
    d["presets"][name] = preset
    d["hidden"] = [h for h in d["hidden"] if h != name]
    _save_presets(base, d)


def delete_preset(base: Path, name: str, in_config: bool) -> bool:
    d = page_presets(base)
    hit = d["presets"].pop(name, None) is not None
    if in_config and name not in d["hidden"]:
        d["hidden"].append(name)
        hit = True
    _save_presets(base, d)
    return hit


def effective_presets(base: Path, config_presets: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """(presets, source) — config.yaml presets minus hidden ones, then the page's (which win)."""
    d = page_presets(base)
    cfg = config_presets if isinstance(config_presets, dict) else {}
    out = {k: v for k, v in cfg.items() if k not in d["hidden"]}
    src = {k: "config" for k in out}
    for k, v in d["presets"].items():
        out[k], src[k] = v, "page"
    return out, src

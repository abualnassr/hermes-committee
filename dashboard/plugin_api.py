"""Backend routes for the Committee page in Hermes Desktop, mounted at /api/plugins/hermes-committee/.

Read-only over the run records, plus one write: a Stop request for a live run (a flag file the
engine polls). Preset edits do not come through here; the page saves them with Hermes's own
plugin-settings writer, which validates them against plugin.yaml.

Hermes loads this file on its own, outside the plugin package, so the pure ``committee`` core is
loaded by path under a private package name (it never sits on sys.path under a bare name).
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

_CORE = "hermes_committee_dashboard_core"


def _core():
    mod = sys.modules.get(_CORE)
    if mod is None:
        root = Path(__file__).resolve().parent.parent / "committee"
        spec = importlib.util.spec_from_file_location(_CORE, root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        if spec is None or spec.loader is None:
            raise ImportError(f"hermes-committee: cannot load {root}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_CORE] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(_CORE, None)
            raise
    return mod


def _mods():
    _core()
    import importlib
    return (importlib.import_module(f"{_CORE}.store"), importlib.import_module(f"{_CORE}.insights"),
            importlib.import_module(f"{_CORE}.templates"), importlib.import_module(f"{_CORE}.config"))


PLUGIN_ID = "hermes-committee"
STALE_AFTER_S = 900          # a non-finished run nobody has saved for 15 min is abandoned
router = APIRouter()


def _profile_name() -> str:
    """The Hermes profile this backend serves (…/profiles/<name>/plugin-data/… → <name>), so the page
    can start the committee's chat in the same profile."""
    d = _base()
    parts = d.parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i - 1].lower() == "profiles" and parts[i + 1] == "plugin-data":
            return parts[i]
    return "default"


def _base() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_ID)


def _entry() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    return entry if isinstance(entry, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stale(run: dict[str, Any], base: Path, store) -> bool:
    if run.get("status") in ("done", "failed", "cancelled"):
        return False
    try:
        mtime = (store.run_dir(base, run["id"]) / "run.json").stat().st_mtime
    except (OSError, ValueError):
        return False
    return time.time() - mtime > STALE_AFTER_S


@router.get("/health")
async def health():
    return {"ok": True, "plugin": PLUGIN_ID}


@router.get("/runs")
async def runs(limit: int = 200):
    store, insights, _t, _c = _mods()
    base = _base()
    out = []
    for r in store.all_runs(base, max(1, min(limit, 500))):
        row = insights.summary(r)
        row["stale"] = _stale(r, base, store)
        row["cancel_requested"] = store.cancel_requested(base, r["id"])
        out.append(row)
    return {"runs": out}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str):
    store, insights, _t, _c = _mods()
    base = _base()
    try:
        run = store.load(base, run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if run is None:
        raise HTTPException(404, f"No run {run_id}")
    history = [r for r in store.all_runs(base, 200) if r.get("status") == "done"]
    out = insights.public_run(run, history)
    out["stale"] = _stale(run, base, store)
    out["cancel_requested"] = store.cancel_requested(base, run_id)
    return out


@router.post("/runs/{run_id}/extend")
async def extend(run_id: str, body: dict):
    """Answer a limit warning: {kind: 'time'|'usd', add: seconds|USD} or {kind, decline: true}.
    The run applies it within half a second (between stages, or while it waits on seats)."""
    import uuid
    store, _i, _t, _c = _mods()
    base = _base()
    try:
        run = store.load(base, run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if run is None:
        raise HTTPException(404, f"No run {run_id}")
    if run.get("status") in ("done", "failed", "cancelled"):
        return {"ok": False, "note": "The run has already finished."}
    kind = (body or {}).get("kind")
    if kind not in ("time", "usd"):
        raise HTTPException(400, "kind must be 'time' or 'usd'")
    lim = run.get("limits") or {}
    if not lim.get("max_secs" if kind == "time" else "max_usd"):
        raise HTTPException(400, "This run has no such limit to extend.")
    req = {"id": uuid.uuid4().hex[:12], "kind": kind, "t": time.time()}
    if (body or {}).get("decline"):
        req["decline"] = True
    else:
        try:
            add = float(body.get("add"))
        except (TypeError, ValueError):
            raise HTTPException(400, "add must be a number")
        top = 3600 if kind == "time" else 500
        if not 0 < add <= top:
            raise HTTPException(400, f"add must be more than 0 and at most {top}")
        req["add"] = add
    store.add_request(base, run_id, req)
    return {"ok": True, "id": req["id"]}


@router.post("/runs/{run_id}/stop")
async def stop(run_id: str):
    """Stop a live run from the page. A run that nobody is advancing any more (its chat turn
    ended without finishing) is marked cancelled at once."""
    store, _i, _t, _c = _mods()
    base = _base()
    try:
        run = store.load(base, run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if run is None:
        raise HTTPException(404, f"No run {run_id}")
    if run.get("status") in ("done", "failed", "cancelled"):
        return {"ok": True, "status": run["status"], "note": "The run had already finished."}
    store.request_cancel(base, run_id)
    if _stale(run, base, store):
        run["status"], run["next_stage"] = "cancelled", None
        run.setdefault("timeline", []).append({"t": round(time.time(), 3), "event": "cancelled",
                                               "during": "abandoned"})
        store.save(base, run)
        return {"ok": True, "status": "cancelled", "note": "The run was abandoned; it is now marked cancelled."}
    return {"ok": True, "status": "stopping",
            "note": "Stop requested. No new model calls will start; calls already sent may still finish and be billed."}


_LLM_KEY = "plugins.entries.hermes-committee.llm"


def _allowlist(llm: dict[str, Any], key: str) -> Optional[list[str]]:
    """Mirror of Hermes's own rule (agent/plugin_llm.py): a missing / non-list value means no
    allowlist (anything passes); a list restricts to its lower-cased entries; "*" allows all."""
    raw = llm.get(key)
    if not isinstance(raw, list):
        return None
    return [x.strip().lower() for x in raw if isinstance(x, str) and x.strip()]


def _blocked(allow: Optional[list[str]], wanted: list[str]) -> list[str]:
    if allow is None or "*" in allow:
        return []
    return sorted({w for w in wanted if w and w.strip().lower() not in allow})


def _gate(entry: dict[str, Any], slots: list[dict[str, Any]]) -> dict[str, Any]:
    """What Hermes would refuse for these seats, and the exact commands that allow it.
    The plugin never grants itself access: the user runs the commands (or edits config.yaml).
    Hermes re-reads this policy on every call, so no restart is needed afterwards."""
    import json as _json
    llm = _dict(entry.get("llm"))
    providers = [str(s.get("provider") or "") for s in slots]
    models = [str(s.get("model") or "") for s in slots]
    out: dict[str, Any] = {"commands": []}
    if any(providers) and not llm.get("allow_provider_override"):
        out["commands"].append(f"hermes config set {_LLM_KEY}.allow_provider_override true")
    if any(models) and not llm.get("allow_model_override"):
        out["commands"].append(f"hermes config set {_LLM_KEY}.allow_model_override true")
    pa, ma = _allowlist(llm, "allowed_providers"), _allowlist(llm, "allowed_models")
    out["locked"] = _blocked(pa, providers)
    out["locked_models"] = _blocked(ma, models)
    if out["locked"]:
        out["commands"].append(f"hermes config set {_LLM_KEY}.allowed_providers "
                               f"'{_json.dumps(sorted(set(pa or []) | set(out['locked'])))}'")
    if out["locked_models"]:
        out["commands"].append(f"hermes config set {_LLM_KEY}.allowed_models "
                               f"'{_json.dumps(sorted(set(ma or []) | set(out['locked_models'])))}'")
    return out


def _policy(entry: dict[str, Any]) -> dict[str, Any]:
    llm = _dict(entry.get("llm"))
    return {"providers": _allowlist(llm, "allowed_providers"), "models": _allowlist(llm, "allowed_models"),
            "provider_override": bool(llm.get("allow_provider_override")),
            "model_override": bool(llm.get("allow_model_override"))}


@router.post("/drafts")
async def create_draft(body: dict):
    """Store the committee set up on the page. Checks it the same way a run will, so problems show
    on the page before anything is sent to a chat."""
    store, _i, _t, cfgmod = _mods()
    entry = _entry()
    settings = _dict(entry.get("settings"))
    settings = {**settings, "presets": store.effective_presets(_base(), settings.get("presets"))[0]}
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "Type the question the committee should decide.")
    seats = body.get("seats") if isinstance(body.get("seats"), list) else []
    try:
        roster = cfgmod.build_roster(settings, template=body.get("template"), seats=seats or None,
                                     review=body.get("review"), chairman=_dict(body.get("chairman")), exact=True,
                                     max_minutes=body.get("max_minutes"), max_usd=body.get("max_usd"),
                                     deliberate=body.get("deliberate"), deliberate_rounds=body.get("deliberate_rounds"),
                                     chairman_guidance=body.get("chairman_guidance"),
                                     decision_focus=body.get("decision_focus"))
    except cfgmod.ConfigError as exc:
        raise HTTPException(400, str(exc))
    gate = _gate(entry, [*roster["seats"], roster["chairman"]])
    if gate["commands"]:
        what = gate["locked"] + gate["locked_models"]
        return {"ok": False, **gate,
                "error": "Hermes has not allowed the committee to use "
                         + (", ".join(what) if what else "a different model for each seat") + " yet."}
    ev_check = _evidence_check(body, roster)
    if ev_check.get("error"):
        return {"ok": False, "error": ev_check["error"], "commands": [], "locked": [], "locked_models": []}
    est = _estimate_for(body, roster, settings)
    if est["unknown"] and est["limit"] and not body.get("allow_unknown_price"):
        return {"ok": False, "commands": [], "locked": [], "locked_models": [], "estimate": est, "needs_ok": "unknown_price",
                "error": f"No price is known for {', '.join(est['unknown'])}, so the budget cannot count them. "
                         "Tick “Run anyway” next to the budget, or pick models with a known price."}
    if est["limit"] and est["low"] > est["limit"]:
        return {"ok": False, "commands": [], "locked": [], "locked_models": [], "estimate": est,
                "error": f"Even a short run is estimated at {est['low_fmt']}, above your {est['limit_fmt']} budget. "
                         "Raise the budget, remove evidence, or use fewer or cheaper seats."}
    if body.get("follow_of"):
        try:
            prev = store.load(_base(), str(body["follow_of"]))
        except ValueError:
            prev = None
        if prev is None:
            raise HTTPException(400, "The earlier committee is no longer available.")
    did = store.save_draft(_base(), {**body, "question": question})
    return {"ok": True, "draft": did, "planned_calls": cfgmod.planned_calls(roster), "warnings": roster["warnings"],
            "max_secs": roster["limits"]["max_secs"], "evidence": ev_check, "estimate": est,
            "message": f"/hermes-committee:convene draft={did} {' '.join(question.split())[:240]}"}


@router.get("/standings")
async def standings():
    store, insights, _t, _c = _mods()
    return insights.standings(store.all_runs(_base(), 500))


@router.get("/setup")
async def setup():
    """Presets, templates and the provider allowlist, for the Seats editor and the Convene box."""
    store, insights, templates, cfgmod = _mods()
    entry = _entry()
    settings = _dict(entry.get("settings"))
    llm = _dict(entry.get("llm"))
    presets, sources = store.effective_presets(_base(), settings.get("presets"))
    settings = {**settings, "presets": presets}
    tpls = [{**t, "seats": templates.get_template(t["id"])["seats"]} for t in templates.list_templates()]
    known: dict[str, set[str]] = {}
    for p in presets.values():
        if not isinstance(p, dict):
            continue
        for slot in [*(p.get("seats") or []), p.get("chairman") or {}]:
            if isinstance(slot, dict) and slot.get("provider") and slot.get("model"):
                known.setdefault(slot["provider"], set()).add(slot["model"])
    allowed = [str(x) for x in llm.get("allowed_providers") or [] if isinstance(x, str)]
    rosters: dict[str, Any] = {}
    for name in presets:
        try:
            r = cfgmod.build_roster(settings, preset=name)
            rosters[name] = {"template": r["template"], "review": r["review"], "chairman": r["chairman"],
                             "deliberate": r["deliberate"], "deliberate_rounds": r["deliberate_rounds"],
                             "chairman_guidance": r["chairman_guidance"], "decision_focus": r["decision_focus"],
                             "max_minutes": (presets[name] or {}).get("max_minutes"),
                             "max_usd": (presets[name] or {}).get("max_usd"), "source": sources.get(name),
                             "seats": [{k: s[k] for k in ("name", "instructions", "provider", "model")}
                                       for s in r["seats"]]}
        except cfgmod.ConfigError as exc:
            rosters[name] = {"error": str(exc)}
    default = settings.get("default_preset") or ""
    return {"default_preset": default if default in presets else "", "presets": presets, "templates": tpls,
            "profile": _profile_name(),
            "rosters": rosters, "policy": _policy(entry),
            "stage_timeout": settings.get("stage_timeout") or cfgmod.STAGE_TIMEOUT_DEFAULT,
            "allowed_providers": allowed,
            "provider_override": bool(llm.get("allow_provider_override")),
            "model_override": bool(llm.get("allow_model_override")),
            "known_models": {k: sorted(v) for k, v in known.items()},
            "expected_secs": insights.expected_seconds(r for r in store.all_runs(_base(), 200)
                                                       if r.get("status") == "done"),
            "limits": {"min_seats": cfgmod.MIN_SEATS, "max_seats": cfgmod.MAX_SEATS,
                       "max_instructions": cfgmod.MAX_INSTRUCTIONS,
                       "max_minutes_default": _minutes(settings.get("max_minutes"), cfgmod.MAX_MINUTES_DEFAULT),
                       "max_minutes_max": cfgmod.MAX_MINUTES_MAX,
                       "max_usd_default": _minutes(settings.get("max_usd"), cfgmod.MAX_USD_DEFAULT),
                       "max_usd_max": cfgmod.MAX_USD_MAX,
                       "deliberate_default": str(settings.get("deliberate") or cfgmod.DELIBERATE_DEFAULT)}}


def _minutes(v: Any, default: int) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default


# ── evidence files ──────────────────────────────────────────────────────────

def _ev():
    _core()
    import importlib
    hc = importlib.import_module(f"{_CORE}.hostcaps")
    hc.set_base(_base())
    return importlib.import_module(f"{_CORE}.evidence"), hc


def _decode(data: Any) -> bytes:
    import base64
    if not isinstance(data, str) or not data:
        raise HTTPException(400, "No file data was sent.")
    if data.startswith("data:"):
        data = data.split(",", 1)[1] if "," in data else ""
    try:
        return base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "The file data could not be decoded.") from None


@router.post("/evidence")
async def new_evidence():
    """Start an evidence list for one committee set-up."""
    store, _i, _t, _c = _mods()
    return {"pack": store.new_pack(_base()), "limits": _evidence_limits()}


def _evidence_limits() -> dict[str, Any]:
    ev, _h = _ev()
    return {"max_file_mb": ev.MAX_FILE_BYTES // (1024 * 1024), "max_files": ev.MAX_FILES,
            "max_images": ev.MAX_IMAGES, "image_tokens": ev.IMAGE_TOKENS, "extensions": sorted(ev.SUPPORTED_EXT)}


@router.get("/evidence/{pack_id}")
async def get_evidence(pack_id: str):
    store, _i, _t, _c = _mods()
    try:
        return {"pack": pack_id, "files": store.pack_files(_base(), pack_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/evidence/{pack_id}/files")
async def add_evidence(pack_id: str, body: dict):
    """Add one file (``{name, data}``, data = base64 or a data: URL). Reads it now, once."""
    import asyncio
    store, _i, _t, _c = _mods()
    ev, _h = _ev()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "The file has no name.")
    raw = _decode(body.get("data"))
    try:
        summ = await asyncio.to_thread(store.add_to_pack, _base(), pack_id, name, raw)
    except ValueError as exc:            # EvidenceError is a ValueError; so is a bad pack id
        return {"ok": False, "name": name, "error": str(exc)}
    return {"ok": True, "file": summ}


@router.delete("/evidence/{pack_id}/files/{fid}")
async def remove_evidence(pack_id: str, fid: str):
    store, _i, _t, _c = _mods()
    try:
        return {"ok": store.remove_from_pack(_base(), pack_id, fid)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/caps")
async def model_caps(body: dict):
    """What the plugin knows about each seat's model: ``{routes: [{provider, model}]}`` →
    ``{"provider|model": {vision, context_fail, price_in, price_out, billing}}``. ``vision`` is ``false``
    only for a model that rejected a picture before; otherwise pictures are tried."""
    _ev_mod, hostcaps = _ev()
    out: dict[str, Any] = {}
    for r in (body.get("routes") or [])[:40]:
        if isinstance(r, dict):
            key = f"{r.get('provider') or ''}|{r.get('model') or ''}"
            out[key] = hostcaps.model_caps(str(r.get("provider") or ""), str(r.get("model") or ""))
    return {"caps": out}


def _evidence_check(body: dict, roster: dict) -> dict[str, Any]:
    """Pictures need a seat that can see them; the evidence must fit every seat's context window."""
    pack = body.get("evidence_pack")
    if not pack:
        return {}
    store, _i, _t, _c = _mods()
    _ev_mod, hostcaps = _ev()
    try:
        files = store.pack_files(_base(), str(pack))
    except ValueError:
        return {"error": "The evidence list is invalid. Add the files again."}
    chosen = body.get("evidence_files")
    if isinstance(chosen, list):
        files = [f for f in files if f["fid"] in set(chosen)]
    tokens = sum(f.get("tokens") or 0 for f in files)
    pics = sum(f.get("images") or 0 for f in files)
    slots = [*roster["seats"], roster["chairman"]]
    caps = [hostcaps.model_caps(s.get("provider") or "", s.get("model") or "") for s in slots]
    seeing = [s["name"] for s, c in zip(roster["seats"], caps) if hostcaps.sees_images(s, c)]
    chair_sees = hostcaps.sees_images(roster["chairman"], caps[-1])
    if pics and not seeing and not chair_sees:
        return {"error": "Your evidence includes pictures, but every model in this committee has refused "
                         "pictures before (or is set to 'Descriptions only'). Give the chairman or one seat a "
                         "model that takes pictures, or remove the photos."}
    warnings = []
    for s, c in zip(slots, caps):
        failed = c.get("context_fail")
        if failed and tokens + 6_000 >= failed * 0.9:
            who = s.get("name") or "The chairman"
            warnings.append(f"{who}'s model failed before with about {failed:,} tokens of input; this evidence is "
                            f"about {tokens:,}. It may fail again: remove some files or pick another model.")
    return {"files": len(files), "tokens": tokens, "pictures": pics, "seats_seeing": seeing,
            "chair_sees": chair_sees, "warnings": warnings}


# ── cost estimate ────────────────────────────────────────────────────────────

def _estimate_for(body: dict, roster: dict, settings: dict) -> dict[str, Any]:
    """The same estimate the run will enforce, for the page's Convene bar and the draft check."""
    store, _i, _t, _c = _mods()
    _core()
    import importlib
    engine = importlib.import_module(f"{_CORE}.engine")
    pricing = importlib.import_module(f"{_CORE}.pricing")
    _ev_mod, hostcaps = _ev()
    prev = None
    if body.get("follow_of"):
        try:
            prev = store.load(_base(), str(body["follow_of"]))
        except ValueError:
            prev = None
    brief = str(body.get("brief") or "")
    if prev is not None:
        brief = importlib.import_module(f"{_CORE}.followup").context(prev) + "\n\n" + brief
    run = engine.new_run(roster, question=str(body.get("question") or "?"), brief=brief,
                         evidence=str(body.get("evidence") or ""))
    overrides = _dict(settings.get("billing"))
    for slot in [*run["seats"], run["chairman"]]:
        caps = hostcaps.model_caps(slot.get("provider") or "", slot.get("model") or "")
        slot["price"] = hostcaps.price(slot, caps, overrides)
        slot["sees_images"] = hostcaps.sees_images(slot, caps)
    files = []
    if body.get("evidence_pack"):
        try:
            files = store.pack_files(_base(), str(body["evidence_pack"]))
        except ValueError:
            files = []
        if isinstance(body.get("evidence_files"), list):
            files = [f for f in files if f["fid"] in set(body["evidence_files"])]
    if prev is not None and body.get("follow_evidence") is not False:
        files = list(prev.get("files") or []) + files
    pics = sum(f.get("images") or 0 for f in files)
    run["evidence_index"] = {"tokens": sum(f.get("tokens") or 0 for f in files), "sections": [],
                             "images": [{"id": f"I{i + 1}", "description": None} for i in range(pics)]}
    if pics:
        run["describer"] = next((s for s in [run["chairman"], *run["seats"]] if s["sees_images"]), None)
    est = pricing.estimate(run)
    lim = pricing.limit(run)
    ctx_warn = []
    for slot in [*run["seats"], run["chairman"]]:
        failed = hostcaps.model_caps(slot.get("provider") or "", slot.get("model") or "").get("context_fail")
        if failed and est["input_tokens"] / max(1, est["calls"]) * 1.2 >= failed * 0.9:
            ctx_warn.append(f"{slot.get('name') or 'Chairman'} may get too much text: its model failed before at "
                            f"about {int(failed):,} tokens. Remove some files or pick another model.")
    return {**est, "limit": lim, "low_fmt": pricing.fmt(est["low"]), "high_fmt": pricing.fmt(est["high"]),
            "context_warnings": ctx_warn,
            "list_high_fmt": pricing.fmt(est["list_high"]), "limit_fmt": pricing.fmt(lim) if lim else None,
            "seats": {s["name"]: s["price"] for s in run["seats"]}, "chairman": run["chairman"]["price"],
            "providers": _billing_rows(run, hostcaps, overrides)}


def _billing_rows(run: dict, hostcaps: Any, overrides: dict) -> list[dict[str, Any]]:
    """One row per provider in the committee: how it is paid for and who has no price."""
    rows: dict[str, dict[str, Any]] = {}
    active = hostcaps.active_route()[0]
    low = {str(k).lower(): v for k, v in (overrides or {}).items()}
    for slot in [*run["seats"], run["chairman"]]:
        slug = (slot.get("provider") or active or "").lower()
        if not slug:
            continue
        caps = hostcaps.model_caps(slot.get("provider") or "", slot.get("model") or "")
        r = rows.setdefault(slug, {"provider": slug, "billing": slot["price"].get("billing"),
                                   "chosen": caps.get("billing_chosen", False),
                                   "detected": caps.get("billing_detected", False), "need_price": [],
                                   "in_config": low.get(slug) in ("paid", "subscription"),
                                   "who": [], "unpriced": [], "via": []})
        who = slot.get("name") or "Chairman"
        r["who"].append(who)
        if slot["price"].get("billing") == "unknown":
            r["unpriced"].append(who)
            if slot.get("model") and all(x["model"] != slot["model"] for x in r["need_price"]):
                r["need_price"].append({"model": slot["model"], "who": who})
        elif slot["price"].get("via"):
            r["via"].append(f"{who} ({slot['price']['via']})")
    return list(rows.values())


@router.post("/estimate")
async def estimate(body: dict):
    """Cost range for the committee set up on the page (no draft is saved)."""
    _store, _i, _t, cfgmod = _mods()
    settings = _dict(_entry().get("settings"))
    seats = body.get("seats") if isinstance(body.get("seats"), list) else []
    try:
        roster = cfgmod.build_roster(settings, template=body.get("template"), seats=seats or None,
                                     review=body.get("review"), chairman=_dict(body.get("chairman")), exact=True,
                                     max_minutes=body.get("max_minutes"), max_usd=body.get("max_usd"),
                                     deliberate=body.get("deliberate"), deliberate_rounds=body.get("deliberate_rounds"),
                                     chairman_guidance=body.get("chairman_guidance"),
                                     decision_focus=body.get("decision_focus"))
    except cfgmod.ConfigError as exc:
        return {"error": str(exc)}
    return _estimate_for(body, roster, settings)



# ── presets saved from the page ──────────────────────────────────────────────

@router.post("/presets")
async def save_preset(body: dict):
    """Save the committee set up on the page as a named preset (``{name, preset}``)."""
    store, _i, _t, cfgmod = _mods()
    name = str(body.get("name") or "").strip()
    preset = _dict(body.get("preset"))
    try:
        cfgmod.build_roster({"presets": {name: preset}}, preset=name)       # same checks as a run
        store.save_preset(_base(), name, preset)
    except (cfgmod.ConfigError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name}


@router.delete("/presets/{name}")
async def delete_preset(name: str):
    """Delete a preset. One from config.yaml is hidden here (config.yaml is not edited)."""
    store, _i, _t, _c = _mods()
    in_config = name in _dict(_dict(_entry().get("settings")).get("presets"))
    if not store.delete_preset(_base(), name, in_config):
        return {"ok": False, "error": f"No preset {name!r}."}
    return {"ok": True, "from_config": in_config}


@router.delete("/evidence/{pack_id}")
async def clear_evidence(pack_id: str):
    """Remove every file from the page's evidence list ("Clear all")."""
    store, _i, _t, _c = _mods()
    try:
        return {"ok": True, "removed": store.clear_pack(_base(), pack_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))



@router.post("/models/remember")
async def remember_models(body: dict):
    """The page passes the price and sign-in kind of each provider's models from Hermes's public
    ``model.options``; chat and terminal runs then price seats from this copy."""
    _ev_mod, hostcaps = _ev()
    rows = [{"slug": r.get("slug"), "auth_type": r.get("auth_type"), "pricing": r.get("pricing")}
            for r in (body.get("providers") or [])[:200] if isinstance(r, dict)]
    return {"ok": True, "priced": hostcaps.remember_providers(rows)}



@router.post("/billing")
async def set_billing(body: dict):
    """The user's answer for one provider: ``subscription`` (counts $0), ``paid``, or ``null`` to forget."""
    _ev_mod, hostcaps = _ev()
    b = body.get("billing")
    if b not in ("subscription", "paid", None):
        raise HTTPException(400, "billing must be 'subscription', 'paid' or null")
    try:
        return {"ok": True, "provider": hostcaps.set_billing(str(body.get("provider") or ""), b)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc



@router.post("/price")
async def set_price(body: dict):
    """The user's price for a pay-per-use model with no known price: ``{provider, model, in, out}``
    in USD per million tokens; ``in`` and ``out`` null forget it."""
    _ev_mod, hostcaps = _ev()
    try:
        hostcaps.set_price(str(body.get("provider") or ""), str(body.get("model") or ""), body.get("in"), body.get("out"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}

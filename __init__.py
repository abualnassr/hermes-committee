"""hermes-committee — Karpathy's LLM Council for Hermes Agent, with seat roles, evidence and reports.

Hermes surfaces used (all public): ctx.register_tool / register_hook / register_skill /
register_cli_command, ctx.llm (per-seat provider/model, gated by the user's
plugins.entries.hermes-committee.llm policy), ctx.get_config, ctx.post_message, and
plugins.plugin_storage.plugin_data_dir.

Flow: the bundled skill (/hermes-committee:convene) has the agent call ``committee_open`` once,
then ``committee_advance`` until the run is done — one stage per tool call, so no call nears
Hermes's 420 s tool limit. Stop in the chat cancels the run through the post_tool_call /
on_session_end hooks; no new model calls start after that.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .committee import __version__
from .committee import config as cconfig
from .committee import engine, report_md, store
from .committee.engine import Reply
from .committee.templates import list_templates

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-committee"
TOOL_NAMES = ("committee_open", "committee_advance")
_CTX: Any = None
_LOCK = threading.Lock()
_ACTIVE: dict[str, dict[str, Any]] = {}       # run_id -> {"cancel", "session", "handle", "busy"}
_CANCEL_STATUSES = {"cancelled", "canceled", "interrupted", "timeout", "timed_out"}
_CANCEL_ERRORS = {"tool_interrupted", "tool_timeout", "timeout"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_ID)


def _settings() -> dict[str, Any]:
    get = _CTX.get_config
    return {"default_preset": get("default_preset", ""), "presets": get("presets", {}) or {},
            "stage_timeout": get("stage_timeout", None), "max_tokens": get("max_tokens", None)}


def _usage(u: Any) -> dict[str, Any]:
    if u is None:
        return {}
    return {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cost_usd")}


class _HermesLLM:
    """engine.LLM on top of ctx.llm. Provider/model are only passed when the seat sets them,
    so an unconfigured seat runs on the user's active model with no override needed."""

    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _route(provider: str, model: str) -> dict[str, str]:
        return {k: v for k, v in (("provider", provider), ("model", model)) if v}

    def structured(self, *, provider, model, instructions, text, schema, schema_name, max_tokens, timeout, purpose):
        r = self.ctx.llm.complete_structured(
            instructions=instructions, input=[{"type": "text", "text": text}], json_schema=schema,
            schema_name=schema_name, max_tokens=max_tokens, timeout=timeout, purpose=purpose,
            **self._route(provider, model))
        return Reply(content_type=r.content_type, parsed=r.parsed, text=r.text or "", usage=_usage(r.usage),
                     provider=getattr(r, "provider", "") or "", model=getattr(r, "model", "") or "")

    def text(self, *, provider, model, instructions, text, max_tokens, timeout, purpose):
        r = self.ctx.llm.complete(
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": text}],
            max_tokens=max_tokens, timeout=timeout, purpose=purpose, **self._route(provider, model))
        return Reply(content_type="text", text=r.text or "", usage=_usage(r.usage),
                     provider=getattr(r, "provider", "") or "", model=getattr(r, "model", "") or "")


def _err(msg: str, **extra) -> str:
    return json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False)


def _toast(entry: dict[str, Any], text: str) -> None:
    """Update the run's live progress message (Desktop toast / editable chat message)."""
    try:
        h = entry.get("handle")
        if h is None or not getattr(h, "ok", False):
            entry["handle"] = _CTX.post_message(text)
        else:
            h.edit(text)
    except Exception:  # noqa: BLE001 - progress is best-effort, never fails a stage
        logger.debug("hermes-committee: progress update failed", exc_info=True)


def _entry(run: dict[str, Any], session_id: str) -> dict[str, Any]:
    with _LOCK:
        e = _ACTIVE.get(run["id"])
        if e is None:
            e = _ACTIVE[run["id"]] = {"cancel": threading.Event(), "session": session_id, "handle": None,
                                      "busy": False}
        if session_id:
            e["session"] = session_id
        return e


# ── tool: committee_open ────────────────────────────────────────────────────

OPEN_SCHEMA = {
    "name": "committee_open",
    "description": (
        "Convene a committee of AI models (Karpathy's LLM Council): seats answer a question independently, "
        "rank each other's anonymous answers, and a chairman writes the final answer, with a Markdown and "
        "PDF report. Use only for hard, contested or high-stakes questions — it makes several model calls. "
        "Load the skill hermes-committee:convene first; it explains the full procedure. After this call, "
        "call committee_advance with the returned run_id until status is done."),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question exactly as the committee should answer it."},
            "brief": {"type": "string", "description": "Context, constraints and decision criteria from the user."},
            "evidence": {"type": "string", "description": "Evidence pack (facts, data, excerpts with sources) every seat must use."},
            "preset": {"type": "string", "description": "Named preset from the user's settings. Omit for the default."},
            "template": {"type": "string", "description": "Template id: classic, expert-panel, decision-board, red-team, reliability-board."},
            "seats": {"type": "array", "description": "Custom seats when the user names the roles. Models come from the preset by position.",
                      "items": {"type": "object", "properties": {
                          "name": {"type": "string"}, "instructions": {"type": "string"},
                          "provider": {"type": "string"}, "model": {"type": "string"}},
                          "required": ["name"]}},
            "review": {"type": "boolean", "description": "Run the anonymous peer-ranking stage (default true)."},
        },
        "required": ["question"],
    },
}


def handle_open(args: dict, **kwargs) -> str:
    try:
        args = args or {}
        roster = cconfig.build_roster(_settings(), preset=args.get("preset"), template=args.get("template"),
                                      seats=args.get("seats"), review=args.get("review"))
        run = engine.new_run(roster, question=str(args.get("question") or ""), brief=str(args.get("brief") or ""),
                             evidence=str(args.get("evidence") or ""), session_id=str(kwargs.get("session_id") or ""))
    except (cconfig.ConfigError, KeyError, ValueError) as exc:
        return _err(str(exc).strip("'\""))
    store.save(_data_dir(), run)
    e = _entry(run, run["session_id"])
    _toast(e, f"⏳ Committee convened ({run['template_title']}, {len(run['seats'])} seats)")
    return json.dumps({
        "ok": True, "run_id": run["id"], "status": run["status"], "next_stage": run["next_stage"],
        "template": run["template_title"], "preset": run["preset"],
        "seats": [{"name": s["name"], "model": cconfig.model_label(s)} for s in run["seats"]],
        "chairman": cconfig.model_label(run["chairman"]), "review": run["review"], "quorum": run["quorum"],
        "planned_model_calls": cconfig.planned_calls(run), "warnings": run["warnings"],
        "next": "Call committee_advance with this run_id, and repeat until status is 'done' or 'failed'.",
    }, ensure_ascii=False)


# ── tool: committee_advance ─────────────────────────────────────────────────

ADVANCE_SCHEMA = {
    "name": "committee_advance",
    "description": ("Run the next stage of an open committee run (Stage 1 answers → Stage 2 anonymous peer "
                    "ranking → Stage 3 chairman + reports). Call repeatedly with the same run_id until "
                    "status is 'done' or 'failed'."),
    "parameters": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
}


def _render_reports(run: dict[str, Any]) -> None:
    d = store.run_dir(_data_dir(), run["id"])
    md_path = d / "report.md"
    md_path.write_text(report_md.render(run), encoding="utf-8")
    run["reports"] = {"markdown": str(md_path)}
    try:
        from .committee import report_pdf
        run["reports"]["pdf"] = str(report_pdf.render(run, d / "report.pdf"))
    except Exception as exc:  # noqa: BLE001 - the Markdown report still stands
        logger.warning("hermes-committee: PDF report failed", exc_info=True)
        run["reports"]["pdf_error"] = f"{type(exc).__name__}: {exc}"


def _final_payload(run: dict[str, Any]) -> dict[str, Any]:
    chair = run.get("chair") or {}
    seats = []
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        seats.append({"response": s["label"], "seat": s["name"], "model": cconfig.model_label(s),
                      "status": a.get("status", "not run"), "position": a.get("summary") or a.get("error"),
                      "confidence": a.get("confidence")})
    return {"ok": run["status"] == "done", "run_id": run["id"], "status": run["status"], "error": run.get("error"),
            "decision": chair.get("decision"), "confidence": chair.get("confidence"),
            "final_answer": chair.get("final_answer"), "dissent": chair.get("dissent"),
            "next_actions": chair.get("next_actions"), "seats": seats, "ranking": run.get("ranking"),
            "reports": run.get("reports"), "usage": engine.usage_totals(run), "warnings": run.get("warnings")}


def handle_advance(args: dict, **kwargs) -> str:
    run_id = str((args or {}).get("run_id") or "").strip()
    base = _data_dir()
    try:
        run = store.load(base, run_id)
    except ValueError as exc:
        return _err(str(exc))
    if run is None:
        return _err(f"No committee run {run_id!r}.")
    if run["status"] in ("done", "failed", "cancelled"):
        return json.dumps(_final_payload(run), ensure_ascii=False)

    e = _entry(run, str(kwargs.get("session_id") or ""))
    with _LOCK:
        if e["busy"]:
            return _err("This run is already advancing.")
        e["busy"] = True
    title = run["template_title"]
    try:
        if e["cancel"].is_set():
            raise engine.Cancelled()
        stage = engine.advance(run, _HermesLLM(_CTX), e["cancel"], lambda m: _toast(e, f"⏳ Committee · {m}"))
    except engine.Cancelled:
        run["status"], run["next_stage"] = "cancelled", None
        store.save(base, run)
        _toast(e, "⏹ Committee cancelled. No further model calls will start.")
        with _LOCK:
            _ACTIVE.pop(run_id, None)
        return json.dumps({"ok": False, "run_id": run_id, "status": "cancelled"})
    finally:
        with _LOCK:
            e["busy"] = False

    if run["status"] in ("chaired", "failed"):
        _render_reports(run)
        if run["status"] == "chaired":
            run["status"] = "done"
        _toast(e, ("✅ Committee done · report ready" if run["status"] == "done"
                   else f"⚠ Committee stopped: {run.get('error')}"))
        store.save(base, run)
        with _LOCK:
            _ACTIVE.pop(run_id, None)
        return json.dumps(_final_payload(run), ensure_ascii=False)

    store.save(base, run)
    if stage == "stage1":
        body = {"answered": sum(1 for a in run["answers"].values() if a["status"] == "ok"), "of": len(run["seats"]),
                "seats": [{"seat": s["name"], "status": run["answers"][s["label"]]["status"],
                           "position": run["answers"][s["label"]].get("summary") or run["answers"][s["label"]].get("error")}
                          for s in run["seats"]]}
    else:
        body = {"reviews": sum(1 for r in run["reviews"].values() if r["status"] == "ok"),
                "ranking": run["ranking"]}
    _toast(e, f"⏳ Committee ({title}) · {stage} done, continuing")
    return json.dumps({"ok": True, "run_id": run_id, "stage_completed": stage, "status": run["status"],
                       "next_stage": run["next_stage"], **body,
                       "next": "Call committee_advance again with the same run_id."}, ensure_ascii=False)


# ── cancel hooks (Stop in the chat) ─────────────────────────────────────────

def _cancel_session(session_id: str, why: str) -> None:
    if not session_id:
        return
    with _LOCK:
        hits = [(rid, e) for rid, e in _ACTIVE.items() if e["session"] == session_id]
    for rid, e in hits:
        e["cancel"].set()
        logger.info("hermes-committee: cancelling run %s (%s)", rid, why)
        if not e["busy"]:
            try:
                run = store.load(_data_dir(), rid)
                if run and run["status"] not in ("done", "failed", "cancelled"):
                    run["status"], run["next_stage"] = "cancelled", None
                    run["timeline"].append({"t": engine._now(), "event": "cancelled", "during": "between stages"})
                    store.save(_data_dir(), run)
                    _toast(e, "⏹ Committee cancelled.")
            except Exception:  # noqa: BLE001
                logger.debug("hermes-committee: cancel bookkeeping failed", exc_info=True)
            with _LOCK:
                _ACTIVE.pop(rid, None)


def _on_post_tool_call(tool_name=None, session_id=None, status=None, error_type=None, **_kw):
    if tool_name in TOOL_NAMES and (str(status or "").lower() in _CANCEL_STATUSES
                                    or str(error_type or "").lower() in _CANCEL_ERRORS):
        _cancel_session(str(session_id or ""), f"tool {status}/{error_type}")


def _on_session_end(session_id=None, interrupted=None, **_kw):
    if interrupted:
        _cancel_session(str(session_id or ""), "turn interrupted")


# ── CLI: hermes committee ... ───────────────────────────────────────────────

def _cli_setup(sub):
    subs = sub.add_subparsers(dest="committee_cmd")
    subs.add_parser("templates", help="List built-in templates")
    subs.add_parser("presets", help="List your presets and their seats")
    subs.add_parser("runs", help="List recent runs and their reports")
    p = subs.add_parser("doctor", help="Check that every seat of a preset can answer (one tiny call per seat)")
    p.add_argument("--preset", default=None)
    p = subs.add_parser("run", help="Run a committee from the terminal (headless; works in scripts and cron)")
    p.add_argument("question")
    p.add_argument("--preset", default=None)
    p.add_argument("--template", default=None)
    p.add_argument("--brief", default="")
    p.add_argument("--evidence-file", default=None, help="Text/Markdown file used as the evidence pack")
    p.add_argument("--lite", action="store_true", help="Skip Stage 2 (peer ranking)")


def _cli(args) -> int:
    cmd = getattr(args, "committee_cmd", None)
    if cmd == "templates":
        for t in list_templates():
            print(f"{t['id']:<18} {t['title']}\n{'':<18} {t['description']}\n{'':<18} seats: {', '.join(t['seats'])}")
        return 0
    if cmd == "presets":
        s = _settings()
        if not s["presets"]:
            print("No presets. Add one under plugins.entries.hermes-committee.settings.presets (see README).")
        for name in s["presets"]:
            try:
                r = cconfig.build_roster(s, preset=name)
            except cconfig.ConfigError as exc:
                print(f"{name}: INVALID — {exc}")
                continue
            star = " (default)" if name == s["default_preset"] else ""
            print(f"{name}{star} · {r['template_title']} · chairman {cconfig.model_label(r['chairman'])}")
            for seat in r["seats"]:
                print(f"   - {seat['name']:<28} {cconfig.model_label(seat)}")
        return 0
    if cmd == "runs":
        for r in store.recent(_data_dir()):
            print(f"{r['id']}  {r['status']:<9} {r['template']:<17} {r['question']}")
            for k, v in (r["reports"] or {}).items():
                print(f"{'':<22}{k}: {v}")
        return 0
    if cmd == "doctor":
        return _doctor(getattr(args, "preset", None))
    if cmd == "run":
        return _cli_run(args)
    print("Usage: hermes committee {templates|presets|runs|doctor|run}")
    return 1


def _cli_run(args) -> int:
    evidence = ""
    if args.evidence_file:
        evidence = Path(args.evidence_file).read_text(encoding="utf-8")
    req = {"question": args.question, "brief": args.brief, "evidence": evidence, "preset": args.preset,
           "template": args.template, "review": False if args.lite else None}
    opened = json.loads(handle_open({k: v for k, v in req.items() if v not in (None, "")}, session_id=""))
    if not opened.get("ok"):
        print(f"Could not open: {opened.get('error')}")
        return 1
    print(f"Run {opened['run_id']} · {opened['template']} · {opened['planned_model_calls']['total']} model calls planned")
    for w in opened.get("warnings") or []:
        print(f"Warning: {w}")
    out: dict[str, Any] = {}
    while True:
        out = json.loads(handle_advance({"run_id": opened["run_id"]}, session_id=""))
        if out.get("stage_completed"):
            print(f"  ✓ {out['stage_completed']} ({out.get('answered', out.get('reviews'))} ok)")
            continue
        break
    print(f"\nStatus: {out.get('status')}" + (f" — {out['error']}" if out.get("error") else ""))
    if out.get("decision"):
        print(f"Decision: {out['decision']}\nConfidence: {out.get('confidence')}")
    for k, v in (out.get("reports") or {}).items():
        print(f"{k}: {v}")
    u = out.get("usage") or {}
    print(f"Model calls: {u.get('calls')} · tokens in {u.get('input_tokens')} out {u.get('output_tokens')}"
          f" · cost {u.get('cost_usd') if u.get('cost_usd') is not None else 'not reported'}")
    return 0 if out.get("status") == "done" else 1


def _doctor(preset: str | None) -> int:
    from concurrent.futures import ThreadPoolExecutor
    try:
        r = cconfig.build_roster(_settings(), preset=preset)
    except cconfig.ConfigError as exc:
        print(f"Preset problem: {exc}")
        return 1
    slots = [(s["name"], s) for s in r["seats"]] + [("Chairman", r["chairman"])]
    llm = _HermesLLM(_CTX)

    def ping(item):
        name, slot = item
        try:
            rep = llm.text(provider=slot.get("provider", ""), model=slot.get("model", ""), instructions="Reply with OK.",
                           text="ping", max_tokens=256, timeout=120, purpose="hermes-committee.doctor")
            return name, slot, bool(rep.text.strip()), ""
        except Exception as exc:  # noqa: BLE001
            return name, slot, False, f"{type(exc).__name__}: {exc}"

    print(f"Preset: {r['preset'] or '(none — template defaults on the active model)'} · {r['template_title']}")
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        results = list(pool.map(ping, slots))
    bad = 0
    for name, slot, ok, err in results:
        bad += not ok
        print(f"  {'OK ' if ok else 'FAIL'} {name:<28} {cconfig.model_label(slot)}" + (f"\n       {err[:300]}" if err else ""))
    if bad:
        print("\nIf a failure mentions allowed_providers or allow_*_override, update "
              "plugins.entries.hermes-committee.llm in config.yaml (see README → Permissions).")
    for w in r["warnings"]:
        print(f"Warning: {w}")
    return 1 if bad else 0


# ── registration ────────────────────────────────────────────────────────────

def register(ctx):
    global _CTX
    _CTX = ctx
    ctx.register_tool(name="committee_open", toolset="committee", schema=OPEN_SCHEMA, handler=handle_open,
                      description="Convene an AI committee (LLM Council)", emoji="🏛")
    ctx.register_tool(name="committee_advance", toolset="committee", schema=ADVANCE_SCHEMA,
                      handler=handle_advance, description="Run the next committee stage", emoji="🏛")
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_cli_command(name="committee", help="hermes-committee: templates, presets, runs, doctor",
                             setup_fn=_cli_setup, handler_fn=_cli)
    skills = Path(__file__).parent / "skills"
    for child in sorted(skills.iterdir()) if skills.is_dir() else []:
        if (child / "SKILL.md").is_file():
            ctx.register_skill(child.name, child / "SKILL.md")
    logger.debug("hermes-committee %s registered", __version__)

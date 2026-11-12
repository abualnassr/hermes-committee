"""hermes-committee — a committee of AI models for hard decisions, with roles, evidence, debate and reports.

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

import contextvars
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .committee import followup  # noqa: E402
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
    presets, _src = store.effective_presets(_data_dir(), get("presets", {}) or {})
    default = get("default_preset", "") or ""
    return {"default_preset": default if default in presets else "", "presets": presets,
            "stage_timeout": get("stage_timeout", None), "max_tokens": get("max_tokens", None),
            "max_minutes": get("max_minutes", None), "max_usd": get("max_usd", None),
            "billing": get("billing", {}) or {}}


def _usage(u: Any) -> dict[str, Any]:
    if u is None:
        return {}
    return {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cost_usd")}


# Thinking levels ride on auxiliary tasks this plugin registers (Hermes folds a task's reasoning_effort into
# the call). Filled by register(); empty on a Hermes without plugin tasks, and calls then go out as before.
_THINK_LEVELS = ("low", "medium", "high")
_THINK_TASKS: dict[str, str] = {}


def _register_thinking(ctx) -> None:
    reg = getattr(ctx, "register_auxiliary_task", None)
    if not callable(reg):
        return
    for level in _THINK_LEVELS:
        key = f"committee_think_{level}"
        try:
            reg(key, display_name=f"Committee: {level} thinking",
                description=(f"Used by hermes-committee for calls set to {level} thinking. The committee picks "
                             "each seat's provider and model itself; only the reasoning level comes from here."),
                defaults={"reasoning_effort": level})
        except Exception:  # noqa: BLE001 - thinking levels are optional; the run works without them
            logger.debug("hermes-committee: could not register %s", key, exc_info=True)
            continue
        _THINK_TASKS[level] = key


class _HermesLLM:
    """engine.LLM on top of ctx.llm. Provider/model are only passed when the seat sets them,
    so an unconfigured seat runs on the user's active model with no override needed."""

    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _route(provider: str, model: str, thinking: str = "default") -> dict[str, str]:
        """Provider/model override, plus the plugin's own auxiliary task for a thinking level. Hermes applies
        that task's ``reasoning_effort`` to the call; the seat's provider and model still choose the model.
        A task with no explicit model would fall back to the task's own routing, so an unset seat gets the
        active model spelled out."""
        out = {k: v for k, v in (("provider", provider), ("model", model)) if v}
        task = _THINK_TASKS.get(thinking)
        if task:
            if not (provider and model):
                from .committee import hostcaps
                ap, am = hostcaps.active_route()
                full = {"provider": provider or ap, "model": model or am}
                if full["provider"] and full["model"]:
                    out = full
            if out.get("provider") and out.get("model"):
                out["task"] = task
        return out

    def structured(self, *, provider, model, instructions, text, schema, schema_name, max_tokens, timeout, purpose,
                   images=(), thinking="default"):
        blocks = [{"type": "text", "text": text}] + [
            {"type": "image", "data": i["data"], "mime_type": i.get("mime_type") or "image/jpeg"} for i in images or ()]
        r = self.ctx.llm.complete_structured(
            instructions=instructions, input=blocks, json_schema=schema,
            schema_name=schema_name, max_tokens=max_tokens, timeout=timeout, purpose=purpose,
            **self._route(provider, model, thinking))
        return Reply(content_type=r.content_type, parsed=r.parsed, text=r.text or "", usage=_usage(r.usage),
                     provider=getattr(r, "provider", "") or "", model=getattr(r, "model", "") or "")

    # Research first: Hermes's own web tools (the search provider the user set up), dispatched by the plugin.
    def _tool(self, name: str, args: dict) -> str:
        dispatch = getattr(self.ctx, "dispatch_tool", None)
        if not callable(dispatch):
            return json.dumps({"error": "this Hermes version has no plugin tool dispatch"})
        try:
            out = dispatch(name, args)
            if "Unknown tool" in str(out)[:200]:
                # `hermes committee run` (a terminal command) does not load Hermes's tools the way a chat
                # does; load the built-in ones once and try again. Chat and page runs never get here.
                import model_tools  # noqa: F401  (Hermes: registers the built-in tools)
                out = dispatch(name, args)
        except Exception as exc:  # noqa: BLE001 - research is optional; the run goes on without it
            return json.dumps({"error": f"{name} failed: {exc}"[:300]})
        return out if isinstance(out, str) else json.dumps(out, default=str)

    def web_search(self, query: str, limit: int) -> str:
        return self._tool("web_search", {"query": query, "limit": int(limit)})

    def web_extract(self, urls: list) -> str:
        return self._tool("web_extract", {"urls": list(urls)})

    def text(self, *, provider, model, instructions, text, max_tokens, timeout, purpose, thinking="default"):
        r = self.ctx.llm.complete(
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": text}],
            max_tokens=max_tokens, timeout=timeout, purpose=purpose, **self._route(provider, model, thinking))
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


class _CancelSignal(threading.Event):
    """Set by Stop in the chat (hooks) or by the Desktop page's Stop button (a flag file in the
    run folder). The engine polls ``is_set()`` every half second while it waits on seats."""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def is_set(self) -> bool:  # noqa: D401
        if super().is_set():
            return True
        if store.cancel_requested(_data_dir(), self.run_id):
            self.set()
            return True
        return False

    def requests(self) -> list:
        """Extend / decline answers sent from the Committee page (see engine.apply_requests)."""
        return store.read_requests(_data_dir(), self.run_id)


def _publish(run: dict[str, Any]) -> None:
    """Persist the run and tell the Desktop page it changed. Best-effort: never fails a stage."""
    try:
        store.save(_data_dir(), run)
    except Exception:  # noqa: BLE001
        logger.debug("hermes-committee: live save failed", exc_info=True)
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, "run.updated", {"run_id": run["id"], "status": run.get("status")})
    except Exception:  # noqa: BLE001 - no Desktop attached, or an older Hermes
        logger.debug("hermes-committee: run event not delivered", exc_info=True)


def _entry(run: dict[str, Any], session_id: str) -> dict[str, Any]:
    with _LOCK:
        e = _ACTIVE.get(run["id"])
        if e is None:
            e = _ACTIVE[run["id"]] = {"cancel": _CancelSignal(run["id"]), "session": session_id, "handle": None,
                                      "busy": False}
        if session_id:
            e["session"] = session_id
        return e


# ── tool: committee_open ────────────────────────────────────────────────────

OPEN_SCHEMA = {
    "name": "committee_open",
    "description": (
        "Use when the user asks, in any words, to convene a committee or council on a question (often with "
        "attached files). "
        "Convene a committee of AI models: seats answer a question independently, "
        "rank each other's anonymous answers, and a chairman writes the final answer, with a Markdown and "
        "PDF report. Use only for hard, contested or high-stakes questions — it makes several model calls. "
        "Load the skill hermes-committee:convene first; it explains the full procedure. After this call, "
        "call committee_advance with the returned run_id until status is done."),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question exactly as the committee should answer it."},
            "brief": {"type": "string", "description":
                      "Context, constraints and decision criteria the user stated in this conversation. Never add "
                      "facts from your memory or profile of the user, or from other sessions, unless the user asks: "
                      "the brief goes to every seat's provider. Ignored when `draft` is given."},
            "evidence": {"type": "string", "description": "Evidence pack (facts, data, excerpts with sources) every seat must use."},
            "preset": {"type": "string", "description": "Named preset from the user's settings. Omit for the default."},
            "template": {"type": "string", "description": "Template id: classic, expert-panel, decision-board, red-team, reliability-board."},
            "seats": {"type": "array", "description": "Custom seats when the user names the roles. Models come from the preset by position.",
                      "items": {"type": "object", "properties": {
                          "name": {"type": "string"}, "instructions": {"type": "string"},
                          "provider": {"type": "string"}, "model": {"type": "string"},
                          "thinking": {"type": "string", "enum": ["default", "low", "medium", "high"],
                                       "description": "How long this seat thinks before answering. Omit for medium."}},
                          "required": ["name"]}},
            "review": {"type": "boolean", "description": "Run the anonymous peer-ranking stage (default true)."},
            "research": {"type": "boolean", "description":
                         "Research first: before the seats answer, search the web once (Hermes's own web search) "
                         "and add short passages with their sources to the evidence, shared by every seat. Use it "
                         "when the question depends on current facts, prices, standards or recent events. Only the "
                         "question is sent to the search provider, never the files. Default false."},
            "research_secs": {"type": "integer", "enum": [45, 90, 150], "description":
                              "Most seconds Research first may take (not counting the source review). Default 90."},
            "deliberate": {"type": "string", "enum": ["material", "action", "always", "never"],
                           "description": "Let the members debate the points they disagree on before the chairman "
                                          "decides. material (default): when they differ on the action or on a claim "
                                          "the decision rests on; action: only when they recommend different "
                                          "actions; always; never. Set only when the user asks."},
            "chairman_guidance": {"type": "string", "description":
                                  "What the chairman's verdict must contain, when the user says so (e.g. 'rank the "
                                  "options by cost'). Omit to use the committee type's brief."},
            "decision_focus": {"type": "boolean", "description":
                               "true: every seat must end with a clear recommendation (a decision). false: an answer "
                               "to a question. Omit to use the committee type's mode."},
            "deliberate_rounds": {"type": "integer", "enum": [1, 2],
                                  "description": "Debate rounds (default 1). 2 only when the user asks."},
            "evidence_files": {"type": "array", "items": {"type": "string"},
                               "description": "Local paths of files the user attached or named (text, PDF, Word, Excel, photos). Every seat reads them; photos go to seats whose model can see pictures."},
            "evidence_paths": {"type": "array", "items": {"type": "string"},
                               "description": "With a draft only: local paths of files attached in this chat (the draft already holds the page's files)."},
            "title": {"type": "string", "description": "A short name for this committee (shown in History and on the report). Only if the user gives one."},
            "follow_of": {"type": "string", "description": "run_id of an earlier committee this one follows up (\"convene again\", \"ask the committee again\"). Its verdict is given to the seats as background and its evidence files are reused unless you pass new ones; omit seats/template to keep the same committee."},
            "max_usd": {"type": "number", "description": "Budget for the whole run in USD (0 = no limit). Omit to use the user's default ($2). Set only when the user names a budget."},
            "max_minutes": {"type": "number", "description": "Time limit for the whole run in minutes (0 = no limit). Omit to use the user's default (10). Set only when the user asks."},
            "draft": {"type": "string", "description": "Draft id from the Committee page (`draft=<id>` in the user's "
                                                        "message). The draft holds the question, seats, models and "
                                                        "evidence; pass only the id, plus brief/evidence you add."},
        },
        "required": [],
    },
}


def _from_draft(args: dict) -> dict:
    """Merge a Committee-page draft into the tool arguments (the page's choices win for seats/models)."""
    d = store.load_draft(_data_dir(), str(args["draft"]))
    if d is None:
        raise cconfig.ConfigError(f"Draft {args['draft']!r} was not found. Open the Committee page and press Convene again.")
    merged = dict(args)
    # The page's set-up is used exactly: a brief, evidence text or files the chat agent adds are ignored,
    # so nothing reaches the seats that the user did not put on the page (an agent could otherwise add
    # facts from its memory of the user, and they would go to every seat's provider). Noted in the run.
    chat_paths = [*(args.get("evidence_paths") or []), *(args.get("evidence_files") or [])]
    merged["_ignored"] = [k for k, v in (("brief", args.get("brief")), ("evidence", args.get("evidence")),
                                         ("files", chat_paths)) if v and (str(v).strip() if isinstance(v, str) else True)]
    merged.pop("evidence_files", None)
    merged["evidence_paths"] = []
    merged["question"] = d.get("question") or args.get("question") or ""
    merged["evidence"] = d.get("evidence") or ""
    merged["brief"] = d.get("brief") or ""
    for k in ("template", "seats", "chairman", "review", "research", "research_secs", "deliberate", "deliberate_rounds", "chairman_guidance",
              "decision_focus", "preset", "max_minutes",
              "max_usd", "evidence_pack",
              "title", "follow_of", "follow_context", "follow_evidence"):
        if d.get(k) not in (None, "", []):
            merged[k] = d[k]
    if d.get("evidence_pack") and isinstance(d.get("evidence_files"), list):
        merged["evidence_files"] = d["evidence_files"]          # the ticked files, even when none are ticked
    return merged


def _follow_source(args: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The earlier run a follow-up builds on; without seats/template given, its committee is kept."""
    fid = str(args.get("follow_of") or "").strip()
    if not fid:
        return None
    try:
        prev = store.load(_data_dir(), fid)
    except ValueError:
        prev = None
    if prev is None:
        raise cconfig.ConfigError(f"No earlier committee {fid!r} to follow up.")
    if not args.get("seats") and not args.get("template") and not args.get("preset"):
        args["template"] = prev.get("template")
        args["seats"] = [{k: s[k] for k in ("name", "instructions", "provider", "model", "vision", "thinking") if k in s}
                         for s in prev.get("seats") or []]
        args["chairman"] = {k: v for k, v in (prev.get("chairman") or {}).items() if k in ("provider", "model", "thinking")}
        if args.get("review") is None:
            args["review"] = prev.get("review")
        if args.get("research") is None and prev.get("research"):
            args["research"] = True
        if prev.get("custom_brief"):
            args.setdefault("chairman_guidance", prev.get("chairman_guidance"))
            args.setdefault("decision_focus", prev.get("decision_focus"))
        if args.get("deliberate") in (None, "") and prev.get("deliberate"):
            args["deliberate"] = prev.get("deliberate")
            args.setdefault("deliberate_rounds", prev.get("deliberate_rounds"))
    return prev


def _set_reserves(roster: dict[str, Any]) -> None:
    """Time kept back for the chairman and the blind vote: the slowest of the last runs on these models
    (median would under-reserve), clamped to 60–300 s. Falls back to the engine defaults."""
    lim = roster.setdefault("limits", {})
    if not lim.get("max_secs"):
        return
    try:
        from .committee import insights
        runs = [r for r in store.all_runs(_data_dir(), limit=20) if r.get("status") == "done"]
        chair_key = cconfig.model_label(roster["chairman"])
        chair, review, answer = [], [], []
        for r in runs:
            secs = insights.stage_seconds(r)
            if secs.get("stage1"):
                answer.append(max(secs["stage1"].values()))
            if cconfig.model_label(r.get("chairman") or {}) == chair_key and "chair" in secs.get("chair", {}):
                chair.append(secs["chair"]["chair"])
            if secs.get("review"):
                review.append(max(secs["review"].values()))
        if chair:
            lim["chair_reserve"] = int(min(300, max(60, max(chair[:5]) * 1.2)))
        if review:
            lim["review_reserve"] = int(min(300, max(60, max(review[:5]) * 1.2)))
        if answer and roster.get("research"):
            lim["answer_reserve"] = int(min(240, max(45, max(answer[:5]) * 1.2)))
    except Exception:  # noqa: BLE001 - reserves are an optimisation; defaults stay
        logger.debug("hermes-committee: could not size time reserves", exc_info=True)


_SECRET_NAMES = {"auth.json", "auth.lock", "webhook_subscriptions.json", "credentials", "credentials.json",
                 ".netrc", ".pgpass", ".git-credentials", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
_SECRET_DIRS = {"mcp-tokens", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"}


def _hermes_roots() -> list[Path]:
    """The active Hermes home (and the shared root above ``profiles/``), found from this plugin's
    own data dir ``<home>/plugin-data/<id>/`` so no core module is imported."""
    home = _data_dir().resolve().parent.parent
    roots = [home]
    if home.parent.name == "profiles":
        roots.append(home.parent.parent)
    return roots


def _refuse_secret_path(p: Path) -> None:
    """Fail closed on paths a model names: nothing inside a Hermes home except this plugin's data,
    and no known credential file anywhere. Symlinks are resolved first."""
    rp = p.resolve(strict=True)
    name = rp.name.lower()
    parts = {x.lower() for x in rp.parts[:-1]}
    if (name in _SECRET_NAMES or name.startswith(".env") or ("oauth" in name and name.endswith(".json"))
            or name.endswith((".pem", ".p12", ".pfx", ".kdbx")) or parts & _SECRET_DIRS):
        raise PermissionError(f"{p.name}: refused, this looks like a credential file. Committee evidence "
                              "must be your own documents; attach them on the Committee page.")
    data = _data_dir().resolve()
    for home in _hermes_roots():
        if rp.is_relative_to(home) and not rp.is_relative_to(data):
            raise PermissionError(f"{p.name}: refused, it is inside Hermes's own folder. Attach evidence "
                                  "on the Committee page or give a path outside Hermes's folder.")


def _read_paths(paths: list[Any], trusted: bool = False) -> list[dict[str, Any]]:
    """``trusted`` only for paths the user typed (``hermes committee run``); paths that arrive
    through the ``committee_open`` tool come from a model and are checked by ``_refuse_secret_path``."""
    from .committee import evidence as ev
    files = []
    for n, raw in enumerate(paths or []):
        p = Path(str(raw)).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Evidence file not found: {p}. Attach it again or give its full path.")
        if not trusted:
            _refuse_secret_path(p)
        if p.stat().st_size > ev.MAX_FILE_BYTES:
            raise ValueError(f"{p.name}: over the {ev.MAX_FILE_BYTES // (1024 * 1024)} MB limit per file.")
        try:
            f = ev.extract(p.name, p.read_bytes())
        except ev.EvidenceError as exc:
            raise ValueError(f"{p.name}: {exc}") from exc
        files.append({**f, "fid": f"p{n:07d}"})
    if len(files) > ev.MAX_FILES:
        raise ValueError(f"A committee can read at most {ev.MAX_FILES} files.")
    return files


def _attach_evidence(run: dict[str, Any], args: dict[str, Any], prior: Optional[list] = None,
                     trusted_paths: bool = False) -> None:
    """Evidence files from the page (a pack) and/or the chat (paths) → run copy; then decide who
    sees the pictures. A committee with pictures but no model that can see them is refused."""
    from .committee import evidence as ev
    from .committee import hostcaps
    base = _data_dir()
    files: list[dict[str, Any]] = list(prior or [])
    if args.get("evidence_pack"):
        fids = args.get("evidence_files") if isinstance(args.get("evidence_files"), list) else None
        files += store.load_pack(base, str(args["evidence_pack"]), fids)
    paths = args.get("evidence_paths") or ([] if args.get("evidence_pack") else args.get("evidence_files"))
    if isinstance(paths, list) and paths:
        files += _read_paths(paths, trusted=trusted_paths)
    if not files:
        return
    if sum(f["images"] for f in files) > ev.MAX_IMAGES:
        raise ValueError(f"A committee can look at most {ev.MAX_IMAGES} pictures (photos and scanned pages).")
    store.attach_files_to_run(base, run, files)
    _route_pictures(run)


def _route_pictures(run: dict[str, Any]) -> None:
    from .committee import hostcaps
    for slot in [*run["seats"], run["chairman"]]:
        slot["sees_images"] = hostcaps.sees_images(slot)
    if run["evidence_index"]["images"]:
        seeing = [{k: s.get(k) for k in ("name", "provider", "model")}
                  for s in [run["chairman"], *run["seats"]] if s["sees_images"]]
        uniq = list({(d.get("provider"), d.get("model")): d for d in seeing}.values())
        run["describer"], run["describer_alts"] = (uniq[0] if uniq else None), uniq[1:3]
        undescribed = [i for i in run["evidence_index"]["images"] if i.get("description") is None]
        if run["describer"] is None and undescribed:      # a follow-up reuses the earlier descriptions
            raise ValueError("The evidence includes pictures, but none of this committee's models is known to see "
                             "pictures. Give the chairman or one seat a vision model (or mark it 'Can see photos' "
                             "in the seat editor), or remove the photos.")


def _price_slots(run: dict[str, Any]) -> None:
    """Each seat, the chairman and the describer get their list price and billing type."""
    from .committee import hostcaps, pricing
    overrides = _settings().get("billing") or {}
    for slot in [*run["seats"], run["chairman"]]:
        slot["price"] = hostcaps.price(slot, overrides=overrides)
    if run.get("describer"):
        src = next((s for s in [run["chairman"], *run["seats"]] if s.get("model") == run["describer"].get("model")
                    and s.get("provider") == run["describer"].get("provider")), None)
        run["describer"]["price"] = (src or {}).get("price") or hostcaps.price(run["describer"], overrides=overrides)
    run["estimate"] = pricing.estimate(run)
    unknown = run["estimate"]["unknown"]
    if unknown and pricing.limit(run) is not None:
        run["warnings"].append(f"No price is known for {', '.join(unknown)}: their calls are not counted "
                               "toward the budget. Enter the price in the Committee page's budget menu, or mark "
                               "the provider Subscription there.")


def _cli_cost(u: dict[str, Any]) -> str:
    from .committee.pricing import fmt
    if u.get("est_charged") is None:
        return f" · cost {u.get('cost_usd') if u.get('cost_usd') is not None else 'not reported'}"
    out = f" · estimated cost {fmt(u['est_charged'])}"
    if (u.get("est_list") or 0) > u["est_charged"]:
        out += f" ({fmt(u['est_list'])} at list price)"
    if u.get("limit_usd"):
        out += f" of a {fmt(u['limit_usd'])} budget"
    if u.get("est_unknown_calls"):
        out += f"; {u['est_unknown_calls']} unpriced call(s) not counted"
    return out


def _budget_line(run: dict[str, Any]) -> str:
    from .committee import pricing
    est, lim = run.get("estimate") or {}, pricing.limit(run)
    if not est:
        return ""
    line = f"estimated {pricing.fmt(est['low'])}–{pricing.fmt(est['high'])}"
    line += f" (limit {pricing.fmt(lim)})" if lim else " (no budget limit)"
    if est.get("subscription"):
        line += f"; on a subscription, not counted: {', '.join(est['subscription'])}"
    return line


def _evidence_line(run: dict[str, Any]) -> str:
    files = run.get("files") or []
    if not files:
        return ""
    pics = len((run.get("evidence_index") or {}).get("images") or [])
    seeing = [s["name"] for s in run["seats"] if s.get("sees_images")]
    line = f"{len(files)} file(s), about {run['evidence_index'].get('tokens', 0):,} tokens per call"
    if pics:
        line += (f"; {pics} picture(s): seen by {len(seeing)} of {len(run['seats'])} seats, described for the rest")
    return line


def handle_open(args: dict, **kwargs) -> str:
    try:
        args = args or {}
        if args.get("draft"):
            args = _from_draft(args)
        if not str(args.get("question") or "").strip():
            return _err("A committee needs a question.")
        prev = _follow_source(args)
        roster = cconfig.build_roster(_settings(), preset=args.get("preset"), template=args.get("template"),
                                      seats=args.get("seats"), review=args.get("review"),
                                      chairman=args.get("chairman"), exact=bool(args.get("draft")),
                                      max_minutes=args.get("max_minutes"), max_usd=args.get("max_usd"),
                                      deliberate=args.get("deliberate"), deliberate_rounds=args.get("deliberate_rounds"),
                                      chairman_guidance=args.get("chairman_guidance"),
                                      decision_focus=args.get("decision_focus"),
                                      research=args.get("research") if isinstance(args.get("research"), bool) else None,
                                      research_secs=args.get("research_secs"))
        _set_reserves(roster)
        brief = str(args.get("brief") or "")
        if prev is not None and args.get("follow_context") is not False:
            brief = "\n\n".join(x for x in (followup.context(prev), brief) if x.strip())
        run = engine.new_run(roster, question=str(args.get("question") or ""), brief=brief,
                             evidence=str(args.get("evidence") or ""), session_id=str(kwargs.get("session_id") or ""))
        if args.get("draft"):
            run["draft"] = str(args["draft"])
            if args.get("_ignored"):
                run["agent_added_ignored"] = list(args["_ignored"])
        run["title"] = followup.clean_title(args.get("title"))
        prior = store.reuse_run_files(_data_dir(), prev) if prev is not None and args.get("follow_evidence") is not False else []
        _attach_evidence(run, args, prior, trusted_paths=kwargs.get("_cli_trusted") is True)
        if prev is not None:
            run["follow_of"] = prev["id"]
            run["follow_prev"] = followup.prev_summary(prev)
        _price_slots(run)
    except (cconfig.ConfigError, KeyError, ValueError, FileNotFoundError, PermissionError) as exc:
        return _err(str(exc).strip("'\""))
    _publish(run)
    e = _entry(run, run["session_id"])
    _toast(e, f"⏳ Committee convened ({run['template_title']}, {len(run['seats'])} seats)")
    return json.dumps({
        "ok": True, "run_id": run["id"], "status": run["status"], "next_stage": run["next_stage"],
        "template": run["template_title"], "preset": run["preset"],
        "seats": [{"name": s["name"], "model": cconfig.model_label(s)} for s in run["seats"]],
        "chairman": cconfig.model_label(run["chairman"]), "review": run["review"], "quorum": run["quorum"],
        "deliberate": run["deliberate"], "deliberate_rounds": run["deliberate_rounds"], "research": run["research"],
        "research_secs": run.get("research_secs"),
        "planned_model_calls": cconfig.planned_calls(run), "warnings": run["warnings"],
        "evidence": _evidence_line(run),
        "budget": _budget_line(run),
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
    run["reports"] = {"markdown": md_path.as_posix()}
    try:
        from .committee import report_pdf
        run["reports"]["pdf"] = report_pdf.render(run, d / "report.pdf").as_posix()
    except Exception as exc:  # noqa: BLE001 - the Markdown report still stands
        logger.warning("hermes-committee: PDF report failed", exc_info=True)
        msg = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, ImportError):
            msg += (" — this Hermes process started before the plugin's PDF dependencies were installed. "
                    "Restart the gateway (`hermes gateway restart`), then run "
                    f"`hermes committee report {run['id']}` to create the PDF without new model calls.")
        run["reports"]["pdf_error"] = msg


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
    store.load_run_evidence(base, run)
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
        def progress(m: str) -> None:
            _toast(e, f"⏳ Committee · {m}")
            _publish(run)

        stage = engine.advance(run, _HermesLLM(_CTX), e["cancel"], progress)
    except engine.Cancelled:
        run["status"], run["next_stage"] = "cancelled", None
        if not any(t.get("event") == "cancelled" for t in run["timeline"]):
            run["timeline"].append({"t": engine._now(), "event": "cancelled", "during": "between stages"})
        _publish(run)
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
        _publish(run)
        with _LOCK:
            _ACTIVE.pop(run_id, None)
        return json.dumps(_final_payload(run), ensure_ascii=False)

    _publish(run)
    if stage == "stage1":
        body = {"answered": sum(1 for a in run["answers"].values() if a["status"] == "ok"), "of": len(run["seats"]),
                "seats": [{"seat": s["name"], "status": run["answers"][s["label"]]["status"],
                           "position": run["answers"][s["label"]].get("summary") or run["answers"][s["label"]].get("error")}
                          for s in run["seats"]]}
    elif stage == "deliberate":
        d = run.get("deliberation") or {}
        body = {"deliberation": {"skipped": d.get("skipped"),
                                 "disputes": [{"id": x["id"], "level": x["level"], "point": x["point"]}
                                              for x in d.get("disputes") or []],
                                 "rounds": len(d.get("rounds") or []),
                                 "changed_minds": [f"{m['seat']} on {m['dispute']}" for m in d.get("moves") or []]}}
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
                    _publish(run)
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
    p.add_argument("--evidence-file", action="append", default=None,
                   help="Evidence file (text, PDF, Word, Excel or photo); repeat for several")
    p.add_argument("--evidence-dir", default=None, help="Folder whose supported files are all used as evidence")
    p.add_argument("--lite", action="store_true", help="Skip Stage 2 (peer ranking)")
    p.add_argument("--research", action="store_true", help="Search the web once before the seats answer (Research first)")
    p.add_argument("--research-secs", type=int, choices=[45, 90, 150], default=None, help="Most seconds the research may take (default 90)")
    p.add_argument("--deliberate", choices=["material", "action", "always", "never"], default=None,
                   help="Debate disputed points before the verdict (default: material)")
    p.add_argument("--rounds", type=int, choices=[1, 2], default=None, help="Deliberation rounds (default 1)")
    p.add_argument("--max-minutes", type=float, default=None, help="Time limit for the whole run (0 = no limit)")
    p.add_argument("--max-usd", type=float, default=None, help="Budget for the whole run in USD (0 = no limit)")
    p.add_argument("--title", default=None, help="A short name for this committee")
    p.add_argument("--follow-of", default=None, help="run_id of an earlier committee to follow up (same seats; its verdict as background)")
    p = subs.add_parser("report", help="Re-render a finished run's Markdown and PDF reports (no model calls)")
    p.add_argument("run_id")


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
    if cmd == "report":
        run = store.load(_data_dir(), args.run_id)
        if run is None or run["status"] not in ("done", "failed"):
            print(f"No finished run {args.run_id!r}.")
            return 1
        _render_reports(run)
        store.save(_data_dir(), run)
        for k, v in run["reports"].items():
            print(f"{k}: {v}")
        return 0 if "pdf" in run["reports"] else 1
    print("Usage: hermes committee {templates|presets|runs|doctor|run|report}")
    return 1


def _cli_run(args) -> int:
    from .committee import evidence as ev
    paths = list(args.evidence_file or [])
    if args.evidence_dir:
        paths += sorted(str(p) for p in Path(args.evidence_dir).rglob("*")
                        if p.is_file() and p.suffix.lower() in ev.SUPPORTED_EXT)
    req = {"question": args.question, "brief": args.brief, "evidence_paths": paths, "preset": args.preset,
           "template": args.template, "review": False if args.lite else None, "research": True if args.research else None, "research_secs": args.research_secs, "max_minutes": args.max_minutes, "max_usd": args.max_usd,
           "title": args.title, "follow_of": args.follow_of,
           "deliberate": getattr(args, "deliberate", None), "deliberate_rounds": getattr(args, "rounds", None)}
    opened = json.loads(handle_open({k: v for k, v in req.items() if v not in (None, "")}, session_id="",
                                     _cli_trusted=True))
    if not opened.get("ok"):
        print(f"Could not open: {opened.get('error')}")
        return 1
    print(f"Run {opened['run_id']} · {opened['template']} · {opened['planned_model_calls']['total']} model calls planned")
    if opened.get("evidence"):
        print(f"Evidence: {opened['evidence']}")
    if opened.get("budget"):
        print(f"Cost: {opened['budget']}")
    for w in opened.get("warnings") or []:
        print(f"Warning: {w}")
    out: dict[str, Any] = {}
    seen_w: set[str] = set()
    while True:
        out = json.loads(handle_advance({"run_id": opened["run_id"]}, session_id=""))
        if out.get("stage_completed"):
            run_now = store.load(_data_dir(), opened["run_id"]) or {}
            why = (run_now.get("skipped") or {}).get(out["stage_completed"])
            if why:
                print(f"  – {out['stage_completed']} skipped ({'budget' if why == 'budget' else 'time limit'})")
            else:
                if out["stage_completed"] == "deliberate":
                    dd = out.get("deliberation") or {}
                    print(f"  ✓ deliberate ({dd.get('skipped') or str(dd.get('rounds')) + ' round(s)'})")
                else:
                    print(f"  ✓ {out['stage_completed']} ({out.get('answered', out.get('reviews'))} ok)")
            for w in (run_now.get("warnings") or [])[len(opened.get("warnings") or []):]:
                if w not in seen_w:
                    seen_w.add(w)
                    print(f"  Note: {w}")
            continue
        break
    print(f"\nStatus: {out.get('status')}" + (f" — {out['error']}" if out.get("error") else ""))
    if out.get("decision"):
        print(f"Decision: {out['decision']}\nConfidence: {out.get('confidence')}")
    for k, v in (out.get("reports") or {}).items():
        print(f"{k}: {v}")
    u = out.get("usage") or {}
    print(f"Model calls: {u.get('calls')} · tokens in {u.get('input_tokens')} out {u.get('output_tokens')}"
          + _cli_cost(u))
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
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:   # context copy per task: see research.py
        results = [f.result() for f in [pool.submit(contextvars.copy_context().run, ping, it) for it in slots]]
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
    try:
        from .committee import hostcaps
        hostcaps.set_base(_data_dir())
    except Exception:  # noqa: BLE001 - model notes are optional
        logger.debug("hermes-committee: no data dir for model notes", exc_info=True)
    _register_thinking(ctx)
    ctx.register_tool(name="committee_open", toolset="committee", schema=OPEN_SCHEMA, handler=handle_open,
                      description="Convene a committee of AI models", emoji="🏛")
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

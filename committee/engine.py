"""The committee pipeline: Stage 1 (independent answers) → Stage 2 (anonymous peer ranking)
→ Stage 3 (chairman). Code drives the stages; models never skip or reorder them.

Each call to :func:`advance` runs exactly one stage and returns. The Hermes glue exposes that
as one tool call per stage, so no single tool call approaches Hermes's 420 s tool limit.

The LLM is injected (see :class:`LLM`), so the whole pipeline is testable without Hermes.
"""
from __future__ import annotations

import contextvars
import random
import re
import string
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from . import hostcaps, pricing, prompts
from .config import model_label

STAGES = ("stage1", "review", "deliberate", "chair")
_POLL_S = 0.5


@dataclass
class Reply:
    content_type: str                  # "json" or "text"
    parsed: Any = None
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""


class LLM(Protocol):
    def structured(self, *, provider: str, model: str, instructions: str, text: str,
                   schema: dict, schema_name: str, max_tokens: int, timeout: float, purpose: str,
                   images: list[dict[str, Any]] = ...) -> Reply: ...

    def text(self, *, provider: str, model: str, instructions: str, text: str,
             max_tokens: int, timeout: float, purpose: str) -> Reply: ...


class Cancelled(Exception):
    pass


Progress = Callable[[str], None]


def _now() -> float:
    return round(time.time(), 3)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ── run creation ─────────────────────────────────────────────────────────────

def new_run(roster: dict[str, Any], *, question: str, brief: str = "", evidence: str = "",
            session_id: str = "", rng: Optional[random.Random] = None) -> dict[str, Any]:
    rng = rng or random.Random()
    q = (question or "").strip()
    if not q:
        raise ValueError("A question is required.")
    n = len(roster["seats"])
    labels = rng.sample(list(string.ascii_uppercase[:n]), n)   # anonymous labels, not seat order
    seats = [{**s, "label": labels[i]} for i, s in enumerate(roster["seats"])]
    return {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "version": 1,
        "created": _now(),
        "session_id": session_id,
        "status": "open",
        "next_stage": "stage1",
        "question": q,
        "brief": (brief or "").strip(),
        "evidence": (evidence or "").strip(),
        "custom_brief": bool(roster.get("custom_brief")),
        **{k: roster[k] for k in ("preset", "template", "template_title", "chairman_guidance", "decision_focus",
                                  "chairman", "review", "quorum", "stage_timeout", "max_tokens", "warnings")},
        "deliberate": roster.get("deliberate") or "never",
        "deliberate_rounds": 2 if roster.get("deliberate_rounds") == 2 else 1,
        "deliberation": None,
        "limits": {"max_secs": None, "chair_reserve": CHAIR_RESERVE_DEFAULT, "review_reserve": REVIEW_RESERVE_DEFAULT,
                   **(roster.get("limits") or {})},
        "deadline": None,
        "skipped": {},
        "seats": seats,
        "answers": {},
        "reviews": {},
        "ranking": [],
        "chair": None,
        "usage": [],
        "timeline": [{"t": _now(), "event": "opened"}],
        "error": None,
        "reports": {},
    }


def seat_by_label(run: dict[str, Any], label: str) -> dict[str, Any]:
    return next(s for s in run["seats"] if s["label"] == label)


# ── parallel execution with deadline + cancel ────────────────────────────────

# ── extending the limits during a run (Desktop page) ───────────────────────
# A run convened from the page can be given more time or budget while it runs. The page writes
# requests (store.add_request); the engine applies them between stages and while it waits on seats.
# Before a stage would be skipped or cut, the engine pauses up to ASK_WAIT_S for an answer; paused
# time does not count against the limit; no answer = decline; after a decline it does not ask again
# about that limit. Runs from chat never pause (nobody can answer there).
ASK_WAIT_S = 45
EXT_MAX_SECS, EXT_MAX_USD = 3600, 500.0
_LIVE = contextvars.ContextVar("committee_live", default=None)


def interactive(run: dict[str, Any], cancel: Any = None) -> bool:
    return bool(run.get("draft")) and callable(getattr(cancel, "requests", None))


def apply_requests(run: dict[str, Any], cancel: Any) -> list[str]:
    """Apply the page's answers not yet applied. Returns the kinds ('time', 'usd') that changed."""
    reader = getattr(cancel, "requests", None)
    if not callable(reader):
        return []
    try:
        reqs = reader() or []
    except Exception:  # noqa: BLE001 - a bad file must not stop the run
        return []
    seen = run.setdefault("requests_applied", [])
    asks = run.setdefault("limit_asks", {})
    lim = run.setdefault("limits", {})
    changed = []
    for r in reqs:
        rid, kind = str(r.get("id") or ""), r.get("kind")
        if not rid or rid in seen or kind not in ("time", "usd"):
            continue
        seen.append(rid)
        if r.get("decline"):
            asks[kind] = "declined"
            run["timeline"].append({"t": _now(), "event": "limit_declined", "kind": kind})
            changed.append(kind)
            continue
        try:
            add = float(r.get("add") or 0)
        except (TypeError, ValueError):
            continue
        if kind == "time" and lim.get("max_secs"):
            add = max(0.0, min(add, EXT_MAX_SECS - float(lim["max_secs"])))
            if add <= 0:
                continue
            lim["max_secs"] = float(lim["max_secs"]) + add
            if run.get("deadline"):
                run["deadline"] += add
        elif kind == "usd" and lim.get("max_usd"):
            add = round(max(0.0, min(add, EXT_MAX_USD - float(lim["max_usd"]))), 4)
            if add <= 0:
                continue
            lim["max_usd"] = round(float(lim["max_usd"]) + add, 4)
        else:
            continue
        asks[kind] = "extended"
        run.setdefault("extensions", []).append({"t": _now(), "kind": kind, "add": add})
        run["timeline"].append({"t": _now(), "event": "limit_extended", "kind": kind, "add": add})
        changed.append(kind)
    return changed


def _ask(run: dict[str, Any], cancel: Any, progress: "Progress", kind: str, why: str, need: float) -> bool:
    """Pause for the user to extend `kind` before something is skipped or cut. True = extended."""
    if not interactive(run, cancel):
        return False
    asks = run.setdefault("limit_asks", {})
    changed = apply_requests(run, cancel)
    if kind in changed and asks.get(kind) == "extended":
        return True
    if asks.get(kind) == "declined":
        return False
    t0 = _now()
    before = dict(run.get("live") or {})
    run["live"] = {**before, "ask": {"kind": kind, "why": why, "need": round(max(0.0, need), 4),
                                     "since": t0, "until": t0 + ASK_WAIT_S}}
    run["timeline"].append({"t": t0, "event": "limit_ask", "kind": kind, "why": why})
    progress(f"Paused · {why} · extend on the Committee page, or it continues in {ASK_WAIT_S} s")
    got = False
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            if kind in apply_requests(run, cancel):
                got = asks.get(kind) == "extended"
                break
            if _now() - t0 >= ASK_WAIT_S:
                asks[kind] = "declined"
                run["timeline"].append({"t": _now(), "event": "limit_declined", "kind": kind, "auto": True})
                break
            time.sleep(_POLL_S)
    finally:
        waited = _now() - t0
        lim = run.setdefault("limits", {})
        lim["paused"] = round(float(lim.get("paused") or 0) + waited, 1)
        if run.get("deadline"):
            run["deadline"] += waited                     # paused time does not count
        run["live"] = before
    progress("Extended · continuing" if got else "Continuing within the limit")
    return got


def _fit_usd(run, cancel, progress, calls: Callable[[], list], what: str) -> bool:
    """Do these calls fit what is left of the budget? Asks to extend when they do not."""
    while True:
        over, need = _over(run, calls())
        if not over:
            return True
        if not _ask(run, cancel, progress, "usd", f"{what} does not fit the budget left", need - (pricing.left(run) or 0)):
            return False


def _fit_time(run, cancel, progress, stage: str, least: float, what: str) -> float:
    """This stage's window; asks to extend when it is shorter than `least`."""
    while True:
        window = stage_window(run, stage)
        if not run.get("deadline") or window >= least:
            return window
        if not _ask(run, cancel, progress, "time", f"{what} does not fit the time left", least - window):
            return window


def _grow(started: float) -> float:
    """For _parallel: seconds added to the time limit by the page since the last check."""
    ctx = _LIVE.get()
    if not ctx:
        return 0.0
    run, cancel = ctx
    if not interactive(run, cancel):
        return 0.0
    before = float((run.get("limits") or {}).get("max_secs") or 0)
    apply_requests(run, cancel)
    return float((run.get("limits") or {}).get("max_secs") or 0) - before


# ── time limit ──────────────────────────────────────────────────────────────
CHAIR_RESERVE_DEFAULT, REVIEW_RESERVE_DEFAULT = 90, 90   # seconds kept back for later stages
MIN_STAGE_S = 30            # a stage given less than this is skipped, not started


def _limit_minutes(run: dict[str, Any]) -> str:
    secs = (run.get("limits") or {}).get("max_secs") or 0
    return f"{secs / 60:g}-minute"


def stage_window(run: dict[str, Any], stage: str, now: Optional[float] = None,
                 reserve_review: bool = True) -> float:
    """Seconds this stage may use: the per-stage cap, shortened so that the later stages
    (the chairman always, the blind vote while it is still planned) keep their reserve.
    Never longer than the time left before the run's deadline."""
    cap = float(run["stage_timeout"])
    deadline = run.get("deadline")
    if not deadline:
        return cap
    lim = run.get("limits") or {}
    left = deadline - (_now() if now is None else now)
    after = 0.0
    if stage in ("stage1", "review", "deliberate"):
        after += float(lim.get("chair_reserve") or CHAIR_RESERVE_DEFAULT)
    if stage == "stage1" and reserve_review and run.get("review") and not run.setdefault("skipped", {}).get("review"):
        after += float(lim.get("review_reserve") or REVIEW_RESERVE_DEFAULT)
    return max(0.0, min(cap, left - after))


def _parallel(jobs: dict[str, Callable[[], Any]], *, timeout: float, cancel: threading.Event,
              on_done: Callable[[str, bool], None], why: str = "",
              into: Optional[dict[str, tuple[bool, Any]]] = None) -> dict[str, tuple[bool, Any]]:
    """Run jobs concurrently. Never waits past ``timeout`` and stops waiting on cancel.
    Workers that are still running are abandoned (their results are discarded).
    ``into``: a dict to fill, so ``on_done`` can read a finished job's value as it lands."""
    results: dict[str, tuple[bool, Any]] = into if into is not None else {}
    if not jobs:
        return results
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="committee")
    try:
        futs = {pool.submit(contextvars.copy_context().run, fn): key for key, fn in jobs.items()}
        pending = set(futs)
        start = time.monotonic()
        deadline = start + timeout
        ctx = _LIVE.get()
        cap = start + max(timeout, float(ctx[0].get("stage_timeout") or timeout)) if ctx else deadline
        while pending:
            if cancel.is_set():
                raise Cancelled()
            added = _grow(start)
            if added > 0:
                deadline = min(cap, deadline + added)
            left = deadline - time.monotonic()
            if left <= 0:
                break
            done, pending = wait(pending, timeout=min(_POLL_S, left), return_when=FIRST_COMPLETED)
            for f in done:
                key = futs[f]
                try:
                    results[key] = (True, f.result())
                except Exception as exc:  # noqa: BLE001 - a seat failure must not sink the stage
                    results[key] = (False, f"{type(exc).__name__}: {_clip(str(exc), 300)}")
                on_done(key, results[key][0])
        for f in pending:
            results[futs[f]] = (False, f"deadline: no answer within {int(timeout)} s"
                                + (f" ({why})" if why else ""))
        return results
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _record_usage(run: dict[str, Any], stage: str, who: str, slot: dict[str, Any], reply: Reply, secs: float):
    u = reply.usage or {}
    charged, lst, known = pricing.cost(slot, u.get("input_tokens"), u.get("output_tokens"))
    if known and isinstance(u.get("cost_usd"), (int, float)) and (slot.get("price") or {}).get("billing") == "paid":
        charged = float(u["cost_usd"])                     # the provider's own figure wins when it sends one
    with _USAGE_LOCK:
        run["usage"].append({"stage": stage, "who": who, "provider": reply.provider or slot.get("provider") or "",
                             "model": reply.model or slot.get("model") or "",
                             "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                             "cost_usd": u.get("cost_usd"), "cost_est": round(charged, 6), "cost_list": round(lst, 6),
                             "cost_known": known, "secs": round(secs, 1)})


_USAGE_LOCK = threading.Lock()


def _record_abandoned(run: dict[str, Any], stage: str, calls: dict[str, tuple], results: dict[str, tuple]) -> None:
    """A call cut off by the time limit may still finish at the provider and be billed: count it at its
    worst case (full input, maximum output), marked as an estimate."""
    for key, (slot, who, tin, tmax) in calls.items():
        ok, val = results.get(key, (True, None))
        if ok or not str(val).startswith("deadline"):
            continue
        charged, lst, known = pricing.cost(slot, tin, tmax)
        with _USAGE_LOCK:
            run["usage"].append({"stage": stage, "who": who, "provider": slot.get("provider") or "",
                                 "model": slot.get("model") or "", "input_tokens": None, "output_tokens": None,
                                 "cost_usd": None, "cost_est": round(charged, 6), "cost_list": round(lst, 6),
                                 "cost_known": known, "abandoned": True, "secs": None})


def _over(run: dict[str, Any], calls: list[tuple]) -> tuple[bool, float]:
    """Would these calls, at their worst, go over what is left of the budget?"""
    room = pricing.left(run)
    need = pricing._sum(calls)["charged"]
    return (room is not None and need > room + 1e-9), need


def _budget_gate_stage1(run: dict[str, Any]) -> Optional[str]:
    """Before any call: answers + picture descriptions + the chairman's share must fit, lowering the
    output cap (not below FLOOR_TOKENS) if needed. Returns an error to fail with, or None."""
    if pricing.limit(run) is None:
        return None
    full = int(run["max_tokens"])
    steps = [full] + [m for m in pricing.CUT_STEPS if m < full]
    for mt in steps:
        calls = pricing.describe_calls(run) + pricing.stage1_calls(run, mt) + [pricing.chair_call(run, mt, with_review=False)]
        over, need = _over(run, calls)
        if not over:
            if mt < full:
                run["budget_tokens"] = mt
                run["warnings"].append(f"To stay within the {pricing.fmt(pricing.limit(run))} limit, each answer was "
                                       f"limited to {mt:,} output tokens (normally {full:,}).")
            return None
    return (f"Not started: this committee could cost up to {pricing.fmt(need)}, above your "
            f"{pricing.fmt(pricing.limit(run))} limit, even with shorter answers. Raise the limit, remove "
            "evidence, use fewer seats or cheaper models.")




def _call_structured(llm: LLM, run, stage, who, slot, instructions, text, schema, name, images=None,
                     fallback_text=None, max_tokens=None) -> tuple[Optional[dict], str, bool]:
    """Structured call, one re-ask on a schema miss, then plain-text fallback. Images ride only on the
    structured calls; the plain-text fallback uses ``fallback_text`` (the described version) instead.
    Returns (parsed_or_None, raw_text, used_fallback)."""
    kw = dict(provider=slot.get("provider") or "", model=slot.get("model") or "",
              max_tokens=int(max_tokens or run.get("budget_tokens") or run["max_tokens"]),
              timeout=float(run["stage_timeout"]), purpose=f"hermes-committee.{stage}")
    extra = {"images": images} if images else {}
    for attempt in range(2):
        t0 = time.monotonic()
        try:
            r = llm.structured(instructions=instructions, text=text, schema=schema, schema_name=name, **kw, **extra)
        except Exception as exc:  # noqa: BLE001
            if hostcaps.looks_too_long(exc):
                hostcaps.learn(slot.get("provider") or "", slot.get("model") or "",
                               context_fail=pricing.tok(instructions) + pricing.tok(text)
                               + len(images or []) * pricing.IMAGE_TOKENS)
                raise RuntimeError(f"the evidence is too long for this model ({exc})") from exc
            if images and fallback_text and hostcaps.looks_like_image_rejection(exc):
                # This model does not take pictures: remember it, and answer from the descriptions.
                hostcaps.learn(slot.get("provider") or "", slot.get("model") or "", vision=False)
                slot["sees_images"] = False
                slot["image_rejected"] = True
                text, extra, images = fallback_text, {}, None
                continue
            raise
        _record_usage(run, stage, who, slot, r, time.monotonic() - t0)
        if r.content_type == "json" and isinstance(r.parsed, dict):
            return r.parsed, r.text or "", False
        if attempt == 0:
            instructions = instructions + " Respond with JSON that matches the schema exactly."
    t0 = time.monotonic()
    r = llm.text(instructions=instructions, text=fallback_text if images and fallback_text else text, **kw)
    _record_usage(run, stage, who, slot, r, time.monotonic() - t0)
    return None, r.text or "", True


# ── pictures: who sees them, and descriptions for everyone else ─────────────

def picture_inputs(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Image blocks in I1… order, from the bytes the host loaded into ``run['_images']``."""
    raw = run.get("_images") or {}
    return [{"data": raw[i["id"]]["data"], "mime_type": raw[i["id"]]["mime"]}
            for i in (run.get("evidence_index") or {}).get("images") or [] if i["id"] in raw]


def _undescribed(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in (run.get("evidence_index") or {}).get("images") or []
            if i.get("description") is None and not i.get("describe_error")]


def _description_text(parsed: Optional[dict], raw: str) -> str:
    if not isinstance(parsed, dict):
        return raw.strip()
    out = str(parsed.get("description") or "").strip()
    if str(parsed.get("visible_text") or "").strip():
        out += f"\nText in the picture: {parsed['visible_text'].strip()}"
    if str(parsed.get("unclear") or "").strip():
        out += f"\nUnclear: {parsed['unclear'].strip()}"
    return out.strip()


def run_describe(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress) -> None:
    """One call per picture on the describer model (a vision model), so seats that cannot see
    pictures, the blind vote and the chairman all read the same written account."""
    slot = run.get("describer")
    todo = _undescribed(run)
    raw = run.get("_images") or {}
    if not slot:
        for i in todo:
            i["describe_error"] = "no vision model in this committee"
        return
    lim = run.get("limits") or {}
    if lim.get("max_secs") and not run.get("deadline"):
        run["deadline"] = _now() + lim["max_secs"]
    window = stage_window(run, "stage1", reserve_review=False)
    run["live"] = {"stage": "stage1", "started": _now(), "done": {}, "ends": _now() + window,
                   "describing": len(todo)}
    progress(f"Preparing evidence · describing {len(todo)} picture{'s' if len(todo) > 1 else ''}")

    def job(img):
        def go():
            instr, text = prompts.describe(run, img)
            block = raw.get(img["id"])
            if not block:
                raise RuntimeError("picture file missing")
            last = None
            for cand in [slot, *(run.get("describer_alts") or [])]:
                try:
                    parsed, txt, _fb = _call_structured(
                        llm, run, "describe", f"Describer {img['id']}", cand, instr, text, prompts.DESCRIBE_SCHEMA,
                        "committee_picture", images=[{"data": block["data"], "mime_type": block["mime"]}],
                        max_tokens=pricing.DESCRIBE_MAX)
                except Exception as exc:  # noqa: BLE001
                    if not hostcaps.looks_like_image_rejection(exc):
                        raise
                    hostcaps.learn(cand.get("provider") or "", cand.get("model") or "", vision=False)
                    last = exc
                    continue
                desc = _description_text(parsed, txt)
                if not desc:
                    raise RuntimeError("empty description")
                return desc, model_label(cand)
            raise RuntimeError(f"no model in this committee accepted the picture ({last})")
        return go

    results = _parallel({i["id"]: job(i) for i in todo}, timeout=window, cancel=cancel,
                        on_done=lambda *_: None, why="time limit" if window < run["stage_timeout"] else "")
    for i in todo:
        ok, val = results.get(i["id"], (False, "not run"))
        if ok:
            i["description"], i["described_by"] = val
        else:
            i["describe_error"] = str(val)
    run["timeline"].append({"t": _now(), "event": "describe",
                            "described": sum(1 for i in todo if i.get("description")), "of": len(todo)})


# ── Stage 1 ─────────────────────────────────────────────────────────────────

def _as_list(v: Any) -> list[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _norm_answer(parsed: Optional[dict], raw: str) -> dict[str, Any]:
    if parsed is None:
        first = next((ln.strip("#* ").strip() for ln in raw.splitlines() if ln.strip()), "")
        return {"answer": raw.strip(), "summary": _clip(first, 300), "confidence": None, "key_points": [],
                "assumptions": [], "risks": [], "would_change_mind": "", "evidence_gaps": []}
    conf = parsed.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = None
    return {"answer": str(parsed.get("answer") or "").strip(), "summary": str(parsed.get("summary") or "").strip(),
            "confidence": conf, "key_points": _as_list(parsed.get("key_points")),
            "assumptions": _as_list(parsed.get("assumptions")), "risks": _as_list(parsed.get("risks")),
            "would_change_mind": str(parsed.get("would_change_mind") or "").strip(),
            "evidence_gaps": _as_list(parsed.get("evidence_gaps"))}


def run_stage1(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress) -> None:
    n = len(run["seats"])
    done = [0]

    def job(seat):
        def go():
            see = bool(seat.get("sees_images")) and bool(pictures)
            instr, text = prompts.stage1(run, seat, see_images=see)
            t0 = time.monotonic()
            parsed, raw, fb = _call_structured(llm, run, "stage1", seat["name"], seat, instr, text,
                                               prompts.STAGE1_SCHEMA, "committee_answer",
                                               images=pictures if see else None,
                                               fallback_text=prompts.stage1(run, seat)[1] if see else None)
            ans = _norm_answer(parsed, raw)
            if not ans["answer"]:
                raise RuntimeError("empty answer")
            return {**ans, "fallback": fb, "secs": round(time.monotonic() - t0, 1)}
        return go

    got: dict[str, tuple[bool, Any]] = {}

    def on_done(key, ok):
        done[0] += 1
        run["live"]["done"][key] = ok
        if ok:
            run["live"].setdefault("answers", {})[key] = _clip(str(got[key][1].get("summary") or ""), 240)
        progress(f"Stage 1 of 3 · {done[0]}/{n} seats answered")

    lim = run.get("limits") or {}
    if lim.get("max_secs") and not run.get("deadline"):
        run["deadline"] = _now() + lim["max_secs"]
    lim = run.get("limits") or {}
    if lim.get("max_secs") and not run.get("deadline"):
        run["deadline"] = _now() + lim["max_secs"]
    err = _budget_gate_stage1(run)
    if err:
        run["status"], run["next_stage"], run["error"] = "failed", None, err
        run.setdefault("skipped", {})["run"] = "budget"
        run["timeline"].append({"t": _now(), "event": "budget_refused"})
        return
    pictures = picture_inputs(run)
    if pictures and _undescribed(run):
        run_describe(run, llm, cancel, progress)
    # Answers come first: only the chairman's time is kept back. A seat cut short loses a whole view,
    # while a skipped vote only loses the ranking, so the vote is decided after Stage 1 on the time
    # actually left (a live run lost a seat at 82 s while 200 s went unused when the vote was reserved).
    window = stage_window(run, "stage1", reserve_review=False)
    run["live"] = {"stage": "stage1", "started": _now(), "done": {}, "ends": _now() + window}
    progress(f"Stage 1 of 3 · 0/{n} seats answered")
    why = "time limit" if window < run["stage_timeout"] else ""
    results = _parallel({s["label"]: job(s) for s in run["seats"]}, timeout=window,
                        cancel=cancel, on_done=on_done, why=why, into=got)
    _record_abandoned(run, "stage1", {s["label"]: (s, s["name"], tin, hi) for s, tin, _lo, hi in
                                       pricing.stage1_calls(run)}, results)
    for s in run["seats"]:
        ok, val = results.get(s["label"], (False, "not run"))
        run["answers"][s["label"]] = {"status": "ok", **val} if ok else {"status": "failed", "error": val}
    ok_n = sum(1 for a in run["answers"].values() if a["status"] == "ok")
    run["timeline"].append({"t": _now(), "event": "stage1", "answered": ok_n, "of": n})
    if ok_n < run["quorum"]:
        run["status"], run["next_stage"] = "failed", None
        run["error"] = f"Only {ok_n} of {n} seats answered; the quorum is {run['quorum']}."
        return
    run["status"] = "stage1_done"
    run["next_stage"] = "review" if run["review"] and ok_n >= 2 and not run.setdefault("skipped", {}).get("review") \
        else after_vote(run)


# ── Stage 2 ─────────────────────────────────────────────────────────────────

_LABEL_RE = re.compile(r"^\s*(?:response\s+)?([A-Z])\s*[.):]?\s*$", re.IGNORECASE)
_FINAL_RE = re.compile(r"FINAL RANKING\s*:?(.*)", re.IGNORECASE | re.DOTALL)
_ANY_LABEL_RE = re.compile(r"Response\s+([A-Z])\b", re.IGNORECASE)


def _norm_label(x: Any) -> Optional[str]:
    m = _LABEL_RE.match(str(x or ""))
    return m.group(1).upper() if m else None


def scrub_identity(run: dict[str, Any], text: str, *, names_too: bool = True) -> str:
    """Remove model ids (and, for reviewers, seat names) a member may have written into its own
    answer. Reviewers must not know who wrote what; the chairman must not know which model did
    (self-preference bias when the chairman also holds a seat)."""
    models = {s["model"] for s in run["seats"] if len(s.get("model") or "") >= 3}
    names = {s["name"] for s in run["seats"] if names_too and len(s.get("name") or "") >= 3}
    for t in sorted(models, key=len, reverse=True):                 # model ids: any case
        text = re.sub(re.escape(t), "[member]", text, flags=re.IGNORECASE)
    for t in sorted(names, key=len, reverse=True):                  # seat names: exact word, exact case
        text = re.sub(rf"(?<!\w){re.escape(t)}(?!\w)", "[member]", text)
    return text


def repair_ranking(raw: list[Any], shown: list[str]) -> tuple[list[str], bool]:
    """Dedupe, drop unknown labels, append missing labels (worst, in shown order)."""
    out: list[str] = []
    for x in raw or []:
        lab = _norm_label(x)
        if lab in shown and lab not in out:
            out.append(lab)
    repaired = out != list(raw or [])
    for lab in shown:
        if lab not in out:
            out.append(lab)
            repaired = True
    return out, repaired


def parse_text_ranking(text: str) -> list[str]:
    """Karpathy-style fallback: labels in order after the last 'FINAL RANKING'."""
    m = None
    for m in _FINAL_RE.finditer(text or ""):
        pass
    region = m.group(1) if m else (text or "")
    return [x.upper() for x in _ANY_LABEL_RE.findall(region)]


def aggregate(reviews: dict[str, dict[str, Any]], labels: list[str]) -> list[dict[str, Any]]:
    pos: dict[str, list[int]] = {lab: [] for lab in labels}
    firsts = {lab: 0 for lab in labels}
    for r in reviews.values():
        if r.get("status") != "ok":
            continue
        for i, lab in enumerate(r["ranking"]):
            if lab in pos:
                pos[lab].append(i + 1)
        if r["ranking"]:
            firsts[r["ranking"][0]] = firsts.get(r["ranking"][0], 0) + 1
    rows = [{"label": lab, "avg_rank": sum(p) / len(p), "first_votes": firsts[lab], "votes": len(p)}
            for lab, p in pos.items() if p]
    rows.sort(key=lambda r: (r["avg_rank"], -r["first_votes"], r["label"]))
    return rows


def run_review(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress,
               rng: Optional[random.Random] = None) -> None:
    rng = rng or random.Random()
    ok_labels = [lab for lab, a in run["answers"].items() if a["status"] == "ok"]
    reviewers = [seat_by_label(run, lab) for lab in ok_labels]
    n, done = len(reviewers), [0]

    def job(seat):
        order = ok_labels[:]
        rng_local = random.Random(rng.random())
        rng_local.shuffle(order)                       # per-reviewer order cancels position bias
        shown = [(lab, scrub_identity(run, run["answers"][lab]["answer"])) for lab in order]

        def go():
            instr, text = prompts.review(run, shown)
            parsed, raw, fb = _call_structured(llm, run, "review", seat["name"], seat, instr, text,
                                               prompts.REVIEW_SCHEMA, "committee_review")
            if parsed is not None:
                ranking, repaired = repair_ranking(parsed.get("ranking") or [], order)
                evals = []
                for e in parsed.get("evaluations") or []:
                    lab = _norm_label(e.get("label")) if isinstance(e, dict) else None
                    if lab in order:
                        evals.append({"label": lab, "strengths": str(e.get("strengths") or "").strip(),
                                      "weaknesses": str(e.get("weaknesses") or "").strip()})
            else:
                ranking, repaired = repair_ranking(parse_text_ranking(raw), order)
                evals = []
                repaired = True
            return {"order": order, "ranking": ranking, "evaluations": evals, "repaired": repaired,
                    "fallback": fb}
        return go

    got: dict[str, tuple[bool, Any]] = {}

    def on_done(key, ok):
        done[0] += 1
        run["live"]["done"][key] = ok
        if ok:
            run["live"].setdefault("ballots", {})[key] = list(got[key][1].get("ranking") or [])
        progress(f"Stage 2 of 3 · {done[0]}/{n} reviews in")

    if not _fit_usd(run, cancel, progress, lambda: pricing.review_calls(run) + [pricing.chair_call(run, with_review=True)],
                    "The blind vote"):
        run.setdefault("skipped", {})["review"] = "budget"
        run["timeline"].append({"t": _now(), "event": "review_skipped", "reason": "budget"})
        progress("Stage 2 skipped to stay within the budget")
        run["status"], run["next_stage"] = "review_done", after_vote(run)
        return
    window = _fit_time(run, cancel, progress, "review", min(MIN_STAGE_S, run["stage_timeout"]), "The blind vote")
    if run.get("deadline") and window < min(MIN_STAGE_S, run["stage_timeout"]):
        run.setdefault("skipped", {})["review"] = "time"
        run["timeline"].append({"t": _now(), "event": "review_skipped", "reason": "time"})
        progress("Stage 2 skipped to stay within the time limit")
        run["status"], run["next_stage"] = "review_done", after_vote(run)
        return
    run["live"] = {"stage": "review", "started": _now(), "done": {}, "ends": _now() + window}
    progress(f"Stage 2 of 3 · 0/{n} reviews in")
    results = _parallel({s["label"]: job(s) for s in reviewers}, timeout=window, cancel=cancel, on_done=on_done,
                        why="time limit" if window < run["stage_timeout"] else "", into=got)
    _record_abandoned(run, "review", {s["label"]: (s, s["name"], tin, hi) for s, tin, _lo, hi in
                                       pricing.review_calls(run)}, results)
    for lab in ok_labels:
        ok, val = results.get(lab, (False, "not run"))
        run["reviews"][lab] = {"status": "ok", **val} if ok else {"status": "failed", "error": val}
    run["ranking"] = aggregate(run["reviews"], ok_labels)
    good = sum(1 for r in run["reviews"].values() if r["status"] == "ok")
    run["timeline"].append({"t": _now(), "event": "review", "reviews": good, "of": n})
    run["status"], run["next_stage"] = "review_done", after_vote(run)


# ── Stage 3 ─────────────────────────────────────────────────────────────────

def _critiques(run: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in run["reviews"].values():
        for e in r.get("evaluations") or []:
            notes = []
            if e["strengths"]:
                notes.append("Strength: " + _clip(e["strengths"], 500))
            if e["weaknesses"]:
                notes.append("Weakness: " + _clip(e["weaknesses"], 500))
            out.setdefault(e["label"], []).extend(notes)
    return out


def _norm_exec(x: Any) -> dict[str, Any]:
    x = x if isinstance(x, dict) else {}
    return {"bottom_line": str(x.get("bottom_line") or "").strip(),
            "reasons": [r for r in _as_list(x.get("reasons")) if r.strip()][:3],
            "change": str(x.get("change") or "").strip()}


def action_text(item: dict[str, Any]) -> str:
    """'Sign the checklist — Shift supervisor · today 18:00' (owner/when only when given)."""
    tail = " · ".join(x for x in (item.get("owner"), item.get("when")) if x)
    return f"{item['action']} — {tail}" if tail else item["action"]


def _norm_actions(parsed: dict) -> dict[str, Any]:
    items = []
    for a in parsed.get("action_items") or []:
        if isinstance(a, dict) and str(a.get("action") or "").strip():
            items.append({k: _clip(str(a.get(k) or "").strip(), 300 if k == "action" else 80) for k in ("action", "owner", "when")})
    if not items:                                   # older schema / a model that ignored the field
        items = [{"action": x, "owner": "", "when": ""} for x in _as_list(parsed.get("next_actions"))]
    return {"action_items": items, "next_actions": [action_text(a) for a in items]}


def _norm_chair(parsed: Optional[dict], raw: str) -> dict[str, Any]:
    if parsed is None:
        first = next((ln.strip("#* ").strip() for ln in raw.splitlines() if ln.strip()), "")
        return {"final_answer": raw.strip(), "decision": _clip(first, 300), "confidence": "", "confidence_note": "",
                "reverse_if": [], "agreement": [], "dissent": [], "open_questions": [], "next_actions": [],
                "action_items": []}
    conf = str(parsed.get("confidence") or "").lower()
    dissent = [{"label": (_norm_label(d.get("label")) or str(d.get("label") or "")), "view": str(d.get("view") or "").strip()}
               for d in parsed.get("dissent") or [] if isinstance(d, dict) and d.get("view")]
    return {"final_answer": str(parsed.get("final_answer") or "").strip(),
            "decision": str(parsed.get("decision") or "").strip(),
            "confidence": conf if conf in ("high", "medium", "low") else "",
            "confidence_note": str(parsed.get("confidence_note") or "").strip(),
            "reverse_if": _as_list(parsed.get("reverse_if")), "agreement": _as_list(parsed.get("agreement")),
            "dissent": dissent, "open_questions": _as_list(parsed.get("open_questions")),
            **_norm_actions(parsed),
            "change_from_earlier": str(parsed.get("change_from_earlier") or "").strip(),
            "executive_summary": _norm_exec(parsed.get("executive_summary")),
            "disputes_resolved": [{"dispute": str(x.get("dispute") or "").strip(),
                                   "decided": str(x.get("decided") or "").strip(),
                                   "by_argument": str(x.get("by_argument") or "").strip()}
                                  for x in parsed.get("disputes_resolved") or [] if isinstance(x, dict)]}


def run_chair(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress) -> None:
    rank_pos = {r["label"]: i for i, r in enumerate(run["ranking"])}
    answers = sorted(
        [{"label": lab, "name": seat_by_label(run, lab)["name"], **a,
          "answer": scrub_identity(run, a["answer"], names_too=False)}
         for lab, a in run["answers"].items() if a["status"] == "ok"],
        key=lambda a: (rank_pos.get(a["label"], 99), a["label"]))
    slot = run["chairman"]
    chair_mt = None
    while pricing.limit(run) is not None:
        steps = [_mt := int(run.get("budget_tokens") or run["max_tokens"])] + [m for m in pricing.CUT_STEPS if m < _mt]
        for mt in steps:
            if not _over(run, [pricing.chair_call(run, mt)])[0]:
                chair_mt = mt
                break
        if chair_mt is not None:
            break
        least = pricing._sum([pricing.chair_call(run, steps[-1])])["charged"]
        if _ask(run, cancel, progress, "usd", "The chairman's verdict does not fit the budget left",
                least - (pricing.left(run) or 0)):
            continue
        run["chair"] = {"status": "failed", "model": model_label(slot), "error": "budget reached"}
        run["status"], run["next_stage"] = "failed", None
        run["error"] = (f"No verdict within the {pricing.fmt(pricing.limit(run))} limit: "
                        f"{pricing.fmt(pricing.spent(run)['charged'])} is spent and the chairman could cost more "
                        "than what is left. The seats' answers are kept.")
        run["timeline"].append({"t": _now(), "event": "chair", "ok": False})
        return
    window = _fit_time(run, cancel, progress, "chair", min(5, run["stage_timeout"]), "The chairman's verdict")
    if run.get("deadline") and window < min(5, run["stage_timeout"]):
        run["chair"] = {"status": "failed", "model": model_label(slot), "error": "time limit reached"}
        run["status"], run["next_stage"] = "failed", None
        run["error"] = (f"No verdict within the {_limit_minutes(run)} limit: the time ran out before the "
                        "chairman could start. The seats' answers are kept.")
        run["timeline"].append({"t": _now(), "event": "chair", "ok": False})
        return
    run["live"] = {"stage": "chair", "started": _now(), "done": {}, "ends": _now() + window}
    progress("Stage 3 of 3 · chairman is writing the final answer")

    def go():
        instr, text = prompts.chairman(run, answers, _critiques(run), run["ranking"])
        t0 = time.monotonic()
        parsed, raw, fb = _call_structured(llm, run, "chair", "Chairman", slot, instr, text,
                                           prompts.CHAIR_SCHEMA, "committee_verdict", max_tokens=chair_mt)
        out = _norm_chair(parsed, raw)
        disputes = (run.get("deliberation") or {}).get("disputes") or []
        if out.get("disputes_resolved") and disputes:
            hit = match_resolved(out["disputes_resolved"], disputes)
            out["disputes_resolved"] = [{**x, "dispute": i, "as_written": x["dispute"]} if x["dispute"] != i else x
                                        for i, x in hit.items()] + \
                [x for x in out["disputes_resolved"] if all(x is not y for y in hit.values())]
        if not out["final_answer"]:
            raise RuntimeError("empty final answer")
        return {**out, "fallback": fb, "secs": round(time.monotonic() - t0, 1)}

    timed = bool(run.get("deadline")) and window < run["stage_timeout"]
    res = _parallel({"chair": go}, timeout=window, cancel=cancel, on_done=lambda *_: None,
                    why="time limit" if timed else "")
    ok, val = res["chair"]
    run["timeline"].append({"t": _now(), "event": "chair", "ok": ok})
    if ok:
        from .followup import name_chair
        run["chair"] = {"status": "ok", "model": model_label(slot), **name_chair(val, run["seats"])}
        run["status"], run["next_stage"] = "chaired", None
    else:
        run["chair"] = {"status": "failed", "model": model_label(slot), "error": val}
        run["status"], run["next_stage"] = "failed", None
        run["error"] = (f"No verdict within the {_limit_minutes(run)} limit: the chairman did not finish in time. "
                        "The seats' answers are kept.") if timed and str(val).startswith("deadline") \
            else f"The chairman did not answer: {val}"


# ── deliberation (optional, between the blind vote and the verdict) ─────────

DELIBERATE_CHOICES = ("material", "action", "always", "never")
MAX_DISPUTES = 3
_LEVEL_ORDER = {"action": 0, "claim": 1, "emphasis": 2}


def after_vote(run: dict[str, Any]) -> str:
    """The stage after the answers (and blind vote): deliberation when it is switched on, else the verdict."""
    ok_n = sum(1 for a in run["answers"].values() if a.get("status") == "ok")
    return "deliberate" if pricing.deliberating(run) and ok_n >= 2 else "chair"


def should_debate(disputes: list[dict[str, Any]], setting: str) -> bool:
    """The plugin, not the model, decides from the levels the chairman gave each point."""
    levels = {d["level"] for d in disputes}
    if setting == "always":
        return bool(disputes)
    if setting == "action":
        return "action" in levels
    if setting == "material":
        return bool(levels & {"action", "claim"})
    return False


def norm_agenda(parsed: Optional[dict], seats: list[dict[str, Any]], setting: str) -> dict[str, Any]:
    """Clean the chairman's agenda: known seats only, ids D1.., the debated points first and at most three."""
    names = {s["name"].lower(): s["name"] for s in seats}
    out = []
    for d in (parsed or {}).get("disputes") or []:
        if not isinstance(d, dict) or not str(d.get("point") or "").strip():
            continue
        level = str(d.get("level") or "").lower()
        level = level if level in _LEVEL_ORDER else "claim"
        pos = []
        for p in d.get("positions") or []:
            if not isinstance(p, dict):
                continue
            nm = names.get(str(p.get("seat") or "").strip().lower())
            if nm and nm not in {x["seat"] for x in pos}:
                pos.append({"seat": nm, "side": str(p.get("side") or "").strip()[:60] or "unstated",
                            "view": str(p.get("view") or "").strip()})
        out.append({"level": level, "point": str(d["point"]).strip(), "positions": pos,
                    "question": str(d.get("question") or d["point"]).strip()})
    out.sort(key=lambda d: _LEVEL_ORDER[d["level"]])            # stable: the chairman's order within a level
    debated = [d for d in out if setting == "always" or d["level"] != "emphasis"][:MAX_DISPUTES]
    for i, d in enumerate(debated, 1):
        d["id"] = f"D{i}"
    noted = [d for d in out if d not in debated]
    return {"disputes": debated, "noted": noted, "reason": str((parsed or {}).get("reason") or "").strip()}


def _side_of(dispute: dict[str, Any], seat: str) -> str:
    return next((p["side"] for p in dispute["positions"] if p["seat"] == seat), "")


def _norm_reply(parsed: Optional[dict], raw: str, disputes: list[dict[str, Any]], seat: str) -> dict[str, Any]:
    by_id = {d["id"]: d for d in disputes}
    resp = []
    for x in (parsed or {}).get("responses") or []:
        if not isinstance(x, dict):
            continue
        did = str(x.get("dispute") or "").strip().upper()
        if did not in by_id or did in {r["dispute"] for r in resp}:
            continue
        stance = str(x.get("stance") or "").lower()
        stance = stance if stance in ("hold", "change", "partly") else "hold"
        sides = {p["side"].lower(): p["side"] for p in by_id[did]["positions"]}
        side = sides.get(str(x.get("side") or "").strip().lower())
        if side is None:                                    # an unknown tag: keep the old side unless they moved
            side = _side_of(by_id[did], seat) if stance == "hold" else (str(x.get("side") or "").strip()[:60] or "other")
        resp.append({"dispute": did, "stance": stance, "side": side,
                     "argument": str(x.get("argument") or "").strip(),
                     "evidence": [str(e).strip() for e in x.get("evidence") or [] if str(e).strip()][:8]})
    if parsed is None and raw.strip():
        return {"responses": [], "updated_position": _clip(raw.strip(), 400), "confidence": None, "fallback": True}
    conf = parsed.get("confidence") if parsed else None
    return {"responses": resp, "updated_position": str((parsed or {}).get("updated_position") or "").strip(),
            "confidence": conf if isinstance(conf, (int, float)) else None}


def _current_side(d: dict[str, Any], rounds_: list[dict[str, Any]], seat: str) -> str:
    side = _side_of(d, seat)
    for rnd in rounds_:
        r = rnd["replies"].get(seat) or {}
        for x in r.get("responses") or []:
            if x["dispute"] == d["id"]:
                side = x["side"]
    return side


def open_disputes(delib: dict[str, Any]) -> list[dict[str, Any]]:
    """Points where the members still take more than one side after the rounds so far."""
    out = []
    for d in delib["disputes"]:
        seats = [p["seat"] for p in d["positions"]]
        sides = {_current_side(d, delib["rounds"], s) for s in seats} - {""}
        if len(sides) > 1:
            out.append(d)
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9-]{4,}", str(text or "").lower())}


def match_resolved(resolved: list[dict[str, Any]], disputes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The chairman's per-point decisions keyed by dispute id. The chairman is asked to label each by its id
    (D1…); when it writes the point's wording instead, match by shared words, and fall back to the order."""
    ids = [d["id"] for d in disputes]
    out: dict[str, dict[str, Any]] = {}
    rest = []
    for x in resolved or []:
        if not isinstance(x, dict):
            continue
        m = re.match(r"\s*\W?(D\d+)\b", str(x.get("dispute") or ""), re.I)
        if m and m.group(1).upper() in ids and m.group(1).upper() not in out:
            out[m.group(1).upper()] = x
        else:
            rest.append(x)
    for x in list(rest):
        words = _words(x.get("dispute"))
        best, score = None, 0.0
        for d in disputes:
            if d["id"] in out:
                continue
            dw = _words(d["point"]) | _words(d.get("question"))
            s = len(words & dw) / max(1, min(len(words), len(dw)))
            if s > score:
                best, score = d["id"], s
        if best and score >= 0.4:
            out[best] = x
            rest.remove(x)
    free = [i for i in ids if i not in out]
    if rest and len(rest) == len(free):                 # same number left over: take them in order
        out.update(zip(free, rest))
    return out


def moves(delib: dict[str, Any]) -> list[dict[str, Any]]:
    """Who changed their mind, on which point, and the argument they gave (computed, not asked)."""
    out = []
    for d in delib["disputes"]:
        for p in d["positions"]:
            seat, start = p["seat"], p["side"]
            for n, rnd in enumerate(delib["rounds"], 1):
                for x in (rnd["replies"].get(seat) or {}).get("responses") or []:
                    if x["dispute"] == d["id"] and x["stance"] in ("change", "partly"):
                        out.append({"seat": seat, "dispute": d["id"], "round": n, "stance": x["stance"],
                                    "from": start, "to": x["side"], "because": x["argument"],
                                    "kept_side": x["side"] == start})   # a concession, not a change of side
                        start = x["side"]
    return out


def _delib_skip(run: dict[str, Any], why: str, progress: Progress, note: str) -> None:
    run.setdefault("skipped", {})["deliberate"] = why
    (run.get("deliberation") or run.setdefault("deliberation", {})).update({"skipped": why})
    run["timeline"].append({"t": _now(), "event": "deliberate_skipped", "reason": why})
    progress(note)
    run["status"], run["next_stage"] = "deliberated", "chair"


def run_deliberate(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress) -> None:
    setting = run.get("deliberate") or "never"
    run["deliberation"] = {"setting": setting, "rounds_planned": pricing.rounds(run), "disputes": [], "noted": [],
                           "rounds": [], "moves": [], "reason": ""}
    delib = run["deliberation"]
    ok_labels = [lab for lab, a in run["answers"].items() if a["status"] == "ok"]
    answers = [{"label": lab, "name": seat_by_label(run, lab)["name"], **run["answers"][lab],
                "answer": scrub_identity(run, run["answers"][lab]["answer"], names_too=False),
                "summary": scrub_identity(run, run["answers"][lab].get("summary") or "", names_too=False)}
               for lab in ok_labels]
    if setting == "never" or len(answers) < 2:
        return _delib_skip(run, "off", progress, "Deliberation is off")
    if not _fit_usd(run, cancel, progress, lambda: [pricing.agenda_call(run)] + pricing.round_calls(run) + [pricing.chair_call(run)],
                    "The debate"):
        return _delib_skip(run, "budget", progress, "Deliberation skipped to stay within the budget")
    window = _fit_time(run, cancel, progress, "deliberate", 2 * min(MIN_STAGE_S, run["stage_timeout"]), "The debate")
    if run.get("deadline") and window < 2 * min(MIN_STAGE_S, run["stage_timeout"]):
        return _delib_skip(run, "time", progress, "Deliberation skipped to stay within the time limit")

    # Step A: the agenda
    run["live"] = {"stage": "deliberate", "step": "agenda", "started": _now(), "done": {}, "ends": _now() + window}
    progress("Deliberation · the chairman is listing the disputed points")
    slot = run["chairman"]

    def agenda_job():
        instr, text = prompts.agenda(run, answers)
        parsed, raw, _fb = _call_structured(llm, run, "deliberate", "Chairman", slot, instr, text,
                                            prompts.AGENDA_SCHEMA, "committee_agenda", max_tokens=pricing.AGENDA_MAX)
        return parsed
    res = _parallel({"agenda": agenda_job}, timeout=window, cancel=cancel, on_done=lambda *_: None,
                    why="time limit" if window < run["stage_timeout"] else "")
    _record_abandoned(run, "deliberate", {"agenda": (slot, "Chairman", *[pricing.agenda_call(run)[i] for i in (1, 3)])}, res)
    ok, parsed = res["agenda"]
    if not ok or parsed is None:
        delib["error"] = str(parsed) if not ok else "the agenda was not in the expected form"
        return _delib_skip(run, "agenda failed", progress, "Deliberation skipped: the chairman could not list the points")
    delib.update(norm_agenda(parsed, run["seats"], setting))
    run["timeline"].append({"t": _now(), "event": "agenda", "disputes": len(delib["disputes"])})
    if not should_debate(delib["disputes"], setting):
        return _delib_skip(run, "no material disagreement" if setting != "action" else "same action",
                           progress, "No material disagreement · going to the verdict")

    # Step B: the rounds
    names = {s["name"]: s for s in run["seats"]}
    debaters = [names[a["name"]] for a in answers]
    for n in range(1, pricing.rounds(run) + 1):
        todo = delib["disputes"] if n == 1 else open_disputes(delib)
        if not todo:
            break
        if not _fit_usd(run, cancel, progress, lambda n=n: pricing.round_calls(run, n_round=n) + [pricing.chair_call(run)],
                        f"Debate round {n}"):
            delib["stopped"] = "budget"
            break
        window = _fit_time(run, cancel, progress, "deliberate", min(MIN_STAGE_S, run["stage_timeout"]), f"Debate round {n}")
        if run.get("deadline") and window < min(MIN_STAGE_S, run["stage_timeout"]):
            delib["stopped"] = "time"
            break
        run["live"] = {"stage": "deliberate", "step": f"round {n}", "started": _now(), "done": {},
                       "ends": _now() + window}
        done = [0]
        progress(f"Deliberation · round {n} · 0/{len(debaters)} replies in")
        earlier = [r["replies"] for r in delib["rounds"]]

        def job(seat, todo=todo, earlier=earlier):
            def go():
                instr, text = prompts.deliberation_round(run, seat, answers, todo, earlier)
                parsed, raw, fb = _call_structured(llm, run, "deliberate", seat["name"], seat, instr, text,
                                                   prompts.REPLY_SCHEMA, "committee_reply",
                                                   max_tokens=pricing.REPLY_MAX)
                return _norm_reply(parsed, raw, todo, seat["name"])
            return go

        got: dict[str, tuple[bool, Any]] = {}

        def on_done(key, ok, n=n, got=got):
            done[0] += 1
            run["live"]["done"][key] = ok
            if ok:
                run["live"].setdefault("replies", {})[key] = [
                    {"dispute": x.get("dispute"), "stance": x.get("stance")} for x in got[key][1].get("responses") or []]
            progress(f"Deliberation · round {n} · {done[0]}/{len(debaters)} replies in")

        res = _parallel({s["name"]: job(s) for s in debaters}, timeout=window, cancel=cancel, on_done=on_done,
                        why="time limit" if window < run["stage_timeout"] else "", into=got)
        _record_abandoned(run, "deliberate", {s["name"]: (s, s["name"], tin, hi) for s, tin, _lo, hi in pricing.round_calls(run, n_round=n)}, res)
        replies = {}
        for s in debaters:
            ok, val = res.get(s["name"], (False, "not run"))
            replies[s["name"]] = {"status": "ok", **val} if ok else {"status": "failed", "error": str(val)}
        delib["rounds"].append({"disputes": [d["id"] for d in todo], "replies": replies})
        run["timeline"].append({"t": _now(), "event": "deliberate_round", "round": n,
                                "replies": sum(1 for r in replies.values() if r["status"] == "ok"),
                                "of": len(debaters)})
    delib["moves"] = moves(delib)
    delib["open"] = [d["id"] for d in open_disputes(delib)]
    run["status"], run["next_stage"] = "deliberated", "chair"


# ── driver ──────────────────────────────────────────────────────────────────

def advance(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress,
            rng: Optional[random.Random] = None) -> str:
    """Run the next stage. Returns the stage that ran. Raises Cancelled if cancelled."""
    stage = run.get("next_stage")
    if run.get("status") == "cancelled":
        raise Cancelled()
    if stage not in STAGES:
        raise ValueError(f"Run {run['id']} has nothing left to run (status: {run['status']}).")
    token = _LIVE.set((run, cancel))
    try:
        if interactive(run, cancel):
            apply_requests(run, cancel)
        if stage == "stage1":
            run_stage1(run, llm, cancel, progress)
        elif stage == "review":
            run_review(run, llm, cancel, progress, rng)
        elif stage == "deliberate":
            run_deliberate(run, llm, cancel, progress)
        else:
            run_chair(run, llm, cancel, progress)
    except Cancelled:
        run["status"], run["next_stage"] = "cancelled", None
        run["timeline"].append({"t": _now(), "event": "cancelled", "during": stage})
        raise
    finally:
        _LIVE.reset(token)
    return stage


def usage_totals(run: dict[str, Any]) -> dict[str, Any]:
    calls = run.get("usage") or []
    def total(k):
        vals = [c[k] for c in calls if isinstance(c.get(k), (int, float))]
        return sum(vals) if vals else None
    costs = [c["cost_usd"] for c in calls if isinstance(c.get("cost_usd"), (int, float))]
    sp = pricing.spent(run)
    return {"calls": sum(1 for c in calls if not c.get("abandoned")), "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "cost_usd": sum(costs) if costs and len(costs) == len(calls) else None,
            "cost_partial": bool(costs) and len(costs) != len(calls),
            "est_charged": sp["charged"], "est_list": sp["list"], "est_unknown_calls": sp["unknown_calls"],
            "abandoned": sum(1 for c in calls if c.get("abandoned")), "limit_usd": pricing.limit(run)}

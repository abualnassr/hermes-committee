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

from . import prompts
from .config import model_label

STAGES = ("stage1", "review", "chair")
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
                   schema: dict, schema_name: str, max_tokens: int, timeout: float, purpose: str) -> Reply: ...

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
        **{k: roster[k] for k in ("preset", "template", "template_title", "chairman_guidance", "decision_focus",
                                  "chairman", "review", "quorum", "stage_timeout", "max_tokens", "warnings")},
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

def _parallel(jobs: dict[str, Callable[[], Any]], *, timeout: float, cancel: threading.Event,
              on_done: Callable[[str, bool], None]) -> dict[str, tuple[bool, Any]]:
    """Run jobs concurrently. Never waits past ``timeout`` and stops waiting on cancel.
    Workers that are still running are abandoned (their results are discarded)."""
    results: dict[str, tuple[bool, Any]] = {}
    if not jobs:
        return results
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="committee")
    try:
        futs = {pool.submit(contextvars.copy_context().run, fn): key for key, fn in jobs.items()}
        pending = set(futs)
        deadline = time.monotonic() + timeout
        while pending:
            if cancel.is_set():
                raise Cancelled()
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
            results[futs[f]] = (False, f"deadline: no answer within {int(timeout)} s")
        return results
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _record_usage(run: dict[str, Any], stage: str, who: str, slot: dict[str, Any], reply: Reply, secs: float):
    u = reply.usage or {}
    run["usage"].append({"stage": stage, "who": who, "provider": reply.provider or slot.get("provider") or "",
                         "model": reply.model or slot.get("model") or "",
                         "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                         "cost_usd": u.get("cost_usd"), "secs": round(secs, 1)})


def _call_structured(llm: LLM, run, stage, who, slot, instructions, text, schema, name) -> tuple[Optional[dict], str, bool]:
    """Structured call, one re-ask on a schema miss, then plain-text fallback.
    Returns (parsed_or_None, raw_text, used_fallback)."""
    kw = dict(provider=slot.get("provider") or "", model=slot.get("model") or "", max_tokens=run["max_tokens"],
              timeout=float(run["stage_timeout"]), purpose=f"hermes-committee.{stage}")
    for attempt in range(2):
        t0 = time.monotonic()
        r = llm.structured(instructions=instructions, text=text, schema=schema, schema_name=name, **kw)
        _record_usage(run, stage, who, slot, r, time.monotonic() - t0)
        if r.content_type == "json" and isinstance(r.parsed, dict):
            return r.parsed, r.text or "", False
        if attempt == 0:
            instructions = instructions + " Respond with JSON that matches the schema exactly."
    t0 = time.monotonic()
    r = llm.text(instructions=instructions, text=text, **kw)
    _record_usage(run, stage, who, slot, r, time.monotonic() - t0)
    return None, r.text or "", True


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
            instr, text = prompts.stage1(run, seat)
            t0 = time.monotonic()
            parsed, raw, fb = _call_structured(llm, run, "stage1", seat["name"], seat, instr, text,
                                               prompts.STAGE1_SCHEMA, "committee_answer")
            ans = _norm_answer(parsed, raw)
            if not ans["answer"]:
                raise RuntimeError("empty answer")
            return {**ans, "fallback": fb, "secs": round(time.monotonic() - t0, 1)}
        return go

    def on_done(_key, _ok):
        done[0] += 1
        progress(f"Stage 1 of 3 · {done[0]}/{n} seats answered")

    progress(f"Stage 1 of 3 · 0/{n} seats answered")
    results = _parallel({s["label"]: job(s) for s in run["seats"]}, timeout=run["stage_timeout"],
                        cancel=cancel, on_done=on_done)
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
    run["next_stage"] = "review" if run["review"] and ok_n >= 2 else "chair"


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

    def on_done(_k, _ok):
        done[0] += 1
        progress(f"Stage 2 of 3 · {done[0]}/{n} reviews in")

    progress(f"Stage 2 of 3 · 0/{n} reviews in")
    results = _parallel({s["label"]: job(s) for s in reviewers}, timeout=run["stage_timeout"],
                        cancel=cancel, on_done=on_done)
    for lab in ok_labels:
        ok, val = results.get(lab, (False, "not run"))
        run["reviews"][lab] = {"status": "ok", **val} if ok else {"status": "failed", "error": val}
    run["ranking"] = aggregate(run["reviews"], ok_labels)
    good = sum(1 for r in run["reviews"].values() if r["status"] == "ok")
    run["timeline"].append({"t": _now(), "event": "review", "reviews": good, "of": n})
    run["status"], run["next_stage"] = "review_done", "chair"


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


def _norm_chair(parsed: Optional[dict], raw: str) -> dict[str, Any]:
    if parsed is None:
        first = next((ln.strip("#* ").strip() for ln in raw.splitlines() if ln.strip()), "")
        return {"final_answer": raw.strip(), "decision": _clip(first, 300), "confidence": "", "confidence_note": "",
                "reverse_if": [], "agreement": [], "dissent": [], "open_questions": [], "next_actions": []}
    conf = str(parsed.get("confidence") or "").lower()
    dissent = [{"label": (_norm_label(d.get("label")) or str(d.get("label") or "")), "view": str(d.get("view") or "").strip()}
               for d in parsed.get("dissent") or [] if isinstance(d, dict) and d.get("view")]
    return {"final_answer": str(parsed.get("final_answer") or "").strip(),
            "decision": str(parsed.get("decision") or "").strip(),
            "confidence": conf if conf in ("high", "medium", "low") else "",
            "confidence_note": str(parsed.get("confidence_note") or "").strip(),
            "reverse_if": _as_list(parsed.get("reverse_if")), "agreement": _as_list(parsed.get("agreement")),
            "dissent": dissent, "open_questions": _as_list(parsed.get("open_questions")),
            "next_actions": _as_list(parsed.get("next_actions"))}


def run_chair(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress) -> None:
    rank_pos = {r["label"]: i for i, r in enumerate(run["ranking"])}
    answers = sorted(
        [{"label": lab, "name": seat_by_label(run, lab)["name"], **a,
          "answer": scrub_identity(run, a["answer"], names_too=False)}
         for lab, a in run["answers"].items() if a["status"] == "ok"],
        key=lambda a: (rank_pos.get(a["label"], 99), a["label"]))
    progress("Stage 3 of 3 · chairman is writing the final answer")
    slot = run["chairman"]

    def go():
        instr, text = prompts.chairman(run, answers, _critiques(run), run["ranking"])
        t0 = time.monotonic()
        parsed, raw, fb = _call_structured(llm, run, "chair", "Chairman", slot, instr, text,
                                           prompts.CHAIR_SCHEMA, "committee_verdict")
        out = _norm_chair(parsed, raw)
        if not out["final_answer"]:
            raise RuntimeError("empty final answer")
        return {**out, "fallback": fb, "secs": round(time.monotonic() - t0, 1)}

    res = _parallel({"chair": go}, timeout=run["stage_timeout"], cancel=cancel, on_done=lambda *_: None)
    ok, val = res["chair"]
    run["timeline"].append({"t": _now(), "event": "chair", "ok": ok})
    if ok:
        run["chair"] = {"status": "ok", "model": model_label(slot), **val}
        run["status"], run["next_stage"] = "chaired", None
    else:
        run["chair"] = {"status": "failed", "model": model_label(slot), "error": val}
        run["status"], run["next_stage"] = "failed", None
        run["error"] = f"The chairman did not answer: {val}"


# ── driver ──────────────────────────────────────────────────────────────────

def advance(run: dict[str, Any], llm: LLM, cancel: threading.Event, progress: Progress,
            rng: Optional[random.Random] = None) -> str:
    """Run the next stage. Returns the stage that ran. Raises Cancelled if cancelled."""
    stage = run.get("next_stage")
    if run.get("status") == "cancelled":
        raise Cancelled()
    if stage not in STAGES:
        raise ValueError(f"Run {run['id']} has nothing left to run (status: {run['status']}).")
    try:
        if stage == "stage1":
            run_stage1(run, llm, cancel, progress)
        elif stage == "review":
            run_review(run, llm, cancel, progress, rng)
        else:
            run_chair(run, llm, cancel, progress)
    except Cancelled:
        run["status"], run["next_stage"] = "cancelled", None
        run["timeline"].append({"t": _now(), "event": "cancelled", "during": stage})
        raise
    return stage


def usage_totals(run: dict[str, Any]) -> dict[str, Any]:
    calls = run.get("usage") or []
    def total(k):
        vals = [c[k] for c in calls if isinstance(c.get(k), (int, float))]
        return sum(vals) if vals else None
    costs = [c["cost_usd"] for c in calls if isinstance(c.get("cost_usd"), (int, float))]
    return {"calls": len(calls), "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "cost_usd": sum(costs) if costs and len(costs) == len(calls) else None,
            "cost_partial": bool(costs) and len(costs) != len(calls)}

"""Facts computed from run records for the Desktop page: per-run insights, the ballot matrix, the
cross-run standings and per-model timing. Pure functions over run dicts — no model calls, no Hermes.

Every number here is derived from what the run recorded; nothing is estimated by a model.
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional

TERMINAL = ("done", "failed", "cancelled")


def model_key(slot: dict[str, Any]) -> str:
    return slot.get("model") or "active model"


def _ok_answers(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lab: a for lab, a in (run.get("answers") or {}).items() if a.get("status") == "ok"}


def _ok_reviews(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lab: r for lab, r in (run.get("reviews") or {}).items() if r.get("status") == "ok" and r.get("ranking")}


def rank_score(avg_rank: float, n: int) -> float:
    """1.0 = ranked first by everyone, 0.0 = ranked last by everyone."""
    return 1.0 if n <= 1 else max(0.0, min(1.0, 1 - (avg_rank - 1) / (n - 1)))


def stage_seconds(run: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Seconds per seat label per stage, summed over retries/fallbacks, from the usage ledger."""
    by_name = {s["name"]: s["label"] for s in run.get("seats") or []}
    out: dict[str, dict[str, float]] = {}
    for u in run.get("usage") or []:
        stage = u.get("stage")
        who = "chair" if stage == "chair" or u.get("who") == "Chairman" else by_name.get(u.get("who"))
        if who is None or not isinstance(u.get("secs"), (int, float)):
            continue
        out.setdefault(stage, {}).setdefault(who, 0.0)
        out[stage][who] += float(u["secs"])
    return out


def ballot_matrix(run: dict[str, Any]) -> dict[str, Any]:
    """rows = reviewer labels, cols = answer labels, cell = rank (1 = best) or None."""
    answers = sorted(_ok_answers(run))
    reviews = _ok_reviews(run)
    live = run.get("live") or {}
    if not reviews and live.get("stage") == "review" and run.get("status") not in TERMINAL:
        reviews = {lab: {"ranking": r} for lab, r in (live.get("ballots") or {}).items() if r}
    rows = sorted(reviews)
    cells = {r: {c: (reviews[r]["ranking"].index(c) + 1 if c in reviews[r]["ranking"] else None) for c in answers}
             for r in rows}
    return {"rows": rows, "cols": answers, "cells": cells}


def run_insights(run: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language findings about how this committee behaved. Each has kind/title/text."""
    out: list[dict[str, str]] = []
    seats = {s["label"]: s for s in run.get("seats") or []}
    answers = _ok_answers(run)
    reviews = _ok_reviews(run)
    ranking = run.get("ranking") or []
    pos = {r["label"]: i + 1 for i, r in enumerate(ranking)}

    if reviews:
        selfish = [lab for lab, r in reviews.items() if r["ranking"][0] == lab]
        k = len(answers) or 1
        chance = len(reviews) / k
        names = ", ".join(seats[lab]["name"] for lab in selfish if lab in seats)
        if selfish:
            out.append({"kind": "self", "title": "Self-preference",
                        "text": f"{len(selfish)} of {len(reviews)} reviewers ranked their own anonymous answer first"
                                f" ({names}). Chance alone would give about {chance:.1f}."})
        else:
            out.append({"kind": "self", "title": "No self-preference",
                        "text": f"None of the {len(reviews)} reviewers ranked their own anonymous answer first."})

        firsts = [r["ranking"][0] for r in reviews.values()]
        top = max(set(firsts), key=firsts.count)
        if firsts.count(top) == len(firsts) and len(firsts) > 1:
            out.append({"kind": "consensus", "title": "Unanimous first place",
                        "text": f"Every reviewer ranked {seats.get(top, {}).get('name', top)} first."})

    conf = {lab: a["confidence"] for lab, a in answers.items() if isinstance(a.get("confidence"), (int, float))}
    if len(conf) >= 2 and pos:
        hi = max(conf, key=lambda k: conf[k])
        lo_rank = max(pos.values())
        if hi in pos and pos[hi] == lo_rank and lo_rank > 1:
            out.append({"kind": "calibration", "title": "Confidence ≠ quality",
                        "text": f"{seats[hi]['name']} was the most confident ({conf[hi]:.2f}) and its peers ranked it last."})
        elif hi in pos and pos[hi] == 1:
            out.append({"kind": "calibration", "title": "Confidence matched quality",
                        "text": f"{seats[hi]['name']} was the most confident ({conf[hi]:.2f}) and its peers ranked it first."})

    chair = run.get("chair") or {}
    from .followup import seat_of
    dissent = {seat_of(d.get("label"), run.get("seats") or []) for d in chair.get("dissent") or []}
    for lab in [r["label"] for r in ranking[:2]]:
        if lab in dissent and lab in seats:
            out.append({"kind": "dissent", "title": "Dissent ranked high",
                        "text": f"{seats[lab]['name']} disagreed with the verdict yet its peers ranked it #{pos[lab]}; "
                                f"its objection is recorded in the report."})
            break

    d = run.get("deliberation") or {}
    if d.get("rounds"):
        mv = d.get("moves") or []
        held = d.get("open") or []
        changed = [m for m in mv if not m.get("kept_side", m.get("from") is not None and m.get("from") == m.get("to"))]
        conceded = [m for m in mv if m not in changed]
        if changed:
            who = sorted({m["seat"] for m in changed})
            extra = f" and {len(conceded)} concession{'s' if len(conceded) != 1 else ''}" if conceded else ""
            out.append({"kind": "debate", "title": "The debate changed minds",
                        "text": f"{len(changed)} change{'s' if len(changed) != 1 else ''} of side{extra} in the "
                                f"deliberation ({', '.join(who)}). Each gave its reason; the Deliberation section shows them."})
        elif conceded:
            who = sorted({m["seat"] for m in conceded})
            out.append({"kind": "debate", "title": "Concessions, no change of side",
                        "text": f"{', '.join(who)} conceded part of the other side's argument but kept its position; "
                                "the Deliberation section shows what was conceded."})
        else:
            out.append({"kind": "debate", "title": "Nobody moved",
                        "text": "Every member held its position through the deliberation, so the chairman decided "
                                "between arguments that did not converge."})
        if held:
            out.append({"kind": "debate", "title": "Still disputed",
                        "text": f"{len(held)} point{'s' if len(held) != 1 else ''} stayed disputed after the debate; "
                                "the remaining views are kept as dissent."})

    secs = stage_seconds(run).get("stage1") or {}
    if len(secs) >= 2:
        slow = max(secs, key=lambda k: secs[k])
        fast = min(secs, key=lambda k: secs[k])
        if secs[slow] >= 2.5 * max(secs[fast], 1):
            out.append({"kind": "timing", "title": "One seat set the pace",
                        "text": f"Stage 1 waited {secs[slow]:.0f} s for {seats[slow]['name']}; "
                                f"the fastest seat took {secs[fast]:.0f} s."})
    return out


def summary(run: dict[str, Any]) -> dict[str, Any]:
    """One docket row."""
    chair = run.get("chair") or {}
    tl = run.get("timeline") or []
    end = tl[-1]["t"] if tl else run.get("created")
    usage = run.get("usage") or []
    return {"id": run["id"], "created": run.get("created"), "status": run.get("status"),
            "template": run.get("template"), "template_title": run.get("template_title"),
            "preset": run.get("preset"), "question": run.get("question") or "",
            "decision": chair.get("decision") if chair.get("status") == "ok" else None,
            "confidence": chair.get("confidence") if chair.get("status") == "ok" else None,
            "error": run.get("error"), "seats": len(run.get("seats") or []),
            "calls": len(usage), "elapsed": round(end - run["created"], 1) if end and run.get("created") else None,
            "live": (run.get("live") or {}).get("stage") if run.get("status") not in TERMINAL else None,
            "has_pdf": bool((run.get("reports") or {}).get("pdf")),
            "draft": run.get("draft"), "session_id": run.get("session_id") or "",
            "title": run.get("title") or "", "follow_of": run.get("follow_of")}


def standings(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Per-model record across finished runs that had peer review."""
    rows: dict[str, dict[str, Any]] = {}
    points: list[dict[str, Any]] = []
    counted = 0
    for run in runs:
        ranking = run.get("ranking") or []
        if run.get("status") != "done" or not ranking:
            continue
        counted += 1
        n = len(ranking)
        seats = {s["label"]: s for s in run.get("seats") or []}
        reviews = _ok_reviews(run)
        secs = stage_seconds(run).get("stage1") or {}
        for i, r in enumerate(ranking):
            s = seats.get(r["label"])
            if not s:
                continue
            key = model_key(s)
            row = rows.setdefault(key, {"model": key, "provider": s.get("provider") or "", "seats": 0, "runs": set(),
                                        "scores": [], "firsts": 0, "conf": [], "secs": [], "self_first": 0,
                                        "wins": 0, "roles": set()})
            score = rank_score(r["avg_rank"], n)
            row["seats"] += 1
            row["runs"].add(run["id"])
            row["scores"].append(score)
            row["firsts"] += r.get("first_votes", 0)
            row["wins"] += 1 if i == 0 else 0
            row["roles"].add(s["name"])
            a = (run.get("answers") or {}).get(r["label"]) or {}
            if isinstance(a.get("confidence"), (int, float)):
                row["conf"].append(a["confidence"])
                points.append({"model": key, "role": s["name"], "run": run["id"],
                               "confidence": a["confidence"], "score": round(score, 3)})
            if r["label"] in secs:
                row["secs"].append(secs[r["label"]])
            if r["label"] in reviews and reviews[r["label"]]["ranking"][0] == r["label"]:
                row["self_first"] += 1
    table = []
    for row in rows.values():
        table.append({"model": row["model"], "provider": row["provider"], "seats": row["seats"],
                      "runs": len(row["runs"]), "score": round(statistics.mean(row["scores"]), 3),
                      "first_votes": row["firsts"], "wins": row["wins"],
                      "confidence": round(statistics.mean(row["conf"]), 3) if row["conf"] else None,
                      "median_secs": round(statistics.median(row["secs"]), 1) if row["secs"] else None,
                      "self_first": row["self_first"], "roles": sorted(row["roles"])})
    table.sort(key=lambda r: (-r["score"], -r["first_votes"], r["model"]))
    return {"runs": counted, "table": table, "points": points, "enough": counted >= 10}


def expected_seconds(runs: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Median Stage 1 seconds per model, used to animate a live seat's progress ring."""
    acc: dict[str, list[float]] = {}
    for run in runs:
        seats = {s["label"]: s for s in run.get("seats") or []}
        for lab, secs in (stage_seconds(run).get("stage1") or {}).items():
            if lab in seats:
                acc.setdefault(model_key(seats[lab]), []).append(secs)
    return {k: round(statistics.median(v), 1) for k, v in acc.items() if v}


# ── live views for the Desktop page (lanes, tokens, totals, event log, final stances) ─────────────────

def _t0(run: dict[str, Any]) -> float:
    tl = run.get("timeline") or []
    return float(run.get("created") or (tl[0]["t"] if tl else 0))


def _events(run: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    return [e for e in run.get("timeline") or [] if e.get("event") in names]


def stage_starts(run: dict[str, Any]) -> dict[str, float]:
    """Seconds after opening at which each stage began (a stage begins when the one before it ended)."""
    t0, out = _t0(run), {}
    desc = _events(run, "describe", "research")
    if run.get("research"):
        out["research"] = 0.0
    out["stage1"] = (desc[-1]["t"] if desc else t0) - t0
    s1 = _events(run, "stage1")
    if s1:
        out["review"] = s1[-1]["t"] - t0
    rv = _events(run, "review", "review_skipped")
    if rv and (run.get("deliberate") or "never") != "never":
        out["deliberate"] = rv[-1]["t"] - t0
    dl = _events(run, "agenda", "deliberate_round", "deliberate_skipped")
    if dl or rv:
        out["chair"] = (dl[-1]["t"] if dl else rv[-1]["t"]) - t0
    elif s1:
        out["chair"] = s1[-1]["t"] - t0
    live = run.get("live") or {}
    if run.get("status") not in TERMINAL and live.get("stage") and live.get("started"):
        st = live["stage"]
        if st == "deliberate":
            out.setdefault("deliberate", live["started"] - t0)
        elif live.get("researching") is not None or live.get("sources"):
            pass                                            # still before the answers
        else:
            out[st] = live["started"] - t0                  # the running stage: exact
    return {k: round(max(0.0, v), 1) for k, v in out.items()}


def _who(run: dict[str, Any], u: dict[str, Any]) -> Optional[str]:
    if u.get("who") == "Chairman" or u.get("stage") == "chair":
        return "chair"
    return {s["name"]: s["label"] for s in run.get("seats") or []}.get(u.get("who"))


def lanes(run: dict[str, Any]) -> list[dict[str, Any]]:
    """One bar per model call: who, stage, start (s after opening), secs, tokens; plus a running bar for each
    call still out in the live stage. Starts are stage starts (calls in a stage run in parallel); retries and
    later deliberation rounds follow on."""
    starts, t0 = stage_starts(run), _t0(run)
    rounds = [e["t"] - t0 for e in _events(run, "agenda", "deliberate_round")]
    cursor: dict[tuple[str, str], float] = {}
    n_delib: dict[str, int] = {}
    out = []
    sub = _is_sub(run)
    for u in run.get("usage") or []:
        who, stage = _who(run, u), u.get("stage")
        if who is None or stage not in starts or not isinstance(u.get("secs"), (int, float)):
            continue
        if stage == "deliberate" and who != "chair" and rounds:
            k = n_delib.get(who, 0)
            n_delib[who] = k + 1
            start = rounds[min(k, len(rounds) - 1)]
        else:
            start = cursor.get((stage, who), starts[stage])
        cursor[(stage, who)] = start + float(u["secs"])
        out.append({"who": who, "stage": stage, "start": round(start, 1), "secs": round(float(u["secs"]), 1),
                    "tokens": int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0), "running": False,
                    "cost": 0.0 if sub(u) else (round(float(u.get("cost_est") or 0), 6) if u.get("cost_known") else None)})
    live = run.get("live") or {}
    st = live.get("stage")
    if run.get("status") not in TERMINAL and st in starts:
        seats = run.get("seats") or []
        ok = [lab for lab, a in (run.get("answers") or {}).items() if a.get("status") == "ok"]
        done = live.get("done") or {}
        if st == "stage1" and (live.get("researching") or live.get("sources")):
            want = {"chair": "research"} if live.get("researching") else {}
            st = "research"
        elif st == "stage1":
            want = {s["label"]: s["label"] for s in seats}                       # label -> key in live.done
        elif st == "review":
            want = {lab: lab for lab in ok}
        elif st == "deliberate" and live.get("step") == "agenda":
            want = {"chair": "agenda"}
        elif st == "deliberate":
            want = {s["label"]: s["name"] for s in seats if s["label"] in ok}   # replies are keyed by seat name
        else:
            want = {"chair": "chair"}
        for lab, key in want.items():
            if key in done or (st == "chair" and done):
                continue
            out.append({"who": lab, "stage": st, "start": round(live["started"] - t0, 1), "secs": None,
                        "tokens": 0, "running": True, "cost": 0.0})
    return out


def seat_tokens(run: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in run.get("usage") or []:
        who = _who(run, u)
        if who:
            out[who] = out.get(who, 0) + int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
    return out


def _is_sub(run: dict[str, Any]):
    slots = {s["name"]: s for s in run.get("seats") or []}
    slots["Chairman"] = run.get("chairman") or {}
    return lambda u: ((slots.get(u.get("who")) or {}).get("price") or {}).get("billing") == "subscription"


def run_totals(run: dict[str, Any]) -> dict[str, Any]:
    """Calls, tokens and the known estimated cost. Subscription calls count $0; calls without a known price are
    counted separately and never as $0."""
    usage = run.get("usage") or []
    sub = _is_sub(run)
    known = [u for u in usage if u.get("cost_known") and not sub(u)]
    return {"calls": len(usage),
            "tokens": sum(int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) for u in usage),
            "cost_usd": round(sum(float(u.get("cost_est") or 0) for u in known), 4),
            "subscription_calls": sum(1 for u in usage if sub(u)),
            "unknown_calls": sum(1 for u in usage if not u.get("cost_known") and not sub(u))}


def final_stances(run: dict[str, Any]) -> dict[str, str]:
    """After the verdict: 'dissent' for seats the chairman named as dissenting, 'agree' for the other answers."""
    chair = run.get("chair") or {}
    if run.get("status") != "done" or chair.get("status") != "ok":
        return {}
    seats = run.get("seats") or []
    by_name = {s["name"].lower(): s["label"] for s in seats}
    labels = {s["label"] for s in seats}
    dis = set()
    for d in chair.get("dissent") or []:
        for key in (str(d.get("name") or "").lower(), str(d.get("label") or "")):
            if key in by_name:
                dis.add(by_name[key])
            elif key in labels:
                dis.add(key)
    return {lab: ("dissent" if lab in dis else "agree") for lab in _ok_answers(run)}


_STANCE_WORD = {"hold": "holds", "change": "changes position", "partly": "partly concedes"}


def event_log(run: dict[str, Any]) -> list[dict[str, Any]]:
    """What happened, in order, for the live log: t (s after opening), kind, who, text, quote. Ballots name the
    response letter only — seat names stay hidden until the ranking is in."""
    t0, starts = _t0(run), stage_starts(run)
    seats = {s["label"]: s for s in run.get("seats") or []}
    live = run.get("live") or {}
    ev: list[dict[str, Any]] = [{"t": 0.0, "kind": "start", "who": "", "text": f"convened · {len(seats)} seats + chairman", "quote": ""}]
    ends: dict[tuple[str, str], float] = {}
    for b in lanes(run):
        if not b["running"]:
            ends[(b["stage"], b["who"])] = b["start"] + b["secs"]
    answers = run.get("answers") or {}
    peeks = live.get("answers") or {}
    for lab, s in seats.items():
        a = answers.get(lab) or {}
        t = ends.get(("stage1", lab))
        if a.get("status") == "ok" or lab in peeks:
            ev.append({"t": t if t is not None else starts.get("review", 0), "kind": "answer", "who": s["name"],
                       "text": "answered", "quote": a.get("summary") or peeks.get(lab, "")})
        elif a.get("status") == "failed":
            ev.append({"t": t if t is not None else starts.get("review", 0), "kind": "fail", "who": s["name"],
                       "text": "no answer", "quote": str(a.get("error") or "")[:160]})
    ballots = list((_ok_reviews(run) or {}).keys()) or list((live.get("ballots") or {}).keys())
    for lab in ballots:
        ev.append({"t": ends.get(("review", lab), starts.get("deliberate", 0)), "kind": "ballot",
                   "who": f"Response {lab}", "text": "ranking in", "quote": ""})
    for e in run.get("timeline") or []:
        t, kind = round(e["t"] - t0, 1), e.get("event")
        if kind == "review" and run.get("ranking"):
            ev.append({"t": t, "kind": "ranking", "who": "", "quote": "",
                       "text": "ranking · " + " · ".join(f"{r['label']} {r['avg_rank']:.1f}" for r in run["ranking"])})
        elif kind in ("review_skipped", "deliberate_skipped"):
            what = "blind vote" if kind == "review_skipped" else "debate"
            ev.append({"t": t, "kind": "skip", "who": "", "text": f"{what} skipped · {e.get('reason', '')}", "quote": ""})
        elif kind == "agenda":
            for d in (run.get("deliberation") or {}).get("disputes") or []:
                ev.append({"t": t, "kind": "agenda", "who": d.get("id", ""), "text": f"{d.get('level', '')} · debated",
                           "quote": d.get("question") or d.get("point") or ""})
        elif kind == "chair" and (run.get("chair") or {}).get("status") == "ok":
            c = run["chair"]
            ev.append({"t": t, "kind": "verdict", "who": "Chairman",
                       "text": f"decided · {c.get('confidence') or ''} confidence".replace(" ·  ", " · "),
                       "quote": c.get("decision") or ""})
        elif kind == "research":
            web = run.get("web") or {}
            n = len(web.get("items") or [])
            ev.append({"t": t, "kind": "ranking" if n else "skip", "who": "Research", "quote": "" if n else web.get("note", ""),
                       "text": (f"{n} web source{'s' if n != 1 else ''} added" + (f" · {e.get('dropped')} unticked" if e.get("dropped") else ""))
                       if n else "no web sources"})
        elif kind == "sources_ask":
            ev.append({"t": t, "kind": "skip", "who": "", "text": f"paused · review the {e.get('items')} web sources", "quote": ""})
        elif kind == "cancelled":
            ev.append({"t": t, "kind": "stop", "who": "", "text": "stopped", "quote": ""})
        elif kind == "limit_ask":
            ev.append({"t": t, "kind": "skip", "who": "", "text": f"paused · {e.get('why', '')}", "quote": ""})
        elif kind == "limit_extended":
            add = float(e.get("add") or 0)
            ev.append({"t": t, "kind": "ranking", "who": "", "quote": "",
                       "text": f"time limit +{add / 60:g} min" if e.get("kind") == "time" else f"budget +${add:,.2f}"})
        elif kind == "limit_declined":
            what = "time" if e.get("kind") == "time" else "budget"
            ev.append({"t": t, "kind": "skip", "who": "", "quote": "",
                       "text": f"no answer · continuing within the {what} limit" if e.get("auto") else f"{what} extension declined"})
    d = run.get("deliberation") or {}
    rounds_t = [e["t"] - t0 for e in _events(run, "deliberate_round")]
    for n, rnd in enumerate(d.get("rounds") or []):
        for name, rep in (rnd.get("replies") or {}).items():
            for x in rep.get("responses") or []:
                ev.append({"t": round(rounds_t[n], 1) if n < len(rounds_t) else starts.get("chair", 0),
                           "kind": "reply", "who": name, "text": f"{_STANCE_WORD.get(x.get('stance'), x.get('stance'))} on {x.get('dispute')}",
                           "quote": x.get("argument") or ""})
    if live.get("stage") == "deliberate" and run.get("status") not in TERMINAL:
        for name, reps in (live.get("replies") or {}).items():
            for x in reps:
                ev.append({"t": round(_now_rel(run), 1), "kind": "reply", "who": name,
                           "text": f"{_STANCE_WORD.get(x.get('stance'), x.get('stance'))} on {x.get('dispute')}", "quote": ""})
    ev.sort(key=lambda x: x["t"])
    return ev


def _now_rel(run: dict[str, Any]) -> float:
    import time as _time
    return _time.time() - _t0(run)


def limit_changes(run: dict[str, Any], kind: str) -> str:
    """'10 min + 5 min added during the run' style tail for the reports ('' when nothing changed)."""
    ext = [x for x in run.get("extensions") or [] if x.get("kind") == kind]
    tl = run.get("timeline") or []
    declined = any(t.get("event") == "limit_declined" and t.get("kind") == kind for t in tl)
    asked = sum(1 for t in tl if t.get("event") == "limit_ask" and t.get("kind") == kind)
    parts = []
    if ext:
        add = sum(float(x.get("add") or 0) for x in ext)
        parts.append(f"{add / 60:g} min added during the run" if kind == "time" else f"${add:,.2f} added during the run")
    if declined:
        parts.append("an extension was declined" if asked else "an offered extension was declined")
    paused = float((run.get("limits") or {}).get("paused") or 0)
    if kind == "time" and paused >= 1:
        parts.append(f"{int(round(paused))} s paused waiting for an answer, not counted")
    return "; ".join(parts)


def public_run(run: dict[str, Any], history: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """The run as the page needs it: the record plus derived facts."""
    out = {k: v for k, v in run.items() if not k.startswith("_")}
    if (run.get("chair") or {}).get("status") == "ok":
        from .followup import name_chair
        out["chair"] = name_chair(run["chair"], run.get("seats") or [])
    d = run.get("deliberation") or {}
    if d.get("disputes") and (out.get("chair") or {}).get("disputes_resolved"):
        from .engine import match_resolved
        hit = match_resolved(out["chair"]["disputes_resolved"], d["disputes"])
        out["chair"] = {**out["chair"], "disputes_resolved": [{**x, "dispute": i} for i, x in hit.items()]}
    if run.get("status") == "done":
        from .report_md import exec_summary
        es = exec_summary(run)
        out["exec"] = {k: es.get(k) for k in ("bottom_line", "reasons", "change", "assembled")} if es["ok"] else None
    out["summary"] = summary(run)
    from .followup import own_brief
    out["background"] = own_brief(run)
    out["insights"] = run_insights(run) if run.get("status") == "done" else []
    out["matrix"] = ballot_matrix(run)
    out["lanes"] = lanes(run)
    out["seat_tokens"] = seat_tokens(run)
    out["totals"] = run_totals(run)
    out["events"] = event_log(run)
    out["stances"] = final_stances(run)
    out["starts"] = stage_starts(run)
    out["seconds"] = stage_seconds(run)
    out["expected_secs"] = expected_seconds(history or [])
    return out

"""Cost estimates and the run budget. Hermes-free.

Cost = tokens × the list price in Hermes's model catalog (USD per million tokens), because the
providers report no cost. A seat on a subscription (sign-in, not an API key) is charged $0; its
list-price equivalent is kept beside it. A seat with no known price is "unknown": it is not
counted, and the page asks the user to accept that before Convene.

Each slot (seat, chairman, describer) carries ``price = {"in", "out", "billing"}`` set by the host.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

IMAGE_TOKENS = 1600          # per picture per call to a model that sees it (an estimate; providers differ)
DESC_TOKENS = 250            # a written picture description
OVERHEAD = 600               # instructions and headings around the context
DESCRIBE_MAX = 1200          # output cap for a picture description
FLOOR_TOKENS = 2048          # never cut a call's output below this to fit a budget
TYPICAL_OUT = {"describe": 400, "stage1": 1800, "review": 900, "chair": 2500, "agenda": 800, "reply": 700}
AGENDA_MAX, REPLY_MAX = 1500, 1200   # output caps for the deliberation calls
CUT_STEPS = (6144, 4096, 3072, 2048)


def tok(text: str) -> int:
    return math.ceil(len(text or "") / 3.5)


def known(slot: dict[str, Any]) -> bool:
    p = slot.get("price") or {}
    return p.get("in") is not None and p.get("out") is not None and p.get("billing") != "unknown"


def cost(slot: dict[str, Any], tin: Optional[float], tout: Optional[float]) -> tuple[float, float, bool]:
    """(charged USD, list-price USD, price known) for one call."""
    if not known(slot):
        return 0.0, 0.0, False
    p = slot["price"]
    lst = (tin or 0) * p["in"] / 1e6 + (tout or 0) * p["out"] / 1e6
    return (0.0 if p.get("billing") == "subscription" else lst), lst, True


# ── what each stage will send ────────────────────────────────────────────────

def _images(run: dict[str, Any]) -> int:
    return len((run.get("evidence_index") or {}).get("images") or [])


def _base(run: dict[str, Any]) -> int:
    from . import research
    files_text = (run.get("evidence_index") or {}).get("tokens", 0) - _images(run) * IMAGE_TOKENS
    return OVERHEAD + tok(run.get("question", "")) + tok(run.get("brief", "")) + tok(run.get("evidence", "")) \
        + max(0, files_text) + research.tokens(run)


def research_calls(run: dict[str, Any]) -> list[tuple[dict, int, int, int]]:
    """The two chairman calls of Research first (queries, passages), while it has not run yet."""
    from . import research
    if not research.wanted(run):
        return []
    q = tok(run.get("question", ""))
    return [(run["chairman"], research.EST_QUERY_IN + q, 150, research.QUERIES_MAX_TOKENS),
            (run["chairman"], research.EST_EXCERPT_IN + q, 1200, research.EXCERPTS_MAX_TOKENS)]


def _mt(run: dict[str, Any]) -> int:
    return int(run.get("budget_tokens") or run.get("max_tokens") or 8192)


def describe_calls(run: dict[str, Any]) -> list[tuple[dict, int, int, int]]:
    slot = run.get("describer")
    if not slot:
        return []
    todo = [i for i in (run.get("evidence_index") or {}).get("images") or []
            if i.get("description") is None and not i.get("describe_error")]
    return [(slot, 400 + tok(run.get("question", "")) + IMAGE_TOKENS, TYPICAL_OUT["describe"], DESCRIBE_MAX)
            for _ in todo]


def stage1_calls(run: dict[str, Any], mt: Optional[int] = None) -> list[tuple[dict, int, int, int]]:
    mt = mt or _mt(run)
    n_img, base = _images(run), _base(run)
    out = []
    for s in run["seats"]:
        pics = n_img * (IMAGE_TOKENS + DESC_TOKENS if s.get("sees_images") else DESC_TOKENS)
        out.append((s, base + tok(s.get("instructions", "")) + pics, min(TYPICAL_OUT["stage1"], mt), mt))
    return out


def _answer_tokens(run: dict[str, Any], mt: int) -> tuple[list[int], list[int]]:
    """(low, high) output tokens of the answers: the real ones once Stage 1 has run."""
    ans = run.get("answers") or {}
    if ans:
        real = [tok(a.get("answer", "")) for a in ans.values() if a.get("status") == "ok"]
        return real, real
    n = len(run["seats"])
    return [min(TYPICAL_OUT["stage1"], mt)] * n, [mt] * n


def review_calls(run: dict[str, Any], mt: Optional[int] = None, typical: bool = False) -> list[tuple[dict, int, int, int]]:
    mt = mt or _mt(run)
    low, high = _answer_tokens(run, mt)
    high = low if typical else high
    ctx = _base(run) + _images(run) * DESC_TOKENS
    ok = [s for s in run["seats"] if (run.get("answers") or {}).get(s["label"], {}).get("status", "ok") == "ok"]
    return [(s, ctx + sum(high), min(TYPICAL_OUT["review"], mt), mt) for s in ok]


def chair_call(run: dict[str, Any], mt: Optional[int] = None, with_review: Optional[bool] = None,
               typical: bool = False) -> tuple[dict, int, int, int]:
    mt = mt or _mt(run)
    low, high = _answer_tokens(run, mt)
    high = low if typical else high
    review = run.get("review") and not (run.get("skipped") or {}).get("review") if with_review is None else with_review
    reviews = len(high) * min(TYPICAL_OUT["review"] if typical else 1500, mt) if review else 0
    if run.get("reviews"):
        reviews = sum(800 for r in run["reviews"].values() if r.get("status") == "ok")
    reviews += _delib_tokens(run)
    return run["chairman"], _base(run) + _images(run) * DESC_TOKENS + sum(high) + reviews, \
        min(TYPICAL_OUT["chair"], mt), mt


def _sum(calls: Iterable[tuple[dict, int, int, int]], high: bool = True) -> dict[str, Any]:
    charged = lst = 0.0
    unknown: list[str] = []
    for slot, tin, lo, hi in calls:
        c, l, k = cost(slot, tin, hi if high else lo)
        charged, lst = charged + c, lst + l
        if not k:
            unknown.append(slot.get("name") or "Chairman")
    return {"charged": charged, "list": lst, "unknown": unknown}


def estimate(run: dict[str, Any]) -> dict[str, Any]:
    """Whole-run range before anything is sent: low = typical answer lengths, high = every call
    writes its maximum. ``list_*`` are the same at list price (subscription seats included)."""
    delib = deliberating(run)
    calls = research_calls(run) + describe_calls(run) + stage1_calls(run) + (review_calls(run) if run.get("review") else []) \
        + ([agenda_call(run)] + round_calls(run) * rounds(run) if delib else []) + [chair_call(run)]
    typical = research_calls(run) + describe_calls(run) + stage1_calls(run) \
        + (review_calls(run, typical=True) if run.get("review") else []) \
        + ([agenda_call(run, typical=True)] if delib else []) + [chair_call(run, typical=True)]
    lo, hi = _sum(typical, high=False), _sum(calls, high=True)
    subs = sorted({s.get("name") or "Chairman" for s, *_ in calls
                   if (s.get("price") or {}).get("billing") == "subscription"})
    return {"low": round(lo["charged"], 4), "high": round(hi["charged"], 4),
            "list_low": round(lo["list"], 4), "list_high": round(hi["list"], 4),
            "unknown": sorted(set(hi["unknown"])), "subscription": subs, "calls": len(calls),
            "deliberation_calls": (1 + len(run["seats"]) * rounds(run)) if delib else 0,
            "input_tokens": sum(c[1] for c in calls)}


# ── spending so far ──────────────────────────────────────────────────────────

def spent(run: dict[str, Any]) -> dict[str, float]:
    u = run.get("usage") or []
    return {"charged": round(sum(c.get("cost_est") or 0 for c in u), 6),
            "list": round(sum(c.get("cost_list") or 0 for c in u), 6),
            "unknown_calls": sum(1 for c in u if c.get("cost_known") is False)}


def limit(run: dict[str, Any]) -> Optional[float]:
    v = (run.get("limits") or {}).get("max_usd")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def left(run: dict[str, Any]) -> Optional[float]:
    lim = limit(run)
    return None if lim is None else lim - spent(run)["charged"]


def fmt(usd: float) -> str:
    if usd <= 0:
        return "$0.00"
    return f"${usd:,.2f}" if usd >= 0.1 else f"${usd:.3f}" if usd >= 0.001 else "<$0.001"


# ── deliberation ─────────────────────────────────────────────────────────────

def deliberating(run: dict[str, Any]) -> bool:
    return (run.get("deliberate") or "never") != "never"


def rounds(run: dict[str, Any]) -> int:
    return 2 if run.get("deliberate_rounds") == 2 else 1


def _delib_tokens(run: dict[str, Any]) -> int:
    d = run.get("deliberation") or {}
    if d.get("rounds"):
        from . import prompts
        return tok(prompts.deliberation_block(run))
    return 0


def agenda_call(run: dict[str, Any], mt: Optional[int] = None, typical: bool = False) -> tuple[dict, int, int, int]:
    mt = mt or _mt(run)
    low, high = _answer_tokens(run, mt)
    return run["chairman"], _base(run) + _images(run) * DESC_TOKENS + sum(low if typical else high), \
        TYPICAL_OUT["agenda"], min(AGENDA_MAX, mt)


def round_calls(run: dict[str, Any], mt: Optional[int] = None, n_round: int = 1) -> list[tuple[dict, int, int, int]]:
    """One round: every member that answered replies to the agenda, having read all the answers."""
    mt = mt or _mt(run)
    _low, high = _answer_tokens(run, mt)
    ok = [s for s in run["seats"] if (run.get("answers") or {}).get(s["label"], {}).get("status", "ok") == "ok"]
    ctx = _base(run) + _images(run) * DESC_TOKENS + sum(high) + AGENDA_MAX + (n_round - 1) * len(ok) * REPLY_MAX
    return [(s, ctx + tok(s.get("instructions", "")), TYPICAL_OUT["reply"], min(REPLY_MAX, mt)) for s in ok]

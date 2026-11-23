"""Check that the evidence a seat or the chairman cites exists.

Seats cite the user's files as [E1], pictures as [I1] and web passages as [W1]. A model can cite a number that
was never given to it (a misremembered or invented source). This module compares every citation in the answers,
the debate replies and the verdict with the IDs the run actually has. It checks that the ID exists, not that the
source says what the model claims: "points to real evidence", not "verified".
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# [E3]  [E2, E5]  [E2–E4]  [E2-4]  [E3; W1]  — one bracket, one or more IDs; a bare number takes the previous letter
_BRACKET = re.compile(r"\[((?:[EIW]\d+)(?:\s*[,;–-]\s*(?:[EIW]?\d+))*)\]")
_PART = re.compile(r"([,;–-])?\s*([EIW]?)(\d+)")
_BARE = re.compile(r"[EIW]\d+")
MAX_RANGE = 50               # [E1-9999] is a typo, not 9999 citations
PREFIX = "Citations to evidence that does not exist: "


def cited(text: str) -> list[str]:
    """Every evidence ID cited in ``text``, in order, ranges expanded, each once."""
    out: list[str] = []
    for m in _BRACKET.finditer(str(text or "")):
        letter, prev = "E", None
        for sep, let, num in _PART.findall(m.group(1)):
            letter = let or letter
            n = int(num)
            if sep in ("–", "-") and prev is not None and prev[0] == letter and 0 < n - prev[1] <= MAX_RANGE:
                ids = [f"{letter}{k}" for k in range(prev[1] + 1, n + 1)]
            else:
                ids = [f"{letter}{n}"]
            for i in ids:
                if i not in out:
                    out.append(i)
            prev = (letter, n)
    return out


def known(run: dict[str, Any]) -> set[str]:
    """The IDs this run has: file sections, pictures, kept web passages, and any [E1]-style labels the user
    wrote in pasted evidence."""
    idx = run.get("evidence_index") or {}
    ids = {str(s.get("id")) for s in (idx.get("sections") or run.get("_sections") or [])}
    ids |= {str(i.get("id")) for i in idx.get("images") or []}
    ids |= {str(x.get("id")) for x in (run.get("web") or {}).get("items") or [] if x.get("keep", True)}
    ids |= set(cited(run.get("evidence") or ""))
    return {i for i in ids if i and i != "None"}


def _strings(x: Any) -> Iterable[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            if not str(k).startswith("_") and k not in ("status", "error", "model", "provider", "usage"):
                yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _ids_in(x: Any) -> list[str]:
    out: list[str] = []
    for s in _strings(x):
        found = cited(s) or ([s.strip()] if _BARE.fullmatch(s.strip()) else [])   # evidence lists hold bare "E3"
        out += [i for i in found if i not in out]
    return out


def audit(run: dict[str, Any]) -> dict[str, Any]:
    """Store and return ``run["citations"]`` = {cited, unknown: [{who, ids}], line}; keep one warning up to date."""
    have = known(run)
    names = {s["label"]: s["name"] for s in run.get("seats") or []}
    per: dict[str, list[str]] = {}

    def add(who: str, x: Any) -> None:
        for i in _ids_in(x):
            per.setdefault(who, [])
            if i not in per[who]:
                per[who].append(i)

    for lab, a in (run.get("answers") or {}).items():
        if a.get("status") == "ok":
            add(names.get(lab, lab), a)
    for rnd in (run.get("deliberation") or {}).get("rounds") or []:
        for who, r in (rnd.get("replies") or {}).items():
            if r.get("status") == "ok":
                add(names.get(who, who), r)
    chair = run.get("chair") or {}
    if chair.get("status") == "ok":
        add("Chairman", {k: v for k, v in chair.items() if k not in ("role_fit",)})

    total = sum(len(v) for v in per.values())
    unknown = [{"who": who, "ids": [i for i in ids if i not in have]} for who, ids in per.items()]
    unknown = [u for u in unknown if u["ids"]]
    bad = sum(len(u["ids"]) for u in unknown)
    line = ""
    if bad:
        line = (PREFIX + "; ".join(f"{u['who']} cites {', '.join('[' + i + ']' for i in u['ids'])}" for u in unknown)
                + f" ({bad} of {total} cited). "
                + ("This run had no numbered evidence." if not have else
                   "Treat the points that rest on them as unsupported."))
    run["citations"] = {"cited": total, "unknown": unknown, "line": line}
    run["warnings"] = [w for w in run.get("warnings") or [] if not w.startswith(PREFIX)] + ([line] if line else [])
    return run["citations"]


def unknown_for(run: dict[str, Any], label: str) -> list[str]:
    """The non-existent IDs a seat (by label) cites, for the chairman's prompt."""
    name = next((s["name"] for s in run.get("seats") or [] if s["label"] == label), label)
    return next((u["ids"] for u in (run.get("citations") or {}).get("unknown") or [] if u["who"] == name), [])

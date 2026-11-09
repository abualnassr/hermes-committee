"""Re-convening a committee on a follow-up: what the new seats are told about the earlier run. Hermes-free."""
from __future__ import annotations

import re
import time
from typing import Any

MAX_ANSWER_CHARS = 2500
TITLE_MAX = 80


def name_labels(text: Any, names: dict[str, str]) -> Any:
    """Replace the anonymous labels the chairman used ("Response B", "B's", a bare "C") with the seat
    names. A bare "A" is left alone (it is usually the article); "Response A" and "A's" are replaced."""
    import re
    if not isinstance(text, str) or not names:
        return text
    labs = "".join(sorted(names))
    out = re.sub(rf"\b[Rr]esponses? ([{labs}])\b", lambda m: names[m.group(1)], text)
    out = re.sub(rf"\b([{labs}])('s|’s)\b", lambda m: names[m.group(1)] + m.group(2), out)
    bare = "".join(l for l in labs if l != "A")
    if bare:
        out = re.sub(rf"(?<![\w.\-/])([{bare}])(?![\w\-/%]|\.\w)", lambda m: names[m.group(1)], out)
    return out


def seat_of(label: Any, seats: list[dict[str, Any]]) -> str:
    """The seat letter a chairman's free-text label points at ("Response A (Brand-2.5)", "B", "Lab-Fast");
    the seat's name wins over a letter, because a chairman can mix up letters but rarely names."""
    text = str(label or "").strip()
    letters = {s["label"] for s in seats if s.get("label")}
    if text in letters:
        return text
    low = text.lower()
    named = [s["label"] for s in seats if s.get("name") and s["name"].lower() in low]
    if len(named) == 1:
        return named[0]
    m = re.search(r"\b(?:response|seat|member)?\s*([A-Z])\b", text, re.IGNORECASE)
    if m and m.group(1).upper() in letters:
        return m.group(1).upper()
    return ""


def own_brief(run: dict[str, Any]) -> str:
    """The brief without the follow-up background the plugin added for the seats."""
    brief = str(run.get("brief") or "")
    if run.get("follow_of") and brief.startswith("This is a follow-up to an earlier committee"):
        cut = brief.find(FOLLOW_END)
        brief = brief[cut + len(FOLLOW_END):] if cut >= 0 else ""
    return brief.strip()


def name_chair(chair: dict[str, Any], seats: list[dict[str, Any]]) -> dict[str, Any]:
    names = {s["label"]: s["name"] for s in seats if s.get("label") and s.get("name")}
    out = dict(chair)
    for k in ("final_answer", "decision", "confidence_note"):
        out[k] = name_labels(out.get(k), names)
    for k in ("reverse_if", "agreement", "open_questions", "next_actions"):
        if isinstance(out.get(k), list):
            out[k] = [name_labels(x, names) for x in out[k]]
    if isinstance(out.get("dissent"), list):
        fixed = []
        for d in out["dissent"]:
            if not isinstance(d, dict):
                continue
            lab = seat_of(d.get("label"), seats)
            fixed.append({**d, "label": lab or str(d.get("label") or ""), "name": names.get(lab) or str(d.get("label") or ""),
                          "view": name_labels(d.get("view"), names)})
        out["dissent"] = fixed
    if out.get("change_from_earlier"):
        out["change_from_earlier"] = name_labels(out["change_from_earlier"], names)
    return out


def clean_title(raw: Any) -> str:
    """A committee name: one line, at most TITLE_MAX characters ('' when none)."""
    return " ".join(str(raw or "").split())[:TITLE_MAX]


FOLLOW_END = ("Treat the earlier verdict as background, not as settled. Judge the follow-up on its own merits "
              "and say plainly if it changes the earlier decision.")


def prev_summary(prev: dict[str, Any]) -> dict[str, Any]:
    """What a follow-up's report needs about the earlier run (kept on the new run)."""
    chair = prev.get("chair") or {}
    ok = chair.get("status") == "ok"
    return {"id": prev.get("id"), "title": prev.get("title") or "", "created": prev.get("created"),
            "question": prev.get("question") or "", "decision": chair.get("decision") if ok else None,
            "confidence": chair.get("confidence") if ok else None}


def context(prev: dict[str, Any]) -> str:
    """Background for every seat: the earlier question, verdict, dissent and next actions.
    The new seats see it as context to test, not as an answer to repeat."""
    chair = prev.get("chair") or {}
    when = time.strftime("%Y-%m-%d", time.localtime(prev.get("created") or time.time()))
    lines = [f"This is a follow-up to an earlier committee ({when}"
             + (f", “{prev['title']}”" if prev.get("title") else "") + ").",
             f"Earlier question: {prev.get('question', '').strip()}"]
    if chair.get("status") == "ok":
        lines.append(f"Earlier decision: {chair.get('decision') or '—'} (confidence: {chair.get('confidence') or 'not stated'})")
        answer = str(chair.get("final_answer") or "").strip()
        if answer:
            cut = answer[:MAX_ANSWER_CHARS] + ("…" if len(answer) > MAX_ANSWER_CHARS else "")
            lines.append(f"Earlier reasoning (chairman's summary):\n{cut}")
        for label, items in (("Dissent recorded", chair.get("dissent")), ("Next actions agreed", chair.get("next_actions")),
                             ("It would reverse if", chair.get("reverse_if"))):
            items = [str(_text(i)) for i in items or [] if _text(i)]
            if items:
                lines.append(label + ":\n" + "\n".join(f"- {i}" for i in items[:8]))
        moved = (prev.get("deliberation") or {}).get("moves") or []
        if moved:
            lines.append("In the earlier deliberation:\n" + "\n".join(
                f"- {m['seat']} {'conceded a point on' if m.get('kept_side', m.get('from') is not None and m.get('from') == m.get('to')) else 'changed view on'} "
                f"“{_point(prev, m['dispute'])}”: {m['because'][:200]}" for m in moved[:6]))
    else:
        lines.append("The earlier committee did not reach a verdict.")
    lines.append(FOLLOW_END)
    return "\n\n".join(lines)


def _point(run: dict[str, Any], did: str) -> str:
    return next((d["point"] for d in (run.get("deliberation") or {}).get("disputes") or [] if d.get("id") == did), did)


def _text(item: Any) -> str:
    if isinstance(item, dict):
        return " — ".join(str(v) for v in item.values() if v)
    return str(item or "").strip()

"""Prompts and JSON schemas for the three stages.

Anonymity rule (Karpathy): Stage-2 reviewers see only "Response A/B/C" labels — never seat
names, roles or model ids. The chairman sees role names (useful context) but never model ids.
Every schema lists all properties as required (strict structured-output providers demand it);
empty strings / lists are allowed.
"""
from __future__ import annotations

from typing import Any

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

STAGE1_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Your full answer in Markdown."},
        "summary": {"type": "string", "description": "Your position in one or two sentences."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "key_points": _STRS,
        "assumptions": _STRS,
        "risks": _STRS,
        "would_change_mind": _STR,
        "evidence_gaps": _STRS,
    },
    "required": ["answer", "summary", "confidence", "key_points", "assumptions", "risks",
                 "would_change_mind", "evidence_gaps"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": _STR, "strengths": _STR, "weaknesses": _STR},
            "required": ["label", "strengths", "weaknesses"],
            "additionalProperties": False,
        }},
        "ranking": {"type": "array", "items": _STR,
                    "description": "Labels from best to worst, e.g. [\"C\", \"A\", \"B\"]."},
    },
    "required": ["evaluations", "ranking"],
    "additionalProperties": False,
}

CHAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "final_answer": {"type": "string", "description": "The committee's final answer in Markdown."},
        "decision": {"type": "string", "description": "The bottom line in one sentence."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence_note": _STR,
        "reverse_if": _STRS,
        "agreement": _STRS,
        "dissent": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": _STR, "view": _STR},
            "required": ["label", "view"],
            "additionalProperties": False,
        }},
        "open_questions": _STRS,
        "action_items": {"type": "array", "description":
                         "The next actions, most urgent first. owner: the role or team if the question or evidence "
                         "names one or it is clearly implied, else an empty string. when: the timing if stated or "
                         "clearly needed (e.g. 'before start-up', 'today'), else an empty string. Never invent people.",
                         "items": {"type": "object",
                                   "properties": {"action": _STR, "owner": _STR, "when": _STR},
                                   "required": ["action", "owner", "when"], "additionalProperties": False}},
        "change_from_earlier": {"type": "string", "description":
                                "Only for a follow-up to an earlier committee: one or two sentences on whether the "
                                "earlier decision changed and why. An empty string otherwise."},
        "executive_summary": {"type": "object", "description":
                              "For a manager who has read nothing else. Plain words, no citations, no jargon, "
                              "no member names.",
                              "properties": {
                                  "bottom_line": {"type": "string", "description":
                                                  "3-4 short sentences: what to do, why, and the main condition."},
                                  "reasons": {"type": "array", "items": {"type": "string"}, "description":
                                              "The 3 strongest reasons, one sentence each."},
                                  "change": {"type": "string", "description":
                                             "Only for a follow-up: at most 12 words on whether the earlier "
                                             "decision changed, e.g. 'Earlier decision confirmed'. Empty otherwise."}},
                              "required": ["bottom_line", "reasons", "change"], "additionalProperties": False},
        "disputes_resolved": {"type": "array", "description":
                              "Only when the committee deliberated: for each disputed point, what you decided and "
                              "which argument decided it. An empty list otherwise.",
                              "items": {"type": "object",
                                        "properties": {"dispute": {"type": "string", "description":
                                                                   "The point's id exactly as listed, e.g. D1."},
                                                       "decided": _STR, "by_argument": _STR},
                                        "required": ["dispute", "decided", "by_argument"],
                                        "additionalProperties": False}},
    },
    "required": ["final_answer", "decision", "confidence", "confidence_note", "reverse_if", "agreement",
                 "dissent", "open_questions", "action_items", "change_from_earlier", "executive_summary",
                 "disputes_resolved"],
    "additionalProperties": False,
}


DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "visible_text": {"type": "string"},
        "unclear": {"type": "string"},
    },
    "required": ["description", "visible_text", "unclear"],
    "additionalProperties": False,
}


def describe(run: dict[str, Any], image: dict[str, Any]) -> tuple[str, str]:
    """One picture → a factual description for committee members who cannot see it."""
    instructions = (
        "You describe a picture for committee members who cannot see it. Be factual and specific: what it "
        "shows, its condition, any damage, wear, leaks or marks, sizes and positions, gauge or meter readings, "
        "labels and tags. Copy any text in the picture exactly into visible_text. Do not interpret beyond what "
        "is visible and do not recommend anything. Put anything you cannot make out in unclear.")
    where = image["source"] + (f" · {image['locator']}" if image.get("locator") else "")
    return instructions, (f"The committee is deciding: {run['question'].strip()}\n\n"
                          f"Describe picture [{image['id']}] ({where}).")


def _files_block(run: dict[str, Any], see_images: bool) -> list[str]:
    secs = run.get("_sections") or []
    imgs = (run.get("evidence_index") or {}).get("images") or []
    parts = []
    if secs:
        parts.append("# Evidence files\nNumbered sections from the user's files. Cite them by number, for "
                     "example [E3], when you rely on them. Do not invent facts beyond them; say so when "
                     "evidence is missing.\n\n" + "\n\n".join(
                         f"## [{s['id']}] {s['source']}" + (f" · {s['locator']}" if s.get("locator") else "")
                         + f"\n{s['text'].strip()}" for s in secs))
    if imgs:
        if see_images:
            parts.append("# Pictures\nThe pictures are attached in this order. Look at them yourself and cite "
                         "them by number, for example [I2]. A written description of each is included in case "
                         "an attachment did not reach you; where you can see the picture, trust your own eyes.\n\n"
                         + "\n\n".join(
                             f"## [{i['id']}] attached picture {n} · {i['source']}"
                             + (f" · {i['locator']}" if i.get("locator") else "")
                             + "\n" + (i.get("description") or "(No description.)") for n, i in enumerate(imgs, 1)))
        else:
            parts.append("# Pictures (described)\nYou cannot see these pictures. Each was described by the "
                         "committee's vision model; treat the description as a second-hand account and cite "
                         "the picture by number, for example [I2].\n\n" + "\n\n".join(
                             f"## [{i['id']}] {i['source']}" + (f" · {i['locator']}" if i.get("locator") else "")
                             + "\n" + (i.get("description") or "(This picture could not be described.)")
                             for i in imgs))
    return parts


def _context_block(run: dict[str, Any], see_images: bool = False) -> str:
    parts = [f"# Question\n{run['question'].strip()}"]
    if run.get("brief"):
        parts.append(f"# Brief from the user\n{run['brief'].strip()}")
    if run.get("evidence"):
        parts.append("# Evidence pack\nUse this evidence. Cite it when you rely on it. "
                     "Do not invent facts beyond it; say so when evidence is missing.\n\n" + run["evidence"].strip())
    parts += _files_block(run, see_images)
    return "\n\n".join(parts)


def stage1(run: dict[str, Any], seat: dict[str, Any], see_images: bool = False) -> tuple[str, str]:
    role = seat.get("instructions", "").strip()
    lines = ["You are one member of a committee answering a question independently. "
             "Other members answer the same question separately; you will not see their answers yet."]
    if role:
        lines.append(f"Your seat: {seat['name']}. {role}")
    lines.append("Give your own best answer. Be specific. State your confidence honestly (0 to 1). "
                 "List assumptions, risks, what would change your mind, and any evidence you would need. "
                 "Use empty lists or strings where a field does not apply.")
    if run.get("decision_focus"):
        lines.append("This is a decision. Make a clear recommendation in your summary.")
    return " ".join(lines), _context_block(run, see_images)


def review(run: dict[str, Any], shown: list[tuple[str, str]]) -> tuple[str, str]:
    instructions = (
        "You are reviewing anonymous responses to a question. Evaluate each response on accuracy and "
        "insight: give its main strengths and weaknesses. Then rank all responses from best to worst. "
        "Judge the content only; you do not know who wrote them. Use each response's label exactly "
        "(for example \"A\"). The ranking must contain every label once.")
    body = [_context_block(run), "# Responses"]
    for label, text in shown:
        body.append(f"## Response {label}\n{text.strip()}")
    return instructions, "\n\n".join(body)


def chairman(run: dict[str, Any], answers: list[dict[str, Any]], critiques: dict[str, list[str]],
             ranking: list[dict[str, Any]]) -> tuple[str, str]:
    instructions = (
        "You are the chairman of a committee. Members answered a question independently, then ranked "
        "each other's anonymous responses. Write the committee's final answer. " + run["chairman_guidance"] + " "
        "Rules: base the answer on the members' responses and the evidence; keep real disagreement visible "
        "(record it under dissent with the member's name as the label) instead of averaging it away; in your text, refer to "
        "members by their name (shown in brackets after each response label), never by a bare letter; do not claim "
        "consensus that is not there; prefer the better-ranked reasoning but correct errors wherever you find "
        "them. Confidence is high, medium or low."
        + (" This is a decision: the decision field must be one clear recommendation." if run.get("decision_focus")
           else " This is a question, not a decision: put the direct answer in one sentence in the decision field."))
    body = [_context_block(run), "# Member responses"]
    for a in answers:
        body.append(f"## Response {a['label']} ({a['name']})\nStated confidence: {a.get('confidence')}\n\n"
                    f"{a['answer'].strip()}")
    if ranking:
        body.append("# Peer ranking (average position, lower is better)\n" + "\n".join(
            f"- Response {r['label']}: {r['avg_rank']:.2f} (first-place votes: {r['first_votes']})" for r in ranking))
    if critiques:
        body.append("# Peer critiques")
        for label, notes in critiques.items():
            body.append(f"## On response {label}\n" + "\n".join(f"- {n}" for n in notes))
    delib = deliberation_block(run)
    if delib:
        instructions += (" The members then deliberated on the disputed points (below). You make the final "
                         "call: for each disputed point, say in disputes_resolved (labelled with its id, e.g. D1) what "
                         "you decided and which argument decided it. Weigh arguments and evidence, not how many members hold a view; "
                         "a member who changed position without a real reason does not add weight. A view still "
                         "held after the debate goes under dissent.")
        body.append(delib)
    return instructions, "\n\n".join(body)


# ── deliberation ─────────────────────────────────────────────────────────────

AGENDA_SCHEMA = {
    "type": "object",
    "properties": {
        "disputes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["action", "claim", "emphasis"],
                          "description": "action: they recommend different actions. claim: they agree or differ "
                                         "on the action but dispute a fact, figure or reading of the evidence the "
                                         "decision rests on. emphasis: only priorities or wording differ."},
                "point": {"type": "string", "description": "The disputed point as a neutral question."},
                "positions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"seat": _STR,
                                   "side": {"type": "string", "description": "A 2-5 word tag for this side; "
                                            "members on the same side get the same tag."},
                                   "view": _STR},
                    "required": ["seat", "side", "view"], "additionalProperties": False}},
                "question": {"type": "string", "description": "What every member must answer about this point."},
            },
            "required": ["level", "point", "positions", "question"], "additionalProperties": False}},
        "reason": {"type": "string", "description": "One sentence: how far apart the members are, and why."},
    },
    "required": ["disputes", "reason"],
    "additionalProperties": False,
}

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "responses": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "dispute": {"type": "string", "description": "The point's id, e.g. D1."},
                "stance": {"type": "string", "enum": ["hold", "change", "partly"]},
                "side": {"type": "string", "description": "The side tag you now take (one of the listed tags)."},
                "argument": {"type": "string", "description": "Your argument. If you change, name what changed "
                                                              "your mind."},
                "evidence": _STRS,
            },
            "required": ["dispute", "stance", "side", "argument", "evidence"], "additionalProperties": False}},
        "updated_position": {"type": "string", "description": "Your recommendation now, in one sentence."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["responses", "updated_position", "confidence"],
    "additionalProperties": False,
}


def agenda(run: dict[str, Any], answers: list[dict[str, Any]]) -> tuple[str, str]:
    """The chairman lists the points the members actually disagree on, before any verdict."""
    instructions = (
        "You chair a committee. Its members answered a question independently. Before any verdict, list the "
        "points on which they genuinely disagree (at most 5, the most important first). For each point give its "
        "level: action (they recommend different actions), claim (a fact, figure or reading of the evidence the "
        "decision rests on is disputed, even if they agree on the action) or emphasis (only priorities or "
        "wording differ). Give every member's position on the point with a short side tag (members who agree "
        "share a tag) and one question every member must answer. Refer to members by name. Report "
        "disagreement faithfully: do not invent it and do not smooth it over. An empty list means they agree.")
    body = [_context_block(run), "# Member responses"]
    for a in answers:
        body.append(f"## {a['name']}\nStated confidence: {a.get('confidence')}\nPosition: {a.get('summary') or ''}"
                    f"\n\n{a['answer'].strip()}")
    return instructions, "\n\n".join(body)


def _agenda_text(disputes: list[dict[str, Any]]) -> str:
    out = []
    for d in disputes:
        sides = sorted({p["side"] for p in d["positions"]})
        out.append(f"## {d['id']} ({d['level']}): {d['point']}\nQuestion: {d['question']}\n"
                   f"Side tags: {' | '.join(sides)}\n"
                   + "\n".join(f"- {p['seat']} [{p['side']}]: {p['view']}" for p in d["positions"]))
    return "\n\n".join(out)


def deliberation_round(run: dict[str, Any], seat: dict[str, Any], answers: list[dict[str, Any]],
                       disputes: list[dict[str, Any]], earlier: list[dict[str, Any]]) -> tuple[str, str]:
    """One member answers the agenda point by point, having read the others' answers (and earlier replies)."""
    role = seat.get("instructions", "").strip()
    instructions = (
        f"You are {seat['name']}, a member of a committee. You answered independently; now the committee "
        "deliberates on the points below, where members disagree. " + (f"Your seat: {role} " if role else "")
        + "For every point give your stance: hold (you keep your position), change (you now take another side) "
        "or partly (you concede part of it). Pick the side tag you now take from the listed tags. Argue from "
        "evidence and reasoning and cite evidence by number where you can. To change position you must name "
        "the argument or evidence that changed your mind; 'the majority agrees' is not a reason, and neither is "
        "deference to another member. Holding a well-founded view against the others is valuable. Answer only "
        "the listed points.")
    body = [_context_block(run), "# Answers"]
    for a in answers:
        tag = " (your answer)" if a["name"] == seat["name"] else ""
        body.append(f"## {a['name']}{tag}\n{a['answer'].strip()}")
    body.append("# Disputed points\n" + _agenda_text(disputes))
    for i, rnd in enumerate(earlier, 1):
        body.append(f"# Replies in round {i}\n" + _replies_text(rnd))
    return instructions, "\n\n".join(body)


def _replies_text(rnd: dict[str, dict[str, Any]]) -> str:
    out = []
    for name, r in rnd.items():
        if r.get("status") != "ok":
            out.append(f"## {name}\n(no reply)")
            continue
        lines = [f"## {name}\nNow recommends: {r.get('updated_position') or ''}"]
        for x in r.get("responses") or []:
            ev = f" [{', '.join(x['evidence'])}]" if x.get("evidence") else ""
            lines.append(f"- {x['dispute']}: {x['stance']} → {x['side']}. {x['argument']}{ev}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def deliberation_block(run: dict[str, Any]) -> str:
    """The deliberation as the chairman reads it before the verdict ('' when there was none)."""
    d = run.get("deliberation") or {}
    if not d.get("rounds"):
        return ""
    parts = ["# Deliberation\n" + _agenda_text(d["disputes"])]
    for i, rnd in enumerate(d["rounds"], 1):
        parts.append(f"## Round {i} replies\n" + _replies_text(rnd["replies"]))
    return "\n\n".join(parts)

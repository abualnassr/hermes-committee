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
        "next_actions": _STRS,
    },
    "required": ["final_answer", "decision", "confidence", "confidence_note", "reverse_if", "agreement",
                 "dissent", "open_questions", "next_actions"],
    "additionalProperties": False,
}


def _context_block(run: dict[str, Any]) -> str:
    parts = [f"# Question\n{run['question'].strip()}"]
    if run.get("brief"):
        parts.append(f"# Brief from the user\n{run['brief'].strip()}")
    if run.get("evidence"):
        parts.append("# Evidence pack\nUse this evidence. Cite it when you rely on it. "
                     "Do not invent facts beyond it; say so when evidence is missing.\n\n" + run["evidence"].strip())
    return "\n\n".join(parts)


def stage1(run: dict[str, Any], seat: dict[str, Any]) -> tuple[str, str]:
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
    return " ".join(lines), _context_block(run)


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
        "(record it under dissent with the response label) instead of averaging it away; do not claim "
        "consensus that is not there; prefer the better-ranked reasoning but correct errors wherever you find "
        "them. Confidence is high, medium or low.")
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
    return instructions, "\n\n".join(body)

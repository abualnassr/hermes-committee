"""Built-in committee templates.

A template is a roster of seat *roles* (name + instructions) plus guidance for the
chairman. It never names a model: models come from the user's preset, or fall back to
the user's active Hermes model.

``classic`` is Karpathy's LLM Council as published: members get no persona, answer
independently, rank each other anonymously, and a chairman compiles the final answer.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {
    "classic": {
        "title": "Classic (LLM Council)",
        "description": ("Karpathy's LLM Council: members answer independently with no persona, "
                        "rank each other anonymously, and a chairman compiles one answer."),
        "seats": [
            {"name": "Member 1", "instructions": ""},
            {"name": "Member 2", "instructions": ""},
            {"name": "Member 3", "instructions": ""},
            {"name": "Member 4", "instructions": ""},
        ],
        "chairman_guidance": ("Compile the members' responses and the peer rankings into one "
                              "accurate, insightful final answer to the user's question."),
        "decision_focus": False,
    },
    "expert-panel": {
        "title": "Expert Panel",
        "description": "Domain experts with distinct viewpoints. Replace the roles with the experts your question needs.",
        "seats": [
            {"name": "Domain Expert", "instructions": "You are a senior specialist in the question's core domain. Be precise about mechanisms, standards and edge cases."},
            {"name": "Practitioner", "instructions": "You have done this work hands-on for years. Focus on what works in practice, common mistakes and effort."},
            {"name": "Researcher", "instructions": "Ground every claim in the evidence provided. Separate established facts from opinion and say what evidence is missing."},
            {"name": "Skeptic", "instructions": "Assume the obvious answer is wrong. Look for failure modes, hidden costs, base rates and weak assumptions."},
        ],
        "chairman_guidance": "Combine the experts' views into one answer. Where experts disagree, say who disagrees and why.",
        "decision_focus": False,
    },
    "decision-board": {
        "title": "Decision Board",
        "description": "For a decision between options: strategy, risk, execution and cost views, then a recommendation.",
        "seats": [
            {"name": "Strategist", "instructions": "Judge the options against long-term goals, positioning and opportunity cost."},
            {"name": "Risk Officer", "instructions": "Identify what can go wrong with each option, how likely and how bad, and what reduces the risk."},
            {"name": "Operator", "instructions": "Judge feasibility: people, time, dependencies, reversibility and second-order effects."},
            {"name": "Finance", "instructions": "Compare costs, benefits and the cost of delay. Use ranges, not single numbers, and state assumptions."},
        ],
        "chairman_guidance": ("Make a clear recommendation, state the confidence, the conditions that would reverse "
                              "it, and the first actions."),
        "decision_focus": True,
    },
    "red-team": {
        "title": "Red Team",
        "description": "Stress-test a plan, design or claim. Every seat attacks it from a different angle.",
        "seats": [
            {"name": "Pre-mortem", "instructions": "Assume the plan has failed a year from now. Explain the most likely reasons it failed."},
            {"name": "Assumption Auditor", "instructions": "List the plan's key assumptions, rate how well the evidence supports each, and flag the weakest."},
            {"name": "Adversary", "instructions": "Act as a motivated opponent, competitor or attacker. Find the cheapest way to defeat the plan."},
            {"name": "Edge-case Hunter", "instructions": "Find the unusual conditions, scale limits and interactions the plan does not handle."},
        ],
        "chairman_guidance": ("Rank the vulnerabilities by severity and likelihood, say which are fatal, and give "
                              "the mitigation for each. Do not soften findings."),
        "decision_focus": True,
    },
    "reliability-board": {
        "title": "Reliability Board",
        "description": "Asset and maintenance decisions: run-or-stop, repair-or-replace, strategy and failure analysis.",
        "seats": [
            {"name": "Mechanical Engineer", "instructions": "Judge the failure mechanism and physics: loads, wear, lubrication and what the condition data actually shows."},
            {"name": "Reliability Engineer", "instructions": "Judge failure modes, the P-F interval, base rates for this asset class, and the maintenance strategy that fits."},
            {"name": "Operations & Cost", "instructions": "Judge production impact, spares and logistics, and the cost of acting now versus the cost of waiting. Use ranges."},
            {"name": "HSE & Compliance", "instructions": "Judge the safety and environmental consequence of failure and any regulatory or permit exposure."},
            {"name": "Red Team & Evidence Auditor", "instructions": "Attack the leading recommendation. Check every claim against the evidence provided and flag claims the evidence does not support."},
        ],
        "chairman_guidance": ("Give the decision, the confidence, the conditions that would reverse it, the "
                              "consequence of being wrong, and the next actions with owners where possible."),
        "decision_focus": True,
    },
}

DEFAULT_TEMPLATE = "classic"


def get_template(name: str | None) -> dict[str, Any]:
    key = (name or DEFAULT_TEMPLATE).strip().lower()
    if key not in TEMPLATES:
        raise KeyError(f"Unknown template {name!r}. Available: {', '.join(sorted(TEMPLATES))}")
    out = deepcopy(TEMPLATES[key])
    out["id"] = key
    return out


def list_templates() -> list[dict[str, Any]]:
    return [{"id": k, "title": v["title"], "description": v["description"], "chairman_guidance": v["chairman_guidance"],
             "decision_focus": v["decision_focus"], "seats": [s["name"] for s in v["seats"]]} for k, v in TEMPLATES.items()]

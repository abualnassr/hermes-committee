"""Turn plugin settings + a request into a concrete roster.

Settings live under ``plugins.entries.hermes-committee.settings`` in config.yaml::

    default_preset: my-board
    presets:
      my-board:
        template: decision-board        # roles come from here unless a seat overrides them
        chairman: {provider: anthropic, model: claude-opus-4}
        seats:                           # by position; name/instructions optional overrides
          - {provider: openai, model: gpt-5}
          - {provider: openrouter, model: google/gemini-3-pro, name: Risk Officer}
        review: true                     # Stage 2 on/off (off = "lite")
        quorum: 3                        # min seats that must answer in Stage 1

A seat with no provider/model runs on the user's active Hermes model. That makes the
plugin work with zero configuration (one model, several roles), at the cost of
correlated answers — the plan says so.
"""
from __future__ import annotations

import math
from typing import Any

from .templates import DEFAULT_TEMPLATE, get_template

MIN_SEATS, MAX_SEATS = 2, 8
STAGE_TIMEOUT_DEFAULT, STAGE_TIMEOUT_MAX = 300, 400   # Hermes cuts a tool call at 420 s
MAX_INSTRUCTIONS = 1200
_VIRTUAL_PROVIDERS = {"moa", "council"}                 # never fan out into another fan-out


class ConfigError(ValueError):
    """A request or preset that cannot be turned into a valid roster."""


def _slot(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if provider.lower() in _VIRTUAL_PROVIDERS:
        raise ConfigError(f"Provider {provider!r} is a virtual fan-out provider and cannot sit on a committee.")
    return {"provider": provider, "model": model}


def model_label(slot: dict[str, Any]) -> str:
    if slot.get("provider") or slot.get("model"):
        return f"{slot.get('provider') or 'default'} / {slot.get('model') or 'default'}"
    return "active model"


def build_roster(settings: dict[str, Any] | None, *, preset: str | None = None, template: str | None = None,
                 seats: list[dict[str, Any]] | None = None, review: bool | None = None) -> dict[str, Any]:
    """Resolve the committee for one run. Raises ConfigError with a user-facing message."""
    settings = settings if isinstance(settings, dict) else {}
    presets = settings.get("presets") if isinstance(settings.get("presets"), dict) else {}
    preset_name = (preset or settings.get("default_preset") or "").strip() or None
    if preset_name and preset_name not in presets:
        if preset:  # explicitly requested
            raise ConfigError(f"Unknown preset {preset_name!r}. Configured: {', '.join(sorted(presets)) or 'none'}.")
        preset_name = None
    p = presets.get(preset_name, {}) if preset_name else {}
    p = p if isinstance(p, dict) else {}

    tpl = get_template(template or p.get("template") or DEFAULT_TEMPLATE)
    preset_seats = [s for s in (p.get("seats") or []) if isinstance(s, dict)]

    if seats:  # ad-hoc roles from the request; models borrowed from the preset by position
        roles = [{"name": str(s.get("name") or f"Seat {i + 1}").strip()[:60],
                  "instructions": str(s.get("instructions") or "").strip()} for i, s in enumerate(seats)]
        model_src = [s if (s.get("provider") or s.get("model")) else (preset_seats[i] if i < len(preset_seats) else {})
                     for i, s in enumerate(seats)]
    elif preset_seats:
        roles = []
        for i, s in enumerate(preset_seats):
            base = tpl["seats"][i] if i < len(tpl["seats"]) else {"name": f"Member {i + 1}", "instructions": ""}
            roles.append({"name": str(s.get("name") or base["name"]).strip()[:60],
                          "instructions": str(s.get("instructions") if s.get("instructions") is not None
                                              else base["instructions"]).strip()})
        model_src = preset_seats
    else:
        roles = [dict(s) for s in tpl["seats"]]
        model_src = [{} for _ in roles]

    if not MIN_SEATS <= len(roles) <= MAX_SEATS:
        raise ConfigError(f"A committee needs {MIN_SEATS}-{MAX_SEATS} seats; got {len(roles)}.")
    for r in roles:
        if len(r["instructions"]) > MAX_INSTRUCTIONS:
            raise ConfigError(f"Instructions for seat {r['name']!r} exceed {MAX_INSTRUCTIONS} characters.")
    names = [r["name"].lower() for r in roles]
    if len(set(names)) != len(names):
        raise ConfigError("Seat names must be unique.")

    roster = [{"index": i, **roles[i], **_slot(model_src[i])} for i in range(len(roles))]
    chairman = _slot(p.get("chairman"))

    n = len(roster)
    quorum = p.get("quorum")
    quorum = int(quorum) if isinstance(quorum, (int, float)) and quorum >= 1 else max(MIN_SEATS, math.ceil(0.75 * n))
    quorum = min(quorum, n)
    timeout = p.get("stage_timeout", settings.get("stage_timeout"))
    timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else STAGE_TIMEOUT_DEFAULT
    do_review = bool(p.get("review", True)) if review is None else bool(review)

    distinct = {(s["provider"], s["model"]) for s in roster}
    warnings = []
    if len(distinct) == 1 and n > 1:
        warnings.append("All seats use the same model, so their answers will be correlated. "
                        "Assign different models in a preset for independent views.")

    return {
        "preset": preset_name,
        "template": tpl["id"],
        "template_title": tpl["title"],
        "chairman_guidance": tpl["chairman_guidance"],
        "decision_focus": tpl["decision_focus"],
        "seats": roster,
        "chairman": chairman,
        "review": do_review,
        "quorum": quorum,
        "stage_timeout": min(timeout, STAGE_TIMEOUT_MAX),
        "max_tokens": int(p.get("max_tokens") or settings.get("max_tokens") or 8192),
        "warnings": warnings,
    }


def planned_calls(roster: dict[str, Any]) -> dict[str, int]:
    n = len(roster["seats"])
    review = n if roster["review"] else 0
    return {"stage1": n, "review": review, "chairman": 1, "total": n + review + 1}

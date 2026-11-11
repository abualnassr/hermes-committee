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
STAGE_TIMEOUT_DEFAULT, STAGE_TIMEOUT_MAX = 360, 400   # Hermes cuts a tool call at 420 s
MAX_INSTRUCTIONS = 1200
MAX_MINUTES_DEFAULT, MAX_MINUTES_MAX = 10, 60       # whole-run time limit; 0 = no limit
MAX_USD_DEFAULT, MAX_USD_MAX = 2.0, 500.0           # whole-run budget in USD; 0 = no limit
_VIRTUAL_PROVIDERS = {"moa", "council"}                 # never fan out into another fan-out


class ConfigError(ValueError):
    """A request or preset that cannot be turned into a valid roster."""


# How long a model thinks before it answers. "default" sends nothing: the provider's own default.
THINKING = ("default", "low", "medium", "high")


def _slot(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if provider.lower() in _VIRTUAL_PROVIDERS:
        raise ConfigError(f"Provider {provider!r} is a virtual fan-out provider and cannot sit on a committee.")
    out = {"provider": provider, "model": model}
    if isinstance(raw.get("vision"), bool):           # seat editor: 'Can see photos' / 'Descriptions only'
        out["vision"] = raw["vision"]
    if str(raw.get("thinking") or "").lower() in THINKING:   # seat editor / chairman row: 'Thinking'
        out["thinking"] = str(raw["thinking"]).lower()
    return out


DELIBERATE_CHOICES = ("material", "action", "always", "never")
DELIBERATE_DEFAULT = "material"


def model_label(slot: dict[str, Any]) -> str:
    if slot.get("provider") or slot.get("model"):
        return f"{slot.get('provider') or 'default'} / {slot.get('model') or 'default'}"
    return "active model"


def _research_cap(v: Any) -> int:
    from . import research as _r
    return _r.cap_of(v)


def build_roster(settings: dict[str, Any] | None, *, preset: str | None = None, template: str | None = None,
                 seats: list[dict[str, Any]] | None = None, review: bool | None = None,
                 chairman: dict[str, Any] | None = None, exact: bool = False,
                 max_minutes: int | float | None = None, max_usd: int | float | None = None,
                 deliberate: str | None = None, deliberate_rounds: int | None = None,
                 chairman_guidance: str | None = None, decision_focus: bool | None = None,
                 research: bool | None = None, research_secs: int | float | None = None) -> dict[str, Any]:
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
        # exact (the Committee page): what the user sees is what runs, so a seat with no model
        # uses the active model instead of borrowing the preset's.
        model_src = [s if (exact or s.get("provider") or s.get("model"))
                     else (preset_seats[i] if i < len(preset_seats) else {}) for i, s in enumerate(seats)]
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
    # An explicit chairman (from the Committee page) wins, and {} means "the active model";
    # None (a chat call that does not mention the chairman) keeps the preset's chairman.
    chair_src = chairman if isinstance(chairman, dict) else p.get("chairman")
    chairman = _slot(chair_src)

    n = len(roster)
    quorum = p.get("quorum")
    quorum = int(quorum) if isinstance(quorum, (int, float)) and quorum >= 1 else max(MIN_SEATS, math.ceil(0.75 * n))
    quorum = min(quorum, n)
    timeout = p.get("stage_timeout", settings.get("stage_timeout"))
    timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else STAGE_TIMEOUT_DEFAULT
    do_review = bool(p.get("review", True)) if review is None else bool(review)
    limit = max_minutes if max_minutes is not None else p.get("max_minutes", settings.get("max_minutes"))
    limit = MAX_MINUTES_DEFAULT if limit is None or isinstance(limit, bool) else limit
    if not isinstance(limit, (int, float)) or limit < 0 or limit > MAX_MINUTES_MAX:
        raise ConfigError(f"The time limit must be 0 (no limit) to {MAX_MINUTES_MAX} minutes; got {limit!r}.")
    usd = max_usd if max_usd is not None else p.get("max_usd", settings.get("max_usd"))
    usd = MAX_USD_DEFAULT if usd is None or isinstance(usd, bool) else usd
    if not isinstance(usd, (int, float)) or usd < 0 or usd > MAX_USD_MAX:
        raise ConfigError(f"The budget must be 0 (no limit) to {MAX_USD_MAX:g} USD; got {usd!r}.")

    delib = deliberate if deliberate not in (None, "") else p.get("deliberate", settings.get("deliberate"))
    delib = DELIBERATE_DEFAULT if delib in (None, "") else str(delib).strip().lower()
    if delib not in DELIBERATE_CHOICES:
        raise ConfigError(f"Deliberate must be one of {', '.join(DELIBERATE_CHOICES)}; got {delib!r}.")
    rounds = deliberate_rounds if deliberate_rounds is not None else p.get("deliberate_rounds", 1)
    if rounds not in (1, 2, "1", "2"):
        raise ConfigError(f"Deliberation rounds must be 1 or 2; got {rounds!r}.")

    # The chairman's brief and the answer/decision mode: the request, then the saved committee, then the type.
    guidance = chairman_guidance if str(chairman_guidance or "").strip() else p.get("chairman_guidance")
    guidance = str(guidance).strip() if str(guidance or "").strip() else tpl["chairman_guidance"]
    if len(guidance) > MAX_INSTRUCTIONS:
        raise ConfigError(f"The chairman's instructions exceed {MAX_INSTRUCTIONS} characters.")
    focus = decision_focus if isinstance(decision_focus, bool) else p.get("decision_focus")
    focus = focus if isinstance(focus, bool) else bool(tpl["decision_focus"])

    distinct = {(s["provider"], s["model"]) for s in roster}
    warnings = []
    if len(distinct) == 1 and n > 1:
        warnings.append("All seats use the same model, so their answers will be correlated. "
                        "Assign different models in a preset for independent views.")

    return {
        "preset": preset_name,
        "template": tpl["id"],
        "template_title": tpl["title"],
        "chairman_guidance": guidance,
        "decision_focus": focus,
        "custom_brief": guidance != tpl["chairman_guidance"] or focus != bool(tpl["decision_focus"]),
        "seats": roster,
        "chairman": chairman,
        "review": do_review,
        "research": research if isinstance(research, bool) else bool(p.get("research", False)),
        "research_secs": _research_cap(research_secs if research_secs not in (None, "") else p.get("research_secs")),
        "deliberate": delib,
        "deliberate_rounds": int(rounds),
        "quorum": quorum,
        "stage_timeout": min(timeout, STAGE_TIMEOUT_MAX),
        "max_tokens": int(p.get("max_tokens") or settings.get("max_tokens") or 8192),
        "limits": {"max_secs": int(round(limit * 60)) or None, "max_usd": round(float(usd), 4) or None},
        "warnings": warnings,
    }


def planned_calls(roster: dict[str, Any]) -> dict[str, int]:
    n = len(roster["seats"])
    review = n if roster["review"] else 0
    pictures = len((roster.get("evidence_index") or {}).get("images") or [])
    delib = (1 + n * (roster.get("deliberate_rounds") or 1)) if (roster.get("deliberate") or "never") != "never" else 0
    web = 2 if roster.get("research") else 0
    return {"research": web, "describe": pictures, "stage1": n, "review": review, "deliberate": delib, "chairman": 1,
            "total": web + pictures + n + review + 1, "total_if_debate": web + pictures + n + review + 1 + delib}

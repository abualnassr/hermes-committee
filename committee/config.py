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
import re
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


def setup_warnings(slots: list, seats: int, quorum: int) -> list:
    """Set-ups that fail easily: a model with no provider, and a committee where every seat must answer."""
    out = []
    bare = [s.get("name") or "Chairman" for s in slots if s.get("model") and not s.get("provider")]
    if bare:
        one = len(bare) == 1
        out.append(f"{', '.join(bare)} name{'s' if one else ''} a model but no provider, so {'its' if one else 'their'} "
                   "calls go to your main provider, which may not serve that model. Set the provider for each seat "
                   "(for example openrouter) to be sure.")
    if seats >= 2 and quorum >= seats:
        out.append(f"With {seats} seats every seat must answer: one refused call stops the verdict. "
                   "Add a seat, or use seats on different providers.")
    return out


def free_model_warnings(slots: list) -> list:
    """Free models (``:free``) are often rate-limited by the provider behind them; when one is refused,
    Hermes answers with the user's main model instead. Said before the run, in plain words."""
    free = [s.get("name") or "Chairman" for s in slots if str(s.get("model") or "").endswith(":free")]
    if not free:
        return []
    who = ", ".join(free)
    return [f"{who} use{'s' if len(free) == 1 else ''} a free model (:free). Free models are often rate-limited; "
            "a refused call is answered by your main model instead (the report says so), or fails if that is "
            "refused too. Paid models are more reliable."]


def active_as(run: dict) -> dict[str, str]:
    """{seat name or 'Chairman': model} for slots set to Hermes's active model, naming the model that really
    answered (from the call records). A seat given the main model on a retry is one of these."""
    slots = [(s["name"], s, "stage1") for s in run.get("seats") or []] + [("Chairman", run.get("chairman") or {}, "chair")]
    out: dict[str, str] = {}
    for who, slot, stage in slots:
        if slot.get("model"):
            continue
        used = [c for c in run.get("usage") or [] if c.get("who") == who and c.get("model")]
        pick = [c for c in used if c.get("stage") == stage] or used
        if pick:
            out[who] = str(pick[-1]["model"])
    return out


def answered_by(run: dict, who: str) -> str:
    """' (answered by <model>)' when Hermes answered this seat with another model (see engine.served_by), or
    ' (<model>)' when the seat was set to Hermes's active model, so a report never just says 'active model'."""
    sw = (run.get("switched") or {}).get(who)
    if sw:
        return f" (answered by {str(sw['model']).rsplit('/', 1)[-1]})"
    real = active_as(run).get(who)
    return f" ({real.rsplit('/', 1)[-1]})" if real else ""


def _same_model_key(model: str) -> str:
    """A model id for 'is this the same model?': no route prefix, variant or :free tier. The same model through
    two providers is one model (same weights, same blind spots); no model named means Hermes's main model."""
    m = re.sub(r"\[[^\]]*\]$", "", str(model or "").strip().lower())
    return m.split(":", 1)[0].rsplit("/", 1)[-1] or "(main model)"


def independence(run: dict[str, Any]) -> dict[str, Any]:
    """How many different models wrote the answers that came in. Four answers from two models are not four
    independent views: a seat answered by Hermes's fallback, or two seats on one model, agree for the same
    reasons and miss the same things.

    Returns {answers, models, fallback: [seat names], shared: [[seat names on one model], ...], line}.
    ``line`` is empty when every answer came from its own model."""
    by: dict[str, list[str]] = {}
    fallback: list[str] = []
    real = active_as(run)
    for s in run.get("seats") or []:
        if ((run.get("answers") or {}).get(s["label"]) or {}).get("status") != "ok":
            continue
        sw = (run.get("switched") or {}).get(s["name"]) or {}
        model = sw.get("model") if "stage1" in (sw.get("stages") or []) else (s.get("model") or real.get(s["name"]))
        if "stage1" in (sw.get("stages") or []):
            fallback.append(s["name"])
        by.setdefault(_same_model_key(model), []).append(s["name"])
    n, m = sum(len(v) for v in by.values()), len(by)
    line = ""
    if n > 1 and m < n:
        line = (f"{n} answers came from {m} model{'s' if m != 1 else ''}"
                + (f" ({len(fallback)} by Hermes's fallback model)" if fallback else "")
                + (": a single-model panel, not an independent committee." if m == 1 else
                   ", so agreement between answers from the same model is not independent confirmation."))
    return {"answers": n, "models": m, "fallback": fallback, "shared": [v for v in by.values() if len(v) > 1],
            "line": line}


CHAIR_SETUP = "The chairman's model ("
CHAIR_RUN = "The chairman ("


def _names_text(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def chair_setup_warning(seats: list[dict[str, Any]], chairman: dict[str, Any]) -> list[str]:
    """Before a run: the chairman is set to a model that a seat also uses (only when both name a model)."""
    if not chairman.get("model"):
        return []
    key = _same_model_key(chairman["model"])
    same = [s["name"] for s in seats if s.get("model") and _same_model_key(s["model"]) == key]
    if not same:
        return []
    return [f"{CHAIR_SETUP}{str(chairman['model']).rsplit('/', 1)[-1]}) is also used by {_names_text(same)}: the "
            f"chairman may favour {'that answer' if len(same) == 1 else 'those answers'} for reading like its own. "
            "Pick a chairman model no seat uses to avoid it."]


def chair_overlap(run: dict[str, Any]) -> dict[str, Any]:
    """After the verdict: seats whose answer came from the same model as the chairman's verdict.
    The chairman does not know which answer is its own model's, but models tend to prefer their own style.
    Returns {model, seats, line}; ``line`` is empty when the chairman shares a model with no answer."""
    chair = run.get("chair") or {}
    if chair.get("status") != "ok":
        return {"model": "", "seats": [], "line": ""}
    real = active_as(run)
    sw = (run.get("switched") or {}).get("Chairman") or {}
    cm = sw.get("model") if "chair" in (sw.get("stages") or []) else \
        ((run.get("chairman") or {}).get("model") or real.get("Chairman") or "")
    if not cm:
        return {"model": "", "seats": [], "line": ""}
    key, same = _same_model_key(cm), []
    for s in run.get("seats") or []:
        if ((run.get("answers") or {}).get(s["label"]) or {}).get("status") != "ok":
            continue
        ssw = (run.get("switched") or {}).get(s["name"]) or {}
        m = ssw.get("model") if "stage1" in (ssw.get("stages") or []) else (s.get("model") or real.get(s["name"]))
        if m and _same_model_key(m) == key:
            same.append(s["name"])
    short = str(cm).rsplit("/", 1)[-1]
    line = (f"{CHAIR_RUN}{short}) is the same model as {_names_text(same)}, so it may favour "
            f"{'that answer' if len(same) == 1 else 'those answers'} for reading like its own. Weigh that part of "
            "the verdict with care.") if same else ""
    return {"model": short, "seats": same, "line": line}


def note_chair_overlap(run: dict[str, Any]) -> None:
    """Keep one up-to-date warning about it; the run's line replaces the set-up warning, which it confirms."""
    line = chair_overlap(run)["line"]
    keep = [w for w in run.get("warnings") or [] if not w.startswith(CHAIR_RUN)]
    if line:
        keep = [w for w in keep if not w.startswith(CHAIR_SETUP)] + [line]
    run["warnings"] = keep


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
                 research: bool | None = None, research_secs: int | float | None = None,
                 solo: bool | None = None) -> dict[str, Any]:
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

    distinct = {_same_model_key(s["model"]) for s in roster}     # one model through several providers is one model
    warnings = []
    if len(distinct) == 1 and n > 1:
        warnings.append("All seats use the same model, so their answers will be correlated. "
                        "Assign different models in a preset for independent views.")
    warnings += free_model_warnings([*roster, chairman]) + setup_warnings([*roster, chairman], len(roster), quorum)
    warnings += chair_setup_warning(roster, chairman)

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
        "solo": solo if isinstance(solo, bool) else bool(p.get("solo", False)),
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
    web = 3 if roster.get("research") else 0
    solo = 1 if roster.get("solo") else 0
    return {"research": web, "describe": pictures, "stage1": n, "solo": solo, "review": review, "deliberate": delib,
            "chairman": 1, "total": web + pictures + n + solo + review + 1,
            "total_if_debate": web + pictures + n + solo + review + 1 + delib}

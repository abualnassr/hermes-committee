"""What the plugin knows about each model, without Hermes internals.

Two sources, kept in ``<plugin data>/models.json``:

* **The Desktop page** sends the price per model and how each provider signs in, read from
  Hermes's public ``model.options`` (``remember_providers``). Chat and terminal runs use the copy.
* **Earlier runs**: a model that rejected a picture is remembered as text-only; evidence that was too
  long for a model is remembered with its size (``learn``).

Photos are tried by default: every seat gets the picture *and* its written description, so a model
that quietly ignores images still has the description. A seat whose provider rejects the picture is
retried with the description only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import _hermes_compat

UNKNOWN: dict[str, Any] = {"vision": None, "context_fail": None, "price_in": None, "price_out": None,
                           "billing": "unknown"}
_BASE: Optional[Path] = None
# Multi-model hubs whose public price lists also price the same model on a direct provider.
HUBS = {"openrouter": "OpenRouter", "nous": "Nous Portal"}
_LOCK = threading.Lock()


def set_base(base: Any) -> None:
    global _BASE
    _BASE = Path(base)


def _path() -> Optional[Path]:
    return _BASE / "models.json" if _BASE else None


def _load() -> dict[str, Any]:
    p = _path()
    if p is None:
        return {"models": {}, "providers": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        d = {}
    return {k: d.get(k) if isinstance(d.get(k), dict) else {} for k in ("models", "providers", "by_name")}


def _save(d: dict[str, Any]) -> None:
    p = _path()
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".models-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def norm(model: Any) -> str:
    """``vendor/model-2.5`` and ``model-2-5[1m]`` → ``model-2-5``."""
    m = str(model or "").lower().split("/")[-1]
    m = re.sub(r"\[[^\]]*\]$", "", m)
    return re.sub(r"[._ ]+", "-", m).strip("-")


def key(provider: str, model: str) -> str:
    return f"{(provider or '').lower()}|{model or ''}"


def active_route() -> tuple[str, str]:
    try:
        from hermes_cli.config import load_config_readonly
        m = (load_config_readonly() or {}).get("model") or {}
    except Exception:  # noqa: BLE001
        return "", ""
    if isinstance(m, str):
        return "", m
    return str(m.get("provider") or ""), str(m.get("default") or m.get("model") or "")


# ── from the page ───────────────────────────────────────────────────────────

def _usd_per_m(v: Any) -> Optional[float]:
    s = str(v if v is not None else "").strip().lower().replace("$", "").replace(",", "")
    if s in ("free", "0"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def billing_from_auth(auth_type: Any) -> str:
    """Sign-in (OAuth, an external app) means a subscription; an API key means pay per use."""
    a = str(auth_type or "").lower()
    if not a:
        return "unknown"
    return "subscription" if a.startswith("oauth") or a == "external_process" else "paid"


def remember_providers(rows: list[dict[str, Any]]) -> int:
    """Store prices and sign-in kind from ``model.options`` rows. Returns the models priced."""
    n = 0
    with _LOCK:
        d = _load()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or "").lower()
            if not slug:
                continue
            prov = d["providers"].setdefault(slug, {})
            prov.update(billing=billing_from_auth(row.get("auth_type")), seen=int(time.time()))
            for model, p in (row.get("pricing") or {}).items():
                if not isinstance(p, dict):
                    continue
                pin, pout = _usd_per_m(p.get("input")), _usd_per_m(p.get("output"))
                if p.get("free") is True:
                    pin = pin if pin is not None else 0.0
                    pout = pout if pout is not None else 0.0
                if pin is None or pout is None:
                    continue
                m = d["models"].setdefault(key(slug, model), {})
                m.update(price_in=pin, price_out=pout, priced=int(time.time()))
                n += 1
                if slug in HUBS and ":" not in str(model):      # skip ':free' and regional variants
                    nm = norm(model)
                    if nm not in d["by_name"] or slug == "openrouter":
                        d["by_name"][nm] = {"in": pin, "out": pout, "via": HUBS[slug]}
        _save(d)
    return n


def set_price(provider: str, model: str, pin: Any, pout: Any) -> None:
    """The user's own price for a pay-per-use model Hermes has no price for (USD per million tokens);
    ``None`` for both forgets it."""
    if not model:
        raise ValueError("no model")
    with _LOCK:
        d = _load()
        m = d["models"].setdefault(key(provider or active_route()[0], model), {})
        if pin is None and pout is None:
            m.pop("user_in", None)
            m.pop("user_out", None)
        else:
            a, b = float(pin), float(pout)
            if a < 0 or b < 0 or a > 10_000 or b > 10_000:
                raise ValueError("prices must be between 0 and 10,000 USD per million tokens")
            m.update(user_in=a, user_out=b)
        _save(d)


def set_billing(provider: str, billing: Optional[str]) -> str:
    """The user's answer for one provider: ``subscription`` ($0), ``paid``, or ``None`` to forget it."""
    slug = (provider or active_route()[0] or "").lower()
    if not slug:
        raise ValueError("no provider")
    with _LOCK:
        d = _load()
        prov = d["providers"].setdefault(slug, {})
        if billing in ("subscription", "paid"):
            prov["chosen"] = billing
        else:
            prov.pop("chosen", None)
        _save(d)
    return slug


# ── from earlier runs ───────────────────────────────────────────────────────

_IMAGE_REJECT = re.compile(r"image|vision|multimodal|multi-modal|image_url|does not support (?:file|attachment)",
                           re.IGNORECASE)
_TOO_LONG = re.compile(r"context(?:[_ ]length| window)|maximum context|too many tokens|prompt is too long|"
                       r"input is too long|token limit|exceeds the (?:maximum|limit)|request too large",
                       re.IGNORECASE)


_THINKING_REJECT = re.compile(r"reasoning|thinking|effort|budget_tokens", re.IGNORECASE)


def looks_like_thinking_rejection(err: Any) -> bool:
    return bool(_THINKING_REJECT.search(str(err or ""))) and not looks_too_long(err)


def looks_too_long(err: Any) -> bool:
    return bool(_TOO_LONG.search(str(err or "")))


def looks_like_image_rejection(err: Any) -> bool:
    return bool(_IMAGE_REJECT.search(str(err or ""))) and not looks_too_long(err)


def learn(provider: str, model: str, **facts: Any) -> None:
    """``vision=False`` after a rejected picture; ``context_fail=<tokens>`` after too-long evidence."""
    if not (provider and model):
        ap, am = active_route()
        provider, model = provider or ap, model or am
    if not model:
        return
    with _LOCK:
        d = _load()
        m = d["models"].setdefault(key(provider, model), {})
        if facts.get("context_fail") and m.get("context_fail"):
            facts["context_fail"] = min(int(m["context_fail"]), int(facts["context_fail"]))
        m.update(facts, learned=int(time.time()))
        _save(d)


# ── what callers read ───────────────────────────────────────────────────────

def refuses_thinking(provider: str, model: str) -> bool:
    """True once this model rejected a reasoning level (cheap: no price lookup, read on every call)."""
    if not (provider and model):
        ap, am = active_route()
        provider, model = provider or ap, model or am
    if not model:
        return False
    try:
        return ((_load().get("models") or {}).get(key(provider, model)) or {}).get("thinking") is False
    except Exception:  # noqa: BLE001 - notes are optional
        return False


def model_caps(provider: str, model: str) -> dict[str, Any]:
    if not (provider and model):
        ap, am = active_route()
        provider, model = provider or ap, model or am
    if not model:
        return dict(UNKNOWN)
    d = _load()
    m = d["models"].get(key(provider, model)) or {}
    prov = d["providers"].get((provider or "").lower()) or {}
    slug = (provider or "").lower()
    chosen = prov.get("chosen")
    detected = _hermes_compat.billing(slug, model)
    bill = chosen or detected or (prov.get("billing") if prov.get("billing") != "unknown" else None) or "unknown"
    pin = pout = via = None
    if m.get("user_in") is not None and m.get("user_out") is not None:
        pin, pout, via = m["user_in"], m["user_out"], "your price"
    elif slug in HUBS and m.get("price_in") is not None and m.get("price_out") is not None:
        pin, pout = m["price_in"], m["price_out"]          # the hub's own list, read by the page
    else:                               # kept so an override to pay-per-use still has a price
        hp = _hermes_compat.price(slug, model)
        if hp:
            pin, pout = hp
        elif m.get("price_in") is not None and m.get("price_out") is not None:
            pin, pout = m["price_in"], m["price_out"]
        elif slug not in HUBS:
            hub = d["by_name"].get(norm(model))
            if hub:
                pin, pout, via = hub.get("in"), hub.get("out"), hub.get("via")
    return {"vision": False if m.get("vision") is False else None,
            "thinking": False if m.get("thinking") is False else None,
            "context_fail": m.get("context_fail"),
            "price_in": pin, "price_out": pout, "price_via": via,
            "billing": bill, "billing_chosen": bool(chosen), "billing_detected": bool(detected) and not chosen}


def price(slot: dict[str, Any], caps: Optional[dict[str, Any]] = None,
          overrides: Optional[dict[str, str]] = None) -> dict[str, Any]:
    caps = caps if caps is not None else model_caps(slot.get("provider") or "", slot.get("model") or "")
    prov = (slot.get("provider") or active_route()[0] or "").lower()
    o = {str(k).lower(): v for k, v in (overrides or {}).items()}.get(prov)
    bill = o if o in ("paid", "subscription") else caps.get("billing") or "unknown"
    if bill == "subscription":          # included in a plan: no price is needed or shown
        return {"in": 0.0, "out": 0.0, "billing": "subscription"}
    if caps.get("price_in") is None or caps.get("price_out") is None:
        return {"in": None, "out": None, "billing": "unknown"}
    if bill == "unknown":
        bill = "paid"                   # a known price with an unknown sign-in counts as pay-per-use
    out = {"in": float(caps["price_in"]), "out": float(caps["price_out"]), "billing": bill}
    if caps.get("price_via"):
        out["via"] = caps["price_via"]
    return out


def sees_images(slot: dict[str, Any], caps: Optional[dict[str, Any]] = None) -> bool:
    """The seat editor's choice wins; else try, unless this model rejected a picture before."""
    if isinstance(slot.get("vision"), bool):
        return slot["vision"]
    caps = caps if caps is not None else model_caps(slot.get("provider") or "", slot.get("model") or "")
    return caps.get("vision") is not False

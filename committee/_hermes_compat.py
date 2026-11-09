"""The only place the plugin reads Hermes internals: list prices and how a provider is paid for.

Hermes has no public seam for either yet. Each call is lazy, read-only and fails soft: if a Hermes
release moves these, the functions return ``None`` and the plugin falls back to the public model
list (``hostcaps``) and to the user's own choice in the budget menu.

* ``price``  — Hermes's pricing tables (``agent.usage_pricing``), the same ones Hermes uses to record
  what a session cost.
* ``billing`` — ``subscription`` when Hermes bills the route as included in a plan, when the provider
  is a subscription by name, or when its saved login is an account sign-in (OAuth); ``paid`` for an
  API key or a credits hub (OpenRouter, Nous Portal). Read with ``read_credential_pool``, which never
  writes Hermes's auth store.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
ENABLED = True                      # tests switch this off to stay offline and deterministic
_PAID_MODES = {"official_models_api"}


def _on() -> bool:
    return ENABLED and not os.environ.get("HERMES_COMMITTEE_NO_HERMES_PRICES")


def billing(provider: str, model: str = "") -> Optional[str]:
    if not _on() or not provider:
        return None
    slug = provider.strip().lower()
    try:
        from agent.usage_pricing import resolve_billing_route
        mode = resolve_billing_route(model or "", provider=slug).billing_mode
        if mode == "subscription_included":
            return "subscription"
        if mode in _PAID_MODES:
            return "paid"
    except Exception:  # noqa: BLE001
        logger.debug("hermes-committee: billing route unavailable", exc_info=True)
    if "subscription" in slug:
        return "subscription"
    try:
        from hermes_cli.auth import read_credential_pool
        entries = read_credential_pool(slug)
    except Exception:  # noqa: BLE001
        logger.debug("hermes-committee: credential pool unavailable", exc_info=True)
        return None
    kinds = {str((e or {}).get("auth_type") or "api_key").lower() for e in entries or [] if isinstance(e, dict)}
    if "oauth" in kinds:
        return "subscription"
    if kinds:
        return "paid"
    return None


def price(provider: str, model: str) -> Optional[tuple[float, float]]:
    """USD per million tokens (input, output) from Hermes's pricing tables, or ``None``."""
    if not _on() or not model:
        return None
    try:
        from agent.usage_pricing import get_pricing_entry
        e = get_pricing_entry(model, (provider or "").strip().lower() or None)
    except Exception:  # noqa: BLE001
        logger.debug("hermes-committee: pricing tables unavailable", exc_info=True)
        return None
    pin = getattr(e, "input_cost_per_million", None) if e is not None else None
    pout = getattr(e, "output_cost_per_million", None) if e is not None else None
    if pin is None or pout is None:
        return None
    return float(pin), float(pout)

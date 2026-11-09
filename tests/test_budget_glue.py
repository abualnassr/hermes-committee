"""The budget through the page routes, the chat tool and prices from the host."""
import asyncio
import importlib
import json

from committee import store

from test_desktop_backend import _load_api

SEATS = [{"name": "Engineer", "instructions": "", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "", "provider": "p2", "model": "m-two"}]
CHAIR = {"provider": "p2", "model": "m-two"}


def _caps(prices):
    return lambda p, m: {"vision": False, "context": 1_000_000, **prices.get(m, {"price_in": None, "price_out": None,
                                                                            "billing": "unknown"})}


def _api(monkeypatch, tmp_path, settings, prices):
    api = _load_api(monkeypatch, tmp_path, settings)
    hc = importlib.import_module("hermes_committee_dashboard_core.hostcaps")
    monkeypatch.setattr(hc, "model_caps", _caps(prices))
    return api


PAID = {"price_in": 2.0, "price_out": 10.0, "billing": "paid"}
SUB = {"price_in": 3.0, "price_out": 15.0, "billing": "subscription"}


def test_estimate_route_counts_subscriptions_at_zero(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, {"m-one": PAID, "m-two": SUB})
    est = asyncio.run(api.estimate({"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR}))
    assert est["limit"] == 2.0 and 0 < est["low"] < est["high"]
    assert est["subscription"] == ["Chairman", "Skeptic"] and not est["unknown"]
    assert est["seats"]["Skeptic"]["billing"] == "subscription"


def test_unknown_price_needs_run_anyway_only_with_a_budget(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, {"m-one": PAID})
    body = {"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR}
    r = asyncio.run(api.create_draft(body))
    assert r["ok"] is False and r["needs_ok"] == "unknown_price" and "Run anyway" in r["error"]
    assert asyncio.run(api.create_draft({**body, "allow_unknown_price": True}))["ok"]
    assert asyncio.run(api.create_draft({**body, "max_usd": 0}))["ok"]          # no budget: nothing to count


def test_draft_refused_when_even_a_short_run_is_over_budget(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, {"m-one": PAID, "m-two": PAID})
    r = asyncio.run(api.create_draft({"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR,
                                      "max_usd": 0.001}))
    assert r["ok"] is False and "Even a short run" in r["error"]


def test_chat_open_prices_every_slot_and_reports_the_range(plugin, tmp_path, monkeypatch):
    mod, _ctx = plugin
    hc = importlib.import_module(mod.__name__ + ".committee.hostcaps")
    monkeypatch.setattr(hc, "model_caps", _caps({m: PAID for m in ("m-one", "m-two", "m-three", "m-four", "m-chair")}))
    out = json.loads(mod.handle_open({"question": "q?", "max_usd": 5}, session_id="s1"))
    assert out["ok"] and "limit $5.00" in out["budget"]
    run = store.load(tmp_path, out["run_id"])
    assert run["limits"]["max_usd"] == 5 and all(s["price"]["billing"] == "paid" for s in run["seats"])
    assert run["estimate"]["high"] > 0


def test_billing_override_from_settings(plugin, tmp_path, monkeypatch):
    mod, ctx = plugin
    hc = importlib.import_module(mod.__name__ + ".committee.hostcaps")
    monkeypatch.setattr(hc, "model_caps", _caps({m: PAID for m in ("m-one", "m-two", "m-three", "m-four", "m-chair")}))
    monkeypatch.setattr(mod, "_settings", lambda: {**ctx_settings(ctx), "billing": {"p1": "subscription"}})
    run = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?"}, session_id="s1"))["run_id"])
    by = {s["provider"]: s["price"]["billing"] for s in run["seats"]}
    assert by["p1"] == "subscription" and by["p2"] == "paid"


def ctx_settings(ctx):
    get = ctx.get_config
    return {"default_preset": get("default_preset", ""), "presets": get("presets", {}) or {}}

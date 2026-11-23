"""When a seat's model is refused, Hermes may answer with the user's main model instead (its fallback).
The run must say so, show that model and price it at that model; refusals become one short line each, and
a run where too few seats answer explains why and what to change."""
import random
import threading

from committee import config as c
from committee import engine as e
from committee import report_md
from committee.engine import Reply
from conftest import FakeLLM

NOOP = lambda _m: None  # noqa: E731
FREE = {"default_preset": "free", "deliberate": "never", "presets": {"free": {
    "template": "decision-board",
    "chairman": {"provider": "hub", "model": "lab/chair-1:free"},
    "seats": [{"provider": "hub", "model": "lab/alpha-2:free"}, {"provider": "hub", "model": "lab/beta-3:free"},
              {"provider": "hub", "model": "lab/gamma-4:free"}, {"provider": "hub", "model": "lab/delta-5:free"}]}}}


class FallbackLLM(FakeLLM):
    """Answers every call, but the models in ``swap`` come back as ``main/main-model`` (Hermes's fallback)."""

    def __init__(self, swap=(), **kw):
        super().__init__(**kw)
        self.swap = set(swap)

    def structured(self, **kw):
        r = super().structured(**kw)
        return Reply(r.content_type, r.parsed, r.text, r.usage, "main", "main-model[1m]") if kw["model"] in self.swap else r


class RefusingLLM(FakeLLM):
    """Raises a provider error shaped like the real ones for the models in ``errors``."""

    def __init__(self, errors, **kw):
        super().__init__(**kw)
        self.errors = errors

    def structured(self, **kw):
        if kw["model"] in self.errors:
            raise self.errors[kw["model"]]
        return super().structured(**kw)


class ApiError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


RATE = ApiError("Error code: 429 - {'error': {'message': 'Provider returned error', 'code': 429, 'metadata': {'raw': "
                "'lab/alpha-2:free is temporarily rate-limited upstream. Please retry shortly, or add your own key: "
                "https://example.test/byok', 'provider_name': 'X'}}}", 429)


def _run(settings):
    return e.new_run(c.build_roster(settings), question="Change it now or later?", rng=random.Random(7))


def _go(run, llm):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP, rng=random.Random(3))


def test_served_by_ignores_route_prefixes_tiers_and_variants():
    slot = {"provider": "hub", "model": "lab/alpha-2:free"}
    assert e.served_by(slot, Reply("json", model="alpha-2")) is None
    assert e.served_by(slot, Reply("json", model="lab/alpha-2")) is None
    assert e.served_by({"model": "brand-chat-3"}, Reply("json", model="brand-chat-3-20261101")) is None
    assert e.served_by({"model": "brand-chat-3[1m]"}, Reply("json", model="brand-chat-3")) is None
    assert e.served_by({"model": ""}, Reply("json", model="anything")) is None          # unset seat: active model
    assert e.served_by(slot, Reply("json", provider="main", model="main-model")) == ("main", "main-model")


def test_a_fallback_answer_is_kept_labelled_and_warned(monkeypatch):
    monkeypatch.setattr(e, "_price_of", lambda p, m: {"in": 3.0, "out": 15.0, "billing": "paid"})
    run = _run(FREE)
    _go(run, FallbackLLM(swap={"lab/alpha-2:free", "lab/gamma-4:free"}))
    assert run["status"] == "chaired" and all(a["status"] == "ok" for a in run["answers"].values())
    names = {s["model"]: s["name"] for s in run["seats"]}
    sw = run["switched"]
    assert set(sw) == {names["lab/alpha-2:free"], names["lab/gamma-4:free"]}
    assert all(x["model"] == "main-model[1m]" and "stage1" in x["stages"] and "review" in x["stages"] for x in sw.values())
    warned = [w for w in run["warnings"] if "fallback model" in w and "answers came from" not in w]
    assert len(warned) == 2                                    # once per seat, not once per call
    summary = [w for w in run["warnings"] if "answers came from" in w]
    assert len(summary) == 1 and "(2 by Hermes's fallback model)" in summary[0]   # and once for the count
    # the switched calls are priced at the model that answered: 100 in + 50 out at $3/$15 per Mtok
    swapped = [u for u in run["usage"] if u.get("assigned")]
    assert swapped and all(abs(u["cost_est"] - 0.00105) < 1e-9 for u in swapped)
    assert all(u.get("assigned") is None for u in run["usage"] if u["who"] not in sw)
    md = report_md.render(run)
    assert md.count("(answered by main-model[1m])") >= 2 and "fallback model" in md


def test_errors_are_one_short_line_per_kind():
    assert e.describe_error(RATE) == "rate-limited (429): lab/alpha-2:free is temporarily rate-limited upstream."
    assert e.describe_error(ApiError("Error code: 402 - {'error': {'message': 'Insufficient credits on this key'}}")) \
        .startswith("out of credit")
    assert e.describe_error(ApiError("bad", 401)).startswith("the provider refused the key (401)")
    assert e.describe_error(ApiError("Error code: 404 - {'error': {'message': 'No endpoints found for x'}}")) \
        .startswith("model not found (404)")
    assert e.describe_error(TimeoutError("Request timed out.")).startswith("timed out")
    assert len(e.describe_error(ApiError("Error code: 429 - " + "x" * 5000))) < 200


def test_a_run_where_seats_are_refused_says_why_and_what_to_change():
    run = _run(FREE)
    errs = {m: RATE for m in ("lab/alpha-2:free", "lab/beta-3:free", "lab/gamma-4:free")}
    _go(run, RefusingLLM(errs))
    assert run["status"] == "failed"
    err = run["error"]
    assert err.startswith("Only 1 of 4 seats answered") and "rate-limited (429)" in err
    assert "Free models are often rate-limited" in err and "share one provider" in err
    assert len(err) < 600 and "https://" not in err and "{'error'" not in err
    assert all(a["error"].startswith("rate-limited (429)") for a in run["answers"].values() if a["status"] == "failed")


def test_free_models_are_warned_before_the_run():
    roster = c.build_roster(FREE)
    assert any("free model (:free)" in w and "Chairman" in w for w in roster["warnings"])
    paid = {**FREE, "presets": {"free": {**FREE["presets"]["free"], "chairman": {"provider": "hub", "model": "lab/chair-1"},
                                         "seats": [{"provider": "hub", "model": f"lab/m-{i}"} for i in range(4)]}}}
    assert not any(":free" in w for w in c.build_roster(paid)["warnings"])


def test_a_router_that_reports_auto_is_not_called_a_switch():
    assert e.served_by({"model": "lab/alpha-2"}, Reply("json", provider="custom", model="auto")) is None
    assert e.served_by({"model": "lab/alpha-2"}, Reply("json", provider="hub", model="hub/auto")) is None
    assert e.served_by({"model": "auto"}, Reply("json", provider="hub", model="lab/beta-3")) is None
    assert e.served_by({"model": "lab/alpha-2"}, Reply("json", provider="main", model="main-model")) == ("main", "main-model")


def test_a_503_from_a_router_without_a_key_is_named_and_advised():
    class InternalServerError(Exception):
        status_code = 503
    err = InternalServerError("Error code: 503 - {'error': {'message': 'No candidate model has a configured, usable "
                              "provider key. Add provider API keys in the dashboard. Soonest reset ~20h.'}}")
    line = e.describe_error(err)
    assert line.startswith("no usable key at the provider (503): No candidate model")
    assert e.describe_error(ApiError("Error code: 502 - {'error': {'message': 'Bad gateway upstream'}}")) \
        .startswith("the provider is unavailable (502)")
    run = {"seats": [{"label": "A", "name": "Seat A", "provider": "", "model": "lab/alpha-2"},
                     {"label": "B", "name": "Seat B", "provider": "", "model": "lab/beta-3"}]}
    why = e.failure_summary(run, {"A": (False, line), "B": (True, {})})
    assert why.startswith("Seat A: no usable key at the provider (503).") and "daily cap" in why


def test_a_seat_with_a_model_but_no_provider_is_warned():
    bare = {"default_preset": "b", "deliberate": "never", "presets": {"b": {
        "template": "classic", "chairman": {"provider": "hub", "model": "lab/chair-1"},
        "seats": [{"provider": "", "model": "lab/alpha-2"}, {"provider": "hub", "model": "lab/beta-3"}]}}}
    w = [x for x in c.build_roster(bare)["warnings"] if "no provider" in x]
    assert len(w) == 1 and "names a model but no provider" in w[0]
    assert not any("no provider" in x for x in c.build_roster(FREE)["warnings"])


def test_a_committee_where_every_seat_must_answer_is_warned():
    assert any("every seat must answer" in w for w in c.setup_warnings([], 2, 2))
    assert any("every seat must answer" in w for w in c.setup_warnings([], 3, 3))
    assert not c.setup_warnings([], 4, 3)

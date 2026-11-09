"""The USD budget: list price × tokens, subscriptions at $0, refusal, cuts and skips."""
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import pricing

NOOP = lambda _m: None  # noqa: E731
PAID = {"in": 2.0, "out": 10.0, "billing": "paid"}          # $ per million tokens
SUB = {"in": 2.0, "out": 10.0, "billing": "subscription"}


def _run(settings, price=PAID, chair_price=None, **kw):
    run = e.new_run(c.build_roster(settings, **kw), question="Run the pump to the outage?", rng=random.Random(3))
    for s in run["seats"]:
        s["price"] = dict(price) if price else None
    run["chairman"]["price"] = dict(chair_price or price) if (chair_price or price) else None
    return run


def _go(run, llm):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP)


def test_defaults_and_bounds(settings):
    assert c.build_roster(settings)["limits"]["max_usd"] == 2.0
    assert c.build_roster(settings, max_usd=0)["limits"]["max_usd"] is None
    with pytest.raises(c.ConfigError):
        c.build_roster(settings, max_usd=-1)


def test_cost_maths_and_subscription_is_free():
    assert pricing.cost({"price": PAID}, 1_000_000, 100_000) == pytest.approx((3.0, 3.0, True))
    assert pricing.cost({"price": SUB}, 1_000_000, 100_000) == pytest.approx((0.0, 3.0, True))
    assert pricing.cost({"price": None}, 1000, 1000) == (0.0, 0.0, False)


def test_every_call_is_priced_from_its_tokens(settings, fake_llm):
    run = _run(settings, max_usd=0)
    _go(run, fake_llm())
    u = run["usage"][0]                                   # the fake reports 100 in / 50 out
    assert u["cost_est"] == pytest.approx(100 * 2 / 1e6 + 50 * 10 / 1e6) and u["cost_known"] is True
    tot = e.usage_totals(run)
    assert tot["est_charged"] == pytest.approx(len(run["usage"]) * u["cost_est"])


def test_subscription_seats_cost_nothing_but_keep_a_list_price(settings, fake_llm):
    run = _run(settings, price=SUB, max_usd=0)
    _go(run, fake_llm())
    tot = e.usage_totals(run)
    assert tot["est_charged"] == 0 and tot["est_list"] > 0


def test_estimate_range_and_unknown_seats(settings):
    run = _run(settings, max_usd=0)
    est = pricing.estimate(run)
    assert 0 < est["low"] < est["high"] and est["calls"] == 4 + 4 + 1 and not est["unknown"]
    run["seats"][1]["price"] = None
    assert pricing.estimate(run)["unknown"] == [run["seats"][1]["name"]]


def test_refused_before_any_call_when_even_short_answers_do_not_fit(settings, fake_llm):
    run = _run(settings, max_usd=0.01)
    llm = fake_llm()
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["status"] == "failed" and "Not started" in run["error"] and "$0.010 limit" in run["error"]
    assert llm.calls == []


def test_answers_are_shortened_to_fit(settings, fake_llm):
    run = _run(settings, max_usd=0.4)                    # 4 × 8192 + chair at $10/M out ($0.48) does not fit
    full = pricing._sum(pricing.stage1_calls(run) + [pricing.chair_call(run, with_review=False)])["charged"]
    assert full > 0.4
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["status"] == "stage1_done" and run["budget_tokens"] < 8192
    assert any("limited to" in w for w in run["warnings"])


def test_the_vote_is_skipped_when_it_no_longer_fits(settings, fake_llm, monkeypatch):
    run = _run(settings, max_usd=1.0)
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    run["usage"].append({"stage": "stage1", "cost_est": 0.80, "cost_list": 0.80, "cost_known": True})   # pricey answers
    _go(run, fake_llm())
    assert run["skipped"].get("review") == "budget" and run["status"] == "chaired"


def test_no_verdict_when_the_chairman_cannot_fit(settings, fake_llm):
    run = _run(settings, max_usd=1.0, review=False)
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    run["usage"].append({"stage": "stage1", "cost_est": 0.999, "cost_list": 0.999, "cost_known": True})
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["status"] == "failed" and "No verdict within the $1.00 limit" in run["error"]


def test_a_call_cut_off_by_time_is_counted_at_its_worst(settings, fake_llm):
    run = _run(settings, max_usd=0, max_minutes=0)
    run["stage_timeout"] = 1
    e.advance(run, fake_llm(slow={"m-four": 3}), threading.Event(), NOOP)
    ab = [u for u in run["usage"] if u.get("abandoned")]
    assert len(ab) == 1 and ab[0]["cost_est"] >= 8192 * 10 / 1e6
    assert e.usage_totals(run)["abandoned"] == 1


def test_unknown_price_is_not_counted_and_never_blocks(settings, fake_llm):
    run = _run(settings, price=None, max_usd=0.01)
    _go(run, fake_llm())
    assert run["status"] == "chaired" and e.usage_totals(run)["est_unknown_calls"] == len(run["usage"])


def test_reports_show_estimated_cost_and_budget(settings, fake_llm, tmp_path):
    from committee import report_md, report_pdf
    run = _run(settings, price=SUB, max_usd=3)
    _go(run, fake_llm())
    md = report_md.render(run)
    assert "Estimated cost: $0.00 (at list price, including subscription seats:" in md and "Budget: $3.00" in md
    assert "| Answers | 4 |" in md and "| Chairman | 1 |" in md
    assert report_pdf.render(run, tmp_path / "r.pdf").stat().st_size > 2000

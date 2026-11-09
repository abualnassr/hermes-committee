"""Extending the time limit or budget during a run: the page's answers, the pause before a stage is skipped,
decline, no answer, and runs from chat (which never pause)."""
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import store

NOOP = lambda _m: None  # noqa: E731
PAID = {"in": 2.0, "out": 10.0, "billing": "paid"}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(e, "_now", clk)
    monkeypatch.setattr(e, "_POLL_S", 0)
    return clk


class Page(threading.Event):
    """Stands in for the Committee page: each poll is one second; answers after `after` polls."""

    def __init__(self, clock, answer=None, after=3):
        super().__init__()
        self.clock, self.answer, self.after, self.polls, self.asked = clock, answer, after, 0, False

    def requests(self):
        self.polls += 1
        self.clock.t += 1
        return [self.answer] if self.answer and self.polls > self.after else []


def _run(settings, draft="d-1", price=None, **kw):
    run = e.new_run(c.build_roster(settings, **kw), question="q", rng=random.Random(1))
    run["draft"] = draft
    if price:
        for s in [*run["seats"], run["chairman"]]:
            s["price"] = dict(price)
    return run


def _short_of_time(settings, fake_llm, clock, **kw):
    run = _run(settings, max_minutes=6, **kw)
    e.advance(run, fake_llm(), threading.Event(), NOOP)            # stage 1 (no page yet)
    clock.t = run["deadline"] - 100                                # not enough left for the vote
    return run


def _events(run, name):
    return [t for t in run["timeline"] if t["event"] == name]


def test_extending_time_saves_the_vote(settings, fake_llm, clock):
    run = _short_of_time(settings, fake_llm, clock)
    deadline = run["deadline"]
    page = Page(clock, {"id": "r1", "kind": "time", "add": 300})
    e.advance(run, fake_llm(), page, NOOP)
    assert not (run.get("skipped") or {}).get("review") and run["next_stage"] == "chair"
    assert run["limits"]["max_secs"] == 660 and run["extensions"][0]["add"] == 300
    assert run["limits"]["paused"] > 0 and run["deadline"] == pytest.approx(deadline + 300 + run["limits"]["paused"])
    assert len(_events(run, "limit_ask")) == 1 and _events(run, "limit_extended")
    assert "ask" not in run["live"]


def test_declining_keeps_todays_behaviour_and_is_not_asked_again(settings, fake_llm, clock):
    run = _short_of_time(settings, fake_llm, clock)
    page = Page(clock, {"id": "r1", "kind": "time", "decline": True})
    e.advance(run, fake_llm(), page, NOOP)
    assert run["skipped"]["review"] == "time" and run["limit_asks"]["time"] == "declined"
    clock.t = run["deadline"] + 1                                   # now even the chairman is short
    e.advance(run, fake_llm(), page, NOOP)
    assert run["status"] == "failed" and len(_events(run, "limit_ask")) == 1


def test_no_answer_continues_after_the_wait(settings, fake_llm, clock):
    run = _short_of_time(settings, fake_llm, clock)
    t0 = clock.t
    e.advance(run, fake_llm(), Page(clock), NOOP)
    assert run["skipped"]["review"] == "time"
    auto = _events(run, "limit_declined")
    assert auto and auto[0].get("auto") and e.ASK_WAIT_S <= clock.t - t0 <= e.ASK_WAIT_S + 3


def test_runs_from_chat_never_pause(settings, fake_llm, clock):
    run = _short_of_time(settings, fake_llm, clock, draft=None)
    page = Page(clock, {"id": "r1", "kind": "time", "add": 300})
    e.advance(run, fake_llm(), page, NOOP)
    assert run["skipped"]["review"] == "time" and not _events(run, "limit_ask") and page.polls == 0


def test_extending_the_budget_saves_the_vote(settings, fake_llm, clock):
    run = _run(settings, price=PAID, max_usd=1.0, max_minutes=0)
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    run["usage"].append({"stage": "stage1", "cost_est": 0.80, "cost_list": 0.80, "cost_known": True})
    e.advance(run, fake_llm(), Page(clock, {"id": "u1", "kind": "usd", "add": 2}), NOOP)
    assert not (run.get("skipped") or {}).get("review") and run["limits"]["max_usd"] == 3.0


def test_an_early_extension_is_applied_without_a_pause(settings, fake_llm, clock):
    run = _short_of_time(settings, fake_llm, clock)
    page = Page(clock, {"id": "r1", "kind": "time", "add": 600}, after=0)     # clicked before the vote
    e.advance(run, fake_llm(), page, NOOP)
    assert run["limits"]["max_secs"] == 960 and not _events(run, "limit_ask")
    assert not (run.get("skipped") or {}).get("review")


def test_requests_are_applied_once_and_capped(settings, clock):
    run = _run(settings, max_minutes=55, max_usd=499)
    run["deadline"] = clock() + 100
    reqs = [{"id": "a", "kind": "time", "add": 600}, {"id": "a", "kind": "time", "add": 600},
            {"id": "b", "kind": "usd", "add": 5}, {"id": "c", "kind": "bogus", "add": 1}, {"id": "d", "kind": "time", "add": "x"}]

    class P(threading.Event):
        def requests(self):
            return reqs
    assert e.apply_requests(run, P()) == ["time", "usd"]
    assert run["limits"]["max_secs"] == 3600 and run["limits"]["max_usd"] == 500 and run["deadline"] == clock() + 400
    assert e.apply_requests(run, P()) == []                           # already applied


def test_store_round_trip(tmp_path):
    rid = "20261109-120000-abcdef"
    d = store.run_dir(tmp_path, rid)
    d.mkdir(parents=True)
    (d / "run.json").write_text("{}", encoding="utf-8")
    assert store.add_request(tmp_path, rid, {"id": "x", "kind": "time", "add": 300})
    assert store.read_requests(tmp_path, rid) == [{"id": "x", "kind": "time", "add": 300}]
    assert store.read_requests(tmp_path, "20261109-120000-ffffff") == []


def test_log_and_reports_say_what_changed(settings, fake_llm, clock):
    from committee import insights, report_md
    run = _short_of_time(settings, fake_llm, clock)
    e.advance(run, fake_llm(), Page(clock, {"id": "r1", "kind": "time", "add": 300}), NOOP)
    while run["next_stage"]:
        e.advance(run, fake_llm(), Page(clock), NOOP)
    run["status"] = "done"
    texts = [x["text"] for x in insights.event_log(run)]
    assert any(t.startswith("paused · The blind vote does not fit") for t in texts) and "time limit +5 min" in texts
    assert "5 min added during the run" in insights.limit_changes(run, "time")
    assert "5 min added during the run" in report_md._limits_line(run)

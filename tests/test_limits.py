"""The whole-run time limit: windows per stage, chairman reserve, degrade instead of fail."""
import random
import threading

import pytest

from committee import config as c
from committee import engine as e

NOOP = lambda _m: None  # noqa: E731


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(e, "_now", clk)
    return clk


def _run(settings, **kw):
    return e.new_run(c.build_roster(settings, **kw), question="q", rng=random.Random(1))


def test_roster_limits_defaults_and_off(settings):
    assert c.build_roster(settings)["limits"]["max_secs"] == 600            # default 10 minutes
    assert c.build_roster(settings, max_minutes=0)["limits"]["max_secs"] is None
    assert c.build_roster(settings, max_minutes=2.5)["limits"]["max_secs"] == 150
    assert c.build_roster({**settings, "max_minutes": 4})["limits"]["max_secs"] == 240
    for bad in (-1, 61, "ten"):
        with pytest.raises(c.ConfigError):
            c.build_roster(settings, max_minutes=bad)


def test_no_limit_keeps_the_per_stage_cap(settings, clock):
    run = _run(settings, max_minutes=0)
    assert e.stage_window(run, "stage1") == run["stage_timeout"]


def test_windows_keep_the_chair_and_review_reserves(settings, clock):
    run = _run(settings, max_minutes=5)
    run["deadline"] = clock() + 300
    assert e.stage_window(run, "stage1") == 300 - 90 - 90
    assert e.stage_window(run, "stage1", reserve_review=False) == 300 - 90
    assert e.stage_window(run, "review") == 300 - 90
    assert e.stage_window(run, "chair") == 300
    clock.t += 250
    assert e.stage_window(run, "chair") == 50
    clock.t += 100
    assert e.stage_window(run, "chair") == 0


def test_a_tight_limit_decides_the_vote_after_the_answers(settings, fake_llm, clock):
    # 180 s: the usual vote reserve doesn't fit (180-90-90 = 0), but quick answers leave room,
    # so the vote is not given up before Stage 1 (a real run showed this waste).
    run = _run(settings, max_minutes=3)
    llm = fake_llm()
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["live"]["ends"] - run["live"]["started"] == pytest.approx(90)   # chair reserve only
    assert run["next_stage"] == "review" and not run["skipped"]
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP)
    assert run["reviews"] and run["status"] == "chaired"


def test_a_tight_limit_skips_the_vote_when_the_answers_were_slow(settings, fake_llm, clock):
    run = _run(settings, max_minutes=3)
    llm = fake_llm()
    e.advance(run, llm, threading.Event(), NOOP)
    clock.t += 80                                       # answers took most of the time
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP)
    assert run["skipped"] == {"review": "time"} and run["reviews"] == {} and run["status"] == "chaired"
    assert not any(call["schema_name"] == "committee_review" for call in llm.calls)


def test_review_is_skipped_when_stage1_used_the_time(settings, fake_llm, clock):
    run = _run(settings, max_minutes=6)
    e.advance(run, fake_llm(), threading.Event(), NOOP)          # stage 1
    assert run["next_stage"] == "review"
    clock.t = run["deadline"] - 100                              # chat overhead ate the time
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["skipped"]["review"] == "time" and run["next_stage"] == "chair"
    assert any(t["event"] == "review_skipped" for t in run["timeline"])


def test_out_of_time_before_the_chairman_is_a_clear_failure(settings, fake_llm, clock):
    run = _run(settings, max_minutes=4, review=False)
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    clock.t = run["deadline"] + 1
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["status"] == "failed"
    assert "No verdict within the 4-minute limit" in run["error"] and "answers are kept" in run["error"]
    assert all(a["status"] == "ok" for a in run["answers"].values())


def test_slow_seats_are_dropped_at_the_window(settings, fake_llm, monkeypatch):
    run = _run(settings, max_minutes=0)
    run["stage_timeout"] = 1
    e.advance(run, fake_llm(slow={"m-four": 3}), threading.Event(), NOOP)
    failed = [a for a in run["answers"].values() if a["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error"].startswith("deadline")


def test_deadline_starts_at_the_first_stage_not_at_open(settings, fake_llm, clock):
    run = _run(settings, max_minutes=10)
    assert run["deadline"] is None
    clock.t += 3600                                   # the chat waited an hour before advancing
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["deadline"] == pytest.approx(clock() + 600)
    assert run["live"]["ends"] <= run["deadline"]


def test_report_states_the_limit_and_the_skipped_vote(settings, fake_llm, clock):
    from committee import report_md
    run = _run(settings, max_minutes=3)
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    clock.t += 80
    while run["next_stage"]:
        e.advance(run, fake_llm(), threading.Event(), NOOP)
    md = report_md.render(run)
    assert "Time limit for the whole run: 3 min" in md and "blind vote was skipped" in md
    assert "Time limit for the whole run: none" in report_md.render(_run(settings, max_minutes=0))


def test_answers_get_all_time_but_the_chairmans(settings, fake_llm, clock):
    run = _run(settings, max_minutes=6)
    run["limits"]["review_reserve"] = 203          # a slow vote in the past must not squeeze the answers
    e.advance(run, fake_llm(), threading.Event(), NOOP)
    assert run["live"]["ends"] - run["live"]["started"] == pytest.approx(360 - 90)

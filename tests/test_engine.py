import random
import threading
import time

import pytest

from committee import config as c
from committee import engine as e

NOOP = lambda _m: None  # noqa: E731


def _run(settings, **kw):
    roster = c.build_roster(settings, **{k: v for k, v in kw.items() if k in ("template", "review", "seats")})
    if "stage_timeout" in kw:
        roster["stage_timeout"] = kw["stage_timeout"]
    return e.new_run(roster, question="Rewrite or modernize?", brief="Small team.", evidence="10 years old.",
                     rng=random.Random(7))


def _go(run, llm, cancel=None):
    stages = []
    while run["next_stage"]:
        stages.append(e.advance(run, llm, cancel or threading.Event(), NOOP, rng=random.Random(3)))
    return stages


def test_full_pipeline_runs_three_stages_in_order(settings, fake_llm):
    run, llm = _run(settings), fake_llm()
    assert _go(run, llm) == ["stage1", "review", "chair"]
    assert run["status"] == "chaired"
    assert all(a["status"] == "ok" for a in run["answers"].values())
    assert len(run["ranking"]) == 4 and run["chair"]["decision"] == "Go incremental."
    assert e.usage_totals(run)["calls"] == 9           # 4 answers + 4 reviews + 1 chair
    assert e.usage_totals(run)["cost_usd"] is None      # never invented


def test_labels_are_random_not_seat_order(settings):
    seen = {tuple(s["label"] for s in e.new_run(c.build_roster(settings), question="q",
                                                 rng=random.Random(i))["seats"]) for i in range(10)}
    assert len(seen) > 1


def test_reviewers_never_see_names_or_models(settings, fake_llm):
    run, llm = _run(settings), fake_llm(self_identify=True)
    _go(run, llm)
    review_calls = [x for x in llm.calls if x.get("schema_name") == "committee_review"]
    assert len(review_calls) == 4
    for call in review_calls:
        blob = call["instructions"] + call["text"]
        for s in run["seats"]:
            assert s["name"] not in blob
            assert s["model"] not in blob, "model id leaked to a reviewer"
    chair = next(x for x in llm.calls if x.get("schema_name") == "committee_verdict")
    for s in run["seats"]:
        assert s["model"] not in chair["text"] and s["model"] not in chair["instructions"]


def test_each_reviewer_gets_a_shuffled_order(settings, fake_llm):
    run, llm = _run(settings), fake_llm()
    _go(run, llm)
    assert len({tuple(r["order"]) for r in run["reviews"].values()}) > 1


def test_quorum_failure_stops_before_review(settings, fake_llm):
    run, llm = _run(settings), fake_llm(fail={"m-one", "m-two"})
    assert _go(run, llm) == ["stage1"]
    assert run["status"] == "failed" and "quorum" in run["error"]


def test_one_seat_down_still_reaches_verdict(settings, fake_llm):
    run, llm = _run(settings), fake_llm(fail={"m-four"})
    _go(run, llm)
    assert run["status"] == "chaired"
    assert sum(a["status"] == "failed" for a in run["answers"].values()) == 1
    assert len(run["reviews"]) == 3


def test_structured_miss_falls_back_to_text(settings, fake_llm):
    run, llm = _run(settings), fake_llm(text_only={"m-two"})
    _go(run, llm)
    lab = next(s["label"] for s in run["seats"] if s["model"] == "m-two")
    assert run["answers"][lab]["status"] == "ok" and run["answers"][lab]["fallback"] is True
    assert run["reviews"][lab]["fallback"] is True and run["reviews"][lab]["repaired"] is True


def test_lite_mode_skips_review(settings, fake_llm):
    run = _run(settings, review=False)
    assert _go(run, fake_llm()) == ["stage1", "chair"]
    assert run["reviews"] == {} and run["ranking"] == []


def test_slow_seat_is_dropped_at_the_deadline(settings, fake_llm):
    run = _run(settings, stage_timeout=1)
    llm = fake_llm(slow={"m-three": 3})
    t0 = time.monotonic()
    e.advance(run, llm, threading.Event(), NOOP)
    assert time.monotonic() - t0 < 2.5
    lab = next(s["label"] for s in run["seats"] if s["model"] == "m-three")
    assert "deadline" in run["answers"][lab]["error"]
    assert run["next_stage"] == "review"


def test_cancel_stops_waiting_quickly(settings, fake_llm):
    run, cancel = _run(settings), threading.Event()
    llm = fake_llm(slow={m: 5 for m in ("m-one", "m-two", "m-three", "m-four")})
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(e.Cancelled):
        e.advance(run, llm, cancel, NOOP)
    assert time.monotonic() - t0 < 1.5
    assert run["status"] == "cancelled" and run["next_stage"] is None
    with pytest.raises(e.Cancelled):
        e.advance(run, llm, threading.Event(), NOOP)


def test_repair_and_text_ranking():
    assert e.repair_ranking(["Response C", "a", "a", "Z"], ["A", "B", "C"]) == (["C", "A", "B"], True)
    assert e.repair_ranking(["A", "B"], ["A", "B"]) == (["A", "B"], False)
    txt = "Response A is fine.\nFINAL RANKING:\n1. Response C\n2. Response A\n3. Response B"
    assert e.parse_text_ranking(txt) == ["C", "A", "B"]


def test_aggregate_average_rank_and_first_votes():
    reviews = {"A": {"status": "ok", "ranking": ["B", "A", "C"]},
               "B": {"status": "ok", "ranking": ["B", "C", "A"]},
               "C": {"status": "failed"}}
    rows = e.aggregate(reviews, ["A", "B", "C"])
    assert [r["label"] for r in rows] == ["B", "A", "C"]
    assert rows[0] == {"label": "B", "avg_rank": 1.0, "first_votes": 2, "votes": 2}


def test_scrub_identity_keeps_ordinary_words(settings):
    run = _run(settings)
    risk_seat = next(s for s in run["seats"] if s["name"] == "Risk Officer")
    text = f"As the Risk Officer using {risk_seat['model'].upper()}, the risk officer view is cautious."
    out = e.scrub_identity(run, text)
    assert "Risk Officer" not in out and risk_seat["model"].upper() not in out
    assert "risk officer view" in out

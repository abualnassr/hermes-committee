"""Deliberation stage: the trigger, the rounds, who moved, and the time/budget rules."""
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import pricing, prompts

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


def _run(settings, deliberate="material", rounds=1, **kw):
    roster = c.build_roster(settings, deliberate=deliberate, deliberate_rounds=rounds,
                            **{k: v for k, v in kw.items() if k in ("review", "max_minutes", "max_usd")})
    return e.new_run(roster, question="Run GB-7 to the outage or swap now?", rng=random.Random(7))


def _go(run, llm):
    stages = []
    while run["next_stage"]:
        stages.append(e.advance(run, llm, threading.Event(), NOOP, rng=random.Random(3)))
    return stages


def _names(run):
    return [s["name"] for s in run["seats"]]


def _agenda(run, level="claim", split=1):
    """One disputed point: the first `split` seats on side 'yes', the rest on 'no'."""
    n = _names(run)
    return {"disputes": [{"level": level, "point": "Is the break-even claim right?",
                          "positions": [{"seat": s, "side": "yes" if i < split else "no", "view": f"{s} view"}
                                        for i, s in enumerate(n)],
                          "question": "What shows the break-even point?"}],
            "reason": "They dispute the economics."}


def test_material_debates_a_claim_even_when_the_action_is_agreed(settings, fake_llm):
    run = _run(settings)
    llm = fake_llm(agenda=_agenda(run, "claim"))
    assert _go(run, llm) == ["stage1", "review", "deliberate", "chair"]
    d = run["deliberation"]
    assert [x["id"] for x in d["disputes"]] == ["D1"] and len(d["rounds"]) == 1
    assert sum(1 for x in llm.calls if x["schema_name"] == "committee_reply") == 4
    assert not run["skipped"].get("deliberate") and run["status"] == "chaired"
    chair = next(x for x in llm.calls if x["schema_name"] == "committee_verdict")
    assert "# Deliberation" in chair["text"] and "disputes_resolved" in chair["instructions"]


def test_action_setting_skips_a_claim_only_dispute(settings, fake_llm):
    run = _run(settings, deliberate="action")
    llm = fake_llm(agenda=_agenda(run, "claim"))
    _go(run, llm)
    assert run["skipped"]["deliberate"] == "same action"
    assert not any(x["schema_name"] == "committee_reply" for x in llm.calls)
    chair = next(x for x in llm.calls if x["schema_name"] == "committee_verdict")
    assert "# Deliberation" not in chair["text"]


def test_agreement_goes_straight_to_the_verdict(settings, fake_llm):
    run = _run(settings)
    llm = fake_llm()                                        # empty agenda
    _go(run, llm)
    assert run["skipped"]["deliberate"] == "no material disagreement"
    assert sum(1 for x in llm.calls if x["schema_name"] == "committee_agenda") == 1
    assert not any(x["schema_name"] == "committee_reply" for x in llm.calls)


def test_emphasis_only_is_not_debated_but_always_is(settings, fake_llm):
    run = _run(settings)
    _go(run, fake_llm(agenda=_agenda(run, "emphasis")))
    assert run["skipped"]["deliberate"] == "no material disagreement"
    assert run["deliberation"]["noted"] and not run["deliberation"]["disputes"]
    run2 = _run(settings, deliberate="always")
    _go(run2, fake_llm(agenda=_agenda(run2, "emphasis")))
    assert len(run2["deliberation"]["rounds"]) == 1


def test_never_is_todays_flow(settings, fake_llm):
    run = _run(settings, deliberate="never")
    llm = fake_llm(agenda=_agenda(run, "action"))
    assert _go(run, llm) == ["stage1", "review", "chair"]
    assert not any(x["schema_name"] == "committee_agenda" for x in llm.calls)


def test_the_plugin_decides_not_the_model():
    act = [{"level": "action"}]
    claim = [{"level": "claim"}]
    emph = [{"level": "emphasis"}]
    assert e.should_debate(act, "action") and e.should_debate(claim, "material")
    assert not e.should_debate(claim, "action") and not e.should_debate(emph, "material")
    assert e.should_debate(emph, "always") and not e.should_debate([], "always")
    assert not e.should_debate(act, "never")


def test_agenda_is_cleaned_ordered_and_capped(settings):
    seats = [{"name": "Reliability"}, {"name": "Cost"}]
    parsed = {"disputes": [
        {"level": "emphasis", "point": "Tone", "positions": [], "question": "q"},
        {"level": "claim", "point": "C1", "positions": [{"seat": "reliability", "side": "a", "view": "v"},
                                                         {"seat": "Unknown", "side": "b", "view": "v"}],
         "question": "q"},
        {"level": "action", "point": "A1", "positions": [], "question": "q"},
        {"level": "claim", "point": "C2", "positions": [], "question": "q"},
        {"level": "bogus", "point": "C3", "positions": [], "question": "q"},
    ], "reason": "r"}
    ag = e.norm_agenda(parsed, seats, "material")
    assert [d["point"] for d in ag["disputes"]] == ["A1", "C1", "C2"]          # action first, capped at 3
    assert [d["id"] for d in ag["disputes"]] == ["D1", "D2", "D3"]
    assert ag["disputes"][1]["positions"] == [{"seat": "Reliability", "side": "a", "view": "v"}]  # unknown dropped
    assert {d["point"] for d in ag["noted"]} == {"Tone", "C3"}


def test_who_changed_their_mind_is_computed(settings, fake_llm):
    run = _run(settings)
    n = _names(run)
    mover = next(s for s in run["seats"] if s["name"] == n[0])
    llm = fake_llm(agenda=_agenda(run, "claim", split=1), replies={mover["model"]: {
        "responses": [{"dispute": "D1", "stance": "change", "side": "no", "argument": "The outage plan [E2] shows "
                       "the swap is cheaper.", "evidence": ["E2"]}],
        "updated_position": "Swap now.", "confidence": 0.6}})
    _go(run, llm)
    mv = run["deliberation"]["moves"]
    assert mv == [{"seat": n[0], "dispute": "D1", "round": 1, "stance": "change", "from": "yes", "to": "no",
                   "because": "The outage plan [E2] shows the swap is cheaper.", "kept_side": False}]
    assert run["deliberation"]["open"] == []                 # everyone on 'no' now


def test_round_two_only_for_points_still_open(settings, fake_llm):
    run = _run(settings, rounds=2)
    llm = fake_llm(agenda=_agenda(run, "action", split=2))   # everyone holds → still open
    _go(run, llm)
    assert len(run["deliberation"]["rounds"]) == 2
    assert run["deliberation"]["open"] == ["D1"]
    second = [x for x in llm.calls if x["schema_name"] == "committee_reply"][-1]
    assert "# Replies in round 1" in second["text"]


def test_round_two_is_not_run_when_settled(settings, fake_llm):
    run = _run(settings, rounds=2)
    n = _names(run)
    mover = next(s for s in run["seats"] if s["name"] == n[0])
    llm = fake_llm(agenda=_agenda(run, "action", split=1), replies={mover["model"]: {
        "responses": [{"dispute": "D1", "stance": "change", "side": "no", "argument": "Convinced by the data.",
                       "evidence": []}], "updated_position": "Swap.", "confidence": 0.7}})
    _go(run, llm)
    assert len(run["deliberation"]["rounds"]) == 1


def test_members_see_names_and_the_agenda_but_no_model_ids(settings, fake_llm):
    run = _run(settings)
    llm = fake_llm(agenda=_agenda(run, "action", split=2))
    _go(run, llm)
    for call in [x for x in llm.calls if x["schema_name"] in ("committee_reply", "committee_agenda")]:
        blob = call["instructions"] + call["text"]
        for s in run["seats"]:
            assert s["model"] not in blob
        assert "## Response A" not in blob
    reply = next(x for x in llm.calls if x["schema_name"] == "committee_reply")
    assert "Side tags: no | yes" in reply["text"] and "majority agrees" in reply["instructions"]


def test_a_failing_member_does_not_stop_the_round(settings, fake_llm):
    run = _run(settings)
    bad = run["seats"][2]["model"]

    class Flaky(fake_llm):
        def structured(self, **kw):
            if kw["schema_name"] == "committee_reply" and kw["model"] == bad:
                raise RuntimeError("down")
            return super().structured(**kw)
    _go(run, Flaky(agenda=_agenda(run, "action", split=2)))
    replies = run["deliberation"]["rounds"][0]["replies"]
    assert replies[run["seats"][2]["name"]]["status"] == "failed"
    assert sum(r["status"] == "ok" for r in replies.values()) == 3 and run["status"] == "chaired"


def test_a_failed_agenda_skips_to_the_verdict(settings, fake_llm):
    run = _run(settings)

    class NoAgenda(fake_llm):
        def structured(self, **kw):
            if kw["schema_name"] == "committee_agenda":
                raise RuntimeError("chair down for this call")
            return super().structured(**kw)
    _go(run, NoAgenda())
    assert run["skipped"]["deliberate"] == "agenda failed" and run["status"] == "chaired"


def test_deliberation_is_skipped_when_time_is_short(settings, fake_llm, clock):
    run = _run(settings, max_minutes=4)
    llm = fake_llm(agenda=_agenda(run, "action", split=2))
    e.advance(run, llm, threading.Event(), NOOP)              # stage 1
    e.advance(run, llm, threading.Event(), NOOP)              # vote
    assert run["next_stage"] == "deliberate"
    clock.t = run["deadline"] - 100                            # only the chairman's share is left
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["skipped"]["deliberate"] == "time" and run["next_stage"] == "chair"
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["status"] == "chaired"


def test_deliberation_is_skipped_when_the_budget_is_short(settings, fake_llm, monkeypatch):
    run = _run(settings, max_usd=1)
    llm = fake_llm(agenda=_agenda(run, "action", split=2))
    e.advance(run, llm, threading.Event(), NOOP)
    e.advance(run, llm, threading.Event(), NOOP)
    seen = []
    monkeypatch.setattr(e, "_over", lambda r, calls: (seen.append(len(calls)) or True, 1.5))
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["skipped"]["deliberate"] == "budget" and seen == [1 + 4 + 1]   # agenda + a round + the verdict
    assert not any(x["schema_name"] == "committee_agenda" for x in llm.calls)


def test_estimate_counts_the_possible_debate(settings):
    on = _run(settings)
    off = _run(settings, deliberate="never")
    on_e, off_e = pricing.estimate(on), pricing.estimate(off)
    assert on_e["calls"] == off_e["calls"] + 1 + 4 and on_e["deliberation_calls"] == 5
    two = pricing.estimate(_run(settings, rounds=2))
    assert two["calls"] == off_e["calls"] + 1 + 8


def test_config_validates_the_setting(settings):
    assert c.build_roster(settings, deliberate="ALWAYS")["deliberate"] == "always"
    assert c.build_roster({**settings, "deliberate": None})["deliberate"] == "material"   # the plugin default
    with pytest.raises(c.ConfigError):
        c.build_roster(settings, deliberate="sometimes")
    with pytest.raises(c.ConfigError):
        c.build_roster(settings, deliberate_rounds=3)


def test_verdict_keeps_disputes_resolved():
    parsed = {"final_answer": "x", "decision": "d", "confidence": "high", "confidence_note": "", "reverse_if": [],
              "agreement": [], "dissent": [], "open_questions": [], "next_actions": [], "change_from_earlier": "",
              "disputes_resolved": [{"dispute": "D1", "decided": "Swap now", "by_argument": "Cost data [E2]"}]}
    assert e._norm_chair(parsed, "")["disputes_resolved"][0]["by_argument"] == "Cost data [E2]"
    assert "disputes_resolved" in prompts.CHAIR_SCHEMA["required"]

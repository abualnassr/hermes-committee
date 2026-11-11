"""Seats keep to their roles: the role comes first with a scope rule, the chairman sees the roles and
notes who drifted; the blind vote never sees a role."""
import random
import threading

from committee import config as c
from committee import engine as e
from committee import prompts, report_md

from conftest import FakeLLM


def _run(settings, llm, template=None):
    roster = c.build_roster(settings, template=template) if template else c.build_roster(settings)
    run = e.new_run(roster, question="Run gearbox GB-7 to the outage or change it at the weekend stop?",
                    rng=random.Random(3))
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda m: None)
    return run


def _calls(llm, schema):
    return [x for x in llm.calls if x.get("schema_name") == schema]


def test_role_comes_first_with_the_scope_rule(settings):
    llm = FakeLLM()
    run = _run(settings, llm)                       # decision-board: every seat has instructions
    for call in _calls(llm, "committee_answer"):
        assert call["instructions"].startswith("# Your seat: ")
        assert prompts.SCOPE_RULE in call["instructions"]


def test_no_role_no_scope_rule(settings):
    llm = FakeLLM()
    _run(settings, llm, template="classic")
    for call in _calls(llm, "committee_answer"):
        assert "# Your seat" not in call["instructions"] and prompts.SCOPE_RULE not in call["instructions"]
        assert call["instructions"].startswith("You are one member")


def test_chairman_sees_roles_and_the_vote_does_not(settings):
    llm = FakeLLM()
    run = _run(settings, llm)
    roles = [s["instructions"] for s in run["seats"]]
    chair = _calls(llm, "committee_verdict")[0]
    assert all(r[:60] in chair["text"] for r in roles)
    assert "Weigh each member mainly within its role" in chair["instructions"]
    for review in _calls(llm, "committee_review"):
        assert not any(r[:40] in review["text"] or r[:40] in review["instructions"] for r in roles)


def test_debate_carries_the_role(settings):
    settings["deliberate"] = "always"
    agenda = {"reason": "split", "noted": [], "disputes": [{"point": "Is the wear fast?", "question": "Fast?", "level": "claim",
              "positions": [{"seat": s, "side": "x", "view": "v"} for s in ("Strategist", "Operator", "Finance", "Skeptic")]}]}
    llm = FakeLLM(agenda=agenda)
    run = _run(settings, llm)
    replies = _calls(llm, "committee_reply")
    assert replies and all(x["instructions"].startswith("# Your seat: ") and "Argue from your role." in x["instructions"]
                           for x in replies)
    assert run["status"] in ("done", "chaired")


def test_role_fit_is_matched_shown_and_optional(settings):
    names = None
    llm = FakeLLM()
    run = _run(settings, llm)
    assert run["chair"]["role_fit"] == []           # the chairman wrote none: no marks, no failure
    names = [s["name"] for s in run["seats"]]
    fit = [{"seat": f"Response A ({names[0]})", "fit": "kept", "note": "Stayed on strategy."},
           {"seat": names[1], "fit": "drifted", "note": "Mostly argued cost, not operations."},
           {"seat": "Nobody", "fit": "drifted", "note": "unknown seat is dropped"},
           {"seat": names[2], "fit": "maybe", "note": "bad value is dropped"}]
    out = e.norm_role_fit(fit, run["seats"])
    assert [(x["seat"], x["fit"]) for x in out] == [(names[0], "kept"), (names[1], "drifted")]
    run["chair"]["role_fit"] = out
    md = report_md.render(run)
    assert "Drifted from role (chairman's note):" in md and "Mostly argued cost" in md
    assert "Role (chairman's note): kept to it · Stayed on strategy." in md.replace("_", "")


def test_role_fit_ignored_for_seats_without_instructions(settings):
    llm = FakeLLM()
    run = _run(settings, llm, template="classic")
    assert e.norm_role_fit([{"seat": run["seats"][0]["name"], "fit": "drifted", "note": "x"}], run["seats"]) == []


def test_verdict_schema_lists_role_fit():
    assert "role_fit" in prompts.CHAIR_SCHEMA["properties"] and "role_fit" in prompts.CHAIR_SCHEMA["required"]

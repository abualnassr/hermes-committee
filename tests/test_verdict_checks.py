"""0.5.1: the chairman may defer on one fact (rule checked in code); open disputes the verdict skips are named."""
import random
import threading

import pytest

from committee import config as c, engine as e, report_md

DEFER = {"needed": "Can a crane be on site by Saturday 06:00?", "how_to_find": "Phone the hire firm.",
         "if_yes": "Change GB-7 at the weekend stop.", "if_no": "Run GB-7 to the shutdown with weekly oil samples."}


def test_a_defer_within_the_rule_is_kept():
    assert e._norm_defer(DEFER) == {"defer": DEFER}


@pytest.mark.parametrize("d, why", [
    ({**DEFER, "if_no": "change GB-7 at the weekend stop!"}, "same decision either way"),
    ({**DEFER, "if_no": ""}, "did not give a decision for each answer"),
])
def test_a_defer_outside_the_rule_is_refused(d, why):
    out = e._norm_defer(d)
    assert "defer" not in out and why in out["defer_refused"]


def test_deciding_now_has_no_defer():
    assert e._norm_defer({"needed": "", "how_to_find": "", "if_yes": "", "if_no": ""}) == {}
    assert e._norm_defer(None) == {}


def _verdict_run(settings, fake_llm, verdict, agenda=None):
    settings["deliberate"] = "always"
    fake = fake_llm(agenda=agenda) if agenda else fake_llm()
    real = fake.structured

    def structured(**kw):
        r = real(**kw)
        if kw["schema_name"] == "committee_verdict":
            r.parsed = {**r.parsed, **verdict}
        return r
    fake.structured = structured
    run = e.new_run(c.build_roster(settings), question="Change GB-7 now?", rng=random.Random(2))
    while run["next_stage"]:
        e.advance(run, fake, threading.Event(), lambda _m: None)
    run["status"] = "done"
    return run


def test_a_deferred_verdict_reaches_the_reports(settings, fake_llm, tmp_path):
    run = _verdict_run(settings, fake_llm, {"decision": "Not yet: phone the crane hire firm.", "defer": DEFER})
    assert run["chair"]["defer"] == DEFER
    md = report_md.render(run)
    assert "**Not yet. Find out:** Can a crane be on site" in md and "**If no:** Run GB-7 to the shutdown" in md
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "NOT YET: ONE FACT DECIDES IT" in text and "If yes: Change GB-7 at the weekend stop." in text


def test_a_refused_defer_is_a_warning_and_the_decision_stands(settings, fake_llm):
    run = _verdict_run(settings, fake_llm, {"defer": {**DEFER, "if_no": DEFER["if_yes"]}})
    assert "defer" not in run["chair"] and run["chair"]["decision"] == "Go incremental."
    assert any(w.startswith(e.DEFER_WARN) and "same decision either way" in w for w in run["warnings"])


def _open_run(settings, fake_llm, verdict):
    seats = c.build_roster(settings)["seats"]
    agenda = {"disputes": [
        {"level": "action", "point": "Should the gearbox swap wait for the shutdown?", "question": "When?",
         "positions": [{"seat": s["name"], "side": "now" if i else "wait", "view": "v"} for i, s in enumerate(seats)]},
        {"level": "claim", "point": "Is the spare bearing really in stock?", "question": "Who checked?",
         "positions": [{"seat": s["name"], "side": "yes" if i else "no", "view": "v"} for i, s in enumerate(seats)]}],
        "reason": "They split."}
    return _verdict_run(settings, fake_llm, verdict, agenda=agenda)


def test_an_open_point_the_verdict_leaves_out_is_named(settings, fake_llm):
    run = _open_run(settings, fake_llm, {"final_answer": "Swap the gearbox at the shutdown, not now.",
                                         "disputes_resolved": []})
    assert run["deliberation"]["open"] == ["D1", "D2"]          # everyone held
    assert run["coverage"]["missing"] == ["D2"]                 # D1 is discussed in words, D2 not at all
    w = [x for x in run["warnings"] if x.startswith(e.COVER_WARN)]
    assert w == ["The verdict does not address D2: Is the spare bearing really in stock? "
                 "The seats still disagreed on it after the debate."]


def test_a_ruling_or_a_mention_counts(settings, fake_llm):
    run = _open_run(settings, fake_llm, {"final_answer": "Swap at the shutdown. On D2, check the stores.",
                                         "disputes_resolved": [{"dispute": "D1", "decided": "wait", "by_argument": "x"}]})
    assert run["coverage"]["missing"] == [] and not any(x.startswith(e.COVER_WARN) for x in run["warnings"])

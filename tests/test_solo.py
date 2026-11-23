"""Chairman alone (opt-in): the chairman answers by itself beside the seats; shown next to the verdict."""
import random
import threading

from committee import config as c, engine as e, insights, pricing, report_md


def _go(settings, fake, solo=True):
    roster = c.build_roster(settings, solo=solo)
    run = e.new_run(roster, question="Swap GB-7 now?", rng=random.Random(2))
    while run["next_stage"]:
        e.advance(run, fake, threading.Event(), lambda _m: None)
    return run, roster


def test_off_by_default_and_costs_nothing(settings, fake_llm):
    run, roster = _go(settings, fake_llm(), solo=None)
    assert run["solo"] is False and run["solo_answer"] is None
    assert c.planned_calls(roster)["solo"] == 0 and insights.solo_compare(run) is None
    assert not any(u["stage"] == "solo" for u in run["usage"])


def test_one_extra_call_on_the_chairman_model_that_the_verdict_never_sees(settings, fake_llm):
    fake = fake_llm()
    run, roster = _go(settings, fake)
    assert c.planned_calls(roster)["solo"] == 1
    solo = [x for x in fake.calls if x["purpose"].endswith(".solo")]
    assert len(solo) == 1 and solo[0]["model"] == run["chairman"]["model"]
    assert "Answer this question on your own" in solo[0]["instructions"]
    assert run["solo_answer"]["status"] == "ok"
    chair = next(x for x in fake.calls if x["purpose"].endswith(".chair"))
    assert run["solo_answer"]["answer"] not in chair["text"]          # no anchoring on its own answer
    assert [u["who"] for u in run["usage"] if u["stage"] == "solo"] == ["Chairman"]
    s = insights.solo_compare(run)
    assert s["ok"] and s["verdict"] == run["chair"]["decision"]
    assert "solo" in insights.stage_starts(run)


def test_the_estimate_counts_it(settings, fake_llm):
    off = e.new_run(c.build_roster(settings), question="q", rng=random.Random(1))
    on = e.new_run(c.build_roster(settings, solo=True), question="q", rng=random.Random(1))
    assert pricing.estimate(on)["calls"] == pricing.estimate(off)["calls"] + 1


def test_its_failure_does_not_touch_the_verdict(settings, fake_llm):
    fake = fake_llm()
    real = fake.structured

    def structured(**kw):
        if kw["purpose"].endswith(".solo"):
            raise RuntimeError("the chairman's model is down")
        return real(**kw)
    fake.structured = structured
    fake.text = lambda **kw: (_ for _ in ()).throw(RuntimeError("down")) if kw["purpose"].endswith(".solo") \
        else type(fake).text(fake, **kw)
    run, _ = _go(settings, fake)
    assert run["status"] != "failed" and run["chair"]["status"] == "ok"
    assert run["solo_answer"]["status"] == "failed"
    assert insights.solo_compare(run)["ok"] is False
    assert "did not come in" in report_md.render(run)


def test_it_reaches_both_reports(settings, fake_llm, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run, _ = _go(settings, fake_llm())
    run["status"] = "done"
    md = report_md.render(run)
    assert "## Chairman alone" in md and "**With the committee:**" in md
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "Chairman alone" in text and "With the committee" in text


def test_a_draft_with_solo_runs_it_through_the_tools_and_the_chat_result_shows_it(plugin, tmp_path):
    import json
    from committee import store
    mod, _ctx = plugin
    seats = [{"name": "Engineer", "provider": "p1", "model": "m-one"}, {"name": "Skeptic", "provider": "p2", "model": "m-two"}]
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": seats, "solo": True})
    opened = json.loads(mod.handle_open({"draft": did}, session_id="s1"))
    assert opened["solo"] is True and opened["planned_model_calls"]["solo"] == 1
    while True:
        out = json.loads(mod.handle_advance({"run_id": opened["run_id"]}, session_id="s1"))
        if out.get("status") in ("done", "failed"):
            break
    assert out["chairman_alone"]["ok"] and out["chairman_alone"]["verdict"] == out["decision"]

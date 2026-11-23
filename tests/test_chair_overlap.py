"""A note when the chairman is the same model as a seat it judges: it may favour that answer's style."""
import random
import threading

from committee import config as c, engine as e, insights


def _run(settings, fake_llm, chair_model="m-chair"):
    settings["presets"]["board"]["chairman"] = {"provider": "p-chair", "model": chair_model}
    roster = c.build_roster(settings)
    run = e.new_run(roster, question="Swap GB-7 now?", rng=random.Random(2))
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    return run, roster


def _seat(run, model):
    return next(s["name"] for s in run["seats"] if s["model"] == model)


def test_a_chairman_on_its_own_model_needs_no_note(settings, fake_llm):
    run, roster = _run(settings, fake_llm)
    assert c.chair_overlap(run)["line"] == ""
    assert not any(w.startswith((c.CHAIR_SETUP, c.CHAIR_RUN)) for w in run["warnings"] + roster["warnings"])


def test_the_setup_warns_and_the_run_confirms_it_once(settings, fake_llm):
    run, roster = _run(settings, fake_llm, chair_model="openrouter/m-two:free")    # same model as a seat
    two = _seat(run, "m-two")
    assert any(w.startswith(c.CHAIR_SETUP) and two in w for w in roster["warnings"])
    line = c.chair_overlap(run)["line"]
    assert line.startswith("The chairman (m-two:free) is the same model as " + two)
    assert [w for w in run["warnings"] if w.startswith((c.CHAIR_SETUP, c.CHAIR_RUN))] == [line]
    assert insights.public_run(run)["chair_overlap"]["seats"] == [two]


def test_a_retried_seat_on_the_main_model_is_caught(settings, fake_llm):
    # a seat retried with Hermes's main model, which is also the chairman's model
    run, _ = _run(settings, fake_llm, chair_model="brand-chat-3")
    s = run["seats"][1]
    s["provider"], s["model"] = "", ""
    run["usage"].append({"stage": "stage1", "who": s["name"], "provider": "desk-app", "model": "brand-chat-3"})
    c.note_chair_overlap(run)
    assert c.chair_overlap(run)["seats"] == [s["name"]]
    assert sum(w.startswith(c.CHAIR_RUN) for w in run["warnings"]) == 1


def test_no_verdict_no_note(settings, fake_llm):
    run, _ = _run(settings, fake_llm, chair_model="m-two")
    run["chair"] = {"status": "failed", "error": "x"}
    assert c.chair_overlap(run)["line"] == ""


def test_it_reaches_both_reports(settings, fake_llm, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_md, report_pdf
    run, _ = _run(settings, fake_llm, chair_model="m-two")
    run["status"] = "done"
    assert "may favour that answer" in report_md.render(run)
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "may favour that answer" in text

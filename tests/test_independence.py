"""Answers are counted by the models that wrote them: a fallback or a shared model is not an independent view."""
import random
import threading

from committee import config as c, engine as e, insights, prompts, report_md


def _drive(run, llm):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    run["status"] = "done" if run["status"] == "chaired" else run["status"]
    return run


def _run(settings, fake_llm, models=None):
    if models:
        for s, m in zip(settings["presets"]["board"]["seats"], models):
            s["model"] = m
    return _drive(e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2)), fake_llm())


def _fallback(run, names, model="main-fallback"):
    for n in names:
        run.setdefault("switched", {})[n] = {"provider": "p-main", "model": model, "assigned": "x", "stages": ["stage1"]}


def test_four_different_models_need_no_note(settings, fake_llm):
    ind = c.independence(_run(settings, fake_llm))
    assert (ind["answers"], ind["models"], ind["line"]) == (4, 4, "")


def test_the_same_model_through_two_routes_counts_once(settings, fake_llm):
    run = _run(settings, fake_llm, ["m-one", "openrouter/m-one:free", "m-three", "m-four"])
    ind = c.independence(run)
    assert ind["models"] == 3 and len(ind["shared"]) == 1 and "4 answers came from 3 models" in ind["line"]


def test_fallback_answers_count_as_the_model_that_wrote_them(settings, fake_llm):
    run = _run(settings, fake_llm)
    names = [s["name"] for s in run["seats"]]
    _fallback(run, names[:3])
    ind = c.independence(run)
    assert ind["models"] == 2 and sorted(ind["fallback"]) == sorted(names[:3])
    assert "(3 by Hermes's fallback model)" in ind["line"] and "not independent confirmation" in ind["line"]
    _fallback(run, names[3:])
    assert "single-model panel, not an independent committee" in c.independence(run)["line"]


def test_a_fallback_only_in_a_later_stage_does_not_change_the_answer_count(settings, fake_llm):
    run = _run(settings, fake_llm)
    run["switched"] = {run["seats"][0]["name"]: {"model": "main-fallback", "stages": ["review"]}}
    assert c.independence(run)["models"] == 4


def test_failed_seats_are_not_counted(settings, fake_llm):
    run = _drive(e.new_run(c.build_roster(settings), question="q?", rng=random.Random(2)), fake_llm(fail=("m-four",)))
    assert c.independence(run)["answers"] == 3


def test_reports_chairman_and_page_all_say_it(settings, fake_llm):
    run = _run(settings, fake_llm, ["m-one", "m-one", "m-three", "m-four"])
    assert "(3 models)" in " ".join(report_md.exec_summary(run)["stood"])
    instr, text = prompts.chairman(run, [{"label": s["label"], "name": s["name"], "answer": "x"} for s in run["seats"]],
                                   {}, [])
    assert "one view, not several" in instr and "# Independence" in text
    assert insights.public_run(run)["independence"]["models"] == 3
    fine = _run({**settings}, fake_llm, ["m-one", "m-two", "m-three", "m-four"])
    assert "# Independence" not in prompts.chairman(fine, [], {}, [])[1]


def test_pdf_figure_names_the_models(settings, fake_llm, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run = _run(settings, fake_llm, ["m-one", "m-one", "m-three", "m-four"])
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    assert "seats answered · 3 models" in doc[0].get_text().replace("\n", " ")


def test_the_count_warning_is_replaced_not_repeated_on_a_retry(settings, fake_llm):
    run = _run(settings, fake_llm)
    run["warnings"] = ["4 answers came from 2 models (2 by Hermes's fallback model), so agreement ..."]
    _fallback(run, [run["seats"][0]["name"]], model=run["seats"][2]["model"])   # the fallback is seat 3's model
    run["answers"][run["seats"][1]["label"]] = {"status": "failed", "error": "x"}
    run["status"], run["chair"] = "failed", None
    e.reopen(run)
    _drive(run, fake_llm())
    assert sum(" answers came from " in w for w in run["warnings"]) == 1


def test_a_shared_model_without_fallback_is_warned_once(settings, fake_llm):
    run = _run(settings, fake_llm, ["m-one", "m-one", "m-three", "m-four"])
    assert sum(" answers came from " in w for w in run["warnings"]) == 1
    same = _run(settings, fake_llm, ["m-one"] * 4)                       # the plan's warning already says it
    assert not any(" answers came from " in w for w in same["warnings"])
    assert any(w.startswith("All seats use the same model") for w in same["warnings"])


def _main_model_seat(run, model):
    """A seat set to Hermes's active model (as a retry with 'use the main model' does) that `model` answered."""
    s = run["seats"][1]
    s["provider"], s["model"] = "", ""
    run["usage"] = [u for u in run.get("usage") or [] if u.get("who") != s["name"]] + \
        [{"stage": "stage1", "who": s["name"], "provider": "p-main", "model": model}]
    return s


def test_a_main_model_seat_counts_as_the_model_that_answered(settings, fake_llm):
    # found in a real run: the retried seat showed only "active model", so a duplicate would have been missed
    run = _run(settings, fake_llm)
    s = _main_model_seat(run, run["seats"][0]["model"])           # the main model is seat 1's model
    assert c.active_as(run)[s["name"]] == run["seats"][0]["model"]
    ind = c.independence(run)
    assert ind["models"] == 3 and "4 answers came from 3 models" in ind["line"]
    assert c.answered_by(run, s["name"]) == f" ({run['seats'][0]['model']})"
    assert insights.public_run(run)["active_as"][s["name"]] == run["seats"][0]["model"]


def test_a_main_model_seat_on_a_new_model_needs_no_note(settings, fake_llm):
    run = _run(settings, fake_llm)
    _main_model_seat(run, "brand-chat-3")
    assert c.independence(run)["line"] == ""

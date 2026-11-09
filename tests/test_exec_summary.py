"""Page 1 is an executive summary: the chairman's bottom line (or one assembled from the verdict), one page only."""
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import insights, prompts, report_md

LONG = "This is a deliberately long sentence about pump readiness and cooling continuity during transfer. " * 6


def _done(settings, fake_llm, **chair):
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    run["status"] = "done"
    run["chair"].update(chair)
    return run


def test_schema_asks_the_chairman_for_it():
    assert "executive_summary" in prompts.CHAIR_SCHEMA["required"]
    parsed = {"final_answer": "x", "decision": "d", "executive_summary": {"bottom_line": " Swap now. ",
              "reasons": ["a", "b", "c", "d"], "change": ""}}
    es = e._norm_chair(parsed, "")["executive_summary"]
    assert es == {"bottom_line": "Swap now.", "reasons": ["a", "b", "c"], "change": ""}


def test_chairman_text_is_used(settings, fake_llm):
    run = _done(settings, fake_llm, executive_summary={"bottom_line": "Swap to GB-8 now [E2].",
                                                        "reasons": ["Leak is rising."], "change": ""})
    es = report_md.exec_summary(run)
    assert es["bottom_line"] == "Swap to GB-8 now." and es["reasons"] == ["Leak is rising."]   # citations dropped
    assert not es["assembled"]


def test_older_runs_get_an_assembled_summary(settings, fake_llm):
    run = _done(settings, fake_llm, final_answer="## Verdict\n\nGo incremental. It is cheaper. It is safer. "
                "It is boring.\n\n- step", agreement=["incremental [E1]", "keep the team"])
    run["chair"].pop("executive_summary", None)
    es = report_md.exec_summary(run)
    assert es["assembled"] and es["bottom_line"] == "Go incremental. It is cheaper. It is safer."
    assert es["reasons"] == ["incremental", "keep the team"]
    assert "Executive summary" in report_md.render(run)


def test_follow_up_shows_one_change_line(settings, fake_llm):
    run = _done(settings, fake_llm, executive_summary={"bottom_line": "b", "reasons": [], "change": ""},
                change_from_earlier="The earlier decision stands. Readiness is now confirmed.")
    run["follow_of"] = "x"
    assert report_md.exec_summary(run)["change"] == "The earlier decision stands."


def test_page_one_is_one_page_even_when_everything_is_long(settings, fake_llm, tmp_path):
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run = _done(settings, fake_llm, decision=LONG[:280], executive_summary={"bottom_line": LONG, "reasons": [LONG] * 3,
                "change": ""}, next_actions=[LONG] * 6, reverse_if=[LONG] * 4)
    doc = fitz.open(str(report_pdf.render(run, tmp_path / "r" / "report.pdf")))
    toc = {t[1]: t[2] for t in doc.get_toc()}
    assert toc["Executive summary"] == 1 and toc["Decision in detail"] == 2
    p1 = doc[0].get_text().replace("\n", " ")
    assert p1.startswith("HERMES COMMITTEE  ·  EXECUTIVE SUMMARY") and "Do next" in p1
    assert "3 more in Decision in detail." in p1 and "Inside this report:" in p1


def test_the_page_shows_the_bottom_line(settings, fake_llm):
    run = _done(settings, fake_llm, executive_summary={"bottom_line": "Swap now.", "reasons": ["r"], "change": ""})
    assert insights.public_run(run)["exec"]["bottom_line"] == "Swap now."

"""The chairman's per-point call is found even without its id; a concession is not shown as a change of side."""
from committee import engine as e
from committee import insights, report_md

POINT = ("Does the new lab report establish GB-8's restart readiness, or does that conclusion "
         "remain conditional on a repeat oil sample?")
DISPUTES = [{"id": "D1", "level": "claim", "point": POINT, "question": "Is the statement enough?",
             "positions": [{"seat": "Alpha", "side": "Restart readiness accepted", "view": "v"},
                           {"seat": "Bravo", "side": "Restart remains conditional", "view": "v"},
                           {"seat": "Charlie", "side": "Restart readiness accepted", "view": "v"}]}]
# a chairman that quotes the wording, not the id
AS_WRITTEN = [{"dispute": "Does the user's lab report establish reported restart readiness, or must "
                          "a repeat oil sample be taken?",
               "decided": "Accept reported restart readiness.", "by_argument": "Alpha."}]


def test_match_by_id_wording_and_order():
    assert e.match_resolved([{"dispute": "D1: x", "decided": "a"}], DISPUTES)["D1"]["decided"] == "a"
    assert e.match_resolved(AS_WRITTEN, DISPUTES)["D1"]["decided"] == "Accept reported restart readiness."
    two = DISPUTES + [{"id": "D2", "level": "claim", "point": "Is the debris limit right?", "question": "q",
                       "positions": []}]
    hit = e.match_resolved([{"dispute": "first", "decided": "a"}, {"dispute": "second", "decided": "b"}], two)
    assert (hit["D1"]["decided"], hit["D2"]["decided"]) == ("a", "b")          # order when nothing else matches
    assert e.match_resolved([{"dispute": "unrelated", "decided": "a"}], two) == {}   # never guesses a lone one


def _run(settings):
    import random
    from committee import config as c
    run = e.new_run(c.build_roster(settings, deliberate="material"), question="q", rng=random.Random(1))
    for s_, name in zip(run["seats"], ["Alpha", "Bravo", "Charlie", "Spare"]):
        s_["name"] = name
    reply = {"status": "ok", "updated_position": "x", "confidence": 0.7, "responses": [
        {"dispute": "D1", "stance": "partly", "side": "Restart readiness accepted",
         "argument": "I concede the files contain no repeat sample.", "evidence": []}]}
    d = {"setting": "material", "disputes": DISPUTES, "noted": [], "reason": "r",
         "rounds": [{"disputes": ["D1"], "replies": {"Charlie": reply}}]}
    d["moves"], d["open"] = e.moves(d), ["D1"]
    run.update(status="done", deliberation=d,
               chair={"status": "ok", "decision": "Transfer.", "final_answer": "x", "dissent": [], "agreement": [],
                      "reverse_if": [], "next_actions": [], "open_questions": [], "confidence": "high",
                      "disputes_resolved": AS_WRITTEN})
    return run


def test_a_concession_is_not_a_change_of_side(settings):
    run = _run(settings)
    mv = run["deliberation"]["moves"]
    assert mv[0]["kept_side"] is True
    md = report_md.render(run)
    assert "Charlie conceded a point on D1" in md
    assert "kept “Restart readiness accepted” but conceded a point." in md
    assert "accepted → Restart readiness accepted" not in md
    assert "**Chairman's call:** Accept reported restart readiness." in md
    titles = [i["title"] for i in insights.run_insights(run)]
    assert "Concessions, no change of side" in titles and "The debate changed minds" not in titles


def test_the_page_gets_ids_for_older_runs(settings):
    out = insights.public_run(_run(settings))
    assert out["chair"]["disputes_resolved"][0]["dispute"] == "D1"


def test_pdf_shows_the_call_and_the_concession(settings, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    doc = fitz.open(str(report_pdf.render(_run(settings), tmp_path / "r" / "report.pdf")))
    text = " ".join(p.get_text() for p in doc).replace("\n", " ")
    assert "Chairman's call: Accept reported restart readiness." in text
    assert "conceded a point" in text

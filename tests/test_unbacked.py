"""Flag a member that moves to the majority side without new evidence (the debate failure the papers found)."""
import random
import threading

from committee import config as c, engine as e, insights, report_md


def _delib(positions, reply):
    d = {"id": "D1", "level": "action", "point": "Swap now?", "question": "Now or later?",
         "positions": [{"seat": s, "side": side, "view": f"{s} says {side}."} for s, side in positions]}
    return {"disputes": [d], "rounds": [{"disputes": ["D1"], "replies": reply}], "moves": []}


def _reply(side, stance="change", argument="I now agree.", evidence=()):
    return {"status": "ok", "responses": [{"dispute": "D1", "stance": stance, "side": side, "argument": argument,
                                           "evidence": list(evidence)}], "updated_position": "x", "confidence": 0.5}


def _run(answers):
    return {"seats": [{"name": n, "label": l} for n, l in (("Ana", "A"), ("Ben", "B"), ("Cy", "C"))],
            "answers": {l: {"status": "ok", "answer": a} for l, a in zip("ABC", answers)}}


LONE = [("Ana", "now"), ("Ben", "now"), ("Cy", "later")]


def test_a_lone_member_giving_in_with_no_evidence_is_flagged():
    m, = e.moves(_delib(LONE, {"Cy": _reply("now")}), _run(["a", "b", "c"]))
    assert m["unbacked"] and m["alone"] and m["new_evidence"] == []


def test_a_new_source_backs_the_change():
    m, = e.moves(_delib(LONE, {"Cy": _reply("now", argument="The inspection log [E2] shows wear.")}),
                 _run(["a", "b", "Per [E1], later."]))
    assert not m["unbacked"] and m["new_evidence"] == ["E2"]


def test_citing_only_what_it_had_already_cited_is_not_new():
    m, = e.moves(_delib(LONE, {"Cy": _reply("now", evidence=["E1"])}), _run(["a", "b", "Per [E1], later."]))
    assert m["unbacked"]


def test_moving_toward_a_minority_is_not_flagged():
    # the real run: Strategist left the majority to join Risk Officer, who stood alone
    positions = [("Ana", "leave"), ("Ben", "stay"), ("Cy", "stay")]
    m, = e.moves(_delib(positions, {"Cy": _reply("leave")}), _run(["a", "b", "c"]))
    assert not m["unbacked"] and not m["alone"]


def test_a_three_way_split_has_no_majority_to_join():
    positions = [("Ana", "budget"), ("Ben", "cross-contact"), ("Cy", "never")]
    m, = e.moves(_delib(positions, {"Cy": _reply("cross-contact")}), _run(["a", "b", "c"]))
    assert not m["unbacked"] and m["alone"]


def test_a_concession_is_not_a_change_of_side():
    m, = e.moves(_delib(LONE, {"Cy": _reply("later", stance="partly")}), _run(["a", "b", "c"]))
    assert m["kept_side"] and "unbacked" not in m


def test_old_callers_without_the_run_get_the_old_moves():
    m, = e.moves(_delib(LONE, {"Cy": _reply("now")}))
    assert "unbacked" not in m


def test_the_chairman_findings_and_reports_say_it(settings, fake_llm, tmp_path):
    settings["deliberate"] = "always"
    seats = c.build_roster(settings)["seats"]
    names = {s["model"]: s["name"] for s in seats}
    lone = names["m-four"]
    agenda = {"disputes": [{"level": "action", "point": "Swap now?", "question": "Now or later?",
                            "positions": [{"seat": s["name"], "side": "later" if s["name"] == lone else "now",
                                           "view": "Wait for the shutdown." if s["name"] == lone else "Swap now."}
                                          for s in seats]}], "reason": "They differ."}
    fake = fake_llm(agenda=agenda)
    fake.replies = {"m-four": {"responses": [{"dispute": "D1", "stance": "change", "side": "now",
                                              "argument": "The others make a fair case.", "evidence": []}],
                               "updated_position": "Swap now.", "confidence": 0.6}}
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    while run["next_stage"]:
        e.advance(run, fake, threading.Event(), lambda _m: None)
    run["status"] = "done"
    weak = e.unbacked(run)
    assert [(m["seat"], m["alone"]) for m in weak] == [(lone, True)]
    chair = next(x for x in fake.calls if x["purpose"].endswith("chair"))
    assert "Weigh such a member's original position" in chair["instructions"]
    assert f"- {lone} on D1 (round 1): later → now, after standing alone. Its original view: Wait for the shutdown." \
        in chair["text"]
    assert any(i["title"] == "Joined the majority without new evidence" for i in insights.run_insights(run))
    md = report_md.render(run)
    assert "joined the majority without new evidence (had stood alone)" in md
    assert "1 joined the majority without new evidence" in md
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "joined the majority without new evidence" in text and "(no new evidence)" in text

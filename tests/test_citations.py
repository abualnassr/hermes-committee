"""Citations are checked against the evidence the run has: an ID that was never given is flagged."""
import random
import threading

from committee import citations as ct, config as c, engine as e, prompts

SECTIONS = [{"id": f"E{i}", "source": "plan.pdf", "text": f"Section {i}."} for i in (1, 2, 3)]


def test_the_parser_reads_lists_ranges_and_mixed_letters():
    assert ct.cited("As [E2] says, and [E1, E3]; also [E2–E4] and [W1; 2] and [I1-2].") == \
        ["E2", "E1", "E3", "E4", "W1", "W2", "I1", "I2"]
    assert ct.cited("[E3] main.js:120") == ["E3"]
    assert ct.cited("a typo [E1-9999]") == ["E1", "E9999"]          # not 9999 citations
    assert ct.cited("[link](http://x) and [Note] and E5 alone") == []


def test_known_ids_come_from_files_pictures_web_and_pasted_labels():
    run = {"evidence_index": {"sections": [{"id": "E1"}], "images": [{"id": "I1"}]},
           "web": {"items": [{"id": "W1"}, {"id": "W2", "keep": False}]},
           "evidence": "[E7] my own note"}
    assert ct.known(run) == {"E1", "I1", "W1", "E7"}


def _run(settings, fake_llm, *, seat_cites="", chair_cites="", sections=SECTIONS, rounds=False):
    class Citing:
        def __init__(self, fake):
            self.fake = fake

        def __getattr__(self, k):
            return getattr(self.fake, k)

        def text(self, **kw):
            return self.fake.text(**kw)

        def structured(self, **kw):
            r = self.fake.structured(**kw)
            p = r.parsed or {}
            if kw["purpose"].endswith("stage1") and kw["model"] == "m-two" and isinstance(p.get("answer"), str):
                p["answer"] += " " + seat_cites
            if kw["purpose"].endswith("chair") and isinstance(p.get("final_answer"), str):
                p["final_answer"] += " " + chair_cites
            return r

    fake = fake_llm()
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    run["_sections"] = list(sections)
    run["evidence_index"] = {"sections": [{"id": s["id"]} for s in sections], "images": []}
    llm = Citing(fake)
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    return run, fake


def _two(run):
    return next(s for s in run["seats"] if s["model"] == "m-two")


def test_real_citations_raise_nothing(settings, fake_llm):
    run, _ = _run(settings, fake_llm, seat_cites="See [E1] and [E2–E3].", chair_cites="Per [E2].")
    assert run["citations"]["unknown"] == [] and run["citations"]["cited"] >= 4
    assert not any(w.startswith(ct.PREFIX) for w in run["warnings"])


def test_a_made_up_citation_is_flagged_and_the_chairman_is_told(settings, fake_llm):
    run, fake = _run(settings, fake_llm, seat_cites="See [E2] and [E9], and [W4].", chair_cites="Per [E2].")
    seat = _two(run)
    assert run["citations"]["unknown"] == [{"who": seat["name"], "ids": ["E9", "W4"]}]
    line = next(w for w in run["warnings"] if w.startswith(ct.PREFIX))
    assert f"{seat['name']} cites [E9], [W4]" in line and "unsupported" in line
    chair = next(x["text"] for x in fake.calls if x["purpose"].endswith("chair"))
    assert "Cites evidence that does not exist: [E9], [W4]" in chair
    assert "[E9]" not in next(x["instructions"] for x in fake.calls if x["purpose"].endswith("chair"))


def test_the_chairmans_own_bad_citation_is_caught_once(settings, fake_llm):
    run, _ = _run(settings, fake_llm, seat_cites="[E1]", chair_cites="Per [E12].")
    assert run["citations"]["unknown"] == [{"who": "Chairman", "ids": ["E12"]}]
    ct.audit(run); ct.audit(run)                                   # the audit can run again without repeating
    assert sum(w.startswith(ct.PREFIX) for w in run["warnings"]) == 1


def test_no_evidence_at_all_says_so(settings, fake_llm):
    run, _ = _run(settings, fake_llm, seat_cites="[E1]", sections=[])
    assert "This run had no numbered evidence." in run["citations"]["line"]


def test_the_pdf_does_not_link_a_citation_that_points_nowhere(settings, fake_llm, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run, _ = _run(settings, fake_llm, seat_cites="[E1]", chair_cites="Per [E2] and [E12].")
    run["status"] = "done"
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "[E12: not found]" in text and "[E2]" in text


def test_the_reports_own_explanation_of_citations_is_not_marked(settings, fake_llm, tmp_path):
    # found in a real run: with only [E1], Appendix C read "cite text as [E1], [E2: not found]…"
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run, _ = _run(settings, fake_llm, seat_cites="[E1]", sections=SECTIONS[:1])
    run["status"] = "done"
    run["files"] = [{"fid": "f1", "name": "food.pdf", "kind": "pdf", "size": 100, "tokens": 20}]
    text = " ".join(p.get_text() for p in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "[E2: not found]" not in text and "[I1: not found]" not in text
    assert "The seats cite text as [E1], [E2]" in text

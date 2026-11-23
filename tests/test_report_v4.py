"""PDF v2 layout: chapters in reading order, appendices lettered without gaps, logos, symbols that print."""
import copy
import random
import threading

import pytest

from committee import config as c, engine as e, logos

fitz = pytest.importorskip("fitz")
pytest.importorskip("fpdf")
from committee import report_pdf  # noqa: E402


def _run(settings, fake_llm, **chair):
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now? Iron went 12 → 40 ppm.", rng=random.Random(2))
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    run["status"] = "done"
    run["chair"].update(chair)
    return run


def _debate(run):
    seats = [s["name"] for s in run["seats"]]
    run["deliberation"] = {
        "reason": "Two seats disagreed on the crane.", "setting": "material", "open": [],
        "disputes": [{"id": "D1", "point": "Does the change need the crane?", "level": "action",
                      "positions": [{"seat": seats[0], "side": "Conditional", "view": "Only with the crane."},
                                    {"seat": seats[1], "side": "Regardless", "view": "Hire one if needed."}]}],
        "rounds": [{"disputes": ["D1"], "replies": {
            seats[0]: {"status": "ok", "responses": [{"dispute": "D1", "stance": "change", "side": "Regardless",
                                                      "argument": "A hired crane covers it. The window holds.",
                                                      "evidence": []}]},
            seats[1]: {"status": "ok", "responses": [{"dispute": "D1", "stance": "hold", "side": "Regardless",
                                                      "argument": "Nothing new against it.", "evidence": []}]}}}],
        "moves": [], "noted": ""}
    return run


def test_logos_come_from_the_desktop_table():
    assert logos.maker_of("anthropic/claude-sonnet-5.5") == "claude"
    assert logos.maker_of("z-ai/glm-5", "openrouter") == "zai"             # by the model, not the route
    assert logos.maker_of("", "anthropic") == "claude"                      # the provider only when no model
    svg = logos.logo_svg(logos.maker_of("google/gemini-4-carbon"), (10, 20, 30)) or b""
    assert svg.startswith(b"<svg") and b'fill="#0a141e"' in svg
    assert logos.logo_svg(logos.maker_of("acme/unknown-1")) is None and logos.logo_svg(None) is None


def test_chapters_follow_reading_order_and_letters_have_no_gap(settings, fake_llm, tmp_path):
    run = _debate(_run(settings, fake_llm))
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    top = [t[1] for t in doc.get_toc() if t[0] == 1]
    assert top[:4] == ["Executive summary", "Why: the chairman's reasoning", "Where they disagreed", "The committee"]
    assert "Appendix B · The debate in full" in top
    calm = copy.deepcopy(run)
    calm["deliberation"] = None
    doc2 = fitz.open(report_pdf.render(calm, tmp_path / "s.pdf"))
    top2 = [t[1] for t in doc2.get_toc() if t[0] == 1]
    assert "Where they disagreed" not in top2 and not any("debate in full" in t for t in top2)
    letters = [t.split(" · ")[0][-1] for t in top2 if t.startswith("Appendix")]
    assert letters == list("ABCD"[:len(letters)])


def test_disagreement_card_shows_the_move_and_the_call(settings, fake_llm, tmp_path):
    run = _debate(_run(settings, fake_llm))
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    page = next(p for p in doc if "Where they disagreed" in p.get_text() and "Side: before" in p.get_text())
    text = page.get_text()
    assert "D1 · Does the change need the crane?" in text and "Conditional → Regardless" in text.replace("\n", " ")


def test_page_one_has_the_figures_and_page_two_does_not_repeat_the_verdict_heading(settings, fake_llm, tmp_path):
    run = _run(settings, fake_llm, final_answer="## Verdict\nSwap it.\n\n## Why\nThe iron is rising.")
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    assert "seats answered" in doc[0].get_text() and "run time" in doc[0].get_text()
    p2 = doc[1].get_text()
    assert "The iron is rising." in p2 and "Verdict\n" not in p2


def test_arrows_print_instead_of_blank(settings, fake_llm, tmp_path):
    run = _run(settings, fake_llm)
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    assert "12 → 40 ppm" in "".join(p.get_text() for p in doc).replace("\n", " ")
    assert any("Math" in f[3] for p in doc for f in p.get_fonts())


def test_committee_table_draws_maker_logos(settings, fake_llm, tmp_path):
    run = _run(settings, fake_llm)
    for s in run["seats"]:
        s["model"] = "anthropic/claude-sonnet-5.5"
    doc = fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))
    page = next(p for p in doc if p.get_text().startswith("Hermes Committee") and "The committee\n" in p.get_text())
    fills = [d for d in page.get_drawings() if d.get("fill") and len(d.get("items", [])) > 8]
    assert len(fills) >= len(run["seats"])

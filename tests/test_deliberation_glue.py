"""The Deliberate setting travels from the page, the chat tool, follow-ups and saved committees into the run."""
import asyncio
import json
import pathlib

import pytest

from committee import followup, store

from test_desktop_backend import _load_api

SEATS = [{"name": "Engineer", "instructions": "", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "", "provider": "p2", "model": "m-two"}]


def test_draft_keeps_the_page_setting(plugin, tmp_path):
    mod, _ctx = plugin
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": SEATS,
                                      "deliberate": "always", "deliberate_rounds": 2})
    out = json.loads(mod.handle_open({"draft": did}, session_id="s1"))
    run = store.load(tmp_path, out["run_id"])
    assert (run["deliberate"], run["deliberate_rounds"]) == ("always", 2)
    assert out["planned_model_calls"]["deliberate"] == 1 + 2 * 2


def test_chat_tool_setting_and_bad_value(plugin, tmp_path):
    mod, _ctx = plugin
    run = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?", "deliberate": "action"},
                                                          session_id="s1"))["run_id"])
    assert run["deliberate"] == "action" and run["next_stage"] == "stage1"
    bad = json.loads(mod.handle_open({"question": "q?", "deliberate": "loud"}, session_id="s2"))
    assert bad["ok"] is False and "Deliberate must be one of" in bad["error"]


def test_follow_up_inherits_the_setting(plugin, tmp_path):
    mod, _ctx = plugin
    first = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?", "deliberate": "always"},
                                                            session_id="s1"))["run_id"])
    first["status"] = "done"
    first["chair"] = {"status": "ok", "decision": "Swap"}
    store.save(tmp_path, first)
    nxt = store.load(tmp_path, json.loads(mod.handle_open({"question": "again?", "follow_of": first["id"]},
                                                          session_id="s2"))["run_id"])
    assert nxt["deliberate"] == "always"


def test_follow_up_context_mentions_who_changed_their_mind():
    prev = {"id": "x", "question": "q", "chair": {"status": "ok", "decision": "Swap"},
            "deliberation": {"disputes": [{"id": "D1", "point": "Is the break-even right?"}],
                             "moves": [{"seat": "Skeptic", "dispute": "D1", "because": "The cost table [E2]."}]}}
    ctx = followup.context(prev)
    assert "Skeptic changed view on “Is the break-even right?”: The cost table [E2]." in ctx


def test_full_run_through_the_tools(plugin, tmp_path):
    mod, ctx = plugin
    ctx._fake.agenda = {"disputes": [{"level": "action", "point": "Swap now?", "positions": [
        {"seat": "Engineer", "side": "swap", "view": "v"}, {"seat": "Skeptic", "side": "wait", "view": "v"}],
        "question": "Why?"}], "reason": "They split."}
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": SEATS})
    rid = json.loads(mod.handle_open({"draft": did}, session_id="s1"))["run_id"]
    seen = []
    while True:
        out = json.loads(mod.handle_advance({"run_id": rid}, session_id="s1"))
        seen.append(out.get("stage_completed") or out.get("status"))
        if out.get("stage_completed") == "deliberate":
            assert out["deliberation"]["disputes"][0]["point"] == "Swap now?" and out["deliberation"]["rounds"] == 1
        if out.get("status") in ("done", "failed"):
            break
    run = store.load(tmp_path, rid)
    assert "deliberate" in seen and run["status"] == "done" and len(run["deliberation"]["rounds"]) == 1
    assert "pdf" in run["reports"] and "markdown" in run["reports"]
    md = pathlib.Path(run["reports"]["markdown"]).read_text(encoding="utf-8")
    assert "## Deliberation" in md and "The seats deliberated on 1 disputed point; nobody changed position; D1 stayed disputed." in md
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(run["reports"]["pdf"])
    text = " ".join(p.get_text() for p in doc).replace("\n", " ")
    assert "D1 · Swap now?" in text and "Deliberation was set to run when they disagreed" in text
    assert any(t[1] == "Deliberation" for t in doc.get_toc())
    assert "Nobody moved" in text                       # the insight


def test_skipped_deliberation_is_one_line_in_the_reports(settings, fake_llm, tmp_path):
    import random
    import threading
    from committee import config as c, engine as e, report_md, report_pdf
    run = e.new_run(c.build_roster(settings, deliberate="material"), question="q?", rng=random.Random(1))
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    assert "No deliberation: the seats had no material disagreement." in report_md.render(run)
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(str(report_pdf.render(run, tmp_path / "r" / "report.pdf")))
    assert "No deliberation: the seats had no material disagreement." in doc[1].get_text().replace("\n", " ")


def test_setup_and_estimate_routes(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    s = asyncio.run(api.setup())
    assert s["limits"]["deliberate_default"] == "never"           # the test settings pin the old flow
    body = {"question": "q?", "template": "classic", "seats": SEATS, "deliberate": "material"}
    on = asyncio.run(api.estimate(body))
    off = asyncio.run(api.estimate({**body, "deliberate": "never"}))
    assert on["deliberation_calls"] == 1 + 2 and off["deliberation_calls"] == 0

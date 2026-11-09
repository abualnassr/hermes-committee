"""The chairman's brief and the answer/decision mode can be set per committee and are saved with it."""
import asyncio
import json
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import report_md, store, templates

from test_desktop_backend import _load_api

SEATS = [{"name": "Engineer", "instructions": "", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "", "provider": "p2", "model": "m-two"}]
BRIEF = "Rank the options by total cost and name the fatal risks."


def test_defaults_come_from_the_type(settings):
    r = c.build_roster(settings, template="red-team")
    t = templates.get_template("red-team")
    assert r["chairman_guidance"] == t["chairman_guidance"] and r["decision_focus"] is True and not r["custom_brief"]


def test_request_beats_saved_committee_beats_type(settings):
    settings["presets"]["board"].update(chairman_guidance="Saved brief.", decision_focus=False)
    r = c.build_roster(settings, preset="board")
    assert (r["chairman_guidance"], r["decision_focus"], r["custom_brief"]) == ("Saved brief.", False, True)
    r = c.build_roster(settings, preset="board", chairman_guidance=BRIEF, decision_focus=True)
    assert (r["chairman_guidance"], r["decision_focus"]) == (BRIEF, True)
    r = c.build_roster(settings, preset="board", chairman_guidance="  ")            # blank = not given
    assert r["chairman_guidance"] == "Saved brief."


def test_too_long_brief_is_refused(settings):
    with pytest.raises(c.ConfigError, match="chairman's instructions"):
        c.build_roster(settings, chairman_guidance="x" * (c.MAX_INSTRUCTIONS + 1))


def test_the_prompts_follow_the_mode_and_brief(settings, fake_llm):
    run = e.new_run(c.build_roster(settings, template="classic", chairman_guidance=BRIEF, decision_focus=True),
                    question="Swap now?", rng=random.Random(1))
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    ans = next(x for x in llm.calls if x["schema_name"] == "committee_answer")
    chair = next(x for x in llm.calls if x["schema_name"] == "committee_verdict")
    assert "Make a clear recommendation" in ans["instructions"]
    assert BRIEF in chair["instructions"] and "This is a decision" in chair["instructions"]
    md = report_md.render(run)
    assert f"Chairman's brief (custom): {BRIEF} · Mode: decision" in md


def test_page_draft_preset_and_follow_up_keep_it(plugin, tmp_path):
    mod, _ctx = plugin
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": SEATS,
                                      "chairman_guidance": BRIEF, "decision_focus": True})
    first = store.load(tmp_path, json.loads(mod.handle_open({"draft": did}, session_id="s1"))["run_id"])
    assert (first["chairman_guidance"], first["decision_focus"], first["custom_brief"]) == (BRIEF, True, True)
    first["status"], first["chair"] = "done", {"status": "ok", "decision": "Swap"}
    store.save(tmp_path, first)
    nxt = store.load(tmp_path, json.loads(mod.handle_open({"question": "again?", "follow_of": first["id"]},
                                                          session_id="s2"))["run_id"])
    assert nxt["chairman_guidance"] == BRIEF and nxt["decision_focus"] is True


def test_setup_lists_type_briefs_and_saved_ones(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    store.save_preset(tmp_path, "My board", {"template": "classic", "seats": SEATS,
                                             "chairman_guidance": BRIEF, "decision_focus": True})
    s = asyncio.run(api.setup())
    red = next(t for t in s["templates"] if t["id"] == "red-team")
    assert red["chairman_guidance"].startswith("Rank the vulnerabilities") and red["decision_focus"] is True
    assert s["rosters"]["My board"]["chairman_guidance"] == BRIEF and s["rosters"]["My board"]["decision_focus"] is True

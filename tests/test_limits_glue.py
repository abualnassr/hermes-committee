"""The time limit travels from the page (draft) and the chat tool into the run record."""
import asyncio
import json

from committee import store

from test_desktop_backend import _load_api

SEATS = [{"name": "Engineer", "instructions": "", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "", "provider": "p2", "model": "m-two"}]


def test_draft_keeps_the_page_time_limit(plugin, tmp_path):
    mod, _ctx = plugin
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": SEATS, "max_minutes": 3})
    run = store.load(tmp_path, json.loads(mod.handle_open({"draft": did}, session_id="s1"))["run_id"])
    assert run["limits"]["max_secs"] == 180 and run["deadline"] is None


def test_chat_tool_default_and_off(plugin, tmp_path):
    mod, _ctx = plugin
    on = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?"}, session_id="s1"))["run_id"])
    off = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?", "max_minutes": 0}, session_id="s2"))["run_id"])
    assert on["limits"]["max_secs"] == 600 and off["limits"]["max_secs"] is None


def test_bad_limit_is_a_plain_error(plugin):
    mod, _ctx = plugin
    out = json.loads(mod.handle_open({"question": "q?", "max_minutes": 90}, session_id="s1"))
    assert out["ok"] is False and "time limit" in out["error"]


def test_reserves_come_from_past_chairman_times(plugin, tmp_path, fake_llm):
    mod, _ctx = plugin
    past = {"id": "20261101-000000-aaaaaa", "status": "done", "chairman": {"provider": "p-chair", "model": "m-chair"},
            "seats": [], "usage": [{"stage": "chair", "who": "chair", "secs": 150.0}]}
    store.save(tmp_path, past)
    run = store.load(tmp_path, json.loads(mod.handle_open({"question": "q?"}, session_id="s1"))["run_id"])
    assert run["chairman"]["model"] == "m-chair"
    assert run["limits"]["chair_reserve"] == 180      # slowest recent × 1.2


def test_setup_and_draft_routes_report_the_limit(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    s = asyncio.run(api.setup())
    assert s["limits"]["max_minutes_default"] == 10 and s["limits"]["max_minutes_max"] == 60
    r = asyncio.run(api.create_draft({"question": "q?", "template": "classic", "seats": SEATS, "max_minutes": 0}))
    assert r["ok"], r
    assert r["max_secs"] is None and store.load_draft(tmp_path, r["draft"])["max_minutes"] == 0

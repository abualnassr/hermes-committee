"""Convene from the Committee page: drafts, the permission gate, and a draft-backed run."""
import asyncio
import json

import pytest

from committee import config as c
from committee import store

from test_desktop_backend import _load_api

SEATS = [{"name": "Engineer", "instructions": "Give numbers.", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "Attack the leading option.", "provider": "p2", "model": "m-two"}]
CHAIR = {"provider": "p2", "model": "m-two"}


def _post(api, body):
    return asyncio.run(api.create_draft(body))


def test_draft_is_stored_and_the_message_is_one_short_line(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    q = "Run the gearbox\nto the outage, or change it out now?  " + "x" * 400
    r = _post(api, {"question": q, "evidence": "debris 3x baseline", "template": "classic", "review": True,
                    "seats": SEATS, "chairman": CHAIR})
    assert r["ok"] is True and r["planned_calls"]["total"] == 5
    msg = r["message"]
    assert msg.startswith(f"/hermes-committee:convene draft={r['draft']} ") and "\n" not in msg
    assert len(msg) < 300
    d = store.load_draft(tmp_path, r["draft"])
    assert d["question"] == q.strip() and d["seats"][1]["instructions"] == "Attack the leading option."
    assert d["chairman"] == CHAIR and d["evidence"] == "debris 3x baseline"


def test_draft_rejects_an_empty_question_and_a_bad_roster(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ex:
        _post(api, {"question": "  ", "seats": SEATS})
    assert ex.value.status_code == 400
    with pytest.raises(HTTPException) as ex:
        _post(api, {"question": "q", "seats": [SEATS[0], dict(SEATS[0])]})   # duplicate names
    assert ex.value.status_code == 400


def test_gate_names_the_exact_commands_and_never_writes_config(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings, llm={"allowed_providers": ["p1"], "allowed_models": ["m-one"]})
    r = _post(api, {"question": "q", "seats": SEATS, "chairman": CHAIR})
    assert r["ok"] is False and r["locked"] == ["p2"] and r["locked_models"] == ["m-two"]
    key = "plugins.entries.hermes-committee.llm"
    assert r["commands"] == [f"hermes config set {key}.allow_provider_override true",
                             f"hermes config set {key}.allow_model_override true",
                             f"hermes config set {key}.allowed_providers '[\"p1\", \"p2\"]'",
                             f"hermes config set {key}.allowed_models '[\"m-one\", \"m-two\"]'"]
    assert "p2" in r["error"] and not (tmp_path / "drafts").exists()


def test_gate_follows_hermes_rules_for_missing_and_star_lists(monkeypatch, tmp_path, settings):
    open_llm = {"allow_provider_override": True, "allow_model_override": True}
    api = _load_api(monkeypatch, tmp_path, settings, llm=open_llm)            # no list = no allowlist
    assert _post(api, {"question": "q", "seats": SEATS})["ok"] is True
    api = _load_api(monkeypatch, tmp_path, settings, llm={**open_llm, "allowed_providers": ["*"]})
    assert _post(api, {"question": "q", "seats": SEATS})["ok"] is True
    api = _load_api(monkeypatch, tmp_path, settings, llm={**open_llm, "allowed_providers": []})
    assert _post(api, {"question": "q", "seats": SEATS, "chairman": CHAIR})["locked"] == ["p1", "p2"]  # [] = nothing
    setup = asyncio.run(api.setup())
    assert setup["policy"]["providers"] == [] and setup["rosters"]["board"]["seats"]


def test_seats_on_the_active_model_need_no_permission(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings, llm={})
    seats = [{"name": "One"}, {"name": "Two"}]
    assert _post(api, {"question": "q", "seats": seats, "chairman": {}})["ok"] is True


def test_convene_from_a_draft_uses_the_page_roster(plugin, tmp_path):
    mod, _ctx = plugin
    did = store.save_draft(tmp_path, {"question": "Page question?", "evidence": "page facts", "template": "classic",
                                      "review": False, "seats": SEATS, "chairman": CHAIR})
    out = json.loads(mod.handle_open({"draft": did, "brief": "from chat"}, session_id="s9"))
    assert out["ok"] is True
    run = store.load(tmp_path, out["run_id"])
    assert run["question"] == "Page question?" and run["draft"] == did and run["review"] is False
    assert [s["name"] for s in run["seats"]] == ["Engineer", "Skeptic"]
    assert run["chairman"]["model"] == "m-two" and "page facts" in run["evidence"] and run["brief"] == "from chat"


def test_unknown_or_malformed_draft_is_a_plain_error(plugin):
    mod, _ctx = plugin
    for bad in ("d0000000000", "../../x"):
        out = json.loads(mod.handle_open({"draft": bad}, session_id="s1"))
        assert out["ok"] is False and "Committee page" in out["error"]


def test_chairman_override_in_roster(settings):
    r = c.build_roster(settings, chairman={"provider": "p1", "model": "m-one"})
    assert (r["chairman"]["provider"], r["chairman"]["model"]) == ("p1", "m-one")
    # None keeps the preset's chairman; {} from the page means "the active model"
    assert c.build_roster(settings, chairman=None)["chairman"]["provider"] == "p-chair"
    empty = c.build_roster(settings, chairman={})["chairman"]
    assert not empty.get("provider") and not empty.get("model")

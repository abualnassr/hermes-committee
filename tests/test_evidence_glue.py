"""Evidence through the chat tool and the page draft, and the vision gate."""
import io
import json
import sys

import pytest

from committee import store

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _jpg_file(tmp_path, name="gear.jpg"):
    p = tmp_path / name
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, "JPEG")
    p.write_bytes(buf.getvalue())
    return p


def _caps(mod, vision_models):
    import importlib
    hc = importlib.import_module(mod.__name__ + ".committee.hostcaps")
    return lambda provider, model: {"vision": model in vision_models, "context": 100_000}, hc


def _open(mod, **args):
    return json.loads(mod.handle_open({"question": "Run the pump?", **args}, session_id="s1"))


def test_chat_paths_become_numbered_evidence_and_vision_seats_are_marked(plugin, tmp_path, monkeypatch):
    mod, _ctx = plugin
    caps, hc = _caps(mod, {"m-one", "m-chair"})
    monkeypatch.setattr(hc, "model_caps", caps)
    notes = tmp_path / "notes.md"
    notes.write_text("Oil debris rising since May.", encoding="utf-8")
    out = _open(mod, evidence_files=[str(notes), str(_jpg_file(tmp_path))])
    assert out["ok"], out
    run = store.load(tmp_path, out["run_id"])
    assert [f["name"] for f in run["files"]] == ["notes.md", "gear.jpg"]
    assert {s["model"]: s["sees_images"] for s in run["seats"]}["m-one"] is True
    assert {s["model"]: s["sees_images"] for s in run["seats"]}["m-two"] is False
    assert run["describer"]["model"] == "m-chair" and out["planned_model_calls"]["describe"] == 1
    assert "1 picture(s): seen by 1 of 4 seats" in out["evidence"]


def test_pictures_without_any_vision_model_are_refused_before_any_call(plugin, tmp_path, monkeypatch):
    mod, ctx = plugin
    caps, hc = _caps(mod, set())
    monkeypatch.setattr(hc, "model_caps", caps)
    out = _open(mod, evidence_files=[str(_jpg_file(tmp_path))])
    assert out["ok"] is False and "none of this committee's models is known to see pictures" in out["error"]
    assert not ctx._fake.calls


def test_a_seat_override_beats_the_catalog(plugin, tmp_path, monkeypatch):
    mod, _ctx = plugin
    caps, hc = _caps(mod, set())
    monkeypatch.setattr(hc, "model_caps", caps)
    seats = [{"name": "Eyes", "provider": "p1", "model": "m-one", "vision": True},
             {"name": "Ears", "provider": "p2", "model": "m-two"}]
    did = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": seats,
                                      "chairman": {"provider": "p2", "model": "m-two"}})
    pid = store.new_pack(tmp_path)
    store.add_to_pack(tmp_path, pid, "gear.jpg", _jpg_file(tmp_path).read_bytes())
    f2 = store.add_to_pack(tmp_path, pid, "skip.md", b"not chosen")
    d = store.load_draft(tmp_path, did)
    did2 = store.save_draft(tmp_path, {**d, "evidence_pack": pid,
                                       "evidence_files": [x["fid"] for x in store.pack_files(tmp_path, pid)
                                                          if x["fid"] != f2["fid"]]})
    out = json.loads(mod.handle_open({"draft": did2}, session_id="s1"))
    assert out["ok"], out
    run = store.load(tmp_path, out["run_id"])
    assert [f["name"] for f in run["files"]] == ["gear.jpg"]           # only the ticked file
    assert run["describer"]["model"] == "m-one"                        # the seat marked 'Can see photos'


def test_missing_file_and_unsupported_type_are_plain_errors(plugin, tmp_path):
    mod, _ctx = plugin
    assert "not found" in _open(mod, evidence_files=[str(tmp_path / "nope.pdf")])["error"]
    bad = tmp_path / "old.doc"
    bad.write_bytes(b"x")
    assert "old.doc: Old Word format" in _open(mod, evidence_files=[str(bad)])["error"]


def test_advance_loads_the_evidence_for_the_seats(plugin, tmp_path):
    mod, ctx = plugin
    notes = tmp_path / "notes.md"
    notes.write_text("Bearing temperature 92 °C.", encoding="utf-8")
    out = _open(mod, evidence_files=[str(notes)])
    json.loads(mod.handle_advance({"run_id": out["run_id"]}, session_id="s1"))
    texts = [c["text"] for c in ctx._fake.calls if c["schema_name"] == "committee_answer"]
    assert texts and all("[E1] notes.md" in t and "92 °C" in t for t in texts)


def test_draft_keeps_page_choices_and_ignores_agent_files(plugin, tmp_path):
    mod, _ctx = plugin
    pid = store.new_pack(tmp_path)
    store.add_to_pack(tmp_path, pid, "page.md", b"from the page")
    seats = [{"name": "A", "provider": "p1", "model": "m-one"}, {"name": "B", "provider": "p2", "model": "m-two"}]
    none_ticked = store.save_draft(tmp_path, {"question": "q?", "template": "classic", "seats": seats,
                                              "evidence_pack": pid, "evidence_files": []})
    chat = tmp_path / "chat.md"
    chat.write_text("from the chat", encoding="utf-8")
    out = json.loads(mod.handle_open({"draft": none_ticked, "evidence_files": [str(chat)]}, session_id="s1"))
    assert out["ok"], out
    run = store.load(tmp_path, out["run_id"])
    assert not run.get("files")                                  # unticked page file left out; the agent's file ignored
    assert run["agent_added_ignored"] == ["files"]

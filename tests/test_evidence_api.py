"""The page's evidence routes: add, list, remove, model capabilities and the draft check."""
import asyncio
import base64
import importlib
import io

import pytest

from committee import store

from test_desktop_backend import _load_api

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

SEATS = [{"name": "Engineer", "instructions": "", "provider": "p1", "model": "m-one"},
         {"name": "Skeptic", "instructions": "", "provider": "p2", "model": "m-two"}]
CHAIR = {"provider": "p2", "model": "m-two"}


def _jpg_b64():
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (0, 200, 0)).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _api(monkeypatch, tmp_path, settings, vision=(), context=1_000_000):
    api = _load_api(monkeypatch, tmp_path, settings)
    api._core()
    hc = importlib.import_module("hermes_committee_dashboard_core.hostcaps")
    monkeypatch.setattr(hc, "model_caps", lambda p, m: {"vision": None if m in vision else False,
                                                       "context_fail": context,
                                                       "price_in": 1.0, "price_out": 4.0, "billing": "paid"})
    return api


def run(coro):
    return asyncio.run(coro)


def test_add_list_remove_and_a_plain_error_for_a_bad_file(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings)
    pack = run(api.new_evidence())["pack"]
    a = run(api.add_evidence(pack, {"name": "notes.md", "data": base64.b64encode(b"Oil debris rising").decode()}))
    b = run(api.add_evidence(pack, {"name": "gear.jpg", "data": _jpg_b64()}))
    assert a["ok"] and b["ok"] and b["file"]["images"] == 1
    bad = run(api.add_evidence(pack, {"name": "old.xls", "data": base64.b64encode(b"x").decode()}))
    assert bad["ok"] is False and "save it as .xlsx" in bad["error"]
    assert [f["name"] for f in run(api.get_evidence(pack))["files"]] == ["notes.md", "gear.jpg"]
    assert run(api.remove_evidence(pack, a["file"]["fid"]))["ok"]
    assert [f["name"] for f in run(api.get_evidence(pack))["files"]] == ["gear.jpg"]


def test_caps_route_reports_each_route(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, vision={"m-one"})
    caps = run(api.model_caps({"routes": [{"provider": "p1", "model": "m-one"}, {"provider": "p2", "model": "m-two"}]}))
    assert caps["caps"]["p1|m-one"]["vision"] is None and caps["caps"]["p2|m-two"]["vision"] is False


def test_draft_with_pictures_needs_a_seat_that_sees_them(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, vision=())
    pack = run(api.new_evidence())["pack"]
    run(api.add_evidence(pack, {"name": "gear.jpg", "data": _jpg_b64()}))
    body = {"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR, "evidence_pack": pack}
    r = run(api.create_draft(body))
    assert r["ok"] is False and "every model in this committee has refused pictures" in r["error"]
    marked = [{**SEATS[0], "vision": True}, SEATS[1]]
    r2 = run(api.create_draft({**body, "seats": marked}))
    assert r2["ok"], r2
    assert r2["evidence"]["seats_seeing"] == ["Engineer"] and r2["evidence"]["pictures"] == 1
    d = store.load_draft(tmp_path, r2["draft"])
    assert d["evidence_pack"] == pack


def test_draft_warns_when_a_model_failed_on_long_evidence_before(monkeypatch, tmp_path, settings):
    api = _api(monkeypatch, tmp_path, settings, context=30_000)
    pack = run(api.new_evidence())["pack"]
    run(api.add_evidence(pack, {"name": "big.txt", "data": base64.b64encode(b"word " * 20_000).decode()}))
    r = run(api.create_draft({"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR,
                              "evidence_pack": pack}))
    assert r["ok"], r
    assert any("Engineer's model failed before with about 30,000 tokens" in w for w in r["evidence"]["warnings"])
    # unticking the big file clears the warning
    r2 = run(api.create_draft({"question": "q?", "template": "classic", "seats": SEATS, "chairman": CHAIR,
                               "evidence_pack": pack, "evidence_files": []}))
    assert r2["ok"] and not r2["evidence"]["warnings"], r2

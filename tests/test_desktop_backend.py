"""The Desktop page's data: insights, ballots, standings, live state, page Stop and the backend routes."""
import asyncio
import importlib.util
import json
import random
import sys
import threading
import time
import types

import pytest

from committee import config as c
from committee import engine as e
from committee import insights as ins
from committee import store

from conftest import ROOT

NOOP = lambda _m: None  # noqa: E731


def _done(settings, llm, rng=7):
    run = e.new_run(c.build_roster(settings), question="Rewrite or modernize?", rng=random.Random(rng))
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP, rng=random.Random(3))
    run["status"] = "done"
    return run


def test_ballot_matrix_matches_reviews(settings, fake_llm):
    run = _done(settings, fake_llm())
    m = ins.ballot_matrix(run)
    assert m["rows"] == m["cols"] == sorted(run["answers"])
    for r in m["rows"]:
        assert [m["cells"][r][col] for col in run["reviews"][r]["ranking"]] == list(range(1, len(m["cols"]) + 1))


def test_insights_are_facts_from_the_record(settings, fake_llm):
    run = _done(settings, fake_llm())
    kinds = {x["kind"]: x for x in ins.run_insights(run)}
    # the fake reviewers all rank alphabetically, so exactly one reviewer (A) ranks itself first
    assert "1 of 4 reviewers" in kinds["self"]["text"]
    assert kinds["consensus"]["title"] == "Unanimous first place"
    assert all(set(x) == {"kind", "title", "text"} for x in kinds.values())


def test_insights_are_empty_for_a_cancelled_run(settings, fake_llm):
    run = e.new_run(c.build_roster(settings), question="q", rng=random.Random(1))
    run["status"] = "cancelled"
    assert ins.public_run(run)["insights"] == []


def test_standings_aggregate_models_across_runs(settings, fake_llm):
    runs = [_done(settings, fake_llm(), rng=i) for i in range(3)]
    st = ins.standings(runs)
    assert st["runs"] == 3 and st["enough"] is False
    assert {r["model"] for r in st["table"]} == {"m-one", "m-two", "m-three", "m-four"}
    assert all(r["runs"] == 3 and 0 <= r["score"] <= 1 for r in st["table"])
    assert st["table"] == sorted(st["table"], key=lambda r: (-r["score"], -r["first_votes"], r["model"]))
    assert len(st["points"]) == 12


def test_standings_skip_unfinished_and_lite_runs(settings, fake_llm):
    lite = e.new_run(c.build_roster(settings, review=False), question="q", rng=random.Random(2))
    while lite["next_stage"]:
        e.advance(lite, fake_llm(), threading.Event(), NOOP)
    lite["status"] = "done"
    assert ins.standings([lite])["runs"] == 0


def test_rank_score_bounds():
    assert ins.rank_score(1, 4) == 1.0 and ins.rank_score(4, 4) == 0.0 and ins.rank_score(1, 1) == 1.0


def test_engine_records_live_progress(settings, fake_llm):
    run = e.new_run(c.build_roster(settings), question="q", rng=random.Random(1))
    seen = []
    e.advance(run, fake_llm(), threading.Event(), lambda m: seen.append(json.dumps(run.get("live"))))
    assert run["live"]["stage"] == "stage1" and len(run["live"]["done"]) == 4
    assert all(run["live"]["done"].values())
    assert '"done": {}' in seen[0]          # first update: stage started, nobody answered yet


def test_page_stop_flag_cancels_a_running_stage(plugin, tmp_path):
    mod, _ctx = plugin
    slow = types.SimpleNamespace(**{k: getattr(mod._HermesLLM, k) for k in ("structured", "text")})
    opened = json.loads(mod.handle_open({"question": "q", "lite": True}, session_id="s1"))
    rid = opened["run_id"]
    sig = mod._entry(store.load(tmp_path, rid), "s1")["cancel"]
    assert sig.is_set() is False
    assert store.request_cancel(tmp_path, rid) is True
    assert sig.is_set() is True               # the engine's next poll sees the page's Stop
    assert store.cancel_requested(tmp_path, "not-an-id") is False
    del slow


TRUSTED = {"allowed_providers": ["p1", "p2"], "allow_provider_override": True, "allow_model_override": True}


def _load_api(monkeypatch, tmp_path, settings, llm=None):
    for k in [k for k in sys.modules if k.startswith("hermes_committee_dashboard_core")]:
        del sys.modules[k]
    fake_storage = types.ModuleType("plugins.plugin_storage")
    fake_storage.plugin_data_dir = lambda _pid: tmp_path
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.load_config_readonly = lambda: {"plugins": {"entries": {"hermes-committee": {
        "settings": settings, "llm": TRUSTED if llm is None else llm}}}}
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", fake_storage)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location("committee_api_under_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api._core()
    hc = importlib.import_module("hermes_committee_dashboard_core.hostcaps")
    monkeypatch.setattr(hc, "model_caps", lambda p, m: {"vision": False, "context": 1_000_000, "price_in": 1.0,
                                                       "price_out": 4.0, "billing": "paid"})
    return api


def test_backend_routes(monkeypatch, tmp_path, settings, fake_llm):
    api = _load_api(monkeypatch, tmp_path, settings)
    done = _done(settings, fake_llm())
    store.save(tmp_path, done)
    live = e.new_run(c.build_roster(settings), question="still going", rng=random.Random(9))
    store.save(tmp_path, live)

    runs = asyncio.run(api.runs())["runs"]
    assert {r["id"] for r in runs} == {done["id"], live["id"]}
    assert next(r for r in runs if r["id"] == done["id"])["decision"] == "Go incremental."

    detail = asyncio.run(api.run_detail(done["id"]))
    assert detail["insights"] and detail["matrix"]["rows"] and "stage1" in detail["seconds"]
    json.dumps(detail)

    st = asyncio.run(api.standings())
    assert st["runs"] == 1

    setup = asyncio.run(api.setup())
    assert setup["default_preset"] == "board" and "board" in setup["presets"]
    assert {t["id"] for t in setup["templates"]} >= {"classic", "reliability-board"}
    assert setup["known_models"]["p1"] == ["m-one"] and setup["limits"]["max_seats"] == c.MAX_SEATS

    r = asyncio.run(api.stop(live["id"]))
    assert r["status"] == "stopping" and store.cancel_requested(tmp_path, live["id"])
    assert asyncio.run(api.stop(done["id"]))["status"] == "done"


def test_backend_marks_an_abandoned_run_cancelled(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    run = e.new_run(c.build_roster(settings), question="orphan", rng=random.Random(4))
    p = store.save(tmp_path, run)
    old = time.time() - api.STALE_AFTER_S - 5
    import os
    os.utime(p, (old, old))
    assert asyncio.run(api.runs())["runs"][0]["stale"] is True
    assert asyncio.run(api.stop(run["id"]))["status"] == "cancelled"
    assert store.load(tmp_path, run["id"])["status"] == "cancelled"


def test_backend_rejects_bad_ids(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ex:
        asyncio.run(api.run_detail("../../etc"))
    assert ex.value.status_code == 400
    with pytest.raises(HTTPException) as ex:
        asyncio.run(api.run_detail("20990101-000000-abcdef"))
    assert ex.value.status_code == 404

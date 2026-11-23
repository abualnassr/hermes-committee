"""Retry a run that stopped without a verdict: only what failed is asked again, the rest is kept."""
import asyncio
import json
import random
import threading

import pytest

from committee import config as c, engine as e, report_md, store


def _drive(run, llm):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    if run["status"] == "chaired":
        run["status"] = "done"
    return run


def _failed_run(settings, fake_llm, down=("m-one", "m-two")):
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    return _drive(run, fake_llm(fail=down))          # 2 of 4 answer; the quorum is 3


def _stage1_calls(llm):
    return sorted(x["model"] for x in llm.calls if x["purpose"].endswith("stage1"))


def test_failed_seats_are_asked_again_and_the_answers_that_came_in_are_kept(settings, fake_llm):
    run = _failed_run(settings, fake_llm)
    assert run["status"] == "failed" and e.retry_target(run) == ("stage1", "")
    kept = {lab: dict(a) for lab, a in run["answers"].items() if a["status"] == "ok"}
    spent = len(run["usage"])
    done = e.reopen(run)
    assert run["next_stage"] == "stage1" and run["status"] == "open" and run["error"] is None
    assert sorted(done["who"]) == sorted(s["name"] for s in run["seats"] if s["model"] in ("m-one", "m-two"))
    llm = fake_llm()
    _drive(run, llm)
    assert run["status"] == "done" and run["chair"]["status"] == "ok"
    assert _stage1_calls(llm) == ["m-one", "m-two"]                    # nobody else was asked again
    for lab, a in kept.items():
        assert run["answers"][lab] == a
    assert len(run["usage"]) > spent and run["usage"][:spent]            # the first try's calls still count
    assert run["retries"][0]["stage"] == "stage1" and all(run["retries"][0]["errors"].values())
    assert "Retry 1: asked" in report_md.render(run)


def test_a_failed_seat_can_get_the_main_model(settings, fake_llm):
    run = _failed_run(settings, fake_llm)
    seat = next(s for s in run["seats"] if s["model"] == "m-one")
    e.reopen(run, {seat["name"]: {"provider": "", "model": ""}})
    assert seat["was"] == {"provider": "p1", "model": "m-one"} and c.model_label(seat) == "active model"
    assert "p1 / m-one → active model" in run["retries"][0]["swapped"][seat["name"]]
    llm = fake_llm(fail=("m-two",))                                     # m-two is still down
    _drive(run, llm)
    assert run["answers"][seat["label"]]["status"] == "ok" and "m-one" not in _stage1_calls(llm)
    two = next(s for s in run["seats"] if s["model"] == "m-two")
    assert run["answers"][two["label"]]["status"] == "failed"
    assert run["status"] == "done"                                      # 3 of 4 meets the quorum of 3


def test_the_chairman_alone_is_retried(settings, fake_llm):
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    _drive(run, fake_llm(fail=("m-chair",)))
    assert run["status"] == "failed" and run["chair"]["status"] == "failed"
    assert e.retry_target(run) == ("chair", "")
    e.reopen(run)
    llm = fake_llm()
    _drive(run, llm)
    assert run["status"] == "done" and not _stage1_calls(llm)


def test_what_cannot_be_retried_says_why(settings, fake_llm):
    done = _drive(e.new_run(c.build_roster(settings), question="q?", rng=random.Random(2)), fake_llm())
    with pytest.raises(ValueError, match="without a verdict"):
        e.reopen(done)
    run = _failed_run(settings, fake_llm)
    ok_seat = next(s for s in run["seats"] if run["answers"][s["label"]]["status"] == "ok")
    with pytest.raises(ValueError, match="answered"):
        e.reopen(run, {ok_seat["name"]: {"provider": "", "model": ""}})
    with pytest.raises(ValueError, match="No seat called"):
        e.reopen(run, {"Nobody": {}})
    never = dict(run, skipped={"run": "budget"})
    assert e.retry_target(never)[0] is None and "never started" in e.retry_target(never)[1]
    for _ in range(e.MAX_RETRIES):
        e.reopen(run)
        _drive(run, fake_llm(fail=("m-one", "m-two")))
    assert e.retry_target(run)[0] is None and "already retried" in e.retry_target(run)[1]


def test_retry_through_the_chat_tool(plugin, tmp_path):
    mod, ctx = plugin
    ctx._fake.fail = {"m-one", "m-two"}
    run_id = json.loads(mod.handle_open({"question": "Swap GB-7 now?"}, session_id="s"))["run_id"]
    out = {}
    while out.get("status") not in ("done", "failed"):
        out = json.loads(mod.handle_advance({"run_id": run_id}, session_id="s"))
    assert out["status"] == "failed" and out["can_retry"] and "kept" in out["retry_hint"]
    store.request_cancel(tmp_path, run_id)                              # an old Stop must not cancel the retry
    ctx._fake.fail = set()
    r = json.loads(mod.handle_retry({"run_id": run_id}, session_id="s"))
    assert r["ok"] and len(r["retrying"]) == 2 and len(r["kept_answers"]) == 2
    assert not store.cancel_requested(tmp_path, run_id)
    out = {}
    while out.get("status") not in ("done", "failed"):
        out = json.loads(mod.handle_advance({"run_id": run_id}, session_id="s"))
    assert out["status"] == "done" and out["retries"][0]["stage"] == "stage1"
    import yaml
    from pathlib import Path
    declared = yaml.safe_load((Path(mod.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(declared["provides_tools"]) == set(ctx.tools)            # what Hermes is told matches what registers
    again = json.loads(mod.handle_retry({"run_id": run_id}))
    assert again["ok"] is False and "without a verdict" in again["error"]


def test_page_route_stores_the_choice_for_the_chat(monkeypatch, tmp_path, settings, fake_llm, plugin):
    from test_desktop_backend import _load_api
    mod, ctx = plugin
    run = _failed_run(settings, fake_llm)
    store.save(tmp_path, run)
    api = _load_api(monkeypatch, tmp_path, settings)
    detail = asyncio.run(api.run_detail(run["id"]))
    assert detail["retry"] == {"stage": "stage1", "why": ""}
    failed = [s["name"] for s in run["seats"] if run["answers"][s["label"]]["status"] != "ok"]
    bad = asyncio.run(api.retry(run["id"], {"main": ["Nobody"]}))
    assert bad["ok"] is False and "Only a seat that failed" in bad["error"]
    r = asyncio.run(api.retry(run["id"], {"main": failed[:1]}))
    assert r["ok"] and r["message"] == f"/hermes-committee:convene retry={run['id']}"
    out = json.loads(mod.handle_retry({"run_id": run["id"]}))            # the chat applies the stored choice
    assert out["ok"] and list(out["swapped"]) == failed[:1]
    assert "retry_request" not in (store.load(tmp_path, run["id"]) or {})


def test_terminal_retry_reads_seat_swaps(plugin, capsys):
    from types import SimpleNamespace
    mod, ctx = plugin
    ctx._fake.fail = {"m-one", "m-two"}
    assert mod._cli_run(SimpleNamespace(question="Swap GB-7 now?", brief="", evidence_file=None, evidence_dir=None,
                                        preset=None, template=None, lite=False, research=False, research_secs=None,
                                        max_minutes=None, max_usd=None, title=None, follow_of=None,
                                        deliberate=None, rounds=None)) == 1
    printed = capsys.readouterr().out
    assert "hermes committee retry" in printed
    run_id = printed.split("Run ", 1)[1].split()[0]
    assert mod._cli_retry(SimpleNamespace(run_id=run_id, seat=["Somebody=no-slash"])) == 1
    assert "NAME=PROVIDER/MODEL" in capsys.readouterr().out
    ctx._fake.fail = {"m-two"}
    run = store.load(mod._data_dir(), run_id)
    one = next(s["name"] for s in run["seats"] if s["model"] == "m-one")
    assert mod._cli_retry(SimpleNamespace(run_id=run_id, seat=[f"{one}=main"])) == 0
    out = capsys.readouterr().out
    assert "Retrying" in out and "active model" in out and "Status: done" in out

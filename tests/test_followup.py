"""Committee names, follow-up runs, deletable presets and clearing evidence."""
import asyncio
import json

from committee import followup, store

from test_desktop_backend import _load_api

DONE = {"id": "20261108-100000-aaaaaa", "created": 1_700_000_000, "question": "Run GB-7 to the outage?",
        "title": "GB-7 gearbox", "status": "done",
        "chair": {"status": "ok", "decision": "Swap to GB-8 now", "confidence": "medium",
                  "final_answer": "The leak rate is rising.", "dissent": ["The Skeptic would wait a week"],
                  "next_actions": ["Prove GB-8"], "reverse_if": ["GB-8 fails its test"]}}


def test_context_carries_the_earlier_verdict_and_says_it_is_background():
    ctx = followup.context(DONE)
    for bit in ("Run GB-7 to the outage?", "Swap to GB-8 now", "The Skeptic would wait a week", "Prove GB-8",
                "“GB-7 gearbox”", "background, not as settled"):
        assert bit in ctx
    assert "did not reach a verdict" in followup.context({**DONE, "chair": {"status": "failed"}})


def test_title_is_one_short_line():
    assert followup.clean_title("  a\n b  ") == "a b"
    assert len(followup.clean_title("x" * 200)) == followup.TITLE_MAX


def _first_run(mod, tmp_path, **extra):
    out = json.loads(mod.handle_open({"question": "Run GB-7 to the outage?", "title": "GB-7 gearbox", **extra},
                                     session_id="s1"))
    assert out["ok"], out
    run = store.load(tmp_path, out["run_id"])
    run["status"], run["chair"] = "done", DONE["chair"]
    store.save(tmp_path, run)
    return run


def test_follow_up_keeps_the_committee_and_gets_the_verdict(plugin, tmp_path):
    mod, _ctx = plugin
    first = _first_run(mod, tmp_path)
    assert first["title"] == "GB-7 gearbox"
    out = json.loads(mod.handle_open({"question": "GB-8 vibrates too. Still swap?", "follow_of": first["id"]},
                                     session_id="s1"))
    assert out["ok"], out
    run = store.load(tmp_path, out["run_id"])
    assert run["follow_of"] == first["id"]
    assert [(s["name"], s["model"]) for s in sorted(run["seats"], key=lambda s: s["name"])] == \
        [(s["name"], s["model"]) for s in sorted(first["seats"], key=lambda s: s["name"])]
    assert "Swap to GB-8 now" in run["brief"] and run["template"] == first["template"]


def test_follow_up_without_background(plugin, tmp_path):
    mod, _ctx = plugin
    first = _first_run(mod, tmp_path)
    out = json.loads(mod.handle_open({"question": "Fresh look?", "follow_of": first["id"], "follow_context": False},
                                     session_id="s1"))
    assert "Swap to GB-8" not in store.load(tmp_path, out["run_id"])["brief"]


def test_follow_up_of_a_missing_run_is_refused(plugin):
    mod, _ctx = plugin
    out = json.loads(mod.handle_open({"question": "q?", "follow_of": "20990101-000000-ffffff"}, session_id="s1"))
    assert out["ok"] is False and "No earlier committee" in out["error"]


def test_presets_saved_on_the_page_can_be_deleted(tmp_path):
    store.save_preset(tmp_path, "my-board", {"template": "classic"})
    p, src = store.effective_presets(tmp_path, {"council": {"template": "classic"}})
    assert set(p) == {"council", "my-board"} and src["my-board"] == "page" and src["council"] == "config"
    assert store.delete_preset(tmp_path, "my-board", in_config=False)
    assert store.delete_preset(tmp_path, "council", in_config=True)          # config.yaml one: hidden
    assert store.effective_presets(tmp_path, {"council": {"template": "classic"}})[0] == {}
    store.save_preset(tmp_path, "council", {"template": "classic"})          # saving it again un-hides it
    assert "council" in store.effective_presets(tmp_path, {"council": {}})[0]


def test_preset_routes(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    r = asyncio.run(api.save_preset({"name": "quick", "preset": {"template": "classic", "max_usd": 0.5}}))
    assert r["ok"], r
    setup = asyncio.run(api.setup())
    assert setup["rosters"]["quick"]["max_usd"] == 0.5 and setup["rosters"]["quick"]["source"] == "page"
    assert asyncio.run(api.save_preset({"name": "-bad", "preset": {}}))["ok"] is False
    assert asyncio.run(api.save_preset({"name": "Gearbox Board (GB-7)", "preset": {"template": "classic"}}))["ok"]
    assert asyncio.run(api.delete_preset("quick"))["ok"]
    assert "quick" not in asyncio.run(api.setup())["rosters"]
    assert asyncio.run(api.delete_preset("quick"))["ok"] is False


def test_clear_all_evidence(monkeypatch, tmp_path, settings):
    api = _load_api(monkeypatch, tmp_path, settings)
    pack = asyncio.run(api.new_evidence())["pack"]
    import base64
    for n in ("a.txt", "b.txt"):
        data = "data:text/plain;base64," + base64.b64encode(b"pump note").decode()
        assert asyncio.run(api.add_evidence(pack, {"name": n, "data": data}))["ok"]
    assert len(asyncio.run(api.get_evidence(pack))["files"]) == 2
    assert asyncio.run(api.clear_evidence(pack))["removed"] == 2
    assert asyncio.run(api.get_evidence(pack))["files"] == []


def test_summary_and_reports_carry_name_and_run_time(settings, fake_llm, tmp_path):
    import random
    import threading
    from committee import config as c
    from committee import engine as e
    from committee import insights, report_md
    run = e.new_run(c.build_roster(settings), question="Run the pump?", rng=random.Random(1))
    run["title"], run["follow_of"] = "Pump call", DONE["id"]
    while run["next_stage"]:
        e.advance(run, fake_llm(), threading.Event(), lambda _m: None)
    assert insights.summary(run)["title"] == "Pump call"
    md = report_md.render(run)
    assert md.startswith("# Committee report: Pump call") and "**Question:** Run the pump?" in md
    assert f"Follow-up to run `{DONE['id']}`" in md and "actual run time" in md


def test_chairman_text_uses_seat_names():
    seats = [{"label": "A", "name": "Alpha"}, {"label": "B", "name": "Bravo"}, {"label": "C", "name": "Charlie"}]
    chair = {"final_answer": "A cautious plan. Response A agrees with B.", "decision": "Swap now",
             "dissent": [{"label": "B", "view": "B gives stronger claims; it rejects B's date and C's projection."}],
             "next_actions": ["Ask C."]}
    out = followup.name_chair(chair, seats)
    assert out["final_answer"] == "A cautious plan. Alpha agrees with Bravo."
    assert out["dissent"][0]["view"] == "Bravo gives stronger claims; it rejects Bravo's date and Charlie's projection."
    assert out["next_actions"] == ["Ask Charlie."] and chair["dissent"][0]["view"].startswith("B gives")


def test_follow_up_adds_new_files_to_the_earlier_evidence(plugin, tmp_path):
    mod, _ctx = plugin
    a, b = tmp_path / "first.txt", tmp_path / "new.txt"
    a.write_text("Oil debris 40 ppm and rising.", encoding="utf-8")
    b.write_text("GB-8 vibration 4.1 mm/s.", encoding="utf-8")
    first = _first_run(mod, tmp_path, evidence_files=[str(a)])
    assert [f["name"] for f in first["files"]] == ["first.txt"]
    out = json.loads(mod.handle_open({"question": "Still swap?", "follow_of": first["id"],
                                      "evidence_files": [str(b)]}, session_id="s1"))
    run = store.load(tmp_path, out["run_id"])
    assert [f["name"] for f in run["files"]] == ["first.txt", "new.txt"]
    store.load_run_evidence(tmp_path, run)
    assert {s["source"] for s in run["_sections"]} == {"first.txt", "new.txt"}
    out2 = json.loads(mod.handle_open({"question": "Only new?", "follow_of": first["id"], "follow_evidence": False,
                                       "evidence_files": [str(b)]}, session_id="s1"))
    assert [f["name"] for f in store.load(tmp_path, out2["run_id"])["files"]] == ["new.txt"]


def test_reused_pictures_keep_their_descriptions(tmp_path):
    from committee import evidence as ev
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    f = ev.extract("gear.jpg", buf.getvalue())
    prev = {"id": "20261108-100000-bbbbbb", "question": "q"}
    store.attach_files_to_run(tmp_path, prev, [f])
    prev["evidence_index"]["images"][0].update(description="Worn gear teeth, label GB-7", described_by="M1")
    reuse = store.reuse_run_files(tmp_path, prev)
    run = {"id": "20261108-100001-cccccc"}
    store.attach_files_to_run(tmp_path, run, reuse)
    img = run["evidence_index"]["images"][0]
    assert img["description"] == "Worn gear teeth, label GB-7" and img["described_by"] == "M1"

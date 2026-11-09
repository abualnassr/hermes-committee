"""Evidence in a run: packs, the run copy, and who sees which pictures."""
import io
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import evidence as ev
from committee import store

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

NOOP = lambda _m: None  # noqa: E731


def _jpg(color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def _run_with_pack(tmp_path, settings, files, vision=("m-one", "m-chair"), **kw):
    run = e.new_run(c.build_roster(settings, **kw), question="Run the pump to the outage?", rng=random.Random(2))
    pid = store.new_pack(tmp_path)
    for name, data in files:
        store.add_to_pack(tmp_path, pid, name, data)
    store.attach_pack_to_run(tmp_path, run, pid)
    for s in [*run["seats"], run["chairman"]]:
        s["sees_images"] = s.get("model") in vision
    run["describer"] = run["chairman"] if run["chairman"]["sees_images"] else None
    store.save(tmp_path, run)
    store.load_run_evidence(tmp_path, run)
    return run


def test_pack_add_remove_and_limits(tmp_path, monkeypatch):
    pid = store.new_pack(tmp_path)
    a = store.add_to_pack(tmp_path, pid, "notes.md", b"vibration 7.1 mm/s")
    b = store.add_to_pack(tmp_path, pid, "gear.jpg", _jpg())
    assert [f["name"] for f in store.pack_files(tmp_path, pid)] == ["notes.md", "gear.jpg"]
    assert b["images"] == 1 and b["tokens"] == ev.IMAGE_TOKENS and a["kind"] == "text"
    assert store.remove_from_pack(tmp_path, pid, b["fid"])
    assert [f["name"] for f in store.pack_files(tmp_path, pid)] == ["notes.md"]
    monkeypatch.setattr(ev, "MAX_FILES", 1)
    with pytest.raises(ev.EvidenceError, match="at most 1 files"):
        store.add_to_pack(tmp_path, pid, "more.md", b"x")
    for bad in ("../x", "d0123456789"):
        with pytest.raises(ValueError):
            store.pack_files(tmp_path, bad)


def test_run_gets_its_own_copy_and_the_record_stays_small(tmp_path, settings):
    run = _run_with_pack(tmp_path, settings, [("survey.md", b"Debris 3x baseline. " * 200), ("gear.jpg", _jpg())])
    idx = run["evidence_index"]
    assert [s["id"] for s in idx["sections"]] == ["E1"] and [i["id"] for i in idx["images"]] == ["I1"]
    rec = (store.run_dir(tmp_path, run["id"]) / "run.json").read_text(encoding="utf-8")
    assert "Debris 3x" not in rec and '"_images"' not in rec and '"_sections"' not in rec           # text and bytes live beside the record
    assert run["_sections"][0]["text"].startswith("Debris") and run["_images"]["I1"]["data"][:2] == b"\xff\xd8"


def test_only_vision_seats_get_pictures_and_everyone_else_gets_descriptions(tmp_path, settings, fake_llm):
    run = _run_with_pack(tmp_path, settings, [("gear.jpg", _jpg()), ("notes.md", b"Oil debris rising since May.")])
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP)
    assert run["status"] == "chaired"
    desc = [x for x in llm.calls if x["schema_name"] == "committee_picture"]
    assert len(desc) == 1 and desc[0]["model"] == "m-chair" and desc[0]["images"] == 1
    img = run["evidence_index"]["images"][0]
    assert "worn teeth" in img["description"] and img["described_by"].endswith("m-chair")
    answers = [x for x in llm.calls if x["schema_name"] == "committee_answer"]
    for call in answers:                                   # pictures only to models known to see them
        assert (call["images"] == 1) == (call["model"] == "m-one")
        if call["model"] != "m-one":
            assert "worn teeth" in call["text"] and "You cannot see these pictures" in call["text"]
        assert "[E1] notes.md" in call["text"] and "Oil debris rising" in call["text"]
    for call in llm.calls:
        if call["schema_name"] in ("committee_review", "committee_verdict"):
            assert call["images"] == 0 and "worn teeth" in call["text"]
    assert any(u["stage"] == "describe" for u in run["usage"])
    assert c.planned_calls(run)["describe"] == 1


def test_no_vision_model_means_no_pictures_are_sent(tmp_path, settings, fake_llm):
    run = _run_with_pack(tmp_path, settings, [("gear.jpg", _jpg())], vision=())
    llm = fake_llm()
    e.advance(run, llm, threading.Event(), NOOP)
    assert all(x["images"] == 0 for x in llm.calls)
    assert run["evidence_index"]["images"][0]["describe_error"]
    assert all("could not be described" in x["text"] for x in llm.calls if x["schema_name"] == "committee_answer")


def test_a_failed_description_does_not_sink_the_run(tmp_path, settings, fake_llm):
    run = _run_with_pack(tmp_path, settings, [("gear.jpg", _jpg())])
    llm = fake_llm(fail={"m-chair"})
    e.advance(run, llm, threading.Event(), NOOP)
    assert run["status"] == "stage1_done"
    assert "describe_error" in run["evidence_index"]["images"][0]


def test_prune_removes_only_old_packs(tmp_path):
    import os
    import time
    old, new = store.new_pack(tmp_path), store.new_pack(tmp_path)
    store.add_to_pack(tmp_path, old, "a.md", b"x")
    past = time.time() - 9 * 86400
    for p in [*(tmp_path / "evidence" / old).iterdir(), tmp_path / "evidence" / old]:
        os.utime(p, (past, past))
    assert store.prune_packs(tmp_path) == 1
    assert not (tmp_path / "evidence" / old).exists() and (tmp_path / "evidence" / new).exists()


def test_reports_list_files_and_picture_descriptions(tmp_path, settings, fake_llm):
    from committee import report_md, report_pdf
    run = _run_with_pack(tmp_path, settings, [("gear.jpg", _jpg()), ("notes.md", b"Oil debris rising since May.")])
    llm = fake_llm()
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), NOOP)
    md = report_md.render(run)
    assert "### Evidence files" in md and "| notes.md | text |" in md and "[I1] gear.jpg" in md
    assert "worn teeth" in md and "Pictures: 1; seen directly by" in md
    pdf = report_pdf.render(run, tmp_path / "r.pdf")
    assert pdf.stat().st_size > 2000

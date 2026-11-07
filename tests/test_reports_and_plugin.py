import json
import random
import threading

import pytest

from committee import config as c
from committee import engine as e
from committee import report_md


def _done_run(settings, fake_llm, question="Rewrite or modernize?"):
    run = e.new_run(c.build_roster(settings), question=question, evidence="Fact 1.", rng=random.Random(1))
    while run["next_stage"]:
        e.advance(run, fake_llm(), threading.Event(), lambda m: None)
    run["status"] = "done"
    return run


def test_markdown_report_has_every_section(settings, fake_llm):
    md = report_md.render(_done_run(settings, fake_llm))
    for heading in ("## Verdict", "## Final answer", "## Roster", "## Peer ranking", "## Dissent kept on record",
                    "## Seat answers", "## Method and usage", "## Appendix: brief and evidence"):
        assert heading in md
    assert "m-one" in md                       # the report de-anonymizes
    assert "unknown (the providers did not report cost)" in md
    assert report_md.DISCLAIMER in md


def test_pdf_report_renders_including_arabic(settings, fake_llm, tmp_path):
    pytest.importorskip("fpdf")
    from committee import report_pdf
    run = _done_run(settings, fake_llm, question="هل نعيد كتابة النظام أم نحدّثه تدريجياً؟ (rewrite vs modernize)")
    out = report_pdf.render(run, tmp_path / "r.pdf")
    data = out.read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 10_000
    assert report_pdf.has_rtl(run["question"]) and not report_pdf.has_rtl("plain English")


def test_plugin_open_advance_until_done(plugin):
    mod, ctx = plugin
    assert set(ctx.tools) == {"committee_open", "committee_advance"}
    assert set(ctx.hooks) == {"post_tool_call", "on_session_end"} and "convene" in ctx.skills
    opened = json.loads(ctx.tools["committee_open"]({"question": "Rewrite?", "evidence": "x"}, session_id="s1"))
    assert opened["ok"] and opened["planned_model_calls"]["total"] == 9
    rid, out = opened["run_id"], None
    for _ in range(5):
        out = json.loads(ctx.tools["committee_advance"]({"run_id": rid}, session_id="s1"))
        if out["status"] in ("done", "failed"):
            break
    assert out["status"] == "done" and out["decision"] == "Go incremental."
    assert out["reports"]["markdown"].endswith("report.md")
    assert any("done" in t for kind, t in ctx.messages)          # progress toast finished
    again = json.loads(ctx.tools["committee_advance"]({"run_id": rid}, session_id="s1"))
    assert again["status"] == "done"                               # idempotent after completion


def test_plugin_open_reports_config_errors(plugin):
    _, ctx = plugin
    out = json.loads(ctx.tools["committee_open"]({"question": "q", "preset": "missing"}))
    assert out["ok"] is False and "Unknown preset" in out["error"]
    out = json.loads(ctx.tools["committee_open"]({"question": "  "}))
    assert out["ok"] is False and "question" in out["error"]


def test_stop_between_stages_cancels_the_run(plugin):
    mod, ctx = plugin
    rid = json.loads(ctx.tools["committee_open"]({"question": "q"}, session_id="s9"))["run_id"]
    ctx.tools["committee_advance"]({"run_id": rid}, session_id="s9")
    ctx.hooks["on_session_end"](session_id="s9", interrupted=True, turn_exit_reason="interrupted_by_user")
    out = json.loads(ctx.tools["committee_advance"]({"run_id": rid}, session_id="s9"))
    assert out["status"] == "cancelled"


def test_abandoned_tool_call_cancels_only_that_session(plugin):
    mod, ctx = plugin
    a = json.loads(ctx.tools["committee_open"]({"question": "q"}, session_id="A"))["run_id"]
    b = json.loads(ctx.tools["committee_open"]({"question": "q"}, session_id="B"))["run_id"]
    ctx.hooks["post_tool_call"](tool_name="committee_advance", session_id="A", status="cancelled",
                                error_type="tool_interrupted")
    ctx.hooks["post_tool_call"](tool_name="terminal", session_id="B", status="cancelled")   # other tools ignored
    assert json.loads(ctx.tools["committee_advance"]({"run_id": a}, session_id="A"))["status"] == "cancelled"
    assert json.loads(ctx.tools["committee_advance"]({"run_id": b}, session_id="B"))["status"] != "cancelled"


def test_bad_run_id_is_rejected(plugin):
    _, ctx = plugin
    assert "Invalid run id" in json.loads(ctx.tools["committee_advance"]({"run_id": "../../etc"}))["error"]

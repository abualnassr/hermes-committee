"""Data the v3 page draws from: live peeks as calls land, lanes, tokens, totals, the event log, final stances,
and the chairman's action items."""
import copy
import random
import threading

from committee import config as c
from committee import engine as e
from committee import insights, prompts

from test_deliberation import _agenda, _run


def _go(run, llm, stop_after=None):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
        if stop_after and run["status"] == stop_after:
            break
    return run


def _plain(settings, fake_llm, **kw):
    run = e.new_run(c.build_roster(settings, **kw), question="Swap GB-7 now?", rng=random.Random(3))
    return run, fake_llm()


def test_live_peeks_land_with_each_call(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    seen = []
    orig = e._parallel

    def spy(jobs, **kw):
        cb = kw["on_done"]
        kw["on_done"] = lambda k, ok: (cb(k, ok), seen.append(copy.deepcopy(run["live"])))
        return orig(jobs, **kw)

    e._parallel = spy
    try:
        _go(run, llm, stop_after="review_done")
    finally:
        e._parallel = orig
    first = next(x for x in seen if x["stage"] == "stage1")
    assert len(first["answers"]) == 1 and "says incremental" in next(iter(first["answers"].values()))
    in_review = [x for x in seen if x["stage"] == "review"]
    assert len(in_review[0]["ballots"]) == 1 and len(in_review[-1]["ballots"]) == len(run["reviews"])


def test_matrix_fills_from_live_ballots(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm, stop_after="stage1_done")
    run.update(status="stage1_done", live={"stage": "review", "started": run["created"] + 30, "done": {"A": True},
                                            "ballots": {"A": ["A", "B", "C"]}})
    m = insights.ballot_matrix(run)
    assert m["rows"] == ["A"] and m["cells"]["A"]["B"] == 2


def test_lanes_for_a_finished_run(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm)
    run["status"] = "done"
    L = insights.lanes(run)
    n = len(run["seats"])
    assert sum(1 for b in L if b["stage"] == "stage1") == n and sum(1 for b in L if b["stage"] == "review") == n
    assert [b["who"] for b in L if b["stage"] == "chair"] == ["chair"]
    st = insights.stage_starts(run)
    assert all(b["start"] == st["review"] for b in L if b["stage"] == "review") and not any(b["running"] for b in L)
    assert all(b["tokens"] == 150 for b in L) and all("cost" in b for b in L)


def test_running_bars_for_calls_still_out(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm, stop_after="stage1_done")
    run.update(status="stage1_done", live={"stage": "review", "started": run["created"] + 40, "done": {"A": True}})
    running = [b for b in insights.lanes(run) if b["running"]]
    assert sorted(b["who"] for b in running) == sorted(s["label"] for s in run["seats"] if s["label"] != "A")
    assert all(b["stage"] == "review" and b["secs"] is None and b["start"] == 40 for b in running)


def test_tokens_and_totals_never_price_the_unknown(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm)
    tok = insights.seat_tokens(run)
    assert tok["chair"] == 150 and all(tok[s["label"]] == 300 for s in run["seats"])
    run["usage"][0]["cost_known"] = False
    run["usage"][0]["cost_est"] = 9.0
    t = insights.run_totals(run)
    assert t["calls"] == len(run["usage"]) and t["unknown_calls"] >= 1 and t["cost_usd"] < 9.0


def test_event_log_keeps_names_out_of_ballots(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm)
    run["status"] = "done"
    ev = insights.event_log(run)
    kinds = [x["kind"] for x in ev]
    assert kinds[0] == "start" and kinds[-1] == "verdict" and kinds.count("answer") == len(run["seats"])
    assert all(x["who"].startswith("Response ") for x in ev if x["kind"] == "ballot")
    assert kinds.index("ranking") > max(i for i, k in enumerate(kinds) if k == "ballot")
    assert [x["t"] for x in ev] == sorted(x["t"] for x in ev)


def test_event_log_shows_the_debate(settings, fake_llm):
    run = _run(settings, deliberate="material")
    _go(run, fake_llm(agenda=_agenda(run, "claim")))
    run["status"] = "done"
    ev = insights.event_log(run)
    assert any(x["kind"] == "agenda" and x["who"] == "D1" for x in ev)
    assert any(x["kind"] == "reply" and "on D1" in x["text"] for x in ev)
    assert any(b["stage"] == "deliberate" for b in insights.lanes(run))


def test_final_stances_only_after_the_verdict(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm)
    assert insights.final_stances(run) == {}                         # chaired, not done yet
    run["status"] = "done"
    st = insights.final_stances(run)
    assert st["B"] == "dissent" and all(v == "agree" for k, v in st.items() if k != "B")


def test_action_items_with_owner_and_when():
    assert "action_items" in prompts.CHAIR_SCHEMA["required"] and "next_actions" not in prompts.CHAIR_SCHEMA["required"]
    out = e._norm_chair({"decision": "d", "action_items": [{"action": "Sign the checklist", "owner": "Shift supervisor",
                                                            "when": "today"}, {"action": "Order seals", "owner": "", "when": ""}]}, "")
    assert out["next_actions"] == ["Sign the checklist — Shift supervisor · today", "Order seals"]
    assert out["action_items"][0]["owner"] == "Shift supervisor"
    old = e._norm_chair({"decision": "d", "next_actions": ["map modules"]}, "")
    assert old["action_items"] == [{"action": "map modules", "owner": "", "when": ""}]


def test_public_run_carries_the_views(settings, fake_llm):
    run, llm = _plain(settings, fake_llm)
    _go(run, llm)
    run["status"] = "done"
    pub = insights.public_run(run)
    assert {"lanes", "seat_tokens", "totals", "events", "stances", "starts"} <= set(pub)

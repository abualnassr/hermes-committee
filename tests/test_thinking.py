"""Thinking levels: medium for the seats' answers and the verdict, low for the clerical steps, the seat's or
chairman's own choice where it applies, and the call goes out as before when a provider refuses the setting."""
import random
import threading

from committee import config as c
from committee import engine as e
from committee import hostcaps


def _run(settings, seats=None, chairman=None):
    if chairman is not None:
        settings["presets"]["board"]["chairman"].update(chairman)
    for i, extra in enumerate(seats or []):
        settings["presets"]["board"]["seats"][i].update(extra)
    return e.new_run(c.build_roster(settings), question="Rewrite or modernize?", rng=random.Random(7))


def _go(run, llm):
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None, rng=random.Random(3))


def test_slot_keeps_a_valid_level_and_drops_anything_else():
    assert c._slot({"model": "m", "thinking": "High"})["thinking"] == "high"
    assert "thinking" not in c._slot({"model": "m", "thinking": "turbo"})
    assert "thinking" not in c._slot({"model": "m"})


def test_answers_and_verdict_think_at_medium_the_rest_at_low(settings, fake_llm, tmp_path):
    hostcaps.set_base(tmp_path)
    run, llm = _run(settings), fake_llm()
    _go(run, llm)
    by_schema: dict[str, set] = {}
    for k in llm.calls:
        by_schema.setdefault(k["schema_name"], set()).add(k["thinking"])
    assert by_schema == {"committee_answer": {"medium"}, "committee_review": {"low"}, "committee_verdict": {"medium"}}
    assert {u["stage"]: u["thinking"] for u in run["usage"]} == {"stage1": "medium", "review": "low", "chair": "medium"}


def test_a_seats_choice_sets_its_answer_only_and_the_chairmans_sets_the_verdict_only(settings, fake_llm, tmp_path):
    hostcaps.set_base(tmp_path)
    run = _run(settings, seats=[{"thinking": "high"}, {"thinking": "default"}], chairman={"thinking": "low"})
    llm = fake_llm()
    _go(run, llm)
    answer = {k["model"]: k["thinking"] for k in llm.calls if k["schema_name"] == "committee_answer"}
    assert answer == {"m-one": "high", "m-two": "default", "m-three": "medium", "m-four": "medium"}
    assert {k["thinking"] for k in llm.calls if k["schema_name"] == "committee_review"} == {"low"}
    assert [k["thinking"] for k in llm.calls if k["schema_name"] == "committee_verdict"] == ["low"]


def test_clerical_steps_ignore_the_chairmans_verdict_level():
    for stage in ("research", "deliberate", "describe", "review"):
        assert e.thinking_for(stage, {"model": "m", "thinking": "high"}) == "low"
    assert e.thinking_for("chair", {"model": "m", "thinking": "high"}) == "high"


class _RefusesThinking:
    """A provider that rejects any reasoning level, like an API without the field."""
    def __init__(self, inner):
        self.inner, self.calls = inner, inner.calls

    def structured(self, **kw):
        if kw.get("thinking", "default") != "default" and kw["model"] == "m-two":
            kw["thinking"] = kw["thinking"]
            self.calls.append({**kw, "refused": True, "schema_name": kw["schema_name"]})
            raise RuntimeError("Unrecognized request argument supplied: reasoning_effort")
        return self.inner.structured(**kw)

    def text(self, **kw):
        return self.inner.text(**kw)


def test_a_provider_that_refuses_the_level_is_called_as_before_and_remembered(settings, fake_llm, tmp_path):
    hostcaps.set_base(tmp_path)
    run = _run(settings)
    llm = _RefusesThinking(fake_llm())
    _go(run, llm)
    seat = next(s for s in run["seats"] if s["model"] == "m-two")
    assert run["answers"][seat["label"]]["status"] == "ok"
    assert hostcaps.model_caps(seat["provider"], "m-two")["thinking"] is False
    two = [k for k in llm.calls if k["model"] == "m-two" and k["schema_name"] == "committee_answer"]
    assert [k.get("refused", False) for k in two] == [True, False] and two[-1]["thinking"] == "default"
    # the next call to that model, in this run and later ones, goes straight out with no level
    assert all(k["thinking"] == "default" for k in llm.calls if k["model"] == "m-two" and not k.get("refused")
               and k["schema_name"] == "committee_review")


# ── the Hermes glue: one plugin task per level ───────────────────────────────

def test_register_adds_one_task_per_level_with_its_reasoning_effort(plugin):
    mod, ctx = plugin
    assert ctx.aux_tasks == {f"committee_think_{lv}": {"reasoning_effort": lv} for lv in ("low", "medium", "high")}
    assert mod._THINK_TASKS == {lv: f"committee_think_{lv}" for lv in ("low", "medium", "high")}


def test_calls_route_through_the_task_for_their_level_and_keep_the_seats_model(plugin, monkeypatch):
    mod, _ctx = plugin
    r = mod._HermesLLM._route("p1", "m-one", "medium")
    assert r == {"provider": "p1", "model": "m-one", "task": "committee_think_medium"}
    assert mod._HermesLLM._route("p1", "m-one", "default") == {"provider": "p1", "model": "m-one"}
    # an unset seat runs on the active model: spelled out, so the task's own routing never picks another one
    monkeypatch.setattr(hostcaps, "active_route", lambda: ("act-prov", "act-model"))
    sub = __import__(mod.__name__ + ".committee.hostcaps", fromlist=["x"])
    monkeypatch.setattr(sub, "active_route", lambda: ("act-prov", "act-model"))
    assert mod._HermesLLM._route("", "", "low") == {"provider": "act-prov", "model": "act-model",
                                                     "task": "committee_think_low"}
    # no active model known: no task, the call goes out exactly as it did before thinking levels
    monkeypatch.setattr(sub, "active_route", lambda: ("", ""))
    assert mod._HermesLLM._route("", "", "low") == {}


def test_a_hermes_without_plugin_tasks_runs_with_no_task(plugin):
    mod, _ctx = plugin
    mod._THINK_TASKS.clear()
    assert mod._HermesLLM._route("p1", "m-one", "high") == {"provider": "p1", "model": "m-one"}

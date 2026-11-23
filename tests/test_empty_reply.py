"""A debate reply cut off at its output cap gets more room; a reply with nothing in it counts as no reply.
Found in a real run: a thinking model used its whole 1,200-token allowance three times and wrote nothing, and
the run still said '3 of 3 replied'."""
import random
import threading

from committee import config as c, engine as e, pricing
from committee.engine import Reply


class Thinker:
    """`model` spends any cap up to `needs` tokens thinking and returns nothing usable; above it, it answers."""

    def __init__(self, fake, model, needs):
        self.fake, self.model, self.needs, self.caps = fake, model, needs, []

    def __getattr__(self, k):
        return getattr(self.fake, k)

    def structured(self, **kw):
        if kw["schema_name"] == "committee_reply" and kw["model"] == self.model:
            self.caps.append(kw["max_tokens"])
            if kw["max_tokens"] <= self.needs:
                return Reply("text", text="", usage={"input_tokens": 900, "output_tokens": kw["max_tokens"]})
        return self.fake.structured(**kw)

    def text(self, **kw):
        if kw["purpose"].endswith("deliberate") and kw["model"] == self.model:
            self.caps.append(kw["max_tokens"])
            return Reply("text", text="", usage={"input_tokens": 900, "output_tokens": kw["max_tokens"]})
        return self.fake.text(**kw)


def _debate(settings, fake_llm, needs):
    settings["deliberate"] = "always"
    seats = [s["name"] for s in c.build_roster(settings)["seats"]]
    agenda = {"disputes": [{"level": "action", "point": "Swap now?", "question": "Now or later?",
                            "positions": [{"seat": seats[0], "side": "Now", "view": "Now."},
                                          {"seat": seats[1], "side": "Later", "view": "Later."}]}],
              "reason": "They differ."}
    fake = fake_llm(agenda=agenda)
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    llm = Thinker(fake, "m-three", needs)
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    who = next(s["name"] for s in run["seats"] if s["model"] == "m-three")
    return run, fake, llm, who


def test_a_cut_off_reply_is_asked_again_with_more_room(settings, fake_llm):
    run, _, llm, who = _debate(settings, fake_llm, needs=pricing.REPLY_MAX)
    assert llm.caps == [pricing.REPLY_MAX, 3 * pricing.REPLY_MAX]
    reply = run["deliberation"]["rounds"][0]["replies"][who]
    assert reply["status"] == "ok" and reply["responses"]


def test_the_extra_room_never_exceeds_the_runs_own_cap(settings, fake_llm):
    settings["presets"]["board"]["max_tokens"] = 2000
    run, _, llm, who = _debate(settings, fake_llm, needs=10 ** 6)
    assert run["max_tokens"] == 2000 and llm.caps == [pricing.REPLY_MAX, 2000, 2000]


def test_an_empty_reply_is_no_reply(settings, fake_llm):
    run, fake, llm, who = _debate(settings, fake_llm, needs=10 ** 6)
    reply = run["deliberation"]["rounds"][0]["replies"][who]
    assert reply["status"] == "failed" and "empty reply" in reply["error"]
    rnd = next(t for t in run["timeline"] if t["event"] == "deliberate_round")
    assert rnd["replies"] == rnd["of"] - 1
    chair = next(x["text"] for x in fake.calls if x["purpose"].endswith("chair"))
    assert f"## {who}\n(no reply)" in chair
    assert run["status"] == "chaired"                     # one silent debater does not stop the verdict


def test_a_reply_with_only_a_position_still_counts():
    out = e._norm_reply({"responses": [], "updated_position": "Swap after the shutdown."}, "", [], "X")
    assert out["updated_position"] == "Swap after the shutdown."


class Overthinker(Thinker):
    """Thinks through any cap and writes nothing, unless thinking is off (seen live with a reasoning-heavy model)."""

    def __init__(self, fake, model):
        super().__init__(fake, model, 0)
        self.levels = []

    def structured(self, **kw):
        if kw["schema_name"] == "committee_reply" and kw["model"] == self.model:
            self.levels.append(kw.get("thinking"))
            if kw.get("thinking") != "none":
                return Reply("text", text="", usage={"input_tokens": 900, "output_tokens": kw["max_tokens"]})
        return self.fake.structured(**kw)


def test_a_seat_that_thinks_through_its_reply_gets_thinking_off_for_the_rest_of_the_run(settings, fake_llm):
    settings["deliberate"] = "always"
    settings["deliberate_rounds"] = 2
    seats = [s["name"] for s in c.build_roster(settings)["seats"]]
    agenda = {"disputes": [{"level": "action", "point": "Swap now?", "question": "When?",
                            "positions": [{"seat": n, "side": "now" if i else "later", "view": "v"} for i, n in enumerate(seats)]}],
              "reason": "They differ."}
    fake = fake_llm(agenda=agenda)
    run = e.new_run(c.build_roster(settings, deliberate_rounds=2), question="Swap GB-7 now?", rng=random.Random(2))
    llm = Overthinker(fake, "m-three")
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    who = next(s["name"] for s in run["seats"] if s["model"] == "m-three")
    assert llm.levels[:2] == ["low", "none"]                 # round 1: cut off, then asked again with thinking off
    assert run["deliberation"]["rounds"][0]["replies"][who]["status"] == "ok"
    assert run["think_off"] == [who]
    if len(run["deliberation"]["rounds"]) > 1:              # round 2 (if it ran) starts with thinking off
        assert llm.levels[2] == "none"
    answer = [x for x in fake.calls if x["model"] == "m-three" and x["purpose"].endswith("stage1")]
    assert answer and answer[0]["thinking"] != "none"        # the answer itself keeps its level


def test_live_replies_keep_the_time_they_arrived(settings, fake_llm, monkeypatch):
    from committee import insights
    run = e.new_run(c.build_roster(settings), question="q", rng=random.Random(1))
    t0 = insights._t0(run)
    run["live"] = {"stage": "deliberate", "started": t0 + 100, "replied_at": {"Ana": t0 + 130},
                   "replies": {"Ana": [{"dispute": "D1", "stance": "hold"}]}}
    run["status"] = "deliberating"
    first = [x["t"] for x in insights.event_log(run) if x["kind"] == "reply"]
    monkeypatch.setattr(insights, "_now_rel", lambda _r: 999.0)
    assert first == [130.0] and [x["t"] for x in insights.event_log(run) if x["kind"] == "reply"] == [130.0]

"""A plain-text debate reply that is JSON in the model's own words is read, not stored as raw text."""
import pytest

from committee import engine as e

D = [{"id": "D1", "positions": [{"seat": "Finance", "side": "wait for shutdown"}, {"seat": "Risk Officer", "side": "change now"}]},
     {"id": "D2", "positions": [{"seat": "Finance", "side": "if crane late"}, {"seat": "Risk Officer", "side": "if oil rises"}]}]

# the shape a reasoning-heavy model wrote in a live run (round 1)
REAL = '''{
  "points": [
    {"point": "D1", "stance": "change", "side_tag": "change now",
     "argument": "Change: I now back changing now, not waiting. A failure costs far more than the change [E1]."},
    {"point": "D2", "stance": "change", "side_tag": "if oil rises", "argument": "Same reason [E1]."}
  ],
  "updated_position": "Change GB-7 at the weekend stop.",
  "confidence": 0.7
}'''


def test_the_real_shape_is_read():
    r = e._norm_reply(None, REAL, D, "Finance")
    assert [(x["dispute"], x["stance"], x["side"]) for x in r["responses"]] == [
        ("D1", "change", "change now"), ("D2", "change", "if oil rises")]
    assert r["updated_position"] == "Change GB-7 at the weekend stop." and r["fallback"]


def test_inside_a_code_fence():
    r = e._norm_reply(None, "Here is my reply:\n```json\n" + REAL + "\n```", D, "Finance")
    assert len(r["responses"]) == 2


def test_json_it_cannot_read_is_a_failed_reply_not_a_position():
    with pytest.raises(RuntimeError, match="unreadable reply"):
        e._norm_reply(None, '{"thoughts": "hmm", "points": "D1 I change"', D, "Finance")


def test_prose_is_still_kept_as_the_position():
    r = e._norm_reply(None, "I now agree with the oil check.", D, "Finance")
    assert r["responses"] == [] and r["updated_position"].startswith("I now agree")

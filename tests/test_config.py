import pytest

from committee import config as c
from committee.templates import TEMPLATES, get_template


def test_zero_config_is_classic_on_active_model():
    r = c.build_roster(None)
    assert r["template"] == "classic"
    assert len(r["seats"]) == 4
    assert all(s["provider"] == "" and s["model"] == "" for s in r["seats"])
    assert all(s["instructions"] == "" for s in r["seats"])      # classic: no personas
    assert r["quorum"] == 3
    assert any("correlated" in w for w in r["warnings"])


def test_default_preset_maps_template_roles_by_position(settings):
    r = c.build_roster(settings)
    assert r["preset"] == "board" and r["template"] == "decision-board"
    assert [s["name"] for s in r["seats"]] == [s["name"] for s in TEMPLATES["decision-board"]["seats"]]
    assert [s["model"] for s in r["seats"]] == ["m-one", "m-two", "m-three", "m-four"]
    assert r["chairman"] == {"provider": "p-chair", "model": "m-chair"}
    assert r["warnings"] == []


def test_seat_override_and_adhoc_seats_borrow_models(settings):
    settings["presets"]["board"]["seats"][1]["name"] = "Custom"
    r = c.build_roster(settings)
    assert r["seats"][1]["name"] == "Custom"
    adhoc = c.build_roster(settings, seats=[{"name": "Lawyer", "instructions": "Law."},
                                            {"name": "CFO", "instructions": "Money."},
                                            {"name": "Own", "provider": "px", "model": "mx"}])
    assert [(s["name"], s["model"]) for s in adhoc["seats"]] == [("Lawyer", "m-one"), ("CFO", "m-two"), ("Own", "mx")]


def test_template_override_and_review_flag(settings):
    r = c.build_roster(settings, template="red-team", review=False)
    assert r["template"] == "red-team" and r["review"] is False
    assert c.planned_calls(r) == {"research": 0, "describe": 0, "stage1": 4, "review": 0, "deliberate": 0, "chairman": 1, "total": 5,
                                  "total_if_debate": 5}


@pytest.mark.parametrize("kwargs,msg", [
    ({"preset": "nope"}, "Unknown preset"),
    ({"template": "nope"}, "Unknown template"),
    ({"seats": [{"name": "solo"}]}, "2-8 seats"),
    ({"seats": [{"name": "a"}, {"name": "A"}]}, "unique"),
    ({"seats": [{"name": "a", "provider": "moa"}, {"name": "b"}]}, "virtual"),
])
def test_invalid_requests(settings, kwargs, msg):
    with pytest.raises((c.ConfigError, KeyError), match=msg):
        c.build_roster(settings, **kwargs)


def test_stage_timeout_is_capped_below_hermes_tool_limit(settings):
    settings["stage_timeout"] = 9999
    assert c.build_roster(settings)["stage_timeout"] == c.STAGE_TIMEOUT_MAX < 420


def test_every_template_is_well_formed():
    for key in TEMPLATES:
        t = get_template(key)
        assert c.MIN_SEATS <= len(t["seats"]) <= c.MAX_SEATS
        assert t["chairman_guidance"]

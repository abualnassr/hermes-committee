"""Model notes without Hermes internals: prices from the page, lessons from earlier runs."""
import random
import threading

from committee import config as c
from committee import engine as e
from committee import hostcaps

ROWS = [{"slug": "openrouter", "auth_type": "api_key",
         "pricing": {"vendor/m": {"input": "2.00", "output": "10.00", "free": False},
                     "vendor/free": {"input": "free", "output": "free", "free": True},
                     "vendor/unknown": {"input": "", "output": "?", "free": False}}},
        {"slug": "my-sub", "auth_type": "oauth_device_code", "pricing": {"big": {"input": "3", "output": "15"}}}]


def test_prices_and_billing_from_model_options(tmp_path):
    hostcaps.set_base(tmp_path)
    assert hostcaps.remember_providers(ROWS) == 3
    assert hostcaps.price({"provider": "openrouter", "model": "vendor/m"}) == {"in": 2.0, "out": 10.0, "billing": "paid"}
    assert hostcaps.price({"provider": "openrouter", "model": "vendor/free"})["in"] == 0.0
    assert hostcaps.price({"provider": "openrouter", "model": "vendor/unknown"})["billing"] == "unknown"
    assert hostcaps.price({"provider": "my-sub", "model": "big"})["billing"] == "subscription"
    assert hostcaps.price({"provider": "my-sub", "model": "big"}, overrides={"my-sub": "paid"})["billing"] == "paid"


def test_pictures_are_tried_until_a_model_refuses(tmp_path):
    hostcaps.set_base(tmp_path)
    slot = {"provider": "p", "model": "m"}
    assert hostcaps.sees_images(slot) is True
    hostcaps.learn("p", "m", vision=False)
    assert hostcaps.sees_images(slot) is False and hostcaps.sees_images({**slot, "vision": True}) is True
    hostcaps.learn("p", "m", context_fail=90_000)
    hostcaps.learn("p", "m", context_fail=120_000)          # keeps the smaller size that failed
    assert hostcaps.model_caps("p", "m")["context_fail"] == 90_000


def test_error_classifier():
    assert hostcaps.looks_like_image_rejection("Error 400: this model does not support image input")
    assert hostcaps.looks_too_long("This model's maximum context length is 65536 tokens")
    assert not hostcaps.looks_like_image_rejection("maximum context length exceeded")
    assert not hostcaps.looks_like_image_rejection("rate limit, try again")


class _RejectsImages:
    """A fake model that refuses picture input, like a text-only provider."""

    def __init__(self, fake):
        self.fake, self.calls = fake, []

    def structured(self, **kw):
        self.calls.append(kw)
        if kw.get("images") and kw.get("model") == "m-two":
            raise RuntimeError("400 Bad Request: model does not support image_url content")
        return self.fake.structured(**kw)

    def text(self, **kw):
        return self.fake.text(**kw)


def test_a_seat_that_refuses_the_photo_answers_from_the_description(settings, fake_llm, tmp_path):
    hostcaps.set_base(tmp_path)
    run = e.new_run(c.build_roster(settings), question="Gearbox?", rng=random.Random(2))
    run["evidence_index"] = {"tokens": 1600, "sections": [],
                             "images": [{"id": "I1", "source": "gear.jpg", "locator": "", "description": "Worn gear teeth",
                                         "mime": "image/jpeg", "file": "I1.jpg"}]}
    run["_images"] = {"I1": {"data": b"\xff\xd8x", "mime": "image/jpeg"}}
    run["_sections"] = []
    for s in run["seats"]:
        s["sees_images"] = True
    llm = _RejectsImages(fake_llm())
    e.advance(run, llm, threading.Event(), lambda _m: None)
    seat = next(s for s in run["seats"] if s["model"] == "m-two")
    assert run["answers"][seat["label"]]["status"] == "ok" and seat["image_rejected"] is True
    assert hostcaps.model_caps(seat["provider"], "m-two")["vision"] is False
    retry = [k for k in llm.calls if k.get("model") == "m-two"][-1]
    assert not retry.get("images") and "Worn gear teeth" in retry["text"]


HUB = [{"slug": "openrouter", "pricing": {"vendor/brand-model-2.5": {"input": "4", "output": "20"},
                                          "lab/small-1.3-fast": {"input": "0.12", "output": "0.40"},
                                          "lab/small-1.3-fast:free": {"input": "free", "output": "free", "free": True}}},
       {"slug": "direct-sub", "models": ["brand-model-2-5[1m]"]},
       {"slug": "lab-direct", "models": ["small-1.3-fast"]}]


def test_direct_providers_use_the_hub_list_price_by_model_name(tmp_path):
    hostcaps.set_base(tmp_path)
    hostcaps.remember_providers(HUB)
    assert hostcaps.norm("brand-model-2-5[1m]") == hostcaps.norm("vendor/brand-model-2.5") == "brand-model-2-5"
    p = hostcaps.price({"provider": "lab-direct", "model": "small-1.3-fast"})
    assert p == {"in": 0.12, "out": 0.40, "billing": "paid", "via": "OpenRouter"}      # not the ':free' variant
    assert hostcaps.price({"provider": "lab-direct", "model": "no-such-model"})["billing"] == "unknown"


def test_the_users_subscription_choice_counts_zero_even_without_a_price(tmp_path):
    hostcaps.set_base(tmp_path)
    hostcaps.remember_providers(HUB)
    hostcaps.set_billing("direct-sub", "subscription")
    p = hostcaps.price({"provider": "direct-sub", "model": "brand-model-2-5[1m]"})
    assert p == {"in": 0.0, "out": 0.0, "billing": "subscription"}                # a subscription is never priced
    hostcaps.set_billing("lab-direct", "subscription")
    q = hostcaps.price({"provider": "lab-direct", "model": "no-such-model"})
    assert q == {"in": 0.0, "out": 0.0, "billing": "subscription"}
    # config.yaml's settings.billing still wins; forgetting the choice goes back to the default
    assert hostcaps.price({"provider": "lab-direct", "model": "small-1.3-fast"}, overrides={"lab-direct": "paid"})["billing"] == "paid"
    hostcaps.set_billing("lab-direct", None)
    assert hostcaps.price({"provider": "lab-direct", "model": "no-such-model"})["billing"] == "unknown"
    # a refreshed model list keeps the choice
    hostcaps.remember_providers(HUB)
    assert hostcaps.price({"provider": "direct-sub", "model": "brand-model-2-5[1m]"})["billing"] == "subscription"


def test_hermes_detection_and_pricing_come_first_and_the_users_price_fills_gaps(tmp_path, monkeypatch):
    from committee import _hermes_compat as hc
    hostcaps.set_base(tmp_path)
    monkeypatch.setattr(hc, "_on", lambda: True)
    monkeypatch.setattr(hc, "billing", lambda p, m="": {"codex": "subscription", "vendor-b": "paid"}.get(p))
    monkeypatch.setattr(hc, "price", lambda p, m: (0.15, 0.60) if (p, m) == ("vendor-b", "flash") else None)
    assert hostcaps.price({"provider": "codex", "model": "gpt"}) == {"in": 0.0, "out": 0.0, "billing": "subscription"}
    assert hostcaps.price({"provider": "vendor-b", "model": "flash"}) == {"in": 0.15, "out": 0.60, "billing": "paid"}
    assert hostcaps.model_caps("vendor-b", "flash")["billing_detected"] is True
    # an API model nobody prices needs the user's price; it is then used and labelled
    assert hostcaps.price({"provider": "vendor-b", "model": "new-x"})["billing"] == "unknown"
    hostcaps.set_price("vendor-b", "new-x", 0.3, 1.2)
    assert hostcaps.price({"provider": "vendor-b", "model": "new-x"}) == {"in": 0.3, "out": 1.2, "billing": "paid", "via": "your price"}
    # the user's choice still corrects a wrong detection
    hostcaps.set_billing("codex", "paid")
    assert hostcaps.price({"provider": "codex", "model": "gpt"})["billing"] == "unknown"

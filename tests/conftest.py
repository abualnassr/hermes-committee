"""Shared fixtures: a deterministic fake LLM for the pure engine, and a fake Hermes ctx for the glue."""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from committee.engine import Reply  # noqa: E402

_LABELS = re.compile(r"## Response ([A-Z])")


class FakeLLM:
    """Answers by schema. ``fail``/``slow``/``text_only`` are keyed by model id."""

    def __init__(self, fail=(), slow=None, text_only=(), self_identify=False):
        self.fail, self.slow, self.text_only = set(fail), dict(slow or {}), set(text_only)
        self.self_identify = self_identify
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def _log(self, **kw):
        with self._lock:
            self.calls.append(kw)

    def structured(self, *, provider, model, instructions, text, schema, schema_name, max_tokens, timeout, purpose):
        self._log(kind="structured", provider=provider, model=model, instructions=instructions, text=text,
                  schema_name=schema_name)
        if model in self.slow:
            time.sleep(self.slow[model])
        if model in self.fail:
            raise RuntimeError(f"{model} is down")
        if model in self.text_only:
            return Reply(content_type="text", text="not json", usage={"input_tokens": 1, "output_tokens": 1})
        u = {"input_tokens": 100, "output_tokens": 50, "cost_usd": None}
        if schema_name == "committee_answer":
            me = f" As {model}, I think so." if self.self_identify else ""
            return Reply("json", {"answer": f"Answer by {model}.{me} Do the incremental option.",
                                  "summary": f"{model} says incremental", "confidence": 0.8, "key_points": ["k"],
                                  "assumptions": [], "risks": ["r"], "would_change_mind": "new data",
                                  "evidence_gaps": []}, usage=u, provider=provider, model=model)
        if schema_name == "committee_review":
            labels = _LABELS.findall(text)
            return Reply("json", {"evaluations": [{"label": l, "strengths": "clear", "weaknesses": "thin"} for l in labels],
                                  "ranking": sorted(labels)}, usage=u, provider=provider, model=model)
        if schema_name == "committee_verdict":
            return Reply("json", {"final_answer": "## Verdict\nGo incremental.\n- step one", "decision": "Go incremental.",
                                  "confidence": "high", "confidence_note": "all agree", "reverse_if": ["EOL platform"],
                                  "agreement": ["incremental"], "dissent": [{"label": "B", "view": "partial rewrite"}],
                                  "open_questions": [], "next_actions": ["map modules"]},
                         usage=u, provider=provider, model=model)
        raise AssertionError(schema_name)

    def text(self, *, provider, model, instructions, text, max_tokens, timeout, purpose):
        self._log(kind="text", provider=provider, model=model, instructions=instructions, text=text)
        if model in self.fail:
            raise RuntimeError(f"{model} is down")
        return Reply("text", text="Plain text answer.\nFINAL RANKING:\n1. Response B\n2. Response A",
                     usage={"input_tokens": 10, "output_tokens": 5})


PRESET_SETTINGS = {
    "default_preset": "board",
    "presets": {"board": {
        "template": "decision-board",
        "chairman": {"provider": "p-chair", "model": "m-chair"},
        "seats": [{"provider": "p1", "model": "m-one"}, {"provider": "p2", "model": "m-two"},
                  {"provider": "p3", "model": "m-three"}, {"provider": "p4", "model": "m-four"}],
    }},
}


@pytest.fixture
def settings():
    import copy
    return copy.deepcopy(PRESET_SETTINGS)


@pytest.fixture
def fake_llm():
    return FakeLLM


# ── fake Hermes ctx for the plugin glue ─────────────────────────────────────

class FakeHandle:
    def __init__(self, log, text):
        self.log, self.ok, self.message_id = log, True, "m1"
        log.append(("post", text))

    def edit(self, text):
        self.log.append(("edit", text))
        return {"ok": True, "edited": True}


class FakeCtx:
    def __init__(self, settings, llm):
        self.settings, self.messages = settings, []
        self.tools, self.hooks, self.skills, self.cli = {}, {}, {}, {}
        self.llm = SimpleNamespace(complete_structured=self._structured, complete=self._complete)
        self._fake = llm

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def post_message(self, text):
        return FakeHandle(self.messages, text)

    def _structured(self, *, instructions, input, json_schema, schema_name, max_tokens, timeout, purpose,
                    provider=None, model=None):
        r = self._fake.structured(provider=provider or "", model=model or "", instructions=instructions,
                                  text=input[0]["text"], schema=json_schema, schema_name=schema_name,
                                  max_tokens=max_tokens, timeout=timeout, purpose=purpose)
        usage = SimpleNamespace(**{k: r.usage.get(k) for k in ("input_tokens", "output_tokens", "cost_usd")})
        return SimpleNamespace(content_type=r.content_type, parsed=r.parsed, text=r.text, usage=usage,
                               provider=provider, model=model)

    def _complete(self, *, messages, max_tokens, timeout, purpose, provider=None, model=None):
        r = self._fake.text(provider=provider or "", model=model or "", instructions=messages[0]["content"],
                            text=messages[1]["content"], max_tokens=max_tokens, timeout=timeout, purpose=purpose)
        return SimpleNamespace(text=r.text, usage=None, provider=provider, model=model)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_skill(self, name, path):
        self.skills[name] = path

    def register_cli_command(self, name, **kw):
        self.cli[name] = kw


@pytest.fixture
def plugin(tmp_path, monkeypatch, settings):
    """Load the plugin package the way Hermes does, with storage pointed at tmp_path."""
    name = "hermes_committee_under_test"
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_data_dir", lambda: tmp_path)
    ctx = FakeCtx(settings, FakeLLM())
    mod.register(ctx)
    mod._ACTIVE.clear()
    return mod, ctx

"""The Desktop page's model-maker logos: every maker rule has a bundled logo, and a model maps to its
maker (not to the route it is reached through). The rules are JavaScript regexes; the subset used is
also valid Python, so they are checked here without a JS runtime."""
import re
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text(encoding="utf-8")


def _makers():
    block = JS.split("const MAKERS = [", 1)[1].split("]]\n", 1)[0] + "]"
    return [(k, re.compile(rx)) for k, rx in re.findall(r"\['(\w+)', /((?:\\/|[^/])+)/\]", block)]


def _logos():
    block = JS.split("const LOGOS = {", 1)[1].split("\n}\n", 1)[0]
    return set(re.findall(r"^\s+(\w+): \[", block, re.M))


def maker_of(model, provider=""):
    find = lambda t: next((k for k, rx in _makers() if rx.search(t)), None)
    m = (model or "").lower()
    return find(m) if m else find((provider or "").lower())


def test_every_maker_rule_has_a_logo():
    keys = [k for k, _ in _makers()]
    assert len(keys) >= 15 and set(keys) <= _logos()


def test_models_map_to_their_maker_not_their_route():
    cases = {
        "openrouter/z-ai/glm-4.6": "zai", "glm-4.5-air": "zai",
        "x-ai/grok-3-mini": "grok", "grok-2": "grok",
        "claude-sonnet-4-5[1m]": "claude", "anthropic/claude-haiku": "claude",
        "gpt-4o-mini": "openai", "openai/o3": "openai", "gpt-oss-120b": "openai",
        "gemini-2.5-flash": "gemini", "google/gemma-3-27b": "gemma",
        "deepseek-chat": "deepseek", "qwen3-coder": "qwen", "moonshotai/kimi-k2": "kimi",
        "mistral-large": "mistral", "meta-llama/llama-3.1-70b": "meta", "nvidia/nemotron-70b": "nvidia",
        "minimax-m2": "minimax", "command-r-plus": "cohere", "mimo-v2": "xiaomimimo", "step-3": "stepfun",
        "hunyuan-large": "hunyuan",
    }
    for model, want in cases.items():
        assert maker_of(model) == want, model


def test_unknown_models_keep_initials_and_the_provider_only_fills_an_empty_model():
    for model in ("my-finetune-7b", "nimbus-pro", "local-model", "orbit-mini"):
        assert maker_of(model) is None, model
    assert maker_of("", "xai-oauth") == "grok"
    assert maker_of("nimbus-pro", "xai-oauth") is None   # a set model decides; the route never does

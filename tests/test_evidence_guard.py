"""Paths a model names in committee_open must never reach Hermes's credentials (catalog review)."""
import json
import os

import pytest


@pytest.fixture
def home(plugin, tmp_path, monkeypatch):
    """A fake Hermes root with a profile; the plugin's data dir sits inside the profile."""
    mod, _ctx = plugin
    root = tmp_path / "hermes"
    prof = root / "profiles" / "work"
    data = prof / "plugin-data" / "hermes-committee"
    data.mkdir(parents=True)
    monkeypatch.setattr(mod, "_data_dir", lambda: data)
    outside = tmp_path / "docs"
    outside.mkdir()
    return mod, root, prof, data, outside


def _open(mod, paths, **kw):
    return json.loads(mod.handle_open({"question": "Run the pump?", "evidence_files": [str(p) for p in paths]},
                                      session_id="s1", **kw))


def _w(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("rel", ["auth.json", ".env", "config.yaml", "logs/agent.log", "sessions/s.json"])
def test_files_inside_the_profile_home_are_refused(home, rel):
    mod, _root, prof, _data, _out = home
    out = _open(mod, [_w(prof / rel)])
    assert out.get("ok") is not True and "refused" in out["error"]


@pytest.mark.parametrize("rel", ["auth.json", "config.yaml", "mcp-tokens/github.json"])
def test_files_inside_the_shared_root_are_refused(home, rel):
    mod, root, _prof, _data, _out = home
    out = _open(mod, [_w(root / rel)])
    assert "refused" in out["error"]


@pytest.mark.parametrize("name", ["auth.json", ".env", ".env.local", "google_oauth.json",
                                  "webhook_subscriptions.json", "mcp-tokens/x.json", ".ssh/notes.txt"])
def test_known_credential_files_are_refused_anywhere(home, name):
    mod, _root, _prof, _data, out_dir = home
    out = _open(mod, [_w(out_dir / name)])
    assert "refused" in out["error"] and "credential" in out["error"]


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="no symlinks")
def test_a_symlink_to_a_credential_file_is_resolved_and_refused(home):
    mod, _root, prof, _data, out_dir = home
    target = _w(prof / "auth.json", '{"token": "secret"}')
    link = out_dir / "quotes.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks need privileges here")
    out = _open(mod, [link])
    assert "refused" in out["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_a_junction_into_the_hermes_home_is_resolved_and_refused(home):
    """Junctions need no admin rights on Windows, so this always exercises resolve() there."""
    import _winapi
    mod, _root, prof, _data, out_dir = home
    _w(prof / "memories" / "MEMORY.md", "private")
    j = out_dir / "notes"
    _winapi.CreateJunction(str(prof / "memories"), str(j))
    out = _open(mod, [j / "MEMORY.md"])
    assert "refused" in out["error"] and "Hermes" in out["error"]


def test_own_documents_outside_hermes_still_work(home):
    mod, _root, _prof, _data, out_dir = home
    out = _open(mod, [_w(out_dir / "pump-notes.txt", "Bearing temperature 92 C."),
                      _w(out_dir / "oauth-design.md", "Not a secret, just a design note.")])
    assert out.get("ok") is True, out


def test_files_in_the_plugins_own_data_dir_still_work(home):
    mod, _root, _prof, data, _out = home
    out = _open(mod, [_w(data / "packs" / "p1" / "notes.txt", "From an earlier upload.")])
    assert out.get("ok") is True, out


def test_paths_the_user_types_on_the_cli_are_trusted(home):
    mod, _root, prof, _data, _out = home
    out = _open(mod, [_w(prof / "my-notes.txt", "Typed by the user.")], _cli_trusted=True)
    assert out.get("ok") is True, out


def test_the_tool_cannot_claim_trust_through_its_arguments(home):
    mod, _root, prof, _data, _out = home
    args = {"question": "Q?", "evidence_files": [str(_w(prof / "auth.json"))], "_cli_trusted": True}
    out = json.loads(mod.handle_open(args, session_id="s1"))
    assert "refused" in out["error"]

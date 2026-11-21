"""Code and other text files as evidence: read by content, code with numbered lines."""
import pytest

from committee import evidence as ev
from committee import prompts

JS = b"function step(dir) {\r\n  if (busy) return\r\n  move(dir)\r\n}\r\n"


def test_code_is_numbered_and_labelled():
    f = ev.extract("controls.js", JS)
    assert f["kind"] == "code"
    item = f["items"][0]
    assert item["text"].splitlines() == ["1| function step(dir) {", "2|   if (busy) return", "3|   move(dir)", "4| }"]
    assert item["locator"] == "JavaScript · 4 lines, numbered"


@pytest.mark.parametrize("name,lang", [("engine.js.txt", "JavaScript"), ("Dockerfile", "Dockerfile"),
                                       ("tool.py", "Python"), ("App.TSX", "TypeScript (TSX)")])
def test_code_language_from_the_name(name, lang):
    assert ev.code_language(name) == lang


def test_plain_txt_and_unknown_text_files_are_text():
    assert ev.code_language("notes.txt") == "" and ev.kind_of("notes.txt") == "text"
    f = ev.extract("settings.conf", b"[server]\nport = 8080\n")
    assert f["kind"] == "text" and "port = 8080" in f["items"][0]["text"]
    assert ev.kind_of("noext", "plain words\n".encode()) == "text"


@pytest.mark.parametrize("name,data", [("tool.exe", b"MZ\x90\x00\x03"), ("blob.dat", b"\x00\x01\x02binary\x00" * 50),
                                       ("clip.mp4", b"....ftypisom")])
def test_binary_files_are_refused(name, data):
    with pytest.raises(ev.EvidenceError, match="not a text file"):
        ev.extract(name, data)


def test_utf16_and_arabic_text_are_text():
    assert ev.looks_text("مرحبا\n".encode("utf-16"))
    assert ev.looks_text("ملاحظات الصيانة\n".encode("utf-8"))


def test_seats_are_told_to_cite_file_and_line():
    code = [{"id": "E1", "source": "main.js", "locator": "JavaScript · 4 lines, numbered", "text": "1| x"}]
    out = "\n".join(prompts._files_block({"_sections": code}, True))
    assert "main.js:120" in out and "## [E1] main.js · JavaScript · 4 lines, numbered" in out
    plain = [{"id": "E1", "source": "a.txt", "locator": "", "text": "x"}]
    assert "main.js:120" not in "\n".join(prompts._files_block({"_sections": plain}, True))

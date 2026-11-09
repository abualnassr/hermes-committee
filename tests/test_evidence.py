"""Evidence files: every format is read into text sections or images, with honest notes."""
import io
import zipfile

import pytest

from committee import evidence as ev

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _png(w=2400, h=1200, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(w=800, h=1100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 90, 90)).save(buf, "JPEG")
    return buf.getvalue()


def _pdf(pages) -> bytes:
    """pages: list of str (a text page) or bytes (a page holding only that picture = scanned)."""
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    for p in pages:
        pdf.add_page()
        if isinstance(p, bytes):
            pdf.image(io.BytesIO(p), x=10, y=10, w=180)
        else:
            pdf.set_font("helvetica", size=11)
            pdf.multi_cell(0, 6, p)
    return bytes(pdf.output())


def _docx(body_xml: str, media=0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body>{body_xml}</w:body></w:document>")
        for i in range(media):
            z.writestr(f"word/media/image{i}.png", _png(10, 10))
    return buf.getvalue()


def _p(text, style=None):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def test_text_files_keep_arabic_and_report_tokens():
    f = ev.extract("notes.md", "# Gearbox\nالاهتزاز مرتفع".encode())
    assert f["kind"] == "text" and "الاهتزاز" in f["items"][0]["text"] and f["tokens"] > 0
    cp = ev.extract("old.txt", "الضغط".encode("cp1256"))
    assert cp["items"][0]["text"] == "الضغط"


def test_pdf_text_pages_and_a_scanned_page():
    pytest.importorskip("pypdf")
    data = _pdf(["Vibration 7.1 mm/s on the drive end bearing. " * 3, _jpeg(), "Oil debris 3x baseline. " * 4])
    f = ev.extract("survey.pdf", data)
    kinds = [(i["type"], i["locator"]) for i in f["items"]]
    assert kinds == [("text", "p. 1"), ("image", "p. 2 (scanned)"), ("text", "p. 3")]
    assert f["images"] == 1 and any("Scanned: 1 page" in n for n in f["notes"])
    assert f["tokens"] >= ev.IMAGE_TOKENS


def test_password_pdf_is_a_clear_error():
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter()
    w.add_blank_page(200, 200)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(ev.EvidenceError, match="password"):
        ev.extract("locked.pdf", buf.getvalue())


def test_docx_headings_lists_tables_and_a_picture_note():
    tbl = ("<w:tbl><w:tr><w:tc>" + _p("Tag") + "</w:tc><w:tc>" + _p("mm/s") + "</w:tc></w:tr>"
           "<w:tr><w:tc>" + _p("DE") + "</w:tc><w:tc>" + _p("7.1") + "</w:tc></w:tr></w:tbl>")
    f = ev.extract("report.docx", _docx(_p("Findings", "Heading1") + _p("Bearing is hot.") + tbl, media=2))
    text = f["items"][0]["text"]
    assert "## Findings" in text and "Bearing is hot." in text and "| DE | 7.1 |" in text
    assert any("2 pictures" in n for n in f["notes"])


def test_xlsx_sheets_dates_and_row_cut(monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trend"
    ws.append(["Date", "Velocity"])
    for d in range(30):
        ws.append([datetime.date(2026, 10, 1) + datetime.timedelta(days=d), 3.0 + d / 10])
    wb.create_sheet("Empty")
    buf = io.BytesIO()
    wb.save(buf)
    monkeypatch.setattr(ev, "XLSX_MAX_ROWS", 10)
    f = ev.extract("trend.xlsx", buf.getvalue())
    assert len(f["items"]) == 1 and f["items"][0]["locator"] == "sheet “Trend”"
    assert "| 2026-10-01 | 3 |" in f["items"][0]["text"]
    assert any("cut at 10 rows" in n for n in f["notes"])


def test_photos_are_shrunk_and_reencoded():
    f = ev.extract("pump.png", _png())
    it = f["items"][0]
    assert it["mime"] == "image/jpeg" and max(it["w"], it["h"]) == ev.IMAGE_LONG_SIDE
    assert f["tokens"] == ev.IMAGE_TOKENS


def test_unsupported_empty_and_oversize_files(monkeypatch):
    with pytest.raises(ev.EvidenceError, match="save it as .docx"):
        ev.extract("old.doc", b"x")
    with pytest.raises(ev.EvidenceError, match="not supported"):
        ev.extract("movie.mp4", b"x")
    with pytest.raises(ev.EvidenceError, match="empty"):
        ev.extract("a.txt", b"")
    monkeypatch.setattr(ev, "MAX_FILE_BYTES", 10)
    with pytest.raises(ev.EvidenceError, match="limit per file"):
        ev.extract("a.txt", b"x" * 11)


def test_long_text_is_cut_with_a_note(monkeypatch):
    monkeypatch.setattr(ev, "MAX_TEXT_CHARS", 100)
    f = ev.extract("big.txt", b"a" * 500)
    assert len(f["items"][0]["text"]) == 100 and any("Text cut" in n for n in f["notes"])


def test_sections_number_text_and_images_in_file_order():
    a = {"name": "a.md", "items": [{"type": "text", "locator": "", "text": "x"}]}
    b = {"name": "b.pdf", "items": [{"type": "text", "locator": "p. 1", "text": "y"},
                                    {"type": "image", "locator": "p. 2 (scanned)", "mime": "image/jpeg", "data": b"j"}]}
    secs, imgs = ev.sections([a, b])
    assert [s["id"] for s in secs] == ["E1", "E2"] and imgs[0]["id"] == "I1"
    assert ev.where(secs[1]) == "b.pdf · p. 1" and ev.where(imgs[0]) == "b.pdf · p. 2 (scanned)"

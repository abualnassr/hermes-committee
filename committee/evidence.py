"""Evidence files → text sections and images every seat can read. Hermes-free.

A file is read once, when it is added. Text formats, PDF (text pages; a scanned page is lifted out
as the picture it contains), Word (.docx, read with the standard library), Excel (.xlsx) and photos.
Optional libraries are imported lazily so a missing one names itself instead of breaking the plugin.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from pathlib import PurePath
from typing import Any
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 60
MAX_PACK_BYTES = 120 * 1024 * 1024
MAX_TEXT_CHARS = 400_000          # per file; more is cut with a note
MAX_IMAGES_PER_FILE = 30          # scanned pages per PDF
MAX_IMAGES = 40                   # per evidence pack (photos + scanned pages)
XLSX_MAX_ROWS, XLSX_MAX_COLS = 2000, 50
IMAGE_LONG_SIDE = 1568
IMAGE_TOKENS = 1600               # rough input tokens per image per call; providers differ
SCANNED_MIN_CHARS = 50            # a PDF page with less text than this is treated as scanned

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".ini", ".cfg", ".toml", ".rst"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
SUPPORTED_EXT = TEXT_EXT | IMAGE_EXT | {".pdf", ".docx", ".xlsx", ".xlsm"}
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class EvidenceError(ValueError):
    """A file that cannot be used, with a message written for the user."""


def text_tokens(text: str) -> int:
    return math.ceil(len(text or "") / 3.5)


def kind_of(name: str) -> str:
    ext = PurePath(name).suffix.lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in IMAGE_EXT:
        return "image"
    return {".pdf": "pdf", ".docx": "word", ".xlsx": "excel", ".xlsm": "excel"}.get(ext, "")


def unsupported_reason(name: str) -> str:
    ext = PurePath(name).suffix.lower()
    if ext == ".doc":
        return "Old Word format (.doc): save it as .docx and add it again."
    if ext == ".xls":
        return "Old Excel format (.xls): save it as .xlsx and add it again."
    if ext in (".pptx", ".ppt"):
        return "PowerPoint is not supported yet: save it as PDF and add the PDF."
    return f"{PurePath(name).suffix or 'This file type'} is not supported. Use text, PDF, Word, Excel or photos."


def extract(name: str, data: bytes) -> dict[str, Any]:
    """One file → ``{name, kind, size, items, notes, tokens, images}``.

    ``items`` are ``{"type": "text", "locator", "text"}`` or
    ``{"type": "image", "locator", "mime", "data", "w", "h"}``. Raises ``EvidenceError``."""
    kind = kind_of(name)
    if not kind:
        raise EvidenceError(unsupported_reason(name))
    if len(data) > MAX_FILE_BYTES:
        raise EvidenceError(f"{_mb(len(data))} is over the {_mb(MAX_FILE_BYTES)} limit per file.")
    if not data:
        raise EvidenceError("The file is empty.")
    notes: list[str] = []
    reader = {"text": _text_file, "pdf": _pdf, "word": _docx, "excel": _xlsx, "image": _image_file}[kind]
    items = reader(name, data, notes)
    items = _cap_text(items, notes)
    if not items:
        raise EvidenceError("No readable text or pictures were found in this file.")
    images = sum(1 for i in items if i["type"] == "image")
    tokens = sum(text_tokens(i["text"]) for i in items if i["type"] == "text") + images * IMAGE_TOKENS
    return {"name": PurePath(name).name, "kind": kind, "size": len(data), "items": items, "notes": notes,
            "tokens": tokens, "images": images}


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n < 10 * 1024 * 1024 else f"{n // (1024 * 1024)} MB"


def _cap_text(items: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    total, out = 0, []
    for it in items:
        if it["type"] != "text":
            out.append(it)
            continue
        if total >= MAX_TEXT_CHARS:
            continue
        room = MAX_TEXT_CHARS - total
        if len(it["text"]) > room:
            it = {**it, "text": it["text"][:room]}
            notes.append(f"Text cut at {MAX_TEXT_CHARS:,} characters (about {text_tokens('x' * MAX_TEXT_CHARS):,} tokens).")
        total += len(it["text"])
        out.append(it)
    return out


# ── text ─────────────────────────────────────────────────────────────────────

def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    for enc in ("cp1256", "latin-1"):        # Arabic Windows first, then anything
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _text_file(name: str, data: bytes, notes: list[str]) -> list[dict[str, Any]]:
    text = decode_text(data).replace("\r\n", "\n").strip()
    return [{"type": "text", "locator": "", "text": text}] if text else []


# ── PDF ──────────────────────────────────────────────────────────────────────

def _need(module: str, pip_name: str):
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise EvidenceError(f"Reading this file needs the '{pip_name}' package, which is not installed in this "
                            "Hermes environment yet. Restart the gateway (`hermes gateway restart`) after "
                            "updating the plugin, then add the file again.") from exc


def _pdf(name: str, data: bytes, notes: list[str]) -> list[dict[str, Any]]:
    pypdf = _need("pypdf", "pypdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise EvidenceError("This PDF is password-protected. Remove the password and add it again.")
        pages = list(reader.pages)
    except EvidenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - damaged PDFs raise many types
        raise EvidenceError(f"This PDF could not be read ({type(exc).__name__}).") from exc
    items: list[dict[str, Any]] = []
    scanned, skipped, blank = 0, 0, 0
    for n, page in enumerate(pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            text = ""
        if len(text) >= SCANNED_MIN_CHARS:
            items.append({"type": "text", "locator": f"p. {n}", "text": text})
            continue
        picture = _largest_page_image(page)
        if picture is None:
            if text:
                items.append({"type": "text", "locator": f"p. {n}", "text": text})
            else:
                blank += 1
            continue
        if scanned >= MAX_IMAGES_PER_FILE:
            skipped += 1
            continue
        try:
            items.append({"type": "image", "locator": f"p. {n} (scanned)", **normalize_image(picture)})
            scanned += 1
        except EvidenceError:
            blank += 1
    if scanned:
        notes.append(f"Scanned: {scanned} page{'s' if scanned > 1 else ''} read as images "
                     f"(≈ {scanned * IMAGE_TOKENS:,} tokens per call).")
    if skipped:
        notes.append(f"{skipped} more scanned pages left out (limit {MAX_IMAGES_PER_FILE} per PDF).")
    if blank:
        notes.append(f"{blank} page{'s' if blank > 1 else ''} with no text or picture.")
    return items


def _largest_page_image(page) -> bytes | None:
    try:
        imgs = list(page.images)
    except Exception:  # noqa: BLE001 - unsupported image filters
        return None
    best = None
    for im in imgs:
        try:
            raw = im.data
        except Exception:  # noqa: BLE001
            continue
        if raw and (best is None or len(raw) > len(best)):
            best = raw
    return best


# ── Word (.docx) ─────────────────────────────────────────────────────────────

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(name: str, data: bytes, notes: list[str]) -> list[dict[str, Any]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise EvidenceError("This Word file could not be read. Is it a real .docx?") from exc
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            lines.append(_para(el))
        elif el.tag == f"{_W}tbl":
            lines.extend(_table(el))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    pictures = sum(1 for n in z.namelist() if n.startswith("word/media/"))
    if pictures:
        notes.append(f"{pictures} picture{'s' if pictures > 1 else ''} inside the document not included. "
                     "Add them as photos if the committee should see them.")
    return [{"type": "text", "locator": "", "text": text}] if text else []


def _para(p) -> str:
    out = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            out.append(node.text)
        elif node.tag == f"{_W}tab":
            out.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            out.append("\n")
    text = "".join(out).strip()
    style = p.find(f"{_W}pPr/{_W}pStyle")
    sval = (style.get(f"{_W}val") or "").lower() if style is not None else ""
    m = re.match(r"heading(\d)", sval)
    if text and m:
        return "#" * min(6, int(m.group(1)) + 1) + " " + text
    if text and p.find(f"{_W}pPr/{_W}numPr") is not None:
        return "- " + text
    return text


def _table(tbl) -> list[str]:
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = [" ".join(_para(p) for p in tc.findall(f"{_W}p")).replace("|", "/").strip()
                 for tc in tr.findall(f"{_W}tc")]
        if any(cells):
            rows.append(cells)
    return _markdown_table(rows)


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["", "| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out + [""]


# ── Excel (.xlsx) ────────────────────────────────────────────────────────────

def _xlsx(name: str, data: bytes, notes: list[str]) -> list[dict[str, Any]]:
    openpyxl = _need("openpyxl", "openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceError(f"This Excel file could not be read ({type(exc).__name__}).") from exc
    items = []
    try:
        for ws in wb.worksheets:
            rows, cut = [], False
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= XLSX_MAX_ROWS:
                    cut = True
                    break
                cells = [_cell(v) for v in row[:XLSX_MAX_COLS]]
                if any(cells):
                    rows.append(cells)
            if not rows:
                continue
            last = max(max((j for j, c in enumerate(r) if c), default=0) for r in rows) + 1
            rows = [r[:last] for r in rows]
            items.append({"type": "text", "locator": f"sheet “{ws.title}”",
                          "text": "\n".join(_markdown_table(rows)).strip()})
            if cut:
                notes.append(f"Sheet “{ws.title}” cut at {XLSX_MAX_ROWS:,} rows.")
    finally:
        wb.close()
    if items:
        notes.append("Formulas are shown as their last saved values.")
    return items


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    if hasattr(v, "isoformat"):
        s = v.isoformat()
        return s[:-9] if s.endswith("T00:00:00") else s
    return str(v).replace("|", "/").replace("\n", " ").strip()


# ── images ───────────────────────────────────────────────────────────────────

def _image_file(name: str, data: bytes, notes: list[str]) -> list[dict[str, Any]]:
    return [{"type": "image", "locator": "", **normalize_image(data, PurePath(name).suffix.lower())}]


def normalize_image(data: bytes, ext: str = "") -> dict[str, Any]:
    """EXIF-rotate, shrink to IMAGE_LONG_SIDE and re-encode as JPEG. Without Pillow, PNG/JPEG/WebP
    pass through unchanged (up to 5 MB) and anything else is refused."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if ext in _MIME and len(data) <= 5 * 1024 * 1024:
            return {"mime": _MIME[ext], "data": data, "w": 0, "h": 0}
        raise EvidenceError("Photos in this format need the Pillow package, which this Hermes lacks.") from None
    if ext in (".heic", ".heif"):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise EvidenceError("HEIC photos need the pillow-heif package. Save the photo as JPEG.") from None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = ImageOps.exif_transpose(im)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((IMAGE_LONG_SIDE, IMAGE_LONG_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85, optimize=True)
    except Exception as exc:  # noqa: BLE001 - PIL raises many types for bad images
        raise EvidenceError(f"This picture could not be read ({type(exc).__name__}).") from exc
    return {"mime": "image/jpeg", "data": buf.getvalue(), "w": im.width, "h": im.height}


# ── pack-level view ──────────────────────────────────────────────────────────

def summary(f: dict[str, Any]) -> dict[str, Any]:
    """What the page shows for one file (no bytes, no text)."""
    return {k: f[k] for k in ("fid", "name", "kind", "size", "tokens", "images", "notes") if k in f} | {
        "pages": sum(1 for i in f["items"] if i["locator"].startswith("p. ")) or None,
        "chars": sum(len(i.get("text") or "") for i in f["items"])}


def sections(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Number the pack: text sections E1… and images I1…, in file order."""
    secs, imgs = [], []
    for f in files:
        for it in f["items"]:
            ref = {"source": f["name"], "locator": it["locator"]}
            if it["type"] == "text":
                secs.append({"id": f"E{len(secs) + 1}", **ref, "text": it["text"]})
            else:
                imgs.append({"id": f"I{len(imgs) + 1}", **ref, "mime": it["mime"], "data": it["data"]})
    return secs, imgs


def where(item: dict[str, Any]) -> str:
    return item["source"] + (f" · {item['locator']}" if item.get("locator") else "")

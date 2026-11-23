"""PDF committee report via fpdf2 + uharfbuzz text shaping.

Layout: a one-page summary first (title block, decision, key figures, what changed since an earlier
run, reversal triggers, next actions), then the committee (roster, positions, dissent, vote chart,
what the run reveals), the chairman's full reasoning, and appendices (each seat's answer, the
evidence with photo descriptions, how it ran and what it cost). Seats are named everywhere; the
anonymous letters stay inside the engine. Citations such as [E3] link to the evidence appendix;
sections are bookmarked.

Design: four type sizes (20 / 13 / 10 / 8 pt), text set to a reading measure of about 80 characters,
hanging indents, tabular figures, and one accent colour from a theme (``THEMES``), used only for the
decision, the top-ranked bar and links.

Any paragraph that contains Arabic script is laid out right-to-left and right-aligned
(fpdf2's default base direction reverses Arabic word order — verified in Phase 0).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from .config import answered_by, model_label
from .logos import logo_svg, maker_of
from .engine import usage_totals
from . import __version__
from .report_md import (DISCLAIMER, PRODUCT, about, arg_parts, debate_tally, delib_decided, delib_last, delib_line,
                        exec_summary, extra_cites,
                        stance_word, unbacked_note, move_of, solo_lines, defer_lines)

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FOOTER = "Advisory output from AI models, not professional approval. See the disclaimer at the end."
INK, MUTED, FAINT, RULE, TINT = (28, 28, 32), (96, 98, 108), (140, 142, 152), (220, 221, 226), (244, 244, 246)
TITLE, SECTION, BODY, SMALL = 20, 13, 10, 8          # the only four type sizes
MEASURE = 150                                         # mm: about 80 characters of 10 pt text
THEMES = {
    "slate": {"name": "Slate", "accent": (44, 72, 110), "tint": (236, 240, 246), "soft": (160, 177, 202)},
    "teal": {"name": "Teal", "accent": (13, 104, 92), "tint": (230, 243, 240), "soft": (140, 191, 182)},
    "oxblood": {"name": "Oxblood", "accent": (128, 40, 44), "tint": (247, 237, 236), "soft": (206, 160, 160)},
}
DEFAULT_THEME = "slate"
_AR = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_INLINE = [(re.compile(r"__(.+?)__"), r"\1"), (re.compile(r"`([^`]+)`"), r"\1"),
           (re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)"), r"\1 (\2)")]
_CITE = re.compile(r"\[((?:E|I|W)\d+(?:\s*[,;–-]\s*(?:[EIW]?\d+))*)\]")
STATUS = {"failed": "No verdict", "stopped": "Stopped", "running": "Running"}
_TNUM = {"tnum": True}


def short_model(slot: dict[str, Any]) -> str:
    """Model id without the provider route (tables); 'active model' when unset."""
    return slot.get("model") or model_label(slot)


def table_model(slot: dict[str, Any]) -> str:
    """The model's own name for a narrow table column: 'claude-sonnet-5.5', not 'anthropic/claude-sonnet-5.5'
    (the full route is in Appendix A). A slash in a name made it break mid-word."""
    m = slot.get("model") or ""
    return m.rsplit("/", 1)[-1] if m else model_label(slot)


def has_rtl(text: str) -> bool:
    return bool(_AR.search(text or ""))


def _plain(text: str, keep_bold: bool = False) -> str:
    text = str(text or "")
    for rx, rep in _INLINE:
        text = rx.sub(rep, text)
    if not keep_bold:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"</?details>|<summary>.*?</summary>", "", text)
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)   # drop emoji outside the bundled fonts


def conf_text(x: Any) -> str:
    """A seat's 0–1 confidence in the chairman's words: 'High (0.80)'."""
    if not isinstance(x, (int, float)):
        return "–"
    word = "High" if x >= 0.75 else "Medium" if x >= 0.5 else "Low"
    return f"{word} ({x:.2f})"


def duration(secs: float) -> str:
    secs = int(round(secs or 0))
    return f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def run_seconds(run: dict) -> float:
    """From opening to the last recorded event (the verdict, a failure or a stop)."""
    tl = run.get("timeline") or []
    return max(0.0, tl[-1]["t"] - run["created"]) if tl and run.get("created") else 0.0


def _new_pdf(run: dict[str, Any], theme: dict):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    head = f"{PRODUCT}  ·  {run.get('title') or 'Committee report'}"

    class Report(FPDF):
        MARKDOWN_LINK_UNDERLINE = False
        MARKDOWN_LINK_COLOR = theme["accent"]

        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("Sans", "", SMALL)
            self.set_text_color(*FAINT)
            where = getattr(self, "cur_section", "")
            self.cell(0, 6, _plain(head + (f"  ·  {where}" if where else "")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(6)

        def footer(self):
            self.set_y(-15)
            self.set_font("Sans", "", SMALL)
            self.set_text_color(*FAINT)
            self.cell(self.epw - 20, 5, FOOTER, align="L")
            self.cell(20, 5, f"{self.page_no()} / {{nb}}", align="R")

    pdf = Report(format="A4")
    pdf.set_margins(24, 18, 24)
    pdf.set_auto_page_break(True, 20)
    pdf.alias_nb_pages()
    pdf.add_font("Sans", "", FONTS / "NotoSans-Regular.ttf")
    pdf.add_font("Sans", "B", FONTS / "NotoSans-Bold.ttf")
    pdf.add_font("Arabic", "", FONTS / "NotoNaskhArabic-Regular.ttf")
    pdf.add_font("Arabic", "B", FONTS / "NotoNaskhArabic-Bold.ttf")
    # Noto Sans has no arrows, maths or check marks (→ ≈ ≤ ≥ ✓ ☐ …): without these, "12 → 40 ppm" printed as
    # "12  40 ppm". Subsets of Noto Sans Math and Symbols 2 fill the gap, in any weight.
    pdf.add_font("Math", "", FONTS / "NotoSansMath-Subset.ttf")
    pdf.add_font("Symbols", "", FONTS / "NotoSansSymbols2-Subset.ttf")
    pdf.set_fallback_fonts(["Arabic", "Sans", "Math", "Symbols"], exact_match=False)
    pdf.set_text_shaping(use_shaping_engine=True, features=_TNUM, direction="ltr")
    pdf.set_title(run.get("title") or f"Committee report {run['id']}")
    pdf.set_subject(_plain(run.get("question") or "")[:200])
    pdf.set_creator(f"{PRODUCT} {__version__} (plugin for Hermes Agent)")
    pdf.set_author(PRODUCT)
    pdf.set_keywords("Hermes Committee, AI committee, decision report")
    return pdf, XPos, YPos


def _md_safe(text: str) -> str:
    """Neutralise fpdf2's other markdown markers so only **bold** and links take effect."""
    return text.replace("\\", "/").replace("--", "–").replace("~~", "≈").replace("__", "_ _")


class _W:
    """Writing helpers bound to one document and one theme."""

    def __init__(self, pdf, XPos, YPos, theme: dict, ev_page: Optional[int] = None, known: Optional[set] = None):
        self.pdf, self.X, self.Y, self.t, self.ev_page = pdf, XPos, YPos, theme, ev_page
        self.known = known                     # evidence IDs the run has; a citation to anything else is not linked
        self.pages: dict[str, int] = {}        # section title -> page, for the executive summary's contents

    def explain(self, text: str, **kw):
        """A paragraph of the report's own words that shows the citation style ([E1], [W2]…): not checked."""
        known, self.known = self.known, None
        try:
            self.para(text, **kw)
        finally:
            self.known = known

    @property
    def accent(self):
        return self.t["accent"]

    def _prep(self, raw: str) -> tuple[str, bool, bool]:
        rtl = has_rtl(raw)
        text = _plain(raw, keep_bold=not rtl).strip()
        md = False
        if self.known is not None and _CITE.search(text):
            from .citations import cited
            text = _CITE.sub(lambda m: m.group(0) if not cited(m.group(0)) or set(cited(m.group(0))) & self.known
                             else f"[{m.group(1)}: not found]", text)
        if not rtl and ("**" in text or (self.ev_page and _CITE.search(text))):
            md = True
            text = _md_safe(text)
            if self.ev_page:
                text = _CITE.sub(lambda m: f"[[{m.group(1)}]({self.ev_page})]", text)   # brackets stay visible
        return text, rtl, md

    def para(self, text: str, size: float = BODY, bold: bool = False, color=INK, indent: float = 0,
             lh: Optional[float] = None, after: float = 0, width: Optional[float] = None, x: Optional[float] = None):
        """A paragraph set to the reading measure (``width`` overrides it)."""
        pdf = self.pdf
        raw = str(text or "").strip()
        if not raw:
            return
        text, rtl, md = self._prep(raw)
        lh = lh or size * 0.53
        if rtl:
            pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="arab", language="ara")
        pdf.set_font("Arabic" if rtl else "Sans", "B" if bold else "", size + (1 if rtl else 0))
        pdf.set_text_color(*color)
        left = (x if x is not None else pdf.l_margin) + indent
        w = width if width is not None else min(pdf.epw, MEASURE) - indent
        pdf.set_x(left)
        pdf.multi_cell(w, lh + (1.5 if rtl else 0), text, align="R" if rtl else "L",
                       new_x=self.X.LMARGIN, new_y=self.Y.NEXT, markdown=md)
        if rtl:
            pdf.set_text_shaping(use_shaping_engine=True, features=_TNUM, direction="ltr")
        if after:
            pdf.ln(after)

    def item(self, mark: str, text: str, size: float = BODY, indent: float = 0, after: float = 1.6, color=INK):
        """A list item with a hanging indent: wrapped lines align with the text, not the marker."""
        pdf = self.pdf
        raw = str(text or "").strip()
        if not raw:
            return
        if has_rtl(raw):
            self.para(f"{raw} {mark}", size=size, indent=indent, after=after, color=color)
            return
        hang = 6 if len(mark) > 1 else 4.5
        y = pdf.get_y()
        if pdf.will_page_break(size * 0.53 + 1):
            pdf.add_page()
            y = pdf.get_y()
        pdf.set_font("Sans", "B" if mark[0].isdigit() else "", size)
        pdf.set_text_color(*(self.accent if mark[0].isdigit() else FAINT))
        pdf.set_xy(pdf.l_margin + indent, y)
        pdf.cell(hang, size * 0.53, mark)
        pdf.set_xy(pdf.l_margin + indent + hang, y)
        self.para(raw, size=size, color=color, width=min(pdf.epw, MEASURE) - indent - hang,
                  x=pdf.l_margin + indent + hang, after=after)

    def list_height(self, items: list[str], size: float = BODY, numbered: bool = False, after: float = 1.6) -> float:
        """Height a list will take (dry run), so a short list can be kept on one page."""
        pdf = self.pdf
        hang = 6 if numbered else 4.5
        width = min(pdf.epw, MEASURE) - hang
        pdf.set_font("Sans", "", size)
        total = 0.0
        for it in items:
            text = _plain(str(it))
            total += pdf.multi_cell(width, size * 0.53, text, dry_run=True, output="HEIGHT") + after
        return total

    def keep_list(self, title: str, items: list[str], numbered: bool = False, after: float = 1.6, above: float = 5):
        """A heading and its list, moved together to the next page when they do not fit."""
        need = 12 + self.list_height(items, numbered=numbered, after=after)
        if need < 120 and self.pdf.will_page_break(need):
            self.pdf.add_page()
        self.heading(title, above=above, room=0)
        self.bullets(items, numbered=numbered, after=after)

    def bullets(self, items: list[str], size: float = BODY, numbered: bool = False, indent: float = 0,
                after: float = 1.6):
        for n, it in enumerate(items, 1):
            self.item(f"{n}." if numbered else "•", str(it), size=size, indent=indent, after=after)

    def section(self, text: str, new_page: bool = False, room: float = 45, level: int = 0):
        """A bookmarked section title with a thin rule; a new page when asked or when space is short."""
        pdf = self.pdf
        if level == 0:
            pdf.cur_section = text            # set before the page turn, so the new page's header names it
        if new_page or pdf.will_page_break(room):
            pdf.add_page()
        else:
            pdf.ln(6)
        pdf.start_section(_plain(text), level=level)
        self.pages.setdefault(text, pdf.page_no())
        self.para(text, size=SECTION, bold=True, lh=7)
        pdf.ln(1.5)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.25)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    def heading(self, text: str, room: float = 26, above: float = 5):
        """A sub-heading: body size, bold, with clear space above."""
        if room and self.pdf.will_page_break(room):
            self.pdf.add_page()
        else:
            self.pdf.ln(above)
        self.para(text, size=BODY, bold=True, lh=5.4)
        self.pdf.ln(1.6)

    def caps(self, text: str, color=MUTED, indent: float = 0, x: Optional[float] = None, width: Optional[float] = None):
        self.para(text.upper(), size=SMALL, bold=True, color=color, lh=4.4, indent=indent, x=x, width=width)
        self.pdf.ln(0.8)

    def markdown(self, md: str, size: float = BODY):
        """Readable rendering of a Markdown answer: headings, bullets, tables, code, paragraphs."""
        buf: list[str] = []
        code = False

        def flush():
            if buf:
                self.para(" ".join(buf), size=size)
                self.pdf.ln(2.2)
                buf.clear()

        for raw in (md or "").splitlines():
            line = raw.rstrip()
            if line.strip().startswith("```"):
                flush()
                code = not code
                continue
            if code:
                self.para(line or " ", size=SMALL, color=MUTED, indent=4, lh=4.2)
                continue
            s = line.strip()
            if not s:
                flush()
                continue
            m = re.match(r"^(#{1,6})\s+(.*)", s)
            if m:
                flush()
                self.heading(m.group(2), above=3)
                continue
            if re.match(r"^\|?\s*:?-{3,}", s):
                continue
            if s.startswith("|"):
                flush()
                self.para("  |  ".join(c.strip() for c in s.strip("|").split("|")), size=SMALL, indent=2)
                continue
            m = re.match(r"^([-*+]|\d+[.)])\s+(.*)", s)
            if m:
                flush()
                mark = "•" if m.group(1) in "-*+" else m.group(1)
                self.item(mark, m.group(2), size=size, indent=(raw.find(m.group(1)) // 2) * 4, after=1.4)
                continue
            buf.append(s)
        flush()

    def table(self, header: list[str], rows: list[list[str]], widths: tuple, align: Optional[tuple] = None,
              size: float = BODY, logos: Optional[list] = None, md: bool = False, shade: Optional[set] = None,
              width: Optional[float] = None):
        """Unfilled table with hairline rules; numbers right-aligned; never a lone header at a page end.
        ``logos``: one SVG (or None) per row, drawn in the first column (``header`` names it, usually "").
        ``md``: **bold** inside cells. ``shade``: (row, column) cells tinted, columns counted after the logo."""
        import io
        from fpdf.fonts import FontFace
        pdf = self.pdf
        if rows and pdf.will_page_break(min(7.5 * (len(rows) + 1) + 4, 110)):
            pdf.add_page()
        pdf.set_font("Sans", "", size)
        pdf.set_text_color(*INK)
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        tint = FontFace(fill_color=self.t["tint"])
        with pdf.table(col_widths=widths, text_align=align or "LEFT", line_height=5.6 if size >= BODY else 4.3,
                       first_row_as_headings=True,
                       borders_layout="HORIZONTAL_LINES", cell_fill_mode="NONE", repeat_headings=1, padding=(1.4, 1.5),
                       headings_style=FontFace(emphasis="BOLD", color=MUTED, size_pt=SMALL), markdown=md,
                       width=width or pdf.epw, align="LEFT") as t:
            t.row([_plain(h) for h in header])
            for i, r in enumerate(rows):
                row = t.row()
                if logos is not None:
                    if logos[i]:
                        row.cell(img=io.BytesIO(logos[i]), img_fill_width=False)
                    else:
                        row.cell("")
                for j, c in enumerate(r):
                    text = _md_safe(_plain(str(c), keep_bold=True)) if md else _plain(str(c))
                    row.cell(text, style=tint if shade and (i, j) in shade else None)
        pdf.ln(3)

    def callout(self, label: str, text: str, note: str = "", size: float = BODY):
        """A slim tinted line with the accent bar: 'LABEL  text  note' (the decision on page 2, a chairman's call)."""
        pdf = self.pdf
        pad, bar = 3.0, 1.2
        inner = pdf.epw - 2 * pad - bar
        x = pdf.l_margin + bar + pad
        body = f"**{label}**   {text}" + (f"   {note}" if note else "")

        def emit(p, ww, y):
            p.set_y(y + pad)
            ww.para(body, size=size, x=x, width=inner)
            return p.get_y() + pad - 1 - y

        y0 = pdf.get_y()
        with pdf.offset_rendering() as dummy:
            h = emit(dummy, _W(dummy, self.X, self.Y, self.t, self.ev_page), y0)
        if pdf.will_page_break(h + 2):
            pdf.add_page()
            y0 = pdf.get_y()
        pdf.set_fill_color(*self.t["tint"])
        pdf.rect(pdf.l_margin, y0, pdf.epw, h, "F")
        pdf.set_fill_color(*self.accent)
        pdf.rect(pdf.l_margin, y0, bar, h, "F")
        emit(pdf, self, y0)
        pdf.set_y(y0 + h + 3)

    def logo_line(self, logo: Optional[bytes], text: str, size: float = SMALL, color=MUTED):
        """A line led by a small maker logo (the chairman under the committee table)."""
        import io
        pdf = self.pdf
        if not logo:
            self.para(text, size=size, color=color)
            return
        y = pdf.get_y()
        pdf.image(io.BytesIO(logo), x=pdf.l_margin, y=y + 0.5, w=3.2, h=3.2)
        self.para(text, size=size, color=color, x=pdf.l_margin + 4.6, width=min(pdf.epw, MEASURE) - 4.6)


# ── data helpers ─────────────────────────────────────────────────────────────

def _slot_logo(run: dict, slot: dict, name: str, rgb=INK) -> Optional[bytes]:
    """The logo of the model that answered: the seat's own, or the one Hermes answered with instead."""
    sw = (run.get("switched") or {}).get(name) or {}
    model = sw.get("model") or slot.get("model")
    return logo_svg(maker_of(model, sw.get("provider") or slot.get("provider")), rgb)


def _sv(run: dict) -> bool:
    from .engine import self_votes_left_out
    return self_votes_left_out(run)


def _answered_label(run: dict) -> str:
    """'seats answered', plus how many models wrote them when that is fewer than the answers."""
    from .config import independence
    ind = independence(run)
    return f"seats answered · {ind['models']} model{'s' if ind['models'] != 1 else ''}" if ind["line"] else "seats answered"


def _exec_figures(run: dict) -> list[tuple[str, str]]:
    """Page 1's figures: who answered, dissent, the debate, time and cost."""
    seats = run.get("seats") or []
    ok = sum(1 for s in seats if (run["answers"].get(s["label"]) or {}).get("status") == "ok")
    out = [(f"{ok} of {len(seats)}", _answered_label(run))]
    chair = run.get("chair") or {}
    if chair.get("status") == "ok":
        k = len(chair.get("dissent") or [])
        out.append((f"{k} dissent{'s' if k != 1 else ''}", "kept on record") if k else ("No dissent", "on record"))
    d = run.get("deliberation") or {}
    if d.get("rounds"):
        still = len(d.get("open") or [])
        out.append((f"{still} open", f"point{'s' if still != 1 else ''} still disputed") if still
                   else ("All settled", "after the debate"))
    secs = run_seconds(run)
    if secs:
        out.append((duration(secs), "run time"))
    out.append(_cost_value(run))
    return out


def _has_evidence(run: dict) -> bool:
    from .followup import own_brief
    return bool(run.get("files") or run.get("evidence") or own_brief(run) or (run.get("web") or {}).get("items"))


def _chapters(run: dict, ok_chair: bool) -> dict:
    """Section titles in report order. Appendices are lettered by what this run has, so there is never a gap."""
    debate = bool((run.get("deliberation") or {}).get("rounds"))
    letters = iter("ABCDEF")
    return {"why": WHY if ok_chair else WHAT, "dis": DISAGREED if debate else None, "com": "The committee",
            "A": f"Appendix {next(letters)} · Each seat's answer",
            "B": f"Appendix {next(letters)} · The debate in full" if debate else None,
            "C": f"Appendix {next(letters)} · Evidence" if _has_evidence(run) else None,
            "D": f"Appendix {next(letters)} · How it ran and what it cost"}


def _default_toc(run: dict, ch: dict) -> dict:
    """First pass: every chapter at a placeholder page, so page 1 is laid out with the right number of lines."""
    out: dict = {}
    for key in ("why", "dis", "com", "A", "B", "C", "D"):
        if ch.get(key):
            out[ch[key]] = 88
        if key == "A":
            out.update({f"seat:{x['name']}": 88 for x in _seats(run)})
    return out


def _two_lists(w: _W, a: tuple, b: tuple):
    """Two short headed lists side by side (stacked when one is missing or either is long)."""
    pdf = w.pdf
    (ta, ia), (tb, ib) = a, b
    gap = 8
    cw = (pdf.epw - gap) / 2

    def height(items):
        pdf.set_font("Sans", "", BODY)
        return sum(pdf.multi_cell(cw - 4.5, BODY * 0.53, _plain(str(x)), dry_run=True, output="HEIGHT") + 0.9
                   for x in items)

    if not ia or not ib or max(height(ia), height(ib)) > 70:
        for t, items in (a, b):
            if items:
                w.keep_list(t, items, after=0.9, above=3)
        return
    need = 9 + max(height(ia), height(ib))
    if pdf.will_page_break(need + 4):
        pdf.add_page()
    else:
        pdf.ln(4)
    y, ends = pdf.get_y(), []
    for k, (t, items) in enumerate((a, b)):
        x = pdf.l_margin + k * (cw + gap)
        pdf.set_y(y)
        w.para(t, bold=True, lh=5.4, x=x, width=cw)
        pdf.ln(1.6)
        for it in items:
            iy = pdf.get_y()
            pdf.set_font("Sans", "", BODY)
            pdf.set_text_color(*FAINT)
            pdf.set_xy(x, iy)
            pdf.cell(4.5, BODY * 0.53, "•")
            pdf.set_y(iy)
            w.para(str(it), x=x + 4.5, width=cw - 4.5, after=0.9)
        ends.append(pdf.get_y())
    pdf.set_y(max(ends))


def _drop_verdict(md: str) -> str:
    """The chairman's reasoning without its opening 'Verdict' section: the decision is already shown above it."""
    m = re.match(r"\s*#{1,6}\s*(?:verdict|decision|recommendation)\b[^\n]*\n.*?(?=^#{1,6}\s|\Z)", md or "",
                 re.I | re.S | re.M)
    rest = (md or "")[m.end():] if m else (md or "")
    return rest if rest.strip() else (md or "")


def _lead(text: str, at_least: int = 60, limit: int = 180) -> str:
    """A debate reply in one line: its first sentences, enough to carry the reason ('Hold.' alone does not)."""
    out = ""
    for part in arg_parts(text):
        out = f"{out} {part}".strip()
        if len(out) >= at_least:
            break
    return _clip_txt(out, limit)


def _seats(run: dict) -> list[dict]:
    return sorted(run.get("seats") or [], key=lambda s: s["label"])


def _names(run: dict) -> dict[str, str]:
    return {s["label"]: s["name"] for s in run.get("seats") or []}


def _earlier(run: dict, out_path: Path) -> Optional[dict]:
    if not run.get("follow_of"):
        return None
    if run.get("follow_prev"):
        return run["follow_prev"]
    p = out_path.parent.parent / str(run["follow_of"]) / "run.json"
    try:
        from .followup import prev_summary
        return prev_summary(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"id": run["follow_of"]}


def _subscription_seats(run: dict) -> list[str]:
    slots = [*(run.get("seats") or []), {**(run.get("chairman") or {}), "name": "Chairman"}]
    return [s.get("name") or "Chairman" for s in slots if (s.get("price") or {}).get("billing") == "subscription"]


def _cost_value(run: dict) -> tuple[str, str]:
    from .pricing import fmt
    u = usage_totals(run)
    if u.get("est_charged") is None:
        return "–", "cost not estimated"
    subs = _subscription_seats(run)
    if u["est_charged"] <= 0 and subs and len(subs) >= len(run.get("seats") or []) + 1 and not u.get("est_unknown_calls"):
        return "Subscription", "no pay-per-use cost"
    return fmt(u["est_charged"]) + (" + unpriced" if u.get("est_unknown_calls") else ""), "pay-per-use cost"


def _figures(run: dict) -> list[tuple[str, str]]:
    seats = run.get("seats") or []
    ok = sum(1 for s in seats if (run["answers"].get(s["label"]) or {}).get("status") == "ok")
    out = [(f"{ok} of {len(seats)}", _answered_label(run))]
    secs = run_seconds(run)
    if secs:
        out.append((duration(secs), "run time"))
    out.append(_cost_value(run))
    idx = run.get("evidence_index") or {}
    if run.get("files"):
        pics = len(idx.get("images") or [])
        n = len(run["files"])
        out.append((f"{n} file{'s' if n != 1 else ''}", f"evidence, {pics} photo{'s' if pics != 1 else ''}" if pics
                    else "evidence"))
    rk = run.get("ranking") or []
    from .engine import clear_first
    if rk and clear_first(rk):
        top = _names(run).get(rk[0]["label"], rk[0]["label"])
        out.append((top, f"ranked first, {rk[0]['first_votes']} of {rk[0].get('votes') or len(rk)}"))
    return out


def _fit(pdf, text: str, width: float) -> str:
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _strip(w: _W, figures: list[tuple[str, str]]):
    """Key figures: value over label, separated by hairlines (no boxes)."""
    pdf = w.pdf
    k = max(1, len(figures))
    cw = pdf.epw / k
    h = 14
    if pdf.will_page_break(h + 6):
        pdf.add_page()
    y = pdf.get_y()
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.line(pdf.l_margin, y + h + 2, pdf.w - pdf.r_margin, y + h + 2)
    for i, (value, label) in enumerate(figures):
        x = pdf.l_margin + i * cw
        if i:
            pdf.line(x, y + 2.5, x, y + h - 0.5)
        pad = 0 if i == 0 else 3.5
        pdf.set_xy(x + pad, y + 2.5)
        pdf.set_font("Sans", "B", SECTION)
        pdf.set_text_color(*INK)
        pdf.cell(cw - pad - 2, 6.5, _fit(pdf, _plain(value), cw - pad - 2))
        pdf.set_xy(x + pad, y + 9.5)
        pdf.set_font("Sans", "", SMALL)
        pdf.set_text_color(*MUTED)
        pdf.cell(cw - pad - 2, 4, _fit(pdf, label, cw - pad - 2))
    pdf.set_xy(pdf.l_margin, y + h + 6)


def _vote_chart(w: _W, run: dict):
    from .insights import rank_score
    pdf = w.pdf
    names = _names(run)
    rk = run.get("ranking") or []
    n = len(rk)
    label_w, info_w = 38, 46
    bar_w = pdf.epw - label_w - info_w - 4
    if pdf.will_page_break(8 * n + 6):
        pdf.add_page()
    for i, r in enumerate(rk):
        y = pdf.get_y()
        pdf.set_xy(pdf.l_margin, y)
        pdf.set_font("Sans", "B" if i == 0 else "", BODY)
        pdf.set_text_color(*INK)
        pdf.cell(label_w, 6, _fit(pdf, _plain(names.get(r["label"], r["label"])), label_w - 2))
        score = rank_score(r["avg_rank"], r.get("of") or n)
        x0 = pdf.l_margin + label_w
        pdf.set_fill_color(*TINT)
        pdf.rect(x0, y + 1.6, bar_w, 3, "F")
        pdf.set_fill_color(*(w.accent if i == 0 else w.t["soft"]))
        pdf.rect(x0, y + 1.6, max(1.2, bar_w * score), 3, "F")
        pdf.set_xy(x0 + bar_w + 4, y)
        pdf.set_font("Sans", "", SMALL)
        pdf.set_text_color(*MUTED)
        pdf.cell(info_w, 6, f"{ordinal(i + 1)} · avg place {r['avg_rank']:.2f} · {r['first_votes']} first", align="R")
        pdf.set_xy(pdf.l_margin, y + 8)
    pdf.ln(1)


def _ballots(run: dict) -> list[str]:
    names = _names(run)
    out = []
    for s in _seats(run):
        r = (run.get("reviews") or {}).get(s["label"]) or {}
        if r.get("status") == "ok" and r.get("ranking"):
            out.append(f"{s['name']} ranked " + ", then ".join(names.get(x, x) for x in r["ranking"]))
    return out


def _kb(f: dict) -> str:
    return f"{max(1, round((f.get('size') or 0) / 1024)):,} KB" if f.get("size") else ""


def _evidence_rows(run: dict) -> list[list[str]]:
    idx = run.get("evidence_index") or {}
    files = {f.get("name"): f for f in run.get("files") or []}
    imgs = idx.get("images") or []
    rows, seen = [], set()
    for sec in idx.get("sections") or []:
        f = files.get(sec.get("source")) or {}
        first = sec.get("source") not in seen
        seen.add(sec.get("source"))
        rows.append([sec["id"], sec.get("source") or "", sec.get("locator") or "–",
                     (f.get("kind") or "") if first else "", _kb(f) if first else "",
                     f"{f.get('tokens') or 0:,}" if first and f else "", "; ".join(f.get("notes") or []) if first else ""])
    for img in imgs:
        f = files.get(img.get("source")) or {}
        seen.add(img.get("source"))
        rows.append([img["id"], img.get("source") or "", img.get("locator") or "–", "photo", _kb(f), "",
                     "description below"])
    for name, f in files.items():
        if name not in seen:
            rows.append(["–", name, "–", f.get("kind") or "", _kb(f), "", "; ".join(f.get("notes") or []) or "no text found"])
    return rows


def _method_lines(run: dict) -> list[str]:
    from .pricing import fmt, limit
    n = len(run.get("seats") or [])
    out = [f"Committee type: {run.get('template_title') or run.get('template')}"
           + (" (with a custom chairman brief)" if run.get("custom_brief") else "") + ". "
           + ("Each seat had to end with a clear recommendation. " if run.get("decision_focus") else "")
           + f"The chairman was asked to: {run.get('chairman_guidance') or '—'}",
           "Each seat answered on its own, without seeing the others' answers."]
    web = run.get("web") or {}
    if run.get("research"):
        out.insert(1, f"Research first: before the seats answered, one web search added {len(web.get('items') or [])} "
                      "passages with their sources to the evidence, the same for every seat."
                   if web.get("items") else f"Research first was on, but no web sources were added: {web.get('note') or 'not run'}")
    from .engine import self_votes_left_out
    out.append("Then each seat ranked all the answers, its own included, without knowing who wrote which (the blind vote)"
               + ("; its vote for its own answer is left out of the places." if self_votes_left_out(run) else ".")
               if run.get("review") else "There was no blind vote.")
    d = run.get("deliberation") or {}
    mode = {"material": "when they disagreed on the action or on a claim", "action": "only when they disagreed on the action",
            "always": "always"}.get(run.get("deliberate") or "never")
    if mode:
        line = f"Deliberation was set to run {mode}"
        if d.get("rounds"):
            line += (f": the chairman listed {len(d['disputes'])} disputed point(s) and the seats answered them in "
                     f"{len(d['rounds'])} round(s), each holding or changing its position with a reason.")
        else:
            line += f"; it did not run ({delib_line(run).removeprefix('No deliberation: ').rstrip('.') or 'skipped'})."
        out.append(line)
    out.append(f"The chairman ({short_model(run.get('chairman') or {})}) read the answers"
               f"{' and the votes' if run.get('review') else ''}{' and the deliberation' if d.get('rounds') else ''} "
               "and wrote the verdict.")
    st = run.get("stage_timeout")
    out.append(f"A verdict needed answers from at least {run.get('quorum')} of {n} seats"
               + (f"; each stage could take up to {st / 60:g} min." if st else "."))
    secs = (run.get("limits") or {}).get("max_secs")
    line = f"Time limit: {secs / 60:g} min" if secs else "Time limit: none"
    from .insights import limit_changes
    if limit_changes(run, "time"):
        line += f" ({limit_changes(run, 'time')})"
    took = run_seconds(run)
    if took:
        line += f"; the run took {duration(took)}"
    if (run.get("skipped") or {}).get("review") == "time":
        line += " (the blind vote was skipped to stay within it)"
    out.append(line + ".")
    idx = run.get("evidence_index") or {}
    if run.get("files"):
        out.append(f"Evidence: {len(run['files'])} files, about {idx.get('tokens', 0):,} tokens sent with each call.")
    imgs = idx.get("images") or []
    if imgs:
        seeing = [s["name"] for s in _seats(run) if s.get("sees_images")]
        by = next((i.get("described_by") for i in imgs if i.get("described_by")), None)
        out.append(f"Photos: {len(imgs)}, sent to {', '.join(seeing) or 'no seat'}"
                   + (f"; described in writing by {by.split(' / ')[-1]} for the blind vote and the chairman." if by
                      else "; no description could be written."))
    u = usage_totals(run)
    if u.get("est_charged") is not None:
        subs = _subscription_seats(run)
        line = f"Estimated cost: {fmt(u['est_charged'])} for pay-per-use seats"
        if subs:
            line += f"; on a subscription and not counted: {', '.join(subs)}"
        out.append(line + ". Token counts × list price, not a provider bill.")
        lim = limit(run)
        b = f"Budget: {fmt(lim)}" if lim else "Budget: none"
        if limit_changes(run, "usd"):
            b += f" ({limit_changes(run, 'usd')})"
        if (run.get("skipped") or {}).get("review") == "budget":
            b += " (the blind vote was skipped to stay within it)"
        if run.get("budget_tokens"):
            b += f"; answers were limited to {run['budget_tokens']:,} output tokens to fit"
        out.append(b + ".")
        if u.get("est_unknown_calls"):
            out.append(f"{u['est_unknown_calls']} call(s) on models with no known price are not counted.")
        if u.get("abandoned"):
            out.append(f"{u['abandoned']} call(s) cut off by the time limit are counted at their maximum cost.")
    line = f"Model calls: {u['calls']}"
    if u.get("input_tokens"):
        line += f"; tokens in {u['input_tokens']:,}, out {(u.get('output_tokens') or 0):,}"
    if u.get("cost_usd") is not None:
        line += f"; the providers reported ${u['cost_usd']:.4f}"
    out.append(line + ".")
    from .report_md import retry_lines
    return out + retry_lines(run) + [f"Warning: {x}" for x in run.get("warnings") or []]


def _stage_rows(run: dict) -> list[list[str]]:
    from .pricing import fmt
    titles = {"describe": "Photo descriptions", "stage1": "Answers", "review": "Blind vote", "deliberate": "Deliberation",
              "chair": "Chairman"}
    rows = []
    for st, title in titles.items():
        calls = [c for c in run.get("usage") or [] if c.get("stage") == st]
        if calls:
            rows.append([title, str(len(calls)), f"{sum(c.get('input_tokens') or 0 for c in calls):,}",
                         f"{sum(c.get('output_tokens') or 0 for c in calls):,}",
                         fmt(sum(c.get("cost_est") or 0 for c in calls))])
    return rows


def _debate_full(w: _W, run: dict, d: dict, chair: dict):
    """Per disputed point: each seat's side before → after with its whole argument, then the chairman's call."""
    pdf = w.pdf
    rounds = len(d["rounds"])
    w.para(f"Every argument as the seats gave it, over {rounds} round{'s' if rounds > 1 else ''}. "
           "The summary is in Where they disagreed.", size=SMALL, color=MUTED, after=3)
    decided = delib_decided(chair, d["disputes"])
    kinds = {"action": "about the action", "claim": "about a claim the decision rests on", "emphasis": "emphasis"}
    for p in d["disputes"]:
        if pdf.will_page_break(48):
            pdf.add_page()
        still = " · still disputed" if p["id"] in (d.get("open") or []) else ""
        w.para(f"**{p['id']} · {p['point']}**", after=0.6)
        w.para(f"{kinds.get(p['level'], p['level'])}{still}", size=SMALL, color=MUTED, after=1.6)
        for pos in p["positions"]:
            r = delib_last(d, pos["seat"], p["id"])
            moved = f" → {r['side']}" if r and r.get("side") and r["side"] != pos["side"] else ""
            word = stance_word(r, pos["side"])
            lead, *rest = arg_parts((r or {}).get("argument") or pos["view"]) or [""]
            if pdf.will_page_break(26):
                pdf.add_page()
            w.item("•", f"**{pos['seat']}** · {pos['side']}{moved} · {word}"
                   + unbacked_note(move_of(d, pos["seat"], p["id"])), after=0.5)
            if lead:
                w.para(lead, indent=4.5, after=0.7)
            for t in rest:
                w.item("–", t, size=SMALL, indent=4.5, after=0.5, color=MUTED)
            if extra_cites(r):
                w.para("Evidence: " + ", ".join(extra_cites(r)), size=SMALL, color=MUTED, indent=4.5, after=0.5)
            pdf.ln(1.4)
        x = decided.get(p["id"])
        if x:
            pdf.ln(0.6)
            w.para(f"**Chairman's call:** {x['decided']}" + (f" Decided by: {x['by_argument']}" if x.get("by_argument") else ""),
                   after=3)
        else:
            pdf.ln(2.4)


def _disagreements(w: _W, run: dict, d: dict, chair: dict, full: Optional[str], toc: dict):
    """One card per disputed point: the chairman's call on top, then each seat's side before → after, whether it
    moved, and its reason in one line. The whole arguments are in the appendix."""
    pdf = w.pdf
    w.para("**" + " · ".join(debate_tally(d)) + "**", after=1.2)
    rounds = len(d["rounds"])
    w.para((d.get("reason") or "") + f" The seats answered the points below in {rounds} round{'s' if rounds > 1 else ''}; "
           "each had to hold or change its position and give its reason.", size=SMALL, color=MUTED, after=2)
    decided = delib_decided(chair, d["disputes"])
    kinds = {"action": "about the action", "claim": "about a claim the decision rests on", "emphasis": "emphasis"}
    for p in d["disputes"]:
        if pdf.will_page_break(58):
            pdf.add_page()
        else:
            pdf.ln(3)
        still = p["id"] in (d.get("open") or [])
        w.para(f"**{p['id']} · {p['point']}**", after=0.4)
        w.para(f"{kinds.get(p['level'], p['level'])} · {'still disputed' if still else 'settled'}", size=SMALL,
               color=MUTED, after=1.8)
        x = decided.get(p["id"])
        if x:
            w.callout("Chairman's call:", f"**{x['decided']}**",
                      f"Because: {x['by_argument']}" if x.get("by_argument") else "")
        rows = []
        for pos in p["positions"]:
            r = delib_last(d, pos["seat"], p["id"])
            after = (r or {}).get("side")
            side = f"{pos['side']} → **{after}**" if after and after != pos["side"] else pos["side"]
            if (move_of(d, pos["seat"], p["id"]) or {}).get("unbacked"):
                side += " (no new evidence)"
            rows.append([pos["seat"], side, stance_word(r, pos["side"]),
                         _lead((r or {}).get("argument") or pos["view"], 50, 125)])
        w.table(["Seat", "Side: before → after", "Moved", "Its reason, in one line"], rows, (29, 38, 21, 74),
                size=SMALL, md=True)
    if full:
        w.para(f"Each seat's whole argument: {full}, page {toc.get(full, '')}.", size=SMALL, color=MUTED)


def _matrix(w: _W, run: dict):
    """Who ranked whom: one row per reviewer, the place it gave each answer; its own answer shaded."""
    pdf = w.pdf
    names = _names(run)
    order = [r["label"] for r in run.get("ranking") or []]
    revs = [(x, (run.get("reviews") or {}).get(x["label"]) or {}) for x in _seats(run)]
    revs = [(x, r) for x, r in revs if r.get("status") == "ok" and r.get("ranking")]
    if not order or not revs:
        return
    rows, shade = [], set()
    for i, (x, r) in enumerate(revs):
        row = [x["name"]]
        for j, lab in enumerate(order, 1):
            pl = r["ranking"].index(lab) + 1 if lab in r["ranking"] else None
            row.append("–" if pl is None else f"**{pl}**" if pl == 1 else str(pl))
            if lab == x["label"]:
                shade.add((i, j))
        rows.append(row)
    k = len(order)
    first, rest = 38, min(22.0, (pdf.epw - 38) / k)
    w.table(["Ranked by"] + [names.get(lab, lab) for lab in order], rows, (first,) + (rest,) * k,
            align=("LEFT",) + ("CENTER",) * k, size=SMALL, md=True, shade=shade, width=first + rest * k)
    w.para("Each row is one reviewer's ballot: the place it gave each answer (1 = best). Shaded: its own answer, "
           + ("not counted in the places, " if _sv(run) else "")
           + "which it ranked without knowing it was its own.", size=SMALL, color=MUTED)


# ── page 1: executive summary ────────────────────────────────────────────────

WHY, WHAT, DISAGREED = "Why: the chairman's reasoning", "What happened", "Where they disagreed"
# (bottom line, reason, action, condition, conditions shown): shorten text first, drop conditions last
_EXEC_LIMITS = ((480, 220, 200, 200, 2), (420, 190, 170, 170, 2), (420, 190, 170, 170, 1), (360, 160, 140, 150, 1),
                (300, 130, 120, 130, 1), (260, 110, 100, 0, 0))
EXEC_LAST = len(_EXEC_LIMITS) - 1


def _clip_txt(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _defer_block(w: _W, chair: dict, ok_chair: bool):
    """Under the decision box of a deferred verdict: what to find out, and the decision either way."""
    lines = defer_lines(chair) if ok_chair else []
    if lines:
        w.pdf.ln(2)
        for x in lines:
            w.para(x, after=1.2)


def _exec_page(w: _W, run: dict, ok_chair: bool, toc: dict, level: int):
    """One page for a reader who has seen nothing else: decision, bottom line, why, next steps, what would change
    it, how the committee stood, and where to read more. Never longer than one page (render() shortens it)."""
    pdf = w.pdf
    es = exec_summary(run)
    lim_bottom, lim_reason, lim_act, lim_rev, n_rev = _EXEC_LIMITS[min(level, EXEC_LAST)]
    w.caps(f"{PRODUCT}  ·  Executive summary", color=w.accent, width=pdf.epw)
    pdf.ln(3)
    w.para(run.get("title") or "Committee report", size=TITLE, bold=True, lh=9.5, width=pdf.epw)
    pdf.ln(1.5)
    when = time.strftime("%d %b %Y", time.localtime(run["created"]))
    status = "Verdict delivered" if ok_chair else STATUS.get(run["status"], "No verdict")
    meta = [x for x in (when, status, "Follow-up" if run.get("follow_of") else "", f"run {run['id']}") if x]
    w.para("  ·  ".join(meta), size=SMALL, color=MUTED, width=pdf.epw)
    pdf.ln(4.5)
    _decision_box(w, run.get("chair") or {}, ok_chair, run, brief=True)
    _defer_block(w, run.get("chair") or {}, ok_chair)
    if es["ok"]:
        if es.get("change"):
            w.para(f"**Since the earlier run:** {es['change']}", color=MUTED)
            pdf.ln(2)
        w.para(_clip_txt(es["bottom_line"], lim_bottom), after=1)
        if es["reasons"]:
            w.heading("Why", above=4, room=0)
            w.bullets([_clip_txt(r, lim_reason) for r in es["reasons"]], after=1.2)
        if es["actions"]:
            w.heading("Do next", above=4, room=0)
            w.bullets([_clip_txt(a, lim_act) for a in es["actions"]], numbered=True, after=1.2)
            if es["more_actions"]:
                w.para(f"{es['more_actions']} more under Next actions, page {toc.get(WHY, 2)}.", size=SMALL, color=MUTED)
        if es["reverse"] and n_rev:
            w.heading("What would change this", above=4, room=0)
            w.bullets([_clip_txt(r, lim_rev) for r in es["reverse"][:n_rev]], after=1.2)
    # The figures and the contents never start a new page: when they do not fit, render() shortens the text
    # above first; at the last level the seat lines fold into one, then the figures, then the contents go.
    avail = pdf.h - 24 - pdf.get_y()
    tiles_h = 24
    full, short = _contents_height(w, run, toc, False), _contents_height(w, run, toc, True)
    plan = next(((t, c) for t, c, need in ((True, False, tiles_h + full), (True, True, tiles_h + short),
                                            (False, True, short), (False, None, 0)) if need <= avail), (False, None))
    if plan[0]:
        pdf.ln(4)
        _strip(w, _exec_figures(run))
    if plan[1] is not None:
        _contents(w, run, toc, collapse=plan[1])
    squeezed = plan != (True, False)
    pdf.exec_fit = (pdf.page_no() == 1 and pdf.get_y() <= pdf.h - 24        # about 8 mm of air above the footer
                    and not (squeezed and level < EXEC_LAST))
    if es.get("assembled"):
        pass                                   # older run: the page is built from the verdict's own fields


def _contents_rows(toc: dict, collapse: bool) -> list:
    rows: list[tuple[str, int, int]] = []                     # (title, page, level)
    seats = [(k[5:], pg) for k, pg in toc.items() if k.startswith("seat:")]
    for t, pg in toc.items():
        if t.startswith("seat:"):
            continue
        rows.append((t, pg, 0))
        if t.startswith("Appendix A") and seats:
            if len(seats) <= 5 and not collapse:
                rows += [(n, p, 1) for n, p in seats]
            else:
                rows.append(("  ·  ".join(f"{n} {p}" for n, p in seats), 0, 2))
    return rows


def _contents_split(rows: list) -> int:
    # the right column starts at a main line: the appendices when that keeps the columns near even, else the
    # main line that makes the taller column shortest
    starts = [i for i, r in enumerate(rows) if i and not r[2]] or [len(rows)]
    app = next((i for i in starts if rows[i][0].startswith("Appendix")), None)
    best = min(max(i, len(rows) - i) for i in starts)
    return app if app is not None and max(app, len(rows) - app) <= best + 2 else \
        min(starts, key=lambda i: (max(i, len(rows) - i), i))


def _contents_height(w: _W, run: dict, toc: dict, collapse: bool) -> float:
    rows = _contents_rows(toc, collapse)
    if not rows:
        return 0.0
    k = _contents_split(rows)
    lines = lambda part: sum(9.0 if r[2] == 2 else 4.6 for r in part)
    return 5 + 5.2 + max(lines(rows[:k]), lines(rows[k:])) + 1


def _contents(w: _W, run: dict, toc: dict, collapse: bool = False):
    """Page 1's contents: one list flowing over two columns, dot leaders and page numbers, every line a link.
    Each seat gets its own indented line under Appendix A; a committee of more than five shares one line, so the
    block stays short. Page numbers are filled in on the second pass."""
    pdf = w.pdf
    rows = _contents_rows(toc, collapse)
    if not rows:
        return
    pdf.ln(5)
    w.caps("Contents", width=pdf.epw)
    gap, lh = 12, 4.6
    cw = (pdf.epw - gap) / 2
    split = _contents_split(rows)
    top = pdf.get_y()
    nw = 7

    def row(x: float, y: float, title: str, page: int, level: int) -> float:
        indent = 4 if level else 0
        pdf.set_font("Sans", "", SMALL)
        if level == 2:                                       # many seats on one line
            pdf.set_text_color(*MUTED)
            pdf.set_xy(x + indent, y)
            pdf.multi_cell(cw - indent, 4.2, _plain(title), new_x=w.X.LEFT, new_y=w.Y.NEXT)
            return pdf.get_y()
        link = pdf.add_link(page=page)
        tw = min(pdf.get_string_width(_plain(title)) + 0.5, cw - indent - nw - 4)
        pdf.set_text_color(*(MUTED if level else INK))
        pdf.set_xy(x + indent, y)
        pdf.cell(tw, lh, _fit(pdf, _plain(title), tw), link=link)
        room = cw - indent - tw - nw - 1
        dot = pdf.get_string_width(" .")
        if room > dot * 2:
            pdf.set_text_color(*RULE if level else FAINT)
            pdf.set_xy(x + cw - nw - 1 - dot * int(room / dot), y)
            pdf.cell(dot * int(room / dot), lh, " ." * int(room / dot), link=link)
        pdf.set_text_color(*(MUTED if level else INK))
        pdf.set_font("Sans", "" if level else "B", SMALL)
        pdf.set_xy(x + cw - nw, y)
        pdf.cell(nw, lh, str(page), align="R", link=link)
        return y + lh

    ends = []
    for col, part in enumerate((rows[:split], rows[split:])):
        y = top
        for t, pg, lvl in part:
            y = row(pdf.l_margin + col * (cw + gap), y, t, pg, lvl)
        ends.append(y)
    pdf.set_xy(pdf.l_margin, max(ends) + 1)


# ── page-1 blocks ────────────────────────────────────────────────────────────

def _decision_box(w: _W, chair: dict, ok_chair: bool, run: dict, brief: bool = False):
    """The only shaded block in the report: tint and accent bar drawn first, text on top."""
    pdf = w.pdf
    pad, bar = 4.2, 1.6
    inner = pdf.epw - 2 * pad - bar

    def draw(target_y: float, measure_only: bool):
        if measure_only:
            return
        pdf.set_y(target_y + pad)
        x = pdf.l_margin + bar + pad
        if ok_chair:
            w.caps("Not yet: one fact decides it" if chair.get("defer") else "Decision", color=w.accent, x=x, width=inner)
            w.para(chair.get("decision") or "—", size=SECTION, bold=True, lh=6.2, x=x, width=inner)
            pdf.ln(2)
            conf = (chair.get("confidence") or "not stated").capitalize()
            w.para(f"**Confidence: {conf}.**" + ("" if brief else f" {chair.get('confidence_note') or ''}"),
                   size=BODY, color=MUTED, x=x, width=inner)
        else:
            w.caps("No verdict", color=w.accent, x=x, width=inner)
            w.para(run.get("error") or "The chairman did not deliver a verdict; the seats' answers follow.",
                   size=SECTION, bold=True, x=x, width=inner)

    # Measure with a dry run on a throwaway copy of the state.
    y0 = pdf.get_y()
    with pdf.offset_rendering() as dummy:
        dummy.set_y(y0 + pad)
        _w2 = _W(dummy, w.X, w.Y, w.t, w.ev_page)
        x = dummy.l_margin + bar + pad
        if ok_chair:
            _w2.caps("Decision", x=x, width=inner)
            _w2.para(chair.get("decision") or "—", size=SECTION, bold=True, lh=6.2, x=x, width=inner)
            dummy.ln(2)
            conf = (chair.get("confidence") or "not stated").capitalize()
            _w2.para(f"**Confidence: {conf}.**" + ("" if brief else f" {chair.get('confidence_note') or ''}"),
                     size=BODY, x=x, width=inner)
        else:
            _w2.caps("No verdict", x=x, width=inner)
            _w2.para(run.get("error") or "The chairman did not deliver a verdict; the seats' answers follow.",
                     size=SECTION, bold=True, x=x, width=inner)
        h = dummy.get_y() - y0 + pad - 1
    if pdf.will_page_break(h):
        pdf.add_page()
        y0 = pdf.get_y()
    pdf.set_fill_color(*w.t["tint"])
    pdf.rect(pdf.l_margin, y0, pdf.epw, h, "F")
    pdf.set_fill_color(*w.accent)
    pdf.rect(pdf.l_margin, y0, bar, h, "F")
    draw(y0, False)
    pdf.set_y(y0 + h + 5)


def _changed(w: _W, prev: dict, chair: dict, ok_chair: bool):
    """Earlier → Now in two unshaded columns, the reason below."""
    pdf = w.pdf
    if pdf.will_page_break(40):
        pdf.add_page()
    w.caps("What changed since the earlier run")
    gap = 8
    col = (pdf.epw - gap) / 2
    y = pdf.get_y() + 1
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, y - 0.5, pdf.w - pdf.r_margin, y - 0.5)
    pwhen = time.strftime("%d %b %Y", time.localtime(prev["created"])) if prev.get("created") else ""
    cols = [("Earlier" + (f" · {pwhen}" if pwhen else "") + (f" · {prev['confidence']} confidence" if prev.get("confidence") else ""),
             prev.get("decision") or "No earlier verdict was recorded.", MUTED),
            ("Now" + (f" · {chair.get('confidence')} confidence" if ok_chair and chair.get("confidence") else ""),
             chair.get("decision") if ok_chair else "No verdict.", INK)]
    bottoms = []
    for i, (head, text, colr) in enumerate(cols):
        x = pdf.l_margin + i * (col + gap)
        pdf.set_y(y + 2)
        w.caps(head, color=w.accent if i else FAINT, x=x, width=col)
        w.para(text, size=BODY, color=colr, x=x, width=col)
        bottoms.append(pdf.get_y())
    pdf.set_draw_color(*RULE)
    pdf.line(pdf.l_margin + col + gap / 2, y + 2, pdf.l_margin + col + gap / 2, max(bottoms))
    pdf.set_y(max(bottoms) + 2)
    if chair.get("change_from_earlier"):
        w.para(f"**Why it changed:** {chair['change_from_earlier']}", size=BODY, after=1)
    ref = " · ".join(x for x in (f"“{prev['title']}”" if prev.get("title") else "", f"run {prev.get('id')}") if x)
    w.para(f"Earlier run: {ref}. Its verdict was given to the seats as background to test, not as settled.",
           size=SMALL, color=FAINT)
    pdf.ln(2)


def _seat_band(w: _W, s: dict, a: dict, place_txt: str, logo: Optional[bytes] = None):
    """A seat's header: a light band with the maker's logo and the name, then model, confidence and peer place."""
    import io
    pdf = w.pdf
    if pdf.will_page_break(60):
        pdf.add_page()
    y = pdf.get_y()
    h = 15
    pdf.set_fill_color(*TINT)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "F")
    pdf.set_fill_color(*w.accent)
    pdf.rect(pdf.l_margin, y, 1.2, h, "F")
    lx = 0
    if logo:
        pdf.image(io.BytesIO(logo), x=pdf.l_margin + 5, y=y + 2.9, w=4.6, h=4.6)
        lx = 6.6
    pdf.set_xy(pdf.l_margin + 5 + lx, y + 2.2)
    pdf.set_font("Sans", "B", SECTION)
    pdf.set_text_color(*INK)
    pdf.cell(pdf.epw - 8 - lx, 6, _fit(pdf, _plain(s["name"]), pdf.epw - 8 - lx))
    pdf.set_xy(pdf.l_margin + 5, y + 8.6)
    pdf.set_font("Sans", "", SMALL)
    pdf.set_text_color(*MUTED)
    bits = [model_label(s) + (s.get("_answered_by") or ""), f"confidence {conf_text(a.get('confidence'))}"] + ([place_txt] if place_txt else [])
    pdf.cell(pdf.epw - 8, 4.5, _fit(pdf, _plain("   ·   ".join(bits)), pdf.epw - 8))
    pdf.set_y(y + h + 4)


# ── the document ─────────────────────────────────────────────────────────────

def _build(run: dict[str, Any], out_path: Path, ev_page: Optional[int], theme: dict,
           toc: Optional[dict] = None, level: int = 0):
    from .followup import name_chair, own_brief
    from .insights import run_insights
    pdf, XPos, YPos = _new_pdf(run, theme)
    from .citations import known as _known
    w = _W(pdf, XPos, YPos, theme, ev_page, _known(run))
    chair = run.get("chair") or {}
    ok_chair = chair.get("status") == "ok"
    if ok_chair:
        chair = name_chair(chair, run.get("seats") or [])
    names = _names(run)
    rank = {r["label"]: r for r in run.get("ranking") or []}
    from .engine import places as _places
    place = _places(run.get("ranking") or [])
    n_ranked = len(place)

    ch = _chapters(run, ok_chair)
    toc = toc or _default_toc(run, ch)
    d = run.get("deliberation") or {}

    # ── page 1: executive summary ───────────────────────────────────────────
    pdf.add_page()
    pdf.start_section("Executive summary")
    _exec_page(w, run, ok_chair, toc, level)

    # ── why: the chairman's reasoning (or what happened) ─────────────────────
    w.section(ch["why"], new_page=True)
    prev = _earlier(run, out_path)
    q = ("**Follow-up question:** " if run.get("follow_of") else "**Question:** ") + run["question"]
    if ok_chair:
        conf = (chair.get("confidence") or "not stated").lower()
        w.callout("DECISION", f"**{chair.get('decision') or '—'}**",
                  f"Confidence {conf}" + (f": {chair['confidence_note']}" if chair.get("confidence_note") else "."))
        w.para(q, color=MUTED, after=1)
        if delib_line(run) and not ch["dis"]:          # why there was no debate (the next page covers one)
            w.para(delib_line(run), size=SMALL, color=MUTED, after=1)
        if prev:
            pdf.ln(2)
            _changed(w, prev, chair, ok_chair)
        reasoning = _drop_verdict(chair.get("final_answer") or "")
        if reasoning.strip():
            w.markdown(reasoning)
        acts = chair.get("next_actions") or []
        if acts:
            parts = [re.split(r"\s+·\s+", _plain(a)) for a in acts]
            if all(2 <= len(x) <= 3 for x in parts):
                w.heading("Next actions", room=40)
                w.table(["#", "Action", "Who", "When"],
                        [[str(i), x[0], x[1], x[2] if len(x) > 2 else ""] for i, x in enumerate(parts, 1)],
                        (8, 88, 38, 28), size=SMALL)
            else:
                w.keep_list("Next actions", acts, numbered=True, after=0.9, above=2)
        _two_lists(w, ("It would reverse if", chair.get("reverse_if") or []),
                   ("Where they agreed", chair.get("agreement") or []))
        if chair.get("open_questions"):
            w.keep_list("Still open", chair["open_questions"], after=0.9, above=3)
        if chair.get("dissent"):
            w.heading("Dissent kept on record")
            for x in chair["dissent"]:
                w.para(f"**{x.get('name') or names.get(x.get('label'), x.get('label'))}.** {x['view']}", after=2.6)
        drifted = [x for x in chair.get("role_fit") or [] if x.get("fit") == "drifted"]
        if drifted:
            w.heading("Drifted from role")
            w.bullets([f"{x['seat']}: {x['note']}" if x.get("note") else x["seat"] for x in drifted])
        solo = solo_lines(run)
        if solo:
            w.heading(solo[0])
            for i, x in enumerate(solo[1:]):
                w.para(x, color=MUTED if i == 0 else INK, after=2.2)
    else:
        _decision_box(w, chair, ok_chair, run)
        w.para(q, color=MUTED)
        if delib_line(run) and not ch["dis"]:
            pdf.ln(1.5)
            w.para(delib_line(run), size=SMALL, color=MUTED)
        if prev:
            pdf.ln(2)
            _changed(w, prev, chair, ok_chair)

    # ── where they disagreed ─────────────────────────────────────────────────
    if ch["dis"]:
        w.section(ch["dis"], new_page=True)
        _disagreements(w, run, d, chair if ok_chair else {}, ch["B"], toc)

    # ── the committee ────────────────────────────────────────────────────────
    w.section(ch["com"], new_page=True)
    rows, logos = [], []
    for s in _seats(run):
        a = run["answers"].get(s["label"]) or {}
        r = rank.get(s["label"])
        rows.append([s["name"], table_model(s) + answered_by(run, s["name"]), "yes" if a.get("status") == "ok" else a.get("status", "not run"),
                     conf_text(a.get("confidence")),
                     f"{ordinal(place[s['label']])} of {n_ranked}" if r else "–", str(r["first_votes"]) if r else "–"])
        logos.append(_slot_logo(run, s, s["name"]))
    w.table(["", "Seat", "Model", "Answered", "Confidence", "Peer place", "1st votes"], rows, (7, 29, 40, 18, 26, 22, 20),
            align=("LEFT", "LEFT", "LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT"), logos=logos)
    w.logo_line(_slot_logo(run, run.get("chairman") or {}, "Chairman", MUTED),
                f"Chairman: {table_model(run['chairman'])}{answered_by(run, 'Chairman')}. Providers and full model "
                f"names are listed with each seat in {ch['A'].split(' · ')[0]}.")

    w.heading("Positions")
    for s in _seats(run):
        a = run["answers"].get(s["label"]) or {}
        text = a.get("summary") if a.get("status") == "ok" else f"did not answer ({a.get('error', 'not run')})"
        w.para(f"**{s['name']}.** {text or '—'}", after=1.8)

    if run.get("ranking"):
        w.heading("How they ranked each other", room=50)
        w.para("Each seat ranked all the answers, its own included, without knowing who wrote which, in a shuffled "
               + ("order; its vote for its own answer is left out of the places. " if _sv(run) else "order. ")
               + "Columns run from the best-placed answer to the last.", size=SMALL, color=MUTED, after=2)
        _matrix(w, run)

    found = run_insights(run) if run.get("reviews") else []
    if found:
        w.heading("What the run reveals")
        w.bullets([f"**{f['title']}.** {f['text']}" for f in found], size=SMALL, after=1.2)

    # ── appendix A: seats ────────────────────────────────────────────────────
    w.section(ch["A"], new_page=True)
    for i, s in enumerate(_seats(run)):
        a = run["answers"].get(s["label"]) or {}
        if i:
            if pdf.will_page_break(80):
                pdf.add_page()
            else:
                pdf.ln(8)
        pdf.start_section(_plain(s["name"]), level=1)
        w.pages.setdefault(f"seat:{s['name']}", pdf.page_no())
        r = rank.get(s["label"])
        _seat_band(w, {**s, "_answered_by": answered_by(run, s["name"])}, a,
                   f"peer place {ordinal(place[s['label']])} of {n_ranked}" if r else "", _slot_logo(run, s, s["name"]))
        if s.get("instructions"):
            w.heading("Instructions given", above=1)
            w.para(s["instructions"], color=MUTED)
            rf = next((x for x in (run.get("chair") or {}).get("role_fit") or [] if x.get("label") == s["label"]), None)
            if rf:
                w.para(f"Role (chairman's note): {'drifted' if rf['fit'] == 'drifted' else 'kept to it'}"
                       + (f" · {rf['note']}" if rf.get("note") else ""), color=MUTED)
        if a.get("status") != "ok":
            w.para(f"Did not answer: {a.get('error', 'not run')}", bold=True)
            continue
        for title, key in (("Position", "summary"), ("Would change its mind if", "would_change_mind")):
            if a.get(key):
                w.heading(title)
                w.para(a[key])
        for title, key in (("Key points", "key_points"), ("Assumptions", "assumptions"), ("Risks", "risks"),
                           ("Evidence it would need", "evidence_gaps")):
            if a.get(key):
                w.heading(title)
                w.bullets(a[key])
        crit = [(names.get(rl, rl), e) for rl, rv in (run.get("reviews") or {}).items() if rv.get("status") == "ok"
                for e in rv.get("evaluations") or [] if e["label"] == s["label"]]
        if crit:
            w.heading("What the reviewers said about it")
            w.bullets([f"**{who}{' (its own answer, unknowingly)' if who == s['name'] else ''}.** "
                       f"Strengths: {e['strengths'] or '—'} Weaknesses: {e['weaknesses'] or '—'}" for who, e in crit])
        w.heading("Full answer")
        w.markdown(a["answer"])

    # ── appendix: the debate in full ─────────────────────────────────────────
    if ch["B"]:
        w.section(ch["B"], new_page=True)
        _debate_full(w, run, d, chair if ok_chair else {})

    # ── appendix: evidence ───────────────────────────────────────────────────
    ev_start = None
    background = own_brief(run)
    web = run.get("web") or {}
    if ch["C"]:
        w.section(ch["C"], new_page=True)
        ev_start = pdf.page_no()
        if run.get("files"):
            w.explain("The seats cite text as [E1], [E2]… and photos as [I1], [I2]…. Citations in this report link here.",
                   size=SMALL, color=MUTED, after=3)
            w.table(["ID", "File", "Where", "Type", "Size", "Tokens", "Notes"], _evidence_rows(run),
                    (10, 40, 30, 14, 15, 15, 38), align=("LEFT", "LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT", "LEFT"),
                    size=SMALL)
            imgs = (run.get("evidence_index") or {}).get("images") or []
            if imgs:
                # Photos are listed with their written descriptions, not embedded: the report stays short
                # however many photos a run has. The pictures themselves are kept in the run folder.
                w.heading("Photos")
                w.para("Each photo's written description, as read by the blind vote and the chairman. The photos "
                       "themselves are kept in the run folder (evidence).", size=SMALL, color=MUTED, after=2)
                for img in imgs:
                    where = f" · {img['locator']}" if img.get("locator") else ""
                    desc = img.get("description") or f"Not described: {img.get('describe_error') or 'unknown'}"
                    by = f" (described by {img['described_by'].split(' / ')[-1]})" if img.get("described_by") else ""
                    w.item("•", f"**[{img['id']}] {img['source']}{where}.** {desc}{by}", after=2.4)
        if web.get("items"):
            w.heading("Web sources (Research first)")
            w.explain("Found by one web search before the seats answered; every seat got the same passages, cited as "
                   "[W1], [W2]…. Unverified" + (f"; accessed {web['accessed']}" if web.get("accessed") else "") + ".",
                   size=SMALL, color=MUTED, after=2)
            for x in web["items"]:
                quote = x["excerpt"] if len(x["excerpt"]) <= 420 else x["excerpt"][:417].rsplit(" ", 1)[0] + "…"
                w.item("•", f"**[{x['id']}] {x['title']}** · {x['site']}. “{quote}” {x['url']}", after=2.4)
        if run.get("evidence"):
            w.heading("Evidence typed in")
            w.markdown(run["evidence"])
        if background:
            w.heading("Background given to the seats")
            w.markdown(background)

    # ── appendix: method and cost ────────────────────────────────────────────
    w.section(ch["D"], room=90)
    w.bullets(_method_lines(run))
    rows = _stage_rows(run)
    if rows:
        pdf.ln(2)
        w.table(["Stage", "Calls", "Tokens in", "Tokens out", "Est. cost"], rows, (50, 20, 30, 30, 32),
                align=("LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"))
    w.heading("Disclaimer")
    w.para(DISCLAIMER, size=SMALL, color=MUTED)
    w.heading("About this report")
    w.para(about(run), size=SMALL, color=MUTED)
    pdf.exec_fit = getattr(pdf, "exec_fit", True)
    pdf.toc = w.pages
    return pdf, ev_start


def render(run: dict[str, Any], out_path: Path, theme: Optional[str] = None) -> Path:
    out_path = Path(out_path)
    th = THEMES.get(theme or run.get("report_theme") or DEFAULT_THEME) or THEMES[DEFAULT_THEME]
    # First pass: where every section lands (and whether page 1 fits at full length). Second pass: the same
    # layout with the page numbers and citation links filled in. Page 1 is shortened step by step if needed.
    level = 0
    pdf, ev_page = _build(run, out_path, None, th, None, level)
    while not pdf.exec_fit and level < EXEC_LAST:
        level += 1
        pdf, ev_page = _build(run, out_path, None, th, None, level)
    idx = run.get("evidence_index") or {}
    links = ev_page if ev_page and (idx.get("sections") or idx.get("images")) else None
    toc = pdf.toc
    pdf, _ = _build(run, out_path, links, th, toc, level)
    while not pdf.exec_fit and level < EXEC_LAST:                # real page numbers made the contents line longer
        level += 1
        pdf, _ = _build(run, out_path, links, th, toc, level)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path

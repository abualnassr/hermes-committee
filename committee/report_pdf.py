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

from .config import model_label
from .engine import usage_totals
from . import __version__
from .report_md import (DISCLAIMER, PRODUCT, about, delib_decided, delib_last, delib_line, exec_summary,
                        stance_word)

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
_CITE = re.compile(r"\[((?:E|I)\d+(?:\s*[,;–-]\s*(?:[EI]?\d+))*)\]")
STATUS = {"failed": "No verdict", "stopped": "Stopped", "running": "Running"}
_TNUM = {"tnum": True}


def short_model(slot: dict[str, Any]) -> str:
    """Model id without the provider route (tables); 'active model' when unset."""
    return slot.get("model") or model_label(slot)


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

    head = f"{PRODUCT}  ·  {run.get('title') or 'Committee report'}  ·  run {run['id']}"

    class Report(FPDF):
        MARKDOWN_LINK_UNDERLINE = False
        MARKDOWN_LINK_COLOR = theme["accent"]

        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("Sans", "", SMALL)
            self.set_text_color(*FAINT)
            self.cell(0, 6, _plain(head), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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
    pdf.set_fallback_fonts(["Arabic", "Sans"])
    pdf.set_text_shaping(use_shaping_engine=True, features=_TNUM, direction="ltr")
    pdf.set_title(run.get("title") or f"Committee report {run['id']}")
    pdf.set_subject(_plain(run.get("question") or "")[:200])
    pdf.set_creator(f"{PRODUCT} {__version__} (plugin for Hermes Agent)")
    pdf.set_author(PRODUCT)
    pdf.set_keywords("Hermes Committee, LLM council, committee report")
    return pdf, XPos, YPos


def _md_safe(text: str) -> str:
    """Neutralise fpdf2's other markdown markers so only **bold** and links take effect."""
    return text.replace("\\", "/").replace("--", "–").replace("~~", "≈").replace("__", "_ _")


class _W:
    """Writing helpers bound to one document and one theme."""

    def __init__(self, pdf, XPos, YPos, theme: dict, ev_page: Optional[int] = None):
        self.pdf, self.X, self.Y, self.t, self.ev_page = pdf, XPos, YPos, theme, ev_page
        self.pages: dict[str, int] = {}        # section title -> page, for the executive summary's contents

    @property
    def accent(self):
        return self.t["accent"]

    def _prep(self, raw: str) -> tuple[str, bool, bool]:
        rtl = has_rtl(raw)
        text = _plain(raw, keep_bold=not rtl).strip()
        md = False
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
              size: float = BODY):
        """Unfilled table with hairline rules; numbers right-aligned; never a lone header at a page end."""
        from fpdf.fonts import FontFace
        pdf = self.pdf
        if rows and pdf.will_page_break(min(7.5 * (len(rows) + 1) + 4, 110)):
            pdf.add_page()
        pdf.set_font("Sans", "", size)
        pdf.set_text_color(*INK)
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        with pdf.table(col_widths=widths, text_align=align or "LEFT", line_height=5.6, first_row_as_headings=True,
                       borders_layout="HORIZONTAL_LINES", cell_fill_mode="NONE", repeat_headings=1, padding=(1.4, 1.5),
                       headings_style=FontFace(emphasis="BOLD", color=MUTED, size_pt=SMALL),
                       width=pdf.epw) as t:
            t.row([_plain(h) for h in header])
            for r in rows:
                t.row([_plain(str(c)) for c in r])
        pdf.ln(3)


# ── data helpers ─────────────────────────────────────────────────────────────

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
    out = [(f"{ok} of {len(seats)}", "seats answered")]
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
    if rk:
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
        score = rank_score(r["avg_rank"], n)
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
    out.append("Then each seat ranked the others' answers without knowing who wrote them (the blind vote)."
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
    return out + [f"Warning: {x}" for x in run.get("warnings") or []]


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


def _deliberation(w: _W, run: dict, d: dict, chair: dict):
    """Per disputed point: each seat's side before → after with its argument, then the chairman's call."""
    pdf = w.pdf
    w.heading("Deliberation", room=60)
    pdf.start_section("Deliberation", level=1)
    rounds = len(d["rounds"])
    w.para((d.get("reason") or "") + f" The seats answered the points below in {rounds} round{'s' if rounds > 1 else ''}; "
           "each had to hold or change its position and give its reason.", size=SMALL, color=MUTED, after=3)
    decided = delib_decided(chair, d["disputes"])
    kinds = {"action": "about the action", "claim": "about a claim the decision rests on", "emphasis": "emphasis"}
    for p in d["disputes"]:
        if pdf.will_page_break(30):
            pdf.add_page()
        still = " · still disputed" if p["id"] in (d.get("open") or []) else ""
        w.para(f"**{p['id']} · {p['point']}**", after=0.6)
        w.para(f"{kinds.get(p['level'], p['level'])}{still}", size=SMALL, color=MUTED, after=1.6)
        for pos in p["positions"]:
            r = delib_last(d, pos["seat"], p["id"])
            moved = f" → {r['side']}" if r and r.get("side") and r["side"] != pos["side"] else ""
            word = stance_word(r, pos["side"])
            said = (r or {}).get("argument") or ""
            ev = "".join(f" [{e}]" for e in (r or {}).get("evidence") or []
                         if re.fullmatch(r"[EI]\d+", e) and not re.search(rf"\b{e}\b", said))   # not cited twice
            w.item("•", f"**{pos['seat']}** ({pos['side']}{moved}; {word}). {(r or {}).get('argument') or pos['view']}{ev}",
                   after=1.4)
        x = decided.get(p["id"])
        if x:
            pdf.ln(0.6)
            w.para(f"**Chairman's call:** {x['decided']}" + (f" Decided by: {x['by_argument']}" if x.get("by_argument") else ""),
                   after=3)
        else:
            pdf.ln(2.4)
    seats = [s["name"] for s in _seats(run)]
    rows = []
    for name in seats:
        row = [name]
        for p in d["disputes"]:
            r = delib_last(d, name, p["id"])
            start = next((q["side"] for q in p["positions"] if q["seat"] == name), None)
            row.append(stance_word(r, start) if start is not None else "–")
        rows.append(row)
    if d["disputes"]:
        k = len(d["disputes"])
        first = 40
        rest = (min(pdf.epw, 162) - first) / k
        pdf.ln(2)
        w.table(["Seat"] + [p["id"] for p in d["disputes"]], rows, (first,) + (rest,) * k, size=SMALL)


# ── page 1: executive summary ────────────────────────────────────────────────

_CONTENTS = ("Decision in detail", "The committee", "The chairman's full reasoning", "Appendix A · Each seat's answer",
             "Appendix B · Evidence", "Appendix C · How it ran and what it cost")
# (bottom line, reason, action, condition, conditions shown): shorten text first, drop conditions last
_EXEC_LIMITS = ((480, 220, 200, 200, 2), (420, 190, 170, 170, 2), (420, 190, 170, 170, 1), (360, 160, 140, 150, 1),
                (300, 130, 120, 130, 1), (260, 110, 100, 0, 0))
EXEC_LAST = len(_EXEC_LIMITS) - 1


def _clip_txt(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


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
                w.para(f"{es['more_actions']} more in Decision in detail.", size=SMALL, color=MUTED)
        if es["reverse"] and n_rev:
            w.heading("What would change this", above=4, room=0)
            w.bullets([_clip_txt(r, lim_rev) for r in es["reverse"][:n_rev]], after=1.2)
        pdf.ln(3)
        w.para("How the committee stood: " + " · ".join(es["stood"]) + f" · {duration(run_seconds(run))}"
               + f" · {_cost_value(run)[0]} {_cost_value(run)[1]}", size=SMALL, color=MUTED)
    # contents with page numbers (filled in on the second pass), clickable
    items = [(t, toc[t]) for t in _CONTENTS if t in toc]
    if items:
        pdf.ln(3)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + min(pdf.epw, MEASURE), pdf.get_y())
        pdf.ln(2.5)
        w.para("**Inside this report:** " + "  ·  ".join(f"[{t}]({p}) {p}" for t, p in items),
               size=SMALL, color=MUTED)
    pdf.exec_fit = pdf.page_no() == 1 and pdf.get_y() <= pdf.h - 24    # about 8 mm of air above the footer
    if es.get("assembled"):
        pass                                   # older run: the page is built from the verdict's own fields


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
            w.caps("Decision", color=w.accent, x=x, width=inner)
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


def _seat_band(w: _W, s: dict, a: dict, place_txt: str):
    """A seat's header: a light band with the name, then model, confidence and peer place."""
    pdf = w.pdf
    if pdf.will_page_break(60):
        pdf.add_page()
    y = pdf.get_y()
    h = 15
    pdf.set_fill_color(*TINT)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "F")
    pdf.set_fill_color(*w.accent)
    pdf.rect(pdf.l_margin, y, 1.2, h, "F")
    pdf.set_xy(pdf.l_margin + 5, y + 2.2)
    pdf.set_font("Sans", "B", SECTION)
    pdf.set_text_color(*INK)
    pdf.cell(pdf.epw - 8, 6, _fit(pdf, _plain(s["name"]), pdf.epw - 8))
    pdf.set_xy(pdf.l_margin + 5, y + 8.6)
    pdf.set_font("Sans", "", SMALL)
    pdf.set_text_color(*MUTED)
    bits = [model_label(s), f"confidence {conf_text(a.get('confidence'))}"] + ([place_txt] if place_txt else [])
    pdf.cell(pdf.epw - 8, 4.5, _fit(pdf, _plain("   ·   ".join(bits)), pdf.epw - 8))
    pdf.set_y(y + h + 4)


# ── the document ─────────────────────────────────────────────────────────────

def _build(run: dict[str, Any], out_path: Path, ev_page: Optional[int], theme: dict,
           toc: Optional[dict] = None, level: int = 0):
    from .followup import name_chair, own_brief
    from .insights import run_insights
    pdf, XPos, YPos = _new_pdf(run, theme)
    w = _W(pdf, XPos, YPos, theme, ev_page)
    chair = run.get("chair") or {}
    ok_chair = chair.get("status") == "ok"
    if ok_chair:
        chair = name_chair(chair, run.get("seats") or [])
    names = _names(run)
    rank = {r["label"]: r for r in run.get("ranking") or []}
    place = {r["label"]: i + 1 for i, r in enumerate(run.get("ranking") or [])}
    n_ranked = len(place)

    # ── page 1: executive summary ───────────────────────────────────────────
    pdf.add_page()
    pdf.start_section("Executive summary")
    _exec_page(w, run, ok_chair, toc or {t: 88 for t in _CONTENTS}, level)

    # ── decision in detail ───────────────────────────────────────────────────
    w.section("Decision in detail", new_page=True)
    detail_page = pdf.page_no()
    _decision_box(w, chair, ok_chair, run)
    w.para(("**Follow-up question:** " if run.get("follow_of") else "**Question:** ") + run["question"], size=BODY,
           color=MUTED)
    if delib_line(run):
        pdf.ln(1.5)
        w.para(delib_line(run), size=BODY, color=MUTED)
    pdf.ln(3.5)
    _strip(w, _figures(run))

    prev = _earlier(run, out_path)
    if prev:
        _changed(w, prev, chair, ok_chair)

    if ok_chair:
        if chair.get("reverse_if"):
            w.keep_list("It would reverse if", chair["reverse_if"], after=0.9, above=2)
        if chair.get("next_actions"):
            w.keep_list("Next actions", chair["next_actions"], numbered=True, after=0.9, above=2)
        if chair.get("dissent") and not pdf.will_page_break(10):
            pdf.ln(2)
            w.para(f"Dissent kept on record: {', '.join(d.get('name') or d.get('label') for d in chair['dissent'])} "
                   "(see The committee).", size=SMALL, color=MUTED)

    # ── the committee ────────────────────────────────────────────────────────
    w.section("The committee", new_page=pdf.page_no() == detail_page, room=90)
    rows = []
    for s in _seats(run):
        a = run["answers"].get(s["label"]) or {}
        r = rank.get(s["label"])
        rows.append([s["name"], short_model(s), "yes" if a.get("status") == "ok" else a.get("status", "not run"),
                     conf_text(a.get("confidence")),
                     f"{ordinal(place[s['label']])} of {n_ranked}" if r else "–", str(r["first_votes"]) if r else "–"])
    w.table(["Seat", "Model", "Answered", "Confidence", "Peer place", "1st votes"], rows, (30, 44, 20, 28, 22, 18),
            align=("LEFT", "LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT"))
    w.para(f"Chairman: {short_model(run['chairman'])}. Provider routes are listed with each seat in Appendix A.",
           size=SMALL, color=MUTED)

    w.heading("Positions")
    for s in _seats(run):
        a = run["answers"].get(s["label"]) or {}
        text = a.get("summary") if a.get("status") == "ok" else f"did not answer ({a.get('error', 'not run')})"
        w.para(f"**{s['name']}.** {text or '—'}", after=2.6)

    if ok_chair and chair.get("dissent"):
        w.heading("Dissent kept on record")
        for d in chair["dissent"]:
            w.para(f"**{d.get('name') or names.get(d.get('label'), d.get('label'))}.** {d['view']}", after=2.6)

    d = run.get("deliberation") or {}
    if d.get("rounds"):
        _deliberation(w, run, d, chair if ok_chair else {})

    if run.get("ranking"):
        w.heading("How they ranked each other", room=50)
        w.para("Each seat ranked the others' answers without knowing who wrote them, in a shuffled order. "
               "A longer bar means a better average place.", size=SMALL, color=MUTED, after=3)
        _vote_chart(w, run)
        w.bullets(_ballots(run), SMALL, after=0.8)

    found = run_insights(run) if run.get("reviews") else []
    if found:
        w.heading("What the run reveals")
        w.bullets([f"**{f['title']}.** {f['text']}" for f in found])

    if ok_chair:
        if chair.get("agreement"):
            w.heading("Where they agreed")
            w.bullets(chair["agreement"])
        if chair.get("open_questions"):
            w.heading("Open questions")
            w.bullets(chair["open_questions"])
        w.section("The chairman's full reasoning", new_page=True)
        w.markdown(chair["final_answer"])

    # ── appendix A: seats ────────────────────────────────────────────────────
    w.section("Appendix A · Each seat's answer", new_page=True)
    for i, s in enumerate(_seats(run)):
        a = run["answers"].get(s["label"]) or {}
        if i:
            if pdf.will_page_break(80):
                pdf.add_page()
            else:
                pdf.ln(8)
        pdf.start_section(_plain(s["name"]), level=1)
        r = rank.get(s["label"])
        _seat_band(w, s, a, f"peer place {ordinal(place[s['label']])} of {n_ranked}" if r else "")
        if s.get("instructions"):
            w.heading("Instructions given", above=1)
            w.para(s["instructions"], color=MUTED)
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
            w.heading("What the other seats said about it")
            w.bullets([f"**{who}.** Strengths: {e['strengths'] or '—'} Weaknesses: {e['weaknesses'] or '—'}"
                       for who, e in crit])
        w.heading("Full answer")
        w.markdown(a["answer"])

    # ── appendix B: evidence ─────────────────────────────────────────────────
    ev_start = None
    background = own_brief(run)
    if run.get("files") or run.get("evidence") or background:
        w.section("Appendix B · Evidence", new_page=True)
        ev_start = pdf.page_no()
        if run.get("files"):
            w.para("The seats cite text as [E1], [E2]… and photos as [I1], [I2]…. Citations in this report link here.",
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
        if run.get("evidence"):
            w.heading("Evidence typed in")
            w.markdown(run["evidence"])
        if background:
            w.heading("Background given to the seats")
            w.markdown(background)

    # ── appendix C: method and cost ──────────────────────────────────────────
    w.section("Appendix C · How it ran and what it cost", room=90)
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

"""PDF committee report (neutral design) via fpdf2 + uharfbuzz text shaping.

Any paragraph that contains Arabic script is laid out right-to-left and right-aligned
(fpdf2's default base direction reverses Arabic word order — verified in Phase 0).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .config import model_label
from .engine import usage_totals
from .report_md import DISCLAIMER, ranking_matrix

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FOOTER = "Advisory output from AI models, not professional approval. Full disclaimer on the method page."
INK, MUTED, RULE, ACCENT = (24, 24, 27), (100, 100, 110), (215, 215, 222), (37, 99, 235)
_AR = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_INLINE = [(re.compile(r"\*\*(.+?)\*\*"), r"\1"), (re.compile(r"__(.+?)__"), r"\1"),
           (re.compile(r"`([^`]+)`"), r"\1"), (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)")]


def short_model(slot: dict[str, Any]) -> str:
    """Model id without the provider route (tables); 'active model' when unset."""
    return slot.get("model") or model_label(slot)


def has_rtl(text: str) -> bool:
    return bool(_AR.search(text or ""))


def _plain(text: str) -> str:
    text = text or ""
    for rx, rep in _INLINE:
        text = rx.sub(rep, text)
    text = re.sub(r"</?details>|<summary>.*?</summary>", "", text)
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)   # drop emoji outside the bundled fonts


def _new_pdf(run: dict[str, Any]):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    class Report(FPDF):
        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("Sans", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 6, f"hermes-committee  ·  {run['template_title']}  ·  run {run['id']}",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*RULE)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(4)

        def footer(self):
            self.set_y(-14)
            self.set_font("Sans", "", 7)
            self.set_text_color(*MUTED)
            self.cell(self.epw - 20, 5, FOOTER, align="L")
            self.cell(20, 5, f"{self.page_no()} / {{nb}}", align="R")

    pdf = Report(format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, 18)
    pdf.alias_nb_pages()
    pdf.add_font("Sans", "", FONTS / "NotoSans-Regular.ttf")
    pdf.add_font("Sans", "B", FONTS / "NotoSans-Bold.ttf")
    pdf.add_font("Arabic", "", FONTS / "NotoNaskhArabic-Regular.ttf")
    pdf.add_font("Arabic", "B", FONTS / "NotoNaskhArabic-Bold.ttf")
    pdf.set_fallback_fonts(["Arabic", "Sans"])
    pdf.set_text_shaping(use_shaping_engine=True, direction="ltr")
    pdf.set_title(f"Committee report {run['id']}")
    pdf.set_creator("hermes-committee")
    return pdf, XPos, YPos


class _W:
    """Small writing helpers bound to one document."""

    def __init__(self, pdf, XPos, YPos):
        self.pdf, self.X, self.Y = pdf, XPos, YPos

    def para(self, text: str, size: float = 10, bold: bool = False, color=INK, indent: float = 0, lh: float = 5.2):
        text = _plain(text).strip()
        if not text:
            return
        pdf = self.pdf
        rtl = has_rtl(text)
        if rtl:
            pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="arab", language="ara")
        pdf.set_font("Arabic" if rtl else "Sans", "B" if bold else "", size + (1 if rtl else 0))
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(pdf.epw - indent, lh + (1.5 if rtl else 0), text, align="R" if rtl else "L",
                       new_x=self.X.LMARGIN, new_y=self.Y.NEXT)
        if rtl:
            pdf.set_text_shaping(use_shaping_engine=True, direction="ltr")

    def heading(self, text: str, size: float = 13):
        self.pdf.ln(2)
        self.para(text, size=size, bold=True, lh=7)
        self.pdf.ln(1)

    def label(self, text: str, indent: float = 0):
        self.para(text.upper(), size=8, bold=True, color=MUTED, lh=4.5, indent=indent)

    def bullets(self, items: list[str], size: float = 10):
        for it in items:
            self.para(("• " if not has_rtl(it) else "") + it + (" •" if has_rtl(it) else ""), size=size, indent=3)

    def markdown(self, md: str, size: float = 10):
        """Readable rendering of a Markdown answer: headings, bullets, tables, code, paragraphs."""
        buf: list[str] = []
        code = False

        def flush():
            if buf:
                self.para(" ".join(buf), size=size)
                self.pdf.ln(1)
                buf.clear()

        for raw in (md or "").splitlines():
            line = raw.rstrip()
            if line.strip().startswith("```"):
                flush()
                code = not code
                continue
            if code:
                self.para(line or " ", size=size - 1.5, color=MUTED, indent=3, lh=4.4)
                continue
            s = line.strip()
            if not s:
                flush()
                continue
            m = re.match(r"^(#{1,6})\s+(.*)", s)
            if m:
                flush()
                self.para(m.group(2), size=max(size + 3 - len(m.group(1)), size), bold=True, lh=6)
                continue
            if re.match(r"^\|?\s*:?-{3,}", s):
                continue
            if s.startswith("|"):
                flush()
                self.para("  |  ".join(c.strip() for c in s.strip("|").split("|")), size=size - 1, indent=2)
                continue
            m = re.match(r"^([-*+]|\d+[.)])\s+(.*)", s)
            if m:
                flush()
                mark = "•" if m.group(1) in "-*+" else m.group(1)
                self.para(f"{mark} {m.group(2)}", size=size, indent=3 + (raw.find(m.group(1)) // 2) * 3)
                continue
            buf.append(s)
        flush()

    def table(self, header: list[str], rows: list[list[str]], widths: tuple, align="LEFT"):
        pdf = self.pdf
        pdf.set_font("Sans", "", 8.5)
        pdf.set_text_color(*INK)
        with pdf.table(col_widths=widths, text_align=align, line_height=5, first_row_as_headings=True,
                       borders_layout="HORIZONTAL_LINES") as t:
            t.row([_plain(h) for h in header])
            for r in rows:
                t.row([_plain(str(c)) for c in r])
        pdf.ln(3)


def render(run: dict[str, Any], out_path: Path) -> Path:
    pdf, XPos, YPos = _new_pdf(run)
    w = _W(pdf, XPos, YPos)
    chair = run.get("chair") or {}
    ok_chair = chair.get("status") == "ok"
    rank = {r["label"]: r for r in run.get("ranking") or []}

    # ── page 1: verdict ────────────────────────────────────────────────────
    pdf.add_page()
    w.label(f"Committee report · {run['template_title']} · "
            f"{time.strftime('%Y-%m-%d', time.localtime(run['created']))} · {run['status']}")
    pdf.ln(2)
    w.para(run["question"], size=17, bold=True, lh=7.5)
    pdf.ln(3)
    if ok_chair:
        y0, page0 = pdf.get_y(), pdf.page_no()
        w.label("Verdict", indent=6)
        w.para(chair.get("decision") or "—", size=12, bold=True, indent=6, lh=6)
        conf = (chair.get("confidence") or "not stated").capitalize()
        w.para(f"Confidence: {conf}" + (f". {chair['confidence_note']}" if chair.get("confidence_note") else ""),
               size=9.5, color=MUTED, indent=6)
        for item in chair.get("reverse_if") or []:
            w.para(f"Would reverse if: {item}", size=9.5, color=MUTED, indent=6)
        y1 = pdf.get_y()
        if pdf.page_no() == page0 and y1 > y0:   # accent bar beside the verdict block (never over text)
            pdf.set_fill_color(*ACCENT)
            pdf.rect(pdf.l_margin, y0, 1.4, y1 - y0, "F")
        pdf.set_y(y1 + 4)
    else:
        w.para(f"No chairman verdict. {run.get('error') or ''}", size=11, bold=True)

    w.heading("Roster")
    rows = []
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        r = rank.get(s["label"])
        conf = a.get("confidence")
        rows.append([s["label"], s["name"], short_model(s), a.get("status", "not run"),
                     f"{conf:.2f}" if isinstance(conf, (int, float)) else "–",
                     f"{r['avg_rank']:.2f}" if r else "–"])
    w.table(["", "Seat", "Model", "Stage 1", "Conf.", "Avg rank"], rows, (8, 52, 52, 22, 15, 25))
    w.para(f"Chairman: {short_model(run['chairman'])}. Full provider routes are on each seat's page.",
           size=8.5, color=MUTED)

    w.heading("Positions")
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        text = a.get("summary") if a.get("status") == "ok" else f"did not answer ({a.get('error', 'not run')})"
        w.para(f"{s['label']} · {s['name']}: {text or '—'}", size=9.5, indent=2)
        pdf.ln(0.8)

    if run.get("reviews"):
        w.heading("Peer ranking (anonymous)")
        w.para("Each reviewer saw anonymous responses in a shuffled order. 1 = best. "
               "Rows are reviewers; columns are the responses they ranked.", size=8.5, color=MUTED)
        labels, mrows = ranking_matrix(run)
        n = max(1, len(labels))
        w.table(["Reviewer"] + labels, mrows, (28,) + ((pdf.epw - 28) / n,) * n, align="CENTER")
        if run.get("ranking"):
            w.para("Overall: " + ", ".join(f"{r['label']} ({r['avg_rank']:.2f}, {r['first_votes']} first)"
                                           for r in run["ranking"]), size=9)

    if ok_chair:
        if chair.get("dissent"):
            w.heading("Dissent kept on record")
            w.bullets([f"Response {d['label']}: {d['view']}" for d in chair["dissent"]])
        if chair.get("agreement"):
            w.heading("Agreement")
            w.bullets(chair["agreement"])
        if chair.get("next_actions"):
            w.heading("Next actions")
            w.bullets(chair["next_actions"])
        if chair.get("open_questions"):
            w.heading("Open questions")
            w.bullets(chair["open_questions"])
        pdf.add_page()
        w.heading("Final answer", 15)
        w.markdown(chair["final_answer"])

    # ── one page per seat ───────────────────────────────────────────────────
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        pdf.add_page()
        w.heading(f"Response {s['label']}: {s['name']}", 15)
        r = rank.get(s["label"])
        conf = a.get("confidence")
        conf_s = "%.2f" % conf if isinstance(conf, (int, float)) else "n/a"
        rank_s = "%.2f" % r["avg_rank"] if r else "n/a"
        w.para(f"Model: {model_label(s)}   ·   Confidence: {conf_s}   ·   Average peer rank: {rank_s}",
               size=8.5, color=MUTED)
        if s.get("instructions"):
            w.label("Instructions given")
            w.para(s["instructions"], size=9.5)
        if a.get("status") != "ok":
            w.para(f"Did not answer: {a.get('error', 'not run')}", bold=True)
            continue
        for title, key in (("Position", "summary"), ("Would change its mind if", "would_change_mind")):
            if a.get(key):
                w.label(title)
                w.para(a[key])
        for title, key in (("Key points", "key_points"), ("Assumptions", "assumptions"), ("Risks", "risks"),
                           ("Evidence it would need", "evidence_gaps")):
            if a.get(key):
                w.label(title)
                w.bullets(a[key], size=9.5)
        crit = [e for rv in run["reviews"].values() if rv.get("status") == "ok"
                for e in rv.get("evaluations") or [] if e["label"] == s["label"]]
        if crit:
            w.label("Peer critiques received")
            w.bullets([f"Strengths: {e['strengths'] or '—'}  Weaknesses: {e['weaknesses'] or '—'}" for e in crit], 9)
        w.label("Full answer")
        w.markdown(a["answer"], size=9.5)

    # ── method ─────────────────────────────────────────────────────────────
    pdf.add_page()
    w.heading("Method and usage", 15)
    u = usage_totals(run)
    cost = f"${u['cost_usd']:.4f}" if u["cost_usd"] is not None else "unknown (providers did not report cost)"
    w.bullets([f"Template: {run['template_title']}" + (f"; preset {run['preset']}" if run.get("preset") else ""),
               "Stages: independent answers → " + ("anonymous peer ranking → " if run["review"] else "")
               + "chairman synthesis",
               f"Quorum: {run['quorum']} of {len(run['seats'])}; stage time limit {run['stage_timeout']} s",
               f"Model calls: {u['calls']}; tokens in {u['input_tokens'] or 'n/a'}, out {u['output_tokens'] or 'n/a'}; cost {cost}"]
              + [f"Warning: {x}" for x in run.get("warnings") or []], size=9.5)
    w.heading("Disclaimer")
    w.para(DISCLAIMER, size=9.5)
    if run.get("brief"):
        w.heading("Brief")
        w.markdown(run["brief"], 9.5)
    if run.get("evidence"):
        w.heading("Evidence pack")
        w.markdown(run["evidence"], 9)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path

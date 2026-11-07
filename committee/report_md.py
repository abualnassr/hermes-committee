"""Markdown committee report — the searchable, copyable record of a run."""
from __future__ import annotations

import time
from typing import Any

from .config import model_label
from .engine import seat_by_label, usage_totals

DISCLAIMER = ("Advisory output from multiple AI models. It is not professional, engineering, legal, medical "
              "or financial approval. Check the reasoning and evidence before acting on it.")


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "_None recorded._"


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _conf(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def ranking_matrix(run: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    labels = [s["label"] for s in sorted(run["seats"], key=lambda s: s["label"])
              if run["answers"].get(s["label"], {}).get("status") == "ok"]
    rows = []
    for rev in labels:
        r = run["reviews"].get(rev) or {}
        if r.get("status") != "ok":
            rows.append([rev] + ["–"] * len(labels))
            continue
        rows.append([rev] + [str(r["ranking"].index(lab) + 1) if lab in r["ranking"] else "–" for lab in labels])
    return labels, rows


def render(run: dict[str, Any]) -> str:
    chair = run.get("chair") or {}
    ok_chair = chair.get("status") == "ok"
    rank = {r["label"]: r for r in run.get("ranking") or []}
    out: list[str] = []
    out.append(f"# Committee report: {run['question'].splitlines()[0][:150]}")
    out.append(f"{run['template_title']} · {_date(run['created'])} · run `{run['id']}` · status **{run['status']}**")
    if run.get("error"):
        out.append(f"> **Incomplete run:** {run['error']}")

    out.append("## Verdict")
    if ok_chair:
        out.append(f"**Decision:** {chair['decision'] or '—'}")
        conf = chair.get("confidence") or "not stated"
        out.append(f"**Confidence:** {conf}" + (f". {chair['confidence_note']}" if chair.get("confidence_note") else ""))
        if chair.get("reverse_if"):
            out.append("**Would reverse if:**\n" + _bullets(chair["reverse_if"]))
        out.append("## Final answer\n" + chair["final_answer"])
    else:
        out.append("_No chairman verdict. The individual answers below are the committee's output._")

    out.append("## Roster")
    out.append("| Response | Seat | Model | Stage 1 | Confidence | Avg peer rank |\n|---|---|---|---|---|---|")
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        r = rank.get(s["label"])
        avg = "%.2f" % r["avg_rank"] if r else "–"
        out.append(f"| {s['label']} | {_cell(s['name'])} | {_cell(model_label(s))} | {a.get('status', 'not run')} | "
                   f"{_conf(a.get('confidence'))} | {avg} |")
    out.append(f"Chairman: {model_label(run['chairman'])}")

    if run.get("reviews"):
        labels, rows = ranking_matrix(run)
        out.append("## Peer ranking\nEach reviewer saw anonymous responses in a shuffled order. "
                   "1 = best. Rows are reviewers, columns are the responses they ranked.")
        out.append("| Reviewer | " + " | ".join(labels) + " |\n|---|" + "---|" * len(labels))
        out.extend("| " + " | ".join(r) + " |" for r in rows)
        if run.get("ranking"):
            out.append("**Overall:** " + ", ".join(
                f"{r['label']} ({r['avg_rank']:.2f}, {r['first_votes']} first)" for r in run["ranking"]))

    if ok_chair:
        out.append("## Agreement\n" + _bullets(chair.get("agreement") or []))
        out.append("## Dissent kept on record")
        if chair.get("dissent"):
            out.extend(f"- **Response {d['label']}:** {d['view']}" for d in chair["dissent"])
        else:
            out.append("_No dissent recorded._")
        out.append("## Open questions\n" + _bullets(chair.get("open_questions") or []))
        out.append("## Next actions\n" + _bullets(chair.get("next_actions") or []))

    out.append("## Seat answers")
    for s in sorted(run["seats"], key=lambda s: s["label"]):
        a = run["answers"].get(s["label"]) or {}
        out.append(f"### Response {s['label']}: {s['name']} ({model_label(s)})")
        if s.get("instructions"):
            out.append(f"_Instructions:_ {s['instructions']}")
        if a.get("status") != "ok":
            out.append(f"**Did not answer:** {a.get('error', 'not run')}")
            continue
        out.append(f"**Position:** {a.get('summary') or '—'}  \n**Confidence:** {_conf(a.get('confidence'))}"
                   + ("  \n_Answer returned as plain text (structured output failed)._" if a.get("fallback") else ""))
        for title, key in (("Key points", "key_points"), ("Assumptions", "assumptions"), ("Risks", "risks"),
                           ("Evidence it would need", "evidence_gaps")):
            if a.get(key):
                out.append(f"**{title}:**\n" + _bullets(a[key]))
        if a.get("would_change_mind"):
            out.append(f"**Would change its mind if:** {a['would_change_mind']}")
        crit = [e for r in run["reviews"].values() if r.get("status") == "ok"
                for e in r.get("evaluations") or [] if e["label"] == s["label"]]
        if crit:
            out.append("**Peer critiques received:**\n" + "\n".join(
                f"- Strengths: {e['strengths'] or '—'} Weaknesses: {e['weaknesses'] or '—'}" for e in crit))
        out.append("<details><summary>Full answer</summary>\n\n" + a["answer"] + "\n\n</details>")

    u = usage_totals(run)
    cost = (f"${u['cost_usd']:.4f}" if u["cost_usd"] is not None
            else "unknown (the providers did not report cost)" + (" — partial figures only" if u["cost_partial"] else ""))
    out.append("## Method and usage")
    out.append(_bullets([
        f"Template: {run['template_title']}" + (f", preset `{run['preset']}`" if run.get("preset") else ""),
        f"Stages: independent answers → {'anonymous peer ranking → ' if run['review'] else ''}chairman synthesis",
        f"Quorum: {run['quorum']} of {len(run['seats'])} seats; stage time limit {run['stage_timeout']} s",
        f"Model calls: {u['calls']}; tokens in {u['input_tokens'] or 'n/a'}, out {u['output_tokens'] or 'n/a'}; cost {cost}",
    ] + [f"Warning: {w}" for w in run.get("warnings") or []]))
    if run.get("brief") or run.get("evidence"):
        out.append("## Appendix: brief and evidence")
        if run.get("brief"):
            out.append("### Brief\n" + run["brief"])
        if run.get("evidence"):
            out.append("### Evidence pack\n" + run["evidence"])
    out.append("---\n_" + DISCLAIMER + "_")
    return "\n\n".join(out) + "\n"

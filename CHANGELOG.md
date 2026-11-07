# Changelog

## 0.1.0 — unreleased

First version.

- Three-stage committee: independent answers, anonymous peer ranking with per-reviewer shuffling
  and identity scrubbing, and a chairman synthesis that keeps dissent visible.
- Templates: classic (Karpathy), expert-panel, decision-board, red-team, reliability-board.
  Custom seats can also be named in chat.
- Per-seat provider and model through `ctx.llm`, gated by the user's allowlist.
- One stage per tool call; quorum, a time limit per stage, and fallback when structured output fails.
- Stop-to-cancel through the `post_tool_call` and `on_session_end` hooks.
- Markdown and PDF report (neutral design, right-to-left Arabic), plus a JSON record of each run.
- `/hermes-committee:convene` skill, and the `hermes committee templates|presets|runs|doctor` CLI.

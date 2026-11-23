---
name: convene
description: Convene a committee of AI models on a hard question.
---

# Convene the committee

The user wants a committee of AI models to deliberate. The committee runs in stages:
**Stage 1** every seat answers independently, **Stage 2** seats rank each other's
anonymous answers, **Stage 3** a chairman writes the final answer. The plugin runs the stages;
your job is the brief before and the presentation after.

## 0. Started from the Committee page? (`draft=<id>`)

If the message contains `draft=<id>` (for example `/hermes-committee:convene draft=d3f9a1b2c4 Should we…`),
the user already set up the committee on the Committee page: question, seats, models, instructions
and evidence are stored in that draft.

- Your **first action** is `committee_open` with only `draft` set to the id. Then continue with
  step 4 below.
- Do **not** look for files first, even when the question mentions attached files or file names:
  the draft already holds every file from the page. Do not list folders, search, or read any file.
- Do **not** ask clarifying questions and do **not** change the seats or models.
- Do **not** add a `brief`, `evidence` or files: the plugin uses exactly what is on the page and
  ignores them. Never add facts about the user from memory.

## 0b. A retry (`retry=<run_id>`, or the user asks to retry a failed run)

If the message contains `retry=<run_id>` (the Committee page's Retry button), or the user asks to retry a
committee that stopped without a verdict:

- Call `committee_retry` with the `run_id`. Pass `swaps` only when the user named another model for a
  failed seat in this conversation (empty provider and model = Hermes's main model). With `retry=<id>` from
  the page, pass no `swaps`: the page already stored the choice.
- Do not open a new run, ask questions, change seats, or add evidence. Then continue with step 4.2
  (`committee_advance` until the status is `done`, `failed` or `cancelled`) and step 5.

## 1. Frame the question

- Take the question from the user's message (text after the command, or the conversation).
- If the question is too vague to answer well (no options, no goal, no context), ask **one**
  short clarifying question first. Otherwise proceed — do not interrogate the user.
- Write a **brief**: goal, constraints, decision criteria, and anything the user already said
  **in this conversation**. Never add facts from your memory or profile of the user (their hardware,
  employer, projects, location, preferences) or from other sessions, unless the user asks you to:
  the brief goes to every seat's provider. The Decision page shows the brief to the user as
  "Background given to the seats".

## 2. Build the evidence pack

Seats have no tools. They reason only over what you give them, so the evidence pack is
what makes the answers grounded:

- The user may ask in plain words ("convene a committee …") instead of the slash command: Hermes
  Desktop does not allow a slash command with attachments, so a request with attached files always
  arrives this way. Treat it exactly like the command.
- **Files the user attached or named** (PDF, Word, Excel, text, photos): pass their local paths as
  `evidence_files`. Do not paste their contents into `evidence`. The plugin reads each file once,
  numbers the sections ([E1], [E2]…) and sends photos with a written description to every seat
  (a seat whose model refuses the picture is retried with the description). If `committee_open`
  reports that every model refused pictures before, tell the user and ask whether to drop the photos or change a seat's model.
- Include other facts the user supplied (numbers, logs, prior messages) in `evidence`.
- If the question depends on current or external facts, pass `research: true`: the plugin runs
  one web search before the seats answer and adds checked passages with their sources ([W1]…).
  Do not also search yourself. Without that argument (older versions), do a short, focused search
  and paste the relevant excerpts **with their sources**.
- Never invent evidence. If something important is unknown, say so in the pack.
- Keep it under ~15,000 characters; summarize long documents and keep key numbers verbatim.

## 3. Choose the committee

- Default: omit `preset` and `template` (the user's default preset is used).
- If the user names a preset or template (`classic`, `expert-panel`, `decision-board`,
  `red-team`, `reliability-board`, `engineering-review`), pass it. `engineering-review` fits software
  questions: a design doc, an architecture or library choice, a refactor, or code files to review.
- If the user describes roles ("a structural engineer, a lawyer and a CFO"), pass them as
  `seats`, each with a `name` and specific `instructions` written from the user's wording.
- `review: false` only if the user asks for a quick/lite run.
- `deliberate` only if the user asks: "let them debate" / "make them argue it out" → `always`;
  "no debate" / "quick" → `never`; "only if they disagree on what to do" → `action`. Otherwise omit
  it (the default debates material disagreement). `deliberate_rounds: 2` only if the user asks for
  a longer debate. When `committee_advance` reports a deliberation, mention in one line how many
  points were debated and who changed their mind.
- `max_minutes` only if the user names a time limit ("within 5 minutes" → 5; "no time limit" → 0).
- `title` only if the user names the committee ("call it GB-7 gearbox").
- **Follow-ups.** When the user asks the committee again in a chat where a committee already ran
  ("convene again", "ask the committee whether…", "what if…, ask the committee"), pass
  `follow_of` = that run's `run_id` and the new question. Leave out seats/template so the same
  committee answers; its verdict goes to the seats as background and its evidence files are reused.
  A plain question about the verdict ("why did the Skeptic disagree?") is not a follow-up: answer it
  yourself from the result, without convening.
- `max_usd` only if the user names a budget ("under a dollar" → 1; "no budget" → 0). The default is $2.
  When `committee_open` returns a `budget` line, show it to the user in one short sentence
  (the estimated range and the limit). If the run fails with "Not started" or "No verdict within
  the … limit", tell the user plainly and suggest raising the budget or using cheaper seats.
  Otherwise omit it: the user's default (10 minutes) applies. If the run reports the blind vote was
  skipped or "No verdict within the … limit", tell the user plainly and suggest a longer limit.

## 4. Run it

1. Call `committee_open` with `question`, `brief`, `evidence` and any choices from step 3
   (or with `draft`, see step 0).
   If it returns an error, tell the user plainly and stop.
2. Call `committee_advance` with the `run_id`. Repeat until `status` is `done`, `failed`
   or `cancelled`. Do not add commentary between calls. Each call runs one stage.
3. If the user presses Stop, the run is cancelled; do not restart it unless asked.

## 5. Present the result

When `status` is `done`:

- Lead with the **decision** and **confidence** in one or two sentences.
- Give the committee's final answer (from `final_answer`), trimmed to what matters.
- A compact table: seat · model · position · confidence. Then the peer-ranking order.
- State any **dissent** explicitly. Never present a split committee as unanimous.
- Deliver the PDF report: put `MEDIA:<reports.pdf>` alone on its own line, using the path exactly as returned (forward slashes, no leading slash before a drive letter, not inside a link or backticks). Mention the Markdown
  report path too (`reports.markdown`); it is the copyable, searchable version.
- If `usage.cost_usd` is null, say cost was not reported by the providers; never estimate it
  as a fact.
- Pass on any `warnings` (e.g. all seats on the same model → correlated answers).

When `status` is `failed`: say which stage failed and why (`error`), name seats that did not
answer, and still deliver whatever report paths exist. If `can_retry` is true, add one line from
`retry_hint`: the user can ask you to retry (the answers that came in are kept), optionally with another
model for a failed seat. Do not retry unless the user asks. When `cancelled`: confirm it stopped.

## Rules

- This is advisory output. For safety, legal, medical or financial decisions, say that a
  qualified person must review it.
- Do not run a committee for simple lookups or chit-chat; answer those yourself.
- Do not re-run a committee to get a different answer unless the user asks.

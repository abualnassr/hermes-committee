---
name: convene
description: Convene an AI committee (Karpathy's LLM Council) on a hard question.
---

# Convene the committee

The user wants a committee of AI models to deliberate. The committee runs Karpathy's LLM
Council: **Stage 1** every seat answers independently, **Stage 2** seats rank each other's
anonymous answers, **Stage 3** a chairman writes the final answer. The plugin runs the stages;
your job is the brief before and the presentation after.

## 1. Frame the question

- Take the question from the user's message (text after the command, or the conversation).
- If the question is too vague to answer well (no options, no goal, no context), ask **one**
  short clarifying question first. Otherwise proceed — do not interrogate the user.
- Write a **brief**: goal, constraints, decision criteria, and anything the user already said.

## 2. Build the evidence pack

Seats have no tools. They reason only over what you give them, so the evidence pack is
what makes the answers grounded:

- Include facts the user supplied (attachments, numbers, logs, prior messages).
- If the question depends on current or external facts and you have research tools, do a
  short, focused search and paste the relevant excerpts **with their sources**.
- Never invent evidence. If something important is unknown, say so in the pack.
- Keep it under ~15,000 characters; summarize long documents and keep key numbers verbatim.

## 3. Choose the committee

- Default: omit `preset` and `template` (the user's default preset is used).
- If the user names a preset or template (`classic`, `expert-panel`, `decision-board`,
  `red-team`, `reliability-board`), pass it.
- If the user describes roles ("a structural engineer, a lawyer and a CFO"), pass them as
  `seats`, each with a `name` and specific `instructions` written from the user's wording.
- `review: false` only if the user asks for a quick/lite run.

## 4. Run it

1. Call `committee_open` with `question`, `brief`, `evidence` and any choices from step 3.
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
answer, and still deliver whatever report paths exist. When `cancelled`: confirm it stopped.

## Rules

- This is advisory output. For safety, legal, medical or financial decisions, say that a
  qualified person must review it.
- Do not run a committee for simple lookups or chit-chat; answer those yourself.
- Do not re-run a committee to get a different answer unless the user asks.

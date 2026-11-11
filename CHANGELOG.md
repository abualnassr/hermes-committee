# Changelog

## 0.4.0

- **Thinking levels per step.** The seats' answers and the verdict ask for medium reasoning; the clerical
  steps (searches, passages, photo notes, blind vote, the dispute list, debate replies) ask for low. A seat's
  *Thinking for the answer* and the chairman's *Thinking for the verdict* change their own step (Low, Medium,
  High, Model default). The level rides on three plugin auxiliary tasks, so the seat's provider and model are
  unchanged; a provider that rejects the level is retried without it and remembered. Each call's level is in
  the run record, and Time & cost shows it per seat. With a reasoning-heavy chairman, listing the
  disputes took 45 s instead of 229 s in one test.
- **Research works on a multi-profile gateway.** The parallel searches and page reads now run in a copy of
  the caller's context, so Hermes's web tools can read the profile's search and extract keys. Before, a
  gateway serving more than one profile refused those reads in the worker threads, every page came back
  unread and the committee ran with no web sources. When pages cannot be read, the note now gives the
  real reason instead of a bare "could not be read".
- **Only what you give reaches the seats.** A run convened from the page now uses exactly the page's
  question, evidence and files; a brief, evidence text or files the chat agent adds are ignored (and
  noted in the run). Before, the agent could add facts from its memory of the user to the brief,
  and they went to every seat's provider. The convene skill and the tool description tell the agent
  never to add facts from memory in chat runs either, and the Decision page shows any background
  the seats got, under the question.
- **Seats keep to their roles.** A seat's instructions now come first, as their own block, with one
  rule: stay within your role and say in one line when a point falls outside it. The answer fields
  for risks and evidence gaps accept "outside my role", so the fixed answer format no longer pulls
  every seat toward the same coverage. The debate uses the same block. No extra calls.
- **The chairman sees the roles** and weighs each view mainly within its role, and notes what no
  member covered. The blind vote still sees no roles (they would reveal the author).
- **Role-fit note.** In the same verdict call the chairman marks each seat with instructions as kept
  or drifted, with a reason: an amber "!" on the seat in "How the table stood", a tag in "Answers
  ranked", a line in the seat's answer and in the Markdown and PDF reports. Optional: a verdict
  without it still works.
- **Research first** (off by default; saved with a committee; `research: true` in chat,
  `--research` in the terminal). Before the seats answer, the chairman's model writes 2–4 searches;
  Hermes's own `web_search` and `web_extract` tools (the search provider you set up) find and read
  up to 6 pages (one per site first), all searches at once; the plugin splits each page into numbered
  passages and the chairman's model picks up to 8 by number, so the text always comes word for word
  from the page and the same passage cannot appear twice. At most 2 per site and about 2,300 tokens
  in all. They join the evidence as [W1]… with title, site and date, marked unverified and
  untrusted for every seat, the vote and the chairman. Only the question and evidence file names are
  used for the searches; file contents never leave. Page runs pause up to 30 s for a source review
  (untick any; no answer keeps all; paused time does not count). **Time allowed** for research is
  your choice (45 s, 1½ min by default, or 2½ min; `research_secs`, `--research-secs`), saved with
  the committee: research stops there and the committee goes on with what it found. Research counts
  against the time limit and budget; it is skipped with a note when it does not fit. Every failure (no search
  provider, nothing found, pages blocked) is soft: the committee goes on from your evidence. New
  Research step and teal timeline bar on the Convene page, a Web sources tab on the Decision page,
  and a web sources list in the reports.
- **The table shows where the evidence goes.** The Convene table is redrawn: seats with glowing
  progress rings, curved spokes that light up while a seat works, an answer's first line beside the
  seat when it lands, ballots as diamonds, the chairman's bench filling as it writes, and the decision
  under the bench with a ripple. An evidence box sits over the table, in page text so it stays
  readable at any window size: your files, photos and typed notes as pills, web sources in a second
  row (they rise from the chairman's bench when research finishes), "+N more" when there are many. Every model call that takes evidence
  draws it from the box's outlet: each seat for its answer and its ballot, the debating seats for each
  reply, the chairman for the agenda and the verdict. Only real calls animate. While the chairman
  writes, the seats dim and say "waiting for the verdict"; seats outside a debate say "not in the
  debate".
- **One button system.** Every button on the page is one of six kinds (main, secondary, tinted,
  quiet, danger, choice chip) in one outlined style that lights up in the theme accent, with a
  keyboard focus ring, a neutral disabled state and a spinner while convening. Reduced motion turns
  the sheen and pill animations off.

## 0.3.0

- **New Committee page: Brief, Convene, Decision.** Three steps along the top.
  *Brief*: the question and evidence on the left; on the right the committee type, a small table
  (click a seat), a seat table with an inline editor per seat (role, model, instructions with a
  1,200-character counter, starter chips, duplicate/remove) and for the chairman (model, brief,
  answer/decision), then blind vote, deliberation, time limit and budget with an estimate bar.
  *Convene*: members on an arc with the chairman's bench below. The question travels up each seat's
  line; a ring spins while a seat's call is out; answered seats show their first line; during the
  blind vote the seats show their letters and ballots travel along the arc as each one lands; sparks
  run between seats that debate; the verdict travels down to the bench. Beside it: the stages with
  counts, time/cost/calls/tokens gauges, a "Now" panel, a timeline with one bar per model call, a
  live log and the vote matrix filling ballot by ballot. *Decision*: the ruling with confidence,
  six figures (agree, dissent, debate, time, cost, calls), Why / Do next / What would change this,
  how the table stood (green in line, amber dissent), the blind ranking and matrix, and tabs for
  answers, debate, dissent, insights, time and cost, agreement and the full answer.
- **Everything shown comes from the run record.** The engine now records each answer's summary,
  each ballot and each debate reply as it lands (not only at the end of the stage), so the page can
  show them live. Nothing is invented: models do not report their thinking, so the log lists only
  answers, ballots, positions and the verdict. The ring around an answering seat is an estimate from
  that model's usual time. Tokens and cost update as each call finishes; subscription calls count $0
  and calls with no known price show "+", never $0.
- **Extend the time limit or budget during a run** (runs convened from the Committee page). When
  70% of the time or budget is used, a box offers +5/+10 min or +$1/+$2 without stopping anything
  ("Not now" hides it). When a stage would be skipped or cut (the blind vote, the debate, a debate
  round, the verdict), the run pauses up to 45 s for your answer; paused time does not count against
  the limit; no answer or Decline continues exactly as before, and after a decline it does not ask
  about that limit again. Caps stay 60 min and $500. Extensions, declines and pauses appear in the
  live log and in both reports. Runs started from chat never pause.
- **The timeline spans the time limit** set in the brief: bars start at the left and fill toward the
  marked limit; without a limit it uses round lengths that grow in rare steps.
- **Replay at 10×** rebuilds a finished run from its saved timings, ballots and positions.
- **Seat colours stay neutral until the verdict**, then show who was in line and who dissented.
- **Next actions with owner and timing.** The chairman lists actions with an owner and a "when"
  when the question or evidence names them (never invented); the Decision page shows them as a
  table, with "—" for unknowns. Reports and older runs keep the plain list.
- **Follows the Desktop theme**: colours come from the theme's tokens; light or dark sets the
  stage colours. Respects reduced motion.

- **Your own chairman brief and mode.** Click ⚖ Chairman to edit the instructions for the verdict
  (pre-filled with the committee type's brief, with a reset link) and choose whether the committee
  gives **an answer** or **a decision** (decision: every seat must end with a clear recommendation).
  Both are saved with the committee, kept by follow-ups, settable from chat (`chairman_guidance`,
  `decision_focus`) and in presets, and shown in the reports. The five types stay as starting points.

- **Executive summary on page 1 of the PDF** (always exactly one page) for a reader who saw nothing
  else: the decision and confidence, a plain-words bottom line, the 3 strongest reasons, the top 3
  next actions, what would change the decision, one line on how the committee stood (answers,
  dissent, debate, top-ranked seat, time, cost) and a clickable contents line with page numbers.
  The chairman writes the bottom line and reasons in the same verdict call (`executive_summary`, no
  extra call); older runs get one assembled from the verdict. Follow-ups show one "Since the earlier
  run" line. The former summary page is now "Decision in detail". The Markdown report opens with the
  same summary and the Desktop verdict shows the bottom line under the decision.

- **Deliberation** (optional stage between the blind vote and the verdict). The chairman lists up to
  three disputed points and tags each as about the action, about a claim the decision rests on, or
  emphasis; the plugin decides from the **Deliberate** setting whether to debate (`material` default:
  action or claim; `action`; `always`; `never`). In each round (1 or 2) every seat holds, changes or
  partly changes its position per point with an argument and evidence; round 2 covers only open
  points. The chairman says which argument decided each point (`disputes_resolved`). Positions before
  and after and "who changed their mind" are computed by the plugin. Time limit and budget keep the
  chairman's share first and skip the debate when it does not fit. Convene-bar chip, live step,
  Deliberation tab, verdict chip, insight, PDF/Markdown section and summary line, chat
  (`deliberate`, `deliberate_rounds`), CLI (`--deliberate`, `--rounds`), presets and follow-ups.

- **Time limit for the whole run** (default 10 minutes; 0 = no limit; max 60). Set it on the
  Convene bar (⏱), per preset (`max_minutes`), in settings, in chat, or with `--max-minutes`.
  The chairman's time is reserved (sized from the slowest recent runs on that model); when time
  runs short, late seats are left out and the blind vote is skipped instead of the run failing.
  If the chairman itself runs out of time the run ends with "No verdict" and keeps the answers.
  The live view counts down; the verdict and both reports state the limit and any skipped stage.
- **Evidence files, folders and photos.** Add files, choose a folder, drop files or paste text.
  Text formats, PDF (scanned pages read as pictures), Word (.docx), Excel (.xlsx) and photos; up to
  25 MB per file, 60 files, 40 pictures. Files are read once and numbered for citation ([E1]…, [I1]…).
  Each file shows its size and tokens, and the box shows the total for the run before Convene.
- **Photos go to every seat with a written description**; a seat whose provider refuses the picture
  is asked again with the description only and the model is remembered as text-only (overridable per seat).
  A vision model in the committee describes each picture once for the other seats, the blind vote
  and the chairman. A committee with photos but no vision model is refused before any call, and
  evidence larger than a seat's context window is refused with the seat named.
- Chat: `evidence_files` (paths of attached files). CLI: `--evidence-file` (repeatable) and
  `--evidence-dir`. Reports list the files and each picture's description.
- New dependencies: `pypdf`, `openpyxl` (Pillow already ships with Hermes).
- **Budget in USD for the whole run** (default $2; 0 = no limit). Set it on the Convene bar (💲),
  per preset (`max_usd`), in settings, in chat, or with `--max-usd`. Cost is estimated from token
  counts and the list prices in Hermes's model catalog; seats on a subscription count $0 (their
  list-price equivalent is shown). The bar shows a typical-to-worst-case range before Convene.
  Every stage is checked at its worst case: answers are shortened to fit, the blind vote is skipped
  if it no longer fits, the chairman's share is kept back, and a run that cannot fit is refused
  before any call. Calls cut off by the time limit are counted at their maximum. Models with no
  known price need *Run anyway*. Reports show the estimated cost, the budget and a cost table by stage.
- **Re-convene with a follow-up** (verdict): the same committee answers a new question with the
  earlier verdict as background and the same evidence (picture descriptions reused). Reports link
  the follow-up to the earlier run. Chat: `follow_of`; CLI: `--follow-of`.
- Re-convene: **add new or missed evidence** in the follow-up dialog (added to the earlier files).
- **💾 Save committee** (Convene bar and verdict): keeps seats, models, instructions, photo settings,
  chairman and limits under any name; reuse it with a new question and new evidence.
- The verdict, dissent and reports use the seats' names instead of the anonymous letters (also for
  earlier runs).
- Fix: Convene now starts the background chat in the plugin's own profile (it fell back to opening
  a chat when Desktop's main profile was a different one).
- Vision and context limits are learned from earlier runs instead of read from Hermes internals. Evidence that was too long for a
  model before is flagged on the Convene bar.
- **Subscription seats are not priced**; API seats always are. Hermes detects how each provider is
  paid for and prices API seats from its own pricing tables (one compatibility file,
  `committee/_hermes_compat.py`, falls back to the public model list if Hermes changes). A
  pay-per-use model with no price asks for yours in the 💲 budget menu; "Run anyway" is gone.
  The detected billing can be corrected per provider.
- Reports say where they came from: "Hermes Committee" above the title, in the page header, and an
  "About this report" note (version, project page, date) at the end of the PDF and Markdown report.
- **PDF report redesign**: a one-page summary first (decision, confidence, key figures, what would
  reverse it, next actions); for follow-ups a **What changed since the earlier run** box with the
  chairman's reason; the committee page with a vote chart, the blind ballots and **What the run
  reveals**; seats' full answers, the evidence (with an ID column; photos listed with their descriptions, not embedded) and the cost
  in appendices. Seats are named everywhere, citations such as [E3] link to the evidence, and the
  PDF has bookmarks. Fix: "Response Response A" in dissent, and the earlier run's background no
  longer pasted into a follow-up's report.
- **Committee name** (optional): shown in History, on the verdict, on the reports and as the chat
  title. Chat: `title`; CLI: `--title`.
- **Actual run time** on the verdict and in the reports.
- **Clear all** in the evidence box.
- **Delete preset.** Presets saved on the page are now stored in the plugin's data folder (Hermes's
  settings writer cannot remove keys); `config.yaml` presets are still read, and Delete hides them.
  Presets now also restore their budget.
- **Convene keeps you on the page**: the committee runs in its own background chat, named after
  the committee. Falls back to opening the chat on Hermes versions that cannot do this.
- "Ask a follow-up" is now **Discuss in chat** (one model answers there, not the committee).
- Chat: a plain-words request with attached files ("Convene a committee: … use the attached files")
  is the documented path, because Hermes Desktop refuses a slash command sent with attachments.
- Time limit: answers now get all the time except the chairman's; the blind vote is decided after
  the answers on the time actually left (a slow past vote no longer cuts a seat short).

## 0.2.0

The Committee page in Hermes Desktop: one screen to set up, run and read a committee.

- Sidebar entry and command-palette command: **Committee**.
- **Set up**: type the question and optional evidence, pick a committee type or a saved preset,
  then click any seat to set its role, its **AI model** (searchable list of every provider
  connected to Hermes) and its **instructions** (with one-click starters). Add, remove or
  duplicate seats (2–8) and choose the chairman's model. Shows the number of model calls and
  warns when every seat uses the same model.
- **Convene** stores the set-up as a draft and sends `/hermes-committee:convene draft=<id> …` in a
  new chat, so every run is a visible chat turn; the page then follows the run. If the chat cannot
  be reached, the request is typed in (or copied) for the user to send.
- **Permissions**: providers or models Hermes has not allowed for the plugin are marked 🔒; Convene
  shows the exact `hermes config set` commands with a Copy button. The plugin never changes them.
- **In session**: blind seats ("Response A–E") with progress, the three stages, a token meter and Stop.
- **Verdict**: the decision first (confidence, dissent, what would reverse it), seats on a podium
  by peer rank, each seat's answer on click, and tabs for ranked answers, next actions, dissent,
  blind ballots and the chairman's full answer. Replay, Show PDF, Ask a follow-up.
- **History** menu with search, and **Model standings** across runs.
- First-run 3-step guide, a Help window, and a README rewritten as a step-by-step guide with
  troubleshooting.
- `committee_open` accepts `draft`; a page run uses exactly the page's seats and chairman.
- **Save as preset** writes through Hermes's plugin-settings writer.
- Engine records live per-seat progress; runs are saved and announced (`plugin.hermes-committee.run.updated`)
  on every seat update.
- Backend routes under `/api/plugins/hermes-committee/` (`dashboard/plugin_api.py`).
- `hermes committee run` (headless) and `hermes committee report <run-id>` (re-render, no calls).
- Default stage time limit 360 s; default `max_tokens` 8192; report paths use forward slashes;
  a missing PDF says why and how to recover it.

## 0.1.0

First version.

- Three-stage committee: independent answers, anonymous peer ranking with per-reviewer shuffling
  and identity scrubbing, and a chairman synthesis that keeps dissent visible.
- Templates: classic, expert-panel, decision-board, red-team, reliability-board.
  Custom seats can also be named in chat.
- Per-seat provider and model through `ctx.llm`, gated by the user's allowlist.
- One stage per tool call; quorum, a time limit per stage, and fallback when structured output fails.
- Stop-to-cancel through the `post_tool_call` and `on_session_end` hooks.
- Markdown and PDF report (neutral design, right-to-left Arabic), plus a JSON record of each run.
- `/hermes-committee:convene` skill, and the `hermes committee templates|presets|runs|doctor` CLI.

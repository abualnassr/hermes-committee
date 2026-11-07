# Hermes Committee

**Karpathy's [LLM Council](https://github.com/karpathy/llm-council) for [Hermes Agent](https://hermes-agent.nousresearch.com),
with seat roles, a shared evidence pack, Stop-to-cancel, and a full committee report (Markdown + PDF).**

Ask a hard question. Several AI models answer it independently, rank each other's anonymous
answers, and a chairman model writes the final answer. You get the verdict in chat and a report
showing what every seat said, how they ranked each other, and where they disagreed.

```
/hermes-committee:convene Should we rewrite our 10-year-old billing system or modernize it in place?
```

## How it works

| Stage | What happens | Same as Karpathy's council? |
|---|---|---|
| 0. Brief | Hermes frames the question and gathers an **evidence pack** (your files, numbers, short research with sources). | Added |
| 1. Answers | Every seat answers **independently and in parallel**. A seat can have a role and instructions ("You are a structural engineer…"). | Yes, roles optional |
| 2. Peer ranking | Every seat reviews and ranks **all answers, anonymized** as Response A, B, C… Each reviewer sees them in a **different shuffled order**, and names and model ids a seat wrote about itself are scrubbed. | Yes, plus shuffling and scrubbing |
| 3. Chairman | A chairman model writes the final answer from the answers, critiques and rankings. It must **keep dissent visible**, and it never sees which model wrote which answer. | Yes, plus dissent rule |
| Report | Markdown + PDF: verdict, roster, ranking matrix, agreement and dissent, one page per seat, method and usage. | Added |

The **classic** template is the original council: no personas, the same three stages.

Each stage is its own tool call, run by plugin code. The model cannot skip a stage or see
identities it shouldn't. No call gets near Hermes's 420-second tool limit.

## Install

```bash
hermes plugins install abualnassr/hermes-committee
hermes plugins enable hermes-committee
```

Enabling asks you to approve:

- two Python packages: `fpdf2` and `uharfbuzz`, for the PDF report;
- two capabilities: `llm.provider_override` and `llm.model_override`, so each seat can use a different model.

Requires Hermes **0.23.0** or newer. After enabling:

- run `hermes gateway restart`. A running gateway never switches to a new dependency environment,
  so PDF reports only work after the restart. Restarting the Desktop app is not enough, because the
  gateway is a separate process;
- open a new session, so the tools load.

If a run finishes without a PDF, its result says why. `hermes committee report <run-id>` creates
the PDF from the saved run, with no new model calls.

## Quick start (zero configuration)

With no configuration, the committee is the **classic** template: four seats plus a chairman,
all on your **active model**. That works, but one model talking to itself gives correlated
answers, and the plan says so. For real independence, give the seats different models.

## Use different models per seat

**1. Allow the providers.** Hermes only lets a plugin choose a provider and model when you allow
it. Approving the capabilities at enable time sets the two `allow_*` flags. Add an allowlist so
the committee can only use providers you approve:

```yaml
plugins:
  entries:
    hermes-committee:
      llm:
        allow_provider_override: true
        allow_model_override: true
        allowed_providers: [openai-codex, anthropic, openrouter, deepseek]
```

A seat whose provider is not on the list fails with a clear error, and the run continues if
the quorum is still met.

**2. Define a preset:**

```yaml
plugins:
  entries:
    hermes-committee:
      settings:
        default_preset: my-board
        presets:
          my-board:
            template: decision-board          # roles come from the template, by position
            chairman: {provider: anthropic, model: claude-opus-4}
            seats:
              - {provider: openai-codex, model: gpt-5}
              - {provider: openrouter, model: google/gemini-3-pro}
              - {provider: deepseek, model: deepseek-chat}
              - {provider: anthropic, model: claude-sonnet-4, name: "Finance", instructions: "Judge cost and ROI."}
            review: true      # false = skip Stage 2 (lite)
            quorum: 3         # minimum seats that must answer
```

**3. Check it:**

```bash
hermes committee presets          # what each preset resolves to
hermes committee doctor           # one tiny call per seat to prove every route works
```

## Templates

| Id | Seats | Use for |
|---|---|---|
| `classic` | 4 members, no persona | Karpathy's original council |
| `expert-panel` | Domain Expert, Practitioner, Researcher, Skeptic | Technical questions |
| `decision-board` | Strategist, Risk Officer, Operator, Finance | Choosing between options |
| `red-team` | Pre-mortem, Assumption Auditor, Adversary, Edge-case Hunter | Stress-testing a plan or claim |
| `reliability-board` | Mechanical Eng., Reliability Eng., Operations & Cost, HSE & Compliance, Red Team & Evidence Auditor | Asset and maintenance decisions |

You can also name the roles in chat. For example: *"convene a committee with a structural engineer,
a planning lawyer and a CFO"*. Hermes passes them as custom seats with instructions, and the
models are taken from your preset by position.

## The report

Each run is stored under `<HERMES_HOME>/plugin-data/hermes-committee/runs/<run-id>/`:

- `report.pdf`: neutral A4 layout with verdict, roster, ranking matrix, dissent, final answer, one page per seat, and method
- `report.md`: the same content, searchable and copyable
- `run.json`: the complete record of the run (inputs, every answer, every ranking, timings, token usage)

Paragraphs containing Arabic script are set right-to-left. `hermes committee runs` lists recent runs.

## Stop

Pressing Stop in the chat cancels the run within about a second. No new model calls start
after Stop. Calls already in flight cannot be recalled: the provider finishes them and may bill
for them, but their results are discarded. A cancelled run stays cancelled; ask again to start
a new one.

## Privacy, cost and risk (please read)

- **Your question, brief and evidence pack are sent to every seat's provider and to the
  chairman's provider.** With four seats on four providers, the same data reaches four companies.
  Use `allowed_providers` to keep sensitive work on providers you trust.
- **One run makes N + N + 1 model calls**: N seats answer, N seats review, and one chairman
  call. Reasoning models can use many tokens. Token counts are recorded for every call. Cost is
  shown only when the provider reports it; otherwise the report says "unknown" rather than
  guessing.
- Reports are written **locally only**. The plugin has no telemetry, makes no network calls of
  its own, and never reads API keys. All model calls go through Hermes's `ctx.llm` with your
  existing credentials.
- **Advisory only.** Committee output is not professional, engineering, legal, medical or
  financial approval, and every report says so.

## Limitations

- Seats have **no tools** of their own. They reason over the evidence pack Hermes gathers before
  the run, so every seat judges the same evidence.
- Arabic copied out of the PDF can come out garbled. It renders correctly on the page, but use
  `report.md` when you need to copy text.
- The plugin runs inside the Hermes process. It cannot run under `plugins.isolation: host`
  because the live progress message uses `ctx.post_message`.

## How this differs from `hermes-council`

[`girayk/hermes-council`](https://github.com/girayk/hermes-council) also ports Karpathy's council.
This is a separate implementation. Hermes Committee:

- uses only public plugin APIs (`ctx.llm`, tools, hooks, a bundled skill) and adds no model
  provider to your model menu;
- runs one stage per visible tool call, with live progress and Stop-to-cancel;
- adds per-seat roles and instructions, templates, the evidence pack, and the full report.

## Development

```bash
cd tests
uv run --with "pytest>=8,<10" --with "fpdf2>=2.8,<3" --with "uharfbuzz>=0.39,<1" python -m pytest -q
hermes plugins validate .
```

The core (`committee/`) has no Hermes imports. The tests drive the whole pipeline with a fake
LLM, covering: anonymity guards, per-reviewer shuffling, quorum, deadlines, cancel,
structured-output fallback, the reports, and the tool/hook glue.

## Credits and license

- Council concept and the three-stage protocol: [karpathy/llm-council](https://github.com/karpathy/llm-council).
- Fonts: Noto Sans and Noto Naskh Arabic, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
- Code: MIT (see `LICENSE`).

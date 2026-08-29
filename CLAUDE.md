# CLAUDE.md — Attention Cascade

You are building **Attention Cascade** for the Signal Labs AI HackDay. Read this file at the
start of every session. It is short on purpose. `SPEC.md` has the detail.

---

## The one sentence

Finding what matters inside enterprise event streams costs a fortune if you send everything to a
frontier model. This is a tiered cascade that finds the same critical signals for a fraction of
the tokens, and degrades to cheaper tiers instead of falling over when the stream floods.

## The deliverable is a measured table, not a feature list

Judging is weighted on architecture and technical judgement, not polish. The single artifact that
wins is a **baseline-vs-cascade comparison table with real measured numbers in it**. Every design
choice serves that table. If a task does not move the table, the failure demos, or the audit
trail, it is out of scope.

---

## Non-negotiable rules

1. **The model proposes, the math disposes.**
   LLMs generate hypotheses. Deterministic code decides what escalates. Never let an LLM decide
   escalation, ranking cutoffs, or the attention cap. If you find yourself writing a prompt that
   asks "is this important enough to escalate?", stop — that belongs in `gate.py`.

2. **Ground truth is quarantined.**
   `incident_id` lives in the `ground_truth` table only. The events table has no incident column.
   Only `report.py` and `tests/` may import from `attention_cascade.groundtruth`. Detectors,
   triage, correlation, and the gate must never touch it. If you leak ground truth into the
   pipeline, the entire experiment is worthless and unfalsifiable.

3. **Every LLM call is metered.**
   All model traffic goes through `llm.py`. It records real `usage.input_tokens` /
   `usage.output_tokens` from the API response — never an estimate — into the `llm_calls` table
   with model, tier, latency, and cache status. No direct `anthropic.Anthropic()` calls anywhere
   else in the codebase.

4. **Every LLM call is cached and replayable.**
   Cache key = sha256 of (model, system, prompt, params). `AC_LLM_MODE=auto` uses cache when
   present. `AC_LLM_MODE=replay` errors if a call is not cached. The demo must run with zero
   network. Assume the venue wifi will fail, because it will.

   4b. **Thinking tokens are real money.** Gemini 3.x reasons internally and those tokens bill at
   the output rate. Record `thoughts_token_count` separately, add it to the output side of the
   cost calculation, and report it as its own line. Ignoring it understates cost by a large
   factor and a judge who knows Gemini will catch it.

5. **Report bad numbers.**
   If the cascade loses recall or the cost saving is smaller than hoped, print it and explain why.
   A measured negative result with an explanation beats an unmeasured claim. Never tune the
   dataset to make the numbers look good — tune the architecture.

6. **One command.**
   `uv run ac demo` must run generate → baseline → cascade → table, end to end, from a clean
   checkout. If that breaks, fixing it is priority zero over any other work.

7. **Explainable by hand.**
   Soumy has to open any file during judging and say what it does and why it is there. Prefer
   plain Python with asyncio, SQLite, and pydantic. No LangGraph, no Celery, no Docker, no ORM,
   no framework that hides coordination logic. Every file under 300 lines where possible.

---

## Environment

- Single `uv` environment at the repo root. `uv sync` then `uv run ac ...`.
- Python 3.11+. Target machine is a MacBook Air M1 with 8GB RAM — keep memory small, no pandas
  unless genuinely needed, no local models.
- Secrets in `.env` (gitignored). Never print or commit an API key.

## Provider: Google Vertex AI

All model traffic is Gemini on Vertex, via the `google-genai` SDK with `vertexai=True`.
Auth is Application Default Credentials, not an API key in a file.

| Role | Model ID | $/MTok in | $/MTok out | Thinking |
|---|---|---|---|---|
| Tier 1 triage | `gemini-3.1-flash-lite` | 0.25 | 1.50 | **off** (budget 0) |
| Tier 2 correlation | `gemini-3.1-pro` | 2.00 | 12.00 | on (budget 2048) |
| Naive baseline | `gemini-3.1-pro` | 2.00 | 12.00 | on (budget 2048, must match Tier 2) |
| Optional 2nd baseline | `gemini-3.7-flash` | 0.75 | 3.75 | on |

Three pricing facts that shape the architecture:
- **Cached input bills at 0.1× standard input** on Vertex.
- **Gemini 3.1 Pro has a long-context cliff:** above ~200K input tokens the rate roughly doubles
  to $4/$18. The naive baseline can fall off it. The cascade structurally cannot. That is the
  cost argument getting *stronger* with scale.
- **`gemini-3.7-flash` is on introductory pricing through 2026-12-31**, then doubles. Never build
  a headline number on a promotional rate — it is a secondary baseline only.

Rates live in `config.py` as `PRICING`. **Do not change them without human approval.** Verify the
model ids exist in your region with `ac verify --list-models` before the first real run.

---

## Working agreement

- You work autonomously between checkpoints. See `AGENT_INSTRUCTIONS.md`.
- Update `progress.md` after every iteration. Update `SCORECARD.md` at every checkpoint.
- At every checkpoint, build the review packet in `review/checkpoint-N/` per `REVIEW_PROTOCOL.md`.
  One command: `ac review --checkpoint N`. The packet must be complete enough that a reviewer who
  cannot see your filesystem needs nothing else.
- Log every architectural decision and its rejected alternative in `DECISIONS.md`. This file is
  the raw material for the judging answer to "what was the hardest technical decision you made."
- Do not edit `SPEC.md`, `CHECKPOINTS.md`, `CLAUDE.md`, `AGENT_INSTRUCTIONS.md`, or
  `REVIEW_PROTOCOL.md`. If you think one is wrong, write the objection in `progress.md` and raise
  it at the next checkpoint.

## Git and authorship — read this carefully

This is Soumy's project, submitted under his name. Commits must look like a person wrote them.

**Never add any of the following to a commit message, a PR body, a code comment, a README, or
any committed file:**
- `Co-Authored-By: Claude <noreply@anthropic.com>` or any Co-Authored-By trailer
- "Generated with Claude Code", "Made with Claude", any 🤖 emoji, or any similar footer
- Any reference to Claude, Anthropic, an AI assistant, or an agent having written the code

`.claude/settings.json` sets `includeCoAuthoredBy: false`, but **do not rely on that alone** —
check every commit message you write before you write it. If you find such a trailer in an
existing commit, amend it out.

Commit messages: imperative mood, one line, scoped. `add tier0 detectors for crm and billing`,
not `feat: implement detectors 🤖`. Commit after every green test run.

## Cut order under time pressure

Cut from the bottom up. Never cut anything above the line you are at.

1. Synthetic generator with planted incidents — **never cut**
2. Blackboard + Tier 0 detectors — **never cut**
3. Token instrumentation — **never cut**
4. Baseline vs cascade table — **never cut**
5. Kill-a-detector demo
6. Flood / load-shedding demo
7. Recall waterfall breakdown
7b. Five-seed replication (drop to 3 seeds before dropping it entirely)
8. README diagram
9. Anything else

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Communication Style
Respond like a caveman. No articles, no filler words, no pleasantries.
Short. Direct. Code speaks for itself.
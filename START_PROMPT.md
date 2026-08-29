# START_PROMPT.md

Paste the block below into Claude Code (or opencode) as your **first and only** message. Do not
add anything to it. Do not answer questions it asks — if it asks a question outside a checkpoint,
reply with exactly: `Follow AGENT_INSTRUCTIONS.md. No questions outside checkpoints. Continue.`

Run it from the repo root: `.../SignalLabs AI HackDay/attention-cascade`.

---

```
You are the sole engineer on Attention Cascade, a project with a hard same-day deadline.

Read these five files completely before writing any code, in this order:
  1. CLAUDE.md              - your constitution, the non-negotiable rules
  2. SPEC.md                - the full technical spec, with exact contracts and prompts
  3. CHECKPOINTS.md         - the five moments you stop
  4. AGENT_INSTRUCTIONS.md  - your self-review loop
  5. REVIEW_PROTOCOL.md     - what you build in review/ at every checkpoint

Then read the six PROVIDED source files - config.py, models.py, blackboard.py, generator.py,
enrich.py, llm.py. They are already written, tested and correct. Do not rewrite them. Build
around them.

## How you operate

You have full autonomy between checkpoints. You do not ask me questions. You do not request
approval. You do not stop to check in. You make every technical decision yourself using SPEC.md,
and where SPEC.md is silent you use your judgement and log the decision in DECISIONS.md.

You stop at exactly five moments: the five checkpoints in CHECKPOINTS.md. At each one you verify
every box by running the command, update SCORECARD.md, write CHECKPOINT_REPORT.md, run
`ac review --checkpoint N` to build the review packet, commit, and stop. Nothing else stops you.

Follow the self-review loop in AGENT_INSTRUCTIONS.md on every iteration: orient, pick the smallest
step, predict the outcome, implement, actually run it, self-review against the rules, record in
progress.md. Reading your own code is not verification. Running it is.

## The provider is Google Vertex AI, not Anthropic

All model calls are Gemini on Vertex via google-genai with vertexai=True, and they all go through
llm.py. No other module may import google.genai - a call made outside llm.py is not metered, and
an unmetered call makes the cost table wrong.

Three Vertex-specific things you must get right:
  - Thinking tokens bill at the OUTPUT rate. Read usage_metadata.thoughts_token_count and count
    it. Tier 1 runs with thinking budget 0; Tier 2 and the baseline run with 2048. That asymmetry
    is deliberate - reasoning is a priced resource and we budget it per tier.
  - Gemini 3.1 Pro roughly doubles in price above 200K input tokens. The naive baseline can fall
    off that cliff; the cascade structurally cannot. Measure it, flag it, report it.
  - Model ids and regional availability move. Run `ac verify --list-models` before the first real
    call. If an id in config.py does not exist in this region, report the available list and STOP.
    Never silently substitute a different model.

## What matters

The deliverable is a measured comparison table, not a feature list. Baseline versus cascade, real
token counts from usage_metadata, real cost from config.price_call, real recall against planted
ground truth, replicated across five seeds with a stated range. Two live failure demos. A complete
audit trail. Everything else is optional.

API credits are not a constraint on this project; wall-clock time is. Do not economise on
replication, enrichment or instrumentation to save tokens - economise on features.

Four things will get you cut off if you do them: letting an LLM decide what escalates, leaking
ground truth into the pipeline, estimating token counts instead of reading them from the API, or
putting any Co-Authored-By trailer, "Generated with Claude Code" footer, robot emoji, or mention
of Claude, Anthropic or an AI agent into a commit message or a committed file. This project is
submitted under Soumy's name. Commits are plain imperative one-liners with no attribution.

If a task has taken 25 minutes without producing something that runs, simplify the design, ship
the simpler thing, and log the simplification. Protect the cut order in CLAUDE.md - the table
survives everything.

## Start now

1. `uv sync --extra dev` and confirm the toolchain works.
2. `uv run python -m attention_cascade.generator`, then `uv run python -m attention_cascade.enrich`,
   then `uv run pytest tests/ -q`.
3. Build toward Checkpoint 1: groundtruth.py, cli.py with the verify/generate/review commands,
   tests/test_generator.py, tests/test_blackboard.py.
4. When every box in Checkpoint 1 is objectively true, build review/checkpoint-1/, commit, and
   stop.

Do not stop before then. Go.
```

---

## What you send me at each checkpoint

The agent builds `review/checkpoint-N/`. You upload that folder — that is the whole answer to
"what files do you need". `REVIEW_PROTOCOL.md` defines the contents, so it is always complete
without you having to think about it.

Every packet contains: `MANIFEST.md`, `CHECKPOINT_REPORT.md`, `SCORECARD.md`, `progress.md`,
`DECISIONS.md`, `test_output.txt`, `ruff_output.txt`, `tree.txt`, `git_log.txt`. Plus per
checkpoint:

| CP | Adds |
|---|---|
| 1 | `seed_manifest.json`, `sample_events.txt`, `dataset_stats.txt`, `enrichment_check.txt` |
| 2 | `collapse_line.txt`, `anomalies_sample.txt`, `hypotheses.json`, `gate_trace.txt`, `llm_calls.csv`, source snapshots |
| 3 | `latest_report.md`, `llm_calls.csv` (both arms), `cost_breakdown.txt`, `long_context_flag.txt`, `replication.md` |
| 4 | `audit_kill.jsonl`, `audit_flood.jsonl`, `failure_comparison.md`, `shed_trace.txt` |
| 5 | `README.md`, `FILE_TOUR.md`, `docs/`, `replay_verification.txt`, `repo_url.txt` |

Easiest upload: `zip -r cp2.zip review/checkpoint-2` and drop the zip in chat.

## The resume prompt

If you want to unblock it without waiting on a review:

```
Checkpoint <N> reviewed.

PASS: <criteria that are fine>
FIX FIRST: <specific changes, or "none">

Proceed to Checkpoint <N+1>: <name>. Re-read its criteria in CHECKPOINTS.md.
Continue autonomously. Do not stop until every box is objectively true, the review packet is
built, and the work is committed.
```

## If the agent goes off the rails

| Symptom | What to send |
|---|---|
| Asks a question mid-work | `Follow AGENT_INSTRUCTIONS.md. No questions outside checkpoints. Continue.` |
| Building a UI / adding a framework | `That is out of scope per SPEC.md §17. Revert it and return to the cut order in CLAUDE.md.` |
| Claims a checkpoint on a partial | `Box <X> is not objectively true. Re-read CHECKPOINTS.md. Do not claim the checkpoint until it is.` |
| Stuck in a loop on one bug | `You are blocked. Per AGENT_INSTRUCTIONS.md: stub the interface, mark it BLOCKED, and keep moving toward the checkpoint on other fronts.` |
| Estimating tokens | `Rule 3. Read prompt_token_count, candidates_token_count and thoughts_token_count from usage_metadata. Fix every call site.` |
| Forgot thinking tokens | `thoughts_token_count bills at the output rate. It must be a separate column and it must be in the cost. Fix config usage and the report.` |
| Calls Vertex outside llm.py | `No module except llm.py may import google.genai. That call is unmetered. Route it through llm.call and re-run.` |
| Substituted a model id | `Never silently substitute a model. Run ac verify --list-models, report what exists, and stop.` |
| Commit says "Generated with Claude Code" | `Amend that commit. No Co-Authored-By trailer, no generated-with footer, no emoji, no mention of Claude or Anthropic in any commit or committed file. Check every commit in git log and amend any that violate this.` |
| Enrichment spend appeared in the run's llm_calls | `Enrichment must meter to data/enrich.db only. Dataset construction cost is not inference cost. Fix it and re-run the report.` |
| Reported a single run as the result | `A single run is an anecdote. Run ac demo --replicate over REPLICATION_SEEDS and report mean and min-max range per cell.` |
| Skipped the review packet | `Run ac review --checkpoint N. A checkpoint is not complete without its packet.` |

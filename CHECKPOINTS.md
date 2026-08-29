# CHECKPOINTS.md

Five checkpoints. These are the **only** times you stop and ask for human review. Between them you
work fully autonomously — no questions, no approval requests, no "should I?".

At each checkpoint: verify every box by running the command, update `SCORECARD.md`, write
`CHECKPOINT_REPORT.md`, run `ac review --checkpoint N` to build the review packet per
`REVIEW_PROTOCOL.md`, commit, and stop.

**The packet is the deliverable of a checkpoint.** Soumy uploads `review/checkpoint-N/` to chat
and the reviewer sees nothing else. An incomplete packet is a failed checkpoint.

A checkpoint is **not** reached until every box below is objectively true. Do not claim a
checkpoint on a partial. If you cannot reach it, say so explicitly and list the blocker.

---

## Checkpoint 1 — Data foundation

**Trigger:** `uv run ac generate` produces a database and all generator/blackboard tests pass.

**Must be true:**
- [ ] `uv sync --extra dev` works from a clean checkout; `ac` entry point resolves
- [ ] `uv run ac verify --list-models` confirms the three model ids in `config.py` exist in
      `GOOGLE_CLOUD_LOCATION`; if any is missing, report the available list and STOP
- [ ] `uv run ac generate` produces `data/events.db` with 900–1100 events across 4 streams
- [ ] `uv run ac generate --enrich` rewrites every event's text via `enrich.py`; a second run is
      free from cache and produces identical text
- [ ] Enrichment spend landed in `data/enrich.db`, NOT in the run's `llm_calls` table
- [ ] `sample_events.txt` shows a near-miss whose prose is indistinguishable from a real
      incident's — that is the point of enrichment, and the human will check it
- [ ] Exactly 5 incidents and 4 near-misses present in the `ground_truth` table
- [ ] Running `ac generate` twice produces identical row counts and identical event ids (seeded)
- [ ] `events` table has **no** incident column
- [ ] `tests/test_no_groundtruth_leak.py` passes
- [ ] `tests/test_generator.py` asserts each incident spans its declared streams

**Artifacts:** `review/checkpoint-1/` containing the standard packet plus `seed_manifest.json`,
`sample_events.txt` (20 per stream, one full incident chain, one near-miss chain) and
`dataset_stats.txt`.

**Human will check:** that the planted incidents are actually *plausible* — that a smart human
reading the raw events could find them, and that the near-misses are genuinely tempting. This is
the one thing you cannot judge for yourself. Print 20 sample events from each stream and one full
incident chain in the report so the human can eyeball it.

**Cut rule:** none. Never cut this.

---

## Checkpoint 2 — Cascade runs end to end with real numbers

**Trigger:** `uv run ac run --arm cascade` completes and prints the collapse line with metered tokens.

**Must be true:**
- [ ] All four Tier 0 detectors run concurrently and write anomalies (60–100 of them)
- [ ] Tier 1 batches 10 anomalies per call on `gemini-3.1-flash-lite` with thinking budget 0
- [ ] Tier 2 makes ≤2 `gemini-3.1-pro` calls with thinking budget 2048, returns validated hypotheses
- [ ] `thoughts_token_count` is non-zero for Tier 2 and zero for Tier 1 — proof the budgets applied
- [ ] Gate applies dedup → sufficiency → floor → rank → 7-cap, with reasons logged
- [ ] `llm_calls` table has real `usage_metadata` counts, with `thinking_tokens` as its own column
- [ ] No module outside `llm.py` imports `google.genai`
- [ ] Response cache is populated; a second identical run works with `AC_LLM_MODE=replay`
- [ ] The collapse line prints, e.g. `1000 → 84 → 16 → 11 → 7`
- [ ] `tests/test_gate.py` covers: dedup, single-source rejection, floor rejection, displacement, tie-break determinism

**Artifacts:** `review/checkpoint-2/` — standard packet plus `collapse_line.txt`,
`anomalies_sample.txt`, `hypotheses.json`, `gate_trace.txt`, `llm_calls.csv`, and a `.py.txt`
snapshot of every source file.

**Human will check:** whether the collapse ratios look right and whether any hypothesis is
obviously nonsense.

**Cut rule:** if Tier 2 is unstable at the 60% time mark, simplify to a single call with fewer
candidates. Do not cut metering.

---

## Checkpoint 3 — The table

**Trigger:** `uv run ac demo` runs both arms and writes `runs/latest_report.md` with real numbers.

**Must be true:**
- [ ] Baseline arm runs, sends all events to `gemini-3.1-pro` with the same thinking budget as Tier 2, produces hypotheses through the same gate
- [ ] Table A is fully populated with measured values — no placeholders, no estimates
- [ ] Table B (recall waterfall) is populated using the exact definition in SPEC §10
- [ ] Cost comes from `config.price_call` — thinking tokens billed at the output rate, cached
      input at 0.1×, Pro long-context cliff applied where crossed
- [ ] Table C (cost by tier) shows Tier 2 as few calls holding most of the spend
- [ ] Whether either arm crossed the 200K Pro price cliff is stated explicitly
- [ ] The extrapolation row is labelled as an extrapolation
- [ ] Hallucinated-event_id drop count is reported for both arms
- [ ] `uv run ac demo` works end to end from a clean checkout in one command
- [ ] `uv run ac demo --replicate` runs all five `REPLICATION_SEEDS` and Table A reports
      **mean and min–max range** per cell, not a single run's numbers

**Artifacts:** `review/checkpoint-3/` — standard packet plus `latest_report.md`,
`llm_calls.csv` for both arms, `cost_breakdown.txt`, `long_context_flag.txt`.

**Human will check:** whether the numbers are believable and whether the story survives them. If
recall is worse than expected, that is a finding, not a failure — report it.

**Cut rule:** never cut. If you reach the time limit with nothing else, you must have this.

---

## Checkpoint 4 — Break it on purpose

**Trigger:** both failure demos run reproducibly and their effects are visible in the audit trail.

**Must be true:**
- [ ] `ac chaos kill --detector support` kills that detector mid-run; the run completes
- [ ] The audit trail records the dead stream explicitly — the gap is visible, not silent
- [ ] Hypotheses that depended on the dead stream fail the two-source check rather than escalating
      on thin evidence, and the report shows which incidents were lost as a result
- [ ] `ac chaos flood --stream support --factor 50` triggers backpressure, then Tier 2 shedding
- [ ] Under shed, the run completes on Tiers 0 and 1, marked `degraded=True`, with a `SHED` record
- [ ] Cost under flood is *lower* than the normal run — demonstrate that the cost mechanism and the
      load-shedding mechanism are the same mechanism
- [ ] Both demos complete in under 60 seconds each (they will be run live)

**Artifacts:** `review/checkpoint-4/` — standard packet plus `audit_kill.jsonl`,
`audit_flood.jsonl`, `failure_comparison.md`, `shed_trace.txt`.

**Human will check:** that the demos are dramatic enough to land in 60 seconds on a projector.

**Cut rule:** cut flood before kill. Kill-a-detector is the more important demo.

---

## Checkpoint 5 — Shippable

**Trigger:** repo is clean, documented, and pushed.

**Must be true:**
- [ ] `README.md` with: one-line pitch, architecture diagram (mermaid), the table, how to run,
      the recall definition, and the out-of-scope list stated as judgement
- [ ] `docs/architecture.md` with the tier diagram and the concurrency/shedding explanation
- [ ] `docs/demo_script.md` — the 5-minute run of show with timings
- [ ] `DECISIONS.md` has at least 8 entries, each with the rejected alternative
- [ ] `runs/cache/` committed so the demo runs offline; verified with `AC_LLM_MODE=replay`
- [ ] `uv run ac verify` passes
- [ ] `FILE_TOUR.md` — one line per source file: what it does and why it exists
- [ ] All tests pass; `ruff check` clean
- [ ] `git log` contains **no** Co-Authored-By trailer, no "Generated with Claude Code" footer,
      no 🤖, and no mention of Claude, Anthropic, or an AI agent in any commit or committed file
- [ ] `AC_LLM_MODE=replay uv run ac demo` completes with networking disabled

**Artifacts:** `review/checkpoint-5/` — standard packet plus `README.md`, `FILE_TOUR.md`,
`docs/architecture.md`, `docs/demo_script.md`, `replay_verification.txt`, `repo_url.txt`.

**Human will check:** everything, and then read every file out loud.

---

## What to do if you finish early

In this order, and only after Checkpoint 5:
1. Add a `--ablation` flag that runs cascade with the gate disabled, to show what the gate is worth
2. Add per-tier latency breakdown to the report
3. Add a second baseline: `gemini-3.7-flash` on all events, to show the cascade beats
   *cheap-model-everything* too, not just expensive-model-everything. Label its rate as
   introductory-through-2026-12-31 in the report — do not quietly use a promotional price
4. Improve the terminal output aesthetics

Do not start any of these before Checkpoint 5 is complete.

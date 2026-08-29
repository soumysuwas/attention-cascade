# CHECKPOINT 1 — Data foundation
**Status:** READY FOR REVIEW (10 of 11 boxes met; 1 blocked on a human decision)

## Boxes

- [x] `uv sync --extra dev` works from a clean checkout; `ac` entry point resolves
      — evidence: `uv sync --extra dev` → "Resolved 41 packages"; `uv run ac --help` lists
      generate / verify / review / stats.

- [ ] `uv run ac verify --list-models` confirms the three model ids in `config.py` exist in
      `GOOGLE_CLOUD_LOCATION` — **NOT MET.** `gemini-3.1-pro` (both `TIER2_MODEL` and
      `BASELINE_MODEL`) is not callable in this project on any endpoint. Per SPEC §15 I report the
      available list and stop rather than substituting. Evidence: `model_availability.txt`.

      | role | model id | callable |
      |---|---|---|
      | tier1 triage | `gemini-3.1-flash-lite` | yes (in=6 out=1 think=0) |
      | tier2 correlation | `gemini-3.1-pro` | **NO — 404 NOT_FOUND** |
      | naive baseline | `gemini-3.1-pro` | **NO — 404 NOT_FOUND** |
      | optional 2nd baseline | `gemini-3.7-flash` | yes (in=6 out=1 think=77) |

      `gemini-3.1-pro-preview` **is** callable and returns thinking tokens correctly. See the
      question below and D-7.

- [x] `uv run ac generate` produces `data/events.db` with 900–1100 events across 4 streams
      — evidence: 1090 events — billing 278, crm 118, engineering 157, support 537. The command
      exits 1 if the count falls outside `config.TARGET_EVENT_COUNT`.

- [x] `uv run ac generate --enrich` rewrites every event's text; a second run is free from cache
      and produces identical text — evidence: "rewritten 1090/1090 across 28 batches, 0 failed".
      Second run under `AC_LLM_MODE=replay` completed in **0.44s with zero network** and produced
      a byte-identical corpus: sha256 over every (id, text) pair is `3c2ad09cd5adc068` both times.
      Reaching 1090/1090 required a repair pass — see Decisions.

- [x] Enrichment spend landed in `data/enrich.db`, NOT in the run's `llm_calls` table
      — evidence: `data/enrich.db` holds 115 calls, 318,984 in / 156,680 out, **$0.3148**, all
      `gemini-3.1-flash-lite` at tier `enrich`. No run database exists yet, so no measured run can
      have been contaminated. Model-availability probes are likewise isolated in
      `runs/verify/run.db` (2 calls, $0.0003).

- [x] `sample_events.txt` shows a near-miss whose prose is indistinguishable from a real
      incident's — evidence: `sample_events.txt` prints the INC-1 chain and the NM-1 chain
      side by side; `enrichment_check.txt` prints 10 noise texts against 10 incident texts with
      labels stripped. **This is the box the human must actually check, not me.**

- [x] Exactly 5 incidents and 4 near-misses present in the `ground_truth` table
      — evidence: `groundtruth.stats()` → 5 incidents, 4 near-misses, 63 rows. Per-incident event
      counts are 14 / 8 / 8 / 13 / 5, matching SPEC §4's verified figures exactly.

- [x] Running `ac generate` twice produces identical row counts and identical event ids
      — evidence: `dataset_stats.txt` ends with a determinism check that generates twice into
      scratch databases: row counts equal, event ids identical, full rows identical. Also asserted
      by `test_generation_is_deterministic`, which additionally compares the ground-truth table.

- [x] `events` table has **no** incident column
      — evidence: columns are exactly `{id, ts, stream, entity_id, kind, numeric, payload}`.
      Asserted in three places: the quarantine test, `test_events_table_shape_is_locked`, and
      `test_ground_truth_is_not_reachable_from_the_events_table`.

- [x] `tests/test_no_groundtruth_leak.py` passes
      — evidence: 12 of the 46 tests. It caught a real leak during this checkpoint: `cli.py`
      printed `stats['ground_truth_rows']`. Fixed by removing the print, not by widening the
      test's allow-list (D-9).

- [x] `tests/test_generator.py` asserts each incident spans its declared streams
      — evidence: `test_incident_spans_its_declared_streams`, parametrised over all five
      incidents, plus `test_only_inc5_relies_on_a_silence_hop`.

**Also true:** 46 tests pass, `ruff check` is clean.

## What I built
- `groundtruth.py` — the quarantined read side. Every incident lookup in the codebase goes through
  this one importable module, which is what makes the quarantine checkable by an AST walk instead
  of by trust.
- `report.py` — dataset inspection artefacts (stats, samples, enrichment check, determinism) and
  the `build_packet` review-packet assembler. The only pipeline-adjacent module allowed to read
  ground truth.
- `cli.py` — `ac generate [--enrich]`, `ac verify [--list-models]`, `ac review --checkpoint N`,
  `ac stats`. `verify --list-models` probes each configured id with a real metered call because
  `models.list()` demonstrably over-reports (D-5).
- `tests/test_generator.py` — 17 tests. The load-bearing one asserts each incident really crosses
  the systems it claims to.
- `tests/test_blackboard.py` — 21 tests, aimed at the architecture's claims rather than at CRUD:
  arm scoping, thinking tokens in their own column, append-only audit, and the two concurrency
  guarantees (200 concurrent writes → exactly 200 rows; a crashing writer does not wedge the lock).
- `__init__.py` — loads `.env` before `config.py` reads `os.environ`.
- Two minimal fixes to PROVIDED files: a repair pass in `enrich.py` (D-8) and one dead local
  removed from `generator.py` (ruff F841).

## Numbers right now
```
events                1090   (billing 278, crm 118, engineering 157, support 537)
accounts              12 + internal,  60 days,  12 event kinds
planted incidents     5     INC-1 14ev crm/eng/support · INC-2 8ev billing/support
                            INC-3 8ev billing/crm/support · INC-4 13ev billing/eng/support
                            INC-5 5ev billing/crm  (support hop is a silence — see below)
near-misses           4     NM-1 support 9ev · NM-2 billing 3ev · NM-3 eng 2ev · NM-4 crm 1ev
ground-truth rows     63
enrichment            1090/1090 rewritten, 115 calls, $0.3148, isolated in data/enrich.db
offline replay        0.44s, zero network, identical corpus (sha 3c2ad09cd5adc068)
tests                 46 passed · ruff check clean
cascade / baseline    not yet built — blocked on the Tier 2 model
```

## Decisions made since last checkpoint
- **D-5** Verify model availability with a real call, not `models.list()` — the catalogue listed
  four ids that all 404'd.
- **D-6** `GOOGLE_CLOUD_LOCATION=global` rather than `us-central1` — every Gemini 3.x id 404s
  regionally. Rejected downgrading to the 2.5 family, which would have meant editing `PRICING`.
- **D-7** Stop at the missing Tier 2 model instead of substituting the preview id.
- **D-8** Add a repair pass to enrichment rather than accept 1089/1090.
- **D-9** The CLI stays blind to ground truth; widening the quarantine allow-list was rejected.
- **D-10** Score recall from the `ground_truth` table, not the seed manifest; INC-5's silence hop
  is an explicit named exception rather than a relaxed assertion.

## Blockers

**B-1 — `gemini-3.1-pro` does not exist in this project.** Confirmed by direct calls, not inference:
404 NOT_FOUND from both `us-central1` and `global`. `gemini-3.1-pro-preview` is callable from
`global` and returns `thoughts_token_count` correctly under a 2048 budget.

This blocks Tier 2, the naive baseline, and therefore Table A, Table B and Table C — Checkpoints 2
and 3 in their entirety. It blocks nothing else: Tier 0 detectors, the gate, the orchestrator and
the chaos injectors are all independent of which frontier model sits at Tier 2, and I will build
them next regardless.

I did not substitute because `PRICING` in `config.py` is keyed by model id and marked
do-not-edit-without-approval. Using the preview id would mean either applying `gemini-3.1-pro`'s
$2/$12 rates to an endpoint I have no evidence they cover, or inventing a pricing row — and the
headline cost claim is the deliverable.

## What I need from the human

1. **Approve one of these for `TIER2_MODEL` and `BASELINE_MODEL`** (this is the only blocking one):
   - **(a) `gemini-3.1-pro-preview` at the existing `gemini-3.1-pro` rates** — my recommendation.
     It is the same model family, it is callable now, and it thinks correctly. The report would
     state plainly that the preview endpoint was used and that pricing is assumed to match GA.
   - **(b) `gemini-3.7-flash`** — callable, but it is on introductory pricing through 2026-12-31,
     which `config.py` explicitly warns against building a headline number on.
   - **(c) a different GCP project** with `gemini-3.1-pro` enabled.
2. **Eyeball `sample_events.txt`.** Do the INC-1 and NM-1 chains read as plausible enterprise
   records, and is NM-1 genuinely tempting? This is the one Checkpoint 1 claim I cannot verify
   myself, and it is upstream of every recall number.
3. **NM-4 is a single CRM event.** That may be too thin to be a real test of the gate — it will be
   rejected as single-source no matter how good or bad the gate is. Worth strengthening, or is
   one trivially-rejected near-miss acceptable?

## Next checkpoint
**Checkpoint 2 — Cascade runs end to end with real numbers.** First three steps, none of which
need the blocker answered:
1. `detectors.py` — the four Tier 0 stream workers as independent asyncio tasks, tuned on noise
   density to land in the 60–100 anomaly band. `ticket_silence` cites the last 3 tickets before
   the window so an absence is still citable evidence.
2. `gate.py` and `tests/test_gate.py` — the pure function first, with dedup, single-source
   rejection, floor rejection, displacement and tie-break determinism covered before anything
   calls it. This is the heart of the judging story and it needs no model at all.
3. `triage.py` — Tier 1 on `gemini-3.1-flash-lite`, which is callable, batching 10 anomalies per
   call at thinking budget 0, failing open on malformed JSON.

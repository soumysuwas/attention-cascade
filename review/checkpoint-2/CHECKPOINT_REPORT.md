# CHECKPOINT 2 — Cascade runs end to end with real numbers
**Status:** READY FOR REVIEW (all boxes met)

## Boxes

- [x] **All four Tier 0 detectors run concurrently and write anomalies (60–100)**
      — 75 anomalies from 1069 events. Four `asyncio` tasks, one per stream, each blind to the
      others. Counts: ticket_volume_spike 29, stage_stall 11, delivery_slip 10, forecast_drop 8,
      reopen_spike 5, usage_decline 4, severity_spike 3, usage_drop 3, invoice_dispute 1.

- [x] **Tier 1 batches 10 anomalies per call on the cheap model with thinking budget 0**
      — 8 calls for 75 anomalies. `gemini-3.5-flash-lite`, `thinking_budget=0`,
      `max_output_tokens=1000`, temperature 0, JSON mime type. System prompt asserted
      byte-identical to SPEC §6 by a scripted comparison.

- [x] **Tier 2 makes ≤2 pro calls with thinking budget 2048, returns validated hypotheses**
      — **1 call**, `gemini-3.1-pro-preview`, `thinking_budget=2048`. Every cited `event_id` is
      checked against the events table; 0 hallucinated this run. Prompt byte-identical to SPEC §7.

- [x] **`thoughts_token_count` non-zero for Tier 2, zero for Tier 1 — the budgets applied**
      — Tier 1: **0** thinking tokens across 8 calls. Tier 2: **2,109** across 1 call.
      Straight out of `llm_calls`, see `llm_calls.csv`.

- [x] **Gate applies dedup → sufficiency → floor → rank → 7-cap, with reasons logged**
      — `gate.py` is pure, does no I/O and has no randomness. See `gate_trace.txt`. 32 tests.

- [x] **`llm_calls` has real `usage_metadata` counts with `thinking_tokens` as its own column**
      — `llm_calls.csv`. `test_llm_call_records_thinking_tokens_in_their_own_column` asserts
      visible output never absorbs thinking tokens.

- [x] **No module outside `llm.py` imports `google.genai`**
      — now machine-checked: `test_only_llm_py_imports_the_vendor_sdk` walks every module's AST,
      and `test_no_module_constructs_its_own_client` forbids `genai.Client` elsewhere.

- [x] **Cache populated; a second identical run works with `AC_LLM_MODE=replay`**
      — `AC_LLM_MODE=replay uv run ac run --arm cascade` reproduces `1069 → 75 → 75 → 4 → 4`
      with zero network in 0.1s.

- [x] **The collapse line prints** — `1069 events → 75 anomalies → 75 candidates → 4 hypotheses
      → 4 signals`

- [x] **`tests/test_gate.py` covers dedup, single-source rejection, floor rejection, displacement,
      tie-break determinism** — 32 tests, including shuffle-invariance over 25 permutations and an
      accounting test that every hypothesis either escalates or carries a stated reason.

**Also true:** 94 tests pass, `ruff check` clean, `ac check-linkage` passes for all five incidents.

## What I built
- `gate.py` + 32 tests — written and tested **before** anything called it, as instructed.
- `detectors.py` — nine detectors across four concurrent workers, plus the kill and flood
  injectors Checkpoint 4 will use. `ticket_silence` cites the last 3 tickets before the gap so an
  absence is still citable evidence.
- `triage.py` — Tier 1, batches of 10, retry once then **fail open** so a parse error cannot
  silently destroy recall.
- `correlate.py` — Tier 2, validates every cited `event_id`, counts hallucinations.
- `orchestrator.py` — bounded queues, a backpressure watcher, and the shed path.
- Reviewer's F-1 through F-4: `component`/`symptom` join keys, NM-5, NM-4 strengthened,
  and `ac check-linkage` with a negative control.
- `review_artifacts/` flat mirror, printed as the last line of `ac review`.

## Numbers right now
```
collapse        1069 events → 75 anomalies → 75 candidates → 4 hypotheses → 4 signals
recall          4 of 5 planted incidents FOUND  (INC-1, INC-2, INC-3, INC-5)
false escal.    0
hallucinated    0 cited event_ids did not exist
cost            $0.0838   tier1 $0.0145 (17.3%, 8 calls) · tier2 $0.0692 (82.7%, 1 call)
thinking        tier1 0 · tier2 2,109   (billed at the output rate, counted in cost)
latency         51.5s live · 0.1s from cache
```
Table C has the shape it should: **1 call of 9 holds 83% of the spend.**

### Recall waterfall (Table B)
| Stage | Items | Recoverable (of 5) | Which |
|---|---|---|---|
| Raw events | 1069 | 5 | all |
| After Tier 0 | 75 | 5 | all |
| After Tier 1 | 75 | 5 | all |
| After Tier 2 | 4 | 4 | INC-1,2,3,5 |
| After gate | 4 | 4 | INC-1,2,3,5 |
| **FOUND** | 4 | **4** | INC-1,2,3,5 |

The entire recall loss is at Tier 2, and it is one incident.

## Decisions made since last checkpoint
D-11 approved models at sourced rates · D-12 structured `component` join keys · D-13
`check-linkage` treats undiscoverability as a build error · D-14 NM-5 so the floor is tested ·
D-15 suppress normal usage inside a planted decline · D-16 a separate detector for slow bleeds ·
D-17 broaden `invoice_dispute` so it can fire on noise · **D-18 group Tier 2's payload by account
(2/5 → 4/5)** · D-19 two-call split by account not rank · D-20 report Tier 1's permissiveness
rather than tune it away · D-21 packet replays into a scratch database.

## Blockers
None.

## What I need from the human
1. **Tier 1 removes nothing** (75 of 75 plausible, $0.0145, zero volume cut). The prompt says "be
   permissive" and the model obeys. Do I leave it and report that Tier 0 owns the reduction, or is
   forcing selectivity worth deviating from the verbatim prompt? I did not tune it, per D-20.
2. **Is 4/5 acceptable here?** INC-4 survives to Tier 2's input, is confirmed joinable, and is
   still not proposed. I have localised it but not explained it.
3. **Three recall defects reached a running pipeline before anything caught them.** I want to add
   a detectability guard to the generator — assert each incident produces Tier 0 anomalies in ≥2
   of its streams — which would have caught the INC-5 decay immediately. Worth the 15 minutes
   before Checkpoint 3, or defer?

## Next checkpoint
**Checkpoint 3 — The table.** First three steps:
1. `baseline.py` — all 1069 events to `gemini-3.1-pro-preview` at thinking budget 2048, chunked by
   6-day windows with 2-day overlap only if it crosses 180K input, through the **same gate**.
2. `metering.py` + Table A, with the long-context cliff flag and the extrapolation row labelled.
3. `ac demo` and `ac demo --replicate` across the five `REPLICATION_SEEDS`, reporting mean and
   min–max per cell.

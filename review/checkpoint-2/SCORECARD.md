# SCORECARD.md

Updated at every checkpoint. Append-only — keep the history so the trend is visible.
This is the file to paste into chat for external review.

## Metric history

| Checkpoint | Events | Anomalies | Candidates | Hypotheses | Signals | Incidents found /5 | False escalations | Cascade cost $ | Baseline cost $ | Cost ratio | Cascade latency s | Baseline latency s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1090 | — | — | — | — | — | — | — | — | — | — | — |
| 2 | 1069 | 75 | 75 | 4 | 4 | 4 | 0 | 0.0838 | — | — | 51.5 | — |
| 3 | | | | | | | | | | | | |
| 4 | | | | | | | | | | | | |
| 5 | | | | | | | | | | | | |

## Self-assessment against the judging rubric

Score each 1–5 honestly at every checkpoint. An honest 2 is more useful than a hopeful 4.

| Criterion | CP1 | CP2 | CP3 | CP4 | CP5 | Notes |
|---|---|---|---|---|---|---|
| Architecture & technical judgement | 3 | 4 | | | | All five tiers run. The account-grouped Tier 2 payload and the gate's total-order ranking are decisions I can defend. Losing a mark because Tier 1 currently earns nothing. |
| Real-world problem solving | 3 | 4 | | | | Finds 4 of 5 planted incidents for $0.08 with zero false escalations. Misses INC-4. |
| Behaviour under scale & failure | 2 | 3 | | | | Backpressure, the shed path and kill/flood injectors are written and wired, but not yet demonstrated. That is Checkpoint 4. |
| Measurability / falsifiability | 4 | 5 | | | | Every token from usage_metadata, thinking counted separately, the waterfall localises every lost incident, and check-linkage fails the build if an incident becomes undiscoverable. The waterfall found three real defects this checkpoint. |
| Explainability (can every file be defended?) | 4 | 4 | | | | Fifteen modules, each with its docstring; 21 decisions each carrying a cost. detectors.py is the longest at ~320 lines and is the one file that is a list rather than an argument. |
| Demo readiness (runs offline, under 5 min) | 2 | 4 | | | | The full cascade replays offline in 0.1s from the committed cache. No `ac demo`, no baseline arm yet. |

## The three weakest things right now

State them plainly. This is the section the external reviewer reads first.

1. **Tier 1 currently earns nothing.** It marks 75 of 75 anomalies plausible, costs $0.0145, and
   removes zero volume. The spec expected 12-20 candidates. The prompt says "be permissive" and
   the model is obeying it, so I reported it rather than tuning until the number matched (D-20) —
   but as it stands the middle tier of a three-tier cascade is pure overhead on this dataset, and
   a judge will ask what it is for.
2. **INC-4 is missed and I do not know why.** It survives Tier 0 and Tier 1, it is present in
   Tier 2's input, `check-linkage` confirms it is joinable on `integrations` and `v3.11`, and the
   model still does not propose it. I know exactly where it is lost and not why. Recall is 4/5.
3. **Three separate recall defects reached a running pipeline before anything caught them.** A
   silent 15-candidate drop, a planted usage decay that was not actually in the data, and a Tier 2
   payload that scattered every cross-system chain. All three were found by the recall waterfall
   after the fact, not by a test in advance. `check-linkage` guards one class; the other two have
   no guard yet.

## What I would fix with one more hour

Add a third generator guard, alongside `check-linkage`: assert that every planted incident is
actually *detectable*, by running Tier 0 over a fresh dataset and requiring each incident to
produce anomalies in at least two of its streams. That single check would have caught the INC-5
usage-decay defect immediately instead of three iterations later, and it closes the gap between
"the incident is joinable" and "the incident is present in the data at all".

# SCORECARD.md

Updated at every checkpoint. Append-only — keep the history so the trend is visible.
This is the file to paste into chat for external review.

## Metric history

| Checkpoint | Events | Anomalies | Candidates | Hypotheses | Signals | Incidents found /5 | False escalations | Cascade cost $ | Baseline cost $ | Cost ratio | Cascade latency s | Baseline latency s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1090 | — | — | — | — | — | — | — | — | — | — | — |
| 2 | | | | | | | | | | | | |
| 3 | | | | | | | | | | | | |
| 4 | | | | | | | | | | | | |
| 5 | | | | | | | | | | | | |

## Self-assessment against the judging rubric

Score each 1–5 honestly at every checkpoint. An honest 2 is more useful than a hopeful 4.

| Criterion | CP1 | CP2 | CP3 | CP4 | CP5 | Notes |
|---|---|---|---|---|---|---|
| Architecture & technical judgement | 3 | | | | | Tiers are specified and the quarantine is enforced by a test, but no tier is built yet. Score is for design on paper, not running code. |
| Real-world problem solving | 3 | | | | | The dataset models a real cross-system problem convincingly. Nothing solves it yet. |
| Behaviour under scale & failure | 2 | | | | | Only the blackboard's failure behaviour is tested (serialized writes, a crashing writer not wedging the lock). No shedding, no chaos. |
| Measurability / falsifiability | 4 | | | | | Ground truth is planted, quarantined, and the quarantine is machine-checked. Enrichment closes the prose confound. This is the strongest column and it is the one that matters most. |
| Explainability (can every file be defended?) | 4 | | | | | Nine source files, all under 300 lines, each with the three-line docstring. The INC-5 silence exception is the only thing needing a paragraph. |
| Demo readiness (runs offline, under 5 min) | 2 | | | | | `generate --enrich` replays offline in 0.44s, but there is no demo yet and Tier 2 is blocked. |

## The three weakest things right now

State them plainly. This is the section the external reviewer reads first.

1. **The Tier 2 model does not exist in this project.** `gemini-3.1-pro` 404s on every endpoint.
   The entire headline comparison — cascade versus naive frontier baseline — is blocked on a
   human decision about substituting `gemini-3.1-pro-preview`. I refused to substitute silently
   because `PRICING` is keyed by model id and marked do-not-edit. Nothing downstream of Tier 2
   can start until this is answered.
2. **Nobody has confirmed the planted incidents are actually findable.** The dataset is
   deterministic, correctly shaped and enriched, but "a smart human reading raw events could find
   these" is the one claim I cannot check myself, and every recall number depends on it. If the
   incidents are too easy the cascade looks better than it is; too hard and both arms score zero
   and the table says nothing.
3. **Not one line of the actual pipeline exists.** Tier 0 through the gate, the orchestrator, the
   baseline, metering and the table are all still specification. Checkpoint 1 is a foundation, and
   a foundation is the cheapest part of this build to have finished.

## What I would fix with one more hour

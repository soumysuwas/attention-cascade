# FILE_TOUR.md

One line per source file: what it does and why it exists. Kept current as modules land, not
written at the end. Read it top to bottom and the architecture should be obvious.

## Data foundation

| File | What it does | Why it exists |
|---|---|---|
| `config.py` | Every tunable constant: model ids, `PRICING`, detector thresholds, queue sizes, gate parameters. | So every cost number in the report traces to one auditable table. Marked do-not-edit-without-approval. |
| `models.py` | Pydantic types that cross a tier boundary: Event, Anomaly, Candidate, Hypothesis, Signal, GateResult, LLMCall. | Tiers share a blackboard, so the shapes must be agreed in exactly one place. Deliberately has no incident field. |
| `blackboard.py` | SQLite shared store. Every write attributed, timestamped, and serialized behind one lock. Append-only audit trail mirrored to jsonl. | Indirect coordination through shared state is what lets a detector die without taking the system with it. |
| `generator.py` | ~1080 synthetic events over 60 days with 5 planted cross-system incidents and 5 near-misses. | Without ground truth the cost claim is unfalsifiable. The crown jewel — never cut. |
| `enrich.py` | Rewrites every event's `payload["text"]` through a blind, shuffled model pass against its own database. | Removes the confound where planted incidents have more distinctive prose than noise. Spend never enters a measured run. |
| `groundtruth.py` | The only read side for the `ground_truth` table. | Puts every incident lookup behind one importable module so the quarantine is machine-checkable by an AST walk, not by trust. |
| `llm.py` | The single metered, cached gateway to Vertex. Records real `usage_metadata` including `thoughts_token_count`. | A cost table built on estimated tokens is worthless. No other module may import `google.genai`. |
| `report.py` | Dataset artefacts, the linkage check, recall scoring, and the review-packet builder. | The only pipeline-adjacent module allowed to read ground truth, so the grading code sits where the quarantine test can point at it. |
| `cli.py` | The `ac` entry point: generate, verify, check-linkage, review, stats. | One command has to run the whole thing, and `verify --list-models` probes with real calls because the catalogue over-reports. |

## Pipeline

| File | What it does | Why it exists |
|---|---|---|
| `gate.py` | *(next)* Pure deterministic escalation: dedup, sufficiency, confidence floor, rank, 7-cap, displacement. | The heart of the story. The model proposes, the math disposes — no LLM decides what escalates. |
| `detectors.py` | *(next)* Four Tier 0 stream workers as independent asyncio tasks. Zero tokens. | Cuts ~1080 events to ~60-100 anomalies before any model is paid. |
| `triage.py` | *(next)* Tier 1, batches of 10, thinking budget 0. | Discards the obviously routine cheaply. Fails open on bad JSON so a parse error cannot destroy recall. |
| `correlate.py` | *(next)* Tier 2, thinking budget 2048, validates every cited event_id. | The only place frontier-model money is spent. |
| `orchestrator.py` | *(next)* Bounded queues, backpressure, load shedding. | The cost-control mechanism and the load-shedding mechanism are the same mechanism. |
| `baseline.py` | *(next)* All events to the frontier model, then the same gate. | The comparison is only honest if both arms share escalation logic. |
| `metering.py` | *(next)* Sums `llm_calls` into per-tier and per-arm cost. | Table C. |
| `chaos.py` | *(next)* Kill and flood injectors. | The two live failure demos. |

## Tests

| File | What it covers |
|---|---|
| `test_no_groundtruth_leak.py` | AST walk asserting no pipeline module imports `groundtruth` or names an incident field. Build-stopping. |
| `test_generator.py` | Size band, stream split, incident stream spans, near-miss structure, INC-5's silence hop, determinism, and the linkage check with a negative control. |
| `test_blackboard.py` | Round-trips, arm scoping, thinking tokens in their own column, append-only audit, WAL, and the concurrency claims. |
| `test_gate.py` | *(next)* Dedup, single-source rejection, floor rejection, displacement, tie-break determinism. |

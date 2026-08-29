# File tour

One line per source file: what it does and why it exists.

## Data foundation

| File | What it does | Why it exists |
|---|---|---|
| `config.py` | Every tunable constant in one place: model ids, `PRICING`, detector thresholds, queue sizes, gate parameters, and `price_call`. | So every cost number traces back to one auditable table. Thinking tokens billed at the output rate and the 200K Pro cliff both live in `price_call`, implemented once. |
| `models.py` | Pydantic types that cross a tier boundary: `Event`, `Anomaly`, `Candidate`, `Hypothesis`, `EvidenceRef`, `Signal`, `GateResult`, `LLMCall`. | Tiers share a blackboard rather than calling each other, so the shapes have to be agreed in exactly one place. Deliberately has no incident field anywhere. |
| `blackboard.py` | SQLite shared store. Every write attributed, timestamped and serialized behind a single `asyncio.Lock`; append-only audit trail mirrored to `audit.jsonl`. | Indirect coordination through shared state is what lets a detector die without taking the system with it. Writes are serialized so evidence cannot double-count. |
| `generator.py` | ~1069 synthetic events over 60 days, 12 accounts and 4 streams, containing 5 planted cross-system incidents and 5 near-misses, plus the ground-truth mapping. | Without ground truth the cost claim is unfalsifiable. Also carries the `usage_suppression_windows` that make a planted decline actually replace an account's normal usage instead of hiding inside it. |
| `enrich.py` | Rewrites every event's `payload["text"]` through a blind, shuffled model pass against its own database, with a repair round for ids the model omits. | Hand-written noise reuses a few canned strings while planted incidents get bespoke wording, which would let a model find incidents by prose rather than by reasoning. Shuffling guarantees no causal chain is ever visible while writing. Its spend never enters a measured run. |
| `groundtruth.py` | The only read side for the `ground_truth` table: `incidents`, `near_misses`, `event_to_incidents`, `stats`. | Puts every incident lookup behind one importable module, so the quarantine is checkable by an AST walk instead of by trust. |
| `llm.py` | The single metered, cached gateway to Vertex. Records real `usage_metadata` including `thoughts_token_count`, caches by sha256 of (model, system, prompt, params), and raises on a cache miss in replay mode. | A cost table built on estimated tokens is worthless. Nothing else in the codebase may import the SDK — a call made elsewhere would not be metered. |

## Pipeline

| File | What it does | Why it exists |
|---|---|---|
| `detectors.py` | Nine Tier 0 detectors across four independent asyncio workers, one per stream, spending zero tokens. Also holds `KillSpec` and `FloodSpec` for the failure demos. | Cuts 1069 events to 75 anomalies before any model is paid — the single largest saving in the system, and arithmetic does not hallucinate. `ticket_silence` cites the last 3 tickets *before* a gap so an absence is still citable evidence. |
| `triage.py` | Tier 1. Batches 10 anomalies per call at thinking budget 0, retries once on malformed JSON, then fails open. | Most anomalies are routine and a frontier model should never read them. Thinking is off because a yes/no question does not need reasoning, and thinking bills at the output rate. Failing open is deliberate: a parse error must not silently destroy recall. |
| `correlate.py` | Tier 2. One call (two at most) at thinking budget 2048, with anomalies grouped **by account**, validating every cited `event_id` against the blackboard and counting hallucinations. | The only place frontier-model money is spent. The account grouping is load-bearing: a flat score-ordered list scatters each account's anomalies so the cross-system chain is not visible anywhere in the input. |
| `gate.py` | Pure deterministic escalation: dedup by `(stream, event_id)`, ≥2 distinct sources, confidence floor, rank by `impact × confidence`, cap at 7 with explicit displacement. No I/O, no model, no randomness. | The heart of the design. A model asked "is this important?" says yes far too often, which makes an attention budget meaningless. Escalation authority is arithmetic, and arithmetic is auditable, testable and free. |
| `orchestrator.py` | Wires the tiers with bounded queues, a backpressure watcher and the load-shedding path; returns the collapse line. | The interesting claim is not that a cascade is cheaper but that the mechanism controlling cost *is* the mechanism shedding load. Under flood the system gets cheaper and dumber instead of dying. |
| `report.py` | Dataset artefacts, the linkage check, recall scoring and the waterfall, cost-by-tier, and report assembly. | The only pipeline-adjacent module allowed to read ground truth, so the grading code sits where the quarantine test can point at it and say "only this file". The waterfall is what localises a lost incident to a stage. |
| `cli.py` | The `ac` entry point: `generate`, `verify`, `run`, `check-linkage`, `review`, `stats`. | One command runs the whole thing. `verify --list-models` probes each configured id with a real metered call, because the publisher catalogue over-reports — it listed four ids here that all returned 404. |

### Not yet built

| File | Intended role |
|---|---|
| `baseline.py` | All events to the frontier model at Tier 2's thinking budget, through the same gate. Implemented in outline; its comparison table is not yet populated. |
| `metering.py` | Cost aggregation across arms for Table A. Currently done inline in `report.py`. |
| `chaos.py` | Standalone kill and flood entry points. The injectors themselves live in `detectors.py` and are wired through `orchestrator.py`. |

## Tests

| File | What it covers |
|---|---|
| `test_no_groundtruth_leak.py` | AST walk asserting no pipeline module imports `groundtruth` or names an incident field, that the `events` table has no incident column, and that nothing outside `llm.py` imports the Vertex SDK or builds a client. Build-stopping. |
| `test_generator.py` | Size band, stream split, per-incident stream spans, near-miss structure, INC-5's silence hop as a named exception, determinism under a fixed seed, and the linkage check — including a negative control that strips a join key and asserts the incident becomes unreachable. |
| `test_blackboard.py` | Round-trips for every typed object, arm scoping so the two arms cannot contaminate each other, thinking tokens in their own column, append-only audit, WAL mode, and the two concurrency claims: 200 concurrent writes produce exactly 200 rows, and a crashing writer does not wedge the lock. |
| `test_gate.py` | Every gate rule, plus the properties worth probing: duplicate evidence cannot fake a second source, ranking is invariant under input shuffling, the cap is a live budget rather than a truncation, and every hypothesis is accounted for exactly once. |

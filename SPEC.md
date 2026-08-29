# SPEC.md — Attention Cascade

Authoritative technical specification. Read fully before writing code. Where this file gives a
function signature, a schema, or a prompt, use it verbatim — do not redesign it.

---

## 1. Objective

Prove, with measured numbers, that a tiered cascade finds the same critical cross-system business
signals as a naive frontier-model baseline, at a small fraction of the token cost and latency, and
that it degrades gracefully instead of failing when a source dies or a stream floods.

**Success is a table, two live failure demos, and an audit trail.**

---

## 2. File structure

```
attention-cascade/
├── CLAUDE.md                     # agent constitution (read-only)
├── SPEC.md                       # this file (read-only)
├── CHECKPOINTS.md                # milestone definitions (read-only)
├── AGENT_INSTRUCTIONS.md         # how you work (read-only)
├── progress.md                   # you update every iteration
├── SCORECARD.md                  # you update every checkpoint
├── DECISIONS.md                  # you append every architectural decision
├── README.md                     # you write at Checkpoint 5
├── pyproject.toml
├── uv.lock
├── .env                          # gitignored, human-provided
├── .env.example
├── .gitignore
├── data/
│   ├── events.db                 # generated dataset (gitignored)
│   ├── enrich.db                 # enrichment token spend, kept OUT of the experiment
│   └── seed_manifest.json        # incident definitions, committed
├── runs/
│   ├── cache/                    # LLM response cache, committed for offline demo
│   ├── <run_id>/run.db           # blackboard for one run
│   ├── <run_id>/audit.jsonl      # append-only event log
│   └── latest_report.md          # the table
├── docs/
│   ├── architecture.md           # diagram + explanation
│   └── demo_script.md            # the 5-minute run of show
├── review/checkpoint-{1..5}/      # review packets, see REVIEW_PROTOCOL.md
├── .claude/settings.json          # includeCoAuthoredBy: false
├── src/attention_cascade/
│   ├── __init__.py
│   ├── config.py                 # constants, pricing, thresholds  [PROVIDED]
│   ├── models.py                 # pydantic types                  [PROVIDED]
│   ├── blackboard.py             # SQLite shared store             [PROVIDED]
│   ├── generator.py              # synthetic streams + incidents   [PROVIDED]
│   ├── enrich.py                 # blind text enrichment           [PROVIDED]
│   ├── groundtruth.py            # QUARANTINED accessors           [you build]
│   ├── llm.py                    # metered, cached Vertex client   [PROVIDED]
│   ├── detectors.py              # Tier 0                          [you build]
│   ├── triage.py                 # Tier 1                          [you build]
│   ├── correlate.py              # Tier 2                          [you build]
│   ├── gate.py                   # sufficiency + 7-cap             [you build]
│   ├── orchestrator.py           # asyncio workers, queues, shedding[you build]
│   ├── baseline.py               # naive all-events-to-frontier    [you build]
│   ├── metering.py               # cost accounting                 [you build]
│   ├── report.py                 # the table + recall waterfall    [you build]
│   ├── chaos.py                  # kill / flood injectors          [you build]
│   └── cli.py                    # typer CLI, entry point `ac`     [you build]
└── tests/
    ├── test_generator.py
    ├── test_gate.py
    ├── test_blackboard.py
    └── test_no_groundtruth_leak.py   # MANDATORY, see §11
```

Files marked **[PROVIDED]** already exist and are correct. Read them, do not rewrite them. If you
believe one has a bug, fix the bug minimally and log it in `progress.md`.

---

## 3. Data model

Full pydantic definitions are in `models.py`. Summary:

**Event** — one raw fact from one source system.
`id, ts, stream, entity_id, kind, numeric, payload`
`stream ∈ {crm, engineering, support, billing}`. `entity_id` is an account id, the join key across
systems. **No incident field.**

**Anomaly** — Tier 0 output, zero tokens.
`id, detector, stream, entity_id, window_start, window_end, score, kind, event_ids, summary`

**Candidate** — Tier 1 verdict on one anomaly.
`anomaly_id, plausible, reason, business_hint, model, tokens_in, tokens_out`

**Hypothesis** — Tier 2 output, a proposed cross-system causal chain.
`id, title, narrative, entity_id, evidence[], impact, confidence`
where `evidence[]` is a list of `EvidenceRef(event_id, stream, anomaly_id)`.

**Signal** — a hypothesis that passed the gate and holds an attention slot.
`hypothesis_id, rank, escalated_at, displaced_hypothesis_id | None`

**GroundTruth** — separate table, `(incident_id, event_id, stream)`. Quarantined.

---

## 4. Synthetic data (§ crown jewel — already built, verify it)

`generator.py` produces ~1000 events across 60 simulated days, 12 accounts, 4 streams, with **5
planted cross-system incidents** and **4 near-misses**.

The five incidents:

| ID | Name | Streams | Chain |
|---|---|---|---|
| INC-1 | feature_slip_cascade | eng → support → crm | Feature slips → account tickets spike → deal stalls |
| INC-2 | outage_billing | support → billing | Severity spike → usage collapses |
| INC-3 | pricing_migration | billing → support → crm | Plan change → billing disputes → renewal risk |
| INC-4 | integration_regression | eng → billing → support | Regression ships → API usage drops → tickets |
| INC-5 | champion_churn | crm → support → billing | Contact departs → ticket silence → usage decline |

The four near-misses are single-stream spikes that look exactly like an incident's first hop but
have **no corroborating second system**. They exist so the sufficiency gate has something real to
reject. If your gate escalates a near-miss, that is a false escalation and it goes in the table.

### 4a. Enrichment — why the text is model-written, and why that is not cheating

`generator.py` produces the *skeleton*: timestamps, streams, accounts, numeric values, causal
structure, ground truth. All deterministic, all offline, never model-touched.

`enrich.py` then rewrites `payload["text"]` for **every** event through `gemini-3.1-flash-lite`,
in shuffled batches, blind to incident membership. Run it once with `ac generate --enrich`; it is
cached, so it costs nothing on subsequent runs and reproduces identical text.

This exists to close a real methodological hole. Hand-written noise reuses a handful of canned
strings while planted incidents get bespoke wording, so a model could surface incidents by
spotting distinctive prose rather than by reasoning across systems. Enriching everything through
one blind pass removes that confound.

Two properties make it defensible, and you should be able to state both:
- **Shuffling before batching** guarantees an incident's events land in different batches, so the
  model never sees a causal chain while writing.
- **Enrichment runs against `data/enrich.db`**, so its token spend never enters `llm_calls` for a
  measured run. Dataset construction cost is not inference cost, and conflating them would inflate
  both arms.

If a judge asks whether you built the test you pass, this is the answer: the structure is planted,
the prose is blind, and the near-misses are prose-identical to the real incidents.

INC-5 is deliberately a *silence* signal — the detector must catch absence of activity, not a
spike. Do not drop it; it is the most interesting one to demo.

**Note on silence:** an absence has no events of its own. The `ticket_silence` detector must set
`event_ids` to the last 3 tickets *before* the silence window so the anomaly is still citable as
evidence. INC-5 still reaches two sources via its CRM `contact_departed` event and its billing
usage decline; the silence anomaly is corroboration, not the load-bearing evidence. Verified
counts from the shipped generator: INC-1 spans eng/support/crm (14 events), INC-2 support/billing
(8), INC-3 billing/support/crm (8), INC-4 eng/billing/support (13), INC-5 crm/billing (5).

---

## 5. Tier 0 — Detection (zero tokens)

Four detector workers, one per stream, running as independent asyncio tasks. Each watches only its
own stream and writes anomalies to the blackboard. **No detector knows about any other detector.**

Required detector logic:

| Stream | Detectors |
|---|---|
| crm | `stage_stall` (days-in-stage > p90), `forecast_drop` (Δ amount < −25%) |
| engineering | `delivery_slip` (planned − delivered > threshold), `reopen_spike` (z > 2) |
| support | `ticket_volume_spike` (per-account daily z > 2), `severity_spike`, `ticket_silence` (zero tickets for an account that averaged > 1/day) |
| billing | `usage_drop` (7-day mean vs trailing 21-day mean, < −30%), `invoice_dispute` |

Target output: **60–100 anomalies from ~1000 events.** If you get fewer than 40 or more than 150,
tune thresholds in `config.py` — but tune them on *noise density*, never by peeking at ground truth.

Contract:

```python
async def run_detectors(bb: Blackboard, streams: list[str], *, flood: FloodSpec | None = None,
                        kill: KillSpec | None = None) -> None:
    """Spawn one task per stream. Each writes Anomaly rows. Returns when all tasks finish or are killed."""
```

---

## 6. Tier 1 — Triage (cheap model, small prompts)

One narrow question per anomaly: could a decision-maker plausibly need to act on this?

- Model: `gemini-3.1-flash-lite` on Vertex.
- **Batch 10 anomalies per call.** Do not make one call per anomaly.
- **Thinking budget 0.** This tier answers a yes/no question; internal reasoning here is pure
  cost with no benefit, and thinking tokens bill at the output rate. Turning thinking off in the
  cheap tier and on in the expensive tier is itself an attention-budget decision — log it in
  `DECISIONS.md` and say it out loud during judging.
- `max_output_tokens=1000`, temperature 0, `response_mime_type="application/json"`.
- Output must be JSON only, no prose, no markdown fences.

System prompt (use verbatim):

```
You are a triage filter in a business-signal pipeline. You will receive a batch of detected
anomalies from enterprise systems. For each one, answer a single narrow question: could this
plausibly require attention from a business decision-maker, either on its own or as part of a
larger cross-system pattern?

Be permissive at this stage. A later stage does the expensive reasoning and a deterministic gate
decides what escalates. Your job is only to discard the obviously routine.

Return ONLY a JSON array, no other text, no markdown fences. One object per input anomaly:
[{"anomaly_id": "...", "plausible": true, "reason": "<max 15 words>", "business_hint": "<max 10 words>"}]
```

Target output: **12–20 candidates from 60–100 anomalies.**

Contract:
```python
async def triage(bb: Blackboard, anomalies: list[Anomaly]) -> list[Candidate]:
```

**If a batch returns malformed JSON, retry once with a repair instruction, then fail open** —
mark all anomalies in that batch as `plausible=True` and log it. Failing open is correct here:
a triage error must not silently destroy recall. Log the fail-open in the audit trail.

---

## 7. Tier 2 — Correlation (expensive model, rich context)

The only place frontier-model money is spent.

- Model: `gemini-3.1-pro` on Vertex.
- **One call** with all candidates plus per-account context, if it fits. Two calls maximum.
- Thinking budget 2048. This is where reasoning earns its cost.
- `max_output_tokens=4000`, temperature 0, `response_mime_type="application/json"`.
- Provide: each candidate anomaly's summary, its stream, entity, window, and up to 5 representative
  raw events per anomaly.

System prompt (use verbatim):

```
You are a correlation engine for cross-system business signals. You receive anomalies detected
independently across four enterprise systems: CRM, engineering delivery, customer support, and
billing/usage.

Your job is to propose causal hypotheses that span MORE THAN ONE system. A single-system spike is
not interesting on its own. What matters is a chain: something in one system explains or predicts
something in another, usually for the same account.

For each hypothesis, cite specific event_ids as evidence and state which stream each came from.
Do not invent event_ids. Only cite ids present in the input.

Return ONLY a JSON array, no other text, no markdown fences:
[{
  "title": "<max 12 words>",
  "narrative": "<2-3 sentences: the causal chain and why it matters commercially>",
  "entity_id": "<account id>",
  "evidence": [{"event_id": "...", "stream": "...", "anomaly_id": "..."}],
  "impact": 0.0,
  "confidence": 0.0
}]

impact = estimated commercial consequence, 0 to 1.
confidence = how strongly the evidence supports the causal claim, 0 to 1.
Propose at most 12 hypotheses. Prefer fewer, better-evidenced ones.
```

Contract:
```python
async def correlate(bb: Blackboard, candidates: list[Candidate]) -> list[Hypothesis]:
```

**Validate every cited `event_id` against the blackboard. Drop hallucinated ids and log the drop
count** — hallucination rate is a number worth reporting.

---

## 8. The attention gate (zero tokens, deliberately not a model)

`gate.py`. This is the heart of the judging story. Pure function, fully unit-tested.

```python
def apply_gate(hypotheses: list[Hypothesis], *, cap: int = 7,
               min_sources: int = 2, min_confidence: float = 0.55) -> GateResult:
    """
    Returns escalated signals, rejected hypotheses with reasons, and displacement events.
    Pure and deterministic. No I/O, no model calls, no randomness.
    """
```

Rules, in order:

1. **Deduplicate evidence** by `(stream, event_id)`. The same fact arriving by two paths counts
   once. This is what stops double-counting toward sufficiency.
2. **Sufficiency:** `len({e.stream for e in evidence}) >= min_sources`. Reject with
   `reason="single_source"` otherwise.
3. **Confidence floor:** `confidence >= min_confidence`. Reject with `reason="below_floor"`.
4. **Rank** survivors by `impact * confidence`, descending. Ties broken by evidence count, then
   by hypothesis id for determinism.
5. **Cap:** take the top `cap`. For each hypothesis beyond the cap that outranks the current
   weakest held signal, **displace** it and record a `DisplacementEvent(incoming, displaced,
   incoming_score, displaced_score)`. Otherwise it waits with `reason="attention_budget_full"`.

Every rejection and every displacement is written to the audit trail with its reason. During the
demo you must be able to point at a near-miss and say "this one was rejected here, for this reason."

---

## 9. The naive baseline

`baseline.py`. Deliberately the strongest reasonable version of the dumb approach, so the
comparison is honest.

- Model: `gemini-3.1-pro` with thinking budget 2048 — **identical to Tier 2**. If the baseline
  gets a weaker model or less thinking, the comparison is rigged and a judge will say so.
- Send **all** events, formatted compactly (one line per event), in a single call if under 180k
  input tokens; otherwise chunk by 6-day windows with 2-day overlap and merge results.
- **Watch the long-context cliff.** If the single call crosses 200K input tokens, Pro rates roughly
  double to $4/$18. Do not avoid this — measure it and report it. `config.crossed_long_context()`
  flags it and `llm.py` writes a `LONG_CONTEXT_CLIFF` audit record. A baseline that falls off the
  cliff while the cascade never can is the cost argument at its strongest.
- Same output schema as Tier 2 hypotheses.
- Then apply **the same gate** to its output. Both arms use identical escalation logic, so the
  comparison isolates the cascade, not the gate.

State the chunking honestly in the report if it triggers.

---

## 10. Metering and the report

`metering.py` sums the `llm_calls` table. `report.py` renders `runs/latest_report.md`.

**Table A — the headline:**

| | Naive baseline | Attention Cascade |
|---|---|---|
| Method | all events → Opus 5 | Tier 0 → Tier 1 → Tier 2 |
| LLM calls | | |
| Input tokens | | |
| Visible output tokens | | |
| Thinking tokens (billed as output) | | |
| Crossed the 200K Pro price cliff? | | |
| Cost per run (USD) | | |
| Cost per 1M events/day (USD, extrapolated) | | |
| Incidents found (of 5) | | |
| False escalations | | |
| Wall-clock latency (s) | | |

Mark the extrapolated row clearly as a linear extrapolation, not a measurement.

**Table B — the recall waterfall.** This is the table that shows you understand your own risk:

| Stage | Items | Incidents still recoverable (of 5) |
|---|---|---|
| Raw events | ~1000 | 5 |
| After Tier 0 detection | | |
| After Tier 1 triage | | |
| After Tier 2 correlation | | |
| After sufficiency gate | | |
| In final 7 | | |

An incident is **recoverable at a stage** if events from ≥2 of its streams still survive at that
stage. An incident is **found** if some escalated signal's deduplicated evidence contains events
tagged with that `incident_id` from ≥2 distinct streams. Implement exactly this definition in
`report.py` and write it in the README — a vague recall definition is the first thing a good judge
attacks.

**Cost formula:** use `config.price_call(model, input_tokens, output_tokens, thoughts_tokens,
cached_input_tokens)`. Do not reimplement it. It already handles thinking tokens billing at the
output rate, cached input at 0.1x, and the Pro long-context cliff.

Token counts come from `resp.usage_metadata`: `prompt_token_count`, `candidates_token_count`,
`thoughts_token_count`, `cached_content_token_count`. Never estimate, never use len(text)/4.

### Replication — n=5, not n=1

A single run is an anecdote. Run the full experiment across `config.REPLICATION_SEEDS` (five
datasets, same structure, different noise) and report **mean and min–max range** for every cell in
Table A, not a single number. `ac demo --replicate` does this; `ac demo` runs one seed for speed
during development.

This is cheap — five baseline calls and five cascade runs — and it is the difference between
"here is a number" and "here is a measurement". If recall varies across seeds, say so: variance
is a finding about the architecture, not an embarrassment. If the clock gets tight, drop to three
seeds and label it.

**Table C — cost by tier.** One row per tier: calls, input, visible output, thinking tokens, cost,
and share of total spend. The expected shape is that Tier 2 is a small number of calls holding
most of the cost. If it is not, something is wrong with your batching.

---

## 11. Ground-truth quarantine (mandatory test)

`tests/test_no_groundtruth_leak.py` must:

1. Walk every module in `src/attention_cascade/` except `report.py` and `groundtruth.py`.
2. Parse with `ast` and assert none of them imports `groundtruth` or references the string
   `incident_id`.
3. Assert the `events` table schema contains no incident column.

This test failing is a build-stopping error. It is also a strong thing to show a judge.

---

## 12. Concurrency, backpressure, and shedding

`orchestrator.py`. Plain asyncio, no framework.

- `asyncio.Queue(maxsize=...)` between tiers. `anomaly_q` maxsize 200, `candidate_q` maxsize 50.
- Detectors are producers. Tier 1 runs 3 concurrent workers. Tier 2 runs 1 worker (it is the
  rate-limited bottleneck, and that is the point).
- SQLite: `PRAGMA journal_mode=WAL`, one write lock, `busy_timeout=5000`. All writes go through
  `Blackboard`, which serializes them behind a single `asyncio.Lock`. Say this out loud in judging:
  writes are serialized deliberately so evidence cannot double-count.
- **Backpressure:** producers `await q.put(...)` and block when full. Log every block > 100ms.
- **Load shedding:** if `anomaly_q` has been full for > `SHED_TRIGGER_SECONDS` (default 3), set
  `run.degraded = True`, stop dispatching to Tier 2, and escalate using Tier 1 candidates and the
  gate alone. Write a `SHED` record to the audit trail with the timestamp and queue depth. The
  system gets **cheaper and dumber**, never dead. This is the elegant line: the cost-control
  mechanism *is* the load-shedding mechanism.

---

## 13. LLM layer — PROVIDED, do not rewrite

`llm.py` is written and correct. Read it, then use it. Its contract:

```python
async def call(*, model: str, system: str, prompt: str, max_tokens: int, tier: str,
               run_id: str, bb: Blackboard, thinking_budget: int = 0,
               json_output: bool = True, temperature: float = 0.0) -> LLMResult
```

It handles: Vertex client construction from ADC, the disk cache, replay mode, retry with
exponential backoff, real token extraction including thinking tokens, cost via
`config.price_call`, the long-context cliff flag, and writing every call to `llm_calls` plus the
audit trail. `LLMResult.json()` parses the response, tolerating fences.

`AC_LLM_MODE`: `auto` (cache-then-live), `live` (always call, still caches), `replay` (cache only,
raises on miss so a demo never silently hits the network).

**Commit `runs/cache/` to git.** That is what makes the demo work with no wifi.

**No other module may import `google.genai`.** If you find yourself constructing a client outside
`llm.py`, stop — that call will not be metered and the cost table will be wrong.

## 14. CLI

Entry point `ac` via `[project.scripts]` in `pyproject.toml`. Use `typer`.

```
uv run ac generate                              # build data/events.db from seed_manifest.json
uv run ac run --arm cascade                     # one cascade run
uv run ac run --arm baseline                    # one baseline run
uv run ac report                                # render runs/latest_report.md
uv run ac chaos kill --detector support         # cascade run with a detector killed at t=3s
uv run ac chaos flood --stream support --factor 50
uv run ac demo                                  # THE ONE COMMAND: generate + both arms + report
uv run ac verify                                # preflight: ADC, project, db, cache, tests
uv run ac verify --list-models                  # confirm the config.py model ids exist here
uv run ac review --checkpoint N                 # build review/checkpoint-N/ per REVIEW_PROTOCOL.md
```

`ac demo` must print a live progress line showing the collapse: `1000 events → 84 anomalies →
16 candidates → 11 hypotheses → 7 signals`. That single line is the demo.

---

## 15. Error handling

| Failure | Behaviour |
|---|---|
| No ADC / no `GOOGLE_CLOUD_PROJECT` | `ac verify` fails loudly with the `gcloud auth application-default login` instruction. Other commands suggest `AC_LLM_MODE=replay`. |
| Model id not found in region | `ac verify --list-models` prints what is available; fail with that list, do not silently substitute a model |
| API 429 / quota / 5xx | retry 3× with backoff (handled in `llm.py`), then mark the tier degraded and continue |
| Response truncated by `max_output_tokens` because thinking consumed the budget | detect `finish_reason`, log it, retry once with a larger budget, then fail open |
| Malformed JSON from Tier 1 | retry once, then fail open (all plausible), log it |
| Malformed JSON from Tier 2 | retry once, then salvage with a permissive JSON extractor; if still bad, record zero hypotheses and say so in the report |
| Hallucinated event_id | drop that evidence ref, count it, report it |
| Detector crash | log, mark stream dead in audit trail, keep running |
| SQLite lock | `busy_timeout=5000`, then retry twice |
| Cache miss in replay mode | raise immediately — do not silently go live during a demo |

**Never swallow an exception silently.** Every caught exception writes to the audit trail.

---

## 16. Code conventions

- Python 3.11+, full type hints, `from __future__ import annotations`.
- pydantic v2 for all cross-boundary data. Plain dataclasses for internal-only structs.
- `ruff` for lint and format, line length 100.
- No `print()` in library code — use the `audit()` helper and `rich` for CLI output only.
- Every module gets a 3-line docstring at the top: what it does, what it does not do, why it
  exists. Soumy reads these out loud during judging.
- Deterministic where possible: seed all randomness from `config.SEED`. Two runs of `ac generate`
  must produce byte-identical databases.

---

## 17. Out of scope — state this as judgement, not as gaps

- No learning or feedback loop. It is the obvious next layer, it is not buildable well in the
  time, and a half-working one is worse than none.
- No multi-tenancy, no auth, no persistence beyond the run.
- No real data connectors. Synthetic by design, because ground truth is what makes the cost claim
  falsifiable.
- Minimal UI. Clean terminal output and the results table beat a dashboard on this rubric.

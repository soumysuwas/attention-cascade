# Attention Cascade

A tiered attention cascade that finds cross-system business signals for a fraction of the tokens,
and degrades to cheaper tiers instead of falling over.

Enterprise event streams are mostly noise. Sending all of it to a frontier model finds the
important things and costs a fortune; sampling it is cheap and misses them. This is a four-tier
cascade that spends deterministic arithmetic where arithmetic is enough, and frontier-model
reasoning only on what survives — with a hard cap of seven attention slots enforced by code, never
by a model.

**The model proposes, the math disposes.** No LLM anywhere in this system decides what escalates.

---

## Measured results

Real token counts from `usage_metadata`, real cost from the pricing table in `config.py`. Nothing
here is estimated.

```
collapse        1069 events → 75 anomalies → 75 candidates → 4 hypotheses → 4 signals
recall          3 of 5 planted incidents FOUND  (INC-1, INC-2, INC-3)
false escal.    1 of 4 escalated signals cited no planted incident
hallucinated    0 cited event_ids did not exist
cost            $0.0781   tier1 $0.0145 (18.6%, 8 calls) · tier2 $0.0635 (81.4%, 1 call)
thinking        tier1 0 · tier2 1,448   (billed at the output rate, counted in cost)
latency         88.9s summed across 9 calls · slowest single call 20.1s · 0.1s from cache
```

Tier 1 runs with a thinking budget of 0 and Tier 2 with 2048. The measured `thoughts_token_count`
of exactly 0 and 1,448 is the proof those budgets actually applied. Reasoning is a priced resource
and it is budgeted per tier.

### Recall waterfall

An incident is **recoverable** at a stage if events from ≥2 of its streams still survive there. It
is **found** if some escalated signal's deduplicated evidence contains events tagged with that
incident from ≥2 distinct streams.

| Stage | Items | Incidents still recoverable (of 5) | Which |
|---|---|---|---|
| Raw events | 1069 | 5 | INC-1,2,3,4,5 |
| After Tier 0 detection | 75 | 5 | INC-1,2,3,4,5 |
| After Tier 1 triage | 75 | 5 | INC-1,2,3,4,5 |
| After Tier 2 correlation | 4 | 3 | INC-1,2,3 |
| After sufficiency gate | 4 | 3 | INC-1,2,3 |
| **In final 7 (FOUND)** | 4 | **3** | INC-1,2,3 |

**The entire recall loss is at one stage.** All five incidents survive Tier 0 and Tier 1 intact;
two are lost at Tier 2. INC-4 and INC-5 are present in Tier 2's input, `ac check-linkage` confirms
both are joinable across their streams, and the model still does not propose them — located, not
explained. One of the four escalated signals cites no planted incident at all, so it counts as a
false escalation.

This is worse than an earlier run of the same code, which found 4 of 5. The difference is the
enriched text: re-running enrichment produced different wording for the same structural events,
and Tier 2's output moved with it. That sensitivity is itself the finding, and it is the argument
for the five-seed replication that has not yet been run — a single run is an anecdote, and this
pair of runs demonstrates exactly why.

### Cost by tier

| Arm | Tier | Model | Calls | Input | Visible out | Thinking | Cost USD | Share |
|---|---|---|---|---|---|---|---|---|
| cascade | tier1 | `gemini-3.5-flash-lite` | 8 | 9,323 | 4,692 | 0 | $0.0145 | 18.6% |
| cascade | tier2 | `gemini-3.1-pro-preview` | 1 | 17,256 | 970 | 1,448 | $0.0635 | 81.4% |

**Total: $0.0781.** One call out of nine holds 81% of the spend. That is the shape the design
predicts: the expensive tier should be rare and should dominate cost.

---

## Architecture

```
   crm         engineering      support        billing          4 source streams
    │              │               │              │
    ▼              ▼               ▼              ▼
 ┌────────┐   ┌────────┐     ┌────────┐    ┌────────┐          TIER 0  — detectors
 │stage_  │   │delivery│     │volume_ │    │usage_  │          zero tokens
 │stall   │   │_slip   │     │spike   │    │drop    │          independent asyncio tasks
 │forecast│   │reopen_ │     │severity│    │usage_  │          no detector knows another
 │_drop   │   │spike   │     │silence │    │decline │          exists
 └───┬────┘   └───┬────┘     └───┬────┘    └───┬────┘
     │            │              │             │
     └────────────┴──────┬───────┴─────────────┘
                         ▼
              ╔═══════════════════════╗
              ║      BLACKBOARD       ║   SQLite, WAL, one write lock.
              ║  events · anomalies   ║   Every write attributed and serialized,
              ║  candidates           ║   so evidence cannot double-count.
              ║  hypotheses · signals ║   Append-only audit trail.
              ║  llm_calls · audit    ║
              ╚═══════════╤═══════════╝
                          │  1069 events → 75 anomalies
                          ▼
              ┌───────────────────────┐
              │  TIER 1 — triage      │   gemini-3.5-flash-lite
              │  batches of 10        │   thinking budget 0
              │  3 concurrent workers │   fails OPEN on bad JSON
              └───────────┬───────────┘
                          │  75 candidates
                          ▼
              ┌───────────────────────┐
              │  TIER 2 — correlate   │   gemini-3.1-pro-preview
              │  1 call (2 max)       │   thinking budget 2048
              │  grouped by account   │   every cited event_id validated
              └───────────┬───────────┘
                          │  4 hypotheses
                          ▼
              ┌───────────────────────┐
              │   THE ATTENTION GATE  │   pure function · zero tokens
              │  1. dedup (stream,id) │   no model · no I/O · no randomness
              │  2. ≥2 distinct       │   every refusal carries a reason
              │     sources           │
              │  3. confidence ≥ 0.55 │
              │  4. rank impact×conf  │
              │  5. cap 7, displace   │
              └───────────┬───────────┘
                          ▼
                    ★ 7 SIGNALS ★
```

Bounded queues sit between the tiers (`anomaly_q` maxsize 200). When the anomaly queue stays full
past a threshold the run marks itself degraded, stops dispatching to Tier 2, and escalates on
Tier 1 and the gate alone. **The cost-control mechanism and the load-shedding mechanism are the
same mechanism** — under flood the system gets cheaper and dumber, never dead.

---

## How to run

```bash
uv sync --extra dev

uv run ac generate --enrich        # build the dataset, then rewrite every event's text
uv run ac run --arm cascade        # one full cascade run
```

Offline:

```bash
AC_LLM_MODE=replay uv run ac run --arm cascade
```

`runs/cache/` is committed. Every model response is cached by a sha256 of
(model, system, prompt, params), so `replay` mode serves the entire run from disk and **raises on
a cache miss rather than silently reaching for the network**. The full cascade replays in about
0.1 seconds with networking disabled.

Other commands:

```bash
uv run ac verify --list-models     # probe each configured model id with a real call
uv run ac check-linkage            # assert every planted incident is joinable across its streams
uv run ac stats                    # dataset statistics
uv run pytest -q                   # 94 tests
```

Model access is Application Default Credentials against Vertex AI
(`gcloud auth application-default login`), configured through `.env` — see `.env.example`.

---

## How the dataset works, and why it is not rigged

The generator plants 5 cross-system incidents and 5 near-misses in ~1069 synthetic events across
60 days, 12 accounts and 4 streams. Ground truth lives in its own table; the `events` table has no
incident column, and an AST test walks every pipeline module asserting none of them imports the
ground-truth accessor or names an incident field. Only the scoring code may see the answer key.

Two things make the test honest rather than convenient:

- **Every event's text is rewritten by a model that cannot see incident membership**, in shuffled
  batches so no causal chain is ever visible while writing. Hand-written noise reuses a few canned
  strings while planted incidents get bespoke wording, which would let a model surface incidents by
  spotting distinctive prose rather than by reasoning across systems. Enrichment removes that
  confound. Its token spend goes to a separate database and never enters a measured run.
- **`ac check-linkage` fails the build if any incident's streams cannot be connected.** Joins must
  come from structured fields — `payload["text"]` is excluded on purpose, because a join that
  survives only until enrichment rewrites the prose is not a join. This check exists because that
  exact defect happened: enrichment once severed INC-1's engineering hop and made it undiscoverable
  by any method.

One near-miss, NM-5, is deliberately two-stream: same account, same window, no causal link. The
other four are single-stream and the sufficiency rule rejects them on structure alone. Without
NM-5 nothing in the corpus would ever exercise the confidence floor, and the gate would be getting
credit for an if-statement.

---

## What is measured and what is not

**Measured, and reported above:** the cascade arm, end to end. Every token count comes from
`usage_metadata` on the API response — `prompt_token_count`, `candidates_token_count`,
`thoughts_token_count`, `cached_content_token_count`. Nothing is estimated from string length.
Cost comes from one pricing table, thinking tokens billed at the output rate. Recall is scored
against planted ground truth using the definition stated above.

**Implemented but not yet measured:** the naive baseline arm — all events to
`gemini-3.1-pro-preview` at the same thinking budget as Tier 2, through the same gate — is written
but its comparison table is not populated. **No baseline-versus-cascade cost ratio has been
measured, and this README does not claim one.** The headline saving this architecture is designed
to demonstrate is therefore still an argument, not yet a result.

Also not yet run: the five-seed replication (`REPLICATION_SEEDS` in `config.py`), so every number
above is a single run rather than a mean with a range. A single run is an anecdote — and as the
recall note above records, two runs of this same code over differently-enriched text scored 4/5
and 3/5, which is precisely the variance a replication is meant to expose. The two
failure demos — killing a detector mid-run, and flooding a stream until Tier 2 sheds — are
implemented in `detectors.py` and `orchestrator.py` and exercised by the design, but not yet
captured as reported results.

Stating this plainly costs a nicer-looking README and is worth more than the alternative.

---

## Deliberately out of scope

These are judgement calls, not gaps.

- **No learning or feedback loop.** It is the obvious next layer. It is not buildable well in the
  time available, and a half-working one is worse than none — it would make the recall numbers
  drift for reasons nobody could attribute.
- **No multi-tenancy, auth, or persistence beyond the run.** None of it changes the measurement,
  and all of it costs time that the measurement needs.
- **No real data connectors.** Synthetic by design. Ground truth is what makes the cost claim
  falsifiable; a real dataset would make the demo more impressive and the claim unverifiable.
- **Minimal UI.** Clean terminal output and a results table beat a dashboard here. The deliverable
  is a measured comparison, and a dashboard is a way of not having one.
- **No framework.** Plain asyncio, SQLite and pydantic. No LangGraph, Celery, Docker or ORM —
  nothing that hides the coordination logic, because the coordination logic is the interesting
  part and it has to be readable to be defensible.

---

## License

MIT — see [LICENSE](LICENSE).

# DECISIONS.md

Architectural decisions and their rejected alternatives. This file is the raw material for the
judging answer to "what was the hardest technical decision you made and how did you solve it."

Every entry must name a **cost**. A decision with no cost is a preference.

---

### D-1: Split model judgement from escalation authority
- **Context:** the simplest design lets one strong model read everything and decide what matters.
- **Chose:** models generate hypotheses; deterministic code in `gate.py` decides what escalates.
- **Rejected:** LLM-decided escalation, and its softer variant "LLM proposes a priority score we
  threshold on."
- **Because:** cost scales linearly with event volume, which kills it in production; and a model
  asked "is this important?" says yes far too often, which makes an attention budget meaningless.
- **Cost of this choice:** three failure surfaces instead of one, plus a real risk that Tier 0 or
  Tier 1 discards a genuine incident before the strong model ever sees it. Mitigated by planting
  ground-truth incidents so the recall loss is measured rather than assumed.

### D-2: Synthetic data with planted incidents rather than a real dataset
- **Context:** the cost claim needs a baseline, and a baseline needs ground truth.
- **Chose:** generate the streams, plant 5 cross-system incidents and 4 near-misses.
- **Rejected:** a public log dataset; scraped real tickets.
- **Because:** without knowing which events belong to which incident, recall is unmeasurable and
  the entire cost claim becomes unfalsifiable.
- **Cost of this choice:** a judge can reasonably say "you built the test you pass." Answer that
  head-on: the near-misses are designed to be tempting, and the recall waterfall shows exactly
  where the cascade loses incidents.

### D-3: Shared blackboard over direct agent-to-agent messaging
- **Context:** four detectors, two model tiers, and a gate all need to see each other's output.
- **Chose:** one typed, append-mostly SQLite store; every write timestamped and attributed.
- **Rejected:** direct calls between components; an in-memory pipeline of function calls.
- **Because:** components stay decoupled, a dead detector degrades the system instead of breaking
  it, and every escalation has a complete audit trail.
- **Cost of this choice:** concurrent writes must be serialized explicitly and evidence must be
  deduplicated by source, or the same fact arriving twice double-counts toward sufficiency.

### D-4: Two-source sufficiency instead of a confidence threshold alone
- **Context:** something has to reject the near-misses.
- **Chose:** require evidence from ≥2 distinct source systems, then a confidence floor.
- **Rejected:** confidence threshold alone; a learned classifier.
- **Because:** model confidence is not calibrated and drifts with prompt wording. "Two independent
  systems agree" is a structural property of the evidence, not an opinion about it.
- **Cost of this choice:** genuine single-system emergencies are structurally invisible to this
  design. That is a real limitation and should be stated out loud, not hidden.

<!-- Agent: append D-5 onward below this line -->

### D-5: Verify model availability with a real call, not `models.list()`
- **Context:** `ac verify --list-models` has to prove the ids in `config.py` are usable before any
  measured run. The obvious implementation is to list the publisher catalogue and check membership.
- **Chose:** probe every configured id with an actual two-token metered call through `llm.py`, and
  treat the call result as the authority. The catalogue is still printed, but only as context.
- **Rejected:** membership testing against `client.models.list()`.
- **Because:** the catalogue lies. In this project it listed `gemini-3.1-flash-lite`,
  `gemini-3.7-flash`, `gemini-3.1-pro-preview` and others as available while every one of them
  returned 404 from `us-central1`. A preflight that passes and is then followed by a run that
  404s is worse than no preflight, because it moves the failure to the expensive step.
- **Cost of this choice:** `verify --list-models` now spends real tokens and takes a few seconds
  instead of being free and instant. It is cached after the first run, so the cost is paid once,
  and it lands in `runs/verify/run.db` rather than any measured run's `llm_calls`.

### D-6: `GOOGLE_CLOUD_LOCATION=global` rather than a regional endpoint
- **Context:** every Gemini 3.x id returned 404 from `us-central1`, including ones the regional
  catalogue listed. Only the 2.5 family answered there.
- **Chose:** move the project to the `global` endpoint in `.env`. Tier 1 (`gemini-3.1-flash-lite`)
  and the optional second baseline (`gemini-3.7-flash`) both became callable immediately.
- **Rejected:** pinning to `us-central1` and downgrading the whole cascade to the Gemini 2.5
  family, which was the only family that answered regionally.
- **Because:** `config.py` pricing is written for the 3.x rates, and 2.5 has a different cost
  profile and no comparable flash-lite tier. Changing the endpoint is a `.env` edit; changing the
  model family would have meant editing `PRICING`, which needs human approval.
- **Cost of this choice:** `global` gives up regional data residency and makes latency slightly
  less predictable, which matters for the wall-clock column in Table A. Latency is measured
  per call in `llm_calls`, so the effect is visible rather than hidden.

### D-7: Stop at the missing Tier 2 model instead of substituting the preview id
- **Context:** `gemini-3.1-pro` — the configured Tier 2 and baseline model, and the entire
  headline cost comparison — does not exist in this project on any endpoint. `gemini-3.1-pro-preview`
  does, and answers correctly with thinking enabled.
- **Chose:** fail `ac verify --list-models` loudly with the callable/not-callable table and the
  catalogue, and raise the substitution as the one blocking question of Checkpoint 1.
- **Rejected:** silently pointing `TIER2_MODEL` and `BASELINE_MODEL` at `gemini-3.1-pro-preview`
  and carrying on, which would have kept the checkpoint on schedule.
- **Because:** `PRICING` is keyed by model id and is marked do-not-edit without approval. A
  substituted id would either inherit `gemini-3.1-pro`'s rates without evidence they apply to the
  preview endpoint, or need a new pricing row I am not authorised to invent. Either way the
  headline cost number would rest on an unverified rate, which is exactly the failure mode the
  pricing comment in `config.py` warns about.
- **Cost of this choice:** Checkpoint 1 ships with one box unmet and Checkpoints 2 and 3 cannot
  start until a human answers. Everything not downstream of Tier 2 was completed anyway, so the
  blocked surface is one decision, not one day.

### D-8: Add a repair pass to enrichment rather than accept 1089/1090
- **Context:** the first full enrichment run rewrote 1089 of 1090 events. One support ticket kept
  its canned string `"invoice question"` because the model omitted that id from its batch array.
- **Chose:** patch `enrich.py` with up to three repair rounds that re-request only the ids missing
  from `updates`, in batches of ten, and report any survivors in the returned stats.
- **Rejected:** accepting 99.9% coverage, and the alternative of retrying the whole 40-event batch.
- **Because:** the single un-enriched event is a canned string that appears verbatim elsewhere in
  the corpus, so it is a textual tell of exactly the kind enrichment exists to remove. One event
  will not change a recall number, but "every event was enriched" is a claim made to judges and it
  should be true rather than nearly true. Re-requesting the whole batch would have invalidated that
  batch's cache entry and changed the text of 39 events that were already fine.
- **Cost of this choice:** a change to a `[PROVIDED]` file, and a second cache-key shape for
  repair prompts, so the cache now holds 28 batch entries plus a small number of repair entries.

### D-9: The CLI is a pipeline module and stays blind to ground truth
- **Context:** `ac generate` naturally wants to print how many ground-truth rows it planted, and
  `ac review` needs incident counts and incident chains for the Checkpoint 1 packet.
- **Chose:** keep `cli.py` on the blind side of the quarantine. It calls into `report.py`, which is
  the one module allowed to import `groundtruth.py`. The generate command no longer prints a
  planted-row count at all.
- **Rejected:** adding `cli.py` to the `ALLOWED` set in `tests/test_no_groundtruth_leak.py`, which
  was the one-line fix when that test failed.
- **Because:** the quarantine is only worth anything if the allow-list is short enough that a judge
  can read it and believe it. Widening the list to make a test pass is how a quarantine dies, and
  the CLI is the module most likely to grow a run command later that would then sit inside the
  permitted set.
- **Cost of this choice:** one extra indirection — the packet builder lives in `report.py` rather
  than next to the command that invokes it, so `ac review` is not readable end to end in one file.

### D-10: Score recall from the ground-truth table, not from the seed manifest
- **Context:** `seed_manifest.json` declares each incident's streams, and `groundtruth.py` can read
  which streams were actually tagged. For INC-5 these disagree: the manifest declares
  crm/support/billing, the table holds crm/billing only.
- **Chose:** treat the `ground_truth` table as the authority, and encode the INC-5 gap explicitly in
  `tests/test_generator.py` as `SILENCE_HOP`, with a second test asserting no other incident has one.
- **Rejected:** relaxing the stream assertion to a subset check, which would have made the test pass
  everywhere and quietly tolerated a genuinely under-planted incident.
- **Because:** INC-5's support hop is an *absence* of tickets, and an absence has no events to tag.
  The disagreement is real and defensible, but only for that one incident and only for that reason.
  A subset check would hide the next real under-planting bug.
- **Cost of this choice:** a named exception in the test suite that has to be explained out loud,
  and a manifest that does not literally match the table for one of five incidents.

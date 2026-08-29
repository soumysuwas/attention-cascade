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

### D-11: Accept `gemini-3.1-pro-preview` at GA 3.1 Pro rates (human-approved)
- **Context:** D-7 blocked on a model that does not exist in this project.
- **Chose:** `TIER1_MODEL=gemini-3.5-flash-lite` ($0.30/$2.50), `TIER2_MODEL` and `BASELINE_MODEL`
  `=gemini-3.1-pro-preview` ($2.00/$12.00, cliff $4/$18 above 200K). Both `PRICING` rows added
  under explicit human approval, with the sourcing recorded in a comment beside them.
- **Rejected:** `gemini-3.7-flash` as Tier 2, which is callable but on introductory pricing.
- **Because:** a headline cost number must not rest on a promotional rate that doubles in December.
- **Cost of this choice:** the Tier1:Tier2 input ratio narrows from 8x to 6.7x, so the per-token
  spread flatters the cascade less. That is the honest story — the saving comes from cutting the
  volume reaching the expensive tier, not from the price gap — and the report says so.

### D-12: Structured `component` fields as the cross-system join key
- **Context:** enrichment rewrote prose, and INC-1's only engineering-to-support link was the word
  "Atlas" inside a ticket body. Engineering rows are `entity_id="internal"`, so account equality
  cannot bridge them. Once rewritten, INC-1's engineering hop was undiscoverable by any method.
- **Chose:** every support ticket carries a `component` drawn from an eight-value pool that
  includes `F-Atlas` and `integrations`; incident tickets pin the value that matches their
  engineering counterpart, and INC-4's also carry `release="v3.11"`.
- **Rejected:** giving engineering events an `entity_id` of the affected account, which would have
  made the join a trivial equality check.
- **Because:** delivery trackers really are organised by team, not by customer. Making the
  correlator bridge that asymmetry through a shared component name is both realistic and a
  genuinely harder problem than an account join — and it is the part worth paying a frontier
  model for.
- **Cost of this choice:** the join is now only as good as the component vocabulary. If the
  enricher ever drops the component from the text, the semantic link weakens again, and only the
  structured field remains.

### D-13: `ac check-linkage`, and treating undiscoverability as a build error
- **Context:** the enrichment defect was found by a human reading sample output. That does not scale
  and it does not survive a 2am generator change.
- **Chose:** a command that builds a stream graph per incident, where an edge exists if two streams
  share an `entity_id` or any structured payload value, and fails if any incident's graph is
  disconnected. `payload["text"]` is excluded from join keys on purpose.
- **Rejected:** a test that merely asserts each incident has >= 2 streams tagged, which was already
  passing while the incident was unfindable.
- **Because:** "spans two streams" and "can be connected by something" are different claims, and
  only the second one makes an incident findable. Excluding prose is what makes the check catch
  the enrichment class of defect rather than miss it.
- **Cost of this choice:** the join rule is a heuristic. It accepts any shared string of length
  >= 3, so it can report a link that is technically present but too weak for a model to use. It
  proves discoverability is possible, not that it is easy.

### D-14: NM-5 — a two-stream near-miss, so the confidence floor is actually tested
- **Context:** all four original near-misses were single-stream, so sufficiency rejected them on
  structure and the confidence floor never executed against anything adversarial.
- **Chose:** plant NM-5 as a support spike and a billing dip on one account in one window with no
  causal link, and an innocent cause stated in the data.
- **Rejected:** leaving the floor untested and reporting a gate that was really one if-statement.
- **Because:** a rule that never fires is not evidence of anything. Whether the floor catches NM-5
  is now a measured outcome, and either result is reportable.
- **Cost of this choice:** the false-escalation count can now be non-zero, which makes the headline
  table look worse. That is the correct trade.

### D-15: Suppress normal usage snapshots inside a planted decline window
- **Context:** INC-5 declares "usage decays", but the generator ADDED four declining snapshots
  alongside the account's ordinary ~24k readings. The series wobbled between 18k and 25k and its
  worst 7-day-versus-trailing change was -11%. The decay was not in the data.
- **Chose:** `usage_suppression_windows` removes the account's normal snapshots for the duration of
  a planted decline, so the decline replaces the baseline instead of interleaving with it.
- **Rejected:** lowering `BILLING_USAGE_DROP_PCT` until the detector fired, which would have been
  tuning a threshold to find a phenomenon that did not exist and would have flooded the tier with
  noise.
- **Because:** a detector cannot find what is not there, and scoring recall against a story the
  data does not tell is exactly the unfalsifiable claim this project exists to avoid.
- **Cost of this choice:** the generator now has a second kind of window to keep consistent with
  the incident definitions, and a drift between them would silently weaken an incident again.

### D-16: A separate `usage_decline` detector for slow bleeds
- **Context:** with suppression applied, INC-2 and INC-4 showed clean -61% and -64% cliffs, but
  INC-5's gradual slide still only reached -12.4% against an -18% threshold.
- **Chose:** add `usage_decline` — four or more consecutive readings, mostly monotonic, falling
  more than 20% inside 14 days.
- **Rejected:** lowering the `usage_drop` threshold to -12%, which sits inside the ±8% band that
  ordinary noise already produces.
- **Because:** `usage_drop` compares a 7-day mean to a trailing baseline, so a gradual decay is
  invisible to it by construction — the baseline decays with the signal. A cliff and a slow bleed
  are different phenomena and need different instruments.
- **Cost of this choice:** a detector beyond the four the spec lists for billing, and one more
  threshold set that has to be defended as tuned on noise rather than on ground truth.

### D-17: `invoice_dispute` broadened so it can fire on noise
- **Context:** as first written it triggered only on `plan_change` or `category=billing_dispute`,
  both of which exist only inside a planted incident. It fired exactly once, on INC-3.
- **Chose:** also flag invoices deviating more than 25% from that account's own median.
- **Rejected:** leaving it, since it "worked".
- **Because:** a detector that can only fire on planted structure is not a detector, it is a
  lookup, and it inflates Tier 0 precision with a number that means nothing.
- **Cost of this choice:** it still fires rarely, because the generator issues two identical
  invoices per account. The broadening is correct but the data gives it little to find.

### D-18: Tier 2 sees anomalies grouped by account, not ranked by score
- **Context:** the first working cascade found 2 of 5 incidents. The waterfall showed INC-3 and
  INC-4 surviving Tier 1 and then vanishing at Tier 2 — they were in the model's input and it did
  not propose them.
- **Chose:** restructure the Tier 2 payload as `{accounts: [{entity_id, anomalies}], shared
  engineering context}` instead of a flat score-ordered list. Recall went from 2/5 to 4/5 with no
  change to the prompt, the model, or the budget.
- **Rejected:** editing the verbatim system prompt to demand more hypotheses, and raising the
  thinking budget.
- **Because:** ordering by score scatters one account's CRM, support and billing anomalies across
  seventy-odd entries, so the cross-system chain the model is asked to find is not visible anywhere
  in its input. This is the same defect as the enrichment one in a different costume: the
  information was present but not connectable. Accounts are the join key, so accounts are the
  structure.
- **Cost of this choice:** the payload now encodes an assumption that accounts are the primary
  correlation axis. A genuine cross-ACCOUNT incident — one vendor outage hitting five customers —
  would be harder for this shape to surface, and none is planted, so that weakness is untested.

### D-19: Two calls, if ever needed, split by account rather than by rank
- **Context:** the two-call fallback originally sliced the score-ordered list in half.
- **Chose:** keep whole accounts together and duplicate the shared `internal` engineering context
  into both calls.
- **Rejected:** an even split by rank, and dropping the overflow.
- **Because:** splitting by rank severs the same chains the grouping exists to preserve, and a
  hard per-call cap silently dropped every candidate past the 40th — recall loss that no number in
  the report would have shown.
- **Cost of this choice:** the shared engineering context is paid for twice when two calls happen.
  With the single-call threshold now at 90 anomalies this path is rare, and it costs correctness
  nothing when it does fire.

### D-20: Report Tier 1's permissiveness rather than tune it away
- **Context:** Tier 1 marks 75 of 75 anomalies plausible. The spec expected 12-20 candidates.
- **Chose:** leave the verbatim prompt alone and report the result, including in the collapse line
  where the flat segment is plainly visible.
- **Rejected:** rewriting the prompt to force selectivity, or thresholding on a model-produced
  score.
- **Because:** the prompt says "Be permissive at this stage", and the model is obeying it. Tuning
  until the number matched the spec's expectation would be fitting the measurement to the
  prediction. All volume reduction is therefore happening at Tier 0, which is a finding about
  where the cheap win actually lives.
- **Cost of this choice:** Tier 1 currently costs $0.0145 and removes nothing, so on this dataset
  it is pure overhead. Its value would appear at a noise density where Tier 0 emits far more.

### D-21: The review packet replays into a scratch database
- **Context:** building the packet re-ran the cascade against the finished run's own database,
  appending a second set of `llm_calls` rows. Table C reported $0.1675, 16 Tier 1 calls and 2 Tier
  2 calls for a run that was really $0.0838, 8 calls and 1 call.
- **Chose:** copy the events into `runs/_packet/`, replay there from cache, and read the cost
  tables from the measured run's untouched database.
- **Rejected:** deduplicating `llm_calls` by run_id when reporting, which would have papered over a
  writer that should not have been writing.
- **Because:** the measured run is the evidence. Anything that writes to it during reporting can
  corrupt the number being reported, and a cost table that doubles under observation is worse than
  no cost table.
- **Cost of this choice:** the packet's figures come from one database and its hypotheses from
  another. They agree only because replay is deterministic from the cache, which is a property
  worth stating rather than assuming.

# progress.md

Agent working log. Append one entry per iteration. Never delete entries.

## Current status
- **Current checkpoint:** 1 — Data foundation
- **Progress:** complete except one box — see Blockers
- **Blockers:** `gemini-3.1-pro` (TIER2_MODEL and BASELINE_MODEL) is not callable in this
  project on any endpoint. `gemini-3.1-pro-preview` is. Not substituting without approval; see
  D-7. Checkpoints 2 and 3 are blocked on this answer, nothing else is.

---

## Iteration log

### Iteration 0 — scaffold received
- **Goal:** establish baseline state before autonomous work begins
- **Prediction:** repo contains CLAUDE.md, SPEC.md, CHECKPOINTS.md, AGENT_INSTRUCTIONS.md,
  pyproject.toml, and four PROVIDED source modules
- **Did:** nothing yet — this entry is the template anchor
- **Verified by:** n/a
- **Outcome:** n/a
- **Notes:** read CLAUDE.md and SPEC.md fully before iteration 1
- **Next:** `uv sync`, then `uv run pytest` to see what exists, then build `groundtruth.py`

### Iteration 1 — read the constitution, probe the provider
- **Goal:** establish what actually works before writing anything; Checkpoint 1
- **Prediction:** the six PROVIDED files run as shipped and the three configured model ids resolve
- **Did:** read CLAUDE.md, SPEC.md, CHECKPOINTS.md, AGENT_INSTRUCTIONS.md, REVIEW_PROTOCOL.md and
  all six PROVIDED modules; replayed the human's earlier terminal log; probed Vertex directly
- **Verified by:** a scratch `generate_content` loop over the configured ids in two locations
- **Outcome:** partial — one real blocker found
- **Notes:** two surprises. (1) every Gemini 3.x id 404s from `us-central1` but answers from
  `global`. (2) `models.list()` is not a availability check — it listed four ids that all 404'd.
  `gemini-3.1-pro` does not exist anywhere reachable; only `gemini-3.1-pro-preview` does.
- **Next:** `__init__.py` dotenv load so config reads a populated environment

### Iteration 2 — package init, groundtruth.py
- **Goal:** the quarantined accessors, and .env loaded before config.py reads os.environ
- **Prediction:** `groundtruth.stats()` reports 5 incidents / 4 near-misses matching SPEC §4's
  verified counts of 14/8/8/13/5
- **Did:** wrote `__init__.py` (dotenv only) and `groundtruth.py` (Incident dataclass, `load`,
  `incidents`, `near_misses`, `event_to_incidents`, `stats`)
- **Verified by:** `uv run python -c "from attention_cascade import groundtruth as g; g.stats()"`
- **Outcome:** success — 5/4, and per-incident counts 14/8/8/13/5 exactly as SPEC §4 states
- **Next:** report.py, so the CLI has somewhere to put ground-truth-touching packet logic

### Iteration 3 — report.py: dataset artefacts and the packet builder
- **Goal:** dataset_stats, sample_events, enrichment_check, determinism_check, build_packet
- **Prediction:** dataset_stats prints 1090 events inside the 900–1100 band with 4 streams
- **Did:** wrote `report.py`; the packet builder captures pytest and ruff as subprocesses so the
  packet contains proof rather than assertion, per REVIEW_PROTOCOL
- **Verified by:** `uv run python -c "from attention_cascade import report; report.dataset_stats()"`
- **Outcome:** success
- **Next:** cli.py

### Iteration 4 — cli.py with generate / verify / review / stats
- **Goal:** the `ac` entry point, and a `verify --list-models` that cannot lie
- **Prediction:** `verify --list-models` fails and names `gemini-3.1-pro` as missing
- **Did:** wrote `cli.py`; `_probe` makes a real metered two-token call per configured id, into
  `runs/verify/run.db` so probe spend never lands in a measured run (D-5)
- **Verified by:** `uv run ac verify --list-models`
- **Outcome:** success — exits 1, prints the callable/NO table, names the missing id, refuses to
  substitute, and dumps the catalogue with a note that the catalogue over-reports
- **Notes:** `.env` moved to `GOOGLE_CLOUD_LOCATION=global` (D-6). Tier 1 and the optional second
  baseline became callable; Tier 2 did not.
- **Next:** enrichment, which needs only the Tier 1 model and is therefore not blocked

### Iteration 5 — enrichment, and the one event that got away
- **Goal:** rewrite every event's text through the blind pass
- **Prediction:** 1090/1090 rewritten, 0 failed batches
- **Did:** ran `ac generate --enrich`
- **Verified by:** the command output, then a scan for surviving canned strings
- **Outcome:** partial — 1089/1090. `evt_00360` kept `"invoice question"` because the model
  omitted that id from its batch array. Not a failed batch, so nothing reported it.
- **Notes:** a single canned string is exactly the textual tell enrichment exists to remove, so
  "nearly every event" is not good enough here
- **Next:** repair pass

### Iteration 6 — repair pass in enrich.py
- **Goal:** close the 1089/1090 gap without invalidating the 28 good batch cache entries
- **Prediction:** 1090/1090, and the second run still reproduces identical text from cache
- **Did:** patched `enrich.py` with up to 3 repair rounds over ids missing from `updates`, in
  batches of 10; added `unrewritten` to the returned stats so a future gap reports itself (D-8)
- **Verified by:** `uv run ac generate --enrich`, then
  `AC_LLM_MODE=replay uv run ac generate --enrich`, comparing a sha256 over every (id, text) pair
- **Outcome:** success — 1090/1090; replay run took 0.44s with zero network and produced the
  identical sha `3c2ad09cd5adc068`
- **Notes:** enrichment spend is $0.3148 over 115 calls, all in `data/enrich.db`, none in any
  run's `llm_calls`
- **Next:** the two test files Checkpoint 1 requires

### Iteration 7 — test_generator.py, and INC-5's missing stream
- **Goal:** assert each incident spans its declared streams
- **Prediction:** all five incidents match their declared streams
- **Did:** wrote `tests/test_generator.py` (17 tests: size band, stream split, 5/4 counts,
  per-incident stream spans, near-miss single-source, account joins, INC-5 silence window,
  determinism, schema shape)
- **Verified by:** `uv run pytest tests/test_generator.py -q`
- **Outcome:** partial then success — INC-5 declares crm/support/billing but only tags crm/billing
- **Notes:** not a bug. INC-5's support hop is an absence of tickets and an absence has no events
  to tag; SPEC §4 states the verified count as crm/billing (5). Encoded as an explicit
  `SILENCE_HOP` exception plus a second test asserting no other incident has one, rather than
  relaxing to a subset check that would hide the next real under-planting (D-10).
- **Next:** test_blackboard.py

### Iteration 8 — test_blackboard.py
- **Goal:** test the coordination guarantees the architecture claims, not just the CRUD
- **Prediction:** 200 concurrent writes from four simulated detectors produce exactly 200 rows
- **Did:** wrote `tests/test_blackboard.py` (21 tests): round-trips for every typed object, arm
  scoping so the two arms cannot contaminate each other, thinking tokens in their own column,
  append-only audit mirrored to jsonl, WAL mode, and the two concurrency claims — serialized
  writes lose nothing, and a crashing writer does not wedge the survivors
- **Verified by:** `uv run pytest tests/ -q`
- **Outcome:** success, after one failure worth keeping
- **Notes:** the quarantine test failed on `cli.py`, which referenced `stats['ground_truth_rows']`.
  Fixed by removing the print rather than by widening the test's ALLOWED set (D-9).
- **Next:** ruff, then the checkpoint packet

### Iteration 9 — ruff clean
- **Goal:** `ruff check` clean before the checkpoint, per AGENT_INSTRUCTIONS
- **Prediction:** mostly E501
- **Did:** `ruff check --fix` (17 auto), then hand-wrapped 22 long lines and removed one genuinely
  dead local (`d` in `generator._plant_incidents`, F841)
- **Verified by:** `uv run ruff check`; then re-ran generate+enrich and compared the text sha
- **Outcome:** success — clean, 46 tests pass, sha still `3c2ad09cd5adc068`
- **Notes:** the auto-fixes touched three PROVIDED files (`timezone.utc` → `UTC`, an unused
  `Path` import, `typing.Iterable` → `collections.abc`). All cosmetic, none behavioural, dataset
  byte-identical afterwards. Flagging rather than hiding it: PROVIDED files were edited.
- **Next:** SCORECARD, CHECKPOINT_REPORT, `ac review --checkpoint 1`, commit, stop

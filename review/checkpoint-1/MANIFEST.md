# Review packet — Checkpoint 1: Data foundation
Generated: 2026-08-29T10:48:06+00:00   Commit: a5eefaa

## Claim
The dataset foundation is built, deterministic, quarantined from the pipeline, and enriched so planted incidents are not textually distinguishable from noise. One box is NOT met: the configured Tier 2 / baseline model id does not exist in this project, and per SPEC I stopped rather than substituting one.

## Files in this packet
- `CHECKPOINT_REPORT.md` — the claim being made, box by box
- `DECISIONS.md` — the trade-offs, each with its rejected alternative and its cost
- `SCORECARD.md` — metric and rubric trend across checkpoints
- `dataset_stats.txt` — counts by stream/kind/account, planted detail, determinism check
- `enrichment_check.txt` — 10 noise texts vs 10 incident texts, labels stripped
- `git_log.txt` — commit hygiene, and proof of no unwanted attribution
- `model_availability.txt` — which config.py model ids are actually callable in this project
- `progress.md` — how I got here, including the dead ends
- `ruff_output.txt` — captured `uv run ruff check`
- `sample_events.txt` — 20 events per stream, plus an incident chain and a near-miss chain
- `seed_manifest.json` — the incident definitions the dataset was built from
- `test_output.txt` — captured `uv run pytest -v`
- `tree.txt` — every file that actually exists in the repo

## The three things I am least confident about
1. Whether the planted incidents are genuinely findable by a smart human reading raw events — this is the one thing I cannot judge for myself.
2. Whether the near-misses are tempting enough to be a real test of the gate, particularly NM-4, which is a single CRM event.
3. Whether enrichment left any structural tell (length, jargon density) that separates incident text from noise text even though the wording is model-written.

## Specific questions for the reviewer
- gemini-3.1-pro is not callable in this project; gemini-3.1-pro-preview is. Approve the substitution for TIER2_MODEL and BASELINE_MODEL, or point at another project?
- Do the INC-1 and NM-1 chains in sample_events.txt read as plausible enterprise data?

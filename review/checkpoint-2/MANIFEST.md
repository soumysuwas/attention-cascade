# Review packet — Checkpoint 2: Cascade runs end to end with real numbers
Generated: 2026-08-29T11:35:50+00:00   Commit: 543390b

## Claim
The cascade runs end to end on real metered Vertex calls. 1069 events collapse to 75 anomalies, 75 candidates, 4 hypotheses and 4 signals. Thinking tokens are 0 at Tier 1 and non-zero at Tier 2, proving the per-tier budgets applied. Recall is 4 of 5 planted incidents; INC-4 is missed and the waterfall shows exactly where.

## Files in this packet
- `CHECKPOINT_REPORT.md` — the claim being made, box by box
- `DECISIONS.md` — the trade-offs, each with its rejected alternative and its cost
- `SCORECARD.md` — metric and rubric trend across checkpoints
- `anomalies_sample.txt` — see checkpoint report
- `collapse_line.txt` — see checkpoint report
- `cost_by_tier.md` — see checkpoint report
- `gate_trace.txt` — see checkpoint report
- `git_log.txt` — commit hygiene, and proof of no unwanted attribution
- `hypotheses.json` — see checkpoint report
- `linkage.txt` — proof each planted incident is joinable across its streams post-enrichment
- `llm_calls.csv` — see checkpoint report
- `progress.md` — how I got here, including the dead ends
- `recall_waterfall.md` — see checkpoint report
- `ruff_output.txt` — captured `uv run ruff check`
- `src___init__.py.txt` — see checkpoint report
- `src_blackboard.py.txt` — see checkpoint report
- `src_cli.py.txt` — see checkpoint report
- `src_config.py.txt` — see checkpoint report
- `src_correlate.py.txt` — see checkpoint report
- `src_detectors.py.txt` — see checkpoint report
- `src_enrich.py.txt` — see checkpoint report
- `src_gate.py.txt` — see checkpoint report
- `src_generator.py.txt` — see checkpoint report
- `src_groundtruth.py.txt` — see checkpoint report
- `src_llm.py.txt` — see checkpoint report
- `src_models.py.txt` — see checkpoint report
- `src_orchestrator.py.txt` — see checkpoint report
- `src_report.py.txt` — see checkpoint report
- `src_triage.py.txt` — see checkpoint report
- `test_output.txt` — captured `uv run pytest -v`
- `tree.txt` — every file that actually exists in the repo

## The three things I am least confident about
1. Tier 1 discarded nothing (75 of 75 plausible), so all volume reduction happens at Tier 0. The spec expected 12-20 candidates. I did not tune the prompt to force it.
2. INC-4 is recoverable right up to Tier 2 and is still not proposed. I know where it is lost but not yet why the model declines to propose it.
3. Only 4 hypotheses come back from 75 anomalies. The prompt says 'prefer fewer', so this may be correct behaviour or may be under-production I am reading charitably.

## Specific questions for the reviewer
- Tier 1 passing everything: force selectivity by changing the prompt, or report the permissive instruction as the cause and let Tier 0 own the reduction?
- Is 4 of 5 acceptable for Checkpoint 2, given the waterfall localises the loss?

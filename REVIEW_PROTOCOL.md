# REVIEW_PROTOCOL.md

How checkpoint review works. The agent follows this without being asked.

---

## The rule

When you reach a checkpoint N, you **copy** (never move) everything a reviewer needs into
`review/checkpoint-N/`, then print the manifest and stop. Soumy uploads that one folder to chat.
Nothing else should be needed for a full review — if the reviewer has to ask "can you also send
X", the packet was incomplete and that is a defect.

Copies, not links. Copies, not summaries. The reviewer cannot see your filesystem.

Implement this as `ac review --checkpoint N` so it is one command and cannot be half-done.

---

## What goes in every packet, at every checkpoint

| File in `review/checkpoint-N/` | Source | Why the reviewer needs it |
|---|---|---|
| `CHECKPOINT_REPORT.md` | repo root | the claim being made |
| `SCORECARD.md` | repo root | the metric trend across checkpoints |
| `progress.md` | repo root | how you got here, including dead ends |
| `DECISIONS.md` | repo root | the trade-offs, which is what judging rewards |
| `test_output.txt` | `uv run pytest -v` captured | proof, not assertion |
| `ruff_output.txt` | `uv run ruff check` captured | proof, not assertion |
| `tree.txt` | `find . -type f -not -path './.venv/*' ...` | what actually exists |
| `git_log.txt` | `git log --oneline -20` | commit hygiene, and no unwanted attribution |

## What each checkpoint adds on top

**Checkpoint 1 — Data foundation**
- `seed_manifest.json`
- `sample_events.txt` — 20 events per stream, plus one full incident chain and one near-miss
  chain printed side by side. This is the artifact the human eyeballs for plausibility.
- `dataset_stats.txt` — counts by stream, incidents, near-misses, determinism check result
- `enrichment_check.txt` — 10 noise events and 10 incident events side by side, text only, so the
  reviewer can confirm they are not textually distinguishable

**Checkpoint 2 — Cascade runs**
- `collapse_line.txt` — the `1000 → 84 → 16 → 11 → 7` output
- `anomalies_sample.txt` — 15 anomalies with detector, score, summary
- `hypotheses.json` — everything Tier 2 proposed, before the gate
- `gate_trace.txt` — every hypothesis with its verdict and reason
- `llm_calls.csv` — the full `llm_calls` table, including the thinking-token column
- `src/` snapshot — copy of every source file as `src_<name>.py.txt`

**Checkpoint 3 — The table**
- `latest_report.md` — Table A and Table B in full
- `llm_calls.csv` for both arms
- `cost_breakdown.txt` — per tier: calls, input, visible output, thinking tokens, cost, and
  what share of total spend each tier took
- `replication.md` — per-seed results and the mean/range for every Table A cell
- `long_context_flag.txt` — did either arm cross the Pro 200K price cliff, and where

**Checkpoint 4 — Failure demos**
- `audit_kill.jsonl` and `audit_flood.jsonl`
- `failure_comparison.md` — signals and cost for normal vs killed vs flooded, side by side
- `shed_trace.txt` — queue depths over time and the moment Tier 2 was shed

**Checkpoint 5 — Shippable**
- `README.md`, `FILE_TOUR.md`, `docs/architecture.md`, `docs/demo_script.md`
- `replay_verification.txt` — output of `AC_LLM_MODE=replay uv run ac demo` with networking off
- `repo_url.txt`

---

## The manifest

Every packet contains `MANIFEST.md`, generated last:

```markdown
# Review packet — Checkpoint <N>: <name>
Generated: <timestamp>   Commit: <short sha>

## Claim
<one sentence: what I am asking to be reviewed>

## Files in this packet
- <file> — <one line on what it shows>

## The three things I am least confident about
1.
2.
3.

## Specific questions for the reviewer
<only things you genuinely cannot resolve yourself>
```

The "least confident" section is mandatory and must be honest. A packet claiming everything is
fine is a packet that gets a slower review.

---

## Rules

- Never delete a previous checkpoint's folder. The history is the point.
- Never edit files inside `review/` after generating the packet. If something changes, regenerate.
- Redact nothing except credentials. If an API error leaked a project id, scrub it and say so.
- `review/` is committed to git. It is part of the story of how the thing was built.

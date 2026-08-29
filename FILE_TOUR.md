# FILE_TOUR.md

One line per source file: what it does and why it exists. The agent fills this in at Checkpoint 5.
You read it out loud during judging. If a file cannot be described in one honest line, it should
not be in the repo.

| File | What it does | Why it exists |
|---|---|---|
| `config.py` | Every tunable constant: pricing, thresholds, model ids | So a judge asking "where did $5/MTok come from" gets one answer |
| `models.py` | Pydantic types crossing tier boundaries | Four workers share a blackboard, so shapes are agreed once |
| `blackboard.py` | SQLite shared store, serialized writes, audit trail | Indirect coordination is what lets a detector die without killing the run |
| `generator.py` | Synthetic streams with 5 planted incidents, 4 near-misses | No ground truth means no baseline means no falsifiable cost claim |
| `groundtruth.py` | | |
| `llm.py` | | |
| `detectors.py` | | |
| `triage.py` | | |
| `correlate.py` | | |
| `gate.py` | | |
| `orchestrator.py` | | |
| `baseline.py` | | |
| `metering.py` | | |
| `report.py` | | |
| `chaos.py` | | |
| `cli.py` | | |

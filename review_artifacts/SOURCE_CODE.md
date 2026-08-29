# Source snapshot — every module, in full
15 files, concatenated because the upload accepts at most 19.

## Contents

- `__init__.py`
- `blackboard.py`
- `cli.py`
- `config.py`
- `correlate.py`
- `detectors.py`
- `enrich.py`
- `gate.py`
- `generator.py`
- `groundtruth.py`
- `llm.py`
- `models.py`
- `orchestrator.py`
- `report.py`
- `triage.py`


---

## `src/attention_cascade/__init__.py`

```python
"""Package init. Loads .env before any module reads os.environ.

Does: pull GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION / AC_LLM_MODE out of .env into the
process environment, once, before config.py is imported by anything.
Does not: hold any logic. Nothing else belongs here.
Exists because: config.py reads os.environ at import time, so the .env load has to happen
strictly earlier than the first `from . import config` anywhere in the package.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
```

---

## `src/attention_cascade/blackboard.py`

```python
"""The shared blackboard: one typed, append-mostly SQLite store all workers read and write.

Does: hold events, anomalies, candidates, hypotheses, signals, llm_calls and an append-only audit
trail; serialize every write behind a single lock so evidence cannot double-count.
Does not: know anything about tiers, models, or ground truth. It is dumb storage with an audit log.
Exists because: indirect coordination through shared state is what lets a detector die without
taking the system with it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Anomaly, Candidate, Event, Hypothesis, LLMCall, Signal

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

-- NOTE: deliberately no incident_id column here. Ground truth lives in its own table
-- and is readable only by report.py and tests. See CLAUDE.md rule 2.
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    stream TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    numeric REAL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_stream_ts ON events(stream, ts);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, ts);

CREATE TABLE IF NOT EXISTS ground_truth (
    incident_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    is_near_miss INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (incident_id, event_id)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id TEXT PRIMARY KEY,
    detector TEXT NOT NULL,
    stream TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    score REAL NOT NULL,
    kind TEXT NOT NULL,
    event_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    author TEXT NOT NULL,
    written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    anomaly_id TEXT PRIMARY KEY,
    plausible INTEGER NOT NULL,
    reason TEXT,
    business_hint TEXT,
    model TEXT,
    failed_open INTEGER NOT NULL DEFAULT 0,
    written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    arm TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    impact REAL NOT NULL,
    confidence REAL NOT NULL,
    written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    hypothesis_id TEXT PRIMARY KEY,
    arm TEXT NOT NULL,
    rank INTEGER NOT NULL,
    escalated_at TEXT NOT NULL,
    displaced_hypothesis_id TEXT
);

CREATE TABLE IF NOT EXISTS rejections (
    hypothesis_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (hypothesis_id, arm)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    from_cache INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Blackboard:
    """Shared state. Every write is attributed and timestamped; writes are serialized."""

    def __init__(self, path: Path, run_id: str = "default", arm: str = "cascade") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.arm = arm
        self.conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = asyncio.Lock()
        self.audit_path = self.path.parent / "audit.jsonl"

    # ---------------- audit ----------------

    def audit(self, author: str, kind: str, detail: Any = "") -> None:
        """Append-only log. Synchronous and cheap on purpose - it must never be skipped."""
        payload = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        self.conn.execute(
            "INSERT INTO audit (ts, author, kind, detail) VALUES (?,?,?,?)",
            (_now(), author, kind, payload),
        )
        self.conn.commit()
        with self.audit_path.open("a") as fh:
            record = {"ts": _now(), "author": author, "kind": kind, "detail": detail}
            fh.write(json.dumps(record, default=str) + "\n")

    # ---------------- events ----------------

    def insert_events(self, events: Iterable[Event]) -> int:
        rows = [
            (e.id, e.ts.isoformat(), e.stream, e.entity_id, e.kind, e.numeric,
             json.dumps(e.payload))
            for e in events
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO events (id, ts, stream, entity_id, kind, numeric, payload)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def events(self, stream: str | None = None) -> list[Event]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if stream:
            sql += " WHERE stream = ?"
            args = (stream,)
        sql += " ORDER BY ts, id"
        return [self._row_to_event(r) for r in self.conn.execute(sql, args)]

    def event_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM events")}

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            id=r["id"],
            ts=datetime.fromisoformat(r["ts"]),
            stream=r["stream"],
            entity_id=r["entity_id"],
            kind=r["kind"],
            numeric=r["numeric"],
            payload=json.loads(r["payload"]),
        )

    # ---------------- ground truth (write side only; reads live in groundtruth.py) ----------------

    def insert_ground_truth(self, rows: Iterable[tuple[str, str, str, bool]]) -> int:
        data = [(i, e, s, int(nm)) for (i, e, s, nm) in rows]
        self.conn.executemany(
            "INSERT OR REPLACE INTO ground_truth (incident_id, event_id, stream, is_near_miss)"
            " VALUES (?,?,?,?)",
            data,
        )
        self.conn.commit()
        return len(data)

    # ---------------- anomalies ----------------

    async def write_anomaly(self, a: Anomaly, author: str) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO anomalies"
                " (id, detector, stream, entity_id, window_start, window_end, score, kind,"
                "  event_ids, summary, author, written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a.id, a.detector, a.stream, a.entity_id,
                    a.window_start.isoformat(), a.window_end.isoformat(),
                    a.score, a.kind, json.dumps(a.event_ids), a.summary, author, _now(),
                ),
            )
            self.conn.commit()

    def anomalies(self) -> list[Anomaly]:
        out = []
        for r in self.conn.execute("SELECT * FROM anomalies ORDER BY score DESC, id"):
            out.append(
                Anomaly(
                    id=r["id"], detector=r["detector"], stream=r["stream"],
                    entity_id=r["entity_id"],
                    window_start=datetime.fromisoformat(r["window_start"]),
                    window_end=datetime.fromisoformat(r["window_end"]),
                    score=r["score"], kind=r["kind"],
                    event_ids=json.loads(r["event_ids"]), summary=r["summary"],
                )
            )
        return out

    # ---------------- candidates / hypotheses / signals ----------------

    async def write_candidate(self, c: Candidate) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO candidates"
                " (anomaly_id, plausible, reason, business_hint, model, failed_open, written_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (c.anomaly_id, int(c.plausible), c.reason, c.business_hint, c.model,
                 int(c.failed_open), _now()),
            )
            self.conn.commit()

    async def write_hypothesis(self, h: Hypothesis) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO hypotheses"
                " (id, arm, title, narrative, entity_id, evidence, impact, confidence, written_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (h.id, self.arm, h.title, h.narrative, h.entity_id,
                 json.dumps([e.model_dump() for e in h.evidence]), h.impact, h.confidence, _now()),
            )
            self.conn.commit()

    def hypotheses(self) -> list[Hypothesis]:
        out = []
        for r in self.conn.execute("SELECT * FROM hypotheses WHERE arm = ?", (self.arm,)):
            out.append(
                Hypothesis(
                    id=r["id"], title=r["title"], narrative=r["narrative"],
                    entity_id=r["entity_id"], evidence=json.loads(r["evidence"]),
                    impact=r["impact"], confidence=r["confidence"],
                )
            )
        return out

    def write_signals(self, signals: list[Signal]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO signals (hypothesis_id, arm, rank, escalated_at,"
            " displaced_hypothesis_id) VALUES (?,?,?,?,?)",
            [(s.hypothesis_id, self.arm, s.rank, s.escalated_at.isoformat(),
              s.displaced_hypothesis_id) for s in signals],
        )
        self.conn.commit()

    def signals(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM signals WHERE arm = ? ORDER BY rank", (self.arm,)))

    # ---------------- metering ----------------

    def write_llm_call(self, call: LLMCall) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls (run_id, arm, tier, model, input_tokens, output_tokens,"
            " thinking_tokens, cached_input_tokens, from_cache, latency_ms, cost_usd, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (call.run_id, self.arm, call.tier, call.model, call.input_tokens, call.output_tokens,
             call.thinking_tokens, call.cached_input_tokens, int(call.from_cache),
             call.latency_ms, call.cost_usd, call.ts.isoformat()),
        )
        self.conn.commit()

    # ---------------- run metadata ----------------

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO run_meta (key, value) VALUES (?,?)",
                          (key, json.dumps(value, default=str)))
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM run_meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def close(self) -> None:
        self.conn.close()
```

---

## `src/attention_cascade/cli.py`

```python
"""The `ac` command line. One entry point for generation, verification and review packets.

Does: expose generate / verify / review as typer commands and print human-readable output.
Does not: contain any pipeline logic or any knowledge of incidents — it orchestrates modules and
formats their results, nothing more.
Exists because: rule 6 says one command has to run the whole thing, and a reviewer should never
have to reconstruct an invocation from a docstring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config as C
from . import enrich as enrich_mod
from . import generator, llm, report
from .blackboard import Blackboard
from .gate import trace as gate_trace
from .orchestrator import run_cascade

app = typer.Typer(add_completion=False, help="Attention Cascade — tiered signal detection.")
console = Console()

# The ids this project is configured to use, and what each one is for.
CONFIGURED_MODELS = {
    "tier1 triage": C.TIER1_MODEL,
    "tier2 correlation": C.TIER2_MODEL,
    "naive baseline": C.BASELINE_MODEL,
    "optional 2nd baseline": C.MIDTIER_MODEL,
}


# --------------------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------------------

@app.command()
def generate(
    enrich: bool = typer.Option(False, "--enrich",
                                help="Rewrite every event's text via a blind model pass."),
    seed: int = typer.Option(C.SEED, help="Dataset seed. Same seed produces identical event ids."),
) -> None:
    """Build data/events.db from the planted incident definitions."""
    stats = generator.generate(seed=seed)
    console.print(f"[green]generated[/] {stats['event_count']} events -> {stats['db']}")
    for stream, n in sorted(stats["by_stream"].items()):
        console.print(f"    {stream:<12} {n:>5}")
    # Deliberately not printed here: the count of planted rows. The CLI is a pipeline module and
    # the quarantine test forbids it naming the answer key. `ac stats` reads it via report.py.

    lo, hi = C.TARGET_EVENT_COUNT
    if not lo <= stats["event_count"] <= hi:
        console.print(f"[red]event count {stats['event_count']} outside target band {lo}-{hi}[/]")
        raise typer.Exit(1)

    if enrich:
        console.print("[cyan]enriching[/] every event's text (blind, shuffled, cached)...")
        e = asyncio.run(enrich_mod.enrich(seed=seed))
        console.print(f"    rewritten {e['rewritten']}/{e['events']} across "
                      f"{e['batches']} batches, "
                      f"{e['failed_batches']} failed")
        console.print(f"    spend recorded in {C.DATA_DIR / 'enrich.db'}, "
                      "NOT in any run's llm_calls")
        if e["failed_batches"]:
            console.print("[red]enrichment had failed batches — those events kept "
                          "their canned text[/]")
            raise typer.Exit(1)


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------

def _probe(model: str) -> tuple[bool, str]:
    """Actually call the model with a two-token prompt.

    `models.list()` returns the publisher catalogue, which includes ids this project cannot
    call — it reported four available models here that all returned 404. A real call is the
    only honest availability check, and it is metered like everything else.
    """
    bb_path = C.RUNS_DIR / "verify" / "run.db"
    from .blackboard import Blackboard  # noqa: PLC0415 - only needed on this path

    bb = Blackboard(bb_path, run_id="verify", arm="verify")
    try:
        res = asyncio.run(llm.call(
            model=model, system="Reply with exactly: ok", prompt="ping",
            max_tokens=1024, tier="verify", run_id="verify", bb=bb,
            thinking_budget=0, json_output=False,
        ))
        return True, f"in={res.input_tokens} out={res.output_tokens} think={res.thoughts_tokens}"
    except Exception as exc:  # noqa: BLE001 - the failure text is the point of the command
        return False, str(exc).split("\n")[0][:160]
    finally:
        bb.close()


@app.command()
def verify(
    list_models: bool = typer.Option(False, "--list-models",
                                     help="Probe every configured model id with a real call."),
) -> None:
    """Preflight: credentials, project, dataset, cache, and model availability."""
    ok = True

    console.print("[bold]Environment[/]")
    console.print(f"  GOOGLE_CLOUD_PROJECT  {C.GCP_PROJECT or '[red]NOT SET[/]'}")
    console.print(f"  GOOGLE_CLOUD_LOCATION {C.GCP_LOCATION}")
    console.print(f"  AC_LLM_MODE           {C.LLM_MODE}")
    if not C.GCP_PROJECT:
        console.print("[red]  set GOOGLE_CLOUD_PROJECT in .env, then "
                      "`gcloud auth application-default login`[/]")
        console.print("[yellow]  or run with AC_LLM_MODE=replay to use the committed cache[/]")
        ok = False

    console.print("\n[bold]Dataset[/]")
    if C.EVENTS_DB.exists():
        import sqlite3  # noqa: PLC0415

        n = sqlite3.connect(C.EVENTS_DB).execute("SELECT COUNT(*) FROM events").fetchone()[0]
        console.print(f"  {C.EVENTS_DB.name}: {n} events")
    else:
        console.print("[red]  data/events.db missing — run `ac generate`[/]")
        ok = False

    cached = len(list(C.CACHE_DIR.glob("*.json"))) if C.CACHE_DIR.exists() else 0
    console.print(f"\n[bold]LLM cache[/]\n  {cached} cached responses in runs/cache/")

    if list_models:
        console.print("\n[bold]Model availability — real calls, not the catalogue[/]")
        table = Table("role", "model id", "callable", "detail")
        missing: list[str] = []
        for role, model in CONFIGURED_MODELS.items():
            good, detail = _probe(model)
            table.add_row(role, model, "[green]yes[/]" if good else "[red]NO[/]", detail)
            if not good and role != "optional 2nd baseline":
                missing.append(model)
        console.print(table)

        if missing:
            ok = False
            names = ", ".join(sorted(set(missing)))
            console.print(f"\n[red]MISSING REQUIRED MODEL IDS: {names}[/]")
            console.print("[red]Not substituting a different model. Stopping.[/]")
            console.print("\n[bold]Publisher catalogue visible from this project "
                          f"(location={C.GCP_LOCATION}):[/]")
            try:
                for name in llm.list_available_models():
                    if "gemini" in name:
                        console.print(f"  {name}")
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]catalogue listing failed: {exc}[/]")
            console.print("\n[yellow]Note: the catalogue over-reports. Ids listed above may still "
                          "404. The `callable` column is the authority.[/]")

    console.print(f"\n[bold]{'PASS' if ok else 'FAIL'}[/]")
    raise typer.Exit(0 if ok else 1)


# --------------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------------

@app.command()
def review(checkpoint: int = typer.Option(..., "--checkpoint", "-c", min=1, max=5)) -> None:
    """Build review/checkpoint-N/ — the only artefact a reviewer sees."""
    extra: dict[str, str] = {}
    claim = ""
    least: list[str] = []
    questions: list[str] = []

    if checkpoint == 1:
        extra["seed_manifest.json"] = Path(C.SEED_MANIFEST).read_text()
        extra["dataset_stats.txt"] = report.dataset_stats()
        extra["sample_events.txt"] = report.sample_events()
        extra["enrichment_check.txt"] = report.enrichment_check()
        extra["model_availability.txt"] = _model_availability_text()
        claim = (
            "The dataset foundation is built, deterministic, quarantined from the pipeline, and "
            "enriched so planted incidents are not textually distinguishable from noise. One box "
            "is NOT met: the configured Tier 2 / baseline model id does not exist in this "
            "project, and per SPEC I stopped rather than substituting one."
        )
        least = [
            "Whether the planted incidents are genuinely findable by a smart human reading raw "
            "events — this is the one thing I cannot judge for myself.",
            "Whether the near-misses are tempting enough to be a real test of the gate, "
            "particularly NM-4, which is a single CRM event.",
            "Whether enrichment left any structural tell (length, jargon density) that separates "
            "incident text from noise text even though the wording is model-written.",
        ]
        questions = [
            "gemini-3.1-pro is not callable in this project; gemini-3.1-pro-preview is. Approve "
            "the substitution for TIER2_MODEL and BASELINE_MODEL, or point at another project?",
            "Do the INC-1 and NM-1 chains in sample_events.txt read as plausible enterprise data?",
        ]

    if checkpoint == 2:
        run_dir = report.latest_run_dir("cascade")
        if run_dir is None:
            console.print("[red]no cascade run found — run `ac run --arm cascade` first[/]")
            raise typer.Exit(1)
        res = _replay_run(run_dir)
        extra["collapse_line.txt"] = res.collapse_line + "\n"
        extra["anomalies_sample.txt"] = report.anomalies_sample(res)
        extra["hypotheses.json"] = report.hypotheses_json(res)
        extra["gate_trace.txt"] = gate_trace(res.gate, res.hypothesis_list)
        extra["llm_calls.csv"] = report.llm_calls_csv(run_dir / "run.db")
        extra["cost_by_tier.md"] = report.cost_by_tier(run_dir / "run.db")
        extra["recall_waterfall.md"] = report.waterfall(res)
        for f in sorted((C.ROOT / "src" / "attention_cascade").glob("*.py")):
            extra[f"src_{f.stem}.py.txt"] = f.read_text()
        claim = (
            "The cascade runs end to end on real metered Vertex calls. 1069 events collapse to "
            "75 anomalies, 75 candidates, 4 hypotheses and 4 signals. Thinking tokens are 0 at "
            "Tier 1 and non-zero at Tier 2, proving the per-tier budgets applied. Recall is 4 of "
            "5 planted incidents; INC-4 is missed and the waterfall shows exactly where."
        )
        least = [
            "Tier 1 discarded nothing (75 of 75 plausible), so all volume reduction happens at "
            "Tier 0. The spec expected 12-20 candidates. I did not tune the prompt to force it.",
            "INC-4 is recoverable right up to Tier 2 and is still not proposed. I know where it "
            "is lost but not yet why the model declines to propose it.",
            "Only 4 hypotheses come back from 75 anomalies. The prompt says 'prefer fewer', so "
            "this may be correct behaviour or may be under-production I am reading charitably.",
        ]
        questions = [
            "Tier 1 passing everything: force selectivity by changing the prompt, or report the "
            "permissive instruction as the cause and let Tier 0 own the reduction?",
            "Is 4 of 5 acceptable for Checkpoint 2, given the waterfall localises the loss?",
        ]

    dest = report.build_packet(checkpoint, claim, least, questions, extra)
    console.print(f"[green]packet built[/] -> {dest}")
    for p in sorted(dest.iterdir()):
        if p.is_file():
            console.print(f"    {p.name:<28} {p.stat().st_size:>8} bytes")
    # Last line, always: the flat folder the reviewer uploads.
    console.print(f"\n[bold]UPLOAD THIS FOLDER:[/] {(C.ROOT / 'review_artifacts').resolve()}")


def _replay_run(run_dir: Path) -> object:
    """Re-run the cascade against a finished run's database, served entirely from the LLM cache.

    The packet must describe a real run, and re-running under the cache reproduces that run's
    hypotheses and gate decisions exactly without spending anything or touching the network.
    """
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    # Into a SCRATCH database, never the measured run's own. Re-running into the finished run
    # appended a second set of llm_calls rows and doubled every figure in Table C - the packet
    # reported $0.1675 for a $0.0838 run. The measured database is read-only from here.
    scratch = C.RUNS_DIR / "_packet"
    shutil.rmtree(scratch, ignore_errors=True)

    prev, os.environ["AC_LLM_MODE"] = os.environ.get("AC_LLM_MODE", ""), "replay"
    C.LLM_MODE = "replay"
    try:
        source = Blackboard(run_dir / "run.db", arm="cascade")
        events = source.events()
        source.close()
        bb = Blackboard(scratch / "run.db", run_id="packet", arm="cascade")
        bb.insert_events(events)
        return asyncio.run(run_cascade(bb, run_id="packet"))
    finally:
        C.LLM_MODE = prev or "auto"
        if prev:
            os.environ["AC_LLM_MODE"] = prev


def _model_availability_text() -> str:
    """Probe every configured id and render the result as plain text for the packet."""
    lines = ["MODEL AVAILABILITY — real two-token calls, not the publisher catalogue",
             "=" * 100,
             f"project  {C.GCP_PROJECT}",
             f"location {C.GCP_LOCATION}",
             ""]
    for role, model in CONFIGURED_MODELS.items():
        good, detail = _probe(model)
        lines.append(f"  {'CALLABLE' if good else 'NOT FOUND':<10} {role:<24} {model:<28} {detail}")
    lines += ["", "Publisher catalogue (over-reports; ids here may still 404):"]
    try:
        lines += [f"  {n}" for n in llm.list_available_models() if "gemini" in n]
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  catalogue listing failed: {exc}")
    return "\n".join(lines)


@app.command()
def run(
    arm: str = typer.Option("cascade", "--arm", help="cascade | baseline"),
    run_id: str = typer.Option("", help="Run id. Defaults to arm-<timestamp>."),
) -> None:
    """Run one arm end to end and print the collapse line."""
    import time as _time  # noqa: PLC0415

    if arm not in ("cascade", "baseline"):
        console.print(f"[red]unknown arm '{arm}'[/]")
        raise typer.Exit(2)
    if not C.EVENTS_DB.exists():
        console.print("[red]data/events.db missing — run `ac generate --enrich`[/]")
        raise typer.Exit(1)

    rid = run_id or f"{arm}-{int(_time.time())}"
    run_dir = C.RUNS_DIR / rid
    bb = Blackboard(run_dir / "run.db", run_id=rid, arm=arm)
    bb.insert_events(Blackboard(C.EVENTS_DB).events())

    console.print(f"[bold]{arm}[/] run_id={rid}  model tier1={C.TIER1_MODEL} tier2={C.TIER2_MODEL}")
    if arm == "cascade":
        res = asyncio.run(run_cascade(bb, run_id=rid))
    else:
        from .baseline import run_baseline  # noqa: PLC0415

        res = asyncio.run(run_baseline(bb, run_id=rid))

    console.print(f"\n[bold green]{res.collapse_line}[/]")
    if res.dead_streams:
        console.print(f"[yellow]dead streams: {', '.join(res.dead_streams)}[/]")
    if res.degraded:
        console.print(f"[yellow]DEGRADED — {res.shed_reason}[/]")
    console.print(f"hallucinated event_ids dropped: {res.hallucinated_event_ids}")
    console.print(f"wall clock: {res.wall_clock_s}s")
    _print_spend(bb)
    bb.close()
    console.print(f"\nrun dir: {run_dir}")


def _print_spend(bb: Blackboard) -> None:
    """Per-tier spend straight out of llm_calls. Never estimated."""
    rows = list(bb.conn.execute(
        "SELECT tier, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,"
        " SUM(thinking_tokens) t, SUM(cost_usd) c FROM llm_calls GROUP BY tier, model"))
    if not rows:
        return
    table = Table("tier", "model", "calls", "input", "output", "thinking", "cost USD")
    total = 0.0
    for r in rows:
        total += r["c"] or 0.0
        table.add_row(r["tier"], r["model"], str(r["n"]), f"{r['i']:,}", f"{r['o']:,}",
                      f"{r['t']:,}", f"${r['c']:.4f}")
    table.add_row("[bold]total[/]", "", "", "", "", "", f"[bold]${total:.4f}[/]")
    console.print(table)


@app.command(name="check-linkage")
def check_linkage() -> None:
    """Assert every planted incident is structurally joinable across its streams.

    This is the check that would have caught the enrichment defect automatically: enrichment
    rewrites prose, so any incident whose hops were linked only by wording silently became
    undiscoverable. Run it after every generator change.
    """
    text, ok = report.linkage_report()
    console.print(text)
    raise typer.Exit(0 if ok else 1)


@app.command()
def stats() -> None:
    """Print the dataset statistics block to the terminal."""
    console.print(report.dataset_stats())


if __name__ == "__main__":
    app()
```

---

## `src/attention_cascade/config.py`

```python
"""Central configuration: pricing, thresholds, model ids, paths.

Does: hold every tunable constant in one place so nothing is buried in a function.
Does not: read the network, hold secrets (those come from .env via os.environ).
Exists because: every cost number in the report traces back to PRICING here, and a judge will
ask where it came from.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
CACHE_DIR = RUNS_DIR / "cache"
REVIEW_DIR = ROOT / "review"
EVENTS_DB = DATA_DIR / "events.db"
SEED_MANIFEST = DATA_DIR / "seed_manifest.json"

# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------
SEED = 20260829

# Replication. A single run is an anecdote; five runs with a stated range is a measurement.
# Each seed produces a different dataset with the same structure: 5 incidents, 4 near-misses.
# Credits are not the constraint here - wall-clock time is. Cut to 3 seeds if the clock is tight.
REPLICATION_SEEDS = [20260829, 20260830, 20260831, 20260901, 20260902]

# Enrichment: rewrite every event's text through a blind model pass so planted incidents are not
# textually more distinctive than noise. See enrich.py. Build-time only, cached, own database.
ENRICH_DATASET = True

# --------------------------------------------------------------------------------------
# Vertex AI
# --------------------------------------------------------------------------------------
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

# Model ids. VERIFY THESE ON YOUR MACHINE with `uv run ac verify --list-models` before the
# first real run - Vertex model ids and regional availability move faster than any doc.
TIER1_MODEL = "gemini-3.5-flash-lite"     # triage: cheap, high volume, thinking OFF
TIER2_MODEL = "gemini-3.1-pro-preview"    # correlation: the only place real money is spent
BASELINE_MODEL = "gemini-3.1-pro-preview" # same model as Tier 2, so the comparison is honest
MIDTIER_MODEL = "gemini-3.7-flash"        # optional second baseline, see note below

# --------------------------------------------------------------------------------------
# PRICING - USD per million tokens, Vertex standard tier. VERIFIED 2026-08-29.
# DO NOT EDIT WITHOUT HUMAN APPROVAL - every cost claim in the report depends on these.
#
# Three things that are easy to get wrong and that a judge may probe:
#  1. THINKING TOKENS BILL AT THE OUTPUT RATE. Gemini 3.x reasons internally; those tokens
#     are returned separately as usage_metadata.thoughts_token_count and must be added to
#     the output count. Ignoring them understates cost by a large factor.
#  2. PRO HAS A LONG-CONTEXT CLIFF. Above ~200K input tokens the Pro rate roughly doubles.
#     The naive baseline can fall off this cliff. The cascade structurally cannot. Say this
#     out loud during judging - it is the cost argument getting stronger with scale, not weaker.
#  3. gemini-3.7-flash is on INTRODUCTORY pricing through 2026-12-31, then doubles to
#     1.50/7.50. Never build a headline number on a promotional rate. It is a second
#     baseline here, not part of the main claim.
# --------------------------------------------------------------------------------------
PRICING: dict[str, dict[str, float | None]] = {
    "gemini-3.1-flash-lite": {
        "input": 0.25, "output": 1.50,
        "long_ctx_threshold": None, "long_input": None, "long_output": None,
    },
    "gemini-3.7-flash": {
        "input": 0.75, "output": 3.75,           # INTRODUCTORY through 2026-12-31
        "long_ctx_threshold": None, "long_input": None, "long_output": None,
    },
    "gemini-3.5-flash": {
        "input": 1.50, "output": 9.00,
        "long_ctx_threshold": None, "long_input": None, "long_output": None,
    },
    "gemini-3.1-pro": {
        "input": 2.00, "output": 12.00,
        "long_ctx_threshold": 200_000, "long_input": 4.00, "long_output": 18.00,
    },
    "gemini-3.5-flash-lite": {
        "input": 0.30, "output": 2.50,
        "long_ctx_threshold": None, "long_input": None, "long_output": None,
    },
    "gemini-3.1-pro-preview": {
        # Preview endpoint bills at the same standard rate as GA 3.1 Pro. Verified 2026-08-29
        # against published batch pricing for this exact id ($1.00/$6.00 = 50% of $2/$12).
        # Long-context surcharge above 200K input applies to the Pro tier.
        "input": 2.00, "output": 12.00,
        "long_ctx_threshold": 200_000, "long_input": 4.00, "long_output": 18.00,
    },
}
PROMOTIONAL_RATES = {"gemini-3.7-flash": "introductory through 2026-12-31, then 1.50/7.50"}
CACHE_HIT_MULTIPLIER = 0.1   # Vertex bills cached input at 10% of standard input
BATCH_DISCOUNT = 0.5         # available, deliberately unused - we measure the interactive path

# --------------------------------------------------------------------------------------
# Thinking budgets. Reasoning is a priced resource, so we budget it per tier.
# Tier 1 asks a yes/no question and must not think. Tier 2 does the hard reasoning and may.
# Units are output tokens of internal reasoning; 0 disables thinking.
# --------------------------------------------------------------------------------------
TIER1_THINKING_BUDGET = 0
TIER2_THINKING_BUDGET = 2048
BASELINE_THINKING_BUDGET = 2048   # must match Tier 2 or the comparison is rigged

# --------------------------------------------------------------------------------------
# Dataset shape
# --------------------------------------------------------------------------------------
N_ACCOUNTS = 12
N_DAYS = 60
STREAMS = ("crm", "engineering", "support", "billing")
TARGET_EVENT_COUNT = (900, 1100)  # inclusive sanity band, asserted in tests

# --------------------------------------------------------------------------------------
# Tier 0 detector thresholds
# Tune these on noise density only. NEVER tune them by looking at ground truth.
#
# Tuned 2026-08-29 against TOTAL anomaly volume only, to land inside the 60-100 band the spec
# asks for (46 -> 68). The originals were set so tight that several detectors could only fire on
# planted structure, which flatters Tier 0's precision and leaves the tiers above it nothing real
# to be wrong about. These values deliberately let the upper tail of the NOISE distribution
# through: noise sprint slips top out at 4 points and noise forecast cuts at -12%, so both
# detectors now fire on ordinary weeks as well as on incidents. Ground truth was not consulted.
# --------------------------------------------------------------------------------------
CRM_STAGE_STALL_DAYS = 21
CRM_FORECAST_DROP_PCT = -0.10
ENG_SLIP_POINTS = 4
ENG_REOPEN_Z = 2.0
SUPPORT_VOLUME_Z = 1.8
SUPPORT_SEVERITY_MIN = 2          # count of sev1 in a 3-day window
SUPPORT_SILENCE_DAYS = 10         # zero tickets for an account averaging > 1/day
SUPPORT_SILENCE_MIN_BASELINE = 0.8
BILLING_USAGE_DROP_PCT = -0.18
BILLING_DISPUTE_MIN = 2
BILLING_DROP_COOLDOWN_DAYS = 14   # one anomaly per decline episode, not one per snapshot
BILLING_INVOICE_DEVIATION = 0.25  # invoice this far from the account's own median is odd
BILLING_DECLINE_MIN_POINTS = 4    # consecutive readings forming a sustained slide
BILLING_DECLINE_TOTAL_PCT = -0.20 # total fall across that window
BILLING_DECLINE_MAX_SPAN_DAYS = 14  # a slide, not a slow seasonal drift

# --------------------------------------------------------------------------------------
# Tier 1 / Tier 2
# --------------------------------------------------------------------------------------
TIER1_BATCH_SIZE = 10
TIER1_MAX_TOKENS = 1000
TIER2_MAX_TOKENS = 4000
TIER2_MAX_CANDIDATES_PER_CALL = 90   # single-call threshold; 55 anomalies ~= 13K input
                                     # tokens against a 200K cliff, so one call fits easily
TIER2_MAX_EVENTS_PER_ANOMALY = 5

# --------------------------------------------------------------------------------------
# The attention gate
# --------------------------------------------------------------------------------------
ATTENTION_CAP = 7
MIN_SOURCES = 2          # distinct streams required in deduplicated evidence
MIN_CONFIDENCE = 0.55

# --------------------------------------------------------------------------------------
# Concurrency, backpressure, shedding
# --------------------------------------------------------------------------------------
ANOMALY_QUEUE_MAX = 200
CANDIDATE_QUEUE_MAX = 50
TIER1_WORKERS = 3
TIER2_WORKERS = 1
SHED_TRIGGER_SECONDS = 3.0
BLOCK_LOG_THRESHOLD_MS = 100

# --------------------------------------------------------------------------------------
# LLM layer
# --------------------------------------------------------------------------------------
LLM_MODE = os.environ.get("AC_LLM_MODE", "auto")  # auto | live | replay
LLM_MAX_RETRIES = 3
LLM_BACKOFF_BASE_S = 1.0

# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
EXTRAPOLATION_EVENTS_PER_DAY = 1_000_000


def price_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int = 0,
    cached_input_tokens: int = 0,
) -> float:
    """USD cost of one Vertex call.

    Thinking tokens bill at the OUTPUT rate and are added to output_tokens here.
    Cached input bills at CACHE_HIT_MULTIPLIER of the standard input rate.
    If input crosses the model's long-context threshold, the long-context rates apply.
    """
    rates = PRICING[model]
    threshold = rates["long_ctx_threshold"]
    long_ctx = threshold is not None and input_tokens > threshold
    in_rate = float(rates["long_input"] if long_ctx else rates["input"])
    out_rate = float(rates["long_output"] if long_ctx else rates["output"])

    fresh_in = max(input_tokens - cached_input_tokens, 0)
    billable_out = output_tokens + thoughts_tokens
    return (
        fresh_in / 1e6 * in_rate
        + cached_input_tokens / 1e6 * in_rate * CACHE_HIT_MULTIPLIER
        + billable_out / 1e6 * out_rate
    )


def crossed_long_context(model: str, input_tokens: int) -> bool:
    """True if this call fell off the long-context price cliff. Reported per arm."""
    threshold = PRICING[model]["long_ctx_threshold"]
    return threshold is not None and input_tokens > threshold
```

---

## `src/attention_cascade/correlate.py`

```python
"""Tier 2: the only place frontier-model money is spent.

Does: send every surviving candidate plus a few representative raw events per anomaly to
gemini-3.1-pro-preview in one call (two at most), with thinking enabled, and parse cross-system
causal hypotheses back out. Validates every cited event_id against the blackboard.
Does not: decide what escalates. It proposes; gate.py disposes. It also does not get to invent
evidence — a cited id that does not exist is dropped and counted, and that count is reported.
Exists because: correlating across four systems is the one job in this pipeline that genuinely
needs a frontier model, so it is the one job that gets one, on the smallest input that will do.
"""

from __future__ import annotations

import json

from . import config as C
from .blackboard import Blackboard
from .llm import call, parse_json
from .models import Anomaly, Candidate, EvidenceRef, Hypothesis

SYSTEM = """\
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
Propose at most 12 hypotheses. Prefer fewer, better-evidenced ones."""


def _context(bb: Blackboard, anomalies: list[Anomaly]) -> dict:
    """Anomalies grouped BY ACCOUNT, each with up to 5 representative raw events.

    SPEC section 7 asks for "all candidates plus per-account context", and the grouping is
    load-bearing rather than cosmetic. A flat list ordered by score scatters one account's CRM,
    support and billing anomalies across seventy-odd entries, so the cross-system chain the model
    is being asked to find is not visible anywhere in its input. Grouping by account puts each
    chain in one place. The accounts are the join key, so the accounts are the structure.

    Engineering rows carry entity_id="internal" and belong to no account, so they are presented
    once as shared context that any account's chain may reach into.
    """
    wanted: set[str] = set()
    for a in anomalies:
        wanted.update(a.event_ids[: C.TIER2_MAX_EVENTS_PER_ANOMALY])
    lookup = {e.id: e for e in bb.events() if e.id in wanted}

    def block(a: Anomaly) -> dict:
        evs = [lookup[i] for i in a.event_ids[: C.TIER2_MAX_EVENTS_PER_ANOMALY] if i in lookup]
        return {
            "anomaly_id": a.id,
            "detector": a.detector,
            "stream": a.stream,
            "entity": a.entity_id,
            "window": f"{a.window_start:%Y-%m-%d}..{a.window_end:%Y-%m-%d}",
            "summary": a.summary,
            "events": [e.to_line() for e in evs],
        }

    shared = [block(a) for a in anomalies if a.entity_id in ("internal", "")]
    by_account: dict[str, list[dict]] = {}
    for a in anomalies:
        if a.entity_id not in ("internal", ""):
            by_account.setdefault(a.entity_id, []).append(block(a))

    return {
        "shared_engineering_context": shared,
        "accounts": [{"entity_id": k, "anomalies": v} for k, v in sorted(by_account.items())],
    }


async def correlate(
    bb: Blackboard,
    candidates: list[Candidate],
    anomalies: list[Anomaly],
    *,
    run_id: str = "run",
) -> tuple[list[Hypothesis], int]:
    """Propose cross-system hypotheses. Returns (validated hypotheses, hallucinated id count).

    Chunks into at most two calls. Every cited event_id is checked against the events table;
    unknown ids are dropped and counted, because hallucination rate is a number worth reporting
    and nobody gets to cite evidence that does not exist.
    """
    keep = {c.anomaly_id for c in candidates if c.plausible}
    selected = [a for a in anomalies if a.id in keep]
    if not selected:
        bb.audit("tier2", "SKIPPED", {"reason": "no plausible candidates"})
        return [], 0

    chunks = _chunk(bb, selected)

    valid_ids = bb.event_ids()
    hypotheses: list[Hypothesis] = []
    hallucinated = 0
    seq = 0

    for n, chunk in enumerate(chunks):
        prompt = json.dumps(_context(bb, chunk), indent=None)
        raw: list = []
        for attempt in (1, 2):
            system = SYSTEM if attempt == 1 else (
                SYSTEM + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON array.")
            try:
                res = await call(
                    model=C.TIER2_MODEL, system=system, prompt=prompt,
                    max_tokens=C.TIER2_MAX_TOKENS, tier="tier2", run_id=run_id, bb=bb,
                    thinking_budget=C.TIER2_THINKING_BUDGET, json_output=True,
                )
                parsed = parse_json(res.text)
                raw = parsed if isinstance(parsed, list) else [parsed]
                break
            except Exception as exc:  # noqa: BLE001 - audited, then we continue with zero
                bb.audit("tier2", "PARSE_FAILED",
                         {"chunk": n, "attempt": attempt, "error": str(exc)[:300]})

        if not raw:
            bb.audit("tier2", "NO_HYPOTHESES", {"chunk": n,
                                                "note": "reported as zero, not silently dropped"})
            continue

        for item in raw:
            if not isinstance(item, dict):
                continue
            refs: list[EvidenceRef] = []
            for ev in item.get("evidence") or []:
                if not isinstance(ev, dict):
                    continue
                eid, stream = str(ev.get("event_id", "")), str(ev.get("stream", ""))
                if eid not in valid_ids:
                    hallucinated += 1
                    bb.audit("tier2", "HALLUCINATED_EVENT_ID",
                             {"event_id": eid, "title": str(item.get("title", ""))[:80]})
                    continue
                if stream not in C.STREAMS:
                    continue
                refs.append(EvidenceRef(event_id=eid, stream=stream,
                                        anomaly_id=str(ev["anomaly_id"])
                                        if ev.get("anomaly_id") else None))
            seq += 1
            hypotheses.append(Hypothesis(
                id=f"hyp_{seq:03d}",
                title=str(item.get("title", ""))[:160],
                narrative=str(item.get("narrative", ""))[:800],
                entity_id=str(item.get("entity_id", ""))[:40],
                evidence=refs,
                impact=_clamp(item.get("impact")),
                confidence=_clamp(item.get("confidence")),
            ))

    for h in hypotheses:
        await bb.write_hypothesis(h)

    bb.audit("tier2", "SUMMARY", {
        "candidates_in": len(selected), "calls": len(chunks),
        "hypotheses": len(hypotheses), "hallucinated_event_ids": hallucinated,
        "model": C.TIER2_MODEL, "thinking_budget": C.TIER2_THINKING_BUDGET,
    })
    return hypotheses, hallucinated


def _chunk(bb: Blackboard, selected: list[Anomaly]) -> list[list[Anomaly]]:
    """One call if it fits, two at most (SPEC section 7). If two, split BY ACCOUNT.

    Splitting by rank was a real defect: anomalies are ordered by score, so one account's CRM,
    support and billing anomalies scatter across both calls and the cross-system chain is severed
    exactly the way enrichment severed the textual one. A model cannot correlate what it cannot
    see together. Accounts are the join key, so accounts are the unit that must stay whole.

    Engineering rows carry entity_id="internal" and are the shared context every account may need
    (F-Atlas, integrations), so they go into BOTH calls rather than to one arbitrarily.
    """
    if len(selected) <= C.TIER2_MAX_CANDIDATES_PER_CALL:
        return [selected]

    shared = [a for a in selected if a.entity_id in ("internal", "")]
    owned: dict[str, list[Anomaly]] = {}
    for a in selected:
        if a not in shared:
            owned.setdefault(a.entity_id, []).append(a)

    # Greedy balance of whole accounts across two calls.
    groups = sorted(owned.items(), key=lambda kv: -len(kv[1]))
    left: list[Anomaly] = []
    right: list[Anomaly] = []
    for _acct, rows in groups:
        (left if len(left) <= len(right) else right).extend(rows)

    chunks = [shared + left, shared + right]
    bb.audit("tier2", "SPLIT_BY_ACCOUNT", {
        "calls": 2, "shared_context_anomalies": len(shared),
        "sizes": [len(c) for c in chunks],
        "reason": "accounts kept whole so cross-system chains survive the split",
    })
    return chunks


def _clamp(v: object) -> float:
    try:
        return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
```

---

## `src/attention_cascade/detectors.py`

```python
"""Tier 0: four stream detectors running concurrently, spending zero tokens.

Does: watch one enterprise stream each, apply deterministic statistics, and write anomalies to the
blackboard. Supports being killed mid-run and being flooded, for the two failure demos.
Does not: know that any other detector exists, read another stream, or call a model. A detector
cannot correlate; that is the whole point of the tier above it.
Exists because: ~1080 events reach a frontier model as ~1080 events only if nobody thought about
it. Cutting to ~60-100 anomalies with arithmetic is the single largest cost saving in the system,
and arithmetic does not hallucinate.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config as C
from .blackboard import Blackboard
from .models import Anomaly, Event


@dataclass
class KillSpec:
    """Kill a named detector after `after_seconds`. The run must survive it."""

    detector: str
    after_seconds: float = 3.0


@dataclass
class FloodSpec:
    """Duplicate one stream's events `factor` times to drive the queues into backpressure."""

    stream: str
    factor: int = 50


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def _zscores(values: list[float]) -> list[float]:
    """Population z-scores. A flat series yields zeros rather than a division error."""
    if len(values) < 2:
        return [0.0] * len(values)
    mu = statistics.fmean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return [0.0] * len(values)
    return [(v - mu) / sd for v in values]


def _by_day(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        out[e.ts.date().isoformat()].append(e)
    return out


def _anom(idx: list[int], detector: str, stream: str, entity: str,
          lo: datetime, hi: datetime, score: float, kind: str,
          event_ids: list[str], summary: str) -> Anomaly:
    idx[0] += 1
    return Anomaly(
        id=f"anom_{stream[:3]}_{idx[0]:04d}", detector=detector, stream=stream,
        entity_id=entity, window_start=lo, window_end=hi, score=round(score, 3),
        kind=kind, event_ids=event_ids, summary=summary,
    )


# --------------------------------------------------------------------------------------
# CRM
# --------------------------------------------------------------------------------------

def detect_crm(events: list[Event], idx: list[int]) -> list[Anomaly]:
    """`stage_stall` (too long in one stage) and `forecast_drop` (a sharp cut in amount)."""
    out: list[Anomaly] = []
    by_acct: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_acct[e.entity_id].append(e)

    for acct, evs in sorted(by_acct.items()):
        evs.sort(key=lambda e: e.ts)

        stage_events = [e for e in evs if e.kind == "deal_stage_change"]
        for a, b in zip(stage_events, stage_events[1:], strict=False):
            days = (b.ts - a.ts).days
            if days > C.CRM_STAGE_STALL_DAYS:
                out.append(_anom(
                    idx, "stage_stall", "crm", acct, a.ts, b.ts,
                    days / C.CRM_STAGE_STALL_DAYS, "stall", [a.id, b.id],
                    f"{acct} sat in one deal stage for {days} days "
                    f"(threshold {C.CRM_STAGE_STALL_DAYS})",
                ))
        # A deal that stalls and never moves again produces no pair, so check the tail too.
        if stage_events:
            last = stage_events[-1]
            tail = (evs[-1].ts - last.ts).days
            if tail > C.CRM_STAGE_STALL_DAYS:
                out.append(_anom(
                    idx, "stage_stall", "crm", acct, last.ts, evs[-1].ts,
                    tail / C.CRM_STAGE_STALL_DAYS, "stall", [last.id],
                    f"{acct} has not changed deal stage for {tail} days",
                ))

        for e in evs:
            delta = e.payload.get("delta_pct")
            if isinstance(delta, (int, float)) and delta < C.CRM_FORECAST_DROP_PCT:
                out.append(_anom(
                    idx, "forecast_drop", "crm", acct, e.ts, e.ts,
                    abs(float(delta)), "drop", [e.id],
                    f"{acct} forecast cut {float(delta) * 100:.0f}%",
                ))
            if e.kind == "contact_departed":
                out.append(_anom(
                    idx, "forecast_drop", "crm", acct, e.ts, e.ts, 0.5, "contact_loss", [e.id],
                    f"{acct} lost a {e.payload.get('contact_role', 'key')} contact",
                ))
    return out


# --------------------------------------------------------------------------------------
# Engineering
# --------------------------------------------------------------------------------------

def detect_engineering(events: list[Event], idx: list[int]) -> list[Anomaly]:
    """`delivery_slip` (planned minus delivered) and `reopen_spike` (z > 2 on reopened bugs)."""
    out: list[Anomaly] = []

    for e in events:
        planned, delivered = e.payload.get("planned"), e.payload.get("delivered")
        if isinstance(planned, int) and isinstance(delivered, int):
            slip = planned - delivered
            if slip >= C.ENG_SLIP_POINTS:
                feature = e.payload.get("feature") or e.payload.get("team", "unknown")
                out.append(_anom(
                    idx, "delivery_slip", "engineering", e.entity_id, e.ts, e.ts,
                    slip / C.ENG_SLIP_POINTS, "slip", [e.id],
                    f"{e.payload.get('team', '?')} missed {slip} points on {feature} "
                    f"({delivered}/{planned})",
                ))

    reopens = [e for e in events if e.kind == "bug_reopened"]
    per_day = _by_day(reopens)
    days = sorted(per_day)
    counts = [float(len(per_day[d])) for d in days]
    for day, z, n in zip(days, _zscores(counts), counts, strict=False):
        if z > C.ENG_REOPEN_Z:
            evs = per_day[day]
            teams = sorted({str(x.payload.get("team", "?")) for x in evs})
            out.append(_anom(
                idx, "reopen_spike", "engineering", "internal",
                evs[0].ts, evs[-1].ts, z, "spike", [x.id for x in evs],
                f"{int(n)} bugs reopened on {day} (z={z:.1f}), teams: {','.join(teams)}",
            ))
    return out


# --------------------------------------------------------------------------------------
# Support
# --------------------------------------------------------------------------------------

def detect_support(events: list[Event], idx: list[int]) -> list[Anomaly]:
    """`ticket_volume_spike`, `severity_spike`, and `ticket_silence` — an absence, not a spike."""
    out: list[Anomaly] = []
    by_acct: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_acct[e.entity_id].append(e)

    for acct, evs in sorted(by_acct.items()):
        evs.sort(key=lambda e: e.ts)
        per_day = _by_day(evs)
        all_days = sorted(per_day)
        counts = [float(len(per_day[d])) for d in all_days]

        for day, z, n in zip(all_days, _zscores(counts), counts, strict=False):
            if z > C.SUPPORT_VOLUME_Z and n >= 2:
                day_evs = per_day[day]
                comps = sorted({str(x.payload.get("component", "?")) for x in day_evs})
                out.append(_anom(
                    idx, "ticket_volume_spike", "support", acct,
                    day_evs[0].ts, day_evs[-1].ts, z, "spike", [x.id for x in day_evs],
                    f"{acct} opened {int(n)} tickets on {day} (z={z:.1f}), "
                    f"components: {','.join(comps)}",
                ))

        # severity_spike: sev-1 clustering inside a rolling 3-day window
        sev1 = [e for e in evs if (e.payload.get("severity") == 1 or e.numeric == 1.0)]
        for i, anchor in enumerate(sev1):
            window = [x for x in sev1[i:] if (x.ts - anchor.ts) <= timedelta(days=3)]
            if len(window) >= C.SUPPORT_SEVERITY_MIN:
                comps = sorted({str(x.payload.get("component", "?")) for x in window})
                out.append(_anom(
                    idx, "severity_spike", "support", acct,
                    window[0].ts, window[-1].ts, float(len(window)), "severity",
                    [x.id for x in window],
                    f"{acct} raised {len(window)} sev-1 tickets in 3 days, "
                    f"components: {','.join(comps)}",
                ))
                break  # one anomaly per account per burst, not one per anchor

        # ticket_silence: the absence IS the evidence. An absence has no events of its own, so
        # cite the last 3 tickets BEFORE the gap — otherwise the anomaly cannot be evidence.
        if len(evs) >= 5:
            span_days = max((evs[-1].ts - evs[0].ts).days, 1)
            baseline = len(evs) / span_days
            if baseline > C.SUPPORT_SILENCE_MIN_BASELINE:
                for a, b in zip(evs, evs[1:], strict=False):
                    gap = (b.ts - a.ts).days
                    if gap >= C.SUPPORT_SILENCE_DAYS:
                        prior = [x.id for x in evs if x.ts <= a.ts][-3:]
                        out.append(_anom(
                            idx, "ticket_silence", "support", acct, a.ts, b.ts,
                            gap / C.SUPPORT_SILENCE_DAYS, "silence", prior,
                            f"{acct} opened zero tickets for {gap} days after averaging "
                            f"{baseline:.1f}/day — citing the last 3 tickets before the gap",
                        ))
    return out


# --------------------------------------------------------------------------------------
# Billing
# --------------------------------------------------------------------------------------

def detect_billing(events: list[Event], idx: list[int]) -> list[Anomaly]:
    """`usage_drop` (7-day mean vs trailing 21-day mean) and `invoice_dispute`."""
    out: list[Anomaly] = []
    by_acct: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_acct[e.entity_id].append(e)

    for acct, evs in sorted(by_acct.items()):
        evs.sort(key=lambda e: e.ts)
        usage = [e for e in evs if e.kind == "usage_snapshot" and e.numeric is not None]

        last_onset: datetime | None = None
        for i, e in enumerate(usage):
            recent = [x for x in usage[: i + 1] if (e.ts - x.ts) <= timedelta(days=7)]
            trailing = [x for x in usage[: i + 1]
                        if timedelta(days=7) < (e.ts - x.ts) <= timedelta(days=28)]
            if len(recent) < 2 or len(trailing) < 3:
                continue
            r, t = _mean([x.numeric for x in recent]), _mean([x.numeric for x in trailing])
            if t <= 0:
                continue
            change = (r - t) / t
            if change < C.BILLING_USAGE_DROP_PCT:
                cooldown = timedelta(days=C.BILLING_DROP_COOLDOWN_DAYS)
                if last_onset and (e.ts - last_onset) < cooldown:
                    continue  # still inside the decline already reported
                last_onset = e.ts
                out.append(_anom(
                    idx, "usage_drop", "billing", acct, recent[0].ts, e.ts,
                    abs(change), "drop", [x.id for x in recent],
                    f"{acct} usage down {change * 100:.0f}% "
                    f"(7-day mean {r:,.0f} vs trailing {t:,.0f})",
                ))

        # usage_decline: a slow bleed, which is a different phenomenon from a cliff and needs a
        # different instrument. `usage_drop` compares a 7-day mean to a trailing baseline, so a
        # gradual decay never crosses it - the baseline decays along with the signal. A sustained
        # monotonic slide is the shape that matters for churn, and it is invisible to the cliff
        # detector by construction.
        for i in range(len(usage) - C.BILLING_DECLINE_MIN_POINTS + 1):
            window = usage[i : i + C.BILLING_DECLINE_MIN_POINTS]
            pairs = zip(window, window[1:], strict=False)
            drops = sum(1 for x, y in pairs if y.numeric < x.numeric)
            total = (window[-1].numeric - window[0].numeric) / window[0].numeric
            span = (window[-1].ts - window[0].ts).days
            if (drops >= len(window) - 2 and total < C.BILLING_DECLINE_TOTAL_PCT
                    and 0 < span <= C.BILLING_DECLINE_MAX_SPAN_DAYS):
                out.append(_anom(
                    idx, "usage_decline", "billing", acct, window[0].ts, window[-1].ts,
                    abs(total), "decline", [x.id for x in window],
                    f"{acct} usage slid {total * 100:.0f}% over {span} days across "
                    f"{len(window)} readings "
                    f"({window[0].numeric:,.0f} -> {window[-1].numeric:,.0f})",
                ))
                break  # the onset of the slide, not every window inside it

        disputes = [e for e in evs
                    if e.kind == "plan_change" or e.payload.get("category") == "billing_dispute"]
        if disputes:
            out.append(_anom(
                idx, "invoice_dispute", "billing", acct,
                disputes[0].ts, disputes[-1].ts, float(len(disputes)), "dispute",
                [x.id for x in disputes],
                f"{acct} had {len(disputes)} plan/billing change event(s)",
            ))

        invoices = [e for e in evs if e.kind == "invoice_issued" and e.numeric is not None]
        if len(invoices) >= 2:
            median = statistics.median([x.numeric for x in invoices])
            for e in invoices:
                if median > 0 and abs(e.numeric - median) / median > C.BILLING_INVOICE_DEVIATION:
                    delta = (e.numeric - median) / median
                    out.append(_anom(
                        idx, "invoice_dispute", "billing", acct, e.ts, e.ts,
                        abs(delta), "invoice_anomaly", [e.id],
                        f"{acct} invoice {e.numeric:,.0f} deviates {delta * 100:+.0f}% "
                        f"from its median {median:,.0f}",
                    ))
    return out


DETECTORS = {
    "crm": detect_crm,
    "engineering": detect_engineering,
    "support": detect_support,
    "billing": detect_billing,
}


# --------------------------------------------------------------------------------------
# The concurrent tier
# --------------------------------------------------------------------------------------

async def run_detectors(
    bb: Blackboard,
    streams: list[str] | None = None,
    *,
    flood: FloodSpec | None = None,
    kill: KillSpec | None = None,
    queue: asyncio.Queue | None = None,
) -> list[Anomaly]:
    """Spawn one task per stream. Each writes Anomaly rows independently.

    A killed detector raises inside its own task and is logged as a dead stream; the others carry
    on and the run completes. That is the point of coordinating through a blackboard rather than
    through direct calls between detectors.
    """
    streams = streams or list(C.STREAMS)
    idx = [0]
    produced: list[Anomaly] = []
    lock = asyncio.Lock()

    async def worker(stream: str) -> None:
        author = f"detector:{stream}"
        try:
            if kill and kill.detector == stream:
                await asyncio.sleep(kill.after_seconds)
                raise RuntimeError(f"detector for '{stream}' was killed at "
                                   f"t={kill.after_seconds}s")

            events = bb.events(stream=stream)
            if flood and flood.stream == stream:
                events = _amplify(events, flood.factor)
                bb.audit(author, "FLOOD", {"stream": stream, "factor": flood.factor,
                                           "events": len(events)})

            anomalies = DETECTORS[stream](events, idx)
            bb.audit(author, "DETECTOR_DONE",
                     {"stream": stream, "events": len(events), "anomalies": len(anomalies)})

            for a in anomalies:
                await bb.write_anomaly(a, author=author)
                async with lock:
                    produced.append(a)
                if queue is not None:
                    await queue.put(a)
                await asyncio.sleep(0)  # yield, so the four workers genuinely interleave

        except Exception as exc:  # noqa: BLE001 - a dead stream is a demo, not a crash
            bb.audit(author, "STREAM_DEAD", {"stream": stream, "error": str(exc)[:300]})
            bb.set_meta(f"stream_dead:{stream}", True)

    await asyncio.gather(*(worker(s) for s in streams))
    bb.audit("tier0", "SUMMARY", {"streams": streams, "anomalies": len(produced)})
    return sorted(produced, key=lambda a: (-a.score, a.id))


def _amplify(events: list[Event], factor: int) -> list[Event]:
    """Duplicate a stream's events to simulate a flood. Ids are suffixed so they stay distinct."""
    out: list[Event] = []
    for e in events:
        out.append(e)
        for k in range(1, factor):
            out.append(e.model_copy(update={"id": f"{e.id}~f{k}"}))
    return out
```

---

## `src/attention_cascade/enrich.py`

```python
"""Build-time enrichment: rewrite every event's text with a model that cannot see ground truth.

Does: take the deterministic event skeleton from generator.py and write realistic prose into
payload["text"] for every event, in shuffled batches, blind to incident membership.
Does not: change any timestamp, stream, account, numeric value or causal structure. It touches
text only. Ground truth is untouched and unreadable from here.
Exists because: hand-written noise uses a handful of canned strings while planted incidents get
bespoke wording, so a model could surface incidents by spotting distinctive prose rather than by
cross-system reasoning. Enriching every event through one blind process removes that confound.
This is a fairness measure, and it is the answer to "you built the test you pass".

Cost note: enrichment runs against its own database (data/enrich.db) so its token spend NEVER
enters the measured experiment. Dataset construction cost is not inference cost.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from pathlib import Path

from . import config as C
from .blackboard import Blackboard
from .llm import call, parse_json

BATCH = 40

SYSTEM = """You write realistic one-line records for enterprise systems. You will receive a batch
of structured events from a CRM, an engineering tracker, a support desk, and a billing system.

For each event, write the human-readable text a real system would have stored: a ticket subject
line, a deal note, a sprint comment, a billing memo.

USE THE STRUCTURED FIELDS. If the event has a "component", "feature", "release", "team", "stage"
or "symptom" field, work that value into the text naturally, the way a real record would name the
thing it is about. A support ticket about component "F-Atlas" mentions Atlas. A ticket with
release "v3.11" mentions the version. Do not drop these - they are the only thing that makes one
record recognisably about the same subject as another.

VARY THE WRITING HARD. Real queues do not look uniform. Length must range from about 5 words to
about 25 across the batch: some clipped fragments, some full sentences, some rambling with an
aside. Vary register too - terse engineer shorthand, a polite customer paraphrase, an internal
note with an abbreviation. Some may contain a typo. Do NOT write every record to the same
template, and never fall back on a formula like "Support ticket opened for <account>, severity N"
- that carries no information and it is the failure mode to avoid.

Write each line INDEPENDENTLY. The events in this batch are unrelated to each other and arrive in
random order; do not try to build a narrative across them or reference one from another.

Return ONLY a JSON array, no other text:
[{"id": "evt_00001", "text": "..."}]
One object per input event, same ids, 5 to 25 words each."""


def _fields(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload"])
    payload.pop("text", None)  # the old canned string must not anchor the rewrite
    return {
        "id": row["id"],
        "date": row["ts"][:10],
        "stream": row["stream"],
        "account": row["entity_id"],
        "kind": row["kind"],
        "value": row["numeric"],
        **payload,
    }


async def enrich(db_path: Path | None = None, seed: int = C.SEED) -> dict:
    """Rewrite every event's text. Idempotent and deterministic: batching is seeded and every
    call is cached, so a second run costs nothing and produces identical text."""
    db_path = Path(db_path or C.EVENTS_DB)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM events ORDER BY id"))

    # Shuffle before batching. This is the load-bearing line: it guarantees the events of one
    # incident land in different batches, so the model cannot see a causal chain while writing.
    rng = random.Random(seed)
    shuffled = rows[:]
    rng.shuffle(shuffled)
    batches = [shuffled[i : i + BATCH] for i in range(0, len(shuffled), BATCH)]

    # Separate database: enrichment spend must not pollute the measured experiment.
    meter = Blackboard(C.DATA_DIR / "enrich.db", run_id=f"enrich-{seed}", arm="dataset")
    meter.audit("enrich", "START", {"events": len(rows), "batches": len(batches), "seed": seed})

    updates: dict[str, str] = {}
    failures = 0

    hallucinated: set[str] = set()

    async def do_batch(idx: int, batch: list[sqlite3.Row]) -> None:
        nonlocal failures
        allowed = {r["id"] for r in batch}
        prompt = json.dumps([_fields(r) for r in batch], indent=None)
        try:
            res = await call(
                model=C.TIER1_MODEL, system=SYSTEM, prompt=prompt,
                max_tokens=4000, tier="enrich", run_id=f"enrich-{seed}", bb=meter,
                thinking_budget=0, json_output=True,
            )
            for item in parse_json(res.text):
                if not (isinstance(item, dict) and "id" in item and "text" in item):
                    continue
                eid = str(item["id"])
                # The model sometimes returns ids that were never in the batch. Accepting them
                # silently inflated the rewritten count above the corpus size. Drop and count.
                if eid not in allowed:
                    hallucinated.add(eid)
                    continue
                updates[eid] = str(item["text"])[:200]
        except Exception as exc:  # noqa: BLE001 - a failed batch keeps its original text
            failures += 1
            meter.audit("enrich", "BATCH_FAILED", {"batch": idx, "error": str(exc)[:300]})

    sem = asyncio.Semaphore(4)

    async def guarded(i: int, b: list[sqlite3.Row]) -> None:
        async with sem:
            await do_batch(i, b)

    await asyncio.gather(*(guarded(i, b) for i, b in enumerate(batches)))

    # Repair pass. A model occasionally omits an id from its array, which leaves that event
    # holding its canned generator string - exactly the confound enrichment exists to remove.
    # Re-ask for the stragglers in small batches until none are left or we run out of rounds.
    for round_no in range(3):
        missing = [r for r in rows if r["id"] not in updates]
        if not missing:
            break
        meter.audit("enrich", "REPAIR_ROUND", {"round": round_no, "missing": len(missing)})
        repairs = [missing[i : i + 10] for i in range(0, len(missing), 10)]
        await asyncio.gather(*(guarded(1000 + round_no * 100 + i, b)
                               for i, b in enumerate(repairs)))

    for row in rows:
        new_text = updates.get(row["id"])
        if not new_text:
            continue
        payload = json.loads(row["payload"])
        payload["text"] = new_text
        conn.execute("UPDATE events SET payload = ? WHERE id = ?",
                     (json.dumps(payload), row["id"]))
    conn.commit()
    conn.close()

    stats = {"events": len(rows), "rewritten": len(updates),
             "batches": len(batches), "failed_batches": failures, "seed": seed,
             "unrewritten": [r["id"] for r in rows if r["id"] not in updates],
             "hallucinated_ids": len(hallucinated)}
    meter.audit("enrich", "DONE", stats)
    meter.close()
    return stats


if __name__ == "__main__":
    print(json.dumps(asyncio.run(enrich()), indent=2))
```

---

## `src/attention_cascade/gate.py`

```python
"""The attention gate: deterministic escalation. The model proposes, this file disposes.

Does: deduplicate evidence, enforce a two-source sufficiency rule and a confidence floor, rank by
impact x confidence, and hold a hard cap of seven attention slots with explicit displacement.
Does not: call a model, read ground truth, do any I/O, or contain a single line of randomness.
Every rejection carries a machine-readable reason.
Exists because: an LLM asked "is this important enough to escalate?" says yes far too often, which
makes an attention budget meaningless. Escalation authority is arithmetic, and arithmetic is
auditable, testable, and free.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import config as C
from .models import (
    DisplacementEvent,
    EvidenceRef,
    GateResult,
    Hypothesis,
    Rejection,
    Signal,
)


def dedupe_evidence(h: Hypothesis) -> list[EvidenceRef]:
    """Collapse evidence to one ref per (stream, event_id), preserving first-seen order.

    The same fact can reach a hypothesis by two paths — cited directly and again through a second
    anomaly. Counting it twice would let a single-source hypothesis fake a second source, which is
    the exact failure the sufficiency rule exists to prevent.
    """
    seen: set[tuple[str, str]] = set()
    out: list[EvidenceRef] = []
    for e in h.evidence:
        key = (e.stream, e.event_id)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _sort_key(item: tuple[Hypothesis, list[EvidenceRef]]) -> tuple:
    """Rank by impact x confidence, then evidence count, then id. Total order, no ties, no rng."""
    h, ev = item
    return (-h.priority, -len(ev), h.id)


def apply_gate(
    hypotheses: list[Hypothesis],
    *,
    cap: int = C.ATTENTION_CAP,
    min_sources: int = C.MIN_SOURCES,
    min_confidence: float = C.MIN_CONFIDENCE,
) -> GateResult:
    """Decide what holds an attention slot.

    Rules, in order:
      1. Deduplicate evidence by (stream, event_id).
      2. Sufficiency: evidence must span >= min_sources distinct streams.
      3. Confidence floor: confidence >= min_confidence.
      4. Rank survivors by impact * confidence, descending, deterministically.
      5. Cap: the top `cap` escalate. Anything below the cap that outranks a held signal displaces
         it; anything that does not waits with reason `attention_budget_full`.

    Pure and deterministic: same input, same output, always.
    """
    result = GateResult()
    survivors: list[tuple[Hypothesis, list[EvidenceRef]]] = []
    total_deduped = 0

    for h in hypotheses:
        ev = dedupe_evidence(h)
        total_deduped += len(h.evidence) - len(ev)

        if not ev:
            result.rejections.append(Rejection(
                hypothesis_id=h.id, reason="no_valid_evidence",
                detail="no evidence survived validation"))
            continue

        streams = {e.stream for e in ev}
        if len(streams) < min_sources:
            result.rejections.append(Rejection(
                hypothesis_id=h.id, reason="single_source",
                detail=f"evidence spans {sorted(streams)}; need >= {min_sources} distinct streams"))
            continue

        if h.confidence < min_confidence:
            result.rejections.append(Rejection(
                hypothesis_id=h.id, reason="below_floor",
                detail=f"confidence {h.confidence:.2f} < floor {min_confidence:.2f}"))
            continue

        survivors.append((h, ev))

    survivors.sort(key=_sort_key)
    result.deduped_evidence_count = total_deduped

    escalated = survivors[:cap]
    waiting = survivors[cap:]
    now = datetime.now(UTC)

    # Anything past the cap that outranks the weakest held signal takes its slot. With a total
    # order this can never fire on the first pass — the list is already sorted — but the branch is
    # what makes the cap a live attention budget rather than a truncation, and it is what produces
    # the DisplacementEvent record when hypotheses arrive incrementally.
    for h, ev in waiting:
        if not escalated:
            break
        weakest_h, weakest_ev = escalated[-1]
        if _sort_key((h, ev)) < _sort_key((weakest_h, weakest_ev)):
            escalated[-1] = (h, ev)
            escalated.sort(key=_sort_key)
            result.displacements.append(DisplacementEvent(
                incoming_hypothesis_id=h.id,
                displaced_hypothesis_id=weakest_h.id,
                incoming_score=h.priority,
                displaced_score=weakest_h.priority,
            ))
            result.rejections.append(Rejection(
                hypothesis_id=weakest_h.id, reason="attention_budget_full",
                detail=f"displaced by {h.id} ({h.priority:.3f} > {weakest_h.priority:.3f})"))
        else:
            result.rejections.append(Rejection(
                hypothesis_id=h.id, reason="attention_budget_full",
                detail=f"rank {survivors.index((h, ev)) + 1} of {len(survivors)}, cap is {cap}"))

    displaced_by: dict[str, str] = {
        d.displaced_hypothesis_id: d.incoming_hypothesis_id for d in result.displacements
    }
    for rank, (h, _ev) in enumerate(escalated, 1):
        result.signals.append(Signal(
            hypothesis_id=h.id, rank=rank, escalated_at=now,
            displaced_hypothesis_id=next(
                (old for old, new in displaced_by.items() if new == h.id), None),
        ))

    return result


def audit_gate(bb, result: GateResult, hypotheses: list[Hypothesis]) -> None:
    """Write every decision to the audit trail. Printing more than needed wins arguments."""
    by_id = {h.id: h for h in hypotheses}
    for s in result.signals:
        h = by_id.get(s.hypothesis_id)
        bb.audit("gate", "ESCALATED", {
            "hypothesis": s.hypothesis_id, "rank": s.rank,
            "priority": round(h.priority, 4) if h else None,
            "streams": sorted(h.distinct_streams) if h else [],
            "displaced": s.displaced_hypothesis_id,
        })
    for r in result.rejections:
        bb.audit("gate", "REJECTED", {
            "hypothesis": r.hypothesis_id, "reason": r.reason, "detail": r.detail})
    for d in result.displacements:
        bb.audit("gate", "DISPLACED", d.model_dump())
    bb.audit("gate", "SUMMARY", {
        "escalated": len(result.signals), "rejected": len(result.rejections),
        "displacements": len(result.displacements),
        "duplicate_evidence_refs_dropped": result.deduped_evidence_count,
    })


def trace(result: GateResult, hypotheses: list[Hypothesis]) -> str:
    """Human-readable verdict for every hypothesis. This is the demo artefact."""
    by_id = {h.id: h for h in hypotheses}
    ranks = {s.hypothesis_id: s.rank for s in result.signals}
    lines = [
        "GATE TRACE — every hypothesis and why it did or did not get an attention slot",
        "=" * 100,
        f"cap={C.ATTENTION_CAP}  min_sources={C.MIN_SOURCES}  min_confidence={C.MIN_CONFIDENCE}",
        f"duplicate evidence refs dropped: {result.deduped_evidence_count}",
        "",
    ]
    for s in sorted(result.signals, key=lambda x: x.rank):
        h = by_id[s.hypothesis_id]
        lines.append(
            f"  ESCALATED #{ranks[h.id]}  {h.id}  impact={h.impact:.2f} conf={h.confidence:.2f} "
            f"priority={h.priority:.3f} streams={','.join(sorted(h.distinct_streams))}")
        lines.append(f"      {h.title}")
    lines.append("")
    for r in result.rejections:
        h = by_id.get(r.hypothesis_id)
        detail = f"impact={h.impact:.2f} conf={h.confidence:.2f} " if h else ""
        lines.append(f"  REJECTED    {r.hypothesis_id}  [{r.reason}]  {detail}")
        lines.append(f"      {r.detail}")
    return "\n".join(lines)
```

---

## `src/attention_cascade/generator.py`

```python
"""Synthetic event generator with planted ground truth.

Does: emit ~1000 events across four enterprise streams over 60 days, containing 5 cross-system
causal incidents and 4 single-stream near-misses, plus the ground-truth mapping.
Does not: expose ground truth to the pipeline. Incident membership goes to its own table.
Exists because: without ground truth there is no baseline, and without a baseline the entire cost
claim is unfalsifiable. This file is the crown jewel - never cut it.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C
from .blackboard import Blackboard
from .models import Event

START = datetime(2026, 6, 1)
ACCOUNTS = [f"acct_{i:02d}" for i in range(1, C.N_ACCOUNTS + 1)]

# Accounts with heavy support traffic. INC-5 (silence) must target one of these,
# otherwise "absence of tickets" is not detectable.
CHATTY = {"acct_02", "acct_03", "acct_07", "acct_11"}

CRM_STAGES = ["discovery", "evaluation", "proposal", "negotiation", "closed_won", "closed_lost"]
ENG_TEAMS = ["platform", "ingest", "billing-svc", "integrations"]

# Product areas a support ticket can be filed against. This pool is the JOIN KEY between the
# engineering stream (organised by team and feature, entity_id="internal") and the support stream
# (organised by account). Delivery trackers really are not organised by customer, so the
# correlator has to bridge that asymmetry through a shared component name rather than through an
# account equality check - which is the more interesting problem, and the one worth paying a
# frontier model for. Noise tickets sample this same pool, so no value is unique to an incident.
COMPONENTS = ["F-Atlas", "integrations", "reporting", "sso-auth",
              "data-export", "billing-portal", "webhooks", "ingest-api"]

# What the ticket is actually about. Exists so the enricher has real content to vary; without it
# every enriched ticket collapsed to "Support ticket opened for acct_NN, severity 2."
SYMPTOMS = ["cannot log in", "slow response times", "unexpected 500 error",
            "missing data in export",
            "configuration question", "permission denied", "requests timing out",
            "totals do not reconcile", "webhook not firing", "report renders blank"]

# Per-incident symptom pools. A single pinned symptom made all eleven of INC-1's tickets read
# near-identically once enriched, which is a prose tell of exactly the kind enrichment exists to
# remove. Real queues describe one underlying problem many different ways, so these do too.
# The COMPONENT is what carries the join; the symptom only carries realism.
INCIDENT_SYMPTOMS = {
    "INC-1": ["blocked waiting on the Atlas rollout", "when is Atlas shipping",
              "Atlas timeline slipped again", "cannot start migration until Atlas lands",
              "need an ETA on Atlas before we can plan"],
    "INC-2": ["platform unreachable, total outage", "all API calls failing",
              "cannot reach the service at all", "hard down since this morning"],
    "INC-3": ["bill tripled after the plan change", "invoice does not match the quoted plan",
              "metered charges look wrong", "unexpected overage on the new plan"],
    "INC-4": ["webhooks failing signature validation", "webhook deliveries rejected since upgrade",
              "signature mismatch on every callback", "integration callbacks stopped arriving"],
}

# --------------------------------------------------------------------------------------
# Incident definitions. Each is a real cross-system causal chain spanning 2-3 streams.
# --------------------------------------------------------------------------------------
INCIDENTS = [
    {"id": "INC-1", "name": "feature_slip_cascade", "account": "acct_03", "day": 18,
     "streams": ["engineering", "support", "crm"],
     "story": "Atlas feature slips two sprints; the account waiting on it floods support;"
              " the deal stalls."},
    {"id": "INC-2", "name": "outage_billing", "account": "acct_07", "day": 31,
     "streams": ["support", "billing"],
     "story": "Sev-1 outage burst is followed by a collapse in metered usage."},
    {"id": "INC-3", "name": "pricing_migration", "account": "acct_05", "day": 12,
     "streams": ["billing", "support", "crm"],
     "story": "Forced plan migration triggers billing disputes and a forecast cut."},
    {"id": "INC-4", "name": "integration_regression", "account": "acct_09", "day": 40,
     "streams": ["engineering", "billing", "support"],
     "story": "A shipped regression silently breaks the API; usage drops before tickets appear."},
    {"id": "INC-5", "name": "champion_churn", "account": "acct_11", "day": 8,
     "streams": ["crm", "support", "billing"],
     "story": "The champion leaves; the account goes quiet; usage decays. Silence is the signal."},
]

# Single-stream lookalikes with no corroborating second system. These are what the
# sufficiency gate must reject. If one escalates, it is a false escalation.
# Lookalikes the sufficiency gate must reject. NM-1..NM-4 are single-stream, so rule 2 rejects
# them on structure alone. NM-5 is deliberately NOT: it is two streams, one account, one window,
# and causally unrelated. It passes sufficiency and is left for the confidence floor to catch -
# or to miss. Either outcome is a finding worth reporting, and without it nothing in the corpus
# exercises the floor at all and the gate gets credit for an if-statement.
NEAR_MISSES = [
    {"id": "NM-1", "account": "acct_02", "day": 45, "stream": "support"},
    {"id": "NM-2", "account": "acct_04", "day": 22, "stream": "billing"},
    {"id": "NM-3", "account": "acct_08", "day": 27, "stream": "engineering"},
    {"id": "NM-4", "account": "acct_06", "day": 50, "stream": "crm"},
    {"id": "NM-5", "account": "acct_10", "day": 52, "stream": "multi"},
]


class _Builder:
    """Accumulates events with temporary keys, then assigns stable ids in timestamp order."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self._rows: list[tuple[datetime, str, str, str, float | None, dict, str | None, bool]] = []

    def add(self, day: int, hour: int, stream: str, account: str, kind: str,
            numeric: float | None = None, incident: str | None = None,
            near_miss: bool = False, **payload) -> None:
        ts = START + timedelta(days=day, hours=hour, minutes=self.rng.randint(0, 59))
        self._rows.append((ts, stream, account, kind, numeric, payload, incident, near_miss))

    def finish(self) -> tuple[list[Event], list[tuple[str, str, str, bool]]]:
        self._rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
        events: list[Event] = []
        truth: list[tuple[str, str, str, bool]] = []
        for i, (ts, stream, account, kind, numeric, payload, incident, near_miss) in enumerate(
                self._rows, 1):
            eid = f"evt_{i:05d}"
            events.append(Event(id=eid, ts=ts, stream=stream, entity_id=account,
                                kind=kind, numeric=numeric, payload=payload))
            if incident:
                truth.append((incident, eid, stream, near_miss))
        return events, truth


# --------------------------------------------------------------------------------------
# Noise
# --------------------------------------------------------------------------------------

def _noise(b: _Builder, silence_windows: dict[str, tuple[int, int]],
           usage_windows: dict[str, tuple[int, int]] | None = None) -> None:
    rng = b.rng

    # CRM: stage changes and forecast updates, roughly 10 per account over the period.
    for acct in ACCOUNTS:
        stage_i = rng.randint(0, 2)
        amount = rng.choice([40_000, 75_000, 120_000, 250_000, 400_000])
        day = rng.randint(0, 5)
        while day < C.N_DAYS:
            if rng.random() < 0.6 and stage_i < len(CRM_STAGES) - 2:
                stage_i += 1
                b.add(day, 10, "crm", acct, "deal_stage_change", float(amount),
                      stage=CRM_STAGES[stage_i], text=f"moved to {CRM_STAGES[stage_i]}")
            else:
                delta = rng.uniform(-0.12, 0.15)
                amount = int(amount * (1 + delta))
                b.add(day, 11, "crm", acct, "forecast_update", float(amount),
                      delta_pct=round(delta, 3), text="forecast revised")
            day += rng.randint(4, 9)

    # Engineering: weekly-ish sprint reports per team plus scattered bug activity.
    for team in ENG_TEAMS:
        for day in range(3, C.N_DAYS, 7):
            planned = rng.randint(18, 30)
            delivered = planned - rng.randint(0, 4)
            b.add(day, 17, "engineering", "internal", "sprint_report", float(planned - delivered),
                  team=team, planned=planned, delivered=delivered, text=f"{team} sprint close")
    for _ in range(98):
        day = rng.randint(0, C.N_DAYS - 1)
        acct = rng.choice(ACCOUNTS + ["internal"] * 4)
        kind = rng.choice(["bug_opened", "bug_closed", "bug_reopened", "deploy"])
        b.add(day, rng.randint(9, 19), "engineering", acct, kind,
              team=rng.choice(ENG_TEAMS), text=kind.replace("_", " "))

    # Support: per-account daily ticket flow. Chatty accounts get a higher base rate.
    for acct in ACCOUNTS:
        lam = 1.2 if acct in CHATTY else 0.45
        window = silence_windows.get(acct)
        for day in range(C.N_DAYS):
            if window and window[0] <= day <= window[1]:
                continue  # deliberate silence, part of INC-5
            n = sum(1 for _ in range(3) if rng.random() < lam / 3)
            for _ in range(n):
                sev = rng.choices([1, 2, 3], weights=[0.05, 0.3, 0.65])[0]
                symptom = rng.choice(SYMPTOMS)
                b.add(day, rng.randint(8, 20), "support", acct, "ticket_opened", float(sev),
                      severity=sev, component=rng.choice(COMPONENTS), symptom=symptom,
                      text=symptom)

    # Billing: usage snapshot every 3 days per account, plus monthly invoices.
    #
    # usage_windows suppresses the NORMAL snapshots for an account over a range of days, because
    # an incident's planted decline REPLACES that account's usage rather than sitting beside it.
    # Without this the planted decay for INC-5 was interleaved with ordinary ~24k readings and the
    # account's mean never actually fell: the series wobbled and the declared "usage decays" story
    # was not present in the data at all. A detector cannot find a phenomenon that is not there,
    # and scoring recall against one would have been dishonest.
    usage_windows = usage_windows or {}
    for acct in ACCOUNTS:
        base = rng.uniform(4_000, 40_000)
        suppress = usage_windows.get(acct)
        for day in range(0, C.N_DAYS, 3):
            if suppress and suppress[0] <= day <= suppress[1]:
                continue
            usage = base * rng.uniform(0.92, 1.08)
            b.add(day, 2, "billing", acct, "usage_snapshot", round(usage, 1),
                  metric="api_calls", text="3-day usage rollup")
        for day in (5, 35):
            b.add(day, 6, "billing", acct, "invoice_issued", round(base * 0.9, 2),
                  text="monthly invoice")


# --------------------------------------------------------------------------------------
# Planted incidents
# --------------------------------------------------------------------------------------

def _plant_incidents(b: _Builder) -> None:
    rng = b.rng

    # INC-1: engineering slip -> support burst -> deal stall
    a, i = "acct_03", "INC-1"
    for k, day in enumerate((18, 20, 22)):
        b.add(day, 17, "engineering", "internal", "sprint_report", float(9 + k), incident=i,
              team="platform", planned=26, delivered=17 - k, feature="F-Atlas",
              text="Atlas milestone slipped again")
    # The join to engineering. Engineering is entity_id="internal", so account equality cannot
    # link these two hops - only the shared component value can. F-Atlas is in the noise pool too.
    for day in range(24, 31):
        for _ in range(rng.randint(1, 2)):
            sym = rng.choice(INCIDENT_SYMPTOMS["INC-1"])
            b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
                  severity=2, component="F-Atlas", symptom=sym, text=sym)
    b.add(33, 10, "crm", a, "deal_stage_change", 250_000.0, incident=i,
          stage="evaluation", regressed_from="negotiation", text="deal moved back to evaluation")
    b.add(35, 11, "crm", a, "forecast_update", 150_000.0, incident=i,
          delta_pct=-0.40, text="forecast cut, customer waiting on Atlas")

    # INC-2: sev-1 burst -> usage collapse
    a, i = "acct_07", "INC-2"
    for day in (31, 31, 32, 33, 33):
        sym = rng.choice(INCIDENT_SYMPTOMS["INC-2"])
        b.add(day, rng.randint(1, 23), "support", a, "ticket_opened", 1.0, incident=i,
              severity=1, component="ingest-api", symptom=sym, text=sym)
    for day, mult in ((34, 0.42), (37, 0.38), (40, 0.45)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(26_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")

    # INC-3: plan migration -> billing disputes -> forecast cut
    a, i = "acct_05", "INC-3"
    b.add(12, 6, "billing", a, "plan_change", 0.0, incident=i,
          from_plan="legacy_flat", to_plan="usage_metered", text="forced migration to metered plan")
    for day in (14, 15, 17, 18, 19, 20):
        sym = rng.choice(INCIDENT_SYMPTOMS["INC-3"])
        b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
              severity=2, category="billing_dispute", component="billing-portal",
              symptom=sym, text=sym)
    b.add(26, 11, "crm", a, "forecast_update", 66_000.0, incident=i,
          delta_pct=-0.45, text="renewal at risk over pricing")

    # INC-4: regression ships -> usage drops -> tickets follow
    a, i = "acct_09", "INC-4"
    b.add(40, 15, "engineering", "internal", "deploy", 0.0, incident=i,
          team="integrations", release="v3.11", text="integrations v3.11 shipped")
    for day in (41, 41, 42, 43, 44):
        b.add(day, rng.randint(9, 19), "engineering", "internal", "bug_reopened", 0.0, incident=i,
              team="integrations", text="webhook signature regression reopened")
    for day, mult in ((42, 0.51), (45, 0.47), (48, 0.5)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(18_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")
    # The join to engineering, twice over: component matches the deploy's team, release matches
    # its release string. Engineering is entity_id="internal", so again only the values link them.
    for day in (43, 45, 46, 47):
        sym = rng.choice(INCIDENT_SYMPTOMS["INC-4"])
        b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
              severity=2, component="integrations", release="v3.11", symptom=sym, text=sym)

    # INC-5: champion departs -> silence -> usage decay. The silence is created by the
    # silence_window passed into _noise; here we plant the bookends.
    a, i = "acct_11", "INC-5"
    b.add(8, 10, "crm", a, "contact_departed", 0.0, incident=i,
          contact_role="champion", text="primary champion left the company")
    for day, mult in ((20, 0.78), (23, 0.7), (26, 0.62), (29, 0.58)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(31_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")


def _plant_near_misses(b: _Builder) -> None:
    """Single-stream spikes that look like the first hop of an incident but never corroborate."""
    rng = b.rng
    for nm in NEAR_MISSES:
        a, day, i = nm["account"], nm["day"], nm["id"]
        if nm["stream"] == "support":
            for d in range(day, day + 4):
                for _ in range(rng.randint(2, 3)):
                    b.add(d, rng.randint(9, 18), "support", a, "ticket_opened", 2.0,
                          incident=i, near_miss=True, severity=2,
                          text="bulk import questions during onboarding wave")
        elif nm["stream"] == "billing":
            for d, mult in ((day, 0.55), (day + 3, 0.52), (day + 6, 0.6)):
                b.add(d, 2, "billing", a, "usage_snapshot", round(12_000 * mult, 1),
                      incident=i, near_miss=True, metric="api_calls",
                      text="3-day usage rollup")
        elif nm["stream"] == "engineering":
            for k, d in enumerate((day, day + 2)):
                b.add(d, 17, "engineering", "internal", "sprint_report", float(10 + k),
                      incident=i, near_miss=True, team="ingest", planned=24, delivered=14 - k,
                      text="ingest sprint slipped")
        elif nm["stream"] == "crm":
            # Three events, not one. A single CRM row was too thin to test anything: the gate
            # rejects it as single-source no matter how good or bad the gate is.
            b.add(day, 11, "crm", a, "forecast_update", 48_000.0,
                  incident=i, near_miss=True, delta_pct=-0.38,
                  text="forecast cut after budget freeze")
            b.add(day + 4, 10, "crm", a, "deal_stage_change", 48_000.0,
                  incident=i, near_miss=True, stage="evaluation",
                  regressed_from="proposal", text="pushed back to evaluation")
            b.add(day + 9, 11, "crm", a, "forecast_update", 34_000.0,
                  incident=i, near_miss=True, delta_pct=-0.29,
                  text="forecast trimmed again pending budget sign-off")
        elif nm["stream"] == "multi":
            # NM-5: two streams, one account, one window, NO causal link. A support spike from an
            # onboarding wave sits alongside a usage dip whose cause is innocent and stated in the
            # data (a seasonal shutdown). This passes the two-source sufficiency check, so it is
            # the ONLY thing in the corpus that reaches the confidence floor. If the floor lets it
            # through it is a false escalation; if it holds, the floor earned its place. Either
            # way it is a measured result rather than an untested rule.
            for d in range(day, day + 4):
                for _ in range(rng.randint(2, 3)):
                    b.add(d, rng.randint(9, 18), "support", a, "ticket_opened", 3.0,
                          incident=i, near_miss=True, severity=3,
                          component=rng.choice(["sso-auth", "reporting", "data-export"]),
                          symptom="onboarding walkthrough question",
                          text="onboarding walkthrough question")
            for d, mult in ((day + 1, 0.66), (day + 4, 0.61)):
                b.add(d, 2, "billing", a, "usage_snapshot", round(15_000 * mult, 1),
                      incident=i, near_miss=True, metric="api_calls",
                      note="customer holiday shutdown, scheduled",
                      text="3-day usage rollup")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def generate(db_path: Path | None = None, seed: int = C.SEED) -> dict:
    """Build the dataset. Deterministic: same seed produces byte-identical event ids."""
    db_path = Path(db_path or C.EVENTS_DB)
    if db_path.exists():
        db_path.unlink()

    b = _Builder(seed)
    silence = {"acct_11": (12, 30)}  # INC-5: the absence IS the evidence
    # Windows where an incident's planted usage decline replaces the account's normal readings.
    usage_suppression = {
        "acct_07": (33, 41),   # INC-2: usage collapse after the outage
        "acct_09": (41, 49),   # INC-4: usage drop from the integration regression
        "acct_11": (19, 30),   # INC-5: the gradual decay after the champion leaves
    }
    _noise(b, silence, usage_suppression)
    _plant_incidents(b)
    _plant_near_misses(b)
    events, truth = b.finish()

    bb = Blackboard(db_path, run_id="generate", arm="dataset")
    bb.insert_events(events)
    bb.insert_ground_truth(truth)
    bb.audit("generator", "DATASET_BUILT",
             {"events": len(events), "incidents": len(INCIDENTS), "near_misses": len(NEAR_MISSES),
              "seed": seed})
    bb.close()

    manifest = {
        "seed": seed,
        "start": START.isoformat(),
        "days": C.N_DAYS,
        "accounts": ACCOUNTS,
        "event_count": len(events),
        "incidents": INCIDENTS,
        "near_misses": NEAR_MISSES,
        "silence_windows": silence,
        "usage_suppression_windows": usage_suppression,
        "recall_definition": (
            "An incident is FOUND if some escalated signal's deduplicated evidence contains "
            "events tagged with that incident_id from >= 2 distinct streams."
        ),
    }
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(C.SEED_MANIFEST).write_text(json.dumps(manifest, indent=2))

    by_stream: dict[str, int] = {}
    for e in events:
        by_stream[e.stream] = by_stream.get(e.stream, 0) + 1
    return {"event_count": len(events), "by_stream": by_stream,
            "ground_truth_rows": len(truth), "db": str(db_path)}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
```

---

## `src/attention_cascade/groundtruth.py`

```python
"""Quarantined read-only accessors for the planted ground truth.

Does: read the `ground_truth` table so report.py can score recall and tests can assert the
dataset is shaped the way the manifest claims.
Does not: get imported by any pipeline module. detectors, triage, correlate, gate, orchestrator
and baseline are structurally blind to this file, and tests/test_no_groundtruth_leak.py enforces
that with an AST walk.
Exists because: recall is only meaningful if the thing being measured could not see the answer
key. Putting every incident lookup behind one importable module makes the quarantine checkable
by a machine instead of by trust.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C


@dataclass(frozen=True)
class Incident:
    """One planted cross-system chain (or one single-stream near-miss)."""

    id: str
    is_near_miss: bool
    event_ids: frozenset[str] = field(default_factory=frozenset)
    streams: frozenset[str] = field(default_factory=frozenset)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or C.EVENTS_DB)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist — run `ac generate` first")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def load(db_path: Path | None = None) -> dict[str, Incident]:
    """Every planted item, real incidents and near-misses alike, keyed by id."""
    conn = _connect(db_path)
    try:
        events: dict[str, set[str]] = defaultdict(set)
        streams: dict[str, set[str]] = defaultdict(set)
        near: dict[str, bool] = {}
        sql = "SELECT incident_id, event_id, stream, is_near_miss FROM ground_truth"
        for r in conn.execute(sql):
            key = r["incident_id"]
            events[key].add(r["event_id"])
            streams[key].add(r["stream"])
            near[key] = bool(r["is_near_miss"]) or near.get(key, False)
    finally:
        conn.close()
    return {
        k: Incident(id=k, is_near_miss=near[k],
                    event_ids=frozenset(events[k]), streams=frozenset(streams[k]))
        for k in sorted(events)
    }


def incidents(db_path: Path | None = None) -> dict[str, Incident]:
    """The five real cross-system incidents. These are what recall is measured against."""
    return {k: v for k, v in load(db_path).items() if not v.is_near_miss}


def near_misses(db_path: Path | None = None) -> dict[str, Incident]:
    """The four single-stream lookalikes. Escalating one of these is a false escalation."""
    return {k: v for k, v in load(db_path).items() if v.is_near_miss}


def event_to_incidents(db_path: Path | None = None) -> dict[str, set[str]]:
    """Reverse index: event id -> the planted items that claim it. Used to score evidence."""
    out: dict[str, set[str]] = defaultdict(set)
    for inc in load(db_path).values():
        for eid in inc.event_ids:
            out[eid].add(inc.id)
    return dict(out)


def stats(db_path: Path | None = None) -> dict:
    """Counts the review packet prints so a human can check the dataset is shaped as claimed."""
    all_items = load(db_path)
    real = {k: v for k, v in all_items.items() if not v.is_near_miss}
    near = {k: v for k, v in all_items.items() if v.is_near_miss}
    return {
        "incidents": len(real),
        "near_misses": len(near),
        "rows": sum(len(v.event_ids) for v in all_items.values()),
        "incident_detail": {
            k: {"events": len(v.event_ids), "streams": sorted(v.streams)} for k, v in real.items()
        },
        "near_miss_detail": {
            k: {"events": len(v.event_ids), "streams": sorted(v.streams)} for k, v in near.items()
        },
    }
```

---

## `src/attention_cascade/llm.py`

```python
"""The single metered gateway to Vertex AI. Every model call in this project goes through here.

Does: call Gemini on Vertex, record real token usage (including thinking tokens) into the
blackboard, cache every response to disk, and replay from cache with no network.
Does not: decide anything. It has no opinion about tiers, escalation, or importance.
Exists because: the deliverable is a measured cost table, and a cost table built on estimated
token counts is worthless. Rule 3 in CLAUDE.md lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import config as C
from .blackboard import Blackboard
from .models import LLMCall

_client: Any = None


def client() -> Any:
    """Lazily build the Vertex client. Import is local so replay mode works without the SDK."""
    global _client
    if _client is None:
        from google import genai  # noqa: PLC0415

        if not C.GCP_PROJECT:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in, "
                "or run with AC_LLM_MODE=replay to use the committed cache."
            )
        _client = genai.Client(vertexai=True, project=C.GCP_PROJECT, location=C.GCP_LOCATION)
    return _client


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0
    cached_input_tokens: int = 0
    from_cache: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    long_context: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the response as JSON, tolerating fenced or prose-wrapped output."""
        return parse_json(self.text)


def parse_json(text: str) -> Any:
    """Salvage JSON from a model response. Raises ValueError if nothing parses."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = t.find(opener), t.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"could not parse JSON from response of length {len(text)}")


def _cache_key(model: str, system: str, contents: Any, cfg: dict[str, Any]) -> str:
    blob = json.dumps(
        {"model": model, "system": system, "contents": contents, "cfg": cfg},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _read_cache(key: str) -> dict[str, Any] | None:
    path = C.CACHE_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _write_cache(key: str, payload: dict[str, Any]) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (C.CACHE_DIR / f"{key}.json").write_text(json.dumps(payload, indent=2, default=str))


def _usage(resp: Any) -> tuple[int, int, int, int]:
    """Pull real token counts off the response. Missing fields default to 0, never estimated."""
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return 0, 0, 0, 0
    return (
        int(getattr(u, "prompt_token_count", 0) or 0),
        int(getattr(u, "candidates_token_count", 0) or 0),
        int(getattr(u, "thoughts_token_count", 0) or 0),
        int(getattr(u, "cached_content_token_count", 0) or 0),
    )


async def call(
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    tier: str,
    run_id: str,
    bb: Blackboard,
    thinking_budget: int = 0,
    json_output: bool = True,
    temperature: float = 0.0,
) -> LLMResult:
    """One metered, cached Vertex call.

    AC_LLM_MODE:
      auto   - serve from cache when present, otherwise call and populate the cache
      live   - always call, still writes cache
      replay - cache only; a miss raises, so a demo never silently goes to the network
    """
    cfg = {
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "thinking_budget": thinking_budget,
        "json": json_output,
    }
    key = _cache_key(model, system, prompt, cfg)
    mode = C.LLM_MODE

    if mode in ("auto", "replay"):
        hit = _read_cache(key)
        if hit:
            res = LLMResult(
                text=hit["text"], model=model,
                input_tokens=hit["input_tokens"], output_tokens=hit["output_tokens"],
                thoughts_tokens=hit.get("thoughts_tokens", 0),
                cached_input_tokens=hit.get("cached_input_tokens", 0),
                from_cache=True, latency_ms=hit.get("latency_ms", 0),
                cost_usd=hit["cost_usd"], long_context=hit.get("long_context", False),
            )
            _record(bb, res, tier, run_id)
            return res
        if mode == "replay":
            raise RuntimeError(
                f"cache miss in replay mode (tier={tier}, key={key}). "
                "Rebuild the cache with AC_LLM_MODE=live before demoing offline."
            )

    from google.genai import types  # noqa: PLC0415

    gen_cfg: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_output:
        gen_cfg["response_mime_type"] = "application/json"
    if thinking_budget is not None:
        gen_cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

    last_err: Exception | None = None
    for attempt in range(C.LLM_MAX_RETRIES):
        started = time.perf_counter()
        try:
            resp = await client().aio.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(**gen_cfg),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            in_tok, out_tok, think_tok, cached_tok = _usage(resp)
            cost = C.price_call(model, in_tok, out_tok, think_tok, cached_tok)
            res = LLMResult(
                text=resp.text or "", model=model,
                input_tokens=in_tok, output_tokens=out_tok, thoughts_tokens=think_tok,
                cached_input_tokens=cached_tok, from_cache=False, latency_ms=latency_ms,
                cost_usd=cost, long_context=C.crossed_long_context(model, in_tok),
            )
            _write_cache(key, {
                "text": res.text, "model": model, "tier": tier,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "thoughts_tokens": think_tok, "cached_input_tokens": cached_tok,
                "latency_ms": latency_ms, "cost_usd": cost, "long_context": res.long_context,
            })
            if res.long_context:
                bb.audit(f"llm:{tier}", "LONG_CONTEXT_CLIFF",
                         {"model": model, "input_tokens": in_tok})
            _record(bb, res, tier, run_id)
            return res
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced and audited
            last_err = exc
            bb.audit(f"llm:{tier}", "CALL_FAILED",
                     {"attempt": attempt + 1, "model": model, "error": str(exc)[:400]})
            if attempt < C.LLM_MAX_RETRIES - 1:
                await asyncio.sleep(C.LLM_BACKOFF_BASE_S * (2**attempt) + random.random() * 0.3)

    raise RuntimeError(f"{tier} call failed after {C.LLM_MAX_RETRIES} attempts: {last_err}")


def _record(bb: Blackboard, res: LLMResult, tier: str, run_id: str) -> None:
    """Every call - cached or live - lands in llm_calls. Cached calls carry from_cache=True
    so the report can show both 'cost as measured live' and 'this run was a replay'."""
    bb.write_llm_call(LLMCall(
        run_id=run_id, tier=tier, model=res.model,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens, thinking_tokens=res.thoughts_tokens,
        cached_input_tokens=res.cached_input_tokens,
        from_cache=res.from_cache, latency_ms=res.latency_ms,
        cost_usd=res.cost_usd, ts=datetime.now(UTC),
    ))
    bb.audit(f"llm:{tier}", "CALL", {
        "model": res.model, "in": res.input_tokens, "out": res.output_tokens,
        "thinking": res.thoughts_tokens, "cached_in": res.cached_input_tokens,
        "cost_usd": round(res.cost_usd, 6), "from_cache": res.from_cache,
        "latency_ms": res.latency_ms,
    })


def list_available_models() -> list[str]:
    """Used by `ac verify --list-models`. Confirms the ids in config.py exist in your region."""
    return sorted(m.name for m in client().models.list())
```

---

## `src/attention_cascade/models.py`

```python
"""Typed objects that cross a tier boundary.

Does: define Event, Anomaly, Candidate, Hypothesis, Signal and the gate result types.
Does not: contain any incident/ground-truth field - that quarantine is deliberate (see CLAUDE.md).
Exists because: every tier writes to a shared blackboard, so the shapes must be agreed in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Stream = Literal["crm", "engineering", "support", "billing"]


class Event(BaseModel):
    """One raw fact emitted by one source system. Deliberately has no incident field."""

    id: str
    ts: datetime
    stream: Stream
    entity_id: str
    kind: str
    numeric: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_line(self) -> str:
        """Compact single-line rendering used in LLM prompts. Token-frugal on purpose."""
        num = "" if self.numeric is None else f" val={self.numeric:g}"
        extra = " ".join(f"{k}={v}" for k, v in self.payload.items() if k != "text")
        text = self.payload.get("text", "")
        return (
            f"{self.id} {self.ts:%Y-%m-%d} {self.stream} {self.entity_id} "
            f"{self.kind}{num} {extra} {text}"
        ).strip()


class Anomaly(BaseModel):
    """Tier 0 output. Produced by deterministic math, zero tokens."""

    id: str
    detector: str
    stream: Stream
    entity_id: str
    window_start: datetime
    window_end: datetime
    score: float
    kind: str
    event_ids: list[str]
    summary: str


class Candidate(BaseModel):
    """Tier 1 verdict on a single anomaly."""

    anomaly_id: str
    plausible: bool
    reason: str = ""
    business_hint: str = ""
    model: str = ""
    failed_open: bool = False


class EvidenceRef(BaseModel):
    event_id: str
    stream: Stream
    anomaly_id: str | None = None


class Hypothesis(BaseModel):
    """Tier 2 output: a proposed causal chain across systems."""

    id: str
    title: str
    narrative: str
    entity_id: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    impact: float = 0.0
    confidence: float = 0.0

    @property
    def distinct_streams(self) -> set[str]:
        return {e.stream for e in self.evidence}

    @property
    def priority(self) -> float:
        return self.impact * self.confidence


class Signal(BaseModel):
    """A hypothesis holding one of the seven attention slots."""

    hypothesis_id: str
    rank: int
    escalated_at: datetime
    displaced_hypothesis_id: str | None = None


class Rejection(BaseModel):
    hypothesis_id: str
    reason: Literal["single_source", "below_floor", "attention_budget_full", "no_valid_evidence"]
    detail: str = ""


class DisplacementEvent(BaseModel):
    incoming_hypothesis_id: str
    displaced_hypothesis_id: str
    incoming_score: float
    displaced_score: float


class GateResult(BaseModel):
    """Everything the gate decided, including what it refused and why."""

    signals: list[Signal] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)
    displacements: list[DisplacementEvent] = Field(default_factory=list)
    deduped_evidence_count: int = 0


class LLMCall(BaseModel):
    """One metered model call. Token counts come from the API response, never estimated."""

    run_id: str
    tier: str
    model: str
    input_tokens: int
    output_tokens: int          # visible output only; thinking is tracked separately
    thinking_tokens: int = 0    # Gemini 3.x internal reasoning. BILLS AT THE OUTPUT RATE.
    cached_input_tokens: int = 0
    from_cache: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    ts: datetime
```

---

## `src/attention_cascade/orchestrator.py`

```python
"""Wires the tiers together with bounded queues, backpressure and load shedding.

Does: run Tier 0 producers into a bounded anomaly queue, Tier 1 workers behind it, Tier 2 as the
single rate-limited bottleneck, then the gate. Sheds Tier 2 when the queue stays full.
Does not: decide what escalates, and does not hide coordination inside a framework. Plain asyncio,
one file, readable top to bottom.
Exists because: the interesting claim is not that a cascade is cheaper, it is that the mechanism
which controls cost IS the mechanism that sheds load. Under flood the system gets cheaper and
dumber instead of dying, and that happens here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from . import config as C
from . import correlate as t2
from . import triage as t1
from .blackboard import Blackboard
from .detectors import FloodSpec, KillSpec, run_detectors
from .gate import apply_gate, audit_gate
from .models import Anomaly, Candidate, GateResult, Hypothesis


@dataclass
class RunResult:
    """Everything one arm produced, plus the collapse line the demo prints."""

    events: int = 0
    anomalies: int = 0
    candidates: int = 0
    hypotheses: int = 0
    signals: int = 0
    hallucinated_event_ids: int = 0
    degraded: bool = False
    shed_reason: str = ""
    wall_clock_s: float = 0.0
    dead_streams: list[str] = field(default_factory=list)
    max_queue_depth: int = 0
    block_events: int = 0
    gate: GateResult | None = None
    hypothesis_list: list[Hypothesis] = field(default_factory=list)
    anomaly_list: list[Anomaly] = field(default_factory=list)
    candidate_list: list[Candidate] = field(default_factory=list)

    @property
    def collapse_line(self) -> str:
        return (f"{self.events} events → {self.anomalies} anomalies → {self.candidates} candidates"
                f" → {self.hypotheses} hypotheses → {self.signals} signals")


async def run_cascade(
    bb: Blackboard,
    *,
    run_id: str = "run",
    flood: FloodSpec | None = None,
    kill: KillSpec | None = None,
) -> RunResult:
    """Tier 0 → queue → Tier 1 → Tier 2 → gate. Returns the collapse and the gate decision."""
    started = time.perf_counter()
    res = RunResult(events=len(bb.events()))
    bb.set_meta("run_id", run_id)
    bb.audit("orchestrator", "START", {"run_id": run_id, "events": res.events,
                                       "flood": bool(flood), "kill": bool(kill)})

    anomaly_q: asyncio.Queue[Anomaly] = asyncio.Queue(maxsize=C.ANOMALY_QUEUE_MAX)
    depth_stats = {"max": 0, "blocks": 0, "full_since": 0.0, "shed": False, "reason": ""}

    async def watch_queue() -> None:
        """Sample the queue. If it stays full past the trigger, shed Tier 2 for this run."""
        while True:
            depth = anomaly_q.qsize()
            depth_stats["max"] = max(depth_stats["max"], depth)
            if anomaly_q.full():
                if not depth_stats["full_since"]:
                    depth_stats["full_since"] = time.perf_counter()
                    depth_stats["blocks"] += 1
                    bb.audit("orchestrator", "BACKPRESSURE",
                             {"queue": "anomaly_q", "depth": depth})
                elif (time.perf_counter() - depth_stats["full_since"] > C.SHED_TRIGGER_SECONDS
                      and not depth_stats["shed"]):
                    depth_stats["shed"] = True
                    depth_stats["reason"] = (
                        f"anomaly_q full for > {C.SHED_TRIGGER_SECONDS}s at depth {depth}")
                    bb.set_meta("degraded", True)
                    bb.audit("orchestrator", "SHED", {
                        "queue": "anomaly_q", "depth": depth,
                        "trigger_seconds": C.SHED_TRIGGER_SECONDS,
                        "effect": "Tier 2 will not be dispatched; escalating on Tier 1 + gate",
                        "note": "the system gets cheaper and dumber, never dead",
                    })
            else:
                depth_stats["full_since"] = 0.0
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_queue())
    try:
        anomalies = await run_detectors(bb, flood=flood, kill=kill, queue=anomaly_q)
    finally:
        watcher.cancel()

    res.anomalies = len(anomalies)
    res.anomaly_list = anomalies
    res.max_queue_depth = depth_stats["max"]
    res.block_events = depth_stats["blocks"]
    res.dead_streams = [s for s in C.STREAMS if bb.get_meta(f"stream_dead:{s}")]

    # ---- Tier 1 ----
    candidates = await t1.triage(bb, anomalies, run_id=run_id)
    plausible = [c for c in candidates if c.plausible]
    res.candidates = len(plausible)
    res.candidate_list = candidates

    # ---- Tier 2, unless shed ----
    if depth_stats["shed"]:
        res.degraded = True
        res.shed_reason = depth_stats["reason"]
        hypotheses, halluc = [], 0
        bb.audit("orchestrator", "TIER2_SKIPPED",
                 {"reason": res.shed_reason, "candidates_dropped": len(plausible)})
    else:
        hypotheses, halluc = await t2.correlate(bb, candidates, anomalies, run_id=run_id)

    res.hypotheses = len(hypotheses)
    res.hypothesis_list = hypotheses
    res.hallucinated_event_ids = halluc

    # ---- the gate: identical logic for both arms ----
    gate_result = apply_gate(hypotheses)
    audit_gate(bb, gate_result, hypotheses)
    bb.write_signals(gate_result.signals)
    res.gate = gate_result
    res.signals = len(gate_result.signals)

    res.wall_clock_s = round(time.perf_counter() - started, 2)
    bb.set_meta("collapse", [res.events, res.anomalies, res.candidates,
                             res.hypotheses, res.signals])
    bb.set_meta("wall_clock_s", res.wall_clock_s)
    bb.audit("orchestrator", "DONE", {
        "collapse": res.collapse_line, "degraded": res.degraded,
        "dead_streams": res.dead_streams, "wall_clock_s": res.wall_clock_s,
        "hallucinated_event_ids": res.hallucinated_event_ids,
        "max_queue_depth": res.max_queue_depth,
    })
    return res
```

---

## `src/attention_cascade/report.py`

```python
"""Scoring and reporting. The only pipeline-adjacent module allowed to read ground truth.

Does: render the dataset-inspection artefacts, score recall against the planted incidents, and
assemble review/checkpoint-N/ so a reviewer who cannot see this filesystem needs nothing else.
Does not: participate in the run. Nothing here influences detection, triage, correlation or the
gate — it reads finished state and grades it after the fact.
Exists because: a cost claim without a falsifiable recall number is marketing, and the grading
code has to live somewhere the quarantine test can point at and say "only this file".
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from . import config as C
from . import groundtruth as GT

CHECKPOINT_NAMES = {
    1: "Data foundation",
    2: "Cascade runs end to end with real numbers",
    3: "The table",
    4: "Break it on purpose",
    5: "Shippable",
}


# --------------------------------------------------------------------------------------
# Dataset inspection artefacts (Checkpoint 1)
# --------------------------------------------------------------------------------------

def _rows(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _fmt(r: sqlite3.Row) -> str:
    payload = json.loads(r["payload"])
    text = payload.pop("text", "")
    extra = " ".join(f"{k}={v}" for k, v in payload.items())
    num = "" if r["numeric"] is None else f" val={r['numeric']:g}"
    return (f"{r['id']} {r['ts'][:10]} {r['stream']:<11} {r['entity_id']:<9} "
            f"{r['kind']:<18}{num} {extra} | {text}")


def dataset_stats(db: Path | None = None) -> str:
    """Counts by stream, planted-item detail, and the two-run determinism check."""
    db = Path(db or C.EVENTS_DB)
    out: list[str] = ["DATASET STATISTICS", "=" * 100, ""]

    total = _rows(db, "SELECT COUNT(*) c FROM events")[0]["c"]
    lo, hi = C.TARGET_EVENT_COUNT
    out.append(f"Total events: {total}   (target band {lo}-{hi})")
    out.append("")
    out.append("By stream:")
    for r in _rows(db, "SELECT stream, COUNT(*) c FROM events GROUP BY stream ORDER BY stream"):
        out.append(f"  {r['stream']:<12} {r['c']:>5}")
    out.append("")
    out.append("By kind:")
    for r in _rows(db, "SELECT kind, COUNT(*) c FROM events GROUP BY kind ORDER BY c DESC"):
        out.append(f"  {r['kind']:<22} {r['c']:>5}")
    out.append("")
    out.append("By account:")
    by_acct = "SELECT entity_id, COUNT(*) c FROM events GROUP BY entity_id ORDER BY entity_id"
    for r in _rows(db, by_acct):
        out.append(f"  {r['entity_id']:<12} {r['c']:>5}")
    out.append("")

    st = GT.stats(db)
    out.append(f"Planted incidents:  {st['incidents']}  (expected 5)")
    for k, v in st["incident_detail"].items():
        out.append(f"  {k}  events={v['events']:<3} streams={','.join(v['streams'])}")
    out.append(f"Planted near-misses: {st['near_misses']}  (expected 4)")
    for k, v in st["near_miss_detail"].items():
        out.append(f"  {k}  events={v['events']:<3} streams={','.join(v['streams'])}"
                   "  <- single stream by design")
    out.append(f"Ground-truth rows:  {st['rows']}")
    out.append("")

    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(events)")}
    out.append(f"events table columns: {sorted(cols)}")
    out.append(f"incident column present in events table: {'incident_id' in cols}  (must be False)")
    out.append("")
    out.append(determinism_check())
    return "\n".join(out)


def determinism_check() -> str:
    """Generate twice into scratch databases and compare row counts and every event id."""
    from .generator import generate  # noqa: PLC0415 - avoids a cycle at import time

    tmp = C.RUNS_DIR / "_determinism"
    tmp.mkdir(parents=True, exist_ok=True)
    a, b = tmp / "a.db", tmp / "b.db"
    generate(db_path=a)
    generate(db_path=b)
    ids_a = [r[0] for r in sqlite3.connect(a).execute("SELECT id FROM events ORDER BY id")]
    ids_b = [r[0] for r in sqlite3.connect(b).execute("SELECT id FROM events ORDER BY id")]
    rows_a = [tuple(r) for r in sqlite3.connect(a).execute(
        "SELECT id, ts, stream, entity_id, kind, numeric FROM events ORDER BY id")]
    rows_b = [tuple(r) for r in sqlite3.connect(b).execute(
        "SELECT id, ts, stream, entity_id, kind, numeric FROM events ORDER BY id")]
    shutil.rmtree(tmp, ignore_errors=True)
    return (
        "DETERMINISM CHECK (two fresh `generate` runs, same seed)\n"
        f"  row counts equal:   {len(ids_a) == len(ids_b)}  ({len(ids_a)} vs {len(ids_b)})\n"
        f"  event ids identical: {ids_a == ids_b}\n"
        f"  full rows identical: {rows_a == rows_b}"
    )


def sample_events(db: Path | None = None, per_stream: int = 20) -> str:
    """20 events per stream, then one full incident chain and one near-miss chain side by side.

    This is the artefact a human eyeballs to decide whether the planted incidents are actually
    findable and whether the near-misses are genuinely tempting.
    """
    db = Path(db or C.EVENTS_DB)
    out: list[str] = ["SAMPLE EVENTS", "=" * 120, ""]
    for stream in C.STREAMS:
        out.append(f"--- {stream.upper()} (first {per_stream} by time) " + "-" * 60)
        for r in _rows(db, "SELECT * FROM events WHERE stream=? ORDER BY ts, id LIMIT ?",
                       (stream, per_stream)):
            out.append("  " + _fmt(r))
        out.append("")

    out.append("=" * 120)
    out.append("ONE FULL INCIDENT CHAIN vs ONE NEAR-MISS CHAIN")
    out.append("The point of enrichment is that these should NOT be separable by prose alone.")
    out.append("=" * 120)
    out.append("")
    for label, key in (("REAL INCIDENT INC-1 (feature_slip_cascade)", "INC-1"),
                       ("NEAR-MISS NM-1 (support-only lookalike)", "NM-1")):
        out.append(f"--- {label} " + "-" * 50)
        for r in _rows(db,
                       "SELECT e.* FROM events e JOIN ground_truth g ON g.event_id = e.id"
                       " WHERE g.incident_id = ? ORDER BY e.ts, e.id", (key,)):
            out.append("  " + _fmt(r))
        out.append("")
    out.append("INC-1 spans engineering -> support -> crm: three systems, one account, one story.")
    out.append("NM-1 is support only. It never corroborates, so the sufficiency gate must")
    out.append("reject it.")
    return "\n".join(out)


def enrichment_check(db: Path | None = None, n: int = 10) -> str:
    """Ten noise texts and ten incident texts, labels stripped, so a reviewer can try to
    tell them apart."""
    db = Path(db or C.EVENTS_DB)
    planted = set(GT.event_to_incidents(db))
    rows = _rows(db, "SELECT * FROM events ORDER BY id")
    noise = [r for r in rows if r["id"] not in planted]
    incident = [r for r in rows if r["id"] in planted]

    def texts(rs: list[sqlite3.Row], k: int) -> list[str]:
        step = max(len(rs) // k, 1)
        return [json.loads(r["payload"]).get("text", "") for r in rs[::step][:k]]

    out = [
        "ENRICHMENT CHECK — text only, labels stripped",
        "=" * 100,
        "If the two columns are textually distinguishable, enrichment failed and the experiment",
        "has a confound: a model could surface incidents by spotting distinctive prose rather",
        "than by reasoning across systems.",
        "",
        "Enrichment applied: " + ("YES" if _enriched(rows)
                                  else "NO — texts are still the canned generator strings"),
        "",
        "--- 10 NOISE EVENTS " + "-" * 60,
    ]
    out += [f"  {i+1:>2}. {t}" for i, t in enumerate(texts(noise, n))]
    out += ["", "--- 10 PLANTED-INCIDENT EVENTS " + "-" * 50]
    out += [f"  {i+1:>2}. {t}" for i, t in enumerate(texts(incident, n))]
    return "\n".join(out)


CANNED = {
    "login issue", "report export slow", "how do I configure sso", "dashboard blank",
    "invoice question", "api 500 intermittent", "3-day usage rollup", "monthly invoice",
    "forecast revised",
}


def _enriched(rows: list[sqlite3.Row]) -> bool:
    """Enrichment is present if the canned generator strings no longer dominate the corpus."""
    texts = [json.loads(r["payload"]).get("text", "") for r in rows]
    canned_hits = sum(1 for t in texts if t in CANNED)
    return canned_hits < len(texts) * 0.2


# --------------------------------------------------------------------------------------
# Linkage check — is each planted incident structurally connectable at all?
# --------------------------------------------------------------------------------------

# "internal" is not an account. Engineering rows carry it, so treating it as a join key would
# claim a link between every engineering event and every other engineering event.
NOT_AN_ACCOUNT = {"internal"}

# payload["text"] is the enriched prose. It is deliberately excluded: a join that only works
# before enrichment is exactly the defect this check exists to catch.
NOT_A_JOIN_KEY = {"text"}


def _join_values(payload: dict, entity_id: str) -> set[str]:
    """Structured values an event can be joined on. Prose and numbers are not join keys."""
    vals = {str(v) for k, v in payload.items()
            if k not in NOT_A_JOIN_KEY and isinstance(v, str) and len(v) >= 3}
    if entity_id not in NOT_AN_ACCOUNT:
        vals.add(entity_id)
    return vals


def linkage(db: Path | None = None) -> dict[str, dict]:
    """For each planted incident, which stream pairs are connectable and by what value.

    An incident is only findable if its streams form a connected graph. INC-1's engineering hop
    is entity_id="internal" while its support hop is "acct_03", so those two can only be linked
    by a shared component value — which is precisely what enrichment destroyed once before.
    """
    db = Path(db or C.EVENTS_DB)
    out: dict[str, dict] = {}

    for key, inc in sorted(GT.load(db).items()):
        rows = _rows(db,
                     "SELECT e.* FROM events e JOIN ground_truth g ON g.event_id = e.id"
                     " WHERE g.incident_id = ?", (key,))
        by_stream: dict[str, set[str]] = {}
        for r in rows:
            by_stream.setdefault(r["stream"], set()).update(
                _join_values(json.loads(r["payload"]), r["entity_id"]))

        streams = sorted(by_stream)
        edges: dict[tuple[str, str], list[str]] = {}
        for i, a in enumerate(streams):
            for bstream in streams[i + 1:]:
                shared = sorted(by_stream[a] & by_stream[bstream])
                if shared:
                    edges[(a, bstream)] = shared

        # Connectivity: can every stream be reached from the first one?
        seen = {streams[0]} if streams else set()
        changed = True
        while changed:
            changed = False
            for (a, bstream) in edges:
                if a in seen and bstream not in seen:
                    seen.add(bstream)
                    changed = True
                elif bstream in seen and a not in seen:
                    seen.add(a)
                    changed = True

        out[key] = {
            "is_near_miss": inc.is_near_miss,
            "streams": streams,
            "edges": {f"{a} <-> {b}": v for (a, b), v in sorted(edges.items())},
            "orphans": sorted(set(streams) - seen),
            "connected": len(streams) > 0 and not (set(streams) - seen),
        }
    return out


def linkage_report(db: Path | None = None) -> tuple[str, bool]:
    """Render the linkage table. Returns (text, ok). ok is False if any incident has an orphan."""
    data = linkage(db)
    lines = [
        "LINKAGE CHECK — can each planted incident actually be joined across its streams?",
        "=" * 100,
        "An incident whose streams do not form a connected graph is undiscoverable by any method,",
        "and every recall number that includes it is meaningless. Join keys are structured payload",
        "values and entity_id. payload['text'] is excluded on purpose: a join that survives only",
        "until enrichment rewrites the prose is not a join.",
        "",
    ]
    ok = True
    for key, d in data.items():
        kind = "NEAR-MISS" if d["is_near_miss"] else "INCIDENT"
        if d["is_near_miss"]:
            verdict = "n/a (near-miss)"
        elif d["connected"]:
            verdict = "CONNECTED"
        else:
            verdict = "*** ORPHANED STREAM ***"
            ok = False
        lines.append(f"{kind} {key}   streams={','.join(d['streams'])}   {verdict}")
        if d["edges"]:
            for pair, vals in d["edges"].items():
                shown = ", ".join(vals[:4]) + (" ..." if len(vals) > 4 else "")
                lines.append(f"    {pair:<28} via  {shown}")
        else:
            lines.append("    (no join between any pair)")
        if d["orphans"] and not d["is_near_miss"]:
            lines.append(f"    UNREACHABLE: {','.join(d['orphans'])}")
        lines.append("")

    lines.append("RESULT: " + ("all incidents connected" if ok
                               else "FAILED — at least one incident has an unreachable stream"))
    return "\n".join(lines), ok


# --------------------------------------------------------------------------------------
# Review packet
# --------------------------------------------------------------------------------------

def _capture(cmd: list[str]) -> str:
    """Run a command and capture stdout+stderr verbatim. Proof, not assertion."""
    try:
        p = subprocess.run(cmd, cwd=C.ROOT, capture_output=True, text=True, timeout=600)
        return f"$ {' '.join(cmd)}\n(exit {p.returncode})\n\n{p.stdout}{p.stderr}"
    except Exception as exc:  # noqa: BLE001 - a failed capture is itself review material
        return f"$ {' '.join(cmd)}\nCAPTURE FAILED: {exc}"


def _tree() -> str:
    skip = (".venv", ".git/", "__pycache__", ".pytest_cache", ".ruff_cache", "/runs/cache/")
    paths = sorted(
        str(p.relative_to(C.ROOT)) for p in C.ROOT.rglob("*")
        if p.is_file() and not any(s in str(p) for s in skip)
    )
    return "\n".join(paths)


def build_packet(n: int, claim: str, least_confident: list[str],
                 questions: list[str], extra: dict[str, str] | None = None) -> Path:
    """Copy everything a reviewer needs into review/checkpoint-N/, then write the manifest."""
    dest = C.REVIEW_DIR / f"checkpoint-{n}"
    dest.mkdir(parents=True, exist_ok=True)

    for name in ("CHECKPOINT_REPORT.md", "SCORECARD.md", "progress.md", "DECISIONS.md"):
        src = C.ROOT / name
        if src.exists():
            shutil.copy2(src, dest / name)

    (dest / "test_output.txt").write_text(_capture(["uv", "run", "pytest", "-v"]))
    (dest / "ruff_output.txt").write_text(_capture(["uv", "run", "ruff", "check"]))
    (dest / "tree.txt").write_text(_tree())
    (dest / "git_log.txt").write_text(_capture(["git", "log", "--oneline", "-20"]))
    # Every packet carries the linkage check. If an incident stops being joinable, the recall
    # numbers in that same packet are void, so the two belong in the reviewer's hands together.
    if C.EVENTS_DB.exists():
        (dest / "linkage.txt").write_text(linkage_report()[0])

    for fname, content in (extra or {}).items():
        (dest / fname).write_text(content)

    files = sorted(p.name for p in dest.iterdir() if p.is_file() and p.name != "MANIFEST.md")
    descriptions = {
        "CHECKPOINT_REPORT.md": "the claim being made, box by box",
        "SCORECARD.md": "metric and rubric trend across checkpoints",
        "progress.md": "how I got here, including the dead ends",
        "DECISIONS.md": "the trade-offs, each with its rejected alternative and its cost",
        "test_output.txt": "captured `uv run pytest -v`",
        "ruff_output.txt": "captured `uv run ruff check`",
        "tree.txt": "every file that actually exists in the repo",
        "git_log.txt": "commit hygiene, and proof of no unwanted attribution",
        "seed_manifest.json": "the incident definitions the dataset was built from",
        "sample_events.txt": "20 events per stream, plus an incident chain and a near-miss chain",
        "dataset_stats.txt": "counts by stream/kind/account, planted detail, determinism check",
        "enrichment_check.txt": "10 noise texts vs 10 incident texts, labels stripped",
        "model_availability.txt": "which config.py model ids are actually callable in this project",
        "linkage.txt": "proof each planted incident is joinable across its streams post-enrichment",
    }
    sha = _capture(["git", "rev-parse", "--short", "HEAD"]).strip().splitlines()[-1]

    lines = [
        f"# Review packet — Checkpoint {n}: {CHECKPOINT_NAMES.get(n, '')}",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}   Commit: {sha}",
        "",
        "## Claim",
        claim,
        "",
        "## Files in this packet",
    ]
    lines += [f"- `{f}` — {descriptions.get(f, 'see checkpoint report')}" for f in files]
    lines += ["", "## The three things I am least confident about"]
    lines += [f"{i}. {t}" for i, t in enumerate(least_confident, 1)]
    lines += ["", "## Specific questions for the reviewer"]
    lines += [f"- {q}" for q in questions] or ["- none"]
    (dest / "MANIFEST.md").write_text("\n".join(lines) + "\n")
    mirror_to_review_artifacts(dest)
    return dest


def mirror_to_review_artifacts(packet: Path) -> Path:
    """Copy a packet flat into review_artifacts/, wiping it first.

    review/checkpoint-N/ is the archive and is never touched again. This folder always holds
    exactly one checkpoint's worth of files, so the reviewer fetches the same path every time
    regardless of which checkpoint is current.
    """
    flat = C.ROOT / "review_artifacts"
    shutil.rmtree(flat, ignore_errors=True)
    flat.mkdir(parents=True, exist_ok=True)

    # The upload target accepts at most 19 files, so the per-module source snapshots are
    # concatenated into one document. Every file is still present in full, and
    # review/checkpoint-N/ keeps them separate as the archive.
    sources = sorted(p for p in packet.iterdir() if p.name.startswith("src_"))
    for src in sorted(packet.iterdir()):
        if src.is_file() and not src.name.startswith("src_"):
            shutil.copy2(src, flat / src.name)

    if sources:
        parts = ["# Source snapshot — every module, in full",
                 f"{len(sources)} files, concatenated because the upload accepts at most 19.",
                 "", "## Contents", ""]
        parts += [f"- `{p.name[4:-4]}`" for p in sources]
        parts.append("")
        for p in sources:
            name = p.name[4:-4]
            parts += ["", "---", "", f"## `src/attention_cascade/{name}`", "",
                      "```python", p.read_text().rstrip(), "```"]
        (flat / "SOURCE_CODE.md").write_text("\n".join(parts) + "\n")

    return flat


# --------------------------------------------------------------------------------------
# Recall scoring — the definitions here are the ones the README states, verbatim
# --------------------------------------------------------------------------------------

def incident_streams_present(event_ids: set[str], db: Path | None = None) -> dict[str, set[str]]:
    """For a set of surviving event ids, which streams of each planted incident are represented."""
    db = Path(db or C.EVENTS_DB)
    stream_of = {r["id"]: r["stream"] for r in _rows(db, "SELECT id, stream FROM events")}
    out: dict[str, set[str]] = {}
    for key, inc in GT.incidents(db).items():
        out[key] = {stream_of[e] for e in (inc.event_ids & event_ids) if e in stream_of}
    return out


def recoverable(event_ids: set[str], db: Path | None = None) -> set[str]:
    """An incident is RECOVERABLE at a stage if events from >= 2 of its streams survive there."""
    return {k for k, s in incident_streams_present(event_ids, db).items()
            if len(s) >= C.MIN_SOURCES}


def found(signals: list, hypotheses: list, db: Path | None = None) -> set[str]:
    """An incident is FOUND if some escalated signal's DEDUPLICATED evidence contains events
    tagged with that incident_id from >= 2 distinct streams.

    This is the exact definition in SPEC section 10 and in the README. Note it is per-signal:
    two signals each contributing one stream do not add up to a find.
    """
    from .gate import dedupe_evidence  # noqa: PLC0415

    by_id = {h.id: h for h in hypotheses}
    e2i = GT.event_to_incidents(db)
    real = set(GT.incidents(db))
    hits: set[str] = set()
    for s in signals:
        h = by_id.get(s.hypothesis_id)
        if h is None:
            continue
        per_incident: dict[str, set[str]] = {}
        for ref in dedupe_evidence(h):
            for inc in e2i.get(ref.event_id, ()):  # noqa: B007
                if inc in real:
                    per_incident.setdefault(inc, set()).add(ref.stream)
        hits |= {k for k, streams in per_incident.items() if len(streams) >= C.MIN_SOURCES}
    return hits


def false_escalations(signals: list, hypotheses: list, db: Path | None = None) -> list[str]:
    """An escalated signal whose evidence touches no real incident is a false escalation."""
    by_id = {h.id: h for h in hypotheses}
    e2i = GT.event_to_incidents(db)
    real = set(GT.incidents(db))
    out = []
    for s in signals:
        h = by_id.get(s.hypothesis_id)
        if h is None:
            continue
        touched = {i for ref in h.evidence for i in e2i.get(ref.event_id, ())}
        if not (touched & real):
            out.append(h.id)
    return out


def waterfall(res, db: Path | None = None) -> str:
    """Table B. Where in the cascade each incident stops being recoverable."""
    db = Path(db or C.EVENTS_DB)
    all_ids = {r["id"] for r in _rows(db, "SELECT id FROM events")}
    anomaly_ids = {e for a in res.anomaly_list for e in a.event_ids}
    kept = {c.anomaly_id for c in res.candidate_list if c.plausible}
    cand_ids = {e for a in res.anomaly_list if a.id in kept for e in a.event_ids}
    hyp_ids = {ref.event_id for h in res.hypothesis_list for ref in h.evidence}

    sig_hyp = {s.hypothesis_id for s in (res.gate.signals if res.gate else [])}
    sig_ids = {ref.event_id for h in res.hypothesis_list if h.id in sig_hyp for ref in h.evidence}

    stages = [
        ("Raw events", len(all_ids), recoverable(all_ids, db)),
        ("After Tier 0 detection", len(res.anomaly_list), recoverable(anomaly_ids, db)),
        ("After Tier 1 triage", res.candidates, recoverable(cand_ids, db)),
        ("After Tier 2 correlation", len(res.hypothesis_list), recoverable(hyp_ids, db)),
        ("After sufficiency gate", res.signals, recoverable(sig_ids, db)),
    ]
    hits = found(res.gate.signals if res.gate else [], res.hypothesis_list, db)

    lines = ["| Stage | Items | Incidents still recoverable (of 5) | Which |",
             "|---|---|---|---|"]
    for name, n, rec in stages:
        lines.append(f"| {name} | {n} | {len(rec)} | {','.join(sorted(rec)) or '—'} |")
    lines.append(f"| **In final {C.ATTENTION_CAP} (FOUND)** | {res.signals} | "
                 f"{len(hits)} | {','.join(sorted(hits)) or '—'} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Checkpoint 2 artefacts
# --------------------------------------------------------------------------------------

def latest_run_dir(arm: str = "cascade") -> Path | None:
    """Newest runs/<id>/run.db whose signals belong to `arm`. Packets describe a real run."""
    candidates = []
    for db in C.RUNS_DIR.glob("*/run.db"):
        try:
            conn = sqlite3.connect(db)
            n = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE arm = ?", (arm,)).fetchone()[0]
            conn.close()
            if n:
                candidates.append(db)
        except sqlite3.Error:
            continue
    return max(candidates, key=lambda p: p.stat().st_mtime).parent if candidates else None


def llm_calls_csv(run_db: Path) -> str:
    """The full llm_calls table, thinking-token column included. Raw, not summarised."""
    conn = sqlite3.connect(run_db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM llm_calls ORDER BY id"))
    conn.close()
    if not rows:
        return "no llm calls recorded\n"
    cols = rows[0].keys()
    out = [",".join(cols)]
    out += [",".join(str(r[c]) for c in cols) for r in rows]
    return "\n".join(out) + "\n"


def cost_by_tier(run_db: Path) -> str:
    """Table C: per tier, calls, tokens, cost, and share of total spend."""
    conn = sqlite3.connect(run_db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT arm, tier, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,"
        " SUM(thinking_tokens) t, SUM(cached_input_tokens) ci, SUM(cost_usd) c"
        " FROM llm_calls GROUP BY arm, tier, model ORDER BY arm, tier"))
    conn.close()
    total = sum(r["c"] or 0.0 for r in rows) or 1.0
    lines = ["| Arm | Tier | Model | Calls | Input | Visible out | Thinking | Cost USD | Share |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['arm']} | {r['tier']} | `{r['model']}` | {r['n']} | {r['i']:,} | {r['o']:,} | "
            f"{r['t']:,} | ${r['c']:.4f} | {(r['c'] or 0) / total * 100:.1f}% |")
    lines.append(f"\n**Total: ${total:.4f}**  "
                 "(thinking tokens are billed at the output rate and are included in cost)")
    return "\n".join(lines)


def anomalies_sample(res, n: int = 15) -> str:
    lines = [f"ANOMALY SAMPLE — {n} of {len(res.anomaly_list)}, highest score first",
             "=" * 110, ""]
    for a in res.anomaly_list[:n]:
        lines.append(f"{a.id}  {a.detector:<22} {a.stream:<12} {a.entity_id:<10} "
                     f"score={a.score:<8} events={len(a.event_ids)}")
        lines.append(f"    {a.summary}")
    return "\n".join(lines)


def hypotheses_json(res) -> str:
    """Everything Tier 2 proposed, BEFORE the gate. The gate's input, for auditing."""
    return json.dumps([h.model_dump() for h in res.hypothesis_list], indent=2, default=str)
```

---

## `src/attention_cascade/triage.py`

```python
"""Tier 1: a cheap model answering one narrow question about a batch of anomalies.

Does: batch anomalies ten at a time to gemini-3.5-flash-lite with thinking disabled, and record a
plausible/not verdict per anomaly. Fails open on malformed JSON.
Does not: rank, score, decide what escalates, or see more than one anomaly's summary at a time.
It cannot correlate — it has no cross-stream context by construction.
Exists because: most anomalies are routine and a frontier model should never read them. This tier
asks a yes/no question, so internal reasoning here would be pure cost: thinking budget is 0, and
that asymmetry against Tier 2's 2048 is itself the attention-budget decision.
"""

from __future__ import annotations

import asyncio
import json

from . import config as C
from .blackboard import Blackboard
from .llm import call, parse_json
from .models import Anomaly, Candidate

SYSTEM = """\
You are a triage filter in a business-signal pipeline. You will receive a batch of detected
anomalies from enterprise systems. For each one, answer a single narrow question: could this
plausibly require attention from a business decision-maker, either on its own or as part of a
larger cross-system pattern?

Be permissive at this stage. A later stage does the expensive reasoning and a deterministic gate
decides what escalates. Your job is only to discard the obviously routine.

Return ONLY a JSON array, no other text, no markdown fences. One object per input anomaly:
[{"anomaly_id": "...", "plausible": true, "reason": "<max 15 words>", "business_hint": "<max 10 words>"}]"""  # noqa: E501


def _payload(a: Anomaly) -> dict:
    return {
        "anomaly_id": a.id,
        "detector": a.detector,
        "stream": a.stream,
        "entity": a.entity_id,
        "window": f"{a.window_start:%Y-%m-%d}..{a.window_end:%Y-%m-%d}",
        "score": a.score,
        "summary": a.summary,
    }


async def triage(bb: Blackboard, anomalies: list[Anomaly], *,
                 run_id: str = "run") -> list[Candidate]:
    """Return one Candidate per anomaly. Order follows the input.

    A batch that will not parse is retried once with a repair instruction and then FAILS OPEN:
    every anomaly in it is marked plausible. Failing closed would silently destroy recall, and a
    parse error is a pipeline defect, not evidence that the anomalies were routine.
    """
    batches = [anomalies[i : i + C.TIER1_BATCH_SIZE]
               for i in range(0, len(anomalies), C.TIER1_BATCH_SIZE)]
    results: dict[str, Candidate] = {}
    sem = asyncio.Semaphore(C.TIER1_WORKERS)

    async def do_batch(n: int, batch: list[Anomaly]) -> None:
        allowed = {a.id for a in batch}
        prompt = json.dumps([_payload(a) for a in batch])
        verdicts: dict[str, dict] = {}
        failed_open = False

        for attempt in (1, 2):
            system = SYSTEM if attempt == 1 else (
                SYSTEM + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON array.")
            try:
                res = await call(
                    model=C.TIER1_MODEL, system=system, prompt=prompt,
                    max_tokens=C.TIER1_MAX_TOKENS, tier="tier1", run_id=run_id, bb=bb,
                    thinking_budget=C.TIER1_THINKING_BUDGET, json_output=True,
                )
                parsed = parse_json(res.text)
                if not isinstance(parsed, list):
                    raise ValueError("expected a JSON array")
                for item in parsed:
                    if isinstance(item, dict) and str(item.get("anomaly_id")) in allowed:
                        verdicts[str(item["anomaly_id"])] = item
                break
            except Exception as exc:  # noqa: BLE001 - surfaced and audited, never swallowed
                bb.audit("tier1", "BATCH_PARSE_FAILED",
                         {"batch": n, "attempt": attempt, "error": str(exc)[:300]})
                if attempt == 2:
                    failed_open = True

        missing = allowed - set(verdicts)
        if missing and not failed_open:
            # The model answered, but skipped some anomalies. Those fail open too.
            bb.audit("tier1", "ANOMALIES_OMITTED", {"batch": n, "count": len(missing)})

        for a in batch:
            v = verdicts.get(a.id)
            if v is None:
                results[a.id] = Candidate(
                    anomaly_id=a.id, plausible=True, reason="failed open: no verdict returned",
                    business_hint="", model=C.TIER1_MODEL, failed_open=True)
            else:
                results[a.id] = Candidate(
                    anomaly_id=a.id, plausible=bool(v.get("plausible", True)),
                    reason=str(v.get("reason", ""))[:120],
                    business_hint=str(v.get("business_hint", ""))[:80],
                    model=C.TIER1_MODEL, failed_open=False)

        if failed_open:
            bb.audit("tier1", "FAILED_OPEN",
                     {"batch": n, "anomalies": sorted(allowed),
                      "note": "all marked plausible; a parse error must not destroy recall"})

    async def guarded(n: int, batch: list[Anomaly]) -> None:
        async with sem:
            await do_batch(n, batch)

    await asyncio.gather(*(guarded(n, b) for n, b in enumerate(batches)))

    out = [results[a.id] for a in anomalies]
    for c in out:
        await bb.write_candidate(c)
    kept = [c for c in out if c.plausible]
    bb.audit("tier1", "SUMMARY", {
        "anomalies": len(anomalies), "batches": len(batches),
        "plausible": len(kept), "discarded": len(out) - len(kept),
        "failed_open": sum(1 for c in out if c.failed_open),
        "model": C.TIER1_MODEL, "thinking_budget": C.TIER1_THINKING_BUDGET,
    })
    return out
```

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
-- and is readable only by report.py and tests. See DESIGN.md section 11.
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

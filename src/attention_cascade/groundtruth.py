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

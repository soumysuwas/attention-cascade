"""The blackboard is the only coordination mechanism, so its guarantees have to be real.

Covers: schema round-trips for every typed object, the append-only audit trail, metered call
recording including the thinking-token column, and the claim the architecture rests on — that
concurrent writers are serialized so no anomaly is lost and no evidence is double-counted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from attention_cascade.blackboard import Blackboard
from attention_cascade.models import (
    Anomaly,
    Candidate,
    Event,
    EvidenceRef,
    Hypothesis,
    LLMCall,
    Signal,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def bb(tmp_path: Path) -> Blackboard:
    board = Blackboard(tmp_path / "run.db", run_id="t", arm="cascade")
    yield board
    board.close()


def _anomaly(i: int, stream: str = "support") -> Anomaly:
    return Anomaly(
        id=f"anom_{i:03d}", detector="ticket_volume_spike", stream=stream,
        entity_id="acct_01", window_start=NOW, window_end=NOW, score=float(i),
        kind="spike", event_ids=[f"evt_{i:05d}"], summary=f"spike {i}",
    )


# ---------------- schema round-trips ----------------

def test_events_round_trip(bb: Blackboard) -> None:
    evs = [
        Event(id="evt_00001", ts=NOW, stream="crm", entity_id="acct_01",
              kind="forecast_update", numeric=1000.0, payload={"text": "hi", "delta_pct": -0.4}),
        Event(id="evt_00002", ts=NOW, stream="billing", entity_id="acct_02",
              kind="usage_snapshot", numeric=None, payload={}),
    ]
    assert bb.insert_events(evs) == 2
    back = bb.events()
    assert [e.id for e in back] == ["evt_00001", "evt_00002"]
    assert back[0].payload["delta_pct"] == -0.4
    assert back[1].numeric is None
    assert bb.event_ids() == {"evt_00001", "evt_00002"}


def test_events_can_be_filtered_by_stream(bb: Blackboard) -> None:
    bb.insert_events([
        Event(id="e1", ts=NOW, stream="crm", entity_id="a", kind="k"),
        Event(id="e2", ts=NOW, stream="billing", entity_id="a", kind="k"),
    ])
    assert [e.id for e in bb.events(stream="crm")] == ["e1"]


def test_insert_events_is_idempotent(bb: Blackboard) -> None:
    """Re-running generation must not duplicate rows — the id is the primary key."""
    e = Event(id="evt_00001", ts=NOW, stream="crm", entity_id="a", kind="k")
    bb.insert_events([e])
    bb.insert_events([e])
    assert len(bb.events()) == 1


async def test_anomaly_round_trip(bb: Blackboard) -> None:
    await bb.write_anomaly(_anomaly(1), author="detector:support")
    (got,) = bb.anomalies()
    assert got.id == "anom_001"
    assert got.event_ids == ["evt_00001"]
    assert got.detector == "ticket_volume_spike"


async def test_anomalies_come_back_ranked_by_score(bb: Blackboard) -> None:
    for i in (1, 5, 3):
        await bb.write_anomaly(_anomaly(i), author="d")
    assert [a.score for a in bb.anomalies()] == [5.0, 3.0, 1.0]


async def test_candidate_round_trip(bb: Blackboard) -> None:
    await bb.write_candidate(Candidate(anomaly_id="anom_001", plausible=True,
                                       reason="r", business_hint="h",
                                       model="gemini-3.1-flash-lite", failed_open=True))
    row = bb.conn.execute("SELECT * FROM candidates").fetchone()
    assert row["plausible"] == 1 and row["failed_open"] == 1


async def test_hypothesis_round_trip_preserves_evidence(bb: Blackboard) -> None:
    h = Hypothesis(
        id="hyp_1", title="t", narrative="n", entity_id="acct_01", impact=0.8, confidence=0.7,
        evidence=[EvidenceRef(event_id="evt_00001", stream="support", anomaly_id="anom_001"),
                  EvidenceRef(event_id="evt_00002", stream="billing")],
    )
    await bb.write_hypothesis(h)
    (got,) = bb.hypotheses()
    assert got.distinct_streams == {"support", "billing"}
    assert got.evidence[1].anomaly_id is None
    assert got.priority == pytest.approx(0.56)


async def test_hypotheses_are_scoped_to_the_arm(bb: Blackboard) -> None:
    """Both arms share one database file, so a leak here would corrupt the comparison."""
    await bb.write_hypothesis(Hypothesis(id="h_casc", title="t", narrative="n", entity_id="a"))
    bb.arm = "baseline"
    await bb.write_hypothesis(Hypothesis(id="h_base", title="t", narrative="n", entity_id="a"))
    assert [h.id for h in bb.hypotheses()] == ["h_base"]
    bb.arm = "cascade"
    assert [h.id for h in bb.hypotheses()] == ["h_casc"]


def test_signals_round_trip_with_displacement(bb: Blackboard) -> None:
    bb.write_signals([
        Signal(hypothesis_id="h1", rank=1, escalated_at=NOW),
        Signal(hypothesis_id="h2", rank=2, escalated_at=NOW, displaced_hypothesis_id="h9"),
    ])
    rows = bb.signals()
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[1]["displaced_hypothesis_id"] == "h9"


# ---------------- metering ----------------

def test_llm_call_records_thinking_tokens_in_their_own_column(bb: Blackboard) -> None:
    """Thinking tokens bill at the output rate. They must be stored separately, never folded in."""
    bb.write_llm_call(LLMCall(
        run_id="t", tier="tier2", model="gemini-3.1-pro", input_tokens=1200,
        output_tokens=300, thinking_tokens=2048, cached_input_tokens=100,
        from_cache=False, latency_ms=900, cost_usd=0.031, ts=NOW,
    ))
    row = bb.conn.execute("SELECT * FROM llm_calls").fetchone()
    assert row["thinking_tokens"] == 2048
    assert row["output_tokens"] == 300, "visible output must not absorb thinking tokens"
    assert row["cached_input_tokens"] == 100
    assert row["arm"] == "cascade"


# ---------------- audit trail ----------------

def test_audit_is_append_only_and_mirrored_to_jsonl(bb: Blackboard) -> None:
    bb.audit("gate", "REJECTED", {"hypothesis": "h1", "reason": "single_source"})
    bb.audit("gate", "REJECTED", {"hypothesis": "h2", "reason": "below_floor"})
    rows = list(bb.conn.execute("SELECT * FROM audit ORDER BY id"))
    assert len(rows) == 2
    assert json.loads(rows[0]["detail"])["reason"] == "single_source"

    lines = [json.loads(x) for x in bb.audit_path.read_text().splitlines()]
    assert [x["detail"]["reason"] for x in lines] == ["single_source", "below_floor"]


def test_audit_accepts_a_plain_string(bb: Blackboard) -> None:
    bb.audit("detector:support", "STREAM_DEAD", "killed at t=3s")
    assert bb.conn.execute("SELECT detail FROM audit").fetchone()[0] == "killed at t=3s"


# ---------------- run metadata ----------------

def test_meta_round_trip_and_default(bb: Blackboard) -> None:
    bb.set_meta("degraded", True)
    bb.set_meta("collapse", [1090, 84, 16, 11, 7])
    assert bb.get_meta("degraded") is True
    assert bb.get_meta("collapse") == [1090, 84, 16, 11, 7]
    assert bb.get_meta("missing", "fallback") == "fallback"


# ---------------- the concurrency claim ----------------

async def test_concurrent_writers_do_not_lose_or_duplicate_anomalies(bb: Blackboard) -> None:
    """Four detectors write at once, exactly as they do in a real run.

    Writes are serialized behind a single asyncio.Lock deliberately. This test is the evidence
    for that claim: 200 concurrent writes must produce exactly 200 distinct rows.
    """
    streams = ("crm", "engineering", "support", "billing")

    async def detector(stream: str, offset: int) -> None:
        for i in range(50):
            await bb.write_anomaly(_anomaly(offset + i, stream), author=f"detector:{stream}")
            await asyncio.sleep(0)

    await asyncio.gather(*(detector(s, k * 50) for k, s in enumerate(streams)))

    rows = bb.anomalies()
    assert len(rows) == 200
    assert len({a.id for a in rows}) == 200
    per_stream = {s: sum(1 for a in rows if a.stream == s) for s in streams}
    assert per_stream == {s: 50 for s in streams}


async def test_a_crashing_writer_does_not_hold_the_lock(bb: Blackboard) -> None:
    """A detector dying mid-run must not wedge the other three. This is the kill demo's premise."""

    async def bad() -> None:
        async with bb._lock:
            raise RuntimeError("detector crashed")

    with pytest.raises(RuntimeError):
        await bad()
    await bb.write_anomaly(_anomaly(99), author="detector:billing")
    assert len(bb.anomalies()) == 1


# ---------------- durability ----------------

def test_schema_survives_reopen_and_uses_wal(tmp_path: Path) -> None:
    path = tmp_path / "run.db"
    b1 = Blackboard(path, run_id="t", arm="cascade")
    b1.insert_events([Event(id="e1", ts=NOW, stream="crm", entity_id="a", kind="k")])
    b1.close()

    b2 = Blackboard(path, run_id="t", arm="cascade")
    assert [e.id for e in b2.events()] == ["e1"]
    mode = b2.conn.execute("PRAGMA journal_mode").fetchone()[0]
    b2.close()
    assert mode.lower() == "wal"


def test_events_table_shape_is_locked(tmp_path: Path) -> None:
    """If a column is ever added here, the ground-truth quarantine test must be re-read."""
    b = Blackboard(tmp_path / "run.db")
    cols = {r[1] for r in b.conn.execute("PRAGMA table_info(events)")}
    b.close()
    assert cols == {"id", "ts", "stream", "entity_id", "kind", "numeric", "payload"}


def test_ground_truth_lives_in_its_own_table(tmp_path: Path) -> None:
    b = Blackboard(tmp_path / "run.db")
    b.insert_ground_truth([("INC-1", "evt_00001", "support", False)])
    tables = {r[0] for r in b.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    b.close()
    assert "events" in tables
    conn = sqlite3.connect(tmp_path / "run.db")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

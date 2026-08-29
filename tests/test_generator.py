"""The dataset is the experiment. If it is not shaped as claimed, every number downstream is void.

Asserts the size band, the stream split, determinism under a fixed seed, and — the important one —
that every planted incident actually spans the streams its definition declares. An incident that
only reaches one stream cannot be found by a two-source gate, so it would be an unwinnable test.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from attention_cascade import config as C
from attention_cascade import groundtruth as GT
from attention_cascade.generator import INCIDENTS, NEAR_MISSES, generate


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("gen") / "events.db"
    generate(db_path=path)
    return path


def test_event_count_in_target_band(db: Path) -> None:
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    lo, hi = C.TARGET_EVENT_COUNT
    assert lo <= n <= hi, f"{n} events, expected {lo}-{hi}"


def test_all_four_streams_present(db: Path) -> None:
    streams = {r[0] for r in sqlite3.connect(db).execute("SELECT DISTINCT stream FROM events")}
    assert streams == set(C.STREAMS)
    for s in C.STREAMS:
        n = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM events WHERE stream=?", (s,)).fetchone()[0]
        assert n >= 50, f"stream {s} only has {n} events"


def test_exactly_five_incidents_and_five_near_misses(db: Path) -> None:
    assert len(GT.incidents(db)) == 5
    assert len(GT.near_misses(db)) == 5


# INC-5's support hop is a SILENCE. An absence of tickets has no events of its own, so there is
# nothing for the ground_truth table to tag with a support row. The chain still reaches two
# systems (crm contact_departed + billing usage decay), which is what the gate actually needs.
# See DESIGN.md section 4. This is the only incident whose declared and tagged streams differ,
# and the difference is stated here rather than hidden by a weaker assertion.
SILENCE_HOP = {"INC-5": {"support"}}


@pytest.mark.parametrize("spec", INCIDENTS, ids=lambda s: s["id"])
def test_incident_spans_its_declared_streams(db: Path, spec: dict) -> None:
    """The load-bearing dataset assertion: a declared cross-system chain must really cross."""
    actual = GT.incidents(db)[spec["id"]].streams
    expected = set(spec["streams"]) - SILENCE_HOP.get(spec["id"], set())
    assert actual == expected, f"{spec['id']} expected {sorted(expected)}, planted {sorted(actual)}"
    assert len(actual) >= 2, f"{spec['id']} is single-source and could never pass the gate"


def test_only_inc5_relies_on_a_silence_hop(db: Path) -> None:
    """Guards the exception above: if another incident starts under-planting, this fails."""
    for spec in INCIDENTS:
        gap = set(spec["streams"]) - GT.incidents(db)[spec["id"]].streams
        assert gap == SILENCE_HOP.get(spec["id"], set()), (
            f"{spec['id']} declares {sorted(spec['streams'])} but only planted "
            f"{sorted(GT.incidents(db)[spec['id']].streams)}"
        )


SINGLE_STREAM_NEAR_MISSES = [n for n in NEAR_MISSES if n["stream"] != "multi"]


@pytest.mark.parametrize("spec", SINGLE_STREAM_NEAR_MISSES, ids=lambda s: s["id"])
def test_near_miss_is_single_stream(db: Path, spec: dict) -> None:
    """These four are rejected by sufficiency on structure alone, before confidence matters."""
    streams = GT.near_misses(db)[spec["id"]].streams
    assert streams == {spec["stream"]}, f"{spec['id']} spans {sorted(streams)}"


def test_nm5_is_two_stream_so_it_reaches_the_confidence_floor(db: Path) -> None:
    """NM-5 is the only thing in the corpus that tests the floor.

    The other four near-misses are single-stream, so rule 2 rejects them and the confidence
    floor never runs. NM-5 passes sufficiency deliberately - two streams, one account, one
    window, no causal link - so whether the floor catches it is a measured result.
    """
    nm5 = GT.near_misses(db)["NM-5"]
    assert nm5.streams == {"support", "billing"}, f"NM-5 spans {sorted(nm5.streams)}"

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT e.entity_id, e.ts FROM events e JOIN ground_truth g ON g.event_id = e.id"
        " WHERE g.incident_id = 'NM-5'").fetchall()
    assert {r[0] for r in rows} == {"acct_10"}, "NM-5 must be one account or it is not tempting"
    days = {r[1][:10] for r in rows}
    assert len(days) <= 8, f"NM-5 spread over {len(days)} days; it must look like one window"


def test_incident_events_belong_to_the_declared_account(db: Path) -> None:
    """Incidents join across systems on entity_id. Internal engineering rows are the exception."""
    conn = sqlite3.connect(db)
    for spec in INCIDENTS:
        rows = conn.execute(
            "SELECT e.entity_id FROM events e JOIN ground_truth g ON g.event_id = e.id"
            " WHERE g.incident_id = ?", (spec["id"],)).fetchall()
        accounts = {r[0] for r in rows} - {"internal"}
        assert accounts == {spec["account"]}, f"{spec['id']} touches {accounts}"


def test_inc5_silence_window_really_is_silent(db: Path) -> None:
    """INC-5 is an absence signal. If tickets exist in the window, there is nothing to detect."""
    manifest = json.loads(Path(C.SEED_MANIFEST).read_text())
    acct, (lo, hi) = next(iter(manifest["silence_windows"].items()))
    start = manifest["start"][:10]
    n = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM events WHERE stream='support' AND entity_id=?"
        " AND date(ts) BETWEEN date(?, ?) AND date(?, ?)",
        (acct, start, f"+{lo} days", start, f"+{hi} days")).fetchone()[0]
    assert n == 0, f"{acct} has {n} tickets inside its silence window"


def test_generation_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    generate(db_path=a)
    generate(db_path=b)
    q = "SELECT id, ts, stream, entity_id, kind, numeric FROM events ORDER BY id"
    assert list(sqlite3.connect(a).execute(q)) == list(sqlite3.connect(b).execute(q))
    q2 = "SELECT * FROM ground_truth ORDER BY incident_id, event_id"
    assert list(sqlite3.connect(a).execute(q2)) == list(sqlite3.connect(b).execute(q2))


def test_ground_truth_is_not_reachable_from_the_events_table(db: Path) -> None:
    """Belt and braces alongside the AST test: no join path exists inside `events` itself."""
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(events)")}
    assert not any("incident" in c or "truth" in c or "near" in c for c in cols)


# --------------------------------------------------------------------------------------
# Linkage — the check that would have caught the enrichment defect automatically
# --------------------------------------------------------------------------------------

def test_every_incident_is_structurally_connected(db: Path) -> None:
    """An incident whose streams cannot be joined is undiscoverable, and scoring it is dishonest.

    This failed for real: enrichment rewrote prose, and INC-1's only engineering-to-support link
    was the phrase "Atlas" inside a ticket body. Once rewritten, the engineering hop became
    unreachable by any method. The fix was a structured `component` field; this test is the guard.
    """
    from attention_cascade.report import linkage

    for key, d in linkage(db).items():
        if d["is_near_miss"]:
            continue
        assert d["connected"], f"{key} has unreachable stream(s): {d['orphans']}"


def test_incident_joins_do_not_depend_on_prose(db: Path) -> None:
    """The two cross-entity joins must come from structured fields, not from payload['text']."""
    from attention_cascade.report import linkage

    data = linkage(db)
    assert "F-Atlas" in data["INC-1"]["edges"]["engineering <-> support"]
    inc4 = data["INC-4"]["edges"]["engineering <-> support"]
    assert "integrations" in inc4 and "v3.11" in inc4


def test_linkage_detects_an_orphaned_stream(tmp_path: Path) -> None:
    """Negative control: if the check cannot fail, it proves nothing."""
    from attention_cascade.report import linkage

    db = tmp_path / "broken.db"
    generate(db_path=db)
    conn = sqlite3.connect(db)
    # Strip the component field that links INC-1's engineering hop to its support hop, exactly
    # as enrichment did by accident.
    for eid, payload in conn.execute(
        "SELECT e.id, e.payload FROM events e JOIN ground_truth g ON g.event_id = e.id"
        " WHERE g.incident_id = 'INC-1' AND e.stream = 'support'").fetchall():
        p = json.loads(payload)
        p.pop("component", None)
        p["text"] = "redacted"
        conn.execute("UPDATE events SET payload = ? WHERE id = ?", (json.dumps(p), eid))
    conn.commit()
    conn.close()

    assert not linkage(db)["INC-1"]["connected"]
    assert "engineering" in linkage(db)["INC-1"]["orphans"]

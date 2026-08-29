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
                    seen.add(bstream); changed = True
                elif bstream in seen and a not in seen:
                    seen.add(a); changed = True

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
    for src in sorted(packet.iterdir()):
        if src.is_file():
            shutil.copy2(src, flat / src.name)
    return flat

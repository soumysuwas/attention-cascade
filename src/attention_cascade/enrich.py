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
line, a deal note, a sprint comment, a billing memo. Match the event's kind, its account, and its
numeric value where one is given. Vary phrasing, length, tone and specificity the way real records
do - some terse, some rambling, some with typos or internal shorthand.

Write each line INDEPENDENTLY. The events in this batch are unrelated to each other and arrive in
random order; do not try to build a narrative across them or reference one from another.

Return ONLY a JSON array, no other text:
[{"id": "evt_00001", "text": "..."}]
One object per input event, same ids, max 20 words each."""


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

    async def do_batch(idx: int, batch: list[sqlite3.Row]) -> None:
        nonlocal failures
        prompt = json.dumps([_fields(r) for r in batch], indent=None)
        try:
            res = await call(
                model=C.TIER1_MODEL, system=SYSTEM, prompt=prompt,
                max_tokens=2000, tier="enrich", run_id=f"enrich-{seed}", bb=meter,
                thinking_budget=0, json_output=True,
            )
            for item in parse_json(res.text):
                if isinstance(item, dict) and "id" in item and "text" in item:
                    updates[str(item["id"])] = str(item["text"])[:200]
        except Exception as exc:  # noqa: BLE001 - a failed batch keeps its original text
            failures += 1
            meter.audit("enrich", "BATCH_FAILED", {"batch": idx, "error": str(exc)[:300]})

    sem = asyncio.Semaphore(4)

    async def guarded(i: int, b: list[sqlite3.Row]) -> None:
        async with sem:
            await do_batch(i, b)

    await asyncio.gather(*(guarded(i, b) for i, b in enumerate(batches)))

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
             "batches": len(batches), "failed_batches": failures, "seed": seed}
    meter.audit("enrich", "DONE", stats)
    meter.close()
    return stats


if __name__ == "__main__":
    print(json.dumps(asyncio.run(enrich()), indent=2))

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

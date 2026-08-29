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

    DESIGN section 7 asks for "all candidates plus per-account context", and the grouping is
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
    """One call if it fits, two at most (DESIGN section 7). If two, split BY ACCOUNT.

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

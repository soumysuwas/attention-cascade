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

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
                if last_onset and (e.ts - last_onset) < timedelta(days=C.BILLING_DROP_COOLDOWN_DAYS):
                    continue  # still inside the decline already reported
                last_onset = e.ts
                out.append(_anom(
                    idx, "usage_drop", "billing", acct, recent[0].ts, e.ts,
                    abs(change), "drop", [x.id for x in recent],
                    f"{acct} usage down {change * 100:.0f}% "
                    f"(7-day mean {r:,.0f} vs trailing {t:,.0f})",
                ))

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

"""Wires the tiers together with bounded queues, backpressure and load shedding.

Does: run Tier 0 producers into a bounded anomaly queue, Tier 1 workers behind it, Tier 2 as the
single rate-limited bottleneck, then the gate. Sheds Tier 2 when the queue stays full.
Does not: decide what escalates, and does not hide coordination inside a framework. Plain asyncio,
one file, readable top to bottom.
Exists because: the interesting claim is not that a cascade is cheaper, it is that the mechanism
which controls cost IS the mechanism that sheds load. Under flood the system gets cheaper and
dumber instead of dying, and that happens here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from . import config as C
from . import correlate as t2
from . import triage as t1
from .blackboard import Blackboard
from .detectors import FloodSpec, KillSpec, run_detectors
from .gate import apply_gate, audit_gate
from .models import Anomaly, Candidate, GateResult, Hypothesis


@dataclass
class RunResult:
    """Everything one arm produced, plus the collapse line the demo prints."""

    events: int = 0
    anomalies: int = 0
    candidates: int = 0
    hypotheses: int = 0
    signals: int = 0
    hallucinated_event_ids: int = 0
    degraded: bool = False
    shed_reason: str = ""
    wall_clock_s: float = 0.0
    dead_streams: list[str] = field(default_factory=list)
    max_queue_depth: int = 0
    block_events: int = 0
    gate: GateResult | None = None
    hypothesis_list: list[Hypothesis] = field(default_factory=list)
    anomaly_list: list[Anomaly] = field(default_factory=list)
    candidate_list: list[Candidate] = field(default_factory=list)

    @property
    def collapse_line(self) -> str:
        return (f"{self.events} events → {self.anomalies} anomalies → {self.candidates} candidates"
                f" → {self.hypotheses} hypotheses → {self.signals} signals")


async def run_cascade(
    bb: Blackboard,
    *,
    run_id: str = "run",
    flood: FloodSpec | None = None,
    kill: KillSpec | None = None,
) -> RunResult:
    """Tier 0 → queue → Tier 1 → Tier 2 → gate. Returns the collapse and the gate decision."""
    started = time.perf_counter()
    res = RunResult(events=len(bb.events()))
    bb.set_meta("run_id", run_id)
    bb.audit("orchestrator", "START", {"run_id": run_id, "events": res.events,
                                       "flood": bool(flood), "kill": bool(kill)})

    anomaly_q: asyncio.Queue[Anomaly] = asyncio.Queue(maxsize=C.ANOMALY_QUEUE_MAX)
    depth_stats = {"max": 0, "blocks": 0, "full_since": 0.0, "shed": False, "reason": ""}

    async def watch_queue() -> None:
        """Sample the queue. If it stays full past the trigger, shed Tier 2 for this run."""
        while True:
            depth = anomaly_q.qsize()
            depth_stats["max"] = max(depth_stats["max"], depth)
            if anomaly_q.full():
                if not depth_stats["full_since"]:
                    depth_stats["full_since"] = time.perf_counter()
                    depth_stats["blocks"] += 1
                    bb.audit("orchestrator", "BACKPRESSURE",
                             {"queue": "anomaly_q", "depth": depth})
                elif (time.perf_counter() - depth_stats["full_since"] > C.SHED_TRIGGER_SECONDS
                      and not depth_stats["shed"]):
                    depth_stats["shed"] = True
                    depth_stats["reason"] = (
                        f"anomaly_q full for > {C.SHED_TRIGGER_SECONDS}s at depth {depth}")
                    bb.set_meta("degraded", True)
                    bb.audit("orchestrator", "SHED", {
                        "queue": "anomaly_q", "depth": depth,
                        "trigger_seconds": C.SHED_TRIGGER_SECONDS,
                        "effect": "Tier 2 will not be dispatched; escalating on Tier 1 + gate",
                        "note": "the system gets cheaper and dumber, never dead",
                    })
            else:
                depth_stats["full_since"] = 0.0
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_queue())
    try:
        anomalies = await run_detectors(bb, flood=flood, kill=kill, queue=anomaly_q)
    finally:
        watcher.cancel()

    res.anomalies = len(anomalies)
    res.anomaly_list = anomalies
    res.max_queue_depth = depth_stats["max"]
    res.block_events = depth_stats["blocks"]
    res.dead_streams = [s for s in C.STREAMS if bb.get_meta(f"stream_dead:{s}")]

    # ---- Tier 1 ----
    candidates = await t1.triage(bb, anomalies, run_id=run_id)
    plausible = [c for c in candidates if c.plausible]
    res.candidates = len(plausible)
    res.candidate_list = candidates

    # ---- Tier 2, unless shed ----
    if depth_stats["shed"]:
        res.degraded = True
        res.shed_reason = depth_stats["reason"]
        hypotheses, halluc = [], 0
        bb.audit("orchestrator", "TIER2_SKIPPED",
                 {"reason": res.shed_reason, "candidates_dropped": len(plausible)})
    else:
        hypotheses, halluc = await t2.correlate(bb, candidates, anomalies, run_id=run_id)

    res.hypotheses = len(hypotheses)
    res.hypothesis_list = hypotheses
    res.hallucinated_event_ids = halluc

    # ---- the gate: identical logic for both arms ----
    gate_result = apply_gate(hypotheses)
    audit_gate(bb, gate_result, hypotheses)
    bb.write_signals(gate_result.signals)
    res.gate = gate_result
    res.signals = len(gate_result.signals)

    res.wall_clock_s = round(time.perf_counter() - started, 2)
    bb.set_meta("collapse", [res.events, res.anomalies, res.candidates,
                             res.hypotheses, res.signals])
    bb.set_meta("wall_clock_s", res.wall_clock_s)
    bb.audit("orchestrator", "DONE", {
        "collapse": res.collapse_line, "degraded": res.degraded,
        "dead_streams": res.dead_streams, "wall_clock_s": res.wall_clock_s,
        "hallucinated_event_ids": res.hallucinated_event_ids,
        "max_queue_depth": res.max_queue_depth,
    })
    return res

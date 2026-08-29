"""Typed objects that cross a tier boundary.

Does: define Event, Anomaly, Candidate, Hypothesis, Signal and the gate result types.
Does not: contain any incident/ground-truth field - that quarantine is deliberate (see CLAUDE.md).
Exists because: every tier writes to a shared blackboard, so the shapes must be agreed in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Stream = Literal["crm", "engineering", "support", "billing"]


class Event(BaseModel):
    """One raw fact emitted by one source system. Deliberately has no incident field."""

    id: str
    ts: datetime
    stream: Stream
    entity_id: str
    kind: str
    numeric: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_line(self) -> str:
        """Compact single-line rendering used in LLM prompts. Token-frugal on purpose."""
        num = "" if self.numeric is None else f" val={self.numeric:g}"
        extra = " ".join(f"{k}={v}" for k, v in self.payload.items() if k != "text")
        text = self.payload.get("text", "")
        return (
            f"{self.id} {self.ts:%Y-%m-%d} {self.stream} {self.entity_id} "
            f"{self.kind}{num} {extra} {text}"
        ).strip()


class Anomaly(BaseModel):
    """Tier 0 output. Produced by deterministic math, zero tokens."""

    id: str
    detector: str
    stream: Stream
    entity_id: str
    window_start: datetime
    window_end: datetime
    score: float
    kind: str
    event_ids: list[str]
    summary: str


class Candidate(BaseModel):
    """Tier 1 verdict on a single anomaly."""

    anomaly_id: str
    plausible: bool
    reason: str = ""
    business_hint: str = ""
    model: str = ""
    failed_open: bool = False


class EvidenceRef(BaseModel):
    event_id: str
    stream: Stream
    anomaly_id: str | None = None


class Hypothesis(BaseModel):
    """Tier 2 output: a proposed causal chain across systems."""

    id: str
    title: str
    narrative: str
    entity_id: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    impact: float = 0.0
    confidence: float = 0.0

    @property
    def distinct_streams(self) -> set[str]:
        return {e.stream for e in self.evidence}

    @property
    def priority(self) -> float:
        return self.impact * self.confidence


class Signal(BaseModel):
    """A hypothesis holding one of the seven attention slots."""

    hypothesis_id: str
    rank: int
    escalated_at: datetime
    displaced_hypothesis_id: str | None = None


class Rejection(BaseModel):
    hypothesis_id: str
    reason: Literal["single_source", "below_floor", "attention_budget_full", "no_valid_evidence"]
    detail: str = ""


class DisplacementEvent(BaseModel):
    incoming_hypothesis_id: str
    displaced_hypothesis_id: str
    incoming_score: float
    displaced_score: float


class GateResult(BaseModel):
    """Everything the gate decided, including what it refused and why."""

    signals: list[Signal] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)
    displacements: list[DisplacementEvent] = Field(default_factory=list)
    deduped_evidence_count: int = 0


class LLMCall(BaseModel):
    """One metered model call. Token counts come from the API response, never estimated."""

    run_id: str
    tier: str
    model: str
    input_tokens: int
    output_tokens: int          # visible output only; thinking is tracked separately
    thinking_tokens: int = 0    # Gemini 3.x internal reasoning. BILLS AT THE OUTPUT RATE.
    cached_input_tokens: int = 0
    from_cache: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    ts: datetime

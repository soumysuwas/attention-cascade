"""Synthetic event generator with planted ground truth.

Does: emit ~1000 events across four enterprise streams over 60 days, containing 5 cross-system
causal incidents and 4 single-stream near-misses, plus the ground-truth mapping.
Does not: expose ground truth to the pipeline. Incident membership goes to its own table.
Exists because: without ground truth there is no baseline, and without a baseline the entire cost
claim is unfalsifiable. This file is the crown jewel - never cut it.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C
from .blackboard import Blackboard
from .models import Event

START = datetime(2026, 6, 1)
ACCOUNTS = [f"acct_{i:02d}" for i in range(1, C.N_ACCOUNTS + 1)]

# Accounts with heavy support traffic. INC-5 (silence) must target one of these,
# otherwise "absence of tickets" is not detectable.
CHATTY = {"acct_02", "acct_03", "acct_07", "acct_11"}

CRM_STAGES = ["discovery", "evaluation", "proposal", "negotiation", "closed_won", "closed_lost"]
ENG_TEAMS = ["platform", "ingest", "billing-svc", "integrations"]

# --------------------------------------------------------------------------------------
# Incident definitions. Each is a real cross-system causal chain spanning 2-3 streams.
# --------------------------------------------------------------------------------------
INCIDENTS = [
    {"id": "INC-1", "name": "feature_slip_cascade", "account": "acct_03", "day": 18,
     "streams": ["engineering", "support", "crm"],
     "story": "Atlas feature slips two sprints; the account waiting on it floods support; the deal stalls."},
    {"id": "INC-2", "name": "outage_billing", "account": "acct_07", "day": 31,
     "streams": ["support", "billing"],
     "story": "Sev-1 outage burst is followed by a collapse in metered usage."},
    {"id": "INC-3", "name": "pricing_migration", "account": "acct_05", "day": 12,
     "streams": ["billing", "support", "crm"],
     "story": "Forced plan migration triggers billing disputes and a forecast cut."},
    {"id": "INC-4", "name": "integration_regression", "account": "acct_09", "day": 40,
     "streams": ["engineering", "billing", "support"],
     "story": "A shipped regression silently breaks the API; usage drops before tickets appear."},
    {"id": "INC-5", "name": "champion_churn", "account": "acct_11", "day": 8,
     "streams": ["crm", "support", "billing"],
     "story": "The champion leaves; the account goes quiet; usage decays. Silence is the signal."},
]

# Single-stream lookalikes with no corroborating second system. These are what the
# sufficiency gate must reject. If one escalates, it is a false escalation.
NEAR_MISSES = [
    {"id": "NM-1", "account": "acct_02", "day": 45, "stream": "support"},
    {"id": "NM-2", "account": "acct_04", "day": 22, "stream": "billing"},
    {"id": "NM-3", "account": "acct_08", "day": 27, "stream": "engineering"},
    {"id": "NM-4", "account": "acct_06", "day": 50, "stream": "crm"},
]


class _Builder:
    """Accumulates events with temporary keys, then assigns stable ids in timestamp order."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self._rows: list[tuple[datetime, str, str, str, float | None, dict, str | None, bool]] = []

    def add(self, day: int, hour: int, stream: str, account: str, kind: str,
            numeric: float | None = None, incident: str | None = None,
            near_miss: bool = False, **payload) -> None:
        ts = START + timedelta(days=day, hours=hour, minutes=self.rng.randint(0, 59))
        self._rows.append((ts, stream, account, kind, numeric, payload, incident, near_miss))

    def finish(self) -> tuple[list[Event], list[tuple[str, str, str, bool]]]:
        self._rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
        events: list[Event] = []
        truth: list[tuple[str, str, str, bool]] = []
        for i, (ts, stream, account, kind, numeric, payload, incident, near_miss) in enumerate(self._rows, 1):
            eid = f"evt_{i:05d}"
            events.append(Event(id=eid, ts=ts, stream=stream, entity_id=account,
                                kind=kind, numeric=numeric, payload=payload))
            if incident:
                truth.append((incident, eid, stream, near_miss))
        return events, truth


# --------------------------------------------------------------------------------------
# Noise
# --------------------------------------------------------------------------------------

def _noise(b: _Builder, silence_windows: dict[str, tuple[int, int]]) -> None:
    rng = b.rng

    # CRM: stage changes and forecast updates, roughly 10 per account over the period.
    for acct in ACCOUNTS:
        stage_i = rng.randint(0, 2)
        amount = rng.choice([40_000, 75_000, 120_000, 250_000, 400_000])
        day = rng.randint(0, 5)
        while day < C.N_DAYS:
            if rng.random() < 0.6 and stage_i < len(CRM_STAGES) - 2:
                stage_i += 1
                b.add(day, 10, "crm", acct, "deal_stage_change", float(amount),
                      stage=CRM_STAGES[stage_i], text=f"moved to {CRM_STAGES[stage_i]}")
            else:
                delta = rng.uniform(-0.12, 0.15)
                amount = int(amount * (1 + delta))
                b.add(day, 11, "crm", acct, "forecast_update", float(amount),
                      delta_pct=round(delta, 3), text="forecast revised")
            day += rng.randint(4, 9)

    # Engineering: weekly-ish sprint reports per team plus scattered bug activity.
    for team in ENG_TEAMS:
        for day in range(3, C.N_DAYS, 7):
            planned = rng.randint(18, 30)
            delivered = planned - rng.randint(0, 4)
            b.add(day, 17, "engineering", "internal", "sprint_report", float(planned - delivered),
                  team=team, planned=planned, delivered=delivered, text=f"{team} sprint close")
    for _ in range(110):
        day = rng.randint(0, C.N_DAYS - 1)
        acct = rng.choice(ACCOUNTS + ["internal"] * 4)
        kind = rng.choice(["bug_opened", "bug_closed", "bug_reopened", "deploy"])
        b.add(day, rng.randint(9, 19), "engineering", acct, kind,
              team=rng.choice(ENG_TEAMS), text=kind.replace("_", " "))

    # Support: per-account daily ticket flow. Chatty accounts get a higher base rate.
    for acct in ACCOUNTS:
        lam = 1.2 if acct in CHATTY else 0.45
        window = silence_windows.get(acct)
        for day in range(C.N_DAYS):
            if window and window[0] <= day <= window[1]:
                continue  # deliberate silence, part of INC-5
            n = sum(1 for _ in range(3) if rng.random() < lam / 3)
            for _ in range(n):
                sev = rng.choices([1, 2, 3], weights=[0.05, 0.3, 0.65])[0]
                b.add(day, rng.randint(8, 20), "support", acct, "ticket_opened", float(sev),
                      severity=sev, text=rng.choice(
                          ["login issue", "report export slow", "how do I configure sso",
                           "dashboard blank", "invoice question", "api 500 intermittent"]))

    # Billing: usage snapshot every 3 days per account, plus monthly invoices.
    for acct in ACCOUNTS:
        base = rng.uniform(4_000, 40_000)
        for day in range(0, C.N_DAYS, 3):
            usage = base * rng.uniform(0.92, 1.08)
            b.add(day, 2, "billing", acct, "usage_snapshot", round(usage, 1),
                  metric="api_calls", text="3-day usage rollup")
        for day in (5, 35):
            b.add(day, 6, "billing", acct, "invoice_issued", round(base * 0.9, 2),
                  text="monthly invoice")


# --------------------------------------------------------------------------------------
# Planted incidents
# --------------------------------------------------------------------------------------

def _plant_incidents(b: _Builder) -> None:
    rng = b.rng

    # INC-1: engineering slip -> support burst -> deal stall
    a, d, i = "acct_03", 18, "INC-1"
    for k, day in enumerate((18, 20, 22)):
        b.add(day, 17, "engineering", "internal", "sprint_report", float(9 + k), incident=i,
              team="platform", planned=26, delivered=17 - k, feature="F-Atlas",
              text="Atlas milestone slipped again")
    for day in range(24, 31):
        for _ in range(rng.randint(1, 2)):
            b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
                  severity=2, text="when is Atlas shipping, blocked on rollout")
    b.add(33, 10, "crm", a, "deal_stage_change", 250_000.0, incident=i,
          stage="evaluation", regressed_from="negotiation", text="deal moved back to evaluation")
    b.add(35, 11, "crm", a, "forecast_update", 150_000.0, incident=i,
          delta_pct=-0.40, text="forecast cut, customer waiting on Atlas")

    # INC-2: sev-1 burst -> usage collapse
    a, i = "acct_07", "INC-2"
    for day in (31, 31, 32, 33, 33):
        b.add(day, rng.randint(1, 23), "support", a, "ticket_opened", 1.0, incident=i,
              severity=1, text="platform unreachable, total outage")
    for day, mult in ((34, 0.42), (37, 0.38), (40, 0.45)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(26_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")

    # INC-3: plan migration -> billing disputes -> forecast cut
    a, i = "acct_05", "INC-3"
    b.add(12, 6, "billing", a, "plan_change", 0.0, incident=i,
          from_plan="legacy_flat", to_plan="usage_metered", text="forced migration to metered plan")
    for day in (14, 15, 17, 18, 19, 20):
        b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
              severity=2, category="billing_dispute", text="bill tripled after plan change")
    b.add(26, 11, "crm", a, "forecast_update", 66_000.0, incident=i,
          delta_pct=-0.45, text="renewal at risk over pricing")

    # INC-4: regression ships -> usage drops -> tickets follow
    a, i = "acct_09", "INC-4"
    b.add(40, 15, "engineering", "internal", "deploy", 0.0, incident=i,
          team="integrations", release="v3.11", text="integrations v3.11 shipped")
    for day in (41, 41, 42, 43, 44):
        b.add(day, rng.randint(9, 19), "engineering", "internal", "bug_reopened", 0.0, incident=i,
              team="integrations", text="webhook signature regression reopened")
    for day, mult in ((42, 0.51), (45, 0.47), (48, 0.5)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(18_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")
    for day in (43, 45, 46, 47):
        b.add(day, rng.randint(9, 18), "support", a, "ticket_opened", 2.0, incident=i,
              severity=2, text="webhooks failing signature validation since last week")

    # INC-5: champion departs -> silence -> usage decay. The silence is created by the
    # silence_window passed into _noise; here we plant the bookends.
    a, i = "acct_11", "INC-5"
    b.add(8, 10, "crm", a, "contact_departed", 0.0, incident=i,
          contact_role="champion", text="primary champion left the company")
    for day, mult in ((20, 0.78), (23, 0.7), (26, 0.62), (29, 0.58)):
        b.add(day, 2, "billing", a, "usage_snapshot", round(31_000 * mult, 1), incident=i,
              metric="api_calls", text="3-day usage rollup")


def _plant_near_misses(b: _Builder) -> None:
    """Single-stream spikes that look like the first hop of an incident but never corroborate."""
    rng = b.rng
    for nm in NEAR_MISSES:
        a, day, i = nm["account"], nm["day"], nm["id"]
        if nm["stream"] == "support":
            for d in range(day, day + 4):
                for _ in range(rng.randint(2, 3)):
                    b.add(d, rng.randint(9, 18), "support", a, "ticket_opened", 2.0,
                          incident=i, near_miss=True, severity=2,
                          text="bulk import questions during onboarding wave")
        elif nm["stream"] == "billing":
            for d, mult in ((day, 0.55), (day + 3, 0.52), (day + 6, 0.6)):
                b.add(d, 2, "billing", a, "usage_snapshot", round(12_000 * mult, 1),
                      incident=i, near_miss=True, metric="api_calls",
                      text="3-day usage rollup")
        elif nm["stream"] == "engineering":
            for k, d in enumerate((day, day + 2)):
                b.add(d, 17, "engineering", "internal", "sprint_report", float(10 + k),
                      incident=i, near_miss=True, team="ingest", planned=24, delivered=14 - k,
                      text="ingest sprint slipped")
        elif nm["stream"] == "crm":
            b.add(day, 11, "crm", a, "forecast_update", 48_000.0,
                  incident=i, near_miss=True, delta_pct=-0.38,
                  text="forecast cut after budget freeze")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def generate(db_path: Path | None = None, seed: int = C.SEED) -> dict:
    """Build the dataset. Deterministic: same seed produces byte-identical event ids."""
    db_path = Path(db_path or C.EVENTS_DB)
    if db_path.exists():
        db_path.unlink()

    b = _Builder(seed)
    silence = {"acct_11": (12, 30)}  # INC-5: the absence IS the evidence
    _noise(b, silence)
    _plant_incidents(b)
    _plant_near_misses(b)
    events, truth = b.finish()

    bb = Blackboard(db_path, run_id="generate", arm="dataset")
    bb.insert_events(events)
    bb.insert_ground_truth(truth)
    bb.audit("generator", "DATASET_BUILT",
             {"events": len(events), "incidents": len(INCIDENTS), "near_misses": len(NEAR_MISSES),
              "seed": seed})
    bb.close()

    manifest = {
        "seed": seed,
        "start": START.isoformat(),
        "days": C.N_DAYS,
        "accounts": ACCOUNTS,
        "event_count": len(events),
        "incidents": INCIDENTS,
        "near_misses": NEAR_MISSES,
        "silence_windows": silence,
        "recall_definition": (
            "An incident is FOUND if some escalated signal's deduplicated evidence contains "
            "events tagged with that incident_id from >= 2 distinct streams."
        ),
    }
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(C.SEED_MANIFEST).write_text(json.dumps(manifest, indent=2))

    by_stream: dict[str, int] = {}
    for e in events:
        by_stream[e.stream] = by_stream.get(e.stream, 0) + 1
    return {"event_count": len(events), "by_stream": by_stream,
            "ground_truth_rows": len(truth), "db": str(db_path)}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))

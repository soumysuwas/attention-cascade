# DECISIONS.md

Architectural decisions and their rejected alternatives. This file is the raw material for the
judging answer to "what was the hardest technical decision you made and how did you solve it."

Every entry must name a **cost**. A decision with no cost is a preference.

---

### D-1: Split model judgement from escalation authority
- **Context:** the simplest design lets one strong model read everything and decide what matters.
- **Chose:** models generate hypotheses; deterministic code in `gate.py` decides what escalates.
- **Rejected:** LLM-decided escalation, and its softer variant "LLM proposes a priority score we
  threshold on."
- **Because:** cost scales linearly with event volume, which kills it in production; and a model
  asked "is this important?" says yes far too often, which makes an attention budget meaningless.
- **Cost of this choice:** three failure surfaces instead of one, plus a real risk that Tier 0 or
  Tier 1 discards a genuine incident before the strong model ever sees it. Mitigated by planting
  ground-truth incidents so the recall loss is measured rather than assumed.

### D-2: Synthetic data with planted incidents rather than a real dataset
- **Context:** the cost claim needs a baseline, and a baseline needs ground truth.
- **Chose:** generate the streams, plant 5 cross-system incidents and 4 near-misses.
- **Rejected:** a public log dataset; scraped real tickets.
- **Because:** without knowing which events belong to which incident, recall is unmeasurable and
  the entire cost claim becomes unfalsifiable.
- **Cost of this choice:** a judge can reasonably say "you built the test you pass." Answer that
  head-on: the near-misses are designed to be tempting, and the recall waterfall shows exactly
  where the cascade loses incidents.

### D-3: Shared blackboard over direct agent-to-agent messaging
- **Context:** four detectors, two model tiers, and a gate all need to see each other's output.
- **Chose:** one typed, append-mostly SQLite store; every write timestamped and attributed.
- **Rejected:** direct calls between components; an in-memory pipeline of function calls.
- **Because:** components stay decoupled, a dead detector degrades the system instead of breaking
  it, and every escalation has a complete audit trail.
- **Cost of this choice:** concurrent writes must be serialized explicitly and evidence must be
  deduplicated by source, or the same fact arriving twice double-counts toward sufficiency.

### D-4: Two-source sufficiency instead of a confidence threshold alone
- **Context:** something has to reject the near-misses.
- **Chose:** require evidence from ≥2 distinct source systems, then a confidence floor.
- **Rejected:** confidence threshold alone; a learned classifier.
- **Because:** model confidence is not calibrated and drifts with prompt wording. "Two independent
  systems agree" is a structural property of the evidence, not an opinion about it.
- **Cost of this choice:** genuine single-system emergencies are structurally invisible to this
  design. That is a real limitation and should be stated out loud, not hidden.

<!-- Agent: append D-5 onward below this line -->

# SETUP.md — arrange the folder, then start the agent

Everything here is done by you, in order, before the agent runs. Budget 20 minutes.

---

## 1. Where the repo goes

Unzip so the repo root sits inside your existing folder:

```
/Users/suwas/Desktop/VS Code/Signal Labs/SignalLabs AI HackDay/
└── attention-cascade/          ← THIS is the repo root. Open Claude Code HERE, not one level up.
```

Open a terminal at the repo root and stay there:

```bash
cd "/Users/suwas/Desktop/VS Code/Signal Labs/SignalLabs AI HackDay/attention-cascade"
```

## 2. What the folder should look like

If your unzip matches this, you are correctly arranged. Nothing to move by hand.

```
attention-cascade/
│
├── CLAUDE.md                  # agent constitution — read every session
├── SPEC.md                    # full technical spec, contracts, prompts
├── CHECKPOINTS.md             # the 5 stops
├── AGENT_INSTRUCTIONS.md      # self-review loop
├── REVIEW_PROTOCOL.md         # what goes in a review packet
├── START_PROMPT.md            # the prompt you paste (you read this, agent doesn't need to)
├── HUMAN_TASKS.md             # your list (agent doesn't need this either)
├── SETUP.md                   # this file
│
├── progress.md                # agent writes, every iteration
├── SCORECARD.md               # agent writes, every checkpoint
├── DECISIONS.md               # agent appends, seeded with D-1..D-4
├── CHECKPOINT_REPORT.md       # agent overwrites, every checkpoint
├── FILE_TOUR.md               # agent completes at CP5
│
├── pyproject.toml             # single uv env, `ac` entry point
├── .env.example  →  copy to .env
├── .gitignore
├── .claude/settings.json      # includeCoAuthoredBy: false
│
├── src/attention_cascade/
│   ├── config.py              ✅ written — pricing, thresholds, model ids
│   ├── models.py              ✅ written — pydantic types
│   ├── blackboard.py          ✅ written — SQLite shared store + audit
│   ├── generator.py           ✅ written — 1090 events, 5 incidents, 4 near-misses
│   ├── llm.py                 ✅ written — metered cached Vertex client
│   └── (groundtruth, detectors, triage, correlate, gate,
│         orchestrator, baseline, metering, report, chaos, cli)   ← agent builds these
│
├── tests/
│   └── test_no_groundtruth_leak.py   ✅ written — build-stopping quarantine test
│
├── data/                      # generated dataset (gitignored)
├── runs/cache/                # LLM response cache — COMMITTED, offline demo insurance
├── review/checkpoint-1..5/    # review packets you upload to chat
└── docs/                      # architecture + demo script, agent writes at CP5
```

**One uv environment at the repo root.** Not one per module — this is a single coherent package
and splitting it would just add import pain for no benefit.

---

## 3. Environment (5 min)

```bash
uv sync --extra dev
uv run python -c "from google import genai; import pydantic, typer; print('ok')"
```

## 4. Vertex auth (5 min) — only you can do this

```bash
gcloud auth application-default login
gcloud config set project <your-project-id>

cp .env.example .env
# edit .env: set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION
```

Then the smoke test that decides whether the whole experiment is viable:

```bash
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from google import genai
from google.genai import types
c = genai.Client(vertexai=True, project=os.environ['GOOGLE_CLOUD_PROJECT'],
                 location=os.environ.get('GOOGLE_CLOUD_LOCATION','us-central1'))
r = c.models.generate_content(
    model='gemini-3.1-flash-lite', contents='reply with the single word ok',
    config=types.GenerateContentConfig(max_output_tokens=20, temperature=0))
print(repr(r.text)); print(r.usage_metadata)
"
```

**You need to see token counts printed.** If `usage_metadata` is empty or the call errors, stop
and fix it — every number in your judging table depends on this working.

Then confirm the model ids exist in your region:

```bash
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from google import genai
c = genai.Client(vertexai=True, project=os.environ['GOOGLE_CLOUD_PROJECT'],
                 location=os.environ.get('GOOGLE_CLOUD_LOCATION','us-central1'))
for m in c.models.list(): print(m.name)
" | grep -i gemini
```

If `gemini-3.1-pro`, `gemini-3.1-flash-lite`, or `gemini-3.7-flash` are missing or named
differently, **edit `config.py` yourself now** and tell the agent in your first message. Do not
let it guess and silently substitute a model.

## 5. Verify the data (5 min) — the judgement call only you can make

```bash
uv run python -m attention_cascade.generator     # skeleton: structure, timing, ground truth
uv run python -m attention_cascade.enrich        # blind text pass, ~28 flash-lite calls, pennies
uv run pytest tests/ -q
```

Then look at the data with your own eyes:

```bash
uv run python -c "
import sqlite3; c = sqlite3.connect('data/events.db')
for i in ('INC-1','NM-1'):
    print('===', i)
    for r in c.execute('''SELECT e.ts,e.stream,e.entity_id,e.kind,e.payload FROM events e
        JOIN ground_truth g ON g.event_id=e.id WHERE g.incident_id=? ORDER BY e.ts''',(i,)):
        print('  ', r[0][:10], r[1], r[2], r[3], r[4][:70])
"
```

Ask yourself two questions the agent cannot answer:
- Could a smart human find INC-1 by reading raw events? If no, the dataset is unfair to the
  baseline and a judge will say so.
- Is NM-1 genuinely tempting? If it obviously isn't an incident, your gate gets credit it didn't
  earn.
- **After enrichment: can you tell an incident ticket from a noise ticket by its wording alone?**
  If you can, enrichment didn't do its job and the experiment has a confound. Re-run it.

Adjust `generator.py` and regenerate if either answer is wrong. This is worth the 5 minutes.

## 6. Git, before the agent touches anything

```bash
git init
git config user.name "Soumy Suwas"
git config user.email "<your email>"
git add -A
git commit -m "scaffold: spec, checkpoints, generator, metered vertex client"
gh repo create attention-cascade --private --source=. --push
```

Committing first gives you a clean rollback point and makes any unwanted commit trailer obvious
by contrast.

---

## 7. Start the agent

```bash
cd "/Users/suwas/Desktop/VS Code/Signal Labs/SignalLabs AI HackDay/attention-cascade"
claude
```

Paste the block from `START_PROMPT.md` as your first and only message. Then leave it alone until
it stops at Checkpoint 1.

If it asks you anything before then, reply with exactly:
`Follow AGENT_INSTRUCTIONS.md. No questions outside checkpoints. Continue.`

---

## 8. At each checkpoint

The agent stops and prints a path like `review/checkpoint-1/`. You:

1. Open that folder
2. Upload its contents to the chat (or zip it: `zip -r cp1.zip review/checkpoint-1`)
3. Get the review back, paste the resume block into the agent

`REVIEW_PROTOCOL.md` lists exactly what each packet contains, so you never have to guess whether
you sent enough.

---

## Quick command reference

| What | Command |
|---|---|
| Preflight | `uv run ac verify` |
| Confirm model ids | `uv run ac verify --list-models` |
| Build the dataset | `uv run ac generate` |
| One cascade run | `uv run ac run --arm cascade` |
| Enrich the text (once) | `uv run ac generate --enrich` |
| **The demo** | `uv run ac demo` |
| Full n=5 measurement | `uv run ac demo --replicate` |
| Demo with no wifi | `AC_LLM_MODE=replay uv run ac demo` |
| Kill a detector | `uv run ac chaos kill --detector support` |
| Flood a stream | `uv run ac chaos flood --stream support --factor 50` |
| Build a review packet | `uv run ac review --checkpoint 2` |

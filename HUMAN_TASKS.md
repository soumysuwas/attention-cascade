# HUMAN_TASKS.md — what you do, not the agent

The agent is good at code. It is bad at judgement calls about reality, and it cannot hold a
credential, a laptop, or a room. This is your list.

---

## Before you start the agent (15 minutes, do it now)

**1. Environment.** The agent can run these but you should confirm they work first, because a
broken toolchain costs you an hour of agent thrashing.
```bash
cd "/Users/suwas/Desktop/VS Code/Signal Labs/SignalLabs AI HackDay/attention-cascade"
uv sync --extra dev
uv run python -c "from google import genai; import pydantic, typer; print('ok')"
```

**2. Vertex auth and the smoke test.** Only you can do this. Full commands are in `SETUP.md` §4.
Short version: `gcloud auth application-default login`, set the project, copy `.env.example` to
`.env`, then run the smoke test. **You must see `usage_metadata` with token counts printed.** If
you don't, stop and fix it — every number in your deck depends on it.

**3. Confirm the model ids exist in your region** (`SETUP.md` §4, second snippet). If
`gemini-3.1-pro`, `gemini-3.1-flash-lite` or `gemini-3.7-flash` are absent or named differently
in your location, edit `config.py` yourself and tell the agent in your first message. Check your
Vertex quota for Pro too — the baseline sends every event in one call.

**4. Verify the generator, then look at the data with your own eyes.**
```bash
uv run python -m attention_cascade.generator
uv run pytest tests/ -q
```
This is the one thing the agent genuinely cannot judge: **are the planted incidents plausible, and
are the near-misses actually tempting?** Read one full incident chain and one near-miss. If a smart
human could not find INC-1 by reading the raw events, the dataset is unfair to the baseline and a
judge will say so. If a near-miss is obviously not an incident, the gate gets credit it did not
earn. Adjust and regenerate — this is worth 10 minutes.

**5. Git.**
```bash
git init
git config user.name "Soumy Suwas"
git config user.email "<your email>"
git add -A && git commit -m "scaffold: spec, checkpoints, generator, metered vertex client"
gh repo create attention-cascade --private --source=. --push
```
Do this before the agent starts, so you always have a clean point to roll back to.

**6. Spot-check commit attribution once, early.** `.claude/settings.json` sets
`includeCoAuthoredBy: false` and CLAUDE.md forbids it explicitly, but belt and braces: after the
agent's first few commits run `git log --format='%an %s%n%b' -5` and confirm there is no
Co-Authored-By trailer, no "Generated with Claude Code" line, and no robot emoji. If there is:
`git rebase -i` and amend, or `git filter-branch`/`git-filter-repo` if it is already several
commits deep. Catching it at commit three is trivial; catching it at commit forty is not.

---

## During the build

**Only you can do these:**

- **Approve or reject at the five checkpoints.** The agent builds `review/checkpoint-N/`. Zip it
  (`zip -r cp1.zip review/checkpoint-1`) and drop it in chat — that folder is the whole review
  packet, so you never have to work out what to send.
- **Kill a runaway.** If the agent has been going 25+ minutes without something that runs, stop it
  and force a simplification. It will not do this reliably on its own.
- **Judge whether the story is honest.** If recall drops or the cost saving is modest, you decide
  how to frame it. Frame it as a measurement, never hide it.
- **Watch the spend.** Check your API console once an hour.
- **Guard the cut order.** The agent will want to build the interesting thing. You want the table.

**Rule of thumb:** if you are typing more than one message to the agent between checkpoints, either
the spec was wrong or you are micromanaging. Fix the spec, do not chat.

---

## Before the demo (the last 45 minutes, non-negotiable)

- [ ] **Run the whole thing offline.** Turn wifi off. `AC_LLM_MODE=replay uv run ac demo`. If it
      fails, nothing else matters. The venue wifi will be bad. Note that ADC tokens expire — in
      replay mode nothing authenticates, which is exactly why replay mode exists.
- [ ] **Record a backup video.** Full 5-minute run, screen + audio, on your own machine. If the
      projector or the laptop dies you still have a demo.
- [ ] **Push to GitHub and check the README renders**, especially the mermaid diagram.
- [ ] **Read `git log` one last time.** Judges may open your repo. Nothing in the history should
      say an AI wrote it.
- [ ] **Rehearse out loud, twice, on a timer.** Not in your head. Out loud.
- [ ] **Open five random source files and explain each in 30 seconds.** If you cannot, that file
      is a liability — either understand it or delete it.
- [ ] **Charge the MacBook. Bring the charger. Check the display adapter.** Set terminal font to
      at least 18pt and use a light background if the room is bright.
- [ ] **Close Slack, mail, and every other window.** Have exactly one terminal and one browser tab.

---

## Things only you can answer, that the agent will get wrong

1. **Whether the near-misses are fair.** Agent has no taste for this.
2. **Whether the narrative is honest.** The agent will optimise for a good-looking table.
3. **Whether a judge will find a file defensible.** You are the one being asked.
4. **What to say when a number disappoints.** Prepare this sentence in advance:
   *"Recall dropped from 5 to 4 at Tier 1. Here is exactly where, here is the cost of fixing it,
   and here is why I'd take that trade in production."*
5. **Whether to lead with the thinking-token finding.** If Tier 2's internal reasoning turns out
   to be a large share of your output spend, that is a genuinely interesting result about Gemini
   3.x that most people building on Vertex have not measured. You decide whether it is a headline
   or a footnote.
6. **How much of the Signal Labs vocabulary to use.** Their words — blackboard, sufficiency,
   attention budget, institutional memory — signal you did the homework. Overusing them signals
   you read the homepage. Use each once, deliberately.

---

## The one thing that wins this

You can open any file and say what it does and why it is there. Everything in this repo is
hand-rolled plain Python for exactly that reason. Spend the last 15 minutes reading your own code,
not polishing it.

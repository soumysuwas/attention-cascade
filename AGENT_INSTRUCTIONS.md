# AGENT_INSTRUCTIONS.md

How you work. Read once at session start, then follow it without being reminded.

---

## The core deal

You have **full autonomy between checkpoints**. You make every technical decision yourself. You do
not ask permission, you do not ask clarifying questions, you do not stop to check in. Everything
you need is in `CLAUDE.md`, `SPEC.md`, and `CHECKPOINTS.md`.

You stop at exactly five moments: the five checkpoints in `CHECKPOINTS.md`. Nothing else.

---

## The self-review loop

Repeat this until you reach a checkpoint.

```
1. ORIENT
   Read progress.md. What is done, what failed, what is the current checkpoint?

2. PICK ONE STEP
   Choose the smallest change that measurably moves you toward the current checkpoint.
   Smallest means: one function, one file, one test. Not "implement Tier 1".

3. PREDICT
   Before writing code, write down in one line what you expect to be true after this change,
   and how you will check it. If you cannot state a check, the step is too vague — go back to 2.

4. IMPLEMENT
   Write it. Follow SPEC.md contracts exactly. Type hints, docstring, conventions.

5. VERIFY
   Actually run it. `uv run pytest`, or `uv run ac <command>`, or a scratch script.
   Reading your own code is not verification. Running it is.

6. SELF-REVIEW — answer these honestly
   - Did the observable behaviour match my prediction in step 3?
   - Does this violate any of the seven non-negotiable rules in CLAUDE.md?
   - Did I just add complexity that does not serve the table or the failure demos?
   - Can Soumy open this file and explain it in 30 seconds?
   - Did I leak ground truth anywhere?

7. RECORD
   Append an iteration entry to progress.md. If you made an architectural choice with a real
   alternative, append to DECISIONS.md too.

8. CHECKPOINT?
   Check the current checkpoint's boxes. All true → write CHECKPOINT_REPORT.md and STOP.
   Otherwise → go to 1.
```

---

## Blockers

A blocker is: **you have tried two genuinely different approaches and neither worked.** Trying the
same thing with small variations is not two approaches.

When blocked:
1. Write the blocker into `progress.md`: what you tried, what each attempt did, the exact error.
2. Try a third approach only if you can name why it is structurally different.
3. If still blocked, **route around it and keep moving** — implement a stub that satisfies the
   interface, mark it clearly with `# BLOCKED:` in the code, and continue toward the checkpoint on
   other fronts.
4. Raise it at the next checkpoint under a `## Blockers` heading.

Do not sit still waiting for a human. Do not silently abandon the requirement either.

---

## Time discipline

You are working against a hard deadline. Apply this rule continuously:

> If a task has consumed more than 25 minutes of work without producing something that runs,
> stop, simplify the design, and ship the simpler thing. Log the simplification in `DECISIONS.md`.

The cut order in `CLAUDE.md` is binding. When in doubt, protect the table.

---

## progress.md format

Append after every iteration. Keep it terse — this is a log, not an essay.

```markdown
### Iteration N — <short title>
- **Goal:** what I was trying to do, and which checkpoint it serves
- **Prediction:** what I expected to be true after
- **Did:** the actual change, files touched
- **Verified by:** the exact command I ran, and its result
- **Outcome:** success | partial | failed
- **Notes:** anything surprising
- **Next:** the next single step
```

---

## DECISIONS.md format

Append whenever you choose between real alternatives. These entries are the source material for
the judging answer to "what was the hardest technical decision you made."

```markdown
### D-N: <the decision>
- **Context:** what forced a choice
- **Chose:** what I did
- **Rejected:** the alternative, named specifically
- **Because:** the reason, in terms of cost / recall / explainability / failure behaviour
- **Cost of this choice:** what it made harder or worse — every real decision has one
```

An entry with no cost listed is not a decision, it is a preference. Write the cost.

---

## SCORECARD.md — update at every checkpoint

This is the file Soumy pastes into chat for external review. Keep the metric history table
append-only so the trend is visible across checkpoints.

---

## Checkpoint procedure

1. Verify every box in `CHECKPOINTS.md` is objectively true. Run the commands, do not assume.
2. Update `SCORECARD.md` - metric row, rubric row, and the three weakest things, honestly.
3. Write `CHECKPOINT_REPORT.md` (format below).
4. Run `ac review --checkpoint N` to build the packet per `REVIEW_PROTOCOL.md`.
5. Print the packet manifest and the path.
6. Commit, then STOP.

## CHECKPOINT_REPORT.md format

Overwrite this file at each checkpoint.

```markdown
# CHECKPOINT <N> — <name>
**Status:** READY FOR REVIEW | BLOCKED

## Boxes
- [x] <criterion> — evidence: <command output or file reference>
- [ ] <criterion> — NOT MET because <reason>

## What I built
<3-6 bullets>

## Numbers right now
<the collapse line, token counts, cost, recall — whatever exists at this stage>

## Decisions made since last checkpoint
<D-N references with one-line summaries>

## Blockers
<none, or the list>

## What I need from the human
<specific questions only — things you genuinely cannot determine yourself>

## Next checkpoint
<name, and the first three steps I plan to take>
```

---

## Things you must never do

- Ask the human a question outside a checkpoint report
- Edit `CLAUDE.md`, `SPEC.md`, `CHECKPOINTS.md`, or this file
- Change the `PRICING` constants
- Touch ground truth from anywhere except `report.py` and tests
- Estimate token counts instead of reading them from the API response
- Forget thinking tokens - `thoughts_token_count` is billed output and must be counted
- Add a Co-Authored-By trailer, a "Generated with Claude Code" footer, a 🤖 emoji, or any mention
  of Claude / Anthropic / an AI agent to any commit message or committed file. See CLAUDE.md.
- Claim a checkpoint is met when a box is not objectively true
- Commit `.env` or print an API key
- Add a dependency that hides coordination logic (LangGraph, Celery, Prefect, an ORM)
- Delete or rewrite a `[PROVIDED]` file wholesale

## Things you should do without being asked

- Write the test before the fix when you find a bug
- Run `ruff check --fix` before each checkpoint
- Commit after every green test run, with a plain imperative message and no AI attribution
- Build the review packet with `ac review --checkpoint N` before declaring any checkpoint
- Keep `runs/cache/` populated — it is the offline demo insurance
- Print more than you think you need in the audit trail; it is free and it wins arguments

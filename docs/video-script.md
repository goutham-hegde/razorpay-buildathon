# Five-minute explainer — script

Written to be **read aloud**, not performed. Short sentences, one idea each, no clause
pileups.

Spoken text only — headings, on-screen cues and this preamble excluded — it runs **808
words**. That is **5:03 at 160 words per minute**, which is the pace this
register wants; a deliberately slow read stretches it to 5:23. The
timestamps on each section below are derived from its own word count, not guessed, so they
stay honest if you re-cut it. Re-check after any edit:

```bash
python docs/wordcount.py
```

---

## Before recording: refresh every figure

No number below is typed by hand. Each one has a command that prints it. Run these on a
clean checkout, then update the slots marked `⟦…⟧` and delete this section.

```bash
.venv/Scripts/python -m reclaim.eval.replay      --batch B --arms all --fresh
.venv/Scripts/python -m reclaim.eval.metrics     --batch B   # the four-arm table
.venv/Scripts/python -m reclaim.core.guards      --batch B   # R1-R6
.venv/Scripts/python -m reclaim.eval.sensitivity --batch B   # ranking across 20 worlds
.venv/Scripts/python -m reclaim.eval.confusion   --batch B --provider groq --compare stub
```

The batch A figures shown in `⟦…⟧` are current as of the last run and are there so the
script is readable end to end. **Batch A is the tuning batch — replace every one of them
with the batch B figure before recording.** Saying a tuning number out loud as if it were
the reported one is the single worst thing this video could do.

---

## 0:00 — 0:37 · The trap

> **On screen:** a failed payment, then the same payment retried, then a duplicate charge.

A payment fails. Most of that money is not lost — it is stuck. Whether you get it back
depends on whether your next action matches the *reason* it failed.

Here is the case that makes this hard. Sometimes a payment fails and no clean answer comes
back. The customer may already have been debited. Retry it, and the retry **succeeds** — and
that success is a duplicate charge. You have taken the money twice, you owe a refund, and
you have a furious customer.

A system that retries everything three times finds that case. And charges it twice.

---

## 0:37 — 1:27 · What it does

> **On screen:** the nine causes, each with its one action.

`reclaim` sorts every failure into one of nine root causes, because each implies a different
action. An empty account needs a different **time**, not more attempts. An abandoned OTP
screen needs the *customer* back. A dead card needs a new card. A broken route needs a
different route, right now.

Nine causes, nine right answers. A retry loop gets one by accident.

Exactly one module calls a language model: it reads the issuer's messy free text and names
the cause. That is all. Retry timing is a policy, budgets are arithmetic, anything that moves
money is plain code behind a gate.

Because what a payments reviewer actually asks is what stops this doing something stupid at
3am. That answer has to be a constant in a file they can read. Not a prompt.

---

## 1:27 — 2:27 · Why you can believe the number

> **On screen:** the `core/` ↛ `synth/` boundary, then the control arm.

Now the part that took most of the work — making the measurement hard to fool.

The world is simulated, because this data is not public. Which creates a problem: if the
agent can read the simulator's parameters, it is rediscovering constants I wrote down
myself, and every recovery figure is meaningless.

So the boundary is enforced, not intended. The agent may never import the world, and a test
fails if it does. That is the most important test in the repository.

Second — the one most demos skip. A control arm that does **nothing at all**. Because
⟦**28.2%**⟧ of these payments come back on their own. The customer retries. The bank clears.
Report gross recovery and you are claiming credit for money that was already coming back.

So every number here is **lift over that control**. Never gross.

Third: I tuned on batch A. Everything you are about to see is batch B, which the policy has
never been tuned against.

---

## 2:27 — 3:40 · The result, and the thing it caught

> **On screen:** the four-arm table. Hold on the `halt %` and `net` columns.

Four arms, same batch. Control does nothing. Naive retries three times. Rules-only and the
agent run the *same policy engine*, differing only in what diagnosed the failure — so the
gap between those two is worth what the model is worth, and nothing else.

Now look at naive. Retry a subscription too often and the rail **halts the mandate**. Naive
destroys ⟦**61.8%**⟧ of the recurring book — nine months of forfeited revenue each. It
recovers about ⟦four lakh⟧ rupees of invoices and destroys ⟦sixty-two lakh⟧ of future
revenue. It does not even win on recovery rate, and it loses by ⟦**Rs 67 lakh**⟧ on net.

Invisible if you only report recovery rate. That is the whole reason there is a mandate-halt
column and a net column.

How confident is it? Re-run across twenty worlds, every constant moved by twenty percent at
once. The ordering holds in ⟦**20 of 20**⟧.

That table also reports my own arm's worst case, not just naive's — ⟦**7 of 20**⟧ worlds
where the halt threshold lands low enough to catch the policy too. A robustness check you
only publish when it flatters you is not a robustness check.

---

## 3:40 — 4:25 · Bounded, and provable

> **On screen:** the six invariants, then a case's audit trail in the console.

The track asks for compliant escalation, stopping rules, and an audit trail. Here those are
not features — they are assertions that can fail. Six of them, re-derived from the ledger
after every run.

The first is structural. "No payment charged twice" is a unique constraint in the database,
and the executor inserts the claim **before** it charges. Check-then-charge has a window
where a retry and a redelivered webhook both see nothing and both proceed. A unique
constraint has no window.

The ledger is append-only, enforced by triggers rather than convention. I know, because it
refused one of my own changes. And every decision is on it — what the agent believed, how
confident it was, and why it acted.

---

## 4:25 — 5:03 · What it does not do

> **On screen:** the repo, the README results table.

The limits, straight. The world is synthetic and its constants are anchors from public
sources, not measurements. I am not claiming they are right — I am claiming the ranking
survives moving all of them at once, and I show the range. Escalation to a human is charged
and credited with nothing, so net is a floor.

And the model runs **once per batch, ever**. Its output is committed. Clone this repo with
no API key of any kind, run four commands, and every number reproduces.

That was the goal. Not the highest recovery rate — a number you can check.

---

## Notes for recording

- The word to land on in the first thirty seconds is **"succeeds"** — the retry succeeding
  is what makes the double charge counter-intuitive. Slow down there.
- "Why you can believe the number" is the spine. If a take runs long, cut from "What it
  does" or from the sensitivity paragraph — never from there. `docs/wordcount.py` shows
  which section is over and by how much.
- Do not read the invariants as a list of six. Read the first one properly and let the
  screen carry the rest.
- Resist adding a feature tour. The strongest claim in this project is the control arm, and
  it is the one thing nobody else's demo will have.

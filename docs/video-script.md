# Five-minute explainer — script

Written to be **read aloud**, not performed. Short sentences, one idea each, no clause
pileups.

Spoken text only — headings, on-screen cues and this preamble excluded — it runs **842
words**. That is **5:15 at 160 words per minute**, which is the pace this register wants; a
deliberately slow read stretches it to 5:36. The timestamps on each section below are derived
from its own word count, not guessed, so they stay honest if you re-cut it. Re-check after
any edit:

```bash
python docs/wordcount.py --wpm 160
```

It is 42 words over a strict five minutes. If the take has to land under 5:00, the notes at
the bottom say which paragraph to drop; do not trim evenly, and do not cut the R1 paragraph
to make room — a project whose headline is honest measurement does not cut the part where
the measurement caught it.

---

## Before recording: refresh every figure

No number below is typed by hand. Each one has a command that prints it. Run these on a
clean checkout, and delete this section before publishing.

```bash
.venv/Scripts/python -m reclaim.eval.replay      --batch B --arms all --fresh
.venv/Scripts/python -m reclaim.eval.metrics     --batch B   # the four-arm table
.venv/Scripts/python -m reclaim.core.guards      --batch B   # R1-R6
.venv/Scripts/python -m reclaim.eval.sensitivity --batch B   # ranking across 20 worlds
.venv/Scripts/python -m reclaim.eval.confusion   --batch B --provider groq --compare stub
```

Every figure in the script below is now the **batch B** figure, taken from the runs logged
in `progress.md` under D8. Re-run the commands above before recording anyway: if any of them
disagrees with the script, the script is what is wrong. Saying a tuning number out loud as if
it were the reported one is the single worst thing this video could do.

---

## 0:00 — 0:33 · The trap

> **On screen:** a failed payment, then the same payment retried, then a duplicate charge.

A payment fails. Most of that money is not lost — it is stuck. Getting it back depends on
whether your next action matches the *reason* it failed.

Here is the case that makes this hard. Sometimes a payment fails and no clean answer comes
back — the customer may already have been debited. Retry it, and the retry **succeeds**. That
success is a duplicate charge: you owe a refund, and you have a furious customer.

A system that retries everything three times finds that case. And charges it twice.

---

## 0:33 — 1:19 · What it does

> **On screen:** the nine causes, each with its one action.

`reclaim` sorts every failure into one of nine root causes, because each implies a different
action. An empty account needs a different **time**, not more attempts. An abandoned OTP
screen needs the *customer* back. A broken route needs a different route, right now.

Nine causes, nine right answers. A retry loop gets one by accident.

Exactly one module calls a language model: it reads the issuer's messy free text and names
the cause. That is all. Retry timing is a policy, budgets are arithmetic, anything that moves
money is plain code behind a gate.

Because what a payments reviewer asks is what stops this doing something stupid at 3am. That
answer has to be a constant in a file they can read. Not a prompt.

---

## 1:19 — 2:19 · Why you can believe the number

> **On screen:** the `core/` ↛ `synth/` boundary, then the control arm.

Now the part that took most of the work — making the measurement hard to fool.

The world is simulated, because this data is not public. Which creates a problem: if the
agent can read the simulator's parameters, it is rediscovering constants I wrote down myself,
and every recovery figure is meaningless. So the boundary is enforced, not intended: the
agent may never import the world, and a test fails if it does. The most important test in the
repository.

Second — the one most demos skip. A control arm that does **nothing at all**, because
**23.8%** of these payments come back on their own. The customer retries. The bank clears.
Report gross recovery and you are claiming credit for money that was already coming back. So
every number here is **lift over that control**. Never gross.

Third: I tuned on batch A. Everything you are about to see is batch B, which the policy has
never been tuned against.

---

## 2:19 — 3:27 · The result, and the thing it caught

> **On screen:** the four-arm table. Hold on the `halt %` and `net` columns.

Four arms, same batch. Control does nothing; naive retries three times. Rules-only and the
agent run the *same policy engine*, differing only in what diagnosed the failure — so the gap
between them is exactly what the model is worth.

Now look at naive. Retry a subscription too often and the rail **halts the mandate**. Naive
destroys **66.1%** of the recurring book — nine months of forfeited revenue each. It recovers
eight lakh rupees of invoices and destroys sixty lakh of future revenue, finishing **Rs 62
lakh** behind the agent. All of it invisible if you report recovery rate alone. That is why
there is a halt column and a net column.

How confident is it? Twenty worlds, every constant moved twenty percent at once. The agent is
the top arm in **all twenty**; the full ordering holds in **sixteen**. I report sixteen. And
that run gives my own arm's worst case too — **7 of 20** worlds where the halt threshold
catches the policy as well. A robustness check you only publish when it flatters you is not a
robustness check.

---

## 3:27 — 4:37 · Bounded, and provable

> **On screen:** the six invariants, then `case_B00106` in the console — the case that broke
> R1, now showing a single `hold` decision and the reason it was held.

The track asks for compliant escalation, stopping rules, and an audit trail. Here those are
not features — they are six assertions that can fail, re-derived from the ledger after every
run.

The first is structural. "No payment charged twice" is a unique constraint, and the executor
inserts the claim **before** it charges. Check-then-charge leaves a window where a retry and
a redelivered webhook both proceed. A constraint has no window.

And on the held-out batch it broke anyway — not the constraint, but a *new* attempt, with
nothing to violate, against a payment that had already moved money. The model called that one
a technical decline, confidently. So there is one more rule now, and it reads no diagnosis at
all: if the failure came back **after** the debit instruction went out, we never charge
again. It costs four recoveries here, and the repo prices that rather than asserting it.

The ledger is append-only, enforced by triggers rather than convention. I know, because it
refused one of my own changes. Every decision is on it — what the agent believed, how
confident it was, and why.

---

## 4:37 — 5:15 · What it does not do

> **On screen:** the repo, the README results table.

The limits, straight. The world is synthetic and its constants are anchors from public
sources, not measurements. I am not claiming they are right — I am claiming the ranking
survives moving all of them at once, and I show the range. Escalation is charged and credited
with nothing, so net is a floor.

The model runs **once per batch, ever**, and its output is committed. Clone this repo with no
API key of any kind, run four commands, and every number reproduces.

That was the goal. Not the highest recovery rate — a number you can check.

---

## Notes for recording

- The word to land on in the first thirty seconds is **"succeeds"** — the retry succeeding
  is what makes the double charge counter-intuitive. Slow down there.
- "Why you can believe the number" is the spine. If a take runs long, the 42 words to lose
  are the sensitivity paragraph's second half — everything from "And that run gives my own
  arm's worst case" — which costs the least, because the README carries it and the on-screen
  table shows it. Cut that before anything else, and never cut from the spine.
  `docs/wordcount.py --wpm 160` shows which section is over and by how much.
- Do not read the invariants as a list of six. Read the first one properly and let the
  screen carry the rest.
- Resist adding a feature tour. The strongest claim in this project is the control arm, and
  it is the one thing nobody else's demo will have.

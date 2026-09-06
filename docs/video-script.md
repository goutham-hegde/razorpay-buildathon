# Five-minute explainer — script

Written to be **read aloud**, not performed. Short sentences, one idea each, no clause
pileups.

Each section carries the shot to be on screen and the URL that opens it directly, so no shot
has to be hunted for on camera. [`recording-runsheet.md`](recording-runsheet.md) has the
setup block, the fallbacks and the two things worth resisting.

Spoken text only — headings, cues and this preamble excluded — it runs **737 words**:
**4:54** at 150 words per minute, 4:36 at 160. The timestamps below are speaking time,
derived from each section's own word count rather than guessed, so they stay honest if you
re-cut it. They do not include the deliberate silences — the arm switch at 0:36, the pause on
the reason field at 4:22 — which is where the remaining seconds go. Re-check after any edit:

```bash
python docs/wordcount.py
```

---

## Before recording: refresh every figure

No number below is typed by hand. Each has a command that prints it. Run these on a clean
checkout; if any of them disagrees with the script, **the script is what is wrong**. Saying a
tuning number out loud as if it were the reported one is the worst thing this video could do.

```bash
.venv/Scripts/python -m pytest                                    # expect 311 passed
.venv/Scripts/python -m reclaim.eval.replay      --batch B --arms all --fresh
.venv/Scripts/python -m reclaim.eval.metrics     --batch B   # the four-arm table
.venv/Scripts/python -m reclaim.core.guards      --batch B   # expect 6/6, both asserted arms
.venv/Scripts/python -m uvicorn reclaim.api.main:app --port 8000
```

Record at 1920x1080 or wider and leave the console on **Large** — every link below carries
`zoom=present`, which scales type and padding together so a 1080p capture stays legible.

---

## 0:00 — 0:36 · The trap

> **Open:** <http://127.0.0.1:8000/?batch=B&run=naive&case=case_B00072&zoom=present>
>
> **On screen:** `case_B00072` under the **naive** arm — Rs 9,999 recurring mandate, failed
> at `payment_response`, and a trail row reading `DOUBLE CHARGE` in red. Do not scroll. Let
> the trail sit still. The word **"succeeds"** should land while that red row is on screen.
>
> **At the end of the section**, switch the arm picker to **agent** and say nothing about it.
> Same case, one `escalate` decision, closed `reconcile_hold`, no charge at all. The silent
> cut is worth more than a sentence.

A payment fails. Most of that money is not lost — it is stuck. Getting it back depends on
whether your next action matches the *reason* it failed.

Here is the case that makes this hard. Sometimes a payment fails and no clean answer comes
back — the customer may already have been debited. Retry it, and the retry **succeeds**. That
success is a duplicate charge: you owe a refund, and you have a furious customer.

A system that retries everything three times finds that case. And charges it twice.

---

## 0:36 — 1:14 · What it does

> **Show:** the editor, not the browser — `reclaim/core/policy.py`, the `WHAT EACH CAUSE
> BUYS` block near the top of the module docstring. Nine causes, nine actions, one screen,
> no scrolling.

`reclaim` sorts every failure into one of nine root causes, because each implies a different
action. An empty account needs a different **time**, not more attempts. An abandoned OTP
screen needs the *customer* back. A broken route needs another route, now.

Nine causes, nine right answers. A retry loop gets one of them by accident.

Exactly one module calls a language model: it reads the issuer's messy free text and names
the cause. That is all. Retry timing is a policy, budgets are arithmetic, and anything that
moves money is plain code behind a gate.

---

## 1:14 — 2:18 · Why you can believe the number

> **Show:** the terminal. Run this live — it takes about a second, and the test's name is the
> whole argument: `test_the_policy_reads_nothing_from_the_simulated_world`.
>
> ```bash
> .venv/Scripts/python -m pytest tests/test_seal.py -v
> ```
>
> **Then open:** <http://127.0.0.1:8000/?batch=B&run=control&view=book&zoom=present>
> — the control arm's stream, which reads *"no actions recorded — which for the control arm
> is the entire point."* Better than any diagram of one.

Now the part that took most of the work: making the measurement hard to fool.

The world is simulated, because this data is not public. Which creates a problem: if the
agent could read the simulator's parameters, it would be rediscovering constants I wrote down
myself, and every recovery figure would be meaningless. So the boundary is enforced, not
intended: the agent may never import the world, and a test fails if it does. The most
important test in the repository.

Second — the one most demos skip. A control arm that does **nothing at all**. Because
**23.8%** of these payments come back on their own. The customer retries. The bank clears.
Report gross recovery and you are claiming credit for money that was already coming back. So
every number here is **lift over that control**. Never gross.

Third: I tuned on batch A. Everything you are about to see is batch B, which the policy has
never been tuned against.

---

## 2:18 — 3:17 · The result, and the thing it caught

> **Open:** <http://127.0.0.1:8000/?batch=B&view=statement&zoom=present>
>
> **On screen:** the claim sentence, then the derivation beside it — the headline figure
> worked line by line to a double-ruled total. Let a viewer read down it once before you say
> anything. Then scroll to the four-arm table and point at `halt %` and `net Rs`: naive at
> **66.1%** and **−5,188,982**, against a gross figure that looks like a win.
>
> For the sensitivity paragraph there is nothing to show, and that is fine — stay on the
> table. Do not cut to a terminal running `sensitivity`; it takes minutes.

Four arms, same batch. Control does nothing. Naive retries three times. Rules-only and the
agent run the **same policy engine** and differ only in what diagnosed the failure — so the
gap between those two rows is exactly what the model is worth.

Now look at naive. Retry a subscription too often and the rail **halts the mandate**. Naive
destroys **66.1%** of the recurring book — nine months of forfeited revenue each. It recovers
eight lakh rupees of invoices and destroys sixty lakh of future revenue, finishing **Rs 62
lakh** behind the agent. Invisible, if you report recovery rate
alone — which is why there is a halt column and a net column.

How confident am I? Twenty worlds, every constant moved twenty percent at once. The agent is
the top arm in **all twenty**. The full ordering holds in **sixteen**, and sixteen is what I
report.

---

## 3:17 — 4:22 · Bounded, and provable

> **Open:** <http://127.0.0.1:8000/?batch=B&view=assurance&zoom=present>
> — four arms side by side, six checks each. Both asserted arms read `6 of 6 held`. Naive
> reads `5 of 6` with `R1 FAILED` in red and its twenty-six double-charged cases listed
> underneath. Hold there for a beat.
>
> **Then open — the payoff shot of the whole video:**
> <http://127.0.0.1:8000/?batch=B&run=agent&case=case_B00106&zoom=present>
> One decision, and a reason field that explains itself. Let it be legible and stop talking
> over it. A reviewer who reads that sentence off the screen has understood the project.

The track asks for compliant escalation, stopping rules, and an audit trail. Here those are
not features — they are six assertions that can fail, re-derived from the ledger after every
run.

The first is structural. "No payment charged twice" is a unique constraint, and the
executor inserts the claim **before** it charges. Check-then-charge leaves a window where a
retry and a redelivered webhook both get through. A constraint has no window.

And on the held-out batch it broke anyway — not the constraint, but a *new* attempt, against a
payment that had already moved money. The model called that one
a technical decline, confidently. So there is one more rule, and it reads no diagnosis at
all: if the failure came back **after** the debit instruction went out, we never charge again.
It costs four recoveries, and the repo prices that.

The ledger is append-only, enforced by triggers. Every decision is on it — what the agent
believed, how confident, and why.

---

## 4:22 — 4:54 · What it does not do

> **Show:** the GitHub repo page, then the README results table. Nothing to operate.

The limits, straight. The world is synthetic and its constants are anchors from public
sources, not measurements. I am not claiming they are right — I am claiming the ranking
survives moving all of them at once.

The model runs **once per batch, ever**, and its output is committed. Clone this repo with no
API key of any kind, run four commands, and every number reproduces.

That was the goal. Not the highest recovery rate — a number you can check.

---

## Notes for recording

- The word to land on in the first thirty seconds is **"succeeds"** — the retry succeeding is
  what makes the double charge counter-intuitive. Slow down there.
- **"Why you can believe the number" is the spine.** If that section is clean, a stumble
  elsewhere is survivable. If it is not, the take is not worth keeping however good the rest
  was. Never trim it to save time.
- If a take runs long, the cheapest thing to lose is the last sentence of the sensitivity
  paragraph — the on-screen table and the README both carry it. Cut there before anything
  else. `python docs/wordcount.py` shows which section is over and by how much.
- If a take runs *short*, the line to put back is the one cut from "What it does": *"Because
  the first thing a payments reviewer asks is what stops this doing something stupid at 3am.
  That answer has to be a constant in a file, not a prompt."* It is the best line in the
  section and it was cut only because the paragraph above already makes the claim.
- Do not read the invariants as a list of six. Read the first one properly and let the screen
  carry the rest.
- Resist a feature tour. The strongest claim in this project is the control arm, and it is
  the one thing nobody else's demo will have.

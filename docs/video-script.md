# Five-minute explainer — script

Written to be **read aloud**, not performed. Short sentences, one idea each, no clause
pileups.

Each section carries the shot to be on screen and the URL that opens it directly, so no shot
has to be hunted for on camera. [`recording-runsheet.md`](recording-runsheet.md) has the
setup block, the fallbacks and the two things worth resisting.

It opens and closes on a card (`docs/card-open.html`, `docs/card-close.html`) styled from
the console's own tokens, so the whole thing reads as one piece rather than as a screen
recording with talking either side of it.

Spoken text only — headings, cues and this preamble excluded — it runs **749 words**: exactly
**5:00** at 150 words per minute, and **4:41** at 160, which is the pace this register
actually wants. Take the second number: the difference is what pays for the deliberate
silences — the arm switch at 0:53, the derivation at 2:32, the reason field at 4:26, and two
seconds of held card at each end. The timestamps below are speaking time, derived from each
section's own word count rather than guessed, so they stay honest if you re-cut it. Re-check
after any edit:

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

## 0:00 — 0:17 · Title

> **Open:** `docs/card-open.html` from the filesystem, fullscreen. Nothing moves. Hold the
> frame for two seconds before speaking and two after, so the cut has somewhere to land.

I'm Goutham. This is `reclaim`, for Track 3 — it recovers failed payments. Most systems
retry everything three times and call it a day. That works, until it meets the one failure
where retrying is the worst thing you can possibly do.

---

## 0:17 — 0:53 · The trap

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

## 0:53 — 1:30 · What it does

> **Show:** the terminal, same window as the next section — `reclaim/core/policy.py` lines
> 32-51, printed with `sed -n '32,51p' reclaim/core/policy.py`. Twenty lines, no scrolling.
> Run it before the take. Not an editor: the block is a docstring, so every editor colours it
> as one flat string and charges you a sidebar for the privilege. Land the last line on
> `SEALED. Imports nothing from the simulated world.`

It sorts every failure into one of nine root causes, because each implies a different
action. An empty account needs a different **time**, not more attempts. An abandoned OTP
screen needs the *customer* back. A broken route needs another route, now.

Nine causes, nine right answers. A retry loop gets one by accident.

Exactly one module calls a language model: it reads the issuer's messy free text and names
the cause. That is all. Retry timing is a policy, budgets are arithmetic, and anything that
moves money is plain code behind a gate.

---

## 1:30 — 2:32 · Why you can believe the number

> **Show:** the terminal. Run this live — it takes about a second, and the six test names are
> the whole argument. `-vv`, not `-v`: `addopts = "-q"` in `pyproject.toml` cancels a single
> `-v` out and you get a row of dots with no names, which is the one shot that has to show
> names.
>
> ```bash
> .venv/Scripts/python -m pytest tests/test_seal.py -vv
> ```
>
> Same terminal as the last section, `clear`ed, near 100 columns. Have the command typed but
> not run when you start; press Enter on *"and a test fails if it does"* so the six lines
> land on the words. `test_core_cannot_read_ground_truth PASSED` is the one to let land.
>
> **Then open:** <http://127.0.0.1:8000/?batch=B&run=control&view=book&zoom=present>
> — the control arm's stream, which reads *"no actions recorded — which for the control arm
> is the entire point."* Better than any diagram of one.

Now the part that took most of the work.

The world is simulated, because this data is not public. Which creates a problem: if the
agent could read the simulator's parameters, it would be rediscovering constants I wrote down
myself, and every recovery figure would be meaningless. So the boundary is enforced, not
intended: the agent may never import the world, and a test fails if it does. The most
important test in the repository.

Second — the one most demos skip. A control arm that does **nothing at all**: no charges, no
messages, nothing. And it still recovers **23.8%**, because failed payments come back on
their own. The customer retries. The bank clears. Report gross recovery and you take credit
for money that was already coming back. So every number here is **lift over that control**.
Never gross.

Third: this is batch B. I tuned on A, and the policy has never seen this one.

---

## 2:32 — 3:24 · The result, and the thing it caught

> **Open:** <http://127.0.0.1:8000/?batch=B&view=statement&zoom=present>
>
> **Scroll ~2 notches first**, until the `reclaim` masthead is gone and `THE CLAIM` sits at
> the top. The tab bar is sticky so it stays, keeping `BATCH B` and `ARM agent` on screen —
> and only from there is the derivation's double-ruled total, `Net lift over doing nothing
> 5,74,947`, actually inside the frame. Unscrolled it is a few pixels below the fold, which
> would leave you reading down a sum whose answer is off screen.
>
> **On screen:** the claim sentence, the four figures, and the whole derivation. **Hold five
> seconds in silence** and let a viewer read down it. The number you name — **Rs 2,47,129** —
> is in the Notes column on the right, under "is what the model is worth".

Four arms, one batch. Rules-only and the agent run the **same policy engine** and differ
only in what diagnosed the failure. So that gap — **Rs 2,47,129** — is exactly what the model
is worth, and nothing else.

Now look at naive. Retry a subscription too often and the rail **halts the mandate**. Naive
destroys **66.1%** of the recurring book. It recovers eight lakh rupees of invoices and
forfeits sixty lakh of future revenue — finishing **Rs 62 lakh** behind the agent. Invisible,
if you report recovery rate alone. Which is why there is a halt column and a net column.

Twenty worlds, every constant moved twenty percent at once. The agent is the top arm in **all
twenty**. The full ordering holds in **sixteen**, and sixteen is what I report.

---

## 3:24 — 4:26 · Bounded, and provable

> **Open:** <http://127.0.0.1:8000/?batch=B&view=assurance&zoom=present>
> — four arms side by side, six checks each. Both asserted arms read `6 of 6 held`. Naive
> reads `5 of 6` with `R1 FAILED` in red and its twenty-six double-charged cases listed
> underneath. Hold there for a beat.
>
> **Then open — the payoff shot of the whole video:**
> <http://127.0.0.1:8000/?batch=B&run=agent&case=case_B00106&zoom=present>
> One decision, and a reason field that explains itself. Let it be legible and stop talking
> over it. A reviewer who reads that sentence off the screen has understood the project.

Compliant escalation, stopping rules, an audit trail. Not features — six assertions that can
fail, re-derived from the ledger after every run.

The first is structural. "No payment charged twice" is a unique constraint, and the executor
inserts the claim **before** it charges. Check-then-charge leaves a window where a retry and
a redelivered webhook both get through. A constraint has no window.

And on the held-out batch it broke anyway — not the constraint, but a *new* attempt, against
a payment that had already moved money. The model called that one a technical decline,
confidently. So there is one more rule, and it reads no diagnosis at all: if the failure came
back **after** the debit instruction went out, we never charge again. It costs four
recoveries, and the repo prices that.

The ledger is append-only. Every decision is on it — what the agent believed, how confident,
and why.

---

## 4:26 — 4:55 · What it does not do

> **Show:** the GitHub repo page, then the README results table. Nothing to operate.

The limits, straight. The world is synthetic and its constants are anchors from public
sources, not measurements. I am not claiming they are right — I am claiming the ranking
survives moving all of them at once.

The model runs **once per batch, ever**, and its output is committed. No key to get:
four commands, and every number reproduces.

That was the goal. Not the highest recovery rate — a number you can check.

---

## 4:55 — 5:00 · Close

> **Open:** `docs/card-close.html`, fullscreen. The three figures on it are the reported
> batch B numbers, and the repo URL is under them. Hold four seconds after the last word.

The repo is public and runs without a key. Thank you.

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

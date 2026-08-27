# reclaim — approach and solution

A bounded revenue-recovery agent for failed payments and failed recurring mandates.
Submission for Razorpay's buildathon, **Track 3 — AI Revenue Recovery**.

The track's bar is not "build a retry bot". It is:

> Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail.

Every clause in that sentence is an evaluation requirement, and this document is organised
around meeting them rather than around features.

---

## 1. The problem, stated precisely

When a digital payment fails in India, roughly a quarter to a third of the money is not
actually lost — it is *stuck*, and the difference between recovering it and losing it is
whether the next action matches the reason it failed.

The reasons are not interchangeable:

- An account was empty at 09:00 on the 3rd. Retrying at 09:30, 10:00 and 10:30 buys three
  guaranteed declines and three processing fees. Retrying at 14:00 on the 5th — payday —
  works. **The lever is *when*, and nothing else.**
- A customer walked away from an OTP screen. There is nobody there. A silent retry against
  an absent human is worth about five percent, and no amount of retrying changes that. The
  only lever is getting them back to the page.
- A card is closed. No retry will *ever* succeed. Every attempt is pure cost.
- Our own gateway broke while the bank was fine. A different route works *right now*.
- And the expensive one: we never got a clean answer, and the customer may already have been
  debited. Here the retry **succeeds** — and that success is a duplicate charge, a refund, an
  unwind cost, and a customer who no longer trusts the payment page.

A system that retries everything three times at a fixed interval is wrong in all five
directions at once. **The gap between "retry" and "the right action for this reason" is the
product.**

---

## 2. The thesis

Recovery is a **classification problem wearing a scheduling problem's clothes**.

`RootCause` has nine members, and each exists because it implies a materially different
action:

| Root cause | What it actually means | The action |
|---|---|---|
| `issuer_technical_decline` | Plumbing broke. Nothing is wrong. | Retry on a short backoff — 20m, 2h, 8h |
| `psp_routing_failure` | *Our* route broke, not the bank | Re-route to an untried PSP, now |
| `insufficient_funds` | Account empty; customer willing | Wait for payday, then present once |
| `limit_exceeded` | A cap was hit | Wait for the cap to roll over |
| `auth_abandoned` | Nobody completed the step | Reach out, then charge *while they are still there* |
| `instrument_invalid` | Card dead | No charge can work — ask for a new instrument |
| `mandate_revoked` | Permission withdrawn | No charge can work — ask for fresh authorisation |
| `risk_declined` | A fraud rule fired | **Stop.** Retrying hard-blocks the customer |
| `ambiguous_debited` | Money may already have moved | **Never charge.** Hold and reconcile |

The taxonomy is the value. The scheduler around it is arithmetic.

---

## 3. What makes the measurement trustworthy — "the seal"

This is the part most submissions will not have, and it is where most of the engineering
went.

The system is split in two, and the split is enforced by a test:

```
reclaim/synth/   the simulated world. Owns ground truth: which root cause a failure
                 really has, when money returns, when an outage ends, whether a
                 customer engages.

reclaim/core/    the agent. Sees events and chooses actions.

reclaim/eval/    the only package allowed to import both.
```

**`reclaim/core/` may never import from `reclaim/synth/`.** `tests/test_seal.py` enforces
this and it is the most important test in the repo.

Why this matters: if the policy could read the world's parameters, the evaluation would be
circular. The agent would be rediscovering constants we wrote down ourselves, and every
recovery figure would be meaningless. A reviewer will look for exactly this.

Five reinforcements sit on top of the import boundary:

1. **Separate files.** Ground truth goes to `truth.jsonl`; the agent reads `cases.jsonl`.
   `core.feed` carries an explicit allow-list and raises `SealViolation` on `truth.jsonl`,
   so the seam is guarded from both sides.
2. **Two batches.** Different seeds, shifted failure mix. **Tune on A, report on B.**
3. **A control arm that does nothing at all.** Some cases recover on their own —
   28.2% of batch A. Counting those as agent wins is the central dishonesty this project
   exists to avoid. Every reported figure is **lift over control**, not gross recovery.
4. **One frozen `Calibration` dataclass** holding every tunable number, and a `jitter()`
   that perturbs all of them at once. The sensitivity run is the answer to "you made these
   numbers up": yes, and here is the range over which the *ranking* survives.
5. **The agent's own beliefs are deliberately wrong.** The world thinks a halted mandate
   costs 9 months of revenue; the agent assumes 6. The world charges Rs 90 for a human
   escalation; the agent budgets Rs 120. If these matched, a reviewer would be right to ask
   whether the policy had been handed the answer.

The calibration constants are order-of-magnitude anchors from public sources (NPCI bank-wise
UPI decline data, published card-vs-UPI completion gaps) — **not measurements**, and never
presented as such. No conclusion is allowed to depend on their exact values.

---

## 4. Where the LLM is, and where it deliberately is not

Exactly one module calls a model: `core/diagnose.py`, which turns messy issuer free text
into the nine-way taxonomy.

Everything else is deterministic on purpose:

- retry timing is a policy, not a generation
- budgets are arithmetic
- anything that moves money is plain code behind a gate

Stating this out loud is a stronger signal than any feature. The one question a payments
reviewer will ask about a recovery agent is *what stops it doing something stupid at three
in the morning*, and the answer has to be a constant in a file they can read — not a prompt.

### Why diagnosis is genuinely hard

Three pairs of causes are near-indistinguishable in the generated error text, because they
are near-indistinguishable in reality:

```
ambiguous_debited    vs  issuer_technical_decline    both read as a timeout
mandate_revoked      vs  instrument_invalid          both read as "not valid"
psp_routing_failure  vs  issuer_technical_decline    both read as a gateway error
```

The first pair is the expensive one. The only honest signal separating a real ambiguous
debit from a plain timeout is that **a bank reference tends to come back when money actually
moved** — 78% against 16%. That is a *tendency*, not a rule. A regex cannot weigh a tendency.

This is measured, not asserted. A `StubDiagnoser` — deterministic keyword matching, no
network — ships alongside and runs as its own arm. On batch A it scores **0 out of 20** on
`ambiguous_debited`. Not "poorly": zero. A test pins this, so if some future edit teaches the
stub this class by another route, the claim gets rewritten rather than silently kept.

### The model runs once per batch, ever

Output is written to `data/<batch>/diagnoses.jsonl`, which is **committed**, and the eval
harness reads that file rather than calling a model.

This is not a cost optimisation. The README claims every number reproduces on a clean
checkout, and **someone cloning this repo has no API key of any kind**. A diagnosis step that
needs a live API is a reproducibility claim that is simply false. Caching it is what makes
the claim true.

---

## 5. The policy engine

`core/policy.py` is a pure function: hand it a `CaseView` — everything the agent knows at one
instant — and it returns the single next `Action`.

It is deliberately **not** a plan. A plan computed up front has to guess at the outcome of
its own first step, and the interesting cases are exactly the ones where step two depends on
step one: outreach that lands makes a charge worth attempting, and outreach that does not
makes the same charge worthless.

Being a pure function is also what makes it testable without a simulator.

### Order of evaluation

```
1. stopping rules      checked FIRST, before the cause is even looked at
2. the ambiguity gate  an independent refusal to charge over evidence of a debit
3. the cause table     the nine-way action mapping
```

Stopping rules go first on purpose. A stopping rule that only applies when the policy has not
thought of something more interesting to do is not a stopping rule.

### The ambiguity gate — defence in depth

The one place the policy second-guesses the diagnoser. It refuses to present a charge when
**both**: the diagnosis did not clear a confidence bar, **and** the failure carried a bank
reference.

The constant is written as a **bar to clear**, not a threshold to fall under:

```python
CHARGE_OVER_REFERENCE_CONFIDENCE = 0.75
```

...because that is the direction the asymmetry runs. A missed recovery costs the invoice. A
duplicate debit costs a refund, an unwind, and a customer who now distrusts the payment page.
A diagnoser that cannot get above this on a timeout is telling us it cannot tell — and "I
cannot tell" is not a mandate to take the money a second time.

**Measured effect on batch A:** the keyword-driven arm would have double-charged all 20
ambiguous cases. The gate caught 16. The remaining 4 carried no bank reference, and nothing
observable separates those from an ordinary timeout — only reading the description can. That
residue is precisely what the model is for.

---

## 6. Compliance and stopping rules

Every bound lives in one short file, `core/compliance.py`, so a reviewer can check them in
thirty seconds rather than trusting prose.

| Bound | Value | Basis |
|---|---|---|
| Contact window | 09:00–21:00 IST | TRAI TCCCPR; a recovery nudge carrying an incentive is promotional in substance whatever it is labelled |
| Contacts per customer | 3 per rolling 7 days | Stated risk appetite. **Rolling**, not per-calendar-week — a cap that resets on Monday permits 3 Sunday night and 3 Monday morning |
| Minimum gap | 20 hours | " |
| Contacts per case | 2 | One failed payment does not get to consume a customer's whole contact budget |
| Charge attempts per case | 4 | " |
| Charge attempts, recurring | 3 | Tighter because a halted mandate forfeits months, not one invoice |
| Case horizon | 14 days | Chasing a three-week-old failure annoys more than the money is worth |
| Economic floor | Rs 20 | Below this, recovery costs more than it returns |

The timezone is stated explicitly because *"no messages after 21:00" is meaningless until you
say whose 21:00*.

**Escalation** is spent in exactly one place: `ambiguous_debited` goes to a human
reconciliation queue. "We may have taken this customer's money and cannot tell" is not a
state a batch job gets to close on its own. That is what the escalation cost in the results
table buys.

---

## 7. Invariants — asserted, never claimed

`core/guards.py` re-derives six invariants from the ledger after every run:

```
R1  no payment charged more than once across all recovery attempts
R2  sum(recovered) <= sum(at_risk)                   no phantom recovery
R3  no customer contacted more than 3 times per 7d   frequency cap holds
R4  no contact outside permitted hours               quiet hours hold
R5  no action on a terminated or opted-out case
R6  no case left non-terminal after the batch drains
```

Three properties of how these are checked matter more than the list itself.

**R1 is structural.** `UNIQUE (run_id, payment_id, attempt_no)` on `charge_claims`, and the
executor **inserts first, then charges**. The tempting alternative — `SELECT` to check, then
`INSERT` — has a window between the two statements in which a retry, a redelivered webhook or
a second worker both see an empty result and both proceed. The window is small and the
failure mode is a duplicate charge on a real customer's card. A unique constraint is
evaluated atomically and has no window at all.

**They are re-derived, not read back.** R3 does not call the helper the policy used to decide
whether to send; it sorts raw rows and slides its own window. *A check that shares an
implementation with the thing it is checking will agree with it about a bug.*

**Not every arm is required to hold them.** `control` and `agent` are arms this project makes
claims about; a violation there is a defect. `naive` and `rules` are baselines that exist to
be beaten, and their violations *are the finding*. An invariant suite that no arm can ever
fail is not evidence of anything.

The ledger itself is append-only, and that is enforced rather than promised: every table
carries `BEFORE UPDATE` and `BEFORE DELETE` triggers that abort. *A ledger you can quietly
correct is not an audit trail, and "we only ever insert" is a code-review convention that
survives exactly until the first bug fix under deadline.*

---

## 8. The four arms

| Arm | What it does | Why it exists |
|---|---|---|
| `control` | Nothing at all | Establishes organic recovery. Without it, every other number silently includes money that was never at risk |
| `naive` | Retry immediately, 3×, fixed interval | The strawman almost every real recovery system starts as |
| `rules` | Same policy engine, **keyword** diagnoser | Isolates what the LLM contributes from what the policy contributes |
| `agent` | Same policy engine, **model** diagnoser | The product |

**`rules` and `agent` are the same engine.** They differ only in which `Diagnoser` produced
their input. Whatever gap the results table shows between them is attributable to diagnosis
quality and to nothing else, because there is nothing else different.

Keeping the rules arm is the difference between a measured claim and an assumed one. If the
delta turns out to be small, this document will say so.

---

## 9. Results so far

**Batch A (the tuning batch — reported figures will come from held-out batch B):**

```
arm        rec %   gross Rs  cost Rs  residual     net Rs   net lift  cost/Re  halt % double
--------------------------------------------------------------------------------------------
control    28.2%    515,531        0         0    515,531          -        -    0.0%      0
naive      52.5%    925,485    6,235 6,415,893 -5,496,643 -6,012,174    0.015   59.9%     20
rules      47.5%    925,515    9,150         0    916,365   +400,834    0.022    0.0%      2
```

Read the columns, not the headline:

- **28.2% of cases recover with no help at all.** Naive's 52.5% "recovery rate" is mostly
  not naive's doing. This is why lift is the headline and rate is not.
- **Naive has the best recovery rate in the table and by far the worst outcome.** It halts
  223 of 372 mandates — 59.9% of the recurring book — and the forfeited subscription
  months turn about Rs 4 lakh of recovered invoices into **−Rs 60 lakh** of destroyed
  revenue. This is the entire reason `residual` is a column and not a footnote.
- The policy arm recovers **five points less** than naive and is worth **Rs 64 lakh more**.
- It also makes **one tenth the double charges** (2 vs 20) on identical inputs.

**Diagnosis quality**, stratified sample of batch A weighted to the hard confusion pairs:

| | stub (keyword) | groq `gpt-oss-120b` |
|---|---|---|
| accuracy | 0.325 | **0.985** |
| `ambiguous_debited` recall | 0/14 | **14/14** |
| `ambiguous_debited` precision | — | **1.000** |

Batch B diagnosis is running now; the agent arm and the reported table follow from it.

---

## 10. The finding that mattered most

Two versions of this table exist, and the difference between them is the most useful thing
in this document.

### The version that was wrong, and why it looked fine

Until D5 the `halt %` column read **0.0% for every arm, in every run**. That is a plausible
number. It is also, in hindsight, the only number that column could ever have printed.

The world halts a mandate after `mandate_halt_after` consecutive failed presentations,
which is 4. It counted those failures from zero at the start of the run — but **a recurring
case only exists because a presentation already failed**, and the rail counted that one.
Starting the count at zero handed every arm one free failure that reality does not give it,
and no arm retrying three times could ever reach four. The downside metric was
structurally incapable of firing, and it sat in the results table looking like evidence
that no arm was destroying subscriptions.

Nothing failed. Every test passed, every invariant held, and the column was wrong.

What found it was not a test but a question: *what would have to be true for this column to
be non-zero, and is that reachable from here?* It was not. One line in `World.__init__` now
seeds the counter from the failure that opened the case.

Two things are worth saying about the fix. It changed no generated data — the batches and
the committed diagnoses are untouched, because the miscount was in the world's runtime
state and not in what it wrote down. And it flatters the arm I built, which is exactly the
kind of correction that deserves the most scrutiny: the defence is that the constant it
turns on is one the sensitivity run moves by ±20% along with everything else, and that the
earlier jittered runs had already shown the same tail in 7 of 20 worlds. The base world was
the outlier, not the jittered ones.

### The version that is right

Re-running the whole comparison across 20 worlds, with every calibration constant moved by
up to ±20% simultaneously:

```
arm              net lift Rs, median [min .. max]       doubles  worst halt %  worlds w/ halt
---------------------------------------------------------------------------------------------
naive         -5,988,517  [-9,140,939 .. 415,196]   20 [20..20]         73.4%           16/20
rules            393,769     [338,346 .. 413,784]      4 [2..4]          0.0%            0/20
```

The claimed ordering `naive < rules` held in **16 of 20**. It is worth being precise about
the four it did not, because they are not noise:

```
  seed 9001: naive=415,196  rules=384,515
  seed 9005: naive=403,352  rules=395,400
  seed 9016: naive=401,533  rules=389,804
  seed 9019: naive=389,809  rules=374,814
```

Those are the four worlds where the jitter pushed the halt threshold high enough that naive
never hit it. In those worlds naive retries three times, halts nothing, and edges the policy
arm out by two to eight percent. **In the other sixteen it loses by around Rs 60 lakh.**

That is the honest shape of the comparison, and reporting it as "the ranking held 20/20"
would have been the more flattering claim and the less true one:

- **Naive is above zero in 4 of 20 worlds.** The policy arm is above zero in **20 of 20**,
  in a band of [+Rs 3.38L, +Rs 4.14L].
- **Naive's median is meaningless** because its outcome is bimodal — it either clears the
  halt threshold in a given world or it does not, and the two branches are separated by
  roughly Rs 64 lakh.
- What is being bought is not a higher median. It is the removal of the left tail.

**Naive is not a slightly worse policy. It is a policy whose expected value is dominated by
a tail you cannot see from its recovery rate.** That is precisely what a mandate-halt metric
is for, and it is invisible without one.

### How this changed the design

The recurring charge budget has now moved twice, both times for the same reason and in the
same direction.

It was originally **3**. The first sensitivity run held the ranking but collapsed the
levels: a budget of 3 against a halt threshold that jitters into 3 destroys the mandate of
every recurring case worked hard. It dropped to **2**.

A budget of 2 survived only while the world was miscounting. With the opening failure
counted, 2 means three presentations against a threshold that jitters down to 3, and the
policy arm halted mandates in **7 of 20 worlds**, worst case 39.5% of the recurring book,
with net lift running to **−Rs 47 lakh**. The same cliff, one step further along.

It is now **1** — two presentations, one clear step under the lowest threshold the jitter
produces.

> The agent does not get to see the rail's threshold. The correct response to a cliff of
> unknown position is **headroom, not a better guess at where the cliff is**.

The price is real and belongs in the open: against a budget of 2 in the base world, a budget
of 1 gives up **4.2 points of recovery rate** — Rs 23,475 of gross recovery on batch A. What
it buys is a distribution with nothing below zero in it. It also halved the double charges,
2 against 4, which was not the goal: the attempt a tighter budget declines to make is
disproportionately the one the policy was least sure about.

### Stopping early is only half a policy

A short budget stops the harm; on its own it also stops pursuing the money. So when the
charge budget is spent, the policy **falls through to outreach** rather than closing the
case:

> A message cannot halt a mandate. A presentation can. Once presenting is off the table,
> the remaining ask is made of the customer rather than of the rail.

That outreach is bounded by exactly the same contact caps as every other message.

### Three bugs the sensitivity run found

None would have been caught by a test, and all three were invisible in a single run:

1. **The mandate-halt counter started from zero**, described above. The metric could not
   fire, and a column of `0.0%` read as a result rather than as a bug.
2. **`mandate_halt_rate` could only ever print 0% or 100%.** The denominator counted cases
   with non-zero residual loss — which is only ever true of cases that *already halted*, so
   numerator and denominator were the same set. It only became obviously wrong when a
   perturbed world produced a column of `100.0`s no policy could have earned. The ledger
   now records `is_recurring` explicitly.
3. **A stale ledger failed with `no such column` from an unrelated module.** `CREATE TABLE
   IF NOT EXISTS` does exactly what it says and leaves an old table alone; an append-only
   store cannot be migrated in place. The ledger now checks its own columns on open and says
   what to do.

---

## 11. Things this does not do, stated plainly

- **Escalation is charged but credited with nothing.** The world prices a human review and
  models no recovery from it, so the agent's net is a **lower bound**, not a best case.
- **Arms share parameters but not a paired RNG stream.** Every arm faces an identically
  seeded world, but the draw sequence diverges once arms take different numbers of actions.
  This is a common-parameter comparison, not a fully paired one; pairing each case to its own
  stream would tighten the lift estimate.
- **The world is synthetic.** Its constants are anchors from public data, not measurements.
  The defence is not that they are right — it is that the sensitivity run reports the range
  over which the *ranking of arms* survives having all of them moved at once.
- **The confusion pairs are engineered.** They are deliberately hard because they are hard in
  reality, and they are not tidied up to make the matrix look better.
- **Naive's median is unstable** because its outcome is bimodal — it either sits under the
  halt threshold in a given world or it does not. That is why the table reports the worst
  case and the share of worlds affected rather than leaning on a median.

---

## 12. Reproducing it

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

.venv/Scripts/python -m pytest                          # the whole suite
.venv/Scripts/python -m pytest tests/test_seal.py -v    # the import boundary

.venv/Scripts/python -m reclaim.eval.replay      --batch B --arms all
.venv/Scripts/python -m reclaim.eval.metrics     --batch B  # the results table
.venv/Scripts/python -m reclaim.core.guards      --batch B  # 6/6 for the asserted arms
.venv/Scripts/python -m reclaim.eval.sensitivity --batch B  # the ranking across 20 worlds
```

No API key is required for any of this. The model's output is committed.

Replaying a batch a second time needs `--fresh`, which discards that batch's ledger and
starts a new one. The ledger is append-only down to the triggers, so a re-run cannot
overwrite a recorded run — it either opens a new audit trail or it refuses. The table in
§9 is written into the README by `python -m reclaim.eval.report --batch B --write`, which
reads the ledger; no figure in this repo is typed in by hand.

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
1. stopping rules              checked FIRST, before the cause is even looked at
2. the ambiguity gate          an independent refusal to charge over evidence of a debit
3. the cause table             the nine-way action mapping
4. the post-authorization veto a last refusal that consults no diagnosis at all
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
observable separates those from an ordinary timeout — only reading the description can.

That residue was written down as "precisely what the model is for", and the held-out batch
disagreed. See below.

### The post-authorization veto — the gate that trusts nothing

The residue above is the honest limit of a gate built on confidence. A confidence bar
catches a diagnoser that admits it is unsure; it cannot catch a diagnoser that is sure and
wrong, and the engineered confusion pair produces exactly that. On the first held-out run,
`case_B00106` — a genuine ambiguous debit, described as *"timeout after debit instruction
sent to BOB"*, no bank reference — was diagnosed `issuer_technical_decline` at 0.70, cleared
the gate on the reference half, was charged again, and the retry succeeded. R1 broke.

So there is a fourth check, and it asks a question about the **failure** rather than about
the diagnosis:

```python
POST_AUTHORIZATION_STEPS = frozenset({ErrorStep.PAYMENT_RESPONSE})
```

`payment_response` is not "the payment failed". It is "we never got an answer to a request we
had already sent". Initiation, the authentication screen, the authorization decision — all of
those fail while the money is still demonstrably ours. This one does not, and no field in the
response says which side of the debit the silence fell on. So no charge goes out over one,
whatever cause was named and however confidently.

**It is blunt, and the bluntness is priced.** A routing failure can also die at
`payment_response`: our own switch lost the answer and the bank moved nothing. Those are
recoverable by re-routing, and the veto refuses them too. `eval/ablation.py` exists to put a
number on that, and the decision was taken on the tuning batch before batch B was re-run:

```
python -m reclaim.eval.ablation --batch A       # arm 'rules'
                       off            on         delta
recovered cases        322           313            -9
gross Rs         1,034,278     1,008,387       -25,891
net lift Rs        510,124       484,781       -25,344
double charges           4             0            -4
```

Nine recoveries and 5.0% of net lift on batch A, to take R1 from broken to held. On the
reported batch the same trade cost the `agent` arm 4 cases and Rs 11,521 of net lift.

**What this does not prove.** In this simulated world `payment_response` is a perfect tell —
every generated `ambiguous_debited` failure carries it, and no other cause is forced to — so
the clean catch rate is a fact about how `synth/generator.py` was written, not evidence the
rule would be perfect against a real acquirer, where that field is set by whichever
integration reported the failure. What transfers is the shape of the rule, not its hit rate:
a structural veto is the only kind of check that survives a confident misdiagnosis, and the
confidence gate is the only kind that can be tuned. Both are in the file, and they catch
different things.

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

**...and that covers less than it sounds like.** The constraint guarantees no attempt is
*executed* twice. It says nothing about a **new** attempt, with its own attempt number and
therefore no constraint to violate, presented against a payment whose money had already
moved. That is the semantic half, only the guard can see it, and it is the half the held-out
batch broke on the first run — see §5. Both sentences are now in the README, separately,
because letting "R1 is structural" carry the weight of both is the exact overclaim a payments
reviewer is looking for.

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

## 9. Results

**Batch B — the held-out batch.** This is the reported table. The policy was tuned on A and
this batch has a different seed and a shifted failure mix.

```
python -m reclaim.eval.replay --batch B --arms all --fresh
python -m reclaim.eval.metrics --batch B

arm        rec %   gross Rs  cost Rs  residual     net Rs   net lift  cost/Re  halt % double
--------------------------------------------------------------------------------------------
control    23.8%    420,657        0         0    420,657          -        -    0.0%      0
naive      44.3%    791,834    7,012 5,973,804 -5,188,982 -5,609,640    0.019   66.1%     26
rules      45.0%    757,730    9,256         0    748,474   +327,817    0.027    0.0%      0
agent      58.2%  1,006,151   10,547         0    995,604   +574,947    0.018    0.0%      0
```

Read the columns, not the headline:

- **23.8% of cases recover with no help at all.** Naive's 44.3% "recovery rate" is mostly not
  naive's doing. This is why lift is the headline and rate is not.
- **Naive destroys 244 of 369 mandates — 66.1% of the recurring book.** The forfeited
  subscription months turn Rs 7.9 lakh of recovered invoices into **−Rs 56 lakh** of net
  lift. This is the entire reason `residual` is a column and not a footnote.
- **The model is worth +Rs 2.47 lakh of net lift over the same policy engine fed by keyword
  matching** — `rules` and `agent` are the *same* code and differ only in the diagnoser. That
  delta is the answer to "what is the LLM actually doing here", and it is measured rather
  than assumed. It is also the single number this project would have had no way to state
  without keeping an arm almost nobody keeps.
- Both policy arms halt **zero** mandates and make **zero** double charges. The zero in the
  last column is not free — §5 prices it.

**Batch A, the tuning batch**, for working, three arms (A has no committed model diagnosis
cache, so there is no `agent` arm on it):

```
control    28.2%    515,531        0         0    515,531          -        -    0.0%      0
naive      51.7%    894,590    6,245 6,573,330 -5,684,985 -6,200,516    0.016   61.8%     20
rules      52.2%  1,008,387    8,075         0  1,000,312   +484,781    0.016    0.0%      0
```

**Diagnosis quality on batch B**, all 600 cases, scored against ground truth:

```
python -m reclaim.eval.confusion --batch B --provider groq
```

| | stub (keyword) | groq `gpt-oss-120b` |
|---|---|---|
| accuracy | 0.552 | **0.977** |
| macro-F1 | 0.452 | **0.961** |
| cost-weighted error | 0.970 | **0.038** |
| `ambiguous_debited` recall | 0/26 | **25/26** |
| `ambiguous_debited` precision | — | 0.714 |

The stub finds **none** of the 26 ambiguous debits — it has no rule that can, because the
text is written to read like a technical decline. The model finds 25 of 26, and the one it
misses is `case_B00106`, which is why §5 has a fourth gate in it. Its precision of 0.714 is
the model erring the *safe* way: 10 technical declines called ambiguous, which costs 10
recoveries and takes no money twice.

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
python -m reclaim.eval.sensitivity --batch A --arms control,naive,rules

arm              net lift Rs, median [min .. max]       doubles  worst halt %  worlds w/ halt
---------------------------------------------------------------------------------------------
naive         -6,197,653  [-8,888,408 .. 382,012]   20 [20..20]         71.8%           16/20
rules            464,972  [-4,006,338 .. 489,700]      0 [0..0]         35.5%            7/20
```

The claimed ordering `naive < rules` held in **20 of 20**.

(The `rules` row moved after this section was first written: the post-authorization veto in
§5 took its median net lift from 490,325 down to 464,972 and its double charges from 4 in
every world to 0 in every world. That is the same trade the ablation prices, restated across
20 perturbed worlds — which is the more convincing form of it, because the veto holding R1
at every point in the jitter range is a stronger claim than it holding on one seed.)

Two columns in that table are doing more work than the ordering line, and both belong in
any honest reading of it:

- **Naive is above zero in 4 of 20 worlds.** It halts mandates in 16 of 20, up to 71.8% of
  the recurring book, and its median outcome is a loss of over Rs 60 lakh.
- **The policy arm has a left tail of its own**, and it is reported rather than buried: in
  7 of 20 worlds it halts up to 35.8% of the book and net lift runs to −Rs 40 lakh. Those
  are the worlds where the jitter pushes the rail's halt threshold to its lowest value.
- **Naive's median is meaningless** because its outcome is bimodal — it either clears the
  halt threshold in a given world or it does not, and the two branches are separated by
  roughly Rs 64 lakh.

**Naive is not a slightly worse policy. It is a policy whose expected value is dominated by
a tail you cannot see from its recovery rate.** That is precisely what a mandate-halt metric
is for, and it is invisible without one.

### How this changed the design

The recurring charge budget is the one number this analysis kept forcing me to revisit, and
the shape of the choice only became clear once the halt counter was fixed.

The jitter moves the rail's halt threshold by exactly one unit, so it is always **3, 4 or
5**. And a recurring case arrives already one failed presentation deep. So the budget maps
straight onto total presentations, and the options are structural rather than a matter of
degree:

| budget | presentations | halts when threshold is | batch A recovery | ordering holds |
|---|---|---|---|---|
| 3 | 4 | 3, 4 — most worlds | — | — |
| **2** | **3** | **3 — seven of twenty** | **53.7%** | **20/20** |
| 1 | 2 | never | ~4 points lower | 16/20 |

A budget of 3 is what the naive arm does, and it is why naive destroys 61.8% of the
recurring book. That one was never in question.

Between 1 and 2 there is a real trade, and it is a judgement rather than a derivation.

**1** buys a distribution with no left tail at all: it halts nothing in any of the twenty
worlds and its net lift stays inside [+Rs 3.38L, +Rs 4.14L]. What it costs is 4.2 points of
recovery rate — Rs 23,475 of gross on batch A — in *every* world, including the two-thirds
where the tail never materialises. It also loses to naive outright in the four worlds where
the threshold lands on 5 and naive gets away with four presentations, which drops the
claimed ordering to 16 of 20.

**2** recovers 53.7%, and the claimed ordering holds in 20 of 20. Its exposure is the seven
worlds where the threshold is 3: there it halts up to 35.8% of the book and net lift runs to
−Rs 40 lakh.

**The budget is 2.** The tail is real and it is *reported* — it is the `worlds w/ halt`
column of the sensitivity table, sitting in plain sight immediately next to the ordering
claim. That is the distinction that matters here: this trades a disclosed risk against a
measured gain, rather than suppressing one to be able to state the other.

### A version that was built, measured, and deleted

The obvious way to have both was a *selective* second presentation: allow it only when the
diagnosed cause is one that clears on its own — an outage ending, a route recovering, a
balance arriving on payday, a cap rolling over — and refuse it for a dead card, a revoked
mandate, an absent customer or a risk block. The theory was that a second attempt which
*succeeds* resets the rail's consecutive-failure counter to zero and therefore costs
nothing, so targeting the attempts should buy the recovery without the halts.

It did not work, and the measurement is more interesting than the idea:

| | recovery | doubles | worlds w/ halt | worst halt |
|---|---|---|---|---|
| blanket budget of 2 | 51.7% | 4 | 7/20 | 39.5% |
| selective | 51.5% | 4 | 7/20 | 39.5% |

Identical, to within noise. (Both measured before the pairing change below, which is why the
levels differ from the table in §9. The comparison is like-for-like and the mechanism does
not depend on how the simulator draws its random numbers.) The reason is simple once seen: **a second presentation that
fails is a strike whatever motivated it**, and the halt rate is set by how often the second
attempt fails, not by why it was made. Selecting better causes raises the success rate of
those attempts, but the failures that remain are still consecutive failures.

The code was removed. It earned nothing and it was not free to read.

### The arms were not actually paired, and it was hiding two points of recovery

Every arm faces a world built with the same calibration and the same seed. That sounds like
a controlled comparison and it is not quite one, because the world held a **single random
generator**. The moment one arm takes a different number of actions than another — and the
whole point is that they do — its draw sequence shifts, and every *subsequent* case sees
different randomness than the same case in the other arm.

So the arms differed by their decisions **and** by an accident of ordering, and there was no
way to tell those apart in the lift estimate. Naive averages 2.5 charge attempts per case
against the policy arm's 1.5, so the two were consuming the draw sequence at very different
rates.

Each case now draws from its own stream, seeded from the case id. This is common random
numbers, and it changes nothing about what is being estimated — only how much noise the
estimate carries. What it cost was fifteen lines; what it bought:

| | shared generator | per-case streams |
|---|---|---|
| policy arm recovery | 51.7% | **53.7%** |
| policy arm net lift | +424,427 | **+510,124** |
| net-lift range across 20 worlds | 5.24L wide | **4.50L wide** |
| double charges across 20 worlds | 3 to 4 | **exactly 4, every world** |

Measured before the post-authorization veto existed; that row now reads 0 in every world,
which is why the table is dated rather than refreshed. The point it was making is about
*variance*, and refreshing it to a column of zeroes would destroy the evidence.

The last row is the clearest evidence that this is noise removal rather than a better draw.
Whether a specific ambiguous payment gets double-charged should be a property of that case,
not of how many actions the arm happened to take on the 200 cases before it. Under the
shared generator it varied. It no longer does.

The recovery figure moving by two points is worth dwelling on, because it means the earlier
number was not wrong so much as **imprecise in a way nothing on screen disclosed**. A
two-point gap between the arms was sitting inside the noise of how the simulator drew random
numbers.

`tests/test_replay.py` asserts the property directly, and — the part that matters for a
regression test — it was checked against the old behaviour and fails there: 5 of 120
untouched cases changed outcome purely because other cases had been acted on first.

This is the third correction in a day that moved numbers in the direction of the arm I
built, which is reason enough to say plainly why it is not a thumb on the scale: the
technique is standard, it is unbiased for the difference between arms, the test defines the
property independently of which arm benefits, and the tightened ranges are the signature you
would predict *before* looking at who won.

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
- **The arms are paired, and this used to be listed here as a limitation.** It is recorded
  as a correction rather than quietly deleted. Until D5 every arm faced an identically
  *seeded* world but shared one generator, so the moment an arm took a different number of
  actions its draw sequence shifted and every later case saw different randomness than the
  same case in another arm. Each case now draws from its own stream. `tests/test_replay.py`
  asserts the property, and it fails against the old behaviour — 5 of 120 untouched cases
  changed outcome purely because other cases had been acted on first.
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

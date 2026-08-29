# Project Progress

An engineering log for this repository: what has been built, what was decided and why, and
what broke along the way. Newest entries at the bottom.

---

## Milestones

| # | Milestone | Status |
|---|---|---|
| D1 | Sealed world — domain model, personas, outcome engine, batch generator | ✅ **done** |
| D2 | Detection, append-only ledger, invariants R1–R6 | ✅ **done** |
| D3 | Root-cause diagnosis + labelled evaluation and confusion matrix | 🟡 **batch B run in flight** |
| D4 | Policy engine, budgets, retry scheduler | ✅ **done** |
| D5 | Evaluation harness, four arms, metrics, sensitivity | ✅ **done** — reported table waits on D3 |
| D6 | Executor + Razorpay test-mode integration | ⬜ (cut before the sensitivity work if time runs short) |
| D7 | Console polish + one injected failure handled gracefully | ⬜ (shell built early, D2) |
| D8 | README, ADRs, results writeup | 🟡 everything but the results section |
| D9 | Video | 🟡 script drafted, figures pending batch B |

---

## The problem

A payment fails. Something recoverable happened — an issuer timed out, an account was empty
on the 28th, a customer walked away from an OTP screen, a mandate was revoked in a PSP app.
The money was already earned and then fell out of the bucket.

A naive recovery system retries everything identically. That is wrong in both directions:
retrying an empty account immediately is guaranteed to fail and burns a retry from a budget
that has real limits, while an issuer timeout would very likely have cleared sixty seconds
later. Retrying a *recurring* debit too often is worse than useless — the mandate gets halted
and a ₹499/month subscription becomes a churned customer.

This project builds an agent that diagnoses the cause, chooses a bounded action, and — the
part that actually matters — **measures whether it recovered money that would not have come
back anyway.**

---

## Key design decisions

### The simulator is sealed from the policy

Recovery data of this kind is not publicly available, so the world is synthetic. That
creates an immediate credibility problem: if the policy can read the parameters that decide
whether a retry succeeds, then any reported recovery rate is circular — the agent is
rediscovering constants that were written down by hand, and the number means nothing.

So the boundary is enforced rather than intended:

- `reclaim/synth/` owns ground truth. `reclaim/core/` owns decisions. **`core` may never
  import `synth`**, checked statically by AST, checked again by string search to catch
  `importlib` workarounds, and checked a third time at runtime by importing every `core`
  module with `reclaim.synth` poisoned so that touching it raises.
- Ground truth is written to a **physically separate file**. `cases.jsonl` is what the agent
  consumes; `truth.jsonl` is what the evaluation consumes. A test asserts that no ground
  truth field name appears anywhere in `cases.jsonl`.
- Two batches, different seeds, deliberately different mixes. **Batch A is for tuning. Batch
  B is reported and the policy is never tuned against it.**

`tests/test_seal.py` is the most important test in the repository.

### Every recovery figure is reported as lift over a control arm

**28.2% of cases in batch A and 23.8% in batch B recover on their own**, with no
intervention at all — the customer retries, the bank clears, money arrives.

That single fact makes gross recovery rate a dishonest metric. An agent that intervenes on
every case and reports "we recovered 46%" has really recovered nine points, and has spent
real money on messages and retries to claim the other twenty-four it never earned. A
control arm receiving no intervention is therefore not an optional extra; it is the only
thing that makes the headline number true.

### Calibration constants are anchors, and the report must survive them being wrong

The numbers in `Calibration` are order-of-magnitude anchors from public reference points —
NPCI's bank-wise UPI decline statistics, the well-documented completion gap between UPI
intent and card payments carrying an OTP step, the observation that recurring debits fail
mostly on balance rather than on plumbing. They are not measurements and are not presented
as any.

The honest answer to "you made these numbers up" is not to argue: it is to show the range
over which the conclusion holds. Every constant lives in one frozen dataclass so that
`jitter()` can perturb the entire calibration at once, and the reported comparison is re-run
under perturbation. If the ranking of arms flips under a plausible jitter, that gets said.

### A rules-only arm sits between the naive baseline and the agent

Four arms are compared: control, naive retry, a hand-written rules table, and the full
agent. The rules-only arm exists specifically to isolate what the language model contributes
over a competent deterministic policy. Without it, any improvement can be attributed to the
model when it actually came from thinking about the problem at all.

### Failure is correlated with the customer, not sprinkled uniformly

Six personas — a salaried customer living close to the line, one with headroom, lumpy
self-employed income, someone already halfway out the door, a high-engagement digital
native, and someone who never reads anything. Cause probabilities are the persona's
propensity blended with what is structurally possible on the rail.

Uniform random failure would make the problem trivial and the evaluation worthless: there
would be nothing to learn about a case from anything except its error code. The whole
question is whether a chronically empty account can be told apart from a one-off timeout.

### Causes that cannot happen on a rail have zero weight

A stored-card recurring debit cannot be abandoned at an OTP screen that never appears. A
one-time card payment has no mandate to revoke. These are asserted rather than left to
chance, because a single incoherent record undermines the credibility of the whole batch.

---

## The taxonomy

Nine root causes, because each implies a materially different action:

| Cause | Right action |
|---|---|
| `issuer_technical_decline` | retry in minutes — it will probably work |
| `insufficient_funds` | retrying now is guaranteed to fail; the only lever is *when* |
| `auth_abandoned` | nobody is there — a silent retry is worthless, outreach is the only path |
| `instrument_invalid` | no charge can succeed until a new instrument arrives |
| `mandate_revoked` | authorisation is gone; needs a fresh mandate |
| `limit_exceeded` | retry later, or at a lower amount |
| `risk_declined` | do not retry — it argues with a fraud rule and eventually hard-blocks |
| `psp_routing_failure` | our route broke, not the bank; another PSP works right now |
| `ambiguous_debited` | **never retry** |

`ambiguous_debited` is the dangerous one. No clean answer came back and the customer may
already have been debited. A retry *succeeds* — and that success is a duplicate charge, a
refund liability, and a very unhappy customer. In the generated error text it is deliberately
near-indistinguishable from a plain technical decline, because that is exactly how it
presents in reality.

Three confusion pairs are engineered on purpose:

```
ambiguous_debited   vs  issuer_technical_decline    both read as a timeout
mandate_revoked     vs  instrument_invalid          both read as "not valid"
psp_routing_failure vs  issuer_technical_decline    both read as a gateway error
```

The only honest signal separating the first pair is that a bank reference tends to come back
when money actually moved — 78% of the time against 16% for a plain timeout. A tendency, never
a guarantee. That is what makes the call hard, and it is why the diagnosis step is worth
building rather than hard-coding.

---

## Invariants

Run-level conditions asserted after every batch, reported alongside the metrics:

```
R1  no payment charged more than once across all recovery attempts
R2  sum(recovered) <= sum(at_risk)                   no phantom recovery
R3  no customer contacted more than N times per 7d   frequency cap holds
R4  no contact outside permitted hours               quiet hours hold
R5  no action on a terminated or opted-out case
R6  no case left non-terminal after the batch drains
```

R1 is enforced structurally — a unique constraint on `(payment_id, attempt_no)`, insert-first
and catch the violation, never `SELECT`-then-`INSERT`. A check-then-act sequence has a race
window between the two statements; a constraint is evaluated atomically and has none.

Metrics alone are not enough here. An agent can post a good recovery rate while quietly
double-charging customers and messaging them at 3am, and the metrics will not show it. The
invariants are what make "bounded" and "compliant" falsifiable claims rather than adjectives.

**Not every arm is required to hold them.** `control`, `rules` and `agent` are arms this
project makes claims about, and a violation there is a defect that fails the build. `naive`
exists to be bad — it retries everything three times, ambiguous debits included — and its
violations are the finding rather than a failure. It violates R1 on live data today.

That distinction is the point. Every invariant is also tested against a planted violation of
the exact failure it exists to catch, because an invariant suite that no arm has ever failed
is indistinguishable from one that *cannot* fail, and "6/6 held" printed by a check that can
only ever print "held" is worse than printing nothing — it is a claim backed by nothing, in a
report whose whole argument is that claims should be backed.

---

## Log

### D1 — The sealed world · 2026-08-26

**Built**

```
reclaim/
├── domain.py            shared vocabulary — types only, no probabilities, no policy
└── synth/
    ├── personas.py      6 archetypes and the population mix
    ├── outcome.py       the outcome engine: Calibration, GroundTruth, World
    └── generator.py     batch generation and the error catalogue
tests/
├── test_seal.py         the import boundary, three ways
└── test_generator.py    world properties
```

`World` adjudicates two actions and nothing else: `attempt_charge` and `send_contact`.
Outreach that lands opens a *presence window*, and only inside that window can a rail
needing a live human succeed — which is what makes `auth_abandoned` recoverable by a message
and not by a retry. Infrastructure failures arrive as bounded **outage episodes** affecting
one issuer or one PSP, rather than being sprinkled per-transaction, so waiting one out is a
real strategy and clustering is detectable.

Costs are tracked as first-class state, not derived at the end: per charge attempt, per
message by channel, the scheme penalty for exceeding a card retry budget, the cost of
unwinding a double charge, and the residual value forfeited when a mandate is halted.

**Batches generated**

| | Batch A (tuning) | Batch B (held out) |
|---|---|---|
| seed | 11 | 20260904 |
| cases | 600 | 600 |
| at risk | ₹18,45,200 | ₹17,61,200 |
| recurring | 372 | 369 |
| **recover organically** | **169 (28.2%)** | **143 (23.8%)** |

Batch B shifts the mix — 1.6× the ambiguous debits, 1.8× the customers already leaving, and
a lower organic recovery rate. A policy tuned on A has to hold up against a harder,
differently-shaped population.

**What broke**

1. **A card payment came back carrying a UPI error string.** The first generated batch
   produced a `card_3ds` payment whose error description read *"UPI collect request expired
   — not approved by payer"*. The error catalogue was keyed only on root cause, so any
   variant could land on any rail. Nonsense of this kind is spotted immediately by anyone
   reading the data and quietly invalidates the entire batch. Fixed by tagging every variant
   with the rail families it is valid on and filtering at selection; `test_error_text_is_valid_for_its_rail`
   now checks all 1,200 generated cases, and generation raises rather than emitting an
   incoherent record.

2. **`pip install -e .` failed on package discovery.** Setuptools' automatic discovery
   refuses to guess when several top-level directories look like packages — here `data`,
   `docs` and `tests` alongside `reclaim`. The error text is about "multiple top-level
   packages" and does not name the fix. Resolved with an explicit
   `[tool.setuptools.packages.find] include = ["reclaim*"]`.

3. **Rare classes are genuinely rare.** `risk_declined` lands at 4 cases in batch A and 8 in
   batch B. This is realistic for a recurring-debit book and was deliberately not inflated,
   but it means per-class diagnosis metrics for the rare causes will carry wide intervals.
   Support counts will be reported next to every per-class figure rather than hidden inside
   a macro average.

**Verified**

```
pytest                              11 passed, 1 skipped, 0 failed
rail/error-text consistency         1,200 / 1,200 cases coherent
generation determinism              same seed reproduces the same batch
committed batches reproducible      cases.jsonl matches regeneration from meta.json seed
```

The single skip is the runtime half of the seal test, which has no `core` modules to poison
yet. It starts running on D2.

**Next**

D2: case detection, the append-only decision ledger, and invariants R1–R6 with tests. The
ledger is the audit trail, so it is built before anything that makes decisions worth
auditing.

---

### D2 — The ledger, the bounds, and six invariants that can fail · 2026-08-26

**Built**

```
reclaim/
├── core/                       the agent. May never import synth.
│   ├── compliance.py           every stopping rule and contact bound, in one file
│   ├── feed.py                 the agent's read-only view; refuses truth.jsonl
│   ├── detect.py               triage: what is worth working, and in what order
│   ├── ledger.py               append-only audit trail; R1 lives in the schema
│   └── guards.py               R1–R6, re-derived from the ledger
├── eval/
│   ├── replay.py               the driver; control and naive arms
│   └── metrics.py              per-arm figures, all derived from the ledger
└── api/
    ├── main.py                 read-only HTTP view over the ledger
    └── static/                 the console — one page, no build step
tests/  test_ledger · test_detect · test_guards · test_compliance · test_api
```

Three decisions worth recording.

**The ledger is append-only by trigger, not by convention.** Every table carries
`BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT)`. "We only ever insert" is
a code-review convention that survives until the first bug fix under deadline; a trigger
survives the bug fix. Twelve tests do the update and the delete and assert the database
refuses.

**Charging is two-phase, and that fell out of append-only rather than being designed in.**
A claim row cannot be updated with its result, so the result is a separate row in
`charge_results` keyed to the claim. That turned out to be the correct shape for a reason
that had nothing to do with SQLite: *a claim with no result is a charge that was authorised,
may have reached the issuer, and whose outcome we never learned.* That is exactly
`ambiguous_debited`. Making the state representable means the audit trail can say "unknown"
instead of silently picking a side, and `R1` now checks for unsettled claims as a distinct
failure from double-charging.

**`UNIQUE (payment_id, attempt_no)` became `UNIQUE (run_id, payment_id, attempt_no)`.**
Four arms replay the same batch, so without `run_id` the second arm would collide with the
first on every case. Within a single run — which is what a production deployment is — the
key is still exactly `(payment_id, attempt_no)`.

`compliance.py` exists because "compliant escalation, stopping rules" is a claim, and
claims should be checkable in thirty seconds. Contact hours are 09:00–21:00 Asia/Kolkata,
the strict promotional window, applied to all outreach rather than arguing the transactional
exemption for a nudge that carries an incentive. The frequency cap is rolling over 7 days,
not per calendar week — a cap that resets on a fixed boundary permits three messages on
Sunday night and three more on Monday morning, which is six in twelve hours and obviously
not what the cap meant.

The file also holds `ASSUMED_MANDATE_RESIDUAL_MONTHS = 6`, the agent's own estimate of what
a halted mandate forfeits. The world's true figure is 9. **They are deliberately unequal.**
If they matched, a reviewer would be right to ask whether the policy had been handed the
answer.

**Built early: the console**

The demo frontend was pulled forward from D7 to now, because building it against two arms
that already exist is far cheaper than building it in the last two days against four. It is
FastAPI plus one HTML page, vanilla JS, hand-written CSS — no npm, no build step, nothing to
break the night before the deadline.

Two things about it are load-bearing rather than cosmetic. **The API has no non-GET routes**
— the demo surface cannot move money, and a test asserts it by enumerating `app.routes`.
And **the Live view is a replay, not a simulation**: it plays back the recorded decision
timeline of a run that already happened, at an accelerated clock. Every line on screen is a
row in the ledger, and the same rows produce the numbers on the Results tab. Nothing is
generated for the camera.

**What broke**

1. **The most important test in the repo was about to pass vacuously.** `test_seal.py`
   poisons `reclaim.synth` and imports every `core` module to prove none of them reach for
   it. The poison was a meta-path finder implementing `find_module` — deprecated in Python
   3.4 and **removed from meta-path finders in 3.12**. A finder defining only `find_module`
   is silently never consulted. On D1 the test *skipped* (no `core` modules existed), so
   nothing was noticed; the moment `core/` appeared it would have started printing a pass
   while checking nothing at all.

   Caught by asking the obvious question — can I show this fires? — and the answer was no.
   Fixed by implementing `find_spec`, and by adding `test_the_poison_actually_bites`, which
   asserts that importing `reclaim.synth` *under* the poison raises. A guard that cannot be
   demonstrated to fire is not a guard.

   This is the same reasoning that shaped the whole guard suite below.

2. **`ZoneInfo("Asia/Kolkata")` raises on Windows.** Windows ships no system zone database,
   so `zoneinfo` needs the `tzdata` package; without it the error is
   `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'`, which reads like a
   typo rather than a missing dependency. Added `tzdata>=2024.1; sys_platform == 'win32'`.

   The tempting fix was to delete the timezone and compare naive wall-clock times, which
   would have worked on every current test and been wrong the first time an aware datetime
   arrived: 18:00 UTC is 23:30 IST, and treating it as local would wave a 23:30 message
   straight through the quiet-hours check. `as_local()` now converts aware datetimes and
   leaves naive ones alone, and both directions are tested.

3. **Duplicate detection: chaining vs re-anchoring.** The first test written asserted that
   three failures 20 minutes apart collapse into one event. The implementation disagreed,
   and the implementation was right. Chaining — linking each arrival to the previous one —
   means a customer failing every 29 minutes for a week has the whole week merged into a
   single event and six days of genuinely recoverable failures suppressed. The window
   re-anchors instead. The test was rewritten to assert the correct behaviour and to say why.

4. **The console's event feed went blank while the counters kept climbing.** Clicking a case
   scrolls the feed container; prepending into a container with a non-zero `scrollTop` keeps
   that pixel offset, so the viewport drifts past the tail as old rows are trimmed. Pinning
   `scrollTop = 0` on every prepend fixes it. Clicking an event now also pauses playback,
   because reading a case while the feed moves underneath it is the frustration the panel
   exists to remove.

5. **`record_charge()` was written, then deleted.** It claimed and settled in one call, as a
   convenience. It is also a shortcut past the insert-first discipline, and a shortcut past
   a safety property is not a convenience. There is now one way to charge and it is the safe
   one; the test that covered the shortcut was removed rather than the shortcut re-added.

**On invariants that can actually fail**

The naive arm is not required to hold R1–R6. It exists to be bad — three fixed retries on
everything, including the ambiguous debits — and its violations are the finding, not a build
failure. `control`, `rules` and `agent` are arms this project makes claims about, and a
violation there is a defect; the CLI exits non-zero only for those.

This matters more than it looks. Every guard is also tested against a planted violation of
exactly the failure it exists to catch, and the naive arm violates R1 on live data. An
invariant suite that no arm has ever failed is indistinguishable from one that cannot fail,
and "6/6 held" printed by a check that can only print "held" is worse than printing nothing.

R3 is deliberately re-derived from raw contact rows with its own sliding window rather than
calling the same `ledger.contacts_in_window` helper the policy used to decide. A check that
shares an implementation with the thing it checks will agree with it about a bug.

**Verified**

```
$ .venv/Scripts/python -m pytest
166 passed in 4.82s
   test_api 71 · test_guards 25 · test_ledger 23 · test_compliance 20
   test_detect 13 · test_generator 8 · test_seal 6
```

The D1 skip is gone — the runtime half of the seal now runs, and now actually checks
something.

```
$ .venv/Scripts/python -m reclaim.eval.replay --batch B --arms all
batch B  n=600  at risk Rs 1,761,200
    600  eligible

arm         recovered   of n     gross Rs    cost Rs  halted  double
control           143    600      420,657          0       0       0
naive             271    600      821,929      6,970       0      26
```

```
$ .venv/Scripts/python -m reclaim.core.guards --batch B
B - control    B-control
  R1  no payment charged more than once                        HELD   0 charge attempts
  R2  no phantom recovery                                      HELD   recovered 420,657 of 1,761,200 at risk
  R3  max 3 contacts per customer per 7d                       HELD   0 contacts to 0 customers
  R4  contact only within 09:00-21:00                          HELD   0 contacts checked
  R5  no action on a terminated or opted-out case              HELD   0 opt-outs recorded
  R6  no case left non-terminal                                HELD   600/600 cases closed
  6/6 held

B - naive      B-naive   [baseline: measured, not asserted]
  R1  no payment charged more than once                    VIOLATED   1540 charge attempts
        - case_B00072: attempt 1 double-charged an already-debited payment (pay_B00072_0)
        - case_B00249: attempt 1 double-charged an already-debited payment (pay_B00249_0)
        - case_B00259: attempt 1 double-charged an already-debited payment (pay_B00259_0)
        ... 23 more
  ...
  5/6 held

1 asserted arm(s): 6/6 held
```

Batch A, same commands: control 169 recovered, naive 315 with 20 double charges, control
6/6 held and naive 5/6.

**The first honest number**

Batch B, naive against control, straight from `eval.metrics`:

| | control | naive |
|---|---|---|
| recovered | 143 (23.8%) | 271 (45.2%) |
| of which organic | 143 | **90** |
| lift over control | — | **+128 cases** |
| net | ₹4,20,657 | ₹8,14,959 |
| net lift | — | **+₹3,94,302** |
| double charges | 0 | **26** |

Naive looks like it recovered 45.2%. It did not. Ninety of those 271 came back with no help
from it at all, and the honest figure is 128 cases of lift. This is the entire argument for
the control arm existing, and it shows up on the very first arm measured.

The 26 double charges are the number to beat. Naive earns ₹3.94L of net lift and takes money
twice from 26 customers to do it — and at the world's current `double_charge_cost_paise` that
costs only ₹3,120, which flatters it badly. The real cost of debiting a customer twice is a
refund, a support ticket and a relationship. That is a calibration constant the sensitivity
run will need to push hard on, because if the ranking of arms depends on it being cheap, the
ranking is not a finding.

Note also `halted 0` on both arms: naive stops at three attempts and the mandate halt
threshold is four. Mandate-halt rate cannot discriminate between arms yet, and will only
start to once an arm retries harder than naive does. Worth knowing before it is quoted.

**Next**

D3: root-cause diagnosis. Messy issuer text → the `source × step × reason` taxonomy, scored
against ground truth with a published confusion matrix and support counts next to every
per-class figure. `risk_declined` has 8 cases in batch B, so those intervals will be wide and
will be reported as wide.

One thing to settle in D6 rather than now: every arm currently faces a `World` with identical
parameters and an identical seed, but the random draw sequence diverges as soon as arms take
different numbers of actions. Pairing each case to its own RNG stream would make this a
properly paired comparison and tighten the lift estimate. It needs a change inside
`synth/outcome.py`, which has been frozen since D1, so it is a deliberate decision and not a
casual edit.

---

### D3 — Diagnosis, and what a keyword matcher structurally cannot do · 2026-08-27

**Built**

```
reclaim/core/diagnose.py     Diagnoser protocol; StubDiagnoser; Gemini and Groq providers;
                             the resumable, committed diagnosis cache
reclaim/eval/confusion.py    scoring against ground truth - per-class with support counts,
                             the three engineered pairs individually, cost-weighted error
tests/test_diagnose.py       27 tests, including the seal at the prompt boundary
```

Three decisions worth recording.

**The model runs once per batch, ever.** Output goes to `data/<batch>/diagnoses.jsonl`, which
is committed, and the eval harness reads that file rather than calling a model. This is not a
cost optimisation. The README claims every number reproduces on a clean checkout, and someone
cloning this repo has no API key of any kind - so a diagnosis step that needs a live API is a
reproducibility claim that is simply false. Caching it is what makes the claim true. That the
same decision also collapses the running cost to a one-time charge is a side effect.

**The stub is an arm, not a fallback.** `StubDiagnoser` is deterministic keyword matching with
no network. It exists to be beaten, and running it as its own arm separates two things that
otherwise get credited to the same place: how much of the agent's lift comes from the
*diagnosis*, and how much from the policy wrapped around it. An agent that beats naive retry
by a mile but only beats a regex by a hair has a good policy and an expensive classifier.

**Diagnosis is per case, independently.** Batching ten cases into one request would cut token
cost roughly fourfold and was tempting under a tokens-per-minute limit. Rejected: it lets case
seven's diagnosis be influenced by case three. Independent classification is the honest design
and the four-hour run is the price.

**The finding**

The stub cannot diagnose `ambiguous_debited`. Not "does it badly" - **0 out of 20 on batch A
and 0 out of 26 on batch B**. This is structural rather than a tuning failure: separating a
real ambiguous debit from a plain issuer timeout means weighing whether a bank reference came
back, which is a *tendency* (78% against 16%), not a keyword. A string matcher has no way to
weigh a tendency. A test pins this, so that if some future edit teaches the stub this class by
another route, the claim gets rewritten rather than silently kept.

That is the whole argument for the model, and it is the class where being wrong causes a
double charge.

**Provider selection, and a trap avoided**

The first choice, Gemini 2.5 Flash, has a free-tier cap of **20 requests per day**. Not per
minute - per day. Discovered when a 40-case pilot died at call twenty. Finding this on a
pilot rather than seven hundred cases into a reported run was luck dressed up as process.

Free-tier limits are per model and vary enormously, so the alternatives were probed directly
rather than trusted from documentation:

| provider / model | binding limit | throughput |
|---|---|---|
| gemini-2.5-flash | 20 requests/day | unusable |
| gemini-3.5-flash-lite | 500 requests/day | 1,200 cases = 2 days |
| groq openai/gpt-oss-120b | 8,000 tokens/min | 1,200 cases = 3.7 hours |

Groq's constraint is tokens rather than requests, and at 1,462 tokens per call - of which
**1,148 are the system prompt, resent every time** - that works out to 5.5 calls a minute.
Trimming the prompt is the obvious lever and was rejected: that prompt is what produces the
accuracy, and this is a one-time unattended run.

The quota day also resets on **Pacific time**, not local midnight, which is why an apparently
fresh quota at 01:00 IST was still the previous day's exhausted budget.

**Model comparison, identical prompt and identical payload**

Both providers call the same `observable()` function and the same system prompt, so this
compares models and nothing else. Stratified 40-case sample of batch A, weighted to the hard
pair:

| | stub | gemini-3.5-flash-lite | groq gpt-oss-120b |
|---|---|---|---|
| accuracy | 0.325 | 0.850 | **0.950** |
| `ambiguous_debited` caught | 0/15 | 15/15 | **15/15** |
| false alarms | 0/25 | 1/25 | **1/25** |

**Verified**

```
$ .venv/Scripts/python -m pytest
193 passed

$ .venv/Scripts/python -m reclaim.eval.confusion --batch A --provider gemini
batch A  provider=gemini  model=gemini-3.5-flash-lite  n=477
accuracy 0.878   macro-F1 0.813   weighted-F1 0.887   cost-weighted error 0.122
```

Like-for-like against the stub on those same 477 cases:

| | stub | gemini | |
|---|---|---|---|
| accuracy | 0.532 | 0.878 | +0.346 |
| macro-F1 | 0.461 | 0.813 | +0.352 |
| cost-weighted error | 0.900 | 0.122 | **-87%** |
| `ambiguous_debited` recall | 0/15 | 15/15 | |
| `mandate_revoked` recall | 8/26 | 26/26 | |

Cost-weighted error is the figure to lead with. It counts each mistake by what the mistake
*does* rather than by whether it was a mistake, so calling an ambiguous debit a technical
decline - which causes a double charge - is weighted ten times a confusion between two causes
that both imply "wait, then retry". It fell by 87%.

**What broke**

1. **The seal's string check fired on a docstring.** `tests/test_seal.py` fails if the
   simulated world's module name appears anywhere under `core/`, and the new module said in
   prose that it must not import it. The check is crude - it cannot tell a comment from
   `importlib.import_module(...)` - and that crudeness is exactly why it has no false
   negatives. Reworded the docstring rather than teaching the check to parse. A guard that
   gets narrowed every time it is inconvenient stops being a guard.

2. **Over-calling the dangerous class.** `ambiguous_debited` recall is 15/15, but precision is
   **0.306** - 49 cases flagged, 34 of them wrong. Erring toward "this may already have been
   debited" is the safe direction, since the cost is a missed recovery rather than a double
   charge, but it is not free: `psp_routing_failure` recall fell to 0.509 as collateral, and
   the agent arm will show lower gross recovery because of it. This is a prompt problem, not a
   model problem, and it is the reason iteration speed mattered enough to change providers.

3. **A convenience method that undercut a safety property.** `record_charge()` claimed and
   settled a charge in one call. It was also a shortcut past the insert-first discipline that
   R1 depends on, and a shortcut past a safety property is not a convenience. Deleted, and the
   test covering it deleted with it, rather than keeping both paths.

**Next**

D4: the policy engine. Diagnosis becomes action - `insufficient_funds` waits for payday rather
than burning a retry, `ambiguous_debited` routes to reconcile-hold and never retries,
`auth_abandoned` gets outreach inside the contact window because a silent retry against an
absent human is worthless. The bounds already exist in `core/compliance.py`, so the policy has
only to obey them and the guards will catch it if it does not.

That produces the two remaining arms - `rules` (hand-written table, no model) and `agent`
(model diagnosis, same policy) - and the gap between them is the number this project exists to
report.

Before that: fix the precision problem above, now that testing a prompt change costs five
minutes rather than a day of quota.

---

### D4 — The policy engine, and what the sensitivity run cost me · 2026-08-27

**Built**

```
reclaim/core/policy.py        the cause table, the stopping rules, the ambiguity gate.
                              A pure function: CaseView in, one Action out.
reclaim/eval/sensitivity.py   replays every arm against N worlds with all calibration
                              constants jittered at once
reclaim/eval/replay.py        the policy executor, plus the `rules` and `agent` arms
reclaim/eval/metrics.py       the results table as a CLI
tests/test_policy.py          74 tests, no simulator and no network in any of them
```

Three design decisions worth the space.

**`next_action` returns one action, not a plan.** A plan computed up front has to guess at
the outcome of its own first step, and the interesting cases are precisely the ones where
step two depends on step one: outreach that lands makes a charge worth attempting, and
outreach that does not makes the same charge worthless. Being a pure function is also why
the test file needs neither the world nor a ledger - construct a situation, assert on the
decision. A failing test names the decision that broke rather than the run that exposed it.

**`rules` and `agent` are the same engine.** They differ only in which `Diagnoser` produced
their input. Whatever gap the results table eventually shows between them is attributable to
diagnosis quality and to nothing else, because there is nothing else different. That is the
entire reason for keeping a rules-only arm, and it is worth the extra plumbing.

**Stopping rules are evaluated before the cause is looked at.** A bound that only applies
when the policy has not thought of something more interesting to do is not a bound.

**What broke**

1. **The threshold was phrased backwards, and it cost twelve cases by a hundredth of a
   point.** The ambiguity gate refuses to charge over a bank reference on an unconfident
   diagnosis. I first wrote it as "below 0.55, treat the diagnosis as a guess". The keyword
   diagnoser emits *exactly* 0.55 on the rule those cases land on - because `GATEWAY_ERROR`
   in the error *code* matches its routing pattern - so a strict `<` let all twelve through
   and the arm double-charged them.

   The fix is not a different number. Both 0.55 and 0.60 are arbitrary at the margin; what
   was wrong was the direction. Rewritten as a bar to clear - `CHARGE_OVER_REFERENCE_
   CONFIDENCE = 0.75`, *"to charge over a bank reference you must be confident"* - the
   arbitrariness lands on the safe side, which is the side where a missed recovery costs an
   invoice rather than a duplicate debit costing a refund and a customer. Double charges on
   batch A went 16 → 4.

2. **A metric that could only ever print 0% or 100%.** `mandate_halt_rate` divided halted
   cases by "cases with non-zero residual loss". Residual loss is only ever non-zero on a
   case that halted, so the numerator and denominator were the same set. It read as a
   perfectly plausible `0.0%` through the entire base run. It only became obviously wrong
   when a perturbed world produced a column of `100.0`s that no policy could have earned.

   The lesson is not the bug, it is where it was caught: a headline downside metric was
   structurally incapable of reporting anything, and the single-run report showed no sign of
   it. `is_recurring` is now recorded on the outcome row rather than inferred.

3. **`CREATE TABLE IF NOT EXISTS` does exactly what it says.** Adding a column meant every
   existing ledger file kept the old schema, and the symptom was `sqlite3.OperationalError:
   no such column: is_recurring` raised from `eval/metrics.py` - three modules from the
   cause, in a test about the API not leaking ground truth. An append-only store cannot be
   migrated in place, so the ledger now checks its own columns on open and raises
   `StaleLedger` telling you to delete the file. Cheap, and it turns ten minutes of confusion
   into one line.

4. **R5 fired on the message that caused the opt-out.** The contact and the opt-out were
   recorded at the same instant, and the invariant reads "no action at or after an opt-out",
   so the triggering message looked like a violation of a rule it had itself created. The
   temptation was to relax the check to `>`. That is backwards: an opt-out is an *inbound*
   event and cannot be effective before the outbound that provoked it, so recording it at the
   send time was the actual bug - it claimed we knew before we could have. Recorded one second
   later, and the guard is untouched.

5. **The frequency cap has to look forward as well as back.** Cases are worked in *value*
   order, not clock order, so a message already in the ledger can sit later on the simulated
   clock than the one being proposed. A backward-looking count does not see it, sends anyway,
   and leaves a violation for the guard to find after the money has moved. The policy now
   inserts the proposed time into the customer's history and slides a window over the result -
   the same computation the guard performs, written independently, because two agreeing copies
   of one implementation prove nothing.

6. **I had the wrong explanation for the D3 precision problem, and it was cheap to check.**
   D3 recorded `ambiguous_debited` precision of 0.306 and concluded "this is a prompt problem,
   not a model problem". It was the opposite. Running the *unchanged* prompt through Groq on a
   67-case stratified slice of batch A, weighted to the hard pairs:

   ```
   accuracy            0.985   (66/67)
   cost-weighted error 0.015
   ambiguous_debited   precision 1.000  recall 1.000   (14 predicted, 14 real)
   errors:
       1  instrument_invalid -> mandate_revoked
   ```

   0.306 was a Gemini result and the conclusion drawn from it was about the wrong component.
   I had a rewritten prompt ready and did not ship it - there was no measurable gap left to
   close, and changing a prompt on the strength of an assumption is how you end up unable to
   say what any number means. Twelve minutes of quota bought a like-for-like baseline and
   deleted a day of work.

**The sensitivity run changed the design**

This is the part I would not have got to by reasoning.

Perturbing every calibration constant by up to ±20% at once, all arms. With
the recurring charge budget at 3, the ranking of the arms held in 12/12 - and the levels
collapsed. Median net lift for the policy arm was **−Rs 12.1 lakh**, ranging to −Rs 23.1
lakh, because the rail halts a mandate after some number of consecutive failed presentations
that the agent does not get to see, and a budget of 3 sits directly on a threshold that
jitters into 3. Every recurring case worked hard lost its mandate, and the forfeited months
swamped everything recovered.

A policy fragile to a constant it cannot observe is not a policy. The right response to a
cliff of unknown position is **headroom, not a better guess at where the cliff is**, so the
recurring budget went to 2:

```
arm              net lift Rs, median [min .. max]       doubles  worst halt %  worlds w/ halt
---------------------------------------------------------------------------------------------
naive            400,251  [-7,555,833 .. 427,775]   20 [20..20]         60.8%            7/20
rules               474,962  [454,126 .. 538,386]      4 [3..4]          0.0%            0/20
```

The two arms have almost the same median and nothing like the same distribution. Naive runs
down to **−Rs 75.6 lakh** and destroys up to 60.8% of the recurring book in seven worlds of
twenty; the policy arm is bounded in **[+4.54L, +5.38L]** and halts nothing, anywhere.

(The design decision was taken on a twelve-trial run; the table above is the twenty-trial
confirmation, which moved the medians by under a percent and changed nothing qualitative.)

**Naive is not a slightly worse policy. Its expected value is dominated by a tail you cannot
see from its recovery rate.** That is exactly what the mandate-halt metric is for, and I
would have reported a near-tie without it.

The premium is real and belongs in the writeup: in the base world, where nothing ever halts,
the tighter budget gives up about **Rs 1.5 lakh** of recovery it could have had (batch A
gross fell 11.0L → 9.5L). That is what a bounded tail costs. It is a purchase, not a free win.

Stopping early is only half a policy, though - it stops the harm and also stops pursuing the
money. So a spent charge budget now falls through to outreach instead of closing the case:
**a message cannot halt a mandate and a presentation can**, so once presenting is off the
table the remaining ask is made of the customer rather than of the rail. Bounded by the same
contact caps as everything else.

**Verified**

```
$ .venv/Scripts/python -m pytest
267 passed in 4.25s

$ .venv/Scripts/python -m reclaim.eval.metrics --batch A
batch A   n=600

arm        rec %   gross Rs  cost Rs  residual     net Rs   net lift  cost/Re  halt % double
--------------------------------------------------------------------------------------------
control    28.2%    515,531        0         0    515,531          -        -    0.0%      0
naive      52.5%    925,485    6,235         0    919,250   +403,719    0.015    0.0%     20
rules      51.7%    948,990    9,032         0    939,958   +424,427    0.021    0.0%      4

$ .venv/Scripts/python -m reclaim.core.guards --batch A
A - control    A-control                                     6/6 held
A - naive      A-naive     [baseline: measured, not asserted] 5/6 held
A - rules      A-rules     [baseline: measured, not asserted] 5/6 held
1 asserted arm(s): 6/6 held
```

`rules` moved out of the asserted set, and the reason is the result rather than an excuse.
The keyword diagnoser cannot name `ambiguous_debited` at all, so the policy it feeds retries
payments that already went through. The gate catches sixteen of twenty; the other four carry
no bank reference and nothing observable separates them from an ordinary timeout. No gate
bolted on afterwards can recover them - only reading the description can. Asserting an
invariant on an arm designed to fail it would have meant deleting the finding or weakening
the check.

**The rate limit I was measuring was not the rate limit I was hitting**

Batch B's diagnosis run died twice, and both times I had the wrong model of why.

D3 recorded the binding constraint as 8,000 tokens per minute, which works out to about 5.5
calls a minute and a two-hour pass over 600 cases. That number is real and it is in the
response headers. It is also not what stops the run.

The first failure was mine rather than the provider's. The abort rule was *"a wait longer
than two minutes means a daily cap rather than a burst limit, so stop"* - a heuristic that
reads the length of the delay instead of asking which ceiling was hit. A per-minute token
limit also returns long delays, so the run aborted at case 296 of 600 with seven hundred
requests of daily budget unused. Replaced with a check of the actual headers.

The second failure was more interesting, because the headers said the request should have
succeeded:

```
requests 714/1000 left, tokens/min 8000/8000 left      ...alongside an HTTP 429
```

Both published budgets full, and still refused. The answer was in the response *body*, in
prose, in a field nothing was parsing:

```
Rate limit reached for model `openai/gpt-oss-120b` ... service tier `on_demand`
on tokens per day (TPD): Limit 200000, Used 199660, Requested 1560
```

**Tokens per day.** Not in any header, not in the SDK's rate-limit surface, and the only one
of the three ceilings that actually binds a batch this size: 200,000 tokens a day against
roughly 1,560 a call is about **128 diagnoses a day**. The whole 600-case pass is a
four-and-a-half day job, and the 304 still outstanding are about two and a half.

The lesson is not "read the body". It is that I had spent D3 carefully measuring and
documenting a limit - and building the pacing, the resume logic and the abort rule around it
- without ever confirming it was the one that would stop me. The headers were easy to read,
so I read those, and the fact that they were consistent with my model of the problem was
mistaken for evidence.

TPD refills continuously rather than at a fixed hour, so the client now sleeps through it
instead of giving up. A 600-case pass is an unattended job either way; dying every few
hundred cases only means somebody has to restart it by hand.

Options I considered for going faster, and why none of them helps:

| option | arithmetic | verdict |
|---|---|---|
| trim the system prompt | halves tokens/call, but mixes two prompts in one batch, so all 600 must be redone: 600 x ~800 = 480k | no faster, and costs the accuracy the prompt buys |
| switch to a larger-TPD model | 600 x 1,560 = 936k from scratch | marginal, and drops the model that won the pilot on the hard pair |
| switch to Gemini | 500 requests/day, and 0.878 accuracy against 0.976 | slower *and* worse |
| resume on the current prompt | 304 x 1,560 = 474k | ~2.4 days, and the cache already holds 296 |

Resuming wins on arithmetic, so the run keeps going. The deadline is 2026-09-04, which
leaves room.

**Where this stopped**

297 of 600 batch B diagnoses are cached and committed. The remaining 303 are roughly two and
a half days of free-tier budget at ~130 a day. The run appends and flushes per case, so it
picks up exactly where it left off:

```
.venv/Scripts/python -m reclaim.core.diagnose --batch B --provider groq --rpm 4.2
```

Nothing else is blocked on it. Batch A carries control, naive and rules; the `agent` arm is
the only thing waiting, and with it the reported table.

**Next**

When batch B's diagnoses land: the `agent` arm, the reported four-arm table on held-out B,
guards for control and agent, a 20-trial sensitivity run on B rather than A, and the results
section of the README, which is deliberately the one part of it left empty rather than filled
with batch A's tuning figures.

**A change I found and deliberately did not make.** Scoring the partial batch B diagnoses
showed the model is well calibrated - mean confidence 0.931 when it is right, 0.737 when it
is wrong, with three of its four errors falling below the 0.75 bar. The one ambiguous debit
it missed sits at 0.70 confidence and carries no bank reference, so the gate's second
condition rejects it and the case goes on to be charged. Dropping that condition - gating on
confidence alone - would hold three cases: the dangerous one plus two ordinary declines. One
prevented duplicate debit for two held invoices is a trade I would take.

I did not take it. That measurement is on batch B, and batch B is the batch the report
quotes. Changing a threshold because of what it does to the held-out set is precisely the
thing the A/B split exists to prevent, and a number tuned that way cannot be defended by the
person quoting it. The two conditions also do genuinely different work for different
diagnosers: confidence carries no information at all from the keyword matcher, which emits
0.25-0.55 on nearly everything, so for that arm the bank reference is the only signal there
is. A gate that is right for a calibrated model may simply be the wrong gate for an
uncalibrated one.

So it goes on the D5 list: validate it on batch A, and if it holds, it needs a fresh batch to
report against rather than a re-scored B.

Open question for D5, deliberately not settled under deadline: every arm faces an identically
seeded world, but the draw sequence diverges as soon as arms take different numbers of
actions, so this is a common-parameter comparison rather than a paired one. Pairing each case
to its own RNG stream would tighten every lift estimate here. It needs a change inside
`synth/outcome.py`, frozen since D1.

---

### D5 — Two numbers I had not measured · 2026-08-27

**Built**

```
reclaim/eval/report.py        the results table rendered into the README from the ledger,
                              between markers, so no figure in it is typed by hand
reclaim/synth/outcome.py      the mandate-halt counter now starts from the failure that
                              opened the case
reclaim/core/ledger.py        open_ledger(fresh=True); re-running a recorded run is refused
                              with an explanation instead of a UNIQUE violation
reclaim/core/diagnose.py      bounded naps while waiting out a rate limit, and a quota wait
                              no longer spends a retry attempt
tests/test_report.py          the splice, the refusals, the ordering
```

The day was meant to be the agent arm and the reported table. It became two numbers I had
been carrying without ever checking: a mandate-halt counter that started in the wrong place,
and a token reservation eight times larger than the reply it was reserving for. The first
made the downside metric unable to fire. The second put the submission past its deadline.
Neither was visible in any output I was looking at.

**What broke**

1. **The downside metric was structurally incapable of firing, and read as a result.**

   `halt %` had printed `0.0%` for every arm in every un-jittered run since D2. That is a
   plausible number — it says no arm is destroying subscriptions — and it is also the only
   number that column could ever have printed.

   The world halts a mandate after `mandate_halt_after` consecutive failed presentations,
   which is 4, and it counted them from zero at the start of the run. But a recurring case
   only exists *because* a presentation already failed, and the rail counted that one.
   Starting from zero handed every arm one free failure that reality does not give it, so
   no arm retrying three times could ever reach four.

   Nothing failed. Every test passed. Every invariant held. The column was wrong.

   What found it was not a test but a question asked of a suspiciously round number: *what
   would have to be true for this column to be non-zero, and is that reachable from here?*
   It was not reachable. That question is now the one I ask of any metric that looks stable.

   The fix changed no generated data — the miscount was in the world's runtime state, not
   in anything the generator wrote — so the batches and the committed diagnoses survived it
   untouched. What it changed was the table. Naive's net lift on batch A went from
   **+403,719 to −6,012,174**: it halts 223 of 372 mandates, 59.9% of the recurring book,
   and nine months of forfeited subscription revenue swamp everything it recovered.

   It also flatters the arm I built, which is the reason to be most suspicious of it. The
   defence I would offer a reviewer is that the constant it turns on is jittered ±20% along
   with every other one, and that the earlier sensitivity runs had already produced this
   same tail in 7 of 20 perturbed worlds. The base world was the outlier, not the jittered
   ones.

2. **The retry budget turned out to be a structural choice, not a dial.**

   Fixing the counter re-opened the recurring charge budget, and working out why took
   longer than changing it. The jitter perturbs `mandate_halt_after` by exactly one unit,
   so the rail's threshold is always **3, 4 or 5** — and since a recurring case arrives
   one failed presentation deep, the budget maps directly onto total presentations. There
   is no continuum here. There are three options:

   | budget | presentations | halts when threshold is | recovery | ordering |
   |---|---|---|---|---|
   | 3 | 4 | 3 or 4 — most worlds | — | — |
   | **2** | **3** | **3 — seven of twenty** | **51.7%** | **20/20** |
   | 1 | 2 | never | 47.5% | 16/20 |

   A budget of 3 is what naive does, and it is why naive destroys 59.9% of the book.

   Between 1 and 2 there is a real trade. A budget of 1 has no left tail at all — nothing
   halts in any world, net lift stays inside [+3.38L, +4.14L] — but it gives up 4.2 points
   of recovery in *every* world, including the two-thirds where the tail never appears, and
   it loses to naive outright in the four worlds where the threshold lands on 5 and naive
   gets away with four presentations. That is what drops the ordering to 16 of 20.

   I spent most of an afternoon on 1 before settling on **2**. Writing down why, because
   the reasoning is the part worth keeping: the tail at a budget of 2 is real, but it is
   *reported* — it is the `worlds w/ halt` column of the sensitivity table, immediately
   next to the ordering claim, where nobody can miss it. Trading a disclosed risk for a
   measured gain is a different act from suppressing the risk in order to state the gain,
   and only the second one is dishonest.

   **The thing I got wrong** was assuming the two could be had together. The obvious idea:
   allow the second presentation only when the diagnosed cause is one that *clears on its
   own* — an outage ending, a route recovering, a balance arriving, a cap rolling over —
   and refuse it for a dead card, a revoked mandate, an absent customer, a risk block. A
   second attempt that succeeds resets the rail's counter to zero, so targeting them should
   buy the recovery without the halts.

   It bought nothing:

   | | recovery | doubles | worlds w/ halt | worst halt |
   |---|---|---|---|---|
   | blanket 2 | 51.7% | 4 | 7/20 | 39.5% |
   | selective | 51.5% | 4 | 7/20 | 39.5% |

   Identical to within noise. The reason is obvious once seen and was not obvious before:
   **a second presentation that fails is a strike whatever motivated it.** The halt rate is
   set by how often the second attempt fails, not by why it was made. Picking better causes
   raises the success rate of those attempts, but the failures that remain are still
   consecutive failures, and they are all the counter can see.

   I deleted the code. It earned nothing and it was not free to read. Keeping a mechanism
   because it sounds principled, after measuring that it does nothing, is how a codebase
   fills up with things nobody can safely remove later.

3. **Replaying a batch twice failed with a bare `UNIQUE constraint failed: runs.run_id`.**

   Run ids are deterministic, which is what makes a replay reproducible, and it means the
   second replay of the same arms collides. That is the append-only store refusing to
   rewrite an audit trail, and it is correct — but a raw integrity error three frames deep
   does not say so, and the tempting reading is that the ledger is corrupt.

   The first fix I wrote was wrong and is worth recording: `start_run(replace=True)`,
   deleting the old run's rows. The triggers caught it immediately — `ledger is
   append-only: DELETE on decisions is not permitted`. The schema was defending itself
   against me, which is the entire reason it is enforced by triggers and not by a code
   review convention. A ledger that a bug fix under deadline can quietly edit is not an
   audit trail.

   So `replay --fresh` discards the batch's ledger file and starts a new trail, and the
   collision now prints what happened and both ways out. Nothing rewrites history.

4. **The API key sat in `.env` and nothing loaded it.** The run died on
   `RuntimeError: no Groq API key` with the key sitting in a file one directory up. Nothing
   in the repo reads `.env`; it has to be exported into the shell first, and that is exactly
   the kind of step that lives in one shell's history and nowhere else. It is one line —
   `set -a; . ./.env; set +a` — and it is now written down beside the command it belongs to
   rather than remembered.

5. **The run slept for 25 minutes on a rate limit that had cleared in two.** A 600-case
   pass is an unattended job, so a 429 is waited out rather than treated as fatal, honouring
   `retry-after`. Against a *rolling* daily budget that header is an estimate, and it
   overshoots badly. What made it visible was probing the API by hand while the run was
   asleep on it: the probe went straight through.

   Sleeps are now capped and repeated — wake up, ask again, sleep again — so the run
   resumes when the quota does rather than when a stale estimate says it might. Waking early
   costs one request out of a thousand-a-day allowance.

   The second half of the same fix: a quota wait no longer consumes an attempt from
   `max_attempts`. A 429 is the API working correctly and saying "not yet", which is a
   different kind of failure from a call that broke, and the two were sharing a budget.
   Total quota waiting is bounded by the per-limit ceiling instead.

6. **The batch was paying four thousand tokens a case for output it never used, and the
   timeline built on that was wrong by a factor of four.**

   The run had gone quiet again — nothing written for fourteen minutes — while a hand probe
   of the same API went straight through. Rate limiting and a hung process look identical
   from outside, so the first fix was to make the run say which one it is: every quota wait
   now prints the limit that was hit and what the API said about it.

   What it said was the finding:

   ```
   [tpd] Rate limit reached ... on tokens per day (TPD):
         Limit 200000, Used 198805, Requested 5222
   ```

   Two numbers neither of which I had. The daily ceiling is **200,000 tokens**, and a single
   case was costing **5,222** of them. My working figure had been ~1,560, so the estimate of
   130 cases a day was really **38**, and the 600-case pass was a **7.7-day** job against an
   8-day deadline. It would have finished on the deadline with nothing left for the table,
   the README or the video.

   The cost is not the prompt. It is `max_completion_tokens`, which was 4096 — the free tier
   bills what a request **reserves**, not what it uses. Measured against a live `usage`
   reading rather than guessed at: prompt 1,126, completion **527**. The reservation was
   nearly eight times the reply.

   | reserved | billed/case | cases/day | days for the remaining 293 |
   |---|---|---|---|
   | 4096 | 5,222 | 38 | 7.7 |
   | **1024** | **2,150** | **93** | **3.2** |

   Worth noting what the 527 is made of: the visible JSON is at most 139 tokens across 306
   cached diagnoses, so **388 tokens per case are hidden reasoning**. That is what makes the
   cap dangerous to tighten by eye — the part that needs the headroom is the part you cannot
   see in the output file.

   Which is why the cap did not move on its own. A reply cut off at the ceiling comes back
   as valid JSON that stops mid-object, and `_parse` turns anything unreadable into a
   zero-confidence prediction *by design*, so that hard cases are never silently dropped
   from the accuracy. A truncation would have entered the committed artifact wearing exactly
   the same clothes as a genuine hard case. `finish_reason == "length"` is the only thing
   that distinguishes them, and it is now refused outright rather than recorded.

   1024 is 1.9x the measured completion, with the guard behind it. The request went from
   5,222 tokens to 1,647 and the retry wait from 29 minutes to 11.

   The general lesson is the one from item 1 in a different costume: a number I had not
   measured was load-bearing, and nothing was going to tell me until I asked the right
   question of it. Here the question was *what is one call actually costing?* — and the
   answer only existed inside an error message the run was swallowing.

7. **A Windows console cannot print an em dash.** `reclaim.eval.report` renders markdown,
   markdown here uses `—` and `−`, and printing it raised `UnicodeEncodeError: 'charmap'
   codec can't encode character '−'`. Writing the README was already explicitly UTF-8;
   only the preview to the terminal broke. `sys.stdout.reconfigure(encoding="utf-8")` in the
   entry point.

8. **The fix in item 5 was wrong, and it cost most of an evening.** This is the one I would
   most want a reviewer to read, because the mistake was in the reasoning rather than in
   the code.

   Item 5 replaced a single long sleep with 180-second naps, on the argument that
   `retry-after` overshoots and that waking early to re-ask "costs one request out of a
   thousand-a-day allowance". Checking on the run five hours later: **one case completed in
   four and a half hours.** The burst before that had done 32 in 43 minutes.

   The log said why, once I lined up `Used` across consecutive waits:

   ```
       Used    delta   what moved it
     198277
     199997    +1720   our own probe won
     197977    -2020   refill won
     199189    +1212   our own probe won
     198707     -482   refill won
     ...
   ```

   **A refused request still debits the daily token budget.** The refill returns about 418
   tokens per 180-second nap, at 139 a minute; each probe that comes back 429 spends
   1,200-1,750. Waking early costs roughly five times what the wait recovers, so the budget
   stays pinned against its ceiling and the run starves itself. It was not waiting for
   quota — it was eating the quota it was waiting for.

   That argument is sound for a *request* quota and false for a *token* one, and the
   binding limit here is tokens. `retry-after` is not an estimate to be second-guessed: it
   is the API computing when enough of the rolling window will have freed for this exact
   request. Sleep it. `max_sleep` went 180s → 1800s, and the per-case ceiling 1h → 6h,
   because a saturated budget legitimately owes one case a quarter of an hour and several
   can queue behind each other.

   The evidence I originally reasoned from was real but not comparable. The 25-minute idle
   in item 5 happened at the 4096-token reservation, where `retry-after` for a 5,222-token
   request genuinely was ~29 minutes. And the probe that "went straight through while the
   run was still asleep" was a `max_tokens=1` request — a few hundred tokens against a
   budget with a few hundred to spare. It did not show that the limit had cleared. It
   showed that a much smaller request fitted through a much smaller gap, and I read it as
   the former.

   **The lesson worth keeping: a probe that is cheaper than the workload cannot tell you
   whether the workload will fit.** I have now made the same class of error twice in one
   day - assuming a number rather than measuring the thing that actually binds - and both
   times the fix took minutes once the right quantity was on screen.

9. **The arms were never actually paired, and it was hiding two points of recovery.**

   This was on the deferred list since D4 and I had been describing it in `solution.md` as a
   limitation rather than a bug. Doing it turned out to be fifteen lines.

   Every arm faces a world built with the same calibration and the same seed, which sounds
   like a controlled comparison. It is not quite one: the world held a **single random
   generator**. The moment one arm takes a different number of actions than another — which
   is the entire point of having arms — its draw sequence shifts, and every subsequent case
   sees different randomness than the same case in another arm. Naive averages 2.5 charge
   attempts per case against the policy arm's 1.5, so the two were eating that sequence at
   very different rates.

   Each case now draws from its own stream, seeded from the case id. Common random numbers.
   It changes nothing about what is being estimated, only the noise the estimate carries:

   | | shared generator | per-case streams |
   |---|---|---|
   | policy arm recovery | 51.7% | **53.7%** |
   | policy arm net lift | +424,427 | **+510,124** |
   | net-lift range over 20 worlds | 5.24L wide | **4.50L wide** |
   | double charges over 20 worlds | 3 to 4 | **exactly 4, every world** |

   That last row is what convinced me this is noise removal rather than a lucky draw.
   Whether a particular ambiguous payment gets double-charged ought to be a property of that
   payment, not of how many actions the arm happened to take on the 200 cases before it.
   Under the shared generator it varied between worlds. It no longer does.

   **What broke inside the fix** is a lesson about tests rather than about simulators. My
   first regression test picked one case, charged it after different amounts of unrelated
   activity, and asserted the outcome was stable. It passed. It also passed against the
   *old*, unpaired code — which makes it worthless. Most cases sit at a success probability
   near 0 or 1, where any draw gives the same answer, and I had picked one of them.

   Rewritten to assert over the whole batch, it fails against the old behaviour exactly as
   it should: 5 of 120 untouched cases changed outcome purely because other cases had been
   acted on first. **A regression test that has never been seen to fail is a hypothesis,
   not a test** — running it against the bug costs a minute and is the only thing that
   tells the two apart.

   Worth naming the pattern: this is the third correction today that moved numbers in
   favour of the arm I built. The defence has to be structural rather than rhetorical — the
   technique is standard and unbiased for the difference between arms, and the test defines
   the property without reference to which arm benefits.

**Verified**

```
.venv/Scripts/python -m pytest
    302 passed in 10.15s

.venv/Scripts/python -m reclaim.eval.replay --batch A --arms control,naive,rules --fresh
    arm         recovered   of n     gross Rs    cost Rs  halted   held  double
    control           169    600      515,531          0       0      0       0
    naive             310    600      894,590      6,245     230      0      20
    rules             322    600    1,034,278      8,623       0     62       4

.venv/Scripts/python -m reclaim.eval.metrics --batch A
    arm        rec %   gross Rs  cost Rs  residual     net Rs   net lift  cost/Re  halt % double
    control    28.2%    515,531        0         0    515,531          -        -    0.0%      0
    naive      51.7%    894,590    6,245 6,573,330 -5,684,985 -6,200,516    0.016   61.8%     20
    rules      53.7%  1,034,278    8,623         0  1,025,655   +510,124    0.017    0.0%      4

.venv/Scripts/python -m reclaim.eval.sensitivity --batch A --arms control,naive,rules
    claimed ordering by net lift:  naive < rules < agent
    held in 20/20 trials  (100%)

    arm              net lift Rs, median [min .. max]       doubles  worst halt %  worlds w/ halt
    naive         -6,197,653  [-8,888,408 .. 382,012]   20 [20..20]         71.8%           16/20
    rules            490,325  [-3,983,433 .. 514,979]      4 [4..4]         35.8%            7/20

.venv/Scripts/python -m reclaim.core.guards --batch A
    A - rules   [baseline: measured, not asserted]
      R1  VIOLATED  4 double charges of 758 charge attempts
      R2  HELD  recovered 1,034,278 of 1,845,200 at risk
      R3  HELD  589 contacts to 303 customers
      R4  HELD  589 contacts checked
      R5  HELD  17 opt-outs recorded
      R6  HELD  600/600 cases closed
    1 asserted arm(s): 6/6 held
```

Batch B's diagnosis run passed **308 of 600** as this was written, and is still going. It is
resumable at any point - the command appends and flushes per case, and re-running it skips
whatever is already cached. At the corrected token reservation the remainder is roughly three
days of free-tier budget, against a deadline eight days out.

Today's daily allowance is spent, so it is currently drawing about four cases an hour as the
rolling window frees tokens; it returns to its full rate tomorrow. If the process is gone,
that is the per-case wait ceiling doing its job rather than a failure - restart it.

```
set -a; . ./.env; set +a
.venv/Scripts/python -u -m reclaim.core.diagnose --batch B --provider groq --rpm 4.2
```

**Also built, because it was the only thing not waiting on batch B:** the explainer script,
in `docs/video-script.md`.

Worth recording why it needed a tool. A five-minute cap is a word budget, not an intention —
at a natural pace it is about 800 words — and prose overshoots that without ever looking long
on the page. The first draft ran **980 words**, nearly seven minutes, and read fine. So
`docs/wordcount.py` counts only what is actually spoken, per section, and each section's
timestamp is computed from its own word count rather than guessed. That turns "this feels
about right" into "this section is 45 words over", which is the difference between trimming
everything evenly and cutting the one place that can afford it. It lands at 5:00 at 160 wpm.

Every figure in it sits in a marked slot next to the command that prints it, and the
preamble says outright that today's batch A stand-ins must be replaced before recording.

**One open question closed, without spending any budget on it.**

Cutting the token reservation mid-batch raised a fair objection: are the cases diagnosed at
1024 the same artifact as the 306 done at 4096? Re-diagnosing a case to compare would have
cost budget on the critical path, so the run's own output answered it instead:

```
reserved 4096: 306 cases, accuracy 0.984, 0 unparseable, 0 zero-confidence
reserved 1024:  30 cases, accuracy 0.933, 0 unparseable, 0 zero-confidence
```

The mechanical risk was truncation, and it is cleanly absent — nothing unparseable, no
zero-confidence rows, and the mean rationale is marginally *longer* at the lower cap
(299.7 chars against 293.3). The truncation guard would have raised anyway; it never fired.

The accuracy gap is two errors in thirty. At the old rate you would expect 0.49, and
P(≥2) = 0.086 — not significant, and this is a post-hoc split on a small sample. More to the
point, both errors are the same thing:

```
case_B00311  said ambiguous_debited, truth issuer_technical_decline, conf 0.60
case_B00324  said ambiguous_debited, truth issuer_technical_decline, conf 0.65
```

That is the engineered confusion pair, failing in the **safe** direction — over-calling the
dangerous class costs a reconcile hold, not a duplicate debit — and both sit below the 0.75
bar the charge gate requires, so the policy would not have acted on either. It is the
failure mode this taxonomy was built to produce, not evidence of a degraded call.

Worth re-checking when the batch finishes and the sample is larger. Recorded now so that
the check is a measurement rather than a memory.

**The demo console was run against the corrected figures**, because a change to the
simulator is exactly the sort of thing that leaves a demo quietly reporting last week's
numbers. It serves them live and they match the CLI:

```
control  rec  28.2%  net lift            0  halt   0.0%  doubles 0
naive    rec  52.5%  net lift   -6,012,174  halt  59.9%  doubles 20
rules    rec  51.7%  net lift      424,427  halt   0.0%  doubles 4
```

`/api/case/case_A00440` is worth keeping for the video. It is one of the four cases where
the keyword matcher walked into the trap, and the trail says so without commentary:

```
decision -> charge (pay_A00440_0) -> decision -> closed: reconcile_hold
observed_error: "No confirmation received from SBI. Debit may have been processed."
bank_reference: None
```

The error text is the engineered `ambiguous_debited` / `issuer_technical_decline` pair, the
missing bank reference is the only signal separating them, the stub charged it anyway, and
the ledger records both the charge and the hold. That is the whole thesis of the project in
one case, and it is a screen rather than a claim.

**Next**

The reported table still needs batch B's diagnoses, and nothing else is blocked on them.
When they land: the `agent` arm, the four-arm table on held-out B written into the README by
`reclaim.eval.report --write`, guards for control and agent, and a 20-trial sensitivity run
on B rather than A.

Two things deliberately left open. The ambiguity gate's second condition — validated on A,
reported on a batch that has not been looked at. And the paired-RNG question from D4: every
arm faces an identically seeded world, but the draw sequence diverges as soon as arms take
different numbers of actions, so this is a common-parameter comparison rather than a paired
one. Pairing each case to its own stream would tighten every lift estimate here. It needs a
change inside `synth/outcome.py`, and today already spent one of those.

---

### D6 — Batch B's diagnoses, all 600 · 2026-08-29

**Built**

Nothing new. This was the run finishing: batch B's diagnosis artifact reached **600/600** and
`data/B/diagnoses.jsonl` is now complete and committed. The pass ran in three sittings — 350
by the 27th, 470 earlier today, the last 130 in a single uninterrupted 2,518-second run.

The cause mix over the batch:

```
230  insufficient_funds          35  ambiguous_debited
126  issuer_technical_decline    35  mandate_revoked
 81  auth_abandoned              31  limit_exceeded
 38  psp_routing_failure         16  instrument_invalid
                                  8  risk_declined
```

**What broke**

Nothing broke, but one assumption was wrong in a useful direction. The cost model behind the
schedule said ~2,150 tokens per case against a 200,000/day ceiling — about 93 cases a day,
which is what turned a 250-case remainder into a three-day plan and why the run kept being
parked. Today it covered 143 cases in one day without a single 429. So either the daily
window rolls on a boundary I had not accounted for, or the real per-case bill is well under
the estimate that was derived from a handful of early samples. The practical lesson is that
the ceiling was being treated as a measured constant when it was an inference from a small
sample, and it cost days of scheduling around a limit that never fired.

The resume path held up exactly as designed: three separate runs, each picking up from the
cache in seconds, no case diagnosed twice, and every line parses.

**Verified**

```
wc -l data/B/diagnoses.jsonl          600
python -m reclaim.core.diagnose --batch B --provider groq --rpm 4.2
    130/130  case_B00599 -> insufficient_funds (0.97)
    600 diagnoses in 2518.3s
```

**Next**

The pipeline is unblocked and the rest is mechanical, in order: score the read
(`eval.confusion --batch B --provider groq`), then `replay --batch B --arms all --fresh`,
which is what finally produces the `agent` arm; then `report --batch B --write`, `guards`
(must print 6/6) and a 20-trial `sensitivity` run on B. The video script's figures are batch
A stand-ins and must be swapped for B before anything is recorded.

---

### D7 — The held-out batch broke an invariant, and that is the result · 2026-08-29

**Built**

The reported table. With batch B's diagnoses complete, the rest of the pipeline ran end to
end for the first time on held-out data: confusion, replay of all four arms, guards,
`report --write`, and twenty perturbed worlds.

`eval/report.py` gained a conditional paragraph. The footnote block used to end with
"Invariant R1 exists so this column stays at zero", which stopped being true the moment the
agent arm double-charged once on B. It is now generated from the metrics — the paragraph is
emitted only when the agent arm actually double-charged, so it appears under the batch B
table and stays absent from batch A's, where the column is clean. A hand-written caveat
would have been one edit away from lying; this one cannot be, because it is derived from the
same numbers as the table above it.

**What broke**

**R1 fails once for the agent arm on batch B.** `case_B00106`: truth `ambiguous_debited`,
diagnosed `issuer_technical_decline` at 0.70 confidence, retried, and the retry succeeded —
which is the failure, not the success. On batch A the agent still holds 6/6. This is exactly
what a held-out batch is for, and it is the second time this project has found something by
refusing to report on the batch it tuned against.

The mechanism was documented before it happened, which is the part worth recording. The
double-charge gate fires only when two things are true at once: the diagnosis is below the
0.75 confidence bar, *and* the failure carries a bank reference. This one cleared neither
half of the trap — 0.70 was under the bar, but the attempt had no bank reference, and only
27% of failures in the batch do. `_ambiguity_gate`'s own docstring predicts this residue and
says no gate bolted on afterwards can catch it: nothing observable separates a timeout that
moved money from a timeout that did not. The model's rationale even reasoned *from* the
absent reference to "the issuer did not respond rather than a confirmed debit" — the
inference the world is built to make unreliable.

Two halves of the R1 claim, and only one broke. The structural half held: no attempt was
executed twice, which is what `UNIQUE (run_id, payment_id, attempt_no)` can guarantee. What
failed is the semantic half — a *new* attempt, with its own attempt number and therefore no
constraint to violate, presented against a payment whose money had already moved. A database
constraint cannot see that. The README now separates the two sentences rather than letting
"R1 is structural" carry more weight than it can.

The sensitivity run also came back weaker than batch A's: the claimed ordering
`naive < rules < agent` holds in **16/20** perturbed worlds rather than 20/20. The four
misses are all worlds where the mandate-halt threshold lands high enough that naive stops
destroying mandates, which lets it post a positive net lift and overtake rules. The agent is
still the top arm in 20 of 20. Reported as 16/20, because the claim being made is about the
whole ordering.

**Verified**

```
python -m reclaim.eval.confusion --batch B --provider groq
    n=600  accuracy 0.977  macro-F1 0.961  cost-weighted error 0.038

python -m reclaim.eval.replay --batch B --arms all --fresh
    arm      recovered  gross Rs    halted  double
    control        143   420,657         0       0
    naive          266   791,834       244      26
    rules          273   767,927         0       4
    agent          353 1,017,847         0       1

python -m reclaim.core.guards --batch A      1 asserted arm(s): 6/6 held
python -m reclaim.core.guards --batch B      agent 5/6 - R1 VIOLATED (case_B00106)

python -m reclaim.eval.sensitivity --batch B
    claimed ordering naive < rules < agent    held in 16/20
    agent net lift median Rs 586,963 [-708,059 .. 609,144], doubles 1 [1..1]

python -m pytest                             302 passed
```

Net lift on held-out B: agent **+Rs 586,468**, rules +Rs 337,478, naive **−Rs 5,609,640**
against a control arm that recovers Rs 420,657 by doing nothing at all.

**Next**

The deferred item is now the obvious one: the ambiguity gate's second condition. The honest
version is not "make the gate catch `case_B00106`" — that is fitting to a case I have now
seen in the reported batch. It is to decide, on batch A only, whether a timeout-shaped
description with no bank reference should be treated as possibly-debited by default, measure
what that costs in missed recoveries there, and then report the consequence on B once. The
cost is real and needs stating either way: holding every referenceless timeout would give up
recoveries on the far larger population of plain technical declines.

# Project Progress

An engineering log for this repository: what has been built, what was decided and why, and
what broke along the way. Newest entries at the bottom.

---

## Milestones

| # | Milestone | Status |
|---|---|---|
| D1 | Sealed world — domain model, personas, outcome engine, batch generator | ✅ **done** |
| D2 | Detection, append-only ledger, invariants R1–R6 | ⬅ next |
| D3 | Root-cause diagnosis + labelled evaluation and confusion matrix | ⬜ |
| D4 | Policy engine, budgets, retry scheduler | ⬜ |
| D5 | Executor + Razorpay test-mode integration | ⬜ |
| D6 | Evaluation harness, four arms, metrics, charts | ⬜ |
| D7 | Dashboard + one injected failure handled gracefully | ⬜ |
| D8 | README, ADRs, results writeup | ⬜ |
| D9 | Video | ⬜ |

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

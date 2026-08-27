# reclaim

A bounded revenue-recovery agent for failed payments and failed recurring mandates.

Built for **Razorpay's buildathon, Track 3 — AI Revenue Recovery**, whose bar is not "build
a retry bot":

> Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail.

So the eval harness and the honest baseline are the product. Features are not.

---

## The idea in one paragraph

When a payment fails, the money is often not lost — it is *stuck*, and whether you get it
back depends on whether your next action matches the reason it failed. An empty account
needs a different **time**, not more attempts. An abandoned OTP screen needs the customer
back, not a silent retry. A dead card needs a new card. A broken gateway needs a different
route. And a payment that *may already have been debited* needs you to **not charge it
again** — because that retry succeeds, and the success is a duplicate charge.

`reclaim` classifies each failure into one of nine root causes, and each cause maps to a
materially different bounded action. A system that retries everything three times is wrong
in five directions at once, and the gap between those two things is what this measures.

---

## Results

<!-- RESULTS-TABLE -->

---

## Why you can believe the numbers

Most of the engineering here went into making the evaluation hard to fool.

**The seal.** The simulated world (`reclaim/synth/`) owns ground truth — which cause a
failure really has, when money returns, whether a customer engages. The agent
(`reclaim/core/`) sees only events. **`core/` may never import `synth/`**, and
`tests/test_seal.py` enforces it. If the policy could read the world's parameters it would
be rediscovering constants we wrote down ourselves, and every recovery figure would be
meaningless.

**A control arm that does nothing.** A large share of failed payments recover on their own.
Counting those as agent wins is the central dishonesty this project exists to avoid, so
every headline figure is **lift over control**, never gross recovery.

**A rules-only arm.** `rules` and `agent` run the *same* policy engine and differ only in
which diagnoser fed it — keyword matching versus a model. The gap between them is
attributable to diagnosis quality and nothing else. Almost nobody includes this arm, and it
is the difference between a measured claim and an assumed one.

**Tune on A, report on B.** Two batches, different seeds, shifted failure mix. The reported
table is the held-out one.

**A sensitivity run.** Every calibration constant is perturbed by up to ±20% at once and the
whole comparison re-runs. This is the answer to *"you wrote the simulator, so you picked the
numbers that make your agent win"*: yes, and here is the range over which the **ranking**
survives. It is also what caught the most important design flaw in the project — see
[`solution.md`](solution.md) §10.

**Invariants, asserted after every run.** Not metrics — falsifiable claims:

```
R1  no payment charged more than once        R4  no contact outside permitted hours
R2  no phantom recovery                      R5  no action on a closed or opted-out case
R3  frequency cap holds (3 per 7 days)       R6  no case left non-terminal
```

R1 is **structural**: `UNIQUE (run_id, payment_id, attempt_no)`, insert-first-then-charge.
The tempting `SELECT`-then-`INSERT` has a window in which a redelivered webhook and a retry
both see an empty result and both proceed; the failure mode is a duplicate charge on a real
customer's card. A unique constraint has no window.

The ledger is append-only and that is *enforced*, not promised — every table carries
`BEFORE UPDATE`/`BEFORE DELETE` triggers that abort.

---

## Where the model is, and where it deliberately is not

Exactly one module calls an LLM: `core/diagnose.py`, mapping messy issuer free text to the
nine-way taxonomy. Everything else is deterministic on purpose — **retry timing is a policy,
budgets are arithmetic, and anything that moves money is plain code behind a gate.**

The one question a payments reviewer asks about a recovery agent is what stops it doing
something stupid at 3am. The answer has to be a constant in a file they can read, not a
prompt.

**The model runs once per batch, ever.** Its output is committed to
`data/<batch>/diagnoses.jsonl` and the harness reads that file. This is not a cost
optimisation — a reviewer cloning this repo has **no API key of any kind**, and every number
must still reproduce for them.

---

## Run it

No API key required. Python 3.13.

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

.venv/Scripts/python -m pytest                          # the whole suite
.venv/Scripts/python -m pytest tests/test_seal.py -v    # the import boundary

.venv/Scripts/python -m reclaim.eval.replay      --batch B --arms all
.venv/Scripts/python -m reclaim.eval.metrics     --batch B   # the results table
.venv/Scripts/python -m reclaim.core.guards      --batch B   # invariants R1-R6
.venv/Scripts/python -m reclaim.eval.sensitivity --batch B   # ranking across 20 worlds
```

The ledger is append-only, so replaying a batch that already has a recorded run needs
`--fresh` — it starts a new audit trail rather than overwriting the old one. The table above
is not typed in: `python -m reclaim.eval.report --batch B --write` reads the ledger and
writes it into this file.

A demo console with the per-case audit trail:

```bash
.venv/Scripts/python -m uvicorn reclaim.api.main:app --reload
```

Regenerating a batch (not needed — both are committed):

```bash
.venv/Scripts/python -m reclaim.synth.generator --batch B --seed 20260904
```

---

## Layout

```
reclaim/
  domain.py          shared vocabulary. Types only - no probabilities, no policy
  synth/             THE SEALED WORLD. generator, outcome engine, personas
  core/              THE AGENT. never imports synth/
    feed.py          the only files the agent may open
    detect.py        which failures are worth working, and in what order
    diagnose.py      the one place a model is called
    policy.py        cause -> bounded action. Pure function, no model
    compliance.py    every stopping rule and contact bound, in one readable file
    guards.py        invariants R1-R6, re-derived from the ledger
    ledger.py        append-only audit trail; R1 lives in the schema
  eval/              the only package allowed to import both sides
    replay.py        the four arms
    metrics.py       lift over control, net of cost and residual
    confusion.py     diagnosis scored against ground truth
    report.py        the results table, rendered into the README from the ledger
    sensitivity.py   the same comparison across perturbed worlds
  api/               demo console (vanilla JS, no build step)
```

---

## Further reading

- [`solution.md`](solution.md) — the full approach: the thesis, the seal, the policy engine,
  the sensitivity finding, and an explicit list of what this does *not* do.
- [`progress.md`](progress.md) — the engineering log, including a "what broke" section per
  day. The most useful part of the repo if you want to know how it was actually built.

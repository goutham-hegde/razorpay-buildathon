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

Batch **B** — the held-out batch — the policy was never tuned against it. 600 failed payments and mandates, Rs 1,761,200 at risk. Every figure here was produced by running the commands under [Run it](#run-it); none of it is typed in by hand.

| arm | what it does | rec % | gross Rs | net Rs | net lift Rs | cost/Re lifted | halt % | double |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **control** | no intervention at all | 23.8% | 420,657 | 420,657 | — | — | 0.0% | 0 |
| **naive** | retry immediately, 3x, fixed interval | 44.3% | 791,834 | -5,188,982 | -5,609,640 | 0.019 | 66.1% | 26 |
| **rules** | policy engine, keyword diagnosis, no model | 45.0% | 757,730 | 748,474 | +327,817 | 0.027 | 0.0% | 0 |
| **agent** | policy engine, model diagnosis | 58.2% | 1,006,151 | 995,604 | +574,947 | 0.018 | 0.0% | 0 |

**net** = gross recovered − cost (retry fees, comms, incentives, double-charge unwinds) −
residual, where residual is the future subscription revenue forfeited by halting a mandate.

**net lift** = this arm's net minus the control arm's. The control arm does nothing at all,
and it still recovers money — some payments come back on their own. Every arm's gross figure
contains those recoveries in full, so lift, not gross, is the claim.

**cost/Re lifted** = paise spent per rupee of *incremental* gross recovery. Dividing by gross
would flatter every arm by the control's free recoveries.

**halt %** = share of recurring cases whose mandate the arm destroyed. An arm can win on
gross recovery and lose here; that is the whole reason residual is a column and not a
footnote.

**double** = duplicate charges. `ambiguous_debited` is the case where the customer may
already have been debited and a retry *succeeds*; the success is the liability, and R1 is
the invariant that asserts against it.

<!-- /RESULTS-TABLE -->

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

R1 has two halves, and it is worth separating them, because the held-out batch broke one and
not the other. The structural half is the constraint above: no attempt is ever *executed*
twice. The semantic half is what the guard actually checks — that no new attempt, carrying
its own attempt number and therefore violating no constraint, is presented against a payment
whose money had already moved. A unique index cannot see that.

**The first held-out run failed the semantic half once**, on `case_B00106`: a real
`ambiguous_debited` failure the model read as a technical decline at 0.70 confidence, charged
again, and the retry succeeded. The double-charge gate did not fire, because it weighs the
diagnoser's confidence *and* looks for a bank reference, and that failure carried none.

The fix is a second gate that consults the diagnosis not at all
(`PolicyEngine._post_authorization_veto`): **never present again when the opening failure was
reported at `payment_response`**. That step means the debit instruction had already left our
hands when the silence started — every earlier step failed while the money was still
demonstrably ours. It is a structural question about the failure, so a confident
misdiagnosis cannot talk it out of firing, and the confusion pair this whole project is built
around produces exactly confident misdiagnoses.

It is blunt and the bluntness is priced. A routing failure can also die at
`payment_response` — our own switch lost the answer and the bank moved nothing — and those
are recoverable by re-routing, which this now refuses. The decision was taken on the tuning
batch, before batch B was re-run, and `reclaim/eval/ablation.py` is the measurement:

```
python -m reclaim.eval.ablation --batch A        # arm 'rules', veto off vs on
                       off            on         delta
recovered cases        322           313            -9
gross Rs         1,034,278     1,008,387       -25,891
net lift Rs        510,124       484,781       -25,344
double charges           4             0            -4
```

Nine recoveries and 5.0% of net lift, to move R1 from broken to held. On the reported batch
the same trade cost the `agent` arm four cases and Rs 11,521 of net lift — and the
sensitivity run now reports **0 double charges in all 20 perturbed worlds**, where it
previously reported 1 in every one of them.

What this does *not* show is a rule that generalises. In the world these numbers come from,
`payment_response` is a perfect tell — every generated ambiguous debit carries it and no
other cause is forced to — so the clean catch rate is a fact about how that world was
written, not evidence the rule would be perfect against a real acquirer. What transfers is
the shape: a veto that does not depend on the diagnosis being right.

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
.venv/Scripts/python -m reclaim.eval.ablation    --batch A   # what one policy rule is worth
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
    ablation.py      one rule at a time, on the tuning batch: off vs on
  api/               demo console (vanilla JS, no build step)
docs/
  video-script.md      the explainer, written to be read aloud
  recording-runsheet.md what to have on screen, per section, as deep links
  wordcount.py         what it actually runs to, per section
```

---

## The same track, approached from the other end

While building this I also built [**`preflight`**](https://github.com/goutham-hegde/raz) —
a separate, self-contained system for the same track that tries to stop the failure instead
of recovering from it. It predicts which stored cards and mandates will fail on their *next*
billing cycle and intervenes before the debit is presented, while the cheap remedies (moving
the debit date, switching rails) are still available.

It is a different submission, not a component of this one, and it is worth a look for two
things this repo does not have:

- **An oracle bound.** Because its simulator authors the world, it can compute what a
  perfectly-informed predictor *and* a perfectly-informed policy would have retained under
  the same costs and contact caps. Every arm is then reported as a share of achievable rather
  than against 100% — a ceiling you can check, instead of an accuracy figure you cannot.
- **A headline that disagrees with its own thesis.** The model arm, the model-plus-LLM arm
  and the oracle arm all retain the *same* money. Every cycle the predictor is confident
  about turns out to be one where the failure is already a fact with a free remedy, so
  nothing above the contact floor is reachable by outreach at all. Choosing the right action
  is worth something there; predicting better is worth nothing — and it can show that,
  because perfect prediction is one of the arms.

Both repos are built the same way and hold the same line: a sealed simulator, tune on A and
report on B, a control arm that does nothing, and invariants asserted after every run.

---

## Further reading

- [`solution.md`](solution.md) — the full approach: the thesis, the seal, the policy engine,
  the sensitivity finding, and an explicit list of what this does *not* do.
- [`progress.md`](progress.md) — the engineering log, including a "what broke" section per
  day. The most useful part of the repo if you want to know how it was actually built.
- [`docs/video-script.md`](docs/video-script.md) — the explainer script, with each section's
  timestamp derived from its own word count by `docs/wordcount.py`, and
  [`docs/recording-runsheet.md`](docs/recording-runsheet.md) — every shot in it as a console
  deep link.

"""Replay a batch through one or more arms and write the result to the ledger.

`eval` is the only package permitted to import both sides. It holds a `World` (ground
truth) and drives `core` (the agent), which is what makes it able to score the agent
without the agent ever seeing the answers.

The four arms:

    control   does nothing at all. Establishes how much money comes back on its own, and
              therefore how much of any other arm's number is actually attributable to it.
    naive     retry immediately, three times, fixed interval. The strawman almost every
              real recovery system starts as.
    rules     `core.policy` driven by the offline keyword diagnoser. No model anywhere.
    agent     `core.policy` driven by the model's diagnoses, read from the committed cache.

`rules` and `agent` run the *same* policy engine and differ only in which `Diagnoser`
produced their input, so the gap between them is attributable to diagnosis quality and to
nothing else. That is the whole reason the rules arm exists, and almost nobody else will
include one.

The arms deliberately share this driver so that they differ only in the actions they
choose, never in how those actions are adjudicated.

ON PRICING THINGS THE WORLD DOES NOT PRICE
------------------------------------------
`World.send_contact` raises engagement when an incentive is attached and charges nothing
for it. Left alone that makes incentives free, and a free lever is not a decision - every
arm would attach one to every message. The driver therefore bills
`compliance.INCENTIVE_COST_PAISE` - the agent's own declared figure, not a world constant -
against any case where the policy attached one, and the ledger carries it alongside the
world's own costs. Every other cost in the results table comes from the world.

ON SEEDING
----------
Every arm faces a `World` constructed with the same calibration and the same seed. The
random *draw sequence* still diverges once arms take different numbers of actions, so this
is a common-parameter comparison rather than a fully paired one. Pairing each case to its
own RNG stream would tighten the lift estimate and is a D6 decision; it needs a change
inside `synth.outcome`, which has been frozen since D1 and should not be edited casually.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from functools import partial
from datetime import datetime, timedelta
from pathlib import Path

from reclaim.core import feed
from reclaim.core.compliance import CASE_HORIZON, INCENTIVE_COST_PAISE
from reclaim.core.detect import Detection, detect, summarise, work_queue
from reclaim.core.diagnose import Diagnosis, StubDiagnoser, cache_path, load_diagnoses
from reclaim.core.ledger import Ledger, open_ledger
from reclaim.core.policy import Action, CaseView, PolicyEngine
from reclaim.domain import Case, Customer, Rail, RootCause
from reclaim.synth.outcome import Calibration, GroundTruth, World
from reclaim.synth.personas import Persona

ARMS = ("control", "naive", "rules", "agent")

#: Which diagnosis cache each policy arm reads. `rules` uses the offline keyword matcher,
#: `agent` uses the model. Both go through `core.policy` unchanged.
ARM_PROVIDER = {"rules": "stub", "agent": "groq"}

#: How many actions one case may take before the driver decides the policy is livelocked.
#: Generous - the bounds cap a case at four charges and two contacts - and it raises rather
#: than force-closing, because a silent force-close would satisfy R6 while hiding exactly
#: the kind of bug R6 exists to find.
MAX_ACTIONS_PER_CASE = 24

#: How long after an outbound message its opt-out reply is recorded as effective. See the
#: note in `_contact`; the value only has to be greater than zero.
OPT_OUT_LEARNED_AFTER = timedelta(seconds=1)

#: The naive baseline's entire policy, in two numbers.
NAIVE_MAX_ATTEMPTS = 3
NAIVE_INTERVAL = timedelta(minutes=30)


def load_truths(batch_dir: Path) -> dict[str, GroundTruth]:
    """Read ground truth. Only `eval` may call this."""
    out: dict[str, GroundTruth] = {}
    with (batch_dir / "truth.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not (line := line.strip()):
                continue
            raw = json.loads(line)
            out[raw["case_id"]] = GroundTruth(
                case_id=raw["case_id"],
                root_cause=RootCause(raw["root_cause"]),
                persona=Persona(raw["persona"]),
                funds_return_at=_dt(raw.get("funds_return_at")),
                outage_ends_at=_dt(raw.get("outage_ends_at")),
                healthy_psp=raw.get("healthy_psp"),
                instrument_alive=raw.get("instrument_alive", True),
                mandate_alive=raw.get("mandate_alive", True),
                organic_recovery_at=_dt(raw.get("organic_recovery_at")),
                monthly_value_paise=raw.get("monthly_value_paise", 0),
                typical_ticket_paise=raw.get("typical_ticket_paise", 50000),
            )
    return out


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


@dataclass(slots=True)
class Arm:
    """One arm's execution context."""

    name: str
    run_id: str
    ledger: Ledger
    world: World
    #: Costs the agent incurs that the world does not model. Today that is incentives; see
    #: the module docstring. Kept per case so it lands on the right row of the ledger.
    extra_cost_paise: dict[str, int] = field(default_factory=dict)

    def close(self, case: Case, at: datetime, status_hint: str | None = None) -> None:
        """Write the terminal outcome for a case. Every arm ends here - that is R6.

        `status_hint` lets a policy that deliberately stopped short say so: a case put on
        reconcile hold is a different outcome from one that was chased and failed, and
        collapsing the two would hide the single most valuable thing the agent does. It is
        only a hint - an actual recovery, or an actual double charge, outranks it, because
        those are facts about money and the hint is a statement of intent.
        """
        st = self.world.state[case.id]
        charges = self.ledger.charges(self.run_id, case.id)
        captured = any(c["outcome"] == "captured" and not c["double_charge"] for c in charges)
        double = any(c["double_charge"] for c in charges)

        recovered = st.recovered_at is not None
        if recovered:
            status = "recovered"
            recovered_by = "charge" if captured else "organic"
            recovered_paise = case.amount_paise
        elif double:
            # A "successful" charge on a payment that already stood. Not revenue - a
            # liability that a human now has to unwind.
            status = "reconcile_hold"
            recovered_by = None
            recovered_paise = 0
        else:
            status = status_hint or "abandoned"
            recovered_by = None
            recovered_paise = 0

        self.ledger.record_decision(
            self.run_id, case.id, at, "close", f"case closed as {status}"
        )
        self.ledger.close_case(
            run_id=self.run_id,
            case_id=case.id,
            closed_at=at,
            status=status,
            at_risk_paise=case.amount_paise,
            recovered_paise=recovered_paise,
            recovered_by=recovered_by,
            cost_paise=st.costs_paise + self.extra_cost_paise.get(case.id, 0),
            mandate_halted=st.mandate_halted,
            residual_loss_paise=self.world.residual_loss_paise(case.id),
            is_recurring=case.kind == "recurring" or case.mandate_id is not None,
        )


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def play_control(arm: Arm, case: Case, detection: Detection, horizon: datetime) -> None:
    """Do nothing. Whatever comes back, came back on its own.

    This is the most important arm in the project. Without it, every other arm's recovery
    rate silently includes money that was never at risk of being lost.
    """
    arm.ledger.record_decision(
        arm.run_id,
        case.id,
        case.opened_at,
        "skip",
        "control arm takes no action",
    )
    arm.world.settle_organic(case.id, horizon)
    arm.close(case, horizon)


def play_naive(arm: Arm, case: Case, detection: Detection, horizon: datetime) -> None:
    """Retry immediately, three times, fixed interval, no diagnosis.

    Wrong in both directions on purpose. It burns attempts on empty accounts that cannot
    clear yet, it hammers rails that need a human who is not there, and - the expensive
    one - it retries ambiguous debits and takes the money a second time.
    """
    if not detection.eligible:
        arm.ledger.record_decision(
            arm.run_id, case.id, case.opened_at, "skip", str(detection.reason)
        )
        arm.close(case, horizon)
        return

    at = case.opened_at
    payment_id = case.attempts[-1].id
    for attempt_no in range(1, NAIVE_MAX_ATTEMPTS + 1):
        at = at + NAIVE_INTERVAL
        if at > horizon:
            break
        # Anything that would have come back on its own by now has already come back;
        # charging over the top of it would be recovering money we already had.
        if arm.world.settle_organic(case.id, at):
            break

        arm.ledger.record_decision(
            arm.run_id,
            case.id,
            at,
            "retry",
            f"naive fixed retry {attempt_no}/{NAIVE_MAX_ATTEMPTS}",
            payload={"interval_minutes": int(NAIVE_INTERVAL.total_seconds() // 60)},
        )
        claim = arm.ledger.claim_charge(
            run_id=arm.run_id,
            case_id=case.id,
            payment_id=payment_id,
            attempt_no=attempt_no,
            at=at,
            rail=str(case.rail),
            psp=case.psp,
            amount_paise=case.amount_paise,
        )
        before = arm.world.state[case.id].costs_paise
        result = arm.world.attempt_charge(
            case.id, at, case.rail, case.psp, case.amount_paise
        )
        arm.ledger.settle_charge(
            claim,
            captured=result.succeeded,
            at=at,
            double_charge=result.double_charge,
            cost_paise=arm.world.state[case.id].costs_paise - before,
            note=result.note,
        )
        if result.succeeded:
            break

    arm.world.settle_organic(case.id, horizon)
    arm.close(case, horizon)


# ---------------------------------------------------------------------------
# The policy arms
# ---------------------------------------------------------------------------


def load_arm_diagnoses(arm: str, batch, root: Path | str) -> dict[str, Diagnosis]:
    """The diagnosis cache an arm runs on.

    `agent` insists on the committed file and refuses to fall back to the stub. A silent
    fallback would let the agent arm quietly become the rules arm and still be reported as
    the agent, which is the single most misleading thing this harness could do.

    `rules` may compute its own, because the keyword matcher is deterministic, needs no
    network and no key, and therefore cannot differ from the committed copy.
    """
    provider = ARM_PROVIDER[arm]
    path = cache_path(batch.dir, provider)
    cached = load_diagnoses(path)

    if arm == "rules":
        stub = StubDiagnoser()
        return {c.id: cached.get(c.id) or stub.diagnose(c) for c in batch.cases}

    missing = [c.id for c in batch.cases if c.id not in cached]
    if missing:
        raise FileNotFoundError(
            f"{path} covers {len(cached)}/{len(batch.cases)} cases of batch {batch.name}; "
            f"{len(missing)} are missing (first: {missing[0]}). The agent arm reads the "
            f"committed model diagnoses and will not silently substitute the keyword stub. "
            f"Run: python -m reclaim.core.diagnose --batch {batch.name} "
            f"--provider {provider}"
        )
    return cached


def _view(
    arm: Arm,
    case: Case,
    detection: Detection,
    diagnosis: Diagnosis,
    now: datetime,
    customer: Customer | None,
    known_psps: tuple[str, ...],
    engaged_at: datetime | None,
    reauth_requested: bool,
) -> CaseView:
    """Assemble what the policy is allowed to know, entirely from the ledger and the batch.

    Nothing here is read from the `World`. That is not a convention - the policy would
    happily accept a leaked field and the recovery numbers would quietly stop meaning
    anything, so the assembly lives in one short function that can be read end to end.
    """
    charges = arm.ledger.charges(arm.run_id, case.id)
    contacts = (
        arm.ledger.contacts(arm.run_id, customer.id) if customer else []
    )
    return CaseView(
        case=case,
        detection=detection,
        diagnosis=diagnosis,
        now=now,
        charge_attempts=len(charges),
        psps_tried=(case.psp,) + tuple(c["psp"] for c in charges),
        contacts_sent=arm.ledger.contact_count(arm.run_id, case.id),
        customer_contact_times=tuple(datetime.fromisoformat(c["at"]) for c in contacts),
        engaged_at=engaged_at,
        reauth_requested=reauth_requested,
        opted_out=bool(customer and customer.opted_out)
        or (customer is not None and customer.id in arm.ledger.opt_outs(arm.run_id)),
        channels=tuple(customer.contactable_channels) if customer else (),
        salary_day=customer.salary_day if customer else None,
        preferred_rail=customer.preferred_rail if customer else None,
        known_psps=known_psps,
    )


def play_policy(
    arm: Arm,
    case: Case,
    detection: Detection,
    horizon: datetime,
    engine: PolicyEngine,
    diagnoses: dict[str, Diagnosis],
    customers: dict[str, Customer],
    known_psps: tuple[str, ...],
) -> None:
    """Drive `core.policy` over one case until it reaches a terminal action.

    The loop is the honest part. The policy is asked for one action, that action is
    executed against the world, and the *result* goes back into the next `CaseView` - so
    the sequence a case actually gets depends on what happened, not on a plan written
    before anything was tried.
    """
    diagnosis = diagnoses[case.id]
    customer = customers.get(case.customer_id)
    now = case.opened_at
    engaged_at: datetime | None = None
    reauth_requested = False
    payment_id = case.attempts[-1].id if case.attempts else case.id

    for _ in range(MAX_ACTIONS_PER_CASE):
        view = _view(
            arm, case, detection, diagnosis, now, customer, known_psps,
            engaged_at, reauth_requested,
        )
        action = engine.next_action(view)

        arm.ledger.record_decision(
            arm.run_id,
            case.id,
            action.at,
            action.kind,
            action.reason,
            diagnosis=str(diagnosis.root_cause),
            confidence=diagnosis.confidence,
            payload=_payload(action),
        )

        if action.terminal:
            if action.kind == "escalate":
                arm.world.escalate_to_human(case.id)
            arm.world.settle_organic(case.id, horizon)
            arm.close(case, horizon, status_hint=action.status)
            return

        if action.at < now:
            raise RuntimeError(
                f"{case.id}: policy scheduled {action.kind} at {action.at}, before the "
                f"current clock {now} - the loop would not terminate"
            )
        now = action.at

        if action.moves_money:
            # Anything that was coming back on its own has already come back by now.
            # Charging over the top of it would be collecting money we already hold.
            if arm.world.settle_organic(case.id, now):
                arm.close(case, horizon)
                return
            if _charge(arm, case, action, payment_id, view.charge_attempts + 1):
                arm.close(case, horizon)
                return
        elif action.kind == "contact":
            engaged_at, stop = _contact(arm, case, action, customer, view.contacts_sent + 1)
            if stop:
                arm.world.settle_organic(case.id, horizon)
                arm.close(case, horizon)
                return
            if action.with_incentive:
                arm.extra_cost_paise[case.id] = (
                    arm.extra_cost_paise.get(case.id, 0) + INCENTIVE_COST_PAISE
                )
            if action.template == "reauthorise":
                reauth_requested = True
        else:
            raise RuntimeError(f"{case.id}: unknown action kind {action.kind!r}")

    raise RuntimeError(
        f"{case.id}: policy took {MAX_ACTIONS_PER_CASE} actions without terminating"
    )


def _payload(action: Action) -> dict | None:
    fields = {
        "psp": action.psp,
        "rail": str(action.rail) if action.rail else None,
        "channel": action.channel,
        "template": action.template,
        "with_incentive": action.with_incentive or None,
        "status": action.status if action.terminal else None,
    }
    out = {k: v for k, v in fields.items() if v is not None}
    return out or None


def _charge(arm: Arm, case: Case, action: Action, payment_id: str, attempt_no: int) -> bool:
    """Claim, charge, settle. Returns True if the case is now recovered.

    Insert-first: the claim row is written before any money moves, so a duplicate attempt
    is refused by a UNIQUE constraint rather than by a check with a race window in it.
    """
    claim = arm.ledger.claim_charge(
        run_id=arm.run_id,
        case_id=case.id,
        payment_id=payment_id,
        attempt_no=attempt_no,
        at=action.at,
        rail=str(action.rail or case.rail),
        psp=action.psp or case.psp,
        amount_paise=case.amount_paise,
    )
    before = arm.world.state[case.id].costs_paise
    result = arm.world.attempt_charge(
        case.id, action.at, action.rail or case.rail, action.psp or case.psp, case.amount_paise
    )
    arm.ledger.settle_charge(
        claim,
        captured=result.succeeded,
        at=action.at,
        double_charge=result.double_charge,
        cost_paise=arm.world.state[case.id].costs_paise - before,
        note=result.note,
    )
    return result.succeeded


def _contact(
    arm: Arm, case: Case, action: Action, customer: Customer | None, contact_no: int
) -> tuple[datetime | None, bool]:
    """Send one message. Returns (engaged_at, stop), where stop means the case is over.

    A customer who opts out here ends the case immediately and irreversibly. That is R5,
    and it is the one outcome the policy is not permitted to weigh against revenue.
    """
    if customer is None:
        return None, True
    result = arm.world.send_contact(
        case.id, action.at, action.channel or "sms", with_incentive=action.with_incentive
    )
    arm.ledger.record_contact(
        run_id=arm.run_id,
        case_id=case.id,
        customer_id=customer.id,
        contact_no=contact_no,
        at=action.at,
        channel=action.channel or "sms",
        template=action.template or "generic",
        delivered=result.delivered,
        engaged=result.engaged,
        with_incentive=action.with_incentive,
        cost_paise=0,  # the world bills contact cost into the case's own cost total
    )
    if result.opted_out_now:
        # An opt-out is an *inbound* event: the customer received this message and replied
        # STOP. It therefore cannot be effective at the instant the outbound went out, and
        # recording it there would claim we knew before we could have - which reads to R5
        # as the outbound itself having been sent after the opt-out. Inbound latency is not
        # modelled, so it is recorded at the earliest instant strictly after the send.
        arm.ledger.record_opt_out(
            arm.run_id, customer.id, action.at + OPT_OUT_LEARNED_AFTER, "outreach fatigue"
        )
        arm.ledger.record_decision(
            arm.run_id,
            case.id,
            action.at,
            "close",
            "customer opted out in response to outreach; no further contact is permitted",
        )
        return None, True
    return (action.at if result.engaged else None), False


_PLAYERS = {"control": play_control, "naive": play_naive}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def replay(
    batch: str,
    arms: list[str],
    root: Path | str = "data",
    calibration: Calibration | None = None,
    ledger: Ledger | None = None,
    tag: str = "",
) -> dict[str, str]:
    """Run `arms` over `batch`. Returns {arm: run_id}."""
    b = feed.load_batch(batch, root)
    truths = load_truths(b.dir)
    seed = int(b.meta.get("seed", 0))

    detections = {d.case_id: d for d in detect(b.cases, b.customers, b.mandates)}
    queue = work_queue(list(detections.values()))
    horizons = {c.id: c.opened_at + CASE_HORIZON for c in b.cases}
    run_horizon = max(horizons.values())

    # The routes this business can present through at all, read off the batch rather than
    # hard-coded. The policy needs it to re-route away from a broken PSP; it is a fact
    # about our own acquiring setup, not about the world.
    known_psps = tuple(sorted({c.psp for c in b.cases}))
    engine = PolicyEngine(known_psps)

    owned = ledger is None
    ledger = ledger or open_ledger(b.name, root)
    run_ids: dict[str, str] = {}
    try:
        for name in arms:
            if name not in _PLAYERS and name not in ARM_PROVIDER:
                raise ValueError(
                    f"unknown arm {name!r}; known arms are "
                    f"{sorted(set(_PLAYERS) | set(ARM_PROVIDER))}"
                )
            run_id = f"{b.name}-{name}" + (f"-{tag}" if tag else "")
            ledger.start_run(
                run_id,
                b.name,
                name,
                seed=seed,
                notes=json.dumps(summarise(list(detections.values())), separators=(",", ":")),
            )
            # Each arm gets its own World, identically parameterised and identically
            # seeded, so the arms differ only in what they choose to do.
            arm = Arm(name, run_id, ledger, World(truths, seed=seed, calibration=calibration))
            if name in ARM_PROVIDER:
                diagnoses = load_arm_diagnoses(name, b, root)
                play = partial(
                    play_policy,
                    engine=engine,
                    diagnoses=diagnoses,
                    customers=b.customers,
                    known_psps=known_psps,
                )
            else:
                play = _PLAYERS[name]

            # Eligible cases first, in priority order; everything else is still closed,
            # because R6 does not exempt a case just because we chose not to work it.
            for detection in queue:
                case = b.case(detection.case_id)
                play(arm, case, detection, horizons[case.id])
            worked = {d.case_id for d in queue}
            for case in b.cases:
                if case.id not in worked:
                    arm.ledger.record_decision(
                        run_id,
                        case.id,
                        case.opened_at,
                        "skip",
                        detections[case.id].reason,
                    )
                    arm.close(case, horizons[case.id])

            ledger.finish_run(run_id, run_horizon, len(b.cases), len(queue))
            run_ids[name] = run_id
    finally:
        if owned:
            ledger.close()
    return run_ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay a batch through the recovery arms.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--arms", default="all", help="comma-separated, or 'all'")
    ap.add_argument("--root", default="data")
    ap.add_argument("--tag", default="", help="suffix for the run id, to keep runs apart")
    args = ap.parse_args()

    arms = list(ARMS) if args.arms == "all" else [a.strip() for a in args.arms.split(",")]
    b = feed.load_batch(args.batch, args.root)
    detections = detect(b.cases, b.customers, b.mandates)

    print(f"batch {b.name}  n={len(b.cases)}  at risk Rs {b.at_risk_paise / 100:,.0f}")
    for disposition, n in sorted(summarise(detections).items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {disposition}")
    print()

    run_ids = replay(args.batch, arms, args.root, tag=args.tag)

    with open_ledger(b.name, args.root) as ledger:
        # `held` and `double` are deliberately separate columns. A case put on reconcile
        # hold is the agent declining to charge something it could not vouch for; a double
        # charge is the money actually taken twice. Both close as `reconcile_hold`, and
        # reporting them in one column would let the arm that avoids the mistake look
        # identical to the arm that makes it.
        print(f"{'arm':<10}{'recovered':>11}{'of n':>7}{'gross Rs':>13}{'cost Rs':>11}"
              f"{'halted':>8}{'held':>7}{'double':>8}")
        for name, run_id in run_ids.items():
            rows = ledger.outcomes(run_id)
            rec = [r for r in rows if r["status"] == "recovered"]
            gross = sum(r["recovered_paise"] for r in rows)
            cost = sum(r["cost_paise"] for r in rows)
            halted = sum(r["mandate_halted"] for r in rows)
            double = sum(
                1 for c in ledger.charges(run_id) if c["double_charge"]
            )
            held = sum(1 for r in rows if r["status"] == "reconcile_hold")
            print(
                f"{name:<10}{len(rec):>11}{len(rows):>7}{gross / 100:>13,.0f}"
                f"{cost / 100:>11,.0f}{halted:>8}{held - double:>7}{double:>8}"
            )
    print("\nrun `python -m reclaim.core.guards --batch " + b.name + "` to assert R1-R6")
    return 0


if __name__ == "__main__":
    sys.exit(main())

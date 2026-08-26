"""Replay a batch through one or more arms and write the result to the ledger.

`eval` is the only package permitted to import both sides. It holds a `World` (ground
truth) and drives `core` (the agent), which is what makes it able to score the agent
without the agent ever seeing the answers.

Arms implemented here so far:

    control   does nothing at all. Establishes how much money comes back on its own, and
              therefore how much of any other arm's number is actually attributable to it.
    naive     retry immediately, three times, fixed interval. The strawman almost every
              real recovery system starts as.

`rules` and `agent` arrive in D4. The arms deliberately share this driver so that they
differ only in the actions they choose, never in how those actions are adjudicated.

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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from reclaim.core import feed
from reclaim.core.compliance import CASE_HORIZON
from reclaim.core.detect import Detection, detect, summarise, work_queue
from reclaim.core.ledger import Ledger, open_ledger
from reclaim.domain import Case, RootCause
from reclaim.synth.outcome import Calibration, GroundTruth, World
from reclaim.synth.personas import Persona

ARMS = ("control", "naive")

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

    def close(self, case: Case, at: datetime) -> None:
        """Write the terminal outcome for a case. Every arm ends here - that is R6."""
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
            status = "abandoned"
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
            cost_paise=st.costs_paise,
            mandate_halted=st.mandate_halted,
            residual_loss_paise=self.world.residual_loss_paise(case.id),
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

    owned = ledger is None
    ledger = ledger or open_ledger(b.name, root)
    run_ids: dict[str, str] = {}
    try:
        for name in arms:
            if name not in _PLAYERS:
                raise ValueError(f"unknown arm {name!r}; known arms are {sorted(_PLAYERS)}")
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
        print(f"{'arm':<10}{'recovered':>11}{'of n':>7}{'gross Rs':>13}{'cost Rs':>11}"
              f"{'halted':>8}{'double':>8}")
        for name, run_id in run_ids.items():
            rows = ledger.outcomes(run_id)
            rec = [r for r in rows if r["status"] == "recovered"]
            gross = sum(r["recovered_paise"] for r in rows)
            cost = sum(r["cost_paise"] for r in rows)
            halted = sum(r["mandate_halted"] for r in rows)
            double = sum(1 for r in rows if r["status"] == "reconcile_hold")
            print(
                f"{name:<10}{len(rec):>11}{len(rows):>7}{gross / 100:>13,.0f}"
                f"{cost / 100:>11,.0f}{halted:>8}{double:>8}"
            )
    print("\nrun `python -m reclaim.core.guards --batch " + b.name + "` to assert R1-R6")
    return 0


if __name__ == "__main__":
    sys.exit(main())

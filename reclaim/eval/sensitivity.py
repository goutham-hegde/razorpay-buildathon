"""Re-run the whole comparison with every world constant moved at once.

THE QUESTION THIS ANSWERS
-------------------------
"You wrote the simulator, so you chose the numbers that make your agent win."

That objection is correct and cannot be argued away. `synth.Calibration` holds around two
dozen probabilities, costs and thresholds, and they are order-of-magnitude anchors from
public sources rather than measurements. Any single reported figure inherits all of that
uncertainty.

What can be defended is something weaker and more useful: that the *ranking of the arms*
does not depend on those choices. So this module perturbs every constant simultaneously -
`synth.outcome.jitter` moves each probability and cost by up to +/- pct and shifts each
integer threshold by at least a whole unit - replays all four arms against the perturbed
world, and reports how often the ordering survives.

A finding that holds across 20 randomly perturbed worlds is a different kind of claim from
a finding measured once. It is still not a measurement of reality. It is a statement that
the conclusion is not an artifact of the specific constants, which is the strongest thing a
simulation can honestly say about itself.

WHY IT PERTURBS EVERYTHING AT ONCE
----------------------------------
Moving one constant at a time and reporting that the answer held is the weaker test, and
the flattering one: real uncertainty is correlated, and the failure mode worth finding is
two constants moving together. Jittering wholesale is also the only version that can
surface the interactions - `mandate_halt_after` dropping while `p_nsf_before_funds` rises
is exactly the world in which an aggressive retry policy starts destroying subscriptions,
and no one-at-a-time sweep would ever construct it.

EACH TRIAL GETS ITS OWN IN-MEMORY LEDGER
----------------------------------------
Trials are not written to the batch's ledger. That file is the audit trail behind the
reported run, and filling it with twenty perturbed replays would mean the numbers in the
report could no longer be reproduced from it by anyone who did not know which run ids to
ignore.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from reclaim.core.ledger import Ledger
from reclaim.eval.metrics import ArmMetrics, batch_metrics
from reclaim.eval.replay import ARMS, replay
from reclaim.synth.outcome import DEFAULT_CALIBRATION, jitter

#: How far every constant is allowed to move. 20% is wide for a probability that was
#: anchored to published data and narrow for one that was a judgement call, which is
#: roughly the right compromise for moving all of them together.
DEFAULT_PCT = 0.20

#: The ordering the report claims. A trial "holds" if net lift is monotonically increasing
#: along this sequence. Stated up front, before any trial runs, because a ranking chosen
#: after seeing the results is not a prediction.
CLAIMED_ORDER = ("naive", "rules", "agent")


@dataclass(frozen=True, slots=True)
class Trial:
    seed: int
    #: {arm: net lift over control, in paise}
    net_lift: dict[str, int]
    double_charges: dict[str, int]
    halt_rate: dict[str, float]

    @property
    def order_held(self) -> bool:
        present = [a for a in CLAIMED_ORDER if a in self.net_lift]
        lifts = [self.net_lift[a] for a in present]
        return all(x < y for x, y in zip(lifts, lifts[1:]))

    @property
    def ranking(self) -> tuple[str, ...]:
        return tuple(sorted(self.net_lift, key=lambda a: -self.net_lift[a]))


def run_trial(batch: str, arms: list[str], root: Path | str, seed: int, pct: float) -> Trial:
    """One perturbed world, all arms, metrics computed and thrown away."""
    cal = jitter(DEFAULT_CALIBRATION, random.Random(seed), pct)
    with Ledger(":memory:") as ledger:
        replay(batch, arms, root, calibration=cal, ledger=ledger, tag=f"s{seed}")
        metrics = {m.arm: m for m in batch_metrics(ledger, batch)}
    return Trial(
        seed=seed,
        net_lift={a: m.lift_net_paise for a, m in metrics.items() if a != "control"},
        double_charges={a: m.double_charges for a, m in metrics.items()},
        halt_rate={a: m.mandate_halt_rate for a, m in metrics.items()},
    )


def run(
    batch: str,
    arms: list[str] | None = None,
    root: Path | str = "data",
    trials: int = 20,
    pct: float = DEFAULT_PCT,
    base_seed: int = 9001,
) -> list[Trial]:
    arms = arms or list(ARMS)
    return [
        run_trial(batch, arms, root, base_seed + i, pct) for i in range(trials)
    ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _spread(values: list[float]) -> str:
    if not values:
        return "-"
    lo, hi = min(values), max(values)
    return f"{statistics.median(values):>10,.0f}  [{lo:,.0f} .. {hi:,.0f}]"


def render(trials: list[Trial], pct: float) -> str:
    if not trials:
        return "no trials"
    arms = [a for a in CLAIMED_ORDER if a in trials[0].net_lift]
    held = sum(t.order_held for t in trials)

    lines = [
        f"{len(trials)} perturbed worlds, every calibration constant moved by up to "
        f"+/-{pct:.0%}",
        "",
        f"claimed ordering by net lift:  {' < '.join(CLAIMED_ORDER)}",
        f"held in {held}/{len(trials)} trials  ({held / len(trials):.0%})",
        "",
        f"{'arm':<9}{'net lift Rs, median [min .. max]':>40}{'doubles':>14}"
        f"{'worst halt %':>14}{'worlds w/ halt':>16}",
        "-" * 93,
    ]
    for arm in arms:
        lift = [t.net_lift[arm] / 100 for t in trials]
        dbl = [t.double_charges.get(arm, 0) for t in trials]
        halt = [t.halt_rate.get(arm, 0.0) * 100 for t in trials]
        # Mandate halting is bimodal - an arm either sits under the rail's threshold in a
        # given world or it does not - so a median is close to meaningless for it. The
        # worst case and the share of worlds where it happens at all are the honest pair.
        halted_in = sum(h > 0 for h in halt)
        lines.append(
            f"{arm:<9}{_spread(lift):>40}"
            f"{f'{statistics.median(dbl):.0f} [{min(dbl)}..{max(dbl)}]':>14}"
            f"{max(halt):>13.1f}%"
            f"{f'{halted_in}/{len(trials)}':>16}"
        )

    if held < len(trials):
        lines.append("")
        lines.append("trials where the ordering did not hold:")
        for t in trials:
            if not t.order_held:
                lifts = "  ".join(f"{a}={t.net_lift[a] / 100:,.0f}" for a in arms)
                lines.append(f"  seed {t.seed}: {lifts}")

    lines.append("")
    lines.append(
        "Read this as a statement about the ranking, not about the levels. The median net"
    )
    lines.append(
        "lift is not a forecast - it is the middle of a distribution generated by a"
    )
    lines.append(
        "simulator whose constants are anchors rather than measurements. What is being"
    )
    lines.append(
        "claimed is only that the ordering of the arms is not an artifact of those choices."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-run the arm comparison across perturbed worlds."
    )
    ap.add_argument("--batch", default="B")
    ap.add_argument("--root", default="data")
    ap.add_argument("--arms", default="all", help="comma-separated, or 'all'")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--pct", type=float, default=DEFAULT_PCT)
    ap.add_argument("--seed", type=int, default=9001)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    arms = list(ARMS) if args.arms == "all" else [a.strip() for a in args.arms.split(",")]
    if "control" not in arms:
        # Without it there is no lift, only gross recovery, and the whole point of the
        # exercise is that gross recovery is not a claim about anything.
        arms = ["control", *arms]

    print(f"batch {args.batch.upper()}  {args.trials} trials  jitter +/-{args.pct:.0%}")
    print("(each trial replays every arm against its own perturbed world)\n", flush=True)
    trials = run(args.batch, arms, args.root, args.trials, args.pct, args.seed)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "seed": t.seed,
                        "net_lift_paise": t.net_lift,
                        "double_charges": t.double_charges,
                        "halt_rate": t.halt_rate,
                        "order_held": t.order_held,
                    }
                    for t in trials
                ],
                indent=2,
            )
        )
        return 0

    print(render(trials, args.pct))
    return 0


if __name__ == "__main__":
    sys.exit(main())

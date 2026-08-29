"""Measure what one policy rule is worth, by running the batch with it and without it.

This exists for a single question that `eval.replay` cannot answer: the results table says
what the shipped policy did, and says nothing about what any individual rule in it
contributed. A rule that never fires and a rule that fires and pays for itself look
identical from the outside.

The rule under test is `PolicyEngine._post_authorization_veto` - refuse to present again
whenever the opening failure was reported at `payment_response`, because at that step the
debit instruction had already been sent and nothing observable says whether money moved.

WHY THIS RUNS ON BATCH A
------------------------
Batch B is the held-out batch and the one the README reports. The D7 run charged
`case_B00106` twice, and the tempting response is to write a rule that catches it and then
show the improvement on B - which would be fitting to a case already seen in the reported
batch, and would make every number around it a claim about hindsight.

So the decision is taken here, on A, on evidence available before B is looked at again. B
gets run once afterwards and whatever it says is what gets reported, including if the rule
costs more there than it does here.

WHAT IT CANNOT MEASURE
----------------------
Batch A has no `groq` diagnosis cache - the model artifact was only ever built for B - so
the `agent` arm cannot be replayed here at all. The vehicle is the `rules` arm, whose
`StubDiagnoser` cache is complete for A. That is a real limitation and it cuts in a
specific direction: the veto ignores the diagnosis entirely, so its *cost* (recoveries
given up on cases it holds) transfers between arms, while its *benefit* depends on how
often the diagnoser was about to be wrong, and the stub is wrong more often than the model.
Read the cost side as informative and the benefit side as an upper bound.

    python -m reclaim.eval.ablation --batch A
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from reclaim.core.ledger import Ledger
from reclaim.core.policy import PolicyEngine
from reclaim.domain import ErrorStep
from reclaim.core import feed
from reclaim.eval.metrics import ArmMetrics, batch_metrics, render
from reclaim.eval.replay import replay

#: Arms worth running. `agent` is deliberately absent - see the module docstring.
ABLATION_ARMS = ("control", "naive", "rules")

VARIANTS = {"off": False, "on": True}


# ---------------------------------------------------------------------------
# The static side: how many cases the rule can possibly touch, and what they are
# ---------------------------------------------------------------------------


def exposure(batch: str, root: Path | str = "data") -> dict[str, Counter]:
    """Cases whose opening failure sits at `payment_response`, by true root cause.

    Reads `truth.jsonl`, which is why this lives in `eval/` and not in `core/`. It is the
    honest statement of the trade before any arm is run: every cause other than
    `ambiguous_debited` in the `held` column is a case the veto refuses to work.
    """
    b = feed.load_batch(batch, root)
    truth: dict[str, str] = {}
    with (b.dir / "truth.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line := line.strip():
                row = json.loads(line)
                truth[row["case_id"]] = row["root_cause"]

    held: Counter = Counter()
    total: Counter = Counter()
    for case in b.cases:
        cause = truth[case.id]
        total[cause] += 1
        attempt = case.attempts[-1] if case.attempts else None
        if attempt and attempt.error and attempt.error.step is ErrorStep.PAYMENT_RESPONSE:
            held[cause] += 1
    return {"held": held, "total": total}


def render_exposure(counts: dict[str, Counter]) -> str:
    held, total = counts["held"], counts["total"]
    lines = [
        f"{'true root cause':<26}{'in batch':>9}{'at payment_response':>21}{'share':>8}",
        "-" * 64,
    ]
    for cause in sorted(total, key=lambda c: (-held[c], c)):
        n = held[cause]
        lines.append(
            f"{cause:<26}{total[cause]:>9}{n:>21}{n / total[cause] * 100:>7.1f}%"
        )
    lines.append("-" * 64)
    lines.append(f"{'all':<26}{sum(total.values()):>9}{sum(held.values()):>21}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The dynamic side: replay the batch under both settings
# ---------------------------------------------------------------------------


def run_variants(
    batch: str,
    arms: list[str],
    root: Path | str = "data",
) -> dict[str, list[ArmMetrics]]:
    """Replay `arms` once per setting of the veto. Returns {variant: metrics}.

    Each variant gets its own in-memory ledger. Nothing here touches `data/<batch>/ledger.db`
    - the reported audit trail is written by `eval.replay` and by nothing else, and an
    ablation that could overwrite it would be one command away from putting an unshipped
    policy variant in the README.
    """
    b = feed.load_batch(batch, root)
    known_psps = tuple(sorted({c.psp for c in b.cases}))

    out: dict[str, list[ArmMetrics]] = {}
    for variant, enabled in VARIANTS.items():
        ledger = Ledger(":memory:")
        try:
            replay(
                batch,
                arms,
                root,
                ledger=ledger,
                tag=variant,
                engine=PolicyEngine(known_psps, post_authorization_hold=enabled),
            )
            out[variant] = batch_metrics(ledger, b.name)
        finally:
            ledger.close()
    return out


def render_delta(results: dict[str, list[ArmMetrics]], arm: str) -> str:
    """The line that answers the question: what did turning the rule on cost and buy."""
    def pick(variant: str) -> ArmMetrics | None:
        return next((m for m in results[variant] if m.arm == arm), None)

    off, on = pick("off"), pick("on")
    if off is None or on is None:
        return f"{arm}: not run under both settings"

    rows = [
        ("recovered cases", f"{off.recovered}", f"{on.recovered}", f"{on.recovered - off.recovered:+d}"),
        ("gross Rs", f"{off.gross_paise / 100:,.0f}", f"{on.gross_paise / 100:,.0f}",
         f"{(on.gross_paise - off.gross_paise) / 100:+,.0f}"),
        ("net Rs", f"{off.net_paise / 100:,.0f}", f"{on.net_paise / 100:,.0f}",
         f"{(on.net_paise - off.net_paise) / 100:+,.0f}"),
        ("net lift Rs", f"{off.lift_net_paise / 100:,.0f}", f"{on.lift_net_paise / 100:,.0f}",
         f"{(on.lift_net_paise - off.lift_net_paise) / 100:+,.0f}"),
        ("double charges", f"{off.double_charges}", f"{on.double_charges}",
         f"{on.double_charges - off.double_charges:+d}"),
        ("mandates halted", f"{off.mandates_halted}", f"{on.mandates_halted}",
         f"{on.mandates_halted - off.mandates_halted:+d}"),
        ("charge attempts", f"{off.charge_attempts}", f"{on.charge_attempts}",
         f"{on.charge_attempts - off.charge_attempts:+d}"),
    ]
    width = max(len(r[0]) for r in rows)
    lines = [
        f"arm '{arm}': post-authorization veto off vs on",
        f"{'':<{width}}{'off':>14}{'on':>14}{'delta':>14}",
        "-" * (width + 42),
    ]
    lines += [f"{label:<{width}}{a:>14}{b:>14}{d:>14}" for label, a, b, d in rows]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure the post-authorization veto by replaying a batch with it off "
        "and on."
    )
    ap.add_argument("--batch", default="A", help="the tuning batch; not B")
    ap.add_argument("--arms", default=",".join(ABLATION_ARMS))
    ap.add_argument("--root", default="data")
    ap.add_argument("--arm", default="rules", help="which arm's delta to tabulate")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",")]

    print(f"EXPOSURE - batch {args.batch.upper()}, cases the veto can touch\n")
    print(render_exposure(exposure(args.batch, args.root)))
    print()

    results = run_variants(args.batch, arms, args.root)
    for variant in ("off", "on"):
        print(f"\nveto {variant.upper()}\n")
        print(render(results[variant]))

    print()
    print(render_delta(results, args.arm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

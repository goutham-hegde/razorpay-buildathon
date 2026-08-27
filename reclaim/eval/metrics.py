"""Per-arm metrics, derived from the ledger and nothing else.

Deliberately computed from `case_outcomes` rather than from the `World` the run used. The
ledger is the audit trail; if a number in the report cannot be reconstructed from it, the
audit trail is not actually auditing anything. It also means these figures survive the
process that produced them - `reclaim.eval.metrics` on a committed ledger export gives the
same answer months later.

The headline is **net lift over control**, not recovery rate. Three separate corrections
sit between those two numbers and all three matter:

  * **control** - some of these cases were always coming back. Gross recovery counts that
    as a win; lift does not.
  * **cost** - retry fees, message costs, incentives and the price of unwinding a double
    charge. An arm can recover more money and be worth less.
  * **residual** - a halted mandate forfeits future months. An arm can win this month's
    revenue by destroying next year's, and only this term catches it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from reclaim.core.ledger import Ledger


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    run_id: str
    batch: str
    arm: str

    cases: int
    eligible: int

    recovered: int
    recovery_rate: float
    #: Of those recovered, how many came back with no help from us.
    recovered_organic: int
    recovered_by_charge: int

    at_risk_paise: int
    gross_paise: int
    cost_paise: int
    residual_loss_paise: int
    #: What the arm was actually worth: money in, minus what it spent, minus what it broke.
    net_paise: int

    charge_attempts: int
    contacts: int
    double_charges: int
    mandates_halted: int
    mandate_halt_rate: float

    #: Filled in against the control arm by `with_lift`.
    lift_cases: int = 0
    lift_rate: float = 0.0
    lift_gross_paise: int = 0
    lift_net_paise: int = 0
    #: Paise spent per rupee of *incremental* recovery. None when there is no lift to buy.
    cost_per_rupee_lifted: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scalar(db: sqlite3.Connection, sql: str, args: tuple) -> int:
    row = db.execute(sql, args).fetchone()
    return int(row[0] or 0)


def arm_metrics(ledger: Ledger, run_id: str) -> ArmMetrics:
    run = ledger.run(run_id)
    if run is None:
        raise KeyError(run_id)
    rows = ledger.outcomes(run_id)

    recovered = [r for r in rows if r["status"] == "recovered"]
    gross = sum(r["recovered_paise"] for r in rows)
    cost = sum(r["cost_paise"] for r in rows)
    residual = sum(r["residual_loss_paise"] for r in rows)
    halted = sum(r["mandate_halted"] for r in rows)
    at_risk = sum(r["at_risk_paise"] for r in rows)

    attempts = _scalar(
        ledger.db, "SELECT COUNT(*) FROM charge_claims WHERE run_id = ?", (run_id,)
    )
    contacts = _scalar(ledger.db, "SELECT COUNT(*) FROM contacts WHERE run_id = ?", (run_id,))
    doubles = _scalar(
        ledger.db,
        "SELECT COUNT(*) FROM charge_results WHERE run_id = ? AND double_charge = 1",
        (run_id,),
    )
    # Recurring cases are the only ones that can halt a mandate, so the rate is over those.
    #
    # Read from `is_recurring`, which the driver records, and NOT from
    # `residual_loss_paise > 0`. That predicate looks equivalent and is not: residual loss
    # is only ever non-zero on a case that actually halted, so it makes the denominator
    # equal to the numerator and the rate can only print 0% or 100%. It did exactly that
    # until the sensitivity run produced a column of 100.0s that no policy could have
    # earned.
    recurring = _scalar(
        ledger.db,
        "SELECT COUNT(*) FROM case_outcomes WHERE run_id = ? AND is_recurring = 1",
        (run_id,),
    )

    return ArmMetrics(
        run_id=run_id,
        batch=run["batch"],
        arm=run["arm"],
        cases=len(rows),
        eligible=int(run["cases_eligible"] or 0),
        recovered=len(recovered),
        recovery_rate=len(recovered) / len(rows) if rows else 0.0,
        recovered_organic=sum(1 for r in recovered if r["recovered_by"] == "organic"),
        recovered_by_charge=sum(1 for r in recovered if r["recovered_by"] == "charge"),
        at_risk_paise=at_risk,
        gross_paise=gross,
        cost_paise=cost,
        residual_loss_paise=residual,
        net_paise=gross - cost - residual,
        charge_attempts=attempts,
        contacts=contacts,
        double_charges=doubles,
        mandates_halted=halted,
        mandate_halt_rate=halted / recurring if recurring else 0.0,
    )


def with_lift(metrics: list[ArmMetrics], control_arm: str = "control") -> list[ArmMetrics]:
    """Attach lift-over-control to every arm.

    Without this the report is a list of gross recovery rates, and a gross recovery rate
    in a world where a quarter of cases settle themselves is not a claim about the agent.
    """
    control = next((m for m in metrics if m.arm == control_arm), None)
    if control is None:
        return metrics

    out: list[ArmMetrics] = []
    for m in metrics:
        lift_cases = m.recovered - control.recovered
        lift_gross = m.gross_paise - control.gross_paise
        lift_net = m.net_paise - control.net_paise
        out.append(
            ArmMetrics(
                **{
                    **m.as_dict(),
                    "lift_cases": lift_cases,
                    "lift_rate": m.recovery_rate - control.recovery_rate,
                    "lift_gross_paise": lift_gross,
                    "lift_net_paise": lift_net,
                    # Cost is only meaningful per rupee actually *added*. Dividing by gross
                    # recovery would flatter every arm by the control's free recoveries.
                    "cost_per_rupee_lifted": (
                        m.cost_paise / lift_gross if lift_gross > 0 else None
                    ),
                }
            )
        )
    return out


def batch_metrics(ledger: Ledger, batch: str) -> list[ArmMetrics]:
    """Metrics for every run recorded against a batch, with lift attached."""
    runs = [r["run_id"] for r in ledger.runs(batch.upper())]
    return with_lift([arm_metrics(ledger, r) for r in runs])


# ---------------------------------------------------------------------------
# The results table
# ---------------------------------------------------------------------------

#: Column order for the reported table. `net lift` is the headline and sits where the eye
#: lands; `recovery rate` is first only because it is the number everyone asks for, and
#: putting the control arm's own rate directly under it is the fastest way to show why the
#: rate on its own is not a claim about anything.
def render(metrics: list[ArmMetrics]) -> str:
    lines: list[str] = []
    order = {"control": 0, "naive": 1, "rules": 2, "agent": 3}
    metrics = sorted(metrics, key=lambda m: (order.get(m.arm, 9), m.run_id))

    head = (
        f"{'arm':<9}{'rec %':>7}{'gross Rs':>11}{'cost Rs':>9}{'residual':>10}"
        f"{'net Rs':>11}{'net lift':>11}{'cost/Re':>9}{'halt %':>8}{'double':>7}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for m in metrics:
        cpr = f"{m.cost_per_rupee_lifted:.3f}" if m.cost_per_rupee_lifted is not None else "-"
        lift = "-" if m.arm == "control" else f"{m.lift_net_paise / 100:>+,.0f}"
        lines.append(
            f"{m.arm:<9}{m.recovery_rate * 100:>6.1f}%{m.gross_paise / 100:>11,.0f}"
            f"{m.cost_paise / 100:>9,.0f}{m.residual_loss_paise / 100:>10,.0f}"
            f"{m.net_paise / 100:>11,.0f}{lift:>11}{cpr:>9}"
            f"{m.mandate_halt_rate * 100:>7.1f}%{m.double_charges:>7}"
        )
    lines.append("")
    lines.append(
        "net = gross recovered - cost (retry fees, comms, incentives, double-charge "
        "unwinds) - residual"
    )
    lines.append(
        "net lift = this arm's net minus the control arm's. The control recovers money "
        "with no help at all;"
    )
    lines.append(
        "           an arm's gross figure silently contains all of it, and lift is what "
        "removes it."
    )
    lines.append(
        "cost/Re  = paise spent per rupee of *incremental* gross recovery, not per rupee "
        "recovered."
    )
    lines.append(
        "halt %   = share of recurring cases whose mandate this arm destroyed. An arm can "
        "win on gross"
    )
    lines.append(
        "           and lose here, which is why residual is a column rather than a "
        "footnote."
    )
    return "\n".join(lines)


def main() -> int:
    import argparse

    from reclaim.core.ledger import open_ledger

    ap = argparse.ArgumentParser(description="The per-arm results table for a batch.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--root", default="data")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with open_ledger(args.batch, args.root) as ledger:
        metrics = batch_metrics(ledger, args.batch)
    if not metrics:
        print(f"no runs recorded for batch {args.batch.upper()} - run reclaim.eval.replay first")
        return 1
    if args.json:
        import json

        print(json.dumps([m.as_dict() for m in metrics], indent=2))
        return 0
    print(f"batch {args.batch.upper()}   n={metrics[0].cases}\n")
    print(render(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

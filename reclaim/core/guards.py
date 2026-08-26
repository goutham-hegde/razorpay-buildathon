"""Invariants R1-R6, asserted after every run against the ledger.

Metrics alone cannot catch the failures that matter here. An arm can post an excellent
recovery rate while double-charging customers, messaging them at three in the morning and
chasing people who asked to be left alone, and every number on the results table will
still look good. Invariants are what turn "bounded" and "compliant" from adjectives into
falsifiable claims.

    R1  no payment charged more than once across all recovery attempts
    R2  sum(recovered) <= sum(at_risk)                   no phantom recovery
    R3  no customer contacted more than N times per 7d   frequency cap holds
    R4  no contact outside permitted hours               quiet hours hold
    R5  no action on a terminated or opted-out case
    R6  no case left non-terminal after the batch drains

Two things worth saying about how these are checked.

**They are re-derived, not read back.** R3 does not call the same
`ledger.contacts_in_window` helper the policy used to decide whether to send; it sorts the
raw contact rows and slides its own window. A check that shares an implementation with the
thing it is checking will agree with it about a bug.

**Not every arm is required to hold them.** `control`, `rules` and `agent` are arms this
project makes claims about, and a violation there is a defect. `naive` is a baseline that
exists to be bad - it retries everything three times including the ambiguous debits - and
its violations are the finding rather than a failure. The CLI reports both and exits
non-zero only for the former. An invariant suite that no arm can ever fail is not
evidence of anything.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from reclaim.core import feed
from reclaim.core.compliance import (
    CONTACT_WINDOW_CLOSE,
    CONTACT_WINDOW_DAYS,
    CONTACT_WINDOW_OPEN,
    MAX_CONTACTS_PER_WINDOW,
    within_contact_window,
)
from reclaim.core.ledger import Ledger, open_ledger

#: Arms whose behaviour is a claim this project makes. A violation here is a bug.
MUST_HOLD = frozenset({"control", "rules", "agent"})


@dataclass(frozen=True, slots=True)
class Violation:
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    id: str
    title: str
    held: bool
    checked: int
    violations: tuple[Violation, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class GuardReport:
    run_id: str
    batch: str
    arm: str
    results: tuple[GuardResult, ...] = field(default_factory=tuple)

    @property
    def held(self) -> int:
        return sum(r.held for r in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def all_held(self) -> bool:
        return self.held == self.total

    @property
    def must_hold(self) -> bool:
        return self.arm in MUST_HOLD

    def summary(self) -> str:
        return f"{self.held}/{self.total} held"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _r1_single_charge(ledger: Ledger, run_id: str) -> GuardResult:
    """No payment charged more than once.

    Four distinct ways this can break, and they are different failures:

      a. the same attempt fired twice          - a system bug, blocked by UNIQUE
      b. two charges on one case both captured - a policy bug: we took the money twice
      c. a charge landed on an already-debited payment - the AMBIGUOUS_DEBITED trap
      d. a claimed charge was never settled    - we may have charged and do not know
    """
    v: list[Violation] = []
    charges = ledger.charges(run_id)

    seen: set[tuple[str, int]] = set()
    for c in charges:
        key = (c["payment_id"], c["attempt_no"])
        if key in seen:
            v.append(Violation(c["case_id"], f"duplicate attempt {key} - UNIQUE was bypassed"))
        seen.add(key)

    captured: dict[str, int] = {}
    for c in charges:
        if c["outcome"] == "captured":
            captured[c["case_id"]] = captured.get(c["case_id"], 0) + 1
        if c["double_charge"]:
            v.append(
                Violation(
                    c["case_id"],
                    f"attempt {c['attempt_no']} double-charged an already-debited payment "
                    f"({c['payment_id']})",
                )
            )
    for case_id, n in captured.items():
        if n > 1:
            v.append(Violation(case_id, f"{n} charges captured on one case"))

    for c in charges:
        if c["outcome"] is None:
            v.append(
                Violation(
                    c["case_id"],
                    f"attempt {c['attempt_no']} was claimed but never settled - the charge "
                    "may have reached the issuer and the outcome is unknown",
                )
            )

    return GuardResult(
        "R1",
        "no payment charged more than once",
        not v,
        len(charges),
        tuple(v),
        f"{len(charges)} charge attempts",
    )


def _r2_no_phantom_recovery(ledger: Ledger, run_id: str) -> GuardResult:
    """Recovered money cannot exceed the money that was at risk.

    Checked per case as well as in aggregate: a total that balances can still hide one
    case over-crediting and another under-crediting.
    """
    v: list[Violation] = []
    outcomes = ledger.outcomes(run_id)
    total_recovered = total_at_risk = 0
    for o in outcomes:
        total_recovered += o["recovered_paise"]
        total_at_risk += o["at_risk_paise"]
        if o["recovered_paise"] > o["at_risk_paise"]:
            v.append(
                Violation(
                    o["case_id"],
                    f"recovered {o['recovered_paise']}p against {o['at_risk_paise']}p at risk",
                )
            )
        if o["recovered_paise"] < 0:
            v.append(Violation(o["case_id"], f"negative recovery {o['recovered_paise']}p"))
        if o["recovered_paise"] > 0 and o["recovered_by"] not in ("organic", "charge"):
            v.append(
                Violation(
                    o["case_id"],
                    f"recovered {o['recovered_paise']}p attributed to {o['recovered_by']!r}; "
                    "every rupee must be attributable to organic settlement or to a charge",
                )
            )
    if total_recovered > total_at_risk:
        v.append(Violation("<batch>", f"recovered {total_recovered}p > at risk {total_at_risk}p"))

    return GuardResult(
        "R2",
        "no phantom recovery",
        not v,
        len(outcomes),
        tuple(v),
        f"recovered {total_recovered / 100:,.0f} of {total_at_risk / 100:,.0f} at risk",
    )


def _r3_frequency_cap(ledger: Ledger, run_id: str) -> GuardResult:
    """No customer contacted more than N times in any rolling 7-day window.

    Re-derived from raw rows with an explicit sliding window rather than by asking the
    ledger helper the policy used. Rolling, not per-calendar-week: a cap that resets on a
    fixed boundary permits N on Sunday night and N more on Monday morning.
    """
    v: list[Violation] = []
    window = timedelta(days=CONTACT_WINDOW_DAYS)
    by_customer: dict[str, list[datetime]] = {}
    rows = ledger.contacts(run_id)
    for r in rows:
        by_customer.setdefault(r["customer_id"], []).append(datetime.fromisoformat(r["at"]))

    for customer_id, times in by_customer.items():
        times.sort()
        left = 0
        for right, t in enumerate(times):
            while t - times[left] > window:
                left += 1
            span = right - left + 1
            if span > MAX_CONTACTS_PER_WINDOW:
                v.append(
                    Violation(
                        customer_id,
                        f"{span} contacts in {CONTACT_WINDOW_DAYS}d ending {t.isoformat()} "
                        f"(cap {MAX_CONTACTS_PER_WINDOW})",
                    )
                )
                break  # one report per customer is enough

    return GuardResult(
        "R3",
        f"max {MAX_CONTACTS_PER_WINDOW} contacts per customer per {CONTACT_WINDOW_DAYS}d",
        not v,
        len(rows),
        tuple(v),
        f"{len(rows)} contacts to {len(by_customer)} customers",
    )


def _r4_quiet_hours(ledger: Ledger, run_id: str) -> GuardResult:
    """No contact outside permitted hours."""
    rows = ledger.contacts(run_id)
    v = tuple(
        Violation(
            r["case_id"],
            f"{r['channel']} sent at {r['at']}, outside "
            f"{CONTACT_WINDOW_OPEN:%H:%M}-{CONTACT_WINDOW_CLOSE:%H:%M}",
        )
        for r in rows
        if not within_contact_window(datetime.fromisoformat(r["at"]))
    )
    return GuardResult(
        "R4",
        f"contact only within {CONTACT_WINDOW_OPEN:%H:%M}-{CONTACT_WINDOW_CLOSE:%H:%M}",
        not v,
        len(rows),
        v,
        f"{len(rows)} contacts checked",
    )


def _r5_no_action_after_terminal(ledger: Ledger, run_id: str) -> GuardResult:
    """No charge or contact on a case that is closed, or a customer who has opted out.

    Both halves matter. Acting after close is a scheduling bug - a timer that outlived the
    case it belonged to. Acting after opt-out is a compliance failure.
    """
    v: list[Violation] = []
    closed_at = {
        o["case_id"]: datetime.fromisoformat(o["closed_at"]) for o in ledger.outcomes(run_id)
    }
    opted_out = ledger.opt_outs(run_id)

    def check(kind: str, case_id: str, customer_id: str | None, at: datetime) -> None:
        end = closed_at.get(case_id)
        if end is not None and at > end:
            v.append(Violation(case_id, f"{kind} at {at.isoformat()} after case closed {end}"))
        if customer_id and (out_at := opted_out.get(customer_id)) is not None and at >= out_at:
            v.append(
                Violation(case_id, f"{kind} at {at.isoformat()} after opt-out at {out_at}")
            )

    charges = ledger.charges(run_id)
    for c in charges:
        check("charge", c["case_id"], None, datetime.fromisoformat(c["at"]))
    contacts = ledger.contacts(run_id)
    for r in contacts:
        check("contact", r["case_id"], r["customer_id"], datetime.fromisoformat(r["at"]))

    return GuardResult(
        "R5",
        "no action on a terminated or opted-out case",
        not v,
        len(charges) + len(contacts),
        tuple(v),
        f"{len(opted_out)} opt-outs recorded",
    )


def _r6_batch_drains(ledger: Ledger, run_id: str, case_ids: set[str]) -> GuardResult:
    """Every case reaches a terminal state. Nothing is left open and forgotten.

    The failure this catches is a case that fell out of the scheduler - never retried,
    never messaged, never written off. It costs nothing, appears nowhere in the metrics,
    and is a customer whose money we quietly stopped trying to collect.
    """
    closed = ledger.closed_case_ids(run_id)
    missing = sorted(case_ids - closed)
    stray = sorted(closed - case_ids)
    v = [Violation(cid, "left non-terminal after the batch drained") for cid in missing[:20]]
    if len(missing) > 20:
        v.append(Violation("<batch>", f"...and {len(missing) - 20} more"))
    v += [Violation(cid, "closed but not present in the batch") for cid in stray[:20]]
    return GuardResult(
        "R6",
        "no case left non-terminal",
        not v,
        len(case_ids),
        tuple(v),
        f"{len(closed)}/{len(case_ids)} cases closed",
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def check_run(ledger: Ledger, run_id: str, case_ids: set[str]) -> GuardReport:
    """Run R1-R6 against one run's ledger rows."""
    row = ledger.run(run_id)
    if row is None:
        raise KeyError(f"no such run: {run_id}")
    return GuardReport(
        run_id=run_id,
        batch=row["batch"],
        arm=row["arm"],
        results=(
            _r1_single_charge(ledger, run_id),
            _r2_no_phantom_recovery(ledger, run_id),
            _r3_frequency_cap(ledger, run_id),
            _r4_quiet_hours(ledger, run_id),
            _r5_no_action_after_terminal(ledger, run_id),
            _r6_batch_drains(ledger, run_id, case_ids),
        ),
    )


def check_batch(batch: str, root: Path | str = "data") -> list[GuardReport]:
    """Run the invariants against every run recorded for a batch."""
    cases = feed.load_batch(batch, root).cases
    case_ids = {c.id for c in cases}
    with open_ledger(batch, root) as ledger:
        return [check_run(ledger, r["run_id"], case_ids) for r in ledger.runs(batch.upper())]


def render(report: GuardReport, verbose: bool = False) -> str:
    lines = [
        f"{report.batch} - {report.arm:<10} {report.run_id}"
        + ("" if report.must_hold else "   [baseline: measured, not asserted]")
    ]
    for r in report.results:
        mark = "HELD" if r.held else "VIOLATED"
        lines.append(f"  {r.id}  {r.title:<52} {mark:>8}   {r.note}")
        if not r.held:
            shown = r.violations if verbose else r.violations[:3]
            for violation in shown:
                lines.append(f"        - {violation.subject}: {violation.detail}")
            if not verbose and len(r.violations) > 3:
                lines.append(f"        ... {len(r.violations) - 3} more")
    lines.append(f"  {report.summary()}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Assert invariants R1-R6 against a batch ledger.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--root", default="data")
    ap.add_argument("--run", default=None, help="check a single run id")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every violation")
    args = ap.parse_args()

    reports = check_batch(args.batch, args.root)
    if args.run:
        reports = [r for r in reports if r.run_id == args.run]
    if not reports:
        print(f"no runs recorded for batch {args.batch.upper()} - run reclaim.eval.replay first")
        return 1

    failures = 0
    for report in reports:
        print(render(report, args.verbose))
        print()
        if report.must_hold and not report.all_held:
            failures += 1

    asserted = [r for r in reports if r.must_hold]
    if asserted and not failures:
        print(f"{len(asserted)} asserted arm(s): {asserted[0].total}/{asserted[0].total} held")
    elif failures:
        print(f"{failures} asserted arm(s) violated an invariant")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

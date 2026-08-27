"""The reported results table, rendered as markdown and spliced into the README.

WHY THIS IS A COMMAND AND NOT A COPY-PASTE
------------------------------------------
Every figure quoted in the README has to be reproducible by running `replay` on a clean
checkout. The failure mode this module exists to prevent is the ordinary one: a number is
pasted into prose, an input changes three days later, and nobody re-derives it. The prose
still reads as confidently as it did when it was true.

So the README does not contain hand-written numbers. It contains a marked region:

    <!-- RESULTS-TABLE -->
    ...generated...
    <!-- /RESULTS-TABLE -->

and this module is the only thing allowed to write inside it. It reads the ledger - the same
append-only ledger `guards.py` re-derives R1-R6 from - and never recomputes anything itself.
If the table and the ledger ever disagree, the ledger is right and this is the bug.

WHAT IS IN THE TABLE, AND WHY
-----------------------------
Six reported columns, plus one that is not a metric so much as a liability count:

    recovery rate       the number everyone asks for, and on its own a claim about nothing
    gross Rs            money recovered, silently including everything the control got free
    net Rs              gross, minus what the arm spent, minus the future revenue it broke
    net lift            net minus the control's net. This is the actual claim
    cost / Re lifted    paise spent per rupee of *incremental* recovery
    halt %              the downside metric: subscriptions destroyed buying today's rupee
    double              duplicate charges. An arm that wins every column and puts a number
                        here has not won

`net lift` is the headline. An arm's gross figure contains the control arm's organic
recoveries in full, and reporting gross as recovery would be counting money that was coming
back anyway.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reclaim.core.ledger import open_ledger
from reclaim.eval.metrics import ArmMetrics, batch_metrics

OPEN_MARKER = "<!-- RESULTS-TABLE -->"
CLOSE_MARKER = "<!-- /RESULTS-TABLE -->"

#: Reporting order. Control first because every other row is read as a difference from it.
ARM_ORDER = {"control": 0, "naive": 1, "rules": 2, "agent": 3}

#: One line per arm, so the table says what each row *is* without the reader leaving it.
ARM_BLURB = {
    "control": "no intervention at all",
    "naive": "retry immediately, 3x, fixed interval",
    "rules": "policy engine, keyword diagnosis, no model",
    "agent": "policy engine, model diagnosis",
}


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def _signed_rupees(paise: int) -> str:
    return f"{paise / 100:+,.0f}"


def markdown_table(metrics: list[ArmMetrics]) -> str:
    """The four-arm table as GitHub-flavoured markdown."""
    ordered = sorted(metrics, key=lambda m: (ARM_ORDER.get(m.arm, 9), m.run_id))

    rows = [
        "| arm | what it does | rec % | gross Rs | net Rs | net lift Rs | cost/Re lifted | halt % | double |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for m in ordered:
        cpr = (
            f"{m.cost_per_rupee_lifted:.3f}"
            if m.cost_per_rupee_lifted is not None
            else "—"
        )
        lift = "—" if m.arm == "control" else _signed_rupees(m.lift_net_paise)
        rows.append(
            f"| **{m.arm}** | {ARM_BLURB.get(m.arm, '')} "
            f"| {m.recovery_rate * 100:.1f}% "
            f"| {_rupees(m.gross_paise)} "
            f"| {_rupees(m.net_paise)} "
            f"| {lift} "
            f"| {cpr} "
            f"| {m.mandate_halt_rate * 100:.1f}% "
            f"| {m.double_charges} |"
        )
    return "\n".join(rows)


FOOTNOTES = """\
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
already have been debited and a retry *succeeds*; the success is the liability. Invariant R1
exists so this column stays at zero.
"""


def render_block(metrics: list[ArmMetrics], batch: str) -> str:
    """The whole spliced region, markers included."""
    n = metrics[0].cases if metrics else 0
    at_risk = metrics[0].at_risk_paise if metrics else 0

    parts = [
        OPEN_MARKER,
        "",
        f"Batch **{batch.upper()}** — the held-out batch. {n:,} failed payments and "
        f"mandates, Rs {_rupees(at_risk)} at risk. Tuning happened on batch A; these "
        "numbers were produced by running the commands under [Run it](#run-it) and were "
        "not touched by hand.",
        "",
        markdown_table(metrics),
        "",
        FOOTNOTES.rstrip(),
        "",
        CLOSE_MARKER,
    ]
    return "\n".join(parts)


def splice(readme: str, block: str) -> str:
    """Replace the marked region, or the bare opening marker, with `block`.

    Deliberately strict: no marker means no write. Guessing where a results table belongs in
    someone's README is exactly the sort of helpfulness that silently mangles a file.
    """
    start = readme.find(OPEN_MARKER)
    if start == -1:
        raise ValueError(
            f"{OPEN_MARKER} not found. Add it where the table should go; this module will "
            "not guess a location."
        )
    end = readme.find(CLOSE_MARKER, start)
    if end == -1:
        # First run: only the opening marker is in the file.
        return readme[:start] + block + readme[start + len(OPEN_MARKER) :]
    return readme[:start] + block + readme[end + len(CLOSE_MARKER) :]


def missing_arms(metrics: list[ArmMetrics]) -> list[str]:
    present = {m.arm for m in metrics}
    return [a for a in ARM_ORDER if a not in present]


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the results table into the README.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--root", default="data")
    ap.add_argument("--readme", default="README.md")
    ap.add_argument(
        "--write",
        action="store_true",
        help="splice into the README. Without this the block is printed and nothing is "
        "modified",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="write even when an arm has no run in the ledger. Off by default: a table "
        "missing the rules arm is not the reported table",
    )
    args = ap.parse_args()

    # Windows consoles default to cp1252 and the table is full of em dashes. Writing the
    # README is already explicitly utf-8; this makes *printing* it survive too.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with open_ledger(args.batch, args.root) as ledger:
        metrics = batch_metrics(ledger, args.batch)

    if not metrics:
        print(
            f"no runs recorded for batch {args.batch.upper()} — run "
            f"`python -m reclaim.eval.replay --batch {args.batch} --arms all` first",
            file=sys.stderr,
        )
        return 1

    absent = missing_arms(metrics)
    if absent:
        print(f"warning: no run for {', '.join(absent)}", file=sys.stderr)
        if args.write and not args.allow_partial:
            print(
                "refusing to write a partial table; pass --allow-partial to override",
                file=sys.stderr,
            )
            return 1

    block = render_block(metrics, args.batch)
    if not args.write:
        print(block)
        return 0

    path = Path(args.readme)
    original = path.read_text(encoding="utf-8")
    updated = splice(original, block)
    if updated == original:
        print(f"{path}: already current")
        return 0
    path.write_text(updated, encoding="utf-8")
    print(f"{path}: results table updated from batch {args.batch.upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

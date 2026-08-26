"""Score a diagnoser against ground truth, and publish the matrix rather than one number.

`eval` may read both sides; `core` may not. That asymmetry is the whole reason a diagnosis
accuracy figure from this repo means anything.

WHY A MATRIX AND NOT AN ACCURACY
--------------------------------
A single accuracy number hides the only errors that cost money. Confusing
`insufficient_funds` with `limit_exceeded` is nearly free - both mean "wait, then retry".
Confusing `ambiguous_debited` with `issuer_technical_decline` means retrying a payment that
already went through, which is a double charge, a refund and a furious customer.

So this module reports, alongside per-class precision and recall:

  * the **confusion pairs** the world was built around, each with its own error rate
  * **cost-weighted error**, which counts a mistake by what the mistake does rather than by
    whether it was a mistake
  * **support counts next to every per-class figure**, because `risk_declined` has 8 cases in
    batch B and a per-class F1 over 8 samples is not a measurement, it is an anecdote

Macro-averaging across nine classes with supports from 8 to 200 would let the rare classes
swing the headline. Both macro and weighted averages are printed, and the gap between them
is itself informative.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from reclaim.core import feed
from reclaim.core.diagnose import Diagnosis, cache_path, load_diagnoses
from reclaim.domain import RootCause

#: The pairs the error catalogue was deliberately built to blur. Reported individually
#: because the aggregate number cannot show whether the hard cases were solved or dodged.
CONFUSION_PAIRS: tuple[tuple[RootCause, RootCause, str], ...] = (
    (
        RootCause.AMBIGUOUS_DEBITED,
        RootCause.ISSUER_TECHNICAL_DECLINE,
        "both read as a timeout; getting this wrong double-charges",
    ),
    (
        RootCause.MANDATE_REVOKED,
        RootCause.INSTRUMENT_INVALID,
        "both read as 'not valid'; instrument vs permission",
    ),
    (
        RootCause.PSP_ROUTING_FAILURE,
        RootCause.ISSUER_TECHNICAL_DECLINE,
        "both read as a gateway error; our route vs their bank",
    ),
)

#: What a misprediction costs, relative. Not money - a stated ranking of harm, so that the
#: headline error metric is weighted by consequence instead of treating every mistake alike.
#:
#: The single worst error in the taxonomy is calling a real ambiguous debit a plain technical
#: decline, because the policy will then retry and take the money twice.
ERROR_COST: dict[tuple[RootCause, RootCause], float] = {
    (RootCause.AMBIGUOUS_DEBITED, RootCause.ISSUER_TECHNICAL_DECLINE): 10.0,
    (RootCause.AMBIGUOUS_DEBITED, RootCause.PSP_ROUTING_FAILURE): 10.0,
    (RootCause.AMBIGUOUS_DEBITED, RootCause.INSUFFICIENT_FUNDS): 8.0,
    # Retrying a risk decline argues with a fraud rule and eventually hard-blocks the customer.
    (RootCause.RISK_DECLINED, RootCause.ISSUER_TECHNICAL_DECLINE): 5.0,
    (RootCause.RISK_DECLINED, RootCause.INSUFFICIENT_FUNDS): 5.0,
    # Believing a dead instrument is merely a timeout burns the whole retry budget on it.
    (RootCause.INSTRUMENT_INVALID, RootCause.ISSUER_TECHNICAL_DECLINE): 3.0,
    (RootCause.MANDATE_REVOKED, RootCause.ISSUER_TECHNICAL_DECLINE): 3.0,
    # Retrying an empty account immediately is wasted but harmless.
    (RootCause.INSUFFICIENT_FUNDS, RootCause.ISSUER_TECHNICAL_DECLINE): 1.5,
}
#: Any error not named above. Wrong, but the action it implies is roughly as good.
DEFAULT_ERROR_COST = 1.0


@dataclass(frozen=True, slots=True)
class ClassScore:
    cause: RootCause
    support: int          # how many really were this
    predicted: int        # how many we said were this
    correct: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class PairScore:
    a: RootCause
    b: RootCause
    note: str
    a_as_b: int           # really a, called b
    b_as_a: int
    a_support: int
    b_support: int


@dataclass(frozen=True, slots=True)
class Report:
    batch: str
    provider: str
    model: str
    n: int
    correct: int
    accuracy: float
    macro_f1: float
    weighted_f1: float
    cost_weighted_error: float
    per_class: tuple[ClassScore, ...]
    pairs: tuple[PairScore, ...]
    matrix: dict[str, dict[str, int]]

    def as_dict(self) -> dict:
        return {
            "batch": self.batch,
            "provider": self.provider,
            "model": self.model,
            "n": self.n,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "macro_f1": self.macro_f1,
            "weighted_f1": self.weighted_f1,
            "cost_weighted_error": self.cost_weighted_error,
            "per_class": [
                {
                    "cause": str(c.cause),
                    "support": c.support,
                    "predicted": c.predicted,
                    "correct": c.correct,
                    "precision": c.precision,
                    "recall": c.recall,
                    "f1": c.f1,
                }
                for c in self.per_class
            ],
            "pairs": [
                {
                    "a": str(p.a),
                    "b": str(p.b),
                    "note": p.note,
                    "a_as_b": p.a_as_b,
                    "b_as_a": p.b_as_a,
                    "a_support": p.a_support,
                    "b_support": p.b_support,
                }
                for p in self.pairs
            ],
            "matrix": self.matrix,
        }


def load_truth_causes(batch_dir: Path) -> dict[str, RootCause]:
    """Ground truth. Only `eval` may call this - see `tests/test_seal.py`."""
    out: dict[str, RootCause] = {}
    with (batch_dir / "truth.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line := line.strip():
                raw = json.loads(line)
                out[raw["case_id"]] = RootCause(raw["root_cause"])
    return out


def score(
    truth: dict[str, RootCause],
    predictions: dict[str, Diagnosis],
    batch: str = "",
) -> Report:
    ids = sorted(set(truth) & set(predictions))
    if not ids:
        raise ValueError("no overlap between ground truth and predictions")

    matrix: dict[str, dict[str, int]] = {
        str(a): {str(b): 0 for b in RootCause} for a in RootCause
    }
    for cid in ids:
        matrix[str(truth[cid])][str(predictions[cid].root_cause)] += 1

    correct = sum(matrix[str(c)][str(c)] for c in RootCause)

    per_class: list[ClassScore] = []
    for cause in RootCause:
        row = matrix[str(cause)]
        support = sum(row.values())
        predicted = sum(matrix[str(other)][str(cause)] for other in RootCause)
        hit = row[str(cause)]
        precision = hit / predicted if predicted else 0.0
        recall = hit / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_class.append(ClassScore(cause, support, predicted, hit, precision, recall, f1))

    present = [c for c in per_class if c.support > 0]
    macro_f1 = sum(c.f1 for c in present) / len(present) if present else 0.0
    total_support = sum(c.support for c in present)
    weighted_f1 = (
        sum(c.f1 * c.support for c in present) / total_support if total_support else 0.0
    )

    # Cost-weighted error: every mistake counted by what the mistake does. Normalised by the
    # sample count, so it reads as "average harm per case" and is comparable across batches.
    harm = 0.0
    for cid in ids:
        actual, predicted_cause = truth[cid], predictions[cid].root_cause
        if actual is not predicted_cause:
            harm += ERROR_COST.get((actual, predicted_cause), DEFAULT_ERROR_COST)

    pairs = tuple(
        PairScore(
            a=a,
            b=b,
            note=note,
            a_as_b=matrix[str(a)][str(b)],
            b_as_a=matrix[str(b)][str(a)],
            a_support=sum(matrix[str(a)].values()),
            b_support=sum(matrix[str(b)].values()),
        )
        for a, b, note in CONFUSION_PAIRS
    )

    sample = predictions[ids[0]]
    return Report(
        batch=batch,
        provider=sample.provider,
        model=sample.model,
        n=len(ids),
        correct=correct,
        accuracy=correct / len(ids),
        macro_f1=macro_f1,
        weighted_f1=weighted_f1,
        cost_weighted_error=harm / len(ids),
        per_class=tuple(sorted(per_class, key=lambda c: -c.support)),
        pairs=pairs,
        matrix=matrix,
    )


def score_batch(batch: str, provider: str, root: Path | str = "data") -> Report:
    b = feed.load_batch(batch, root)
    predictions = load_diagnoses(cache_path(b.dir, provider))
    if not predictions:
        raise FileNotFoundError(
            f"no diagnoses for batch {b.name} provider {provider} - run "
            f"`python -m reclaim.core.diagnose --batch {b.name} --provider {provider}` first"
        )
    return score(load_truth_causes(b.dir), predictions, b.name)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SHORT = {
    RootCause.ISSUER_TECHNICAL_DECLINE: "tech",
    RootCause.INSUFFICIENT_FUNDS: "nsf",
    RootCause.AUTH_ABANDONED: "aband",
    RootCause.INSTRUMENT_INVALID: "instr",
    RootCause.LIMIT_EXCEEDED: "limit",
    RootCause.MANDATE_REVOKED: "mndt",
    RootCause.RISK_DECLINED: "risk",
    RootCause.PSP_ROUTING_FAILURE: "psp",
    RootCause.AMBIGUOUS_DEBITED: "ambig",
}


def render(report: Report) -> str:
    lines: list[str] = []
    lines.append(
        f"batch {report.batch}  provider={report.provider}  model={report.model}  n={report.n}"
    )
    lines.append(
        f"accuracy {report.accuracy:.3f}   macro-F1 {report.macro_f1:.3f}   "
        f"weighted-F1 {report.weighted_f1:.3f}   cost-weighted error {report.cost_weighted_error:.3f}"
    )
    lines.append("")

    lines.append(f"{'cause':<26}{'supp':>6}{'pred':>6}{'prec':>7}{'rec':>7}{'F1':>7}")
    for c in report.per_class:
        if c.support == 0 and c.predicted == 0:
            continue
        thin = "  <- thin support" if 0 < c.support < 15 else ""
        lines.append(
            f"{str(c.cause):<26}{c.support:>6}{c.predicted:>6}"
            f"{c.precision:>7.3f}{c.recall:>7.3f}{c.f1:>7.3f}{thin}"
        )
    lines.append("")

    lines.append("engineered confusion pairs")
    for p in report.pairs:
        a_rate = p.a_as_b / p.a_support if p.a_support else 0.0
        b_rate = p.b_as_a / p.b_support if p.b_support else 0.0
        lines.append(f"  {p.a} <-> {p.b}")
        lines.append(f"    {p.note}")
        lines.append(
            f"    {p.a_as_b}/{p.a_support} ({a_rate:.1%}) called {_SHORT[p.b]}   |   "
            f"{p.b_as_a}/{p.b_support} ({b_rate:.1%}) called {_SHORT[p.a]}"
        )
    lines.append("")

    order = [c.cause for c in report.per_class if c.support or c.predicted]
    header = "".join(f"{_SHORT[c]:>7}" for c in order)
    lines.append(f"{'actual \\ predicted':<26}{header}")
    for actual in order:
        row = report.matrix[str(actual)]
        cells = "".join(f"{row[str(p)]:>7}" for p in order)
        lines.append(f"{str(actual):<26}{cells}")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score diagnoses against ground truth.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--provider", default="stub")
    ap.add_argument("--root", default="data")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--compare", default=None, help="also score this provider, side by side")
    args = ap.parse_args()

    report = score_batch(args.batch, args.provider, args.root)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    print(render(report))
    if args.compare:
        other = score_batch(args.batch, args.compare, args.root)
        print("\n" + "=" * 72 + "\n")
        print(render(other))
        print("\n" + "-" * 72)
        print(f"{'':<26}{args.provider:>16}{args.compare:>16}")
        for label, x, y in (
            ("accuracy", report.accuracy, other.accuracy),
            ("macro-F1", report.macro_f1, other.macro_f1),
            ("weighted-F1", report.weighted_f1, other.weighted_f1),
            ("cost-weighted error", report.cost_weighted_error, other.cost_weighted_error),
        ):
            print(f"{label:<26}{x:>16.3f}{y:>16.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

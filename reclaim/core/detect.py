"""Detection: which failed payments are actually worth working, and in what order.

This runs before any diagnosis and before any policy. It is deliberately dull,
deterministic and cheap - the expensive parts of the system should never see a case that
should not have been opened in the first place.

Detection is where the two quietest sources of harm get caught:

  * **Acting on something that has not failed.** A UPI collect request sitting at
    `PENDING` is not a failure, it is a request the customer has not answered yet.
    Retrying it creates a second collect request against the same person for the same
    money. The rail says "pending", not "declined", and the difference matters.

  * **Acting twice on one economic event.** Payment infrastructure redelivers webhooks.
    Two rows describing one failure look exactly like two failures, and working both is a
    double charge arrived at through bookkeeping rather than through policy.

Neither of these is interesting, and both of them would be the actual bug in production.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from reclaim.core.compliance import (
    ASSUMED_MANDATE_RESIDUAL_MONTHS,
    MIN_ECONOMIC_AMOUNT_PAISE,
)
from reclaim.domain import Case, Customer, Mandate, PaymentStatus, TERMINAL_SUCCESS


class Disposition(StrEnum):
    """Why a detected case was or was not opened for recovery."""

    #: Genuinely at risk and worth working.
    ELIGIBLE = "eligible"

    #: The rail has not said no yet. Not a failure; do not touch it.
    NOT_YET_FAILED = "not_yet_failed"

    #: Already captured. There is nothing at risk.
    ALREADY_SETTLED = "already_settled"

    #: Customer has withdrawn consent. Invariant R5 forbids any action.
    OPTED_OUT = "opted_out"

    #: Recovering it costs more than it returns.
    BELOW_ECONOMIC_FLOOR = "below_economic_floor"

    #: Another case describes the same economic event. Work one, suppress the rest.
    SUSPECTED_DUPLICATE = "suspected_duplicate"

    #: Malformed - no failed attempt to recover.
    NO_FAILED_ATTEMPT = "no_failed_attempt"


#: Two cases are treated as one economic event if they share customer, amount and rail
#: and opened within this window. Long enough to catch a webhook redelivered after a
#: retry storm, short enough not to merge a genuine second month's subscription debit.
DUPLICATE_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Detection:
    """One case, triaged."""

    case_id: str
    disposition: Disposition
    reason: str
    at_risk_paise: int
    #: Ordering key: what this case is worth to the business if recovered, including the
    #: agent's own conservative estimate of subscription residual. Not money - a ranking.
    priority_paise: int = 0
    #: Non-blocking observations passed downstream to diagnosis and policy.
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        return self.disposition is Disposition.ELIGIBLE


def _priority(case: Case) -> int:
    """What recovering this case is worth, for ordering only.

    A recurring case is worth more than its face amount, because losing it can halt the
    mandate and forfeit future months. `ASSUMED_MANDATE_RESIDUAL_MONTHS` is the agent's
    own conservative estimate and is deliberately not the world's true figure - see the
    note in `core.compliance`.
    """
    if case.kind != "recurring":
        return case.amount_paise
    return case.amount_paise * (1 + ASSUMED_MANDATE_RESIDUAL_MONTHS)


def _duplicate_key(case: Case) -> tuple[str, int, str]:
    return (case.customer_id, case.amount_paise, str(case.rail))


def detect(
    cases: list[Case],
    customers: dict[str, Customer],
    mandates: dict[str, Mandate] | None = None,
) -> list[Detection]:
    """Triage a batch. Returns one `Detection` per input case, input order preserved."""
    mandates = mandates or {}

    # Pre-pass: group by economic-event signature so duplicates can be suppressed. The
    # earliest case in a cluster is worked; the rest are recorded, not silently dropped.
    clusters: dict[tuple[str, int, str], list[Case]] = defaultdict(list)
    for case in cases:
        clusters[_duplicate_key(case)].append(case)

    primary: set[str] = set()
    duplicate_of: dict[str, str] = {}
    for group in clusters.values():
        group = sorted(group, key=lambda c: (c.opened_at, c.id))
        head = group[0]
        primary.add(head.id)
        anchor = head
        for other in group[1:]:
            if other.opened_at - anchor.opened_at <= DUPLICATE_WINDOW:
                duplicate_of[other.id] = anchor.id
            else:
                primary.add(other.id)
                anchor = other

    out: list[Detection] = []
    for case in cases:
        out.append(_triage(case, customers, mandates, duplicate_of))
    return out


def _triage(
    case: Case,
    customers: dict[str, Customer],
    mandates: dict[str, Mandate],
    duplicate_of: dict[str, str],
) -> Detection:
    at_risk = case.amount_paise
    flags: list[str] = []

    def result(d: Disposition, reason: str, priority: int = 0) -> Detection:
        return Detection(case.id, d, reason, at_risk, priority, tuple(flags))

    if case.status in ("recovered", "abandoned"):
        return result(Disposition.ALREADY_SETTLED, f"case already {case.status}")

    if not case.attempts:
        return result(Disposition.NO_FAILED_ATTEMPT, "case carries no payment attempt")

    latest = max(case.attempts, key=lambda a: (a.created_at, a.attempt_no))

    if latest.status in TERMINAL_SUCCESS:
        return result(Disposition.ALREADY_SETTLED, "latest attempt was captured")

    if latest.status is PaymentStatus.PENDING:
        # The customer has not answered a collect request. This is not a decline, and
        # pushing another request at them is the wrong read of the rail's own status.
        return result(
            Disposition.NOT_YET_FAILED,
            "latest attempt is pending, not declined - the rail has not said no",
        )

    if latest.status is not PaymentStatus.FAILED:
        return result(Disposition.NOT_YET_FAILED, f"latest attempt is {latest.status}")

    if (dupe := duplicate_of.get(case.id)) is not None:
        return result(
            Disposition.SUSPECTED_DUPLICATE,
            f"same customer, amount and rail as {dupe} within {DUPLICATE_WINDOW}",
        )

    customer = customers.get(case.customer_id)
    if customer is None:
        flags.append("unknown_customer")
    elif customer.opted_out:
        return result(Disposition.OPTED_OUT, "customer has withdrawn consent to be contacted")

    if at_risk < MIN_ECONOMIC_AMOUNT_PAISE:
        return result(
            Disposition.BELOW_ECONOMIC_FLOOR,
            f"at risk {at_risk}p is below the {MIN_ECONOMIC_AMOUNT_PAISE}p floor",
        )

    # Observations that do not block, but change what the policy is allowed to choose.
    if case.mandate_id:
        mandate = mandates.get(case.mandate_id)
        if mandate is None:
            flags.append("mandate_missing")
        elif mandate.status != "active":
            # No charge can succeed against this; only re-authorisation can.
            flags.append(f"mandate_{mandate.status}")
    if customer is not None and not customer.contactable_channels:
        flags.append("no_contact_channel")
    if case.kind == "recurring":
        flags.append("recurring")

    return result(Disposition.ELIGIBLE, "failed payment with recoverable value", _priority(case))


def work_queue(detections: list[Detection]) -> list[Detection]:
    """Eligible cases, highest value first. Ties broken by id so the order is stable."""
    return sorted(
        (d for d in detections if d.eligible),
        key=lambda d: (-d.priority_paise, d.case_id),
    )


def summarise(detections: list[Detection]) -> dict[str, int]:
    """Counts by disposition, for the run report."""
    counts: dict[str, int] = {}
    for d in detections:
        counts[str(d.disposition)] = counts.get(str(d.disposition), 0) + 1
    return counts

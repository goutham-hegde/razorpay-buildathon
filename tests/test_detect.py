"""Detection triage, exercised on hand-built fixtures.

Most of these dispositions do not occur in the generated batches - the generator emits one
clean failed case per customer, so on batch A and B every case comes back `eligible`. That
is a property of the synthetic data, not evidence the rules are unnecessary: pending
collect requests and redelivered webhooks are ordinary in production and are exactly the
two ways a recovery system double-charges someone without any policy bug at all.

So the rules are tested here against fixtures that do contain them, and the run report
prints the disposition counts so a reader can see for themselves which ones fired.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from reclaim.core.compliance import ASSUMED_MANDATE_RESIDUAL_MONTHS, MIN_ECONOMIC_AMOUNT_PAISE
from reclaim.core.detect import DUPLICATE_WINDOW, Disposition, detect, work_queue
from reclaim.domain import (
    Case,
    Customer,
    ErrorSource,
    ErrorStep,
    Mandate,
    ObservedError,
    PaymentAttempt,
    PaymentStatus,
    Rail,
)

T0 = datetime(2026, 8, 10, 12, 0)

ERR = ObservedError(
    code="BAD_REQUEST_ERROR",
    source=ErrorSource.BANK,
    step=ErrorStep.PAYMENT_AUTHORIZATION,
    reason="payment_failed",
    description="insufficient balance in payer account",
)


def make_case(
    case_id: str = "case_1",
    *,
    customer_id: str = "cus_1",
    amount: int = 49900,
    status: PaymentStatus = PaymentStatus.FAILED,
    kind: str = "one_time",
    rail: Rail = Rail.CARD_3DS,
    opened_at: datetime = T0,
    mandate_id: str | None = None,
) -> Case:
    return Case(
        id=case_id,
        customer_id=customer_id,
        amount_paise=amount,
        opened_at=opened_at,
        kind=kind,
        rail=rail,
        issuer="HDFC",
        psp="psp_a",
        mandate_id=mandate_id,
        attempts=[
            PaymentAttempt(
                id=f"pay_{case_id}_0",
                case_id=case_id,
                customer_id=customer_id,
                amount_paise=amount,
                rail=rail,
                issuer="HDFC",
                psp="psp_a",
                created_at=opened_at,
                status=status,
                error=ERR if status is PaymentStatus.FAILED else None,
                mandate_id=mandate_id,
            )
        ],
    )


def customer(cid: str = "cus_1", *, opted_out: bool = False, channels: list[str] | None = None):
    return Customer(
        id=cid,
        salary_day=1,
        preferred_rail=Rail.UPI_INTENT,
        contactable_channels=["sms", "email"] if channels is None else channels,
        opted_out=opted_out,
    )


def triage(cases, customers=None, mandates=None):
    customers = customers or {c.customer_id: customer(c.customer_id) for c in cases}
    return {d.case_id: d for d in detect(cases, customers, mandates or {})}


# ---------------------------------------------------------------------------


def test_a_plain_failed_payment_is_eligible() -> None:
    d = triage([make_case()])["case_1"]
    assert d.disposition is Disposition.ELIGIBLE
    assert d.eligible


def test_a_pending_collect_is_not_a_failure() -> None:
    """The rail said "waiting", not "no". Retrying pushes a second request at the customer."""
    d = triage([make_case(status=PaymentStatus.PENDING, rail=Rail.UPI_COLLECT)])["case_1"]
    assert d.disposition is Disposition.NOT_YET_FAILED
    assert not d.eligible


def test_a_captured_payment_is_not_at_risk() -> None:
    d = triage([make_case(status=PaymentStatus.CAPTURED)])["case_1"]
    assert d.disposition is Disposition.ALREADY_SETTLED


def test_an_opted_out_customer_is_excluded() -> None:
    """R5 forbids acting here at all, so detection must stop it before policy sees it."""
    case = make_case()
    d = triage([case], {"cus_1": customer(opted_out=True)})["case_1"]
    assert d.disposition is Disposition.OPTED_OUT


def test_a_case_below_the_economic_floor_is_not_worked() -> None:
    d = triage([make_case(amount=MIN_ECONOMIC_AMOUNT_PAISE - 1)])["case_1"]
    assert d.disposition is Disposition.BELOW_ECONOMIC_FLOOR


def test_a_redelivered_webhook_is_suppressed_not_worked_twice() -> None:
    """Two rows, one economic event. Working both is a double charge from bookkeeping."""
    first = make_case("case_1", opened_at=T0)
    second = make_case("case_2", opened_at=T0 + timedelta(minutes=5))
    out = triage([first, second])
    assert out["case_1"].disposition is Disposition.ELIGIBLE
    assert out["case_2"].disposition is Disposition.SUSPECTED_DUPLICATE
    assert "case_1" in out["case_2"].reason


def test_a_genuine_second_debit_later_is_not_a_duplicate() -> None:
    """Next month's subscription is the same customer, amount and rail. It is not a dupe."""
    first = make_case("case_1", opened_at=T0)
    second = make_case("case_2", opened_at=T0 + DUPLICATE_WINDOW + timedelta(minutes=1))
    out = triage([first, second])
    assert out["case_1"].disposition is Disposition.ELIGIBLE
    assert out["case_2"].disposition is Disposition.ELIGIBLE


def test_duplicates_are_measured_against_the_event_not_against_each_other() -> None:
    """A duplicate must be close to the event it duplicates, not merely close to another
    duplicate.

    Chaining - linking each arrival to the previous one - is the tempting implementation
    and is wrong. A customer whose payment fails every 29 minutes for a week would have
    the entire week collapsed into a single "event", and six days of genuinely
    recoverable failures would be silently suppressed.

    So the window re-anchors: case_2 at +40m is outside case_0's window, so it opens a new
    event rather than extending the old one.
    """
    cases = [
        make_case(f"case_{i}", opened_at=T0 + timedelta(minutes=20 * i)) for i in range(3)
    ]
    out = triage(cases)
    assert out["case_0"].disposition is Disposition.ELIGIBLE
    assert out["case_1"].disposition is Disposition.SUSPECTED_DUPLICATE  # +20m, inside
    assert out["case_2"].disposition is Disposition.ELIGIBLE             # +40m, re-anchors
    assert out["case_1"].reason.endswith(str(DUPLICATE_WINDOW))


def test_a_revoked_mandate_is_flagged_but_still_worked() -> None:
    """It cannot be charged, but it can be re-authorised. Excluding it would forfeit it."""
    case = make_case(kind="recurring", rail=Rail.UPI_AUTOPAY, mandate_id="mdt_1")
    mandates = {
        "mdt_1": Mandate(
            id="mdt_1", customer_id="cus_1", rail=Rail.UPI_AUTOPAY,
            max_amount_paise=99900, status="revoked",
        )
    }
    d = triage([case], mandates=mandates)["case_1"]
    assert d.disposition is Disposition.ELIGIBLE
    assert "mandate_revoked" in d.flags


def test_a_customer_with_no_channel_is_flagged() -> None:
    case = make_case()
    d = triage([case], {"cus_1": customer(channels=[])})["case_1"]
    assert "no_contact_channel" in d.flags


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def test_recurring_outranks_a_larger_one_off() -> None:
    """A Rs 499 subscription is worth more than a Rs 999 one-off: it recurs."""
    small_recurring = make_case(
        "case_r", customer_id="cus_r", amount=49900, kind="recurring",
        rail=Rail.UPI_AUTOPAY, mandate_id="mdt_r",
    )
    big_one_off = make_case("case_o", customer_id="cus_o", amount=99900)
    queue = work_queue(list(triage([big_one_off, small_recurring]).values()))
    assert [d.case_id for d in queue] == ["case_r", "case_o"]
    assert queue[0].priority_paise == 49900 * (1 + ASSUMED_MANDATE_RESIDUAL_MONTHS)


def test_queue_order_is_stable_for_equal_priority() -> None:
    cases = [make_case(f"case_{i}", customer_id=f"cus_{i}") for i in range(5)]
    ids = [d.case_id for d in work_queue(list(triage(cases).values()))]
    assert ids == sorted(ids)


def test_ineligible_cases_never_reach_the_queue() -> None:
    cases = [
        make_case("case_ok", customer_id="cus_a"),
        make_case("case_pending", customer_id="cus_b", status=PaymentStatus.PENDING),
        make_case("case_tiny", customer_id="cus_c", amount=100),
    ]
    assert [d.case_id for d in work_queue(list(triage(cases).values()))] == ["case_ok"]

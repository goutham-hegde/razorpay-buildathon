"""The policy engine, tested as the pure function it is.

`next_action` takes a `CaseView` and returns one `Action`. That signature is the whole
reason this file needs no simulator, no ledger and no network: every test below constructs
a situation directly and asserts on the decision, so a failure names the decision that
broke rather than the run that happened to expose it.

Three groups, in the order the engine evaluates them:

  1. stopping rules, which must fire *before* the cause table gets a say
  2. the ambiguity gate, which is the only place the policy overrules the diagnoser
  3. the cause table, one test per cause, because the claim that nine causes each imply a
     different action is exactly the claim that has to be checked

Plus the bounds - quiet hours, the rolling frequency cap, the minimum gap - which are the
"compliant" half of the brief and are therefore asserted rather than described.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from reclaim.core.compliance import (
    CASE_HORIZON,
    CONTACT_WINDOW_DAYS,
    INCENTIVE_MIN_AMOUNT_PAISE,
    MAX_CHARGE_ATTEMPTS_PER_CASE,
    MAX_CHARGE_ATTEMPTS_RECURRING,
    MAX_CONTACTS_PER_CASE,
    MAX_CONTACTS_PER_WINDOW,
    MIN_CONTACT_GAP,
    MIN_ECONOMIC_AMOUNT_PAISE,
    within_contact_window,
)
from reclaim.core.detect import Detection, Disposition
from reclaim.core.diagnose import Diagnosis
from reclaim.core.policy import (
    CHARGE_OVER_REFERENCE_CONFIDENCE,
    TECH_BACKOFF,
    Action,
    CaseView,
    PolicyEngine,
)
from reclaim.domain import (
    Case,
    ErrorSource,
    ErrorStep,
    ObservedError,
    PaymentAttempt,
    PaymentStatus,
    Rail,
    RootCause,
)

RC = RootCause
PSPS = ("psp_alpha", "psp_beta", "psp_gamma")

#: Midday on the 10th. Inside the contact window, and far enough into the month that a
#: salary day on the 28th is still ahead and one on the 1st is next month.
T0 = datetime(2026, 8, 10, 12, 0)

#: A salary day that falls inside the 14-day horizon from T0. Chosen deliberately: a
#: payday on the 28th is three weeks out from the 10th, the policy correctly refuses to
#: schedule a charge past the horizon, and a test using it would be asserting the wrong
#: branch while looking like it asserted this one.
SALARY_DAY_IN_HORIZON = 20


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_case(
    rail: Rail = Rail.UPI_INTENT,
    kind: str = "one_time",
    amount: int = 49900,
    bank_reference: str | None = None,
    psp: str = "psp_alpha",
    opened_at: datetime = T0,
) -> Case:
    error = ObservedError(
        code="GATEWAY_ERROR",
        source=ErrorSource.BANK,
        step=ErrorStep.PAYMENT_AUTHORIZATION,
        reason="payment_failed",
        description="something went wrong",
        bank_reference=bank_reference,
    )
    attempt = PaymentAttempt(
        id="pay_1_0",
        case_id="case_1",
        customer_id="cus_1",
        amount_paise=amount,
        rail=rail,
        issuer="HDFC",
        psp=psp,
        created_at=opened_at,
        status=PaymentStatus.FAILED,
        error=error,
        attempt_no=0,
    )
    return Case(
        id="case_1",
        customer_id="cus_1",
        amount_paise=amount,
        opened_at=opened_at,
        kind=kind,
        rail=rail,
        issuer="HDFC",
        psp=psp,
        mandate_id="mdt_1" if kind == "recurring" else None,
        attempts=[attempt],
    )


def make_detection(
    disposition: Disposition = Disposition.ELIGIBLE,
    flags: tuple[str, ...] = (),
    amount: int = 49900,
) -> Detection:
    return Detection("case_1", disposition, "reason", amount, amount, flags)


def make_view(
    cause: RC = RC.ISSUER_TECHNICAL_DECLINE,
    confidence: float = 0.95,
    now: datetime = T0,
    **kw,
) -> CaseView:
    case = kw.pop("case", None) or make_case()
    detection = kw.pop("detection", None) or make_detection(amount=case.amount_paise)
    defaults = {
        "channels": ("whatsapp", "sms"),
        "known_psps": PSPS,
        "psps_tried": (case.psp,),
    }
    return CaseView(
        case=case,
        detection=detection,
        diagnosis=Diagnosis("case_1", cause, confidence, "because", "test", "test"),
        now=now,
        **{**defaults, **kw},
    )


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(PSPS)


# ---------------------------------------------------------------------------
# 1. Stopping rules - these must outrank the cause table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"detection": make_detection(Disposition.OPTED_OUT)}, "detection said not eligible"),
        ({"opted_out": True}, "customer withdrew consent"),
        ({"now": T0 + CASE_HORIZON}, "case reached the horizon"),
        ({"case": make_case(amount=MIN_ECONOMIC_AMOUNT_PAISE - 1)}, "below the economic floor"),
    ],
)
def test_a_stopping_rule_closes_the_case_whatever_the_cause_says(
    engine: PolicyEngine, kwargs: dict, why: str
) -> None:
    """Every one of these is a case the cause table would happily retry.

    The point of the assertion is the *ordering*: a bound that only applies when the policy
    has not thought of something more interesting to do is not a bound.
    """
    action = engine.next_action(make_view(RC.ISSUER_TECHNICAL_DECLINE, **kwargs))
    assert action.kind == "close", f"{why} should stop the case, got {action.kind}"
    assert action.terminal


def test_an_opted_out_customer_is_never_contacted_even_to_reauthorise(
    engine: PolicyEngine,
) -> None:
    """R5 in policy form. `mandate_revoked` is the cause most tempting to message about."""
    action = engine.next_action(make_view(RC.MANDATE_REVOKED, opted_out=True))
    assert action.kind == "close"


# ---------------------------------------------------------------------------
# 2. The ambiguity gate
# ---------------------------------------------------------------------------


def test_a_low_confidence_retry_over_a_bank_reference_is_held(engine: PolicyEngine) -> None:
    """The single most expensive mistake in the taxonomy, blocked without the diagnoser."""
    view = make_view(
        RC.ISSUER_TECHNICAL_DECLINE,
        confidence=CHARGE_OVER_REFERENCE_CONFIDENCE - 0.01,
        case=make_case(bank_reference="HDFC123456789012"),
    )
    action = engine.next_action(view)
    assert action.kind == "hold"
    assert action.status == "reconcile_hold"


def test_the_gate_needs_both_signals_not_either(engine: PolicyEngine) -> None:
    """Low confidence alone, and a bank reference alone, are both far too common to act on.

    16% of ordinary technical declines carry a reference. Gating on that alone would hold
    one failure in six for a human to look at, which is not a recovery system.
    """
    low_conf_no_ref = make_view(
        RC.ISSUER_TECHNICAL_DECLINE, confidence=CHARGE_OVER_REFERENCE_CONFIDENCE - 0.01
    )
    assert engine.next_action(low_conf_no_ref).kind == "retry"

    ref_but_confident = make_view(
        RC.ISSUER_TECHNICAL_DECLINE,
        confidence=CHARGE_OVER_REFERENCE_CONFIDENCE,
        case=make_case(bank_reference="HDFC123456789012"),
    )
    assert engine.next_action(ref_but_confident).kind == "retry"


def test_the_gate_does_not_fire_on_causes_that_never_charge(engine: PolicyEngine) -> None:
    """`insufficient_funds` at low confidence with a reference still waits for payday.

    The gate exists to stop a *charge* going out over evidence of a debit. Applying it to
    every cause would turn every uncertain diagnosis into a human's problem.
    """
    view = make_view(
        RC.INSUFFICIENT_FUNDS,
        confidence=0.3,
        case=make_case(bank_reference="HDFC123456789012"),
        salary_day=SALARY_DAY_IN_HORIZON,
    )
    assert engine.next_action(view).kind == "retry"


# ---------------------------------------------------------------------------
# 3. The cause table - nine causes, nine different actions
# ---------------------------------------------------------------------------


def test_ambiguous_debited_is_never_charged(engine: PolicyEngine) -> None:
    """The trap. A retry here *succeeds*, and the success is a duplicate debit."""
    action = engine.next_action(make_view(RC.AMBIGUOUS_DEBITED))
    assert action.kind == "escalate"
    assert action.status == "reconcile_hold"
    assert not action.moves_money


def test_ambiguous_debited_is_never_charged_at_any_confidence(engine: PolicyEngine) -> None:
    for confidence in (0.0, 0.5, 0.99, 1.0):
        action = engine.next_action(make_view(RC.AMBIGUOUS_DEBITED, confidence=confidence))
        assert not action.moves_money, f"charged an ambiguous debit at confidence {confidence}"


def test_risk_declined_stops_rather_than_retrying(engine: PolicyEngine) -> None:
    """Retrying argues with a fraud rule, and the rule wins by hard-blocking the customer."""
    action = engine.next_action(make_view(RC.RISK_DECLINED))
    assert action.kind == "close"
    assert not action.moves_money


def test_technical_decline_retries_on_the_declared_backoff(engine: PolicyEngine) -> None:
    for n, expected in enumerate(TECH_BACKOFF):
        action = engine.next_action(
            make_view(RC.ISSUER_TECHNICAL_DECLINE, charge_attempts=n)
        )
        assert action.kind == "retry"
        assert action.at == T0 + expected


def test_technical_decline_stops_charging_once_the_backoff_is_spent(
    engine: PolicyEngine,
) -> None:
    """Stops *charging*. It does not necessarily stop working the case.

    Once presenting is off the table the remaining lever is asking the customer, which is
    bounded by the contact caps and cannot halt anything. The assertion is therefore about
    money moving, not about the case closing.
    """
    action = engine.next_action(
        make_view(RC.ISSUER_TECHNICAL_DECLINE, charge_attempts=len(TECH_BACKOFF))
    )
    assert not action.moves_money


def test_routing_failure_reroutes_to_a_psp_not_yet_tried(engine: PolicyEngine) -> None:
    """The bank is fine. Waiting is the wrong lever; a different route is the right one."""
    action = engine.next_action(make_view(RC.PSP_ROUTING_FAILURE))
    assert action.kind == "reroute"
    assert action.psp != "psp_alpha"
    assert action.psp in PSPS


def test_routing_failure_falls_back_to_waiting_once_every_route_is_spent(
    engine: PolicyEngine,
) -> None:
    """Having failed on every route, the evidence no longer supports 'only our side'."""
    action = engine.next_action(make_view(RC.PSP_ROUTING_FAILURE, psps_tried=PSPS))
    assert action.kind == "retry"
    assert action.psp == "psp_alpha"


def test_insufficient_funds_waits_for_payday_rather_than_retrying_now(
    engine: PolicyEngine,
) -> None:
    """The case the whole taxonomy exists for.

    A naive scheduler retries in thirty minutes and buys a decline. The only lever that
    exists is *when*, and for a salaried customer the answer is a date we already have.
    """
    action = engine.next_action(
        make_view(RC.INSUFFICIENT_FUNDS, salary_day=SALARY_DAY_IN_HORIZON)
    )
    assert action.kind == "retry"
    assert action.at.day == SALARY_DAY_IN_HORIZON
    assert action.at > T0 + timedelta(days=7)


def test_insufficient_funds_never_presents_before_the_credit_lands(
    engine: PolicyEngine,
) -> None:
    """Presenting at 00:01 on payday is the hour a naive scheduler picks and the hour the
    money is not there yet."""
    action = engine.next_action(
        make_view(RC.INSUFFICIENT_FUNDS, salary_day=SALARY_DAY_IN_HORIZON)
    )
    assert action.kind == "retry"
    assert action.at.hour >= 12


def test_insufficient_funds_with_no_payday_inside_the_horizon_nudges_instead(
    engine: PolicyEngine,
) -> None:
    """A retry we already know will bounce is worth less than a message.

    The customer can pay from another account. A retry cannot.
    """
    # Salary on the 1st, and we are on the 10th - the next credit is three weeks out,
    # comfortably beyond the 14-day horizon.
    action = engine.next_action(make_view(RC.INSUFFICIENT_FUNDS, salary_day=1))
    assert action.kind == "contact"


def test_insufficient_funds_for_an_unsalaried_customer_waits_a_fixed_period(
    engine: PolicyEngine,
) -> None:
    """Lumpy income arrives on no calendar. The guess is spent like a guess."""
    action = engine.next_action(make_view(RC.INSUFFICIENT_FUNDS, salary_day=None))
    assert action.kind == "retry"
    assert action.at > T0 + timedelta(days=1)


def test_limit_exceeded_waits_for_the_cap_to_roll_over(engine: PolicyEngine) -> None:
    action = engine.next_action(make_view(RC.LIMIT_EXCEEDED))
    assert action.kind == "retry"
    assert action.at.date() > T0.date()


@pytest.mark.parametrize("cause", [RC.INSTRUMENT_INVALID, RC.MANDATE_REVOKED])
def test_a_dead_authorisation_is_never_charged_before_outreach(
    engine: PolicyEngine, cause: RC
) -> None:
    """No charge can succeed until the customer supplies a working instrument.

    Charging first is free money for the acquirer and nothing for us.
    """
    action = engine.next_action(make_view(cause))
    assert action.kind == "contact"
    assert action.template == "reauthorise"


@pytest.mark.parametrize("cause", [RC.INSTRUMENT_INVALID, RC.MANDATE_REVOKED])
def test_a_dead_authorisation_charges_once_the_customer_has_come_back(
    engine: PolicyEngine, cause: RC
) -> None:
    action = engine.next_action(
        make_view(cause, contacts_sent=1, engaged_at=T0, reauth_requested=True)
    )
    assert action.moves_money


def test_auth_abandoned_reaches_out_rather_than_retrying_silently(
    engine: PolicyEngine,
) -> None:
    """Nobody is there. A silent retry against an absent human is worth about five percent."""
    action = engine.next_action(make_view(RC.AUTH_ABANDONED))
    assert action.kind == "contact"


def test_auth_abandoned_charges_inside_the_window_where_the_customer_is_present(
    engine: PolicyEngine,
) -> None:
    action = engine.next_action(make_view(RC.AUTH_ABANDONED, contacts_sent=1, engaged_at=T0))
    assert action.moves_money
    assert action.at == T0


def test_auth_abandoned_does_not_charge_once_the_presence_window_has_closed(
    engine: PolicyEngine,
) -> None:
    """Outreach that landed yesterday is not a customer holding their phone today."""
    action = engine.next_action(
        make_view(
            RC.AUTH_ABANDONED,
            now=T0 + timedelta(days=1),
            contacts_sent=1,
            engaged_at=T0,
        )
    )
    assert not action.moves_money


def test_a_returning_customer_is_offered_the_rail_they_actually_complete(
    engine: PolicyEngine,
) -> None:
    """A customer who abandons every OTP screen is telling us about the OTP screen."""
    action = engine.next_action(
        make_view(
            RC.AUTH_ABANDONED,
            case=make_case(rail=Rail.CARD_3DS),
            contacts_sent=1,
            engaged_at=T0,
            preferred_rail=Rail.UPI_INTENT,
        )
    )
    assert action.rail is Rail.UPI_INTENT


def test_a_stored_mandate_is_never_switched_to_another_rail(engine: PolicyEngine) -> None:
    """There is nobody to hand the choice to, and the authorisation was never granted for it."""
    action = engine.next_action(
        make_view(
            RC.ISSUER_TECHNICAL_DECLINE,
            case=make_case(rail=Rail.UPI_AUTOPAY, kind="recurring"),
            preferred_rail=Rail.UPI_INTENT,
        )
    )
    assert action.rail is Rail.UPI_AUTOPAY


def test_a_dead_mandate_in_the_register_outranks_the_diagnosis(engine: PolicyEngine) -> None:
    """Detection read the mandate register. That is a fact; the diagnosis is a prediction."""
    action = engine.next_action(
        make_view(
            RC.ISSUER_TECHNICAL_DECLINE,
            case=make_case(rail=Rail.UPI_AUTOPAY, kind="recurring"),
            detection=make_detection(flags=("recurring", "mandate_revoked")),
        )
    )
    assert action.kind == "contact"
    assert action.template == "reauthorise"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_a_stored_mandate_gets_a_tighter_charge_budget_than_a_one_time_payment(
    engine: PolicyEngine,
) -> None:
    """A halted mandate forfeits months of revenue, not one invoice.

    The tighter budget is the agent's own arithmetic, not a guess at the rail's threshold -
    which the agent does not know and `eval.sensitivity` moves anyway.
    """
    assert MAX_CHARGE_ATTEMPTS_RECURRING < MAX_CHARGE_ATTEMPTS_PER_CASE

    # `limit_exceeded` on purpose: its schedule depends only on the attempt count, so this
    # asserts the budget and nothing else. Routing it through a cause with date arithmetic
    # would let a horizon interaction masquerade as a budget decision.
    recurring = make_view(
        RC.LIMIT_EXCEEDED,
        case=make_case(rail=Rail.UPI_AUTOPAY, kind="recurring"),
        charge_attempts=MAX_CHARGE_ATTEMPTS_RECURRING,
    )
    assert not engine.next_action(recurring).moves_money

    one_time = make_view(RC.LIMIT_EXCEEDED, charge_attempts=MAX_CHARGE_ATTEMPTS_RECURRING)
    assert engine.next_action(one_time).moves_money


def test_no_charge_is_scheduled_past_the_horizon(engine: PolicyEngine) -> None:
    """Waiting for a payday that falls outside the window we are willing to chase is not
    patience, it is a case that never closes."""
    action = engine.next_action(
        make_view(RC.ISSUER_TECHNICAL_DECLINE, now=T0 + CASE_HORIZON - timedelta(minutes=5))
    )
    assert action.at < T0 + CASE_HORIZON or action.terminal


# ---------------------------------------------------------------------------
# Contact bounds - the "compliant" half of the brief
# ---------------------------------------------------------------------------


def test_outreach_is_deferred_out_of_quiet_hours_rather_than_dropped(
    engine: PolicyEngine,
) -> None:
    """A nudge that would land at 03:00 is held until 09:00. Not cancelled - held."""
    night = datetime(2026, 8, 10, 3, 0)
    action = engine.next_action(
        make_view(RC.AUTH_ABANDONED, now=night, case=make_case(opened_at=night))
    )
    assert action.kind == "contact"
    assert within_contact_window(action.at)
    assert action.at.hour == 9


def test_outreach_after_the_window_closes_waits_for_the_next_morning(
    engine: PolicyEngine,
) -> None:
    evening = datetime(2026, 8, 10, 22, 30)
    action = engine.next_action(
        make_view(RC.AUTH_ABANDONED, now=evening, case=make_case(opened_at=evening))
    )
    assert action.kind == "contact"
    assert action.at.day == 11
    assert action.at.hour == 9


def worst_window(times: tuple[datetime, ...]) -> int:
    """The most messages in any rolling 7-day window - `core.guards`' own computation.

    Written out here rather than imported so that the test and the policy do not share an
    implementation. Two agreeing copies of a bug prove nothing.
    """
    ordered = sorted(times)
    window = timedelta(days=CONTACT_WINDOW_DAYS)
    worst = left = 0
    for right, t in enumerate(ordered):
        while t - ordered[left] > window:
            left += 1
        worst = max(worst, right - left + 1)
    return worst


def test_the_rolling_frequency_cap_is_proved_before_sending(engine: PolicyEngine) -> None:
    """The customer has already had the full weekly allowance on other cases.

    The policy is not required to *refuse* - the bounds defer messages rather than cancel
    them, so scheduling one for after the oldest ages out of the window is correct. What is
    required is that whatever it schedules does not breach the cap.
    """
    recent = tuple(T0 - timedelta(days=d) for d in (1, 2, 3))
    assert len(recent) == MAX_CONTACTS_PER_WINDOW
    action = engine.next_action(
        make_view(RC.AUTH_ABANDONED, customer_contact_times=recent)
    )
    if action.kind == "contact":
        assert worst_window(recent + (action.at,)) <= MAX_CONTACTS_PER_WINDOW


def test_the_frequency_cap_counts_messages_on_both_sides_of_the_proposed_time(
    engine: PolicyEngine,
) -> None:
    """Cases are worked in value order, not clock order.

    So a message already recorded can sit *later* on the simulated clock than the one being
    proposed. A backward-looking count would not see it, would send, and would leave a
    violation for `core.guards` to find after the money had already moved. This is the test
    that fails if the cap check is ever simplified to "how many have we sent so far".
    """
    later = tuple(T0 + timedelta(days=d) for d in (1, 2, 3))
    action = engine.next_action(make_view(RC.AUTH_ABANDONED, customer_contact_times=later))
    if action.kind == "contact":
        assert worst_window(later + (action.at,)) <= MAX_CONTACTS_PER_WINDOW
        assert action.at > max(later), (
            "scheduled into a window already full on the far side of the clock"
        )


def test_the_minimum_gap_holds_even_below_the_weekly_cap(engine: PolicyEngine) -> None:
    """One message under the cap is still one message too soon."""
    action = engine.next_action(
        make_view(
            RC.AUTH_ABANDONED,
            customer_contact_times=(T0 - timedelta(hours=2),),
        )
    )
    if action.kind == "contact":
        assert action.at - (T0 - timedelta(hours=2)) >= MIN_CONTACT_GAP


def test_a_case_gets_no_more_than_its_own_contact_allowance(engine: PolicyEngine) -> None:
    """One failed payment does not get to consume a customer's entire contact budget."""
    action = engine.next_action(
        make_view(RC.AUTH_ABANDONED, contacts_sent=MAX_CONTACTS_PER_CASE)
    )
    assert action.kind != "contact"
    assert action.terminal


def test_a_customer_with_no_channel_is_not_contacted(engine: PolicyEngine) -> None:
    action = engine.next_action(make_view(RC.AUTH_ABANDONED, channels=()))
    assert action.kind != "contact"


def test_the_highest_value_available_channel_is_chosen(engine: PolicyEngine) -> None:
    assert engine.next_action(make_view(RC.AUTH_ABANDONED)).channel == "whatsapp"
    assert (
        engine.next_action(make_view(RC.AUTH_ABANDONED, channels=("email", "sms"))).channel
        == "sms"
    )


def test_the_first_ask_carries_no_incentive(engine: PolicyEngine) -> None:
    """An incentive offered before a plain reminder is money spent on customers who would
    have paid anyway, and it teaches the rest that failing a payment earns a discount."""
    action = engine.next_action(
        make_view(RC.AUTH_ABANDONED, case=make_case(amount=999900))
    )
    assert action.kind == "contact"
    assert not action.with_incentive


def test_an_incentive_is_attached_to_the_last_ask_when_it_pays_for_itself(
    engine: PolicyEngine,
) -> None:
    big = engine.next_action(
        make_view(
            RC.AUTH_ABANDONED,
            case=make_case(amount=999900),
            contacts_sent=MAX_CONTACTS_PER_CASE - 1,
            now=T0 + timedelta(days=2),
        )
    )
    assert big.kind == "contact" and big.with_incentive

    small = engine.next_action(
        make_view(
            RC.AUTH_ABANDONED,
            case=make_case(amount=INCENTIVE_MIN_AMOUNT_PAISE - 1),
            contacts_sent=MAX_CONTACTS_PER_CASE - 1,
            now=T0 + timedelta(days=2),
        )
    )
    assert not small.with_incentive


# ---------------------------------------------------------------------------
# Properties that have to hold for every cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cause", list(RC))
def test_every_cause_produces_an_action(engine: PolicyEngine, cause: RC) -> None:
    """`next_action` never returns None.

    A policy that can return "nothing" needs a caller that knows what to do with nothing,
    and the case that falls through that branch is the case still open when the batch
    drains - which is exactly what R6 catches.
    """
    action = engine.next_action(make_view(cause))
    assert isinstance(action, Action)
    assert action.reason


@pytest.mark.parametrize("cause", list(RC))
def test_no_action_is_ever_scheduled_in_the_past(engine: PolicyEngine, cause: RC) -> None:
    """An action before the clock would make the executor's loop non-terminating."""
    view = make_view(cause, salary_day=SALARY_DAY_IN_HORIZON)
    action = engine.next_action(view)
    assert action.at >= view.now


@pytest.mark.parametrize("cause", list(RC))
def test_the_policy_is_deterministic(engine: PolicyEngine, cause: RC) -> None:
    """Same input, same decision, every time. This is why it can be audited at all."""
    view = make_view(cause, salary_day=SALARY_DAY_IN_HORIZON)
    assert engine.next_action(view) == engine.next_action(view)


@pytest.mark.parametrize("cause", [RC.AMBIGUOUS_DEBITED, RC.RISK_DECLINED])
def test_the_never_retry_causes_never_move_money_in_any_state(
    engine: PolicyEngine, cause: RC
) -> None:
    """Swept across the state space rather than spot-checked, because these two are the
    causes where a single missed branch is a duplicate debit or a blocked customer."""
    for attempts in range(MAX_CHARGE_ATTEMPTS_PER_CASE + 1):
        for contacts in range(MAX_CONTACTS_PER_CASE + 1):
            for engaged in (None, T0):
                for kind, rail in (("one_time", Rail.UPI_INTENT), ("recurring", Rail.UPI_AUTOPAY)):
                    view = make_view(
                        cause,
                        case=make_case(rail=rail, kind=kind),
                        charge_attempts=attempts,
                        contacts_sent=contacts,
                        engaged_at=engaged,
                    )
                    assert not engine.next_action(view).moves_money


def test_the_policy_reads_nothing_from_the_simulated_world() -> None:
    """The seal, at this module's boundary.

    `tests/test_seal.py` enforces it across all of `core/`; this asserts it where it would
    do the most damage, because a policy handed the answer would post excellent numbers
    right up until someone read the imports.
    """
    import reclaim.core.policy as policy

    source = policy.__file__
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "synth" not in text


def test_a_spent_charge_budget_falls_through_to_asking_rather_than_closing(
    engine: PolicyEngine,
) -> None:
    """The other half of the short recurring budget.

    Stopping early stops the harm; on its own it also stops pursuing the money. A message
    cannot halt a mandate and a presentation can, so once presenting is off the table the
    ask is made of the customer instead of the rail.
    """
    action = engine.next_action(
        make_view(
            RC.LIMIT_EXCEEDED,
            case=make_case(rail=Rail.UPI_AUTOPAY, kind="recurring"),
            charge_attempts=MAX_CHARGE_ATTEMPTS_RECURRING,
        )
    )
    assert action.kind == "contact"
    assert action.template == "pay_manually"


def test_a_spent_budget_closes_when_no_outreach_is_permitted(engine: PolicyEngine) -> None:
    """The fall-through is a preference, not an escape hatch from the contact caps."""
    action = engine.next_action(
        make_view(
            RC.LIMIT_EXCEEDED,
            case=make_case(rail=Rail.UPI_AUTOPAY, kind="recurring"),
            charge_attempts=MAX_CHARGE_ATTEMPTS_RECURRING,
            contacts_sent=MAX_CONTACTS_PER_CASE,
        )
    )
    assert action.kind == "close"


@pytest.mark.parametrize("cause", [RC.INSTRUMENT_INVALID, RC.MANDATE_REVOKED])
def test_a_dead_instrument_is_not_charged_on_unrelated_engagement(
    engine: PolicyEngine, cause: RC
) -> None:
    """Engagement alone does not mean the instrument was replaced.

    A customer can open a balance nudge without touching their card. Presenting against an
    instrument that is still closed spends an attempt to learn nothing - and on a stored
    mandate it spends a *consecutive* failure, which is the currency the rail halts you for.
    """
    action = engine.next_action(
        make_view(cause, contacts_sent=1, engaged_at=T0, reauth_requested=False)
    )
    assert not action.moves_money

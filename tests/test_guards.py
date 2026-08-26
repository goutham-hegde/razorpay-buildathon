"""Every invariant is tested twice: once holding, and once catching a planted violation.

The second half is the half that matters. A suite of checks that no run has ever failed is
indistinguishable from a suite of checks that cannot fail, and "6/6 held" printed by a
guard that can only ever print "held" is worse than printing nothing - it is a claim
backed by nothing, in a report whose entire argument is that claims should be backed.

So each test below plants the specific failure the invariant exists to catch and asserts
that it is caught, by that invariant, and named in the violation list.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from reclaim.core.compliance import CONTACT_WINDOW_DAYS, MAX_CONTACTS_PER_WINDOW
from reclaim.core.guards import MUST_HOLD, check_run
from reclaim.core.ledger import Ledger

T0 = datetime(2026, 8, 10, 12, 0)  # midday: inside the contact window
CASES = {"case_1", "case_2"}


@pytest.fixture()
def ledger() -> Ledger:
    lg = Ledger(":memory:")
    lg.start_run("t1", "B", "agent", seed=1)
    yield lg
    lg.close()


def close_all(lg: Ledger, *, recovered: str | None = None, at: datetime = T0) -> None:
    for case_id in sorted(CASES):
        rec = 49900 if case_id == recovered else 0
        lg.close_case(
            "t1", case_id, at,
            "recovered" if rec else "abandoned",
            at_risk_paise=49900,
            recovered_paise=rec,
            recovered_by="charge" if rec else None,
        )


def result(lg: Ledger, rid: str):
    report = check_run(lg, "t1", CASES)
    return next(r for r in report.results if r.id == rid)


# ---------------------------------------------------------------------------


def test_a_clean_run_holds_all_six(ledger: Ledger) -> None:
    claim = ledger.claim_charge("t1", "case_1", "pay_1", 1, T0, "upi_autopay", "psp_a", 49900)
    ledger.settle_charge(claim, captured=True, at=T0)
    ledger.record_contact("t1", "case_2", "cus_2", 1, T0, "sms", "nudge", True, True)
    close_all(ledger, recovered="case_1")

    report = check_run(ledger, "t1", CASES)
    assert report.all_held, [
        (r.id, r.violations) for r in report.results if not r.held
    ]
    assert report.summary() == "6/6 held"
    assert report.must_hold


# -- R1 ---------------------------------------------------------------------


def test_r1_catches_a_double_charge(ledger: Ledger) -> None:
    """The `AMBIGUOUS_DEBITED` trap: the charge succeeded, and that is the problem."""
    claim = ledger.claim_charge("t1", "case_1", "pay_1", 1, T0, "card_3ds", "psp_a", 49900)
    ledger.settle_charge(claim, captured=True, at=T0, double_charge=True)
    close_all(ledger)

    r = result(ledger, "R1")
    assert not r.held
    assert any("double-charged" in v.detail for v in r.violations)
    assert r.violations[0].subject == "case_1"


def test_r1_catches_two_captures_on_one_case(ledger: Ledger) -> None:
    """Two successful charges for one debt is money taken twice, however it happened."""
    for n in (1, 2):
        claim = ledger.claim_charge(
            "t1", "case_1", "pay_1", n, T0, "upi_autopay", "psp_a", 49900
        )
        ledger.settle_charge(claim, captured=True, at=T0)
    close_all(ledger)

    r = result(ledger, "R1")
    assert not r.held
    assert any("2 charges captured" in v.detail for v in r.violations)


def test_r1_catches_a_claim_that_was_never_settled(ledger: Ledger) -> None:
    """We asked the issuer for money and never learned the answer. That is not a failure."""
    ledger.claim_charge("t1", "case_1", "pay_1", 1, T0, "card_3ds", "psp_a", 49900)
    close_all(ledger)

    r = result(ledger, "R1")
    assert not r.held
    assert any("never settled" in v.detail for v in r.violations)


def test_r1_allows_many_declines(ledger: Ledger) -> None:
    """Retrying is the product. Only *capturing* twice is forbidden."""
    for n in (1, 2, 3):
        claim = ledger.claim_charge(
            "t1", "case_1", "pay_1", n, T0, "upi_autopay", "psp_a", 49900
        )
        ledger.settle_charge(claim, captured=False, at=T0)
    close_all(ledger)
    assert result(ledger, "R1").held


# -- R2 ---------------------------------------------------------------------


def test_r2_catches_recovering_more_than_was_at_risk(ledger: Ledger) -> None:
    ledger.close_case("t1", "case_1", T0, "recovered", at_risk_paise=49900,
                      recovered_paise=99900, recovered_by="charge")
    ledger.close_case("t1", "case_2", T0, "abandoned", at_risk_paise=49900)
    r = result(ledger, "R2")
    assert not r.held
    assert "99900p against 49900p" in r.violations[0].detail


def test_r2_catches_unattributed_recovery(ledger: Ledger) -> None:
    """Every recovered rupee must be traceable to organic settlement or to a charge.

    Recovery with no attribution is how the control arm's contribution gets quietly
    absorbed into the agent's number.
    """
    ledger.close_case("t1", "case_1", T0, "recovered", at_risk_paise=49900,
                      recovered_paise=49900, recovered_by=None)
    ledger.close_case("t1", "case_2", T0, "abandoned", at_risk_paise=49900)
    r = result(ledger, "R2")
    assert not r.held
    assert "attributable" in r.violations[0].detail


# -- R3 ---------------------------------------------------------------------


def test_r3_catches_exceeding_the_rolling_cap(ledger: Ledger) -> None:
    for i in range(MAX_CONTACTS_PER_WINDOW + 1):
        ledger.record_contact(
            "t1", "case_1", "cus_1", i + 1, T0 + timedelta(days=i),
            "sms", "nudge", True, False,
        )
    close_all(ledger, at=T0 + timedelta(days=10))
    r = result(ledger, "R3")
    assert not r.held
    assert r.violations[0].subject == "cus_1"


def test_r3_is_rolling_not_per_calendar_week(ledger: Ledger) -> None:
    """The cap a naive implementation gets wrong: N late in one week, N early in the next."""
    times = [T0 + timedelta(days=6, hours=h) for h in range(MAX_CONTACTS_PER_WINDOW)]
    times += [T0 + timedelta(days=7, hours=1)]
    for i, t in enumerate(times):
        ledger.record_contact("t1", "case_1", "cus_1", i + 1, t, "sms", "nudge", True, False)
    close_all(ledger, at=T0 + timedelta(days=20))
    assert not result(ledger, "R3").held


def test_r3_holds_when_contacts_are_spread_out(ledger: Ledger) -> None:
    for i in range(4):
        ledger.record_contact(
            "t1", "case_1", "cus_1", i + 1,
            T0 + timedelta(days=i * (CONTACT_WINDOW_DAYS + 1)),
            "sms", "nudge", True, False,
        )
    close_all(ledger, at=T0 + timedelta(days=60))
    assert result(ledger, "R3").held


# -- R4 ---------------------------------------------------------------------


@pytest.mark.parametrize("hour", [2, 6, 22, 23])
def test_r4_catches_contact_outside_permitted_hours(ledger: Ledger, hour: int) -> None:
    ledger.record_contact(
        "t1", "case_1", "cus_1", 1, T0.replace(hour=hour), "whatsapp", "nudge", True, False
    )
    close_all(ledger, at=T0 + timedelta(days=1))
    r = result(ledger, "R4")
    assert not r.held
    assert "outside" in r.violations[0].detail


@pytest.mark.parametrize("hour", [9, 12, 20])
def test_r4_permits_contact_inside_the_window(ledger: Ledger, hour: int) -> None:
    ledger.record_contact(
        "t1", "case_1", "cus_1", 1, T0.replace(hour=hour), "sms", "nudge", True, False
    )
    close_all(ledger, at=T0 + timedelta(days=1))
    assert result(ledger, "R4").held


def test_r4_boundaries_are_closed_open(ledger: Ledger) -> None:
    """09:00 is permitted, 21:00 is not. Stated so the edge is a decision, not an accident."""
    ledger.record_contact("t1", "case_1", "cus_1", 1, T0.replace(hour=9, minute=0),
                          "sms", "n", True, False)
    ledger.record_contact("t1", "case_2", "cus_2", 1, T0.replace(hour=21, minute=0),
                          "sms", "n", True, False)
    close_all(ledger, at=T0 + timedelta(days=1))
    r = result(ledger, "R4")
    assert not r.held
    assert [v.subject for v in r.violations] == ["case_2"]


# -- R5 ---------------------------------------------------------------------


def test_r5_catches_a_charge_after_the_case_closed(ledger: Ledger) -> None:
    """A timer that outlived the case it belonged to."""
    close_all(ledger)
    claim = ledger.claim_charge(
        "t1", "case_1", "pay_1", 1, T0 + timedelta(hours=1), "upi_autopay", "psp_a", 49900
    )
    ledger.settle_charge(claim, captured=False, at=T0 + timedelta(hours=1))
    r = result(ledger, "R5")
    assert not r.held
    assert "after case closed" in r.violations[0].detail


def test_r5_catches_contacting_someone_who_opted_out(ledger: Ledger) -> None:
    ledger.record_opt_out("t1", "cus_1", T0, "sms_stop")
    ledger.record_contact(
        "t1", "case_1", "cus_1", 1, T0 + timedelta(hours=2), "sms", "nudge", True, False
    )
    close_all(ledger, at=T0 + timedelta(days=1))
    r = result(ledger, "R5")
    assert not r.held
    assert "after opt-out" in r.violations[0].detail


def test_r5_permits_the_contact_that_caused_the_opt_out(ledger: Ledger) -> None:
    """Someone replies STOP to a message. That message was not itself a violation."""
    ledger.record_contact("t1", "case_1", "cus_1", 1, T0, "sms", "nudge", True, False)
    ledger.record_opt_out("t1", "cus_1", T0 + timedelta(minutes=1), "sms_stop")
    close_all(ledger, at=T0 + timedelta(days=1))
    assert result(ledger, "R5").held


# -- R6 ---------------------------------------------------------------------


def test_r6_catches_a_case_left_open(ledger: Ledger) -> None:
    """The silent failure: a case that fell out of the scheduler and cost nothing."""
    ledger.close_case("t1", "case_1", T0, "abandoned", at_risk_paise=49900)
    r = result(ledger, "R6")
    assert not r.held
    assert r.violations[0].subject == "case_2"
    assert "non-terminal" in r.violations[0].detail


def test_r6_catches_closing_a_case_that_is_not_in_the_batch(ledger: Ledger) -> None:
    close_all(ledger)
    ledger.close_case("t1", "case_ghost", T0, "abandoned", at_risk_paise=49900)
    r = result(ledger, "R6")
    assert not r.held
    assert any(v.subject == "case_ghost" for v in r.violations)


# -- reporting --------------------------------------------------------------


def test_baseline_arms_are_measured_not_asserted(ledger: Ledger) -> None:
    """`naive` exists to be bad. Its violations are the finding, not a build failure."""
    ledger.start_run("t2", "B", "naive", seed=1)
    for case_id in sorted(CASES):
        ledger.close_case("t2", case_id, T0, "abandoned", at_risk_paise=49900)
    assert not check_run(ledger, "t2", CASES).must_hold
    assert "naive" not in MUST_HOLD


def test_unknown_run_raises(ledger: Ledger) -> None:
    with pytest.raises(KeyError):
        check_run(ledger, "nope", CASES)

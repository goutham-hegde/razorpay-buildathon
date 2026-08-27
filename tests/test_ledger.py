"""The ledger's two structural promises: it is append-only, and it cannot be double-charged.

Both are enforced by the schema rather than by code, so both are tested by trying to break
them and asserting the database says no.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from reclaim.core.ledger import DuplicateAttempt, DuplicateContact, Ledger

AT = datetime(2026, 8, 10, 12, 0)


@pytest.fixture()
def ledger() -> Ledger:
    lg = Ledger(":memory:")
    lg.start_run("t1", "A", "agent", seed=1)
    yield lg
    lg.close()


# ---------------------------------------------------------------------------
# R1, structurally
# ---------------------------------------------------------------------------


def test_claiming_the_same_attempt_twice_raises(ledger: Ledger) -> None:
    """The whole point. The second claim must fail, not return a second permission."""
    ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "upi_autopay", "psp_a", 49900)
    with pytest.raises(DuplicateAttempt):
        ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "upi_autopay", "psp_a", 49900)


def test_the_duplicate_claim_leaves_no_row_behind(ledger: Ledger) -> None:
    """A rejected claim must not half-write. Otherwise the audit trail grows phantom rows."""
    ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "card_recurring", "psp_a", 49900)
    with pytest.raises(DuplicateAttempt):
        ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "card_recurring", "psp_a", 49900)
    assert len(ledger.charges("t1")) == 1


def test_successive_attempt_numbers_are_allowed(ledger: Ledger) -> None:
    """Uniqueness is per attempt, not per payment - retrying is the entire product."""
    for n in (1, 2, 3):
        ledger.claim_charge("t1", "case_1", "pay_1", n, AT, "upi_autopay", "psp_a", 49900)
    assert ledger.next_attempt_no("t1", "pay_1") == 4


def test_different_runs_may_replay_the_same_payment(ledger: Ledger) -> None:
    """Four arms replay one batch. They must not collide with each other."""
    ledger.start_run("t2", "A", "naive", seed=1)
    ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "upi_autopay", "psp_a", 49900)
    ledger.claim_charge("t2", "case_1", "pay_1", 1, AT, "upi_autopay", "psp_a", 49900)
    assert len(ledger.charges("t1")) == 1
    assert len(ledger.charges("t2")) == 1


def test_an_unsettled_claim_is_visible_as_unresolved(ledger: Ledger) -> None:
    """A charge that was authorised and never came back must not read as a clean failure.

    This is the `AMBIGUOUS_DEBITED` shape: we may have taken the money. The ledger has to
    be able to say "unknown" rather than picking a side.
    """
    claim = ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "card_3ds", "psp_a", 49900)
    assert ledger.charges("t1")[0]["outcome"] is None
    ledger.settle_charge(claim, captured=True, at=AT)
    assert ledger.charges("t1")[0]["outcome"] == "captured"


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["runs", "decisions", "charge_claims", "charge_results", "contacts", "case_outcomes"],
)
def test_updates_are_refused(ledger: Ledger, table: str) -> None:
    """An audit trail you can quietly correct is not an audit trail."""
    _populate(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.db.execute(f"UPDATE {table} SET run_id = 'tampered'")


@pytest.mark.parametrize(
    "table",
    ["runs", "decisions", "charge_claims", "charge_results", "contacts", "case_outcomes"],
)
def test_deletes_are_refused(ledger: Ledger, table: str) -> None:
    _populate(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.db.execute(f"DELETE FROM {table}")


def _populate(ledger: Ledger) -> None:
    claim = ledger.claim_charge("t1", "case_1", "pay_1", 1, AT, "upi_autopay", "psp_a", 49900)
    ledger.settle_charge(claim, captured=False, at=AT)
    ledger.record_decision("t1", "case_1", AT, "retry", "because")
    ledger.record_contact("t1", "case_1", "cus_1", 1, AT, "sms", "nudge", True, False)
    ledger.close_case("t1", "case_1", AT, "abandoned", 49900)


# ---------------------------------------------------------------------------
# Contacts and outcomes
# ---------------------------------------------------------------------------


def test_contact_numbers_are_unique_per_case(ledger: Ledger) -> None:
    ledger.record_contact("t1", "case_1", "cus_1", 1, AT, "sms", "nudge", True, False)
    with pytest.raises(DuplicateContact):
        ledger.record_contact("t1", "case_1", "cus_1", 1, AT, "sms", "nudge", True, False)


def test_a_case_cannot_be_closed_twice(ledger: Ledger) -> None:
    """Two terminal states for one case would make every aggregate double-count."""
    ledger.close_case("t1", "case_1", AT, "recovered", 49900, recovered_paise=49900,
                      recovered_by="charge")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.close_case("t1", "case_1", AT, "abandoned", 49900)


def test_rolling_contact_window_counts_only_the_window(ledger: Ledger) -> None:
    for i, days in enumerate((0, 3, 9)):
        ledger.record_contact(
            "t1", f"case_{i}", "cus_1", i + 1, AT + timedelta(days=days),
            "sms", "nudge", True, False,
        )
    at = AT + timedelta(days=9)
    assert ledger.contacts_in_window("t1", "cus_1", at, timedelta(days=7)) == 2
    assert ledger.contacts_in_window("t1", "cus_1", at, timedelta(days=30)) == 3


def test_opting_out_twice_is_not_an_error(ledger: Ledger) -> None:
    ledger.record_opt_out("t1", "cus_1", AT, "sms_stop")
    ledger.record_opt_out("t1", "cus_1", AT + timedelta(days=1), "sms_stop")
    assert set(ledger.opt_outs("t1")) == {"cus_1"}


def test_case_trail_is_ordered_and_complete(ledger: Ledger) -> None:
    """The audit trail for one case has to read as a story, in order."""
    _populate(ledger)
    trail = ledger.case_trail("t1", "case_1")
    assert {r["kind"] for r in trail} == {"decision", "charge", "contact", "closed"}
    assert [str(r["at"]) for r in trail] == sorted(str(r["at"]) for r in trail)


def test_export_writes_every_table(tmp_path, ledger: Ledger) -> None:
    """The `.db` is gitignored; the JSONL export is what backs a quoted number."""
    _populate(ledger)
    written = ledger.export_jsonl(tmp_path, run_id="t1")
    assert written["charge_claims"] == 1
    assert written["case_outcomes"] == 1
    assert (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").count("\n") >= 1


# ---------------------------------------------------------------------------
# Re-running a run
# ---------------------------------------------------------------------------


def test_reopening_a_recorded_run_is_refused(ledger: Ledger) -> None:
    """Run ids are deterministic, so a second replay collides - on purpose.

    An append-only store has no way to redo a run in place, and quietly appending a second
    `A-rules` would leave the results table reporting one arm twice. The collision is the
    ledger declining to rewrite history; `replay --fresh` starts a new trail instead.
    """
    with pytest.raises(sqlite3.IntegrityError, match="runs.run_id"):
        ledger.start_run("t1", "A", "agent", seed=1)


def test_a_fresh_ledger_discards_the_old_file(tmp_path) -> None:
    from reclaim.core.ledger import open_ledger

    with open_ledger("A", tmp_path) as lg:
        lg.start_run("A-rules", "A", "rules", seed=1)
    with open_ledger("A", tmp_path) as lg:
        assert [r["run_id"] for r in lg.runs("A")] == ["A-rules"]
    with open_ledger("A", tmp_path, fresh=True) as lg:
        assert lg.runs("A") == []
        lg.start_run("A-rules", "A", "rules", seed=1)  # the id is free again

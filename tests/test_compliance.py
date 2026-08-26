"""The contact window, including the timezone handling that a reviewer will poke at.

Quiet hours are the easiest compliance rule to implement almost-correctly. The two ways it
goes wrong are both silent: an unstated timezone, and a "defer" that turns into a "drop".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reclaim.core.compliance import (
    CONTACT_WINDOW_CLOSE,
    CONTACT_WINDOW_OPEN,
    LOCAL_TZ,
    as_local,
    next_contact_window,
    within_contact_window,
)


@pytest.mark.parametrize("hour", [9, 10, 14, 20])
def test_daytime_is_permitted(hour: int) -> None:
    assert within_contact_window(datetime(2026, 8, 10, hour, 30))


@pytest.mark.parametrize("hour", [0, 3, 8, 21, 22, 23])
def test_night_is_not(hour: int) -> None:
    assert not within_contact_window(datetime(2026, 8, 10, hour, 30))


def test_the_window_is_closed_open() -> None:
    """09:00 permitted, 21:00 not. An off-by-one here is a message at 21:00 sharp."""
    day = datetime(2026, 8, 10)
    assert within_contact_window(datetime.combine(day, CONTACT_WINDOW_OPEN))
    assert not within_contact_window(datetime.combine(day, CONTACT_WINDOW_CLOSE))


def test_an_aware_utc_time_is_judged_in_local_hours() -> None:
    """03:30 UTC is 09:00 IST - permitted. Judging it in UTC would refuse a legal send.

    The reverse error is the dangerous one: 18:00 UTC is 23:30 IST. Treating an aware
    datetime as if it were local wall-clock would wave that through.
    """
    assert within_contact_window(datetime(2026, 8, 10, 3, 30, tzinfo=timezone.utc))
    assert not within_contact_window(datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc))


def test_as_local_leaves_naive_times_alone() -> None:
    naive = datetime(2026, 8, 10, 14, 0)
    assert as_local(naive) == naive


def test_as_local_converts_aware_times() -> None:
    aware = datetime(2026, 8, 10, 3, 30, tzinfo=timezone.utc)
    assert as_local(aware) == datetime(2026, 8, 10, 9, 0)


def test_local_tz_is_loadable() -> None:
    """Guards against the Windows trap: no system zone database, so `tzdata` is required."""
    assert LOCAL_TZ.utcoffset(datetime(2026, 8, 10)) == timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# Deferral
# ---------------------------------------------------------------------------


def test_a_message_at_3am_is_held_until_9am_not_dropped() -> None:
    """Deferring costs a few hours. Dropping forfeits the case."""
    assert next_contact_window(datetime(2026, 8, 10, 3, 0)) == datetime(2026, 8, 10, 9, 0)


def test_a_message_at_11pm_rolls_to_the_next_morning() -> None:
    assert next_contact_window(datetime(2026, 8, 10, 23, 30)) == datetime(2026, 8, 11, 9, 0)


def test_a_message_inside_the_window_is_not_delayed() -> None:
    at = datetime(2026, 8, 10, 14, 12, 30)
    assert next_contact_window(at) == at


def test_deferral_always_lands_inside_the_window() -> None:
    """Property: whatever goes in, what comes out is sendable."""
    start = datetime(2026, 8, 10, 0, 0)
    for minutes in range(0, 60 * 48, 37):
        assert within_contact_window(next_contact_window(start + timedelta(minutes=minutes)))


def test_deferral_never_moves_a_message_backwards() -> None:
    start = datetime(2026, 8, 10, 0, 0)
    for minutes in range(0, 60 * 48, 37):
        at = start + timedelta(minutes=minutes)
        assert next_contact_window(at) >= at

"""The bounds. Every stopping rule and contact restriction the agent operates under.

This file exists as a separate module for one reason: the track asks for "compliant
escalation, stopping rules". Those are claims. Putting every bound in one short, readable
file turns them into something a reviewer can check in thirty seconds, and lets
`core.guards` assert against the same constants the policy obeys rather than against a
second copy that can drift.

Nothing here is learned, generated or tuned. These are declared limits.

WHERE THE NUMBERS COME FROM
---------------------------
Contact windows follow India's commercial-communication rules: TRAI's TCCCPR framework
restricts promotional messaging to daytime hours, with 09:00-21:00 the window operators
converge on. A recovery nudge that carries an incentive is promotional in substance
whatever it is labelled, so the strict window is applied to *all* outreach here rather
than arguing the transactional exemption. Erring tight costs a little recovery and is the
defensible side to err on.

Frequency and attempt caps are not regulatory. They are a stated risk appetite - the
number of times this business is willing to chase one customer for one failed payment
before the chasing itself becomes the problem. They are deliberately conservative.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Contact restrictions - invariants R3 and R4 assert these
# ---------------------------------------------------------------------------

#: The zone quiet hours are expressed in. Stated explicitly because a quiet-hours rule
#: with an unstated timezone is not a rule - "no messages after 21:00" is meaningless
#: until you say whose 21:00.
#:
#: Batch timestamps are naive and are wall-clock in this zone. An aware datetime is
#: converted before the window is applied, so a caller passing UTC gets the right answer
#: rather than a silently wrong one.
#:
#: Requires the `tzdata` package on Windows, which ships no system zone database. See the
#: D2 log entry.
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

#: Outreach is permitted only inside [CONTACT_WINDOW_OPEN, CONTACT_WINDOW_CLOSE) local.
CONTACT_WINDOW_OPEN = time(9, 0)
CONTACT_WINDOW_CLOSE = time(21, 0)

#: Maximum messages to one customer in any rolling window of CONTACT_WINDOW_DAYS.
#: Rolling, not per-calendar-week: a cap that resets on Monday lets you send three on
#: Sunday night and three more on Monday morning, which is six in twelve hours and
#: obviously not what the cap meant.
MAX_CONTACTS_PER_WINDOW = 3
CONTACT_WINDOW_DAYS = 7

#: Minimum gap between two messages to the same customer, regardless of the rolling cap.
MIN_CONTACT_GAP = timedelta(hours=20)


# ---------------------------------------------------------------------------
# Stopping rules - when the agent gives up
# ---------------------------------------------------------------------------

#: Hard ceiling on charge attempts per case, across every rail and PSP. The card schemes
#: impose their own budget on top of this; see `synth.Calibration.card_retry_budget`. The
#: agent does not know that number and must not be tuned to sit exactly under it.
MAX_CHARGE_ATTEMPTS_PER_CASE = 4

#: Ceiling on outreach per case. Lower than the 7-day customer cap on purpose: one failed
#: payment does not get to consume a customer's entire contact budget.
MAX_CONTACTS_PER_CASE = 2

#: How long a case stays open before it is written off. Chasing a three-week-old failed
#: payment annoys the customer more than the money is worth.
CASE_HORIZON = timedelta(days=14)

#: Below this, the cost of recovery exceeds the amount at risk and the case is closed
#: unattempted. One charge attempt plus one WhatsApp message already costs Rs 3.35.
MIN_ECONOMIC_AMOUNT_PAISE = 2000


# ---------------------------------------------------------------------------
# The agent's own beliefs about value
# ---------------------------------------------------------------------------

#: What the agent assumes a halted mandate costs in forfeited future revenue, in months.
#:
#: The world has its own figure (`synth.Calibration.mandate_residual_months`) and the two
#: are deliberately NOT equal. The agent is not allowed to know the true residual, so it
#: uses a conservative in-house estimate. If these matched, a reviewer would be right to
#: ask whether the policy had been handed the answer.
ASSUMED_MANDATE_RESIDUAL_MONTHS = 6


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def as_local(at: datetime) -> datetime:
    """Interpret `at` as local wall-clock.

    Naive datetimes are already local by convention. Aware ones are converted, so that a
    caller who passes UTC does not accidentally get their quiet hours checked against the
    wrong clock - which would pass silently and only show up as messages at 02:30 IST.
    """
    return at if at.tzinfo is None else at.astimezone(LOCAL_TZ).replace(tzinfo=None)


def within_contact_window(at: datetime) -> bool:
    """True if `at` falls inside permitted contact hours."""
    t = as_local(at).time()
    return CONTACT_WINDOW_OPEN <= t < CONTACT_WINDOW_CLOSE


def next_contact_window(at: datetime) -> datetime:
    """The earliest permitted contact time at or after `at`.

    Used by the scheduler to defer rather than drop: a message that would land at 03:00
    is not cancelled, it is held until 09:00.
    """
    at = as_local(at)
    if within_contact_window(at):
        return at
    if at.time() < CONTACT_WINDOW_OPEN:
        return at.replace(
            hour=CONTACT_WINDOW_OPEN.hour,
            minute=CONTACT_WINDOW_OPEN.minute,
            second=0,
            microsecond=0,
        )
    nxt = at + timedelta(days=1)
    return nxt.replace(
        hour=CONTACT_WINDOW_OPEN.hour,
        minute=CONTACT_WINDOW_OPEN.minute,
        second=0,
        microsecond=0,
    )

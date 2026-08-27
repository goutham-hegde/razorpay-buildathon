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

#: Tighter ceiling on rails that fire against a stored mandate.
#:
#: Not a second guess at the rail's halt threshold - the agent does not know that number,
#: and the whole point is that it must not need to. It falls out of the agent's own
#: arithmetic: a halted mandate forfeits ASSUMED_MANDATE_RESIDUAL_MONTHS of revenue, which
#: on a subscription is several times this month's ticket. When the downside of one more
#: failed presentation is that large, the rational budget is smaller than on a one-time
#: payment where the only thing at stake is the invoice itself.
#:
#: This was 3, then 1, and is 2. The reason it moved is the useful part. The rail halts a
#: mandate after some number of consecutive failures that we do not get to see, and the
#: budget's whole job is to sit a sensible distance from a cliff of unknown position.
#:
#: The jitter moves that threshold by exactly one unit, so it is always 3, 4 or 5. Since a
#: recurring case arrives already one failed presentation deep, the budget maps to total
#: presentations directly, and the choice is genuinely structural rather than a matter of
#: degree:
#:
#:   3   four presentations. Halts in every world where the threshold is 4 or below, which
#:       is most of them. This is what the naive arm does, and it is why the naive arm
#:       destroys 59.9% of the recurring book.
#:   1   two presentations, under the lowest threshold the jitter produces, so it halts
#:       nothing in any world. It also gives up 4.2 points of recovery rate and loses to
#:       naive outright in the four worlds where the threshold is 5 and naive gets away
#:       with it - the claimed ordering then holds in only 16 of 20.
#:   2   three presentations. Recovers 51.7% against 47.5%, and the claimed ordering holds
#:       in 20 of 20. The cost is exposure in the seven worlds where the threshold is 3:
#:       there it halts up to 39.5% of the book and net lift runs to -Rs 47 lakh.
#:
#: 2 is the choice, and the reasoning is worth being explicit about because it is a
#: judgement rather than a derivation. A budget of 1 buys a distribution with no left tail;
#: what it costs is recovery in every world, including the two-thirds where the tail never
#: materialises. Both numbers are reported - the tail is the `worlds w/ halt` column of the
#: sensitivity run, sitting in plain sight next to the ordering claim - so this trades a
#: visible risk against a measured gain rather than hiding one to claim the other.
#:
#: A selective version was built and measured: allow the second presentation only when the
#: diagnosed cause is one that clears on its own - an outage ending, a route recovering, a
#: balance arriving, a cap rolling over - on the theory that a *successful* second attempt
#: resets the rail's counter and so costs nothing. It did not work. Recovery came out at
#: 51.5% against 51.7%, the same four double charges, and an identical 7 of 20 worlds with
#: halts. A second presentation that fails is a strike whatever motivated it, and the halt
#: rate is set by how often it fails rather than by why it was made. The code was removed:
#: it earned nothing and it was not free to read.
#:
#: Stopping early does not mean giving up on the money. `core.policy` falls through to
#: outreach when the charge budget is spent, because asking the customer to pay cannot
#: halt a mandate and presenting again can.
MAX_CHARGE_ATTEMPTS_RECURRING = 2

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

#: What the agent assumes it costs to put a case in front of a human. Again deliberately
#: not the world's figure (`synth.Calibration.cost_per_human_escalation_paise`), and
#: deliberately on the high side: an estimate that flatters escalation would let the policy
#: escalate its way out of every hard decision.
ASSUMED_ESCALATION_COST_PAISE = 12000

#: What an incentive attached to an outreach message costs us.
#:
#: The simulated world raises engagement when an incentive is attached and charges nothing
#: for it. Left alone that makes incentives free, and a free lever is not a decision. The
#: eval harness therefore bills this figure - the agent's own declared cost - against any
#: case where the policy attached one, so that "was the incentive worth it" is a question
#: the results table can actually answer.
INCENTIVE_COST_PAISE = 2500

#: Below this at-risk amount an incentive is a fifth of the invoice and the unit economics
#: stop working, whatever it does to engagement.
INCENTIVE_MIN_AMOUNT_PAISE = 25000

#: Channels in descending order of how reliably this business sees customers act on them.
#: A stated belief about our own audience, not something learned from the simulator.
CHANNEL_PREFERENCE = ("whatsapp", "sms", "email")


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

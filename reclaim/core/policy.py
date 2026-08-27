"""The policy engine: a diagnosis becomes a bounded sequence of actions.

This is the part the track's brief is actually about - "compliant escalation, stopping
rules, and an audit trail". Diagnosis names the problem; this decides what to do about it,
and refuses to do anything the bounds in `core.compliance` forbid.

DELIBERATELY NOT A MODEL
------------------------
`core.diagnose` calls a language model. Nothing here does, and that is a design decision
rather than an omission. Retry timing is a policy, budgets are arithmetic, and anything
that moves money is plain code behind a gate. A generated retry schedule would be
unreviewable, untestable, and different every time it ran - and the one thing a payments
reviewer will ask about a recovery agent is what stops it doing something stupid at three
in the morning. The answer has to be a constant in a file they can read.

The consequence worth stating out loud: `rules` and `agent` are the *same* engine. They
differ only in which `Diagnoser` produced the input. Whatever gap the results table shows
between them is attributable to diagnosis quality and to nothing else, because there is
nothing else different.

THE SHAPE OF A POLICY
---------------------
`next_action` is a pure function of what the agent knows - a `CaseView` - and returns a
single next `Action`. It is not a plan. A plan computed up front would have to guess at the
outcome of its own first step, and the interesting cases are exactly the ones where step
two depends on step one: outreach that lands makes a charge worth attempting, and outreach
that does not makes the same charge worthless.

Being a pure function is what makes the whole thing testable without a simulator: hand it a
`CaseView` and assert on the `Action`. `tests/test_policy.py` does this for every cause.

WHAT EACH CAUSE BUYS
--------------------
The nine causes exist because each implies a materially different action. This table is the
product thesis, and a naive arm that retries everything identically is the control for it:

    issuer_technical_decline  retry on a short backoff; outages here are minutes, not days
    psp_routing_failure       re-route to a PSP we have not tried; the bank is fine
    insufficient_funds        wait for payday. The lever is *when*, and retrying now is
                              guaranteed to fail and costs a fee to find out
    limit_exceeded            wait for the daily cap to roll over
    auth_abandoned            nobody is there - reach out, then charge inside the window
                              where they are actually looking at their phone
    instrument_invalid        no charge can succeed; ask for a new instrument
    mandate_revoked           no charge can succeed; ask for a fresh authorisation
    risk_declined             stop. Retrying argues with a fraud rule and hard-blocks the
                              customer
    ambiguous_debited         never charge. Hold and reconcile, because the retry succeeds
                              and that success is a duplicate debit

SEALED. Imports nothing from the simulated world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from reclaim.core.compliance import (
    CASE_HORIZON,
    CHANNEL_PREFERENCE,
    CONTACT_WINDOW_DAYS,
    INCENTIVE_MIN_AMOUNT_PAISE,
    MAX_CHARGE_ATTEMPTS_PER_CASE,
    MAX_CHARGE_ATTEMPTS_RECURRING,
    MAX_CONTACTS_PER_CASE,
    MAX_CONTACTS_PER_WINDOW,
    MIN_CONTACT_GAP,
    MIN_ECONOMIC_AMOUNT_PAISE,
    next_contact_window,
    within_contact_window,
)
from reclaim.core.detect import Detection
from reclaim.core.diagnose import Diagnosis
from reclaim.domain import (
    HUMAN_PRESENT_RAILS,
    RECURRING_RAILS,
    Case,
    Rail,
    RootCause,
)

RC = RootCause


# ---------------------------------------------------------------------------
# Timing constants - the policy's own beliefs about when to act
# ---------------------------------------------------------------------------

#: Backoff for a technical decline. Short, because issuer and switch incidents resolve in
#: minutes to a couple of hours, and a retry that waits a day has missed the window in
#: which the answer would have changed. Three steps, widening, so that a longer incident
#: still gets one attempt on the far side of it.
TECH_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=20),
    timedelta(hours=2),
    timedelta(hours=8),
)

#: A routing failure is our own switch, and the fix is a different route rather than time.
#: There is no reason to wait beyond the few minutes it takes to fail over.
REROUTE_DELAY = timedelta(minutes=5)

#: Hour of the day a balance retry is presented. Salary credits land through the morning
#: banking window, so the first safe presentation is the afternoon of that day - and not
#: 00:01, which is the hour a naive scheduler picks and the hour the money is not there.
FUNDS_RETRY_HOUR = time(14, 0)

#: Extra wait before a *second* balance retry, when the first one still bounced.
FUNDS_RETRY_BACKOFF = timedelta(days=2)

#: When the customer has no predictable payday, this is how long to wait before trying
#: again. Lumpy income arrives on no calendar, so this is a guess and is treated as one:
#: the case gets fewer attempts than a salaried one, not more.
UNSCHEDULED_FUNDS_WAIT = timedelta(days=3)

#: A daily cap rolls over at midnight; present the next day once the banking day is open.
LIMIT_RETRY_HOUR = time(10, 30)

#: How long after outreach engages the customer is realistically still holding their phone.
#: The charge has to land inside this or the outreach was spent for nothing. Deliberately
#: shorter than the world's own presence window - being early is free, being late is not.
PRESENCE_WINDOW = timedelta(hours=4)

#: How long to wait for a customer to act on a message before deciding they will not.
CONTACT_RESPONSE_WAIT = timedelta(hours=30)

#: The bar a diagnosis must clear to present a charge against a payment whose failure came
#: back carrying a bank reference.
#:
#: Written as a bar to clear rather than a threshold to fall under, because that is the
#: direction the asymmetry runs. A missed recovery costs the invoice. A duplicate debit
#: costs a refund, an unwind, and a customer who now distrusts the payment page. A
#: diagnoser that cannot get above this on a timeout is telling us it cannot tell, and "I
#: cannot tell" is not a mandate to take the money a second time.
#:
#: The first version of this constant was phrased the other way round - "below 0.55 treat
#: it as a guess" - and it missed twelve real ambiguous debits by a hundredth of a point,
#: because the keyword diagnoser happens to emit exactly 0.55 on the rule those cases land
#: on. Both framings are arbitrary at the margin; only one of them puts the arbitrariness
#: on the safe side.
CHARGE_OVER_REFERENCE_CONFIDENCE = 0.75


# ---------------------------------------------------------------------------
# What the policy is allowed to know, and what it decides
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """One thing to do, and when. The unit the executor consumes and the ledger records."""

    #: retry | reroute | contact | escalate | hold | close
    kind: str
    at: datetime
    reason: str

    psp: str | None = None
    rail: Rail | None = None
    channel: str | None = None
    template: str | None = None
    with_incentive: bool = False

    #: Terminal status to close the case as, for `kind` in {"hold", "escalate", "close"}.
    status: str = "abandoned"

    @property
    def terminal(self) -> bool:
        return self.kind in ("hold", "escalate", "close")

    @property
    def moves_money(self) -> bool:
        return self.kind in ("retry", "reroute")


@dataclass(frozen=True, slots=True)
class CaseView:
    """Everything the policy knows about one case at one instant.

    Assembled by the caller from the batch files and the ledger. Nothing in here comes
    from the simulated world - `charges`, `contacts` and `engaged_at` are all things a real
    recovery system observes about its own actions.
    """

    case: Case
    detection: Detection
    diagnosis: Diagnosis
    now: datetime

    #: Charge attempts this policy has already made on this case.
    charge_attempts: int = 0
    #: PSPs already presented against, including the one that originally failed.
    psps_tried: tuple[str, ...] = ()

    #: Outreach already sent on this case.
    contacts_sent: int = 0
    #: Every message sent to this customer, on any case, in this run. Used to prove the
    #: rolling frequency cap before sending rather than to discover it afterwards.
    customer_contact_times: tuple[datetime, ...] = ()
    #: When outreach last visibly worked - a link clicked, an app opened. Observable to any
    #: real system; it is the whole reason engagement is measurable.
    engaged_at: datetime | None = None
    #: True once the customer has been asked for a fresh instrument or mandate and engaged.
    reauth_requested: bool = False

    opted_out: bool = False
    channels: tuple[str, ...] = ()
    salary_day: int | None = None
    #: The rail this customer completes most reliably, from their own record. A business
    #: fact about our own book, not something the policy is told by the simulator.
    preferred_rail: Rail | None = None
    #: PSPs this business can route through at all.
    known_psps: tuple[str, ...] = ()

    @property
    def horizon(self) -> datetime:
        return self.case.opened_at + CASE_HORIZON

    @property
    def recurring(self) -> bool:
        return self.case.rail in RECURRING_RAILS or self.case.kind == "recurring"

    @property
    def charge_budget(self) -> int:
        return (
            MAX_CHARGE_ATTEMPTS_RECURRING if self.recurring else MAX_CHARGE_ATTEMPTS_PER_CASE
        )

    @property
    def present_until(self) -> datetime | None:
        return self.engaged_at + PRESENCE_WINDOW if self.engaged_at else None

    @property
    def mandate_dead(self) -> bool:
        """Detection saw the mandate is not active. No charge against it can succeed."""
        return any(f.startswith("mandate_") and f != "mandate_missing" for f in self.detection.flags)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Turns a diagnosis into the next bounded action. Deterministic, and no model."""

    def __init__(self, known_psps: tuple[str, ...] = ()) -> None:
        self.known_psps = known_psps

    # -- entry point -------------------------------------------------------

    def next_action(self, view: CaseView) -> Action:
        """The single next thing to do. Always returns something; never returns None.

        A policy that can return "nothing" needs a caller that knows what to do with
        nothing, and the case that falls through that branch is the case that is still
        open when the batch drains - which is exactly what invariant R6 catches. Every
        path here ends in either a scheduled action or a terminal one.
        """
        if (stop := self._stopping_rule(view)) is not None:
            return stop
        if (gate := self._ambiguity_gate(view)) is not None:
            return gate
        return self._by_cause(view)

    # -- stopping rules ----------------------------------------------------

    def _stopping_rule(self, view: CaseView) -> Action | None:
        """The bounds, checked before the cause is even looked at.

        Deliberately first. A stopping rule that only applies when the policy has not
        thought of something more interesting to do is not a stopping rule.
        """
        if not view.detection.eligible:
            return Action("close", view.now, str(view.detection.reason), status="not_eligible")

        if view.opted_out:
            return Action(
                "close",
                view.now,
                "customer has withdrawn consent; no further action is permitted",
                status="abandoned",
            )

        if view.now >= view.horizon:
            return Action(
                "close",
                view.now,
                f"case reached the {CASE_HORIZON.days}-day horizon unrecovered",
                status="abandoned",
            )

        if view.case.amount_paise < MIN_ECONOMIC_AMOUNT_PAISE:
            return Action(
                "close",
                view.now,
                f"at risk {view.case.amount_paise}p is below the economic floor",
                status="abandoned",
            )
        return None

    # -- the double-charge gate -------------------------------------------

    def _ambiguity_gate(self, view: CaseView) -> Action | None:
        """Refuse to charge when the evidence says the money may already have moved.

        Defence in depth, and the only place the policy second-guesses the diagnoser. The
        model is right about `ambiguous_debited` most of the time, and the cost of the
        residual mistakes is asymmetric enough to be worth a second, independent check:
        a missed recovery costs the invoice, a duplicate debit costs a refund, an unwind
        and a customer who now distrusts the payment page.

        The gate fires only when two things are true at once - the diagnosis did not clear
        the confidence bar, and the failure carried a bank reference, which is the
        observable that tends to come back when money actually moved. Either alone is far
        too common to act on. Both together, on a cause whose recommended action is "charge
        it again", is the shape of the one error worth being paranoid about.

        Note what this does *not* catch. Roughly a fifth of real debits come back with no
        reference at all, and nothing observable separates those from a plain timeout. A
        diagnoser that cannot read the description will double-charge them, and no gate
        bolted on afterwards can prevent it. That residue is the measurement the rules arm
        exists to produce.
        """
        if view.diagnosis.root_cause is RC.AMBIGUOUS_DEBITED:
            return None  # already handled by the cause table, which never charges
        if view.diagnosis.root_cause not in (RC.ISSUER_TECHNICAL_DECLINE, RC.PSP_ROUTING_FAILURE):
            return None
        if view.diagnosis.confidence >= CHARGE_OVER_REFERENCE_CONFIDENCE:
            return None
        attempt = view.case.attempts[-1] if view.case.attempts else None
        if attempt is None or attempt.error is None or not attempt.error.bank_reference:
            return None
        return Action(
            "hold",
            view.now,
            f"diagnosed {view.diagnosis.root_cause} at {view.diagnosis.confidence:.2f} "
            f"confidence, below the {CHARGE_OVER_REFERENCE_CONFIDENCE:.2f} needed to charge "
            f"over a bank reference - treating as a possible completed debit and holding "
            f"for reconciliation rather than presenting again",
            status="reconcile_hold",
        )

    # -- the cause table ---------------------------------------------------

    def _by_cause(self, view: CaseView) -> Action:
        cause = view.diagnosis.root_cause

        # Detection already established that the stored authorisation is gone. That is a
        # fact from the mandate register, not a prediction, so it outranks the diagnosis.
        if view.mandate_dead and cause not in (RC.AMBIGUOUS_DEBITED, RC.RISK_DECLINED):
            return self._reauthorise(view, "the mandate register says this authorisation is not active")

        handler = {
            RC.AMBIGUOUS_DEBITED: self._ambiguous,
            RC.RISK_DECLINED: self._risk,
            RC.INSTRUMENT_INVALID: self._needs_new_authorisation,
            RC.MANDATE_REVOKED: self._needs_new_authorisation,
            RC.AUTH_ABANDONED: self._abandoned,
            RC.INSUFFICIENT_FUNDS: self._insufficient_funds,
            RC.LIMIT_EXCEEDED: self._limit,
            RC.PSP_ROUTING_FAILURE: self._routing,
            RC.ISSUER_TECHNICAL_DECLINE: self._technical,
        }[cause]
        return handler(view)

    # -- never charge ------------------------------------------------------

    def _ambiguous(self, view: CaseView) -> Action:
        """The trap. A retry here succeeds, and the success is a duplicate debit.

        Escalated rather than merely abandoned, because "we may have taken this customer's
        money and cannot tell" is not a state a batch job gets to close on its own. The
        settlement file resolves it; a human reads the settlement file. That is what the
        escalation cost in the results table is buying, and it is the only place this
        policy spends it.
        """
        return Action(
            "escalate",
            view.now,
            "possible completed debit with no confirmation - charging again would take the "
            "money twice; queued for reconciliation against the settlement file",
            status="reconcile_hold",
        )

    def _risk(self, view: CaseView) -> Action:
        """Stop. Retrying argues with a fraud rule, and the rule wins by hard-blocking."""
        return Action(
            "close",
            view.now,
            "declined by a risk rule - further presentations argue with the rule and risk "
            "a permanent block on this customer",
            status="abandoned",
        )

    # -- needs the customer to come back -----------------------------------

    def _needs_new_authorisation(self, view: CaseView) -> Action:
        return self._reauthorise(
            view,
            "no charge can succeed until the customer supplies a working instrument or a "
            "fresh authorisation",
        )

    def _reauthorise(self, view: CaseView, why: str) -> Action:
        """Outreach, then exactly one charge once they have actually come back.

        The order matters and is the whole point: charging first is free money for the
        acquirer and nothing for us, because the authorisation the charge needs does not
        exist yet.
        """
        if self._came_back(view) and view.charge_attempts < view.charge_budget:
            return self._charge(
                view,
                "customer engaged with the re-authorisation request; presenting once against "
                "the refreshed authorisation",
                at=max(view.now, view.engaged_at or view.now),
            )
        if (contact := self._contact(view, "reauthorise", why)) is not None:
            return contact
        return Action(
            "close",
            view.now,
            "outreach exhausted and no fresh authorisation arrived",
            status="abandoned",
        )

    def _abandoned(self, view: CaseView) -> Action:
        """Nobody was there. A silent retry against an absent human is worth almost nothing.

        The only lever is to get them back to the page, and then to present *while they are
        still on it* - which is why the charge is scheduled inside the presence window and
        on whichever rail this customer actually completes.
        """
        if self._came_back(view) and view.charge_attempts < view.charge_budget:
            return self._charge(
                view,
                "customer is back after outreach; presenting inside the window where they "
                "are actually looking at their phone",
                at=max(view.now, view.engaged_at or view.now),
                rail=self._friendliest_rail(view),
            )
        if (
            contact := self._contact(
                view,
                "resume_payment",
                "the payment was never declined - it was abandoned, so there is nothing to "
                "retry against until the customer is back",
            )
        ) is not None:
            return contact
        return Action(
            "close",
            view.now,
            "outreach exhausted and the customer never returned to complete the payment",
            status="abandoned",
        )

    # -- wait for the world to change --------------------------------------

    def _insufficient_funds(self, view: CaseView) -> Action:
        """The account was empty. Retrying now is guaranteed to fail and costs a fee to learn.

        This is the case the whole taxonomy exists for. A naive scheduler retries in thirty
        minutes, three times, and buys three declines. The only lever that exists is *when*,
        and for a salaried customer the answer is a date we already know.
        """
        if view.charge_attempts >= view.charge_budget:
            return self._out_of_charges(view, "balance retries exhausted")

        due = self._funds_available_at(view)
        if due is None or due >= view.horizon:
            # Payday falls outside the window in which we are willing to chase this at all.
            # One nudge is worth more than a retry that we already know will bounce: the
            # customer can pay from another account, and a retry cannot.
            if (
                contact := self._contact(
                    view,
                    "balance_nudge",
                    "the account was short and the next predictable credit falls outside the "
                    "recovery horizon, so a retry would be spent on a balance we know is not "
                    "there",
                )
            ) is not None:
                return contact
            return Action(
                "close",
                view.now,
                "insufficient balance with no credit expected inside the horizon",
                status="abandoned",
            )

        return self._charge(
            view,
            f"account was short; presenting after the customer's expected credit on "
            f"{due:%Y-%m-%d}",
            at=due,
        )

    def _funds_available_at(self, view: CaseView) -> datetime | None:
        """When money is next expected in the account.

        For a salaried customer this is a date the business already has, because it is the
        day their debits have historically cleared. For everyone else it is a guess, and is
        spent like a guess - one attempt on a fixed wait rather than a schedule.
        """
        base = max(view.now, view.case.opened_at)
        extra = FUNDS_RETRY_BACKOFF * view.charge_attempts

        if view.salary_day is None:
            return base + UNSCHEDULED_FUNDS_WAIT + extra

        due = _next_month_day(base, view.salary_day)
        due = datetime.combine(due.date(), FUNDS_RETRY_HOUR)
        if due <= base:
            due = datetime.combine(
                _next_month_day(base + timedelta(days=1), view.salary_day).date(),
                FUNDS_RETRY_HOUR,
            )
        return due + extra

    def _limit(self, view: CaseView) -> Action:
        """A cap was hit. Caps roll over; present once the next banking day is open."""
        if view.charge_attempts >= view.charge_budget:
            return self._out_of_charges(view, "limit retries exhausted")
        nxt = datetime.combine(
            (view.now + timedelta(days=1 + view.charge_attempts)).date(), LIMIT_RETRY_HOUR
        )
        return self._charge(
            view, "a per-transaction or daily cap was hit; presenting after it rolls over", at=nxt
        )

    # -- infrastructure ----------------------------------------------------

    def _routing(self, view: CaseView) -> Action:
        """Our route broke, not the bank. A different PSP works right now.

        Falls back to the technical backoff once every route has been tried, because at
        that point the evidence no longer supports "it is only our side".
        """
        untried = [p for p in self.known_psps if p not in view.psps_tried]
        if not untried:
            return self._technical(view)
        if view.charge_attempts >= view.charge_budget:
            return self._out_of_charges(view, "routing retries exhausted")
        psp = untried[0]
        return self._charge(
            view,
            f"the failure came from our own gateway rather than the bank; re-routing to "
            f"{psp}, which has not been tried on this payment",
            at=view.now + REROUTE_DELAY,
            psp=psp,
        )

    def _technical(self, view: CaseView) -> Action:
        """Plumbing. Wait for it to come back, on a backoff measured in minutes and hours."""
        if view.charge_attempts >= min(view.charge_budget, len(TECH_BACKOFF)):
            return self._out_of_charges(
                view, "technical retries exhausted without the issuer recovering"
            )
        delay = TECH_BACKOFF[view.charge_attempts]
        return self._charge(
            view,
            f"transient failure at the issuer or switch; retry {view.charge_attempts + 1} "
            f"after {_human(delay)}",
            at=view.now + delay,
        )

    # -- action builders ---------------------------------------------------

    def _out_of_charges(self, view: CaseView, why: str) -> Action:
        """The charge budget is spent. Ask, rather than close.

        This exists because of what the sensitivity run showed about stored mandates. The
        rail halts a mandate after some number of consecutive failed presentations that we
        never get to see, so the budget on a recurring rail is deliberately short of any
        plausible value of it. Short budget, though, is only half a policy: it stops the
        harm without pursuing the money.

        Outreach is the other half, and the asymmetry is the whole argument for it. A
        message cannot halt a mandate. A presentation can. So once presenting is off the
        table, asking the customer to pay - from another account, on another rail, however
        they like - is strictly the better remaining lever, and it is bounded by the same
        contact caps as every other message this policy sends.
        """
        if (
            contact := self._contact(
                view,
                "pay_manually",
                f"{why}; further presentations against a stored authorisation risk halting "
                f"the mandate and forfeiting future months, so the remaining ask is made of "
                f"the customer rather than of the rail"
                if view.recurring
                else f"{why}; asking the customer directly is the only lever left",
            )
        ) is not None:
            return contact
        return Action("close", view.now, why, status="abandoned")

    def _charge(
        self,
        view: CaseView,
        reason: str,
        at: datetime,
        psp: str | None = None,
        rail: Rail | None = None,
    ) -> Action:
        at = max(at, view.now)
        if at >= view.horizon:
            return Action(
                "close",
                view.now,
                f"the next sensible presentation falls after the {CASE_HORIZON.days}-day "
                f"horizon; stopping rather than chasing it past the point it is worth",
                status="abandoned",
            )
        if view.charge_attempts >= view.charge_budget:
            return self._out_of_charges(
                view,
                f"charge budget of {view.charge_budget} exhausted"
                + (" - tighter on a stored mandate" if view.recurring else ""),
            )
        return Action(
            "reroute" if psp else "retry",
            at,
            reason,
            psp=psp or view.case.psp,
            rail=rail or view.case.rail,
        )

    def _contact(self, view: CaseView, template: str, why: str) -> Action | None:
        """Schedule outreach, or return None if no permitted slot exists inside the horizon.

        Every bound is proved here, before the message exists - the per-case cap, the
        customer's rolling frequency cap, the minimum gap, and quiet hours. `core.guards`
        re-derives all of them from the ledger afterwards from an independent
        implementation, so a bug in this method cannot vouch for itself.
        """
        if not view.channels:
            return None
        if view.contacts_sent >= MAX_CONTACTS_PER_CASE:
            return None

        earliest = view.now
        if view.contacts_sent and view.engaged_at is None:
            # A message already went out and nothing came of it. Give the customer the
            # response window before deciding they are not going to act.
            earliest = max(earliest, view.now + CONTACT_RESPONSE_WAIT)

        at = self._first_permitted_slot(view, earliest)
        if at is None or at >= view.horizon:
            return None

        channel = self._channel(view)
        incentive = self._use_incentive(view)
        return Action(
            "contact",
            at,
            why,
            channel=channel,
            template=template,
            with_incentive=incentive,
        )

    def _first_permitted_slot(self, view: CaseView, earliest: datetime) -> datetime | None:
        """The first time outreach is allowed, or None if there is none before the horizon.

        Walks forward rather than giving up, because the bounds defer messages, they do not
        cancel them: a nudge that would land at 03:00 is held until 09:00, not dropped.
        """
        at = next_contact_window(earliest)
        for _ in range(32):  # bounded; each step advances by at least the minimum gap
            if at >= view.horizon:
                return None
            blocked_until = self._blocked_until(view, at)
            if blocked_until is None:
                return at
            at = next_contact_window(max(blocked_until, at + timedelta(minutes=1)))
        return None

    def _blocked_until(self, view: CaseView, at: datetime) -> datetime | None:
        """None if `at` is permitted; otherwise the earliest time worth reconsidering.

        The frequency cap is checked by *inserting* the proposed time into this customer's
        contact history and sliding a window over the result - the same computation
        `core.guards` performs after the run. Counting only the messages that precede `at`
        would be wrong here: cases are worked in value order rather than clock order, so a
        message already recorded can sit later on the simulated clock than the one being
        proposed, and a backward-looking count would not see it.
        """
        if not within_contact_window(at):
            return next_contact_window(at)

        times = sorted(view.customer_contact_times + (at,))
        gaps = [t for t in view.customer_contact_times if abs(t - at) < MIN_CONTACT_GAP]
        if gaps:
            return max(gaps) + MIN_CONTACT_GAP

        window = timedelta(days=CONTACT_WINDOW_DAYS)
        left = 0
        for right, t in enumerate(times):
            while t - times[left] > window:
                left += 1
            if right - left + 1 > MAX_CONTACTS_PER_WINDOW:
                # Nothing is permitted until the oldest message in the offending window
                # ages out of it.
                return times[left] + window + timedelta(minutes=1)
        return None

    def _channel(self, view: CaseView) -> str:
        for candidate in CHANNEL_PREFERENCE:
            if candidate in view.channels:
                return candidate
        return view.channels[0]

    def _use_incentive(self, view: CaseView) -> bool:
        """Attach an incentive to the last ask, and only where it pays for itself.

        Not to the first: an incentive offered before a plain reminder has been tried is
        money spent on customers who would have paid anyway, and it teaches the ones who
        would not that failing a payment is how you get a discount.
        """
        return (
            view.contacts_sent == MAX_CONTACTS_PER_CASE - 1
            and view.case.amount_paise >= INCENTIVE_MIN_AMOUNT_PAISE
        )

    # -- observations ------------------------------------------------------

    def _came_back(self, view: CaseView) -> bool:
        """True if outreach visibly worked and the customer is probably still there."""
        until = view.present_until
        return until is not None and view.now <= until

    def _friendliest_rail(self, view: CaseView) -> Rail:
        """The rail this customer is most likely to actually complete.

        A customer who abandons an OTP screen every time is not telling us they will not
        pay; they are telling us about the OTP screen. Offering the rail they complete is a
        lever that costs nothing and is invisible to an arm that only knows how to retry.

        Only offered where there is a human to hand the choice to. A stored-mandate debit
        has nobody at the other end, and switching its rail mid-recovery would present
        against an authorisation that was never granted for it.
        """
        rail = view.case.rail
        if rail not in HUMAN_PRESENT_RAILS or view.preferred_rail is None:
            return rail
        if view.preferred_rail not in HUMAN_PRESENT_RAILS:
            return rail
        return view.preferred_rail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_month_day(after: datetime, day: int) -> datetime:
    """The next occurrence of `day` of the month at or after `after`.

    Clamped to 28 so that a customer paid on the 31st does not silently lose February.
    """
    day = max(1, min(day, 28))
    year, month = after.year, after.month
    if after.day > day:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return datetime(year, month, day)


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"

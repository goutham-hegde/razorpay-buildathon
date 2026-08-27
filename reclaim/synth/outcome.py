"""The sealed world: ground truth about whether a given recovery action works.

SEALED. Nothing under `reclaim.core` may import this module, and a test enforces that.
This file decides outcomes; `reclaim.core` decides actions. If the policy could read these
parameters the evaluation would be circular - the agent would be rediscovering constants we
wrote down ourselves and the reported recovery figures would mean nothing.

Written before any policy code, and frozen thereafter.

CALIBRATION
-----------
The numbers in `Calibration` are order-of-magnitude anchors, not measurements. Public
reference points used to set them:

  * NPCI publishes bank-wise UPI technical decline rates monthly; remitter-side technical
    declines sit well under a percent for healthy banks and spike hard during incidents,
    which is why outages here are modelled as bounded windows rather than a flat rate.
  * Card payments carrying an OTP/3DS step complete materially less often than UPI intent,
    the gap being drop-off at the authentication screen rather than issuer refusal.
  * Recurring mandate debits (SIP, subscription, EMI) fail on first presentation at a rate
    driven mostly by balance rather than by plumbing, and a meaningful share of those clear
    on a later presentation once money has arrived.

Because these are anchors rather than measurements, no conclusion in the report is allowed
to depend on their exact values. `eval.sensitivity` re-runs the entire comparison with every
constant jittered, and the README reports whether the ranking of arms survives. That is the
honest answer to "you made these numbers up": yes, and here is the range over which the
finding holds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from reclaim.domain import (
    HUMAN_PRESENT_RAILS,
    RECURRING_RAILS,
    Rail,
    RootCause,
)
from reclaim.synth.personas import Persona, profile


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Calibration:
    """Every tunable number in the world, in one place so it can be perturbed wholesale."""

    # --- issuer / routing outages -----------------------------------------
    #: Success probability when retrying into an ongoing outage.
    p_retry_during_outage: float = 0.10
    #: Success probability once the outage has cleared.
    p_retry_after_outage: float = 0.88
    #: Success when re-routing a PSP failure through a healthy PSP.
    p_reroute_healthy_psp: float = 0.85
    #: Success when re-routing a PSP failure through the same broken PSP.
    p_reroute_same_psp: float = 0.15

    # --- balance ----------------------------------------------------------
    #: Success when debiting an empty account before money has arrived.
    p_nsf_before_funds: float = 0.04
    #: Success once money has landed.
    p_nsf_after_funds: float = 0.80
    #: Extra penalty applied per multiple of the customer's typical ticket size, so
    #: large debits clear less reliably than small ones on a thin balance.
    nsf_amount_sensitivity: float = 0.12

    # --- limits -----------------------------------------------------------
    p_limit_same_day: float = 0.08
    p_limit_next_day: float = 0.70

    # --- authorisation drop-off -------------------------------------------
    #: Silent retry against a rail that needs a live human. Near-worthless by design.
    p_silent_retry_human_rail: float = 0.05
    #: Completion once outreach has actually brought the customer back, on a low-friction
    #: rail (UPI deep link).
    p_complete_when_present_low_friction: float = 0.86
    #: Same, but on a rail that still makes them pass an OTP.
    p_complete_when_present_otp: float = 0.61

    # --- risk -------------------------------------------------------------
    p_risk_retry: float = 0.02
    #: Retries against a risk decline beyond this count hard-block the customer for good.
    risk_hard_block_after: int = 2

    # --- mandates ---------------------------------------------------------
    #: Consecutive failed presentations before the mandate is halted by the rail.
    mandate_halt_after: int = 4
    #: Months of future revenue lost when a mandate is halted.
    mandate_residual_months: int = 9

    # --- card network retry budget ----------------------------------------
    #: Charge attempts against a declined card beyond this count in the window attract a
    #: scheme penalty. Exact scheme rules vary and change; this is a stand-in for the fact
    #: that a budget exists at all.
    card_retry_budget: int = 8
    card_retry_window_days: int = 30
    card_retry_penalty_paise: int = 3500

    # --- outreach ---------------------------------------------------------
    #: Multiplier on engagement when an incentive is attached.
    incentive_engagement_lift: float = 1.35
    #: How long the customer stays "present" after engaging with a message.
    presence_window_hours: int = 6
    #: Engagement multiplier per channel.
    channel_quality: dict[str, float] = field(
        default_factory=lambda: {"whatsapp": 1.0, "sms": 0.72, "email": 0.48}
    )

    # --- costs ------------------------------------------------------------
    cost_per_charge_attempt_paise: int = 250
    cost_per_contact_paise: dict[str, int] = field(
        default_factory=lambda: {"whatsapp": 85, "sms": 22, "email": 3}
    )
    #: Cost of unwinding a double charge: refund fee plus the goodwill it burns.
    double_charge_cost_paise: int = 12000
    cost_per_human_escalation_paise: int = 9000


DEFAULT_CALIBRATION = Calibration()


def jitter(cal: Calibration, rng: random.Random, pct: float) -> Calibration:
    """Return a copy of `cal` with every scalar probability/cost moved by up to +/- pct.

    Used by the sensitivity analysis. Probabilities are clamped to [0, 1]; integer
    thresholds are perturbed by at least one whole unit so that they actually move.
    """

    def move_float(v: float, is_prob: bool) -> float:
        out = v * (1.0 + rng.uniform(-pct, pct))
        return min(1.0, max(0.0, out)) if is_prob else max(0.0, out)

    def move_int(v: int) -> int:
        delta = max(1, round(abs(v) * pct))
        return max(1, v + rng.randint(-delta, delta))

    probs = {
        f: move_float(getattr(cal, f), True)
        for f in (
            "p_retry_during_outage",
            "p_retry_after_outage",
            "p_reroute_healthy_psp",
            "p_reroute_same_psp",
            "p_nsf_before_funds",
            "p_nsf_after_funds",
            "p_limit_same_day",
            "p_limit_next_day",
            "p_silent_retry_human_rail",
            "p_complete_when_present_low_friction",
            "p_complete_when_present_otp",
            "p_risk_retry",
        )
    }
    scalars = {
        f: move_float(getattr(cal, f), False)
        for f in ("nsf_amount_sensitivity", "incentive_engagement_lift")
    }
    ints = {
        f: move_int(getattr(cal, f))
        for f in (
            "risk_hard_block_after",
            "mandate_halt_after",
            "mandate_residual_months",
            "card_retry_budget",
            "presence_window_hours",
            "cost_per_charge_attempt_paise",
            "double_charge_cost_paise",
            "cost_per_human_escalation_paise",
        )
    }
    return replace(cal, **probs, **scalars, **ints)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GroundTruth:
    """Everything true about a case that the agent is not allowed to see."""

    case_id: str
    root_cause: RootCause
    persona: Persona

    #: When money lands, for balance failures.
    funds_return_at: datetime | None = None
    #: When the issuer or route recovers, for infrastructure failures.
    outage_ends_at: datetime | None = None
    #: A PSP that is currently healthy, for routing failures.
    healthy_psp: str | None = None

    instrument_alive: bool = True
    mandate_alive: bool = True

    #: When (if ever) this case would have recovered with no intervention at all. The
    #: control arm exists to measure exactly this, and it is why gross recovery lies.
    organic_recovery_at: datetime | None = None

    #: Monthly revenue that stops if the mandate is halted.
    monthly_value_paise: int = 0
    #: Typical ticket size for this customer, used for the balance-sensitivity term.
    typical_ticket_paise: int = 50000


@dataclass(slots=True)
class CaseState:
    """Mutable world state for one case during a run."""

    charge_attempts: int = 0
    card_attempts_in_window: int = 0
    consecutive_mandate_failures: int = 0
    contacts_sent: int = 0
    risk_retries: int = 0

    opted_out: bool = False
    mandate_halted: bool = False
    risk_hard_blocked: bool = False
    double_charged: bool = False

    #: Set when outreach lands; a human-present rail can only succeed before this.
    present_until: datetime | None = None
    #: True once the customer has supplied a working instrument or fresh mandate.
    reauthorised: bool = False

    recovered_at: datetime | None = None
    costs_paise: int = 0


@dataclass(frozen=True, slots=True)
class ChargeResult:
    succeeded: bool
    #: True when the charge went through on a case that was already debited upstream.
    #: The money is not revenue; it is a liability plus a very unhappy customer.
    double_charge: bool
    mandate_halted_now: bool
    penalty_paise: int
    note: str


@dataclass(frozen=True, slots=True)
class ContactResult:
    delivered: bool
    engaged: bool
    opted_out_now: bool
    note: str


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class World:
    """Adjudicates recovery actions. The only source of truth about what worked.

    The eval harness owns a `World`; the agent never touches one. Every arm - control,
    naive, rules-only, agent - is scored against an identically seeded instance, so the
    arms differ only in the actions they choose.
    """

    def __init__(
        self,
        truths: dict[str, GroundTruth],
        seed: int,
        calibration: Calibration | None = None,
    ) -> None:
        self.truths = truths
        self.cal = calibration or DEFAULT_CALIBRATION
        # A recurring case exists *because* a presentation already failed, and the rail
        # counted that failure. Starting the counter at zero silently gave every arm one
        # free failure more than reality does, and with `mandate_halt_after` at 4 it meant
        # no arm retrying three times could ever halt a mandate: the downside metric read
        # 0.0% for every arm and measured nothing. `monthly_value_paise > 0` is exactly the
        # recurring set - the generator gives a monthly value to mandate-backed cases only.
        self.state: dict[str, CaseState] = {
            cid: CaseState(consecutive_mandate_failures=1 if t.monthly_value_paise > 0 else 0)
            for cid, t in truths.items()
        }
        self._seed = seed
        self._streams: dict[str, random.Random] = {}

    def _stream(self, case_id: str) -> random.Random:
        """The draw sequence for one case. One per case, not one per world.

        This is common random numbers, and it is what makes the arms *paired* rather than
        merely identically parameterised. With a single shared generator, the moment one
        arm takes a different number of actions than another its draw sequence shifts, and
        every subsequent case in that arm sees different randomness than the same case in
        another arm. The arms then differ by their decisions *and* by an accident of
        ordering, and the difference lands in the lift estimate as noise.

        Per-case streams remove that. Case 400 gets the same sequence in every arm no
        matter what the arms did to cases 1 through 399, so a difference between arms on
        that case is a difference in what they chose to do. It does not change what is
        being estimated - only how much noise the estimate carries.

        Seeded from the case id rather than a counter for the same reason: a counter would
        reintroduce order-dependence through the back door.
        """
        rng = self._streams.get(case_id)
        if rng is None:
            rng = random.Random(f"{self._seed}:{case_id}")
            self._streams[case_id] = rng
        return rng

    # -- charging ----------------------------------------------------------

    def attempt_charge(
        self,
        case_id: str,
        at: datetime,
        rail: Rail,
        psp: str,
        amount_paise: int,
    ) -> ChargeResult:
        """Present a charge and decide whether it clears."""
        truth = self.truths[case_id]
        st = self.state[case_id]

        st.charge_attempts += 1
        st.costs_paise += self.cal.cost_per_charge_attempt_paise
        penalty = 0

        if rail in (Rail.CARD_3DS, Rail.CARD_RECURRING):
            st.card_attempts_in_window += 1
            if st.card_attempts_in_window > self.cal.card_retry_budget:
                penalty += self.cal.card_retry_penalty_paise
                st.costs_paise += self.cal.card_retry_penalty_paise

        # A halted mandate or a hard risk block cannot be charged at all.
        if st.mandate_halted and rail in RECURRING_RAILS:
            return ChargeResult(False, False, False, penalty, "mandate halted")
        if st.risk_hard_blocked:
            return ChargeResult(False, False, False, penalty, "risk hard block")

        p = self._success_probability(truth, st, at, rail, psp, amount_paise)
        ok = self._stream(case_id).random() < p

        # The trap. The upstream debit already happened; a "successful" charge here is a
        # duplicate, and invariant R1 exists to make sure the agent never gets here.
        if truth.root_cause is RootCause.AMBIGUOUS_DEBITED and ok:
            st.double_charged = True
            st.costs_paise += self.cal.double_charge_cost_paise
            return ChargeResult(
                True, True, False, penalty, "DOUBLE CHARGE - upstream debit already stood"
            )

        if truth.root_cause is RootCause.RISK_DECLINED and not ok:
            st.risk_retries += 1
            if st.risk_retries > self.cal.risk_hard_block_after:
                st.risk_hard_blocked = True

        halted_now = False
        if rail in RECURRING_RAILS:
            if ok:
                st.consecutive_mandate_failures = 0
            else:
                st.consecutive_mandate_failures += 1
                if (
                    st.consecutive_mandate_failures >= self.cal.mandate_halt_after
                    and not st.mandate_halted
                ):
                    st.mandate_halted = True
                    halted_now = True

        if ok:
            st.recovered_at = at

        return ChargeResult(ok, False, halted_now, penalty, "" if ok else "declined")

    def _success_probability(
        self,
        truth: GroundTruth,
        st: CaseState,
        at: datetime,
        rail: Rail,
        psp: str,
        amount_paise: int,
    ) -> float:
        cal = self.cal
        cause = truth.root_cause

        # Causes that need fresh authorisation are dead until the customer supplies it.
        if cause in (RootCause.INSTRUMENT_INVALID, RootCause.MANDATE_REVOKED):
            if not st.reauthorised:
                return 0.0
            return self._presence_probability(truth, st, at, rail)

        if cause is RootCause.AUTH_ABANDONED:
            # Nobody is there. Only outreach that actually landed makes this recoverable.
            if rail in HUMAN_PRESENT_RAILS:
                return self._presence_probability(truth, st, at, rail)
            return cal.p_silent_retry_human_rail

        if cause is RootCause.ISSUER_TECHNICAL_DECLINE:
            if truth.outage_ends_at and at < truth.outage_ends_at:
                return cal.p_retry_during_outage
            return cal.p_retry_after_outage

        if cause is RootCause.PSP_ROUTING_FAILURE:
            if truth.healthy_psp and psp == truth.healthy_psp:
                return cal.p_reroute_healthy_psp
            if truth.outage_ends_at and at >= truth.outage_ends_at:
                return cal.p_retry_after_outage
            return cal.p_reroute_same_psp

        if cause is RootCause.INSUFFICIENT_FUNDS:
            if truth.funds_return_at and at >= truth.funds_return_at:
                base = cal.p_nsf_after_funds
            else:
                base = cal.p_nsf_before_funds
            # A debit far above the customer's usual ticket clears less often.
            ratio = amount_paise / max(1, truth.typical_ticket_paise)
            return max(0.0, base - cal.nsf_amount_sensitivity * max(0.0, ratio - 1.0))

        if cause is RootCause.LIMIT_EXCEEDED:
            opened = truth.funds_return_at or at
            same_day = at.date() == opened.date()
            return cal.p_limit_same_day if same_day else cal.p_limit_next_day

        if cause is RootCause.RISK_DECLINED:
            return cal.p_risk_retry

        if cause is RootCause.AMBIGUOUS_DEBITED:
            # It "works", which is exactly the problem.
            return 0.92

        return 0.0

    def _presence_probability(
        self, truth: GroundTruth, st: CaseState, at: datetime, rail: Rail
    ) -> float:
        """Probability a charge completes given whether the customer is actually here."""
        if st.present_until is None or at > st.present_until:
            return self.cal.p_silent_retry_human_rail
        low_friction = rail in (Rail.UPI_INTENT, Rail.UPI_COLLECT)
        return (
            self.cal.p_complete_when_present_low_friction
            if low_friction
            else self.cal.p_complete_when_present_otp
        )

    # -- outreach ----------------------------------------------------------

    def send_contact(
        self,
        case_id: str,
        at: datetime,
        channel: str,
        with_incentive: bool = False,
    ) -> ContactResult:
        """Send a message and decide whether the customer actually acts on it."""
        truth = self.truths[case_id]
        st = self.state[case_id]
        prof = profile(truth.persona)
        cal = self.cal

        if st.opted_out:
            return ContactResult(False, False, False, "customer has opted out")

        st.contacts_sent += 1
        st.costs_paise += cal.cost_per_contact_paise.get(channel, 25)

        engagement = prof.base_engagement
        engagement *= (1.0 - prof.fatigue_decay) ** (st.contacts_sent - 1)
        engagement *= cal.channel_quality.get(channel, 0.5)
        if with_incentive:
            engagement *= cal.incentive_engagement_lift
        engagement = min(1.0, engagement)

        engaged = self._stream(case_id).random() < engagement
        if engaged:
            st.present_until = at + timedelta(hours=cal.presence_window_hours)
            # Coming back is also when a dead instrument or mandate gets replaced.
            if truth.root_cause in (
                RootCause.INSTRUMENT_INVALID,
                RootCause.MANDATE_REVOKED,
            ):
                st.reauthorised = True

        opted_out_now = False
        if not engaged and st.contacts_sent > 1:
            if self._stream(case_id).random() < prof.opt_out_rate * st.contacts_sent:
                st.opted_out = True
                opted_out_now = True

        return ContactResult(
            delivered=True,
            engaged=engaged,
            opted_out_now=opted_out_now,
            note="engaged" if engaged else "ignored",
        )

    # -- passive outcomes --------------------------------------------------

    def settle_organic(self, case_id: str, horizon_end: datetime) -> bool:
        """Resolve whether a case recovers on its own with no help from us.

        This runs for every arm, including the control, and it is the single most
        important line in the harness: without it we would count money that was always
        coming back as money the agent won.
        """
        truth = self.truths[case_id]
        st = self.state[case_id]
        if st.recovered_at is not None:
            return False
        if truth.organic_recovery_at and truth.organic_recovery_at <= horizon_end:
            st.recovered_at = truth.organic_recovery_at
            return True
        return False

    def escalate_to_human(self, case_id: str) -> None:
        self.state[case_id].costs_paise += self.cal.cost_per_human_escalation_paise

    # -- reporting ---------------------------------------------------------

    def residual_loss_paise(self, case_id: str) -> int:
        """Future revenue destroyed by halting this customer's mandate."""
        truth = self.truths[case_id]
        st = self.state[case_id]
        if not st.mandate_halted:
            return 0
        return truth.monthly_value_paise * self.cal.mandate_residual_months

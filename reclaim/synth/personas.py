"""Customer archetypes for the simulated world.

SEALED. Nothing under `reclaim.core` may import this module. The recovery policy is not
allowed to know that personas exist, let alone what their parameters are - if it did, the
whole evaluation would be the policy rediscovering numbers we wrote down ourselves.

Personas exist so that failure is *correlated with the customer*, not sprinkled uniformly.
That correlation is what makes the recovery problem non-trivial: a chronically empty
account behaves nothing like a one-off issuer timeout, and an agent that cannot tell them
apart will waste its retry budget on the former.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from reclaim.domain import Rail, RootCause


class Persona(StrEnum):
    #: Salaried, lives close to the line. Debits before payday bounce; after payday clear.
    SALARIED_TIGHT = "salaried_tight"

    #: Salaried with headroom. Failures are almost always technical, not financial.
    SALARIED_COMFORTABLE = "salaried_comfortable"

    #: Freelance or business income. Money arrives in lumps on no fixed calendar.
    SELF_EMPLOYED_LUMPY = "self_employed_lumpy"

    #: Already halfway out the door. Revokes mandates, ignores outreach.
    CHURN_INTENT = "churn_intent"

    #: Pays instantly when reminded. Prefers a UPI deep-link over anything with an OTP.
    DIGITAL_NATIVE = "digital_native"

    #: Reachable in principle, never actually reads anything.
    LOW_ENGAGEMENT = "low_engagement"


@dataclass(frozen=True, slots=True)
class PersonaProfile:
    """Behavioural parameters for one archetype.

    `cause_weights` are relative, not probabilities - the generator normalises them and
    then blends with the rail-level prior, because some causes are impossible on some
    rails (a stored-card debit cannot be abandoned at an OTP screen that never appears).
    """

    persona: Persona
    cause_weights: dict[RootCause, float]

    #: Probability the customer acts on a single well-timed message, before fatigue.
    base_engagement: float

    #: How fast repeated contact degrades engagement. Applied per prior contact.
    fatigue_decay: float

    #: Probability of opting out entirely, per contact, once fatigued.
    opt_out_rate: float

    #: Chance the customer would have recovered on their own with no intervention at all.
    #: This is the organic-recovery rate the control arm measures, and it is the reason
    #: gross recovery figures are misleading.
    organic_recovery: float

    #: Rail the customer completes most reliably when handed a choice.
    preferred_rail: Rail

    #: Whether income arrives on a predictable day of the month.
    salaried: bool


_W = RootCause

PROFILES: dict[Persona, PersonaProfile] = {
    Persona.SALARIED_TIGHT: PersonaProfile(
        persona=Persona.SALARIED_TIGHT,
        cause_weights={
            _W.INSUFFICIENT_FUNDS: 5.0,
            _W.LIMIT_EXCEEDED: 1.0,
            _W.ISSUER_TECHNICAL_DECLINE: 1.5,
            _W.AUTH_ABANDONED: 1.0,
            _W.PSP_ROUTING_FAILURE: 0.6,
            _W.INSTRUMENT_INVALID: 0.3,
            _W.MANDATE_REVOKED: 0.3,
            _W.RISK_DECLINED: 0.2,
            _W.AMBIGUOUS_DEBITED: 0.4,
        },
        base_engagement=0.42,
        fatigue_decay=0.30,
        opt_out_rate=0.04,
        organic_recovery=0.24,
        preferred_rail=Rail.UPI_INTENT,
        salaried=True,
    ),
    Persona.SALARIED_COMFORTABLE: PersonaProfile(
        persona=Persona.SALARIED_COMFORTABLE,
        cause_weights={
            _W.ISSUER_TECHNICAL_DECLINE: 4.0,
            _W.PSP_ROUTING_FAILURE: 2.0,
            _W.AUTH_ABANDONED: 1.5,
            _W.INSUFFICIENT_FUNDS: 0.5,
            _W.LIMIT_EXCEEDED: 0.8,
            _W.INSTRUMENT_INVALID: 0.7,
            _W.MANDATE_REVOKED: 0.3,
            _W.RISK_DECLINED: 0.3,
            _W.AMBIGUOUS_DEBITED: 0.6,
        },
        base_engagement=0.55,
        fatigue_decay=0.25,
        opt_out_rate=0.03,
        organic_recovery=0.34,
        preferred_rail=Rail.CARD_3DS,
        salaried=True,
    ),
    Persona.SELF_EMPLOYED_LUMPY: PersonaProfile(
        persona=Persona.SELF_EMPLOYED_LUMPY,
        cause_weights={
            _W.INSUFFICIENT_FUNDS: 3.5,
            _W.LIMIT_EXCEEDED: 1.5,
            _W.ISSUER_TECHNICAL_DECLINE: 1.5,
            _W.AUTH_ABANDONED: 1.2,
            _W.PSP_ROUTING_FAILURE: 0.6,
            _W.INSTRUMENT_INVALID: 0.4,
            _W.MANDATE_REVOKED: 0.4,
            _W.RISK_DECLINED: 0.3,
            _W.AMBIGUOUS_DEBITED: 0.5,
        },
        base_engagement=0.48,
        fatigue_decay=0.28,
        opt_out_rate=0.05,
        organic_recovery=0.28,
        preferred_rail=Rail.UPI_INTENT,
        salaried=False,
    ),
    Persona.CHURN_INTENT: PersonaProfile(
        persona=Persona.CHURN_INTENT,
        cause_weights={
            _W.MANDATE_REVOKED: 5.0,
            _W.INSTRUMENT_INVALID: 2.0,
            _W.AUTH_ABANDONED: 2.0,
            _W.INSUFFICIENT_FUNDS: 1.0,
            _W.ISSUER_TECHNICAL_DECLINE: 0.8,
            _W.PSP_ROUTING_FAILURE: 0.4,
            _W.LIMIT_EXCEEDED: 0.4,
            _W.RISK_DECLINED: 0.3,
            _W.AMBIGUOUS_DEBITED: 0.3,
        },
        base_engagement=0.12,
        fatigue_decay=0.55,
        opt_out_rate=0.18,
        organic_recovery=0.06,
        preferred_rail=Rail.UPI_INTENT,
        salaried=True,
    ),
    Persona.DIGITAL_NATIVE: PersonaProfile(
        persona=Persona.DIGITAL_NATIVE,
        cause_weights={
            _W.AUTH_ABANDONED: 3.0,
            _W.ISSUER_TECHNICAL_DECLINE: 2.5,
            _W.PSP_ROUTING_FAILURE: 1.8,
            _W.INSUFFICIENT_FUNDS: 1.2,
            _W.LIMIT_EXCEEDED: 1.0,
            _W.INSTRUMENT_INVALID: 0.5,
            _W.MANDATE_REVOKED: 0.4,
            _W.RISK_DECLINED: 0.3,
            _W.AMBIGUOUS_DEBITED: 0.5,
        },
        base_engagement=0.72,
        fatigue_decay=0.22,
        opt_out_rate=0.03,
        organic_recovery=0.45,
        preferred_rail=Rail.UPI_INTENT,
        salaried=True,
    ),
    Persona.LOW_ENGAGEMENT: PersonaProfile(
        persona=Persona.LOW_ENGAGEMENT,
        cause_weights={
            _W.AUTH_ABANDONED: 2.5,
            _W.INSUFFICIENT_FUNDS: 2.0,
            _W.ISSUER_TECHNICAL_DECLINE: 1.5,
            _W.INSTRUMENT_INVALID: 1.2,
            _W.PSP_ROUTING_FAILURE: 0.7,
            _W.LIMIT_EXCEEDED: 0.7,
            _W.MANDATE_REVOKED: 0.8,
            _W.RISK_DECLINED: 0.3,
            _W.AMBIGUOUS_DEBITED: 0.5,
        },
        base_engagement=0.14,
        fatigue_decay=0.40,
        opt_out_rate=0.06,
        organic_recovery=0.11,
        preferred_rail=Rail.UPI_COLLECT,
        salaried=True,
    ),
}


#: Population mix. Roughly a consumer-subscription book: mostly fine, a tight tail, and a
#: small cohort already leaving.
POPULATION_MIX: dict[Persona, float] = {
    Persona.SALARIED_COMFORTABLE: 0.30,
    Persona.SALARIED_TIGHT: 0.24,
    Persona.DIGITAL_NATIVE: 0.18,
    Persona.SELF_EMPLOYED_LUMPY: 0.13,
    Persona.LOW_ENGAGEMENT: 0.10,
    Persona.CHURN_INTENT: 0.05,
}


def profile(persona: Persona) -> PersonaProfile:
    return PROFILES[persona]

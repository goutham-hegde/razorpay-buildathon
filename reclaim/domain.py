"""Shared vocabulary between the simulated world and the recovery agent.

This module holds *types only*. It contains no probabilities, no outcome logic and no
policy. Both `reclaim.synth` (the world) and `reclaim.core` (the agent) import from here,
and that is the only thing they are permitted to have in common - see `tests/test_seal.py`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# Rails and instruments
# ---------------------------------------------------------------------------


class Rail(StrEnum):
    """How the money was asked for.

    The distinction that matters for recovery is whether a human has to *do* something for
    the charge to complete. UPI_COLLECT and CARD_3DS both require live human action, so a
    silent retry against them is close to worthless. UPI_AUTOPAY and EMANDATE_NACH fire
    against a stored authorisation with nobody present.
    """

    UPI_INTENT = "upi_intent"          # deep-link into the PSP app, user approves there
    UPI_COLLECT = "upi_collect"        # request pushed to the user, expires unseen
    UPI_AUTOPAY = "upi_autopay"        # recurring, stored mandate, no human present
    CARD_3DS = "card_3ds"              # one-time card with an OTP step
    CARD_RECURRING = "card_recurring"  # stored card on file, no OTP
    NETBANKING = "netbanking"          # redirect to the bank's own site
    EMANDATE_NACH = "emandate_nach"    # bank-account debit against a registered mandate


#: Rails that fire against a stored authorisation, with no human in the loop.
RECURRING_RAILS = frozenset({Rail.UPI_AUTOPAY, Rail.CARD_RECURRING, Rail.EMANDATE_NACH})

#: Rails that cannot complete unless the customer is present and acting.
HUMAN_PRESENT_RAILS = frozenset(
    {Rail.UPI_INTENT, Rail.UPI_COLLECT, Rail.CARD_3DS, Rail.NETBANKING}
)


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"   # issuer approved, not yet captured
    CAPTURED = "captured"       # the money is ours
    FAILED = "failed"
    PENDING = "pending"         # UPI collect awaiting approval; NOT yet a failure


TERMINAL_SUCCESS = frozenset({PaymentStatus.CAPTURED})


# ---------------------------------------------------------------------------
# Root-cause taxonomy
# ---------------------------------------------------------------------------


class RootCause(StrEnum):
    """What actually went wrong.

    This is the label `core.diagnose` must predict from messy observable error text, and
    the label `synth` assigns as ground truth. Each one implies a materially different
    recovery action, which is the entire reason the taxonomy exists.
    """

    #: Plumbing broke - issuer or switch timed out. Retrying in minutes often works.
    ISSUER_TECHNICAL_DECLINE = "issuer_technical_decline"

    #: The bank said no because the account is empty. Retrying now is guaranteed to fail;
    #: the only useful lever is *when*.
    INSUFFICIENT_FUNDS = "insufficient_funds"

    #: Nothing was declined. The customer left before completing OTP or approving the
    #: collect request. There is nobody there to retry against.
    AUTH_ABANDONED = "auth_abandoned"

    #: Card expired, blocked or closed. No retry will ever succeed.
    INSTRUMENT_INVALID = "instrument_invalid"

    #: Per-transaction or daily cap hit. Retryable later, or at a lower amount.
    LIMIT_EXCEEDED = "limit_exceeded"

    #: The stored authorisation no longer exists - the user revoked it in their PSP app.
    #: Recoverable only by asking for a new mandate.
    MANDATE_REVOKED = "mandate_revoked"

    #: A fraud or risk rule fired. Retrying argues with the rule and can make it worse.
    RISK_DECLINED = "risk_declined"

    #: Our own route to the bank broke, not the bank itself. A different PSP may work now.
    PSP_ROUTING_FAILURE = "psp_routing_failure"

    #: The dangerous one. We never got a clean answer and the customer may already have
    #: been debited. Retrying this double-charges. Must be reconciled, never retried.
    AMBIGUOUS_DEBITED = "ambiguous_debited"


#: Causes for which no charge attempt can succeed without fresh authorisation.
NEEDS_NEW_AUTHORISATION = frozenset(
    {RootCause.INSTRUMENT_INVALID, RootCause.MANDATE_REVOKED}
)

#: Causes where a retry is actively harmful rather than merely useless.
NEVER_RETRY = frozenset({RootCause.AMBIGUOUS_DEBITED, RootCause.RISK_DECLINED})

#: Causes that need the customer to come back and do something themselves.
NEEDS_HUMAN_PRESENT = frozenset(
    {RootCause.AUTH_ABANDONED, RootCause.INSTRUMENT_INVALID, RootCause.MANDATE_REVOKED}
)


# ---------------------------------------------------------------------------
# Observable error shape (Razorpay-flavoured)
# ---------------------------------------------------------------------------


class ErrorSource(StrEnum):
    CUSTOMER = "customer"
    BUSINESS = "business"
    BANK = "bank"
    GATEWAY = "gateway"
    ISSUER = "issuer"
    NPCI = "npci"


class ErrorStep(StrEnum):
    PAYMENT_INITIATION = "payment_initiation"
    PAYMENT_AUTHENTICATION = "payment_authentication"
    PAYMENT_AUTHORIZATION = "payment_authorization"
    PAYMENT_RESPONSE = "payment_response"


@dataclass(frozen=True, slots=True)
class ObservedError:
    """What the agent actually gets to see when a payment fails.

    Deliberately lossy. `description` is free text written by whichever issuer handled the
    transaction, and issuers do not agree with each other about wording, casing, or even
    which field carries the real reason. Recovering `RootCause` from this is the job.
    """

    code: str
    source: ErrorSource
    step: ErrorStep
    reason: str
    description: str
    bank_reference: str | None = None


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Customer:
    id: str
    #: Day of month salary typically lands. None for non-salaried personas.
    salary_day: int | None
    preferred_rail: Rail
    contactable_channels: list[str] = field(default_factory=lambda: ["sms", "email"])
    opted_out: bool = False


@dataclass(slots=True)
class Mandate:
    """A stored authorisation to debit without the customer present."""

    id: str
    customer_id: str
    rail: Rail
    max_amount_paise: int
    status: str = "active"          # active | halted | revoked
    consecutive_failures: int = 0
    created_at: datetime | None = None


@dataclass(slots=True)
class PaymentAttempt:
    """One charge attempt. The unit of both loss and recovery."""

    id: str
    case_id: str
    customer_id: str
    amount_paise: int
    rail: Rail
    issuer: str
    psp: str
    created_at: datetime
    status: PaymentStatus
    error: ObservedError | None = None
    #: 0 for the original attempt; 1..n for recovery attempts.
    attempt_no: int = 0
    mandate_id: str | None = None


@dataclass(slots=True)
class Case:
    """A unit of revenue at risk: one original failure and everything done about it."""

    id: str
    customer_id: str
    amount_paise: int
    opened_at: datetime
    kind: str                        # one_time | recurring
    rail: Rail
    issuer: str
    psp: str
    mandate_id: str | None = None
    #: open | recovered | abandoned | escalated | reconcile_hold
    status: str = "open"
    attempts: list[PaymentAttempt] = field(default_factory=list)


#: A case in any of these states still owes the batch a decision. Invariant R6 asserts
#: that none remain once the batch has drained.
NON_TERMINAL_CASE_STATES = frozenset({"open"})


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _encode(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, StrEnum):
        return str(obj)
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


def to_json(obj: Any) -> str:
    """Serialise a dataclass to a single JSON line."""
    return json.dumps(asdict(obj), default=_encode, separators=(",", ":"))


def rupees(paise: int) -> str:
    """Format paise as rupees for logs and reports."""
    return f"Rs {paise / 100:,.2f}"

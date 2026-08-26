"""Generates a batch of failed payments, and the ground truth about why they failed.

SEALED. Nothing under `reclaim.core` may import this module.

Two files come out, and the split is deliberate rather than cosmetic:

    data/<batch>/cases.jsonl   what the agent is allowed to see
    data/<batch>/truth.jsonl   what really happened

`core/` reads the first. `eval/` reads both. Keeping ground truth in a physically separate
file makes leakage an obvious mistake rather than a subtle one.

Two batches are generated with different seeds and a shifted failure mix. Batch A is for
tuning the policy; batch B is reported and the policy must never be tuned against it.

On the error text
-----------------
`_ERROR_VARIANTS` is deliberately inconsistent, because real issuer responses are. Casing
varies, the useful signal sometimes sits in `description` while `reason` says nothing more
than `payment_failed`, and three pairs of causes are near-indistinguishable on purpose:

    AMBIGUOUS_DEBITED  vs  ISSUER_TECHNICAL_DECLINE   both read as a timeout
    MANDATE_REVOKED    vs  INSTRUMENT_INVALID         both read as "not valid"
    PSP_ROUTING_FAILURE vs ISSUER_TECHNICAL_DECLINE   both read as a gateway error

Those pairs are where diagnosis is actually hard, where the cost of being wrong is highest
(the first pair is the double-charge trap), and where the model has to earn its place. Do
not tidy them up to make the confusion matrix look better.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from reclaim.domain import (
    Case,
    Customer,
    ErrorSource,
    ErrorStep,
    Mandate,
    ObservedError,
    PaymentAttempt,
    PaymentStatus,
    Rail,
    RootCause,
    to_json,
)
from reclaim.synth.outcome import GroundTruth
from reclaim.synth.personas import POPULATION_MIX, Persona, profile

ISSUERS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PNB", "BOB", "YESB", "IDFC"]
PSPS = ["psp_alpha", "psp_beta", "psp_gamma"]

#: Consumer subscription price points, in paise.
TICKETS = [19900, 49900, 99900, 149900, 249900, 499900, 999900]

RC = RootCause
ES, EP = ErrorSource, ErrorStep


# ---------------------------------------------------------------------------
# Error catalogue
# ---------------------------------------------------------------------------

#: Rail families, so that an error string cannot land on a rail where it is nonsense - a
#: card payment must never come back with "UPI collect request expired".
ANY = frozenset({"upi", "card", "netbanking", "nach"})
UPI = frozenset({"upi"})
CARD = frozenset({"card"})
CARDLIKE = frozenset({"card", "netbanking"})
MANDATED = frozenset({"upi", "nach", "card"})

_RAIL_FAMILY: dict[Rail, str] = {
    Rail.UPI_INTENT: "upi",
    Rail.UPI_COLLECT: "upi",
    Rail.UPI_AUTOPAY: "upi",
    Rail.CARD_3DS: "card",
    Rail.CARD_RECURRING: "card",
    Rail.NETBANKING: "netbanking",
    Rail.EMANDATE_NACH: "nach",
}

#: (code, source, step, reason, description, rail families) - description may contain
#: {issuer}.
_ERROR_VARIANTS: dict[
    RootCause, list[tuple[str, ErrorSource, ErrorStep, str, str, frozenset[str]]]
] = {
    RC.ISSUER_TECHNICAL_DECLINE: [
        ("GATEWAY_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Payment processing failed because of an error at {issuer}", ANY),
        ("GATEWAY_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "{issuer} did not respond in time. Please retry.", ANY),
        ("BAD_REQUEST_ERROR", ES.NPCI, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "UPI TECHNICAL DECLINE (RC:U69) REMITTER UNAVAILABLE", UPI),
        ("GATEWAY_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "issuer down for maintenance, try after some time", ANY),
        ("SERVER_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Bank server is not responding", ANY),
    ],
    RC.AMBIGUOUS_DEBITED: [
        # Reads exactly like a technical decline. The tell is that a debit reference
        # came back at all, or that the text says the money left.
        ("GATEWAY_ERROR", ES.BANK, EP.PAYMENT_RESPONSE, "payment_failed",
         "No confirmation received from {issuer}. Debit may have been processed.", ANY),
        ("GATEWAY_ERROR", ES.NPCI, EP.PAYMENT_RESPONSE, "payment_failed",
         "TRANSACTION STATUS UNKNOWN - AWAITING RECONCILIATION", UPI),
        ("SERVER_ERROR", ES.BANK, EP.PAYMENT_RESPONSE, "payment_failed",
         "timeout after debit instruction sent to {issuer}", ANY),
        ("GATEWAY_ERROR", ES.ISSUER, EP.PAYMENT_RESPONSE, "payment_failed",
         "Response not received. Customer account may have been debited.", ANY),
        ("GATEWAY_ERROR", ES.NPCI, EP.PAYMENT_RESPONSE, "payment_failed",
         "deemed approved pending settlement confirmation", ANY),
    ],
    RC.INSUFFICIENT_FUNDS: [
        ("BAD_REQUEST_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Your payment could not be completed due to insufficient balance", ANY),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "INSUFFICIENT FUNDS", ANY),
        ("BAD_REQUEST_ERROR", ES.NPCI, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "RC:U31 - insufficient balance in remitter account", UPI),
        ("BAD_REQUEST_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Debit declined by {issuer} - available balance too low", ANY),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "acct bal low, mandate presentation returned unpaid", MANDATED),
    ],
    RC.AUTH_ABANDONED: [
        ("BAD_REQUEST_ERROR", ES.CUSTOMER, EP.PAYMENT_AUTHENTICATION, "payment_failed",
         "Payment was not completed by the customer", ANY),
        ("BAD_REQUEST_ERROR", ES.CUSTOMER, EP.PAYMENT_AUTHENTICATION,
         "payment_delayed_by_bank_or_customer",
         "OTP was not entered within the allowed time", CARDLIKE),
        ("BAD_REQUEST_ERROR", ES.CUSTOMER, EP.PAYMENT_AUTHENTICATION, "payment_failed",
         "UPI collect request expired - not approved by payer", UPI),
        ("BAD_REQUEST_ERROR", ES.CUSTOMER, EP.PAYMENT_AUTHENTICATION, "payment_failed",
         "customer cancelled at {issuer} authentication page", ANY),
        ("BAD_REQUEST_ERROR", ES.CUSTOMER, EP.PAYMENT_AUTHENTICATION, "payment_failed",
         "3DS session abandoned", CARD),
    ],
    RC.INSTRUMENT_INVALID: [
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "The card has expired", CARD),
        ("BAD_REQUEST_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Card is blocked. Contact {issuer}.", CARD),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_INITIATION, "payment_failed",
         "invalid card - do not honour, reissue required", CARD),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "stored credential no longer valid", CARD),
    ],
    RC.MANDATE_REVOKED: [
        # Overlaps hard with INSTRUMENT_INVALID: both say "not valid", "not found".
        ("BAD_REQUEST_ERROR", ES.NPCI, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "UMN not found or already revoked", UPI),
        ("BAD_REQUEST_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Mandate is no longer active at {issuer}", MANDATED),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_INITIATION, "payment_failed",
         "stored credential no longer valid - authorisation withdrawn by customer",
         MANDATED),
        ("BAD_REQUEST_ERROR", ES.NPCI, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "RC:UM3 MANDATE CANCELLED BY PAYER", UPI),
    ],
    RC.LIMIT_EXCEEDED: [
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Transaction amount exceeds the permitted limit", ANY),
        ("BAD_REQUEST_ERROR", ES.NPCI, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "RC:U67 - per transaction limit breached", UPI),
        ("BAD_REQUEST_ERROR", ES.BANK, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "daily debit cap reached for this account at {issuer}", ANY),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "mandate amount higher than registered maximum", MANDATED),
    ],
    RC.RISK_DECLINED: [
        ("BAD_REQUEST_ERROR", ES.GATEWAY, EP.PAYMENT_INITIATION, "payment_failed",
         "Payment blocked by risk rules", ANY),
        ("BAD_REQUEST_ERROR", ES.ISSUER, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "suspected fraud - do not honour", ANY),
        ("BAD_REQUEST_ERROR", ES.BUSINESS, EP.PAYMENT_INITIATION, "payment_failed",
         "velocity check failed for this customer", ANY),
    ],
    RC.PSP_ROUTING_FAILURE: [
        # Reads like an issuer problem, but the issuer is fine on another route.
        ("GATEWAY_ERROR", ES.GATEWAY, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "Payment processing failed because of an error at the gateway", ANY),
        ("GATEWAY_ERROR", ES.GATEWAY, EP.PAYMENT_INITIATION, "payment_failed",
         "upstream connector to {issuer} unavailable", ANY),
        ("SERVER_ERROR", ES.GATEWAY, EP.PAYMENT_AUTHORIZATION, "payment_failed",
         "route unhealthy, no acquirer available", ANY),
        ("GATEWAY_ERROR", ES.GATEWAY, EP.PAYMENT_RESPONSE, "payment_failed",
         "connector timeout", ANY),
    ],
}


def _make_error(
    rng: random.Random, cause: RootCause, issuer: str, rail: Rail
) -> ObservedError:
    family = _RAIL_FAMILY[rail]
    allowed = [v for v in _ERROR_VARIANTS[cause] if family in v[5]]
    if not allowed:  # never expected; fail loudly rather than emit nonsense
        raise ValueError(f"no {cause} error text valid on {rail}")
    code, source, step, reason, desc, _ = rng.choice(allowed)
    # A debit reference is present far more often when money actually moved. This is the
    # single honest signal separating AMBIGUOUS_DEBITED from a plain timeout - and it is
    # only a tendency, never a guarantee, which is what makes the call hard.
    if cause is RC.AMBIGUOUS_DEBITED:
        has_ref = rng.random() < 0.78
    elif cause is RC.ISSUER_TECHNICAL_DECLINE:
        has_ref = rng.random() < 0.16
    else:
        has_ref = rng.random() < 0.30
    ref = f"{issuer}{rng.randint(10**11, 10**12 - 1)}" if has_ref else None
    return ObservedError(
        code=code,
        source=source,
        step=step,
        reason=reason,
        description=desc.format(issuer=issuer),
        bank_reference=ref,
    )


# ---------------------------------------------------------------------------
# Rail priors
# ---------------------------------------------------------------------------

#: Causes that are structurally impossible on a given rail get zero weight. A stored-card
#: debit cannot be abandoned at an OTP screen that never appears.
_RAIL_PRIOR: dict[Rail, dict[RootCause, float]] = {
    Rail.UPI_INTENT: {
        RC.AUTH_ABANDONED: 2.2, RC.INSUFFICIENT_FUNDS: 1.6,
        RC.ISSUER_TECHNICAL_DECLINE: 1.4, RC.PSP_ROUTING_FAILURE: 0.9,
        RC.LIMIT_EXCEEDED: 0.8, RC.AMBIGUOUS_DEBITED: 0.9, RC.RISK_DECLINED: 0.4,
        RC.INSTRUMENT_INVALID: 0.0, RC.MANDATE_REVOKED: 0.0,
    },
    Rail.UPI_COLLECT: {
        RC.AUTH_ABANDONED: 3.4, RC.INSUFFICIENT_FUNDS: 1.2,
        RC.ISSUER_TECHNICAL_DECLINE: 1.0, RC.PSP_ROUTING_FAILURE: 0.7,
        RC.LIMIT_EXCEEDED: 0.6, RC.AMBIGUOUS_DEBITED: 0.7, RC.RISK_DECLINED: 0.3,
        RC.INSTRUMENT_INVALID: 0.0, RC.MANDATE_REVOKED: 0.0,
    },
    Rail.CARD_3DS: {
        RC.AUTH_ABANDONED: 2.6, RC.INSUFFICIENT_FUNDS: 1.2,
        RC.ISSUER_TECHNICAL_DECLINE: 1.3, RC.PSP_ROUTING_FAILURE: 1.0,
        RC.INSTRUMENT_INVALID: 1.4, RC.LIMIT_EXCEEDED: 0.9,
        RC.AMBIGUOUS_DEBITED: 0.8, RC.RISK_DECLINED: 0.7, RC.MANDATE_REVOKED: 0.0,
    },
    Rail.NETBANKING: {
        RC.AUTH_ABANDONED: 2.4, RC.ISSUER_TECHNICAL_DECLINE: 1.8,
        RC.INSUFFICIENT_FUNDS: 1.1, RC.PSP_ROUTING_FAILURE: 1.1,
        RC.AMBIGUOUS_DEBITED: 1.0, RC.LIMIT_EXCEEDED: 0.5, RC.RISK_DECLINED: 0.3,
        RC.INSTRUMENT_INVALID: 0.0, RC.MANDATE_REVOKED: 0.0,
    },
    Rail.UPI_AUTOPAY: {
        RC.INSUFFICIENT_FUNDS: 3.2, RC.MANDATE_REVOKED: 1.6,
        RC.ISSUER_TECHNICAL_DECLINE: 1.2, RC.LIMIT_EXCEEDED: 1.0,
        RC.PSP_ROUTING_FAILURE: 0.7, RC.AMBIGUOUS_DEBITED: 0.7,
        RC.RISK_DECLINED: 0.3, RC.AUTH_ABANDONED: 0.0, RC.INSTRUMENT_INVALID: 0.0,
    },
    Rail.CARD_RECURRING: {
        RC.INSUFFICIENT_FUNDS: 2.6, RC.INSTRUMENT_INVALID: 1.8,
        RC.ISSUER_TECHNICAL_DECLINE: 1.2, RC.LIMIT_EXCEEDED: 0.9,
        RC.PSP_ROUTING_FAILURE: 0.8, RC.AMBIGUOUS_DEBITED: 0.7,
        RC.RISK_DECLINED: 0.5, RC.MANDATE_REVOKED: 0.9, RC.AUTH_ABANDONED: 0.0,
    },
    Rail.EMANDATE_NACH: {
        RC.INSUFFICIENT_FUNDS: 3.6, RC.MANDATE_REVOKED: 1.4,
        RC.ISSUER_TECHNICAL_DECLINE: 1.0, RC.LIMIT_EXCEEDED: 0.8,
        RC.PSP_ROUTING_FAILURE: 0.5, RC.AMBIGUOUS_DEBITED: 0.6,
        RC.RISK_DECLINED: 0.2, RC.AUTH_ABANDONED: 0.0, RC.INSTRUMENT_INVALID: 0.0,
    },
}

ONE_TIME_RAILS = [Rail.UPI_INTENT, Rail.UPI_COLLECT, Rail.CARD_3DS, Rail.NETBANKING]
RECURRING_RAIL_CHOICES = [Rail.UPI_AUTOPAY, Rail.CARD_RECURRING, Rail.EMANDATE_NACH]


# ---------------------------------------------------------------------------
# Outage episodes
# ---------------------------------------------------------------------------


class Outage:
    """A bounded incident affecting one issuer or one PSP.

    Infrastructure failures arrive in clusters, not sprinkled uniformly - which is what
    makes them worth detecting and worth waiting out. Cases whose failure falls inside an
    episode inherit its end time as ground truth.
    """

    __slots__ = ("kind", "target", "start", "end")

    def __init__(self, kind: str, target: str, start: datetime, end: datetime) -> None:
        self.kind, self.target, self.start, self.end = kind, target, start, end

    def covers(self, at: datetime, issuer: str, psp: str) -> bool:
        if not (self.start <= at < self.end):
            return False
        return self.target == (issuer if self.kind == "issuer" else psp)


def _make_outages(rng: random.Random, window_start: datetime, days: int) -> list[Outage]:
    outages: list[Outage] = []
    for _ in range(rng.randint(5, 9)):
        kind = "issuer" if rng.random() < 0.65 else "psp"
        target = rng.choice(ISSUERS if kind == "issuer" else PSPS)
        start = window_start + timedelta(
            days=rng.uniform(0, days), hours=rng.uniform(0, 24)
        )
        minutes = rng.choice([25, 40, 55, 75, 110, 180, 260])
        outages.append(Outage(kind, target, start, start + timedelta(minutes=minutes)))
    return outages


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _pick(rng: random.Random, weights: dict) -> object:
    total = sum(weights.values())
    r = rng.random() * total
    upto = 0.0
    for key, w in weights.items():
        upto += w
        if r <= upto:
            return key
    return next(iter(weights))


def _next_salary(after: datetime, salary_day: int) -> datetime:
    y, m = after.year, after.month
    if after.day >= salary_day:
        m += 1
        if m > 12:
            m, y = 1, y + 1
    day = min(salary_day, 28)
    return datetime(y, m, day, 11, 0)


def generate(
    n: int,
    seed: int,
    batch: str,
    days: int = 30,
    window_start: datetime | None = None,
) -> tuple[list[Customer], list[Mandate], list[Case], dict[str, GroundTruth]]:
    rng = random.Random(seed)
    start = window_start or datetime(2026, 8, 1, 0, 0)
    outages = _make_outages(rng, start, days)

    # Batch B shifts the mix towards the expensive mistakes - more ambiguous debits, more
    # customers already on their way out. A policy tuned on A must still hold up here.
    mix = dict(POPULATION_MIX)
    ambiguity_lift = 1.0
    if batch.upper() == "B":
        mix[Persona.CHURN_INTENT] = mix[Persona.CHURN_INTENT] * 1.8
        mix[Persona.LOW_ENGAGEMENT] = mix[Persona.LOW_ENGAGEMENT] * 1.3
        ambiguity_lift = 1.6

    customers: list[Customer] = []
    mandates: list[Mandate] = []
    cases: list[Case] = []
    truths: dict[str, GroundTruth] = {}

    for i in range(n):
        persona: Persona = _pick(rng, mix)  # type: ignore[assignment]
        prof = profile(persona)

        cid = f"cus_{batch}{i:05d}"
        salary_day = rng.choice([1, 1, 2, 5, 7, 28]) if prof.salaried else None
        typical = rng.choice(TICKETS)

        recurring = rng.random() < 0.62
        rail = (
            rng.choice(RECURRING_RAIL_CHOICES) if recurring else rng.choice(ONE_TIME_RAILS)
        )
        issuer = rng.choice(ISSUERS)
        psp = rng.choice(PSPS)
        amount = rng.choice(TICKETS) if not recurring else typical
        failed_at = start + timedelta(days=rng.uniform(0, days), hours=rng.uniform(0, 24))

        # Blend persona propensity with what is possible on this rail.
        prior = _RAIL_PRIOR[rail]
        weights = {
            c: prof.cause_weights.get(c, 0.0) * prior.get(c, 0.0)
            for c in RootCause
            if prior.get(c, 0.0) > 0
        }
        for out in outages:
            if out.covers(failed_at, issuer, psp):
                key = (
                    RC.ISSUER_TECHNICAL_DECLINE
                    if out.kind == "issuer"
                    else RC.PSP_ROUTING_FAILURE
                )
                if weights.get(key, 0.0) > 0:
                    weights[key] *= 14.0
        if RC.AMBIGUOUS_DEBITED in weights:
            weights[RC.AMBIGUOUS_DEBITED] *= ambiguity_lift
        if not any(weights.values()):
            weights = {RC.ISSUER_TECHNICAL_DECLINE: 1.0}
        cause: RootCause = _pick(rng, weights)  # type: ignore[assignment]

        case_id = f"case_{batch}{i:05d}"
        mandate_id = None
        if recurring:
            mandate_id = f"mdt_{batch}{i:05d}"
            mandates.append(
                Mandate(
                    id=mandate_id,
                    customer_id=cid,
                    rail=rail,
                    max_amount_paise=max(amount, typical) * 2,
                    created_at=start - timedelta(days=rng.randint(30, 400)),
                )
            )

        customers.append(
            Customer(
                id=cid,
                salary_day=salary_day,
                preferred_rail=prof.preferred_rail,
                contactable_channels=["whatsapp", "sms", "email"]
                if rng.random() < 0.7
                else ["sms", "email"],
            )
        )

        attempt = PaymentAttempt(
            id=f"pay_{batch}{i:05d}_0",
            case_id=case_id,
            customer_id=cid,
            amount_paise=amount,
            rail=rail,
            issuer=issuer,
            psp=psp,
            created_at=failed_at,
            status=PaymentStatus.FAILED,
            error=_make_error(rng, cause, issuer, rail),
            attempt_no=0,
            mandate_id=mandate_id,
        )
        cases.append(
            Case(
                id=case_id,
                customer_id=cid,
                amount_paise=amount,
                opened_at=failed_at,
                kind="recurring" if recurring else "one_time",
                rail=rail,
                issuer=issuer,
                psp=psp,
                mandate_id=mandate_id,
                attempts=[attempt],
            )
        )

        # ---- ground truth -------------------------------------------------
        funds_return_at = None
        if cause is RC.INSUFFICIENT_FUNDS:
            if salary_day:
                funds_return_at = _next_salary(failed_at, salary_day)
            else:
                funds_return_at = failed_at + timedelta(days=rng.uniform(1, 14))
        elif cause is RC.LIMIT_EXCEEDED:
            funds_return_at = failed_at

        outage_ends_at = None
        healthy_psp = None
        if cause in (RC.ISSUER_TECHNICAL_DECLINE, RC.PSP_ROUTING_FAILURE):
            hit = next((o for o in outages if o.covers(failed_at, issuer, psp)), None)
            outage_ends_at = hit.end if hit else failed_at + timedelta(
                minutes=rng.choice([30, 45, 90, 150])
            )
            if cause is RC.PSP_ROUTING_FAILURE:
                healthy_psp = rng.choice([p for p in PSPS if p != psp])

        organic_at = None
        # A customer who deliberately revoked, or who was blocked for risk, is not coming
        # back on their own. Everyone else might.
        if cause not in (RC.MANDATE_REVOKED, RC.RISK_DECLINED, RC.AMBIGUOUS_DEBITED):
            if rng.random() < prof.organic_recovery:
                organic_at = failed_at + timedelta(hours=rng.uniform(2, 24 * 7))

        truths[case_id] = GroundTruth(
            case_id=case_id,
            root_cause=cause,
            persona=persona,
            funds_return_at=funds_return_at,
            outage_ends_at=outage_ends_at,
            healthy_psp=healthy_psp,
            instrument_alive=cause is not RC.INSTRUMENT_INVALID,
            mandate_alive=cause is not RC.MANDATE_REVOKED,
            organic_recovery_at=organic_at,
            monthly_value_paise=amount if recurring else 0,
            typical_ticket_paise=typical,
        )

    return customers, mandates, cases, truths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(to_json(row) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a batch of at-risk payment cases.")
    ap.add_argument("--batch", default="B", help="A (tuning) or B (held out, reported)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    batch = args.batch.upper()
    seed = args.seed if args.seed is not None else (11 if batch == "A" else 20260904)

    customers, mandates, cases, truths = generate(args.n, seed, batch)
    out = Path(args.out) / batch

    _write(out / "customers.jsonl", customers)
    _write(out / "mandates.jsonl", mandates)
    _write(out / "cases.jsonl", cases)
    with (out / "truth.jsonl").open("w", encoding="utf-8") as fh:
        for t in truths.values():
            fh.write(to_json(t) + "\n")
    (out / "meta.json").write_text(
        json.dumps({"batch": batch, "seed": seed, "n": args.n}, indent=2),
        encoding="utf-8",
    )

    at_risk = sum(c.amount_paise for c in cases)
    counts: dict[str, int] = {}
    for t in truths.values():
        counts[t.root_cause] = counts.get(t.root_cause, 0) + 1

    print(f"batch {batch}  seed {seed}  n={len(cases)}")
    print(f"at risk: Rs {at_risk / 100:,.0f}")
    print(f"written: {out}/  (cases.jsonl is what the agent sees; truth.jsonl is not)")
    for cause, k in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:5d}  {cause}")


if __name__ == "__main__":
    main()

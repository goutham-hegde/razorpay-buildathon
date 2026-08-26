"""Properties the generated world must hold, so that the batch is arguable in a review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reclaim.domain import RECURRING_RAILS, Rail, RootCause
from reclaim.synth.generator import _ERROR_VARIANTS, _RAIL_FAMILY, generate

ROOT = Path(__file__).resolve().parents[1]

#: description template -> rail families it is allowed to appear on
_TEMPLATE_RAILS: dict[str, set[str]] = {}
for _variants in _ERROR_VARIANTS.values():
    for _v in _variants:
        _TEMPLATE_RAILS.setdefault(_v[4], set()).update(_v[5])


def test_error_text_is_valid_for_its_rail() -> None:
    """A card payment must never come back with a UPI collect error, and vice versa.

    This is the kind of detail a reviewer notices immediately and which quietly
    invalidates the whole dataset, so it is asserted rather than eyeballed.
    """
    _, _, cases, _ = generate(n=400, seed=7, batch="A")
    for case in cases:
        err = case.attempts[0].error
        assert err is not None
        family = _RAIL_FAMILY[case.rail]
        template = next(
            (t for t in _TEMPLATE_RAILS if t.replace("{issuer}", case.issuer) == err.description),
            None,
        )
        assert template is not None, f"unrecognised error text: {err.description!r}"
        assert family in _TEMPLATE_RAILS[template], (
            f"{case.rail} carried text only valid on {_TEMPLATE_RAILS[template]}: "
            f"{err.description!r}"
        )


def test_impossible_causes_never_occur() -> None:
    """A stored-card debit cannot be abandoned at an OTP screen that never appears."""
    _, _, cases, truths = generate(n=600, seed=9, batch="A")
    for case in cases:
        cause = truths[case.id].root_cause
        if case.rail in RECURRING_RAILS:
            assert cause is not RootCause.AUTH_ABANDONED, (
                f"{case.rail} has no human present to abandon anything"
            )
        if case.rail not in (Rail.CARD_3DS, Rail.CARD_RECURRING):
            assert cause is not RootCause.INSTRUMENT_INVALID
        if case.rail in (Rail.UPI_INTENT, Rail.UPI_COLLECT, Rail.CARD_3DS, Rail.NETBANKING):
            assert cause is not RootCause.MANDATE_REVOKED, (
                "a one-time payment has no mandate to revoke"
            )


def test_generation_is_deterministic_for_a_seed() -> None:
    a = generate(n=50, seed=42, batch="A")[3]
    b = generate(n=50, seed=42, batch="A")[3]
    assert [t.root_cause for t in a.values()] == [t.root_cause for t in b.values()]


def test_batches_differ_and_b_is_harder() -> None:
    """Batch B shifts the mix, so a policy tuned on A cannot simply memorise it."""
    _, _, _, ta = generate(n=1500, seed=11, batch="A")
    _, _, _, tb = generate(n=1500, seed=20260904, batch="B")

    def share(truths, cause: RootCause) -> float:
        return sum(t.root_cause is cause for t in truths.values()) / len(truths)

    assert share(tb, RootCause.AMBIGUOUS_DEBITED) > share(ta, RootCause.AMBIGUOUS_DEBITED)


def test_organic_recovery_exists_and_is_not_universal() -> None:
    """The control arm needs something to measure, but not everything comes back."""
    _, _, _, truths = generate(n=800, seed=3, batch="A")
    organic = [t for t in truths.values() if t.organic_recovery_at is not None]
    assert 0.05 < len(organic) / len(truths) < 0.5


def test_deliberate_revocations_never_recover_on_their_own() -> None:
    """Someone who cancelled a mandate on purpose is not coming back unprompted."""
    _, _, _, truths = generate(n=800, seed=5, batch="B")
    for t in truths.values():
        if t.root_cause in (RootCause.MANDATE_REVOKED, RootCause.RISK_DECLINED):
            assert t.organic_recovery_at is None


@pytest.mark.parametrize("batch", ["A", "B"])
def test_written_batch_matches_regenerated_batch(batch: str) -> None:
    """The committed data must be reproducible from its recorded seed."""
    d = ROOT / "data" / batch
    if not (d / "meta.json").exists():
        pytest.skip(f"batch {batch} not generated")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    _, _, cases, _ = generate(n=meta["n"], seed=meta["seed"], batch=batch)
    on_disk = [json.loads(line)["id"] for line in (d / "cases.jsonl").open(encoding="utf-8")]
    assert [c.id for c in cases] == on_disk

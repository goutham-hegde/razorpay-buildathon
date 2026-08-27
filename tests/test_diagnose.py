"""Diagnosis: the seal at the prompt boundary, the stub's behaviour, and cache durability.

The most important test here is `test_observable_leaks_no_ground_truth`. Every other seal
check guards an import or a file read; this one guards the *payload*, which is the one place
where ground truth could plausibly reach a model by accident and where it would be least
visible afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from reclaim.core.diagnose import (
    Diagnosis,
    GeminiDiagnoser,
    GroqDiagnoser,
    QuotaExhausted,
    StubDiagnoser,
    build,
    cache_path,
    load_diagnoses,
    observable,
    run,
)
from reclaim.domain import (
    Case,
    ErrorSource,
    ErrorStep,
    ObservedError,
    PaymentAttempt,
    PaymentStatus,
    Rail,
    RootCause,
)

T0 = datetime(2026, 8, 10, 12, 0)


def make_case(
    case_id: str = "case_1",
    description: str = "insufficient balance in payer account",
    *,
    rail: Rail = Rail.UPI_AUTOPAY,
    kind: str = "recurring",
    bank_reference: str | None = None,
    code: str = "BAD_REQUEST_ERROR",
    reason: str = "payment_failed",
) -> Case:
    err = ObservedError(
        code=code,
        source=ErrorSource.BANK,
        step=ErrorStep.PAYMENT_AUTHORIZATION,
        reason=reason,
        description=description,
        bank_reference=bank_reference,
    )
    return Case(
        id=case_id,
        customer_id="cus_1",
        amount_paise=49900,
        opened_at=T0,
        kind=kind,
        rail=rail,
        issuer="HDFC",
        psp="psp_a",
        attempts=[
            PaymentAttempt(
                id=f"pay_{case_id}_0",
                case_id=case_id,
                customer_id="cus_1",
                amount_paise=49900,
                rail=rail,
                issuer="HDFC",
                psp="psp_a",
                created_at=T0,
                status=PaymentStatus.FAILED,
                error=err,
            )
        ],
    )


# ---------------------------------------------------------------------------
# The seal, at the prompt boundary
# ---------------------------------------------------------------------------

#: Field names that exist only in `truth.jsonl`.
TRUTH_FIELDS = (
    "root_cause",
    "persona",
    "organic_recovery_at",
    "outage_ends_at",
    "healthy_psp",
    "funds_return_at",
    "typical_ticket_paise",
    "monthly_value_paise",
    "instrument_alive",
    "mandate_alive",
)


def test_observable_leaks_no_ground_truth() -> None:
    """What goes into the prompt must not contain the answer.

    Import boundaries and file allow-lists cannot catch a leak here - a `Case` built by the
    generator could in principle carry an extra attribute, and it would flow silently into
    the prompt and inflate accuracy with nobody the wiser.
    """
    blob = json.dumps(observable(make_case()))
    for field in TRUTH_FIELDS:
        assert field not in blob, f"{field} reached the diagnoser's prompt"


def test_observable_matches_the_error_the_agent_was_given() -> None:
    case = make_case(description="acct bal low", bank_reference="HDFC123")
    obs = observable(case)
    assert obs["error_description"] == "acct bal low"
    assert obs["bank_reference"] == "HDFC123"
    assert obs["rail"] == "upi_autopay"


def test_stub_and_model_are_given_identical_input() -> None:
    """Both providers call `observable()`, so a comparison between them is like-for-like."""
    case = make_case()
    assert observable(case) == observable(case)


# ---------------------------------------------------------------------------
# The stub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        ("insufficient balance in payer account", RootCause.INSUFFICIENT_FUNDS),
        ("acct bal low, mandate presentation returned unpaid", RootCause.INSUFFICIENT_FUNDS),
        ("per txn cap exceeded for this account", RootCause.LIMIT_EXCEEDED),
        ("card expired", RootCause.INSTRUMENT_INVALID),
        ("mandate revoked by customer in PSP app", RootCause.MANDATE_REVOKED),
        ("declined by risk rule", RootCause.RISK_DECLINED),
        ("customer cancelled at HDFC authentication page", RootCause.AUTH_ABANDONED),
        ("gateway did not respond in time", RootCause.PSP_ROUTING_FAILURE),
        ("issuer timed out", RootCause.ISSUER_TECHNICAL_DECLINE),
    ],
)
def test_stub_matches_its_rules(description: str, expected: RootCause) -> None:
    d = StubDiagnoser().diagnose(make_case(description=description))
    assert d.root_cause is expected


def test_stub_is_deterministic() -> None:
    """It has to be, or the arm it backs is not reproducible."""
    case = make_case(description="issuer timed out")
    a, b = StubDiagnoser().diagnose(case), StubDiagnoser().diagnose(case)
    assert (a.root_cause, a.confidence, a.rationale) == (b.root_cause, b.confidence, b.rationale)


def test_stub_never_predicts_ambiguous_debited() -> None:
    """The point of the whole arm, asserted rather than left as an observation.

    Distinguishing an ambiguous debit from a plain timeout means weighing whether a bank
    reference came back - a tendency, not a keyword. A string matcher structurally cannot do
    it, and on batch B the stub scores 0/26 recall on this class. That gap is the measurable
    case for the model, so it is pinned here: if a future edit teaches the stub this class
    by some other route, this test should fail and the claim should be rewritten.
    """
    stub = StubDiagnoser()
    for description, ref in (
        ("deemed approved pending settlement confirmation", "HDFC99887766"),
        ("no response from issuer, final status unknown", "ICIC12341234"),
        ("transaction status could not be confirmed", None),
    ):
        d = stub.diagnose(make_case(description=description, bank_reference=ref))
        assert d.root_cause is not RootCause.AMBIGUOUS_DEBITED


def test_stub_falls_back_rather_than_failing() -> None:
    d = StubDiagnoser().diagnose(make_case(description="zxqv unmatched nonsense"))
    assert d.root_cause is RootCause.ISSUER_TECHNICAL_DECLINE
    assert d.confidence < 0.3  # honest about being a guess


def test_stub_handles_a_case_with_no_error() -> None:
    case = make_case()
    case.attempts[0].error = None
    assert StubDiagnoser().diagnose(case).root_cause is RootCause.ISSUER_TECHNICAL_DECLINE


# ---------------------------------------------------------------------------
# Serialisation and the cache
# ---------------------------------------------------------------------------


def test_diagnosis_round_trips_through_json() -> None:
    d = Diagnosis("case_1", RootCause.AMBIGUOUS_DEBITED, 0.64, "why", "gemini", "flash")
    assert Diagnosis.from_dict(json.loads(d.to_json())) == d


def test_run_writes_a_cache_that_reloads(tmp_path) -> None:
    cases = [make_case(f"case_{i}") for i in range(5)]
    path = tmp_path / "diagnoses.jsonl"
    run(cases, StubDiagnoser(), path, progress_every=0)
    assert len(load_diagnoses(path)) == 5
    assert load_diagnoses(path)["case_3"].root_cause is RootCause.INSUFFICIENT_FUNDS


def test_run_resumes_and_does_not_redo_work(tmp_path) -> None:
    """Free-tier rate limits mean a 1,200-case pass gets interrupted. Losing it is the bug."""
    cases = [make_case(f"case_{i}") for i in range(5)]
    path = tmp_path / "diagnoses.jsonl"
    run(cases[:3], StubDiagnoser(), path, progress_every=0)

    class Exploding(StubDiagnoser):
        def diagnose(self, case):  # noqa: D102
            if case.id in {"case_0", "case_1", "case_2"}:
                raise AssertionError(f"re-diagnosed {case.id}, which was already cached")
            return super().diagnose(case)

    out = run(cases, Exploding(), path, progress_every=0)
    assert len(out) == 5
    assert len(load_diagnoses(path)) == 5


def test_no_resume_rewrites_the_cache(tmp_path) -> None:
    cases = [make_case(f"case_{i}") for i in range(5)]
    path = tmp_path / "diagnoses.jsonl"
    run(cases, StubDiagnoser(), path, progress_every=0)
    run(cases[:2], StubDiagnoser(), path, resume=False, progress_every=0)
    assert len(load_diagnoses(path)) == 2


def test_missing_cache_reads_as_empty(tmp_path) -> None:
    assert load_diagnoses(tmp_path / "nope.jsonl") == {}


def test_stub_and_model_caches_do_not_collide(tmp_path) -> None:
    """Two arms, two files. Overwriting one with the other would silently fake a result."""
    assert cache_path(tmp_path, "stub") != cache_path(tmp_path, "gemini")


# ---------------------------------------------------------------------------
# Provider wiring
# ---------------------------------------------------------------------------


def test_build_returns_the_stub_without_any_key() -> None:
    assert isinstance(build("stub"), StubDiagnoser)


def test_build_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build("gpt")


def test_gemini_without_a_key_says_how_to_proceed(monkeypatch) -> None:
    """The error has to name both fixes: get a free key, or use the stub."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        GeminiDiagnoser()
    assert "stub" in str(exc.value)
    assert "aistudio.google.com" in str(exc.value)


def test_gemini_records_an_unparseable_response_instead_of_dropping_it() -> None:
    """Dropping unparseable cases would quietly delete the hardest ones from the accuracy."""
    g = GeminiDiagnoser(api_key="test-key")
    d = g._parse(make_case(), {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})
    assert d.confidence == 0.0
    assert "unparseable" in d.rationale


def test_gemini_clamps_a_confidence_outside_zero_to_one() -> None:
    g = GeminiDiagnoser(api_key="test-key")
    body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": json.dumps({"root_cause": "risk_declined", "confidence": 7.5})}
                    ]
                }
            }
        ]
    }
    assert g._parse(make_case(), body).confidence == 1.0


# ---------------------------------------------------------------------------
# Waiting out a rate limit
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Just enough of an httpx response for the retry loop."""

    def __init__(self, status_code: int, headers: dict, body: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


def test_a_rate_limit_wait_is_broken_into_bounded_naps(monkeypatch) -> None:
    """`retry-after` is an estimate against a rolling window, and it overshoots.

    Sleeping for the whole of it once left a run idle for 25 minutes on a limit that had
    cleared in two. The loop naps instead, so the run resumes when the quota does.
    """
    import reclaim.core.diagnose as mod

    naps: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: naps.append(s))
    ok = _FakeResponse(
        200,
        {},
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"root_cause": "risk_declined", "confidence": 0.9}
                        )
                    }
                }
            ]
        },
    )
    limited = _FakeResponse(429, {"retry-after": "1800", "x-ratelimit-limit-tokens": "8000"})

    g = GroqDiagnoser(api_key="test-key", max_sleep=180.0)

    class _Client:
        def __init__(self) -> None:
            self.queue = [limited, limited, ok]

        def post(self, *a, **kw):
            return self.queue.pop(0)

    g._client = _Client()
    d = g.diagnose(make_case())

    assert d.root_cause is RootCause.RISK_DECLINED
    assert naps == [180.0, 180.0], "each wait is capped, not slept in full"


def test_waiting_for_quota_does_not_consume_the_retry_budget(monkeypatch) -> None:
    """A 429 is the API working correctly. Only a broken call should spend an attempt."""
    import reclaim.core.diagnose as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    limited = _FakeResponse(429, {"retry-after": "10", "x-ratelimit-limit-tokens": "8000"})
    ok = _FakeResponse(
        200,
        {},
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"root_cause": "insufficient_funds", "confidence": 0.8}
                        )
                    }
                }
            ]
        },
    )

    g = GroqDiagnoser(api_key="test-key", max_attempts=2, max_sleep=10.0)

    class _Client:
        def __init__(self) -> None:
            self.queue = [limited] * 20 + [ok]

        def post(self, *a, **kw):
            return self.queue.pop(0)

    g._client = _Client()
    # Twenty 429s is far past `max_attempts`; the call still succeeds because none of them
    # was an attempt in the sense the budget means.
    assert g.diagnose(make_case()).root_cause is RootCause.INSUFFICIENT_FUNDS


def test_a_quota_wait_gives_up_once_the_ceiling_is_spent(monkeypatch) -> None:
    """Bounded naps must not become an unbounded loop."""
    import reclaim.core.diagnose as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    limited = _FakeResponse(429, {"retry-after": "60", "x-ratelimit-limit-tokens": "8000"})

    g = GroqDiagnoser(api_key="test-key", max_wait=120.0, max_sleep=60.0)

    class _Client:
        def post(self, *a, **kw):
            return limited

    g._client = _Client()
    with pytest.raises(QuotaExhausted):
        g.diagnose(make_case())


def test_a_truncated_reply_is_refused_rather_than_recorded(monkeypatch) -> None:
    """The one failure mode of a low token ceiling that does not announce itself.

    `_parse` turns anything unreadable into a zero-confidence prediction on purpose, so a
    reply cut off mid-JSON would be committed to `diagnoses.jsonl` looking exactly like a
    genuine hard case. `finish_reason == "length"` is the only thing that tells them apart.
    """
    import reclaim.core.diagnose as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    cut_off = _FakeResponse(
        200,
        {},
        {
            "choices": [{"finish_reason": "length", "message": {"content": '{"root_ca'}}],
            "usage": {"prompt_tokens": 1126, "completion_tokens": 64},
        },
    )

    g = GroqDiagnoser(api_key="test-key", max_attempts=1, max_completion_tokens=64)

    class _Client:
        def post(self, *a, **kw):
            return cut_off

    g._client = _Client()
    with pytest.raises(RuntimeError, match="ceiling"):
        g.diagnose(make_case())


def test_a_complete_reply_at_the_ceiling_is_still_accepted() -> None:
    """`finish_reason == "stop"` is the model finishing, however close to the cap it got."""
    g = GroqDiagnoser(api_key="test-key", max_completion_tokens=64)
    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"root_cause": "auth_abandoned", "confidence": 0.7})
                },
            }
        ]
    }
    assert not g._truncated(body)
    assert g._parse(make_case(), body).root_cause is RootCause.AUTH_ABANDONED

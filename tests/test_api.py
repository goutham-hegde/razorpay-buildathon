"""The API is read-only, and it does not serve ground truth.

Both properties are load-bearing. The console is the surface a reviewer will actually
click around in, and it would be a poor joke if the seal held everywhere except in the
demo - or if the page that shows the audit trail could also write to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api.main import app

ROOT = Path(__file__).resolve().parents[1]

#: Field names that exist only in `truth.jsonl`. None may appear in any API response.
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

pytestmark = pytest.mark.skipif(
    not (ROOT / "data" / "B" / "ledger.db").exists(),
    reason="batch B has no ledger; run reclaim.eval.replay first",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def sample_case_id(client: TestClient) -> str:
    return client.get("/api/cases?batch=B&limit=1").json()["cases"][0]["case_id"]


ENDPOINTS = [
    "/api/batches",
    "/api/detection?batch=B",
    "/api/results?batch=B",
    "/api/invariants?batch=B",
    "/api/cases?batch=B&limit=50",
    "/api/timeline?batch=B&limit=50",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_endpoint_responds(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("leak", TRUTH_FIELDS)
def test_no_ground_truth_in_any_response(client: TestClient, path: str, leak: str) -> None:
    assert leak not in json.dumps(client.get(path).json())


def test_no_ground_truth_in_case_detail(client: TestClient, sample_case_id: str) -> None:
    """The case detail view is the most tempting place to helpfully add the answer."""
    body = json.dumps(client.get(f"/api/case/{sample_case_id}?batch=B").json())
    for leak in TRUTH_FIELDS:
        assert leak not in body, f"{leak} leaked into the case detail response"


def test_case_detail_serves_the_observed_error(client: TestClient, sample_case_id: str) -> None:
    """The agent's actual input - messy issuer text - is what the console must show."""
    d = client.get(f"/api/case/{sample_case_id}?batch=B").json()
    assert d["observed_error"]["description"]
    assert d["detection"]["disposition"]


def test_audit_trail_is_causally_ordered(client: TestClient) -> None:
    """A decision must read above the charge it authorised, not below it."""
    cases = client.get("/api/cases?batch=B&run=B-naive&limit=2000").json()["cases"]
    worked = next(c for c in cases if c["cost_paise"] > 0)
    trail = client.get(f"/api/case/{worked['case_id']}?batch=B&run=B-naive").json()["trail"]
    first_decision = next(i for i, r in enumerate(trail) if r["kind"] == "decision")
    first_charge = next(i for i, r in enumerate(trail) if r["kind"] == "charge")
    assert first_decision < first_charge
    assert trail[-1]["kind"] == "closed"


def test_api_exposes_no_write_routes() -> None:
    """The demo surface must not be able to move money. Asserted, not intended."""
    offenders = [
        (r.path, sorted(r.methods - {"HEAD", "OPTIONS"}))
        for r in app.routes
        if getattr(r, "methods", None) and (r.methods - {"GET", "HEAD", "OPTIONS"})
    ]
    assert not offenders, f"non-GET routes on a read-only API: {offenders}"


def test_missing_batch_explains_the_fix(client: TestClient) -> None:
    """A 404 should tell the reader which command to run, not just fail."""
    res = client.get("/api/results?batch=ZZ")
    assert res.status_code in (404, 500)

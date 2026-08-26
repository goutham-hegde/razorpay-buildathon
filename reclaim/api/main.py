"""Read-only HTTP view over the ledger, plus the static console that renders it.

The API takes no actions. It cannot start a run, retry a payment or send a message - it
reads `case_outcomes`, `decisions`, `charge_claims` and `contacts` and serves them. That is
deliberate: the demo surface should not be able to move money, and a reviewer should be
able to satisfy themselves of that by reading one file.

The Live view is a *replay*, not a simulation. It plays back the recorded decision
timeline of a run that already happened, at an accelerated clock. Nothing is generated for
the screen - every row it shows is a row in the ledger, and the same rows back the numbers
on the Results view.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from reclaim.core import feed
from reclaim.core.detect import detect, summarise
from reclaim.core.guards import check_batch
from reclaim.core.ledger import Ledger, open_ledger
from reclaim.eval.metrics import batch_metrics

DATA_ROOT = Path("data")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="reclaim", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _batches() -> list[str]:
    if not DATA_ROOT.exists():
        return []
    return sorted(
        d.name for d in DATA_ROOT.iterdir() if d.is_dir() and (d / "cases.jsonl").exists()
    )


def _batch(name: str) -> feed.Batch:
    """Load a batch, or explain which command would create it."""
    if not (DATA_ROOT / name.upper() / "cases.jsonl").exists():
        raise HTTPException(
            404,
            f"no batch {name.upper()} on disk - run "
            f"`python -m reclaim.synth.generator --batch {name.upper()}`",
        )
    return feed.load_batch(name, DATA_ROOT)


def _ledger(batch: str) -> Ledger:
    path = DATA_ROOT / batch.upper() / "ledger.db"
    if not path.exists():
        raise HTTPException(
            404,
            f"no ledger for batch {batch.upper()} - run "
            f"`python -m reclaim.eval.replay --batch {batch.upper()} --arms all` first",
        )
    return open_ledger(batch, DATA_ROOT)


# ---------------------------------------------------------------------------
# Batch and detection
# ---------------------------------------------------------------------------


@app.get("/api/batches")
def batches() -> dict[str, Any]:
    out = []
    for name in _batches():
        b = _batch(name)
        out.append(
            {
                "name": name,
                "cases": len(b.cases),
                "at_risk_paise": b.at_risk_paise,
                "seed": b.meta.get("seed"),
                "role": "held out - reported" if name == "B" else "tuning",
                "has_ledger": (DATA_ROOT / name / "ledger.db").exists(),
            }
        )
    return {"batches": out}


@app.get("/api/detection")
def detection(batch: str = Query("B")) -> dict[str, Any]:
    """Triage counts, and the rules that produced them."""
    b = _batch(batch)
    detections = detect(b.cases, b.customers, b.mandates)
    return {
        "batch": b.name,
        "total": len(detections),
        "by_disposition": summarise(detections),
    }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@app.get("/api/results")
def results(batch: str = Query("B")) -> dict[str, Any]:
    b = _batch(batch)
    with _ledger(batch) as lg:
        arms = [m.as_dict() for m in batch_metrics(lg, batch)]
    return {
        "batch": b.name,
        "cases": len(b.cases),
        "at_risk_paise": b.at_risk_paise,
        "arms": arms,
        # Named here rather than in the page so the frontend cannot quietly claim an arm
        # exists before it has been built.
        "pending_arms": [
            a for a in ("control", "naive", "rules", "agent")
            if a not in {m["arm"] for m in arms}
        ],
    }


@app.get("/api/invariants")
def invariants(batch: str = Query("B")) -> dict[str, Any]:
    reports = check_batch(batch, DATA_ROOT)
    return {
        "batch": batch.upper(),
        "reports": [
            {
                "run_id": r.run_id,
                "arm": r.arm,
                "must_hold": r.must_hold,
                "held": r.held,
                "total": r.total,
                "results": [
                    {
                        "id": g.id,
                        "title": g.title,
                        "held": g.held,
                        "note": g.note,
                        "checked": g.checked,
                        "violations": [
                            {"subject": v.subject, "detail": v.detail} for v in g.violations[:25]
                        ],
                        "violation_count": len(g.violations),
                    }
                    for g in r.results
                ],
            }
            for r in reports
        ],
    }


# ---------------------------------------------------------------------------
# Cases and the audit trail
# ---------------------------------------------------------------------------


@app.get("/api/cases")
def cases(
    batch: str = Query("B"),
    run: str | None = None,
    status: str | None = None,
    limit: int = Query(200, le=2000),
) -> dict[str, Any]:
    b = _batch(batch)
    by_id = {c.id: c for c in b.cases}
    with _ledger(batch) as lg:
        run = run or _default_run(lg, batch)
        rows = []
        for o in lg.outcomes(run):
            if status and o["status"] != status:
                continue
            case = by_id.get(o["case_id"])
            if case is None:
                continue
            rows.append(
                {
                    "case_id": o["case_id"],
                    "customer_id": case.customer_id,
                    "amount_paise": case.amount_paise,
                    "rail": str(case.rail),
                    "issuer": case.issuer,
                    "psp": case.psp,
                    "kind": case.kind,
                    "opened_at": case.opened_at.isoformat(),
                    "error": case.attempts[-1].error.description if case.attempts else None,
                    "status": o["status"],
                    "recovered_paise": o["recovered_paise"],
                    "recovered_by": o["recovered_by"],
                    "cost_paise": o["cost_paise"],
                    "mandate_halted": bool(o["mandate_halted"]),
                }
            )
            if len(rows) >= limit:
                break
    return {"batch": b.name, "run_id": run, "cases": rows}


@app.get("/api/case/{case_id}")
def case_detail(case_id: str, batch: str = Query("B"), run: str | None = None) -> dict[str, Any]:
    """One case, everything the agent saw, and everything it did. The audit trail."""
    b = _batch(batch)
    try:
        case = b.case(case_id)
    except KeyError:
        raise HTTPException(404, f"no such case: {case_id}") from None

    detections = {d.case_id: d for d in detect(b.cases, b.customers, b.mandates)}
    d = detections[case_id]
    attempt = case.attempts[-1] if case.attempts else None
    customer = b.customers.get(case.customer_id)

    with _ledger(batch) as lg:
        run = run or _default_run(lg, batch)
        trail = [_jsonable(r) for r in lg.case_trail(run, case_id)]

    return {
        "run_id": run,
        "case": {
            "id": case.id,
            "customer_id": case.customer_id,
            "amount_paise": case.amount_paise,
            "kind": case.kind,
            "rail": str(case.rail),
            "issuer": case.issuer,
            "psp": case.psp,
            "opened_at": case.opened_at.isoformat(),
            "mandate_id": case.mandate_id,
        },
        # Exactly what the agent is given. Ground truth is not served by this API at all.
        "observed_error": (
            {
                "code": attempt.error.code,
                "source": str(attempt.error.source),
                "step": str(attempt.error.step),
                "reason": attempt.error.reason,
                "description": attempt.error.description,
                "bank_reference": attempt.error.bank_reference,
            }
            if attempt and attempt.error
            else None
        ),
        "customer": (
            {
                "id": customer.id,
                "channels": customer.contactable_channels,
                "opted_out": customer.opted_out,
            }
            if customer
            else None
        ),
        "detection": {
            "disposition": str(d.disposition),
            "reason": d.reason,
            "priority_paise": d.priority_paise,
            "flags": list(d.flags),
        },
        "trail": trail,
    }


# ---------------------------------------------------------------------------
# Live replay
# ---------------------------------------------------------------------------


@app.get("/api/timeline")
def timeline(
    batch: str = Query("B"),
    run: str | None = None,
    limit: int = Query(400, le=5000),
) -> dict[str, Any]:
    """The recorded decision timeline, in simulated-clock order, for the Live view.

    Skips the bulk `close` decisions - a live console showing six hundred identical
    closures is not showing anything. Closures remain in the per-case audit trail.
    """
    b = _batch(batch)
    by_id = {c.id: c for c in b.cases}
    with _ledger(batch) as lg:
        run = run or _default_run(lg, batch)
        events: list[dict[str, Any]] = []
        for r in lg.decisions(run):
            if r["action"] in ("close", "skip"):
                continue
            case = by_id.get(r["case_id"])
            events.append(
                {
                    "kind": "decision",
                    "at": r["at"],
                    "case_id": r["case_id"],
                    "action": r["action"],
                    "reason": r["reason"],
                    "diagnosis": r["diagnosis"],
                    "confidence": r["confidence"],
                    "amount_paise": case.amount_paise if case else None,
                    "rail": str(case.rail) if case else None,
                }
            )
        for r in lg.charges(run):
            events.append(
                {
                    "kind": "charge",
                    "at": r["at"],
                    "case_id": r["case_id"],
                    "attempt_no": r["attempt_no"],
                    "outcome": r["outcome"] or "unresolved",
                    "double_charge": bool(r["double_charge"]),
                    "amount_paise": r["amount_paise"],
                    "rail": r["rail"],
                }
            )
        for r in lg.contacts(run):
            events.append(
                {
                    "kind": "contact",
                    "at": r["at"],
                    "case_id": r["case_id"],
                    "channel": r["channel"],
                    "engaged": bool(r["engaged"]),
                }
            )
        ledger_rows = sum(
            lg.db.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id = ?", (run,)).fetchone()[0]
            for t in ("decisions", "charge_claims", "charge_results", "contacts", "case_outcomes")
        )

    events.sort(key=lambda e: (str(e["at"]), e["kind"]))
    return {
        "batch": b.name,
        "run_id": run,
        "ledger_rows": ledger_rows,
        "total_events": len(events),
        "events": events[:limit],
    }


# ---------------------------------------------------------------------------


def _default_run(lg: Ledger, batch: str) -> str:
    runs = lg.runs(batch.upper())
    if not runs:
        raise HTTPException(404, f"no runs recorded for batch {batch.upper()}")
    # Prefer the most capable arm that has actually been built.
    order = {"agent": 0, "rules": 1, "naive": 2, "control": 3}
    return sorted(runs, key=lambda r: order.get(r["arm"], 9))[0]["run_id"]


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("payload"), str):
        try:
            out["payload"] = json.loads(out["payload"])
        except (ValueError, TypeError):
            pass
    return out


# ---------------------------------------------------------------------------

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")

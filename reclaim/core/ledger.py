"""The append-only decision ledger. The audit trail, and where invariant R1 actually lives.

Two things this file is trying to be, both of which a reviewer should be able to verify by
reading the schema rather than by trusting prose:

APPEND-ONLY IS ENFORCED, NOT PROMISED
    Every table carries `BEFORE UPDATE` and `BEFORE DELETE` triggers that abort. A ledger
    you can quietly correct is not an audit trail, and "we only ever insert" is a code
    review convention that survives exactly until the first bug fix under deadline.

R1 IS STRUCTURAL
    `UNIQUE (run_id, payment_id, attempt_no)` on `charge_claims`, and the executor
    **inserts first, then charges**. The insert either succeeds - claiming the sole right
    to make that attempt - or raises `DuplicateAttempt`, in which case the attempt has
    already been made and must not be made again.

    The tempting alternative is `SELECT` to check, then `INSERT` if absent. That has a
    window between the two statements in which a retry, a redelivered webhook or a second
    worker sees the same empty result and both proceed. The window is small and the
    failure mode is a duplicate charge on a real customer's card. A unique constraint is
    evaluated atomically and has no window at all.

    (`run_id` is in the key because four arms replay the same payment ids over the same
    batch. Within a run - which is what a production deployment would be - the key is
    exactly `(payment_id, attempt_no)`.)

WHY CHARGING IS TWO-PHASE
    Append-only means a claim row cannot later be updated with its result, so the result
    is a separate row in `charge_results` keyed to the claim. That is not a workaround; it
    is the correct shape. A claim with no result is a charge that was authorised, may have
    reached the issuer, and whose outcome we never learned - which is precisely the
    `AMBIGUOUS_DEBITED` state this whole project is organised around. Making it
    representable means the audit trail can show it instead of silently picking a side.

    Note what the constraint does and does not buy. Structural uniqueness stops the
    *system* from firing one attempt twice. It cannot stop a *policy* from choosing to
    retry a payment that already succeeded upstream. That is caught by the R1 check in
    `core.guards`, not by the schema. Both are needed, they are different failures, and
    `guards` reports which one broke.

The database is a working artifact and is gitignored. `export_jsonl()` writes the same
rows out as newline-delimited JSON so the audit trail behind a reported run can be
committed alongside the numbers it produced.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

#: Every table is append-only. Listed once so the triggers and the export cannot drift.
_TABLES = (
    "runs",
    "run_summary",
    "decisions",
    "charge_claims",
    "charge_results",
    "contacts",
    "opt_outs",
    "case_outcomes",
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- One row per (batch, arm) execution. Arms share a database so they can be compared.
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    batch       TEXT NOT NULL,
    arm         TEXT NOT NULL,
    seed        INTEGER,
    started_at  TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS run_summary (
    run_id          TEXT PRIMARY KEY REFERENCES runs(run_id),
    finished_at     TEXT NOT NULL,
    horizon_end     TEXT NOT NULL,
    cases_detected  INTEGER NOT NULL,
    cases_eligible  INTEGER NOT NULL
);

-- Why the agent did what it did. The reasoning trail, kept separate from the actions so
-- that "what we decided" and "what we did" can be reconciled against each other.
CREATE TABLE IF NOT EXISTS decisions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    case_id     TEXT NOT NULL,
    at          TEXT NOT NULL,           -- simulated clock
    recorded_at TEXT NOT NULL,           -- wall clock, so replays are distinguishable
    action      TEXT NOT NULL,           -- retry | reroute | contact | escalate
                                         -- | hold | wait | close | skip
    reason      TEXT NOT NULL,           -- in words, for the audit trail
    diagnosis   TEXT,                    -- predicted root cause; null before D3
    confidence  REAL,
    payload     TEXT                     -- JSON, action-specific
);
CREATE INDEX IF NOT EXISTS ix_decisions_case ON decisions(run_id, case_id, at);

-- Phase 1 of charging: the reserved right to make exactly one attempt. R1 lives here.
CREATE TABLE IF NOT EXISTS charge_claims (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    case_id       TEXT NOT NULL,
    payment_id    TEXT NOT NULL,         -- the original failed payment being recovered
    attempt_no    INTEGER NOT NULL,      -- 0 is the original failure; 1..n are ours
    at            TEXT NOT NULL,
    rail          TEXT NOT NULL,
    psp           TEXT NOT NULL,
    amount_paise  INTEGER NOT NULL,
    UNIQUE (run_id, payment_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_claims_case ON charge_claims(run_id, case_id, at);

-- Phase 2: what the claim turned into. A claim with no row here is an unresolved charge.
CREATE TABLE IF NOT EXISTS charge_results (
    claim_seq     INTEGER PRIMARY KEY REFERENCES charge_claims(seq),
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    case_id       TEXT NOT NULL,
    settled_at    TEXT NOT NULL,
    outcome       TEXT NOT NULL,         -- captured | failed
    double_charge INTEGER NOT NULL DEFAULT 0,
    cost_paise    INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_results_case ON charge_results(run_id, case_id);

-- Outreach. R3 and R4 are checked against this table.
CREATE TABLE IF NOT EXISTS contacts (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    case_id        TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    contact_no     INTEGER NOT NULL,     -- nth contact on this case
    at             TEXT NOT NULL,
    channel        TEXT NOT NULL,
    template       TEXT NOT NULL,
    with_incentive INTEGER NOT NULL DEFAULT 0,
    delivered      INTEGER NOT NULL,
    engaged        INTEGER NOT NULL,
    cost_paise     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, case_id, contact_no)
);
CREATE INDEX IF NOT EXISTS ix_contacts_customer ON contacts(run_id, customer_id, at);

-- Opt-outs are terminal and irreversible. R5 is checked against this table.
CREATE TABLE IF NOT EXISTS opt_outs (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    customer_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL,
    UNIQUE (run_id, customer_id)
);

-- Terminal state, written once per case. R5 and R6 are checked against this table.
CREATE TABLE IF NOT EXISTS case_outcomes (
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    case_id             TEXT NOT NULL,
    closed_at           TEXT NOT NULL,
    status              TEXT NOT NULL,   -- recovered | abandoned | escalated
                                         -- | reconcile_hold | written_off | not_eligible
    recovered_paise     INTEGER NOT NULL DEFAULT 0,
    recovered_by        TEXT,            -- organic | charge | null
    at_risk_paise       INTEGER NOT NULL,
    cost_paise          INTEGER NOT NULL DEFAULT 0,
    mandate_halted      INTEGER NOT NULL DEFAULT 0,
    residual_loss_paise INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, case_id)
);
"""

#: Append-only triggers, generated for every table so that none can be forgotten.
_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE on {t} is not permitted'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: DELETE on {t} is not permitted'); END;
"""
    for t in _TABLES
)


class DuplicateAttempt(Exception):
    """This (payment_id, attempt_no) is already claimed, so the charge has already been made.

    There is no safe way to catch this and proceed to charge. Catching it and continuing
    defeats the only structural protection against double-charging a real customer.
    """


class DuplicateContact(Exception):
    """This contact number on this case is already recorded."""


@dataclass(frozen=True, slots=True)
class ChargeClaim:
    """A reserved right to make exactly one charge attempt.

    Returned by `claim_charge` *before* the money moves. Pass it to `settle_charge` with
    whatever the rail came back with.
    """

    seq: int
    run_id: str
    case_id: str
    payment_id: str
    attempt_no: int


class Ledger:
    """Append-only store for decisions, actions and outcomes."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.executescript(_TRIGGERS)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        batch: str,
        arm: str,
        seed: int | None = None,
        notes: str = "",
    ) -> str:
        self.db.execute(
            "INSERT INTO runs (run_id, batch, arm, seed, started_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, batch, arm, seed, datetime.now().isoformat(timespec="seconds"), notes),
        )
        return run_id

    def finish_run(
        self, run_id: str, horizon_end: datetime, cases_detected: int, cases_eligible: int
    ) -> None:
        self.db.execute(
            "INSERT INTO run_summary "
            "(run_id, finished_at, horizon_end, cases_detected, cases_eligible) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                datetime.now().isoformat(timespec="seconds"),
                horizon_end.isoformat(),
                cases_detected,
                cases_eligible,
            ),
        )

    def runs(self, batch: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT r.*, s.finished_at, s.horizon_end, s.cases_detected, s.cases_eligible "
            "FROM runs r LEFT JOIN run_summary s USING (run_id)"
        )
        args: tuple = ()
        if batch:
            sql += " WHERE r.batch = ?"
            args = (batch,)
        return list(self.db.execute(sql + " ORDER BY r.started_at, r.run_id", args))

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT r.*, s.finished_at, s.horizon_end, s.cases_detected, s.cases_eligible "
            "FROM runs r LEFT JOIN run_summary s USING (run_id) WHERE r.run_id = ?",
            (run_id,),
        ).fetchone()

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self,
        run_id: str,
        case_id: str,
        at: datetime,
        action: str,
        reason: str,
        diagnosis: str | None = None,
        confidence: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions "
            "(run_id, case_id, at, recorded_at, action, reason, diagnosis, confidence, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                case_id,
                at.isoformat(),
                datetime.now().isoformat(timespec="seconds"),
                action,
                reason,
                diagnosis,
                confidence,
                json.dumps(payload, separators=(",", ":")) if payload else None,
            ),
        )
        return int(cur.lastrowid)

    def decisions(self, run_id: str, case_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        args: list = [run_id]
        if case_id:
            sql += " AND case_id = ?"
            args.append(case_id)
        return list(self.db.execute(sql + " ORDER BY seq", args))

    # -- charging ----------------------------------------------------------

    def claim_charge(
        self,
        run_id: str,
        case_id: str,
        payment_id: str,
        attempt_no: int,
        at: datetime,
        rail: str,
        psp: str,
        amount_paise: int,
    ) -> ChargeClaim:
        """Reserve the right to attempt this charge. Call BEFORE moving any money.

        Raises `DuplicateAttempt` if this attempt is already claimed.
        """
        try:
            cur = self.db.execute(
                "INSERT INTO charge_claims "
                "(run_id, case_id, payment_id, attempt_no, at, rail, psp, amount_paise) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, case_id, payment_id, attempt_no, at.isoformat(), rail, psp, amount_paise),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAttempt(
                f"{payment_id} attempt {attempt_no} is already claimed in run {run_id}; "
                "that charge has already been made and must not be repeated"
            ) from exc
        return ChargeClaim(int(cur.lastrowid), run_id, case_id, payment_id, attempt_no)

    def settle_charge(
        self,
        claim: ChargeClaim,
        captured: bool,
        at: datetime,
        double_charge: bool = False,
        cost_paise: int = 0,
        note: str = "",
    ) -> None:
        """Record what a claimed charge turned into."""
        self.db.execute(
            "INSERT INTO charge_results "
            "(claim_seq, run_id, case_id, settled_at, outcome, double_charge, cost_paise, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.seq,
                claim.run_id,
                claim.case_id,
                at.isoformat(),
                "captured" if captured else "failed",
                int(double_charge),
                cost_paise,
                note,
            ),
        )

    def charges(self, run_id: str, case_id: str | None = None) -> list[sqlite3.Row]:
        """Claims joined to their results. `outcome` is null for an unresolved charge."""
        sql = (
            "SELECT c.*, r.outcome, r.double_charge, r.cost_paise, r.note, r.settled_at "
            "FROM charge_claims c LEFT JOIN charge_results r ON r.claim_seq = c.seq "
            "WHERE c.run_id = ?"
        )
        args: list = [run_id]
        if case_id:
            sql += " AND c.case_id = ?"
            args.append(case_id)
        return list(self.db.execute(sql + " ORDER BY c.seq", args))

    def next_attempt_no(self, run_id: str, payment_id: str) -> int:
        row = self.db.execute(
            "SELECT MAX(attempt_no) AS n FROM charge_claims WHERE run_id = ? AND payment_id = ?",
            (run_id, payment_id),
        ).fetchone()
        return 1 if row["n"] is None else int(row["n"]) + 1

    def charge_count(self, run_id: str, case_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM charge_claims WHERE run_id = ? AND case_id = ?",
            (run_id, case_id),
        ).fetchone()
        return int(row["n"])

    # -- contacts ----------------------------------------------------------

    def record_contact(
        self,
        run_id: str,
        case_id: str,
        customer_id: str,
        contact_no: int,
        at: datetime,
        channel: str,
        template: str,
        delivered: bool,
        engaged: bool,
        with_incentive: bool = False,
        cost_paise: int = 0,
    ) -> int:
        try:
            cur = self.db.execute(
                "INSERT INTO contacts (run_id, case_id, customer_id, contact_no, at, channel, "
                "template, with_incentive, delivered, engaged, cost_paise) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    case_id,
                    customer_id,
                    contact_no,
                    at.isoformat(),
                    channel,
                    template,
                    int(with_incentive),
                    int(delivered),
                    int(engaged),
                    cost_paise,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateContact(
                f"contact {contact_no} on {case_id} already recorded in run {run_id}"
            ) from exc
        return int(cur.lastrowid)

    def contacts(self, run_id: str, customer_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM contacts WHERE run_id = ?"
        args: list = [run_id]
        if customer_id:
            sql += " AND customer_id = ?"
            args.append(customer_id)
        return list(self.db.execute(sql + " ORDER BY at, seq", args))

    def contacts_in_window(
        self, run_id: str, customer_id: str, at: datetime, window: timedelta
    ) -> int:
        """How many messages this customer has had in `window` ending at `at`.

        The policy calls this before sending; `guards` re-derives the same quantity from
        raw rows after the run, so a bug in this helper cannot hide behind itself.
        """
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM contacts "
            "WHERE run_id = ? AND customer_id = ? AND at > ? AND at <= ?",
            (run_id, customer_id, (at - window).isoformat(), at.isoformat()),
        ).fetchone()
        return int(row["n"])

    def last_contact_at(self, run_id: str, customer_id: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(at) AS t FROM contacts WHERE run_id = ? AND customer_id = ?",
            (run_id, customer_id),
        ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def contact_count(self, run_id: str, case_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE run_id = ? AND case_id = ?",
            (run_id, case_id),
        ).fetchone()
        return int(row["n"])

    # -- opt-outs ----------------------------------------------------------

    def record_opt_out(self, run_id: str, customer_id: str, at: datetime, source: str) -> None:
        try:
            self.db.execute(
                "INSERT INTO opt_outs (run_id, customer_id, at, source) VALUES (?, ?, ?, ?)",
                (run_id, customer_id, at.isoformat(), source),
            )
        except sqlite3.IntegrityError:
            pass  # already opted out; opting out twice is not an error

    def opt_outs(self, run_id: str) -> dict[str, datetime]:
        return {
            r["customer_id"]: datetime.fromisoformat(r["at"])
            for r in self.db.execute(
                "SELECT customer_id, at FROM opt_outs WHERE run_id = ?", (run_id,)
            )
        }

    # -- case outcomes -----------------------------------------------------

    def close_case(
        self,
        run_id: str,
        case_id: str,
        closed_at: datetime,
        status: str,
        at_risk_paise: int,
        recovered_paise: int = 0,
        recovered_by: str | None = None,
        cost_paise: int = 0,
        mandate_halted: bool = False,
        residual_loss_paise: int = 0,
    ) -> None:
        self.db.execute(
            "INSERT INTO case_outcomes (run_id, case_id, closed_at, status, recovered_paise, "
            "recovered_by, at_risk_paise, cost_paise, mandate_halted, residual_loss_paise) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                case_id,
                closed_at.isoformat(),
                status,
                recovered_paise,
                recovered_by,
                at_risk_paise,
                cost_paise,
                int(mandate_halted),
                residual_loss_paise,
            ),
        )

    def outcomes(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM case_outcomes WHERE run_id = ? ORDER BY case_id", (run_id,)
            )
        )

    def closed_case_ids(self, run_id: str) -> set[str]:
        return {
            r["case_id"]
            for r in self.db.execute(
                "SELECT case_id FROM case_outcomes WHERE run_id = ?", (run_id,)
            )
        }

    # -- audit trail -------------------------------------------------------

    def case_trail(self, run_id: str, case_id: str) -> list[dict[str, Any]]:
        """Everything that happened to one case, in time order. This is the audit trail."""
        rows: list[dict[str, Any]] = []
        for r in self.decisions(run_id, case_id):
            rows.append({"kind": "decision", "at": r["at"], **dict(r)})
        for r in self.charges(run_id, case_id):
            rows.append({"kind": "charge", "at": r["at"], **dict(r)})
        for r in self.db.execute(
            "SELECT * FROM contacts WHERE run_id = ? AND case_id = ?", (run_id, case_id)
        ):
            rows.append({"kind": "contact", "at": r["at"], **dict(r)})
        for r in self.db.execute(
            "SELECT * FROM case_outcomes WHERE run_id = ? AND case_id = ?", (run_id, case_id)
        ):
            rows.append({"kind": "closed", "at": r["closed_at"], **dict(r)})
        # Ties are broken by causal order, not alphabetically: the decision to retry has
        # to read above the charge it authorised, or the trail argues the wrong way round.
        rank = {"decision": 0, "charge": 1, "contact": 2, "closed": 3}
        return sorted(rows, key=lambda r: (str(r["at"]), rank.get(str(r["kind"]), 9)))

    def export_jsonl(self, out_dir: Path | str, run_id: str | None = None) -> dict[str, int]:
        """Write the ledger out as JSONL so an audited run can be committed.

        The `.db` file is a working artifact and is gitignored; these files are the
        evidence behind any number quoted in the README.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written: dict[str, int] = {}
        for table in _TABLES:
            sql = f"SELECT * FROM {table}"  # noqa: S608 - table names are a module constant
            args: tuple = ()
            if run_id:  # every table carries run_id, including `runs` as its primary key
                sql += " WHERE run_id = ?"
                args = (run_id,)
            rows = list(self.db.execute(sql, args))
            with (out / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(dict(r), separators=(",", ":")) + "\n")
            written[table] = len(rows)
        return written


def open_ledger(batch: str, root: Path | str = "data") -> Ledger:
    """Open (creating if needed) the ledger for a batch."""
    return Ledger(Path(root) / batch.upper() / "ledger.db")

"""What the agent is allowed to read.

The agent's entire view of the world is three files - cases, customers, mandates. Ground
truth for the same batch sits in a fourth file in the same directory, `truth.jsonl`, and
this module refuses to open it.

That refusal is belt-and-braces on top of the import boundary enforced by
`tests/test_seal.py`. It costs nothing, it fails loudly rather than quietly, and it makes
the intent legible to someone reading `core/` for the first time: this is the seam, and
it is guarded on both sides.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

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
)

#: Files in a batch directory the agent may open.
READABLE = frozenset({"cases.jsonl", "customers.jsonl", "mandates.jsonl", "meta.json"})

#: Ground truth. Reading this from `core` would make every reported number circular.
SEALED = frozenset({"truth.jsonl"})


class SealViolation(PermissionError):
    """Raised when `core` tries to open ground truth."""


def _lines(path: Path) -> Iterator[dict]:
    if path.name in SEALED:
        raise SealViolation(
            f"{path.name} is ground truth and is not readable from reclaim.core - "
            "the evaluation would be circular. reclaim.eval reads it instead."
        )
    if path.name not in READABLE:
        raise SealViolation(f"{path.name} is not on the agent's allow-list {sorted(READABLE)}")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _error(raw: dict | None) -> ObservedError | None:
    if raw is None:
        return None
    return ObservedError(
        code=raw["code"],
        source=ErrorSource(raw["source"]),
        step=ErrorStep(raw["step"]),
        reason=raw["reason"],
        description=raw["description"],
        bank_reference=raw.get("bank_reference"),
    )


def _attempt(raw: dict) -> PaymentAttempt:
    return PaymentAttempt(
        id=raw["id"],
        case_id=raw["case_id"],
        customer_id=raw["customer_id"],
        amount_paise=raw["amount_paise"],
        rail=Rail(raw["rail"]),
        issuer=raw["issuer"],
        psp=raw["psp"],
        created_at=_dt(raw["created_at"]),  # type: ignore[arg-type]
        status=PaymentStatus(raw["status"]),
        error=_error(raw.get("error")),
        attempt_no=raw["attempt_no"],
        mandate_id=raw.get("mandate_id"),
    )


def load_cases(batch_dir: Path) -> list[Case]:
    out: list[Case] = []
    for raw in _lines(batch_dir / "cases.jsonl"):
        out.append(
            Case(
                id=raw["id"],
                customer_id=raw["customer_id"],
                amount_paise=raw["amount_paise"],
                opened_at=_dt(raw["opened_at"]),  # type: ignore[arg-type]
                kind=raw["kind"],
                rail=Rail(raw["rail"]),
                issuer=raw["issuer"],
                psp=raw["psp"],
                mandate_id=raw.get("mandate_id"),
                status=raw.get("status", "open"),
                attempts=[_attempt(a) for a in raw.get("attempts", [])],
            )
        )
    return out


def load_customers(batch_dir: Path) -> dict[str, Customer]:
    out: dict[str, Customer] = {}
    for raw in _lines(batch_dir / "customers.jsonl"):
        out[raw["id"]] = Customer(
            id=raw["id"],
            salary_day=raw.get("salary_day"),
            preferred_rail=Rail(raw["preferred_rail"]),
            contactable_channels=list(raw.get("contactable_channels", [])),
            opted_out=bool(raw.get("opted_out", False)),
        )
    return out


def load_mandates(batch_dir: Path) -> dict[str, Mandate]:
    out: dict[str, Mandate] = {}
    for raw in _lines(batch_dir / "mandates.jsonl"):
        out[raw["id"]] = Mandate(
            id=raw["id"],
            customer_id=raw["customer_id"],
            rail=Rail(raw["rail"]),
            max_amount_paise=raw["max_amount_paise"],
            status=raw.get("status", "active"),
            consecutive_failures=raw.get("consecutive_failures", 0),
            created_at=_dt(raw.get("created_at")),
        )
    return out


class Batch:
    """One batch of at-risk cases, as the agent sees it."""

    __slots__ = ("name", "dir", "cases", "customers", "mandates", "meta")

    def __init__(self, name: str, batch_dir: Path) -> None:
        self.name = name
        self.dir = batch_dir
        self.cases = load_cases(batch_dir)
        self.customers = load_customers(batch_dir)
        self.mandates = load_mandates(batch_dir)
        meta_path = batch_dir / "meta.json"
        self.meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    def __len__(self) -> int:
        return len(self.cases)

    @property
    def at_risk_paise(self) -> int:
        return sum(c.amount_paise for c in self.cases)

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.id == case_id:
                return c
        raise KeyError(case_id)


def load_batch(name: str, root: Path | str = "data") -> Batch:
    """Load batch `name` (e.g. "A", "B") from `root`."""
    return Batch(name.upper(), Path(root) / name.upper())

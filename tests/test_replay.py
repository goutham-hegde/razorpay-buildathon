"""The policy executor, end to end against a real (small) world.

`tests/test_policy.py` checks what the policy *decides*. This file checks what happens when
those decisions are executed: that the loop terminates, that every case reaches a terminal
state, that costs the world does not model still land on the ledger, and that the arms
cannot quietly become each other.

A generated batch is used rather than a hand-built one. Hand-built fixtures drift away from
what the generator actually emits, and a driver that works on a fixture and not on a batch
is a driver that works on nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reclaim.core import feed
from reclaim.core.compliance import INCENTIVE_COST_PAISE
from reclaim.core.detect import detect
from reclaim.core.diagnose import Diagnosis, StubDiagnoser, cache_path
from reclaim.core.guards import check_run
from reclaim.core.ledger import Ledger
from reclaim.domain import RootCause, to_json
from reclaim.eval.metrics import batch_metrics
from reclaim.eval.replay import ARM_PROVIDER, load_arm_diagnoses, replay
from reclaim.synth.generator import generate

BATCH = "T"
N = 120
SEED = 4242


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """A small generated batch, written the way `synth.generator` writes one."""
    out = tmp_path_factory.mktemp("data") / BATCH
    out.mkdir(parents=True)
    customers, mandates, cases, truths = generate(N, SEED, BATCH)
    for name, rows in (
        ("customers.jsonl", customers),
        ("mandates.jsonl", mandates),
        ("cases.jsonl", cases),
    ):
        (out / name).write_text(
            "".join(to_json(r) + "\n" for r in rows), encoding="utf-8"
        )
    (out / "truth.jsonl").write_text(
        "".join(to_json(t) + "\n" for t in truths.values()), encoding="utf-8"
    )
    (out / "meta.json").write_text(
        json.dumps({"batch": BATCH, "seed": SEED, "n": N}), encoding="utf-8"
    )
    return out.parent


@pytest.fixture()
def ledger() -> Ledger:
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def run(root: Path, ledger: Ledger, *arms: str) -> dict[str, str]:
    return replay(BATCH, list(arms), root, ledger=ledger)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_the_rules_arm_drains_the_whole_batch(root: Path, ledger: Ledger) -> None:
    """R6, from the executor's side.

    `play_policy` raises rather than force-closing if the policy stops making progress, so
    a completed run is itself the assertion that the loop terminated for all 120 cases.
    """
    run_ids = run(root, ledger, "rules")
    case_ids = {c.id for c in feed.load_batch(BATCH, root).cases}
    report = check_run(ledger, run_ids["rules"], case_ids)
    r6 = next(r for r in report.results if r.id == "R6")
    assert r6.held, [v.detail for v in r6.violations]


def test_every_arm_closes_every_case(root: Path, ledger: Ledger) -> None:
    run_ids = run(root, ledger, "control", "naive", "rules")
    n = len(feed.load_batch(BATCH, root).cases)
    for run_id in run_ids.values():
        assert len(ledger.closed_case_ids(run_id)) == n


def test_the_policy_arm_holds_the_compliance_invariants(root: Path, ledger: Ledger) -> None:
    """R3, R4, R5, R6 - the ones the policy is responsible for rather than the diagnoser.

    R1 is deliberately excluded. The keyword diagnoser cannot see `ambiguous_debited` at
    all, so this arm double-charges the cases whose failure carried no bank reference for
    the gate to catch. That is the measured finding, not a defect, and asserting it here
    would mean either deleting the finding or weakening the check.
    """
    run_ids = run(root, ledger, "rules")
    case_ids = {c.id for c in feed.load_batch(BATCH, root).cases}
    report = check_run(ledger, run_ids["rules"], case_ids)
    for rid in ("R2", "R3", "R4", "R5", "R6"):
        result = next(r for r in report.results if r.id == rid)
        assert result.held, f"{rid}: {[v.detail for v in result.violations][:3]}"


# ---------------------------------------------------------------------------
# The arms cannot become each other
# ---------------------------------------------------------------------------


def test_the_agent_arm_refuses_an_incomplete_cache(root: Path) -> None:
    """The single most misleading thing this harness could do is run the agent arm on stub
    diagnoses and report the result as the agent.

    So an incomplete model cache is an error with instructions, never a silent fallback.
    """
    batch = feed.load_batch(BATCH, root)
    path = cache_path(batch.dir, ARM_PROVIDER["agent"])
    path.write_text(
        Diagnosis(
            batch.cases[0].id, RootCause.INSUFFICIENT_FUNDS, 0.9, "", "groq", "m"
        ).to_json()
        + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(FileNotFoundError) as exc:
            load_arm_diagnoses("agent", batch, root)
        assert "will not silently substitute" in str(exc.value)
        assert "python -m reclaim.core.diagnose" in str(exc.value)
    finally:
        path.unlink()


def test_the_agent_arm_refuses_a_missing_cache_entirely(root: Path) -> None:
    batch = feed.load_batch(BATCH, root)
    assert not cache_path(batch.dir, ARM_PROVIDER["agent"]).exists()
    with pytest.raises(FileNotFoundError):
        load_arm_diagnoses("agent", batch, root)


def test_the_rules_arm_computes_its_own_diagnoses_without_a_cache(root: Path) -> None:
    """Allowed precisely because the keyword matcher is deterministic and offline, so a
    computed diagnosis cannot differ from a committed one."""
    batch = feed.load_batch(BATCH, root)
    diagnoses = load_arm_diagnoses("rules", batch, root)
    assert len(diagnoses) == len(batch.cases)
    stub = StubDiagnoser()
    for case in batch.cases:
        assert diagnoses[case.id].root_cause is stub.diagnose(case).root_cause


# ---------------------------------------------------------------------------
# Money, and the costs the world does not model
# ---------------------------------------------------------------------------


def test_an_incentive_is_billed_even_though_the_world_charges_nothing_for_it(
    root: Path, ledger: Ledger
) -> None:
    """`World.send_contact` raises engagement for an incentive and charges nothing.

    Left alone that makes incentives free, and a free lever is not a decision - every arm
    would attach one to every message. The driver bills the agent's own declared figure.
    """
    run_ids = run(root, ledger, "rules")
    run_id = run_ids["rules"]
    incentivised = {
        r["case_id"]
        for r in ledger.contacts(run_id)
        if r["with_incentive"]
    }
    if not incentivised:
        pytest.skip("no incentive was warranted in this batch")

    outcomes = {r["case_id"]: r for r in ledger.outcomes(run_id)}
    plain = {
        r["case_id"] for r in ledger.contacts(run_id) if not r["with_incentive"]
    } - incentivised
    assert plain, "need a case with outreach but no incentive to compare against"

    for case_id in incentivised:
        assert outcomes[case_id]["cost_paise"] >= INCENTIVE_COST_PAISE


def test_the_control_arm_spends_nothing_and_still_recovers_something(
    root: Path, ledger: Ledger
) -> None:
    """The arm the entire comparison rests on. If it ever spends, lift stops meaning lift."""
    run_ids = run(root, ledger, "control")
    run_id = run_ids["control"]
    assert ledger.charges(run_id) == []
    assert ledger.contacts(run_id) == []
    rows = ledger.outcomes(run_id)
    assert sum(r["cost_paise"] for r in rows) == 0
    assert sum(r["recovered_paise"] for r in rows) > 0, (
        "a control arm that recovers nothing means organic recovery is not being modelled, "
        "and every lift figure would silently equal gross recovery"
    )


def test_recovered_money_is_always_attributable(root: Path, ledger: Ledger) -> None:
    """R2's second half. Every rupee is either organic or the result of a named charge."""
    run_ids = run(root, ledger, "control", "naive", "rules")
    for run_id in run_ids.values():
        for row in ledger.outcomes(run_id):
            if row["recovered_paise"]:
                assert row["recovered_by"] in ("organic", "charge")
                assert row["recovered_paise"] <= row["at_risk_paise"]


def test_a_held_case_is_never_charged(root: Path, ledger: Ledger) -> None:
    """Whatever else happens, a case the policy put on reconcile hold saw no presentation
    after that decision."""
    run_ids = run(root, ledger, "rules")
    run_id = run_ids["rules"]
    for decision in ledger.decisions(run_id):
        if decision["action"] not in ("hold", "escalate"):
            continue
        later = [
            c
            for c in ledger.charges(run_id, decision["case_id"])
            if c["at"] >= decision["at"]
        ]
        assert not later, f"{decision['case_id']} was charged after being held"


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def test_lift_is_measured_against_control_not_against_zero(
    root: Path, ledger: Ledger
) -> None:
    run(root, ledger, "control", "naive", "rules")
    metrics = {m.arm: m for m in batch_metrics(ledger, BATCH)}
    control = metrics["control"]
    assert control.lift_net_paise == 0
    for arm in ("naive", "rules"):
        assert (
            metrics[arm].lift_net_paise
            == metrics[arm].net_paise - control.net_paise
        )
        assert metrics[arm].gross_paise > metrics[arm].lift_gross_paise, (
            "gross recovery must exceed lift, or the control arm recovered nothing and "
            "the two figures are the same claim wearing different names"
        )


def test_the_policy_arm_makes_fewer_double_charges_than_naive(
    root: Path, ledger: Ledger
) -> None:
    """Not a performance claim - a safety one, and the reason the ambiguity gate exists."""
    run(root, ledger, "control", "naive", "rules")
    metrics = {m.arm: m for m in batch_metrics(ledger, BATCH)}
    assert metrics["rules"].double_charges < metrics["naive"].double_charges


def test_detection_is_identical_across_arms(root: Path, ledger: Ledger) -> None:
    """Arms must differ in what they *do*, never in which cases they were handed."""
    run_ids = run(root, ledger, "control", "naive", "rules")
    eligible = {r["cases_eligible"] for r in ledger.runs(BATCH)}
    assert len(eligible) == 1
    batch = feed.load_batch(BATCH, root)
    expected = sum(d.eligible for d in detect(batch.cases, batch.customers, batch.mandates))
    assert eligible.pop() == expected
    assert len(run_ids) == 3

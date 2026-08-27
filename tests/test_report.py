"""The README's results table is generated, and this is what keeps it that way.

The point of `eval.report` is that no figure in the README is typed in by hand. These tests
are therefore about the splice being safe and the refusals being real, not about the
arithmetic - that belongs to `eval.metrics` and is tested there.
"""

from __future__ import annotations

import pytest

from reclaim.eval.metrics import ArmMetrics
from reclaim.eval.report import (
    CLOSE_MARKER,
    OPEN_MARKER,
    markdown_table,
    missing_arms,
    render_block,
    splice,
)


def _arm(name: str, **over) -> ArmMetrics:
    base = dict(
        run_id=f"B-{name}",
        batch="B",
        arm=name,
        cases=600,
        eligible=600,
        recovered=300,
        recovery_rate=0.5,
        recovered_organic=100,
        recovered_by_charge=200,
        at_risk_paise=100_000_00,
        gross_paise=50_000_00,
        cost_paise=1_000_00,
        residual_loss_paise=0,
        net_paise=49_000_00,
        charge_attempts=400,
        contacts=200,
        double_charges=0,
        mandates_halted=0,
        mandate_halt_rate=0.0,
    )
    base.update(over)
    return ArmMetrics(**base)


def test_the_table_orders_arms_control_first() -> None:
    """Control leads because every other row is read as a difference from it."""
    table = markdown_table([_arm("agent"), _arm("control"), _arm("naive"), _arm("rules")])
    rows = [r for r in table.splitlines() if r.startswith("| **")]
    assert [r.split("**")[1] for r in rows] == ["control", "naive", "rules", "agent"]


def test_the_control_row_shows_no_lift_against_itself() -> None:
    table = markdown_table([_arm("control"), _arm("naive", lift_net_paise=10_000_00)])
    control_row = next(r for r in table.splitlines() if "**control**" in r)
    naive_row = next(r for r in table.splitlines() if "**naive**" in r)
    assert "—" in control_row
    assert "+10,000" in naive_row


def test_a_negative_lift_keeps_its_sign() -> None:
    """The naive arm's whole story is a large negative number; it must not be formatted off."""
    table = markdown_table([_arm("naive", lift_net_paise=-6_012_174_00)])
    assert "-6,012,174" in table


def test_splicing_twice_is_idempotent() -> None:
    """The second write has to replace the first block, not nest inside it."""
    readme = f"# r\n\n## Results\n\n{OPEN_MARKER}\n\n---\n\n## Next\n"
    once = splice(readme, render_block([_arm("control")], "B"))
    twice = splice(once, render_block([_arm("control")], "B"))
    assert once == twice
    assert once.count(OPEN_MARKER) == 1
    assert once.count(CLOSE_MARKER) == 1


def test_splicing_leaves_the_rest_of_the_file_alone() -> None:
    readme = f"# r\n\nbefore\n\n{OPEN_MARKER}\n{CLOSE_MARKER}\n\nafter\n"
    out = splice(readme, render_block([_arm("control")], "B"))
    assert out.startswith("# r\n\nbefore\n\n")
    assert out.endswith("\n\nafter\n")


def test_a_readme_without_the_marker_is_refused() -> None:
    """Guessing where a table belongs is how a generator mangles someone's file."""
    with pytest.raises(ValueError, match="RESULTS-TABLE"):
        splice("# r\n\nno marker here\n", render_block([_arm("control")], "B"))


def test_a_partial_run_set_is_named() -> None:
    assert missing_arms([_arm("control"), _arm("naive")]) == ["rules", "agent"]
    assert missing_arms([_arm(a) for a in ("control", "naive", "rules", "agent")]) == []

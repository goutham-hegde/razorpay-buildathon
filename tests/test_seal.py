"""The most important test in the repo.

`reclaim.core` is the agent; `reclaim.synth` is the world that decides whether its actions
worked. If the agent could read the world's parameters, every recovery number this project
reports would be circular - the policy would be rediscovering constants we wrote down
ourselves.

The seal is enforced three ways, because one of them will eventually be worked around by
accident:

  1. No module under `reclaim/core/` may import `reclaim.synth` (static, by AST).
  2. Ground truth lives in a physically separate file from what the agent consumes.
  3. Importing every `core` module with `reclaim.synth` poisoned raises nothing (runtime).
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "reclaim" / "core"
FORBIDDEN = "reclaim.synth"


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE.rglob("*.py") if p.name != "__init__.py")


def test_core_has_no_static_import_of_synth() -> None:
    offenders: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN):
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith(FORBIDDEN):
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
    assert not offenders, (
        "reclaim.core must never import reclaim.synth - the evaluation would be "
        "circular. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_core_never_references_synth_by_string() -> None:
    """Catches the obvious workaround: importlib.import_module('reclaim.synth...')."""
    offenders = [
        f"{p.name}: {FORBIDDEN!r} appears in source"
        for p in _core_modules()
        if FORBIDDEN in p.read_text(encoding="utf-8")
    ]
    assert not offenders, "\n  ".join(offenders)


def test_core_imports_cleanly_with_synth_poisoned() -> None:
    """Runtime proof: every core module loads with `reclaim.synth` made unimportable."""
    modules = [
        f"reclaim.core.{m.name}"
        for m in pkgutil.iter_modules([str(CORE)])
        if not m.ispkg
    ]
    if not modules:
        pytest.skip("no core modules yet")

    class Poison:
        def find_module(self, fullname, path=None):  # noqa: D102 - legacy hook shape
            if fullname.startswith(FORBIDDEN):
                raise AssertionError(f"core tried to import {fullname}")
            return None

    for name in modules:
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, Poison())
    try:
        for name in modules:
            importlib.import_module(name)
    finally:
        sys.meta_path.pop(0)


def test_truth_is_a_separate_file_from_cases() -> None:
    """Ground truth must not be reachable from what the agent reads."""
    for batch in ("A", "B"):
        d = ROOT / "data" / batch
        if not (d / "cases.jsonl").exists():
            pytest.skip(f"batch {batch} not generated")
        cases = (d / "cases.jsonl").read_text(encoding="utf-8")
        assert (d / "truth.jsonl").exists(), "truth.jsonl must exist alongside cases"
        for leak in ("root_cause", "persona", "organic_recovery_at", "outage_ends_at"):
            assert leak not in cases, f"{leak} leaked into cases.jsonl for batch {batch}"

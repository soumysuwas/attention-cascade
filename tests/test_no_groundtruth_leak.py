"""Build-stopping test: the pipeline must never see ground truth.

If this fails, the recall numbers are worthless and the whole experiment is unfalsifiable.
Show this test to a judge who asks how you know you didn't cheat.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "attention_cascade"

# Only these may know about incidents. Everything else in the pipeline is blind.
ALLOWED = {"groundtruth.py", "report.py", "generator.py", "blackboard.py", "__init__.py"}

FORBIDDEN_NAMES = {"incident_id", "ground_truth", "is_near_miss", "INCIDENTS", "NEAR_MISSES"}


def _modules() -> list[Path]:
    return [p for p in SRC.glob("*.py") if p.name not in ALLOWED]


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_does_not_import_ground_truth(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "groundtruth" in node.module:
            pytest.fail(f"{path.name} imports groundtruth")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "groundtruth" not in alias.name, f"{path.name} imports groundtruth"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_does_not_reference_incident_fields(path: Path) -> None:
    text = path.read_text()
    for name in FORBIDDEN_NAMES:
        assert name not in text, (
            f"{path.name} references '{name}'. Ground truth must not reach the pipeline."
        )


def test_events_table_has_no_incident_column(tmp_path: Path) -> None:
    from attention_cascade.generator import generate

    db = tmp_path / "events.db"
    generate(db_path=db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(events)")}
    assert "incident_id" not in cols
    assert cols == {"id", "ts", "stream", "entity_id", "kind", "numeric", "payload"}


# --------------------------------------------------------------------------------------
# Metering quarantine: the same idea as the ground-truth quarantine, applied to spend.
# --------------------------------------------------------------------------------------

def test_only_llm_py_imports_the_vendor_sdk() -> None:
    """A model call made outside llm.py is not metered, and an unmetered call makes the cost
    table wrong. This is the machine-checkable version of that rule."""
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name == "llm.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "genai" in node.module:
                offenders.append(f"{path.name}: from {node.module}")
            if isinstance(node, ast.Import):
                offenders.extend(f"{path.name}: import {a.name}"
                                 for a in node.names if "genai" in a.name)
    assert not offenders, f"only llm.py may import the Vertex SDK; found {offenders}"


def test_no_module_constructs_its_own_client() -> None:
    for path in SRC.glob("*.py"):
        if path.name == "llm.py":
            continue
        assert "genai.Client" not in path.read_text(), f"{path.name} builds its own client"

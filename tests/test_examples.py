"""Tests for the example protocols and simulation backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from stepbot.checker import check_protocol
from stepbot.compiler import compile_file

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = [
    ("dilution_series.yaml", "dilution_series.py"),
    ("plate_fill.yaml", "plate_fill.py"),
    ("reservoir_mix.yaml", "reservoir_mix.py"),
]


@pytest.mark.parametrize("yaml_name,py_name", EXAMPLES)
def test_example_compiles_deterministically_and_matches_committed_output(
    yaml_name: str, py_name: str
) -> None:
    yaml_path = ROOT / "examples" / yaml_name
    committed_path = ROOT / "examples" / "out" / py_name
    compiled = compile_file(yaml_path)
    assert compiled == compile_file(yaml_path)
    assert compiled == committed_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("yaml_name,py_name", EXAMPLES)
def test_example_simulates_with_mock(tmp_path: Path, yaml_name: str, py_name: str) -> None:
    yaml_path = ROOT / "examples" / yaml_name
    out_path = tmp_path / py_name
    compile_file(yaml_path, out_path)
    result = check_protocol(out_path, use_mock=True)
    assert result.ok, result.errors
    assert result.backend == "mock"
    assert result.command_count > 0
    assert result.line_count == len(result.lines)


@pytest.mark.parametrize("yaml_name,py_name", EXAMPLES)
def test_example_simulates_with_opentrons(tmp_path: Path, yaml_name: str, py_name: str) -> None:
    pytest.importorskip("opentrons.simulate")
    yaml_path = ROOT / "examples" / yaml_name
    out_path = tmp_path / py_name
    compile_file(yaml_path, out_path)
    result = check_protocol(out_path, use_mock=False)
    assert result.ok, result.errors
    assert result.backend == "opentrons"
    assert result.command_count > 0
    assert result.line_count > 0

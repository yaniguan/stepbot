"""CLI tests for stepbot compile and check."""

from __future__ import annotations

from pathlib import Path

from stepbot.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_cli_compile_and_check_mock(tmp_path: Path, capsys) -> None:
    yaml_path = ROOT / "examples" / "dilution_series.yaml"
    out_path = tmp_path / "out.py"

    rc = main(["compile", str(yaml_path), "-o", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    captured = capsys.readouterr()
    assert f"Wrote {out_path}" in captured.out

    rc = main(["check", str(out_path), "--mock", "-v"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK (mock):" in captured.out


def test_cli_compile_stdout(capsys) -> None:
    yaml_path = ROOT / "examples" / "plate_fill.yaml"
    rc = main(["compile", str(yaml_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "def run(protocol):" in captured.out


def test_cli_compile_validation_error_returns_nonzero(tmp_path: Path, capsys) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "deck:\n"
        "  plate:\n"
        "    labware: corning_96_wellplate_360ul_flat\n"
        "    slot: 99\n",
        encoding="utf-8",
    )
    rc = main(["compile", str(bad_yaml)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Validation error:" in captured.err


def test_cli_check_missing_file_returns_nonzero(tmp_path: Path, capsys) -> None:
    rc = main(["check", str(tmp_path / "missing.py"), "--mock"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Error" in captured.err

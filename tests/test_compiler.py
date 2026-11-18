"""Unit tests for compiler features, JSON loading, and mock simulation."""

from __future__ import annotations

import json
from pathlib import Path

from stepbot.checker import check_protocol
from stepbot.compiler import compile_file, compile_protocol


def test_json_protocol_compiles_and_simulates_with_mock(tmp_path: Path) -> None:
    protocol = {
        "metadata": {"name": "JSON water transfer"},
        "deck": {
            "plate": {"labware": "nest_96_wellplate_200ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {
            "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
        },
        "steps": [
            {"comment": "Transfer water into column 1"},
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-H1",
                    "volume": 80,
                    "new_tip": "once",
                }
            },
            {"delay": 2},
        ],
    }
    json_path = tmp_path / "water.json"
    out_path = tmp_path / "water.py"
    json_path.write_text(json.dumps(protocol), encoding="utf-8")

    compile_file(json_path, out_path)
    result = check_protocol(out_path, use_mock=True)
    assert result.ok, result.errors
    assert result.command_count > 0


def test_new_tip_never_reuses_tip_and_drops_cleanly(tmp_path: Path) -> None:
    protocol = {
        "metadata": {"name": "Reuse tip test"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 2},
        },
        "pipettes": {
            "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
        },
        "steps": [
            {
                "transfer": {
                    "src": "plate:A1",
                    "dst": "plate:A2",
                    "volume": 50,
                    "new_tip": "never",
                }
            },
            {
                "mix": {
                    "well": "plate:A2",
                    "volume": 40,
                    "reps": 2,
                    "new_tip": "never",
                }
            },
        ],
    }
    source = compile_protocol(protocol)
    assert source.count("pick_up_tip()") == 1
    assert source.count("drop_tip()") == 1

    out_path = tmp_path / "reuse.py"
    out_path.write_text(source, encoding="utf-8")
    result = check_protocol(out_path, use_mock=True)
    assert result.ok, result.errors


def test_dilution_series_column_direction_expands_to_transfers() -> None:
    protocol = {
        "metadata": {"name": "Column dilution"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {
            "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
        },
        "steps": [
            {
                "dilution_series": {
                    "source": "reservoir:A1",
                    "diluent": "reservoir:A2",
                    "first_well": "plate:A1",
                    "n": 4,
                    "factor": 2,
                    "final_volume": 100,
                    "direction": "col",
                }
            }
        ],
    }
    source = compile_protocol(protocol)
    assert 'plate["A1"], plate["B1"]' in source
    assert 'plate["B1"], plate["C1"]' in source
    assert 'plate["C1"], plate["D1"]' in source
    assert "protocol.fixed_trash" in source


def test_mock_checker_detects_runtime_protocol_error(tmp_path: Path) -> None:
    bad_proto = tmp_path / "bad.py"
    bad_proto.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 1)\n'
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 2)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    pip.aspirate(50, plate["A1"])\n',
        encoding="utf-8",
    )
    result = check_protocol(bad_proto, use_mock=True)
    assert not result.ok
    assert result.errors

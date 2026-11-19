"""Unit tests for compiler features, JSON loading, and mock simulation."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from stepbot.checker import api_level_error, check_protocol
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
        "liquids": {"water": {"wells": "reservoir:A1", "volume": 1000}},
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
        "liquids": {"sample": {"wells": "plate:A1", "volume": 100}},
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
        "liquids": {
            "stock": {"wells": "reservoir:A1", "volume": 1000},
            "diluent": {"wells": "reservoir:A2", "volume": 1000},
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


_MOCK_HEADER = (
    'metadata = {"apiLevel": "2.16"}\n'
    "def run(protocol):\n"
    '    plate = protocol.load_labware("nest_96_wellplate_100ul_pcr_full_skirt", 1)\n'
    '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 2)\n'
    '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
    '    water = protocol.define_liquid("water", description="", display_color=None)\n'
)


def _run_mock(tmp_path: Path, body: str):
    proto = tmp_path / "proto.py"
    proto.write_text(_MOCK_HEADER + body, encoding="utf-8")
    return check_protocol(proto, use_mock=True)


def test_mock_checker_rejects_aspirate_from_empty_well(tmp_path: Path) -> None:
    result = _run_mock(
        tmp_path,
        "    pip.pick_up_tip()\n"
        '    pip.aspirate(50, plate["A1"])\n',
    )
    assert not result.ok
    assert "well holds only 0.00 uL" in result.errors[0]


def test_mock_checker_rejects_well_overfill(tmp_path: Path) -> None:
    result = _run_mock(
        tmp_path,
        '    plate["A1"].load_liquid(liquid=water, volume=100)\n'
        '    plate["A2"].load_liquid(liquid=water, volume=60)\n'
        '    pip.transfer(50, plate["A1"], plate["A2"], new_tip="once")\n',
    )
    assert not result.ok
    assert "above well capacity 100.0 uL" in result.errors[0]


def test_mock_checker_rejects_mix_larger_than_well_contents(tmp_path: Path) -> None:
    result = _run_mock(
        tmp_path,
        '    plate["A1"].load_liquid(liquid=water, volume=30)\n'
        "    pip.pick_up_tip()\n"
        '    pip.mix(2, 50, plate["A1"])\n',
    )
    assert not result.ok
    assert "Cannot aspirate 50" in result.errors[0]


def test_mock_checker_tracks_declared_volumes(tmp_path: Path) -> None:
    result = _run_mock(
        tmp_path,
        '    plate["A1"].load_liquid(liquid=water, volume=100)\n'
        '    pip.transfer(40, plate["A1"], [plate["A2"], plate["A3"]], new_tip="once")\n'
        '    pip.transfer(30, plate["A1"], plate["A4"], new_tip="once")\n',
    )
    assert not result.ok
    assert "Cannot aspirate 30" in result.errors[0]


def test_api_level_error_flags_levels_newer_than_installed() -> None:
    assert api_level_error("2.15", (2, 15)) is None
    assert api_level_error("2.14", (2, 15)) is None
    message = api_level_error("2.16", (2, 15))
    assert message is not None and "newer than the installed opentrons" in message
    assert api_level_error(None, (2, 15)) is not None


def test_check_fails_clearly_when_api_level_too_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_simulate = types.ModuleType("opentrons.simulate")
    fake_opentrons = types.ModuleType("opentrons")
    fake_opentrons.simulate = fake_simulate
    fake_api = types.ModuleType("opentrons.protocol_api")
    fake_api.MAX_SUPPORTED_VERSION = types.SimpleNamespace(major=2, minor=15)
    monkeypatch.setitem(sys.modules, "opentrons", fake_opentrons)
    monkeypatch.setitem(sys.modules, "opentrons.simulate", fake_simulate)
    monkeypatch.setitem(sys.modules, "opentrons.protocol_api", fake_api)

    proto = tmp_path / "proto.py"
    proto.write_text(_MOCK_HEADER, encoding="utf-8")
    result = check_protocol(proto)
    assert result.backend == "opentrons"
    assert not result.ok
    assert "apiLevel '2.16' is newer than the installed opentrons supports (max 2.15)" in (
        result.errors[0]
    )

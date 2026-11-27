"""Regression and verification tests for Phase-2 audit issues #28 through #33."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import types
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from community_extensions import (  # noqa: E402
    LIQUID_CLASS_PROFILES,
    MultiLiquidTransferSpec,
    compile_rheology_aware_transfer,
)
from stepbot import (  # noqa: E402
    ValidationError,
    build_protocol_manifest,
    check_protocol,
    check_protocol_source,
    compile_protocol,
    compile_with_mcp_envelope,
    parse_and_validate,
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ---------------------------------------------------------------------------
# Issue #29: Safe AST pre-gate before opentrons.simulate, private _MockWell
# mutators, non-well aspirate block, and v2.16 rheology surface in mock
# ---------------------------------------------------------------------------


def test_issue_29_mock_blocks_private_well_mutators_and_trash_aspirate() -> None:
    spoofed_mutator = (
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 9)\n'
        '    plate["A1"].remove_volume(-5000)\n'
    )
    res_mut = check_protocol_source(spoofed_mutator, use_mock=True)
    assert not res_mut.ok
    assert "remove_volume" in res_mut.errors[0]

    spoofed_private = (
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 9)\n'
        '    plate["A1"]._remove_volume(10)\n'
    )
    res_priv = check_protocol_source(spoofed_private, use_mock=True)
    assert not res_priv.ok
    assert "private/dunder" in res_priv.errors[0]

    spoofed_trash_aspirate = (
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 1)\n'
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 2)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        "    pip.pick_up_tip()\n"
        "    pip.aspirate(100, protocol.fixed_trash)\n"
        '    pip.dispense(100, plate["A1"])\n'
    )
    res_trash = check_protocol_source(spoofed_trash_aspirate, use_mock=True)
    assert not res_trash.ok
    assert "aspirate target must be a labware well" in res_trash.errors[0]


def test_issue_29_check_with_opentrons_runs_safe_ast_pregate_before_simulate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulate_called = False

    def fake_simulate(_handle: Any, file_name: str = "") -> tuple[list[Any], Any]:
        nonlocal simulate_called
        simulate_called = True
        return ([{"payload": {"text": f"simulated {file_name}"}}], None)

    fake_sim_mod = types.ModuleType("opentrons.simulate")
    fake_sim_mod.simulate = fake_simulate  # type: ignore[attr-defined]
    fake_sim_mod.format_runlog = lambda _log: "1. simulated"  # type: ignore[attr-defined]
    fake_ot = types.ModuleType("opentrons")
    fake_ot.simulate = fake_sim_mod  # type: ignore[attr-defined]
    fake_api = types.ModuleType("opentrons.protocol_api")
    fake_api.MAX_SUPPORTED_VERSION = types.SimpleNamespace(major=2, minor=20)  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "opentrons", fake_ot)
    monkeypatch.setitem(sys.modules, "opentrons.simulate", fake_sim_mod)
    monkeypatch.setitem(sys.modules, "opentrons.protocol_api", fake_api)

    evil_py = tmp_path / "evil_import.py"
    evil_py.write_text(
        'import os\nmetadata = {"apiLevel": "2.16"}\ndef run(protocol):\n    os.system("echo bad")\n',
        encoding="utf-8",
    )
    res = check_protocol(evil_py, use_mock=False)
    assert res.backend == "opentrons"
    assert res.ok is False
    assert simulate_called is False
    assert "Import statements are disallowed" in res.errors[0]


def test_issue_29_community_extensions_rheology_scripts_pass_mock_checker() -> None:
    for lclass in LIQUID_CLASS_PROFILES:
        plan = compile_rheology_aware_transfer(
            MultiLiquidTransferSpec(
                transfer_id=f"rheo_{lclass}",
                liquid_class=lclass,
                src_well="A1",
                dst_wells=("A1", "A2"),
                volume_ul=100.0,
                source_well_volume_ul=500.0,
            )
        )
        res = check_protocol_source(plan.python_source, use_mock=True)
        assert res.ok, f"{lclass} failed mock check: {res.errors}"
        assert res.well_volumes_ul["plate:A1"] == pytest.approx(100.0)
        assert res.well_volumes_ul["plate:A2"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Issue #30: Opentrons Flex (apiLevel 2.18-2.20, A1-D3 slots, requirements,
# load_trash_bin, and Flex 1-/8-channel pipettes)
# ---------------------------------------------------------------------------


def test_issue_30_flex_single_channel_compile_mock_and_mcp_bundle() -> None:
    flex_proto = {
        "metadata": {"name": "Flex Plate Fill", "apiLevel": "2.18"},
        "requirements": {"robotType": "Flex"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": "D1"},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": "D2"},
            "tips": {"labware": "opentrons_flex_96_tiprack_200ul", "slot": "C1"},
        },
        "pipettes": {"left": {"model": "flex_1channel_1000", "tip_racks": ["tips"]}},
        "liquids": {
            "stock": {"wells": "reservoir:A1", "volume": 5000.0},
            "diluent": {"wells": "reservoir:A2", "volume": 5000.0},
        },
        "steps": [
            {
                "dilution_series": {
                    "source": "reservoir:A1",
                    "diluent": "reservoir:A2",
                    "first_well": "plate:A1",
                    "n": 4,
                    "factor": 2,
                    "final_volume": 80.0,
                }
            }
        ],
    }
    py_code = compile_protocol(flex_proto)
    assert 'requirements = {\n    "robotType": \'Flex\',\n    "apiLevel": \'2.18\',\n}' in py_code
    assert '"apiLevel"' not in py_code.split("requirements =")[0]
    assert "trash = protocol.load_trash_bin('A3')" in py_code
    assert "protocol.fixed_trash" not in py_code
    assert "dispense(80, trash)" in py_code

    res = check_protocol_source(py_code, use_mock=True)
    assert res.ok, res.errors
    assert res.robot_type == "Flex"
    assert res.api_level == "2.18"
    assert res.loaded_slots == ("C1", "D1", "D2")

    bundle = compile_with_mcp_envelope(flex_proto, secret_key="flex-sec", nonce="flex-n1")
    assert bundle.envelope["envelope_bounds"] == {
        "deck_slots": ["C1", "D1", "D2"],
        "max_transfer_ul": 160.0,
        "api_level": "2.18",
        "robot_type": "Flex",
    }
    manifest = build_protocol_manifest(parse_and_validate(flex_proto), py_code)
    assert manifest["robot_type"] == "Flex"
    assert manifest["trash_slot"] == "A3"


def test_issue_30_flex_8channel_pipette_column_semantics_and_guards() -> None:
    flex_8ch = {
        "metadata": {"name": "Flex 8-Channel Column Fill"},
        "robot_type": "Flex",
        "requirements": {"apiLevel": "2.20"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": "D1"},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": "D2"},
            "tips": {"labware": "opentrons_flex_96_filtertiprack_50ul", "slot": "B2"},
        },
        "pipettes": {"right": {"model": "flex_8channel_50", "tip_racks": ["tips"]}},
        "liquids": {"reagent": {"wells": "reservoir:A1", "volume": 2000.0}},
        "steps": [
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": ["plate:A1", "plate:A2"],
                    "volume": 25.0,
                    "new_tip": "always",
                }
            },
            {
                "mix": {
                    "wells": "plate:A1",
                    "volume": 20.0,
                    "reps": 2,
                    "new_tip": "once",
                }
            },
        ],
    }
    spec = parse_and_validate(flex_8ch)
    # 2 transfers (always -> 2x8 = 16 tips) + 1 mix (once -> 8 tips) = 24 tips
    assert spec.tips_used_per_rack["tips"] == 24
    assert spec.tips_used_per_mount["right"] == 24
    # 1-row reservoir lost 2 columns * 8 channels * 25 uL = 400 uL -> 1600 uL left
    assert spec.well_summaries["reservoir:A1"].final_ul == 1600.0
    # Every row A..H in columns 1 and 2 received 25 uL
    for r_ch in "ABCDEFGH":
        assert spec.well_summaries[f"plate:{r_ch}1"].final_ul == 25.0
        assert spec.well_summaries[f"plate:{r_ch}2"].final_ul == 25.0

    py_src = compile_protocol(flex_8ch)
    check_res = check_protocol_source(py_src, use_mock=True)
    assert check_res.ok, check_res.errors
    assert check_res.tips_used_per_mount["right"] == 24
    assert check_res.well_volumes_ul["reservoir:A1"] == 1600.0
    assert check_res.well_volumes_ul["plate:H2"] == 25.0

    # Non-row-A target with 8-channel pipette is rejected
    bad_row = {
        **flex_8ch,
        "steps": [{"transfer": {"src": "reservoir:A1", "dst": "plate:B1", "volume": 25.0}}],
    }
    with pytest.raises(ValidationError, match="row 'A' column heads"):
        compile_protocol(bad_row)

    # Trash slot A3 collision with labware is rejected
    trash_collision = {
        **flex_8ch,
        "deck": {
            **flex_8ch["deck"],
            "tips": {"labware": "opentrons_flex_96_filtertiprack_50ul", "slot": "A3"},
        },
    }
    with pytest.raises(ValidationError, match="collides with the Flex trash bin slot 'A3'"):
        compile_protocol(trash_collision)

    # Hand-written Flex script with apiLevel only in requirements passes check_protocol_source
    hand_flex_py = (
        'metadata = {"protocolName": "Flex Standard"}\n'
        'requirements = {"robotType": "Flex", "apiLevel": "2.19"}\n'
        "def run(protocol):\n"
        '    trash = protocol.load_trash_bin("A3")\n'
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D1")\n'
        '    protocol.comment("Flex ready")\n'
    )
    hand_res = check_protocol_source(hand_flex_py, use_mock=True)
    assert hand_res.ok, hand_res.errors
    assert hand_res.api_level == "2.19"
    assert hand_res.robot_type == "Flex"


# ---------------------------------------------------------------------------
# Issue #31: StepbotLean formal proofs & zero-sorry / Python parity check
# ---------------------------------------------------------------------------


def test_issue_31_stepbot_lean_proof_integrity_and_python_parity() -> None:
    lean_file = ROOT / "lean" / "StepbotLean.lean"
    assert lean_file.is_file()
    lean_text = lean_file.read_text(encoding="utf-8")
    assert CJK_RE.search(lean_text) is None
    # Strip comments and verify zero `sorry` / `admit` axioms
    stripped_lines = [
        line.split("--", 1)[0]
        for line in lean_text.splitlines()
        if not line.strip().startswith("--")
    ]
    code_only = "\n".join(stripped_lines)
    assert re.search(r"\bsorry\b", code_only) is None
    assert re.search(r"\badmit\b", code_only) is None
    assert "theorem runProtocolSteps_preserves_rack_sum" in lean_text
    assert "theorem runProtocolSteps_never_underflows_source" in lean_text
    assert "def distributeChunks" in lean_text
    assert not (ROOT / "lean" / ".lake").exists()
    assert not (ROOT / "lean" / "lake-manifest.json").exists()

    # Verify Python compiler matches StepbotLean multi-step [10, 96] -> 16 tips & 320 uL remaining
    multi_step_proto = {
        "metadata": {"name": "Lean Multi-Step Parity", "apiLevel": "2.16"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips_1": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
            "tips_2": {"labware": "opentrons_96_tiprack_300ul", "slot": 4},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips_1", "tips_2"]}},
        "liquids": {"buf": {"wells": "reservoir:A1", "volume": 1500.0}},
        "steps": [
            {"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A7", "volume": 100.0, "new_tip": "once"}},
            {"transfer": {"src": "reservoir:A1", "dst": "plate:B1-B8", "volume": 50.0, "new_tip": "always"}},
            {"mix": {"wells": "plate:B1", "volume": 50.0, "reps": 3, "new_tip": "never"}},
            {"mix": {"wells": "plate:B2", "volume": 50.0, "reps": 3, "new_tip": "never"}},
            {"mix": {"wells": "plate:B3-B8", "volume": 50.0, "reps": 3, "new_tip": "always"}},
        ],
    }
    spec = parse_and_validate(multi_step_proto)
    assert spec.tips_used_per_mount["left"] == 16
    assert spec.well_summaries["reservoir:A1"].final_ul == pytest.approx(320.0)

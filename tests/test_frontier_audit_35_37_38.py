"""Tests for Issues #35, #37, #38 plus 8-channel column tracking and rheology profiles."""

from __future__ import annotations

import copy
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from stepbot import (
    LIQUID_CLASS_CATALOG,
    ValidationError,
    build_protocol_manifest,
    check_protocol_source,
    compile_file,
    compile_protocol,
    parse_and_validate,
    plan_transfer_strokes,
)
from stepbot.cli import main as cli_main
from stepbot.codegen import _fmt_num

ROOT = Path(__file__).resolve().parent.parent


def _base_proto(
    *,
    plate_labware: str = "corning_96_wellplate_360ul_flat",
    tip_labware: str = "opentrons_96_tiprack_300ul",
    pipette_model: str = "p300_single_gen2",
    liquids: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    extra_deck: dict[str, Any] | None = None,
    extra_tip_racks: list[str] | None = None,
) -> dict[str, Any]:
    deck: dict[str, Any] = {
        "plate": {"labware": plate_labware, "slot": 1},
        "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
        "tips": {"labware": tip_labware, "slot": 3},
    }
    if extra_deck:
        deck.update(extra_deck)
    racks = ["tips"] + (extra_tip_racks or [])
    return {
        "metadata": {"name": "Frontier audit test", "apiLevel": "2.16"},
        "deck": deck,
        "pipettes": {
            "left": {"model": pipette_model, "tip_racks": racks},
        },
        "liquids": (
            {
                "buffer": {"wells": "reservoir:A1", "volume": 12000},
                "dye": {"wells": "reservoir:A2", "volume": 12000},
            }
            if liquids is None
            else liquids
        ),
        "steps": (
            [{"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 100}}]
            if steps is None
            else steps
        ),
    }


# ---------------------------------------------------------------------------
# Issue #35: Opentrons stroke splitting and tip use for multi_aspirate transfers
# ---------------------------------------------------------------------------


def test_issue_35_stroke_helper_matches_all_five_table_cases() -> None:
    # 1. 700 uL on p300 (300) + 300 uL tips -> 300 + 200 + 200 (3 tip groups)
    assert plan_transfer_strokes(700.0, 300.0, 300.0) == ((300.0,), (200.0,), (200.0,))
    # 2. 600 uL on p300 (300) + 300 uL tips -> 300 + 300 (2 tip groups)
    assert plan_transfer_strokes(600.0, 300.0, 300.0) == ((300.0,), (300.0,))
    # 3. 210 uL on p300 (300) + 200 uL filter tips -> 200 + 10 (1 tip group, 2 strokes)
    assert plan_transfer_strokes(210.0, 300.0, 200.0) == ((200.0, 10.0),)
    # 4. 405 uL on p300 (300) + 200 uL filter tips -> (200, 2.5) + (200, 2.5)
    assert plan_transfer_strokes(405.0, 300.0, 200.0) == ((200.0, 2.5), (200.0, 2.5))
    # 5. 450 uL on p300 (300) + 300 uL tips -> 225 + 225
    assert plan_transfer_strokes(450.0, 300.0, 300.0) == ((225.0,), (225.0,))


def test_issue_35_compile_and_mock_reject_tip_exhaustion_sub_min_strokes_and_early_mix() -> None:
    # Case 1: 700 uL into 40 deep wells (`deep:col:1-5`), `new_tip: always`, one 96-tip rack
    # Needs 40 * 3 = 120 tips -> fails at compile time with tip-budget error!
    proto_40_one_rack = {
        "metadata": {"name": "40 deep wells 700 uL", "apiLevel": "2.16"},
        "deck": {
            "deep": {"labware": "nest_96_wellplate_2ml_deep", "slot": 1},
            "res": {"labware": "nest_1_reservoir_195ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips"]}},
        "liquids": {"buf": {"wells": "res:A1", "volume": 50000}},
        "steps": [
            {
                "transfer": {
                    "src": "res:A1",
                    "dst": "deep:col:1-5",
                    "volume": 700,
                    "new_tip": "always",
                    "multi_aspirate": True,
                }
            }
        ],
    }
    with pytest.raises(ValidationError, match="Not enough tips.*needed 120 tips.*capacity is 96"):
        compile_protocol(proto_40_one_rack)

    # With two 96-tip racks (192 tips), compiles with 120 tips and mock checks cleanly
    proto_40_two_racks = copy.deepcopy(proto_40_one_rack)
    proto_40_two_racks["deck"]["tips2"] = {
        "labware": "opentrons_96_tiprack_300ul",
        "slot": 4,
    }
    proto_40_two_racks["pipettes"]["left"]["tip_racks"] = ["tips", "tips2"]
    spec_120 = parse_and_validate(proto_40_two_racks)
    assert spec_120.tips_used_per_mount["left"] == 120
    assert spec_120.tips_used_per_rack == {"tips": 96, "tips2": 24}
    mock_120 = check_protocol_source(compile_protocol(proto_40_two_racks), use_mock=True)
    assert mock_120.ok, mock_120.errors
    assert mock_120.tips_used_per_mount["left"] == 120

    # Case 3: p300_single_gen2 (min 20 uL) + 200 uL filter tips, 210 uL -> 200 + 10 uL (< 20 uL)
    proto_210 = _base_proto(
        plate_labware="nest_96_wellplate_2ml_deep",
        tip_labware="opentrons_96_filtertiprack_200ul",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 210,
                    "multi_aspirate": True,
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match=r"\[200, 10\] uL; stroke 10 uL falls below"):
        compile_protocol(proto_210)

    # Case 4: Same pipette + 200 uL filter tips, 405 uL -> 200 + 2.5 + 200 + 2.5 uL (< 20 uL)
    proto_405 = _base_proto(
        plate_labware="nest_96_wellplate_2ml_deep",
        tip_labware="opentrons_96_filtertiprack_200ul",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 405,
                    "multi_aspirate": True,
                }
            }
        ],
    )
    with pytest.raises(
        ValidationError, match=r"\[200, 2\.5, 200, 2\.5\] uL; stroke 2\.5 uL falls below"
    ):
        compile_protocol(proto_405)

    # Case 5: 450 uL into empty well with mix_after: [2, 300] -> first stroke is 225 uL < 300 uL
    proto_450_bad_mix = _base_proto(
        plate_labware="nest_96_wellplate_2ml_deep",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 450,
                    "multi_aspirate": True,
                    "mix_after": [2, 300],
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match="mix volume 300 uL is more than the 225 uL"):
        compile_protocol(proto_450_bad_mix)

    # Valid mix_after [2, 200] mixes after BOTH 225 uL strokes in mock
    proto_450_ok_mix = _base_proto(
        plate_labware="nest_96_wellplate_2ml_deep",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 450,
                    "multi_aspirate": True,
                    "mix_after": [2, 200],
                }
            }
        ],
    )
    mock_450 = check_protocol_source(compile_protocol(proto_450_ok_mix), use_mock=True)
    assert mock_450.ok, mock_450.errors
    assert sum("Mixing 2 times with 200.0 uL" in line for line in mock_450.lines) == 2


def test_issue_35_all_five_cases_match_opentrons_simulate_8_8_2() -> None:
    ot_simulate = pytest.importorskip("opentrons.simulate")

    # 1. 700 uL into 40 deep wells with one 96-tip rack -> OutOfTipsError in opentrons.simulate
    raw_40_one_rack = (
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    deep = protocol.load_labware("nest_96_wellplate_2ml_deep", 1)\n'
        '    res = protocol.load_labware("nest_1_reservoir_195ml", 2)\n'
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 3)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    pip.transfer(700, res["A1"], deep.wells()[:40], new_tip="always")\n'
    )
    with pytest.raises(Exception, match="OutOfTipsError"):
        ot_simulate.simulate(io.StringIO(raw_40_one_rack))

    # With 2 racks, stepbot compiles and both mock & opentrons.simulate use 120 tips
    proto_40_two_racks = {
        "metadata": {"name": "40 deep wells 700 uL (2 racks)", "apiLevel": "2.16"},
        "deck": {
            "deep": {"labware": "nest_96_wellplate_2ml_deep", "slot": 1},
            "res": {"labware": "nest_1_reservoir_195ml", "slot": 2},
            "tips1": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
            "tips2": {"labware": "opentrons_96_tiprack_300ul", "slot": 4},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips1", "tips2"]}},
        "liquids": {"buf": {"wells": "res:A1", "volume": 50000}},
        "steps": [
            {
                "transfer": {
                    "src": "res:A1",
                    "dst": "deep:col:1-5",
                    "volume": 700,
                    "new_tip": "always",
                    "multi_aspirate": True,
                }
            }
        ],
    }
    py_40 = compile_protocol(proto_40_two_racks)
    log_40, _ = ot_simulate.simulate(io.StringIO(py_40))
    txt_40 = ot_simulate.format_runlog(log_40)
    assert txt_40.count("Picking up tip") == 120

    # 2. 600 uL into 2 wells with `always` -> 4 tips in both stepbot and opentrons.simulate
    proto_600 = _base_proto(
        plate_labware="corning_24_wellplate_3.4ml_flat",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A2",
                    "volume": 600,
                    "new_tip": "always",
                    "multi_aspirate": True,
                }
            }
        ],
    )
    spec_600 = parse_and_validate(proto_600)
    assert spec_600.tips_used_per_rack["tips"] == 4
    log_600, _ = ot_simulate.simulate(io.StringIO(compile_protocol(proto_600)))
    assert ot_simulate.format_runlog(log_600).count("Picking up tip") == 4

    # 3 & 4. Verify Opentrons 8.8.2 produces 200+10 and 200+2.5+200+2.5 (which stepbot rejects)
    raw_210_405 = (
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    deep = protocol.load_labware("nest_96_wellplate_2ml_deep", 1)\n'
        '    res = protocol.load_labware("nest_1_reservoir_195ml", 2)\n'
        '    tips = protocol.load_labware("opentrons_96_filtertiprack_200ul", 3)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    pip.transfer(210, res["A1"], deep["A1"], new_tip="once")\n'
        '    pip.transfer(405, res["A1"], deep["A2"], new_tip="always")\n'
    )
    log_sub, _ = ot_simulate.simulate(io.StringIO(raw_210_405))
    txt_sub = ot_simulate.format_runlog(log_sub)
    assert "Aspirating 10.0 uL" in txt_sub
    assert txt_sub.count("Aspirating 2.5 uL") == 2

    # 5. Verify Opentrons 8.8.2 runs mix_after after EVERY 225 uL stroke on 450 uL
    proto_450_ok = _base_proto(
        plate_labware="nest_96_wellplate_2ml_deep",
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 450,
                    "multi_aspirate": True,
                    "mix_after": [2, 200],
                }
            }
        ],
    )
    py_450 = compile_protocol(proto_450_ok)
    log_450, _ = ot_simulate.simulate(io.StringIO(py_450))
    txt_450 = ot_simulate.format_runlog(log_450)
    assert txt_450.count("Mixing 2 times with a volume of 200.0 ul") == 2


# ---------------------------------------------------------------------------
# Issue #37: Destination-well liquid contact & dispense_position: top | bottom
# ---------------------------------------------------------------------------


def test_issue_37_destination_contact_lineage_and_dispense_position_top() -> None:
    three_samples_liquids = {
        "buffer": {"wells": "reservoir:A1", "volume": 5000},
        "sample_1": {"wells": "plate:A1", "volume": 100},
        "sample_2": {"wells": "plate:A2", "volume": 100},
        "sample_3": {"wells": "plate:A3", "volume": 100},
    }

    # 1. Buffer into 3 pre-filled sample wells with one tip (default bottom) -> REJECTED
    contam_transfer = _base_proto(
        liquids=three_samples_liquids,
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A3",
                    "volume": 50,
                    "new_tip": "once",
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match="cross-contamination.*sample_1"):
        compile_protocol(contam_transfer)

    # 2. Same transfer with dispense_position: top -> ACCEPTED, emits .top(), clean mock check
    top_transfer = _base_proto(
        liquids=three_samples_liquids,
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A3",
                    "volume": 50,
                    "new_tip": "once",
                    "dispense_position": "top",
                }
            }
        ],
    )
    spec_top = parse_and_validate(top_transfer)
    assert spec_top.well_summaries["reservoir:A1"].species == ("buffer",)
    assert spec_top.well_summaries["plate:A1"].species == ("buffer", "sample_1")
    assert spec_top.well_summaries["plate:A2"].species == ("buffer", "sample_2")
    py_top = compile_protocol(top_transfer)
    assert '[plate["A1"].top(), plate["A2"].top(), plate["A3"].top()]' in py_top
    res_top = check_protocol_source(py_top, use_mock=True)
    assert res_top.ok, res_top.errors

    # 3. dispense_position: top WITH mix_after still contacts destination liquid -> REJECTED
    top_with_mix = _base_proto(
        liquids=three_samples_liquids,
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A3",
                    "volume": 50,
                    "new_tip": "once",
                    "dispense_position": "top",
                    "mix_after": [2, 50],
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match="cross-contamination.*sample_1"):
        compile_protocol(top_with_mix)

    # 4. allow_contamination: true opts out explicitly -> ACCEPTED
    allowed_transfer = copy.deepcopy(contam_transfer)
    allowed_transfer["steps"][0]["transfer"]["allow_contamination"] = True
    assert "def run(protocol):" in compile_protocol(allowed_transfer)

    # 5. Dispensing into EMPTY wells with one tip at default bottom -> ACCEPTED
    empty_dst_transfer = _base_proto(
        liquids={"buffer": {"wells": "reservoir:A1", "volume": 5000}},
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A8",
                    "volume": 50,
                    "new_tip": "once",
                }
            }
        ],
    )
    assert check_protocol_source(compile_protocol(empty_dst_transfer), use_mock=True).ok

    # 6. distribute into 3 pre-filled sample wells (even 1 chunk!) -> REJECTED at bottom, ACCEPTED at top
    contam_dist = _base_proto(
        liquids=three_samples_liquids,
        steps=[
            {
                "distribute": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A3",
                    "volume": 50,
                    "new_tip": "once",
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match="cross-contamination.*sample_1"):
        compile_protocol(contam_dist)

    top_dist = _base_proto(
        liquids=three_samples_liquids,
        steps=[
            {
                "distribute": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A3",
                    "volume": 50,
                    "new_tip": "once",
                    "dispense_position": "top",
                }
            }
        ],
    )
    py_top_dist = compile_protocol(top_dist)
    assert '[plate["A1"].top(), plate["A2"].top(), plate["A3"].top()]' in py_top_dist
    assert check_protocol_source(py_top_dist, use_mock=True).ok

    # 7. Multi-chunk distribute into uniform pre-filled wells -> chunk 2 contaminates reservoir at bottom
    multi_chunk_dist = _base_proto(
        steps=[
            {"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A6", "volume": 100}},
            {"distribute": {"src": "reservoir:A2", "dst": "plate:A1-A6", "volume": 100}},
        ]
    )
    with pytest.raises(ValidationError, match="cross-contamination.*buffer"):
        compile_protocol(multi_chunk_dist)


# ---------------------------------------------------------------------------
# Issue #38: Reject NaN and infinite numbers across all YAML and JSON fields
# ---------------------------------------------------------------------------

_NON_FINITE_TOKENS = [
    ("yaml", ".nan"),
    ("yaml", ".inf"),
    ("yaml", "-.inf"),
    ("json", "NaN"),
    ("json", "Infinity"),
    ("json", "-Infinity"),
]

_NUMERIC_FIELD_TEMPLATES: list[tuple[str, str]] = [
    (
        "transfer_volume",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": {val}}}}}]',
    ),
    (
        "transfer_multi_aspirate_volume",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": {val}, "multi_aspirate": true}}}}]',
    ),
    (
        "mix_after_volume",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": 50, "mix_after": [2, {val}]}}}}]',
    ),
    (
        "mix_after_reps",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": 50, "mix_after": [{val}, 50]}}}}]',
    ),
    (
        "distribute_volume",
        'steps: [{{"distribute": {{"src": "reservoir:A1", "dst": "plate:A1-A2", "volume": {val}}}}}]',
    ),
    (
        "distribute_disposal_volume",
        'steps: [{{"distribute": {{"src": "reservoir:A1", "dst": "plate:A1-A2", "volume": 50, "disposal_volume": {val}}}}}]',
    ),
    (
        "mix_volume",
        'steps: [{{"mix": {{"wells": "reservoir:A1", "volume": {val}, "reps": 2}}}}]',
    ),
    (
        "mix_reps",
        'steps: [{{"mix": {{"wells": "reservoir:A1", "volume": 50, "reps": {val}}}}}]',
    ),
    (
        "discard_volume",
        'steps: [{{"discard": {{"src": "reservoir:A1", "volume": {val}}}}}]',
    ),
    (
        "delay_seconds",
        'steps: [{{"delay": {{"seconds": {val}}}}}]',
    ),
    (
        "delay_minutes",
        'steps: [{{"delay": {{"minutes": {val}}}}}]',
    ),
    (
        "delay_shorthand",
        'steps: [{{"delay": {val}}}]',
    ),
    (
        "pause_seconds",
        'steps: [{{"pause": {{"seconds": {val}}}}}]',
    ),
    (
        "pause_minutes",
        'steps: [{{"pause": {{"minutes": {val}}}}}]',
    ),
    (
        "dilution_factor",
        'steps: [{{"dilution_series": {{"source": "reservoir:A2", "diluent": "reservoir:A1", "first_well": "plate:A1", "n": 4, "factor": {val}, "final_volume": 100}}}}]',
    ),
    (
        "dilution_final_volume",
        'steps: [{{"dilution_series": {{"source": "reservoir:A2", "diluent": "reservoir:A1", "first_well": "plate:A1", "n": 4, "factor": 2, "final_volume": {val}}}}}]',
    ),
    (
        "liquid_volume",
        'liquids: {{"buffer": {{"wells": "reservoir:A1", "volume": {val}}}}}',
    ),
    (
        "rheology_flow_rate",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": 50, "flow_rate": {val}}}}}]',
    ),
    (
        "rheology_air_gap",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": 50, "air_gap": {val}}}}}]',
    ),
    (
        "rheology_aspirate_delay",
        'steps: [{{"transfer": {{"src": "reservoir:A1", "dst": "plate:A1", "volume": 50, "aspirate_delay": {val}}}}}]',
    ),
]


def _render_non_finite_input(fmt: str, token: str, snippet: str) -> str:
    header_obj: dict[str, Any] = {
        "metadata": {"name": "Non-finite test", "apiLevel": "2.16"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips"]}},
        "liquids": {
            "buffer": {"wells": "reservoir:A1", "volume": 5000},
            "dye": {"wells": "reservoir:A2", "volume": 5000},
        },
        "steps": [{"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 50}}],
    }
    filled = snippet.format(val=token)
    key_name = filled.split(":", 1)[0].strip()
    if fmt == "yaml":
        base_yaml = yaml.safe_dump(
            {k: v for k, v in header_obj.items() if k != key_name},
            sort_keys=False,
        )
        return base_yaml + "\n" + filled + "\n"
    # Build raw JSON text with unquoted NaN / Infinity / -Infinity token
    base_dict = {k: v for k, v in header_obj.items() if k != key_name}
    prefix = json.dumps(base_dict)[:-1]
    json_field = filled.replace("steps:", '"steps":').replace("liquids:", '"liquids":')
    return f"{prefix}, {json_field}}}\n"


@pytest.mark.parametrize("fmt,token", _NON_FINITE_TOKENS)
@pytest.mark.parametrize("field_id,snippet", _NUMERIC_FIELD_TEMPLATES)
def test_issue_38_cli_and_compiler_reject_non_finite_numbers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    token: str,
    field_id: str,
    snippet: str,
) -> None:
    ext = ".yaml" if fmt == "yaml" else ".json"
    in_path = tmp_path / f"bad_{field_id}{ext}"
    out_py = tmp_path / "out.py"
    out_manifest = tmp_path / "manifest.json"
    in_path.write_text(_render_non_finite_input(fmt, token, snippet), encoding="utf-8")

    rc = cli_main(
        ["compile", str(in_path), "-o", str(out_py), "--manifest", str(out_manifest)]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("Validation error:")
    assert "must be a finite number" in captured.err
    assert "Traceback" not in captured.err
    assert not out_py.exists()
    assert not out_manifest.exists()


def test_issue_38_codegen_and_manifest_defense_in_depth_against_nan_and_inf() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError, match="must be a finite number"):
            _fmt_num(bad)

    valid_spec = parse_and_validate(_base_proto())
    # Mutating a frozen ProtocolSpec summary copy with NaN must also be caught by manifest builder
    from dataclasses import replace

    corrupted_summary = replace(valid_spec.well_summaries["plate:A1"], final_ul=math.nan)
    corrupted_spec = replace(
        valid_spec,
        well_summaries={**valid_spec.well_summaries, "plate:A1": corrupted_summary},
    )
    with pytest.raises(ValidationError, match="must be a finite number"):
        build_protocol_manifest(corrupted_spec, "def run(protocol):\n    pass\n")


# ---------------------------------------------------------------------------
# Frontier Upgrades: OT-2 & Flex 8-Channel Column Tracking + Rheology Profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "robot_type,pip_model,tip_lw,plate_slot,res_slot,tip_slot,vol_ul",
    [
        ("OT-2", "p20_multi_gen2", "opentrons_96_tiprack_20ul", 1, 2, 3, 10.0),
        ("OT-2", "p300_multi_gen2", "opentrons_96_tiprack_300ul", 1, 2, 3, 100.0),
        ("Flex", "flex_8channel_50", "opentrons_flex_96_tiprack_50ul", "D1", "D2", "C1", 20.0),
        ("Flex", "flex_8channel_1000", "opentrons_flex_96_tiprack_1000ul", "D1", "D2", "C1", 120.0),
    ],
)
def test_frontier_8channel_column_broadcast_tip_and_lineage_tracking(
    robot_type: str,
    pip_model: str,
    tip_lw: str,
    plate_slot: int | str,
    res_slot: int | str,
    tip_slot: int | str,
    vol_ul: float,
) -> None:
    proto: dict[str, Any] = {
        "metadata": {"name": f"8ch {pip_model}"},
        "robot_type": robot_type,
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": plate_slot},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": res_slot},
            "tips": {"labware": tip_lw, "slot": tip_slot},
        },
        "pipettes": {"left": {"model": pip_model, "tip_racks": ["tips"]}},
        "liquids": {"reagent": {"wells": "reservoir:A1", "volume": 10000.0}},
        "steps": [
            {
                "distribute": {
                    "src": "reservoir:A1",
                    "dst": ["plate:A1", "plate:A2"],
                    "volume": vol_ul,
                    "new_tip": "once",
                }
            },
            {
                "mix": {
                    "wells": "plate:A1",
                    "volume": vol_ul,
                    "reps": 2,
                    "new_tip": "always",
                }
            },
        ],
    }
    spec = parse_and_validate(proto)
    assert spec.tips_used_per_mount["left"] == 16
    for row_ch in "ABCDEFGH":
        assert spec.well_summaries[f"plate:{row_ch}1"].final_ul == pytest.approx(vol_ul)
        assert spec.well_summaries[f"plate:{row_ch}2"].final_ul == pytest.approx(vol_ul)

    py_src = compile_protocol(proto)
    mock_res = check_protocol_source(py_src, use_mock=True)
    assert mock_res.ok, mock_res.errors
    assert mock_res.tips_used_per_mount["left"] == 16
    assert mock_res.well_volumes_ul["plate:H2"] == pytest.approx(vol_ul)

    # Lineage check catches contamination even when only row H of column 1 holds a sample!
    contam_8ch = copy.deepcopy(proto)
    contam_8ch["liquids"]["row_h_sample"] = {"wells": "plate:H1", "volume": 20.0}
    contam_8ch["steps"] = [
        {
            "transfer": {
                "src": "reservoir:A1",
                "dst": ["plate:A1", "plate:A2"],
                "volume": vol_ul,
                "new_tip": "once",
            }
        }
    ]
    with pytest.raises(ValidationError, match="cross-contamination.*row_h_sample"):
        compile_protocol(contam_8ch)


@pytest.mark.parametrize(
    "lclass",
    ["aqueous", "viscous_glycerol", "volatile_ethanol", "serum"],
)
def test_frontier_liquid_class_rheology_profiles_mock_and_opentrons_sim(lclass: str) -> None:
    assert lclass in LIQUID_CLASS_CATALOG
    proto = _base_proto(
        liquids={
            "reagent": {
                "wells": "reservoir:A1",
                "volume": 5000.0,
                "liquid_class": lclass,
            }
        },
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A2",
                    "volume": 60.0,
                    "new_tip": "once",
                    "liquid_class": lclass,
                }
            }
        ],
    )
    py_code = compile_protocol(proto)
    assert "flow_rate.aspirate =" in py_code
    assert "flow_rate.dispense =" in py_code
    if lclass == "viscous_glycerol":
        assert "protocol.delay(seconds=2)" in py_code
        assert "touch_tip(" in py_code
    elif lclass == "volatile_ethanol":
        assert "air_gap(" in py_code
        assert "protocol.delay(seconds=0.2)" in py_code
    elif lclass == "serum":
        assert "protocol.delay(seconds=0.5)" in py_code
        assert "touch_tip(" in py_code

    mock_res = check_protocol_source(py_code, use_mock=True)
    assert mock_res.ok, mock_res.errors
    assert mock_res.well_volumes_ul["plate:A1"] == pytest.approx(60.0)
    assert mock_res.well_volumes_ul["plate:A2"] == pytest.approx(60.0)

    try:
        from opentrons import simulate as ot_simulate
    except ImportError:
        return
    runlog, _ = ot_simulate.simulate(io.StringIO(py_code))
    formatted = ot_simulate.format_runlog(runlog)
    assert "Picking up tip" in formatted
    assert "Dropping tip" in formatted


def test_plate_fill_example_uses_dispense_position_top() -> None:
    source = compile_file(ROOT / "examples" / "plate_fill.yaml")
    assert '[plate["A5"].top(), plate["A6"].top(), plate["A7"].top(), plate["A8"].top()]' in source


def test_public_protocols_eval_suite_compiles_and_passes_mock() -> None:
    import sys as _sys

    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from benchmarks.public_protocols_eval import build_public_assay_suite

    suite = build_public_assay_suite()
    assert len(suite) >= 30
    families = {fam for _, fam, _ in suite}
    assert {
        "elisa",
        "pcr_setup",
        "serial_dilution",
        "bead_cleanup",
        "bradford_bca",
        "ngs_library_prep",
    } <= families
    for assay_id, _, proto in suite:
        py_src = compile_protocol(proto)
        res = check_protocol_source(py_src, use_mock=True)
        assert res.ok, f"{assay_id} failed mock check: {res.errors}"

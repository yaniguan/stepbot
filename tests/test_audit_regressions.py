"""Regression tests covering stepbot audit issues #3 through #26."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from stepbot import (
    ValidationError,
    build_protocol_manifest,
    check_protocol,
    check_twinstep_protocol,
    compile_file,
    compile_protocol,
    from_twinstep,
    parse_and_validate,
)
from stepbot.catalog import _grid_spec, get_labware_spec
from stepbot.cli import main
from stepbot.models import DeckItem
from stepbot.wells import resolve_well_refs

ROOT = Path(__file__).resolve().parent.parent


def _proto(
    *,
    plate_labware: str = "corning_96_wellplate_360ul_flat",
    tip_labware: str = "opentrons_96_tiprack_300ul",
    pipette_model: str = "p300_single_gen2",
    liquids: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {"name": "Audit regression test"},
        "deck": {
            "plate": {"labware": plate_labware, "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": tip_labware, "slot": 3},
        },
        "pipettes": {
            "left": {"model": pipette_model, "tip_racks": ["tips"]},
        },
        "liquids": (
            {
                "buffer": {"wells": "reservoir:A1", "volume": 5000},
                "dye": {"wells": "reservoir:A2", "volume": 2000},
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
# Issue #3: Pipette-to-tip-rack volume compatibility & effective max volume
# ---------------------------------------------------------------------------


def test_issue_3_pipette_tip_rack_family_mismatch_rejected(tmp_path: Path) -> None:
    bad = _proto(
        tip_labware="opentrons_96_tiprack_10ul",
        pipette_model="p1000_single_gen2",
        steps=[{"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 200}}],
    )
    with pytest.raises(ValidationError, match="incompatible with pipette 'p1000_single_gen2'"):
        compile_protocol(bad)

    py_path = tmp_path / "mismatch.py"
    py_path.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    tips = protocol.load_labware("opentrons_96_tiprack_10ul", 1)\n'
        '    protocol.load_instrument("p1000_single_gen2", "left", tip_racks=[tips])\n',
        encoding="utf-8",
    )
    res = check_protocol(py_path, use_mock=True)
    assert not res.ok
    assert "incompatible" in res.errors[0]


def test_issue_3_filter_tips_cap_effective_max_volume(tmp_path: Path) -> None:
    over = _proto(
        tip_labware="opentrons_96_filtertiprack_200ul",
        pipette_model="p300_single_gen2",
        steps=[{"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 250}}],
    )
    with pytest.raises(ValidationError, match=r"\[20\.0, 200\.0\] uL.*200 uL tip rack"):
        compile_protocol(over)

    valid = _proto(
        tip_labware="opentrons_96_filtertiprack_200ul",
        pipette_model="p300_single_gen2",
        steps=[{"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 200}}],
    )
    assert "transfer(200" in compile_protocol(valid)

    py_path = tmp_path / "filter_over.py"
    py_path.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    res = protocol.load_labware("nest_12_reservoir_15ml", 1)\n'
        '    tips = protocol.load_labware("opentrons_96_filtertiprack_200ul", 2)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    w = protocol.define_liquid("water", description="", display_color=None)\n'
        '    res["A1"].load_liquid(liquid=w, volume=1000)\n'
        "    pip.pick_up_tip()\n"
        '    pip.aspirate(250, res["A1"])\n',
        encoding="utf-8",
    )
    res = check_protocol(py_path, use_mock=True)
    assert not res.ok
    assert "[20.0, 200.0]" in res.errors[0]


# ---------------------------------------------------------------------------
# Issue #4: distribute disposal_volume in compiler and MockProtocolContext
# ---------------------------------------------------------------------------


def test_issue_4_distribute_disposal_volume_tracked_in_compile_and_mock(tmp_path: Path) -> None:
    # 7 x 100 uL on p300 (min_volume=20) -> 4 chunks ([2,2,2,1]) -> 700 + 4*20 = 780 uL needed.
    insufficient = _proto(
        liquids={"buffer": {"wells": "reservoir:A1", "volume": 720}},
        steps=[{"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A7", "volume": 100}}],
    )
    with pytest.raises(ValidationError, match="aspirating 780 uL.*holds only 720 uL"):
        compile_protocol(insufficient)

    sufficient = _proto(
        liquids={"buffer": {"wells": "reservoir:A1", "volume": 780}},
        steps=[{"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A7", "volume": 100}}],
    )
    py_ok = tmp_path / "dist_ok.py"
    py_ok.write_text(compile_protocol(sufficient), encoding="utf-8")
    assert check_protocol(py_ok, use_mock=True).ok

    # Hand-edited 720 uL script must also fail mock simulation.
    py_dry = tmp_path / "dist_dry.py"
    py_dry.write_text(
        compile_protocol(sufficient).replace("volume=780", "volume=720"),
        encoding="utf-8",
    )
    res_dry = check_protocol(py_dry, use_mock=True)
    assert not res_dry.ok
    assert "Cannot aspirate" in res_dry.errors[0]

    # Explicit disposal_volume=0 allows 720 uL and volume == max_volume (300 uL).
    zero_disp = _proto(
        liquids={"buffer": {"wells": "reservoir:A1", "volume": 720}},
        steps=[
            {
                "distribute": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A7",
                    "volume": 100,
                    "disposal_volume": 0,
                }
            }
        ],
    )
    src_zero = compile_protocol(zero_disp)
    assert "disposal_volume=0" in src_zero
    py_zero = tmp_path / "dist_zero.py"
    py_zero.write_text(src_zero, encoding="utf-8")
    assert check_protocol(py_zero, use_mock=True).ok

    max_vol_fail = _proto(
        steps=[{"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A2", "volume": 300}}]
    )
    with pytest.raises(ValidationError, match="plus disposal_volume"):
        compile_protocol(max_vol_fail)


# ---------------------------------------------------------------------------
# Issue #5: Heterogeneous Falcon 4x50mL + 6x15mL tube rack & per-well capacity
# ---------------------------------------------------------------------------


def test_issue_5_heterogeneous_falcon_rack_per_well_capacities(tmp_path: Path) -> None:
    spec = get_labware_spec("opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical")
    assert spec is not None
    assert spec.max_volume_for("A1") == 15000.0
    assert spec.max_volume_for("B2") == 15000.0
    assert spec.max_volume_for("A3") == 50000.0
    assert spec.max_volume_for("B4") == 50000.0

    valid_50ml = {
        "metadata": {"name": "Falcon 50mL stock"},
        "deck": {
            "rack": {
                "labware": "opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical",
                "slot": 1,
            },
            "plate": {"labware": "corning_24_wellplate_3.4ml_flat", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_1000ul", "slot": 3},
        },
        "pipettes": {"left": {"model": "p1000_single_gen2", "tip_racks": ["tips"]}},
        "liquids": {"stock50": {"wells": "rack:A3", "volume": 40000}},
        "steps": [{"transfer": {"src": "rack:A3", "dst": "plate:A1", "volume": 1000}}],
    }
    py_path = tmp_path / "falcon.py"
    py_path.write_text(compile_protocol(valid_50ml), encoding="utf-8")
    assert check_protocol(py_path, use_mock=True).ok

    over_15ml = dict(valid_50ml)
    over_15ml["liquids"] = {"stock15": {"wells": "rack:A1", "volume": 20000}}
    with pytest.raises(ValidationError, match="exceeds well capacity 15000.0 uL on 'rack:A1'"):
        compile_protocol(over_15ml)


# ---------------------------------------------------------------------------
# Issue #6: Safe AST mock checker (no unfiltered exec)
# ---------------------------------------------------------------------------


def test_issue_6_mock_checker_blocks_unsafe_python_constructs(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    payloads = [
        f'import os\nmetadata = {{"apiLevel": "2.16"}}\ndef run(protocol):\n    os.mkdir({str(sentinel)!r})\n',
        (
            'metadata = {"apiLevel": "2.16"}\n'
            "def run(protocol):\n"
            f"    open({str(sentinel)!r}, 'w').write('x')\n"
        ),
        (
            'metadata = {"apiLevel": "2.16"}\n'
            "def run(protocol):\n"
            "    x = protocol.__class__\n"
        ),
        (
            'metadata = {"apiLevel": "2.16"}\n'
            "def run(protocol):\n"
            "    __import__('os').system('echo bad')\n"
        ),
    ]
    for idx, code in enumerate(payloads):
        p = tmp_path / f"evil_{idx}.py"
        p.write_text(code, encoding="utf-8")
        res = check_protocol(p, use_mock=True)
        assert not res.ok
        assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Issue #7: Strict YAML/JSON key allowlists with typo suggestions
# ---------------------------------------------------------------------------


def test_issue_7_unknown_keys_rejected_with_close_match_hints() -> None:
    typo_step = _proto(
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A2",
                    "volume": 50,
                    "new_tips": "always",
                }
            }
        ]
    )
    with pytest.raises(ValidationError, match="Unknown key 'new_tips'.*did you mean 'new_tip'"):
        compile_protocol(typo_step)

    typo_root = _proto()
    typo_root["liquid"] = typo_root.pop("liquids")
    with pytest.raises(ValidationError, match="Unknown key 'liquid'.*did you mean 'liquids'"):
        compile_protocol(typo_root)

    typo_meta = _proto()
    typo_meta["metadata"]["api_level"] = "2.16"
    with pytest.raises(ValidationError, match="Unknown key 'api_level'.*did you mean 'apiLevel'"):
        compile_protocol(typo_meta)


# ---------------------------------------------------------------------------
# Issue #8: dilution_series source/diluent disjointness from series_wells
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,dil,expected",
    [
        ("reservoir:A1", "reservoir:A1", "distinct wells"),
        ("plate:A1", "reservoir:A2", "overlaps with series_wells"),
        ("plate:A2", "reservoir:A2", "overlaps with series_wells"),
        ("reservoir:A1", "plate:A3", "overlaps with series_wells"),
    ],
)
def test_issue_8_dilution_series_disjoint_wells_enforced(
    src: str, dil: str, expected: str
) -> None:
    proto = _proto(
        liquids={
            "s1": {"wells": "reservoir:A1", "volume": 2000},
            "s2": {"wells": "reservoir:A2", "volume": 2000},
            "p1": {"wells": "plate:A1", "volume": 300},
            "p2": {"wells": "plate:A2", "volume": 300},
            "p3": {"wells": "plate:A3", "volume": 300},
        },
        steps=[
            {
                "dilution_series": {
                    "source": src,
                    "diluent": dil,
                    "first_well": "plate:A1",
                    "n": 4,
                    "factor": 2,
                    "final_volume": 100,
                }
            }
        ],
    )
    with pytest.raises(ValidationError, match=expected):
        compile_protocol(proto)


# ---------------------------------------------------------------------------
# Issue #9: Shared tip racks across left & right mounts in MockProtocolContext
# ---------------------------------------------------------------------------


def test_issue_9_shared_tip_rack_does_not_double_allocate_tips(tmp_path: Path) -> None:
    py_shared = tmp_path / "shared_tips.py"
    py_shared.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)\n'
        '    left = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    right = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tips])\n'
        "    left.pick_up_tip()\n"
        "    right.pick_up_tip()\n"
        "    left.drop_tip()\n"
        "    right.drop_tip()\n",
        encoding="utf-8",
    )
    res = check_protocol(py_shared, use_mock=True)
    assert res.ok
    assert "Picking up tip from A1 of opentrons_96_tiprack_300ul on slot 1" in res.lines[0]
    assert "Picking up tip from B1 of opentrons_96_tiprack_300ul on slot 1" in res.lines[1]

    py_exhaust = tmp_path / "shared_exhaust.py"
    py_exhaust.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)\n'
        '    left = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    right = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tips])\n'
        "    for w in tips.wells():\n"
        "        left.pick_up_tip()\n"
        "        left.drop_tip()\n"
        "    right.pick_up_tip()\n",
        encoding="utf-8",
    )
    res_ex = check_protocol(py_exhaust, use_mock=True)
    assert not res_ex.ok
    assert "Out of tips" in res_ex.errors[0]


# ---------------------------------------------------------------------------
# Issue #11: OSError / IsADirectoryError / UnicodeDecodeError handling
# ---------------------------------------------------------------------------


def test_issue_11_cli_and_checker_handle_directories_and_bad_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_bytes = tmp_path / "invalid_utf8.yaml"
    bad_bytes.write_bytes(b"\xff\xfe\x00\x81invalid")

    assert main(["compile", str(tmp_path)]) == 1
    assert "Validation error:" in capsys.readouterr().err

    assert main(["compile", str(bad_bytes)]) == 1
    assert "Validation error:" in capsys.readouterr().err

    res_dir = check_protocol(tmp_path, use_mock=True)
    assert not res_dir.ok
    assert "directory" in res_dir.errors[0].lower()

    bad_py = tmp_path / "invalid_utf8.py"
    bad_py.write_bytes(b"\xff\xfe\xfa")
    res_utf = check_protocol(bad_py, use_mock=True)
    assert not res_utf.ok
    assert res_utf.errors


# ---------------------------------------------------------------------------
# Issue #12: Mock checker validates metadata['apiLevel'] & 2.14+ liquids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_meta", ['{"apiLevel": 2.1}', '{"apiLevel": "banana"}', '{"apiLevel": "3.0"}'])
def test_issue_12_mock_rejects_invalid_api_level(tmp_path: Path, bad_meta: str) -> None:
    p = tmp_path / "bad_api.py"
    p.write_text(f"metadata = {bad_meta}\ndef run(protocol):\n    pass\n", encoding="utf-8")
    res = check_protocol(p, use_mock=True)
    assert not res.ok
    assert "apiLevel" in res.errors[0]


def test_issue_12_mock_rejects_liquids_below_2_14(tmp_path: Path) -> None:
    p = tmp_path / "old_liq.py"
    p.write_text(
        'metadata = {"apiLevel": "2.13"}\n'
        "def run(protocol):\n"
        '    protocol.define_liquid("water")\n',
        encoding="utf-8",
    )
    res = check_protocol(p, use_mock=True)
    assert not res.ok
    assert "2.14" in res.errors[0]


# ---------------------------------------------------------------------------
# Issue #13: DiscardStep emits blow_out(protocol.fixed_trash)
# ---------------------------------------------------------------------------


def test_issue_13_discard_step_emits_blow_out() -> None:
    compiled = compile_file(ROOT / "examples" / "dilution_series.yaml")
    assert (
        '    pipette_left.aspirate(100, plate["A8"])\n'
        "    pipette_left.dispense(100, protocol.fixed_trash)\n"
        "    pipette_left.blow_out(protocol.fixed_trash)\n"
        "    pipette_left.drop_tip()"
    ) in compiled


# ---------------------------------------------------------------------------
# Issue #14: Unified VOLUME_TOLERANCE and dispense min_volume check in mock
# ---------------------------------------------------------------------------


def test_issue_14_dispense_min_volume_and_float_tolerance(tmp_path: Path) -> None:
    p = tmp_path / "sub_min_dispense.py"
    p.write_text(
        'metadata = {"apiLevel": "2.16"}\n'
        "def run(protocol):\n"
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 1)\n'
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 2)\n'
        '    pip = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])\n'
        '    w = protocol.define_liquid("water")\n'
        '    plate["A1"].load_liquid(liquid=w, volume=100)\n'
        "    pip.pick_up_tip()\n"
        '    pip.aspirate(50, plate["A1"])\n'
        '    pip.dispense(5, plate["A2"])\n',
        encoding="utf-8",
    )
    res = check_protocol(p, use_mock=True)
    assert not res.ok
    assert "dispense volume 5" in res.errors[0]

    # Floating-point rounding tolerance: 3 * (60 / 3) == 60.0
    third = 60.0 / 3.0
    proto = _proto(
        liquids={"buffer": {"wells": "reservoir:A1", "volume": 60.0}},
        steps=[
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": third}},
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A2", "volume": third}},
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A3", "volume": third}},
        ],
    )
    py_tol = tmp_path / "tol.py"
    py_tol.write_text(compile_protocol(proto), encoding="utf-8")
    assert check_protocol(py_tol, use_mock=True).ok


# ---------------------------------------------------------------------------
# Issue #15: pause rejects combining timed wait and operator message
# ---------------------------------------------------------------------------


def test_issue_15_pause_rejects_both_seconds_and_message() -> None:
    proto = _proto(
        steps=[
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 50}},
            {"pause": {"seconds": 10, "message": "Check clarity"}},
        ]
    )
    with pytest.raises(ValidationError, match="cannot specify both duration.*and operator message"):
        compile_protocol(proto)


# ---------------------------------------------------------------------------
# Issue #16: Rectangular well-range order ('row' vs 'col') & col:/row: syntax
# ---------------------------------------------------------------------------


def test_issue_16_well_range_order_and_axis_selectors() -> None:
    spec = get_labware_spec("corning_96_wellplate_360ul_flat")
    assert spec is not None
    deck = {"plate": DeckItem("plate", spec.load_name, 1, spec)}

    row_refs = [w.well for w in resolve_well_refs("plate:A1-B2", deck, well_order="row")]
    assert row_refs == ["A1", "A2", "B1", "B2"]

    col_refs = [w.well for w in resolve_well_refs("plate:A1-B2", deck, well_order="col")]
    assert col_refs == ["A1", "B1", "A2", "B2"]

    col_axis = [w.well for w in resolve_well_refs("plate:col:1-2", deck)]
    assert len(col_axis) == 16
    assert col_axis[:8] == ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]
    assert col_axis[8:] == ["A2", "B2", "C2", "D2", "E2", "F2", "G2", "H2"]

    row_axis = [w.well for w in resolve_well_refs("plate:row:A", deck)]
    assert len(row_axis) == 12
    assert row_axis[0] == "A1" and row_axis[-1] == "A12"

    proto = _proto(
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-B2",
                    "volume": 50,
                    "well_order": "col",
                }
            }
        ]
    )
    compiled = compile_protocol(proto)
    assert '[plate["A1"], plate["B1"], plate["A2"], plate["B2"]]' in compiled


# ---------------------------------------------------------------------------
# Issue #17: Multi-letter rows ('AA1'..'AF48') and leading-zero ('A01') hint
# ---------------------------------------------------------------------------


def test_issue_17_multi_letter_rows_and_leading_zeros() -> None:
    spec_1536 = _grid_spec("custom_1536_wellplate_10ul", 32, 48, 10.0)
    deck = {"hd": DeckItem("hd", spec_1536.load_name, 1, spec_1536)}

    refs = resolve_well_refs(["hd:AA1", "hd:AF48"], deck)
    assert [r.well for r in refs] == ["AA1", "AF48"]

    span = resolve_well_refs("hd:Z1-AB2", deck)
    assert [r.well for r in span] == ["Z1", "Z2", "AA1", "AA2", "AB1", "AB2"]

    with pytest.raises(ValidationError, match="use 'A1' instead of 'A01'"):
        resolve_well_refs("hd:A01", deck)

    with pytest.raises(ValidationError, match="use 'A1' instead of 'A01'"):
        resolve_well_refs("hd:A01-A04", deck)


# ---------------------------------------------------------------------------
# Issue #18: get_labware_spec queries opentrons_shared_data without version=1
# ---------------------------------------------------------------------------


def test_issue_18_get_labware_spec_prefers_latest_shared_data_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_load_definition(load_name: str, version: int | None = None) -> dict[str, Any]:
        calls.append((load_name, version))
        if version == 1:
            raise FileNotFoundError("no v1 definition")
        return {
            "ordering": [["A1", "B1"]],
            "wells": {
                "A1": {"totalLiquidVolume": 500},
                "B1": {"totalLiquidVolume": 250},
            },
            "parameters": {"isTiprack": False},
        }

    fake_sd = types.ModuleType("opentrons_shared_data")
    fake_lw = types.ModuleType("opentrons_shared_data.labware")
    fake_lw.load_definition = fake_load_definition  # type: ignore[attr-defined]
    fake_err = types.ModuleType("opentrons_shared_data.errors")
    fake_err.PythonException = RuntimeError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentrons_shared_data", fake_sd)
    monkeypatch.setitem(sys.modules, "opentrons_shared_data.labware", fake_lw)
    monkeypatch.setitem(sys.modules, "opentrons_shared_data.errors", fake_err)

    spec = get_labware_spec("v2_only_mixed_plate")
    assert spec is not None
    assert calls == [("v2_only_mixed_plate", None)]
    assert spec.max_volume_for("A1") == 500.0
    assert spec.max_volume_for("B1") == 250.0


# ---------------------------------------------------------------------------
# Issue #19: dilution_series precision & non-terminating warning
# ---------------------------------------------------------------------------


def test_issue_19_dilution_series_precision_and_warning(tmp_path: Path) -> None:
    proto = _proto(
        steps=[
            {
                "dilution_series": {
                    "source": "reservoir:A2",
                    "diluent": "reservoir:A1",
                    "first_well": "plate:A1",
                    "n": 4,
                    "factor": 4,
                    "final_volume": 100,
                }
            }
        ]
    )
    with pytest.warns(UserWarning, match="non-terminating at precision=2; rounded to 33.33 uL"):
        source = compile_protocol(proto)
    assert "33.33," in source
    assert "33.3333" not in source

    py_path = tmp_path / "dil_factor4.py"
    py_path.write_text(source, encoding="utf-8")
    res = check_protocol(py_path, use_mock=True)
    assert res.ok
    assert any("33.33 uL" in line for line in res.lines)


# ---------------------------------------------------------------------------
# Issue #20: Multi-aspirate carry splitting when volume > effective_max_volume
# ---------------------------------------------------------------------------


def test_issue_20_multi_aspirate_transfer_and_discard_strokes(tmp_path: Path) -> None:
    proto = _proto(
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
    spec = parse_and_validate(proto)
    assert spec.tips_used_per_rack["tips"] == 2
    py_path = tmp_path / "multi_carry.py"
    py_path.write_text(compile_protocol(proto), encoding="utf-8")
    res = check_protocol(py_path, use_mock=True)
    assert res.ok
    # 2 wells * 2 strokes of 300.0 uL = 4 aspirates of 300.0 uL
    assert sum("Aspirating 300.0 uL" in line for line in res.lines) == 4

    dil_multi = _proto(
        plate_labware="corning_24_wellplate_3.4ml_flat",
        steps=[
            {
                "dilution_series": {
                    "source": "reservoir:A2",
                    "diluent": "reservoir:A1",
                    "first_well": "plate:A1",
                    "n": 3,
                    "factor": 1.5,
                    "final_volume": 200,
                    "multi_aspirate": True,
                }
            }
        ],
    )
    # transfer_vol = 400 uL -> 2 strokes of 200 uL in DiscardStep
    dil_src = compile_protocol(dil_multi)
    assert dil_src.count("dispense(200, protocol.fixed_trash)") == 2
    py_dil = tmp_path / "dil_multi.py"
    py_dil.write_text(dil_src, encoding="utf-8")
    assert check_protocol(py_dil, use_mock=True).ok


# ---------------------------------------------------------------------------
# Issue #21: delay supports 'minutes' parameter
# ---------------------------------------------------------------------------


def test_issue_21_delay_supports_minutes_and_combined_duration(tmp_path: Path) -> None:
    proto = _proto(
        steps=[
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 50}},
            {"delay": {"minutes": 5}},
            {"delay": {"minutes": 1, "seconds": 30}},
        ]
    )
    src = compile_protocol(proto)
    assert "protocol.delay(minutes=5)" in src
    assert "protocol.delay(minutes=1, seconds=30)" in src

    py_path = tmp_path / "delay_min.py"
    py_path.write_text(src, encoding="utf-8")
    res = check_protocol(py_path, use_mock=True)
    assert res.ok
    assert "Delaying for 300.0 seconds" in res.lines
    assert "Delaying for 90.0 seconds" in res.lines


# ---------------------------------------------------------------------------
# Issue #22 & #23: pyproject pytest pythonpath & CI matrix + mypy
# ---------------------------------------------------------------------------


def test_issue_22_23_pyproject_and_ci_configuration() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pythonpath = ["src"]' in pyproject_text

    ci_data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    mock_os = ci_data["jobs"]["test-mock"]["strategy"]["matrix"]["os"]
    assert {"ubuntu-latest", "windows-latest", "macos-latest"} <= set(mock_os)
    typecheck_steps = " ".join(
        str(s.get("run", "")) for s in ci_data["jobs"]["typecheck"]["steps"]
    )
    assert "mypy --strict src/stepbot" in typecheck_steps


# ---------------------------------------------------------------------------
# Issue #24: Reagent lineage tracking detects cross-contamination
# ---------------------------------------------------------------------------


def test_issue_24_reagent_lineage_blocks_cross_contamination() -> None:
    # Case 1: new_tip='never' carries dye back into clean buffer reservoir
    contam_never = _proto(
        steps=[
            {
                "transfer": {
                    "src": "reservoir:A2",
                    "dst": "plate:A1",
                    "volume": 50,
                    "new_tip": "never",
                }
            },
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A2",
                    "volume": 50,
                    "new_tip": "never",
                }
            },
        ]
    )
    with pytest.raises(ValidationError, match="cross-contamination.*carrying \\['dye'\\]"):
        compile_protocol(contam_never)

    # Explicit allow_contamination: true permits intentional reuse
    contam_allowed = dict(contam_never, allow_contamination=True)
    assert "def run(protocol):" in compile_protocol(contam_allowed)

    # Case 2: new_tip='once' with mix_after in spiked destination wells contaminates stock
    contam_mix_after = _proto(
        steps=[
            {"transfer": {"src": "reservoir:A2", "dst": "plate:A1-A2", "volume": 50, "new_tip": "always"}},
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1-A2",
                    "volume": 50,
                    "new_tip": "once",
                    "mix_after": {"reps": 2, "volume": 50},
                }
            },
        ]
    )
    with pytest.raises(ValidationError, match="cross-contamination"):
        compile_protocol(contam_mix_after)


# ---------------------------------------------------------------------------
# Issue #25: twinstep importer / evaluator bridge
# ---------------------------------------------------------------------------


def test_issue_25_twinstep_adapter_accepts_clean_and_rejects_error_twins(tmp_path: Path) -> None:
    clean_twinstep = [
        {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 180, "unit": "uL"},
        {"type": "transfer", "source": "reservoir:dye", "dest": "plate1:A1", "volume": 20, "unit": "uL"},
        {"type": "mix", "well": "plate1:A1", "volume": 100, "unit": "uL", "cycles": 4},
        {"type": "incubate", "plate": "plate1", "temperature": 37, "time": 10, "time_unit": "min"},
        {"type": "wash", "plate": "plate1", "volume": 300, "unit": "uL", "cycles": 3},
        {"type": "read", "plate": "plate1", "mode": "absorbance", "wavelength": 450},
    ]
    spec_dict = from_twinstep({"protocol_id": "mir_demo", "steps": clean_twinstep})
    ok, py_src = check_twinstep_protocol({"protocol_id": "mir_demo", "steps": clean_twinstep})
    assert ok, py_src
    py_path = tmp_path / "twinstep_clean.py"
    py_path.write_text(compile_protocol(spec_dict), encoding="utf-8")
    assert check_protocol(py_path, use_mock=True).ok

    error_twins = [
        # Overfill: 250 + 200 = 450 > 360 uL
        [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 250, "unit": "uL"},
            {"type": "transfer", "source": "reservoir:dye", "dest": "plate1:A1", "volume": 200, "unit": "uL"},
        ],
        # Underflow / aspirate_empty: transfer out of empty A1 before filling A1
        [
            {"type": "transfer", "source": "plate1:A1", "dest": "plate1:A2", "volume": 50, "unit": "uL"},
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 100, "unit": "uL"},
        ],
        # Out of range (wrong_unit: 20 mL = 20000 uL)
        [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 20, "unit": "mL"},
        ],
        # Sub-minimum pipette volume (2 uL on p300)
        [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 2, "unit": "uL"},
        ],
        # Tip reuse cross-contamination across distinct reagents
        [
            {"type": "transfer", "source": "reservoir:dye", "dest": "plate1:A1", "volume": 50, "unit": "uL", "new_tip": "never"},
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A2", "volume": 50, "unit": "uL", "new_tip": "never"},
        ],
    ]
    for twin_steps in error_twins:
        ok_twin, err_msg = check_twinstep_protocol({"corrupted_steps": twin_steps})
        assert not ok_twin and err_msg


# ---------------------------------------------------------------------------
# Issue #26: Machine-readable protocol manifest JSON (--manifest)
# ---------------------------------------------------------------------------


def test_issue_26_cli_emits_protocol_manifest_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = ROOT / "examples" / "dilution_series.yaml"
    out_py = tmp_path / "dilution.py"
    manifest_json = tmp_path / "dilution_manifest.json"

    rc = main(["compile", str(yaml_path), "-o", str(out_py), "--manifest", str(manifest_json)])
    assert rc == 0
    assert out_py.exists() and manifest_json.exists()
    capsys.readouterr()

    manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(out_py.read_bytes()).hexdigest()
    assert manifest["source_sha256"] == expected_hash
    assert manifest["api_level"] == "2.16"
    assert manifest["tips_used_per_rack"] == {"tips": 3}
    assert manifest["total_tips_used"] == 3

    # Reservoir A2 lost 7*100 + 4*20 = 780 uL -> 4220 uL remaining
    assert manifest["well_volumes"]["reservoir:A2"]["initial_ul"] == 5000.0
    assert manifest["well_volumes"]["reservoir:A2"]["final_ul"] == 4220.0
    # Plate A1 peaked at 200 uL and ended at 100 uL
    assert manifest["well_volumes"]["plate:A1"]["peak_ul"] == 200.0
    assert manifest["well_volumes"]["plate:A1"]["final_ul"] == 100.0
    # Plate A8 peaked at 200 uL (100 diluent + 100 serial transfer) and ended at 100 uL after discard
    assert manifest["well_volumes"]["plate:A8"]["peak_ul"] == 200.0
    assert manifest["well_volumes"]["plate:A8"]["final_ul"] == 100.0
    assert set(manifest["well_volumes"]["plate:A8"]["species"]) == {"buffer", "dye"}

    # Step origin indices map back to 1-based YAML steps 1, 2, 3
    source_indices = {s["source_step_index"] for s in manifest["steps"]}
    assert source_indices == {1, 2, 3}

    # Direct API parity check
    spec = parse_and_validate(yaml.safe_load(yaml_path.read_text(encoding="utf-8")))
    assert build_protocol_manifest(spec) == manifest

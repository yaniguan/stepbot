"""Validation error tests for invalid slots, wells, volumes, and tip counts."""

from __future__ import annotations

import copy
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from stepbot.catalog import DEFAULT_API_LEVEL
from stepbot.compiler import compile_protocol, load_protocol_data
from stepbot.models import ValidationError


def _base_protocol() -> dict[str, Any]:
    return {
        "metadata": {"name": "Base validation test"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {
            "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
        },
        "liquids": {
            "stock": {"wells": "reservoir:A1", "volume": 10000},
            "diluent": {"wells": "reservoir:A2", "volume": 10000},
        },
        "steps": [
            {
                "transfer": {
                    "src": "reservoir:A1",
                    "dst": "plate:A1",
                    "volume": 100,
                }
            }
        ],
    }


@pytest.mark.parametrize("bad_slot", [0, 12, -1, "1", True])
def test_invalid_slot_rejected(bad_slot: Any) -> None:
    data = _base_protocol()
    data["deck"]["plate"]["slot"] = bad_slot
    with pytest.raises(ValidationError, match="slot"):
        compile_protocol(data)


def test_duplicate_slot_rejected() -> None:
    data = _base_protocol()
    data["deck"]["reservoir"]["slot"] = 1
    with pytest.raises(ValidationError, match="Duplicate deck slot"):
        compile_protocol(data)


def test_unknown_labware_rejected() -> None:
    data = _base_protocol()
    data["deck"]["plate"]["labware"] = "nonexistent_plate_96"
    with pytest.raises(ValidationError, match="Unknown Opentrons labware"):
        compile_protocol(data)


@pytest.mark.parametrize(
    "bad_well",
    [
        "plate:A13",
        "plate:I1",
        "reservoir:B1",
        "plate:A5-A1",
        "unknown:A1",
        "tips:A1",
    ],
)
def test_bad_well_rejected(bad_well: str) -> None:
    data = _base_protocol()
    data["steps"] = [
        {"transfer": {"src": "reservoir:A1", "dst": bad_well, "volume": 100}}
    ]
    with pytest.raises(ValidationError):
        compile_protocol(data)


@pytest.mark.parametrize("bad_volume", [0, -10, 5, 350, "100", False])
def test_out_of_range_volume_rejected(bad_volume: Any) -> None:
    data = _base_protocol()
    data["steps"] = [
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": bad_volume}}
    ]
    with pytest.raises(ValidationError, match="volume"):
        compile_protocol(data)


def test_dilution_series_out_of_pipette_range_rejected() -> None:
    data = _base_protocol()
    # factor=2, final_volume=200 -> initial_volume=400 (>300 uL pipette max); the pipette
    # range check fires first, so well capacity is covered by the next test.
    data["steps"] = [
        {
            "dilution_series": {
                "source": "reservoir:A1",
                "diluent": "reservoir:A2",
                "first_well": "plate:A1",
                "n": 6,
                "factor": 2,
                "final_volume": 200,
            }
        }
    ]
    with pytest.raises(ValidationError, match="range"):
        compile_protocol(data)


def test_dilution_series_exceeding_row_rejected() -> None:
    data = _base_protocol()
    data["steps"] = [
        {
            "dilution_series": {
                "source": "reservoir:A1",
                "diluent": "reservoir:A2",
                "first_well": "plate:A8",
                "n": 8,
                "factor": 2,
                "final_volume": 100,
            }
        }
    ]
    with pytest.raises(ValidationError, match="exceeds labware grid"):
        compile_protocol(data)


def test_not_enough_tips_rejected() -> None:
    data = _base_protocol()
    # 96 transfers with new_tip=always uses 96 tips; adding one more exceeds the 96-tip rack.
    data["steps"] = [
        {
            "transfer": {
                "src": "reservoir:A1",
                "dst": "plate:A1-H12",
                "volume": 50,
                "new_tip": "always",
            }
        },
        {
            "transfer": {
                "src": "reservoir:A2",
                "dst": "plate:A1",
                "volume": 50,
                "new_tip": "once",
            }
        },
    ]
    with pytest.raises(ValidationError, match="Not enough tips"):
        compile_protocol(copy.deepcopy(data))


def test_non_tiprack_in_tip_racks_rejected() -> None:
    data = _base_protocol()
    data["pipettes"]["left"]["tip_racks"] = ["plate"]
    with pytest.raises(ValidationError, match="not a tip rack"):
        compile_protocol(data)


def test_dilution_series_exceeding_well_capacity_rejected() -> None:
    data = _base_protocol()
    data["deck"]["plate"]["labware"] = "nest_96_wellplate_100ul_pcr_full_skirt"
    # factor=2, final_volume=60 -> initial_volume=120: inside the p300 range, above the 100 uL well.
    data["steps"] = [
        {
            "dilution_series": {
                "source": "reservoir:A1",
                "diluent": "reservoir:A2",
                "first_well": "plate:A1",
                "n": 4,
                "factor": 2,
                "final_volume": 60,
            }
        }
    ]
    with pytest.raises(ValidationError, match="exceeds well capacity"):
        compile_protocol(data)


def test_repeated_transfers_overfilling_well_rejected() -> None:
    data = _base_protocol()
    data["deck"]["plate"]["labware"] = "nest_96_wellplate_100ul_pcr_full_skirt"
    data["steps"] = [
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 80}},
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 80}},
    ]
    with pytest.raises(ValidationError, match=r"Step 2 \(transfer\).*exceeds well capacity"):
        compile_protocol(data)


def test_distribute_overfilling_well_rejected() -> None:
    data = _base_protocol()
    data["deck"]["plate"]["labware"] = "nest_96_wellplate_100ul_pcr_full_skirt"
    data["steps"] = [
        {"distribute": {"src": "reservoir:A1", "dst": ["plate:A1", "plate:A2"], "volume": 150}},
    ]
    with pytest.raises(ValidationError, match="exceeds well capacity"):
        compile_protocol(data)


def test_mix_larger_than_well_contents_rejected() -> None:
    data = _base_protocol()
    data["steps"] = [
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 50}},
        {"mix": {"well": "plate:A1", "volume": 250, "reps": 2}},
    ]
    with pytest.raises(ValidationError, match="mix volume 250 uL is more than the 50 uL"):
        compile_protocol(data)


def test_transfer_mix_after_larger_than_well_contents_rejected() -> None:
    data = _base_protocol()
    data["steps"] = [
        {
            "transfer": {
                "src": "reservoir:A1",
                "dst": "plate:A1",
                "volume": 50,
                "mix_after": {"reps": 2, "volume": 100},
            }
        },
    ]
    with pytest.raises(ValidationError, match="mix volume"):
        compile_protocol(data)


def test_aspirating_from_undeclared_well_rejected() -> None:
    data = _base_protocol()
    data["steps"] = [
        {"transfer": {"src": "reservoir:A3", "dst": "plate:A1", "volume": 50}},
    ]
    with pytest.raises(ValidationError, match="holds only 0 uL"):
        compile_protocol(data)


def test_source_running_dry_rejected() -> None:
    data = _base_protocol()
    data["liquids"]["stock"]["volume"] = 150
    data["steps"] = [
        {"distribute": {"src": "reservoir:A1", "dst": "plate:A1-A2", "volume": 100}},
    ]
    with pytest.raises(ValidationError, match="aspirating 220 uL.*holds only 150 uL"):
        compile_protocol(data)


def test_liquid_volume_above_well_capacity_rejected() -> None:
    data = _base_protocol()
    data["liquids"]["stock"] = {"wells": "plate:A1", "volume": 400}
    with pytest.raises(ValidationError, match="exceeds well capacity"):
        compile_protocol(data)


def test_liquid_wells_overlapping_rejected() -> None:
    data = _base_protocol()
    data["liquids"]["diluent"]["wells"] = "reservoir:A1"
    with pytest.raises(ValidationError, match="assigned to both liquid"):
        compile_protocol(data)


def test_liquids_require_api_level_2_14() -> None:
    data = _base_protocol()
    data["metadata"]["apiLevel"] = "2.13"
    with pytest.raises(ValidationError, match="requires apiLevel 2.14"):
        compile_protocol(data)


@pytest.mark.parametrize("alias", ["pipette_left", "pipette_right", "liquids", "protocol"])
def test_reserved_deck_alias_rejected(alias: str) -> None:
    data = _base_protocol()
    data["deck"][alias] = data["deck"].pop("plate")
    with pytest.raises(ValidationError, match="reserved"):
        compile_protocol(data)


def test_multichannel_pipette_rejected() -> None:
    data = _base_protocol()
    data["pipettes"]["left"]["model"] = "p300_multi_gen2"
    with pytest.raises(ValidationError, match="Multichannel pipette 'p300_multi_gen2'"):
        compile_protocol(data)


@pytest.mark.parametrize("bad_level", [2.1, 2, True])
def test_non_string_api_level_rejected(bad_level: Any) -> None:
    data = _base_protocol()
    data["metadata"]["apiLevel"] = bad_level
    with pytest.raises(ValidationError, match="must be a quoted string"):
        compile_protocol(data)


@pytest.mark.parametrize("bad_level", ["banana", "2", "2.16.1", "", "3.0", "2.016"])
def test_malformed_api_level_rejected(bad_level: str) -> None:
    data = _base_protocol()
    data["metadata"]["apiLevel"] = bad_level
    with pytest.raises(ValidationError, match="apiLevel"):
        compile_protocol(data)


def test_unquoted_yaml_api_level_rejected(tmp_path: Path) -> None:
    data = _base_protocol()
    data["metadata"]["apiLevel"] = "LEVEL"
    text = yaml.safe_dump(data).replace("apiLevel: LEVEL", "apiLevel: 2.10")
    yaml_path = tmp_path / "proto.yaml"
    yaml_path.write_text(text, encoding="utf-8")
    loaded = load_protocol_data(yaml_path)
    assert loaded["metadata"]["apiLevel"] == 2.1
    with pytest.raises(ValidationError, match="2.10 is read as 2.1"):
        compile_protocol(loaded)


def test_quoted_api_level_kept_verbatim() -> None:
    data = _base_protocol()
    data["metadata"]["apiLevel"] = "2.20"
    assert "\"apiLevel\": '2.20'" in compile_protocol(data)


def test_default_api_level_ignores_installed_opentrons(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = types.ModuleType("opentrons.protocol_api")
    fake_api.MAX_SUPPORTED_VERSION = types.SimpleNamespace(major=2, minor=15)
    monkeypatch.setitem(sys.modules, "opentrons", types.ModuleType("opentrons"))
    monkeypatch.setitem(sys.modules, "opentrons.protocol_api", fake_api)
    source = compile_protocol(_base_protocol())
    assert f"\"apiLevel\": '{DEFAULT_API_LEVEL}'" in source

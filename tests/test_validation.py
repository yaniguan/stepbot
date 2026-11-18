"""Validation error tests for invalid slots, wells, volumes, and tip counts."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from stepbot.compiler import compile_protocol
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
    # factor=2, final_volume=200 -> initial_volume=400 (>300 uL pipette max & >360 uL well max)
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

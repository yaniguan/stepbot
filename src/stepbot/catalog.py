"""Built-in catalog of standard Opentrons labware and pipettes."""

from __future__ import annotations

import re
from typing import Any

from stepbot.models import DeckSlot, LabwareSpec, PipetteSpec


def row_index_to_label(row: int) -> str:
    """Convert a 0-based row index to an Opentrons row label (0->'A', 25->'Z', 26->'AA')."""
    if row < 0:
        raise ValueError(f"row index must be >= 0, got {row}")
    chars: list[str] = []
    n = row + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _grid_spec(
    load_name: str,
    rows: int,
    cols: int,
    max_volume: float,
    *,
    is_tiprack: bool = False,
) -> LabwareSpec:
    wells: list[str] = []
    well_grid: dict[str, tuple[int, int]] = {}
    well_volumes: dict[str, float] = {}
    for col in range(cols):
        for row in range(rows):
            name = f"{row_index_to_label(row)}{col + 1}"
            wells.append(name)
            well_grid[name] = (row, col)
            well_volumes[name] = max_volume
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(wells),
        well_grid=well_grid,
        max_volume=max_volume,
        is_tiprack=is_tiprack,
        well_volumes=well_volumes,
    )


def _falcon_4x50_6x15_spec() -> LabwareSpec:
    load_name = "opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical"
    coords = [
        ("A1", 0, 0, 15000.0),
        ("B1", 1, 0, 15000.0),
        ("C1", 2, 0, 15000.0),
        ("A2", 0, 1, 15000.0),
        ("B2", 1, 1, 15000.0),
        ("C2", 2, 1, 15000.0),
        ("A3", 0, 2, 50000.0),
        ("B3", 1, 2, 50000.0),
        ("A4", 0, 3, 50000.0),
        ("B4", 1, 3, 50000.0),
    ]
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(name for name, _, _, _ in coords),
        well_grid={name: (r, c) for name, r, c, _ in coords},
        max_volume=50000.0,
        is_tiprack=False,
        well_volumes={name: vol for name, _, _, vol in coords},
    )


LABWARE_CATALOG: dict[str, LabwareSpec] = {
    spec.load_name: spec
    for spec in [
        _grid_spec("corning_96_wellplate_360ul_flat", 8, 12, 360.0),
        _grid_spec("corning_384_wellplate_112ul_flat", 16, 24, 112.0),
        _grid_spec("corning_24_wellplate_3.4ml_flat", 4, 6, 3400.0),
        _grid_spec("corning_12_wellplate_6.9ml_flat", 3, 4, 6900.0),
        _grid_spec("corning_6_wellplate_16.8ml_flat", 2, 3, 16800.0),
        _grid_spec("nest_96_wellplate_200ul_flat", 8, 12, 200.0),
        _grid_spec("nest_96_wellplate_100ul_pcr_full_skirt", 8, 12, 100.0),
        _grid_spec("nest_96_wellplate_2ml_deep", 8, 12, 2000.0),
        _grid_spec("nest_12_reservoir_15ml", 1, 12, 15000.0),
        _grid_spec("nest_1_reservoir_195ml", 1, 1, 195000.0),
        _grid_spec("usascientific_12_reservoir_22ml", 1, 12, 22000.0),
        _grid_spec("agilent_1_reservoir_290ml", 1, 1, 290000.0),
        _grid_spec("opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", 4, 6, 1500.0),
        _grid_spec("opentrons_24_tuberack_nest_1.5ml_snapcap", 4, 6, 1500.0),
        _grid_spec("opentrons_15_tuberack_falcon_15ml_conical", 3, 5, 15000.0),
        _grid_spec("opentrons_6_tuberack_falcon_50ml_conical", 2, 3, 50000.0),
        _falcon_4x50_6x15_spec(),
        _grid_spec("opentrons_96_tiprack_300ul", 8, 12, 300.0, is_tiprack=True),
        _grid_spec("opentrons_96_tiprack_20ul", 8, 12, 20.0, is_tiprack=True),
        _grid_spec("opentrons_96_tiprack_10ul", 8, 12, 10.0, is_tiprack=True),
        _grid_spec("opentrons_96_tiprack_1000ul", 8, 12, 1000.0, is_tiprack=True),
        _grid_spec("opentrons_96_filtertiprack_200ul", 8, 12, 200.0, is_tiprack=True),
        _grid_spec("opentrons_96_filtertiprack_20ul", 8, 12, 20.0, is_tiprack=True),
        _grid_spec("opentrons_96_filtertiprack_1000ul", 8, 12, 1000.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_tiprack_50ul", 8, 12, 50.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_tiprack_200ul", 8, 12, 200.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_tiprack_1000ul", 8, 12, 1000.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_filtertiprack_50ul", 8, 12, 50.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_filtertiprack_200ul", 8, 12, 200.0, is_tiprack=True),
        _grid_spec("opentrons_flex_96_filtertiprack_1000ul", 8, 12, 1000.0, is_tiprack=True),
    ]
}


PIPETTE_CATALOG: dict[str, PipetteSpec] = {
    spec.model: spec
    for spec in [
        PipetteSpec("p10_single", 1.0, 10.0, 1),
        PipetteSpec("p10_multi", 1.0, 10.0, 8),
        PipetteSpec("p20_single_gen2", 1.0, 20.0, 1),
        PipetteSpec("p20_multi_gen2", 1.0, 20.0, 8),
        PipetteSpec("p50_single", 5.0, 50.0, 1),
        PipetteSpec("p50_multi", 5.0, 50.0, 8),
        PipetteSpec("p300_single", 30.0, 300.0, 1),
        PipetteSpec("p300_multi", 30.0, 300.0, 8),
        PipetteSpec("p300_single_gen2", 20.0, 300.0, 1),
        PipetteSpec("p300_multi_gen2", 20.0, 300.0, 8),
        PipetteSpec("p1000_single", 100.0, 1000.0, 1),
        PipetteSpec("p1000_single_gen2", 100.0, 1000.0, 1),
        PipetteSpec("flex_1channel_50", 1.0, 50.0, 1),
        PipetteSpec("flex_1channel_1000", 5.0, 1000.0, 1),
        PipetteSpec("flex_8channel_50", 1.0, 50.0, 8),
        PipetteSpec("flex_8channel_1000", 5.0, 1000.0, 8),
    ]
}


DEFAULT_API_LEVEL = "2.16"
FLEX_DEFAULT_API_LEVEL = "2.18"
DEFAULT_FLEX_TRASH_SLOT = "A3"

FLEX_WORKING_SLOTS: frozenset[str] = frozenset(
    f"{row}{col}" for row in ("A", "B", "C", "D") for col in (1, 2, 3)
)
FLEX_STAGING_SLOTS: frozenset[str] = frozenset(
    f"{row}4" for row in ("A", "B", "C", "D")
)
FLEX_ALL_SLOTS: frozenset[str] = FLEX_WORKING_SLOTS | FLEX_STAGING_SLOTS

_FLEX_SLOT_RE = re.compile(r"^([A-D])([1-4])$")


def is_flex_pipette(model: str) -> bool:
    """Return True if a pipette model belongs to the Opentrons Flex instrument family."""
    return model.startswith("flex_")


def is_flex_tiprack(load_name: str) -> bool:
    """Return True if a tip-rack load name belongs to the Opentrons Flex tip family."""
    return load_name.startswith("opentrons_flex_")


def is_valid_flex_slot(slot: Any, *, allow_staging: bool = True) -> bool:
    """Return True if `slot` is a canonical Opentrons Flex deck coordinate ('A1'..'D3' or 'A4'..'D4')."""
    if not isinstance(slot, str):
        return False
    allowed = FLEX_ALL_SLOTS if allow_staging else FLEX_WORKING_SLOTS
    return slot in allowed


def deck_slot_sort_key(slot: DeckSlot) -> tuple[int, int, int, str]:
    """Deterministic sort key ordering OT-2 integer slots (1..11) then Flex coordinates (A1..D4)."""
    if isinstance(slot, int) and not isinstance(slot, bool):
        return (0, slot, 0, "")
    s = str(slot)
    match = _FLEX_SLOT_RE.match(s)
    if match:
        row_ch, col_s = match.groups()
        return (1, ord(row_ch) - ord("A"), int(col_s), s)
    return (2, 0, 0, s)


def is_tip_rack_compatible(pip_spec: PipetteSpec, rack_spec: LabwareSpec) -> bool:
    """Return True if a tip rack's nozzle/volume family matches the pipette."""
    if not rack_spec.is_tiprack:
        return False
    pip_flex = is_flex_pipette(pip_spec.model)
    rack_flex = is_flex_tiprack(rack_spec.load_name)
    if pip_flex != rack_flex:
        return False
    tip_vol = rack_spec.max_volume
    if pip_flex:
        if pip_spec.max_volume <= 50.0:
            return abs(tip_vol - 50.0) <= 1e-6
        return 50.0 <= tip_vol <= 1000.0
    if pip_spec.max_volume <= 20.0:
        return 1.0 <= tip_vol <= 20.0
    if pip_spec.max_volume <= 300.0:
        return 50.0 <= tip_vol <= 300.0
    return tip_vol > 300.0


def expected_tip_family_description(pip_spec: PipetteSpec) -> str:
    """Return human-readable compatible tip-rack volume range for error diagnostics."""
    if is_flex_pipette(pip_spec.model):
        if pip_spec.max_volume <= 50.0:
            return "Opentrons Flex 50 uL"
        return "Opentrons Flex 50-1000 uL"
    if pip_spec.max_volume <= 20.0:
        return "10-20 uL"
    if pip_spec.max_volume <= 300.0:
        return "50-300 uL (e.g. 200 uL filter or 300 uL standard)"
    return "1000 uL"


def get_labware_spec(load_name: str) -> LabwareSpec | None:
    """Return a LabwareSpec for a standard Opentrons labware load name."""
    if load_name in LABWARE_CATALOG:
        return LABWARE_CATALOG[load_name]
    try:
        from opentrons_shared_data.errors import PythonException
        from opentrons_shared_data.labware import load_definition
    except ImportError:
        return None
    defn: dict[str, Any] | None = None
    try:
        defn = load_definition(load_name)
    except TypeError:
        for version in (3, 2, 1):
            try:
                defn = load_definition(load_name, version)
                break
            except (FileNotFoundError, KeyError, ValueError, PythonException):
                continue
    except (FileNotFoundError, KeyError, ValueError, PythonException):
        return None
    if defn is None:
        return None
    wells: list[str] = []
    well_grid: dict[str, tuple[int, int]] = {}
    for col_idx, col_wells in enumerate(defn["ordering"]):
        for row_idx, well_name in enumerate(col_wells):
            wells.append(well_name)
            well_grid[well_name] = (row_idx, col_idx)
    well_volumes = {
        w: float(defn["wells"][w]["totalLiquidVolume"]) for w in wells
    }
    is_tiprack = bool(defn["parameters"]["isTiprack"])
    if not well_volumes:
        max_volume = 0.0
    elif is_tiprack:
        max_volume = min(well_volumes.values())
    else:
        max_volume = max(well_volumes.values())
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(wells),
        well_grid=well_grid,
        max_volume=max_volume,
        is_tiprack=is_tiprack,
        well_volumes=well_volumes,
    )


def get_pipette_spec(model: str) -> PipetteSpec | None:
    """Return a PipetteSpec for a standard Opentrons pipette model."""
    return PIPETTE_CATALOG.get(model)


def parse_api_level(api_level: str) -> tuple[int, int]:
    """Split an 'X.Y' API level string into an integer (major, minor) tuple."""
    major, minor = api_level.split(".")
    return int(major), int(minor)


def installed_max_api_level() -> tuple[int, int] | None:
    """Return the highest API level the installed opentrons supports, or None."""
    try:
        from opentrons.protocol_api import MAX_SUPPORTED_VERSION
    except ImportError:
        return None
    return int(MAX_SUPPORTED_VERSION.major), int(MAX_SUPPORTED_VERSION.minor)

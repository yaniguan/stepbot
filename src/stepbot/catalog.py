"""Built-in catalog of standard Opentrons labware and pipettes."""

from __future__ import annotations

from stepbot.models import LabwareSpec, PipetteSpec


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
    for col in range(cols):
        for row in range(rows):
            name = f"{chr(ord('A') + row)}{col + 1}"
            wells.append(name)
            well_grid[name] = (row, col)
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(wells),
        well_grid=well_grid,
        max_volume=max_volume,
        is_tiprack=is_tiprack,
    )


def _falcon_4x50_6x15_spec() -> LabwareSpec:
    load_name = "opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical"
    coords = [
        ("A1", 0, 0), ("B1", 1, 0), ("C1", 2, 0),
        ("A2", 0, 1), ("B2", 1, 1), ("C2", 2, 1),
        ("A3", 0, 2), ("B3", 1, 2),
        ("A4", 0, 3), ("B4", 1, 3),
    ]
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(name for name, _, _ in coords),
        well_grid={name: (r, c) for name, r, c in coords},
        max_volume=15000.0,
        is_tiprack=False,
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
    ]
}


DEFAULT_API_LEVEL = "2.16"


def get_labware_spec(load_name: str) -> LabwareSpec | None:
    """Return a LabwareSpec for a standard Opentrons labware load name."""
    if load_name in LABWARE_CATALOG:
        return LABWARE_CATALOG[load_name]
    try:
        from opentrons_shared_data.labware import load_definition
        from opentrons_shared_data.errors import PythonException
    except ImportError:
        return None
    try:
        defn = load_definition(load_name, 1)
    except (FileNotFoundError, KeyError, ValueError, PythonException):
        return None
    wells: list[str] = []
    well_grid: dict[str, tuple[int, int]] = {}
    for col_idx, col_wells in enumerate(defn["ordering"]):
        for row_idx, well_name in enumerate(col_wells):
            wells.append(well_name)
            well_grid[well_name] = (row_idx, col_idx)
    first_well = defn["wells"][wells[0]]
    return LabwareSpec(
        load_name=load_name,
        wells=tuple(wells),
        well_grid=well_grid,
        max_volume=float(first_well["totalLiquidVolume"]),
        is_tiprack=bool(defn["parameters"]["isTiprack"]),
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
    return MAX_SUPPORTED_VERSION.major, MAX_SUPPORTED_VERSION.minor

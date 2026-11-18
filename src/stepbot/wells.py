"""Well parsing, range expansion, and grid validation helpers."""

from __future__ import annotations

import re

from stepbot.models import DeckItem, ValidationError, WellRef

_WELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def split_labware_and_wells(spec: str) -> tuple[str, str]:
    """Split a reference like 'plate:A1-A8' or 'plate.A1' into (labware, expr)."""
    if ":" in spec:
        labware, rest = spec.split(":", 1)
    elif "." in spec:
        labware, rest = spec.split(".", 1)
    else:
        raise ValidationError(
            f"Invalid well reference '{spec}': expected '<labware>:<well>' (e.g. 'plate:A1')"
        )
    labware = labware.strip()
    rest = rest.strip()
    if not labware or not rest:
        raise ValidationError(f"Invalid well reference '{spec}'")
    return labware, rest


def _parse_well_coord(well: str) -> tuple[int, int]:
    match = _WELL_RE.match(well)
    if not match:
        raise ValidationError(f"Invalid well name '{well}'")
    row_str, col_str = match.groups()
    if len(row_str) != 1:
        raise ValidationError(f"Invalid well name '{well}'")
    row = ord(row_str) - ord("A")
    col = int(col_str) - 1
    return row, col


def _validate_well_exists(item: DeckItem, well: str) -> None:
    if well not in item.spec.well_grid:
        raise ValidationError(
            f"Well '{well}' does not exist on labware '{item.alias}' ({item.load_name})"
        )


def _expand_range(item: DeckItem, start_well: str, end_well: str) -> list[WellRef]:
    _validate_well_exists(item, start_well)
    _validate_well_exists(item, end_well)
    r1, c1 = item.spec.well_grid[start_well]
    r2, c2 = item.spec.well_grid[end_well]
    if r1 > r2 or c1 > c2:
        raise ValidationError(
            f"Invalid well range '{item.alias}:{start_well}-{end_well}': start must precede end"
        )
    coord_to_name = {coord: name for name, coord in item.spec.well_grid.items()}
    refs: list[WellRef] = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            name = coord_to_name.get((r, c))
            if name is None:
                raise ValidationError(
                    f"Non-rectangular well range '{item.alias}:{start_well}-{end_well}' on '{item.load_name}'"
                )
            refs.append(WellRef(item.alias, name))
    return refs


def resolve_well_refs(
    raw: str | list[str],
    deck: dict[str, DeckItem],
    *,
    allow_tiprack: bool = False,
) -> list[WellRef]:
    """Expand a well reference, range, or list of references into WellRefs."""
    tokens = [raw] if isinstance(raw, str) else list(raw)
    if not tokens:
        raise ValidationError("Well reference list cannot be empty")
    result: list[WellRef] = []
    for token in tokens:
        if not isinstance(token, str):
            raise ValidationError(f"Well reference must be a string, got {type(token).__name__}")
        labware_alias, expr = split_labware_and_wells(token)
        if labware_alias not in deck:
            raise ValidationError(f"Unknown labware '{labware_alias}' in '{token}'")
        item = deck[labware_alias]
        if item.spec.is_tiprack and not allow_tiprack:
            raise ValidationError(
                f"Cannot transfer liquid to or from tip rack '{labware_alias}'"
            )
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise ValidationError(f"Empty well entry in '{token}'")
            if "-" in part:
                bounds = part.split("-")
                if len(bounds) != 2:
                    raise ValidationError(f"Invalid well range '{part}' in '{token}'")
                start_w, end_w = bounds[0].strip(), bounds[1].strip()
                result.extend(_expand_range(item, start_w, end_w))
            else:
                _parse_well_coord(part)
                _validate_well_exists(item, part)
                result.append(WellRef(labware_alias, part))
    return result


def resolve_serial_wells(
    first_well_raw: str,
    count: int,
    deck: dict[str, DeckItem],
    *,
    direction: str = "row",
) -> list[WellRef]:
    """Compute consecutive wells for a dilution series starting at first_well."""
    refs = resolve_well_refs(first_well_raw, deck)
    if len(refs) != 1:
        raise ValidationError(
            f"dilution_series first_well must be a single well, got '{first_well_raw}'"
        )
    start = refs[0]
    item = deck[start.labware]
    row, col = item.spec.well_grid[start.well]
    coord_to_name = {coord: name for name, coord in item.spec.well_grid.items()}
    series: list[WellRef] = []
    for idx in range(count):
        if direction == "row":
            coord = (row, col + idx)
        elif direction == "col":
            coord = (row + idx, col)
        else:
            raise ValidationError(
                f"Invalid dilution_series direction '{direction}': expected 'row' or 'col'"
            )
        well_name = coord_to_name.get(coord)
        if well_name is None:
            raise ValidationError(
                f"dilution_series of {count} wells from '{start.labware}:{start.well}' "
                f"exceeds labware grid ({item.load_name})"
            )
        series.append(WellRef(start.labware, well_name))
    return series

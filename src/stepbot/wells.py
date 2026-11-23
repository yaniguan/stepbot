"""Well parsing, range expansion, and grid validation helpers."""

from __future__ import annotations

import re

from stepbot.models import DeckItem, ValidationError, WellRef

_WELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_ROW_RE = re.compile(r"^[A-Z]+$")
_COL_RE = re.compile(r"^\d+$")


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


def _row_label_to_index(row_str: str) -> int:
    """Convert a 1+ letter uppercase row label ('A'..'Z', 'AA'..'AF', ...) to 0-based index."""
    if not _ROW_RE.match(row_str):
        raise ValidationError(f"Invalid row coordinate '{row_str}'")
    idx = 0
    for ch in row_str:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _parse_col_number(col_str: str, context: str) -> int:
    """Parse a 1-based column integer string, rejecting leading zeros explicitly."""
    if not _COL_RE.match(col_str):
        raise ValidationError(f"Invalid column coordinate '{col_str}' in '{context}'")
    if col_str.startswith("0"):
        col_val = int(col_str)
        if col_val == 0:
            raise ValidationError(
                f"Invalid well coordinate '{context}': column numbers start at 1"
            )
        raise ValidationError(
            f"Invalid well coordinate '{context}': leading zeros are not allowed "
            f"(use column '{col_val}' instead of '{col_str}')"
        )
    return int(col_str) - 1


def _parse_well_coord(well: str) -> tuple[int, int]:
    match = _WELL_RE.match(well)
    if not match:
        raise ValidationError(f"Invalid well name '{well}'")
    row_str, col_str = match.groups()
    if col_str.startswith("0"):
        col_val = int(col_str)
        if col_val == 0:
            raise ValidationError(f"Invalid well name '{well}': column numbers start at 1")
        canonical = f"{row_str}{col_val}"
        raise ValidationError(
            f"Invalid well name '{well}': leading zeros are not allowed "
            f"(use '{canonical}' instead of '{well}')"
        )
    row = _row_label_to_index(row_str)
    col = int(col_str) - 1
    return row, col


def _validate_well_exists(item: DeckItem, well: str) -> None:
    if well not in item.spec.well_grid:
        raise ValidationError(
            f"Well '{well}' does not exist on labware '{item.alias}' ({item.load_name})"
        )


def _validate_well_order(well_order: str) -> str:
    if well_order not in ("row", "col"):
        raise ValidationError(
            f"Invalid well_order '{well_order}': expected 'row' (row-major) or 'col' (column-major)"
        )
    return well_order


def _expand_range(
    item: DeckItem,
    start_well: str,
    end_well: str,
    *,
    well_order: str = "row",
) -> list[WellRef]:
    """Expand a rectangular 2D well range in row-major ('row', default) or column-major ('col') order."""
    _validate_well_order(well_order)
    _parse_well_coord(start_well)
    _parse_well_coord(end_well)
    _validate_well_exists(item, start_well)
    _validate_well_exists(item, end_well)
    r1, c1 = item.spec.well_grid[start_well]
    r2, c2 = item.spec.well_grid[end_well]
    if r1 > r2 or c1 > c2:
        raise ValidationError(
            f"Invalid well range '{item.alias}:{start_well}-{end_well}': start must precede end "
            f"(2D ranges expand {well_order}-major; pass well_order: 'col' or '{item.alias}:col:1-2' for column-major)"
        )
    coord_to_name = {coord: name for name, coord in item.spec.well_grid.items()}
    refs: list[WellRef] = []
    if well_order == "row":
        coords = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
    else:
        coords = [(r, c) for c in range(c1, c2 + 1) for r in range(r1, r2 + 1)]
    for coord in coords:
        name = coord_to_name.get(coord)
        if name is None:
            raise ValidationError(
                f"Non-rectangular well range '{item.alias}:{start_well}-{end_well}' on '{item.load_name}'"
            )
        refs.append(WellRef(item.alias, name))
    return refs


def _expand_axis_selector(item: DeckItem, axis: str, selector: str, raw_token: str) -> list[WellRef]:
    """Expand explicit '<labware>:col:1-2' or '<labware>:row:A-B' selectors."""
    max_row = max(r for r, _ in item.spec.well_grid.values())
    max_col = max(c for _, c in item.spec.well_grid.values())
    coord_to_name = {coord: name for name, coord in item.spec.well_grid.items()}
    refs: list[WellRef] = []

    for part in selector.split(","):
        part = part.strip()
        if not part:
            raise ValidationError(f"Empty {axis} entry in '{raw_token}'")
        if axis == "col":
            if "-" in part:
                bounds = part.split("-")
                if len(bounds) != 2:
                    raise ValidationError(f"Invalid column range '{part}' in '{raw_token}'")
                c1 = _parse_col_number(bounds[0].strip(), raw_token)
                c2 = _parse_col_number(bounds[1].strip(), raw_token)
            else:
                c1 = c2 = _parse_col_number(part, raw_token)
            if c1 > c2 or c2 > max_col:
                raise ValidationError(
                    f"Column selector '{part}' out of bounds on '{item.alias}' ({item.load_name})"
                )
            for c in range(c1, c2 + 1):
                for r in range(max_row + 1):
                    name = coord_to_name.get((r, c))
                    if name is None:
                        raise ValidationError(
                            f"Non-rectangular column '{c + 1}' on '{item.load_name}'"
                        )
                    refs.append(WellRef(item.alias, name))
        else:
            if "-" in part:
                bounds = part.split("-")
                if len(bounds) != 2:
                    raise ValidationError(f"Invalid row range '{part}' in '{raw_token}'")
                r1 = _row_label_to_index(bounds[0].strip())
                r2 = _row_label_to_index(bounds[1].strip())
            else:
                r1 = r2 = _row_label_to_index(part)
            if r1 > r2 or r2 > max_row:
                raise ValidationError(
                    f"Row selector '{part}' out of bounds on '{item.alias}' ({item.load_name})"
                )
            for r in range(r1, r2 + 1):
                for c in range(max_col + 1):
                    name = coord_to_name.get((r, c))
                    if name is None:
                        raise ValidationError(
                            f"Non-rectangular row in '{raw_token}' on '{item.load_name}'"
                        )
                    refs.append(WellRef(item.alias, name))
    return refs


def resolve_well_refs(
    raw: str | list[str],
    deck: dict[str, DeckItem],
    *,
    allow_tiprack: bool = False,
    well_order: str = "row",
) -> list[WellRef]:
    """Expand a well reference, range, or list of references into WellRefs.

    2D rectangular ranges (e.g. 'plate:A1-H2') expand row-major by default
    ('A1, A2, B1, B2, ...'); pass well_order='col' or use 'plate:col:1-2' for
    Opentrons column-major traversal ('A1..H1, A2..H2').
    """
    _validate_well_order(well_order)
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
        if expr.startswith("col:") or expr.startswith("row:"):
            axis, selector = expr.split(":", 1)
            selector = selector.strip()
            if not selector:
                raise ValidationError(f"Empty {axis} selector in '{token}'")
            result.extend(_expand_axis_selector(item, axis, selector, token))
            continue
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise ValidationError(f"Empty well entry in '{token}'")
            if "-" in part:
                bounds = part.split("-")
                if len(bounds) != 2:
                    raise ValidationError(f"Invalid well range '{part}' in '{token}'")
                start_w, end_w = bounds[0].strip(), bounds[1].strip()
                result.extend(_expand_range(item, start_w, end_w, well_order=well_order))
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

"""Protocol parser, validator, and compiler."""

from __future__ import annotations

import difflib
import json
import keyword
import math
from pathlib import Path
import re
from typing import Any
import warnings

import yaml

from stepbot.catalog import (
    DEFAULT_API_LEVEL,
    DEFAULT_FLEX_TRASH_SLOT,
    FLEX_DEFAULT_API_LEVEL,
    FLEX_STAGING_SLOTS,
    LIQUID_CLASS_CATALOG,
    build_rheology_spec,
    expected_tip_family_description,
    get_labware_spec,
    get_pipette_spec,
    is_flex_pipette,
    is_tip_rack_compatible,
    is_valid_flex_slot,
    normalize_liquid_class_name,
    parse_api_level,
)
from stepbot.codegen import build_protocol_manifest, render_protocol
from stepbot.models import (
    VOLUME_TOLERANCE,
    CommentStep,
    DeckItem,
    DeckSlot,
    DelayStep,
    DiscardStep,
    DistributeStep,
    DropTipStep,
    LiquidSpec,
    LoweredStep,
    MixStep,
    PauseStep,
    PickUpTipStep,
    PipetteConfig,
    ProtocolSpec,
    RheologySpec,
    StepOrigin,
    TransferStep,
    ValidationError,
    WellRef,
    WellSummary,
    plan_distribute_chunks,
    plan_transfer_strokes,
)
from stepbot.wells import resolve_serial_wells, resolve_well_refs

# Names used by the generated code itself; deck aliases must not shadow them.
_RESERVED_NAMES = {
    "protocol",
    "metadata",
    "requirements",
    "liquids",
    "pipette_left",
    "pipette_right",
}
_VALID_NEW_TIP = {"once", "always", "never"}
_VALID_DISPENSE_POSITION = {"bottom", "top"}
_API_LEVEL_RE = re.compile(r"^\d+\.\d+$")
# Well.load_liquid and ProtocolContext.define_liquid need API level 2.14 or newer.
_MIN_LIQUID_API_LEVEL = (2, 14)
_MIN_FLEX_API_LEVEL = (2, 15)

_ROOT_KEYS = {
    "metadata",
    "requirements",
    "robot_type",
    "trash_slot",
    "deck",
    "pipettes",
    "liquids",
    "steps",
    "allow_contamination",
}
_METADATA_KEYS = {
    "name",
    "protocolName",
    "description",
    "author",
    "apiLevel",
    "robotType",
    "robot_type",
    "trashSlot",
    "trash_slot",
}
_REQUIREMENTS_KEYS = {
    "robotType",
    "robot_type",
    "apiLevel",
    "trashSlot",
    "trash_slot",
}
_DECK_ENTRY_KEYS = {"labware", "slot"}
_PIPETTE_ENTRY_KEYS = {"model", "name", "tip_racks"}
_LIQUID_ENTRY_KEYS = {
    "wells",
    "well",
    "volume",
    "description",
    "color",
    "display_color",
    "well_order",
    "liquid_class",
}
_MIX_AFTER_KEYS = {"reps", "repetitions", "volume"}
_RHEOLOGY_STEP_KEYS = {
    "liquid_class",
    "flow_rate",
    "aspirate_flow_rate",
    "dispense_flow_rate",
    "air_gap",
    "touch_tip",
    "aspirate_delay",
    "settle_delay",
}

_STEP_ALLOWED_KEYS: dict[str, set[str]] = {
    "comment": {"text", "message"},
    "delay": {"seconds", "minutes"},
    "pause": {"seconds", "minutes", "message", "msg"},
    "transfer": {
        "pipette",
        "src",
        "source",
        "dst",
        "dest",
        "destination",
        "volume",
        "new_tip",
        "mix_after",
        "multi_aspirate",
        "dispense_position",
        "allow_contamination",
        "well_order",
        *_RHEOLOGY_STEP_KEYS,
    },
    "distribute": {
        "pipette",
        "src",
        "source",
        "dst",
        "dest",
        "destination",
        "volume",
        "new_tip",
        "disposal_volume",
        "dispense_position",
        "allow_contamination",
        "well_order",
        *_RHEOLOGY_STEP_KEYS,
    },
    "mix": {
        "pipette",
        "well",
        "wells",
        "volume",
        "reps",
        "repetitions",
        "new_tip",
        "allow_contamination",
        "well_order",
        "liquid_class",
        "flow_rate",
        "aspirate_flow_rate",
        "dispense_flow_rate",
        "touch_tip",
    },
    "discard": {
        "pipette",
        "src",
        "source",
        "volume",
        "new_tip",
        "multi_aspirate",
        "allow_contamination",
        "well_order",
    },
    "dilution_series": {
        "pipette",
        "source",
        "src",
        "diluent",
        "first_well",
        "n",
        "count",
        "wells",
        "factor",
        "final_volume",
        "direction",
        "mix_reps",
        "precision",
        "multi_aspirate",
        "allow_contamination",
        "liquid_class",
    },
}


def _check_allowed_keys(
    mapping: dict[str, Any],
    allowed: set[str],
    context: str,
) -> None:
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            hint = ""
            if isinstance(key, str):
                matches = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
                if matches:
                    hint = f" (did you mean '{matches[0]}'?)"
            raise ValidationError(f"Unknown key {key!r} in {context}{hint}")


def _finite_number(raw: Any, label: str) -> float:
    """Validate that `raw` is a finite int or float (excluding bool, NaN, and +/-Inf)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError(f"{label} must be a finite number, got {raw!r}")
    val = float(raw)
    if not math.isfinite(val):
        raise ValidationError(f"{label} must be a finite number, got {raw!r}")
    return val


def _finite_int(raw: Any, label: str, *, min_value: int | None = None) -> int:
    """Validate that `raw` is a finite integer (rejecting bool, NaN/Inf floats, and non-ints)."""
    if isinstance(raw, float) and not math.isfinite(raw):
        raise ValidationError(f"{label} must be a finite number, got {raw!r}")
    if isinstance(raw, bool) or not isinstance(raw, int):
        bound_note = f" >= {min_value}" if min_value is not None else ""
        raise ValidationError(f"{label} must be an integer{bound_note}, got {raw!r}")
    if min_value is not None and raw < min_value:
        raise ValidationError(f"{label} must be an integer >= {min_value}, got {raw!r}")
    return raw


def _parse_bool(raw: Any, label: str, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValidationError(f"{label} must be a boolean (true or false), got {raw!r}")
    return raw


def _parse_dispense_position(raw: Any, label: str) -> str:
    if raw is None:
        return "bottom"
    if not isinstance(raw, str) or raw not in _VALID_DISPENSE_POSITION:
        raise ValidationError(
            f"Invalid {label} {raw!r}: expected 'bottom' or 'top'"
        )
    return raw


def _parse_liquid_class_name(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(
            f"{label} must be one of {sorted(LIQUID_CLASS_CATALOG)}, got {raw!r}"
        )
    canonical = normalize_liquid_class_name(raw)
    if canonical is None:
        raise ValidationError(
            f"Unknown {label} {raw!r}: expected one of {sorted(LIQUID_CLASS_CATALOG)}"
        )
    return canonical


def load_protocol_data(path: Path | str) -> dict[str, Any]:
    """Load a YAML or JSON protocol file into a dictionary."""
    file_path = Path(path)
    if not file_path.exists():
        raise ValidationError(f"Protocol file not found: {file_path}")
    if file_path.is_dir():
        raise ValidationError(f"Protocol path is a directory, not a file: {file_path}")
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"Cannot read protocol file {file_path}: {exc}") from exc
    if file_path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON in {file_path}: {exc}") from exc
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValidationError(f"Invalid YAML in {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Protocol root must be a mapping/object")
    return data


def _require_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not name.isidentifier():
        raise ValidationError(f"{kind} name '{name}' must be a valid Python identifier")
    if keyword.iskeyword(name) or name in _RESERVED_NAMES:
        raise ValidationError(f"{kind} name '{name}' is reserved")
    return name


def _normalize_robot_type(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{label} must be 'OT-2' or 'Flex', got {raw!r}")
    token = raw.strip()
    if token in ("OT-2", "OT2"):
        return "OT-2"
    if token in ("Flex", "OT-3"):
        return "Flex"
    raise ValidationError(f"Unsupported {label} {raw!r}: expected 'OT-2' or 'Flex'")


def _resolve_robot_and_api_level(
    data: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[str, str, str | None]:
    raw_reqs = data.get("requirements")
    requirements: dict[str, Any] = {}
    if raw_reqs is not None:
        if not isinstance(raw_reqs, dict):
            raise ValidationError("'requirements' must be a mapping")
        _check_allowed_keys(raw_reqs, _REQUIREMENTS_KEYS, "'requirements'")
        requirements = raw_reqs

    robot_candidates: list[str] = []
    for src_val, label in (
        (data.get("robot_type"), "robot_type"),
        (requirements.get("robotType"), "requirements.robotType"),
        (requirements.get("robot_type"), "requirements.robot_type"),
        (metadata.get("robotType"), "metadata.robotType"),
        (metadata.get("robot_type"), "metadata.robot_type"),
    ):
        if src_val is not None:
            robot_candidates.append(_normalize_robot_type(src_val, label))

    if len(set(robot_candidates)) > 1:
        raise ValidationError(
            f"Conflicting robot_type declarations: {sorted(set(robot_candidates))}"
        )
    robot_type = robot_candidates[0] if robot_candidates else "OT-2"

    meta_api = metadata.get("apiLevel")
    req_api = requirements.get("apiLevel")
    if meta_api is not None and req_api is not None and meta_api != req_api:
        raise ValidationError(
            f"Conflicting apiLevel between metadata ({meta_api!r}) and requirements ({req_api!r})"
        )
    raw_api = req_api if req_api is not None else meta_api
    default_api = FLEX_DEFAULT_API_LEVEL if robot_type == "Flex" else DEFAULT_API_LEVEL
    api_level = _parse_api_level(raw_api, default_level=default_api)

    if robot_type == "Flex" and parse_api_level(api_level) < _MIN_FLEX_API_LEVEL:
        raise ValidationError(
            f"Opentrons Flex requires apiLevel {_MIN_FLEX_API_LEVEL[0]}.{_MIN_FLEX_API_LEVEL[1]} "
            f"or newer, got '{api_level}'"
        )

    raw_trash: Any = None
    for candidate in (
        data.get("trash_slot"),
        requirements.get("trashSlot"),
        requirements.get("trash_slot"),
        metadata.get("trashSlot"),
        metadata.get("trash_slot"),
    ):
        if candidate is not None:
            raw_trash = candidate
            break

    if robot_type == "OT-2":
        if raw_trash is not None:
            raise ValidationError("trash_slot is only configurable on Opentrons Flex protocols")
        return robot_type, api_level, None

    trash_slot = DEFAULT_FLEX_TRASH_SLOT if raw_trash is None else raw_trash
    if not is_valid_flex_slot(trash_slot, allow_staging=False):
        raise ValidationError(
            f"Invalid Flex trash_slot {trash_slot!r}: expected an addressable deck slot 'A1'..'D3'"
        )
    return robot_type, api_level, str(trash_slot)


def _parse_deck(
    raw_deck: Any,
    *,
    robot_type: str = "OT-2",
    trash_slot: str | None = None,
) -> dict[str, DeckItem]:
    if not isinstance(raw_deck, dict) or not raw_deck:
        raise ValidationError("'deck' must be a non-empty mapping of alias -> {labware, slot}")
    deck: dict[str, DeckItem] = {}
    used_slots: dict[DeckSlot, str] = {}
    eff_trash = (trash_slot or DEFAULT_FLEX_TRASH_SLOT) if robot_type == "Flex" else None

    for alias, entry in raw_deck.items():
        _require_identifier(alias, "Deck alias")
        if not isinstance(entry, dict):
            raise ValidationError(f"Deck entry '{alias}' must be a mapping")
        _check_allowed_keys(entry, _DECK_ENTRY_KEYS, f"deck entry '{alias}'")
        load_name = entry.get("labware")
        slot = entry.get("slot")
        if not isinstance(load_name, str) or not load_name:
            raise ValidationError(f"Deck entry '{alias}' missing 'labware' load name")
        spec = get_labware_spec(load_name)
        if spec is None:
            raise ValidationError(f"Unknown Opentrons labware '{load_name}' for '{alias}'")

        resolved_slot: DeckSlot
        if robot_type == "Flex":
            if not is_valid_flex_slot(slot, allow_staging=True):
                raise ValidationError(
                    f"Invalid slot {slot!r} for '{alias}': Opentrons Flex slot must be "
                    "a coordinate string 'A1'..'D3' (or staging 'A4'..'D4')"
                )
            resolved_slot = str(slot)
            if eff_trash is not None and resolved_slot == eff_trash:
                raise ValidationError(
                    f"Deck slot '{resolved_slot}' for '{alias}' collides with the Flex trash bin "
                    f"slot '{eff_trash}' (move labware or configure a different trash_slot)"
                )
        else:
            if isinstance(slot, bool) or not isinstance(slot, int) or not (1 <= slot <= 11):
                raise ValidationError(
                    f"Invalid slot '{slot}' for '{alias}': slot must be an integer between 1 and 11"
                )
            resolved_slot = slot

        if resolved_slot in used_slots:
            raise ValidationError(
                f"Duplicate deck slot {resolved_slot}: used by both "
                f"'{used_slots[resolved_slot]}' and '{alias}'"
            )
        used_slots[resolved_slot] = alias
        deck[alias] = DeckItem(alias=alias, load_name=load_name, slot=resolved_slot, spec=spec)
    return deck


def _parse_pipettes(
    raw_pipettes: Any,
    deck: dict[str, DeckItem],
    *,
    robot_type: str = "OT-2",
) -> dict[str, PipetteConfig]:
    if not isinstance(raw_pipettes, dict) or not raw_pipettes:
        raise ValidationError("'pipettes' must be a non-empty mapping of mount -> {model, tip_racks}")
    pipettes: dict[str, PipetteConfig] = {}
    for mount, entry in raw_pipettes.items():
        if mount not in ("left", "right"):
            raise ValidationError(f"Invalid pipette mount '{mount}': expected 'left' or 'right'")
        if not isinstance(entry, dict):
            raise ValidationError(f"Pipette entry '{mount}' must be a mapping")
        _check_allowed_keys(entry, _PIPETTE_ENTRY_KEYS, f"pipette '{mount}'")
        model = entry.get("model") or entry.get("name")
        if not isinstance(model, str) or not model:
            raise ValidationError(f"Pipette '{mount}' missing 'model'")
        spec = get_pipette_spec(model)
        if spec is None:
            raise ValidationError(f"Unknown Opentrons pipette model '{model}' on mount '{mount}'")
        if spec.channels not in (1, 8):
            raise ValidationError(
                f"Unsupported pipette channel count {spec.channels} on '{model}' (expected 1 or 8)"
            )
        if robot_type == "Flex":
            if not is_flex_pipette(model):
                raise ValidationError(
                    f"OT-2 pipette '{model}' on mount '{mount}' is incompatible with "
                    "Opentrons Flex (expected 'flex_1channel_*' or 'flex_8channel_*')"
                )
        else:
            if is_flex_pipette(model):
                raise ValidationError(
                    f"Opentrons Flex pipette '{model}' on mount '{mount}' requires "
                    "robot_type 'Flex'"
                )
        tip_racks = entry.get("tip_racks")
        if not isinstance(tip_racks, list) or not tip_racks:
            raise ValidationError(f"Pipette '{mount}' must define a non-empty 'tip_racks' list")
        resolved_racks: list[str] = []
        rack_volumes: list[float] = []
        for rack_alias in tip_racks:
            if not isinstance(rack_alias, str) or rack_alias not in deck:
                raise ValidationError(
                    f"Pipette '{mount}' references unknown tip rack '{rack_alias}'"
                )
            rack_item = deck[rack_alias]
            if not rack_item.spec.is_tiprack:
                raise ValidationError(
                    f"Deck item '{rack_alias}' ({rack_item.load_name}) is not a tip rack"
                )
            if isinstance(rack_item.slot, str) and rack_item.slot in FLEX_STAGING_SLOTS:
                raise ValidationError(
                    f"Tip rack '{rack_alias}' cannot be placed in Flex staging slot '{rack_item.slot}'"
                )
            if not is_tip_rack_compatible(spec, rack_item.spec):
                raise ValidationError(
                    f"Tip rack '{rack_alias}' ({rack_item.load_name}, {rack_item.spec.max_volume:g} uL) "
                    f"is incompatible with pipette '{model}' on mount '{mount}' "
                    f"(expected {expected_tip_family_description(spec)} tip rack)"
                )
            if rack_alias not in resolved_racks:
                resolved_racks.append(rack_alias)
                rack_volumes.append(rack_item.spec.max_volume)
        pipettes[mount] = PipetteConfig(
            mount=mount,
            model=model,
            tip_racks=tuple(resolved_racks),
            spec=spec,
            max_tip_volume=min(rack_volumes),
        )
    return pipettes


def _parse_api_level(raw: Any, *, default_level: str = DEFAULT_API_LEVEL) -> str:
    if raw is None:
        return default_level
    if not isinstance(raw, str):
        raise ValidationError(
            f"metadata.apiLevel must be a quoted string such as \"{default_level}\", "
            f"got {raw!r} ({type(raw).__name__}); unquoted YAML numbers drop trailing "
            "zeros, so 2.10 is read as 2.1"
        )
    level = raw.strip()
    if not _API_LEVEL_RE.match(level):
        raise ValidationError(
            f"metadata.apiLevel '{raw}' must look like '<major>.<minor>', e.g. \"{default_level}\""
        )
    major, minor = parse_api_level(level)
    if f"{major}.{minor}" != level:
        raise ValidationError(f"metadata.apiLevel '{raw}' must not have leading zeros")
    if major != 2:
        raise ValidationError(
            f"metadata.apiLevel '{raw}' is not supported: only Opentrons Python API v2 (2.x) is targeted"
        )
    return level


def _reject_staging_well_refs(
    wells: list[WellRef],
    deck: dict[str, DeckItem],
    label: str,
) -> None:
    for ref in wells:
        slot = deck[ref.labware].slot
        if isinstance(slot, str) and slot in FLEX_STAGING_SLOTS:
            raise ValidationError(
                f"{label}: pipettes cannot access labware '{ref.labware}' in Flex staging slot '{slot}'"
            )


def _resolve_channel_target(
    ref: WellRef,
    deck: dict[str, DeckItem],
    channels: int,
    label: str,
) -> tuple[tuple[WellRef, ...], int]:
    """Expand a well target for 1- or 8-channel pipettes into `(affected_wells, stroke_multiplier)`."""
    if channels == 1:
        return (ref,), 1
    item = deck[ref.labware]
    row, col = item.spec.well_grid[ref.well]
    if row != 0:
        raise ValidationError(
            f"{label}: {channels}-channel pipette must target row 'A' column heads "
            f"(e.g. '{ref.labware}:A{col + 1}'), got '{ref.key()}'"
        )
    col_entries = sorted(
        (r, w_name) for w_name, (r, c) in item.spec.well_grid.items() if c == col
    )
    if len(col_entries) == 1:
        return (ref,), channels
    if len(col_entries) == channels and [r for r, _ in col_entries] == list(range(channels)):
        return tuple(WellRef(ref.labware, w_name) for _, w_name in col_entries), 1
    raise ValidationError(
        f"{label}: {channels}-channel pipette cannot address {len(col_entries)}-row labware "
        f"'{ref.labware}' ({item.load_name}); expected {channels}-row plate or 1-row reservoir"
    )


def _parse_liquids(
    raw_liquids: Any,
    deck: dict[str, DeckItem],
) -> tuple[LiquidSpec, ...]:
    if raw_liquids is None:
        return ()
    if not isinstance(raw_liquids, dict):
        raise ValidationError("'liquids' must be a mapping of name -> {wells, volume}")
    liquids: list[LiquidSpec] = []
    owner: dict[WellRef, str] = {}
    for name, entry in raw_liquids.items():
        if not isinstance(name, str) or not name:
            raise ValidationError(f"Liquid name {name!r} must be a non-empty string")
        if not isinstance(entry, dict):
            raise ValidationError(f"Liquid '{name}' must be a mapping with 'wells' and 'volume'")
        _check_allowed_keys(entry, _LIQUID_ENTRY_KEYS, f"liquid '{name}'")
        well_order = str(entry.get("well_order", "row"))
        raw_wells = entry.get("wells", entry.get("well"))
        if raw_wells is None:
            raise ValidationError(f"Liquid '{name}' requires 'wells'")
        wells = resolve_well_refs(raw_wells, deck, well_order=well_order)
        vol = _finite_number(entry.get("volume"), f"Liquid '{name}' volume")
        if vol <= 0:
            raise ValidationError(f"Liquid '{name}' volume must be a positive number, got {vol!r}")
        liq_class = _parse_liquid_class_name(
            entry.get("liquid_class"), f"Liquid '{name}' liquid_class"
        )
        for ref in wells:
            if ref in owner:
                raise ValidationError(
                    f"Well '{ref.labware}:{ref.well}' is assigned to both liquid "
                    f"'{owner[ref]}' and '{name}'"
                )
            owner[ref] = name
        _check_well_capacity(wells, vol, deck, f"Liquid '{name}'")
        description = entry.get("description", "")
        liquids.append(
            LiquidSpec(
                name=name,
                description=str(description),
                wells=tuple(wells),
                volume=vol,
                liquid_class=liq_class,
            )
        )
    return tuple(liquids)


def _select_pipette(params: dict[str, Any], pipettes: dict[str, PipetteConfig]) -> PipetteConfig:
    mount = params.get("pipette")
    if mount is None:
        if len(pipettes) == 1:
            return next(iter(pipettes.values()))
        raise ValidationError(
            "Step must specify 'pipette' mount ('left' or 'right') when multiple pipettes are configured"
        )
    if mount not in pipettes:
        raise ValidationError(f"Unknown or unconfigured pipette mount '{mount}'")
    return pipettes[mount]


def _resolve_step_rheology(
    params: dict[str, Any],
    pip: PipetteConfig,
    volume_ul: float,
    step_label: str,
    *,
    source_wells: list[WellRef] | None = None,
    deck: dict[str, DeckItem] | None = None,
    well_liquid_classes: dict[WellRef, str] | None = None,
    allow_air_gap: bool = True,
) -> RheologySpec | None:
    """Parse and validate step-level or source-liquid-inherited rheology parameters."""
    explicit_lc = _parse_liquid_class_name(
        params.get("liquid_class"), f"{step_label} 'liquid_class'"
    )
    has_explicit_override = any(
        params.get(k) is not None for k in _RHEOLOGY_STEP_KEYS if k != "liquid_class"
    )

    inherited_lc: str | None = None
    if (
        explicit_lc is None
        and source_wells
        and deck is not None
        and well_liquid_classes
    ):
        expanded_src: list[WellRef] = []
        for s_ref in source_wells:
            s_wells, _ = _resolve_channel_target(s_ref, deck, pip.spec.channels, step_label)
            expanded_src.extend(s_wells)
        found = {
            well_liquid_classes[w]
            for w in expanded_src
            if w in well_liquid_classes
        }
        if len(found) == 1 and expanded_src and all(w in well_liquid_classes for w in expanded_src):
            cand = next(iter(found))
            if cand != "aqueous":
                inherited_lc = cand

    resolved_lc = explicit_lc or inherited_lc
    if resolved_lc is None and not has_explicit_override:
        return None
    if resolved_lc is None:
        resolved_lc = "aqueous"

    shared_rate: float | None = None
    if params.get("flow_rate") is not None:
        shared_rate = _finite_number(params["flow_rate"], f"{step_label} 'flow_rate'")
        if shared_rate <= 0:
            raise ValidationError(f"{step_label} 'flow_rate' must be positive, got {shared_rate!r}")

    asp_rate: float | None = shared_rate
    if params.get("aspirate_flow_rate") is not None:
        asp_rate = _finite_number(
            params["aspirate_flow_rate"], f"{step_label} 'aspirate_flow_rate'"
        )
        if asp_rate <= 0:
            raise ValidationError(
                f"{step_label} 'aspirate_flow_rate' must be positive, got {asp_rate!r}"
            )

    disp_rate: float | None = shared_rate
    if params.get("dispense_flow_rate") is not None:
        disp_rate = _finite_number(
            params["dispense_flow_rate"], f"{step_label} 'dispense_flow_rate'"
        )
        if disp_rate <= 0:
            raise ValidationError(
                f"{step_label} 'dispense_flow_rate' must be positive, got {disp_rate!r}"
            )

    air_gap_val: float | None = None
    if params.get("air_gap") is not None:
        air_gap_val = _finite_number(params["air_gap"], f"{step_label} 'air_gap'")
        if air_gap_val < 0:
            raise ValidationError(
                f"{step_label} 'air_gap' must be >= 0, got {air_gap_val!r}"
            )
        if air_gap_val >= pip.effective_max_volume - VOLUME_TOLERANCE:
            raise ValidationError(
                f"{step_label} 'air_gap' ({air_gap_val} uL) must be less than "
                f"pipette '{pip.model}' effective capacity {pip.effective_max_volume} uL"
            )

    touch_tip_val: bool | None = None
    if params.get("touch_tip") is not None:
        touch_tip_val = _parse_bool(params["touch_tip"], f"{step_label} 'touch_tip'")

    delay_val: float | None = None
    raw_delay = (
        params["aspirate_delay"]
        if params.get("aspirate_delay") is not None
        else params.get("settle_delay")
    )
    if raw_delay is not None:
        delay_key = "aspirate_delay" if params.get("aspirate_delay") is not None else "settle_delay"
        delay_val = _finite_number(raw_delay, f"{step_label} '{delay_key}'")
        if delay_val < 0:
            raise ValidationError(
                f"{step_label} '{delay_key}' must be >= 0, got {delay_val!r}"
            )

    return build_rheology_spec(
        pip,
        volume_ul,
        liquid_class=resolved_lc,
        aspirate_flow_rate=asp_rate,
        dispense_flow_rate=disp_rate,
        air_gap_ul=air_gap_val,
        touch_tip=touch_tip_val,
        aspirate_delay_s=delay_val,
        allow_air_gap=allow_air_gap,
    )


def _check_volume(
    volume: Any,
    pip: PipetteConfig,
    label: str,
    *,
    multi_aspirate: bool = False,
    air_gap: float = 0.0,
) -> tuple[float, tuple[tuple[float, ...], ...]]:
    """Validate volume against pipette + tip-rack bounds; return `(volume, stroke_groups)`."""
    vol = _finite_number(volume, f"{label} volume")
    if vol <= 0:
        raise ValidationError(f"{label} volume must be positive, got {vol}")
    max_vol = pip.effective_max_volume - air_gap
    min_vol = pip.spec.min_volume
    if max_vol <= 0:
        raise ValidationError(
            f"{label} air_gap {air_gap} uL leaves no stroke capacity on pipette '{pip.model}' "
            f"(effective max {pip.effective_max_volume} uL)"
        )
    if vol < min_vol - VOLUME_TOLERANCE:
        raise ValidationError(
            f"{label} volume {vol} uL is outside pipette '{pip.model}' range "
            f"[{min_vol}, {pip.effective_max_volume}] uL"
        )
    if vol > max_vol + VOLUME_TOLERANCE:
        if not multi_aspirate:
            notes: list[str] = []
            if pip.max_tip_volume is not None and pip.max_tip_volume < pip.spec.max_volume:
                notes.append(f"capped by {pip.max_tip_volume:g} uL tip rack")
            if air_gap > 0:
                notes.append(f"less {air_gap:g} uL air_gap")
            note_str = f" ({', '.join(notes)})" if notes else ""
            raise ValidationError(
                f"{label} volume {vol} uL is outside pipette '{pip.model}' range "
                f"[{min_vol}, {max_vol}] uL{note_str}"
            )
        groups = plan_transfer_strokes(
            vol,
            pip.spec.max_volume,
            pip.effective_max_volume,
            air_gap=air_gap,
        )
        flat = [s_vol for grp in groups for s_vol in grp]
        for s_vol in flat:
            if s_vol < min_vol - VOLUME_TOLERANCE:
                strokes_fmt = ", ".join(_fmt_ul(x) for x in flat)
                raise ValidationError(
                    f"{label} volume {vol} uL splits into Opentrons strokes [{strokes_fmt}] uL; "
                    f"stroke {_fmt_ul(s_vol)} uL falls below pipette '{pip.model}' "
                    f"minimum volume {min_vol} uL"
                )
        return vol, groups
    return vol, ((vol,),)


def _check_well_capacity(
    wells: list[WellRef],
    volume: float,
    deck: dict[str, DeckItem],
    label: str,
) -> None:
    for ref in wells:
        max_vol = deck[ref.labware].spec.max_volume_for(ref.well)
        if volume > max_vol + VOLUME_TOLERANCE:
            raise ValidationError(
                f"{label} volume {volume} uL exceeds well capacity {max_vol} uL "
                f"on '{ref.labware}:{ref.well}'"
            )


def _extract_step(raw_step: Any, index: int) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw_step, dict) or not raw_step:
        raise ValidationError(f"Step {index} must be a mapping")
    if "type" in raw_step:
        kind = raw_step["type"]
        params = {k: v for k, v in raw_step.items() if k != "type"}
        return str(kind), params
    if len(raw_step) != 1:
        known_step_names = set(_STEP_ALLOWED_KEYS)
        present_steps = [k for k in raw_step if k in known_step_names]
        if len(present_steps) == 1:
            step_name = present_steps[0]
            extra = [k for k in raw_step if k != step_name]
            raise ValidationError(
                f"Step {index} ('{step_name}') has stray sibling key(s) {extra}; "
                f"indent parameters inside '{step_name}:'"
            )
        raise ValidationError(
            f"Step {index} must either have a 'type' key or a single step-name key"
        )
    kind, body = next(iter(raw_step.items()))
    if kind == "comment" and isinstance(body, str):
        return kind, {"text": body}
    if kind in ("delay", "pause") and isinstance(body, (int, float)) and not isinstance(body, bool):
        return kind, {"seconds": body}
    if not isinstance(body, dict):
        raise ValidationError(f"Step {index} ('{kind}') body must be a mapping")
    return str(kind), body


def _parse_duration_params(params: dict[str, Any], kind: str, idx: int) -> DelayStep:
    has_sec = "seconds" in params and params["seconds"] is not None
    has_min = "minutes" in params and params["minutes"] is not None
    if not has_sec and not has_min:
        raise ValidationError(
            f"Step {idx} ({kind}) requires 'seconds' and/or 'minutes'"
        )
    sec_val = 0.0
    min_val = 0.0
    if has_sec:
        sec_val = _finite_number(params["seconds"], f"{kind} 'seconds'")
        if sec_val < 0:
            raise ValidationError(f"{kind} 'seconds' must be >= 0, got {sec_val!r}")
    if has_min:
        min_val = _finite_number(params["minutes"], f"{kind} 'minutes'")
        if min_val < 0:
            raise ValidationError(f"{kind} 'minutes' must be >= 0, got {min_val!r}")
    return DelayStep(seconds=sec_val, minutes=min_val)


class _TipStateTracker:
    """Manage mounted-tip state and validate tip rack capacity."""

    def __init__(self, deck: dict[str, DeckItem], pipettes: dict[str, PipetteConfig]) -> None:
        self.has_tip: dict[str, bool] = {mount: False for mount in pipettes}
        self.deck = deck
        self.rack_capacity: dict[str, int] = {
            alias: len(item.spec.wells)
            for alias, item in deck.items()
            if item.spec.is_tiprack
        }
        self.rack_remaining: dict[str, int] = dict(self.rack_capacity)
        self.tips_used_per_mount: dict[str, int] = {mount: 0 for mount in pipettes}
        self.pipettes = pipettes

    def consume_tips(self, mount: str, pickup_count: int) -> None:
        if pickup_count <= 0:
            return
        channels = self.pipettes[mount].spec.channels
        physical_tips = pickup_count * channels
        needed = physical_tips
        for rack_alias in self.pipettes[mount].tip_racks:
            avail = self.rack_remaining[rack_alias]
            if channels > 1:
                avail = (avail // channels) * channels
            if avail >= needed:
                self.rack_remaining[rack_alias] -= needed
                self.tips_used_per_mount[mount] += physical_tips
                return
            needed -= avail
            self.rack_remaining[rack_alias] -= avail
        total_capacity = sum(
            len(self.deck[r].spec.wells) for r in self.pipettes[mount].tip_racks
        )
        raise ValidationError(
            f"Not enough tips for pipette '{mount}': needed {self.tips_used_per_mount[mount] + physical_tips} "
            f"tips, capacity is {total_capacity} tips across {list(self.pipettes[mount].tip_racks)}"
        )

    def ensure_no_tip(self, mount: str, out: list[LoweredStep]) -> None:
        if self.has_tip[mount]:
            out.append(DropTipStep(pipette=mount))
            self.has_tip[mount] = False

    def ensure_has_tip(self, mount: str, out: list[LoweredStep]) -> None:
        if not self.has_tip[mount]:
            self.consume_tips(mount, 1)
            out.append(PickUpTipStep(pipette=mount))
            self.has_tip[mount] = True

    @property
    def tips_used_per_rack(self) -> dict[str, int]:
        return {
            alias: self.rack_capacity[alias] - self.rack_remaining[alias]
            for alias in self.rack_capacity
        }


def _fmt_ul(volume: float) -> str:
    return f"{round(volume, 4):g}"


def _well_label(ref: WellRef) -> str:
    return f"'{ref.labware}:{ref.well}'"


class _WellVolumeTracker:
    """Track well volumes, peak occupancy, and per-tip reagent species lineage."""

    def __init__(
        self,
        deck: dict[str, DeckItem],
        pipettes: dict[str, PipetteConfig],
        liquids: tuple[LiquidSpec, ...],
    ) -> None:
        self.deck = deck
        self.pipettes = pipettes
        self.initial_volumes: dict[WellRef, float] = {}
        self.volumes: dict[WellRef, float] = {}
        self.peak_volumes: dict[WellRef, float] = {}
        self.well_species: dict[WellRef, set[str]] = {}
        self.tip_species: dict[str, set[str]] = {mount: set() for mount in pipettes}
        for liquid in liquids:
            for ref in liquid.wells:
                self.initial_volumes[ref] = liquid.volume
                self.volumes[ref] = liquid.volume
                self.peak_volumes[ref] = liquid.volume
                self.well_species[ref] = {liquid.name}

    def _aspirate_species(
        self,
        mount: str,
        ref: WellRef,
        label: str,
        *,
        allow_contamination: bool,
    ) -> None:
        src_sp = self.well_species.get(ref, set())
        tip_sp = self.tip_species[mount]
        if tip_sp and not (tip_sp <= src_sp) and not allow_contamination:
            foreign = sorted(tip_sp - src_sp)
            present = sorted(src_sp) if src_sp else ["<unlabeled>"]
            raise ValidationError(
                f"{label}: cross-contamination on pipette '{mount}': tip carrying {foreign} "
                f"cannot aspirate from {_well_label(ref)} containing {present} "
                "(use new_tip: 'always', dispense_position: 'top', or set allow_contamination: true)"
            )
        tip_sp.update(src_sp)

    def _dispense_species(
        self,
        mount: str,
        ref: WellRef,
        label: str,
        *,
        dispense_position: str = "bottom",
        expected_src_species: set[str] | None = None,
        allow_contamination: bool = False,
    ) -> None:
        tip_sp = self.tip_species[mount]
        dst_existing_sp = set(self.well_species.get(ref, set()))
        was_nonempty = self.volumes.get(ref, 0.0) > VOLUME_TOLERANCE

        if (
            expected_src_species is not None
            and tip_sp
            and not (tip_sp <= (expected_src_species | dst_existing_sp))
            and not allow_contamination
        ):
            foreign = sorted(tip_sp - expected_src_species - dst_existing_sp)
            present = sorted(dst_existing_sp) if dst_existing_sp else ["<empty>"]
            raise ValidationError(
                f"{label}: cross-contamination on pipette '{mount}': tip carrying {foreign} "
                f"cannot dispense into {_well_label(ref)} containing {present} "
                "(use new_tip: 'always', dispense_position: 'top', or set allow_contamination: true)"
            )

        if tip_sp:
            self.well_species.setdefault(ref, set()).update(tip_sp)
        if dispense_position == "bottom" and was_nonempty:
            tip_sp.update(dst_existing_sp if dst_existing_sp else {"<unlabeled>"})

    def _mix_species(
        self,
        mount: str,
        ref: WellRef,
        label: str,
        *,
        allow_contamination: bool,
    ) -> None:
        well_sp = self.well_species.get(ref, set())
        tip_sp = self.tip_species[mount]
        if tip_sp and not (tip_sp <= well_sp) and not allow_contamination:
            foreign = sorted(tip_sp - well_sp)
            present = sorted(well_sp) if well_sp else ["<unlabeled>"]
            raise ValidationError(
                f"{label}: cross-contamination on pipette '{mount}': tip carrying {foreign} "
                f"cannot mix {_well_label(ref)} containing {present} "
                "(use new_tip: 'always' or set allow_contamination: true)"
            )
        tip_sp.update(well_sp)
        if tip_sp:
            self.well_species.setdefault(ref, set()).update(tip_sp)

    def remove(self, ref: WellRef, volume: float, label: str) -> None:
        current = self.volumes.get(ref, 0.0)
        if volume > current + VOLUME_TOLERANCE:
            raise ValidationError(
                f"{label}: aspirating {_fmt_ul(volume)} uL from {_well_label(ref)}, which holds "
                f"only {_fmt_ul(current)} uL (declare starting volumes under 'liquids')"
            )
        self.volumes[ref] = max(0.0, current - volume)
        self.peak_volumes.setdefault(ref, current)

    def add(self, ref: WellRef, volume: float, label: str) -> None:
        new_volume = self.volumes.get(ref, 0.0) + volume
        max_vol = self.deck[ref.labware].spec.max_volume_for(ref.well)
        if new_volume > max_vol + VOLUME_TOLERANCE:
            raise ValidationError(
                f"{label}: dispensing {_fmt_ul(volume)} uL brings {_well_label(ref)} to "
                f"{_fmt_ul(new_volume)} uL, which exceeds well capacity {_fmt_ul(max_vol)} uL"
            )
        self.volumes[ref] = new_volume
        if new_volume > self.peak_volumes.get(ref, 0.0):
            self.peak_volumes[ref] = new_volume

    def check_mix(self, ref: WellRef, volume: float, label: str) -> None:
        current = self.volumes.get(ref, 0.0)
        if volume > current + VOLUME_TOLERANCE:
            raise ValidationError(
                f"{label}: mix volume {_fmt_ul(volume)} uL is more than the "
                f"{_fmt_ul(current)} uL in {_well_label(ref)}"
            )
        self.peak_volumes.setdefault(ref, current)

    def apply(self, step: LoweredStep, label: str, *, allow_contamination: bool = False) -> None:
        if isinstance(step, (PickUpTipStep, DropTipStep)):
            self.tip_species[step.pipette] = set()
            return

        if isinstance(step, TransferStep):
            pip = self.pipettes[step.pipette]
            channels = pip.spec.channels
            air_gap_ul = step.rheology.air_gap_ul if step.rheology is not None else 0.0
            stroke_groups = plan_transfer_strokes(
                step.volume,
                pip.spec.max_volume,
                pip.effective_max_volume,
                air_gap=air_gap_ul,
            )

            srcs, dsts = list(step.src), list(step.dst)
            if len(srcs) == 1:
                srcs = srcs * len(dsts)
            elif len(dsts) == 1:
                dsts = dsts * len(srcs)

            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()
            for src, dst in zip(srcs, dsts):
                src_wells, src_mult = _resolve_channel_target(src, self.deck, channels, label)
                dst_wells, dst_mult = _resolve_channel_target(dst, self.deck, channels, label)
                pair_src_sp: set[str] = set()
                for s_ref in src_wells:
                    pair_src_sp.update(self.well_species.get(s_ref, set()))

                for stroke_group in stroke_groups:
                    if step.new_tip == "always":
                        self.tip_species[step.pipette] = set()
                    for stroke_vol in stroke_group:
                        for s_ref in src_wells:
                            self._aspirate_species(
                                step.pipette,
                                s_ref,
                                label,
                                allow_contamination=allow_contamination,
                            )
                            self.remove(s_ref, stroke_vol * src_mult, label)
                        for d_ref in dst_wells:
                            self._dispense_species(
                                step.pipette,
                                d_ref,
                                label,
                                dispense_position=step.dispense_position,
                                expected_src_species=pair_src_sp,
                                allow_contamination=allow_contamination,
                            )
                            self.add(d_ref, stroke_vol * dst_mult, label)
                        if step.mix_after is not None:
                            for d_ref in dst_wells:
                                self.check_mix(d_ref, step.mix_after[1] * dst_mult, label)
                                self._mix_species(
                                    step.pipette,
                                    d_ref,
                                    label,
                                    allow_contamination=allow_contamination,
                                )
                    if step.new_tip == "always":
                        self.tip_species[step.pipette] = set()
            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()

        elif isinstance(step, DistributeStep):
            pip = self.pipettes[step.pipette]
            channels = pip.spec.channels
            disp_vol = (
                pip.spec.min_volume if step.disposal_volume is None else step.disposal_volume
            )
            air_gap_ul = step.rheology.air_gap_ul if step.rheology is not None else 0.0
            chunks = plan_distribute_chunks(
                len(step.dst),
                step.volume,
                pip.effective_max_volume,
                disp_vol,
                air_gap=air_gap_ul,
            )
            total_aspirated = step.volume * len(step.dst) + disp_vol * len(chunks)

            src_wells, src_mult = _resolve_channel_target(step.src, self.deck, channels, label)
            src_sp_union: set[str] = set()
            for s_ref in src_wells:
                src_sp_union.update(self.well_species.get(s_ref, set()))
                current = self.volumes.get(s_ref, 0.0)
                if total_aspirated * src_mult > current + VOLUME_TOLERANCE:
                    raise ValidationError(
                        f"{label}: aspirating {_fmt_ul(total_aspirated * src_mult)} uL from "
                        f"{_well_label(s_ref)}, which holds only {_fmt_ul(current)} uL "
                        "(declare starting volumes under 'liquids')"
                    )

            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()
            idx = 0
            for chunk_len in chunks:
                chunk_dsts = step.dst[idx : idx + chunk_len]
                chunk_asp = step.volume * chunk_len + disp_vol
                for s_ref in src_wells:
                    self._aspirate_species(
                        step.pipette, s_ref, label, allow_contamination=allow_contamination
                    )
                    self.remove(s_ref, chunk_asp * src_mult, label)
                for dst in chunk_dsts:
                    dst_wells, dst_mult = _resolve_channel_target(dst, self.deck, channels, label)
                    for d_ref in dst_wells:
                        self._dispense_species(
                            step.pipette,
                            d_ref,
                            label,
                            dispense_position=step.dispense_position,
                            expected_src_species=src_sp_union,
                            allow_contamination=allow_contamination,
                        )
                        self.add(d_ref, step.volume * dst_mult, label)
                idx += chunk_len
            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()

        elif isinstance(step, MixStep):
            pip = self.pipettes[step.pipette]
            channels = pip.spec.channels
            for well in step.wells:
                w_refs, w_mult = _resolve_channel_target(well, self.deck, channels, label)
                for w_ref in w_refs:
                    self.check_mix(w_ref, step.volume * w_mult, label)
                    self._mix_species(
                        step.pipette, w_ref, label, allow_contamination=allow_contamination
                    )

        elif isinstance(step, DiscardStep):
            pip = self.pipettes[step.pipette]
            channels = pip.spec.channels
            src_wells, src_mult = _resolve_channel_target(step.src, self.deck, channels, label)
            for stroke_vol in step.effective_strokes():
                for s_ref in src_wells:
                    self._aspirate_species(
                        step.pipette, s_ref, label, allow_contamination=allow_contamination
                    )
                    self.remove(s_ref, stroke_vol * src_mult, label)

    def summaries(self) -> dict[str, WellSummary]:
        all_wells = set(self.initial_volumes) | set(self.volumes) | set(self.peak_volumes)
        out: dict[str, WellSummary] = {}
        for ref in sorted(all_wells, key=lambda r: (r.labware, r.well)):
            init_v = self.initial_volumes.get(ref, 0.0)
            final_v = self.volumes.get(ref, 0.0)
            peak_v = max(init_v, final_v, self.peak_volumes.get(ref, 0.0))
            cap_v = self.deck[ref.labware].spec.max_volume_for(ref.well)
            sp = tuple(sorted(self.well_species.get(ref, set())))
            out[ref.key()] = WellSummary(
                initial_ul=init_v,
                peak_ul=peak_v,
                final_ul=final_v,
                capacity_ul=cap_v,
                species=sp,
            )
        return out


def _lower_transfer(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
    *,
    well_liquid_classes: dict[WellRef, str] | None = None,
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    multi_aspirate = _parse_bool(params.get("multi_aspirate"), "transfer 'multi_aspirate'")
    dispense_position = _parse_dispense_position(
        params.get("dispense_position"), "transfer 'dispense_position'"
    )
    raw_src = params.get("src", params.get("source"))
    raw_dst = params.get("dst", params.get("dest", params.get("destination")))
    if raw_src is None or raw_dst is None:
        raise ValidationError("transfer step requires 'src' and 'dst'")
    src = resolve_well_refs(raw_src, deck, well_order=well_order)
    dst = resolve_well_refs(raw_dst, deck, well_order=well_order)
    _reject_staging_well_refs(src + dst, deck, "transfer")
    for s_ref in src:
        _resolve_channel_target(s_ref, deck, pip.spec.channels, "transfer")
    for d_ref in dst:
        _resolve_channel_target(d_ref, deck, pip.spec.channels, "transfer")
    if len(src) > 1 and len(dst) > 1 and len(src) != len(dst):
        raise ValidationError(
            f"transfer source count ({len(src)}) and destination count ({len(dst)}) mismatch"
        )

    raw_vol = _finite_number(params.get("volume"), "transfer volume")
    if raw_vol <= 0:
        raise ValidationError(f"transfer volume must be positive, got {raw_vol}")

    rheology = _resolve_step_rheology(
        params,
        pip,
        raw_vol,
        "transfer",
        source_wells=src,
        deck=deck,
        well_liquid_classes=well_liquid_classes,
    )
    air_gap_ul = rheology.air_gap_ul if rheology is not None else 0.0
    if (
        rheology is not None
        and params.get("air_gap") is None
        and not multi_aspirate
        and air_gap_ul > 0.0
        and raw_vol + air_gap_ul > pip.effective_max_volume + VOLUME_TOLERANCE
        and raw_vol <= pip.effective_max_volume - 1.0
    ):
        capped_ag = round(max(1.0, pip.effective_max_volume - raw_vol), 2)
        rheology = RheologySpec(
            liquid_class=rheology.liquid_class,
            aspirate_flow_rate=rheology.aspirate_flow_rate,
            dispense_flow_rate=rheology.dispense_flow_rate,
            air_gap_ul=capped_ag,
            touch_tip=rheology.touch_tip,
            aspirate_delay_s=rheology.aspirate_delay_s,
        )
        air_gap_ul = capped_ag

    vol, stroke_groups = _check_volume(
        raw_vol,
        pip,
        "transfer",
        multi_aspirate=multi_aspirate,
        air_gap=air_gap_ul,
    )
    new_tip = params.get("new_tip", "once")
    if new_tip not in _VALID_NEW_TIP:
        raise ValidationError(f"Invalid new_tip '{new_tip}' in transfer")

    mix_after: tuple[int, float] | None = None
    if "mix_after" in params and params["mix_after"] is not None:
        ma = params["mix_after"]
        if isinstance(ma, dict):
            _check_allowed_keys(ma, _MIX_AFTER_KEYS, "transfer 'mix_after'")
            reps = ma.get("reps", ma.get("repetitions"))
            mvol = ma.get("volume")
        elif isinstance(ma, (list, tuple)) and len(ma) == 2:
            reps, mvol = ma
        else:
            raise ValidationError("transfer 'mix_after' must be {reps, volume} or [reps, volume]")
        checked_reps = _finite_int(reps, "mix_after reps", min_value=1)
        checked_mvol, _ = _check_volume(mvol, pip, "mix_after")
        mix_after = (checked_reps, checked_mvol)

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
    elif new_tip == "once":
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)
    else:
        tracker.ensure_no_tip(pip.mount, out)
        pair_count = max(len(src), len(dst))
        tracker.consume_tips(pip.mount, pair_count * len(stroke_groups))

    out.append(
        TransferStep(
            pipette=pip.mount,
            src=tuple(src),
            dst=tuple(dst),
            volume=vol,
            new_tip=new_tip,
            mix_after=mix_after,
            dispense_position=dispense_position,
            rheology=rheology,
        )
    )


def _lower_distribute(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
    *,
    well_liquid_classes: dict[WellRef, str] | None = None,
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    dispense_position = _parse_dispense_position(
        params.get("dispense_position"), "distribute 'dispense_position'"
    )
    raw_src = params.get("src", params.get("source"))
    raw_dst = params.get("dst", params.get("dest", params.get("destination")))
    if raw_src is None or raw_dst is None:
        raise ValidationError("distribute step requires 'src' and 'dst'")
    src = resolve_well_refs(raw_src, deck, well_order=well_order)
    if len(src) != 1:
        raise ValidationError(f"distribute requires a single source well, got {len(src)}")
    dst = resolve_well_refs(raw_dst, deck, well_order=well_order)
    _reject_staging_well_refs(src + dst, deck, "distribute")
    _resolve_channel_target(src[0], deck, pip.spec.channels, "distribute")
    for d_ref in dst:
        _resolve_channel_target(d_ref, deck, pip.spec.channels, "distribute")

    vol, _ = _check_volume(params.get("volume"), pip, "distribute")
    new_tip = params.get("new_tip", "once")
    if new_tip not in ("once", "never"):
        raise ValidationError(
            f"distribute supports new_tip 'once' or 'never', got '{new_tip}'"
        )

    disposal_volume: float | None = None
    if "disposal_volume" in params and params["disposal_volume"] is not None:
        disposal_volume = _finite_number(
            params["disposal_volume"], "distribute 'disposal_volume'"
        )
        if disposal_volume < 0:
            raise ValidationError(
                f"distribute 'disposal_volume' must be a number >= 0, got {disposal_volume!r}"
            )

    rheology = _resolve_step_rheology(
        params,
        pip,
        vol,
        "distribute",
        source_wells=src,
        deck=deck,
        well_liquid_classes=well_liquid_classes,
    )
    air_gap_ul = rheology.air_gap_ul if rheology is not None else 0.0
    eff_disp = pip.spec.min_volume if disposal_volume is None else disposal_volume
    if (
        rheology is not None
        and params.get("air_gap") is None
        and air_gap_ul > 0.0
        and vol + eff_disp + air_gap_ul > pip.effective_max_volume + VOLUME_TOLERANCE
        and vol + eff_disp <= pip.effective_max_volume - 1.0
    ):
        capped_ag = round(max(1.0, pip.effective_max_volume - vol - eff_disp), 2)
        rheology = RheologySpec(
            liquid_class=rheology.liquid_class,
            aspirate_flow_rate=rheology.aspirate_flow_rate,
            dispense_flow_rate=rheology.dispense_flow_rate,
            air_gap_ul=capped_ag,
            touch_tip=rheology.touch_tip,
            aspirate_delay_s=rheology.aspirate_delay_s,
        )
        air_gap_ul = capped_ag

    if vol + eff_disp + air_gap_ul > pip.effective_max_volume + VOLUME_TOLERANCE:
        ag_note = f" plus air_gap {air_gap_ul} uL" if air_gap_ul > 0 else ""
        raise ValidationError(
            f"distribute volume {vol} uL plus disposal_volume {eff_disp} uL{ag_note} "
            f"exceeds pipette '{pip.model}' effective capacity {pip.effective_max_volume} uL "
            "(use transfer or set disposal_volume: 0)"
        )

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
    else:
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)

    out.append(
        DistributeStep(
            pipette=pip.mount,
            src=src[0],
            dst=tuple(dst),
            volume=vol,
            new_tip=new_tip,
            disposal_volume=disposal_volume,
            dispense_position=dispense_position,
            rheology=rheology,
        )
    )


def _lower_mix(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
    *,
    well_liquid_classes: dict[WellRef, str] | None = None,
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    raw_wells = params.get("well", params.get("wells"))
    if raw_wells is None:
        raise ValidationError("mix step requires 'well' or 'wells'")
    wells = resolve_well_refs(raw_wells, deck, well_order=well_order)
    _reject_staging_well_refs(wells, deck, "mix")
    for w_ref in wells:
        _resolve_channel_target(w_ref, deck, pip.spec.channels, "mix")
    vol, _ = _check_volume(params.get("volume"), pip, "mix")
    reps = _finite_int(
        params.get("reps", params.get("repetitions")),
        "mix 'reps'",
        min_value=1,
    )
    new_tip = params.get("new_tip", "once")
    if new_tip not in _VALID_NEW_TIP:
        raise ValidationError(f"Invalid new_tip '{new_tip}' in mix")
    rheology = _resolve_step_rheology(
        params,
        pip,
        vol,
        "mix",
        source_wells=wells,
        deck=deck,
        well_liquid_classes=well_liquid_classes,
        allow_air_gap=False,
    )

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
        out.append(
            MixStep(pipette=pip.mount, wells=tuple(wells), volume=vol, reps=reps, rheology=rheology)
        )
    elif new_tip == "once":
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)
        out.append(PickUpTipStep(pipette=pip.mount))
        out.append(
            MixStep(pipette=pip.mount, wells=tuple(wells), volume=vol, reps=reps, rheology=rheology)
        )
        out.append(DropTipStep(pipette=pip.mount))
    else:
        tracker.ensure_no_tip(pip.mount, out)
        for well in wells:
            tracker.consume_tips(pip.mount, 1)
            out.append(PickUpTipStep(pipette=pip.mount))
            out.append(
                MixStep(pipette=pip.mount, wells=(well,), volume=vol, reps=reps, rheology=rheology)
            )
            out.append(DropTipStep(pipette=pip.mount))


def _lower_discard(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    multi_aspirate = _parse_bool(params.get("multi_aspirate"), "discard 'multi_aspirate'")
    raw_src = params.get("src", params.get("source"))
    if raw_src is None:
        raise ValidationError("discard step requires 'src'")
    srcs = resolve_well_refs(raw_src, deck, well_order=well_order)
    _reject_staging_well_refs(srcs, deck, "discard")
    for s_ref in srcs:
        _resolve_channel_target(s_ref, deck, pip.spec.channels, "discard")
    vol, stroke_groups = _check_volume(
        params.get("volume"), pip, "discard", multi_aspirate=multi_aspirate
    )
    flat_strokes = tuple(s_vol for grp in stroke_groups for s_vol in grp)
    new_tip = params.get("new_tip", "once")
    if new_tip not in _VALID_NEW_TIP:
        raise ValidationError(f"Invalid new_tip '{new_tip}' in discard")

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
        for s_ref in srcs:
            out.append(
                DiscardStep(
                    pipette=pip.mount,
                    src=s_ref,
                    volume=vol,
                    strokes=len(flat_strokes),
                    stroke_volumes=flat_strokes,
                )
            )
    elif new_tip == "once":
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)
        out.append(PickUpTipStep(pipette=pip.mount))
        for s_ref in srcs:
            out.append(
                DiscardStep(
                    pipette=pip.mount,
                    src=s_ref,
                    volume=vol,
                    strokes=len(flat_strokes),
                    stroke_volumes=flat_strokes,
                )
            )
        out.append(DropTipStep(pipette=pip.mount))
    else:
        tracker.ensure_no_tip(pip.mount, out)
        for s_ref in srcs:
            tracker.consume_tips(pip.mount, 1)
            out.append(PickUpTipStep(pipette=pip.mount))
            out.append(
                DiscardStep(
                    pipette=pip.mount,
                    src=s_ref,
                    volume=vol,
                    strokes=len(flat_strokes),
                    stroke_volumes=flat_strokes,
                )
            )
            out.append(DropTipStep(pipette=pip.mount))


def _lower_dilution_series(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
    *,
    well_liquid_classes: dict[WellRef, str] | None = None,
) -> None:
    pip = _select_pipette(params, pipettes)
    multi_aspirate = _parse_bool(
        params.get("multi_aspirate"), "dilution_series 'multi_aspirate'"
    )
    raw_source = params.get("source", params.get("src"))
    raw_diluent = params.get("diluent")
    raw_first = params.get("first_well")
    count_raw = params.get("n", params.get("count", params.get("wells")))
    factor_raw = params.get("factor")
    final_vol_raw = params.get("final_volume")
    direction = params.get("direction", "row")
    mix_reps_raw = params.get("mix_reps", 3)
    precision_raw = params.get("precision", 2)

    if raw_source is None or raw_diluent is None or raw_first is None:
        raise ValidationError("dilution_series requires 'source', 'diluent', and 'first_well'")
    count = _finite_int(count_raw, "dilution_series 'n'", min_value=2)
    factor = _finite_number(factor_raw, "dilution_series 'factor'")
    if factor <= 1.0:
        raise ValidationError(f"dilution_series 'factor' must be > 1, got {factor_raw!r}")
    mix_reps = _finite_int(mix_reps_raw, "dilution_series 'mix_reps'", min_value=1)
    precision = _finite_int(precision_raw, "dilution_series 'precision'")
    if not (0 <= precision <= 4):
        raise ValidationError(
            f"dilution_series 'precision' must be an integer between 0 and 4, got {precision_raw!r}"
        )

    source_refs = resolve_well_refs(raw_source, deck)
    diluent_refs = resolve_well_refs(raw_diluent, deck)
    if len(source_refs) != 1 or len(diluent_refs) != 1:
        raise ValidationError("dilution_series 'source' and 'diluent' must each be a single well")

    series_wells = resolve_serial_wells(raw_first, count, deck, direction=str(direction))
    _reject_staging_well_refs(source_refs + diluent_refs + series_wells, deck, "dilution_series")
    for ref in source_refs + diluent_refs + series_wells:
        _resolve_channel_target(ref, deck, pip.spec.channels, "dilution_series")
    if source_refs[0] == diluent_refs[0]:
        raise ValidationError(
            f"dilution_series 'source' and 'diluent' must be distinct wells, "
            f"both got {_well_label(source_refs[0])}"
        )
    if source_refs[0] in series_wells:
        raise ValidationError(
            f"dilution_series 'source' well {_well_label(source_refs[0])} overlaps with series_wells"
        )
    if diluent_refs[0] in series_wells:
        raise ValidationError(
            f"dilution_series 'diluent' well {_well_label(diluent_refs[0])} overlaps with series_wells"
        )

    final_vol, _ = _check_volume(final_vol_raw, pip, "dilution_series final_volume")
    if final_vol + pip.spec.min_volume > pip.effective_max_volume + VOLUME_TOLERANCE:
        raise ValidationError(
            f"dilution_series final_volume {final_vol} uL plus distribute disposal_volume "
            f"{pip.spec.min_volume} uL exceeds pipette '{pip.model}' range "
            f"[{pip.spec.min_volume}, {pip.effective_max_volume}] uL"
        )

    raw_transfer_vol = final_vol / (factor - 1.0)
    transfer_vol = round(raw_transfer_vol, precision)
    if abs(raw_transfer_vol - transfer_vol) > 1e-9:
        warnings.warn(
            f"dilution_series transfer_volume {raw_transfer_vol:.6f} uL is non-terminating at "
            f"precision={precision}; rounded to {transfer_vol} uL",
            UserWarning,
            stacklevel=3,
        )
    initial_vol = round(final_vol + transfer_vol, precision)

    rheo_step = _resolve_step_rheology(
        params,
        pip,
        transfer_vol if transfer_vol > 0 else final_vol,
        "dilution_series",
        source_wells=source_refs,
        deck=deck,
        well_liquid_classes=well_liquid_classes,
        allow_air_gap=False,
    )

    _, discard_groups = _check_volume(
        transfer_vol,
        pip,
        "dilution_series transfer_volume",
        multi_aspirate=multi_aspirate,
    )
    discard_flat = tuple(s_vol for grp in discard_groups for s_vol in grp)
    _check_volume(
        initial_vol,
        pip,
        "dilution_series initial_volume",
        multi_aspirate=multi_aspirate,
    )
    all_expanded_series: list[WellRef] = []
    for s_ref in series_wells:
        exp_wells, _ = _resolve_channel_target(s_ref, deck, pip.spec.channels, "dilution_series")
        all_expanded_series.extend(exp_wells)
    _check_well_capacity(all_expanded_series, initial_vol, deck, "dilution_series")

    mix_vol = min(final_vol, pip.effective_max_volume)
    mix_vol = max(mix_vol, pip.spec.min_volume)

    # 1. Pre-fill diluent in wells[1:] with one tip.
    tracker.ensure_no_tip(pip.mount, out)
    tracker.consume_tips(pip.mount, 1)
    out.append(
        DistributeStep(
            pipette=pip.mount,
            src=diluent_refs[0],
            dst=tuple(series_wells[1:]),
            volume=final_vol,
            new_tip="once",
        )
    )

    # 2. Load initial stock into wells[0] with one tip.
    tracker.consume_tips(pip.mount, 1)
    out.append(
        TransferStep(
            pipette=pip.mount,
            src=(source_refs[0],),
            dst=(series_wells[0],),
            volume=initial_vol,
            new_tip="once",
            rheology=rheo_step,
        )
    )

    # 3. Serial cascade from high concentration to low concentration.
    # Single-stroke transfers reuse one tip down the gradient; multi-stroke
    # transfers use `new_tip='always'` so stroke 2 never carries mixed diluent
    # from wells[idx + 1] back into wells[idx].
    if len(discard_flat) == 1:
        tracker.consume_tips(pip.mount, 1)
        out.append(PickUpTipStep(pipette=pip.mount))
        for idx in range(len(series_wells) - 1):
            out.append(
                TransferStep(
                    pipette=pip.mount,
                    src=(series_wells[idx],),
                    dst=(series_wells[idx + 1],),
                    volume=transfer_vol,
                    new_tip="never",
                    mix_after=(mix_reps, mix_vol),
                    rheology=rheo_step,
                )
            )
        out.append(
            DiscardStep(
                pipette=pip.mount,
                src=series_wells[-1],
                volume=transfer_vol,
                strokes=len(discard_flat),
                stroke_volumes=discard_flat,
            )
        )
        out.append(DropTipStep(pipette=pip.mount))
    else:
        for idx in range(len(series_wells) - 1):
            tracker.consume_tips(pip.mount, len(discard_groups))
            out.append(
                TransferStep(
                    pipette=pip.mount,
                    src=(series_wells[idx],),
                    dst=(series_wells[idx + 1],),
                    volume=transfer_vol,
                    new_tip="always",
                    mix_after=(mix_reps, mix_vol),
                    rheology=rheo_step,
                )
            )
        tracker.consume_tips(pip.mount, 1)
        out.append(PickUpTipStep(pipette=pip.mount))
        out.append(
            DiscardStep(
                pipette=pip.mount,
                src=series_wells[-1],
                volume=transfer_vol,
                strokes=len(discard_flat),
                stroke_volumes=discard_flat,
            )
        )
        out.append(DropTipStep(pipette=pip.mount))


def parse_and_validate(data: dict[str, Any]) -> ProtocolSpec:
    """Validate raw protocol dictionary and lower high-level steps."""
    if not isinstance(data, dict):
        raise ValidationError("Protocol root must be a mapping/object")
    _check_allowed_keys(data, _ROOT_KEYS, "protocol root")
    protocol_allow_contam = _parse_bool(
        data.get("allow_contamination"), "protocol 'allow_contamination'"
    )

    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValidationError("'metadata' must be a mapping")
    _check_allowed_keys(metadata, _METADATA_KEYS, "'metadata'")
    name = str(metadata.get("name", metadata.get("protocolName", "Stepbot Protocol")))
    description = str(metadata.get("description", ""))
    robot_type, api_level, trash_slot = _resolve_robot_and_api_level(data, metadata)

    deck = _parse_deck(data.get("deck"), robot_type=robot_type, trash_slot=trash_slot)
    pipettes = _parse_pipettes(data.get("pipettes"), deck, robot_type=robot_type)
    liquids = _parse_liquids(data.get("liquids"), deck)
    if liquids and parse_api_level(api_level) < _MIN_LIQUID_API_LEVEL:
        raise ValidationError(
            f"'liquids' requires apiLevel {_MIN_LIQUID_API_LEVEL[0]}.{_MIN_LIQUID_API_LEVEL[1]} "
            f"or newer, got '{api_level}'"
        )

    well_liquid_classes: dict[WellRef, str] = {}
    for liq in liquids:
        if liq.liquid_class is not None:
            for ref in liq.wells:
                well_liquid_classes[ref] = liq.liquid_class

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValidationError("'steps' must be a non-empty list")

    tracker = _TipStateTracker(deck, pipettes)
    volumes = _WellVolumeTracker(deck, pipettes, liquids)
    lowered: list[LoweredStep] = []
    origins: list[StepOrigin] = []

    for idx, raw_step in enumerate(raw_steps, start=1):
        kind, params = _extract_step(raw_step, idx)
        if kind not in _STEP_ALLOWED_KEYS:
            matches = difflib.get_close_matches(kind, sorted(_STEP_ALLOWED_KEYS), n=1, cutoff=0.6)
            hint = f" (did you mean '{matches[0]}'?)" if matches else ""
            raise ValidationError(f"Unsupported step type '{kind}' at step {idx}{hint}")
        _check_allowed_keys(params, _STEP_ALLOWED_KEYS[kind], f"Step {idx} ({kind})")

        step_allow_contam = protocol_allow_contam or _parse_bool(
            params.get("allow_contamination"),
            f"Step {idx} ({kind}) 'allow_contamination'",
        )
        first_new = len(lowered)

        if kind == "comment":
            text = params.get("text", params.get("message", ""))
            lowered.append(CommentStep(text=str(text)))
        elif kind == "delay":
            lowered.append(_parse_duration_params(params, "delay", idx))
        elif kind == "pause":
            has_duration = (
                ("seconds" in params and params["seconds"] is not None)
                or ("minutes" in params and params["minutes"] is not None)
            )
            has_message = (
                ("message" in params and params["message"] is not None)
                or ("msg" in params and params["msg"] is not None)
            )
            if has_duration and has_message:
                raise ValidationError(
                    f"Step {idx} (pause) cannot specify both duration ('seconds'/'minutes') "
                    "and operator message ('message'/'msg'); use 'delay' for timed waits "
                    "or 'pause' for operator-resumed stops"
                )
            if has_duration:
                lowered.append(_parse_duration_params(params, "pause", idx))
            else:
                msg = str(params.get("message", params.get("msg", "")))
                lowered.append(PauseStep(message=msg))
        elif kind == "transfer":
            _lower_transfer(
                params,
                deck,
                pipettes,
                tracker,
                lowered,
                well_liquid_classes=well_liquid_classes,
            )
        elif kind == "distribute":
            _lower_distribute(
                params,
                deck,
                pipettes,
                tracker,
                lowered,
                well_liquid_classes=well_liquid_classes,
            )
        elif kind == "mix":
            _lower_mix(
                params,
                deck,
                pipettes,
                tracker,
                lowered,
                well_liquid_classes=well_liquid_classes,
            )
        elif kind == "discard":
            _lower_discard(params, deck, pipettes, tracker, lowered)
        elif kind == "dilution_series":
            _lower_dilution_series(
                params,
                deck,
                pipettes,
                tracker,
                lowered,
                well_liquid_classes=well_liquid_classes,
            )

        for step in lowered[first_new:]:
            volumes.apply(
                step,
                f"Step {idx} ({kind})",
                allow_contamination=step_allow_contam,
            )
            origins.append(StepOrigin(source_step_index=idx, source_step_type=kind))

    cleanup_start = len(lowered)
    for mount in sorted(pipettes):
        tracker.ensure_no_tip(mount, lowered)
    for step in lowered[cleanup_start:]:
        volumes.apply(step, "End-of-protocol cleanup")
        origins.append(StepOrigin(source_step_index=None, source_step_type="cleanup"))

    return ProtocolSpec(
        name=name,
        description=description,
        api_level=api_level,
        deck=deck,
        pipettes=pipettes,
        steps=lowered,
        liquids=liquids,
        tips_used_per_rack=tracker.tips_used_per_rack,
        tips_used_per_mount=dict(tracker.tips_used_per_mount),
        well_summaries=volumes.summaries(),
        step_origins=tuple(origins),
        robot_type=robot_type,
        trash_slot=trash_slot,
    )


def compile_protocol(data: dict[str, Any]) -> str:
    """Validate and compile a protocol dictionary to Opentrons Python API v2 source."""
    spec = parse_and_validate(data)
    return render_protocol(spec)


def compile_file(
    input_path: Path | str,
    output_path: Path | str | None = None,
    *,
    manifest_path: Path | str | None = None,
) -> str:
    """Compile a YAML or JSON protocol file and optionally write Python and JSON manifest outputs."""
    data = load_protocol_data(input_path)
    spec = parse_and_validate(data)
    source = render_protocol(spec)
    if output_path is not None:
        out = Path(output_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(source, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ValidationError(f"Cannot write output file {out}: {exc}") from exc
    if manifest_path is not None:
        mpath = Path(manifest_path)
        manifest = build_protocol_manifest(spec, source)
        try:
            serialized = json.dumps(manifest, indent=2, allow_nan=False) + "\n"
        except ValueError as exc:
            raise ValidationError(f"Manifest contains non-finite numeric value: {exc}") from exc
        try:
            mpath.parent.mkdir(parents=True, exist_ok=True)
            mpath.write_text(serialized, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ValidationError(f"Cannot write manifest file {mpath}: {exc}") from exc
    return source

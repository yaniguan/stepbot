"""Protocol parser, validator, and compiler."""

from __future__ import annotations

import difflib
import json
import keyword
import math
import re
import warnings
from pathlib import Path
from typing import Any

import yaml

from stepbot.catalog import (
    DEFAULT_API_LEVEL,
    expected_tip_family_description,
    get_labware_spec,
    get_pipette_spec,
    is_tip_rack_compatible,
    parse_api_level,
)
from stepbot.codegen import build_protocol_manifest, render_protocol
from stepbot.models import (
    VOLUME_TOLERANCE,
    CommentStep,
    DeckItem,
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
    StepOrigin,
    TransferStep,
    ValidationError,
    WellRef,
    WellSummary,
)
from stepbot.wells import resolve_serial_wells, resolve_well_refs

# Names used by the generated code itself; deck aliases must not shadow them.
_RESERVED_NAMES = {"protocol", "metadata", "liquids", "pipette_left", "pipette_right"}
_VALID_NEW_TIP = {"once", "always", "never"}
_API_LEVEL_RE = re.compile(r"^\d+\.\d+$")
# Well.load_liquid and ProtocolContext.define_liquid need API level 2.14 or newer.
_MIN_LIQUID_API_LEVEL = (2, 14)

_ROOT_KEYS = {"metadata", "deck", "pipettes", "liquids", "steps", "allow_contamination"}
_METADATA_KEYS = {"name", "protocolName", "description", "author", "apiLevel"}
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
}
_MIX_AFTER_KEYS = {"reps", "repetitions", "volume"}

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
        "allow_contamination",
        "well_order",
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
        "allow_contamination",
        "well_order",
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


def _parse_bool(raw: Any, label: str, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValidationError(f"{label} must be a boolean (true or false), got {raw!r}")
    return raw


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


def _parse_deck(raw_deck: Any) -> dict[str, DeckItem]:
    if not isinstance(raw_deck, dict) or not raw_deck:
        raise ValidationError("'deck' must be a non-empty mapping of alias -> {labware, slot}")
    deck: dict[str, DeckItem] = {}
    used_slots: dict[int, str] = {}
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
        if isinstance(slot, bool) or not isinstance(slot, int) or not (1 <= slot <= 11):
            raise ValidationError(
                f"Invalid slot '{slot}' for '{alias}': slot must be an integer between 1 and 11"
            )
        if slot in used_slots:
            raise ValidationError(
                f"Duplicate deck slot {slot}: used by both '{used_slots[slot]}' and '{alias}'"
            )
        used_slots[slot] = alias
        deck[alias] = DeckItem(alias=alias, load_name=load_name, slot=slot, spec=spec)
    return deck


def _parse_pipettes(raw_pipettes: Any, deck: dict[str, DeckItem]) -> dict[str, PipetteConfig]:
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
        if spec.channels > 1:
            raise ValidationError(
                f"Multichannel pipette '{model}' on mount '{mount}' is not supported yet: "
                "tip counting and well targeting assume a single-channel pipette"
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


def _parse_api_level(raw: Any) -> str:
    if raw is None:
        return DEFAULT_API_LEVEL
    if not isinstance(raw, str):
        raise ValidationError(
            f"metadata.apiLevel must be a quoted string such as \"{DEFAULT_API_LEVEL}\", "
            f"got {raw!r} ({type(raw).__name__}); unquoted YAML numbers drop trailing "
            "zeros, so 2.10 is read as 2.1"
        )
    level = raw.strip()
    if not _API_LEVEL_RE.match(level):
        raise ValidationError(
            f"metadata.apiLevel '{raw}' must look like '<major>.<minor>', e.g. \"{DEFAULT_API_LEVEL}\""
        )
    major, minor = parse_api_level(level)
    if f"{major}.{minor}" != level:
        raise ValidationError(f"metadata.apiLevel '{raw}' must not have leading zeros")
    if major != 2:
        raise ValidationError(
            f"metadata.apiLevel '{raw}' is not supported: only Opentrons Python API v2 (2.x) is targeted"
        )
    return level


def _parse_liquids(raw_liquids: Any, deck: dict[str, DeckItem]) -> tuple[LiquidSpec, ...]:
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
        volume = entry.get("volume")
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or volume <= 0:
            raise ValidationError(f"Liquid '{name}' volume must be a positive number, got {volume!r}")
        vol = float(volume)
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
            LiquidSpec(name=name, description=str(description), wells=tuple(wells), volume=vol)
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


def _check_volume(
    volume: Any,
    pip: PipetteConfig,
    label: str,
    *,
    multi_aspirate: bool = False,
) -> tuple[float, int]:
    """Validate volume against pipette + tip-rack bounds; return (volume, strokes)."""
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise ValidationError(f"{label} volume must be a number, got {volume!r}")
    vol = float(volume)
    if vol <= 0:
        raise ValidationError(f"{label} volume must be positive, got {vol}")
    max_vol = pip.effective_max_volume
    min_vol = pip.spec.min_volume
    if vol < min_vol - VOLUME_TOLERANCE:
        raise ValidationError(
            f"{label} volume {vol} uL is outside pipette '{pip.model}' range "
            f"[{min_vol}, {max_vol}] uL"
        )
    if vol > max_vol + VOLUME_TOLERANCE:
        if not multi_aspirate:
            tip_note = (
                f" (capped by {pip.max_tip_volume:g} uL tip rack)"
                if pip.max_tip_volume is not None and pip.max_tip_volume < pip.spec.max_volume
                else ""
            )
            raise ValidationError(
                f"{label} volume {vol} uL is outside pipette '{pip.model}' range "
                f"[{min_vol}, {max_vol}] uL{tip_note}"
            )
        strokes = max(1, math.ceil((vol - VOLUME_TOLERANCE) / max_vol))
        stroke_vol = vol / strokes
        if stroke_vol < min_vol - VOLUME_TOLERANCE:
            raise ValidationError(
                f"{label} volume {vol} uL split across {strokes} strokes ({stroke_vol:.4g} uL/stroke) "
                f"falls below pipette '{pip.model}' minimum volume {min_vol} uL"
            )
        return vol, strokes
    return vol, 1


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
        raw_s = params["seconds"]
        if isinstance(raw_s, bool) or not isinstance(raw_s, (int, float)) or raw_s < 0:
            raise ValidationError(f"{kind} 'seconds' must be >= 0, got {raw_s!r}")
        sec_val = float(raw_s)
    if has_min:
        raw_m = params["minutes"]
        if isinstance(raw_m, bool) or not isinstance(raw_m, (int, float)) or raw_m < 0:
            raise ValidationError(f"{kind} 'minutes' must be >= 0, got {raw_m!r}")
        min_val = float(raw_m)
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

    def consume_tips(self, mount: str, count: int) -> None:
        if count <= 0:
            return
        needed = count
        for rack_alias in self.pipettes[mount].tip_racks:
            avail = self.rack_remaining[rack_alias]
            if avail >= needed:
                self.rack_remaining[rack_alias] -= needed
                self.tips_used_per_mount[mount] += count
                return
            needed -= avail
            self.rack_remaining[rack_alias] = 0
        total_capacity = sum(
            len(self.deck[r].spec.wells) for r in self.pipettes[mount].tip_racks
        )
        raise ValidationError(
            f"Not enough tips for pipette '{mount}': capacity is {total_capacity} tips "
            f"across {list(self.pipettes[mount].tip_racks)}"
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
                "(use new_tip: 'always' or set allow_contamination: true)"
            )
        tip_sp.update(src_sp)

    def _dispense_species(self, mount: str, ref: WellRef) -> None:
        if self.tip_species[mount]:
            self.well_species.setdefault(ref, set()).update(self.tip_species[mount])

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
            max_vol = pip.effective_max_volume
            strokes = max(1, math.ceil((step.volume - VOLUME_TOLERANCE) / max_vol))
            stroke_vol = step.volume / strokes

            srcs, dsts = list(step.src), list(step.dst)
            if len(srcs) == 1:
                srcs = srcs * len(dsts)
            elif len(dsts) == 1:
                dsts = dsts * len(srcs)

            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()
            for src, dst in zip(srcs, dsts):
                if step.new_tip == "always":
                    self.tip_species[step.pipette] = set()
                for _ in range(strokes):
                    self._aspirate_species(
                        step.pipette, src, label, allow_contamination=allow_contamination
                    )
                    self.remove(src, stroke_vol, label)
                    self._dispense_species(step.pipette, dst)
                    self.add(dst, stroke_vol, label)
                if step.mix_after is not None:
                    self.check_mix(dst, step.mix_after[1], label)
                    self._mix_species(
                        step.pipette, dst, label, allow_contamination=allow_contamination
                    )
                if step.new_tip == "always":
                    self.tip_species[step.pipette] = set()
            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()

        elif isinstance(step, DistributeStep):
            pip = self.pipettes[step.pipette]
            disp_vol = (
                pip.spec.min_volume if step.disposal_volume is None else step.disposal_volume
            )
            max_vol = pip.effective_max_volume
            batch_count = max(1, int((max_vol - disp_vol + VOLUME_TOLERANCE) // step.volume))
            num_chunks = math.ceil(len(step.dst) / batch_count) if step.dst else 0
            total_aspirated = step.volume * len(step.dst) + disp_vol * num_chunks

            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()
            self._aspirate_species(
                step.pipette, step.src, label, allow_contamination=allow_contamination
            )
            self.remove(step.src, total_aspirated, label)
            for dst in step.dst:
                self._dispense_species(step.pipette, dst)
                self.add(dst, step.volume, label)
            if step.new_tip == "once":
                self.tip_species[step.pipette] = set()

        elif isinstance(step, MixStep):
            for well in step.wells:
                self.check_mix(well, step.volume, label)
                self._mix_species(
                    step.pipette, well, label, allow_contamination=allow_contamination
                )

        elif isinstance(step, DiscardStep):
            stroke_vol = step.volume / max(1, step.strokes)
            for _ in range(max(1, step.strokes)):
                self._aspirate_species(
                    step.pipette, step.src, label, allow_contamination=allow_contamination
                )
                self.remove(step.src, stroke_vol, label)

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
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    multi_aspirate = _parse_bool(params.get("multi_aspirate"), "transfer 'multi_aspirate'")
    raw_src = params.get("src", params.get("source"))
    raw_dst = params.get("dst", params.get("dest", params.get("destination")))
    if raw_src is None or raw_dst is None:
        raise ValidationError("transfer step requires 'src' and 'dst'")
    src = resolve_well_refs(raw_src, deck, well_order=well_order)
    dst = resolve_well_refs(raw_dst, deck, well_order=well_order)
    if len(src) > 1 and len(dst) > 1 and len(src) != len(dst):
        raise ValidationError(
            f"transfer source count ({len(src)}) and destination count ({len(dst)}) mismatch"
        )
    vol, _ = _check_volume(
        params.get("volume"), pip, "transfer", multi_aspirate=multi_aspirate
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
        if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
            raise ValidationError(f"mix_after reps must be an integer >= 1, got {reps!r}")
        checked_mvol, _ = _check_volume(mvol, pip, "mix_after")
        mix_after = (reps, checked_mvol)

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
    elif new_tip == "once":
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)
    else:
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, max(len(src), len(dst)))

    out.append(
        TransferStep(
            pipette=pip.mount,
            src=tuple(src),
            dst=tuple(dst),
            volume=vol,
            new_tip=new_tip,
            mix_after=mix_after,
        )
    )


def _lower_distribute(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    raw_src = params.get("src", params.get("source"))
    raw_dst = params.get("dst", params.get("dest", params.get("destination")))
    if raw_src is None or raw_dst is None:
        raise ValidationError("distribute step requires 'src' and 'dst'")
    src = resolve_well_refs(raw_src, deck, well_order=well_order)
    if len(src) != 1:
        raise ValidationError(f"distribute requires a single source well, got {len(src)}")
    dst = resolve_well_refs(raw_dst, deck, well_order=well_order)
    vol, _ = _check_volume(params.get("volume"), pip, "distribute")
    new_tip = params.get("new_tip", "once")
    if new_tip not in ("once", "never"):
        raise ValidationError(
            f"distribute supports new_tip 'once' or 'never', got '{new_tip}'"
        )

    disposal_volume: float | None = None
    if "disposal_volume" in params and params["disposal_volume"] is not None:
        raw_disp = params["disposal_volume"]
        if isinstance(raw_disp, bool) or not isinstance(raw_disp, (int, float)) or raw_disp < 0:
            raise ValidationError(
                f"distribute 'disposal_volume' must be a number >= 0, got {raw_disp!r}"
            )
        disposal_volume = float(raw_disp)

    eff_disp = pip.spec.min_volume if disposal_volume is None else disposal_volume
    if vol + eff_disp > pip.effective_max_volume + VOLUME_TOLERANCE:
        raise ValidationError(
            f"distribute volume {vol} uL plus disposal_volume {eff_disp} uL "
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
        )
    )


def _lower_mix(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
) -> None:
    pip = _select_pipette(params, pipettes)
    well_order = str(params.get("well_order", "row"))
    raw_wells = params.get("well", params.get("wells"))
    if raw_wells is None:
        raise ValidationError("mix step requires 'well' or 'wells'")
    wells = resolve_well_refs(raw_wells, deck, well_order=well_order)
    vol, _ = _check_volume(params.get("volume"), pip, "mix")
    reps = params.get("reps", params.get("repetitions"))
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        raise ValidationError(f"mix 'reps' must be an integer >= 1, got {reps!r}")
    new_tip = params.get("new_tip", "once")
    if new_tip not in _VALID_NEW_TIP:
        raise ValidationError(f"Invalid new_tip '{new_tip}' in mix")

    if new_tip == "never":
        tracker.ensure_has_tip(pip.mount, out)
        out.append(MixStep(pipette=pip.mount, wells=tuple(wells), volume=vol, reps=reps))
    elif new_tip == "once":
        tracker.ensure_no_tip(pip.mount, out)
        tracker.consume_tips(pip.mount, 1)
        out.append(PickUpTipStep(pipette=pip.mount))
        out.append(MixStep(pipette=pip.mount, wells=tuple(wells), volume=vol, reps=reps))
        out.append(DropTipStep(pipette=pip.mount))
    else:
        tracker.ensure_no_tip(pip.mount, out)
        for well in wells:
            tracker.consume_tips(pip.mount, 1)
            out.append(PickUpTipStep(pipette=pip.mount))
            out.append(MixStep(pipette=pip.mount, wells=(well,), volume=vol, reps=reps))
            out.append(DropTipStep(pipette=pip.mount))


def _lower_dilution_series(
    params: dict[str, Any],
    deck: dict[str, DeckItem],
    pipettes: dict[str, PipetteConfig],
    tracker: _TipStateTracker,
    out: list[LoweredStep],
) -> None:
    pip = _select_pipette(params, pipettes)
    multi_aspirate = _parse_bool(
        params.get("multi_aspirate"), "dilution_series 'multi_aspirate'"
    )
    raw_source = params.get("source", params.get("src"))
    raw_diluent = params.get("diluent")
    raw_first = params.get("first_well")
    count = params.get("n", params.get("count", params.get("wells")))
    factor = params.get("factor")
    final_vol_raw = params.get("final_volume")
    direction = params.get("direction", "row")
    mix_reps = params.get("mix_reps", 3)
    precision = params.get("precision", 2)

    if raw_source is None or raw_diluent is None or raw_first is None:
        raise ValidationError("dilution_series requires 'source', 'diluent', and 'first_well'")
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValidationError(f"dilution_series 'n' must be an integer >= 2, got {count!r}")
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or float(factor) <= 1.0:
        raise ValidationError(f"dilution_series 'factor' must be > 1, got {factor!r}")
    if isinstance(mix_reps, bool) or not isinstance(mix_reps, int) or mix_reps < 1:
        raise ValidationError(
            f"dilution_series 'mix_reps' must be an integer >= 1, got {mix_reps!r}"
        )
    if isinstance(precision, bool) or not isinstance(precision, int) or not (0 <= precision <= 4):
        raise ValidationError(
            f"dilution_series 'precision' must be an integer between 0 and 4, got {precision!r}"
        )

    source_refs = resolve_well_refs(raw_source, deck)
    diluent_refs = resolve_well_refs(raw_diluent, deck)
    if len(source_refs) != 1 or len(diluent_refs) != 1:
        raise ValidationError("dilution_series 'source' and 'diluent' must each be a single well")

    series_wells = resolve_serial_wells(raw_first, count, deck, direction=str(direction))
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

    raw_transfer_vol = final_vol / (float(factor) - 1.0)
    transfer_vol = round(raw_transfer_vol, precision)
    if abs(raw_transfer_vol - transfer_vol) > 1e-9:
        warnings.warn(
            f"dilution_series transfer_volume {raw_transfer_vol:.6f} uL is non-terminating at "
            f"precision={precision}; rounded to {transfer_vol} uL",
            UserWarning,
            stacklevel=3,
        )
    initial_vol = round(final_vol + transfer_vol, precision)

    _, discard_strokes = _check_volume(
        transfer_vol,
        pip,
        "dilution_series transfer_volume",
        multi_aspirate=multi_aspirate,
    )
    _check_volume(
        initial_vol,
        pip,
        "dilution_series initial_volume",
        multi_aspirate=multi_aspirate,
    )
    _check_well_capacity(series_wells, initial_vol, deck, "dilution_series")

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
        )
    )

    # 3. Serial cascade from high concentration to low concentration using one tip.
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
            )
        )
    out.append(
        DiscardStep(
            pipette=pip.mount,
            src=series_wells[-1],
            volume=transfer_vol,
            strokes=discard_strokes,
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
    api_level = _parse_api_level(metadata.get("apiLevel"))

    deck = _parse_deck(data.get("deck"))
    pipettes = _parse_pipettes(data.get("pipettes"), deck)
    liquids = _parse_liquids(data.get("liquids"), deck)
    if liquids and parse_api_level(api_level) < _MIN_LIQUID_API_LEVEL:
        raise ValidationError(
            f"'liquids' requires apiLevel {_MIN_LIQUID_API_LEVEL[0]}.{_MIN_LIQUID_API_LEVEL[1]} "
            f"or newer, got '{api_level}'"
        )

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
            _lower_transfer(params, deck, pipettes, tracker, lowered)
        elif kind == "distribute":
            _lower_distribute(params, deck, pipettes, tracker, lowered)
        elif kind == "mix":
            _lower_mix(params, deck, pipettes, tracker, lowered)
        elif kind == "dilution_series":
            _lower_dilution_series(params, deck, pipettes, tracker, lowered)

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
            mpath.parent.mkdir(parents=True, exist_ok=True)
            mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ValidationError(f"Cannot write manifest file {mpath}: {exc}") from exc
    return source

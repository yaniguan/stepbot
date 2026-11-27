"""Interoperability bridge translating twinstep protocols and twins into stepbot specs."""

from __future__ import annotations

from collections.abc import Sequence
import math
import re
from typing import Any

from stepbot.catalog import get_labware_spec, get_pipette_spec
from stepbot.compiler import compile_protocol
from stepbot.models import ValidationError

_EXTERNAL_PREFIXES = ("reservoir", "stock", "trough", "waste")
_BARE_WELL_RE = re.compile(r"^[A-Za-z]+\d+$")
_IDENT_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]")


def _default_tip_rack_for(pipette_model: str) -> str:
    spec = get_pipette_spec(pipette_model)
    if spec is None:
        raise ValidationError(f"Unknown Opentrons pipette model '{pipette_model}'")
    if spec.max_volume <= 20.0:
        return "opentrons_96_tiprack_20ul"
    if spec.max_volume <= 300.0:
        return "opentrons_96_tiprack_300ul"
    return "opentrons_96_tiprack_1000ul"


def _sanitize_alias(raw: str, fallback: str) -> str:
    cleaned = _IDENT_CLEAN_RE.sub("_", raw.strip())
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    if cleaned in {"protocol", "metadata", "liquids", "pipette_left", "pipette_right", "reservoir", "tips"}:
        cleaned = f"lw_{cleaned}"
    return cleaned


def _parse_twinstep_location(loc: Any) -> tuple[bool, str, str]:
    """Return (is_external, container_name, item_or_well_name)."""
    if not isinstance(loc, str) or not loc.strip():
        raise ValidationError(f"Invalid twinstep location {loc!r}")
    text = loc.strip()
    if ":" in text:
        prefix, rest = text.split(":", 1)
        prefix = prefix.strip()
        rest = rest.strip()
        if prefix.lower() in _EXTERNAL_PREFIXES:
            return True, prefix.lower(), rest or "reagent"
        return False, prefix, rest.upper()
    if text.lower() in _EXTERNAL_PREFIXES:
        return True, text.lower(), text.lower()
    if _BARE_WELL_RE.match(text):
        return False, "plate1", text.upper()
    return False, text, ""


def _extract_raw_steps(twinstep_data: dict[str, Any] | Sequence[Any]) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(twinstep_data, dict):
        proto_name = str(
            twinstep_data.get("protocol_id")
            or twinstep_data.get("id")
            or "Twinstep Imported Protocol"
        )
        if "corrupted_steps" in twinstep_data:
            raw_list = twinstep_data["corrupted_steps"]
        elif "steps" in twinstep_data:
            raw_list = twinstep_data["steps"]
        else:
            raise ValidationError(
                "twinstep mapping must contain 'steps' or 'corrupted_steps'"
            )
    elif isinstance(twinstep_data, Sequence) and not isinstance(twinstep_data, (str, bytes)):
        proto_name = "Twinstep Imported Protocol"
        raw_list = list(twinstep_data)
    else:
        raise ValidationError("twinstep_data must be a mapping or sequence of steps")

    if not isinstance(raw_list, Sequence) or not raw_list:
        raise ValidationError("twinstep protocol must contain at least one step")

    steps: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list):
        if isinstance(item, dict):
            steps.append(dict(item))
        elif hasattr(item, "to_dict") and callable(item.to_dict):
            steps.append(dict(item.to_dict()))
        else:
            raise ValidationError(f"twinstep step {idx} must be a dict or Step dataclass")
    return proto_name, steps


def _to_ul(volume: Any, unit: Any, label: str) -> float:
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(float(volume)):
        raise ValidationError(f"{label}: volume must be a finite number, got {volume!r}")
    vol = float(volume)
    if vol <= 0:
        raise ValidationError(f"{label}: volume must be positive, got {vol}")
    if unit == "uL":
        return vol
    if unit == "mL":
        scaled = vol * 1000.0
        if not math.isfinite(scaled):
            raise ValidationError(f"{label}: volume must be a finite number, got {volume!r}")
        return scaled
    raise ValidationError(f"{label}: unsupported volume unit {unit!r} (expected 'uL' or 'mL')")


def from_twinstep(
    twinstep_data: dict[str, Any] | Sequence[Any],
    *,
    plate_labware: str = "corning_96_wellplate_360ul_flat",
    reservoir_labware: str = "nest_12_reservoir_15ml",
    pipette_model: str = "p300_single_gen2",
    tip_rack_labware: str | None = None,
    reservoir_volume: float = 10000.0,
    initial_well_volumes: dict[str, float] | None = None,
    new_tip: str = "always",
    num_tip_racks: int = 1,
) -> dict[str, Any]:
    """Translate a twinstep protocol or TwinRecord into a stepbot protocol dictionary."""
    proto_name, raw_steps = _extract_raw_steps(twinstep_data)

    res_spec = get_labware_spec(reservoir_labware)
    if res_spec is None:
        raise ValidationError(f"Unknown reservoir labware '{reservoir_labware}'")
    plate_spec = get_labware_spec(plate_labware)
    if plate_spec is None:
        raise ValidationError(f"Unknown plate labware '{plate_labware}'")
    tip_load_name = tip_rack_labware or _default_tip_rack_for(pipette_model)

    plate_aliases: dict[str, str] = {}
    reagent_wells: dict[str, str] = {}

    def ensure_plate(raw_plate: str) -> str:
        if raw_plate not in plate_aliases:
            plate_aliases[raw_plate] = _sanitize_alias(raw_plate, "plate")
        return plate_aliases[raw_plate]

    def resolve_loc(loc: Any, *, allow_external: bool) -> str:
        is_ext, container, target = _parse_twinstep_location(loc)
        if is_ext:
            if not allow_external:
                raise ValidationError(f"External reservoir '{loc}' not valid here")
            key = f"{container}:{target}"
            if key not in reagent_wells:
                idx = len(reagent_wells)
                if idx >= len(res_spec.wells):
                    raise ValidationError(
                        f"Protocol uses more external reagents than '{reservoir_labware}' has wells"
                    )
                reagent_wells[key] = res_spec.wells[idx]
            return f"reservoir:{reagent_wells[key]}"
        if not target:
            raise ValidationError(f"Expected well coordinate in '{loc}'")
        alias = ensure_plate(container)
        return f"{alias}:{target}"

    stepbot_steps: list[dict[str, Any]] = []
    for idx, step in enumerate(raw_steps, start=1):
        stype = step.get("type")
        label = f"twinstep step {idx} ({stype})"
        if stype == "transfer":
            vol_ul = _to_ul(step.get("volume"), step.get("unit", "uL"), label)
            src_ref = resolve_loc(step.get("source"), allow_external=True)
            dst_ref = resolve_loc(step.get("dest"), allow_external=True)
            t_params: dict[str, Any] = {
                "src": src_ref,
                "dst": dst_ref,
                "volume": vol_ul,
                "new_tip": step.get("new_tip", new_tip),
            }
            if "allow_contamination" in step:
                t_params["allow_contamination"] = bool(step["allow_contamination"])
            stepbot_steps.append({"transfer": t_params})
        elif stype == "mix":
            vol_ul = _to_ul(step.get("volume"), step.get("unit", "uL"), label)
            cycles = step.get("cycles")
            if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 1:
                raise ValidationError(f"{label}: cycles must be an integer >= 1, got {cycles!r}")
            well_ref = resolve_loc(step.get("well"), allow_external=False)
            m_params: dict[str, Any] = {
                "wells": well_ref,
                "volume": vol_ul,
                "reps": cycles,
                "new_tip": step.get("new_tip", new_tip),
            }
            if "allow_contamination" in step:
                m_params["allow_contamination"] = bool(step["allow_contamination"])
            stepbot_steps.append({"mix": m_params})
        elif stype == "incubate":
            temp = step.get("temperature")
            duration = step.get("time")
            t_unit = step.get("time_unit", step.get("unit", "min"))
            if (
                isinstance(temp, bool)
                or not isinstance(temp, (int, float))
                or not math.isfinite(float(temp))
                or temp <= -273.15
            ):
                raise ValidationError(f"{label}: invalid temperature {temp!r}")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or duration <= 0
            ):
                raise ValidationError(f"{label}: duration must be a positive finite number, got {duration!r}")
            if step.get("plate") is not None:
                ensure_plate(str(step["plate"]))
            stepbot_steps.append(
                {"comment": f"Incubate at {float(temp):g} C for {float(duration):g} {t_unit}"}
            )
            if t_unit == "min":
                stepbot_steps.append({"delay": {"minutes": float(duration)}})
            elif t_unit == "s":
                stepbot_steps.append({"delay": {"seconds": float(duration)}})
            else:
                raise ValidationError(f"{label}: invalid time_unit {t_unit!r}")
        elif stype == "wash":
            vol_ul = _to_ul(step.get("volume"), step.get("unit", "uL"), label)
            cycles = step.get("cycles")
            if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 1:
                raise ValidationError(f"{label}: cycles must be an integer >= 1, got {cycles!r}")
            if vol_ul > 2000.0 or vol_ul < 10.0:
                raise ValidationError(
                    f"{label}: wash volume {vol_ul:g} uL outside physical washer range [10, 2000] uL"
                )
            target_desc = str(step.get("well") or step.get("plate") or "plate1")
            if step.get("plate") is not None:
                ensure_plate(str(step["plate"]))
            if step.get("well") is not None:
                resolve_loc(step["well"], allow_external=False)
            stepbot_steps.append(
                {"comment": f"Wash {target_desc}: {cycles} cycles x {vol_ul:g} uL"}
            )
        elif stype == "read":
            raw_plate = step.get("plate")
            if not isinstance(raw_plate, str) or not raw_plate.strip():
                raise ValidationError(f"{label}: missing plate name")
            alias = ensure_plate(raw_plate)
            mode = step.get("mode", "absorbance")
            wl = step.get("wavelength")
            wl_suffix = f" at {wl} nm" if wl is not None else ""
            stepbot_steps.append({"comment": f"Read {alias} ({mode}{wl_suffix})"})
        else:
            raise ValidationError(f"Unsupported twinstep step type {stype!r} at step {idx}")

    if not plate_aliases:
        plate_aliases["plate1"] = "plate1"

    deck: dict[str, dict[str, Any]] = {}
    slot = 1
    for alias in plate_aliases.values():
        deck[alias] = {"labware": plate_labware, "slot": slot}
        slot += 1
    if reagent_wells:
        deck["reservoir"] = {"labware": reservoir_labware, "slot": slot}
        slot += 1

    tip_rack_aliases: list[str] = []
    for r_idx in range(max(1, num_tip_racks)):
        t_alias = "tips" if num_tip_racks == 1 else f"tips_{r_idx + 1}"
        deck[t_alias] = {"labware": tip_load_name, "slot": slot}
        tip_rack_aliases.append(t_alias)
        slot += 1

    liquids: dict[str, dict[str, Any]] = {}
    for key, r_well in reagent_wells.items():
        liq_name = _IDENT_CLEAN_RE.sub("_", key)
        capped_vol = min(float(reservoir_volume), res_spec.max_volume_for(r_well))
        liquids[liq_name] = {
            "wells": f"reservoir:{r_well}",
            "volume": capped_vol,
            "description": key,
        }

    if initial_well_volumes:
        for idx, (loc_str, vol) in enumerate(initial_well_volumes.items()):
            mapped_well = resolve_loc(loc_str, allow_external=False)
            liquids[f"init_well_{idx + 1}"] = {
                "wells": mapped_well,
                "volume": float(vol),
            }

    result: dict[str, Any] = {
        "metadata": {"name": proto_name},
        "deck": deck,
        "pipettes": {
            "left": {
                "model": pipette_model,
                "tip_racks": tip_rack_aliases,
            }
        },
        "steps": stepbot_steps,
    }
    if liquids:
        result["liquids"] = liquids
    return result


def check_twinstep_protocol(
    twinstep_data: dict[str, Any] | Sequence[Any],
    **kwargs: Any,
) -> tuple[bool, str]:
    """Validate a twinstep protocol or TwinRecord via stepbot's compiler.

    Returns (True, compiled_python_source) when valid, or (False, error_message)
    when rejected by schema, volume, tip, or cross-contamination checks.
    """
    try:
        spec_dict = from_twinstep(twinstep_data, **kwargs)
        source = compile_protocol(spec_dict)
        return True, source
    except ValidationError as exc:
        return False, str(exc)

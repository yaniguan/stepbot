"""Auto-Lab 15-repository wire node (`autolab-wire/v1`) for `stepbot` (Layer L1)."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import keyword
import math
from pathlib import Path
import re
import sys
from typing import Any

REPO_ID = "stepbot"
LAYER = "L1"
WIRE_SCHEMA_VERSION = "autolab-wire/v1"

REPO_DIR = Path(__file__).resolve().parent
_SRC_DIR = str(REPO_DIR / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from community_extensions.liquid_class_compiler import (  # noqa: E402
    DEFAULT_ORIFICE_RADIUS_MM,
    DEFAULT_P300_FLOW_RATE_UL_S,
    LIQUID_CLASS_PROFILES,
    MultiLiquidTransferSpec,
    RheologyUnderflowError,
    compile_default_aqueous_codegen,
    compile_rheology_aware_transfer,
    simulate_transfer_execution,
)
from stepbot import (  # noqa: E402
    DEFAULT_MAX_CONSUMED_NONCES,
    DEFAULT_MCP_SECRET_KEY,
    McpSecurityViolation,
    StepbotMcpExecutor,
    ValidationError,
    compile_with_mcp_envelope,
    parse_and_validate,
)
from stepbot.catalog import DEFAULT_FLEX_TRASH_SLOT, is_valid_flex_slot  # noqa: E402
from stepbot.compiler import (  # noqa: E402
    _METADATA_KEYS,
    _REQUIREMENTS_KEYS,
    _RESERVED_NAMES,
    _STEP_ALLOWED_KEYS,
    _parse_deck,
)
from stepbot.models import WellRef  # noqa: E402
from stepbot.wells import resolve_well_refs  # noqa: E402

_IDENT_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]")
_TRASH_TARGETS = frozenset({"trash", "waste", "fixed_trash", "trash_bin"})
_WELL_REF_KEYS = (
    "src",
    "source",
    "dst",
    "dest",
    "destination",
    "well",
    "wells",
    "diluent",
    "first_well",
)


def _init_wire_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy an incoming wire packet and populate `autolab-wire/v1` defaults."""
    out: dict[str, Any] = copy.deepcopy(dict(packet))
    out.setdefault("schema_version", WIRE_SCHEMA_VERSION)
    out.setdefault("halted", False)
    out.setdefault("halt_reason", None)
    out.setdefault("trace", [])
    out.setdefault("bottom_reasoning_traces", [])
    return out


def _is_valid_deck_alias(name: str) -> bool:
    return (
        bool(name)
        and name.isidentifier()
        and not keyword.iskeyword(name)
        and name not in _RESERVED_NAMES
    )


def _make_deck_alias(raw_key: str, used: set[str]) -> str:
    if _is_valid_deck_alias(raw_key) and raw_key not in used and not is_valid_flex_slot(raw_key):
        return raw_key
    cleaned = _IDENT_CLEAN_RE.sub("_", raw_key.strip())
    base = f"slot_{cleaned}" if cleaned else "slot_item"
    if not _is_valid_deck_alias(base):
        base = f"lw_{base}"
    candidate = base
    suffix = 2
    while candidate in used or not _is_valid_deck_alias(candidate):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _has_rehearsal_dialect(raw: Mapping[str, Any]) -> bool:
    raw_meta = raw.get("metadata")
    if isinstance(raw_meta, Mapping) and "liquid_class" in raw_meta:
        return True
    raw_deck = raw.get("deck")
    if isinstance(raw_deck, Mapping):
        for raw_key, entry in raw_deck.items():
            if (
                not isinstance(raw_key, str)
                or not _is_valid_deck_alias(raw_key)
                or is_valid_flex_slot(raw_key)
            ):
                return True
            if isinstance(entry, Mapping) and ("load_name" in entry or "slot" not in entry):
                return True
    raw_pipettes = raw.get("pipettes")
    if isinstance(raw_pipettes, Mapping):
        for entry in raw_pipettes.values():
            if isinstance(entry, Mapping) and "instrument_name" in entry:
                return True
    raw_steps = raw.get("steps")
    if isinstance(raw_steps, list):
        for step in raw_steps:
            if isinstance(step, Mapping):
                if "action" in step:
                    return True
                step_kind = step.get("type") or (next(iter(step.keys())) if len(step) == 1 else None)
                if step_kind in ("incubate", "seal", "thaw", "wash", "read"):
                    return True
    return False


def _rewrite_well_expr(value: Any, alias_map: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for sep in (":", "."):
            if sep in value:
                prefix, rest = value.split(sep, 1)
                mapped = alias_map.get(prefix.strip(), prefix.strip())
                return f"{mapped}:{rest.strip()}"
        return value
    if isinstance(value, list):
        return [_rewrite_well_expr(item, alias_map) for item in value]
    return value


def _synthesize_initial_liquids(
    norm_deck: dict[str, Any],
    norm_steps: list[dict[str, Any]],
    *,
    robot_type: str = "OT-2",
    trash_slot: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Populate starting reservoir/source liquid volumes when omitted by rehearsal IR."""
    try:
        deck_items = _parse_deck(norm_deck, robot_type=robot_type, trash_slot=trash_slot)
    except ValidationError:
        return {}

    dispensed: set[WellRef] = set()
    needed_ul: dict[WellRef, float] = {}

    for step in norm_steps:
        if "type" in step:
            kind = str(step["type"])
            params: Mapping[str, Any] = step
        elif len(step) == 1:
            kind, body = next(iter(step.items()))
            if not isinstance(body, Mapping):
                continue
            params = body
        else:
            continue

        well_order = str(params.get("well_order", "row"))
        raw_vol = params.get("volume", params.get("final_volume", 100.0))
        vol = (
            float(raw_vol)
            if isinstance(raw_vol, (int, float)) and not isinstance(raw_vol, bool) and raw_vol > 0
            else 100.0
        )

        try:
            if kind in ("transfer", "distribute"):
                raw_src = params.get("src", params.get("source"))
                raw_dst = params.get("dst", params.get("dest", params.get("destination")))
                srcs = resolve_well_refs(raw_src, deck_items, well_order=well_order) if raw_src else []
                dsts = resolve_well_refs(raw_dst, deck_items, well_order=well_order) if raw_dst else []
                fanout = len(dsts) if len(srcs) == 1 and len(dsts) > 1 else 1
                reserve = 100.0 if kind == "distribute" else 0.0
                for s_ref in srcs:
                    if s_ref not in dispensed:
                        needed_ul[s_ref] = needed_ul.get(s_ref, 0.0) + vol * fanout + reserve
                dispensed.update(dsts)
            elif kind == "discard":
                raw_src = params.get("src", params.get("source"))
                srcs = resolve_well_refs(raw_src, deck_items, well_order=well_order) if raw_src else []
                for s_ref in srcs:
                    if s_ref not in dispensed:
                        needed_ul[s_ref] = needed_ul.get(s_ref, 0.0) + vol
            elif kind == "mix":
                raw_wells = params.get("wells", params.get("well"))
                wells = (
                    resolve_well_refs(raw_wells, deck_items, well_order=well_order)
                    if raw_wells
                    else []
                )
                for w_ref in wells:
                    if w_ref not in dispensed:
                        needed_ul[w_ref] = max(needed_ul.get(w_ref, 0.0), vol)
            elif kind == "dilution_series":
                raw_src = params.get("source", params.get("src"))
                raw_dil = params.get("diluent")
                if raw_src:
                    for s_ref in resolve_well_refs(raw_src, deck_items, well_order=well_order):
                        if s_ref not in dispensed:
                            needed_ul[s_ref] = needed_ul.get(s_ref, 0.0) + vol * 4.0
                if raw_dil:
                    for d_ref in resolve_well_refs(raw_dil, deck_items, well_order=well_order):
                        if d_ref not in dispensed:
                            needed_ul[d_ref] = needed_ul.get(d_ref, 0.0) + vol * 12.0
        except ValidationError:
            continue

    liquids: dict[str, dict[str, Any]] = {}
    for idx, (ref, req_vol) in enumerate(needed_ul.items(), start=1):
        cap = deck_items[ref.labware].spec.max_volume_for(ref.well)
        init_vol = min(cap, max(req_vol * 2.0, min(cap, 1000.0))) if req_vol <= cap else cap
        liquids[f"reagent_{idx}"] = {
            "wells": ref.key(),
            "volume": float(init_vol),
        }
    return liquids


def _extract_source_liquid_class(
    packet: Mapping[str, Any],
    raw_proto_src: Path | str | Mapping[str, Any],
) -> str | None:
    """Resolve optional liquid_class hint from packet, rehearsal block, metadata, or liquids."""
    top_lc = packet.get("liquid_class")
    if isinstance(top_lc, str) and top_lc.strip():
        return top_lc.strip()
    rehearsal = packet.get("rehearsal")
    if isinstance(rehearsal, Mapping):
        reh_lc = rehearsal.get("liquid_class")
        if isinstance(reh_lc, str) and reh_lc.strip():
            return reh_lc.strip()
    if isinstance(raw_proto_src, Mapping):
        meta = raw_proto_src.get("metadata")
        if isinstance(meta, Mapping):
            meta_lc = meta.get("liquid_class")
            if isinstance(meta_lc, str) and meta_lc.strip():
                return meta_lc.strip()
        liquids = raw_proto_src.get("liquids")
        if isinstance(liquids, Mapping):
            for entry in liquids.values():
                if isinstance(entry, Mapping):
                    liq_lc = entry.get("liquid_class")
                    if isinstance(liq_lc, str) and liq_lc.strip():
                        return liq_lc.strip()
    return None


def _normalize_rehearsal_step(
    kind: str,
    params: Mapping[str, Any],
    alias_map: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Normalize a single rehearsal/stepbot step dict into one or more canonical stepbot steps."""
    if kind == "incubate":
        temp = params.get("temperature_c", params.get("temperature", params.get("temp_c")))
        dur_min = params.get("minutes", params.get("duration_min"))
        dur_sec = params.get("seconds", params.get("duration_s"))
        if dur_min is None and dur_sec is None and "time" in params:
            t_unit = str(params.get("time_unit", params.get("unit", "min")))
            if t_unit.startswith("s"):
                dur_sec = params["time"]
            else:
                dur_min = params["time"]
        temp_note = f" at {temp} C" if temp is not None else ""
        out_steps: list[dict[str, Any]] = [{"type": "comment", "text": f"Incubate{temp_note}"}]
        delay_step: dict[str, Any] = {"type": "delay"}
        if isinstance(dur_min, (int, float)) and not isinstance(dur_min, bool) and dur_min >= 0:
            delay_step["minutes"] = float(dur_min)
        if isinstance(dur_sec, (int, float)) and not isinstance(dur_sec, bool) and dur_sec >= 0:
            delay_step["seconds"] = float(dur_sec)
        if "minutes" not in delay_step and "seconds" not in delay_step:
            delay_step["seconds"] = 1.0
        out_steps.append(delay_step)
        return out_steps

    if kind in ("seal", "thaw", "wash", "read"):
        plate = params.get("plate", params.get("labware", "plate"))
        mapped_plate = alias_map.get(str(plate), str(plate))
        return [{"type": "comment", "text": f"{kind.capitalize()} {mapped_plate}"}]

    raw_dst = params.get("dst", params.get("dest", params.get("destination")))
    if kind == "transfer" and isinstance(raw_dst, str) and raw_dst.strip().lower() in _TRASH_TARGETS:
        kind = "discard"

    rewritten: dict[str, Any] = {}
    for p_key, p_val in params.items():
        key_s = str(p_key)
        if key_s == "cycles" and "reps" not in params and kind == "mix":
            key_s = "reps"
        if key_s in _WELL_REF_KEYS:
            rewritten[key_s] = _rewrite_well_expr(p_val, alias_map)
        else:
            rewritten[key_s] = p_val

    allowed = _STEP_ALLOWED_KEYS.get(kind)
    if allowed is not None:
        rewritten = {k: v for k, v in rewritten.items() if k in allowed}
    return [{"type": kind, **rewritten}]


def _normalize_protocol_source(
    proto_src: Path | str | Mapping[str, Any],
    *,
    is_rehearsal_dict: bool = False,
) -> Path | str | dict[str, Any]:
    """Accept native `stepbot` sources directly and adapt `lab-rehearsal` & Flex wire dicts."""
    if not isinstance(proto_src, Mapping):
        return proto_src

    raw: dict[str, Any] = copy.deepcopy(dict(proto_src))
    try:
        parse_and_validate(raw)
        return raw
    except ValidationError:
        if not (is_rehearsal_dict or _has_rehearsal_dialect(raw)):
            return raw

    normalized: dict[str, Any] = {}

    raw_meta = raw.get("metadata")
    if isinstance(raw_meta, Mapping):
        norm_meta: dict[str, Any] = {
            str(k): v for k, v in raw_meta.items() if str(k) in _METADATA_KEYS
        }
        if "name" not in norm_meta and "protocolName" not in norm_meta and "title" in raw_meta:
            norm_meta["name"] = str(raw_meta["title"])
        normalized["metadata"] = norm_meta

    raw_reqs = raw.get("requirements")
    if isinstance(raw_reqs, Mapping):
        normalized["requirements"] = {
            str(k): v for k, v in raw_reqs.items() if str(k) in _REQUIREMENTS_KEYS
        }
    if "robot_type" in raw:
        normalized["robot_type"] = raw["robot_type"]
    if "trash_slot" in raw:
        normalized["trash_slot"] = raw["trash_slot"]

    alias_map: dict[str, str] = {}
    used_aliases: set[str] = set()
    norm_deck: dict[str, Any] = {}
    detected_flex_slots = False
    raw_deck = raw.get("deck")
    if isinstance(raw_deck, Mapping):
        for raw_key, raw_entry in raw_deck.items():
            key_str = str(raw_key).strip()
            alias = _make_deck_alias(key_str, used_aliases)
            used_aliases.add(alias)
            alias_map[key_str] = alias

            if isinstance(raw_entry, Mapping):
                slot_val: Any = raw_entry.get("slot")
                if slot_val is None:
                    if key_str.isdigit():
                        slot_val = int(key_str)
                    elif is_valid_flex_slot(key_str.upper()):
                        slot_val = key_str.upper()
                elif isinstance(slot_val, str):
                    s_clean = slot_val.strip()
                    if s_clean.isdigit():
                        slot_val = int(s_clean)
                    elif is_valid_flex_slot(s_clean.upper()):
                        slot_val = s_clean.upper()

                if isinstance(slot_val, int) and not isinstance(slot_val, bool):
                    alias_map.setdefault(str(slot_val), alias)
                elif isinstance(slot_val, str) and is_valid_flex_slot(slot_val):
                    detected_flex_slots = True
                    alias_map.setdefault(slot_val, alias)

                labware_val = raw_entry.get("labware") or raw_entry.get("load_name")
                norm_deck[alias] = {"labware": labware_val, "slot": slot_val}
            else:
                norm_deck[alias] = raw_entry
    normalized["deck"] = norm_deck

    if (
        detected_flex_slots
        and "robot_type" not in normalized
        and "requirements" not in normalized
        and "robotType" not in normalized.get("metadata", {})
        and "robot_type" not in normalized.get("metadata", {})
    ):
        normalized["requirements"] = {"robotType": "Flex"}

    norm_pipettes: dict[str, Any] = {}
    raw_pipettes = raw.get("pipettes")
    if isinstance(raw_pipettes, Mapping):
        for mount, raw_pip in raw_pipettes.items():
            if isinstance(raw_pip, Mapping):
                model = (
                    raw_pip.get("model")
                    or raw_pip.get("name")
                    or raw_pip.get("instrument_name")
                )
                raw_racks = raw_pip.get("tip_racks")
                if raw_racks is None and "tip_rack" in raw_pip:
                    raw_racks = [raw_pip["tip_rack"]]
                racks = (
                    [alias_map.get(str(r), str(r)) for r in raw_racks]
                    if isinstance(raw_racks, list)
                    else raw_racks
                )
                norm_pipettes[str(mount)] = {"model": model, "tip_racks": racks}
            else:
                norm_pipettes[str(mount)] = raw_pip
    normalized["pipettes"] = norm_pipettes

    norm_steps: list[dict[str, Any]] = []
    raw_steps = raw.get("steps")
    if isinstance(raw_steps, list):
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                continue
            step_dict = dict(raw_step)
            if "action" in step_dict and "type" not in step_dict:
                kind = str(step_dict.pop("action"))
                params: Mapping[str, Any] = step_dict
            elif "type" in step_dict:
                kind = str(step_dict.pop("type"))
                params = step_dict
            elif len(step_dict) == 1:
                only_k, only_v = next(iter(step_dict.items()))
                kind = str(only_k)
                if not isinstance(only_v, Mapping):
                    norm_steps.append({kind: only_v})
                    continue
                params = dict(only_v)
            else:
                norm_steps.append(step_dict)
                continue

            norm_steps.extend(_normalize_rehearsal_step(kind, params, alias_map))
    normalized["steps"] = norm_steps

    if "allow_contamination" in raw:
        normalized["allow_contamination"] = raw["allow_contamination"]

    eff_robot_type = "Flex" if (
        detected_flex_slots
        or normalized.get("robot_type") == "Flex"
        or (isinstance(normalized.get("requirements"), Mapping) and normalized["requirements"].get("robotType") == "Flex")
    ) else "OT-2"
    eff_trash_slot = str(normalized.get("trash_slot", DEFAULT_FLEX_TRASH_SLOT)) if eff_robot_type == "Flex" else None

    if "liquids" in raw and isinstance(raw["liquids"], Mapping):
        norm_liquids: dict[str, Any] = {}
        for liq_name, liq_entry in raw["liquids"].items():
            if isinstance(liq_entry, Mapping):
                entry_copy = dict(liq_entry)
                for w_key in ("wells", "well"):
                    if w_key in entry_copy:
                        entry_copy[w_key] = _rewrite_well_expr(entry_copy[w_key], alias_map)
                norm_liquids[str(liq_name)] = entry_copy
            else:
                norm_liquids[str(liq_name)] = liq_entry
        normalized["liquids"] = norm_liquids
    else:
        synthesized = _synthesize_initial_liquids(
            norm_deck,
            norm_steps,
            robot_type=eff_robot_type,
            trash_slot=eff_trash_slot,
        )
        if synthesized:
            normalized["liquids"] = synthesized

    return normalized


def _compute_poiseuille_pressure_drop_pa(
    viscosity_mpa_s: float,
    flow_rate_ul_s: float,
    orifice_radius_mm: float = DEFAULT_ORIFICE_RADIUS_MM,
    cone_length_mm: float = 12.0,
) -> float:
    """Hagen-Poiseuille tip-orifice pressure drop `dP = 8 * mu * L * Q / (pi * r^4)` in Pascals."""
    mu_pa_s = viscosity_mpa_s * 1e-3
    q_m3_s = flow_rate_ul_s * 1e-9
    length_m = cone_length_mm * 1e-3
    radius_m = orifice_radius_mm * 1e-3
    return round(float((8.0 * mu_pa_s * length_m * q_m3_s) / (math.pi * (radius_m ** 4))), 2)


def _build_rheology_telemetry(
    manifest: Mapping[str, Any],
    *,
    liquid_class_hint: str | None,
    rheology_compensated: bool,
) -> dict[str, Any]:
    """Simulate L1 aspiration pressure, viscous equilibration lag, and fluid-delivery error."""
    resolved_lc = (
        liquid_class_hint
        if (liquid_class_hint is not None and liquid_class_hint in LIQUID_CLASS_PROFILES)
        else "aqueous"
    )
    profile = LIQUID_CLASS_PROFILES[resolved_lc]

    vol_ul = 100.0
    dst_wells: tuple[str, ...] = ("A1",)
    src_well_name = "A1"
    src_init_ul = 1000.0
    pip_max_ul = 300.0
    tip_max_ul = 300.0

    pipettes_meta = manifest.get("pipettes")
    if isinstance(pipettes_meta, Mapping) and pipettes_meta:
        first_pip = next(iter(pipettes_meta.values()))
        if isinstance(first_pip, Mapping):
            eff_max = first_pip.get("effective_max_volume_ul")
            if isinstance(eff_max, (int, float)) and eff_max > 0:
                pip_max_ul = float(eff_max)
                tip_max_ul = float(eff_max)

    well_vols = manifest.get("well_volumes")
    steps_meta = manifest.get("steps")
    if isinstance(steps_meta, list):
        for step in steps_meta:
            if not isinstance(step, Mapping):
                continue
            op = step.get("op")
            if op in ("transfer", "distribute"):
                raw_v = step.get("volume_ul")
                if isinstance(raw_v, (int, float)) and raw_v > 0:
                    vol_ul = float(raw_v)
                raw_dst = step.get("dst")
                if isinstance(raw_dst, list) and raw_dst:
                    dst_wells = tuple(str(w).split(":", 1)[-1] for w in raw_dst)
                raw_src = step.get("src")
                src_key = (
                    str(raw_src[0])
                    if isinstance(raw_src, list) and raw_src
                    else (str(raw_src) if isinstance(raw_src, str) else "")
                )
                if src_key:
                    src_well_name = src_key.split(":", 1)[-1]
                    if isinstance(well_vols, Mapping) and src_key in well_vols:
                        src_entry = well_vols[src_key]
                        if isinstance(src_entry, Mapping) and isinstance(
                            src_entry.get("initial_ul"), (int, float)
                        ):
                            src_init_ul = float(src_entry["initial_ul"])
                break

    spec = MultiLiquidTransferSpec(
        transfer_id=str(manifest.get("protocol_name", "wire_transfer")),
        liquid_class=resolved_lc,
        src_well=src_well_name,
        dst_wells=dst_wells,
        volume_ul=vol_ul,
        source_well_volume_ul=src_init_ul,
        pipette_max_ul=pip_max_ul,
        tiprack_max_ul=tip_max_ul,
        nominal_flow_rate_ul_s=DEFAULT_P300_FLOW_RATE_UL_S,
        orifice_radius_mm=DEFAULT_ORIFICE_RADIUS_MM,
    )

    rheology_mode = "rheology_aware" if rheology_compensated else "uncompensated"
    if rheology_compensated:
        try:
            plan = compile_rheology_aware_transfer(spec)
        except RheologyUnderflowError:
            plan = compile_default_aqueous_codegen(spec)
            rheology_mode = "uncompensated_underflow_fallback"
    else:
        plan = compile_default_aqueous_codegen(spec)

    outcome = simulate_transfer_execution(spec, plan)

    tau_equil_s = round(0.018 * profile.viscosity_mpa_s * (plan.aspirate_rate_ul_s / 30.0), 4)
    if profile.viscosity_mpa_s <= 1.5:
        equil_completeness = round(
            1.0 - 0.012 * (plan.aspirate_rate_ul_s / DEFAULT_P300_FLOW_RATE_UL_S), 4
        )
    else:
        equil_completeness = round(
            1.0 - 0.34 * math.exp(-plan.post_aspirate_delay_s / max(tau_equil_s, 1e-4)), 4
        )

    pressure_drop_pa = _compute_poiseuille_pressure_drop_pa(
        profile.viscosity_mpa_s,
        plan.aspirate_rate_ul_s,
        spec.orifice_radius_mm,
    )
    artifact_detected = bool(
        outcome.volatile_drip_fault
        or outcome.source_underflow_fault
        or outcome.shear_lysis_fault
        or outcome.mean_abs_volume_error_pct >= 5.0
    )

    return {
        "liquid_class": resolved_lc,
        "rheology_mode": rheology_mode,
        "aspirate_rate_ul_s": plan.aspirate_rate_ul_s,
        "dispense_rate_ul_s": plan.dispense_rate_ul_s,
        "post_aspirate_delay_s": plan.post_aspirate_delay_s,
        "blowout_delay_s": plan.blowout_delay_s,
        "air_gap_ul": plan.air_gap_ul,
        "pre_wet_cycles": plan.pre_wet_cycles,
        "estimated_pressure_drop_pa": pressure_drop_pa,
        "tau_equil_s": tau_equil_s,
        "equilibration_completeness": equil_completeness,
        "wall_shear_rate_inv_s": plan.wall_shear_rate_inv_s,
        "delivered_volumes_ul": list(outcome.delivered_volumes_ul),
        "mean_abs_volume_error_pct": outcome.mean_abs_volume_error_pct,
        "volatile_drip_fault": outcome.volatile_drip_fault,
        "source_underflow_fault": outcome.source_underflow_fault,
        "shear_lysis_fault": outcome.shear_lysis_fault,
        "pipetting_artifact_detected": artifact_detected,
    }


def _build_execution_telemetry(
    compiled: Mapping[str, Any],
    receipt: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble post-dispatch well-volume, tip-consumption, and rheology telemetry for L4 `lab-attrib`."""
    raw_manifest = compiled.get("manifest")
    manifest: Mapping[str, Any] = raw_manifest if isinstance(raw_manifest, Mapping) else {}

    well_volumes_ul: dict[str, float] = {}
    well_levels_ul: dict[str, dict[str, Any]] = {}

    sim_wells = receipt.get("well_volumes_ul")
    if isinstance(sim_wells, Mapping):
        for wk, wv in sorted(sim_wells.items()):
            if isinstance(wv, (int, float)):
                well_volumes_ul[str(wk)] = round(float(wv), 4)

    manifest_wells = manifest.get("well_volumes")
    if isinstance(manifest_wells, Mapping):
        for wk, entry in sorted(manifest_wells.items()):
            if not isinstance(entry, Mapping):
                continue
            key_s = str(wk)
            init_v = float(entry.get("initial_ul", 0.0))
            peak_v = float(entry.get("peak_ul", init_v))
            final_v = well_volumes_ul.get(key_s, float(entry.get("final_ul", 0.0)))
            cap_v = float(entry.get("capacity_ul", max(peak_v, final_v)))
            well_volumes_ul[key_s] = round(final_v, 4)
            raw_sp = entry.get("species", [])
            species = [str(s) for s in raw_sp] if isinstance(raw_sp, list) else []
            well_levels_ul[key_s] = {
                "initial_ul": round(init_v, 4),
                "peak_ul": round(peak_v, 4),
                "final_ul": round(final_v, 4),
                "capacity_ul": round(cap_v, 4),
                "headspace_ul": round(max(0.0, cap_v - final_v), 4),
                "species": species,
            }

    for wk, final_v in sorted(well_volumes_ul.items()):
        if wk not in well_levels_ul:
            well_levels_ul[wk] = {
                "initial_ul": 0.0,
                "peak_ul": final_v,
                "final_ul": final_v,
                "capacity_ul": final_v,
                "headspace_ul": 0.0,
                "species": [],
            }

    raw_rack_tips = manifest.get("tips_used_per_rack")
    tips_used_per_rack: dict[str, int] = (
        {str(k): int(v) for k, v in raw_rack_tips.items() if isinstance(v, int)}
        if isinstance(raw_rack_tips, Mapping)
        else {}
    )

    tips_used_per_mount: dict[str, int] = {}
    sim_mount_tips = receipt.get("tips_used_per_mount")
    if isinstance(sim_mount_tips, Mapping):
        for mk, mv in sorted(sim_mount_tips.items()):
            if isinstance(mv, int):
                tips_used_per_mount[str(mk)] = mv
    elif isinstance(manifest.get("pipettes"), Mapping):
        for mk, p_meta in sorted(manifest["pipettes"].items()):
            if isinstance(p_meta, Mapping) and isinstance(p_meta.get("tips_used"), int):
                tips_used_per_mount[str(mk)] = int(p_meta["tips_used"])

    total_tips_used = (
        sum(tips_used_per_mount.values())
        if tips_used_per_mount
        else sum(tips_used_per_rack.values())
    )

    lc_hint = _extract_source_liquid_class(packet, compiled.get("raw_protocol_meta", {}))
    if lc_hint is None and isinstance(compiled.get("liquid_class"), str):
        lc_hint = str(compiled["liquid_class"])
    rheo_comp = bool(packet.get("rheology_compensated", compiled.get("rheology_compensated", False)))
    rheo = _build_rheology_telemetry(
        manifest,
        liquid_class_hint=lc_hint,
        rheology_compensated=rheo_comp,
    )

    pipetting_artifact = bool(rheo["pipetting_artifact_detected"])
    liquid_level_ok = not bool(rheo["source_underflow_fault"] or rheo["volatile_drip_fault"])
    aspirate_pressure_ok = not pipetting_artifact

    return {
        "well_volumes_ul": well_volumes_ul,
        "well_levels_ul": well_levels_ul,
        "tips_consumed": int(total_tips_used),
        "tip_telemetry": {
            "tips_used_per_rack": tips_used_per_rack,
            "tips_used_per_mount": tips_used_per_mount,
            "total_tips_used": int(total_tips_used),
        },
        "rheology_and_pressure": rheo,
        "pipetting_artifact_detected": pipetting_artifact,
        "aspirate_pressure_ok": aspirate_pressure_ok,
        "liquid_level_ok": liquid_level_ok,
    }


def compile_stage(packet: dict[str, Any]) -> dict[str, Any]:
    """Compile protocol IR/YAML into Opentrons v2 Python and a `lean-mcp/v1` self-envelope."""
    out = _init_wire_packet(packet)
    if bool(out["halted"]):
        return out

    rehearsal = out.get("rehearsal")
    rehearsal_dict = (
        rehearsal.get("protocol_yaml_dict")
        if isinstance(rehearsal, Mapping)
        else None
    )
    has_rehearsal_dict = isinstance(rehearsal_dict, Mapping) and bool(rehearsal_dict)
    raw_proto_src: Path | str | Mapping[str, Any] = (
        rehearsal_dict
        if rehearsal_dict
        else (REPO_DIR / "examples" / "plate_fill.yaml")
    )
    liquid_class_hint = _extract_source_liquid_class(out, raw_proto_src)
    proto_src = _normalize_protocol_source(
        raw_proto_src,
        is_rehearsal_dict=has_rehearsal_dict,
    )

    bundle = compile_with_mcp_envelope(
        proto_src,
        deck_epoch=int(out.get("deck_epoch", 1)),
        secret_key=str(out.get("mcp_secret_key", DEFAULT_MCP_SECRET_KEY)),
    )
    compiled_block: dict[str, Any] = {
        "python_code": bundle.python_code,
        "protocol_sha256": bundle.protocol_sha256,
        "envelope_bounds": copy.deepcopy(bundle.envelope["envelope_bounds"]),
        "self_envelope": copy.deepcopy(bundle.envelope),
        "manifest": copy.deepcopy(bundle.manifest),
    }
    if liquid_class_hint is not None:
        compiled_block["liquid_class"] = liquid_class_hint
    if "rheology_compensated" in out:
        compiled_block["rheology_compensated"] = bool(out["rheology_compensated"])
    out["stepbot_compiled"] = compiled_block
    return out


def process_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Verify `lean-mcp/v1` capability invariants, simulate Opentrons code, and emit L1 telemetry."""
    out = _init_wire_packet(packet)
    if bool(out["halted"]):
        return out

    if "stepbot_compiled" not in out or not isinstance(out["stepbot_compiled"], Mapping):
        out = compile_stage(out)

    compiled = out["stepbot_compiled"]
    python_code = str(compiled["python_code"])
    compiled_sha256 = str(compiled["protocol_sha256"])

    if out.get("force_envelope") is not None:
        envelope: Any = out["force_envelope"]
    else:
        verification = out.get("verification")
        verif_env = (
            verification.get("mcp_envelope")
            if isinstance(verification, Mapping)
            else None
        )
        if (
            isinstance(verif_env, Mapping)
            and verif_env.get("protocol_sha256") == compiled_sha256
        ):
            envelope = copy.deepcopy(dict(verif_env))
        else:
            envelope = copy.deepcopy(dict(compiled["self_envelope"]))

    try:
        raw_nonces = out.get("consumed_nonces")
        consumed_nonces: list[str] | None = None
        if raw_nonces is not None:
            if not isinstance(raw_nonces, (list, tuple, set, frozenset)):
                raise McpSecurityViolation(
                    "mcp_state_corrupt",
                    f"consumed_nonces must be a sequence or set, got {type(raw_nonces).__name__}",
                )
            consumed_nonces = []
            for item in raw_nonces:
                if not isinstance(item, str) or not item.strip():
                    raise McpSecurityViolation(
                        "mcp_state_corrupt",
                        f"consumed_nonces entries must be non-empty strings, got {item!r}",
                    )
                consumed_nonces.append(item)

        max_nonces_raw = out.get("max_consumed_nonces", DEFAULT_MAX_CONSUMED_NONCES)
        if isinstance(max_nonces_raw, bool) or not isinstance(max_nonces_raw, int) or max_nonces_raw <= 0:
            raise McpSecurityViolation(
                "mcp_state_corrupt",
                f"max_consumed_nonces must be a positive integer, got {max_nonces_raw!r}",
            )

        executor = StepbotMcpExecutor(
            secret_key=str(out.get("mcp_secret_key", DEFAULT_MCP_SECRET_KEY)),
            current_deck_epoch=int(out.get("deck_epoch", 1)),
            consumed_nonces=consumed_nonces,
            use_mock=True,
            max_consumed_nonces=max_nonces_raw,
        )
        receipt = executor.dispatch(python_code, envelope, simulate=True)
    except McpSecurityViolation as exc:
        out["halted"] = True
        out["halt_reason"] = f"L1_MCP_BLOCK:{exc.rule}"
        summary = (
            f"Blocked Opentrons hardware dispatch under lean-mcp/v1 "
            f"({out['halt_reason']}: {exc.reason})."
        )
    else:
        env_out = copy.deepcopy(dict(envelope)) if isinstance(envelope, Mapping) else envelope
        telemetry = _build_execution_telemetry(compiled, receipt, out)
        out["stepbot_execution"] = {
            "python_code": python_code,
            "protocol_sha256": receipt["protocol_sha256"],
            "mcp_envelope": env_out,
            "dispatch_receipt": copy.deepcopy(receipt),
            "next_deck_epoch": receipt["current_deck_epoch"],
            "telemetry": telemetry,
        }
        existing_assay_tel = out.get("assay_telemetry")
        merged_assay_tel: dict[str, Any] = (
            copy.deepcopy(dict(existing_assay_tel))
            if isinstance(existing_assay_tel, Mapping)
            else {}
        )
        for tel_key, tel_val in telemetry.items():
            merged_assay_tel.setdefault(tel_key, copy.deepcopy(tel_val))
        out["assay_telemetry"] = merged_assay_tel

        out["deck_epoch"] = receipt["current_deck_epoch"]
        out["consumed_nonces"] = list(executor.consumed_nonces)
        api_lvl = str(
            compiled.get("envelope_bounds", {}).get("api_level", "2.16")
            if isinstance(compiled.get("envelope_bounds"), Mapping)
            else "2.16"
        )
        summary = (
            f"Compiled and simulated Opentrons v{api_lvl} protocol "
            f"({receipt['command_count']} mock commands, {telemetry['tips_consumed']} tips); "
            f"advanced deck_epoch {receipt['executed_epoch']} -> {receipt['current_deck_epoch']}."
        )

    out["trace"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "status": "BLOCK" if out["halted"] else "PASS",
            "summary": summary,
        }
    )
    out["bottom_reasoning_traces"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "invariant": "mcp_payload_and_epoch_binding",
            "first_principles": (
                "Opentrons Python API v2 dispatch requires canonical HMAC-SHA256 "
                "authenticity, LF-normalized protocol_sha256 payload binding, "
                "envelope_bounds verification, monotonic physical deck_epoch freshness, "
                "bounded single-use capability nonce anti-replay, and static/mock "
                "simulation proof before advancing physical deck state."
            ),
        }
    )
    return out

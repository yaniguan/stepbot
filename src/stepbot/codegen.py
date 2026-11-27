"""Deterministic Opentrons Python API v2 code generator and manifest builder."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from stepbot.catalog import DEFAULT_FLEX_TRASH_SLOT, deck_slot_sort_key
from stepbot.models import (
    CommentStep,
    DelayStep,
    DiscardStep,
    DistributeStep,
    DropTipStep,
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
    plan_distribute_chunks,
    plan_transfer_strokes,
)


def _assert_finite(val: float, context: str = "Codegen number") -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
        raise ValidationError(f"{context} must be a finite number, got {val!r}")
    return float(val)


def _fmt_num(val: float) -> str:
    checked = _assert_finite(val)
    if checked.is_integer():
        return str(int(checked))
    return f"{checked:.4f}".rstrip("0").rstrip(".")


def _fmt_wells(refs: tuple[WellRef, ...], *, position: str = "bottom") -> str:
    if len(refs) == 1:
        return refs[0].expr(position=position)
    items = ", ".join(ref.expr(position=position) for ref in refs)
    return f"[{items}]"


def _pip_var(mount: str) -> str:
    return f"pipette_{mount}"


def _emit_flow_rates(lines: list[str], pv: str, asp_rate: float, disp_rate: float) -> None:
    lines.append(f"    {pv}.flow_rate.aspirate = {_fmt_num(asp_rate)}")
    lines.append(f"    {pv}.flow_rate.dispense = {_fmt_num(disp_rate)}")


def _render_rheology_transfer(
    lines: list[str],
    step: TransferStep,
    pip: PipetteConfig,
    rheo: RheologySpec,
) -> None:
    pv = _pip_var(step.pipette)
    _emit_flow_rates(lines, pv, rheo.aspirate_flow_rate, rheo.dispense_flow_rate)

    srcs, dsts = list(step.src), list(step.dst)
    if len(srcs) == 1 and len(dsts) > 1:
        srcs = srcs * len(dsts)
    elif len(dsts) == 1 and len(srcs) > 1:
        dsts = dsts * len(srcs)

    groups = plan_transfer_strokes(
        step.volume,
        pip.spec.max_volume,
        pip.effective_max_volume,
        air_gap=rheo.air_gap_ul,
    )

    if step.new_tip == "once":
        lines.append(f"    {pv}.pick_up_tip()")

    for src_ref, dst_ref in zip(srcs, dsts):
        dst_disp_expr = dst_ref.expr(position=step.dispense_position)
        dst_well_expr = dst_ref.expr()
        for stroke_group in groups:
            if step.new_tip == "always":
                lines.append(f"    {pv}.pick_up_tip()")
            for stroke_vol in stroke_group:
                lines.append(f"    {pv}.aspirate({_fmt_num(stroke_vol)}, {src_ref.expr()})")
                if rheo.aspirate_delay_s > 0.0:
                    lines.append(f"    protocol.delay(seconds={_fmt_num(rheo.aspirate_delay_s)})")
                if rheo.air_gap_ul > 0.0:
                    lines.append(f"    {pv}.air_gap({_fmt_num(rheo.air_gap_ul)})")
                disp_vol = round(stroke_vol + rheo.air_gap_ul, 4)
                lines.append(f"    {pv}.dispense({_fmt_num(disp_vol)}, {dst_disp_expr})")
                if step.mix_after is not None:
                    reps, mvol = step.mix_after
                    lines.append(f"    {pv}.mix({reps}, {_fmt_num(mvol)}, {dst_disp_expr})")
                if rheo.touch_tip:
                    lines.append(f"    {pv}.touch_tip({dst_well_expr})")
            if step.new_tip == "always":
                lines.append(f"    {pv}.drop_tip()")

    if step.new_tip == "once":
        lines.append(f"    {pv}.drop_tip()")

    _emit_flow_rates(
        lines, pv, pip.spec.nominal_aspirate_rate, pip.spec.nominal_dispense_rate
    )


def _render_rheology_distribute(
    lines: list[str],
    step: DistributeStep,
    pip: PipetteConfig,
    rheo: RheologySpec,
    trash_expr: str,
) -> None:
    pv = _pip_var(step.pipette)
    _emit_flow_rates(lines, pv, rheo.aspirate_flow_rate, rheo.dispense_flow_rate)

    eff_disp = pip.spec.min_volume if step.disposal_volume is None else step.disposal_volume
    chunks = plan_distribute_chunks(
        len(step.dst),
        step.volume,
        pip.effective_max_volume,
        eff_disp,
        air_gap=rheo.air_gap_ul,
    )

    if step.new_tip == "once":
        lines.append(f"    {pv}.pick_up_tip()")

    idx = 0
    for chunk_len in chunks:
        chunk_dsts = step.dst[idx : idx + chunk_len]
        asp_total = round(step.volume * chunk_len + eff_disp, 4)
        lines.append(f"    {pv}.aspirate({_fmt_num(asp_total)}, {step.src.expr()})")
        if rheo.aspirate_delay_s > 0.0:
            lines.append(f"    protocol.delay(seconds={_fmt_num(rheo.aspirate_delay_s)})")
        for dst_ref in chunk_dsts:
            if rheo.air_gap_ul > 0.0:
                lines.append(f"    {pv}.air_gap({_fmt_num(rheo.air_gap_ul)})")
            disp_total = round(step.volume + rheo.air_gap_ul, 4)
            lines.append(
                f"    {pv}.dispense({_fmt_num(disp_total)}, "
                f"{dst_ref.expr(position=step.dispense_position)})"
            )
            if rheo.touch_tip:
                lines.append(f"    {pv}.touch_tip({dst_ref.expr()})")
        if eff_disp > 0.0:
            lines.append(f"    {pv}.blow_out({trash_expr})")
        idx += chunk_len

    if step.new_tip == "once":
        lines.append(f"    {pv}.drop_tip()")

    _emit_flow_rates(
        lines, pv, pip.spec.nominal_aspirate_rate, pip.spec.nominal_dispense_rate
    )


def render_protocol(spec: ProtocolSpec) -> str:
    """Render a validated ProtocolSpec into deterministic Python source."""
    lines: list[str] = []
    is_flex = spec.robot_type == "Flex"

    meta_parts = [f'"protocolName": {spec.name!r}']
    if spec.description:
        meta_parts.append(f'"description": {spec.description!r}')
    if not is_flex:
        meta_parts.append(f'"apiLevel": {spec.api_level!r}')
    lines.append("metadata = {")
    for part in meta_parts:
        lines.append(f"    {part},")
    lines.append("}")

    if is_flex:
        lines.append("")
        lines.append("requirements = {")
        lines.append('    "robotType": \'Flex\',')
        lines.append(f'    "apiLevel": {spec.api_level!r},')
        lines.append("}")

    lines.append("")
    lines.append("")
    lines.append("def run(protocol):")

    trash_expr = "protocol.fixed_trash"
    if is_flex:
        trash_var = "trash" if "trash" not in spec.deck else "trash_bin"
        trash_slot = spec.trash_slot or DEFAULT_FLEX_TRASH_SLOT
        lines.append(f"    {trash_var} = protocol.load_trash_bin({trash_slot!r})")
        trash_expr = trash_var

    for item in sorted(spec.deck.values(), key=lambda d: deck_slot_sort_key(d.slot)):
        lines.append(
            f"    {item.alias} = protocol.load_labware({item.load_name!r}, {item.slot!r})"
        )
    lines.append("")

    for mount in sorted(spec.pipettes):
        pip = spec.pipettes[mount]
        racks = ", ".join(pip.tip_racks)
        lines.append(
            f"    {_pip_var(mount)} = protocol.load_instrument({pip.model!r}, {mount!r}, tip_racks=[{racks}])"
        )
    lines.append("")

    if spec.liquids:
        lines.append("    liquids = {")
        for liquid in spec.liquids:
            lines.append(
                f"        {liquid.name!r}: protocol.define_liquid({liquid.name!r}, "
                f"description={liquid.description!r}, display_color=None),"
            )
        lines.append("    }")
        for liquid in spec.liquids:
            for well in liquid.wells:
                lines.append(
                    f"    {well.expr()}.load_liquid(liquid=liquids[{liquid.name!r}], "
                    f"volume={_fmt_num(liquid.volume)})"
                )
        lines.append("")

    for step in spec.steps:
        if isinstance(step, CommentStep):
            lines.append(f"    protocol.comment({step.text!r})")
        elif isinstance(step, DelayStep):
            _assert_finite(step.minutes, "DelayStep.minutes")
            _assert_finite(step.seconds, "DelayStep.seconds")
            if step.minutes > 0 and step.seconds > 0:
                lines.append(
                    f"    protocol.delay(minutes={_fmt_num(step.minutes)}, "
                    f"seconds={_fmt_num(step.seconds)})"
                )
            elif step.minutes > 0:
                lines.append(f"    protocol.delay(minutes={_fmt_num(step.minutes)})")
            else:
                lines.append(f"    protocol.delay(seconds={_fmt_num(step.seconds)})")
        elif isinstance(step, PauseStep):
            if step.message:
                lines.append(f"    protocol.pause({step.message!r})")
            else:
                lines.append("    protocol.pause()")
        elif isinstance(step, PickUpTipStep):
            lines.append(f"    {_pip_var(step.pipette)}.pick_up_tip()")
        elif isinstance(step, DropTipStep):
            lines.append(f"    {_pip_var(step.pipette)}.drop_tip()")
        elif isinstance(step, TransferStep):
            if step.rheology is not None:
                _render_rheology_transfer(
                    lines, step, spec.pipettes[step.pipette], step.rheology
                )
            else:
                pv = _pip_var(step.pipette)
                args = [
                    _fmt_num(step.volume),
                    _fmt_wells(step.src),
                    _fmt_wells(step.dst, position=step.dispense_position),
                    f"new_tip={step.new_tip!r}",
                ]
                if step.mix_after is not None:
                    reps, mvol = step.mix_after
                    args.append(f"mix_after=({reps}, {_fmt_num(mvol)})")
                lines.append(f"    {pv}.transfer({', '.join(args)})")
        elif isinstance(step, DistributeStep):
            if step.rheology is not None:
                _render_rheology_distribute(
                    lines,
                    step,
                    spec.pipettes[step.pipette],
                    step.rheology,
                    trash_expr,
                )
            else:
                pv = _pip_var(step.pipette)
                args = [
                    _fmt_num(step.volume),
                    step.src.expr(),
                    _fmt_wells(step.dst, position=step.dispense_position),
                    f"new_tip={step.new_tip!r}",
                ]
                if step.disposal_volume is not None:
                    args.append(f"disposal_volume={_fmt_num(step.disposal_volume)}")
                lines.append(f"    {pv}.distribute({', '.join(args)})")
        elif isinstance(step, MixStep):
            pv = _pip_var(step.pipette)
            pip = spec.pipettes[step.pipette]
            if step.rheology is not None:
                _emit_flow_rates(
                    lines,
                    pv,
                    step.rheology.aspirate_flow_rate,
                    step.rheology.dispense_flow_rate,
                )
            for well in step.wells:
                lines.append(
                    f"    {pv}.mix({step.reps}, {_fmt_num(step.volume)}, {well.expr()})"
                )
                if step.rheology is not None and step.rheology.touch_tip:
                    lines.append(f"    {pv}.touch_tip({well.expr()})")
            if step.rheology is not None:
                _emit_flow_rates(
                    lines,
                    pv,
                    pip.spec.nominal_aspirate_rate,
                    pip.spec.nominal_dispense_rate,
                )
        elif isinstance(step, DiscardStep):
            pv = _pip_var(step.pipette)
            for stroke_vol in step.effective_strokes():
                lines.append(f"    {pv}.aspirate({_fmt_num(stroke_vol)}, {step.src.expr()})")
                lines.append(f"    {pv}.dispense({_fmt_num(stroke_vol)}, {trash_expr})")
                lines.append(f"    {pv}.blow_out({trash_expr})")

    lines.append("")
    return "\n".join(lines)


def _serialize_rheology(rheo: RheologySpec) -> dict[str, Any]:
    return {
        "liquid_class": rheo.liquid_class,
        "aspirate_flow_rate_ul_s": _assert_finite(rheo.aspirate_flow_rate),
        "dispense_flow_rate_ul_s": _assert_finite(rheo.dispense_flow_rate),
        "air_gap_ul": _assert_finite(rheo.air_gap_ul),
        "touch_tip": rheo.touch_tip,
        "aspirate_delay_s": _assert_finite(rheo.aspirate_delay_s),
    }


def _serialize_lowered_step(
    idx: int,
    step: LoweredStep,
    origin: StepOrigin | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "lowered_index": idx,
        "source_step_index": origin.source_step_index if origin is not None else None,
        "source_step_type": origin.source_step_type if origin is not None else "unknown",
    }
    if isinstance(step, CommentStep):
        entry.update({"op": "comment", "text": step.text})
    elif isinstance(step, DelayStep):
        entry.update(
            {
                "op": "delay",
                "minutes": _assert_finite(step.minutes, "DelayStep.minutes"),
                "seconds": _assert_finite(step.seconds, "DelayStep.seconds"),
            }
        )
    elif isinstance(step, PauseStep):
        entry.update({"op": "pause", "message": step.message})
    elif isinstance(step, PickUpTipStep):
        entry.update({"op": "pick_up_tip", "pipette": step.pipette})
    elif isinstance(step, DropTipStep):
        entry.update({"op": "drop_tip", "pipette": step.pipette})
    elif isinstance(step, TransferStep):
        mix_list: list[Any] | None = None
        if step.mix_after is not None:
            mix_list = [step.mix_after[0], _assert_finite(step.mix_after[1])]
        entry.update(
            {
                "op": "transfer",
                "pipette": step.pipette,
                "src": [w.key() for w in step.src],
                "dst": [w.key() for w in step.dst],
                "volume_ul": _assert_finite(step.volume, "TransferStep.volume"),
                "new_tip": step.new_tip,
                "mix_after": mix_list,
            }
        )
        if step.dispense_position != "bottom":
            entry["dispense_position"] = step.dispense_position
        if step.rheology is not None:
            entry["rheology"] = _serialize_rheology(step.rheology)
    elif isinstance(step, DistributeStep):
        disp_val = (
            _assert_finite(step.disposal_volume, "DistributeStep.disposal_volume")
            if step.disposal_volume is not None
            else None
        )
        entry.update(
            {
                "op": "distribute",
                "pipette": step.pipette,
                "src": step.src.key(),
                "dst": [w.key() for w in step.dst],
                "volume_ul": _assert_finite(step.volume, "DistributeStep.volume"),
                "new_tip": step.new_tip,
                "disposal_volume_ul": disp_val,
            }
        )
        if step.dispense_position != "bottom":
            entry["dispense_position"] = step.dispense_position
        if step.rheology is not None:
            entry["rheology"] = _serialize_rheology(step.rheology)
    elif isinstance(step, MixStep):
        entry.update(
            {
                "op": "mix",
                "pipette": step.pipette,
                "wells": [w.key() for w in step.wells],
                "volume_ul": _assert_finite(step.volume, "MixStep.volume"),
                "reps": step.reps,
            }
        )
        if step.rheology is not None:
            entry["rheology"] = _serialize_rheology(step.rheology)
    elif isinstance(step, DiscardStep):
        eff = step.effective_strokes()
        for sv in eff:
            _assert_finite(sv, "DiscardStep.stroke_volume")
        entry.update(
            {
                "op": "discard",
                "pipette": step.pipette,
                "src": step.src.key(),
                "volume_ul": _assert_finite(step.volume, "DiscardStep.volume"),
                "strokes": len(eff),
            }
        )
    return entry


def build_protocol_manifest(spec: ProtocolSpec, source: str | None = None) -> dict[str, Any]:
    """Build a deterministic JSON-serializable manifest for downstream Auto-Lab tools."""
    rendered = render_protocol(spec) if source is None else source
    sha256_hex = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    deck_manifest = {
        item.alias: {
            "load_name": item.load_name,
            "slot": item.slot,
            "is_tiprack": item.spec.is_tiprack,
        }
        for item in sorted(spec.deck.values(), key=lambda d: deck_slot_sort_key(d.slot))
    }
    pipette_manifest = {
        mount: {
            "model": pip.model,
            "tip_racks": list(pip.tip_racks),
            "min_volume_ul": _assert_finite(pip.spec.min_volume),
            "effective_max_volume_ul": _assert_finite(pip.effective_max_volume),
            "tips_used": spec.tips_used_per_mount.get(mount, 0),
        }
        for mount, pip in sorted(spec.pipettes.items())
    }
    well_volumes = {
        well_key: {
            "initial_ul": round(_assert_finite(ws.initial_ul), 4),
            "peak_ul": round(_assert_finite(ws.peak_ul), 4),
            "final_ul": round(_assert_finite(ws.final_ul), 4),
            "capacity_ul": round(_assert_finite(ws.capacity_ul), 4),
            "species": list(ws.species),
        }
        for well_key, ws in sorted(spec.well_summaries.items())
    }
    steps_manifest = [
        _serialize_lowered_step(
            idx,
            step,
            spec.step_origins[idx] if idx < len(spec.step_origins) else None,
        )
        for idx, step in enumerate(spec.steps)
    ]

    out: dict[str, Any] = {
        "schema_version": "1.0",
        "protocol_name": spec.name,
        "description": spec.description,
        "api_level": spec.api_level,
        "source_sha256": sha256_hex,
        "deck": deck_manifest,
        "pipettes": pipette_manifest,
        "tips_used_per_rack": dict(sorted(spec.tips_used_per_rack.items())),
        "total_tips_used": sum(spec.tips_used_per_rack.values()),
        "well_volumes": well_volumes,
        "steps": steps_manifest,
    }
    if spec.robot_type != "OT-2":
        out["robot_type"] = spec.robot_type
        out["trash_slot"] = spec.trash_slot or DEFAULT_FLEX_TRASH_SLOT
    # Verify final dictionary is strictly JSON-serializable without NaN/Infinity
    json.dumps(out, allow_nan=False)
    return out

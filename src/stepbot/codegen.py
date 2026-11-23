"""Deterministic Opentrons Python API v2 code generator and manifest builder."""

from __future__ import annotations

import hashlib
from typing import Any

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
    ProtocolSpec,
    StepOrigin,
    TransferStep,
    WellRef,
)


def _fmt_num(val: float) -> str:
    if float(val).is_integer():
        return str(int(val))
    return f"{val:.4f}".rstrip("0").rstrip(".")


def _fmt_wells(refs: tuple[WellRef, ...]) -> str:
    if len(refs) == 1:
        return refs[0].expr()
    items = ", ".join(ref.expr() for ref in refs)
    return f"[{items}]"


def _pip_var(mount: str) -> str:
    return f"pipette_{mount}"


def render_protocol(spec: ProtocolSpec) -> str:
    """Render a validated ProtocolSpec into deterministic Python source."""
    lines: list[str] = []
    meta_parts = [f'"protocolName": {spec.name!r}']
    if spec.description:
        meta_parts.append(f'"description": {spec.description!r}')
    meta_parts.append(f'"apiLevel": {spec.api_level!r}')
    lines.append("metadata = {")
    for part in meta_parts:
        lines.append(f"    {part},")
    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def run(protocol):")

    for item in sorted(spec.deck.values(), key=lambda d: d.slot):
        lines.append(
            f"    {item.alias} = protocol.load_labware({item.load_name!r}, {item.slot})"
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
            pv = _pip_var(step.pipette)
            args = [
                _fmt_num(step.volume),
                _fmt_wells(step.src),
                _fmt_wells(step.dst),
                f"new_tip={step.new_tip!r}",
            ]
            if step.mix_after is not None:
                reps, mvol = step.mix_after
                args.append(f"mix_after=({reps}, {_fmt_num(mvol)})")
            lines.append(f"    {pv}.transfer({', '.join(args)})")
        elif isinstance(step, DistributeStep):
            pv = _pip_var(step.pipette)
            args = [
                _fmt_num(step.volume),
                step.src.expr(),
                _fmt_wells(step.dst),
                f"new_tip={step.new_tip!r}",
            ]
            if step.disposal_volume is not None:
                args.append(f"disposal_volume={_fmt_num(step.disposal_volume)}")
            lines.append(f"    {pv}.distribute({', '.join(args)})")
        elif isinstance(step, MixStep):
            pv = _pip_var(step.pipette)
            for well in step.wells:
                lines.append(
                    f"    {pv}.mix({step.reps}, {_fmt_num(step.volume)}, {well.expr()})"
                )
        elif isinstance(step, DiscardStep):
            pv = _pip_var(step.pipette)
            stroke_vol = step.volume / max(1, step.strokes)
            for _ in range(max(1, step.strokes)):
                lines.append(f"    {pv}.aspirate({_fmt_num(stroke_vol)}, {step.src.expr()})")
                lines.append(f"    {pv}.dispense({_fmt_num(stroke_vol)}, protocol.fixed_trash)")
                lines.append(f"    {pv}.blow_out(protocol.fixed_trash)")

    lines.append("")
    return "\n".join(lines)


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
        entry.update({"op": "delay", "minutes": step.minutes, "seconds": step.seconds})
    elif isinstance(step, PauseStep):
        entry.update({"op": "pause", "message": step.message})
    elif isinstance(step, PickUpTipStep):
        entry.update({"op": "pick_up_tip", "pipette": step.pipette})
    elif isinstance(step, DropTipStep):
        entry.update({"op": "drop_tip", "pipette": step.pipette})
    elif isinstance(step, TransferStep):
        entry.update(
            {
                "op": "transfer",
                "pipette": step.pipette,
                "src": [w.key() for w in step.src],
                "dst": [w.key() for w in step.dst],
                "volume_ul": step.volume,
                "new_tip": step.new_tip,
                "mix_after": list(step.mix_after) if step.mix_after is not None else None,
            }
        )
    elif isinstance(step, DistributeStep):
        entry.update(
            {
                "op": "distribute",
                "pipette": step.pipette,
                "src": step.src.key(),
                "dst": [w.key() for w in step.dst],
                "volume_ul": step.volume,
                "new_tip": step.new_tip,
                "disposal_volume_ul": step.disposal_volume,
            }
        )
    elif isinstance(step, MixStep):
        entry.update(
            {
                "op": "mix",
                "pipette": step.pipette,
                "wells": [w.key() for w in step.wells],
                "volume_ul": step.volume,
                "reps": step.reps,
            }
        )
    elif isinstance(step, DiscardStep):
        entry.update(
            {
                "op": "discard",
                "pipette": step.pipette,
                "src": step.src.key(),
                "volume_ul": step.volume,
                "strokes": step.strokes,
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
        for item in sorted(spec.deck.values(), key=lambda d: d.slot)
    }
    pipette_manifest = {
        mount: {
            "model": pip.model,
            "tip_racks": list(pip.tip_racks),
            "min_volume_ul": pip.spec.min_volume,
            "effective_max_volume_ul": pip.effective_max_volume,
            "tips_used": spec.tips_used_per_mount.get(mount, 0),
        }
        for mount, pip in sorted(spec.pipettes.items())
    }
    well_volumes = {
        well_key: {
            "initial_ul": round(ws.initial_ul, 4),
            "peak_ul": round(ws.peak_ul, 4),
            "final_ul": round(ws.final_ul, 4),
            "capacity_ul": round(ws.capacity_ul, 4),
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

    return {
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

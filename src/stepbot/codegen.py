"""Deterministic Opentrons Python API v2 code generator."""

from __future__ import annotations

from stepbot.models import (
    CommentStep,
    DelayStep,
    DiscardStep,
    DistributeStep,
    DropTipStep,
    MixStep,
    PauseStep,
    PickUpTipStep,
    ProtocolSpec,
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
            f'    {item.alias} = protocol.load_labware({item.load_name!r}, {item.slot})'
        )
    lines.append("")

    for mount in sorted(spec.pipettes):
        pip = spec.pipettes[mount]
        racks = ", ".join(pip.tip_racks)
        lines.append(
            f'    {_pip_var(mount)} = protocol.load_instrument({pip.model!r}, {mount!r}, tip_racks=[{racks}])'
        )
    lines.append("")

    for step in spec.steps:
        if isinstance(step, CommentStep):
            lines.append(f"    protocol.comment({step.text!r})")
        elif isinstance(step, DelayStep):
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
            lines.append(f"    {pv}.distribute({', '.join(args)})")
        elif isinstance(step, MixStep):
            pv = _pip_var(step.pipette)
            for well in step.wells:
                lines.append(
                    f"    {pv}.mix({step.reps}, {_fmt_num(step.volume)}, {well.expr()})"
                )
        elif isinstance(step, DiscardStep):
            pv = _pip_var(step.pipette)
            lines.append(f"    {pv}.aspirate({_fmt_num(step.volume)}, {step.src.expr()})")
            lines.append(f"    {pv}.dispense({_fmt_num(step.volume)}, protocol.fixed_trash)")

    lines.append("")
    return "\n".join(lines)

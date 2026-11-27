"""Idea #18: Rheology-Aware Liquid-Class Lowering Pass for Opentrons API v2.16.

Inspired by PyLabRobot / plr-mcp and Text2WetLab viscous/volatile pipetting failure threads.
Lowers 5 physical liquid classes (`aqueous`, `glycerol_80pct`, `dmso`,
`ethanol_volatile`, `fragile_cells`) into deterministic Opentrons `apiLevel: "2.16"`
Python instructions:
  1. Scales aspirate/dispense flow rates by dynamic viscosity `mu` (mPa*s).
  2. Inserts post-aspirate pressure-equilibration pause and pre-blowout drainage
     delay for high-viscosity `glycerol_80pct`.
  3. Inserts 2x vapor-saturation pre-wet cycles and a trailing `air_gap` for
     `ethanol_volatile` (preventing volatile headspace dripping across deck slots),
     automatically splitting multi-pass strokes so `stroke_vol + air_gap <= eff_tip_max`.
  4. Caps orifice wall shear rate `gamma_dot = 4*Q / (pi * r^3)` for `fragile_cells`
     and disables violent high-speed blowout.
  5. Computes rheology-adjusted dead-volume requirements verified against source well volume.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Any

DEFAULT_API_LEVEL: str = "2.16"
DEFAULT_P300_FLOW_RATE_UL_S: float = 92.86
DEFAULT_ORIFICE_RADIUS_MM: float = 0.42  # standard p300/wide-bore transition orifice


class RheologyUnderflowError(ValueError):
    """Raised when a transfer violates rheology-adjusted source well dead-volume bounds."""


@dataclass(frozen=True)
class LiquidClassProfile:
    """Physical and lowering parameters for a wet-lab liquid class."""

    name: str
    viscosity_mpa_s: float
    vapor_pressure_kpa: float
    surface_tension_mn_m: float
    post_aspirate_delay_s: float
    blowout_delay_s: float
    enable_blowout: bool
    enable_touch_tip: bool
    pre_wet_cycles: int
    air_gap_fraction: float
    max_wall_shear_inv_s: float | None
    base_dead_vol_ul: float
    wall_film_factor: float


LIQUID_CLASS_PROFILES: dict[str, LiquidClassProfile] = {
    "aqueous": LiquidClassProfile(
        name="aqueous",
        viscosity_mpa_s=1.00,
        vapor_pressure_kpa=2.34,
        surface_tension_mn_m=72.8,
        post_aspirate_delay_s=0.0,
        blowout_delay_s=0.0,
        enable_blowout=True,
        enable_touch_tip=False,
        pre_wet_cycles=0,
        air_gap_fraction=0.0,
        max_wall_shear_inv_s=None,
        base_dead_vol_ul=15.0,
        wall_film_factor=0.005,
    ),
    "glycerol_80pct": LiquidClassProfile(
        name="glycerol_80pct",
        viscosity_mpa_s=60.10,
        vapor_pressure_kpa=0.10,
        surface_tension_mn_m=65.2,
        post_aspirate_delay_s=2.0,
        blowout_delay_s=1.0,
        enable_blowout=True,
        enable_touch_tip=True,
        pre_wet_cycles=0,
        air_gap_fraction=0.0,
        max_wall_shear_inv_s=None,
        base_dead_vol_ul=35.0,
        wall_film_factor=0.085,
    ),
    "dmso": LiquidClassProfile(
        name="dmso",
        viscosity_mpa_s=2.00,
        vapor_pressure_kpa=0.08,
        surface_tension_mn_m=43.5,
        post_aspirate_delay_s=0.45,
        blowout_delay_s=0.25,
        enable_blowout=True,
        enable_touch_tip=True,
        pre_wet_cycles=0,
        air_gap_fraction=0.0,
        max_wall_shear_inv_s=None,
        base_dead_vol_ul=20.0,
        wall_film_factor=0.025,
    ),
    "ethanol_volatile": LiquidClassProfile(
        name="ethanol_volatile",
        viscosity_mpa_s=1.20,
        vapor_pressure_kpa=5.95,
        surface_tension_mn_m=22.1,
        post_aspirate_delay_s=0.15,
        blowout_delay_s=0.10,
        enable_blowout=True,
        enable_touch_tip=False,
        pre_wet_cycles=2,
        air_gap_fraction=0.12,
        max_wall_shear_inv_s=None,
        base_dead_vol_ul=25.0,
        wall_film_factor=0.045,
    ),
    "fragile_cells": LiquidClassProfile(
        name="fragile_cells",
        viscosity_mpa_s=1.35,
        vapor_pressure_kpa=2.30,
        surface_tension_mn_m=68.0,
        post_aspirate_delay_s=0.15,
        blowout_delay_s=0.0,
        enable_blowout=False,
        enable_touch_tip=False,
        pre_wet_cycles=0,
        air_gap_fraction=0.0,
        max_wall_shear_inv_s=350.0,
        base_dead_vol_ul=22.0,
        wall_film_factor=0.018,
    ),
}


@dataclass(frozen=True)
class MultiLiquidTransferSpec:
    """High-level specification for a single- or multi-well liquid transfer."""

    transfer_id: str
    liquid_class: str
    src_well: str
    dst_wells: tuple[str, ...]
    volume_ul: float
    source_well_volume_ul: float
    backup_source_well: str | None = None
    backup_source_volume_ul: float = 0.0
    pipette_max_ul: float = 300.0
    tiprack_max_ul: float = 200.0
    nominal_flow_rate_ul_s: float = DEFAULT_P300_FLOW_RATE_UL_S
    orifice_radius_mm: float = DEFAULT_ORIFICE_RADIUS_MM


@dataclass(frozen=True)
class LoweredTransferPlan:
    """Compiled Opentrons v2.16 transfer plan with rheology safety parameters."""

    transfer_id: str
    liquid_class: str
    api_level: str
    active_source_well: str
    active_source_volume_ul: float
    aspirate_rate_ul_s: float
    dispense_rate_ul_s: float
    post_aspirate_delay_s: float
    blowout_delay_s: float
    enable_blowout: bool
    enable_touch_tip: bool
    pre_wet_cycles: int
    air_gap_ul: float
    strokes_per_well: tuple[float, ...]
    required_dead_vol_ul: float
    total_required_source_ul: float
    switched_to_backup_source: bool
    wall_shear_rate_inv_s: float
    python_source: str


@dataclass(frozen=True)
class SimulatedTransferOutcome:
    """Physics simulation metrics for executing a lowered transfer plan."""

    delivered_volumes_ul: tuple[float, ...]
    mean_abs_volume_error_pct: float
    volatile_drip_fault: bool
    source_underflow_fault: bool
    shear_lysis_fault: bool


def compute_wall_shear_rate_inv_s(flow_rate_ul_s: float, orifice_radius_mm: float) -> float:
    """Poiseuille cylindrical orifice wall shear rate `gamma_dot = 4*Q / (pi * r^3)`.

    Note `1 uL = 1 mm^3`, so `Q` in `uL/s` and `r` in `mm` directly yield `s^-1`.
    """
    if orifice_radius_mm <= 0.0:
        raise ValueError("orifice_radius_mm must be positive")
    return float((4.0 * flow_rate_ul_s) / (math.pi * (orifice_radius_mm ** 3)))


def max_flow_rate_for_shear(max_shear_inv_s: float, orifice_radius_mm: float) -> float:
    """Maximum volumetric flow rate `Q` (`uL/s`) keeping wall shear <= `max_shear_inv_s`."""
    return float(max_shear_inv_s * math.pi * (orifice_radius_mm ** 3) / 4.0)


def compute_rheology_dead_volume_ul(
    profile: LiquidClassProfile,
    volume_ul: float,
    n_wells: int,
) -> float:
    """Compute rheology-adjusted minimum source dead-volume + holdup loss (`uL`)."""
    total_nominal = volume_ul * n_wells
    pre_wet_evap_ul = 2.5 * profile.pre_wet_cycles if profile.vapor_pressure_kpa >= 4.0 else 0.0
    film_holdup_ul = profile.wall_film_factor * total_nominal
    return round(profile.base_dead_vol_ul + film_holdup_ul + pre_wet_evap_ul, 4)


def _split_strokes(volume_ul: float, max_liquid_per_stroke_ul: float) -> tuple[float, ...]:
    if volume_ul <= 0.0:
        raise ValueError("volume_ul must be strictly positive")
    if max_liquid_per_stroke_ul <= 0.0:
        raise ValueError("max_liquid_per_stroke_ul must be strictly positive")
    n_strokes = max(1, int(math.ceil(volume_ul / max_liquid_per_stroke_ul - 1e-9)))
    per_stroke = round(volume_ul / n_strokes, 4)
    strokes = [per_stroke] * (n_strokes - 1)
    remainder = round(volume_ul - per_stroke * (n_strokes - 1), 4)
    strokes.append(remainder)
    return tuple(strokes)


def render_opentrons_v216_script(plan: LoweredTransferPlan, dst_wells: tuple[str, ...]) -> str:
    """Emit deterministic Opentrons Python API v2.16 protocol source for a lowered plan."""
    lines: list[str] = [
        "metadata = {",
        f'    "protocolName": {plan.transfer_id!r},',
        f'    "apiLevel": {DEFAULT_API_LEVEL!r},',
        "}",
        "",
        "",
        "def run(protocol):",
        '    reservoir = protocol.load_labware("nest_12_reservoir_15ml", 1)',
        '    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)',
        '    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 3)',
        '    pipette_left = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])',
        f"    reagent = protocol.define_liquid({plan.liquid_class!r}, description='', display_color=None)",
        f"    reservoir[{plan.active_source_well!r}].load_liquid(liquid=reagent, volume={plan.active_source_volume_ul:.2f})",
        f"    pipette_left.flow_rate.aspirate = {plan.aspirate_rate_ul_s:.2f}",
        f"    pipette_left.flow_rate.dispense = {plan.dispense_rate_ul_s:.2f}",
        "    pipette_left.pick_up_tip()",
    ]

    src_expr = f"reservoir[{plan.active_source_well!r}]"
    if plan.pre_wet_cycles > 0:
        pre_wet_vol = round(min(plan.strokes_per_well[0], 50.0), 2)
        for _ in range(plan.pre_wet_cycles):
            lines.append(f"    pipette_left.aspirate({pre_wet_vol}, {src_expr})")
            lines.append(f"    pipette_left.dispense({pre_wet_vol}, {src_expr})")

    for dst in dst_wells:
        dst_expr = f"plate[{dst!r}]"
        for stroke_vol in plan.strokes_per_well:
            lines.append(f"    pipette_left.aspirate({stroke_vol:.2f}, {src_expr})")
            if plan.post_aspirate_delay_s > 0.0:
                lines.append(f"    protocol.delay(seconds={plan.post_aspirate_delay_s:.2f})")
            if plan.air_gap_ul > 0.0:
                lines.append(f"    pipette_left.air_gap({plan.air_gap_ul:.2f})")
            disp_total = round(stroke_vol + plan.air_gap_ul, 4)
            lines.append(f"    pipette_left.dispense({disp_total:.2f}, {dst_expr})")
            if plan.blowout_delay_s > 0.0:
                lines.append(f"    protocol.delay(seconds={plan.blowout_delay_s:.2f})")
            if plan.enable_blowout:
                lines.append(f"    pipette_left.blow_out({dst_expr}.top())")
            if plan.enable_touch_tip:
                lines.append(f"    pipette_left.touch_tip({dst_expr})")

    lines.append("    pipette_left.drop_tip()")
    return "\n".join(lines) + "\n"


def compile_default_aqueous_codegen(spec: MultiLiquidTransferSpec) -> LoweredTransferPlan:
    """Baseline codegen treating every reagent with unscaled aqueous defaults."""
    eff_max = min(spec.pipette_max_ul, spec.tiprack_max_ul)
    strokes = _split_strokes(spec.volume_ul, eff_max)
    naive_dead_vol = 15.0
    total_req = len(spec.dst_wells) * spec.volume_ul + naive_dead_vol
    shear = compute_wall_shear_rate_inv_s(spec.nominal_flow_rate_ul_s, spec.orifice_radius_mm)

    plan_stub = LoweredTransferPlan(
        transfer_id=spec.transfer_id,
        liquid_class=spec.liquid_class,
        api_level=DEFAULT_API_LEVEL,
        active_source_well=spec.src_well,
        active_source_volume_ul=spec.source_well_volume_ul,
        aspirate_rate_ul_s=round(spec.nominal_flow_rate_ul_s, 2),
        dispense_rate_ul_s=round(spec.nominal_flow_rate_ul_s, 2),
        post_aspirate_delay_s=0.0,
        blowout_delay_s=0.0,
        enable_blowout=True,
        enable_touch_tip=False,
        pre_wet_cycles=0,
        air_gap_ul=0.0,
        strokes_per_well=strokes,
        required_dead_vol_ul=naive_dead_vol,
        total_required_source_ul=round(total_req, 4),
        switched_to_backup_source=False,
        wall_shear_rate_inv_s=round(shear, 2),
        python_source="",
    )
    script = render_opentrons_v216_script(plan_stub, spec.dst_wells)
    return LoweredTransferPlan(**{**plan_stub.__dict__, "python_source": script})


def compile_rheology_aware_transfer(spec: MultiLiquidTransferSpec) -> LoweredTransferPlan:
    """Compile a transfer using physical liquid-class rheology rules and dead-volume guards."""
    if spec.liquid_class not in LIQUID_CLASS_PROFILES:
        raise ValueError(f"Unsupported liquid_class: {spec.liquid_class!r}")
    profile = LIQUID_CLASS_PROFILES[spec.liquid_class]
    eff_max = min(spec.pipette_max_ul, spec.tiprack_max_ul)

    # Viscosity flow scaling: Q_safe = Q_0 * (mu_0 / mu)^0.44, clamped to >= 8.0 uL/s
    visc_scale = (1.0 / profile.viscosity_mpa_s) ** 0.44
    asp_rate = max(8.0, spec.nominal_flow_rate_ul_s * visc_scale)
    disp_rate = max(10.0, spec.nominal_flow_rate_ul_s * min(1.0, visc_scale * 1.15))

    if profile.vapor_pressure_kpa >= 4.0:
        # Gentle aspiration avoids low-pressure cavitation in volatile ethanol
        asp_rate = min(asp_rate, 0.58 * spec.nominal_flow_rate_ul_s)

    if profile.max_wall_shear_inv_s is not None:
        shear_cap_q = max_flow_rate_for_shear(
            profile.max_wall_shear_inv_s * 0.92,
            spec.orifice_radius_mm,
        )
        asp_rate = min(asp_rate, shear_cap_q)
        disp_rate = min(disp_rate, shear_cap_q)

    # Compute trailing air_gap and split strokes so stroke_vol + air_gap <= eff_max
    if profile.air_gap_fraction > 0.0:
        air_gap_ul = round(min(20.0, max(10.0, spec.volume_ul * profile.air_gap_fraction)), 2)
    else:
        air_gap_ul = 0.0
    max_liquid_stroke = eff_max - air_gap_ul
    if max_liquid_stroke <= 0.0:
        raise ValueError("Effective tip capacity cannot accommodate required volatile air_gap")
    strokes = _split_strokes(spec.volume_ul, max_liquid_stroke)

    req_dead_vol = compute_rheology_dead_volume_ul(profile, spec.volume_ul, len(spec.dst_wells))
    total_req = round(len(spec.dst_wells) * spec.volume_ul + req_dead_vol, 4)

    active_src = spec.src_well
    active_vol = spec.source_well_volume_ul
    switched = False
    if active_vol < total_req:
        if spec.backup_source_well is not None and spec.backup_source_volume_ul >= total_req:
            active_src = spec.backup_source_well
            active_vol = spec.backup_source_volume_ul
            switched = True
        else:
            raise RheologyUnderflowError(
                f"{spec.transfer_id} ({spec.liquid_class}): required {total_req:.1f} uL "
                f"(incl. {req_dead_vol:.1f} uL rheology dead volume) exceeds source "
                f"{active_vol:.1f} uL"
            )

    shear = compute_wall_shear_rate_inv_s(max(asp_rate, disp_rate), spec.orifice_radius_mm)
    plan_stub = LoweredTransferPlan(
        transfer_id=spec.transfer_id,
        liquid_class=spec.liquid_class,
        api_level=DEFAULT_API_LEVEL,
        active_source_well=active_src,
        active_source_volume_ul=active_vol,
        aspirate_rate_ul_s=round(asp_rate, 2),
        dispense_rate_ul_s=round(disp_rate, 2),
        post_aspirate_delay_s=profile.post_aspirate_delay_s,
        blowout_delay_s=profile.blowout_delay_s,
        enable_blowout=profile.enable_blowout,
        enable_touch_tip=profile.enable_touch_tip,
        pre_wet_cycles=profile.pre_wet_cycles,
        air_gap_ul=air_gap_ul,
        strokes_per_well=strokes,
        required_dead_vol_ul=req_dead_vol,
        total_required_source_ul=total_req,
        switched_to_backup_source=switched,
        wall_shear_rate_inv_s=round(shear, 2),
        python_source="",
    )
    script = render_opentrons_v216_script(plan_stub, spec.dst_wells)
    return LoweredTransferPlan(**{**plan_stub.__dict__, "python_source": script})


def simulate_transfer_execution(
    spec: MultiLiquidTransferSpec,
    plan: LoweredTransferPlan,
) -> SimulatedTransferOutcome:
    """Mechanistic fluid-delivery simulator capturing viscosity lag, volatile drip, and dead volume."""
    profile = LIQUID_CLASS_PROFILES[spec.liquid_class]
    remaining_src_ul = plan.active_source_volume_ul - profile.base_dead_vol_ul

    # Pre-wet evaporation loss in volatile solvents
    if plan.pre_wet_cycles > 0 and profile.vapor_pressure_kpa >= 4.0:
        remaining_src_ul -= 2.0 * plan.pre_wet_cycles

    delivered_per_well: list[float] = []
    volatile_drip_fault = False
    source_underflow_fault = False

    # Hagen-Poiseuille relaxation time constant proportional to viscosity and speed ratio
    tau_equil_s = 0.018 * profile.viscosity_mpa_s * (plan.aspirate_rate_ul_s / 30.0)
    equil_completeness = 1.0 - 0.34 * math.exp(-plan.post_aspirate_delay_s / max(tau_equil_s, 1e-4))
    if profile.viscosity_mpa_s <= 1.5:
        equil_completeness = 1.0 - 0.012 * (plan.aspirate_rate_ul_s / DEFAULT_P300_FLOW_RATE_UL_S)

    # Viscous interior wall retention fraction during dispense
    tau_drain_s = 0.014 * profile.viscosity_mpa_s
    wall_holdup_frac = (
        0.16 * (1.0 - 1.0 / profile.viscosity_mpa_s) * math.exp(-plan.blowout_delay_s / max(tau_drain_s, 1e-4))
        if profile.viscosity_mpa_s > 1.5
        else 0.003
    )
    exterior_wick_loss_ul = (
        1.2 if (profile.surface_tension_mn_m < 50.0 and profile.viscosity_mpa_s >= 1.8 and not plan.enable_touch_tip)
        else 0.0
    )

    for _ in spec.dst_wells:
        well_delivered = 0.0
        for stroke_vol in plan.strokes_per_well:
            gross_draw = stroke_vol * (1.0 + profile.wall_film_factor)
            if remaining_src_ul < gross_draw:
                source_underflow_fault = True
                actual_drawn = max(0.0, remaining_src_ul) * equil_completeness
                remaining_src_ul = 0.0
            else:
                remaining_src_ul -= gross_draw
                actual_drawn = stroke_vol * equil_completeness

            # Volatile headspace expansion dripping during deck transit
            drip_loss_ul = 0.0
            if profile.vapor_pressure_kpa >= 4.0:
                unwetted_factor = 1.0 if plan.pre_wet_cycles == 0 else 0.08
                if plan.air_gap_ul <= 0.0 or plan.pre_wet_cycles == 0:
                    volatile_drip_fault = True
                    drip_loss_ul = stroke_vol * 0.13 * unwetted_factor
                else:
                    drip_loss_ul = stroke_vol * 0.003

            dispensed = max(0.0, actual_drawn * (1.0 - wall_holdup_frac) - drip_loss_ul - exterior_wick_loss_ul)
            well_delivered += dispensed

        delivered_per_well.append(round(well_delivered, 4))

    abs_errs = [abs(v - spec.volume_ul) / spec.volume_ul * 100.0 for v in delivered_per_well]
    mean_err_pct = sum(abs_errs) / len(abs_errs)

    shear_lysis_fault = False
    if profile.max_wall_shear_inv_s is not None:
        if plan.wall_shear_rate_inv_s > profile.max_wall_shear_inv_s or plan.enable_blowout:
            shear_lysis_fault = True

    return SimulatedTransferOutcome(
        delivered_volumes_ul=tuple(delivered_per_well),
        mean_abs_volume_error_pct=round(mean_err_pct, 4),
        volatile_drip_fault=volatile_drip_fault,
        source_underflow_fault=source_underflow_fault,
        shear_lysis_fault=shear_lysis_fault,
    )


def run_benchmark(seed: int = 20261123) -> dict[str, Any]:
    """Benchmark 80 multi-liquid transfers across all 5 physical liquid classes.

    Compares `default_aqueous_codegen` vs `rheology_aware_compiler` (16 transfers per
    liquid class = 80 transfers), reporting volume-delivery error %, volatile-drip
    faults, source-underflow faults, and fragile-cell shear faults.
    """
    rng = random.Random(seed)
    classes = tuple(LIQUID_CLASS_PROFILES.keys())
    specs: list[MultiLiquidTransferSpec] = []

    for idx in range(80):
        lclass = classes[idx % len(classes)]
        vol_ul = float(rng.choice([40.0, 75.0, 120.0, 180.0]))
        n_wells = int(rng.choice([2, 3, 4]))
        dst_wells = tuple(f"A{w + 1}" for w in range(n_wells))

        # Every 4th non-aqueous transfer has a tight primary source well where naive
        # aqueous dead-volume passes (`n*vol + 18 uL`) but viscous film / pre-wet
        # holdup requires switching to the topped-up backup reservoir well `A2`.
        tight_source = (lclass != "aqueous") and ((idx // len(classes)) % 3 == 0)
        if tight_source:
            src_vol = n_wells * vol_ul + 18.0
        else:
            src_vol = n_wells * vol_ul + 140.0

        specs.append(
            MultiLiquidTransferSpec(
                transfer_id=f"xfer_{idx:03d}_{lclass}",
                liquid_class=lclass,
                src_well="A1",
                dst_wells=dst_wells,
                volume_ul=vol_ul,
                source_well_volume_ul=src_vol,
                backup_source_well="A2",
                backup_source_volume_ul=n_wells * vol_ul + 250.0,
            )
        )

    base_errs: list[float] = []
    rheo_errs: list[float] = []
    base_per_class: dict[str, list[float]] = {c: [] for c in classes}
    rheo_per_class: dict[str, list[float]] = {c: [] for c in classes}

    base_drip = base_underflow = base_shear = 0
    rheo_drip = rheo_underflow = rheo_shear = 0
    backup_switches = 0

    for spec in specs:
        base_plan = compile_default_aqueous_codegen(spec)
        rheo_plan = compile_rheology_aware_transfer(spec)
        if rheo_plan.switched_to_backup_source:
            backup_switches += 1

        base_out = simulate_transfer_execution(spec, base_plan)
        rheo_out = simulate_transfer_execution(spec, rheo_plan)

        base_errs.append(base_out.mean_abs_volume_error_pct)
        rheo_errs.append(rheo_out.mean_abs_volume_error_pct)
        base_per_class[spec.liquid_class].append(base_out.mean_abs_volume_error_pct)
        rheo_per_class[spec.liquid_class].append(rheo_out.mean_abs_volume_error_pct)

        base_drip += int(base_out.volatile_drip_fault)
        base_underflow += int(base_out.source_underflow_fault)
        base_shear += int(base_out.shear_lysis_fault)

        rheo_drip += int(rheo_out.volatile_drip_fault)
        rheo_underflow += int(rheo_out.source_underflow_fault)
        rheo_shear += int(rheo_out.shear_lysis_fault)

    return {
        "idea_id": "idea_18_rheology_liquid_class_compiler",
        "api_level": DEFAULT_API_LEVEL,
        "n_transfers": len(specs),
        "liquid_classes": list(classes),
        "default_aqueous_codegen": {
            "mean_volume_delivery_error_pct": round(sum(base_errs) / len(base_errs), 4),
            "max_volume_delivery_error_pct": round(max(base_errs), 4),
            "volatile_drip_faults": base_drip,
            "source_underflow_faults": base_underflow,
            "shear_lysis_faults": base_shear,
            "per_class_error_pct": {
                c: round(sum(vals) / len(vals), 4) for c, vals in base_per_class.items()
            },
        },
        "rheology_aware_compiler": {
            "mean_volume_delivery_error_pct": round(sum(rheo_errs) / len(rheo_errs), 4),
            "max_volume_delivery_error_pct": round(max(rheo_errs), 4),
            "volatile_drip_faults": rheo_drip,
            "source_underflow_faults": rheo_underflow,
            "shear_lysis_faults": rheo_shear,
            "compile_time_underflow_interceptions": backup_switches,
            "per_class_error_pct": {
                c: round(sum(vals) / len(vals), 4) for c, vals in rheo_per_class.items()
            },
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_benchmark(), indent=2))

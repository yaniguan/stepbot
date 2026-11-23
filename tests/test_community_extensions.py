"""Unit and benchmark tests for stepbot community_extensions (Idea #18)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from community_extensions import (  # noqa: E402
    LIQUID_CLASS_PROFILES,
    MultiLiquidTransferSpec,
    RheologyUnderflowError,
    compile_default_aqueous_codegen,
    compile_rheology_aware_transfer,
    run_benchmark,
    simulate_transfer_execution,
)


def test_all_five_liquid_classes_emit_valid_opentrons_v216_python() -> None:
    expected = {"aqueous", "glycerol_80pct", "dmso", "ethanol_volatile", "fragile_cells"}
    assert set(LIQUID_CLASS_PROFILES.keys()) == expected

    for lclass in expected:
        spec = MultiLiquidTransferSpec(
            transfer_id=f"test_{lclass}",
            liquid_class=lclass,
            src_well="A1",
            dst_wells=("A1", "A2"),
            volume_ul=120.0,
            source_well_volume_ul=500.0,
        )
        plan = compile_rheology_aware_transfer(spec)
        assert plan.api_level == "2.16"
        assert '"apiLevel": \'2.16\'' in plan.python_source or '"apiLevel": "2.16"' in plan.python_source
        tree = ast.parse(plan.python_source)
        fn_names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert "run" in fn_names


def test_glycerol_equilibration_and_flow_scaling() -> None:
    spec = MultiLiquidTransferSpec(
        transfer_id="gly_test",
        liquid_class="glycerol_80pct",
        src_well="A1",
        dst_wells=("B1",),
        volume_ul=100.0,
        source_well_volume_ul=300.0,
    )
    aq = compile_default_aqueous_codegen(spec)
    gly = compile_rheology_aware_transfer(spec)

    assert gly.aspirate_rate_ul_s < 0.25 * aq.aspirate_rate_ul_s
    assert gly.post_aspirate_delay_s >= 1.5
    assert gly.blowout_delay_s >= 0.8
    assert "protocol.delay(seconds=2.00)" in gly.python_source
    assert "pipette_left.touch_tip(" in gly.python_source


def test_volatile_ethanol_prewet_airgap_and_tip_capacity_splitting() -> None:
    # 195 uL + air_gap exceeds 200 uL tiprack cap -> must automatically split into 2 strokes!
    spec = MultiLiquidTransferSpec(
        transfer_id="eth_split",
        liquid_class="ethanol_volatile",
        src_well="A1",
        dst_wells=("C1",),
        volume_ul=195.0,
        source_well_volume_ul=400.0,
        pipette_max_ul=300.0,
        tiprack_max_ul=200.0,
    )
    plan = compile_rheology_aware_transfer(spec)
    assert plan.pre_wet_cycles >= 2
    assert plan.air_gap_ul > 0.0
    assert len(plan.strokes_per_well) == 2
    for stroke in plan.strokes_per_well:
        assert stroke + plan.air_gap_ul <= 200.0
    assert "pipette_left.air_gap(" in plan.python_source


def test_fragile_cells_shear_cap_and_no_blowout() -> None:
    spec = MultiLiquidTransferSpec(
        transfer_id="cells_gentle",
        liquid_class="fragile_cells",
        src_well="A1",
        dst_wells=("D1", "D2"),
        volume_ul=80.0,
        source_well_volume_ul=300.0,
    )
    plan = compile_rheology_aware_transfer(spec)
    max_shear = LIQUID_CLASS_PROFILES["fragile_cells"].max_wall_shear_inv_s
    assert max_shear is not None
    assert plan.wall_shear_rate_inv_s <= max_shear
    assert plan.enable_blowout is False
    assert "blow_out" not in plan.python_source


def test_rheology_dead_volume_blocks_unbacked_source_underflow() -> None:
    # 2 * 100 = 200 uL nominal; 220 uL passes naive 15 uL dead-volume, but 80% glycerol
    # requires 35 + 0.085*200 = 52 uL dead-volume -> 252 uL > 220 uL -> raises compile error!
    spec = MultiLiquidTransferSpec(
        transfer_id="gly_underflow",
        liquid_class="glycerol_80pct",
        src_well="A1",
        dst_wells=("A1", "A2"),
        volume_ul=100.0,
        source_well_volume_ul=220.0,
        backup_source_well=None,
    )
    with pytest.raises(RheologyUnderflowError, match="rheology dead volume"):
        compile_rheology_aware_transfer(spec)

    # Naive codegen compiles blindly and suffers runtime source underflow in simulation
    naive = compile_default_aqueous_codegen(spec)
    naive_sim = simulate_transfer_execution(spec, naive)
    assert naive_sim.source_underflow_fault is True


def test_benchmark_80_multi_liquid_transfers() -> None:
    report = run_benchmark()
    assert report["n_transfers"] == 80
    assert report["api_level"] == "2.16"
    assert len(report["liquid_classes"]) == 5

    naive = report["default_aqueous_codegen"]
    rheo = report["rheology_aware_compiler"]

    assert naive["volatile_drip_faults"] == 16
    assert naive["source_underflow_faults"] > 0
    assert naive["shear_lysis_faults"] == 16
    assert naive["mean_volume_delivery_error_pct"] > 10.0

    assert rheo["volatile_drip_faults"] == 0
    assert rheo["source_underflow_faults"] == 0
    assert rheo["shear_lysis_faults"] == 0
    assert rheo["mean_volume_delivery_error_pct"] < 2.0
    assert rheo["compile_time_underflow_interceptions"] > 0

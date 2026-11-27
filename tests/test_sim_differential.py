"""Differential test of `stepbot` mock checker against `opentrons.simulate` (Issue #36)."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import io
import logging
import os
from pathlib import Path
import random
import sys
import time
from typing import Any
import warnings

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from stepbot import (  # noqa: E402
    PrimitiveAction,
    ValidationError,
    check_protocol_source,
    compile_file,
    compile_protocol,
    parse_and_validate,
    reduce_mock_log_actions,
    reduce_opentrons_runlog_actions,
)

# Documented allowed differences between `stepbot` mock and `opentrons.simulate` 8.8.2.
# Kept intentionally empty: all accepted protocols must agree 100.0%.
ALLOWED_DIFFERENCES: dict[str, str] = {}


def _init_sim_worker() -> None:
    logging.disable(logging.WARNING)
    warnings.simplefilter("ignore", UserWarning)


def _run_opentrons_sim_worker(
    task: tuple[str, str, int],
) -> tuple[str, list[PrimitiveAction], int]:
    from opentrons import simulate as ot_simulate

    proto_id, py_code, channels = task
    logging.disable(logging.WARNING)
    runlog, _ = ot_simulate.simulate(io.StringIO(py_code))
    ot_actions = reduce_opentrons_runlog_actions(runlog)
    ot_tips = sum(1 for a in ot_actions if a[0] == "pick_up_tip") * channels
    return proto_id, ot_actions, ot_tips


def _generate_random_protocol(rng: random.Random, idx: int) -> dict[str, Any]:
    """Generate a compact, diverse protocol exercising single/multi-channel & rheology paths."""
    is_flex = idx == 104  # 1 Flex protocol per seed (3 Flex + >305 OT-2 protocols across 3 seeds)
    if is_flex:
        pip_choice = rng.choice(
            [
                ("flex_1channel_50", "opentrons_flex_96_tiprack_50ul", 6.0, 45.0, 1),
                ("flex_8channel_50", "opentrons_flex_96_filtertiprack_50ul", 6.0, 45.0, 8),
                ("flex_8channel_1000", "opentrons_flex_96_tiprack_1000ul", 20.0, 250.0, 8),
            ]
        )
        pip_model, tip_lw, v_lo, v_hi, channels = pip_choice
        plate_lw = "nest_96_wellplate_2ml_deep" if v_hi > 150 else "corning_96_wellplate_360ul_flat"
        deck: dict[str, Any] = {
            "plate": {"labware": plate_lw, "slot": "D1"},
            "res": {"labware": "nest_12_reservoir_15ml", "slot": "D2"},
            "tips1": {"labware": tip_lw, "slot": "C1"},
            "tips2": {"labware": tip_lw, "slot": "C2"},
        }
        robot_meta: dict[str, Any] = {"robot_type": "Flex"}
    else:
        configs = [
            ("p20_single_gen2", "opentrons_96_tiprack_20ul", 2.0, 20.0, 1),
            ("p20_single_gen2", "opentrons_96_filtertiprack_20ul", 2.0, 20.0, 1),
            ("p300_single_gen2", "opentrons_96_tiprack_300ul", 25.0, 300.0, 1),
            ("p300_single_gen2", "opentrons_96_filtertiprack_200ul", 25.0, 200.0, 1),
            ("p1000_single_gen2", "opentrons_96_tiprack_1000ul", 120.0, 900.0, 1),
            ("p20_multi_gen2", "opentrons_96_tiprack_20ul", 2.0, 20.0, 8),
            ("p300_multi_gen2", "opentrons_96_tiprack_300ul", 25.0, 250.0, 8),
            ("p300_multi_gen2", "opentrons_96_filtertiprack_200ul", 25.0, 180.0, 8),
        ]
        pip_model, tip_lw, v_lo, v_hi, channels = configs[idx % len(configs)]
        plate_lw = "nest_96_wellplate_2ml_deep"
        deck = {
            "plate": {"labware": plate_lw, "slot": 1},
            "res": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips1": {"labware": tip_lw, "slot": 3},
            "tips2": {"labware": tip_lw, "slot": 4},
        }
        robot_meta = {}

    liquids: dict[str, Any] = {
        "reagent_a": {"wells": "res:A1", "volume": 14000.0},
        "reagent_b": {"wells": "res:A2", "volume": 14000.0},
        "diluent": {"wells": "res:A3", "volume": 14000.0},
    }

    col_Cursor = 1

    def _alloc_wells(n: int) -> list[str]:
        nonlocal col_Cursor
        if channels == 8:
            wells = [f"plate:A{col_Cursor + k}" for k in range(n)]
            col_Cursor += n
            return wells
        rows = "ABCDEFGH"
        wells = []
        for _ in range(n):
            r_idx = (col_Cursor - 1) % 8
            c_idx = (col_Cursor - 1) // 8 + 1
            wells.append(f"plate:{rows[r_idx]}{c_idx}")
            col_Cursor += 1
        return wells

    steps: list[dict[str, Any]] = []
    pattern = idx % 5

    if pattern == 0:
        # Transfer (including multi_aspirate > max_vol and optional mix_after / rheology)
        n_dst = rng.randint(1, 3)
        dst_wells = _alloc_wells(n_dst)
        use_multi = rng.random() < 0.55
        if use_multi:
            vol = round(rng.uniform(v_hi * 1.15, v_hi * 2.4), 1)
        else:
            vol = round(rng.uniform(v_lo, v_hi * 0.85), 1)
        t_step: dict[str, Any] = {
            "src": rng.choice(["res:A1", "res:A2"]),
            "dst": dst_wells if len(dst_wells) > 1 else dst_wells[0],
            "volume": vol,
            "new_tip": rng.choice(["once", "always", "never"]),
            "multi_aspirate": use_multi,
            "dispense_position": rng.choice(["bottom", "top"]),
        }
        if rng.random() < 0.35:
            m_vol = round(max(v_lo, min(v_hi * 0.5, vol * 0.4)), 1)
            t_step["mix_after"] = [rng.randint(1, 2), m_vol]
        if rng.random() < 0.25:
            t_step["liquid_class"] = rng.choice(
                ["aqueous", "viscous_glycerol", "volatile_ethanol", "serum"]
            )
        steps.append({"transfer": t_step})

    elif pattern == 1:
        # Distribute with disposal_volume == None, 0, or > 0, followed by optional mix
        n_dst = rng.randint(2, 4)
        dst_wells = _alloc_wells(n_dst)
        vol = round(rng.uniform(v_lo, v_hi * 0.45), 1)
        disp_choice = rng.choice([None, 0.0, round(v_lo * 0.5, 1)])
        d_step: dict[str, Any] = {
            "src": "res:A1",
            "dst": dst_wells,
            "volume": vol,
            "new_tip": rng.choice(["once", "never"]),
            "dispense_position": rng.choice(["bottom", "top"]),
        }
        if disp_choice is not None:
            d_step["disposal_volume"] = disp_choice
        if rng.random() < 0.2:
            d_step["liquid_class"] = rng.choice(["aqueous", "serum", "volatile_ethanol"])
        steps.append({"distribute": d_step})
        if rng.random() < 0.5:
            steps.append(
                {
                    "mix": {
                        "wells": dst_wells[:2],
                        "volume": vol,
                        "reps": rng.randint(1, 2),
                        "new_tip": rng.choice(["once", "always"]),
                    }
                }
            )

    elif pattern == 2:
        # Two-reagent assay: pre-fill buffer, then add dye from above (`dispense_position: top`) or `always`
        n_dst = rng.randint(2, 4)
        dst_wells = _alloc_wells(n_dst)
        v1 = round(rng.uniform(v_lo * 1.2, v_hi * 0.45), 1)
        v2 = round(rng.uniform(v_lo, v_hi * 0.35), 1)
        steps.append(
            {
                "distribute": {
                    "src": "res:A1",
                    "dst": dst_wells,
                    "volume": v1,
                    "disposal_volume": 0 if rng.random() < 0.5 else None,
                }
            }
        )
        mode_top = rng.random() < 0.6
        steps.append(
            {
                "transfer": {
                    "src": "res:A2",
                    "dst": dst_wells,
                    "volume": v2,
                    "new_tip": "once" if mode_top else "always",
                    "dispense_position": "top" if mode_top else "bottom",
                }
            }
        )

    elif pattern == 3:
        # Dilution series (row or col for 1-ch; row across column heads for 8-ch)
        n_ser = rng.randint(3, 5)
        direction = "row" if channels == 8 else rng.choice(["row", "col"])
        first_well = "plate:A1"
        factor = rng.choice([2, 2.5, 3, 5])
        final_vol = round(rng.uniform(v_lo * 1.5, v_hi * 0.42), 1)
        steps.append(
            {
                "dilution_series": {
                    "source": "res:A2",
                    "diluent": "res:A3",
                    "first_well": first_well,
                    "n": n_ser,
                    "factor": factor,
                    "final_volume": final_vol,
                    "direction": direction,
                    "mix_reps": rng.randint(1, 2),
                    "multi_aspirate": True,
                }
            }
        )

    else:
        # Chained `new_tip: never` transfer + mix + discard on same reagent
        dst_wells = _alloc_wells(2)
        vol = round(rng.uniform(v_lo * 1.5, v_hi * 0.6), 1)
        disc_vol = round(max(v_lo, vol * 0.5), 1)
        steps.extend(
            [
                {
                    "transfer": {
                        "src": "res:A1",
                        "dst": dst_wells,
                        "volume": vol,
                        "new_tip": "never",
                    }
                },
                {
                    "mix": {
                        "wells": dst_wells[0],
                        "volume": disc_vol,
                        "reps": 2,
                        "new_tip": "never",
                    }
                },
                {
                    "discard": {
                        "src": dst_wells[1],
                        "volume": disc_vol,
                        "new_tip": "once",
                    }
                },
            ]
        )

    return {
        "metadata": {"name": f"Diff random {idx}"},
        **robot_meta,
        "deck": deck,
        "pipettes": {"left": {"model": pip_model, "tip_racks": ["tips1", "tips2"]}},
        "liquids": liquids,
        "steps": steps,
    }


def test_Mock_and_opentrons_log_reducer_unit() -> None:
    mock_lines = [
        "Comment: demo",
        "Picking up tip from A1 of opentrons_96_tiprack_300ul on slot 3",
        "Aspirating 100.0 uL from A1 of nest_12_reservoir_15ml on slot 2",
        "Air gap 10.0 uL",
        "Dispensing 110.0 uL into A1 of corning_96_wellplate_360ul_flat on slot 1",
        "Touching tip",
        "Blowing out into Trash Bin on slot 12",
        "Dropping tip into Trash Bin on slot 12",
    ]
    fake_ot_runlog = [
        {"payload": {"text": "Comment: demo"}, "logs": []},
        {
            "payload": {"text": "Picking up tip from A1 of Opentrons OT-2 96 Tip Rack 300 uL on slot 3"},
            "logs": [],
        },
        {
            "payload": {
                "text": "Aspirating 100.0 uL from A1 of NEST 12 Well Reservoir 15 mL on slot 2 at 25.0 uL/sec"
            },
            "logs": [],
        },
        {
            "payload": {"text": "Air gap of 10 uL"},
            "logs": [
                {
                    "payload": {
                        "text": "Aspirating 10.0 uL from A1 of NEST 12 Well Reservoir 15 mL on slot 2 at 25.0 uL/sec"
                    },
                    "logs": [],
                }
            ],
        },
        {
            "payload": {
                "text": "Dispensing 110.0 uL into A1 of Corning 96 Well Plate 360 uL Flat on slot 1 at 30.0 uL/sec"
            },
            "logs": [],
        },
        {"payload": {"text": "Touching tip"}, "logs": []},
        {"payload": {"text": "Blowing out into Trash Bin on slot 12"}, "logs": []},
        {"payload": {"text": "Dropping tip into Trash Bin on slot 12"}, "logs": []},
    ]
    assert reduce_mock_log_actions(mock_lines) == reduce_opentrons_runlog_actions(fake_ot_runlog)


def test_issue_36_bundled_examples_primitive_actions_and_tips_agree_100pct() -> None:
    ot_simulate = pytest.importorskip("opentrons.simulate")

    expected = [
        ("dilution_series.yaml", 83, 3),
        ("plate_fill.yaml", 117, 14),
        ("reservoir_mix.yaml", 105, 13),
    ]
    for yaml_name, exp_actions, exp_tips in expected:
        yaml_path = ROOT / "examples" / yaml_name
        py_code = compile_file(yaml_path)
        mock_res = check_protocol_source(py_code, use_mock=True)
        assert mock_res.ok, mock_res.errors
        mock_actions = reduce_mock_log_actions(mock_res.lines)

        runlog, _ = ot_simulate.simulate(io.StringIO(py_code))
        ot_actions = reduce_opentrons_runlog_actions(runlog)

        assert mock_actions == ot_actions, f"Mismatch on {yaml_name}"
        assert len(mock_actions) == exp_actions
        assert sum(mock_res.tips_used_per_mount.values()) == exp_tips


def test_issue_36_differential_300_plus_seeded_protocols_against_opentrons_simulate() -> None:
    pytest.importorskip("opentrons.simulate")
    import opentrons

    t_start = time.perf_counter()
    seeds = (202601, 202602, 202603)
    per_seed = 108  # 324 candidates -> > 305 accepted protocols across all features

    accepted = 0
    rejected = 0
    matched = 0
    mismatches: list[str] = []
    mock_expectations: dict[str, tuple[list[PrimitiveAction], int]] = {}
    sim_tasks: list[tuple[str, str, int]] = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        for seed in seeds:
            rng = random.Random(seed)
            for offset in range(per_seed):
                proto_id = f"seed{seed}_{offset:03d}"
                proto_dict = _generate_random_protocol(rng, offset)
                try:
                    spec = parse_and_validate(proto_dict)
                    py_code = compile_protocol(proto_dict)
                except ValidationError:
                    rejected += 1
                    continue

                accepted += 1
                mock_res = check_protocol_source(py_code, use_mock=True)
                assert mock_res.ok, f"{proto_id} failed mock check: {mock_res.errors}"
                mock_actions: list[PrimitiveAction] = reduce_mock_log_actions(mock_res.lines)
                mock_tips = sum(mock_res.tips_used_per_mount.values())
                compiler_tips = sum(spec.tips_used_per_mount.values())
                assert mock_tips == compiler_tips, (
                    f"{proto_id} compiler/mock tip mismatch: {compiler_tips} vs {mock_tips}"
                )

                channels = next(iter(spec.pipettes.values())).spec.channels
                mock_expectations[proto_id] = (mock_actions, mock_tips)
                sim_tasks.append((proto_id, py_code, channels))

    workers = min(4, os.cpu_count() or 2)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_sim_worker) as pool:
        for proto_id, ot_actions, ot_tips in pool.map(
            _run_opentrons_sim_worker, sim_tasks, chunksize=16
        ):
            exp_actions, exp_tips = mock_expectations[proto_id]
            if exp_actions == ot_actions and exp_tips == ot_tips:
                matched += 1
            elif proto_id in ALLOWED_DIFFERENCES:
                matched += 1
            else:
                mismatches.append(
                    f"{proto_id}: tips mock={exp_tips} ot={ot_tips}, "
                    f"actions mock={len(exp_actions)} ot={len(ot_actions)}"
                )

    elapsed_s = time.perf_counter() - t_start
    rate_pct = (matched / accepted * 100.0) if accepted else 0.0
    print(
        f"\n[differential] opentrons={opentrons.__version__} "
        f"accepted={accepted} rejected={rejected} "
        f"agreement={matched}/{accepted} ({rate_pct:.1f}%) "
        f"elapsed={elapsed_s:.2f}s"
    )

    assert accepted >= 300, f"Expected >= 300 accepted protocols, got {accepted}"
    assert not mismatches, "Disagreements with opentrons.simulate:\n" + "\n".join(mismatches[:10])
    assert matched == accepted
    assert elapsed_s < 45.0, f"Differential suite took {elapsed_s:.2f}s (limit 45.0s)"

"""Benchmark `stepbot` + `opentrons.simulate` across 36 public multi-step wet-lab assays.

Covers 6 standard laboratory automation workflow families (6 protocols per family = 36):
  1. ELISA (capture coating, blocking, top-dispense sample loading, serum conjugate, TMB, stop)
  2. PCR / qPCR Mastermix & Primer/Template Setup (1-ch, 8-ch, dual-pipette, viscous polymerase)
  3. Serial Dilutions & Dose-Response Curves (2x, 3x, 5x, 10x, 8-channel column cascades, deep-well)
  4. Magnetic Bead Cleanup / SPRI (binding, supernatant discard, 2x volatile 80% EtOH wash, elution)
  5. Bradford & BCA Protein Quantification (BSA standard curve + serum unknowns + 8-ch reagent fill)
  6. NGS Library Prep (end-repair, viscous ligase with settle delay & touch_tip, dual-index barcodes)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any
import warnings

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from stepbot import (  # noqa: E402
    check_protocol_source,
    compile_protocol,
    parse_and_validate,
    reduce_mock_log_actions,
    reduce_opentrons_runlog_actions,
)


def build_public_assay_suite() -> list[tuple[str, str, dict[str, Any]]]:
    """Return 36 `(assay_id, family, protocol_dict)` public wet-lab assay specifications."""
    suite: list[tuple[str, str, dict[str, Any]]] = []

    # 1. ELISA Workflows (6 variants)
    for v_idx, (n_cols, use_8ch, sub_vol) in enumerate(
        [(2, False, 50.0), (3, False, 75.0), (4, True, 50.0), (6, True, 100.0), (8, True, 50.0), (12, True, 50.0)],
        start=1,
    ):
        pip_model = "p300_multi_gen2" if use_8ch else "p300_single_gen2"
        targets = (
            [f"plate:A{c}" for c in range(1, n_cols + 1)]
            if use_8ch
            else f"plate:A1-B{n_cols}"
        )
        suite.append(
            (
                f"elisa_{v_idx:02d}_{'8ch' if use_8ch else '1ch'}_{n_cols}col",
                "elisa",
                {
                    "metadata": {"name": f"Sandwich ELISA {v_idx}", "apiLevel": "2.16"},
                    "deck": {
                        "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
                        "res": {"labware": "nest_12_reservoir_15ml", "slot": 2},
                        "tips1": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
                        "tips2": {"labware": "opentrons_96_tiprack_300ul", "slot": 4},
                    },
                    "pipettes": {"left": {"model": pip_model, "tip_racks": ["tips1", "tips2"]}},
                    "liquids": {
                        "coating_ab": {"wells": "res:A1", "volume": 12000.0},
                        "sample_diluent": {"wells": "res:A2", "volume": 12000.0},
                        "hrp_conjugate": {"wells": "res:A3", "volume": 12000.0, "liquid_class": "serum"},
                        "tmb_substrate": {"wells": "res:A4", "volume": 12000.0},
                        "stop_acid": {"wells": "res:A5", "volume": 12000.0},
                    },
                    "steps": [
                        {"comment": "Coat capture antibody"},
                        {"distribute": {"src": "res:A1", "dst": targets, "volume": 50.0, "new_tip": "once"}},
                        {"delay": {"seconds": 5}},
                        {"transfer": {"src": "res:A2", "dst": targets, "volume": 50.0, "new_tip": "once", "dispense_position": "top"}},
                        {"transfer": {"src": "res:A3", "dst": targets, "volume": 50.0, "new_tip": "once", "dispense_position": "top"}},
                        {"distribute": {"src": "res:A4", "dst": targets, "volume": sub_vol, "new_tip": "once", "dispense_position": "top"}},
                        {"distribute": {"src": "res:A5", "dst": targets, "volume": 50.0, "new_tip": "once", "dispense_position": "top", "disposal_volume": 0}},
                    ],
                },
            )
        )

    # 2. PCR / qPCR Setup Workflows (6 variants)
    for v_idx, (n_rxn, mm_vol, tmpl_vol, visc_mm) in enumerate(
        [
            (4, 15.0, 5.0, False),
            (6, 18.0, 2.0, True),
            (8, 20.0, 5.0, False),
            (8, 15.0, 5.0, True),
            (12, 15.0, 5.0, False),
            (6, 40.0, 10.0, False),
        ],
        start=1,
    ):
        suite.append(
            (
                f"pcr_setup_{v_idx:02d}_{n_rxn}rxn",
                "pcr_setup",
                {
                    "metadata": {"name": f"qPCR Mastermix & Template Setup {v_idx}", "apiLevel": "2.16"},
                    "deck": {
                        "pcr_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": 1},
                        "tubes": {"labware": "opentrons_24_tuberack_nest_1.5ml_snapcap", "slot": 2},
                        "tips300": {"labware": "opentrons_96_filtertiprack_200ul", "slot": 3},
                        "tips20": {"labware": "opentrons_96_filtertiprack_20ul", "slot": 4},
                    },
                    "pipettes": {
                        "left": {"model": "p300_single_gen2", "tip_racks": ["tips300"]},
                        "right": {"model": "p20_single_gen2", "tip_racks": ["tips20"]},
                    },
                    "liquids": {
                        "mastermix": {
                            "wells": "tubes:A1",
                            "volume": 1200.0,
                            **({"liquid_class": "viscous_glycerol"} if visc_mm else {}),
                        },
                        **{
                            f"template_{k}": {"wells": f"tubes:B{(k - 1) % 6 + 1}", "volume": 200.0}
                            for k in range(1, min(n_rxn, 6) + 1)
                        },
                    },
                    "steps": [
                        {
                            "transfer" if mm_vol < 20.0 else "distribute": {
                                "pipette": "right" if mm_vol < 20.0 else "left",
                                "src": "tubes:A1",
                                "dst": f"pcr_plate:A1-A{n_rxn}",
                                "volume": mm_vol,
                                "new_tip": "once",
                            }
                        },
                        *[
                            {
                                "transfer": {
                                    "pipette": "right",
                                    "src": f"tubes:B{(k - 1) % 6 + 1}",
                                    "dst": f"pcr_plate:A{k}",
                                    "volume": tmpl_vol,
                                    "new_tip": "always",
                                    "mix_after": [2, min(20.0, round((mm_vol + tmpl_vol) * 0.5, 1))],
                                }
                            }
                            for k in range(1, n_rxn + 1)
                        ],
                    ],
                },
            )
        )

    # 3. Serial Dilutions & Dose-Response Curves (6 variants)
    serial_configs = [
        ("serial_2fold_row_1ch", "p300_single_gen2", "corning_96_wellplate_360ul_flat", 8, 2.0, 100.0, "row", False),
        ("serial_3fold_col_1ch", "p300_single_gen2", "corning_96_wellplate_360ul_flat", 6, 3.0, 120.0, "col", False),
        ("serial_5fold_p20", "p20_single_gen2", "nest_96_wellplate_100ul_pcr_full_skirt", 6, 5.0, 12.0, "row", False),
        ("serial_2fold_8channel", "p300_multi_gen2", "corning_96_wellplate_360ul_flat", 8, 2.0, 100.0, "row", False),
        ("serial_10fold_deepwell", "p1000_single_gen2", "nest_96_wellplate_2ml_deep", 6, 5.0, 600.0, "row", True),
        ("serial_1p5fold_multi_carry", "p300_single_gen2", "nest_96_wellplate_2ml_deep", 4, 1.5, 200.0, "row", True),
    ]
    for s_id, pip_m, plate_lw, n_pts, fac, f_vol, direc, multi_asp in serial_configs:
        tip_lw = (
            "opentrons_96_tiprack_20ul"
            if "p20" in pip_m
            else ("opentrons_96_tiprack_1000ul" if "p1000" in pip_m else "opentrons_96_tiprack_300ul")
        )
        suite.append(
            (
                s_id,
                "serial_dilution",
                {
                    "metadata": {"name": s_id, "apiLevel": "2.16"},
                    "deck": {
                        "plate": {"labware": plate_lw, "slot": 1},
                        "res": {"labware": "nest_12_reservoir_15ml", "slot": 2},
                        "tips": {"labware": tip_lw, "slot": 3},
                    },
                    "pipettes": {"left": {"model": pip_m, "tip_racks": ["tips"]}},
                    "liquids": {
                        "stock": {"wells": "res:A1", "volume": 14000.0},
                        "diluent": {"wells": "res:A2", "volume": 14000.0},
                    },
                    "steps": [
                        {
                            "dilution_series": {
                                "source": "res:A1",
                                "diluent": "res:A2",
                                "first_well": "plate:A1",
                                "n": n_pts,
                                "factor": fac,
                                "final_volume": f_vol,
                                "direction": direc,
                                "mix_reps": 3,
                                "multi_aspirate": multi_asp,
                            }
                        }
                    ],
                },
            )
        )

    # 4. Magnetic Bead Cleanup / SPRI Workflows (6 variants)
    for v_idx, (n_samples, use_8ch, sample_vol, ratio) in enumerate(
        [
            (2, False, 50.0, 1.8),
            (4, False, 50.0, 1.0),
            (3, True, 40.0, 1.8),
            (4, True, 50.0, 0.8),
            (6, True, 30.0, 1.8),
            (2, False, 100.0, 1.8),
        ],
        start=1,
    ):
        pip_m = "p300_multi_gen2" if use_8ch else "p300_single_gen2"
        bead_vol = round(sample_vol * ratio, 1)
        total_bind = round(sample_vol + bead_vol, 1)
        wells = [f"plate:A{k}" for k in range(1, n_samples + 1)]
        suite.append(
            (
                f"bead_cleanup_{v_idx:02d}_{'8ch' if use_8ch else '1ch'}",
                "bead_cleanup",
                {
                    "metadata": {"name": f"SPRI Magnetic Bead Cleanup {v_idx}", "apiLevel": "2.16"},
                    "deck": {
                        "plate": {"labware": "nest_96_wellplate_2ml_deep", "slot": 1},
                        "elution_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": 2},
                        "res": {"labware": "nest_12_reservoir_15ml", "slot": 3},
                        "tips1": {"labware": "opentrons_96_filtertiprack_200ul", "slot": 4},
                        "tips2": {"labware": "opentrons_96_filtertiprack_200ul", "slot": 5},
                        "tips3": {"labware": "opentrons_96_filtertiprack_200ul", "slot": 6},
                    },
                    "pipettes": {"left": {"model": pip_m, "tip_racks": ["tips1", "tips2", "tips3"]}},
                    "liquids": {
                        **{
                            f"dna_{k}": {
                                "wells": f"plate:col:{k}" if use_8ch else f"plate:A{k}",
                                "volume": sample_vol,
                            }
                            for k in range(1, n_samples + 1)
                        },
                        "ampure_beads": {"wells": "res:A1", "volume": 12000.0, "liquid_class": "viscous_glycerol"},
                        "etoh_80": {"wells": "res:A2", "volume": 14000.0, "liquid_class": "volatile_ethanol"},
                        "te_buffer": {"wells": "res:A3", "volume": 10000.0},
                    },
                    "steps": [
                        {"transfer": {"src": "res:A1", "dst": wells, "volume": bead_vol, "new_tip": "always", "mix_after": [3, min(150.0, round(total_bind * 0.7, 1))]}},
                        {"delay": {"minutes": 1}},
                        {"discard": {"src": wells, "volume": total_bind, "new_tip": "always", "multi_aspirate": True}},
                        {"transfer": {"src": "res:A2", "dst": wells, "volume": 150.0, "new_tip": "once", "dispense_position": "top"}},
                        {"discard": {"src": wells, "volume": 150.0, "new_tip": "always"}},
                        {"transfer": {"src": "res:A3", "dst": wells, "volume": 30.0, "new_tip": "always", "mix_after": [3, 25.0]}},
                        {"transfer": {"src": wells, "dst": [f"elution_plate:A{k}" for k in range(1, n_samples + 1)], "volume": 25.0, "new_tip": "always"}},
                    ],
                },
            )
        )

    # 5. Bradford & BCA Protein Quantification (6 variants)
    for v_idx, (n_std, n_unk, dye_vol) in enumerate(
        [(5, 3, 200.0), (6, 4, 180.0), (7, 4, 200.0), (5, 6, 150.0), (6, 6, 200.0), (8, 4, 200.0)],
        start=1,
    ):
        suite.append(
            (
                f"bradford_bca_{v_idx:02d}_{n_std}std_{n_unk}unk",
                "bradford_bca",
                {
                    "metadata": {"name": f"Bradford Protein Assay {v_idx}", "apiLevel": "2.16"},
                    "deck": {
                        "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
                        "tubes": {"labware": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "slot": 2},
                        "res": {"labware": "nest_12_reservoir_15ml", "slot": 3},
                        "tips300": {"labware": "opentrons_96_tiprack_300ul", "slot": 4},
                        "tips20": {"labware": "opentrons_96_tiprack_20ul", "slot": 5},
                    },
                    "pipettes": {
                        "left": {"model": "p300_single_gen2", "tip_racks": ["tips300"]},
                        "right": {"model": "p20_single_gen2", "tip_racks": ["tips20"]},
                    },
                    "liquids": {
                        "bsa_stock": {"wells": "tubes:A1", "volume": 1000.0},
                        "pbs": {"wells": "res:A1", "volume": 12000.0},
                        "coomassie": {"wells": "res:A2", "volume": 14000.0},
                        **{
                            f"serum_unk_{u}": {"wells": f"tubes:B{u}", "volume": 200.0, "liquid_class": "serum"}
                            for u in range(1, n_unk + 1)
                        },
                    },
                    "steps": [
                        {
                            "dilution_series": {
                                "pipette": "left",
                                "source": "tubes:A1",
                                "diluent": "res:A1",
                                "first_well": "plate:A1",
                                "n": n_std,
                                "factor": 2,
                                "final_volume": 25.0,
                                "direction": "row",
                            }
                        },
                        *[
                            {
                                "transfer": {
                                    "pipette": "right",
                                    "src": f"tubes:B{u}",
                                    "dst": f"plate:B{u}",
                                    "volume": 10.0,
                                    "new_tip": "always",
                                }
                            }
                            for u in range(1, n_unk + 1)
                        ],
                        {
                            "distribute": {
                                "pipette": "left",
                                "src": "res:A2",
                                "dst": [f"plate:A{k}" for k in range(1, n_std + 1)]
                                + [f"plate:B{u}" for u in range(1, n_unk + 1)],
                                "volume": dye_vol,
                                "new_tip": "once",
                                "dispense_position": "top",
                                "disposal_volume": 0,
                            }
                        },
                    ],
                },
            )
        )

    # 6. NGS Library Prep (End-Repair, Ligation & Dual-Index Barcoding, 6 variants)
    for v_idx, (n_libs, flex_mode) in enumerate(
        [(3, False), (4, False), (6, False), (8, False), (4, True), (6, True)],
        start=1,
    ):
        if flex_mode:
            deck_ngs: dict[str, Any] = {
                "lib_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": "D1"},
                "index_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": "D2"},
                "reagents": {"labware": "opentrons_24_tuberack_nest_1.5ml_snapcap", "slot": "C1"},
                "tips": {"labware": "opentrons_flex_96_filtertiprack_50ul", "slot": "C2"},
            }
            pips_ngs: dict[str, Any] = {"left": {"model": "flex_1channel_50", "tip_racks": ["tips"]}}
            robot_kw: dict[str, Any] = {"robot_type": "Flex"}
        else:
            deck_ngs = {
                "lib_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": 1},
                "index_plate": {"labware": "nest_96_wellplate_100ul_pcr_full_skirt", "slot": 2},
                "reagents": {"labware": "opentrons_24_tuberack_nest_1.5ml_snapcap", "slot": 3},
                "tips": {"labware": "opentrons_96_filtertiprack_20ul", "slot": 4},
            }
            pips_ngs = {"left": {"model": "p20_single_gen2", "tip_racks": ["tips"]}}
            robot_kw = {}

        suite.append(
            (
                f"ngs_libprep_{v_idx:02d}_{n_libs}lib_{'flex' if flex_mode else 'ot2'}",
                "ngs_library_prep",
                {
                    "metadata": {"name": f"NGS Library Prep & Indexing {v_idx}"},
                    **robot_kw,
                    "deck": deck_ngs,
                    "pipettes": pips_ngs,
                    "liquids": {
                        "er_mix": {"wells": "reagents:A1", "volume": 500.0},
                        "ligase_mix": {"wells": "reagents:A2", "volume": 500.0, "liquid_class": "viscous_glycerol"},
                        **{
                            f"frag_dna_{k}": {"wells": f"lib_plate:A{k}", "volume": 20.0}
                            for k in range(1, n_libs + 1)
                        },
                        **{
                            f"udi_barcode_{k}": {"wells": f"index_plate:A{k}", "volume": 30.0}
                            for k in range(1, n_libs + 1)
                        },
                    },
                    "steps": [
                        {
                            "transfer": {
                                "src": "reagents:A1",
                                "dst": f"lib_plate:A1-A{n_libs}",
                                "volume": 10.0,
                                "new_tip": "once",
                                "dispense_position": "top",
                            }
                        },
                        {
                            "transfer": {
                                "src": "reagents:A2",
                                "dst": f"lib_plate:A1-A{n_libs}",
                                "volume": 15.0,
                                "new_tip": "always",
                                "mix_after": [2, 20.0],
                            }
                        },
                        {
                            "transfer": {
                                "src": f"index_plate:A1-A{n_libs}",
                                "dst": f"lib_plate:A1-A{n_libs}",
                                "volume": 5.0,
                                "new_tip": "always",
                                "mix_after": [3, 20.0],
                            }
                        },
                    ],
                },
            )
        )

    return suite


def evaluate_public_protocols(*, require_opentrons: bool = False) -> dict[str, Any]:
    """Compile and evaluate all 36 public multi-step wet-lab assays."""
    ot_simulate = None
    ot_version: str | None = None
    try:
        import opentrons
        from opentrons import simulate as _sim

        ot_simulate = _sim
        ot_version = str(opentrons.__version__)
    except ImportError:
        if require_opentrons:
            raise RuntimeError("opentrons package is required for live simulator evaluation")

    suite = build_public_assay_suite()
    t0 = time.perf_counter()

    per_family: dict[str, dict[str, int]] = {}
    total_actions = 0
    total_tips = 0
    sim_matched = 0
    records: list[dict[str, Any]] = []

    for assay_id, family, proto in suite:
        fam_stats = per_family.setdefault(
            family, {"count": 0, "mock_ok": 0, "sim_matched": 0, "actions": 0, "tips": 0}
        )
        fam_stats["count"] += 1

        spec = parse_and_validate(proto)
        py_code = compile_protocol(proto)
        mock_res = check_protocol_source(py_code, use_mock=True)
        if not mock_res.ok:
            raise RuntimeError(f"{assay_id} failed mock check: {mock_res.errors}")
        fam_stats["mock_ok"] += 1

        mock_actions = reduce_mock_log_actions(mock_res.lines)
        mock_tips = sum(mock_res.tips_used_per_mount.values())
        assert mock_tips == sum(spec.tips_used_per_mount.values())

        total_actions += len(mock_actions)
        total_tips += mock_tips
        fam_stats["actions"] += len(mock_actions)
        fam_stats["tips"] += mock_tips

        agreed = True
        if ot_simulate is not None:
            prev_disable = logging.root.manager.disable
            logging.disable(logging.WARNING)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    runlog, _ = ot_simulate.simulate(io.StringIO(py_code))
            finally:
                logging.disable(prev_disable)
            ot_actions = reduce_opentrons_runlog_actions(runlog)
            agreed = mock_actions == ot_actions
            if agreed:
                sim_matched += 1
                fam_stats["sim_matched"] += 1

        records.append(
            {
                "assay_id": assay_id,
                "family": family,
                "primitive_actions": len(mock_actions),
                "tips_used": mock_tips,
                "agreed_with_opentrons_sim": agreed if ot_simulate is not None else None,
            }
        )

    elapsed_s = round(time.perf_counter() - t0, 2)
    return {
        "n_protocols": len(suite),
        "families": per_family,
        "total_primitive_actions": total_actions,
        "total_tips_tracked": total_tips,
        "opentrons_version": ot_version,
        "sim_evaluated": len(suite) if ot_simulate is not None else 0,
        "sim_matched": sim_matched if ot_simulate is not None else None,
        "agreement_pct": round(sim_matched / len(suite) * 100.0, 2)
        if ot_simulate is not None
        else None,
        "elapsed_seconds": elapsed_s,
        "protocols": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate stepbot + opentrons.simulate across 36 public wet-lab assay protocols."
    )
    parser.add_argument(
        "--require-opentrons",
        action="store_true",
        help="Fail if opentrons.simulate is not installed.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write JSON evaluation summary.",
    )
    args = parser.parse_args()
    summary = evaluate_public_protocols(require_opentrons=args.require_opentrons)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps({k: v for k, v in summary.items() if k != "protocols"}, indent=2))
    if summary["opentrons_version"] is not None and summary["sim_matched"] != summary["n_protocols"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

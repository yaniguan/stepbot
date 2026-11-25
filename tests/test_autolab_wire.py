"""Tests for `stepbot` Auto-Lab 15-repository wire node (`autolab_wire.py`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autolab_wire  # noqa: E402
from stepbot import compute_envelope_hmac  # noqa: E402

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

NATIVE_REHEARSAL_DICT: dict[str, Any] = {
    "metadata": {"name": "Wire Native Transfer", "apiLevel": "2.16"},
    "deck": {
        "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
        "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
        "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
    },
    "pipettes": {
        "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
    },
    "liquids": {
        "buffer": {"wells": "reservoir:A1", "volume": 4000.0},
    },
    "steps": [
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1-A2", "volume": 100.0}},
    ],
}

SLOT_KEYED_REHEARSAL_DICT: dict[str, Any] = {
    "metadata": {"protocolName": "Auto-Lab Rehearsed Protocol", "apiLevel": "2.16"},
    "deck": {
        "1": {"load_name": "opentrons_96_tiprack_300ul"},
        "2": {"load_name": "nest_12_reservoir_15ml"},
        "3": {"load_name": "corning_96_wellplate_360ul_flat"},
    },
    "pipettes": {
        "right": {"instrument_name": "p300_single_gen2", "tip_racks": ["1"]},
    },
    "steps": [
        {
            "action": "transfer",
            "pipette": "right",
            "source": "2:A1",
            "dest": "3:A1",
            "volume": 120.0,
        }
    ],
}


def test_default_plate_fill_compile_stage_and_process_packet() -> None:
    assert autolab_wire.REPO_ID == "stepbot"
    assert autolab_wire.LAYER == "L1"

    original: dict[str, Any] = {"run_id": "wire-plate-fill-01"}
    compiled_pkt = autolab_wire.compile_stage(original)

    # Input packet is never mutated in place
    assert "stepbot_compiled" not in original
    assert compiled_pkt["schema_version"] == "autolab-wire/v1"
    assert compiled_pkt["halted"] is False
    assert compiled_pkt["halt_reason"] is None

    comp = compiled_pkt["stepbot_compiled"]
    assert "def run(protocol):" in comp["python_code"]
    assert len(comp["protocol_sha256"]) == 64
    assert comp["envelope_bounds"]["deck_slots"] == [1, 2, 3]
    assert comp["envelope_bounds"]["max_transfer_ul"] == 150.0
    assert comp["self_envelope"]["protocol_sha256"] == comp["protocol_sha256"]

    # Direct process_packet on fresh packet auto-compiles and executes cleanly
    executed_pkt = autolab_wire.process_packet({"run_id": "wire-plate-fill-02"})
    assert executed_pkt["halted"] is False
    assert executed_pkt["deck_epoch"] == 2

    exec_info = executed_pkt["stepbot_execution"]
    assert exec_info["protocol_sha256"] == comp["protocol_sha256"]
    assert exec_info["next_deck_epoch"] == 2
    assert exec_info["dispatch_receipt"]["ok"] is True
    assert exec_info["dispatch_receipt"]["executed_epoch"] == 1
    assert exec_info["dispatch_receipt"]["current_deck_epoch"] == 2
    assert exec_info["dispatch_receipt"]["command_count"] > 0

    assert len(executed_pkt["trace"]) == 1
    assert executed_pkt["trace"][0]["repo"] == "stepbot"
    assert executed_pkt["trace"][0]["layer"] == "L1"
    assert executed_pkt["trace"][0]["status"] == "PASS"

    assert len(executed_pkt["bottom_reasoning_traces"]) == 1
    assert executed_pkt["bottom_reasoning_traces"][0]["repo"] == "stepbot"
    assert (
        executed_pkt["bottom_reasoning_traces"][0]["invariant"]
        == "mcp_payload_and_epoch_binding"
    )

    # Packet remains 100% JSON-serializable
    serialized = json.dumps(executed_pkt)
    assert '"stepbot"' in serialized


def test_rehearsal_native_and_slot_keyed_dicts_compile_and_execute() -> None:
    pkt_native = {
        "deck_epoch": 3,
        "mcp_secret_key": "wire-test-secret-native",
        "rehearsal": {
            "passed": True,
            "violations": [],
            "protocol_yaml_dict": copy.deepcopy(NATIVE_REHEARSAL_DICT),
        },
    }
    out_native = autolab_wire.process_packet(pkt_native)
    assert out_native["halted"] is False
    assert out_native["deck_epoch"] == 4
    assert out_native["stepbot_execution"]["next_deck_epoch"] == 4
    assert out_native["stepbot_compiled"]["envelope_bounds"]["max_transfer_ul"] == 100.0

    pkt_rehearsal = {
        "deck_epoch": 1,
        "mcp_secret_key": "wire-test-secret-rehearsal",
        "rehearsal": {
            "passed": True,
            "violations": [],
            "protocol_yaml_dict": copy.deepcopy(SLOT_KEYED_REHEARSAL_DICT),
        },
    }
    compiled_rehearsal = autolab_wire.compile_stage(pkt_rehearsal)
    assert compiled_rehearsal["stepbot_compiled"]["envelope_bounds"] == {
        "deck_slots": [1, 2, 3],
        "max_transfer_ul": 120.0,
        "api_level": "2.16",
    }
    out_rehearsal = autolab_wire.process_packet(compiled_rehearsal)
    assert out_rehearsal["halted"] is False
    assert out_rehearsal["deck_epoch"] == 2
    assert len(out_rehearsal["trace"]) == 1
    assert len(out_rehearsal["bottom_reasoning_traces"]) == 1


def test_verification_envelope_selection_and_self_envelope_fallback() -> None:
    secret = "co-minted-wire-secret"
    pkt = autolab_wire.compile_stage(
        {
            "deck_epoch": 5,
            "mcp_secret_key": secret,
            "rehearsal": {"protocol_yaml_dict": copy.deepcopy(NATIVE_REHEARSAL_DICT)},
        }
    )
    comp = pkt["stepbot_compiled"]

    # Case A: lab-verify co-minted envelope bound to compiled python_code SHA-256
    co_minted: dict[str, Any] = {
        "schema_version": "lean-mcp/v1",
        "tool_name": "opentrons.execute_protocol",
        "protocol_sha256": comp["protocol_sha256"],
        "token_epoch": 5,
        "nonce": "lab-verify-minted-nonce-77",
        "lean_proof_valid": True,
        "is_frontier_exception": False,
        "human_supervisor_signed": False,
        "human_supervisor_id": "",
        "envelope_bounds": comp["envelope_bounds"],
    }
    co_minted["hmac_sha256"] = compute_envelope_hmac(co_minted, secret)

    pkt_with_verif = {
        **pkt,
        "verification": {"sql_passed": True, "mcp_envelope": co_minted},
    }
    res_co = autolab_wire.process_packet(pkt_with_verif)
    assert res_co["halted"] is False
    assert res_co["stepbot_execution"]["mcp_envelope"]["nonce"] == "lab-verify-minted-nonce-77"
    assert res_co["deck_epoch"] == 6

    # Case B: verification.mcp_envelope hashes upstream YAML dict rather than Python code
    ir_env = {**co_minted, "protocol_sha256": "0" * 64}
    ir_env["hmac_sha256"] = compute_envelope_hmac(ir_env, secret)
    pkt_ir_verif = {
        **pkt,
        "verification": {"sql_passed": True, "mcp_envelope": ir_env},
    }
    res_fallback = autolab_wire.process_packet(pkt_ir_verif)
    assert res_fallback["halted"] is False
    assert (
        res_fallback["stepbot_execution"]["mcp_envelope"]["nonce"]
        == comp["self_envelope"]["nonce"]
    )
    assert res_fallback["deck_epoch"] == 6


def test_force_envelope_blocks_stale_epoch_tampered_hmac_and_payload_swap() -> None:
    secret = "force-envelope-secret"
    compiled_pkt = autolab_wire.compile_stage(
        {"deck_epoch": 1, "mcp_secret_key": secret}
    )
    valid_env = copy.deepcopy(compiled_pkt["stepbot_compiled"]["self_envelope"])

    # 1. Stale epoch replay: envelope minted for epoch 1 dispatched when deck_epoch == 2
    stale_pkt = {
        **compiled_pkt,
        "deck_epoch": 2,
        "force_envelope": valid_env,
    }
    stale_out = autolab_wire.process_packet(stale_pkt)
    assert stale_out["halted"] is True
    assert stale_out["halt_reason"] == "L1_MCP_BLOCK:mcp_epoch_freshness"
    assert "stepbot_execution" not in stale_out
    assert stale_out["deck_epoch"] == 2
    assert stale_out["trace"][-1]["status"] == "BLOCK"

    # 2. Tampered HMAC: envelope parameter edited after signing
    tampered_hmac_env = {**valid_env, "human_supervisor_signed": True}
    hmac_out = autolab_wire.process_packet(
        {**compiled_pkt, "force_envelope": tampered_hmac_env}
    )
    assert hmac_out["halted"] is True
    assert hmac_out["halt_reason"] == "L1_MCP_BLOCK:mcp_hmac_authentic"
    assert "stepbot_execution" not in hmac_out

    # 3. Payload binding mismatch: validly signed envelope for a different SHA-256
    wrong_sha_env: dict[str, Any] = {
        k: v for k, v in valid_env.items() if k != "hmac_sha256"
    }
    wrong_sha_env["protocol_sha256"] = "f" * 64
    wrong_sha_env["hmac_sha256"] = compute_envelope_hmac(wrong_sha_env, secret)
    sha_out = autolab_wire.process_packet(
        {**compiled_pkt, "force_envelope": wrong_sha_env}
    )
    assert sha_out["halted"] is True
    assert sha_out["halt_reason"] == "L1_MCP_BLOCK:mcp_payload_binding"


def test_halted_packet_passthrough_short_circuits() -> None:
    upstream_halted: dict[str, Any] = {
        "schema_version": "autolab-wire/v1",
        "halted": True,
        "halt_reason": "L4_VERIFY_SQL_BLOCK",
        "deck_epoch": 1,
        "trace": [{"repo": "lab-verify", "layer": "L4", "status": "BLOCK", "summary": "SQL denied"}],
        "bottom_reasoning_traces": [],
    }
    comp_out = autolab_wire.compile_stage(upstream_halted)
    assert comp_out["halted"] is True
    assert comp_out["halt_reason"] == "L4_VERIFY_SQL_BLOCK"
    assert "stepbot_compiled" not in comp_out
    assert len(comp_out["trace"]) == 1

    proc_out = autolab_wire.process_packet(upstream_halted)
    assert proc_out["halted"] is True
    assert proc_out["halt_reason"] == "L4_VERIFY_SQL_BLOCK"
    assert "stepbot_compiled" not in proc_out
    assert "stepbot_execution" not in proc_out
    assert proc_out["deck_epoch"] == 1
    assert len(proc_out["trace"]) == 1


def test_cwd_independence_and_pure_english_documentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = autolab_wire.process_packet({})
    assert out["halted"] is False
    assert out["deck_epoch"] == 2

    for doc_path in (
        ROOT / "autolab_wire.py",
        ROOT / "README.md",
        ROOT / "docs" / "ARCHITECTURE_AND_HISTORY.md",
        Path(__file__).resolve(),
    ):
        text = doc_path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Unexpected CJK character in {doc_path}"


def test_wire_consumed_nonces_bounded_fifo_and_corruption_guard() -> None:
    pkt = autolab_wire.process_packet(
        {
            "run_id": "wire-nonce-fifo",
            "consumed_nonces": ["n-old-1", "n-old-2", "n-old-3"],
            "max_consumed_nonces": 3,
        }
    )
    assert pkt["halted"] is False
    new_nonce = pkt["stepbot_execution"]["mcp_envelope"]["nonce"]
    assert pkt["consumed_nonces"] == ["n-old-2", "n-old-3", new_nonce]

    # Replaying the exact same nonce at the new epoch is blocked
    comp_next = autolab_wire.compile_stage({"deck_epoch": 2, "mcp_secret_key": "s1"})
    replay_env = dict(comp_next["stepbot_compiled"]["self_envelope"])
    replay_env["nonce"] = "already-used"
    replay_env["hmac_sha256"] = compute_envelope_hmac(replay_env, "s1")
    replay_pkt = autolab_wire.process_packet(
        {
            **comp_next,
            "consumed_nonces": ["already-used"],
            "force_envelope": replay_env,
        }
    )
    assert replay_pkt["halted"] is True
    assert replay_pkt["halt_reason"] == "L1_MCP_BLOCK:mcp_nonce_single_use"

    # Malformed consumed_nonces fails closed
    corrupt_pkt = autolab_wire.process_packet(
        {"run_id": "wire-corrupt-nonces", "consumed_nonces": "not-a-list"}
    )
    assert corrupt_pkt["halted"] is True
    assert corrupt_pkt["halt_reason"] == "L1_MCP_BLOCK:mcp_state_corrupt"


def test_wire_post_dispatch_telemetry_nominal_and_rheology_artifacts() -> None:
    glycerol_proto = {
        "metadata": {"name": "Glycerol Assay Fill", "apiLevel": "2.16"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips"]}},
        "liquids": {"stock": {"wells": "reservoir:A1", "volume": 2000.0}},
        "steps": [
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A1-A4", "volume": 100.0}}
        ],
    }

    # 1. Nominal aqueous run: exact well levels, 1 tip consumed, zero pipetting artifacts
    aq_pkt = autolab_wire.process_packet(
        {
            "run_id": "wire-aq-nominal",
            "assay_telemetry": {"upstream_sensor_ok": True},
            "rehearsal": {"protocol_yaml_dict": copy.deepcopy(glycerol_proto)},
        }
    )
    assert aq_pkt["halted"] is False
    tel_aq = aq_pkt["stepbot_execution"]["telemetry"]
    assert tel_aq["well_volumes_ul"]["reservoir:A1"] == 1600.0
    assert tel_aq["well_volumes_ul"]["plate:A1"] == 100.0
    assert tel_aq["well_volumes_ul"]["plate:A4"] == 100.0
    assert tel_aq["well_levels_ul"]["plate:A1"]["headspace_ul"] == 260.0
    assert tel_aq["tips_consumed"] == 1
    assert tel_aq["tip_telemetry"]["total_tips_used"] == 1
    assert tel_aq["pipetting_artifact_detected"] is False
    assert aq_pkt["assay_telemetry"]["upstream_sensor_ok"] is True
    assert aq_pkt["assay_telemetry"]["pipetting_artifact_detected"] is False
    assert json.dumps(aq_pkt)

    # 2. Uncompensated 80% glycerol run: pressure lag & short-aspiration artifact flagged for lab-attrib
    gly_pkt = autolab_wire.process_packet(
        {
            "run_id": "wire-glycerol-uncomp",
            "liquid_class": "glycerol_80pct",
            "rehearsal": {"protocol_yaml_dict": copy.deepcopy(glycerol_proto)},
        }
    )
    tel_gly = gly_pkt["stepbot_execution"]["telemetry"]
    rheo_gly = tel_gly["rheology_and_pressure"]
    assert rheo_gly["liquid_class"] == "glycerol_80pct"
    assert (
        rheo_gly["estimated_pressure_drop_pa"]
        > 50.0 * tel_aq["rheology_and_pressure"]["estimated_pressure_drop_pa"]
    )
    assert rheo_gly["estimated_pressure_drop_pa"] > 5000.0
    assert rheo_gly["mean_abs_volume_error_pct"] >= 5.0
    assert tel_gly["pipetting_artifact_detected"] is True
    assert gly_pkt["assay_telemetry"]["pipetting_artifact_detected"] is True

    # 3. Uncompensated volatile ethanol run: volatile drip fault flagged
    eth_pkt = autolab_wire.process_packet(
        {
            "run_id": "wire-ethanol-uncomp",
            "liquid_class": "ethanol_volatile",
            "rehearsal": {"protocol_yaml_dict": copy.deepcopy(glycerol_proto)},
        }
    )
    rheo_eth = eth_pkt["stepbot_execution"]["telemetry"]["rheology_and_pressure"]
    assert rheo_eth["volatile_drip_fault"] is True
    assert eth_pkt["stepbot_execution"]["telemetry"]["pipetting_artifact_detected"] is True

    # 4. Rheology-compensated ethanol run clears drip fault & artifact flag
    eth_comp_pkt = autolab_wire.process_packet(
        {
            "run_id": "wire-ethanol-comp",
            "liquid_class": "ethanol_volatile",
            "rheology_compensated": True,
            "rehearsal": {"protocol_yaml_dict": copy.deepcopy(glycerol_proto)},
        }
    )
    rheo_comp = eth_comp_pkt["stepbot_execution"]["telemetry"]["rheology_and_pressure"]
    assert rheo_comp["volatile_drip_fault"] is False
    assert eth_comp_pkt["stepbot_execution"]["telemetry"]["pipetting_artifact_detected"] is False


def test_wire_opentrons_flex_and_rehearsal_lifecycle_steps() -> None:
    flex_rehearsal = {
        "metadata": {"title": "Flex Rehearsed Assay", "apiLevel": "2.18"},
        "deck": {
            "C1": {"load_name": "opentrons_flex_96_tiprack_200ul"},
            "D2": {"load_name": "nest_12_reservoir_15ml"},
            "D1": {"load_name": "corning_96_wellplate_360ul_flat"},
        },
        "pipettes": {
            "left": {"instrument_name": "flex_1channel_1000", "tip_racks": ["C1"]},
        },
        "steps": [
            {"action": "thaw", "plate": "D2"},
            {"action": "transfer", "source": "D2:A1", "dest": "D1:A1-A2", "volume": 100.0},
            {"action": "seal", "plate": "D1"},
            {"action": "incubate", "plate": "D1", "temperature_c": 37.0, "minutes": 5.0},
            {"action": "transfer", "source": "D1:A2", "dest": "trash", "volume": 50.0},
        ],
    }
    out = autolab_wire.process_packet({"rehearsal": {"protocol_yaml_dict": flex_rehearsal}})
    assert out["halted"] is False
    assert out["stepbot_compiled"]["envelope_bounds"]["deck_slots"] == ["C1", "D1", "D2"]
    assert out["stepbot_compiled"]["envelope_bounds"]["api_level"] == "2.18"
    assert "load_trash_bin('A3')" in out["stepbot_compiled"]["python_code"]
    assert out["stepbot_execution"]["telemetry"]["well_volumes_ul"]["slot_D1:A1"] == 100.0
    assert out["stepbot_execution"]["telemetry"]["well_volumes_ul"]["slot_D1:A2"] == 50.0

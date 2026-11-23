"""Tests for stepbot zero-trust `Lean-MCP` (`lean-mcp/v1`) runtime and cross-repo wire interop."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import sys

import pytest

from stepbot import (
    LEAN_MCP_SCHEMA_VERSION,
    CompiledMcpBundle,
    McpSecurityViolation,
    StepbotMcpExecutor,
    ValidationError,
    compile_file,
    compile_with_mcp_envelope,
)
from stepbot.cli import main as cli_main

ROOT = Path(__file__).resolve().parent.parent
CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

MINIMAL_PROTOCOL = {
    "metadata": {"name": "MCP Zero-Click Transfer", "apiLevel": "2.16"},
    "deck": {
        "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
        "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
        "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 4},
    },
    "pipettes": {
        "left": {"model": "p300_single_gen2", "tip_racks": ["tips"]},
    },
    "liquids": {
        "buffer": {"wells": "reservoir:A1", "volume": 5000.0},
    },
    "steps": [
        {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 100.0}},
    ],
}


def test_compile_with_mcp_envelope_emits_valid_v1_bundle() -> None:
    bundle = compile_with_mcp_envelope(
        MINIMAL_PROTOCOL,
        deck_epoch=1,
        secret_key="stepbot-unit-secret",
        nonce="unit-nonce-01",
    )
    assert isinstance(bundle, CompiledMcpBundle)
    assert '"apiLevel": \'2.16\'' in bundle.python_code or '"apiLevel": "2.16"' in bundle.python_code
    assert len(bundle.protocol_sha256) == 64

    env = bundle.envelope
    assert env["schema_version"] == LEAN_MCP_SCHEMA_VERSION
    assert env["tool_name"] == "opentrons.execute_protocol"
    assert env["protocol_sha256"] == bundle.protocol_sha256
    assert env["token_epoch"] == 1
    assert env["nonce"] == "unit-nonce-01"
    assert env["lean_proof_valid"] is True
    assert env["is_frontier_exception"] is False
    assert env["human_supervisor_signed"] is False
    assert env["envelope_bounds"] == {
        "deck_slots": [1, 2, 4],
        "max_transfer_ul": 100.0,
        "api_level": "2.16",
    }


def test_zero_click_dispatch_advances_epoch_and_blocks_replay() -> None:
    secret = "dispatch-epoch-secret"
    bundle = compile_with_mcp_envelope(
        ROOT / "examples" / "dilution_series.yaml",
        deck_epoch=1,
        secret_key=secret,
        nonce="dil-nonce-1",
    )
    executor = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)

    receipt = executor.dispatch(bundle.python_code, bundle.envelope)
    assert receipt["ok"] is True
    assert receipt["executed_epoch"] == 1
    assert receipt["current_deck_epoch"] == 2
    assert executor.current_deck_epoch == 2
    assert "dil-nonce-1" in executor.consumed_nonces
    assert int(str(receipt["command_count"])) > 0

    # Immediate replay fails because physical deck liquid volumes mutated to epoch 2
    with pytest.raises(McpSecurityViolation) as exc_info:
        executor.dispatch(bundle.python_code, bundle.envelope)
    assert exc_info.value.rule == "mcp_epoch_freshness"


def test_same_epoch_nonce_replay_blocked_even_when_epoch_matches() -> None:
    secret = "nonce-guard-secret"
    bundle = compile_with_mcp_envelope(
        MINIMAL_PROTOCOL,
        deck_epoch=5,
        secret_key=secret,
        nonce="reused-nonce-9",
    )
    executor = StepbotMcpExecutor(
        secret_key=secret,
        current_deck_epoch=5,
        consumed_nonces={"reused-nonce-9"},
        use_mock=True,
    )
    with pytest.raises(McpSecurityViolation) as exc_info:
        executor.dispatch(bundle.python_code, bundle.envelope)
    assert exc_info.value.rule == "mcp_nonce_single_use"
    assert executor.current_deck_epoch == 5


def test_post_compile_script_tampering_blocked_by_sha256_binding() -> None:
    secret = "sha256-binding-secret"
    bundle = compile_with_mcp_envelope(MINIMAL_PROTOCOL, deck_epoch=1, secret_key=secret)
    executor = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)

    # Attacker alters transfer volume from 100 uL to 250 uL after compilation
    tampered_code = bundle.python_code.replace("transfer(100,", "transfer(250,")
    assert tampered_code != bundle.python_code

    with pytest.raises(McpSecurityViolation) as exc_info:
        executor.dispatch(tampered_code, bundle.envelope)
    assert exc_info.value.rule == "mcp_payload_binding"
    assert executor.current_deck_epoch == 1
    assert len(executor.consumed_nonces) == 0


def test_hmac_parameter_tampering_and_wrong_key_blocked() -> None:
    secret = "hmac-secret-alpha"
    bundle = compile_with_mcp_envelope(MINIMAL_PROTOCOL, deck_epoch=2, secret_key=secret)
    executor = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=2, use_mock=True)

    forged_env = dict(bundle.envelope)
    forged_env["human_supervisor_signed"] = True
    with pytest.raises(McpSecurityViolation) as exc_info:
        executor.dispatch(bundle.python_code, forged_env)
    assert exc_info.value.rule == "mcp_hmac_authentic"

    wrong_key_exec = StepbotMcpExecutor(secret_key="wrong-secret", current_deck_epoch=2, use_mock=True)
    with pytest.raises(McpSecurityViolation) as exc_wrong:
        wrong_key_exec.dispatch(bundle.python_code, bundle.envelope)
    assert exc_wrong.value.rule == "mcp_hmac_authentic"


def test_frontier_exception_requires_human_supervisor_cosignature() -> None:
    secret = "frontier-cosign-secret"
    unsigned_bundle = compile_with_mcp_envelope(
        MINIMAL_PROTOCOL,
        deck_epoch=1,
        secret_key=secret,
        is_frontier_exception=True,
        human_supervisor_signed=False,
        nonce="frontier-unsigned",
    )
    executor = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)

    with pytest.raises(McpSecurityViolation) as exc_info:
        executor.dispatch(unsigned_bundle.python_code, unsigned_bundle.envelope)
    assert exc_info.value.rule == "mcp_proof_or_cosigned_exception"
    assert executor.current_deck_epoch == 1

    # Human supervisor co-signs the frontier override -> executes cleanly and advances epoch
    signed_bundle = compile_with_mcp_envelope(
        MINIMAL_PROTOCOL,
        deck_epoch=1,
        secret_key=secret,
        is_frontier_exception=True,
        human_supervisor_signed=True,
        human_supervisor_id="dr_chen",
        nonce="frontier-signed",
    )
    receipt = executor.dispatch(signed_bundle.python_code, signed_bundle.envelope)
    assert receipt["ok"] is True
    assert executor.current_deck_epoch == 2


def test_canonical_wire_format_cross_repo_contract() -> None:
    """Verify wire-compatible `lean-mcp/v1` canonical JSON HMAC minted externally passes once,
    increments `current_deck_epoch` 1 -> 2, and blocks replay and script tampering."""
    shared_secret = "cross-repo-wire-secret-2026"
    python_code = compile_file(ROOT / "examples" / "plate_fill.yaml")
    code_sha256 = hashlib.sha256(python_code.replace("\r\n", "\n").encode("utf-8")).hexdigest()

    external_payload: dict[str, object] = {
        "schema_version": "lean-mcp/v1",
        "tool_name": "opentrons.execute_protocol",
        "protocol_sha256": code_sha256,
        "token_epoch": 1,
        "nonce": "lab-verify-minted-nonce-42",
        "lean_proof_valid": True,
        "is_frontier_exception": False,
        "human_supervisor_signed": False,
        "human_supervisor_id": "",
        "envelope_bounds": {
            "deck_slots": [1, 2, 3],
            "max_transfer_ul": 150.0,
            "api_level": "2.16",
        },
    }
    canonical_bytes = json.dumps(
        external_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    external_hmac = hmac.new(
        shared_secret.encode("utf-8"),
        canonical_bytes,
        hashlib.sha256,
    ).hexdigest()
    wire_envelope: dict[str, object] = {**external_payload, "hmac_sha256": external_hmac}

    executor = StepbotMcpExecutor(secret_key=shared_secret, current_deck_epoch=1, use_mock=True)
    receipt = executor.dispatch(python_code, wire_envelope)
    assert receipt["ok"] is True
    assert executor.current_deck_epoch == 2

    # Replay immediately blocked at epoch 2
    with pytest.raises(McpSecurityViolation) as replay_err:
        executor.dispatch(python_code, wire_envelope)
    assert replay_err.value.rule == "mcp_epoch_freshness"

    # Post-compile code tampering blocked before execution
    with pytest.raises(McpSecurityViolation) as tamper_err:
        executor.dispatch(python_code + "\n    protocol.comment('injected')\n", wire_envelope)
    assert tamper_err.value.rule == "mcp_payload_binding"


def test_live_sibling_lab_verify_roundtrip_if_present() -> None:
    sibling_src = ROOT.parent / "lab-verify" / "src"
    if not sibling_src.is_dir():
        pytest.skip("Sibling lab-verify checkout not present in isolated CI environment")
    if str(sibling_src) not in sys.path:
        sys.path.insert(0, str(sibling_src))

    from lab_verify import LeanMcpGate, verify_mcp_envelope  # type: ignore[import-not-found]

    shared_secret = "live-lab-verify-stepbot-bridge"
    python_code = compile_file(ROOT / "examples" / "reservoir_mix.yaml")

    # 1. Mint in lab-verify -> consume in stepbot
    lv_gate = LeanMcpGate(secret_key=shared_secret, initial_deck_epoch=1)
    lv_env = lv_gate.mint(python_code, nonce="bridge-n1")
    sb_exec = StepbotMcpExecutor(secret_key=shared_secret, current_deck_epoch=1, use_mock=True)
    receipt = sb_exec.dispatch(python_code, json.loads(lv_env.to_json()))
    assert receipt["ok"] is True
    assert sb_exec.current_deck_epoch == 2

    # 2. Mint in stepbot at epoch 2 -> verify in lab-verify
    sb_bundle = compile_with_mcp_envelope(
        ROOT / "examples" / "dilution_series.yaml",
        deck_epoch=2,
        secret_key=shared_secret,
        nonce="bridge-n2",
    )
    lv_verdict = verify_mcp_envelope(
        sb_bundle.envelope,
        secret_key=shared_secret,
        current_deck_epoch=2,
        actual_protocol=sb_bundle.python_code,
    )
    assert lv_verdict.passed is True


def test_static_validation_failure_blocks_bundle_minting() -> None:
    bad_protocol = {
        **MINIMAL_PROTOCOL,
        "steps": [
            {"transfer": {"src": "reservoir:A1", "dst": "plate:A1", "volume": 950.0}},
        ],
    }
    with pytest.raises(ValidationError):
        compile_with_mcp_envelope(bad_protocol, secret_key="sec")


def test_malformed_envelope_fields_rejected_default_deny() -> None:
    bundle = compile_with_mcp_envelope(MINIMAL_PROTOCOL, secret_key="schema-sec")
    executor = StepbotMcpExecutor(secret_key="schema-sec", current_deck_epoch=1, use_mock=True)

    for mutated in [
        {**bundle.envelope, "schema_version": "lean-mcp/v99"},
        {**bundle.envelope, "token_epoch": True},
        {k: v for k, v in bundle.envelope.items() if k != "hmac_sha256"},
        {**bundle.envelope, "unexpected_field": 123},
    ]:
        with pytest.raises(McpSecurityViolation) as exc_info:
            executor.dispatch(bundle.python_code, mutated)
        assert exc_info.value.rule == "mcp_schema"


def test_cli_mcp_compile_and_stateful_mcp_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = ROOT / "examples" / "dilution_series.yaml"
    out_py = tmp_path / "compiled.py"
    env_json = tmp_path / "envelope.json"
    state_json = tmp_path / "deck_state.json"

    rc_comp = cli_main(
        [
            "mcp-compile",
            str(yaml_path),
            "-o",
            str(out_py),
            "--envelope",
            str(env_json),
            "--epoch",
            "1",
            "--secret-key",
            "cli-e2e-secret",
            "--nonce",
            "cli-n1",
        ]
    )
    assert rc_comp == 0
    assert out_py.exists() and env_json.exists()
    capsys.readouterr()

    # First dispatch succeeds and persists updated deck epoch 2 to state_json
    rc_disp1 = cli_main(
        [
            "mcp-dispatch",
            str(out_py),
            "--envelope",
            str(env_json),
            "--state",
            str(state_json),
            "--secret-key",
            "cli-e2e-secret",
            "--mock",
            "--json",
        ]
    )
    assert rc_disp1 == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    assert receipt["current_deck_epoch"] == 2

    # Second dispatch with same state_json is denied at epoch 2
    rc_disp2 = cli_main(
        [
            "mcp-dispatch",
            str(out_py),
            "--envelope",
            str(env_json),
            "--state",
            str(state_json),
            "--secret-key",
            "cli-e2e-secret",
            "--mock",
            "--json",
        ]
    )
    assert rc_disp2 == 1
    deny = json.loads(capsys.readouterr().out)
    assert deny["ok"] is False
    assert deny["rule"] == "mcp_epoch_freshness"


def test_pure_english_across_stepbot_mcp_files() -> None:
    paths = [
        ROOT / "src" / "stepbot" / "mcp_runtime.py",
        ROOT / "src" / "stepbot" / "cli.py",
        ROOT / "src" / "stepbot" / "__init__.py",
        ROOT / "src" / "stepbot" / "checker.py",
        ROOT / "README.md",
        Path(__file__).resolve(),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Unexpected CJK character in {path}"

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
        ROOT / "src" / "stepbot" / "robot_http.py",
        ROOT / "src" / "stepbot" / "cli.py",
        ROOT / "src" / "stepbot" / "__init__.py",
        ROOT / "src" / "stepbot" / "checker.py",
        ROOT / "README.md",
        Path(__file__).resolve(),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Unexpected CJK character in {path}"


# ---------------------------------------------------------------------------
# Issue #28: Atomic fsync + os.replace state persistence, fail-closed schema,
# and bounded FIFO consumed_nonces retention
# ---------------------------------------------------------------------------


def test_corrupted_state_file_fails_closed_without_epoch_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml_path = ROOT / "examples" / "dilution_series.yaml"
    out_py = tmp_path / "out.py"
    env_json = tmp_path / "env.json"
    state_json = tmp_path / "deck_state.json"

    assert (
        cli_main(
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
                "state-hardening-secret",
                "--nonce",
                "n1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    malformed_payloads = [
        '{"current_deck_epoch": "5", "consumed_nonces": {"n1": true}}\n',
        '["not", "an", "object"]\n',
        '{"current_deck_epoch": true, "consumed_nonces": []}\n',
        '{"current_deck_epoch": -1, "consumed_nonces": []}\n',
        '{"current_deck_epoch": 2, "consumed_nonces": [""]}\n',
        '{"current_deck_epoch": 2}\n',
        "{not-valid-json",
        "",
    ]
    for raw_bad in malformed_payloads:
        state_json.write_text(raw_bad, encoding="utf-8")
        rc = cli_main(
            [
                "mcp-dispatch",
                str(out_py),
                "--envelope",
                str(env_json),
                "--state",
                str(state_json),
                "--secret-key",
                "state-hardening-secret",
                "--mock",
                "--json",
            ]
        )
        assert rc == 1
        err_obj = json.loads(capsys.readouterr().out)
        assert err_obj["ok"] is False
        assert err_obj["rule"] == "mcp_state_corrupt"
        assert state_json.read_text(encoding="utf-8") == raw_bad


def test_save_mcp_state_atomic_uses_fsync_replace_and_preserves_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as std_os
    from stepbot.mcp_runtime import load_mcp_state, save_mcp_state_atomic

    state_file = tmp_path / "atomic_state.json"
    save_mcp_state_atomic(
        state_file,
        current_deck_epoch=4,
        consumed_nonces=("n_first", "n_second"),
    )
    original_bytes = state_file.read_bytes()
    loaded = load_mcp_state(state_file)
    assert loaded.current_deck_epoch == 4
    assert loaded.consumed_nonces == ("n_first", "n_second")
    assert loaded.deck_tainted is False

    fsync_calls: list[int] = []
    replace_calls: list[tuple[str, Path]] = []
    real_fsync = std_os.fsync
    real_replace = std_os.replace

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    def spy_replace(src: str | Path, dst: str | Path) -> None:
        replace_calls.append((str(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(std_os, "fsync", spy_fsync)
    monkeypatch.setattr(std_os, "replace", spy_replace)

    save_mcp_state_atomic(
        state_file,
        current_deck_epoch=5,
        consumed_nonces=("n_first", "n_second", "n_third"),
    )
    assert len(fsync_calls) == 1
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == state_file

    # Simulate ENOSPC / disk write crash inside os.write -> target file stays intact & zero temp residue
    before_crash_bytes = state_file.read_bytes()

    def failing_write(_fd: int, _buf: bytes) -> int:
        raise OSError("Simulated ENOSPC mid-write failure")

    monkeypatch.setattr(std_os, "write", failing_write)
    with pytest.raises(OSError, match="Simulated ENOSPC"):
        save_mcp_state_atomic(
            state_file,
            current_deck_epoch=99,
            consumed_nonces=("corrupted",),
        )
    assert state_file.read_bytes() == before_crash_bytes
    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []


def test_bounded_fifo_consumed_nonces_evicts_oldest() -> None:
    secret = "fifo-cap-secret"
    executor = StepbotMcpExecutor(
        secret_key=secret,
        current_deck_epoch=1,
        consumed_nonces=["seed-1", "seed-2"],
        use_mock=True,
        max_consumed_nonces=3,
    )
    assert executor.consumed_nonces == ("seed-1", "seed-2")

    b1 = compile_with_mcp_envelope(MINIMAL_PROTOCOL, deck_epoch=1, secret_key=secret, nonce="run-3")
    executor.dispatch(b1.python_code, b1.envelope)
    assert executor.consumed_nonces == ("seed-1", "seed-2", "run-3")

    b2 = compile_with_mcp_envelope(MINIMAL_PROTOCOL, deck_epoch=2, secret_key=secret, nonce="run-4")
    executor.dispatch(b2.python_code, b2.envelope)
    assert executor.consumed_nonces == ("seed-2", "run-3", "run-4")


# ---------------------------------------------------------------------------
# Issue #29: Enforce envelope_bounds in StepbotMcpExecutor.dispatch
# ---------------------------------------------------------------------------


def test_dispatch_enforces_envelope_bounds_slots_volume_and_api_level() -> None:
    from stepbot.mcp_runtime import compute_envelope_hmac, compute_protocol_sha256

    secret = "bounds-check-secret"
    bundle = compile_with_mcp_envelope(MINIMAL_PROTOCOL, deck_epoch=1, secret_key=secret)
    sha = compute_protocol_sha256(bundle.python_code)

    # 1. Slot violation: script loads [1, 2, 4], envelope allows only [1, 2]
    bad_slots_env: dict[str, object] = {
        **{k: v for k, v in bundle.envelope.items() if k != "hmac_sha256"},
        "nonce": "bounds-slots",
        "envelope_bounds": {"deck_slots": [1, 2], "max_transfer_ul": 100.0, "api_level": "2.16"},
    }
    bad_slots_env["hmac_sha256"] = compute_envelope_hmac(bad_slots_env, secret)
    exec_slots = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)
    with pytest.raises(McpSecurityViolation) as err_slots:
        exec_slots.dispatch(bundle.python_code, bad_slots_env)
    assert err_slots.value.rule == "mcp_bounds_exceeded"
    assert exec_slots.current_deck_epoch == 1

    # 2. Volume violation: script transfers 100 uL, envelope caps at 20 uL (even with simulate=False)
    bad_vol_env: dict[str, object] = {
        **{k: v for k, v in bundle.envelope.items() if k != "hmac_sha256"},
        "nonce": "bounds-vol",
        "envelope_bounds": {"deck_slots": [1, 2, 4], "max_transfer_ul": 20.0, "api_level": "2.16"},
    }
    bad_vol_env["hmac_sha256"] = compute_envelope_hmac(bad_vol_env, secret)
    exec_vol = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)
    with pytest.raises(McpSecurityViolation) as err_vol:
        exec_vol.dispatch(bundle.python_code, bad_vol_env, simulate=False)
    assert err_vol.value.rule == "mcp_bounds_exceeded"
    assert exec_vol.current_deck_epoch == 1

    # 3. API level violation: script declares 2.16, envelope claims 2.14
    bad_api_env: dict[str, object] = {
        **{k: v for k, v in bundle.envelope.items() if k != "hmac_sha256"},
        "protocol_sha256": sha,
        "nonce": "bounds-api",
        "envelope_bounds": {"deck_slots": [1, 2, 4], "max_transfer_ul": 100.0, "api_level": "2.14"},
    }
    bad_api_env["hmac_sha256"] = compute_envelope_hmac(bad_api_env, secret)
    exec_api = StepbotMcpExecutor(secret_key=secret, current_deck_epoch=1, use_mock=True)
    with pytest.raises(McpSecurityViolation) as err_api:
        exec_api.dispatch(bundle.python_code, bad_api_env)
    assert err_api.value.rule == "mcp_bounds_exceeded"
    assert exec_api.current_deck_epoch == 1


# ---------------------------------------------------------------------------
# Issue #33: Loopback Opentrons HTTP Run API v2 adapter & two-phase taint lock
# ---------------------------------------------------------------------------


def test_opentrons_http_run_adapter_loopback_nominal_busy_default_secret_and_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from stepbot import DEFAULT_MCP_SECRET_KEY, OpentronsHttpRunAdapter

    server_state: dict[str, object] = {
        "existing_runs": [],
        "terminal_status": "succeeded",
        "uploaded_bytes": b"",
        "actions": [],
        "headers_seen": [],
        "poll_count": 0,
    }

    class MockOpentronsHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, payload: dict[str, object]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            ver = self.headers.get("Opentrons-Version", "")
            cast_headers = server_state["headers_seen"]
            assert isinstance(cast_headers, list)
            cast_headers.append(ver)
            if self.path == "/health":
                self._send_json(
                    200,
                    {
                        "name": "loopback-flex-01",
                        "api_version": "7.3.0",
                        "robot_model": "OT-3 Standard",
                        "robot_serial": "FLX-LOOP-01",
                    },
                )
                return
            if self.path == "/runs":
                self._send_json(200, {"data": server_state["existing_runs"]})
                return
            if self.path == "/runs/run-mock-1":
                polls = int(str(server_state["poll_count"])) + 1
                server_state["poll_count"] = polls
                if polls == 1:
                    self._send_json(200, {"data": {"id": "run-mock-1", "status": "running"}})
                else:
                    term = str(server_state["terminal_status"])
                    errs = (
                        [{"errorType": "OverpressureError", "detail": "Tip clogged on well A1"}]
                        if term != "succeeded"
                        else []
                    )
                    self._send_json(
                        200,
                        {
                            "data": {
                                "id": "run-mock-1",
                                "status": term,
                                "commandCount": 14,
                                "errors": errs,
                            }
                        },
                    )
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.path == "/protocols":
                server_state["uploaded_bytes"] = body
                self._send_json(201, {"data": {"id": "proto-mock-1"}})
                return
            if self.path == "/runs":
                self._send_json(201, {"data": {"id": "run-mock-1", "status": "idle"}})
                return
            if self.path == "/runs/run-mock-1/actions":
                act = json.loads(body.decode("utf-8"))["data"]["actionType"]
                acts = server_state["actions"]
                assert isinstance(acts, list)
                acts.append(act)
                self._send_json(201, {"data": {"id": "act-1", "actionType": act}})
                return
            self._send_json(404, {"error": "not found"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockOpentronsHandler)
    port = httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        adapter = OpentronsHttpRunAdapter(base_url, timeout_s=5.0, poll_interval_s=0.01)

        # 1. Public DEFAULT_MCP_SECRET_KEY is rejected when hardware_adapter is attached
        dev_bundle = compile_with_mcp_envelope(
            MINIMAL_PROTOCOL, deck_epoch=1, secret_key=DEFAULT_MCP_SECRET_KEY, nonce="dev-n1"
        )
        dev_exec = StepbotMcpExecutor(
            secret_key=DEFAULT_MCP_SECRET_KEY,
            current_deck_epoch=1,
            use_mock=True,
            hardware_adapter=adapter,
        )
        with pytest.raises(McpSecurityViolation) as exc_def:
            dev_exec.dispatch(dev_bundle.python_code, dev_bundle.envelope)
        assert exc_def.value.rule == "mcp_insecure_default_secret"

        # 2. Busy robot server is rejected BEFORE consuming nonce or advancing epoch
        hw_secret = "dedicated-hil-secret-2026"
        b1 = compile_with_mcp_envelope(
            MINIMAL_PROTOCOL, deck_epoch=1, secret_key=hw_secret, nonce="hil-n1"
        )
        server_state["existing_runs"] = [{"id": "other-run", "status": "running"}]
        hw_exec = StepbotMcpExecutor(
            secret_key=hw_secret,
            current_deck_epoch=1,
            use_mock=True,
            hardware_adapter=adapter,
        )
        with pytest.raises(McpSecurityViolation) as exc_busy:
            hw_exec.dispatch(b1.python_code, b1.envelope)
        assert exc_busy.value.rule == "mcp_hardware_busy"
        assert hw_exec.current_deck_epoch == 1
        assert "hil-n1" not in hw_exec.consumed_nonces
        assert hw_exec.deck_tainted is False

        # 3. Nominal HTTP Run API v2 dispatch succeeds and verifies Opentrons-Version: 3
        server_state["existing_runs"] = []
        server_state["poll_count"] = 0
        receipt = hw_exec.dispatch(b1.python_code, b1.envelope)
        assert receipt["ok"] is True
        assert receipt["backend"] == "opentrons_http_v2"
        assert receipt["protocol_id"] == "proto-mock-1"
        assert receipt["run_id"] == "run-mock-1"
        assert receipt["run_status"] == "succeeded"
        assert receipt["current_deck_epoch"] == 2
        assert "3" in server_state["headers_seen"]  # type: ignore[operator]
        assert b1.python_code.encode("utf-8") in server_state["uploaded_bytes"]  # type: ignore[operator]
        assert server_state["actions"] == ["play"]

        # 4. Mid-run physical fault ('failed') consumes nonce, advances epoch, and taints deck
        server_state["terminal_status"] = "failed"
        server_state["poll_count"] = 0
        server_state["actions"] = []
        out_py = tmp_path / "hil.py"
        env2_json = tmp_path / "env2.json"
        env3_json = tmp_path / "env3.json"
        state_json = tmp_path / "hil_state.json"
        b2 = compile_with_mcp_envelope(
            MINIMAL_PROTOCOL, deck_epoch=2, secret_key=hw_secret, nonce="hil-fault-n2"
        )
        b3 = compile_with_mcp_envelope(
            MINIMAL_PROTOCOL, deck_epoch=3, secret_key=hw_secret, nonce="hil-retry-n3"
        )
        out_py.write_text(b2.python_code, encoding="utf-8", newline="\n")
        env2_json.write_text(json.dumps(b2.envelope), encoding="utf-8")
        env3_json.write_text(json.dumps(b3.envelope), encoding="utf-8")
        from stepbot.mcp_runtime import load_mcp_state, save_mcp_state_atomic

        save_mcp_state_atomic(
            state_json,
            current_deck_epoch=2,
            consumed_nonces=("hil-n1",),
        )

        rc_fault = cli_main(
            [
                "mcp-dispatch",
                str(out_py),
                "--envelope",
                str(env2_json),
                "--state",
                str(state_json),
                "--secret-key",
                hw_secret,
                "--robot-url",
                base_url,
                "--poll-interval",
                "0.01",
                "--run-timeout",
                "5.0",
                "--mock",
                "--json",
            ]
        )
        assert rc_fault == 1
        fault_out = json.loads(capsys.readouterr().out)
        assert fault_out["rule"] == "mcp_hardware_run_failed"

        persisted = load_mcp_state(state_json)
        assert persisted.current_deck_epoch == 3
        assert "hil-fault-n2" in persisted.consumed_nonces
        assert persisted.deck_tainted is True

        # Subsequent automated retry at epoch 3 is blocked by deck_tainted!
        rc_blocked = cli_main(
            [
                "mcp-dispatch",
                str(out_py),
                "--envelope",
                str(env3_json),
                "--state",
                str(state_json),
                "--secret-key",
                hw_secret,
                "--robot-url",
                base_url,
                "--poll-interval",
                "0.01",
                "--mock",
                "--json",
            ]
        )
        assert rc_blocked == 1
        blocked_out = json.loads(capsys.readouterr().out)
        assert blocked_out["rule"] == "mcp_deck_tainted"

        # Explicit human supervisor taint clear restores dispatch capability
        server_state["terminal_status"] = "succeeded"
        server_state["poll_count"] = 0
        rc_recovered = cli_main(
            [
                "mcp-dispatch",
                str(out_py),
                "--envelope",
                str(env3_json),
                "--state",
                str(state_json),
                "--secret-key",
                hw_secret,
                "--robot-url",
                base_url,
                "--poll-interval",
                "0.01",
                "--clear-taint",
                "--supervisor-id",
                "dr_operator",
                "--mock",
                "--json",
            ]
        )
        assert rc_recovered == 0
        rec_out = json.loads(capsys.readouterr().out)
        assert rec_out["ok"] is True
        assert rec_out["current_deck_epoch"] == 4
        assert load_mcp_state(state_json).deck_tainted is False
    finally:
        httpd.shutdown()
        httpd.server_close()

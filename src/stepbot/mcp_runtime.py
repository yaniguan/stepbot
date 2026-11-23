"""Zero-trust `Lean-MCP` (`lean-mcp/v1`) hardware capability compiler and runtime executor."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Any

import yaml

from stepbot.checker import check_protocol_source
from stepbot.codegen import render_protocol
from stepbot.compiler import load_protocol_data, parse_and_validate
from stepbot.models import (
    DiscardStep,
    DistributeStep,
    MixStep,
    ProtocolSpec,
    TransferStep,
    ValidationError,
)

LEAN_MCP_SCHEMA_VERSION = "lean-mcp/v1"
DEFAULT_MCP_TOOL_NAME = "opentrons.execute_protocol"
DEFAULT_MCP_SECRET_KEY = "lean-mcp-dev-secret-v1"

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_REQUIRED_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "tool_name",
        "protocol_sha256",
        "token_epoch",
        "nonce",
        "lean_proof_valid",
        "is_frontier_exception",
        "human_supervisor_signed",
        "human_supervisor_id",
        "envelope_bounds",
        "hmac_sha256",
    }
)


class McpSecurityViolation(RuntimeError):
    """Default-deny exception raised when a Lean-MCP hardware dispatch fails verification."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.reason = message


@dataclass(frozen=True)
class CompiledMcpBundle:
    """Deterministic Opentrons Python API v2 script paired with a `lean-mcp/v1` envelope."""

    python_code: str
    protocol_sha256: str
    envelope: dict[str, object]


def compute_protocol_sha256(python_code: str) -> str:
    """Compute canonical LF-normalized UTF-8 SHA-256 hex digest of compiled Python code."""
    normalized = python_code.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_envelope_hmac(
    envelope: Mapping[str, object],
    secret_key: str = DEFAULT_MCP_SECRET_KEY,
) -> str:
    """Compute canonical hex HMAC-SHA256 over all envelope fields except `hmac_sha256`."""
    if not secret_key:
        raise ValueError("secret_key must be a non-empty string")
    body: dict[str, object] = {k: v for k, v in envelope.items() if k != "hmac_sha256"}
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _load_protocol_mapping(protocol_source: Path | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(protocol_source, Mapping):
        return dict(protocol_source)
    if isinstance(protocol_source, Path):
        return load_protocol_data(protocol_source)
    if "\n" not in protocol_source:
        maybe_path = Path(protocol_source)
        if maybe_path.exists():
            return load_protocol_data(maybe_path)
    try:
        parsed = yaml.safe_load(protocol_source)
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML/JSON protocol source: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Protocol root must be a mapping/object")
    result: dict[str, Any] = {str(k): v for k, v in parsed.items()}
    return result


def _extract_envelope_bounds(spec: ProtocolSpec) -> dict[str, object]:
    deck_slots: list[int] = sorted(item.slot for item in spec.deck.values())
    max_transfer_ul = 0.0
    for step in spec.steps:
        if isinstance(step, (TransferStep, DistributeStep, MixStep, DiscardStep)):
            if step.volume > max_transfer_ul:
                max_transfer_ul = float(step.volume)
    return {
        "deck_slots": deck_slots,
        "max_transfer_ul": float(max_transfer_ul),
        "api_level": spec.api_level,
    }


def compile_with_mcp_envelope(
    protocol_source: Path | str | Mapping[str, Any],
    *,
    deck_epoch: int = 1,
    secret_key: str = DEFAULT_MCP_SECRET_KEY,
    is_frontier_exception: bool = False,
    human_supervisor_signed: bool = False,
    human_supervisor_id: str = "",
    nonce: str | None = None,
    tool_name: str = DEFAULT_MCP_TOOL_NAME,
) -> CompiledMcpBundle:
    """Compile a YAML/JSON protocol and mint a wire-compatible `lean-mcp/v1` envelope."""
    if isinstance(deck_epoch, bool) or deck_epoch < 0:
        raise ValueError("deck_epoch must be a non-negative integer")
    if not secret_key:
        raise ValueError("secret_key must be a non-empty string")
    if not tool_name.strip():
        raise ValueError("tool_name must be a non-empty string")

    data = _load_protocol_mapping(protocol_source)
    spec = parse_and_validate(data)
    python_code = render_protocol(spec)
    static_check = check_protocol_source(python_code, use_mock=True)
    lean_proof_valid = bool(static_check.ok)

    protocol_sha256 = compute_protocol_sha256(python_code)
    resolved_nonce = secrets.token_hex(16) if nonce is None else nonce
    if not resolved_nonce.strip():
        raise ValueError("nonce must be a non-empty string")

    envelope: dict[str, object] = {
        "schema_version": LEAN_MCP_SCHEMA_VERSION,
        "tool_name": tool_name,
        "protocol_sha256": protocol_sha256,
        "token_epoch": deck_epoch,
        "nonce": resolved_nonce,
        "lean_proof_valid": lean_proof_valid,
        "is_frontier_exception": bool(is_frontier_exception),
        "human_supervisor_signed": bool(human_supervisor_signed),
        "human_supervisor_id": human_supervisor_id,
        "envelope_bounds": _extract_envelope_bounds(spec),
    }
    envelope["hmac_sha256"] = compute_envelope_hmac(envelope, secret_key)

    return CompiledMcpBundle(
        python_code=python_code,
        protocol_sha256=protocol_sha256,
        envelope=envelope,
    )


class StepbotMcpExecutor:
    """Stateful zero-trust runtime enforcing `lean-mcp/v1` invariants before deck execution."""

    def __init__(
        self,
        *,
        secret_key: str = DEFAULT_MCP_SECRET_KEY,
        current_deck_epoch: int = 1,
        consumed_nonces: Iterable[str] | None = None,
        use_mock: bool = False,
    ) -> None:
        if not secret_key:
            raise ValueError("secret_key must be a non-empty string")
        if isinstance(current_deck_epoch, bool) or current_deck_epoch < 0:
            raise ValueError("current_deck_epoch must be a non-negative integer")
        self.secret_key: str = secret_key
        self.current_deck_epoch: int = current_deck_epoch
        self.consumed_nonces: set[str] = set(consumed_nonces) if consumed_nonces is not None else set()
        self.use_mock: bool = use_mock

    def dispatch(
        self,
        python_code: str,
        envelope: Mapping[str, object],
        *,
        simulate: bool = True,
    ) -> dict[str, object]:
        """Verify all 5 `lean-mcp/v1` invariants, dry-run/simulate `python_code`, and advance epoch."""
        if not isinstance(envelope, Mapping):
            raise McpSecurityViolation("mcp_schema", "MCP envelope must be a mapping")

        missing = sorted(_REQUIRED_ENVELOPE_KEYS - set(envelope.keys()))
        if missing:
            raise McpSecurityViolation(
                "mcp_schema", f"MCP envelope missing required field(s): {', '.join(missing)}"
            )
        extra = sorted(set(envelope.keys()) - _REQUIRED_ENVELOPE_KEYS)
        if extra:
            raise McpSecurityViolation(
                "mcp_schema", f"MCP envelope contains unknown field(s): {', '.join(extra)}"
            )

        schema_version = envelope["schema_version"]
        tool_name = envelope["tool_name"]
        protocol_sha256 = envelope["protocol_sha256"]
        token_epoch = envelope["token_epoch"]
        nonce = envelope["nonce"]
        lean_proof_valid = envelope["lean_proof_valid"]
        is_frontier_exception = envelope["is_frontier_exception"]
        human_supervisor_signed = envelope["human_supervisor_signed"]
        human_supervisor_id = envelope["human_supervisor_id"]
        envelope_bounds = envelope["envelope_bounds"]
        hmac_sha256 = envelope["hmac_sha256"]

        if schema_version != LEAN_MCP_SCHEMA_VERSION:
            raise McpSecurityViolation(
                "mcp_schema",
                f"Unsupported schema_version {schema_version!r}; expected {LEAN_MCP_SCHEMA_VERSION!r}",
            )
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise McpSecurityViolation("mcp_schema", "tool_name must be a non-empty string")
        if not isinstance(protocol_sha256, str) or not _HEX64_RE.match(protocol_sha256):
            raise McpSecurityViolation(
                "mcp_schema", "protocol_sha256 must be a 64-character hex string"
            )
        if isinstance(token_epoch, bool) or not isinstance(token_epoch, int) or token_epoch < 0:
            raise McpSecurityViolation("mcp_schema", "token_epoch must be a non-negative integer")
        if not isinstance(nonce, str) or not nonce.strip():
            raise McpSecurityViolation("mcp_schema", "nonce must be a non-empty string")
        if not isinstance(lean_proof_valid, bool):
            raise McpSecurityViolation("mcp_schema", "lean_proof_valid must be a boolean")
        if not isinstance(is_frontier_exception, bool):
            raise McpSecurityViolation("mcp_schema", "is_frontier_exception must be a boolean")
        if not isinstance(human_supervisor_signed, bool):
            raise McpSecurityViolation("mcp_schema", "human_supervisor_signed must be a boolean")
        if not isinstance(human_supervisor_id, str):
            raise McpSecurityViolation("mcp_schema", "human_supervisor_id must be a string")
        if not isinstance(envelope_bounds, Mapping):
            raise McpSecurityViolation("mcp_schema", "envelope_bounds must be a mapping")
        if not isinstance(hmac_sha256, str) or not _HEX64_RE.match(hmac_sha256):
            raise McpSecurityViolation("mcp_schema", "hmac_sha256 must be a 64-character hex string")

        # 1. HMAC authenticity (blocks parameter or envelope tampering)
        expected_hmac = compute_envelope_hmac(envelope, self.secret_key)
        if not hmac.compare_digest(hmac_sha256.lower(), expected_hmac.lower()):
            raise McpSecurityViolation(
                "mcp_hmac_authentic",
                "Envelope HMAC-SHA256 verification failed under executor secret_key",
            )

        # 2. Protocol SHA-256 payload binding (blocks post-compile code swap/tampering)
        actual_sha256 = compute_protocol_sha256(python_code)
        if actual_sha256.lower() != protocol_sha256.lower():
            raise McpSecurityViolation(
                "mcp_payload_binding",
                f"Dispatched script SHA-256 ({actual_sha256}) != envelope protocol_sha256 ({protocol_sha256})",
            )

        # 3. Deck epoch freshness (blocks stale-state replay)
        if token_epoch != self.current_deck_epoch:
            raise McpSecurityViolation(
                "mcp_epoch_freshness",
                f"Stale or mismatched token_epoch ({token_epoch}) != current_deck_epoch ({self.current_deck_epoch})",
            )

        # 4. Single-use nonce replay check (blocks same-epoch replay)
        if nonce in self.consumed_nonces:
            raise McpSecurityViolation(
                "mcp_nonce_single_use",
                f"Replay detected: capability nonce {nonce!r} has already been consumed",
            )

        # 5. Formal proof certificate OR human-supervisor co-signed frontier override
        authorized = (lean_proof_valid and not is_frontier_exception) or (
            is_frontier_exception and human_supervisor_signed
        )
        if not authorized:
            raise McpSecurityViolation(
                "mcp_proof_or_cosigned_exception",
                "Dispatch requires a valid Lean/static proof inside regime or a human-cosigned frontier exception",
            )

        # Execute stepbot deck simulator / safe AST dry-run before mutating state
        backend = "dry_run_skipped"
        command_count = 0
        line_count = 0
        if simulate:
            sim_result = check_protocol_source(python_code, use_mock=self.use_mock)
            if not sim_result.ok:
                err_detail = "; ".join(sim_result.errors) or "protocol simulation failed"
                raise McpSecurityViolation("mcp_simulation_failed", err_detail)
            backend = sim_result.backend
            command_count = sim_result.command_count
            line_count = sim_result.line_count

        executed_epoch = self.current_deck_epoch
        self.consumed_nonces.add(nonce)
        self.current_deck_epoch += 1

        return {
            "ok": True,
            "tool_name": tool_name,
            "protocol_sha256": actual_sha256,
            "nonce": nonce,
            "executed_epoch": executed_epoch,
            "current_deck_epoch": self.current_deck_epoch,
            "backend": backend,
            "command_count": command_count,
            "line_count": line_count,
        }

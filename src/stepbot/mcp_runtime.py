"""Zero-trust `Lean-MCP` (`lean-mcp/v1`) hardware capability compiler and runtime executor."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
import contextlib
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from typing import Any

import yaml

from stepbot.catalog import deck_slot_sort_key
from stepbot.checker import CheckResult, check_protocol_source
from stepbot.codegen import build_protocol_manifest, render_protocol
from stepbot.compiler import load_protocol_data, parse_and_validate
from stepbot.models import (
    VOLUME_TOLERANCE,
    DeckSlot,
    DiscardStep,
    DistributeStep,
    McpSecurityViolation,
    MixStep,
    ProtocolSpec,
    TransferStep,
    ValidationError,
)
from stepbot.robot_http import HardwareRunAdapter, OpentronsHttpRunAdapter

LEAN_MCP_SCHEMA_VERSION = "lean-mcp/v1"
DEFAULT_MCP_TOOL_NAME = "opentrons.execute_protocol"
DEFAULT_MCP_SECRET_KEY = "lean-mcp-dev-secret-v1"
DEFAULT_MAX_CONSUMED_NONCES = 4096

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

_ALLOWED_STATE_KEYS = frozenset(
    {
        "current_deck_epoch",
        "consumed_nonces",
        "deck_tainted",
        "taint_reason",
    }
)


@dataclass(frozen=True)
class CompiledMcpBundle:
    """Deterministic Opentrons Python API v2 script paired with a `lean-mcp/v1` envelope."""

    python_code: str
    protocol_sha256: str
    envelope: dict[str, object]
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpPersistentState:
    """Validated anti-replay and taint state persisted by `stepbot mcp-dispatch --state`."""

    current_deck_epoch: int
    consumed_nonces: tuple[str, ...]
    deck_tainted: bool = False
    taint_reason: str | None = None


def load_mcp_state(path: Path) -> McpPersistentState:
    """Parse and strictly validate a persisted `--state` JSON file (fail-closed)."""
    if path.is_dir():
        raise McpSecurityViolation(
            "mcp_state_corrupt", f"MCP state path is a directory, not a JSON file: {path}"
        )
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise McpSecurityViolation(
            "mcp_state_corrupt", f"Cannot read MCP state file {path}: {exc}"
        ) from exc

    if not raw_text.strip():
        raise McpSecurityViolation(
            "mcp_state_corrupt", f"MCP state file {path} is empty (0 bytes or whitespace)"
        )

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise McpSecurityViolation(
            "mcp_state_corrupt", f"Invalid JSON in MCP state file {path}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state root must be a JSON object, got {type(parsed).__name__}",
        )

    extra = sorted(set(parsed.keys()) - _ALLOWED_STATE_KEYS)
    if extra:
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state contains unknown field(s): {', '.join(extra)}",
        )

    if "current_deck_epoch" not in parsed:
        raise McpSecurityViolation(
            "mcp_state_corrupt", "MCP state missing required integer 'current_deck_epoch'"
        )
    raw_epoch = parsed["current_deck_epoch"]
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int) or raw_epoch < 0:
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state 'current_deck_epoch' must be a non-negative integer, got {raw_epoch!r}",
        )

    if "consumed_nonces" not in parsed:
        raise McpSecurityViolation(
            "mcp_state_corrupt", "MCP state missing required list 'consumed_nonces'"
        )
    raw_nonces = parsed["consumed_nonces"]
    if not isinstance(raw_nonces, list):
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state 'consumed_nonces' must be a JSON array, got {type(raw_nonces).__name__}",
        )
    validated_nonces: list[str] = []
    for idx, item in enumerate(raw_nonces):
        if not isinstance(item, str) or not item.strip():
            raise McpSecurityViolation(
                "mcp_state_corrupt",
                f"MCP state 'consumed_nonces[{idx}]' must be a non-empty string, got {item!r}",
            )
        validated_nonces.append(item)

    raw_tainted = parsed.get("deck_tainted", False)
    if not isinstance(raw_tainted, bool):
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state 'deck_tainted' must be a boolean, got {raw_tainted!r}",
        )
    raw_reason = parsed.get("taint_reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise McpSecurityViolation(
            "mcp_state_corrupt",
            f"MCP state 'taint_reason' must be a string or null, got {raw_reason!r}",
        )

    return McpPersistentState(
        current_deck_epoch=raw_epoch,
        consumed_nonces=tuple(validated_nonces),
        deck_tainted=raw_tainted,
        taint_reason=raw_reason,
    )


def save_mcp_state_atomic(
    path: Path,
    *,
    current_deck_epoch: int,
    consumed_nonces: Sequence[str],
    deck_tainted: bool = False,
    taint_reason: str | None = None,
) -> None:
    """Persist `--state` crash-safely via sibling temp file + `os.fsync` + `os.replace`."""
    if isinstance(current_deck_epoch, bool) or current_deck_epoch < 0:
        raise ValueError("current_deck_epoch must be a non-negative integer")
    path.parent.mkdir(parents=True, exist_ok=True)

    payload_obj: dict[str, object] = {
        "current_deck_epoch": current_deck_epoch,
        "consumed_nonces": list(consumed_nonces),
        "deck_tainted": bool(deck_tainted),
    }
    if taint_reason is not None:
        payload_obj["taint_reason"] = taint_reason
    payload_bytes = (json.dumps(payload_obj, indent=2) + "\n").encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    fd_open = True
    try:
        view = memoryview(payload_bytes)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("os.write wrote 0 bytes while persisting MCP state")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd_open = False
        os.replace(tmp_name, path)
    except BaseException:
        if fd_open:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def mcp_state_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive cross-process advisory lock on `<path>.lock`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    deck_slots: list[DeckSlot] = sorted(
        (item.slot for item in spec.deck.values()),
        key=deck_slot_sort_key,
    )
    max_transfer_ul = 0.0
    for step in spec.steps:
        if isinstance(step, (TransferStep, DistributeStep, MixStep, DiscardStep)):
            if step.volume > max_transfer_ul:
                max_transfer_ul = float(step.volume)
        if isinstance(step, TransferStep) and step.mix_after is not None:
            if step.mix_after[1] > max_transfer_ul:
                max_transfer_ul = float(step.mix_after[1])
    bounds: dict[str, object] = {
        "deck_slots": deck_slots,
        "max_transfer_ul": float(max_transfer_ul),
        "api_level": spec.api_level,
    }
    if spec.robot_type != "OT-2":
        bounds["robot_type"] = spec.robot_type
    return bounds


def _verify_envelope_bounds(
    envelope_bounds: Mapping[str, object],
    inspection: CheckResult,
) -> None:
    """Enforce `envelope_bounds` (`api_level`, `deck_slots`, `max_transfer_ul`) against script."""
    if "api_level" in envelope_bounds and envelope_bounds["api_level"] is not None:
        bound_api = envelope_bounds["api_level"]
        if not isinstance(bound_api, str) or not bound_api.strip():
            raise McpSecurityViolation(
                "mcp_schema", "envelope_bounds['api_level'] must be a non-empty string"
            )
        if inspection.api_level != bound_api.strip():
            raise McpSecurityViolation(
                "mcp_bounds_exceeded",
                f"Dispatched protocol apiLevel ({inspection.api_level!r}) != "
                f"envelope_bounds['api_level'] ({bound_api!r})",
            )

    if "robot_type" in envelope_bounds and envelope_bounds["robot_type"] is not None:
        bound_robot = envelope_bounds["robot_type"]
        if not isinstance(bound_robot, str) or not bound_robot.strip():
            raise McpSecurityViolation(
                "mcp_schema", "envelope_bounds['robot_type'] must be a non-empty string"
            )
        if inspection.robot_type != bound_robot.strip():
            raise McpSecurityViolation(
                "mcp_bounds_exceeded",
                f"Dispatched protocol robotType ({inspection.robot_type!r}) != "
                f"envelope_bounds['robot_type'] ({bound_robot!r})",
            )

    if "deck_slots" in envelope_bounds and envelope_bounds["deck_slots"] is not None:
        raw_slots = envelope_bounds["deck_slots"]
        if not isinstance(raw_slots, (list, tuple)):
            raise McpSecurityViolation(
                "mcp_schema", "envelope_bounds['deck_slots'] must be a list of deck slots"
            )
        allowed_slots: set[DeckSlot] = set()
        for slot in raw_slots:
            if isinstance(slot, bool) or not isinstance(slot, (int, str)):
                raise McpSecurityViolation(
                    "mcp_schema",
                    f"Invalid slot {slot!r} inside envelope_bounds['deck_slots']",
                )
            allowed_slots.add(slot)
        disallowed = [s for s in inspection.loaded_slots if s not in allowed_slots]
        if disallowed:
            raise McpSecurityViolation(
                "mcp_bounds_exceeded",
                f"Dispatched protocol loads deck slot(s) {disallowed} outside "
                f"envelope_bounds['deck_slots'] ({sorted(allowed_slots, key=deck_slot_sort_key)})",
            )

    vol_bound_raw: object = envelope_bounds.get(
        "max_transfer_ul", envelope_bounds.get("max_volume_ul")
    )
    if vol_bound_raw is not None:
        if (
            isinstance(vol_bound_raw, bool)
            or not isinstance(vol_bound_raw, (int, float))
            or float(vol_bound_raw) < 0.0
        ):
            raise McpSecurityViolation(
                "mcp_schema",
                "envelope_bounds['max_transfer_ul'] must be a non-negative number",
            )
        max_bound = float(vol_bound_raw)
        if inspection.max_op_volume_ul > max_bound + VOLUME_TOLERANCE:
            raise McpSecurityViolation(
                "mcp_bounds_exceeded",
                f"Dispatched protocol peak operation volume ({inspection.max_op_volume_ul:g} uL) "
                f"exceeds envelope_bounds['max_transfer_ul'] ({max_bound:g} uL)",
            )


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
    manifest = build_protocol_manifest(spec, python_code)
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
        manifest=manifest,
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
        max_consumed_nonces: int = DEFAULT_MAX_CONSUMED_NONCES,
        hardware_adapter: HardwareRunAdapter | None = None,
        deck_tainted: bool = False,
        taint_reason: str | None = None,
    ) -> None:
        if not secret_key:
            raise ValueError("secret_key must be a non-empty string")
        if isinstance(current_deck_epoch, bool) or current_deck_epoch < 0:
            raise ValueError("current_deck_epoch must be a non-negative integer")
        if isinstance(max_consumed_nonces, bool) or max_consumed_nonces <= 0:
            raise ValueError("max_consumed_nonces must be a positive integer")
        self.secret_key: str = secret_key
        self.current_deck_epoch: int = current_deck_epoch
        self.max_consumed_nonces: int = max_consumed_nonces
        self._consumed_nonces: OrderedDict[str, None] = OrderedDict()
        if consumed_nonces is not None:
            for item in consumed_nonces:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"consumed_nonces entries must be non-empty strings, got {item!r}")
                self._record_nonce(item)
        self.use_mock: bool = use_mock
        self.hardware_adapter: HardwareRunAdapter | None = hardware_adapter
        self.deck_tainted: bool = bool(deck_tainted)
        self.taint_reason: str | None = taint_reason
        self.last_check_result: CheckResult | None = None

    @property
    def consumed_nonces(self) -> tuple[str, ...]:
        """Return bounded FIFO-ordered tuple of consumed capability nonces."""
        return tuple(self._consumed_nonces.keys())

    def _record_nonce(self, nonce: str) -> None:
        if nonce in self._consumed_nonces:
            self._consumed_nonces.move_to_end(nonce)
        else:
            self._consumed_nonces[nonce] = None
        while len(self._consumed_nonces) > self.max_consumed_nonces:
            self._consumed_nonces.popitem(last=False)

    def clear_deck_taint(self, *, supervisor_id: str) -> None:
        """Explicitly clear `deck_tainted` after human supervisor physical deck inspection."""
        if not isinstance(supervisor_id, str) or not supervisor_id.strip():
            raise ValueError("supervisor_id must be a non-empty string to clear deck taint")
        self.deck_tainted = False
        self.taint_reason = None

    def dispatch(
        self,
        python_code: str,
        envelope: Mapping[str, object],
        *,
        simulate: bool = True,
        on_pre_play_commit: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Verify all `lean-mcp/v1` invariants, enforce `envelope_bounds`, and execute."""
        if self.deck_tainted:
            detail = f" ({self.taint_reason})" if self.taint_reason else ""
            raise McpSecurityViolation(
                "mcp_deck_tainted",
                f"Deck is tainted from a prior aborted/faulted physical run{detail}; "
                "human supervisor must inspect wells and clear taint before dispatch",
            )

        if self.hardware_adapter is not None and self.secret_key == DEFAULT_MCP_SECRET_KEY:
            raise McpSecurityViolation(
                "mcp_insecure_default_secret",
                "Physical hardware dispatch forbids DEFAULT_MCP_SECRET_KEY; "
                "configure a dedicated secret key",
            )

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
        if nonce in self._consumed_nonces:
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

        # 6. Mandatory safe AST & envelope_bounds verification + pre-flight simulation
        must_simulate = bool(simulate) or (self.hardware_adapter is not None)
        sim_result = check_protocol_source(
            python_code,
            use_mock=(self.use_mock or not must_simulate),
        )
        self.last_check_result = sim_result
        if not sim_result.ok:
            err_detail = "; ".join(sim_result.errors) or "protocol simulation failed"
            raise McpSecurityViolation("mcp_simulation_failed", err_detail)

        _verify_envelope_bounds(envelope_bounds, sim_result)

        if must_simulate:
            backend = sim_result.backend
            command_count = sim_result.command_count
            line_count = sim_result.line_count
        else:
            backend = "dry_run_skipped"
            command_count = 0
            line_count = 0

        # 7. Simulation-only vs two-phase Hardware-in-the-Loop HTTP Run API v2 execution
        if self.hardware_adapter is None:
            executed_epoch = self.current_deck_epoch
            self._record_nonce(nonce)
            self.current_deck_epoch += 1
            if on_pre_play_commit is not None:
                on_pre_play_commit()
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
                "well_volumes_ul": dict(sim_result.well_volumes_ul),
                "tips_used_per_mount": dict(sim_result.tips_used_per_mount),
            }

        health = self.hardware_adapter.preflight_check()
        protocol_id, run_id = self.hardware_adapter.upload_and_create_run(
            python_code,
            expected_sha256=actual_sha256,
        )

        # Two-phase state transition (#33): commit nonce & advance epoch BEFORE physical `play`
        executed_epoch = self.current_deck_epoch
        self._record_nonce(nonce)
        self.current_deck_epoch += 1
        if on_pre_play_commit is not None:
            on_pre_play_commit()

        try:
            hw_result = self.hardware_adapter.play_and_wait(protocol_id, run_id, health)
        except McpSecurityViolation as exc:
            self.deck_tainted = True
            self.taint_reason = (
                f"Hardware transport fault during active run '{run_id}': {exc.reason}"
            )
            raise McpSecurityViolation("mcp_hardware_run_failed", self.taint_reason) from exc

        if not hw_result.ok:
            err_detail = "; ".join(hw_result.errors) or (
                f"Opentrons run '{run_id}' terminated in status {hw_result.run_status!r}"
            )
            self.deck_tainted = True
            self.taint_reason = err_detail
            raise McpSecurityViolation("mcp_hardware_run_failed", err_detail)

        return {
            "ok": True,
            "tool_name": tool_name,
            "protocol_sha256": actual_sha256,
            "nonce": nonce,
            "executed_epoch": executed_epoch,
            "current_deck_epoch": self.current_deck_epoch,
            "backend": "opentrons_http_v2",
            "command_count": hw_result.command_count or command_count,
            "line_count": line_count,
            "protocol_id": hw_result.protocol_id,
            "run_id": hw_result.run_id,
            "run_status": hw_result.run_status,
            "robot_name": hw_result.robot_name,
            "robot_model": hw_result.robot_model,
            "well_volumes_ul": dict(sim_result.well_volumes_ul),
            "tips_used_per_mount": dict(sim_result.tips_used_per_mount),
        }


__all__ = [
    "CompiledMcpBundle",
    "DEFAULT_MAX_CONSUMED_NONCES",
    "DEFAULT_MCP_SECRET_KEY",
    "DEFAULT_MCP_TOOL_NAME",
    "LEAN_MCP_SCHEMA_VERSION",
    "McpPersistentState",
    "McpSecurityViolation",
    "OpentronsHttpRunAdapter",
    "StepbotMcpExecutor",
    "compile_with_mcp_envelope",
    "compute_envelope_hmac",
    "compute_protocol_sha256",
    "load_mcp_state",
    "mcp_state_lock",
    "save_mcp_state_atomic",
]

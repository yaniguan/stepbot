"""stepbot: compiler from simple protocol steps to Opentrons Python API v2."""

from stepbot.checker import CheckResult, check_protocol, check_protocol_source
from stepbot.codegen import build_protocol_manifest
from stepbot.compiler import compile_file, compile_protocol, load_protocol_data, parse_and_validate
from stepbot.interop import check_twinstep_protocol, from_twinstep
from stepbot.mcp_runtime import (
    DEFAULT_MAX_CONSUMED_NONCES,
    DEFAULT_MCP_SECRET_KEY,
    LEAN_MCP_SCHEMA_VERSION,
    CompiledMcpBundle,
    McpPersistentState,
    McpSecurityViolation,
    StepbotMcpExecutor,
    compile_with_mcp_envelope,
    compute_envelope_hmac,
    compute_protocol_sha256,
    load_mcp_state,
    mcp_state_lock,
    save_mcp_state_atomic,
)
from stepbot.models import ProtocolSpec, ValidationError
from stepbot.robot_http import HardwareRunAdapter, HardwareRunResult, OpentronsHttpRunAdapter

__all__ = [
    "CheckResult",
    "CompiledMcpBundle",
    "DEFAULT_MAX_CONSUMED_NONCES",
    "DEFAULT_MCP_SECRET_KEY",
    "HardwareRunAdapter",
    "HardwareRunResult",
    "LEAN_MCP_SCHEMA_VERSION",
    "McpPersistentState",
    "McpSecurityViolation",
    "OpentronsHttpRunAdapter",
    "ProtocolSpec",
    "StepbotMcpExecutor",
    "ValidationError",
    "build_protocol_manifest",
    "check_protocol",
    "check_protocol_source",
    "check_twinstep_protocol",
    "compile_file",
    "compile_protocol",
    "compile_with_mcp_envelope",
    "compute_envelope_hmac",
    "compute_protocol_sha256",
    "from_twinstep",
    "load_mcp_state",
    "load_protocol_data",
    "mcp_state_lock",
    "parse_and_validate",
    "save_mcp_state_atomic",
]

__version__ = "0.1.0"

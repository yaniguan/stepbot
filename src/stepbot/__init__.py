"""stepbot: compiler from simple protocol steps to Opentrons Python API v2."""

from stepbot.checker import CheckResult, check_protocol, check_protocol_source
from stepbot.codegen import build_protocol_manifest
from stepbot.compiler import compile_file, compile_protocol, load_protocol_data, parse_and_validate
from stepbot.interop import check_twinstep_protocol, from_twinstep
from stepbot.mcp_runtime import (
    DEFAULT_MCP_SECRET_KEY,
    LEAN_MCP_SCHEMA_VERSION,
    CompiledMcpBundle,
    McpSecurityViolation,
    StepbotMcpExecutor,
    compile_with_mcp_envelope,
    compute_envelope_hmac,
    compute_protocol_sha256,
)
from stepbot.models import ProtocolSpec, ValidationError

__all__ = [
    "CheckResult",
    "CompiledMcpBundle",
    "DEFAULT_MCP_SECRET_KEY",
    "LEAN_MCP_SCHEMA_VERSION",
    "McpSecurityViolation",
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
    "load_protocol_data",
    "parse_and_validate",
]

__version__ = "0.1.0"

"""stepbot: compiler from simple protocol steps to Opentrons Python API v2."""

from stepbot.catalog import LIQUID_CLASS_CATALOG, LiquidClassSpec, normalize_liquid_class_name
from stepbot.checker import (
    CheckResult,
    PrimitiveAction,
    check_protocol,
    check_protocol_source,
    reduce_mock_log_actions,
    reduce_opentrons_runlog_actions,
)
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
from stepbot.models import (
    ProtocolSpec,
    RheologySpec,
    ValidationError,
    expand_for_volume_constraints,
    flat_transfer_strokes,
    plan_distribute_chunks,
    plan_transfer_strokes,
)
from stepbot.robot_http import HardwareRunAdapter, HardwareRunResult, OpentronsHttpRunAdapter

__all__ = [
    "CheckResult",
    "CompiledMcpBundle",
    "DEFAULT_MAX_CONSUMED_NONCES",
    "DEFAULT_MCP_SECRET_KEY",
    "HardwareRunAdapter",
    "HardwareRunResult",
    "LEAN_MCP_SCHEMA_VERSION",
    "LIQUID_CLASS_CATALOG",
    "LiquidClassSpec",
    "McpPersistentState",
    "McpSecurityViolation",
    "OpentronsHttpRunAdapter",
    "PrimitiveAction",
    "ProtocolSpec",
    "RheologySpec",
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
    "expand_for_volume_constraints",
    "flat_transfer_strokes",
    "from_twinstep",
    "load_mcp_state",
    "load_protocol_data",
    "mcp_state_lock",
    "normalize_liquid_class_name",
    "parse_and_validate",
    "plan_distribute_chunks",
    "plan_transfer_strokes",
    "reduce_mock_log_actions",
    "reduce_opentrons_runlog_actions",
    "save_mcp_state_atomic",
]

__version__ = "0.1.0"

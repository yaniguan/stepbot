"""Command-line interface for stepbot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from stepbot.checker import check_protocol
from stepbot.compiler import compile_file
from stepbot.mcp_runtime import (
    DEFAULT_MCP_SECRET_KEY,
    DEFAULT_MCP_TOOL_NAME,
    McpSecurityViolation,
    StepbotMcpExecutor,
    compile_with_mcp_envelope,
)
from stepbot.models import ValidationError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stepbot",
        description="Compile YAML/JSON liquid-handling protocols to Opentrons Python API v2.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    compile_cmd = sub.add_parser("compile", help="Compile a protocol YAML/JSON file to Python.")
    compile_cmd.add_argument("input", type=Path, help="Path to input YAML or JSON protocol.")
    compile_cmd.add_argument(
        "-o", "--output", type=Path, default=None, help="Output Python file path."
    )
    compile_cmd.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional output path for machine-readable JSON protocol manifest.",
    )

    check_cmd = sub.add_parser("check", help="Simulate and check a compiled protocol file.")
    check_cmd.add_argument("protocol", type=Path, help="Path to compiled Python protocol.")
    check_cmd.add_argument(
        "--mock", action="store_true", help="Force the built-in mock simulator."
    )
    check_cmd.add_argument(
        "-v", "--verbose", action="store_true", help="Print the full simulated run log."
    )

    mcp_compile_cmd = sub.add_parser(
        "mcp-compile",
        help="Compile a YAML/JSON protocol and mint a wire-compatible lean-mcp/v1 envelope.",
    )
    mcp_compile_cmd.add_argument("input", type=Path, help="Path to input YAML or JSON protocol.")
    mcp_compile_cmd.add_argument(
        "-o", "--output", type=Path, default=None, help="Output compiled Python file path."
    )
    mcp_compile_cmd.add_argument(
        "--envelope",
        type=Path,
        default=None,
        help="Output JSON file path for the minted lean-mcp/v1 capability envelope.",
    )
    mcp_compile_cmd.add_argument(
        "--epoch", type=int, default=1, help="Deck epoch token_epoch (default: 1)."
    )
    mcp_compile_cmd.add_argument(
        "--secret-key",
        type=str,
        default=None,
        help="Shared HMAC secret key (defaults to LEAN_MCP_SECRET_KEY or dev key).",
    )
    mcp_compile_cmd.add_argument(
        "--tool-name",
        type=str,
        default=DEFAULT_MCP_TOOL_NAME,
        help="Target MCP hardware tool identifier.",
    )
    mcp_compile_cmd.add_argument(
        "--nonce", type=str, default=None, help="Explicit single-use capability nonce."
    )
    mcp_compile_cmd.add_argument(
        "--frontier-exception",
        action="store_true",
        help="Mark call as an out-of-regime frontier exception.",
    )
    mcp_compile_cmd.add_argument(
        "--supervisor-signed",
        action="store_true",
        help="Include human supervisor co-signature flag.",
    )
    mcp_compile_cmd.add_argument(
        "--supervisor-id",
        type=str,
        default="",
        help="Human supervisor identifier when co-signed.",
    )

    mcp_dispatch_cmd = sub.add_parser(
        "mcp-dispatch",
        help="Verify a lean-mcp/v1 envelope and dispatch compiled Python protocol to simulator.",
    )
    mcp_dispatch_cmd.add_argument(
        "protocol", type=Path, help="Path to compiled Opentrons Python protocol."
    )
    mcp_dispatch_cmd.add_argument(
        "--envelope",
        type=Path,
        required=True,
        help="Path to lean-mcp/v1 capability envelope JSON file.",
    )
    mcp_dispatch_cmd.add_argument(
        "--epoch",
        type=int,
        default=1,
        help="Initial physical deck epoch when --state is not provided (default: 1).",
    )
    mcp_dispatch_cmd.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Optional JSON state file persisting current_deck_epoch and consumed_nonces.",
    )
    mcp_dispatch_cmd.add_argument(
        "--secret-key",
        type=str,
        default=None,
        help="Shared HMAC secret key (defaults to LEAN_MCP_SECRET_KEY or dev key).",
    )
    mcp_dispatch_cmd.add_argument(
        "--mock", action="store_true", help="Force the built-in safe AST mock simulator."
    )
    mcp_dispatch_cmd.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON dispatch receipt."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the stepbot CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "compile":
        try:
            source = compile_file(
                args.input,
                args.output,
                manifest_path=args.manifest,
            )
        except (ValidationError, OSError, UnicodeDecodeError) as exc:
            print(f"Validation error: {exc}", file=sys.stderr)
            return 1
        if args.output is None:
            sys.stdout.write(source)
        else:
            print(f"Wrote {args.output}")
        if args.manifest is not None and args.output is not None:
            print(f"Wrote manifest {args.manifest}")
        return 0

    if args.command == "check":
        result = check_protocol(args.protocol, use_mock=args.mock)
        if not result.ok:
            for err in result.errors:
                print(f"Error ({result.backend}): {err}", file=sys.stderr)
            return 1
        print(
            f"OK ({result.backend}): {result.command_count} commands, "
            f"{result.line_count} run log lines"
        )
        if args.verbose:
            for line in result.lines:
                print(line)
        return 0

    if args.command == "mcp-compile":
        secret = args.secret_key or os.environ.get("LEAN_MCP_SECRET_KEY", DEFAULT_MCP_SECRET_KEY)
        try:
            bundle = compile_with_mcp_envelope(
                args.input,
                deck_epoch=args.epoch,
                secret_key=secret,
                is_frontier_exception=bool(args.frontier_exception),
                human_supervisor_signed=bool(args.supervisor_signed),
                human_supervisor_id=str(args.supervisor_id),
                nonce=args.nonce,
                tool_name=str(args.tool_name),
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(bundle.python_code, encoding="utf-8", newline="\n")
            if args.envelope is not None:
                args.envelope.parent.mkdir(parents=True, exist_ok=True)
                args.envelope.write_text(
                    json.dumps(bundle.envelope, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
        except (ValidationError, ValueError, OSError, UnicodeDecodeError) as exc:
            print(f"MCP compile error: {exc}", file=sys.stderr)
            return 1

        if args.output is None and args.envelope is None:
            print(
                json.dumps(
                    {
                        "python_code": bundle.python_code,
                        "protocol_sha256": bundle.protocol_sha256,
                        "envelope": bundle.envelope,
                    },
                    indent=2,
                )
            )
        else:
            if args.output is not None:
                print(f"Wrote {args.output}")
            if args.envelope is not None:
                print(f"Wrote envelope {args.envelope}")
        return 0

    if args.command == "mcp-dispatch":
        secret = args.secret_key or os.environ.get("LEAN_MCP_SECRET_KEY", DEFAULT_MCP_SECRET_KEY)
        try:
            python_code = args.protocol.read_text(encoding="utf-8")
            raw_env = json.loads(args.envelope.read_text(encoding="utf-8"))
            if not isinstance(raw_env, dict):
                raise McpSecurityViolation("mcp_schema", "Envelope JSON root must be an object")

            deck_epoch: int = int(args.epoch)
            consumed: list[str] = []
            if args.state is not None and args.state.exists():
                state_data = json.loads(args.state.read_text(encoding="utf-8"))
                if isinstance(state_data, dict):
                    raw_ep = state_data.get("current_deck_epoch", deck_epoch)
                    if isinstance(raw_ep, int) and not isinstance(raw_ep, bool):
                        deck_epoch = raw_ep
                    raw_nonces = state_data.get("consumed_nonces", [])
                    if isinstance(raw_nonces, list):
                        consumed = [str(n) for n in raw_nonces]

            executor = StepbotMcpExecutor(
                secret_key=secret,
                current_deck_epoch=deck_epoch,
                consumed_nonces=consumed,
                use_mock=bool(args.mock),
            )
            receipt = executor.dispatch(python_code, raw_env, simulate=True)
            if args.state is not None:
                args.state.parent.mkdir(parents=True, exist_ok=True)
                args.state.write_text(
                    json.dumps(
                        {
                            "current_deck_epoch": executor.current_deck_epoch,
                            "consumed_nonces": sorted(executor.consumed_nonces),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
        except (McpSecurityViolation, json.JSONDecodeError, ValueError, OSError) as exc:
            if args.json:
                rule = exc.rule if isinstance(exc, McpSecurityViolation) else "mcp_io_or_parse"
                print(json.dumps({"ok": False, "rule": rule, "error": str(exc)}, indent=2))
            else:
                print(f"MCP security deny: {exc}", file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps(receipt, indent=2))
        else:
            print(
                f"DISPATCH OK ({receipt['backend']}): "
                f"epoch {receipt['executed_epoch']} -> {receipt['current_deck_epoch']}, "
                f"{receipt['command_count']} commands"
            )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

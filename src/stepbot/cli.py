"""Command-line interface for stepbot."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from stepbot.checker import check_protocol
from stepbot.compiler import compile_file
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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

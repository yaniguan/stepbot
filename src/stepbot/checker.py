"""Protocol simulation checker using Opentrons simulator or built-in safe AST mock."""

from __future__ import annotations

import ast
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stepbot.catalog import (
    DEFAULT_API_LEVEL,
    expected_tip_family_description,
    get_labware_spec,
    get_pipette_spec,
    installed_max_api_level,
    is_tip_rack_compatible,
    parse_api_level,
)
from stepbot.models import VOLUME_TOLERANCE, LabwareSpec, PipetteSpec

_API_LEVEL_RE = re.compile(r"^\d+\.\d+$")
_MIN_LIQUID_API_LEVEL = (2, 14)
_MAX_LOOP_ITERATIONS = 10_000


def _fmt_log_vol(volume: float) -> str:
    if abs(volume - round(volume, 1)) < 1e-6:
        return f"{volume:.1f}"
    return f"{volume:.4f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class CheckResult:
    """Summary of simulating a generated Opentrons protocol."""

    backend: str
    ok: bool
    command_count: int
    line_count: int
    lines: tuple[str, ...]
    errors: tuple[str, ...] = ()


class _MockTrash:
    def __repr__(self) -> str:
        return "Trash Bin on slot 12"


@dataclass(frozen=True)
class _MockLiquid:
    name: str
    description: str | None = None
    display_color: str | None = None


@dataclass(eq=False)
class _MockWell:
    labware_name: str
    slot: int
    well_name: str
    max_volume: float
    api_level: tuple[int, int] = (2, 16)
    has_tip: bool = False
    volume: float = field(default=0.0)

    def __str__(self) -> str:
        return f"{self.well_name} of {self.labware_name} on slot {self.slot}"

    def load_liquid(self, liquid: _MockLiquid, volume: float) -> None:
        if self.api_level < _MIN_LIQUID_API_LEVEL:
            raise RuntimeError(
                f"Well.load_liquid requires apiLevel 2.14 or newer, "
                f"got '{self.api_level[0]}.{self.api_level[1]}'"
            )
        if volume < 0 or volume > self.max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot load {volume} uL of '{liquid.name}' into {self}: "
                f"well capacity is {self.max_volume} uL"
            )
        self.volume = float(volume)

    def remove_volume(self, volume: float) -> None:
        if volume > self.volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot aspirate {volume} uL from {self}: well holds only {self.volume:.2f} uL"
            )
        self.volume = max(0.0, self.volume - volume)

    def add_volume(self, volume: float) -> None:
        if self.volume + volume > self.max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot dispense {volume} uL into {self}: would hold "
                f"{self.volume + volume:.2f} uL, above well capacity {self.max_volume} uL"
            )
        self.volume += volume


class _MockLabware:
    def __init__(
        self,
        load_name: str,
        slot: int,
        spec: LabwareSpec,
        *,
        api_level: tuple[int, int] = (2, 16),
    ) -> None:
        self.load_name = load_name
        self.slot = slot
        self.spec = spec
        self._wells = {
            w: _MockWell(
                load_name,
                slot,
                w,
                spec.max_volume_for(w),
                api_level=api_level,
                has_tip=spec.is_tiprack,
            )
            for w in spec.wells
        }

    def __getitem__(self, well_name: str) -> _MockWell:
        if well_name not in self._wells:
            raise KeyError(
                f"Well '{well_name}' not found in '{self.load_name}' on slot {self.slot}"
            )
        return self._wells[well_name]

    def wells(self) -> list[_MockWell]:
        return [self._wells[w] for w in self.spec.wells]


class _MockInstrumentContext:
    def __init__(
        self,
        model: str,
        mount: str,
        spec: PipetteSpec,
        tip_racks: list[_MockLabware],
        log: list[str],
        fixed_trash: _MockTrash,
    ) -> None:
        self.model = model
        self.mount = mount
        self.spec = spec
        self.tip_racks = tip_racks
        self._log = log
        self._fixed_trash = fixed_trash
        self.has_tip = False
        self.current_volume = 0.0
        self.tips_used = 0
        if tip_racks:
            self.effective_max_volume = min(
                [spec.max_volume] + [r.spec.max_volume for r in tip_racks]
            )
        else:
            self.effective_max_volume = spec.max_volume

    def _require_tip(self, action: str) -> None:
        if not self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) has no tip for {action}")

    def _check_vol(self, volume: float, action: str) -> None:
        if (
            volume < self.spec.min_volume - VOLUME_TOLERANCE
            or volume > self.effective_max_volume + VOLUME_TOLERANCE
        ):
            raise ValueError(
                f"{action} volume {volume} uL outside "
                f"[{self.spec.min_volume}, {self.effective_max_volume}] uL"
            )

    def pick_up_tip(self, location: _MockWell | None = None) -> None:
        if self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) already has a tip")
        target: _MockWell | None = None
        if location is not None:
            if not location.has_tip:
                raise RuntimeError(f"No tip available at {location}")
            target = location
        else:
            for rack in self.tip_racks:
                for well in rack.wells():
                    if well.has_tip:
                        target = well
                        break
                if target is not None:
                    break
        if target is None:
            raise RuntimeError(
                f"Out of tips for pipette '{self.model}' ({self.mount}) after {self.tips_used} tips"
            )
        target.has_tip = False
        self.tips_used += 1
        self.has_tip = True
        self.current_volume = 0.0
        self._log.append(f"Picking up tip from {target}")

    def drop_tip(self) -> None:
        self._require_tip("drop_tip")
        self.has_tip = False
        self.current_volume = 0.0
        self._log.append(f"Dropping tip into {self._fixed_trash}")

    def aspirate(self, volume: float, location: Any) -> None:
        self._require_tip("aspirate")
        self._check_vol(volume, "aspirate")
        if self.current_volume + volume > self.effective_max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot aspirate {volume} uL: exceeds max volume {self.effective_max_volume} uL"
            )
        if isinstance(location, _MockWell):
            location.remove_volume(volume)
        self.current_volume += volume
        self._log.append(f"Aspirating {_fmt_log_vol(volume)} uL from {location}")

    def dispense(self, volume: float, location: Any) -> None:
        self._require_tip("dispense")
        self._check_vol(volume, "dispense")
        if volume > self.current_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot dispense {volume} uL with {self.current_volume:.2f} uL aspirated"
            )
        if isinstance(location, _MockWell):
            location.add_volume(volume)
        self.current_volume = max(0.0, self.current_volume - volume)
        self._log.append(f"Dispensing {_fmt_log_vol(volume)} uL into {location}")

    def blow_out(self, location: Any = None) -> None:
        self._require_tip("blow_out")
        self.current_volume = 0.0
        target = location if location is not None else self._fixed_trash
        self._log.append(f"Blowing out into {target}")

    def mix(self, repetitions: int, volume: float, location: Any) -> None:
        self._require_tip("mix")
        self._check_vol(volume, "mix")
        if repetitions < 1:
            raise ValueError(f"mix repetitions must be >= 1, got {repetitions}")
        self._log.append(f"Mixing {repetitions} times with {_fmt_log_vol(volume)} uL at {location}")
        for _ in range(repetitions):
            self.aspirate(volume, location)
            self.dispense(volume, location)

    def transfer(
        self,
        volume: float,
        source: _MockWell | list[_MockWell],
        dest: _MockWell | list[_MockWell],
        *,
        new_tip: str = "once",
        mix_after: tuple[int, float] | None = None,
    ) -> None:
        if new_tip not in ("once", "always", "never"):
            raise ValueError(f"Invalid new_tip mode: {new_tip}")
        if volume <= 0:
            raise ValueError(f"transfer volume must be positive, got {volume}")
        strokes = max(1, math.ceil((volume - VOLUME_TOLERANCE) / self.effective_max_volume))
        stroke_vol = volume / strokes
        self._check_vol(stroke_vol, "transfer")

        srcs = list(source) if isinstance(source, (list, tuple)) else [source]
        dsts = list(dest) if isinstance(dest, (list, tuple)) else [dest]
        if len(srcs) == 1 and len(dsts) > 1:
            srcs = srcs * len(dsts)
        elif len(dsts) == 1 and len(srcs) > 1:
            dsts = dsts * len(srcs)
        elif len(srcs) != len(dsts):
            raise ValueError("Source and destination well counts do not match")

        if new_tip == "once":
            self.pick_up_tip()
        try:
            for src_well, dst_well in zip(srcs, dsts):
                if new_tip == "always":
                    self.pick_up_tip()
                for _ in range(strokes):
                    self.aspirate(stroke_vol, src_well)
                    self.dispense(stroke_vol, dst_well)
                if mix_after is not None:
                    reps, mvol = mix_after
                    self.mix(reps, mvol, dst_well)
                if new_tip == "always":
                    self.drop_tip()
        finally:
            if new_tip == "once" and self.has_tip:
                self.drop_tip()

    def distribute(
        self,
        volume: float,
        source: _MockWell,
        dest: list[_MockWell] | _MockWell,
        *,
        new_tip: str = "once",
        disposal_volume: float | None = None,
    ) -> None:
        if new_tip not in ("once", "never"):
            raise ValueError(f"distribute supports 'once' or 'never', got {new_tip}")
        dsts = list(dest) if isinstance(dest, (list, tuple)) else [dest]
        self._check_vol(volume, "distribute")
        disp_vol = self.spec.min_volume if disposal_volume is None else float(disposal_volume)
        if disp_vol < 0:
            raise ValueError(f"disposal_volume must be >= 0, got {disp_vol}")
        if volume + disp_vol > self.effective_max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"distribute volume {volume} uL + disposal_volume {disp_vol} uL "
                f"exceeds max volume {self.effective_max_volume} uL"
            )
        if new_tip == "once":
            self.pick_up_tip()
        try:
            idx = 0
            batch_count = max(
                1, int((self.effective_max_volume - disp_vol + VOLUME_TOLERANCE) // volume)
            )
            while idx < len(dsts):
                chunk = dsts[idx : idx + batch_count]
                self.aspirate(volume * len(chunk) + disp_vol, source)
                for dst_well in chunk:
                    self.dispense(volume, dst_well)
                self.blow_out(self._fixed_trash)
                idx += len(chunk)
        finally:
            if new_tip == "once" and self.has_tip:
                self.drop_tip()


class MockProtocolContext:
    """Minimal mock ProtocolContext used when opentrons is not installed."""

    def __init__(self, *, api_level: tuple[int, int] | None = None) -> None:
        self.api_level = parse_api_level(DEFAULT_API_LEVEL) if api_level is None else api_level
        self.log: list[str] = []
        self.fixed_trash = _MockTrash()
        self._slots: dict[int, _MockLabware] = {}
        self._mounts: dict[str, _MockInstrumentContext] = {}

    def load_labware(self, load_name: str, slot: int) -> _MockLabware:
        if isinstance(slot, bool) or not isinstance(slot, int) or not (1 <= slot <= 11):
            raise ValueError(f"Invalid deck slot: {slot}")
        if slot in self._slots:
            raise ValueError(f"Slot {slot} already occupied")
        spec = get_labware_spec(load_name)
        if spec is None:
            raise ValueError(f"Unknown labware: {load_name}")
        lw = _MockLabware(load_name, slot, spec, api_level=self.api_level)
        self._slots[slot] = lw
        return lw

    def load_instrument(
        self,
        model: str,
        mount: str,
        tip_racks: list[_MockLabware] | None = None,
    ) -> _MockInstrumentContext:
        if mount not in ("left", "right"):
            raise ValueError(f"Invalid mount: {mount}")
        if mount in self._mounts:
            raise ValueError(f"Mount {mount} already loaded")
        spec = get_pipette_spec(model)
        if spec is None:
            raise ValueError(f"Unknown pipette model: {model}")
        if spec.channels > 1:
            raise ValueError(f"Multichannel pipette '{model}' is not supported in mock checker")
        racks = list(tip_racks or [])
        for rack in racks:
            if not rack.spec.is_tiprack:
                raise ValueError(f"Labware '{rack.load_name}' is not a tip rack")
            if not is_tip_rack_compatible(spec, rack.spec):
                raise ValueError(
                    f"Tip rack '{rack.load_name}' ({rack.spec.max_volume:g} uL) is incompatible "
                    f"with pipette '{model}' (expected {expected_tip_family_description(spec)})"
                )
        inst = _MockInstrumentContext(
            model=model,
            mount=mount,
            spec=spec,
            tip_racks=racks,
            log=self.log,
            fixed_trash=self.fixed_trash,
        )
        self._mounts[mount] = inst
        return inst

    def define_liquid(
        self,
        name: str,
        description: str | None = None,
        display_color: str | None = None,
    ) -> _MockLiquid:
        if self.api_level < _MIN_LIQUID_API_LEVEL:
            raise RuntimeError(
                f"ProtocolContext.define_liquid requires apiLevel 2.14 or newer, "
                f"got '{self.api_level[0]}.{self.api_level[1]}'"
            )
        return _MockLiquid(name=name, description=description, display_color=display_color)

    def comment(self, msg: str) -> None:
        self.log.append(f"Comment: {msg}")

    def delay(self, seconds: float = 0, minutes: float = 0) -> None:
        if seconds < 0 or minutes < 0:
            raise ValueError("delay seconds and minutes must be >= 0")
        total = float(seconds) + 60.0 * float(minutes)
        self.log.append(f"Delaying for {total:.1f} seconds")

    def pause(self, msg: str = "") -> None:
        self.log.append(f"Pausing: {msg}" if msg else "Pausing")


_SAFE_OBJECT_TYPES = (
    MockProtocolContext,
    _MockLabware,
    _MockWell,
    _MockInstrumentContext,
    _MockTrash,
    _MockLiquid,
)


class _ReturnSignal(Exception):
    """Internal control-flow signal for bare `return` inside `def run(protocol):`."""


class _SafeProtocolInterpreter:
    """Restricted AST interpreter for Opentrons Python API v2 protocols (no exec/eval)."""

    def __init__(self, ctx: MockProtocolContext, param_name: str) -> None:
        self.env: dict[str, Any] = {param_name: ctx}

    def run_body(self, stmts: list[ast.stmt]) -> None:
        try:
            for stmt in stmts:
                self._exec_stmt(stmt)
        except _ReturnSignal:
            return

    def _bind_target(self, target: ast.expr, value: Any) -> None:
        if isinstance(target, ast.Name):
            if target.id.startswith("_"):
                raise ValueError(f"Disallowed private/dunder identifier '{target.id}'")
            self.env[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(value, (tuple, list)) or len(value) != len(target.elts):
                raise ValueError("Cannot unpack non-matching sequence in assignment")
            for elt, item in zip(target.elts, value):
                self._bind_target(elt, item)
            return
        raise ValueError(
            f"Unsupported assignment target '{type(target).__name__}' at line {target.lineno}"
        )

    def _exec_stmt(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Pass):
            return
        if isinstance(stmt, ast.Return):
            if stmt.value is not None and not (
                isinstance(stmt.value, ast.Constant) and stmt.value.value is None
            ):
                raise ValueError(f"run() must not return a value (line {stmt.lineno})")
            raise _ReturnSignal()
        if isinstance(stmt, ast.Expr):
            self._eval_expr(stmt.value)
            return
        if isinstance(stmt, ast.Assign):
            val = self._eval_expr(stmt.value)
            for target in stmt.targets:
                self._bind_target(target, val)
            return
        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                val = self._eval_expr(stmt.value)
                self._bind_target(stmt.target, val)
            return
        if isinstance(stmt, ast.For):
            if stmt.orelse:
                raise ValueError(f"'for ... else' is not supported (line {stmt.lineno})")
            iterable = self._eval_expr(stmt.iter)
            if not isinstance(iterable, (list, tuple)):
                raise ValueError(
                    f"for-loop iterable must be a list or tuple (line {stmt.lineno})"
                )
            if len(iterable) > _MAX_LOOP_ITERATIONS:
                raise ValueError(f"for-loop exceeds {_MAX_LOOP_ITERATIONS} iterations")
            for item in iterable:
                self._bind_target(stmt.target, item)
                for inner in stmt.body:
                    self._exec_stmt(inner)
            return
        raise ValueError(
            f"Unsupported or unsafe statement '{type(stmt).__name__}' in mock check "
            f"(line {stmt.lineno})"
        )

    def _eval_expr(self, expr: ast.expr) -> Any:
        if isinstance(expr, ast.Constant):
            if isinstance(expr.value, (str, int, float, bool)) or expr.value is None:
                return expr.value
            raise ValueError(f"Unsupported constant type at line {expr.lineno}")

        if isinstance(expr, ast.Name):
            if expr.id.startswith("_"):
                raise ValueError(
                    f"Disallowed private/dunder name '{expr.id}' at line {expr.lineno}"
                )
            if expr.id not in self.env:
                raise ValueError(f"Undefined or disallowed name '{expr.id}' at line {expr.lineno}")
            return self.env[expr.id]

        if isinstance(expr, ast.List):
            return [self._eval_expr(e) for e in expr.elts]

        if isinstance(expr, ast.Tuple):
            return tuple(self._eval_expr(e) for e in expr.elts)

        if isinstance(expr, ast.Dict):
            out: dict[Any, Any] = {}
            for k_node, v_node in zip(expr.keys, expr.values):
                if k_node is None:
                    raise ValueError(f"Dict unpacking is not supported (line {expr.lineno})")
                out[self._eval_expr(k_node)] = self._eval_expr(v_node)
            return out

        if isinstance(expr, ast.UnaryOp):
            operand = self._eval_expr(expr.operand)
            if isinstance(operand, bool) or not isinstance(operand, (int, float)):
                raise ValueError(f"Unary operator requires a number (line {expr.lineno})")
            if isinstance(expr.op, ast.UAdd):
                return +operand
            if isinstance(expr.op, ast.USub):
                return -operand
            raise ValueError(f"Unsupported unary operator at line {expr.lineno}")

        if isinstance(expr, ast.BinOp):
            left = self._eval_expr(expr.left)
            right = self._eval_expr(expr.right)
            if (
                isinstance(left, bool)
                or isinstance(right, bool)
                or not isinstance(left, (int, float))
                or not isinstance(right, (int, float))
            ):
                raise ValueError(
                    f"Binary arithmetic is allowed on numbers only (line {expr.lineno})"
                )
            if isinstance(expr.op, ast.Add):
                return left + right
            if isinstance(expr.op, ast.Sub):
                return left - right
            if isinstance(expr.op, ast.Mult):
                return left * right
            if isinstance(expr.op, ast.Div):
                return left / right
            if isinstance(expr.op, ast.FloorDiv):
                return left // right
            raise ValueError(f"Unsupported binary operator at line {expr.lineno}")

        if isinstance(expr, ast.Subscript):
            container = self._eval_expr(expr.value)
            if not isinstance(container, (_MockLabware, dict, list, tuple)):
                raise ValueError(
                    f"Subscript not allowed on '{type(container).__name__}' (line {expr.lineno})"
                )
            if isinstance(expr.slice, ast.Slice):
                if not isinstance(container, (list, tuple)):
                    raise ValueError(
                        f"Slice subscript is only allowed on lists or tuples (line {expr.lineno})"
                    )
                lower = self._eval_expr(expr.slice.lower) if expr.slice.lower else None
                upper = self._eval_expr(expr.slice.upper) if expr.slice.upper else None
                step = self._eval_expr(expr.slice.step) if expr.slice.step else None
                return container[slice(lower, upper, step)]
            key = self._eval_expr(expr.slice)
            if isinstance(container, _MockLabware):
                if not isinstance(key, str):
                    raise ValueError(f"Labware well index must be a string (line {expr.lineno})")
                return container[key]
            if isinstance(container, (list, tuple)):
                if isinstance(key, bool) or not isinstance(key, int):
                    raise ValueError(f"Sequence index must be an integer (line {expr.lineno})")
                return container[key]
            return container[key]

        if isinstance(expr, ast.Attribute):
            if expr.attr.startswith("_"):
                raise ValueError(
                    f"Disallowed private/dunder attribute '{expr.attr}' at line {expr.lineno}"
                )
            owner = self._eval_expr(expr.value)
            if not isinstance(owner, _SAFE_OBJECT_TYPES):
                raise ValueError(
                    f"Attribute access '{expr.attr}' not allowed on '{type(owner).__name__}' "
                    f"(line {expr.lineno})"
                )
            if not hasattr(owner, expr.attr):
                raise AttributeError(
                    f"'{type(owner).__name__}' has no attribute '{expr.attr}'"
                )
            return getattr(owner, expr.attr)

        if isinstance(expr, ast.Call):
            if not isinstance(expr.func, ast.Attribute):
                func_desc = (
                    expr.func.id if isinstance(expr.func, ast.Name) else type(expr.func).__name__
                )
                raise ValueError(
                    f"Disallowed direct function call '{func_desc}' in mock check "
                    f"(line {expr.lineno})"
                )
            callee = self._eval_expr(expr.func)
            self_obj = getattr(callee, "__self__", None)
            if not callable(callee) or not isinstance(self_obj, _SAFE_OBJECT_TYPES):
                raise ValueError(
                    f"Call target is not a bound Opentrons protocol method (line {expr.lineno})"
                )
            pos_args: list[Any] = []
            for arg in expr.args:
                if isinstance(arg, ast.Starred):
                    starred_val = self._eval_expr(arg.value)
                    if not isinstance(starred_val, (list, tuple)):
                        raise ValueError(f"Starred argument must be a sequence (line {expr.lineno})")
                    pos_args.extend(starred_val)
                else:
                    pos_args.append(self._eval_expr(arg))
            kw_args: dict[str, Any] = {}
            for kw in expr.keywords:
                if kw.arg is None or kw.arg.startswith("_"):
                    raise ValueError(f"Unsupported keyword argument at line {expr.lineno}")
                kw_args[kw.arg] = self._eval_expr(kw.value)
            return callee(*pos_args, **kw_args)

        raise ValueError(
            f"Unsupported or unsafe expression '{type(expr).__name__}' in mock check "
            f"(line {expr.lineno})"
        )


def _validate_api_level_format(api_level: Any) -> tuple[tuple[int, int] | None, str | None]:
    """Validate that api_level is a canonical '2.<minor>' string and return ((2, minor), err)."""
    if not isinstance(api_level, str):
        return None, (
            f"Protocol metadata 'apiLevel' must be a string such as '{DEFAULT_API_LEVEL}', "
            f"got {api_level!r}"
        )
    stripped = api_level.strip()
    if not _API_LEVEL_RE.match(stripped):
        return None, f"Protocol apiLevel '{api_level}' is not of the form '<major>.<minor>'"
    major, minor = parse_api_level(stripped)
    if f"{major}.{minor}" != stripped:
        return None, f"Protocol apiLevel '{api_level}' must not contain leading zeros or spaces"
    if major != 2:
        return None, (
            f"Protocol apiLevel '{api_level}' is not supported: only Opentrons Python API v2 (2.x) is supported"
        )
    return (major, minor), None


def _read_api_level(source: str) -> Any:
    """Return metadata['apiLevel'] from protocol source without executing it."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "metadata" for t in node.targets)
        ):
            try:
                metadata = ast.literal_eval(node.value)
            except ValueError:
                return None
            if isinstance(metadata, dict) and "apiLevel" in metadata:
                return metadata["apiLevel"]
    return None


def api_level_error(api_level: Any, max_supported: tuple[int, int]) -> str | None:
    """Return an error message if api_level is malformed or newer than max_supported."""
    if api_level is None:
        return "Protocol metadata has no string 'apiLevel'"
    requested, fmt_err = _validate_api_level_format(api_level)
    if fmt_err is not None or requested is None:
        return fmt_err
    if requested > max_supported:
        return (
            f"Protocol apiLevel '{api_level}' is newer than the installed opentrons supports "
            f"(max {max_supported[0]}.{max_supported[1]}); upgrade opentrons or set an older "
            "metadata.apiLevel in the YAML/JSON input and recompile"
        )
    return None


def _parse_safe_protocol_ast(source: str, filename: str) -> tuple[dict[str, Any], ast.FunctionDef]:
    tree = ast.parse(source, filename=filename)
    metadata: dict[str, Any] | None = None
    run_def: ast.FunctionDef | None = None

    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name
        ):
            name = node.targets[0].id
            if name in ("metadata", "requirements"):
                try:
                    lit = ast.literal_eval(node.value)
                except ValueError as exc:
                    raise ValueError(f"'{name}' must be a constant literal dictionary") from exc
                if not isinstance(lit, dict):
                    raise ValueError(f"'{name}' must be a dictionary")
                if name == "metadata":
                    metadata = lit
                continue
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            if node.decorator_list:
                raise ValueError("Decorators on run() are not supported in mock check")
            if len(node.args.args) != 1 or node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                raise ValueError("run(protocol) must accept exactly one positional parameter")
            run_def = node
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError(
                f"Import statements are disallowed in mock check (line {node.lineno})"
            )
        raise ValueError(
            f"Unsupported top-level statement '{type(node).__name__}' in mock check "
            f"(line {node.lineno})"
        )

    if metadata is None or "apiLevel" not in metadata:
        raise ValueError("Protocol missing metadata['apiLevel']")
    if run_def is None:
        raise ValueError("Protocol missing callable run(protocol)")
    return metadata, run_def


def _check_with_opentrons(path: Path) -> CheckResult | None:
    try:
        from opentrons import simulate as ot_simulate
    except ImportError:
        return None
    try:
        raw_source = path.read_text(encoding="utf-8")
        max_supported = installed_max_api_level()
        if max_supported is not None:
            error = api_level_error(_read_api_level(raw_source), max_supported)
            if error is not None:
                return CheckResult(
                    backend="opentrons",
                    ok=False,
                    command_count=0,
                    line_count=0,
                    lines=(),
                    errors=(error,),
                )
        with path.open("r", encoding="utf-8") as handle:
            runlog, _ = ot_simulate.simulate(handle, file_name=str(path))
        formatted = ot_simulate.format_runlog(runlog)
        lines = tuple(formatted.splitlines())
        return CheckResult(
            backend="opentrons",
            ok=True,
            command_count=len(runlog),
            line_count=len(lines),
            lines=lines,
        )
    except Exception as exc:
        return CheckResult(
            backend="opentrons",
            ok=False,
            command_count=0,
            line_count=0,
            lines=(),
            errors=(str(exc),),
        )


def _check_source_with_mock(source: str, filename: str = "<protocol>") -> CheckResult:
    try:
        metadata, run_def = _parse_safe_protocol_ast(source, filename)
        parsed_level, fmt_err = _validate_api_level_format(metadata.get("apiLevel"))
        if fmt_err is not None or parsed_level is None:
            raise ValueError(fmt_err or "Invalid metadata['apiLevel']")
        ctx = MockProtocolContext(api_level=parsed_level)
        param_name = run_def.args.args[0].arg
        interpreter = _SafeProtocolInterpreter(ctx, param_name)
        interpreter.run_body(run_def.body)
        lines = tuple(ctx.log)
        return CheckResult(
            backend="mock",
            ok=True,
            command_count=len(lines),
            line_count=len(lines),
            lines=lines,
        )
    except Exception as exc:
        return CheckResult(
            backend="mock",
            ok=False,
            command_count=0,
            line_count=0,
            lines=(),
            errors=(str(exc),),
        )


def _check_with_mock(path: Path) -> CheckResult:
    try:
        source = path.read_text(encoding="utf-8")
    except Exception as exc:
        return CheckResult(
            backend="mock",
            ok=False,
            command_count=0,
            line_count=0,
            lines=(),
            errors=(str(exc),),
        )
    return _check_source_with_mock(source, str(path))


def check_protocol_source(source: str, *, use_mock: bool = False) -> CheckResult:
    """Simulate an in-memory compiled Opentrons Python API v2 protocol string."""
    if not use_mock:
        with tempfile.TemporaryDirectory(prefix="stepbot_mcp_") as tmp_dir:
            tmp_file = Path(tmp_dir) / "protocol.py"
            tmp_file.write_text(source, encoding="utf-8", newline="\n")
            real_result = _check_with_opentrons(tmp_file)
            if real_result is not None:
                return real_result
    return _check_source_with_mock(source)


def check_protocol(path: Path | str, *, use_mock: bool = False) -> CheckResult:
    """Simulate a compiled Opentrons protocol file and return a CheckResult."""
    proto_path = Path(path)
    if not proto_path.exists():
        return CheckResult(
            backend="mock" if use_mock else "opentrons",
            ok=False,
            command_count=0,
            line_count=0,
            lines=(),
            errors=(f"Protocol file not found: {proto_path}",),
        )
    if proto_path.is_dir():
        return CheckResult(
            backend="mock" if use_mock else "opentrons",
            ok=False,
            command_count=0,
            line_count=0,
            lines=(),
            errors=(f"Protocol path is a directory, not a file: {proto_path}",),
        )
    if not use_mock:
        real_result = _check_with_opentrons(proto_path)
        if real_result is not None:
            return real_result
    return _check_with_mock(proto_path)

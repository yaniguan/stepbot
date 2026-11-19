"""Protocol simulation checker using Opentrons simulator or built-in mock."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stepbot.catalog import (
    get_labware_spec,
    get_pipette_spec,
    installed_max_api_level,
    parse_api_level,
)
from stepbot.models import LabwareSpec, PipetteSpec


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


_VOLUME_TOLERANCE = 1e-6


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
    volume: float = field(default=0.0)

    def __str__(self) -> str:
        return f"{self.well_name} of {self.labware_name} on slot {self.slot}"

    def load_liquid(self, liquid: _MockLiquid, volume: float) -> None:
        if volume < 0 or volume > self.max_volume + _VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot load {volume} uL of '{liquid.name}' into {self}: "
                f"well capacity is {self.max_volume} uL"
            )
        self.volume = float(volume)

    def remove_volume(self, volume: float) -> None:
        if volume > self.volume + _VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot aspirate {volume} uL from {self}: well holds only {self.volume:.2f} uL"
            )
        self.volume = max(0.0, self.volume - volume)

    def add_volume(self, volume: float) -> None:
        if self.volume + volume > self.max_volume + _VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot dispense {volume} uL into {self}: would hold "
                f"{self.volume + volume:.2f} uL, above well capacity {self.max_volume} uL"
            )
        self.volume += volume


class _MockLabware:
    def __init__(self, load_name: str, slot: int, spec: LabwareSpec) -> None:
        self.load_name = load_name
        self.slot = slot
        self.spec = spec
        self._wells = {
            w: _MockWell(load_name, slot, w, spec.max_volume) for w in spec.wells
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
        self._tip_pool: list[_MockWell] = [
            well for rack in tip_racks for well in rack.wells()
        ]
        self._tip_idx = 0

    def _require_tip(self, action: str) -> None:
        if not self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) has no tip for {action}")

    def _check_vol(self, volume: float, action: str) -> None:
        if volume < self.spec.min_volume or volume > self.spec.max_volume:
            raise ValueError(
                f"{action} volume {volume} uL outside [{self.spec.min_volume}, {self.spec.max_volume}] uL"
            )

    def pick_up_tip(self) -> None:
        if self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) already has a tip")
        if self._tip_idx >= len(self._tip_pool):
            raise RuntimeError(
                f"Out of tips for pipette '{self.model}' ({self.mount}) after {self.tips_used} tips"
            )
        well = self._tip_pool[self._tip_idx]
        self._tip_idx += 1
        self.tips_used += 1
        self.has_tip = True
        self.current_volume = 0.0
        self._log.append(f"Picking up tip from {well}")

    def drop_tip(self) -> None:
        self._require_tip("drop_tip")
        self.has_tip = False
        self.current_volume = 0.0
        self._log.append(f"Dropping tip into {self._fixed_trash}")

    def aspirate(self, volume: float, location: Any) -> None:
        self._require_tip("aspirate")
        self._check_vol(volume, "aspirate")
        if self.current_volume + volume > self.spec.max_volume + 1e-9:
            raise ValueError(
                f"Cannot aspirate {volume} uL: exceeds max volume {self.spec.max_volume} uL"
            )
        if isinstance(location, _MockWell):
            location.remove_volume(volume)
        self.current_volume += volume
        self._log.append(f"Aspirating {volume:.1f} uL from {location}")

    def dispense(self, volume: float, location: Any) -> None:
        self._require_tip("dispense")
        if volume <= 0 or volume > self.current_volume + 1e-9:
            raise ValueError(
                f"Cannot dispense {volume} uL with {self.current_volume:.2f} uL aspirated"
            )
        if isinstance(location, _MockWell):
            location.add_volume(volume)
        self.current_volume = max(0.0, self.current_volume - volume)
        self._log.append(f"Dispensing {volume:.1f} uL into {location}")

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
        self._log.append(f"Mixing {repetitions} times with {volume:.1f} uL at {location}")
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
                self.aspirate(volume, src_well)
                self.dispense(volume, dst_well)
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
    ) -> None:
        if new_tip not in ("once", "never"):
            raise ValueError(f"distribute supports 'once' or 'never', got {new_tip}")
        dsts = list(dest) if isinstance(dest, (list, tuple)) else [dest]
        self._check_vol(volume, "distribute")
        if new_tip == "once":
            self.pick_up_tip()
        idx = 0
        while idx < len(dsts):
            batch_count = max(1, int(self.spec.max_volume // volume))
            chunk = dsts[idx : idx + batch_count]
            self.aspirate(volume * len(chunk), source)
            for dst_well in chunk:
                self.dispense(volume, dst_well)
            idx += len(chunk)
        self.blow_out(self._fixed_trash)
        if new_tip == "once":
            self.drop_tip()


class MockProtocolContext:
    """Minimal mock ProtocolContext used when opentrons is not installed."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.fixed_trash = _MockTrash()
        self._slots: dict[int, _MockLabware] = {}
        self._mounts: dict[str, _MockInstrumentContext] = {}

    def load_labware(self, load_name: str, slot: int) -> _MockLabware:
        if not (1 <= slot <= 11):
            raise ValueError(f"Invalid deck slot: {slot}")
        if slot in self._slots:
            raise ValueError(f"Slot {slot} already occupied")
        spec = get_labware_spec(load_name)
        if spec is None:
            raise ValueError(f"Unknown labware: {load_name}")
        lw = _MockLabware(load_name, slot, spec)
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
        racks = list(tip_racks or [])
        for rack in racks:
            if not rack.spec.is_tiprack:
                raise ValueError(f"Labware '{rack.load_name}' is not a tip rack")
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
        return _MockLiquid(name=name, description=description, display_color=display_color)

    def comment(self, msg: str) -> None:
        self.log.append(f"Comment: {msg}")

    def delay(self, seconds: float = 0, minutes: float = 0) -> None:
        total = float(seconds) + 60.0 * float(minutes)
        self.log.append(f"Delaying for {total:.1f} seconds")

    def pause(self, msg: str = "") -> None:
        self.log.append(f"Pausing: {msg}" if msg else "Pausing")


def _read_api_level(source: str) -> str | None:
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
            if isinstance(metadata, dict):
                level = metadata.get("apiLevel")
                return level if isinstance(level, str) else None
    return None


def api_level_error(api_level: str | None, max_supported: tuple[int, int]) -> str | None:
    """Return an error message if api_level is malformed or newer than max_supported."""
    if api_level is None:
        return "Protocol metadata has no string 'apiLevel'"
    try:
        requested = parse_api_level(api_level)
    except ValueError:
        return f"Protocol apiLevel '{api_level}' is not of the form '<major>.<minor>'"
    if requested > max_supported:
        return (
            f"Protocol apiLevel '{api_level}' is newer than the installed opentrons supports "
            f"(max {max_supported[0]}.{max_supported[1]}); upgrade opentrons or set an older "
            "metadata.apiLevel in the YAML/JSON input and recompile"
        )
    return None


def _check_with_opentrons(path: Path) -> CheckResult | None:
    try:
        from opentrons import simulate as ot_simulate
    except ImportError:
        return None
    max_supported = installed_max_api_level()
    if max_supported is not None:
        error = api_level_error(_read_api_level(path.read_text(encoding="utf-8")), max_supported)
        if error is not None:
            return CheckResult(
                backend="opentrons",
                ok=False,
                command_count=0,
                line_count=0,
                lines=(),
                errors=(error,),
            )
    try:
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


def _check_with_mock(path: Path) -> CheckResult:
    source = path.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    try:
        exec(compile(source, str(path), "exec"), namespace)
        metadata = namespace.get("metadata")
        if not isinstance(metadata, dict) or "apiLevel" not in metadata:
            raise ValueError("Protocol missing metadata['apiLevel']")
        run_fn = namespace.get("run")
        if not callable(run_fn):
            raise ValueError("Protocol missing callable run(protocol)")
        ctx = MockProtocolContext()
        run_fn(ctx)
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
    if not use_mock:
        real_result = _check_with_opentrons(proto_path)
        if real_result is not None:
            return real_result
    return _check_with_mock(proto_path)

"""Protocol simulation checker using Opentrons simulator or built-in safe AST mock."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import tempfile
from typing import Any

from stepbot.catalog import (
    DEFAULT_API_LEVEL,
    DEFAULT_FLEX_TRASH_SLOT,
    FLEX_STAGING_SLOTS,
    deck_slot_sort_key,
    expected_tip_family_description,
    get_labware_spec,
    get_pipette_spec,
    installed_max_api_level,
    is_flex_pipette,
    is_tip_rack_compatible,
    is_valid_flex_slot,
    parse_api_level,
)
from stepbot.models import (
    VOLUME_TOLERANCE,
    DeckSlot,
    LabwareSpec,
    PipetteSpec,
    plan_distribute_chunks,
    plan_transfer_strokes,
)

_API_LEVEL_RE = re.compile(r"^\d+\.\d+$")
_MIN_LIQUID_API_LEVEL = (2, 14)
_MIN_FLEX_API_LEVEL = (2, 15)
_MAX_LOOP_ITERATIONS = 10_000


def _require_finite_num(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return out


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
    api_level: str | None = None
    robot_type: str = "OT-2"
    loaded_slots: tuple[DeckSlot, ...] = ()
    max_op_volume_ul: float = 0.0
    well_volumes_ul: dict[str, float] = field(default_factory=dict)
    tips_used_per_mount: dict[str, int] = field(default_factory=dict)


class _MockTrash:
    def __init__(self, slot: DeckSlot = 12) -> None:
        self.slot = slot

    def __repr__(self) -> str:
        return f"Trash Bin on slot {self.slot}"


@dataclass(frozen=True)
class _MockLiquid:
    name: str
    description: str | None = None
    display_color: str | None = None


@dataclass(frozen=True)
class _MockLocation:
    well: _MockWell
    anchor: str
    offset_mm: float | None = None

    def __str__(self) -> str:
        return str(self.well)


class _MockFlowRate:
    def __init__(
        self,
        aspirate: float = 92.86,
        dispense: float = 92.86,
        blow_out: float = 92.86,
    ) -> None:
        self.aspirate = aspirate
        self.dispense = dispense
        self.blow_out = blow_out


@dataclass(eq=False)
class _MockWell:
    labware_name: str
    slot: DeckSlot
    well_name: str
    max_volume: float
    api_level: tuple[int, int] = (2, 16)
    has_tip: bool = False
    touch_tip_disabled: bool = False
    volume: float = field(default=0.0)
    _touched: bool = field(default=False)

    def __str__(self) -> str:
        return f"{self.well_name} of {self.labware_name} on slot {self.slot}"

    def load_liquid(self, liquid: _MockLiquid, volume: float) -> None:
        if self.api_level < _MIN_LIQUID_API_LEVEL:
            raise RuntimeError(
                f"Well.load_liquid requires apiLevel 2.14 or newer, "
                f"got '{self.api_level[0]}.{self.api_level[1]}'"
            )
        vol = _require_finite_num(volume, "Liquid volume")
        if vol < 0 or vol > self.max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot load {vol} uL of '{liquid.name}' into {self}: "
                f"well capacity is {self.max_volume} uL"
            )
        self.volume = vol
        self._touched = True

    def top(self, z: float | None = None) -> _MockLocation:
        z_val = _require_finite_num(z, "Well.top(z) offset") if z is not None else None
        return _MockLocation(self, "top", z_val)

    def bottom(self, z: float | None = None) -> _MockLocation:
        z_val = _require_finite_num(z, "Well.bottom(z) offset") if z is not None else None
        return _MockLocation(self, "bottom", z_val)

    def _remove_volume(self, volume: float) -> None:
        vol = _require_finite_num(volume, "Aspirate volume")
        if vol < 0:
            raise ValueError(f"Aspirate volume must be a non-negative number, got {volume!r}")
        if vol > self.volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot aspirate {vol} uL from {self}: well holds only {self.volume:.2f} uL"
            )
        self.volume = max(0.0, self.volume - vol)
        self._touched = True

    def _add_volume(self, volume: float) -> None:
        vol = _require_finite_num(volume, "Dispense volume")
        if vol < 0:
            raise ValueError(f"Dispense volume must be a non-negative number, got {volume!r}")
        if self.volume + vol > self.max_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot dispense {vol} uL into {self}: would hold "
                f"{self.volume + vol:.2f} uL, above well capacity {self.max_volume} uL"
            )
        self.volume += vol
        self._touched = True


class _MockLabware:
    def __init__(
        self,
        load_name: str,
        slot: DeckSlot,
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
                touch_tip_disabled=spec.touch_tip_disabled,
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


def _unwrap_well_target(location: Any, action: str) -> _MockWell:
    if isinstance(location, _MockWell):
        return location
    if isinstance(location, _MockLocation):
        return location.well
    raise ValueError(
        f"{action} target must be a labware well (got '{type(location).__name__}')"
    )


class _MockInstrumentContext:
    def __init__(
        self,
        model: str,
        mount: str,
        spec: PipetteSpec,
        tip_racks: list[_MockLabware],
        log: list[str],
        ctx: MockProtocolContext,
    ) -> None:
        self.model = model
        self.mount = mount
        self.spec = spec
        self.tip_racks = tip_racks
        self._log = log
        self._ctx = ctx
        self.flow_rate = _MockFlowRate(
            spec.nominal_aspirate_rate,
            spec.nominal_dispense_rate,
            spec.nominal_blow_out_rate,
        )
        self.has_tip = False
        self._mounted_tip_max_volume: float | None = None
        self._last_well: _MockWell | None = None
        self.current_volume = 0.0
        self._air_gap_volume = 0.0
        self.tips_used = 0
        self._in_composite_op = False
        self.max_op_volume_ul = 0.0
        if tip_racks:
            self.effective_max_volume = min(
                [spec.max_volume] + [r.spec.max_volume for r in tip_racks]
            )
        else:
            self.effective_max_volume = spec.max_volume

    def _current_working_volume(self) -> float:
        if self._mounted_tip_max_volume is not None:
            return min(self.spec.max_volume, self._mounted_tip_max_volume)
        return self.effective_max_volume

    def _peek_next_tip_max_volume(self) -> float | None:
        channels = self.spec.channels
        if channels == 1:
            for rack in self.tip_racks:
                for well in rack.wells():
                    if well.has_tip:
                        return well.max_volume
            return None
        for rack in self.tip_racks:
            max_col = max(c for _, c in rack.spec.well_grid.values())
            for col in range(max_col + 1):
                entries = sorted(
                    (r, w_name)
                    for w_name, (r, c) in rack.spec.well_grid.items()
                    if c == col
                )
                if len(entries) == channels:
                    cand = [rack[w_name] for _, w_name in entries]
                    if all(w.has_tip for w in cand):
                        return cand[0].max_volume
        return None

    def _active_working_volume(self, new_tip: str) -> float:
        if new_tip == "never":
            return self._current_working_volume()
        peek = self._peek_next_tip_max_volume()
        if peek is not None:
            return min(self.spec.max_volume, peek)
        return self.effective_max_volume

    def _require_tip(self, action: str) -> None:
        if not self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) has no tip for {action}")

    def _default_trash(self) -> _MockTrash:
        return self._ctx._require_default_trash()

    def _check_vol(self, volume: float, action: str, *, max_cap: float | None = None) -> None:
        vol = _require_finite_num(volume, f"{action} volume")
        cap = self._current_working_volume() if max_cap is None else max_cap
        if (
            vol < self.spec.min_volume - VOLUME_TOLERANCE
            or vol > cap + VOLUME_TOLERANCE
        ):
            raise ValueError(
                f"{action} volume {vol} uL outside "
                f"[{self.spec.min_volume}, {cap}] uL"
            )

    def _resolve_well_channels(
        self, well: _MockWell, action: str
    ) -> tuple[list[_MockWell], int]:
        if isinstance(well.slot, str) and well.slot in FLEX_STAGING_SLOTS:
            raise ValueError(
                f"{action} cannot access labware on Flex staging slot '{well.slot}'"
            )
        channels = self.spec.channels
        if channels == 1:
            return [well], 1
        lw = self._ctx._slots.get(well.slot)
        if lw is None:
            raise ValueError(f"{action} target well is not on an active deck labware")
        row, col = lw.spec.well_grid[well.well_name]
        if row != 0:
            raise ValueError(
                f"{action} with {channels}-channel pipette must target row 'A' column heads, "
                f"got '{well.well_name}'"
            )
        col_entries = sorted(
            (r, w_name) for w_name, (r, c) in lw.spec.well_grid.items() if c == col
        )
        if len(col_entries) == 1:
            return [well], channels
        if len(col_entries) == channels and [r for r, _ in col_entries] == list(range(channels)):
            return [lw[w_name] for _, w_name in col_entries], 1
        raise ValueError(
            f"{action} with {channels}-channel pipette cannot address {len(col_entries)}-row "
            f"labware '{lw.load_name}'"
        )

    def pick_up_tip(self, location: _MockWell | _MockLocation | None = None) -> None:
        if self.has_tip:
            raise RuntimeError(f"Pipette '{self.model}' ({self.mount}) already has a tip")
        loc_well: _MockWell | None = None
        if location is not None:
            if isinstance(location, _MockLocation):
                loc_well = location.well
            elif isinstance(location, _MockWell):
                loc_well = location
            else:
                raise RuntimeError(f"No tip available at {location}")
        channels = self.spec.channels
        if channels == 1:
            target: _MockWell | None = None
            if loc_well is not None:
                if not loc_well.has_tip:
                    raise RuntimeError(f"No tip available at {loc_well}")
                target = loc_well
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
            self._mounted_tip_max_volume = target.max_volume
            self.current_volume = 0.0
            self._air_gap_volume = 0.0
            self._log.append(f"Picking up tip from {target}")
            return

        # 8-channel column pickup across rows A..H
        col_wells: list[_MockWell] | None = None
        if loc_well is not None:
            resolved, mult = self._resolve_well_channels(loc_well, "pick_up_tip")
            if mult != 1 or len(resolved) != channels or not all(w.has_tip for w in resolved):
                raise RuntimeError(f"No full {channels}-tip column available at {loc_well}")
            col_wells = resolved
        else:
            for rack in self.tip_racks:
                max_col = max(c for _, c in rack.spec.well_grid.values())
                for col in range(max_col + 1):
                    entries = sorted(
                        (r, w_name)
                        for w_name, (r, c) in rack.spec.well_grid.items()
                        if c == col
                    )
                    if len(entries) == channels:
                        cand = [rack[w_name] for _, w_name in entries]
                        if all(w.has_tip for w in cand):
                            col_wells = cand
                            break
                if col_wells is not None:
                    break
        if col_wells is None:
            raise RuntimeError(
                f"Out of tips for pipette '{self.model}' ({self.mount}) after {self.tips_used} tips"
            )
        for w in col_wells:
            w.has_tip = False
        self.tips_used += channels
        self.has_tip = True
        self._mounted_tip_max_volume = col_wells[0].max_volume
        self.current_volume = 0.0
        self._air_gap_volume = 0.0
        self._log.append(f"Picking up tip from {col_wells[0]}")

    def drop_tip(self) -> None:
        self._require_tip("drop_tip")
        trash = self._default_trash()
        self.has_tip = False
        self._mounted_tip_max_volume = None
        self.current_volume = 0.0
        self._air_gap_volume = 0.0
        self._log.append(f"Dropping tip into {trash}")

    def aspirate(self, volume: float, location: Any) -> None:
        self._require_tip("aspirate")
        self._check_vol(volume, "aspirate")
        vol = float(volume)
        cap = self._current_working_volume()
        if self.current_volume + vol > cap + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot aspirate {volume} uL: exceeds max volume {cap} uL"
            )
        well = _unwrap_well_target(location, "aspirate")
        if well.has_tip or (
            well.slot in self._ctx._slots and self._ctx._slots[well.slot].spec.is_tiprack
        ):
            raise ValueError(f"Cannot aspirate liquid from tip rack well {well}")
        affected, mult = self._resolve_well_channels(well, "aspirate")
        for w in affected:
            w._remove_volume(vol * mult)
        if not self._in_composite_op:
            self.max_op_volume_ul = max(self.max_op_volume_ul, vol)
        self.current_volume += vol
        self._last_well = well
        self._log.append(f"Aspirating {_fmt_log_vol(vol)} uL from {location}")

    def air_gap(self, volume: float = 0.0, height: float | None = None) -> None:
        self._require_tip("air_gap")
        vol = _require_finite_num(volume, "air_gap volume")
        if vol < 0:
            raise ValueError(f"air_gap volume must be a non-negative number, got {volume!r}")
        if height is not None:
            _require_finite_num(height, "air_gap height")
        cap = self._current_working_volume()
        if self.current_volume + vol > cap + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot air_gap {volume} uL: exceeds max volume {cap} uL"
            )
        self.current_volume += vol
        self._air_gap_volume += vol
        self._log.append(f"Air gap {_fmt_log_vol(vol)} uL")

    def dispense(self, volume: float, location: Any) -> None:
        self._require_tip("dispense")
        self._check_vol(volume, "dispense")
        vol = float(volume)
        if vol > self.current_volume + VOLUME_TOLERANCE:
            raise ValueError(
                f"Cannot dispense {volume} uL with {self.current_volume:.2f} uL aspirated"
            )
        air_expelled = min(self._air_gap_volume, vol)
        liquid_vol = max(0.0, vol - air_expelled)
        if isinstance(location, _MockTrash):
            self._last_well = None
        elif isinstance(location, (_MockWell, _MockLocation)):
            well = _unwrap_well_target(location, "dispense")
            if well.slot in self._ctx._slots and self._ctx._slots[well.slot].spec.is_tiprack:
                raise ValueError(f"Cannot dispense liquid into tip rack well {well}")
            affected, mult = self._resolve_well_channels(well, "dispense")
            for w in affected:
                w._add_volume(liquid_vol * mult)
            self._last_well = well
        else:
            raise ValueError(
                f"dispense target must be a well or trash container (got '{type(location).__name__}')"
            )
        if not self._in_composite_op:
            self.max_op_volume_ul = max(self.max_op_volume_ul, liquid_vol)
        self._air_gap_volume = max(0.0, self._air_gap_volume - air_expelled)
        self.current_volume = max(0.0, self.current_volume - vol)
        self._log.append(f"Dispensing {_fmt_log_vol(vol)} uL into {location}")

    def blow_out(self, location: Any = None) -> None:
        self._require_tip("blow_out")
        if location is None:
            target: Any = self._default_trash()
        elif isinstance(location, (_MockWell, _MockLocation, _MockTrash)):
            target = location
        else:
            raise ValueError(
                f"blow_out target must be a well, location, or trash (got '{type(location).__name__}')"
            )
        self.current_volume = 0.0
        self._air_gap_volume = 0.0
        self._log.append(f"Blowing out into {target}")

    def touch_tip(
        self,
        location: _MockWell | _MockLocation | None = None,
        *,
        radius: float = 1.0,
        v_offset: float = -1.0,
        speed: float = 60.0,
    ) -> None:
        self._require_tip("touch_tip")
        target_well: _MockWell | None
        if location is None:
            target_well = self._last_well
        elif isinstance(location, _MockWell):
            target_well = location
        elif isinstance(location, _MockLocation):
            target_well = location.well
        else:
            raise ValueError(
                f"touch_tip location must be a well or None (got '{type(location).__name__}')"
            )
        for val, name in ((radius, "radius"), (v_offset, "v_offset"), (speed, "speed")):
            _require_finite_num(val, f"touch_tip {name}")
        if target_well is not None and target_well.touch_tip_disabled:
            return
        self._log.append("Touching tip")

    def mix(self, repetitions: int, volume: float, location: Any) -> None:
        self._require_tip("mix")
        self._check_vol(volume, "mix")
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
            raise ValueError(f"mix repetitions must be >= 1, got {repetitions}")
        _unwrap_well_target(location, "mix")
        vol = float(volume)
        outer = not self._in_composite_op
        if outer:
            self.max_op_volume_ul = max(self.max_op_volume_ul, vol)
            self._in_composite_op = True
        try:
            self._log.append(
                f"Mixing {repetitions} times with {_fmt_log_vol(vol)} uL at {location}"
            )
            for _ in range(repetitions):
                self.aspirate(vol, location)
                self.dispense(vol, location)
        finally:
            if outer:
                self._in_composite_op = False

    def transfer(
        self,
        volume: float,
        source: _MockWell | _MockLocation | list[_MockWell | _MockLocation],
        dest: _MockWell | _MockLocation | list[_MockWell | _MockLocation],
        *,
        new_tip: str = "once",
        mix_after: tuple[int, float] | None = None,
    ) -> None:
        if new_tip not in ("once", "always", "never"):
            raise ValueError(f"Invalid new_tip mode: {new_tip}")
        vol = _require_finite_num(volume, "transfer volume")
        if vol <= 0:
            raise ValueError(f"transfer volume must be positive, got {volume}")

        eff_max = self._active_working_volume(new_tip)
        stroke_groups = plan_transfer_strokes(vol, self.spec.max_volume, eff_max)
        for grp in stroke_groups:
            for s_vol in grp:
                self._check_vol(s_vol, "transfer", max_cap=eff_max)

        srcs = list(source) if isinstance(source, (list, tuple)) else [source]
        dsts = list(dest) if isinstance(dest, (list, tuple)) else [dest]
        if len(srcs) == 1 and len(dsts) > 1:
            srcs = srcs * len(dsts)
        elif len(dsts) == 1 and len(srcs) > 1:
            dsts = dsts * len(srcs)
        elif len(srcs) != len(dsts):
            raise ValueError("Source and destination well counts do not match")

        self.max_op_volume_ul = max(self.max_op_volume_ul, vol)
        if mix_after is not None:
            mvol_checked = _require_finite_num(mix_after[1], "mix_after volume")
            self.max_op_volume_ul = max(self.max_op_volume_ul, mvol_checked)

        prev_composite = self._in_composite_op
        self._in_composite_op = True
        if new_tip == "once":
            self.pick_up_tip()
        try:
            for src_well, dst_well in zip(srcs, dsts):
                for stroke_group in stroke_groups:
                    if new_tip == "always":
                        self.pick_up_tip()
                    for stroke_vol in stroke_group:
                        self.aspirate(stroke_vol, src_well)
                        self.dispense(stroke_vol, dst_well)
                        if mix_after is not None:
                            reps, mvol = mix_after
                            self.mix(reps, mvol, dst_well)
                    if new_tip == "always":
                        self.drop_tip()
        finally:
            try:
                if new_tip == "once" and self.has_tip:
                    self.drop_tip()
            finally:
                self._in_composite_op = prev_composite

    def distribute(
        self,
        volume: float,
        source: _MockWell | _MockLocation,
        dest: list[_MockWell | _MockLocation] | _MockWell | _MockLocation,
        *,
        new_tip: str = "once",
        disposal_volume: float | None = None,
    ) -> None:
        if new_tip not in ("once", "never"):
            raise ValueError(f"distribute supports 'once' or 'never', got {new_tip}")
        dsts = list(dest) if isinstance(dest, (list, tuple)) else [dest]
        eff_max = self._active_working_volume(new_tip)
        self._check_vol(volume, "distribute", max_cap=eff_max)
        vol = float(volume)
        disp_vol = (
            self.spec.min_volume
            if disposal_volume is None
            else _require_finite_num(disposal_volume, "distribute disposal_volume")
        )
        if disp_vol < 0:
            raise ValueError(f"disposal_volume must be >= 0, got {disp_vol}")
        if vol + disp_vol > eff_max + VOLUME_TOLERANCE:
            raise ValueError(
                f"distribute volume {volume} uL + disposal_volume {disp_vol} uL "
                f"exceeds max volume {eff_max} uL"
            )
        trash = self._default_trash()
        self.max_op_volume_ul = max(self.max_op_volume_ul, vol)
        prev_composite = self._in_composite_op
        self._in_composite_op = True
        if new_tip == "once":
            self.pick_up_tip()
        try:
            idx = 0
            chunks = plan_distribute_chunks(len(dsts), vol, eff_max, disp_vol)
            for chunk_len in chunks:
                chunk = dsts[idx : idx + chunk_len]
                self.aspirate(vol * len(chunk) + disp_vol, source)
                for dst_well in chunk:
                    self.dispense(vol, dst_well)
                if disp_vol > 0.0:
                    self.blow_out(trash)
                idx += chunk_len
        finally:
            try:
                if new_tip == "once" and self.has_tip:
                    self.drop_tip()
            finally:
                self._in_composite_op = prev_composite


class MockProtocolContext:
    """Minimal mock ProtocolContext used when opentrons is not installed."""

    def __init__(
        self,
        *,
        api_level: tuple[int, int] | None = None,
        robot_type: str = "OT-2",
    ) -> None:
        self.api_level = parse_api_level(DEFAULT_API_LEVEL) if api_level is None else api_level
        self.robot_type = robot_type
        self.log: list[str] = []
        self._ot2_trash: _MockTrash | None = _MockTrash(12) if robot_type == "OT-2" else None
        self._trash_bins: dict[DeckSlot, _MockTrash] = {}
        self._slots: dict[DeckSlot, _MockLabware] = {}
        self._slot_aliases: dict[DeckSlot, str] = {}
        self._mounts: dict[str, _MockInstrumentContext] = {}

    @property
    def fixed_trash(self) -> _MockTrash:
        if self.robot_type == "Flex":
            raise RuntimeError(
                "protocol.fixed_trash is not supported on Opentrons Flex; "
                f"call protocol.load_trash_bin({DEFAULT_FLEX_TRASH_SLOT!r})"
            )
        assert self._ot2_trash is not None
        return self._ot2_trash

    def _require_default_trash(self) -> _MockTrash:
        if self._ot2_trash is not None:
            return self._ot2_trash
        if self._trash_bins:
            first_slot = next(iter(self._trash_bins))
            return self._trash_bins[first_slot]
        raise RuntimeError(
            "No trash container configured on Opentrons Flex; "
            f"call protocol.load_trash_bin({DEFAULT_FLEX_TRASH_SLOT!r})"
        )

    def load_trash_bin(self, location: str = DEFAULT_FLEX_TRASH_SLOT) -> _MockTrash:
        if self.robot_type != "Flex" and self.api_level < (2, 16):
            raise RuntimeError("protocol.load_trash_bin requires apiLevel 2.16 or newer")
        if self.robot_type == "Flex":
            if not is_valid_flex_slot(location, allow_staging=False):
                raise ValueError(
                    f"Invalid Flex trash bin slot {location!r}: expected 'A1'..'D3'"
                )
            slot: DeckSlot = location
        else:
            if isinstance(location, bool) or not isinstance(location, int) or not (1 <= location <= 12):
                raise ValueError(f"Invalid OT-2 trash bin slot: {location!r}")
            slot = location
        if slot in self._slots or slot in self._trash_bins:
            raise ValueError(f"Slot {slot} already occupied")
        tb = _MockTrash(slot)
        self._trash_bins[slot] = tb
        return tb

    def load_labware(self, load_name: str, slot: DeckSlot) -> _MockLabware:
        if self.robot_type == "Flex":
            if not is_valid_flex_slot(slot, allow_staging=True):
                raise ValueError(
                    f"Invalid Flex deck slot {slot!r}: expected 'A1'..'D3' (or 'A4'..'D4')"
                )
        else:
            if isinstance(slot, bool) or not isinstance(slot, int) or not (1 <= slot <= 11):
                raise ValueError(f"Invalid deck slot: {slot}")
        if slot in self._slots or slot in self._trash_bins:
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
        if spec.channels not in (1, 8):
            raise ValueError(
                f"Unsupported pipette channel count {spec.channels} on '{model}'"
            )
        if self.robot_type == "Flex":
            if not is_flex_pipette(model):
                raise ValueError(
                    f"OT-2 pipette '{model}' is not supported on Opentrons Flex"
                )
        else:
            if is_flex_pipette(model):
                raise ValueError(
                    f"Opentrons Flex pipette '{model}' requires requirements['robotType'] == 'Flex'"
                )
        racks = list(tip_racks or [])
        for rack in racks:
            if not isinstance(rack, _MockLabware) or not rack.spec.is_tiprack:
                raise ValueError(f"Labware '{getattr(rack, 'load_name', rack)}' is not a tip rack")
            if isinstance(rack.slot, str) and rack.slot in FLEX_STAGING_SLOTS:
                raise ValueError(
                    f"Tip rack '{rack.load_name}' cannot be accessed in Flex staging slot '{rack.slot}'"
                )
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
            ctx=self,
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
        sec_val = _require_finite_num(seconds, "delay seconds")
        min_val = _require_finite_num(minutes, "delay minutes")
        if sec_val < 0 or min_val < 0:
            raise ValueError("delay seconds and minutes must be >= 0")
        total = sec_val + 60.0 * min_val
        self.log.append(f"Delaying for {total:.1f} seconds")

    def pause(self, msg: str = "") -> None:
        self.log.append(f"Pausing: {msg}" if msg else "Pausing")


_SAFE_OBJECT_TYPES = (
    MockProtocolContext,
    _MockLabware,
    _MockWell,
    _MockLocation,
    _MockFlowRate,
    _MockInstrumentContext,
    _MockTrash,
    _MockLiquid,
)

_ALLOWED_ATTRS: dict[type[Any], frozenset[str]] = {
    MockProtocolContext: frozenset(
        {
            "fixed_trash",
            "load_labware",
            "load_trash_bin",
            "load_instrument",
            "define_liquid",
            "comment",
            "delay",
            "pause",
        }
    ),
    _MockLabware: frozenset({"wells"}),
    _MockWell: frozenset({"load_liquid", "top", "bottom"}),
    _MockLocation: frozenset(),
    _MockFlowRate: frozenset({"aspirate", "dispense", "blow_out"}),
    _MockInstrumentContext: frozenset(
        {
            "flow_rate",
            "pick_up_tip",
            "drop_tip",
            "aspirate",
            "air_gap",
            "dispense",
            "blow_out",
            "touch_tip",
            "mix",
            "transfer",
            "distribute",
        }
    ),
    _MockTrash: frozenset(),
    _MockLiquid: frozenset(),
}

_ALL_ALLOWED_ATTR_NAMES: frozenset[str] = frozenset().union(*_ALLOWED_ATTRS.values())
_WRITABLE_FLOW_RATE_ATTRS: frozenset[str] = frozenset({"aspirate", "dispense", "blow_out"})


class _ReturnSignal(Exception):
    """Internal control-flow signal for bare `return` inside `def run(protocol):`."""


def _validate_safe_ast_stmt(stmt: ast.stmt) -> None:
    """Statically verify an AST statement inside `run(protocol)` before execution."""
    if isinstance(stmt, ast.Pass):
        return
    if isinstance(stmt, ast.Return):
        if stmt.value is not None and not (
            isinstance(stmt.value, ast.Constant) and stmt.value.value is None
        ):
            raise ValueError(f"run() must not return a value (line {stmt.lineno})")
        return
    if isinstance(stmt, ast.Expr):
        _validate_safe_ast_expr(stmt.value)
        return
    if isinstance(stmt, ast.Assign):
        _validate_safe_ast_expr(stmt.value)
        for target in stmt.targets:
            _validate_safe_ast_target(target)
        return
    if isinstance(stmt, ast.AnnAssign):
        if stmt.value is not None:
            _validate_safe_ast_expr(stmt.value)
            _validate_safe_ast_target(stmt.target)
        return
    if isinstance(stmt, ast.For):
        if stmt.orelse:
            raise ValueError(f"'for ... else' is not supported (line {stmt.lineno})")
        _validate_safe_ast_target(stmt.target, allow_attr=False)
        _validate_safe_ast_expr(stmt.iter)
        for inner in stmt.body:
            _validate_safe_ast_stmt(inner)
        return
    raise ValueError(
        f"Unsupported or unsafe statement '{type(stmt).__name__}' in protocol check "
        f"(line {stmt.lineno})"
    )


def _validate_safe_ast_target(target: ast.expr, *, allow_attr: bool = True) -> None:
    if isinstance(target, ast.Name):
        if target.id.startswith("_"):
            raise ValueError(f"Disallowed private/dunder identifier '{target.id}'")
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _validate_safe_ast_target(elt, allow_attr=False)
        return
    if allow_attr and isinstance(target, ast.Attribute):
        if target.attr.startswith("_") or target.attr not in _WRITABLE_FLOW_RATE_ATTRS:
            raise ValueError(
                f"Unsupported attribute assignment '.{target.attr}' at line {target.lineno}"
            )
        _validate_safe_ast_expr(target.value)
        return
    raise ValueError(
        f"Unsupported assignment target '{type(target).__name__}' at line {target.lineno}"
    )


def _validate_safe_ast_expr(expr: ast.expr) -> None:
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, (str, int, float, bool)) or expr.value is None:
            return
        raise ValueError(f"Unsupported constant type at line {expr.lineno}")
    if isinstance(expr, ast.Name):
        if expr.id.startswith("_"):
            raise ValueError(
                f"Disallowed private/dunder name '{expr.id}' at line {expr.lineno}"
            )
        return
    if isinstance(expr, (ast.List, ast.Tuple)):
        for elt in expr.elts:
            _validate_safe_ast_expr(elt)
        return
    if isinstance(expr, ast.Dict):
        for k_node, v_node in zip(expr.keys, expr.values):
            if k_node is None:
                raise ValueError(f"Dict unpacking is not supported (line {expr.lineno})")
            _validate_safe_ast_expr(k_node)
            _validate_safe_ast_expr(v_node)
        return
    if isinstance(expr, ast.UnaryOp):
        if not isinstance(expr.op, (ast.UAdd, ast.USub)):
            raise ValueError(f"Unsupported unary operator at line {expr.lineno}")
        _validate_safe_ast_expr(expr.operand)
        return
    if isinstance(expr, ast.BinOp):
        if not isinstance(expr.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)):
            raise ValueError(f"Unsupported binary operator at line {expr.lineno}")
        _validate_safe_ast_expr(expr.left)
        _validate_safe_ast_expr(expr.right)
        return
    if isinstance(expr, ast.Subscript):
        _validate_safe_ast_expr(expr.value)
        if isinstance(expr.slice, ast.Slice):
            if expr.slice.lower is not None:
                _validate_safe_ast_expr(expr.slice.lower)
            if expr.slice.upper is not None:
                _validate_safe_ast_expr(expr.slice.upper)
            if expr.slice.step is not None:
                _validate_safe_ast_expr(expr.slice.step)
        else:
            _validate_safe_ast_expr(expr.slice)
        return
    if isinstance(expr, ast.Attribute):
        if expr.attr.startswith("_"):
            raise ValueError(
                f"Disallowed private/dunder attribute '{expr.attr}' at line {expr.lineno}"
            )
        if expr.attr not in _ALL_ALLOWED_ATTR_NAMES:
            raise ValueError(
                f"Disallowed or unknown Opentrons attribute '{expr.attr}' at line {expr.lineno}"
            )
        _validate_safe_ast_expr(expr.value)
        return
    if isinstance(expr, ast.Call):
        if not isinstance(expr.func, ast.Attribute):
            func_desc = (
                expr.func.id if isinstance(expr.func, ast.Name) else type(expr.func).__name__
            )
            raise ValueError(
                f"Disallowed direct function call '{func_desc}' in protocol check "
                f"(line {expr.lineno})"
            )
        _validate_safe_ast_expr(expr.func)
        for arg in expr.args:
            if isinstance(arg, ast.Starred):
                _validate_safe_ast_expr(arg.value)
            else:
                _validate_safe_ast_expr(arg)
        for kw in expr.keywords:
            if kw.arg is None or kw.arg.startswith("_"):
                raise ValueError(f"Unsupported keyword argument at line {expr.lineno}")
            _validate_safe_ast_expr(kw.value)
        return
    raise ValueError(
        f"Unsupported or unsafe expression '{type(expr).__name__}' in protocol check "
        f"(line {expr.lineno})"
    )


class _SafeProtocolInterpreter:
    """Restricted AST interpreter for Opentrons Python API v2 protocols (no exec/eval)."""

    def __init__(self, ctx: MockProtocolContext, param_name: str) -> None:
        self.ctx = ctx
        self.env: dict[str, Any] = {param_name: ctx}

    def run_body(self, stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            _validate_safe_ast_stmt(stmt)
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
            if isinstance(value, _MockLabware) and value.slot not in self.ctx._slot_aliases:
                self.ctx._slot_aliases[value.slot] = target.id
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(value, (tuple, list)) or len(value) != len(target.elts):
                raise ValueError("Cannot unpack non-matching sequence in assignment")
            for elt, item in zip(target.elts, value):
                self._bind_target(elt, item)
            return
        if isinstance(target, ast.Attribute):
            owner = self._eval_expr(target.value)
            if isinstance(owner, _MockFlowRate) and target.attr in _WRITABLE_FLOW_RATE_ATTRS:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0.0
                ):
                    raise ValueError(
                        f"flow_rate.{target.attr} must be a positive finite number, got {value!r}"
                    )
                setattr(owner, target.attr, float(value))
                return
            raise ValueError(
                f"Attribute assignment '.{target.attr}' not allowed on '{type(owner).__name__}' "
                f"at line {target.lineno}"
            )
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
            allowed = _ALLOWED_ATTRS.get(type(owner), frozenset())
            if expr.attr not in allowed:
                raise ValueError(
                    f"Attribute or method '{expr.attr}' is not allowed on '{type(owner).__name__}' "
                    f"(line {expr.lineno})"
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
            allowed_methods = _ALLOWED_ATTRS.get(type(self_obj), frozenset())
            if expr.func.attr not in allowed_methods:
                raise ValueError(
                    f"Method '{expr.func.attr}' is not allowed on '{type(self_obj).__name__}' "
                    f"(line {expr.lineno})"
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
    """Return apiLevel from top-level `requirements` or `metadata` without executing `source`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    meta_level: Any = None
    req_level: Any = None
    has_meta = False
    has_req = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("metadata", "requirements"):
                    try:
                        lit = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    if isinstance(lit, dict) and "apiLevel" in lit:
                        if t.id == "requirements":
                            has_req = True
                            req_level = lit["apiLevel"]
                        else:
                            has_meta = True
                            meta_level = lit["apiLevel"]
    if has_req:
        return req_level
    if has_meta:
        return meta_level
    return None


def api_level_error(api_level: Any, max_supported: tuple[int, int]) -> str | None:
    """Return an error message if api_level is malformed or newer than max_supported."""
    if api_level is None:
        return "Protocol metadata/requirements has no string 'apiLevel'"
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


def _resolve_ast_robot_type(requirements: dict[str, Any] | None) -> str:
    if requirements is None or "robotType" not in requirements:
        return "OT-2"
    raw = requirements["robotType"]
    if raw in ("OT-2", "OT2"):
        return "OT-2"
    if raw in ("Flex", "OT-3"):
        return "Flex"
    raise ValueError(
        f"Unsupported requirements['robotType'] {raw!r}: expected 'OT-2' or 'Flex'"
    )


def _parse_safe_protocol_ast(
    source: str, filename: str
) -> tuple[dict[str, Any], ast.FunctionDef]:
    tree = ast.parse(source, filename=filename)
    metadata: dict[str, Any] | None = None
    requirements: dict[str, Any] | None = None
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
                else:
                    requirements = lit
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
                f"Import statements are disallowed in protocol check (line {node.lineno})"
            )
        raise ValueError(
            f"Unsupported top-level statement '{type(node).__name__}' in protocol check "
            f"(line {node.lineno})"
        )

    has_meta_api = metadata is not None and "apiLevel" in metadata
    has_req_api = requirements is not None and "apiLevel" in requirements
    if has_meta_api and has_req_api:
        raise ValueError("Protocol cannot specify 'apiLevel' in both metadata and requirements")
    if not has_meta_api and not has_req_api:
        raise ValueError("Protocol missing metadata['apiLevel'] or requirements['apiLevel']")
    if run_def is None:
        raise ValueError("Protocol missing callable run(protocol)")

    for stmt in run_def.body:
        _validate_safe_ast_stmt(stmt)

    robot_type = _resolve_ast_robot_type(requirements)
    eff_meta: dict[str, Any] = dict(metadata or {})
    if has_req_api and requirements is not None:
        eff_meta["apiLevel"] = requirements["apiLevel"]
    eff_meta["_resolved_robot_type"] = robot_type
    return eff_meta, run_def


def _extract_mock_telemetry(
    ctx: MockProtocolContext,
) -> tuple[tuple[DeckSlot, ...], float, dict[str, float], dict[str, int]]:
    loaded_slots = tuple(sorted(ctx._slots.keys(), key=deck_slot_sort_key))
    max_op = 0.0
    tips_by_mount: dict[str, int] = {}
    for mount, inst in sorted(ctx._mounts.items()):
        max_op = max(max_op, inst.max_op_volume_ul)
        tips_by_mount[mount] = inst.tips_used

    well_volumes: dict[str, float] = {}
    for slot in loaded_slots:
        lw = ctx._slots[slot]
        if lw.spec.is_tiprack:
            continue
        alias = ctx._slot_aliases.get(slot, f"slot_{slot}")
        for well in lw.wells():
            if well._touched or well.volume > 0.0:
                well_volumes[f"{alias}:{well.well_name}"] = round(well.volume, 4)
    return loaded_slots, round(max_op, 4), well_volumes, tips_by_mount


def _check_with_opentrons(path: Path) -> CheckResult | None:
    try:
        from opentrons import simulate as ot_simulate
    except ImportError:
        return None
    try:
        raw_source = path.read_text(encoding="utf-8")
        # Security pre-gate (#29): enforce safe AST parse & statement allowlist BEFORE opentrons.simulate
        eff_meta, _ = _parse_safe_protocol_ast(raw_source, str(path))
        raw_api_level = eff_meta.get("apiLevel")
        robot_type = str(eff_meta.get("_resolved_robot_type", "OT-2"))

        max_supported = installed_max_api_level()
        if max_supported is not None:
            error = api_level_error(raw_api_level, max_supported)
            if error is not None:
                return CheckResult(
                    backend="opentrons",
                    ok=False,
                    command_count=0,
                    line_count=0,
                    lines=(),
                    errors=(error,),
                    api_level=raw_api_level if isinstance(raw_api_level, str) else None,
                    robot_type=robot_type,
                )

        # Also extract deterministic slot/volume/tip telemetry via safe AST mock
        mock_probe = _check_source_with_mock(raw_source, str(path))
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
            api_level=mock_probe.api_level or (str(raw_api_level) if isinstance(raw_api_level, str) else None),
            robot_type=robot_type,
            loaded_slots=mock_probe.loaded_slots,
            max_op_volume_ul=mock_probe.max_op_volume_ul,
            well_volumes_ul=mock_probe.well_volumes_ul,
            tips_used_per_mount=mock_probe.tips_used_per_mount,
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
        eff_meta, run_def = _parse_safe_protocol_ast(source, filename)
        raw_level = eff_meta.get("apiLevel")
        robot_type = str(eff_meta.get("_resolved_robot_type", "OT-2"))
        parsed_level, fmt_err = _validate_api_level_format(raw_level)
        if fmt_err is not None or parsed_level is None:
            raise ValueError(fmt_err or "Invalid metadata['apiLevel']")
        if robot_type == "Flex" and parsed_level < _MIN_FLEX_API_LEVEL:
            raise ValueError(
                f"Opentrons Flex requires apiLevel {_MIN_FLEX_API_LEVEL[0]}.{_MIN_FLEX_API_LEVEL[1]} "
                f"or newer, got '{raw_level}'"
            )
        ctx = MockProtocolContext(api_level=parsed_level, robot_type=robot_type)
        param_name = run_def.args.args[0].arg
        interpreter = _SafeProtocolInterpreter(ctx, param_name)
        interpreter.run_body(run_def.body)
        lines = tuple(ctx.log)
        loaded_slots, max_op, well_volumes, tips_by_mount = _extract_mock_telemetry(ctx)
        return CheckResult(
            backend="mock",
            ok=True,
            command_count=len(lines),
            line_count=len(lines),
            lines=lines,
            api_level=f"{parsed_level[0]}.{parsed_level[1]}",
            robot_type=robot_type,
            loaded_slots=loaded_slots,
            max_op_volume_ul=max_op,
            well_volumes_ul=well_volumes,
            tips_used_per_mount=tips_by_mount,
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


PrimitiveAction = tuple[str, float | None, str | None, str | None]

_PICK_UP_RE = re.compile(r"^Picking up tip from ([A-Z]+\d+) of .+ on slot ([A-Za-z0-9]+)$")
_ASPIRATE_RE = re.compile(
    r"^Aspirating ([0-9.]+) uL from ([A-Z]+\d+) of .+ on slot ([A-Za-z0-9]+)(?: at .+)?$"
)
_AIR_GAP_MOCK_RE = re.compile(r"^Air gap ([0-9.]+) uL$")
_AIR_GAP_OT_RE = re.compile(r"^Air gap of ([0-9.]+) uL$")
_DISPENSE_WELL_RE = re.compile(
    r"^Dispensing ([0-9.]+) uL into ([A-Z]+\d+) of .+ on slot ([A-Za-z0-9]+)(?: at .+)?$"
)
_DISPENSE_TRASH_RE = re.compile(
    r"^Dispensing ([0-9.]+) uL into Trash Bin on slot ([A-Za-z0-9]+)(?: at .+)?$"
)
_BLOW_OUT_WELL_RE = re.compile(
    r"^Blowing out into ([A-Z]+\d+) of .+ on slot ([A-Za-z0-9]+)$"
)
_BLOW_OUT_TRASH_RE = re.compile(r"^Blowing out into Trash Bin on slot ([A-Za-z0-9]+)$")
_DROP_TIP_TRASH_RE = re.compile(r"^Dropping tip into Trash Bin on slot ([A-Za-z0-9]+)$")
_DROP_TIP_WELL_RE = re.compile(r"^Dropping tip into ([A-Z]+\d+) of .+ on slot ([A-Za-z0-9]+)$")
_DELAY_MOCK_RE = re.compile(r"^Delaying for ([0-9.]+) seconds$")
_DELAY_OT_RE = re.compile(r"^Delaying for (\d+) minutes and ([0-9.]+) seconds$")


def _parse_primitive_line(text: str) -> PrimitiveAction | None:
    stripped = text.strip()
    m = _PICK_UP_RE.match(stripped)
    if m:
        return ("pick_up_tip", None, m.group(1), m.group(2))
    m = _ASPIRATE_RE.match(stripped)
    if m:
        return ("aspirate", round(float(m.group(1)), 4), m.group(2), m.group(3))
    m = _AIR_GAP_MOCK_RE.match(stripped) or _AIR_GAP_OT_RE.match(stripped)
    if m:
        return ("air_gap", round(float(m.group(1)), 4), None, None)
    m = _DISPENSE_WELL_RE.match(stripped)
    if m:
        return ("dispense", round(float(m.group(1)), 4), m.group(2), m.group(3))
    m = _DISPENSE_TRASH_RE.match(stripped)
    if m:
        return ("dispense", round(float(m.group(1)), 4), "TRASH", m.group(2))
    m = _BLOW_OUT_TRASH_RE.match(stripped)
    if m:
        return ("blow_out", None, "TRASH", m.group(1))
    m = _BLOW_OUT_WELL_RE.match(stripped)
    if m:
        return ("blow_out", None, m.group(1), m.group(2))
    if stripped.startswith("Touching tip"):
        return ("touch_tip", None, None, None)
    m = _DROP_TIP_TRASH_RE.match(stripped)
    if m:
        return ("drop_tip", None, "TRASH", m.group(1))
    m = _DROP_TIP_WELL_RE.match(stripped)
    if m:
        return ("drop_tip", None, m.group(1), m.group(2))
    m = _DELAY_MOCK_RE.match(stripped)
    if m:
        return ("delay", round(float(m.group(1)), 4), None, None)
    m = _DELAY_OT_RE.match(stripped)
    if m:
        total_s = 60.0 * float(m.group(1)) + float(m.group(2))
        return ("delay", round(total_s, 4), None, None)
    return None


def reduce_mock_log_actions(lines: Sequence[str]) -> list[PrimitiveAction]:
    """Reduce `MockProtocolContext` run-log lines to `(action, volume, well, slot)` tuples."""
    actions: list[PrimitiveAction] = []
    for line in lines:
        parsed = _parse_primitive_line(line)
        if parsed is not None:
            actions.append(parsed)
    return actions


def reduce_opentrons_runlog_actions(runlog: Sequence[Mapping[str, Any]]) -> list[PrimitiveAction]:
    """Reduce an `opentrons.simulate.simulate` runlog to `(action, volume, well, slot)` tuples."""
    actions: list[PrimitiveAction] = []
    skip_above_level: int | None = None

    def _visit(node: Mapping[str, Any]) -> None:
        nonlocal skip_above_level
        raw_level = node.get("level")
        level = raw_level if isinstance(raw_level, int) and not isinstance(raw_level, bool) else None
        if skip_above_level is not None and level is not None:
            if level > skip_above_level:
                return
            skip_above_level = None

        payload = node.get("payload")
        text = str(payload.get("text", "")).strip() if isinstance(payload, Mapping) else ""
        ag_match = _AIR_GAP_OT_RE.match(text)
        if ag_match:
            actions.append(("air_gap", round(float(ag_match.group(1)), 4), None, None))
            if level is not None:
                skip_above_level = level
            return
        parsed = _parse_primitive_line(text)
        if parsed is not None:
            actions.append(parsed)
        if level is None:
            children = node.get("logs")
            if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
                for child in children:
                    if isinstance(child, Mapping):
                        _visit(child)

    for entry in runlog:
        _visit(entry)
    return actions

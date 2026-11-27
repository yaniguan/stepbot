"""Data models, stroke planners, and exceptions for the stepbot compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

VOLUME_TOLERANCE = 1e-6

DeckSlot = int | str


class ValidationError(ValueError):
    """Raised when a protocol specification fails validation."""


class McpSecurityViolation(RuntimeError):
    """Default-deny exception raised when a Lean-MCP hardware dispatch fails verification."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.reason = message


def expand_for_volume_constraints(volume: float, max_volume: float) -> tuple[float, ...]:
    """Split a transfer volume by `pipette.max_volume` matching Opentrons 8.8.2 `common.py`."""
    if not math.isfinite(max_volume) or max_volume <= 0.0:
        raise ValueError(f"max_volume must be a positive finite number, got {max_volume!r}")
    if not math.isfinite(volume) or volume <= 0.0:
        raise ValueError(f"volume must be a positive finite number, got {volume!r}")
    rem = float(volume)
    cap = float(max_volume)
    parts: list[float] = []
    while rem > cap * 2.0:
        parts.append(cap)
        rem -= cap
    if rem > cap:
        rem /= 2.0
        parts.append(rem)
    parts.append(rem)
    return tuple(parts)


def plan_transfer_strokes(
    volume: float,
    pipette_max_volume: float,
    effective_max_volume: float,
    *,
    air_gap: float = 0.0,
) -> tuple[tuple[float, ...], ...]:
    """Return `(tip_group_strokes, ...)` for one `(src, dst)` well pair matching Opentrons 8.8.2.

    Opentrons `TransferPlan._plan_transfer` first expands `volume` by
    `pipette.max_volume - air_gap` via `expand_for_volume_constraints` (picking up and
    dropping a tip per outer group when `new_tip == 'always'`), and then splits each
    outer group by `min(pipette.max_volume, tip.max_volume) - air_gap` ("max first,
    then remainder"), executing `mix_after` after each inner stroke.
    """
    if not math.isfinite(air_gap) or air_gap < 0.0:
        raise ValueError(f"air_gap must be a non-negative finite number, got {air_gap!r}")
    outer_cap = float(pipette_max_volume) - float(air_gap)
    inner_cap = float(effective_max_volume) - float(air_gap)
    if not math.isfinite(outer_cap) or outer_cap <= 0.0:
        raise ValueError(f"Effective pipette stroke capacity must be positive, got {outer_cap!r}")
    if not math.isfinite(inner_cap) or inner_cap <= 0.0:
        raise ValueError(f"Effective tip stroke capacity must be positive, got {inner_cap!r}")

    outer_steps = expand_for_volume_constraints(float(volume), outer_cap)
    groups: list[tuple[float, ...]] = []
    for step_vol in outer_steps:
        strokes: list[float] = []
        xferred = 0.0
        while xferred < step_vol:
            s_vol = min(inner_cap, step_vol - xferred)
            if s_vol <= 0.0:
                break
            strokes.append(s_vol)
            xferred += s_vol
        groups.append(tuple(strokes))
    return tuple(groups)


def flat_transfer_strokes(
    volume: float,
    pipette_max_volume: float,
    effective_max_volume: float,
    *,
    air_gap: float = 0.0,
) -> tuple[float, ...]:
    """Return the flat sequence of single-aspirate stroke volumes for one `(src, dst)` pair."""
    groups = plan_transfer_strokes(
        volume,
        pipette_max_volume,
        effective_max_volume,
        air_gap=air_gap,
    )
    return tuple(s_vol for group in groups for s_vol in group)


def plan_distribute_chunks(
    dst_count: int,
    volume: float,
    effective_max_volume: float,
    disposal_volume: float,
    *,
    air_gap: float = 0.0,
) -> tuple[int, ...]:
    """Group `dst_count` wells into multi-dispense chunks matching Opentrons 8.8.2 `distribute`."""
    if dst_count <= 0:
        return ()
    vol = float(volume)
    max_vol = float(effective_max_volume)
    disp_vol = float(disposal_volume)
    ag_vol = float(air_gap)
    if vol <= 0.0 or vol + disp_vol + ag_vol > max_vol + VOLUME_TOLERANCE:
        raise ValueError(
            f"Invalid distribute chunk parameters: volume={vol}, disposal={disp_vol}, "
            f"air_gap={ag_vol}, max={max_vol}"
        )
    chunks: list[int] = []
    idx = 0
    while idx < dst_count:
        chunk_len = 0
        chunk_sum = 0.0
        while idx + chunk_len < dst_count:
            cand = chunk_sum + disp_vol + ag_vol + vol
            if cand <= max_vol or (chunk_len == 0 and cand <= max_vol + VOLUME_TOLERANCE):
                chunk_len += 1
                chunk_sum = chunk_len * vol
            else:
                break
        if chunk_len == 0:
            chunk_len = 1
        chunks.append(chunk_len)
        idx += chunk_len
    return tuple(chunks)


@dataclass(frozen=True)
class LabwareSpec:
    """Metadata describing a standard Opentrons labware."""

    load_name: str
    wells: tuple[str, ...]
    well_grid: dict[str, tuple[int, int]]
    max_volume: float
    is_tiprack: bool = False
    well_volumes: dict[str, float] = field(default_factory=dict)
    touch_tip_disabled: bool = False

    def max_volume_for(self, well: str) -> float:
        """Return the liquid capacity (uL) of a specific well."""
        return self.well_volumes.get(well, self.max_volume)


@dataclass(frozen=True)
class PipetteSpec:
    """Metadata describing a standard Opentrons pipette."""

    model: str
    min_volume: float
    max_volume: float
    channels: int = 1
    nominal_aspirate_rate: float = 92.86
    nominal_dispense_rate: float = 92.86
    nominal_blow_out_rate: float = 92.86


@dataclass(frozen=True)
class DeckItem:
    """A labware placed on a deck slot (OT-2 integer 1..11 or Flex coordinate 'A1'..'D4')."""

    alias: str
    load_name: str
    slot: DeckSlot
    spec: LabwareSpec


@dataclass(frozen=True)
class PipetteConfig:
    """A configured pipette mount and its assigned tip racks."""

    mount: str
    model: str
    tip_racks: tuple[str, ...]
    spec: PipetteSpec
    max_tip_volume: float | None = None

    @property
    def effective_max_volume(self) -> float:
        """Return maximum single-stroke volume bounded by both pipette and tip racks."""
        if self.max_tip_volume is None:
            return self.spec.max_volume
        return min(self.spec.max_volume, self.max_tip_volume)


@dataclass(frozen=True)
class WellRef:
    """Reference to a specific well on a deck labware."""

    labware: str
    well: str

    def expr(self, *, position: str = "bottom") -> str:
        """Return the Python expression referencing this well (or `.top()`)."""
        base = f'{self.labware}["{self.well}"]'
        if position == "top":
            return f"{base}.top()"
        return base

    def key(self) -> str:
        """Return canonical '<labware>:<well>' string."""
        return f"{self.labware}:{self.well}"


@dataclass(frozen=True)
class RheologySpec:
    """Calibrated Opentrons v2 rheology execution parameters for a liquid-handling step."""

    liquid_class: str
    aspirate_flow_rate: float
    dispense_flow_rate: float
    air_gap_ul: float = 0.0
    touch_tip: bool = False
    aspirate_delay_s: float = 0.0


@dataclass(frozen=True)
class LiquidSpec:
    """A named liquid loaded into wells before the protocol starts."""

    name: str
    description: str
    wells: tuple[WellRef, ...]
    volume: float
    liquid_class: str | None = None


@dataclass(frozen=True)
class CommentStep:
    """Emit a protocol comment."""

    text: str


@dataclass(frozen=True)
class DelayStep:
    """Pause the protocol for a duration in seconds and/or minutes."""

    seconds: float = 0.0
    minutes: float = 0.0


@dataclass(frozen=True)
class PauseStep:
    """Pause the protocol until resumed."""

    message: str = ""


@dataclass(frozen=True)
class PickUpTipStep:
    """Pick up a tip on a pipette."""

    pipette: str


@dataclass(frozen=True)
class DropTipStep:
    """Drop the currently mounted tip on a pipette."""

    pipette: str


@dataclass(frozen=True)
class TransferStep:
    """Transfer liquid between source and destination wells."""

    pipette: str
    src: tuple[WellRef, ...]
    dst: tuple[WellRef, ...]
    volume: float
    new_tip: str = "once"
    mix_after: tuple[int, float] | None = None
    dispense_position: str = "bottom"
    rheology: RheologySpec | None = None


@dataclass(frozen=True)
class DistributeStep:
    """Distribute liquid from one source well to multiple destinations."""

    pipette: str
    src: WellRef
    dst: tuple[WellRef, ...]
    volume: float
    new_tip: str = "once"
    disposal_volume: float | None = None
    dispense_position: str = "bottom"
    rheology: RheologySpec | None = None


@dataclass(frozen=True)
class MixStep:
    """Mix liquid in one or more wells."""

    pipette: str
    wells: tuple[WellRef, ...]
    volume: float
    reps: int
    rheology: RheologySpec | None = None


@dataclass(frozen=True)
class DiscardStep:
    """Aspirate liquid from a well and dispense into fixed trash."""

    pipette: str
    src: WellRef
    volume: float
    strokes: int = 1
    stroke_volumes: tuple[float, ...] = ()

    def effective_strokes(self) -> tuple[float, ...]:
        """Return explicit per-stroke volumes, falling back to equal division when unset."""
        if self.stroke_volumes:
            return self.stroke_volumes
        count = max(1, self.strokes)
        return (self.volume / count,) * count


LoweredStep = (
    CommentStep
    | DelayStep
    | PauseStep
    | PickUpTipStep
    | DropTipStep
    | TransferStep
    | DistributeStep
    | MixStep
    | DiscardStep
)


@dataclass(frozen=True)
class WellSummary:
    """Initial, peak, and final volume plus species lineage for a well."""

    initial_ul: float
    peak_ul: float
    final_ul: float
    capacity_ul: float
    species: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepOrigin:
    """Mapping from a lowered step back to its 1-based source YAML/JSON step."""

    source_step_index: int | None
    source_step_type: str


@dataclass(frozen=True)
class ProtocolSpec:
    """Validated protocol definition ready for code generation."""

    name: str
    description: str
    api_level: str
    deck: dict[str, DeckItem]
    pipettes: dict[str, PipetteConfig]
    steps: list[LoweredStep] = field(default_factory=list)
    liquids: tuple[LiquidSpec, ...] = ()
    tips_used_per_rack: dict[str, int] = field(default_factory=dict)
    tips_used_per_mount: dict[str, int] = field(default_factory=dict)
    well_summaries: dict[str, WellSummary] = field(default_factory=dict)
    step_origins: tuple[StepOrigin, ...] = ()
    robot_type: str = "OT-2"
    trash_slot: str | None = None

    def to_manifest_meta(self) -> dict[str, Any]:
        """Return static analysis summary fields for JSON manifest emission."""
        return {
            "tips_used_per_rack": dict(self.tips_used_per_rack),
            "tips_used_per_mount": dict(self.tips_used_per_mount),
        }

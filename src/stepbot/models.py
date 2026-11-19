"""Data models and exceptions for the stepbot compiler."""

from __future__ import annotations

from dataclasses import dataclass, field


class ValidationError(ValueError):
    """Raised when a protocol specification fails validation."""


@dataclass(frozen=True)
class LabwareSpec:
    """Metadata describing a standard Opentrons labware."""

    load_name: str
    wells: tuple[str, ...]
    well_grid: dict[str, tuple[int, int]]
    max_volume: float
    is_tiprack: bool = False


@dataclass(frozen=True)
class PipetteSpec:
    """Metadata describing a standard Opentrons pipette."""

    model: str
    min_volume: float
    max_volume: float
    channels: int = 1


@dataclass(frozen=True)
class DeckItem:
    """A labware placed on a deck slot."""

    alias: str
    load_name: str
    slot: int
    spec: LabwareSpec


@dataclass(frozen=True)
class PipetteConfig:
    """A configured pipette mount and its assigned tip racks."""

    mount: str
    model: str
    tip_racks: tuple[str, ...]
    spec: PipetteSpec


@dataclass(frozen=True)
class WellRef:
    """Reference to a specific well on a deck labware."""

    labware: str
    well: str

    def expr(self) -> str:
        """Return the Python expression referencing this well."""
        return f'{self.labware}["{self.well}"]'


@dataclass(frozen=True)
class LiquidSpec:
    """A named liquid loaded into wells before the protocol starts."""

    name: str
    description: str
    wells: tuple[WellRef, ...]
    volume: float


@dataclass(frozen=True)
class CommentStep:
    """Emit a protocol comment."""

    text: str


@dataclass(frozen=True)
class DelayStep:
    """Pause the protocol for a duration in seconds."""

    seconds: float


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


@dataclass(frozen=True)
class DistributeStep:
    """Distribute liquid from one source well to multiple destinations."""

    pipette: str
    src: WellRef
    dst: tuple[WellRef, ...]
    volume: float
    new_tip: str = "once"


@dataclass(frozen=True)
class MixStep:
    """Mix liquid in one or more wells."""

    pipette: str
    wells: tuple[WellRef, ...]
    volume: float
    reps: int


@dataclass(frozen=True)
class DiscardStep:
    """Aspirate liquid from a well and dispense into fixed trash."""

    pipette: str
    src: WellRef
    volume: float


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
class ProtocolSpec:
    """Validated protocol definition ready for code generation."""

    name: str
    description: str
    api_level: str
    deck: dict[str, DeckItem]
    pipettes: dict[str, PipetteConfig]
    steps: list[LoweredStep] = field(default_factory=list)
    liquids: tuple[LiquidSpec, ...] = ()

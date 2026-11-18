# stepbot

`stepbot` compiles simple YAML or JSON liquid-handling protocols into deterministic, readable Opentrons Python API v2 protocol files (`metadata = {"apiLevel": "2.16"}` and `def run(protocol): ...`) and checks them with either the Opentrons simulator or a built-in mock context.

## Install

Base install (compiler, CLI, and built-in mock checker):

```bash
pip install -e .
```

With the Opentrons Python API v2 simulator and test dependencies:

```bash
pip install -e ".[sim,dev]"
```

## Quickstart

Compile a protocol from YAML or JSON:

```bash
stepbot compile examples/dilution_series.yaml -o examples/out/dilution_series.py
```

Simulate and verify the compiled Python protocol:

```bash
stepbot check examples/out/dilution_series.py
```

Pass `--mock` to force the built-in mock simulator even when `opentrons` is installed, or `-v` to print the full run log:

```bash
stepbot check examples/out/dilution_series.py --mock
stepbot check examples/out/dilution_series.py -v
```

## Example output

Running `stepbot compile` and `stepbot check` on `examples/dilution_series.yaml` (measured on a CPU-only Linux box, 2026-11-18):

```text
$ stepbot compile examples/dilution_series.yaml -o examples/out/dilution_series.py
Wrote examples/out/dilution_series.py

$ stepbot check examples/out/dilution_series.py -v
OK (opentrons): 100 commands, 100 run log lines
Prepare 2-fold dye dilution series across row A
Distributing 100.0 from A2 of NEST 12 Well Reservoir 15 mL on slot 2 to A2 of Corning 96 Well Plate 360 µL Flat on slot 1
	Transferring 100.0 from A2 of NEST 12 Well Reservoir 15 mL on slot 2 to A2 of Corning 96 Well Plate 360 µL Flat on slot 1
		Picking up tip from A1 of Opentrons OT-2 96 Tip Rack 300 µL on slot 3
		Aspirating 220.0 uL from A2 of NEST 12 Well Reservoir 15 mL on slot 2 at 92.86 uL/sec
		Dispensing 100.0 uL into A2 of Corning 96 Well Plate 360 µL Flat on slot 1 at 92.86 uL/sec
		Dispensing 100.0 uL into A3 of Corning 96 Well Plate 360 µL Flat on slot 1 at 92.86 uL/sec
		Blowing out into Trash Bin on slot 12
		Aspirating 220.0 uL from A2 of NEST 12 Well Reservoir 15 mL on slot 2 at 92.86 uL/sec
		Dispensing 100.0 uL into A4 of Corning 96 Well Plate 360 µL Flat on slot 1 at 92.86 uL/sec
		Dispensing 100.0 uL into A5 of Corning 96 Well Plate 360 µL Flat on slot 1 at 92.86 uL/sec
		Blowing out into Trash Bin on slot 12
		Aspirating 220.0 uL from A2 of NEST 12 Well Reservoir 15 mL on slot 2 at 92.86 uL/sec
		Dispensing 100.0 uL into A6 of Corning 96 Well Plate 360 µL Flat on slot 1 at 92.86 uL/sec
...
```

Summary across all three bundled examples (`opentrons` 8.8.2 simulator, measured on a CPU-only Linux box, 2026-11-18):

```text
examples/out/dilution_series.py -> OK (opentrons): 100 commands, 100 run log lines
examples/out/plate_fill.py      -> OK (opentrons): 131 commands, 131 run log lines
examples/out/reservoir_mix.py   -> OK (opentrons): 117 commands, 117 run log lines
```

With `--mock` on `examples/out/dilution_series.py`:

```text
OK (mock): 86 commands, 86 run log lines
```

## How it works

1. **Parse and validate** (`stepbot.compiler`, `stepbot.wells`, `stepbot.catalog`):
   - Reads YAML or JSON with `metadata`, `deck`, `pipettes`, and `steps`.
   - Validates labware load names against the catalog, deck slots (`1`–`11`, unique), well coordinates and ranges (`plate:A1-A8`), pipette volume bounds, well liquid capacities, and tip consumption across tip racks.
2. **Lower high-level steps**:
   - Supports `transfer` (`new_tip` `once`/`always`/`never`), `distribute`, `mix`, `dilution_series`, `delay`/`pause`, and `comment`.
   - `dilution_series` (`source`, `diluent`, `first_well`, `n`, `factor`, `final_volume`) expands into a diluent `distribute`, an initial stock `transfer`, sequential serial `transfer` calls with `mix_after`, and a final excess discard to `protocol.fixed_trash`.
3. **Code generation** (`stepbot.codegen`):
   - Emits deterministic Python sorted by deck slot and pipette mount, targeting Opentrons Python API v2 (`apiLevel` `2.16`).
4. **Simulation check** (`stepbot.checker`):
   - Runs `opentrons.simulate.simulate` when `opentrons` is installed, or falls back to `MockProtocolContext` (tracking tips, wells, and volumes) when running without `opentrons`.

## Tests

```bash
pytest
```

On the CPU-only Linux box (2026-11-18), the full suite ran:

```text
40 passed in 1.71s
```

## Limits

- Targets OT-2 style decks (slots `1`–`11` with fixed trash on slot `12`) and standard single/multi-channel liquid-handling steps; hardware modules (temperature, shaker, thermocycler) and custom labware JSON files are not modeled.
- Well ranges (`plate:A1-B4`) expand row-wise within rectangular grids.
- Tip-count validation assumes single-tip transfers; multi-channel column constraints are not modeled separately.
- Real simulation via the `[sim]` extra requires `opentrons>=7.0,<9.0` (Opentrons v9+ removed OT-2 simulation from the `opentrons` package).

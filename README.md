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

## System Profile: Object · Data · Model · Training · History · Unique Properties

`stepbot` is part of the [Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab) ecosystem ([Project Studio](https://yaniguan.github.io/auto-lab/)) at **Layer `L1 · Protocol Compiler & Visuo-Tactile Manipulation`**.

- **Object**: Deterministic liquid-handling compiler and dual-backend verifier translating declarative YAML/JSON protocols into idiomatic Opentrons Python API v2 scripts (`apiLevel` `2.16`).
- **Data**: Declarative protocol schema (`metadata`, `deck`, `pipettes`, optional `liquids` on `origin/review-fixes`, and `steps`), built-in catalog of `24` Opentrons labware definitions and `12` Gen1/Gen2 pipettes, and `3` end-to-end YAML/Python example pairs.
- **Model**: Four-stage pipeline (`wells.py` 2D grid/range resolver -> `compiler.py` step lowerer, `dilution_series` macro expander, `_TipStateTracker` automaton, and `_WellVolumeTracker` -> `codegen.py` slot/mount-sorted Python emitter -> `checker.py` `opentrons.simulate` + `MockProtocolContext`).
- **Training**: Rule-based symbolic compiler (zero learned weights), verified by deterministic codegen checks and pytest suites (`40` tests on `main`; `73` tests with full liquid-conservation and YAML `2.10` type guards on `origin/review-fixes`).
- **History**: `4534ff1` (2026-11-18, `main` v0.1.0 compiler, mock/Opentrons checker, CLI, examples, and CI) -> `ef6865f` (2026-11-18, `origin/review-fixes` deterministic `apiLevel`, `liquids` & well-volume tracking, multichannel/alias guards, and expanded tests).
- **Unique Properties**: Byte-reproducible code generation, compile-time tip-budget and liquid-volume conservation before hardware touch, and direct `L1` integration with [`twinstep`](https://github.com/yaniguan/twinstep), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), and [`lab-hands`](https://github.com/yaniguan/lab-hands).

Full technical specification: [`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md).

## Repository Layout

```text
stepbot/
├── .github/workflows/ci.yml      # Python 3.11/3.12 CI matrix (mock + Opentrons simulator)
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
├── examples/
│   ├── dilution_series.yaml      # 8-well 2-fold serial dilution example
│   ├── plate_fill.yaml           # Multi-reagent distribute, transfer, and mix example
│   ├── reservoir_mix.yaml        # Dual-pipette (p300 + p20) 24-well example
│   └── out/                      # Deterministically compiled Opentrons Python API v2 scripts
├── pyproject.toml                # Package metadata, CLI entrypoint, and [sim,dev] extras
├── src/stepbot/
│   ├── catalog.py                # 24 standard labware specs, 12 pipette specs, API version helpers
│   ├── checker.py                # opentrons.simulate wrapper + zero-dependency MockProtocolContext
│   ├── cli.py                    # `stepbot compile` and `stepbot check` CLI
│   ├── codegen.py                # Slot- and mount-sorted Python API v2 source emitter
│   ├── compiler.py               # Schema validator, macro lowerer, tip & volume state trackers
│   ├── models.py                 # Frozen IR dataclasses and ValidationError
│   └── wells.py                  # Well coordinate parser, rectangular range & serial expanders
└── tests/                        # CLI, compiler, example-determinism, and validation test suites
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

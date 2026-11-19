# stepbot

`stepbot` compiles simple YAML or JSON liquid-handling protocols into deterministic, readable Opentrons Python API v2 protocol files (`metadata = {"apiLevel": "2.16"}` and `def run(protocol): ...`) and checks them with either the Opentrons simulator or a built-in mock context. The emitted `apiLevel` is always `2.16` unless the input sets `metadata.apiLevel`; it does not depend on which `opentrons` version is installed.

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

## Input format

```yaml
metadata:
  name: Two-fold dilution series
  apiLevel: "2.16"        # optional; must be a quoted string

deck:
  plate: {labware: corning_96_wellplate_360ul_flat, slot: 1}
  reservoir: {labware: nest_12_reservoir_15ml, slot: 2}
  tips: {labware: opentrons_96_tiprack_300ul, slot: 3}

pipettes:
  left: {model: p300_single_gen2, tip_racks: [tips]}

liquids:                  # starting volumes; every other well starts empty
  dye: {wells: reservoir:A1, volume: 2000}
  buffer: {wells: reservoir:A2, volume: 5000}

steps:
  - transfer: {src: reservoir:A1, dst: plate:A1-A4, volume: 50}
```

- `apiLevel` must be a quoted string of the form `2.<minor>`. Unquoted YAML numbers are rejected because YAML reads `2.10` as the number `2.1`.
- `liquids` gives each listed well its starting volume (in uL). It is compiled to `protocol.define_liquid(...)` and `well.load_liquid(...)`, which need `apiLevel` 2.14 or newer.
- Deck aliases cannot be Python keywords or names used by the generated code (`protocol`, `metadata`, `liquids`, `pipette_left`, `pipette_right`).

## Example output

`stepbot check --mock` on the bundled examples (measured in this repository's test environment with PyYAML and without `opentrons`):

```text
examples/out/dilution_series.py -> OK (mock): 86 commands, 86 run log lines
examples/out/plate_fill.py      -> OK (mock): 116 commands, 116 run log lines
examples/out/reservoir_mix.py   -> OK (mock): 109 commands, 109 run log lines
```

Start of `stepbot check examples/out/dilution_series.py --mock -v`:

```text
OK (mock): 86 commands, 86 run log lines
Comment: Prepare 2-fold dye dilution series across row A
Picking up tip from A1 of opentrons_96_tiprack_300ul on slot 3
Aspirating 300.0 uL from A2 of nest_12_reservoir_15ml on slot 2
Dispensing 100.0 uL into A2 of corning_96_wellplate_360ul_flat on slot 1
Dispensing 100.0 uL into A3 of corning_96_wellplate_360ul_flat on slot 1
Dispensing 100.0 uL into A4 of corning_96_wellplate_360ul_flat on slot 1
Aspirating 300.0 uL from A2 of nest_12_reservoir_15ml on slot 2
...
```

Command counts from the real `opentrons` simulator: not run (`opentrons` was not installed). Expect them to differ from the mock counts, since the mock does not reproduce every simulator log entry (for example the `distribute` disposal volume).

## How it works

1. **Parse and validate** (`stepbot.compiler`, `stepbot.wells`, `stepbot.catalog`):
   - Reads YAML or JSON with `metadata`, `deck`, `pipettes`, optional `liquids`, and `steps`.
   - Validates labware load names against the catalog, deck slots (`1`–`11`, unique), well coordinates and ranges (`plate:A1-A8`), pipette volume bounds, the `apiLevel` format, and tip consumption across tip racks.
   - Rejects multichannel pipette models (`p*_multi*`) until column handling is implemented.
2. **Lower high-level steps and track well volumes**:
   - Supports `transfer` (`new_tip` `once`/`always`/`never`), `distribute`, `mix`, `dilution_series`, `delay`/`pause`, and `comment`.
   - `dilution_series` (`source`, `diluent`, `first_well`, `n`, `factor`, `final_volume`) expands into a diluent `distribute`, an initial stock `transfer`, sequential serial `transfer` calls with `mix_after`, and a final excess discard to `protocol.fixed_trash`.
   - Keeps a running volume for every well (starting from `liquids`, otherwise 0) across all steps. Compilation fails if a step would push a well above its labware capacity, aspirate more than a well holds, or mix (including `mix_after`) with more volume than the well holds.
3. **Code generation** (`stepbot.codegen`):
   - Emits deterministic Python sorted by deck slot and pipette mount, targeting Opentrons Python API v2 (`apiLevel` `2.16` unless set in the input).
4. **Simulation check** (`stepbot.checker`):
   - When `opentrons` is installed, first checks the protocol's `apiLevel` against the highest level the installed package supports and fails with a clear message if it is too new, then runs `opentrons.simulate.simulate`.
   - Otherwise (or with `--mock`) runs `MockProtocolContext`, which tracks tips, pipette volume, and the volume in every well (set by `load_liquid`, otherwise 0). It fails when a well would go below 0 uL or above its capacity.

## Tests

```bash
pytest
```

With the base install plus `pytest` (no `[sim]` extra, so `opentrons` is absent), `python -m pytest -q --tb=short -o pythonpath=src` gave:

```text
70 passed, 3 skipped
```

The 3 skipped tests run the examples through the real `opentrons` simulator and need the `[sim]` extra; that configuration was not run here.

## Limits

- Targets OT-2 style decks (slots `1`–`11` with fixed trash on slot `12`) and single-channel liquid-handling steps; hardware modules (temperature, shaker, thermocycler) and custom labware JSON files are not modeled.
- Well ranges (`plate:A1-B4`) expand row-wise within rectangular grids.
- Multichannel pipettes are rejected at compile time.
- Volume tracking counts nominal volumes only: the disposal volume that the real Opentrons `distribute` aspirates and blows out is not deducted from the source well, and labware with mixed well sizes uses one capacity for every well.
- The generated `define_liquid`/`load_liquid` calls have been checked with the mock only; they have not been run through the real `opentrons` simulator here.
- Real simulation via the `[sim]` extra requires `opentrons>=7.0,<9.0` (Opentrons v9+ removed OT-2 simulation from the `opentrons` package).

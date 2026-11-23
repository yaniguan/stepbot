# stepbot

`stepbot` compiles simple YAML or JSON liquid-handling protocols into deterministic, readable Opentrons Python API v2 protocol files (`metadata = {"apiLevel": "2.16"}` and `def run(protocol): ...`) and checks them with either the Opentrons simulator or a built-in safe AST mock context. The emitted `apiLevel` is always `2.16` unless the input sets `metadata.apiLevel`; it does not depend on which `opentrons` version is installed.

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

- **Object**: Deterministic liquid-handling compiler and dual-backend verifier translating declarative YAML/JSON protocols into idiomatic Opentrons Python API v2 scripts (`apiLevel` `2.16`) plus optional JSON manifests (`--manifest`).
- **Data**: Strictly validated protocol schema (`metadata`, `deck`, `pipettes`, optional `liquids`, and `steps`), built-in catalog of `24` Opentrons labware definitions (including heterogeneous 4x50 mL + 6x15 mL Falcon racks) and `12` Gen1/Gen2 pipettes, `twinstep` protocol importer (`stepbot.interop.from_twinstep`), and `3` end-to-end YAML/Python example pairs.
- **Model**: Four-stage pipeline (`wells.py` 2D row/column grid & range resolver -> `compiler.py` strict schema validator, macro lowerer, `_TipStateTracker` automaton, and `_WellVolumeTracker` with `distribute` disposal-volume & reagent-lineage tracking -> `codegen.py` slot/mount-sorted Python & manifest emitter -> `checker.py` `opentrons.simulate` + execution-safe AST `MockProtocolContext`).
- **Training**: Rule-based symbolic compiler (zero learned weights), verified by deterministic codegen checks, strict `mypy` type checking, and `102` pytest cases (`99` passing + `3` real-Opentrons simulator tests skipped when `[sim]` is not installed).
- **History**: `4534ff1` (2026-11-18, `main` v0.1.0 compiler, mock/Opentrons checker, CLI, examples, and CI) -> `ef6865f` (2026-11-18, `review-fixes` deterministic `apiLevel`, `liquids` & well-volume tracking, multichannel/alias guards) -> full safety, AST mock sandbox, reagent-lineage, `twinstep` interop, and `--manifest` hardening.
- **Unique Properties**: Byte-reproducible LF code generation across Linux/macOS/Windows, compile-time tip-rack compatibility, liquid-volume conservation, and cross-contamination lineage checks before hardware touch, and direct `L1` integration with [`twinstep`](https://github.com/yaniguan/twinstep), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-ears`](https://github.com/yaniguan/lab-ears), [`labshift`](https://github.com/yaniguan/labshift), and [`lab-hands`](https://github.com/yaniguan/lab-hands).

Full technical specification: [`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md).

## Repository Layout

```text
stepbot/
├── .github/workflows/ci.yml      # Cross-OS mock tests, mypy --strict, and Opentrons sim CI
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
├── examples/
│   ├── dilution_series.yaml      # 8-well 2-fold serial dilution example
│   ├── plate_fill.yaml           # Multi-reagent distribute, transfer, and mix example
│   ├── reservoir_mix.yaml        # Dual-pipette (p300 + p20) 24-well example
│   └── out/                      # Deterministically compiled Opentrons Python API v2 scripts
├── pyproject.toml                # Package metadata, CLI entrypoint, pytest/mypy config
├── src/stepbot/
│   ├── catalog.py                # 24 standard labware specs, 12 pipette specs, API version helpers
│   ├── checker.py                # opentrons.simulate wrapper + safe AST MockProtocolContext
│   ├── cli.py                    # `stepbot compile` (--manifest) and `stepbot check` CLI
│   ├── codegen.py                # Slot/mount-sorted Python API v2 & JSON manifest emitter
│   ├── compiler.py               # Strict schema validator, macro lowerer, tip/volume/lineage trackers
│   ├── interop.py                # `from_twinstep` and `check_twinstep_protocol` bridge
│   ├── models.py                 # Frozen IR dataclasses and ValidationError
│   └── wells.py                  # Well coordinate parser, row/col 2D range & serial expanders
└── tests/                        # CLI, compiler, example-determinism, validation, and audit tests
```

## Quickstart

Compile a protocol from YAML or JSON (optionally writing a JSON static-analysis manifest):

```bash
stepbot compile examples/dilution_series.yaml -o examples/out/dilution_series.py --manifest manifest.json
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
- Unknown or misspelled keys (e.g. `new_tips: always` or `liquid:`) are rejected with `difflib` suggestions.
- `liquids` gives each listed well its starting volume (in uL) and reagent species identity for cross-contamination checks (`allow_contamination: true` opts out when reuse is intentional). It is compiled to `protocol.define_liquid(...)` and `well.load_liquid(...)`, which need `apiLevel` 2.14 or newer.
- Deck aliases cannot be Python keywords or names used by the generated code (`protocol`, `metadata`, `liquids`, `pipette_left`, `pipette_right`).
- Rectangular 2D well ranges (`plate:A1-B4`) expand row-major by default; pass `well_order: col` or `plate:col:1-2` for column-major traversal.

## Example output

`stepbot check --mock` on the bundled examples (measured in this repository's test environment without `opentrons`):

```text
examples/out/dilution_series.py -> OK (mock): 91 commands, 91 run log lines
examples/out/plate_fill.py      -> OK (mock): 127 commands, 127 run log lines
examples/out/reservoir_mix.py   -> OK (mock): 114 commands, 114 run log lines
```

Start of `stepbot check examples/out/dilution_series.py --mock -v`:

```text
OK (mock): 91 commands, 91 run log lines
Comment: Prepare 2-fold dye dilution series across row A
Picking up tip from A1 of opentrons_96_tiprack_300ul on slot 3
Aspirating 220.0 uL from A2 of nest_12_reservoir_15ml on slot 2
Dispensing 100.0 uL into A2 of corning_96_wellplate_360ul_flat on slot 1
Dispensing 100.0 uL into A3 of corning_96_wellplate_360ul_flat on slot 1
Blowing out into Trash Bin on slot 12
Aspirating 220.0 uL from A2 of nest_12_reservoir_15ml on slot 2
...
```

Command counts from the real `opentrons` simulator: not run here (`opentrons` was not installed).

## How it works

1. **Parse and validate** (`stepbot.compiler`, `stepbot.wells`, `stepbot.catalog`):
   - Reads YAML or JSON with strict key validation on `metadata`, `deck`, `pipettes`, optional `liquids`, and `steps`.
   - Validates labware load names (with per-well capacities on mixed racks), deck slots (`1`–`11`, unique), well coordinates (including multi-letter rows `AA1..AF48` and explicit leading-zero diagnostics), pipette-to-tip-rack family compatibility, effective max volume (`min(pipette, tip_rack)`), `apiLevel` format, and tip consumption across racks.
   - Rejects multichannel pipette models (`p*_multi*`) until column handling is implemented.
2. **Lower high-level steps and track well volumes + reagent lineage**:
   - Supports `transfer` (`new_tip` `once`/`always`/`never`, optional `multi_aspirate: true`), `distribute` (modeling Opentrons `disposal_volume`), `mix`, `dilution_series` (disjoint source/diluent/series check, configurable `precision`, and trash `blow_out`), `delay` (`minutes` and/or `seconds`), `pause`, and `comment`.
   - Keeps a running volume and reagent species set for every well and mounted tip across all steps, failing on well overfill, dry-well aspiration, oversized `mix`, or dirty-tip cross-contamination.
3. **Code and manifest generation** (`stepbot.codegen`):
   - Emits deterministic Python sorted by deck slot and pipette mount targeting Opentrons Python API v2 (`apiLevel` `2.16` unless set in the input), plus optional JSON manifests (`--manifest`) with `source_sha256`, tip usage per rack, initial/peak/final well volumes, and step-index provenance.
4. **Simulation check** (`stepbot.checker`):
   - When `opentrons` is installed, checks `apiLevel` against the installed package's `MAX_SUPPORTED_VERSION` and runs `opentrons.simulate.simulate`.
   - Otherwise (or with `--mock`) runs a restricted AST interpreter against `MockProtocolContext` (no `exec()`/`eval()`), tracking shared tip-rack wells, stroke splitting, `distribute` disposal volumes, and per-well capacities.

## Tests

```bash
pytest
```

With the base install plus `pytest` (no `[sim]` extra, so `opentrons` is absent), `python -m pytest -q` gave:

```text
99 passed, 3 skipped
```

The 3 skipped tests run the examples through the real `opentrons` simulator and need the `[sim]` extra.

## Limits

- Targets OT-2 style decks (slots `1`–`11` with fixed trash on slot `12`) and single-channel liquid-handling steps; hardware modules (temperature, shaker, thermocycler) and custom labware JSON files are not modeled.
- Multichannel pipettes are rejected at compile time.
- Real simulation via the `[sim]` extra requires `opentrons>=7.0,<9.0` (Opentrons v9+ removed OT-2 simulation from the `opentrons` package).

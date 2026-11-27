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

- **Object**: Deterministic liquid-handling compiler and dual-backend verifier translating declarative YAML/JSON protocols into idiomatic Opentrons Python API v2 scripts (OT-2 `apiLevel` `2.16` and Opentrons Flex `apiLevel` `2.18`--`2.20` with `requirements` and `load_trash_bin`) plus optional JSON manifests (`--manifest`).
- **Data**: Strictly validated protocol schema (`metadata`, optional `requirements`, `deck`, `pipettes`, optional `liquids` with `liquid_class` rheology profiles, and `steps` with strict finiteness guards rejecting `NaN`/`Infinity`), built-in catalog of `30` Opentrons OT-2 and Flex labware definitions (including heterogeneous 4x50 mL + 6x15 mL Falcon racks, reservoir `touchTipDisabled` quirks, and Flex `50/200/1000 uL` tip racks) and `16` OT-2 Gen1/Gen2 and Flex 1-/8-channel pipettes, `twinstep` protocol importer (`stepbot.interop.from_twinstep`), `3` end-to-end YAML/Python example pairs, and a `36`-protocol public multi-step assay benchmark (`benchmarks/public_protocols_eval.py`).
- **Model**: Four-stage pipeline (`wells.py` 2D row/column grid & range resolver -> `compiler.py` strict schema validator, canonical two-stage Opentrons 8.8.2 stroke splitter, `_TipStateTracker` automaton, and `_WellVolumeTracker` with 8-channel `A..H` column broadcasting, multi-chunk `distribute` disposal-volume accounting, destination-contact reagent-lineage tracking, and `dispense_position: bottom|top` modeling -> `codegen.py` slot/mount-sorted Python, calibrated rheology, & `allow_nan=False` manifest emitter -> `checker.py` AST-gated `opentrons.simulate` + execution-safe `MockProtocolContext` -> `mcp_runtime.py` & `robot_http.py` zero-trust `lean-mcp/v1` executor and hardware-optional `:31950` HTTP Run API v2 adapter), backed by machine-checked Lean 4.36.0 proofs in `lean/StepbotLean.lean`.
- **Training**: Rule-based symbolic compiler (zero learned weights), verified by deterministic codegen checks, strict `mypy` type checking, `100.0%` differential simulation parity against `opentrons.simulate` `8.8.2` (`311/311` seeded protocols + `36/36` public wet-lab assays), and `278` pytest cases (`278 passed` with `opentrons 8.8.2`, `272 passed, 6 skipped` without `[sim]`).
- **History**: `4534ff1` (2026-11-18, `main` v0.1.0 compiler, mock/Opentrons checker, CLI, examples, and CI) -> `ef6865f` (2026-11-18, `review-fixes` deterministic `apiLevel`, `liquids` & well-volume tracking, multichannel/alias guards) -> full safety, AST mock sandbox, reagent-lineage, `twinstep` interop, and `--manifest` hardening.
- **Unique Properties**: Byte-reproducible LF code generation across Linux/macOS/Windows, compile-time tip-rack compatibility, liquid-volume conservation, and cross-contamination lineage checks before hardware touch, and direct `L1` integration across the 15-repository Auto-Lab stack with [`twinstep`](https://github.com/yaniguan/twinstep), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-verify`](https://github.com/yaniguan/lab-verify) (`lean-mcp/v1`), [`lab-ears`](https://github.com/yaniguan/lab-ears), [`labshift`](https://github.com/yaniguan/labshift), [`lab-hands`](https://github.com/yaniguan/lab-hands), [`lab-feet`](https://github.com/yaniguan/lab-feet), and downstream post-run partner [`lab-attrib`](https://github.com/yaniguan/lab-attrib) (`stepbot -> lab-attrib` execution telemetry).

Full technical specification: [`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md).

## Repository Layout

```text
stepbot/
├── .github/workflows/ci.yml      # Cross-OS mock tests, mypy --strict, and Opentrons sim CI
├── autolab_wire.py               # 15-repo autolab-wire/v1 L1 compile & Lean-MCP execution node
├── benchmarks/
│   └── public_protocols_eval.py  # 36-assay multi-step Opentrons 8.8.2 benchmark suite
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
├── examples/
│   ├── dilution_series.yaml      # 8-well 2-fold serial dilution example
│   ├── plate_fill.yaml           # Multi-reagent distribute, top-dispense transfer, and mix
│   ├── reservoir_mix.yaml        # Dual-pipette (p300 + p20) 24-well example
│   └── out/                      # Deterministically compiled Opentrons Python API v2 scripts
├── lean/
│   └── StepbotLean.lean          # Zero-sorry Lean 4.36.0 stroke, tip, and volume proofs
├── pyproject.toml                # Package metadata, CLI entrypoint, pytest/mypy config
├── src/stepbot/
│   ├── catalog.py                # 30 labware specs, 16 1-/8-channel pipettes, rheology classes
│   ├── checker.py                # opentrons.simulate wrapper + safe AST MockProtocolContext
│   ├── cli.py                    # `compile`, `check`, `mcp-compile`, and `mcp-dispatch` CLI
│   ├── codegen.py                # Slot/mount-sorted Python API v2, rheology, & JSON manifest
│   ├── compiler.py               # Schema validator, stroke planner, 8-ch tip/volume/lineage
│   ├── interop.py                # `from_twinstep` and `check_twinstep_protocol` bridge
│   ├── mcp_runtime.py            # Zero-trust `lean-mcp/v1` envelope compiler & stateful executor
│   ├── models.py                 # Frozen IR dataclasses, stroke planners, and ValidationError
│   ├── robot_http.py             # Hardware-optional Opentrons HTTP Run API v2 (:31950) adapter
│   └── wells.py                  # Well coordinate parser, row/col 2D range & serial expanders
└── tests/                        # Differential sim, CLI, compiler, validation, and audit tests
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

## Zero-Trust Lean-MCP Hardware Dispatch (`lean-mcp/v1`)

`stepbot.mcp_runtime` pairs deterministic `apiLevel 2.16` compilation with wire-compatible `lean-mcp/v1` capability envelopes shared with [`lab-verify`](https://github.com/yaniguan/lab-verify). Before executing a protocol on the simulator or deck, `StepbotMcpExecutor.dispatch` enforces:
1. Canonical HMAC-SHA256 authenticity (`mcp_hmac_authentic`);
2. Post-compile script SHA-256 binding (`mcp_payload_binding`);
3. Physical deck epoch freshness (`token_epoch == current_deck_epoch`, `mcp_epoch_freshness`);
4. Bounded FIFO single-use `nonce` replay prevention (`mcp_nonce_single_use`) with crash-safe `os.fsync` + `os.replace` + advisory lockfile `--state` persistence and fail-closed `mcp_state_corrupt` validation;
5. Script `envelope_bounds` verification (`deck_slots`, `max_transfer_ul`, `api_level`, `mcp_bounds_exceeded`) plus zero-click autonomous execution inside the certified regime (`lean_proof_valid and not is_frontier_exception`) or human-supervisor co-signed frontier override (`mcp_proof_or_cosigned_exception`), with optional two-phase pre-play nonce/epoch commit and `deck_tainted` fault lock over the hardware-optional `OpentronsHttpRunAdapter` (`--robot-url http://<robot-ip>:31950`, verified in CI against a loopback HTTP v2 mock server).

```bash
stepbot mcp-compile examples/dilution_series.yaml -o out.py --envelope envelope.json --epoch 1 --secret-key "$LEAN_MCP_SECRET_KEY"
stepbot mcp-dispatch out.py --envelope envelope.json --state deck_state.json --secret-key "$LEAN_MCP_SECRET_KEY" --mock --json
```

## 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)

`autolab_wire.py` (`REPO_ID = "stepbot"`, `LAYER = "L1"`) connects `stepbot` to the 15-repository [Auto-Lab](https://github.com/yaniguan/auto-lab) closed-loop packet pipeline (`lab-ears -> lab-ask-why -> lab-decide -> lab-automation-atlas -> buy-or-make -> labshift -> lab-rehearsal -> lab-verify -> stepbot -> lab-hands -> lab-feet -> lab-attrib -> twinstep -> labfill -> labbench-posttrain`):

- **`compile_stage(packet)`**: Compiles `rehearsal.protocol_yaml_dict` (from [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal)) or `examples/plate_fill.yaml` into deterministic Opentrons `apiLevel 2.16` Python code and emits `stepbot_compiled` (`python_code`, `protocol_sha256`, `envelope_bounds`, `self_envelope`) so [`lab-verify`](https://github.com/yaniguan/lab-verify) can co-mint a `lean-mcp/v1` capability token bound to the exact compiled script hash.
- **`process_packet(packet)`**: Dispatches the compiled script through `StepbotMcpExecutor` (`use_mock=True`, `simulate=True`) using `verification.mcp_envelope` when its `protocol_sha256` matches (or `self_envelope` / `force_envelope` override), halting with `L1_MCP_BLOCK:<rule>` on stale epochs, replayed nonces, or HMAC/SHA-256 tampering (`invariant = "mcp_payload_and_epoch_binding"`).
- **Downstream Post-Run Telemetry (`stepbot -> lab-feet -> lab-attrib`)**: Emits `stepbot_execution` (`dispatch_receipt`, `protocol_sha256`, `mcp_envelope`, `next_deck_epoch`, and structured `telemetry` containing `well_volumes_ul`, `well_levels_ul`, `tips_consumed`, `tip_telemetry`, and Poiseuille/rheology `rheology_and_pressure` diagnostics), merges non-clobbering L1 fields into `assay_telemetry`, updates bounded `consumed_nonces`, and advances `deck_epoch` for [`lab-feet`](https://github.com/yaniguan/lab-feet) (`L0` INT8/4-bit bench runtime) and downstream post-run causal blame partner [`lab-attrib`](https://github.com/yaniguan/lab-attrib) (`L4`), which fuses robotic liquid-handling execution telemetry with NNLS precursor deconvolution to quarantine `EXECUTION_ARTIFACT` runs before SFT/RLVR memory admission.

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
- Non-finite floating-point numbers (YAML `.nan`, `.inf`, `-.inf`; JSON `NaN`, `Infinity`, `-Infinity`) are rejected across all numeric inputs (`volume`, `mix_after`, `disposal_volume`, `delay`, `pause`, `dilution_series` `factor`/`final_volume`, `liquids`, and `rheology` overrides) with `ValidationError: ... must be a finite number` (CLI exit code `1`, zero raw tracebacks), and manifests serialize strictly with `json.dumps(..., allow_nan=False)`.
- Unknown or misspelled keys (e.g. `new_tips: always` or `liquid:`) are rejected with `difflib` suggestions.
- `liquids` gives each listed well its starting volume (in uL), reagent species identity for cross-contamination checks (`allow_contamination: true` opts out when reuse is intentional), and optional `liquid_class` (`aqueous`, `viscous_glycerol`, `volatile_ethanol`, or `serum`). It is compiled to `protocol.define_liquid(...)` and `well.load_liquid(...)`, which need `apiLevel` 2.14 or newer.
- **Destination-well liquid contact & `dispense_position`**: Both `transfer` and `distribute` accept `dispense_position: "bottom" | "top"` (`default = "bottom"`). Under default `"bottom"` dispensing, dispensing into a destination well that already holds liquid (`volume > 0`) brings the tip into contact with the destination mixture and propagates that well's reagent species onto the active tip, preventing subsequent reuse of the dirty tip on a clean reagent source (unless `allow_contamination: true` or a fresh tip is picked up). Setting `dispense_position: "top"` emits `well.top()` in Python codegen so liquid is dispensed from above the meniscus without touching existing destination liquid (provided `mix_after` is not set), as shown in `examples/plate_fill.yaml` step 5.
- **Two-stage `multi_aspirate` stroke splitting**: Matches Opentrons 8.8.2 (`expand_for_volume_constraints` by `pipette.max_volume` first, then tip-aware `"max first, then remainder"` by `min(pipette.max_volume, tip.max_volume)`). `new_tip: always` picks up 1 tip per outer stroke group, `mix_after` runs after every inner stroke, and any resulting stroke below `pipette.min_volume` is rejected at compile time.
- **8-channel pipettes & liquid-class rheology**: Supports OT-2 (`p10_multi`, `p20_multi_gen2`, `p50_multi`, `p300_multi`, `p300_multi_gen2`) and Flex (`flex_8channel_50`, `flex_8channel_1000`) 8-channel pipettes targeting row-`A` column heads (`plate:A1` or `plate:col:1-3`), broadcasting volumes and lineage across all 8 rows `A..H` (or clamping to 1-well trough reservoirs) and consuming 8 tips per pickup. Setting `liquid_class` (`aqueous | viscous_glycerol | volatile_ethanol | serum`) or step `rheology` emits calibrated `flow_rate.aspirate`/`dispense`, aspiration settle `delay`, `air_gap`, `touch_tip` (skipped automatically on `touchTipDisabled` reservoirs), and nominal flow-rate restoration.
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

Primitive-action `(action, volume, well, slot)` and tip-count parity against `opentrons.simulate` `8.8.2`:
- `examples/out/dilution_series.py` -> `83` primitive actions, `3` tips (`100.0%` match)
- `examples/out/plate_fill.py` -> `117` primitive actions, `14` tips (`100.0%` match)
- `examples/out/reservoir_mix.py` -> `105` primitive actions, `13` tips (`100.0%` match)

## Differential Verification & Public Wet-Lab Assay Benchmark (`opentrons 8.8.2`)

`tests/test_sim_differential.py` and `benchmarks/public_protocols_eval.py` measure exact primitive-action `(action, volume, well, slot)` sequence equality (`pick_up_tip`, `aspirate`, `air_gap`, `dispense`, `blow_out`, `touch_tip`, `drop_tip`, `delay`) and physical tip consumption between `stepbot`'s `MockProtocolContext` + static trackers and `opentrons.simulate` `8.8.2`:

- **Seeded Differential Fuzz Suite (`tests/test_sim_differential.py`)**: **`311 / 311` (`100.0%`, `ALLOWED_DIFFERENCES = {}`)** accepted protocols across seeds `(202601, 202602, 202603)` (`16.9 s`, covering `transfer` with/without `multi_aspirate`, `distribute` with `disposal_volume == 0` and `> 0`, `mix`, `dilution_series`, `dispense_position: top|bottom`, `p20`/`p300`/`p1000` 1- & 8-channel, Flex `50`/`1000`, `200 uL` filter & `300 uL` standard tips, and rheology profiles).
- **Public Multi-Step Assay Suite (`python benchmarks/public_protocols_eval.py --require-opentrons`)**: **`36 / 36` (`100.0%`)** exact agreement across `3,607` primitive actions and `971` physical tips (`19.4 s`):

| Assay Family | Protocols | Mock Valid | `opentrons 8.8.2` Exact Action & Tip Match | Primitive Actions | Physical Tips Tracked |
|---|---:|---:|---:|---:|---:|
| Sandwich ELISA (`1ch` & `8ch`, `top` dispense, `serum` conjugate, `disposal_volume=0` stop) | 6 | 6/6 | **6/6 (100.0%)** | 480 | 170 |
| PCR / qPCR Mastermix & Template Setup (dual-pipette `p300`+`p20`, `viscous_glycerol` mix) | 6 | 6/6 | **6/6 (100.0%)** | 472 | 50 |
| Serial Dilutions & Dose-Response (`2x..10x`, `row`/`col`, `8ch` column cascade, multi-stroke) | 6 | 6/6 | **6/6 (100.0%)** | 443 | 45 |
| SPRI Magnetic Bead Cleanup (`1ch` & `8ch`, viscous beads, `volatile_ethanol` air-gap washes) | 6 | 6/6 | **6/6 (100.0%)** | 864 | 587 |
| Bradford & BCA Protein Quantification (`dilution_series` BSA curve + `serum` unknowns) | 6 | 6/6 | **6/6 (100.0%)** | 654 | 51 |
| NGS Library Prep & Dual-Index Barcoding (`OT-2` & `Flex`, settle delay + `touch_tip` ligase) | 6 | 6/6 | **6/6 (100.0%)** | 694 | 68 |
| **Total** | **36** | **36/36** | **36/36 (100.0%)** | **3,607** | **971** |

## How it works

1. **Parse and validate** (`stepbot.compiler`, `stepbot.wells`, `stepbot.catalog`):
   - Reads YAML or JSON with strict key validation and finiteness checks (`_finite_number`) on `metadata`, optional `requirements`, `deck`, `pipettes`, optional `liquids`, and `steps`.
   - Validates labware load names (with per-well capacities on mixed racks and `touchTipDisabled` quirks on reservoirs), OT-2 (`1`–`11`) and Flex (`A1`–`D4`) deck slots, well coordinates (including multi-letter rows `AA1..AF48` and explicit leading-zero diagnostics), single- and 8-channel pipette compatibility (`A..H` column-head validation and 8-tip column budgets), effective max volume (`min(pipette, tip_rack)`), and `apiLevel` format.
2. **Lower high-level steps and track well volumes + reagent lineage**:
   - Supports `transfer` (`new_tip` `once`/`always`/`never`, two-stage `multi_aspirate: true`, `dispense_position: bottom|top`, `liquid_class`/`rheology`), `distribute` (modeling Opentrons `disposal_volume` including `disposal_volume: 0` without blow-out), `mix`, `discard`, `dilution_series` (disjoint source/diluent/series check, configurable `precision`, and trash `blow_out`), `delay` (`minutes` and/or `seconds`), `pause`, and `comment`.
   - Keeps a running volume and reagent species set for every well (across all 8 `A..H` channels for multichannel mounts) and mounted tip across all steps, propagating destination-well liquid species onto the tip on `"bottom"` dispenses into non-empty wells while skipping destination contact on `"top"` dispenses without `mix_after`.
3. **Code and manifest generation** (`stepbot.codegen`):
   - Emits deterministic Python sorted by deck slot and pipette mount targeting Opentrons Python API v2 (`apiLevel` `2.16` on OT-2 or `2.19` on Flex unless overridden), lowering calibrated rheology profiles (`flow_rate`, settle `delay`, `air_gap`, `touch_tip`) and strict `allow_nan=False` JSON manifests (`--manifest`) with `source_sha256`, tip usage per rack, initial/peak/final well volumes, and step-index provenance.
4. **Simulation check** (`stepbot.checker`):
   - When `opentrons` is installed, checks `apiLevel` against the installed package's `MAX_SUPPORTED_VERSION` and runs `opentrons.simulate.simulate`.
   - Otherwise (or with `--mock`) runs a restricted AST interpreter against `MockProtocolContext` (no `exec()`/`eval()`), tracking shared tip-rack wells, two-stage stroke splitting, 8-channel `A..H` column operations, `.top()`/`.bottom()` well locations, `distribute` disposal volumes, rheology primitives, and per-well capacities.

## Tests

```bash
pytest
```

- With `opentrons 8.8.2` (`pip install -e ".[sim,dev]"`): **`278 passed`** (including `311` live differential protocols and all `5` Issue #35 Opentrons 8.8.2 parity cases).
- With the base install plus `pytest` (no `[sim]` extra): **`272 passed, 6 skipped`**.

## Limits

- Targets OT-2 (`1`–`11` + slot `12` fixed trash) and Opentrons Flex (`A1`–`D4` + `load_trash_bin`) decks across single- and 8-channel liquid-handling steps; hardware modules (temperature, shaker, thermocycler, magnetic block) and custom labware JSON files are not modeled.
- 8-channel pipettes address full 8-row (`A..H`) columns via row-`A` column heads (partial-tip pickup is not modeled).
- Real simulation via the `[sim]` extra requires `opentrons>=7.0,<9.0` (Opentrons v9+ removed OT-2 simulation from the `opentrons` package).

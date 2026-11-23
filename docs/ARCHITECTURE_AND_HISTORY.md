# `stepbot` — Architecture and History

> **Auto-Lab Ecosystem**: [`auto-lab`](https://github.com/yaniguan/auto-lab) · [Project Studio](https://yaniguan.github.io/auto-lab/)  
> **Layer**: `L1 · Protocol Compiler & Visuo-Tactile Manipulation`  
> **License**: MIT (`pyproject.toml`, `stepbot v0.1.0`)

---

## 1. Object

`stepbot` is the **Deterministic Liquid-Handling Protocol Compiler & Simulator (`L1`)** of the [Robot AI Auto-Lab](https://github.com/yaniguan/auto-lab). Within the `L0`–`L6` Brain/Body hierarchy, `stepbot` translates declarative YAML or JSON liquid-handling plans into deterministic, human-auditable **Opentrons Python API v2** scripts (`metadata = {"apiLevel": "2.16", ...}` and `def run(protocol): ...`), enforcing static compile-time invariants (labware/pipette specs, deck-slot uniqueness, well bounds, pipette min/max volumes, tip-rack budgets, and—on `origin/review-fixes`—per-well liquid volume conservation) before verifying the emitted Python code against either the official Opentrons simulator or a zero-dependency built-in mock runtime.

### Inputs and Outputs

- **Inputs**:
  - `stepbot compile`: Declarative `.yaml` / `.yml` or `.json` protocol file defining `metadata`, `deck`, `pipettes`, optional `liquids` (`origin/review-fixes`), and high-level `steps`.
  - `stepbot check`: Compiled Opentrons Python API v2 `.py` script (checked via `opentrons.simulate` when installed, or `MockProtocolContext` with `--mock` / base install).
- **Outputs**:
  - Deterministic, byte-reproducible Python protocol source written to disk (`-o`) or `stdout`.
  - `CheckResult` dataclass (`backend`, `ok`, `command_count`, `line_count`, `lines`, `errors`) and CLI exit code (`0` on valid simulation, `1` on compile/simulation error).

### Scope Boundaries

- **Supported**: Opentrons OT-2 `11`-slot deck (`1..11` plus fixed trash on slot `12`); `24` built-in labware specs (with dynamic fallback to `opentrons_shared_data` when installed); single-channel Gen1/Gen2 pipettes; `transfer`, `distribute`, `mix`, `dilution_series` macro expansion, `delay`, `pause`, and `comment`.
- **Out of scope / explicitly rejected**: Hardware modules (thermocycler, heater-shaker, temperature, magnetic), custom labware JSON files, Opentrons Flex alphanumeric deck slots (`A1..D3`), and 8-channelpipette column semantics (explicitly rejected at compile time on `origin/review-fixes`).

---

## 2. Data

### 2.1 Declarative Protocol Schema (YAML / JSON)

1. **`metadata`** (optional mapping):
   - `name` (default `"Stepbot Protocol"`), `description` (default `""`), and `apiLevel` (default `"2.16"`).
   - On `origin/review-fixes` (`ef6865f`), `apiLevel` must be a quoted `"2.<minor>"` string without leading zeros; unquoted YAML floats (where `apiLevel: 2.10` silently parses as `2.1`) are rejected with an explicit diagnostic.
2. **`deck`** (required mapping `alias -> {labware, slot}`):
   - `alias`: Valid Python identifier, not a Python keyword and not in `_RESERVED_NAMES` (`{"protocol", "metadata"}` on `main`; expanded to `{"protocol", "metadata", "liquids", "pipette_left", "pipette_right"}` on `origin/review-fixes`).
   - `slot`: Unique integer in `1..11` (`bool` explicitly excluded).
3. **`pipettes`** (required mapping `mount -> {model, tip_racks}`):
   - `mount` $\in \{\text{"left"}, \text{"right"}\}$; `tip_racks`: non-empty list of deck aliases whose `LabwareSpec.is_tiprack` is `True` (deduplicated preserving order).
4. **`liquids`** (added on `origin/review-fixes`, optional mapping `name -> {wells, volume, description?}`):
   - Requires `apiLevel >= 2.14`; assigns non-overlapping starting liquid volumes (`uL`, positive and $\le$ well `max_volume`) and compiles to `protocol.define_liquid(...)` + `well.load_liquid(...)`. Every unlisted well starts at `0.0 uL`.
5. **`steps`** (required non-empty list of single-key mappings or `{"type": kind, ...}` dicts):
   - `comment`: string shorthand or `{text | message}`
   - `delay`: numeric shorthand or `{seconds >= 0}`
   - `pause`: numeric shorthand (`-> DelayStep`) or `{message | msg}` (`-> PauseStep`)
   - `transfer`: `{src, dst, volume, pipette?, new_tip?: once|always|never, mix_after?: {reps, volume} | [reps, volume]}`
   - `distribute`: `{src, dst, volume, pipette?, new_tip?: once|never}`
   - `mix`: `{well | wells, volume, reps | repetitions, pipette?, new_tip?: once|always|never}`
   - `dilution_series`: `{source, diluent, first_well, n, factor, final_volume, direction?: row|col, mix_reps?: 3, pipette?}`

### 2.2 Built-in Labware & Pipette Catalogs (`src/stepbot/catalog.py`)

- **`LABWARE_CATALOG` (`24` standard definitions)**:
  - Well plates (`8`): Corning `96` (`360 uL`), `384` (`112 uL`), `24` (`3.4 mL`), `12` (`6.9 mL`), `6` (`16.8 mL`); NEST `96` flat (`200 uL`), `96` PCR full-skirt (`100 uL`), `96` deep-well (`2.0 mL`).
  - Reservoirs (`4`): NEST `12`-well (`15 mL`), NEST `1`-well (`195 mL`), USA Scientific `12`-well (`22 mL`), Agilent `1`-well (`290 mL`).
  - Tube racks (`5`): Opentrons `24`-tube Eppendorf/NEST `1.5 mL`, `15`-tube Falcon `15 mL`, `6`-tube Falcon `50 mL`, and mixed non-rectangular `opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical` (`A1..C2` 6×15 mL + `A3..B4` 4×50 mL).
  - Tip racks (`7`): Opentrons `96` standard (`10`, `20`, `300`, `1000 uL`) and filter (`20`, `200`, `1000 uL`).
  - Dynamic fallback: When `opentrons_shared_data` is installed, `get_labware_spec()` loads version-1 definitions on demand.
- **`PIPETTE_CATALOG` (`12` models)**:
  - Single-channel (`7`): `p10_single` (`1–10 uL`), `p20_single_gen2` (`1–20 uL`), `p50_single` (`5–50 uL`), `p300_single` (`30–300 uL`), `p300_single_gen2` (`20–300 uL`), `p1000_single` (`100–1000 uL`), `p1000_single_gen2` (`100–1000 uL`).
  - Eight-channel (`5`): `p10_multi`, `p20_multi_gen2`, `p50_multi`, `p300_multi`, `p300_multi_gen2` (`channels=8`; rejected on `origin/review-fixes` until multi-channel column semantics are modeled).

### 2.3 Bundled Reference Protocols (`examples/` & `examples/out/`)

| Example | Deck & Pipettes | High-Level Steps | Commands (`opentrons` / `mock`) |
|---|---|---|---|
| `dilution_series.yaml` | Corning 96, NEST 12-res, 300 uL tips; `p300_single_gen2` (left) | `comment` + 8-well 2-fold `dilution_series` (`100 uL` final) + `5 s` `delay` | `100` / `86` |
| `plate_fill.yaml` | Corning 96, NEST 12-res, 300 uL tips; `p300_single_gen2` (left) | `distribute` buffer `A1-A8`, `transfer` dye `A1-A4` (`always`), `transfer` water `A5-A8` (`once`), `mix` `A1-A8` (`always`), `10 s` `delay` | `131` / `116` |
| `reservoir_mix.yaml` | Corning 24, NEST 12-res, 300 & 20 uL tips; `p300` (left) + `p20` (right) | `distribute` `250 uL` water (`left`), `transfer` `15 uL` dye (`right`, `always`), `mix` `150 uL × 4` (`left`, `always`), `3 s` `pause` | `117` / `109` |

---

## 3. Model

`stepbot` is structured as a **four-stage deterministic compiler and dual-backend abstract machine** (`src/stepbot/`):

### 3.1 Stage 1 — Well & Grid Coordinate Resolution (`wells.py`)

- Accepts `<alias>:<expr>` or `<alias>.<expr>` strings and lists, rejecting tip-rack targets (`Cannot transfer liquid to or from tip rack`).
- Parses single-letter row + positive column coordinates (`_WELL_RE = r"^([A-Z]+)(\d+)$"`, enforcing `len(row_str) == 1`, i.e., `A`–`Z`).
- Expands rectangular sub-grids `start-end` (e.g., `plate:A1-B4`) in **row-major order** (`for r in r1..r2: for c in c1..c2`), raising `ValidationError` if `r1 > r2`, `c1 > c2`, or any `(r, c)` cell is missing in `well_grid` (catching invalid ranges across the jagged 10-well Falcon rack).
- `resolve_serial_wells()` computes `n` contiguous wells along `'row'` (`(r, c + idx)`) or `'col'` (`(r + idx, c)`).

### 3.2 Stage 2 — Lowering, Tip Automaton & Volume Accounting (`compiler.py`, `models.py`)

- **Typed IR (`models.py`)**: Frozen dataclasses forming the `LoweredStep` union (`CommentStep`, `DelayStep`, `PauseStep`, `PickUpTipStep`, `DropTipStep`, `TransferStep`, `DistributeStep`, `MixStep`, `DiscardStep`) bundled in `ProtocolSpec`.
- **`dilution_series` macro lowering (`_lower_dilution_series`)**:
  Given dilution factor $f > 1$, well count $n \ge 2$, and target well volume $V_{\text{final}}$:
  $$V_{\text{transfer}} = \operatorname{round}\!\left(\frac{V_{\text{final}}}{f - 1},\, 4\right), \qquad V_{\text{initial}} = \operatorname{round}(V_{\text{final}} + V_{\text{transfer}},\, 4), \qquad V_{\text{mix}} = \operatorname{clip}(V_{\text{final}},\, V_{\min},\, V_{\max})$$
  Validates $V_{\text{final}}, V_{\text{transfer}}, V_{\text{initial}} \in [V_{\min}, V_{\max}]$ and $V_{\text{initial}} \le C_{\text{well}}$, then lowers to:
  1. `DistributeStep` of $V_{\text{final}}$ diluent to `wells[1:]` (`new_tip='once'`, `1` tip),
  2. `TransferStep` of $V_{\text{initial}}$ stock to `wells[0]` (`new_tip='once'`, `1` tip),
  3. Single-tip cascade (`1` tip): `PickUpTipStep` $\to$ $(n - 1)$ chained `TransferStep(wells[i] -> wells[i+1], V_transfer, new_tip='never', mix_after=(mix_reps, V_mix))` $\to$ `DiscardStep(wells[-1] -> fixed_trash, V_transfer)` $\to$ `DropTipStep`.
- **Tip state machine (`_TipStateTracker`)**: Tracks `has_tip[mount]` and `rack_remaining[rack_alias]`. Automatically inserts `DropTipStep` before `once`/`always` operations if a tip is still mounted from an earlier `new_tip='never'` step, inserts `PickUpTipStep` on the first `never` step, enforces total tip-rack capacity across assigned racks, and flushes any mounted tips at protocol end.
- **Compile-time well volume tracker (`_WellVolumeTracker`, added on `origin/review-fixes`)**: Initializes per-well volumes from `liquids` (default `0.0 uL`) and simulates every lowered step with `_VOLUME_TOLERANCE = 1e-6`, rejecting source underflow (`volume > current`), cumulative well overfill (`current + volume > max_volume`), and insufficient liquid for `mix` or `mix_after` (`mix_volume > current`).

### 3.3 Stage 3 — Canonical Code Generation (`codegen.py`)

`render_protocol(spec)` produces deterministic Opentrons Python API v2 source:
- Emits `metadata` dictionary (`protocolName`, optional `description`, `apiLevel`).
- Emits `load_labware` calls sorted strictly by integer `slot`, `load_instrument` calls sorted strictly by `mount`, optional `liquids` definitions + `load_liquid` calls (`origin/review-fixes`), and clean Python statements with trailing-zero-stripped numeric literals (`_fmt_num`).

### 3.4 Stage 4 — Dual Simulation Checker (`checker.py`)

- **Official Opentrons Simulator (`_check_with_opentrons`)**: Invokes `opentrons.simulate.simulate` and `format_runlog` when installed (`opentrons>=7.0,<9.0`). On `origin/review-fixes`, first parses `metadata["apiLevel"]` via `ast.parse` + `ast.literal_eval` (without executing the file) and returns a clear version-mismatch diagnostic if `requested > MAX_SUPPORTED_VERSION`.
- **Built-in Mock Context (`MockProtocolContext`)**: Executes compiled `run(ctx)` against `_MockLabware`, `_MockWell` (tracking `load_liquid`, `remove_volume`, `add_volume` on `origin/review-fixes`), and `_MockInstrumentContext` (tracking tip pickup/drop, sequential tip-pool consumption, pipette capacity, `transfer`, `mix`, and batched `distribute` chunked by `max(1, int(max_volume // volume))` followed by `blow_out` to `Trash Bin on slot 12`).

---

## 4. Training

`stepbot` is a **purely symbolic, rule-based compiler and abstract interpreter** with zero learned parameters. Quality and determinism are established through automated verification and dual-backend simulation:

- **Byte-for-byte determinism**: `tests/test_examples.py` verifies `compile_file(yaml_path) == committed_py` across all three bundled protocols.
- **Test & CI progression**:
  - On `main` (`4534ff1`): `40` pytest cases across `4` test modules (`37` mock + CLI + validation tests running in `0.21 s` without `opentrons`, plus `3` real `opentrons.simulate` integration tests; `40 passed in 1.71s` in Linux `[sim,dev]` CI across Python `3.11` and `3.12`).
  - On `origin/review-fixes` (`ef6865f`): Expanded to `73` pytest cases (`70 passed, 3 skipped` without `opentrons`), adding `33` targeted tests for cumulative multi-step well overfill, dry-well aspiration, `mix`/`mix_after` exceeding well volume, overlapping `liquids` declarations, `apiLevel < 2.14` liquid gating, unquoted YAML `2.10` float rejection, environment-independent default `apiLevel`, multichannel pipette rejection, and reserved deck-alias collision prevention.

---

## 5. History

| Date (ISO-8601) | Commit | Branch / Ref | Summary of Changes |
|---|---|---|---|
| `2026-11-18 01:49:56 -0800` | `4534ff1` (`4534ff1dbcef105d9deeb9959356a02d343e2b8a`) | `main` | **Initial release (`Initial version`)**: Implemented catalog (`24` labware, `12` pipettes), well/range resolver, step lowerer (`transfer`, `distribute`, `mix`, `dilution_series`, `delay`, `pause`, `comment`), tip-state tracker, deterministic Opentrons v2 codegen, `opentrons.simulate` + `MockProtocolContext` checker, `stepbot` CLI, 3 example protocols, 40-test suite, and Python 3.11/3.12 CI matrix (`.github/workflows/ci.yml`). |
| `2026-11-18 19:14:38 -0800` | `ef6865f` (`ef6865f7ee080e02822f3402c14ea53aefb64ea6`) | `origin/review-fixes` (open PR branch off `4534ff1`) | **Determinism & volume-conservation hardening (`Fix deterministic apiLevel, well-volume checks, and tests`)**: Removed environment-dependent `select_api_level()` so compilation always emits `"2.16"` unless overridden; rejected unquoted/malformed `apiLevel`, multichannel pipettes, and reserved codegen identifiers (`liquids`, `pipette_left`, `pipette_right`); added `liquids` block (`define_liquid`/`load_liquid`), compile-time `_WellVolumeTracker`, and runtime `_MockWell` volume checks; added AST-based `apiLevel` pre-check in `checker.py`; expanded tests to `73`. |

---

## 6. Unique Properties

### Distinctive Invariants & Guarantees

1. **Environment-Independent Deterministic Codegen**: Slot-sorted deck loads, mount-sorted instruments, canonical number formatting, and (on `origin/review-fixes`) a fixed default `apiLevel = "2.16"` ensure identical YAML/JSON inputs emit identical `.py` files regardless of dict insertion order or whether `opentrons` is installed.
2. **Compile-Time Tip & Liquid Conservation**: Catches tip-rack exhaustion, unmounted-tip reuse transitions, dry-well aspiration (`origin/review-fixes`), cumulative destination overfill, and under-filled mixing *before* generating robot code, then re-verifies every primitive operation in `MockProtocolContext` or `opentrons.simulate`.
3. **YAML `2.10` Pitfall Guard (`origin/review-fixes`)**: Explicitly rejects non-string `metadata.apiLevel` values so YAML's float parser cannot silently turn Opentrons API `2.10` into `2.1`.

### Known Limitations

- **Nominal vs. disposal volume in `distribute`**: Opentrons `InstrumentContext.distribute` aspirates an extra `disposal_volume` (defaulting to `1×` pipette `min_volume`, e.g., `20 uL` on `p300_single_gen2`, hence `220 uL` instead of `200 uL` in real simulator logs and `100` vs. `86` commands on `dilution_series.py`) and blows it out to trash on every chunk; neither `_WellVolumeTracker` nor `MockProtocolContext` deducts `disposal_volume` from the source well.
- **Uniform `max_volume` per labware**: `_falcon_4x50_6x15_spec()` assigns `max_volume = 15000.0 uL` across all 10 wells, under-reporting the capacity of the four `50 mL` wells (`A3`, `B3`, `A4`, `B4`), and `get_labware_spec()` copies `first_well["totalLiquidVolume"]` to all wells.
- **No multi-aspirate volume splitting**: `transfer` volumes exceeding `pip.spec.max_volume` fail validation instead of splitting into multiple aspirate/dispense strokes as Opentrons' native `transfer()` supports.
- **Untrusted code execution in mock checker**: `_check_with_mock()` calls Python `exec()` on the target `.py` file; it is designed for locally compiled protocols, not untrusted third-party scripts.

### Ecosystem Interfaces Across Auto-Lab

- **Upstream from `L2` (`lab-ears`), `L3` (`YLM`), & `twinstep`**: Receives structured volumes, delays, and step sequences extracted from prose by [`lab-ears`](https://github.com/yaniguan/lab-ears), synthesized by [`YLM`](https://github.com/yaniguan/YLM), or stress-tested via seeded error twins in [`twinstep`](https://github.com/yaniguan/twinstep).
- **Sibling `L1` (`lab-hands`) & Orchestration (`labshift`)**: Complements [`lab-hands`](https://github.com/yaniguan/lab-hands) (articulated-arm collision-free motion & visuo-tactile vessel handling) and [`labshift`](https://github.com/yaniguan/labshift) (CP-SAT multi-instrument job scheduling across shared liquid handlers and readers).
- **Downstream / Gatekeeping with `L4` (`lab-rehearsal`, `lab-verify`)**: Acts as the concrete Opentrons code generator and static/mock verifier behind [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal)'s recall-rehearse-verify-run digital-twin loop and [`lab-verify`](https://github.com/yaniguan/lab-verify)'s default-deny protocol gate.

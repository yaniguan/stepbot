# `StepbotLean` -- Formal L1 Opentrons Protocol & Rheology Lowering Proofs

`StepbotLean` (`leanprover/lean4:v4.36.0`, zero external dependencies) machine-checks the deck-slot, tip-capacity, source-volume, and rheology-lowering invariants of `stepbot`:

- **Core Compiler Gate (`checkStepbot`, `stepbot_never_underflows_distribute_source`)**: Verifies pairwise-distinct OT-2 deck slots in `1..11`, effective pipette-tip capacity `transferVol <= min(pipMax, tipRackMax)`, `distribute` disposal conservation `n * vol + disposalVol <= sourceWellVol`, and clean tip sufficiency.
- **Rheology-Aware Liquid-Class Lowering (`checkRheologyLowering`)**: Proves air-gap tip overflow impossibility (`rheologyLowering_prevents_tip_overflow_with_air_gap`), rheology-adjusted dead-volume conservation (`rheologyLowering_conserves_rheology_dead_volume`), volatile ethanol pre-wet + trailing air gap (`volatile_ethanol_enforces_prewet_and_airgap`), 80% glycerol equilibration & blowout dwell (`viscous_glycerol_enforces_equilibration_and_blowout`), and shear-rate capping (`rheologyLowering_caps_aspiration_rate`).

## Build

```bash
cd lean
lake build
```

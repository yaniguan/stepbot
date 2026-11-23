/-!
# StepbotLean: L1 Opentrons Protocol Lowering & Rheology-Aware Liquid-Class Proofs

Formalizes the `stepbot` (Layer L1) deterministic liquid-handling compiler invariants:
1. **Deck Slot Validity & Uniqueness**: Every occupied OT-2 deck slot lies in `1..11`
   with zero slot collisions (`slotsValidAndDistinct`).
2. **Effective Pipette-Tip Capacity Cap**: `transferVol <= min(pipMax, tipRackMax)`.
3. **Distribute Disposal Conservation**: `nDistributeWells * transferVol + disposalVol <= sourceWellVol`.
4. **Rheology-Aware Liquid-Class Lowering (Idea #18)**: Enforces viscosity/shear-capped
   aspiration flow rates, `transferVol + airGap <= min(pipMax, tipRackMax)`, rheology-adjusted
   dead-volume conservation, `ethanolVolatile` pre-wet + positive trailing air gap, and
   `glycerol80pct` post-aspirate equilibration + blowout drainage dwell times.
-/
namespace StepbotLean

/-- Structured boundary violation reporting repository, rule identifier, and detail. -/
structure RegimeViolation where
  repo   : String
  rule   : String
  detail : String
  deriving DecidableEq, Repr

/-- Return the first violation in a list of `(holds, violation)` checks. -/
def firstViolation : List (Bool × RegimeViolation) → Option RegimeViolation
  | [] => none
  | (true, _) :: rest => firstViolation rest
  | (false, v) :: _ => some v

@[simp] theorem firstViolation_nil : firstViolation [] = none := rfl

@[simp] theorem firstViolation_cons (b : Bool) (v : RegimeViolation)
    (rest : List (Bool × RegimeViolation)) :
    firstViolation ((b, v) :: rest) = none ↔ b = true ∧ firstViolation rest = none := by
  cases b <;> simp [firstViolation]

theorem firstViolation_none_iff (checks : List (Bool × RegimeViolation)) :
    firstViolation checks = none ↔ ∀ p ∈ checks, p.1 = true := by
  induction checks with
  | nil => simp [firstViolation]
  | cons hd tl ih =>
    rcases hd with ⟨b, _⟩
    cases b with
    | false => simp [firstViolation]
    | true => simp [firstViolation, ih]

/-- Compile-time deck, tip-family, and volume conservation state in `0.1 uL` units. -/
structure StepbotCompileCheck where
  occupiedSlots     : List Nat    -- OT-2 deck slots 1..11
  pipMaxTenthUL     : Nat
  tipRackMaxTenthUL : Nat
  transferVol       : Nat
  nDistributeWells  : Nat         -- `1` for simple transfer, `>= 2` for `distribute`
  disposalVol       : Nat         -- blow-out / disposal reserve required by `distribute`
  sourceWellVol     : Nat
  tipsRequired      : Nat
  tipsAvailable     : Nat
  deriving DecidableEq, Repr

/-- Verify every occupied OT-2 deck slot lies in `1..11` and slots are pairwise distinct. -/
def slotsValidAndDistinct : List Nat → Bool
  | [] => true
  | s :: ss => decide (1 ≤ s ∧ s ≤ 11) && (!ss.contains s) && slotsValidAndDistinct ss

/-- Core L1 `stepbot` protocol compilation checker. -/
def checkStepbot (c : StepbotCompileCheck) : Option RegimeViolation :=
  let effMax := Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL
  firstViolation
    [ (slotsValidAndDistinct c.occupiedSlots,
        { repo := "stepbot", rule := "ot2_deck_slots_1_to_11_unique",
          detail := "labware slot out of OT-2 range 1..11 or two labwares share one slot" }),
      (decide (0 < c.transferVol ∧ c.transferVol ≤ effMax),
        { repo := "stepbot", rule := "effective_pipette_tip_volume_cap",
          detail := "transfer volume exceeds min(pipette.max_volume, tiprack.max_volume)" }),
      (decide (c.nDistributeWells * c.transferVol + c.disposalVol ≤ c.sourceWellVol),
        { repo := "stepbot", rule := "distribute_disposal_volume_conservation",
          detail := "n * vol + disposal_volume underflows source reservoir well" }),
      (decide (c.tipsRequired ≤ c.tipsAvailable),
        { repo := "stepbot", rule := "tip_rack_sufficiency",
          detail := "protocol requires more clean tips than mounted tipracks provide" }) ]

/-- Source Conservation & Tip-Cap Theorem: Accepted protocols never underflow distribute
    reservoir sources and never exceed `min(pipette.max_volume, tiprack.max_volume)`. -/
theorem stepbot_never_underflows_distribute_source (c : StepbotCompileCheck)
    (hok : checkStepbot c = none) :
    c.nDistributeWells * c.transferVol + c.disposalVol ≤ c.sourceWellVol ∧
    c.transferVol ≤ Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL := by
  unfold checkStepbot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨_, h2, h3, _⟩
  exact ⟨h3, h2.2⟩

/-- Deck & Tip-Sufficiency Theorem: Accepted protocols use valid distinct OT-2 slots
    and stay within mounted clean tip inventory. -/
theorem stepbot_slots_and_tips_valid (c : StepbotCompileCheck)
    (hok : checkStepbot c = none) :
    slotsValidAndDistinct c.occupiedSlots = true ∧ c.tipsRequired ≤ c.tipsAvailable := by
  unfold checkStepbot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨h1, _, _, h4⟩
  exact ⟨h1, h4⟩

/-- Physical liquid classes lowered by Idea #18 (`liquid_class_compiler.py`). -/
inductive LiquidClassId where
  | aqueous
  | glycerol80pct
  | dmso
  | ethanolVolatile
  | fragileCells
  deriving DecidableEq, Repr

/-- Rheology-lowered Opentrons `apiLevel 2.16` transfer check (`0.1 uL` and `ms` units). -/
structure RheologyLoweringCheck where
  base                         : StepbotCompileCheck
  liquidClass                  : LiquidClassId
  aspirateRateTenthULPerS      : Nat
  maxSafeAspirateRateTenthULPerS : Nat
  postAspirateWaitMs           : Nat
  blowoutDelayMs               : Nat
  preWetEnabled                : Bool
  airGapTenthUL                : Nat
  rheologyDeadVolTenthUL       : Nat
  deriving DecidableEq, Repr

/-- Verify rheology-aware liquid-class lowering invariants on top of `checkStepbot`. -/
def checkRheologyLowering (r : RheologyLoweringCheck) : Option RegimeViolation :=
  match checkStepbot r.base with
  | some v => some v
  | none =>
    let effMax := Nat.min r.base.pipMaxTenthUL r.base.tipRackMaxTenthUL
    firstViolation
      [ (decide (0 < r.aspirateRateTenthULPerS ∧
                 r.aspirateRateTenthULPerS ≤ r.maxSafeAspirateRateTenthULPerS),
          { repo := "stepbot", rule := "rheology_flow_and_shear_cap",
            detail := "aspiration flow rate is zero or exceeds viscosity/shear safety ceiling" }),
        (decide (r.base.transferVol + r.airGapTenthUL ≤ effMax),
          { repo := "stepbot", rule := "air_gap_tip_capacity_conservation",
            detail := "transfer volume plus trailing air_gap overflows effective tip capacity" }),
        (decide (r.base.nDistributeWells * r.base.transferVol +
                 r.base.disposalVol + r.rheologyDeadVolTenthUL ≤ r.base.sourceWellVol),
          { repo := "stepbot", rule := "rheology_dead_volume_conservation",
            detail := "transfer + disposal + rheology-adjusted dead volume underflows source well" }),
        (decide (r.liquidClass = .ethanolVolatile → (r.preWetEnabled = true ∧ 0 < r.airGapTenthUL)),
          { repo := "stepbot", rule := "volatile_ethanol_prewet_and_airgap",
            detail := "volatile ethanol requires headspace pre-wet and trailing air_gap" }),
        (decide (r.liquidClass = .glycerol80pct →
                 (1500 ≤ r.postAspirateWaitMs ∧ 800 ≤ r.blowoutDelayMs)),
          { repo := "stepbot", rule := "viscous_glycerol_equilibration_dwell",
            detail := "80% glycerol requires >= 1500 ms post-aspirate pause and >= 800 ms blowout dwell" }) ]

/-- Air-Gap Tip Safety: Lowered transfers with trailing air gaps never overflow tip capacity. -/
theorem rheologyLowering_prevents_tip_overflow_with_air_gap (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none) :
    r.base.transferVol + r.airGapTenthUL ≤ Nat.min r.base.pipMaxTenthUL r.base.tipRackMaxTenthUL := by
  unfold checkRheologyLowering at hok
  cases hb : checkStepbot r.base with
  | some v => simp [hb] at hok
  | none =>
    simp only [hb, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    exact hok.2.1

/-- Rheology Dead-Volume Conservation: Source well covers nominal dispense, disposal reserve,
    and viscosity/volatility dead-volume reserve. -/
theorem rheologyLowering_conserves_rheology_dead_volume (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none) :
    r.base.nDistributeWells * r.base.transferVol +
      r.base.disposalVol + r.rheologyDeadVolTenthUL ≤ r.base.sourceWellVol := by
  unfold checkRheologyLowering at hok
  cases hb : checkStepbot r.base with
  | some v => simp [hb] at hok
  | none =>
    simp only [hb, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    exact hok.2.2.1

/-- Volatile Anti-Drip Soundness: Accepted `ethanolVolatile` transfers always enable
    headspace pre-wetting and a strictly positive trailing `air_gap`. -/
theorem volatile_ethanol_enforces_prewet_and_airgap (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none)
    (heth : r.liquidClass = .ethanolVolatile) :
    r.preWetEnabled = true ∧ 0 < r.airGapTenthUL := by
  unfold checkRheologyLowering at hok
  cases hb : checkStepbot r.base with
  | some v => simp [hb] at hok
  | none =>
    simp only [hb, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    exact hok.2.2.2.1 heth

/-- Viscous Equilibration Soundness: Accepted `glycerol80pct` transfers guarantee
    >= 1500 ms post-aspirate pressure equilibration and >= 800 ms blowout drainage delay. -/
theorem viscous_glycerol_enforces_equilibration_and_blowout (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none)
    (hgly : r.liquidClass = .glycerol80pct) :
    1500 ≤ r.postAspirateWaitMs ∧ 800 ≤ r.blowoutDelayMs := by
  unfold checkRheologyLowering at hok
  cases hb : checkStepbot r.base with
  | some v => simp [hb] at hok
  | none =>
    simp only [hb, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    exact hok.2.2.2.2 hgly

/-- Shear-Rate Cap Soundness: Accepted lowered transfers never exceed the class-specific
    safe aspiration rate ceiling (`fragileCells` shear cap / viscosity cap). -/
theorem rheologyLowering_caps_aspiration_rate (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none) :
    0 < r.aspirateRateTenthULPerS ∧
    r.aspirateRateTenthULPerS ≤ r.maxSafeAspirateRateTenthULPerS := by
  unfold checkRheologyLowering at hok
  cases hb : checkStepbot r.base with
  | some v => simp [hb] at hok
  | none =>
    simp only [hb, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    exact hok.1

/-! ## Compile-Time `#guard` Witnesses -/

def validStepbot : StepbotCompileCheck :=
  { occupiedSlots     := [1, 2, 4]
    pipMaxTenthUL     := 3000
    tipRackMaxTenthUL := 2000
    transferVol       := 800
    nDistributeWells  := 2
    disposalVol       := 100
    sourceWellVol     := 2500
    tipsRequired      := 4
    tipsAvailable     := 96 }

def validEthanolLowering : RheologyLoweringCheck :=
  { base                           := validStepbot
    liquidClass                    := .ethanolVolatile
    aspirateRateTenthULPerS        := 600
    maxSafeAspirateRateTenthULPerS := 650
    postAspirateWaitMs             := 100
    blowoutDelayMs                 := 100
    preWetEnabled                  := true
    airGapTenthUL                  := 150
    rheologyDeadVolTenthUL         := 300 }

def validGlycerolLowering : RheologyLoweringCheck :=
  { base                           := validStepbot
    liquidClass                    := .glycerol80pct
    aspirateRateTenthULPerS        := 150
    maxSafeAspirateRateTenthULPerS := 180
    postAspirateWaitMs             := 2000
    blowoutDelayMs                 := 1000
    preWetEnabled                  := false
    airGapTenthUL                  := 0
    rheologyDeadVolTenthUL         := 450 }

#guard checkStepbot validStepbot == none
#guard checkRheologyLowering validEthanolLowering == none
#guard checkRheologyLowering validGlycerolLowering == none

-- Duplicate deck slot `[1, 2, 2]` or out-of-range slot `12` is rejected
#guard (checkStepbot { validStepbot with occupiedSlots := [1, 2, 2] }).isSome == true
#guard (checkStepbot { validStepbot with occupiedSlots := [1, 12] }).isSome == true

-- Distribute disposal volume underflowing source reservoir (`2*800 + 1000 > 2500`) is rejected
#guard (checkStepbot { validStepbot with disposalVol := 1000 }).isSome == true

-- Ethanol without trailing air_gap (`airGapTenthUL = 0`) is rejected
#guard (checkRheologyLowering { validEthanolLowering with airGapTenthUL := 0 }).isSome == true

-- Ethanol whose `transferVol + airGap` overflows `min(3000, 2000) = 2000` is rejected
#guard (checkRheologyLowering { validEthanolLowering with airGapTenthUL := 1300 }).isSome == true

-- 80% glycerol without post-aspirate equilibration pause is rejected
#guard (checkRheologyLowering { validGlycerolLowering with postAspirateWaitMs := 200 }).isSome == true

end StepbotLean

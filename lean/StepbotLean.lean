/-!
# StepbotLean: L1 Opentrons Protocol Lowering, Multi-Step Tip Depletion & Rheology Proofs

Formalizes the `stepbot` (Layer L1) deterministic liquid-handling compiler invariants:
1. **Deck Slot Validity & Uniqueness**: Every occupied OT-2 deck slot lies in `1..11`
   with zero collisions.
2. **Effective Pipette-Tip Capacity Cap**: `transferVol + disposalVol <= min(pipMax, tipRackMax)`.
3. **Multi-Chunk Distribute Disposal Conservation**: Splitting `nDst` wells across batches of
   `batchCount = max(1, (effMax - dispVol) / vol)` aspirates and blows out `dispVol` on every
   chunk (`nDst * vol + numChunks * dispVol <= sourceWellVol`).
4. **Inductive Multi-Step Tip-Rack Depletion & Source Conservation**: Across arbitrary sequences
   of `transfer`, `distribute`, and `mix` operations under `once | always | never` mounted-tip
   transitions and multi-rack inventories `List Nat`, cumulative tip consumption preserves exact
   rack-sum accounting (`finalState.racks.sum + finalState.tipsConsumed = initState.racks.sum`)
   and never underflows source wells or overfills destination capacity.
5. **Rheology-Aware Liquid-Class Lowering (Idea #18)**: Enforces viscosity/shear-capped
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

/-- Positive ceiling division `ceil(a / b)` on `Nat`. -/
def ceilDiv (a b : Nat) : Nat :=
  (a + b - 1) / b

/-- Wells dispensed per single multi-aspirate stroke in Opentrons `distribute`. -/
def distributeBatchSize (effMax vol dispVol : Nat) : Nat :=
  max 1 ((effMax - dispVol) / vol)

/-- Number of aspirate/blow-out chunks executed by Opentrons `distribute` across `nDst` wells. -/
def distributeChunks (effMax vol dispVol nDst : Nat) : Nat :=
  if nDst = 0 then 0 else ceilDiv nDst (distributeBatchSize effMax vol dispVol)

/-- Total source liquid (`0.1 uL` units) aspirated across all `distribute` chunks. -/
def distributeTotalAspirated (effMax vol dispVol nDst : Nat) : Nat :=
  nDst * vol + distributeChunks effMax vol dispVol nDst * dispVol

theorem distributeBatchSize_pos (effMax vol dispVol : Nat) :
    0 < distributeBatchSize effMax vol dispVol := by
  unfold distributeBatchSize
  have hle : 1 ≤ max 1 ((effMax - dispVol) / vol) := Nat.le_max_left 1 _
  omega

theorem distributeChunks_pos (effMax vol dispVol nDst : Nat) (h : 0 < nDst) :
    1 ≤ distributeChunks effMax vol dispVol nDst := by
  unfold distributeChunks ceilDiv
  have hne : nDst ≠ 0 := by omega
  simp [hne]
  have hb : 0 < distributeBatchSize effMax vol dispVol := distributeBatchSize_pos effMax vol dispVol
  have hle : distributeBatchSize effMax vol dispVol ≤ nDst + distributeBatchSize effMax vol dispVol - 1 := by
    omega
  exact Nat.div_pos hle hb

theorem single_reserve_le_distributeTotalAspirated (effMax vol dispVol nDst : Nat)
    (h : 0 < nDst) :
    nDst * vol + dispVol ≤ distributeTotalAspirated effMax vol dispVol nDst := by
  unfold distributeTotalAspirated
  have hc : 1 ≤ distributeChunks effMax vol dispVol nDst :=
    distributeChunks_pos effMax vol dispVol nDst h
  have hmul : 1 * dispVol ≤ distributeChunks effMax vol dispVol nDst * dispVol :=
    Nat.mul_le_mul_right dispVol hc
  omega

/-- Compile-time deck, tip-family, and volume conservation state in `0.1 uL` units. -/
structure StepbotCompileCheck where
  occupiedSlots     : List Nat    -- OT-2 deck slots 1..11
  pipMaxTenthUL     : Nat
  tipRackMaxTenthUL : Nat
  transferVol       : Nat
  nDistributeWells  : Nat         -- `1` for simple transfer, `>= 2` for `distribute`
  disposalVol       : Nat         -- per-chunk blow-out / disposal reserve required by `distribute`
  sourceWellVol     : Nat
  tipsRequired      : Nat
  tipsAvailable     : Nat
  deriving DecidableEq, Repr

/-- Verify every occupied OT-2 deck slot lies in `1..11` and slots are pairwise distinct. -/
def slotsValidAndDistinct : List Nat → Bool
  | [] => true
  | s :: ss => decide (1 ≤ s ∧ s ≤ 11) && (!ss.contains s) && slotsValidAndDistinct ss

/-- Core L1 `stepbot` protocol compilation checker with multi-chunk `distribute` conservation. -/
def checkStepbot (c : StepbotCompileCheck) : Option RegimeViolation :=
  let effMax := Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL
  let reqSource := distributeTotalAspirated effMax c.transferVol c.disposalVol c.nDistributeWells
  firstViolation
    [ (slotsValidAndDistinct c.occupiedSlots,
        { repo := "stepbot", rule := "ot2_deck_slots_1_to_11_unique",
          detail := "labware slot out of OT-2 range 1..11 or two labwares share one slot" }),
      (decide (0 < c.transferVol ∧ c.transferVol + c.disposalVol ≤ effMax),
        { repo := "stepbot", rule := "effective_pipette_tip_volume_cap",
          detail := "transfer volume + disposal_volume exceeds min(pipette.max_volume, tiprack.max_volume)" }),
      (decide (reqSource ≤ c.sourceWellVol),
        { repo := "stepbot", rule := "distribute_disposal_volume_conservation",
          detail := "n * vol + numChunks * disposal_volume underflows source reservoir well" }),
      (decide (c.tipsRequired ≤ c.tipsAvailable),
        { repo := "stepbot", rule := "tip_rack_sufficiency",
          detail := "protocol requires more clean tips than mounted tipracks provide" }) ]

/-- Multi-Chunk Source Conservation & Tip-Cap Theorem: Accepted protocols never underflow
    multi-chunk `distribute` reservoir sources and stay within `min(pipette, tiprack)`. -/
theorem stepbot_never_underflows_distribute_source (c : StepbotCompileCheck)
    (hok : checkStepbot c = none) :
    distributeTotalAspirated (Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL)
      c.transferVol c.disposalVol c.nDistributeWells ≤ c.sourceWellVol ∧
    c.transferVol + c.disposalVol ≤ Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL ∧
    c.transferVol ≤ Nat.min c.pipMaxTenthUL c.tipRackMaxTenthUL := by
  unfold checkStepbot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨_, h2, h3, _⟩
  exact ⟨h3, h2.2, by omega⟩

/-- Deck & Tip-Sufficiency Theorem: Accepted protocols use valid distinct OT-2 slots
    and stay within mounted clean tip inventory. -/
theorem stepbot_slots_and_tips_valid (c : StepbotCompileCheck)
    (hok : checkStepbot c = none) :
    slotsValidAndDistinct c.occupiedSlots = true ∧ c.tipsRequired ≤ c.tipsAvailable := by
  unfold checkStepbot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨h1, _, _, h4⟩
  exact ⟨h1, h4⟩

/-! ## Inductive Multi-Step Tip-Rack Depletion & Source Conservation (`#31`) -/

/-- Opentrons `new_tip` lifecycle policy for lowered liquid-handling operations. -/
inductive NewTipMode where
  | once
  | always
  | never
  deriving DecidableEq, Repr

/-- High-level `stepbot` protocol operations in `0.1 uL` volume units. -/
inductive ProtocolOp where
  | transfer   (fanout : Nat) (vol : Nat) (mode : NewTipMode)
  | distribute (nDst : Nat) (vol : Nat) (dispVol : Nat) (mode : NewTipMode)
  | mix        (nWells : Nat) (vol : Nat) (reps : Nat) (mode : NewTipMode)
  deriving DecidableEq, Repr

/-- Exact clean-tip consumption and post-step `hasTip` state mirroring `_TipStateTracker`. -/
def stepTipCount (hasTip : Bool) : ProtocolOp → Nat × Bool
  | .transfer _ _ .never       => (if hasTip then 0 else 1, true)
  | .transfer _ _ .once        => (1, false)
  | .transfer fanout _ .always => (fanout, false)
  | .distribute _ _ _ .never   => (if hasTip then 0 else 1, true)
  | .distribute _ _ _ .once    => (1, false)
  | .distribute _ _ _ .always  => (1, false)
  | .mix _ _ _ .never          => (if hasTip then 0 else 1, true)
  | .mix _ _ _ .once           => (1, false)
  | .mix nWells _ _ .always    => (nWells, false)

/-- Sequential tip-rack inventory depletion mirroring `_TipStateTracker.consume_tips`. -/
def consumeRacks : List Nat → Nat → Option (List Nat)
  | racks, 0 => some racks
  | [], _ + 1 => none
  | r :: rs, needed + 1 =>
    if needed + 1 ≤ r then
      some ((r - (needed + 1)) :: rs)
    else
      match consumeRacks rs ((needed + 1) - r) with
      | some rs' => some (0 :: rs')
      | none => none

/-- Exact Rack-Sum Depletion Lemma: Successful `consumeRacks` deducts `needed` tips across racks. -/
theorem consumeRacks_sum (racks racks' : List Nat) (needed : Nat)
    (h : consumeRacks racks needed = some racks') :
    racks'.sum + needed = racks.sum := by
  induction racks generalizing racks' needed with
  | nil =>
    cases needed with
    | zero =>
      simp [consumeRacks] at h
      subst h
      omega
    | succ k =>
      simp [consumeRacks] at h
  | cons r rs ih =>
    cases needed with
    | zero =>
      simp [consumeRacks] at h
      subst h
      omega
    | succ k =>
      simp only [consumeRacks] at h
      split at h
      · simp at h
        subst h
        simp [List.sum_cons]
        omega
      · cases hrec : consumeRacks rs (k + 1 - r) with
        | none =>
          simp [hrec] at h
        | some rs' =>
          simp [hrec] at h
          subst h
          have hih := ih rs' (k + 1 - r) hrec
          simp [List.sum_cons]
          omega

/-- Source volume (`0.1 uL` units) drawn by a single `ProtocolOp`. -/
def opRequiredSource (effMax : Nat) : ProtocolOp → Nat
  | .transfer fanout vol _         => fanout * vol
  | .distribute nDst vol dispVol _ => distributeTotalAspirated effMax vol dispVol nDst
  | .mix _ _ _ _                   => 0

/-- Cumulative source volume (`0.1 uL` units) drawn across a multi-step protocol trace. -/
def opsRequiredSource (effMax : Nat) : List ProtocolOp → Nat
  | [] => 0
  | op :: ops => opRequiredSource effMax op + opsRequiredSource effMax ops

/-- Mutable deck execution state threaded across multi-step `ProtocolOp` verification. -/
structure ProtocolExecState where
  effMaxTenthUL : Nat
  racks         : List Nat
  tipsConsumed  : Nat
  hasTip        : Bool
  sourceVol     : Nat
  destVol       : Nat
  destCap       : Nat
  deriving DecidableEq, Repr

/-- Execute and verify a single `ProtocolOp` against tip-rack, pipette, and well bounds. -/
def stepProtocol (st : ProtocolExecState) (op : ProtocolOp) :
    Except RegimeViolation ProtocolExecState :=
  let (neededTips, nextHasTip) := stepTipCount st.hasTip op
  match op with
  | .transfer fanout vol _ =>
    if !(0 < fanout ∧ 0 < vol ∧ vol ≤ st.effMaxTenthUL) then
      .error { repo := "stepbot", rule := "transfer_bounds",
               detail := "transfer requires positive fanout and 0 < vol <= effMax" }
    else if !(fanout * vol ≤ st.sourceVol) then
      .error { repo := "stepbot", rule := "source_underflow",
               detail := "transfer aspirates more liquid than source well contains" }
    else if !(st.destVol + fanout * vol ≤ st.destCap) then
      .error { repo := "stepbot", rule := "dest_overfill",
               detail := "transfer exceeds destination capacity" }
    else
      match consumeRacks st.racks neededTips with
      | none =>
        .error { repo := "stepbot", rule := "tip_rack_exhausted",
                 detail := "insufficient clean tips across assigned tip racks" }
      | some racks' =>
        .ok { st with
              racks        := racks'
              tipsConsumed := st.tipsConsumed + neededTips
              hasTip       := nextHasTip
              sourceVol    := st.sourceVol - fanout * vol
              destVol      := st.destVol + fanout * vol }
  | .distribute nDst vol dispVol mode =>
    let totalAsp := distributeTotalAspirated st.effMaxTenthUL vol dispVol nDst
    if mode = .always then
      .error { repo := "stepbot", rule := "distribute_tip_mode",
               detail := "distribute supports only once or never tip modes" }
    else if !(0 < nDst ∧ 0 < vol ∧ vol + dispVol ≤ st.effMaxTenthUL) then
      .error { repo := "stepbot", rule := "distribute_bounds",
               detail := "distribute requires 0 < nDst, 0 < vol, and vol + dispVol <= effMax" }
    else if !(totalAsp ≤ st.sourceVol) then
      .error { repo := "stepbot", rule := "distribute_source_underflow",
               detail := "multi-chunk distribute underflows source reservoir" }
    else if !(st.destVol + nDst * vol ≤ st.destCap) then
      .error { repo := "stepbot", rule := "distribute_dest_overfill",
               detail := "distribute exceeds destination capacity" }
    else
      match consumeRacks st.racks neededTips with
      | none =>
        .error { repo := "stepbot", rule := "tip_rack_exhausted",
                 detail := "insufficient clean tips across assigned tip racks" }
      | some racks' =>
        .ok { st with
              racks        := racks'
              tipsConsumed := st.tipsConsumed + neededTips
              hasTip       := nextHasTip
              sourceVol    := st.sourceVol - totalAsp
              destVol      := st.destVol + nDst * vol }
  | .mix nWells vol reps _ =>
    if !(0 < nWells ∧ 0 < reps ∧ 0 < vol ∧ vol ≤ st.effMaxTenthUL) then
      .error { repo := "stepbot", rule := "mix_bounds",
               detail := "mix requires positive wells/reps and 0 < vol <= effMax" }
    else if !(vol ≤ st.destVol ∧ st.destVol ≤ st.destCap) then
      .error { repo := "stepbot", rule := "mix_exceeds_well_volume",
               detail := "mix volume exceeds current liquid in target well" }
    else
      match consumeRacks st.racks neededTips with
      | none =>
        .error { repo := "stepbot", rule := "tip_rack_exhausted",
                 detail := "insufficient clean tips across assigned tip racks" }
      | some racks' =>
        .ok { st with
              racks        := racks'
              tipsConsumed := st.tipsConsumed + neededTips
              hasTip       := nextHasTip }

/-- Fold `stepProtocol` across a sequence of lowered `ProtocolOp` steps. -/
def runProtocolSteps (st : ProtocolExecState) :
    List ProtocolOp → Except RegimeViolation ProtocolExecState
  | [] => .ok st
  | op :: ops =>
    match stepProtocol st op with
    | .error e => .error e
    | .ok st'  => runProtocolSteps st' ops

theorem stepProtocol_invariants (st st' : ProtocolExecState) (op : ProtocolOp)
    (hok : stepProtocol st op = .ok st') :
    st'.racks.sum + st'.tipsConsumed = st.racks.sum + st.tipsConsumed ∧
    st'.sourceVol + opRequiredSource st.effMaxTenthUL op = st.sourceVol ∧
    st'.effMaxTenthUL = st.effMaxTenthUL ∧
    st'.destCap = st.destCap ∧
    st'.destVol ≤ st'.destCap := by
  cases op with
  | transfer fanout vol mode =>
    simp only [stepProtocol] at hok
    split at hok
    · cases hok
    · split at hok
      · cases hok
      · rename_i _ hsrc
        split at hok
        · cases hok
        · rename_i hdst
          cases hcr : consumeRacks st.racks (stepTipCount st.hasTip (.transfer fanout vol mode)).1 with
          | none => simp [hcr] at hok
          | some racks' =>
            simp [hcr] at hok
            subst hok
            have hsum := consumeRacks_sum st.racks racks' _ hcr
            simp at hsrc hdst
            dsimp only [opRequiredSource]
            exact ⟨by omega, by omega, rfl, rfl, hdst⟩
  | distribute nDst vol dispVol mode =>
    simp only [stepProtocol] at hok
    split at hok
    · cases hok
    · split at hok
      · cases hok
      · split at hok
        · cases hok
        · rename_i _ _ hsrc
          split at hok
          · cases hok
          · rename_i hdst
            cases hcr : consumeRacks st.racks (stepTipCount st.hasTip (.distribute nDst vol dispVol mode)).1 with
            | none => simp [hcr] at hok
            | some racks' =>
              simp [hcr] at hok
              subst hok
              have hsum := consumeRacks_sum st.racks racks' _ hcr
              simp at hsrc hdst
              dsimp only [opRequiredSource]
              exact ⟨by omega, by omega, rfl, rfl, hdst⟩
  | mix nWells vol reps mode =>
    simp only [stepProtocol] at hok
    split at hok
    · cases hok
    · split at hok
      · cases hok
      · rename_i _ hcap
        cases hcr : consumeRacks st.racks (stepTipCount st.hasTip (.mix nWells vol reps mode)).1 with
        | none => simp [hcr] at hok
        | some racks' =>
          simp [hcr] at hok
          subst hok
          have hsum := consumeRacks_sum st.racks racks' _ hcr
          simp at hcap
          dsimp only [opRequiredSource]
          exact ⟨by omega, by omega, rfl, rfl, hcap.2⟩

theorem runProtocolSteps_invariants (initState finalState : ProtocolExecState)
    (ops : List ProtocolOp)
    (hcap0 : initState.destVol ≤ initState.destCap)
    (hok : runProtocolSteps initState ops = .ok finalState) :
    finalState.racks.sum + finalState.tipsConsumed =
      initState.racks.sum + initState.tipsConsumed ∧
    finalState.sourceVol + opsRequiredSource initState.effMaxTenthUL ops =
      initState.sourceVol ∧
    finalState.effMaxTenthUL = initState.effMaxTenthUL ∧
    finalState.destVol ≤ finalState.destCap := by
  induction ops generalizing initState with
  | nil =>
    simp [runProtocolSteps] at hok
    subst hok
    simp [opsRequiredSource, hcap0]
  | cons op tl ih =>
    simp only [runProtocolSteps] at hok
    cases hst : stepProtocol initState op with
    | error e => simp [hst] at hok
    | ok mid =>
      simp [hst] at hok
      obtain ⟨hrk1, hsrc1, heff1, _, hdst1⟩ := stepProtocol_invariants initState mid op hst
      obtain ⟨hrk2, hsrc2, heff2, hdst2⟩ := ih mid hdst1 hok
      rw [heff1] at hsrc2 heff2
      simp only [opsRequiredSource]
      exact ⟨by omega, by omega, heff2, hdst2⟩

/-- Multi-Step Cumulative Tip-Rack Conservation Theorem: Starting from `tipsConsumed = 0`,
    any accepted `transfer` + `distribute` + `mix` sequence preserves
    `finalState.racks.sum + finalState.tipsConsumed = initState.racks.sum`, bounding
    cumulative tip depletion by total initial rack inventory. -/
theorem runProtocolSteps_preserves_rack_sum (initState finalState : ProtocolExecState)
    (ops : List ProtocolOp)
    (hzero : initState.tipsConsumed = 0)
    (hcap0 : initState.destVol ≤ initState.destCap)
    (hok : runProtocolSteps initState ops = .ok finalState) :
    finalState.racks.sum + finalState.tipsConsumed = initState.racks.sum ∧
    finalState.tipsConsumed ≤ initState.racks.sum := by
  obtain ⟨hrk, _, _, _⟩ := runProtocolSteps_invariants initState finalState ops hcap0 hok
  omega

/-- Multi-Step Source Conservation Theorem: Accepted multi-step traces never underflow
    the source reservoir across chained `transfer` and multi-chunk `distribute` steps. -/
theorem runProtocolSteps_never_underflows_source (initState finalState : ProtocolExecState)
    (ops : List ProtocolOp)
    (hcap0 : initState.destVol ≤ initState.destCap)
    (hok : runProtocolSteps initState ops = .ok finalState) :
    opsRequiredSource initState.effMaxTenthUL ops ≤ initState.sourceVol ∧
    finalState.sourceVol + opsRequiredSource initState.effMaxTenthUL ops = initState.sourceVol ∧
    finalState.destVol ≤ finalState.destCap := by
  obtain ⟨_, hsrc, _, hdst⟩ := runProtocolSteps_invariants initState finalState ops hcap0 hok
  exact ⟨by omega, hsrc, hdst⟩

/-! ## Rheology-Aware Liquid-Class Lowering (Idea #18) -/

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
  base                           : StepbotCompileCheck
  liquidClass                    : LiquidClassId
  aspirateRateTenthULPerS        : Nat
  maxSafeAspirateRateTenthULPerS : Nat
  postAspirateWaitMs             : Nat
  blowoutDelayMs                 : Nat
  preWetEnabled                  : Bool
  airGapTenthUL                  : Nat
  rheologyDeadVolTenthUL         : Nat
  deriving DecidableEq, Repr

/-- Verify rheology-aware liquid-class lowering invariants on top of `checkStepbot`. -/
def checkRheologyLowering (r : RheologyLoweringCheck) : Option RegimeViolation :=
  match checkStepbot r.base with
  | some v => some v
  | none =>
    let effMax := Nat.min r.base.pipMaxTenthUL r.base.tipRackMaxTenthUL
    let baseAsp := distributeTotalAspirated effMax r.base.transferVol r.base.disposalVol r.base.nDistributeWells
    firstViolation
      [ (decide (0 < r.aspirateRateTenthULPerS ∧
                 r.aspirateRateTenthULPerS ≤ r.maxSafeAspirateRateTenthULPerS),
          { repo := "stepbot", rule := "rheology_flow_and_shear_cap",
            detail := "aspiration flow rate is zero or exceeds viscosity/shear safety ceiling" }),
        (decide (r.base.transferVol + r.airGapTenthUL ≤ effMax),
          { repo := "stepbot", rule := "air_gap_tip_capacity_conservation",
            detail := "transfer volume plus trailing air_gap overflows effective tip capacity" }),
        (decide (baseAsp + r.rheologyDeadVolTenthUL ≤ r.base.sourceWellVol),
          { repo := "stepbot", rule := "rheology_dead_volume_conservation",
            detail := "multi-chunk distribute + rheology-adjusted dead volume underflows source well" }),
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

/-- Rheology Dead-Volume Conservation: Source well covers multi-chunk nominal dispense,
    per-chunk disposal reserve, and viscosity/volatility dead-volume reserve. -/
theorem rheologyLowering_conserves_rheology_dead_volume (r : RheologyLoweringCheck)
    (hok : checkRheologyLowering r = none) :
    distributeTotalAspirated (Nat.min r.base.pipMaxTenthUL r.base.tipRackMaxTenthUL)
      r.base.transferVol r.base.disposalVol r.base.nDistributeWells +
      r.rheologyDeadVolTenthUL ≤ r.base.sourceWellVol := by
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

/-- Issue #31 reproducer: `7 x 100 uL` (`1000` tenth-uL) on `p300` (`effMax = 3000`, `dispVol = 200`)
    splits into `4` chunks (`batchCount = 2`), requiring `7800` tenth-uL (`780 uL`) > `7200` (`720 uL`). -/
def multiChunkCounterexample : StepbotCompileCheck :=
  { occupiedSlots     := [1, 2, 3]
    pipMaxTenthUL     := 3000
    tipRackMaxTenthUL := 3000
    transferVol       := 1000
    nDistributeWells  := 7
    disposalVol       := 200
    sourceWellVol     := 7200
    tipsRequired      := 1
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

def sampleMultiStepOps : List ProtocolOp :=
  [ .distribute 7 1000 200 .once,
    .transfer 8 500 .always,
    .mix 1 500 3 .never,
    .mix 1 500 3 .never,
    .mix 6 500 3 .always ]

def sampleInitExecState : ProtocolExecState :=
  { effMaxTenthUL := 3000
    racks         := [10, 96]
    tipsConsumed  := 0
    hasTip        := false
    sourceVol     := 15000
    destVol       := 0
    destCap       := 30000 }

#guard checkStepbot validStepbot == none
#guard checkRheologyLowering validEthanolLowering == none
#guard checkRheologyLowering validGlycerolLowering == none

-- Multi-chunk distribute counterexample (#31): 720 uL (7200) is rejected; 780 uL (7800) passes
#guard distributeChunks 3000 1000 200 7 == 4
#guard distributeTotalAspirated 3000 1000 200 7 == 7800
#guard (checkStepbot multiChunkCounterexample).isSome == true
#guard checkStepbot { multiChunkCounterexample with sourceWellVol := 7800 } == none

-- Multi-step cumulative tip depletion across [10, 96] -> [0, 90] (1 + 8 + 1 + 0 + 6 = 16 tips)
#guard (runProtocolSteps sampleInitExecState sampleMultiStepOps).toOption ==
  some { effMaxTenthUL := 3000
         racks         := [0, 90]
         tipsConsumed  := 16
         hasTip        := false
         sourceVol     := 3200
         destVol       := 11000
         destCap       := 30000 }

-- Insufficient second rack ([10, 5] = 15 < 16 tips) is rejected at step 5
#guard (runProtocolSteps { sampleInitExecState with racks := [10, 5] } sampleMultiStepOps).isOk == false

-- Duplicate deck slot `[1, 2, 2]` or out-of-range slot `12` is rejected
#guard (checkStepbot { validStepbot with occupiedSlots := [1, 2, 2] }).isSome == true
#guard (checkStepbot { validStepbot with occupiedSlots := [1, 12] }).isSome == true

-- Distribute disposal volume underflowing source reservoir is rejected
#guard (checkStepbot { validStepbot with disposalVol := 1000 }).isSome == true

-- Ethanol without trailing air_gap (`airGapTenthUL = 0`) is rejected
#guard (checkRheologyLowering { validEthanolLowering with airGapTenthUL := 0 }).isSome == true

-- Ethanol whose `transferVol + airGap` overflows `min(3000, 2000) = 2000` is rejected
#guard (checkRheologyLowering { validEthanolLowering with airGapTenthUL := 1300 }).isSome == true

-- 80% glycerol without post-aspirate equilibration pause is rejected
#guard (checkRheologyLowering { validGlycerolLowering with postAspirateWaitMs := 200 }).isSome == true

end StepbotLean

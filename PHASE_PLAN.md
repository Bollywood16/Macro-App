# Verdict-layer phase plan (post-BUILD.md refinement)

Tracks the lettered phases referenced in code comments across `dip_context.py`,
`agreement_engine.py`, `forecast_engine.py`, `index.html`, and
`mm-journal`/`supabase/functions/mm-journal`. Written down here so the phase
sequence survives context resets — it previously existed only in a plan-mode
conversation and had to be reconstructed from scattered `# Phase X` comments.

---

## Phase A — verdict layer (branch `phase-a-verdict-layer`)

Fixes BUILD.md §0 prime directive #3 (confidence gates presentation, never
forces a direction) as it plays out across the whole verdict pipeline.
Sub-items, per existing code comments:

- **A1–A2**: `dip_context.py` `build_verdict()` — replace the hard WAIT
  override on low confidence with `INSUFFICIENT_EVIDENCE` (no-direction
  abstention, distinct from WAIT's "evidence says don't act"). Widen
  `display_range` and add `shadow_size` instead of overriding direction.
  Give `INSUFFICIENT_EVIDENCE` its own render (`v-insufficient` /
  `--ts-insufficient`), never collapsed into WAIT's styling.
- **A3**: Tearsheet pill, Home watchlist, and the Claude handoff bundle all
  headline the *ensemble's* `recommendation_label`, never dip_context's
  verdict alone.
- **A4**: `pick_basis_horizon()` — the horizon actually carrying the
  headline call is the one with the largest directional edge among
  horizons that clear `MIN_EPISODES_FOR_SIGNAL` and aren't
  confidence-label `'low'` (ties break toward the shorter horizon).
  Replaces the old hardcoded `5` used for every ticker regardless of what
  the other horizons showed.
- **A5**: `agreement_engine.py` — `INSUFFICIENT_EVIDENCE` is a true
  abstention (`calibrated=False`, weight 0 in `score()`), not a WAIT vote;
  it can't silently outvote the ensemble.
- **A6**: dip_context's own forecast row (persisted separately so it can be
  scored on its own ledger later — see Phase D) is excluded from every
  display surface via `excludeDipContextGate()`, since it shares its
  sibling ensemble row's `ticker`/`as_of_ts` and would otherwise look like
  a second, competing "the" forecast.

**Status:** implemented in `b48ce34`. Follow-up fixed separately: the
`print_recommendation_card()` Action-log line had hardcoded `"5 trading
days"` regardless of the real `basis_h` — now reads
`recommendation_basis_horizon_days` off the result dict.

**Open question surfaced 2026-08-05 (SMH on-demand run):** A4's gate can
leave the *strongest-edge* horizon excluded because it's confidence-`low`,
handing the headline to a much shorter, noisier horizon (observed: SMH's
1d edge 0.167/moderate-confidence won over 60d's 0.150 and 126d's 0.222,
both gated `low`). Mechanically correct per the gating rule, but whether a
1-day basis horizon is the right thing to headline a
`moderate_long_candidate` tactical call is unresolved — see Phase D, which
is designated the deciding evidence for this.

## Phase B — `source` column migration

Adds a real `source` column (additive) to `forecasts` so dip_context's own
rows are identified structurally instead of by `model_version ILIKE
'mm-dipcontext%'` string-matching in `excludeDipContextGate()`. Migration
should backfill `source='dip_context'` for existing rows tagged that way.
Not started.

## Phase C — (unreserved / not yet defined)

No code comments reference a Phase C. Leaving the letter open rather than
assigning it retroactively.

## Phase D — calibration ledger

BUILD.md §3/§6 step 7: the `calibration` view (`db/003_tearsheet_layer.sql`)
compares stated confidence to realized hit-rate as `outcomes` mature. It
already groups by `(ticker, horizon_days, confidence_label)` and reports
`avg_brier` per group — so per-horizon Brier exists today.

**Spec additions (2026-08-05):**

1. **Break out sharpness per horizon, not just calibration.** Calibration
   (stated confidence vs. realized hit-rate) answers "is the model
   honest"; sharpness (how tight the stated interval/confidence actually
   is, e.g. mean `q80-q20` width or mean `confidence_score`) answers "is
   the model useful." Report both, per `horizon_days`, side by side — a
   horizon can be well-calibrated and still uselessly wide, or sharp and
   badly miscalibrated; the scorecard needs to distinguish those failure
   modes rather than collapsing to one number.
2. **The scorecard must show whether confidence-gating improves Brier.**
   Compare `avg_brier` for forecasts that cleared the confidence gate
   (`confidence_label != 'low'`, i.e. what A4's `pick_basis_horizon()`
   would have accepted as a basis horizon) against `avg_brier` for the
   ones gated out. If gating doesn't measurably improve Brier, gating on
   confidence label is theater; if it does, that's the honest
   justification for A4's design.
3. **This comparison is tagged as the deciding evidence for the
   basis-horizon rule** (A4, see Phase A's open question above). Once
   `outcomes` have matured enough rows per horizon to make the
   gated-vs-ungated Brier comparison meaningful, its result — not
   intuition — decides whether A4's gate should stay as-is, get a
   different threshold, or fall back further down the horizon list
   instead of landing on 1d when longer horizons are excluded.

Not started — blocked on outcomes maturing (BUILD.md §6 step 7: "the
single most valuable near-term action is letting the forecasts resolve,
not adding features").

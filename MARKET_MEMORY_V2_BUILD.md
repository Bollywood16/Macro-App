# Market Memory v2 — Canonical Build Spec

**This file is canonical.** It supersedes `PHASE_PLAN.md` and the phase numbering in
`BUILD.md` (which used a different §6 scheme). Where they disagree, this file wins.
`PHASE_PLAN.md` should be deleted or reduced to a one-line pointer here.

Last updated: 2026-08-05

---

## 0. STATUS — read this first

### Shipped and verified

| Commit | What |
|---|---|
| `2d18dfd` + earlier | Merge of `phase-a-verdict-layer`; **Phase A** verdict-layer fixes; **Phase B** `voter` column, live-verified |
| `ffd5644`, `e0592c7` | **Fix 1 (B3)** — replaced the broken `list_pending_outcomes` filter with a real anti-join view (the `select("*")` bug was a separate outage) |
| `2cb207a` | **Fix 2 (B8)** — all four digest-push workflows share `concurrency: group: digest-push-serializer`, plus 3-attempt retry with `pull --rebase`, `::error::` + exit 1 on exhaustion |
| `fb2bf5e` | **Fix 3 (B6)** — `trading_date` column added, derived from `provider_ts` at America/New_York; `as_of_ts` never overwritten; orphan smoketest row NULLed under a trigger-enforced invariant; `scheduler_drift_days` replaces `is_market_day` |
| `dbbee39` | **Fix 4 (B2) pt.1** — reusable `--rebuild` mode for `outcome_scoring.py`, wired into `outcome-scoring.yml` as a dispatch input |
| `20260805020129_..._uniq_index.sql` | `outcomes_forecast_uniq` migration (idempotent) — schema history now complete |
| `df400c8` | Monte Carlo seed fix — `deterministic_seed(ticker, trading_date, MODEL_VERSION)`, SHA-256 based (not Python `hash()`, which is per-process randomized) |
| `657a46c` | **Phase C1** — `data_context.py`, `DataContext` Protocol + `LiveDataContext`. No engine calls the network at compute time |
| `178d159` | Seed determinism regression test |
| `a6aa3e3` | **Fix 5 (B4+B5)** — fail-loud persistence (write-ahead staging to `data/pending_forecast_writes.jsonl`, raises `PersistenceError`) + gap detector (batch exits non-zero if `ensemble_rows_created < tickers × horizons`) |

### Permanent regression tests (all passing)

- `scripts/tests/test_no_network_calls.py` — guards the C1 seam
- `scripts/tests/test_bottom_scenarios_determinism.py`
- `scripts/tests/test_fail_loud_persistence.py`

### Open — next work

1. **C3 design writeup** (`docs/C3_DESIGN.md`) — investigation only, never ran. **Blocks C2/C3.**
2. **C2** — `replay(ticker, date)`
3. **C3** — point-in-time Parquet store, `as_of()`, git-history extraction
4. **C4** — historical backfill (dedicated GH Actions workflow)
5. **D** — scoring on the replay ledger
6. **E / F / G / H** — bottom-tell library, flush + cross-asset + ATH + committee + event mode, output rebuild, handoff

### Known open risks

- `scanner.yml` and `extension-overlay.yml` historically had **no** rebase step at all
  (`git commit && git push`). They survived only by timing luck. Fixed forward by
  `2cb207a`, but that fix has not yet been exercised against a real overlapping run.
- GitHub Actions scheduler delivers runs 55–66 minutes late routinely. `trading_date`
  now insulates the data from this; `scheduler_drift_days` measures it.
- `mm_journal()` itself is still deliberately fail-soft; it is `forecast_engine.py`'s
  call sites that now fail loud via `persist_or_raise()`.

---

## 1. What the diagnosis found (the record)

### 1.1 The forecast engine was fine. The verdict layer was the bug. (FIXED — Phase A)

Two independent models ran on the same ticker/date:

| | forecast_engine ensemble | dip_context |
|---|---|---|
| Method | `analog_positions()` nearest-neighbour on the full feature vector, unioned with `regime_pos` | narrower dip-and-regime-matched search, `EPISODE_GAP=21` |
| Sample on 7/29 | n_independent 37–42 | 9 episodes |
| Confidence fn | `compute_confidence()` | `deflated_confidence()` (small-n + multiple-comparisons penalty) |
| Output on 7/29 SMH | p_positive 0.71 (5d), 0.85 (20d), 0.83 (60d); "moderate" | "low / likely mined" |

`build_verdict()` (dip_context.py:195-218, "prime directive #3") hard-forced **WAIT**
whenever confidence was "low / likely mined", regardless of `p_positive`. `tsVerdictHTML`
rendered only that verdict.

**The logical error: the gate converted "I don't know" into "no."**

### 1.2 What the model actually said on 7/29 (SMH)

| Horizon | p_positive | q20 | q50 | confidence |
|---|---|---|---|---|
| 5d | 0.707 / 0.718 | — | — | moderate |
| 20d | 0.846 / 0.780 | +2.5% | +5.7% | moderate |
| 60d | 0.825 / 0.769 | — | +10.6% | moderate |

`recommendation_label` = `moderate_long_candidate` on every row. `warnings` empty.
`concentrated` false. At 20d the entire 20th–80th band was positive. **The record
contained no WAIT anywhere.** The call was there and it was buried.

### 1.3 Calibration — first trustworthy read (2026-08-05, post-rebuild)

677 outcomes, single `trading_date` methodology, no mixed vintages.

| Horizon | n | mean forecast | hit rate | Brier | baseline | gap | 95% CI |
|---|---|---|---|---|---|---|---|
| 1d | 377 | 0.5690 | 0.5199 | 0.2556 | 0.2496 | +0.0060 | [−0.0054, +0.0371] |
| 5d | 300 | 0.5509 | 0.4167 | 0.2700 | 0.2431 | +0.0269 | [−0.0106, +0.0796] |

Murphy decomposition:

| Horizon | reliability | **resolution** | uncertainty |
|---|---|---|---|
| 1d | 0.0084 | **0.0024** | 0.2496 |
| 5d | 0.0277 | **0.0021** | 0.2431 |

AUC: 1d 0.5178 [0.4357, 0.5967] (16 independent blocks); 5d 0.4943 [0.3763, 0.6427]
(**3 blocks**).

True sharpness (stddev of `p_positive`): 0.0780 at 1d, 0.0946 at 5d. The earlier
"sharpness" column was `mean(q80−q20)` and is now correctly labelled `interval_width`.

**Reading: "cannot yet tell," NOT "confirmed no signal."** Every CI contains zero.
Resolution ≈ 0 and AUC ≈ 0.5 would normally mean no discrimination, but with 16 and 3
independent blocks there is no power to detect any. **20d/60d/126d/252d have ZERO
resolved outcomes** — the horizons where the model's conviction actually lives, and
where the 7/29 call was made, are entirely unmeasured.

**Do not tune on this sample.** Three weeks, a drawdown, 19 correlated tickers.

**Carry forward as a hypothesis to test on the replay ledger, not to act on:** the 5d
bin table is inverted in the upper bins (0.65 bin → 35.8% observed vs 0.55 bin → 44.0%).
Most likely the drawdown interacting with the model's momentum tilt — the fixed ranking
puts semis and mega-cap tech highest and those fell. But if it persists across a proper
sample it means confidence is *anti-correlated* with accuracy, which is worse than no
signal.

### 1.4 Prior invalid figures — do not cite

The pre-rebuild numbers (mean forecast 0.541, hit rate 0.296, Brier 0.2734 vs 0.2084)
came from outcomes where **593 of 611 (97%) were scored against the wrong entry anchor**.
They are void. Forecast-side analysis (sharpness, per-ticker spreads, SMH never below
0.517) is unaffected — it reads `forecasts`, which was never corrupted.

### 1.5 What already exists (cut from the build)

- Distribution outputs: `p_positive`, `p_beat_benchmark`, `q20/q50/q80`, `expected_mae`,
  `n_independent`, `confidence_score`, `confidence_label`, `benchmark`, `model_version`,
  `voter`, `trading_date`, `scheduler_drift_days`.
- Plain-language explanation: `why_it_triggered`, `invalidation_risks`, `warnings` are
  computed on every row and already read like sentences. **Phase G is a rendering job,
  not a writing job.** (`why_it_triggered` is currently identical across all six
  horizons — needs to become horizon-specific.)
- Point-in-time archive: every commit of `data/*.json` is a timestamped, lookahead-free
  snapshot. `git log --follow` is a free PIT store for rotation/research/extension.
- `effective_price` + `quote_snapshot_id` on forecasts; `quote_snapshots.provider_ts`.

---

## 2. Decisions locked

| Decision | Value |
|---|---|
| Canonical scoring benchmark | **SPY**, always. Everything else descriptive. |
| App purpose | **Forecaster.** It predicts; it does not instruct. |
| Override model | **Advisory only.** No override concept in the app. |
| Database | **Supabase stays.** Analytics off the read path. |
| Front end | **Output layer rebuilt from scratch.** |
| Replay window | **2005–2025.** 2026 held out entirely. |
| Replay universe | **17 tickers.** XLC and XLRE excluded — insufficient history. |
| Replay horizons | **1d, 5d, 20d, 60d.** 126d/252d dropped — overlap too heavily. |
| Paid data | Flows + options — **deferred to Phase F.** |
| Logging | Paper-log every tracked ticker daily, traded or not. |

---

## 3. Non-negotiable guardrails

- **Point-in-time only.** For replay date `D`, use only data published on or before `D`.
- **Publication lag is a property of every feature.** Each declares
  `availability_lag_days`. Features whose lag exceeds the horizon are `context_only`.
- **Freeze at creation.** Forecast rows are never recomputed. Corrections are new rows.
- **No synthetic history.** Never construct a proxy series for a ticker that didn't
  exist. A pre-2018 XLC basket would encode our judgment as data and every calibration
  number would inherit it. Fewer honest series beats more invented ones.
- **2026 is held out.** Calibrate on data through 2025-12-31. **Do not tune anything so
  that 7/29/2026 prints a buy.**
- **Back up before any destructive statement.** `forecasts` is irreplaceable; `outcomes`
  is derived and rebuildable.
- **Never mix sources, vintages, voters, or market/non-market days in one calibration
  number.**
- **Report block count, not effective N.** A variance ratio estimated from 3 blocks is
  not a measurement.
- **Python/SQL computes, the LLM interprets.**
- **Two ledgers, permanently separate.** Model forecasts scored on Brier. User decisions
  scored on process. They never join.

---

## 4. Phase C — Replay harness

The only path to answering whether this model has skill. At ~4 independent 5d blocks per
month, live data will not resolve it within any useful timeframe. Replay over 2005–2025
gives thousands of blocks immediately, at every horizon.

### C1 — DataContext seam ✅ DONE (`657a46c`)

`DataContext` Protocol with `close()`, `ohlcv()`, `vix()`, `hy_oas()`, `episodes()`, and
`benchmark()`. `LiveDataContext` wraps existing fetchers with zero behavior change
(parity test: byte-identical bundle except `computed_at`). `PointInTimeDataContext`
comes in C3.

### C3 design writeup — **DO THIS FIRST** (`docs/C3_DESIGN.md`)

Investigation and proposal only. **Do not implement until reviewed.** Must cover:

1. What's actually in the git-history JSON files, and whether coverage spans all 17
   replay tickers or only rotation/research subsets.
2. Proposed Parquet schema, with `effective_date` and `processed_date` defined
   explicitly **per table** — they mean different things for prices, flows, and macro.
3. **The exact lookahead-safety guarantee for `PointInTimeDataContext`, and how it is
   TESTED rather than asserted.** This is the highest-stakes decision in the project: a
   contaminated backtest does not error, it produces excellent numbers that get trusted
   for months.

### C2 — `replay(ticker, date)`

```python
def replay(ticker: str, date: date) -> TearsheetBundle:
    """Reconstruct the bundle the app would have produced at that close, using only
    as_of() data. Deterministic: same inputs -> byte-identical output."""
```

The live path becomes `replay(ticker, today)`.

**Acceptance:** `replay('SPY', <random 2019 date>)` byte-identical when run against a
store truncated at that date.

### C3 — point-in-time store

```
data/pit/prices/{ticker}/{year}.parquet     # OHLCV, adjusted as-of-date
data/pit/flows/{ticker}/{year}.parquet      # net flow, shares out, NAV
data/pit/options/{ticker}/{year}.parquet    # EOD chain summary
data/pit/macro/{series}/{year}.parquet      # as-published vintages
```

All reads through one function; no code touches Parquet directly:

```python
def as_of(table: str, ticker: str, date: date) -> pd.DataFrame:
    """Rows whose processed_date <= date. Raises on empty."""
```

### C4 — historical backfill

**Dedicated GitHub Actions workflow** with a date-range input, chunked and resumable
(matches every other heavy job in the repo; survives session boundaries).

- 17 tickers × 2005–2025 × 4 horizons × 2 voters
- Writes to `forecasts_replay` (separate table, same schema)
- Both `voter='forecast'` and `voter='dip_context'` — this subsumes the standalone
  dip_context backfill, which should not be run separately
- Report **block count per horizon** on completion

---

## 5. Phase D — Scoring on the replay ledger

Resolver writes realized absolute return, realized return vs SPY, Brier, and **two
baselines**: unconditional base rate, and persistence. Beating "stocks go up" is
trivial; beating persistence is the bar.

Display permanently:

- **Block count** beside every headline number (not effective N)
- Reliability diagram and sharpness histogram, side by side
- **Per-horizon** calibration, sharpness, resolution, and AUC — this is the deciding
  evidence for the confidence-gating question (see below)
- **Cost of WAIT** — forward return of everything gated, and what shadow size returned
- **Gate scorecard** — `voter='forecast'` vs `voter='dip_context'` Brier, head to head.
  **Caveat, added 2026-08-05 (`docs/CREDIT_SERIES.md` §7.4):** `dip_context`'s
  confidence label is frozen at a per-ticker constant for most (ticker, day) pairs —
  14 of 17 replay tickers show it fixed at "low / likely mined" regardless of that
  day's actual dip evidence, set entirely by `decades_cap` (the ticker's listing date)
  and `depth` (which lands on 3 for the common case). A Brier comparison keyed on that
  label is partly comparing *which tickers happen to clear a fixed consistency bar*,
  not *how good the gate's judgment is on a given day*. Stratify by ticker (or at
  minimum by `decades_cap`) before drawing any conclusion from this comparison, or it
  will credit/blame the gate for something fixed before the replay window starts.
- **Regime-conditioning value** — regime-conditioned (`regime_match_depth ≥ 1`) vs.
  unconditioned (depth-0) Brier/hit-rate, **split by how common the query regime tuple
  is** (its own historical match share, tertiled — see `docs/CREDIT_SERIES.md` §6.5).
  Added 2026-08-05: a pre-C3 measurement found today's SMH tuple (`calm`/`flat`/`above`)
  matches 42% of all trading days since 2003 before gap-thinning, i.e. a 3-dimensional
  "full match" can still be a weak filter when the tuple itself is the common case.
  Aggregate regime-vs-unconditioned comparison would average that away — a common tuple
  contributing ~no information looks identical, pooled, to a rare tuple contributing a
  lot. This split is what tells them apart.
- **Per-ticker record**, on the tear sheet itself

### The open design question D must settle

On SMH, every horizon past 1d was confidence-gated to "low" — including 126d, which had
the **largest raw edge of any horizon (0.222)**. So the headline anchors to 1d, the
noisiest and least tradeable read. Per-horizon resolution on the replay ledger tells us
whether confidence-gating protects us or throws away the best edges. **Do not tune the
confidence bar by hand.**

---

## 6. Phase E — Bottom-tell feature library

Every feature registers `availability_lag_days`. Replay uses the lagged value.

### Same-day observable (lag = 0) — build first, no paid data needed

| Feature | Why it marks bottoms |
|---|---|
| `amihud_impact` (\|return\| / dollar volume) | spikes while a motivated seller works, **collapses when they finish** |
| `levered_etf_shares_out` (SOXL/SOXS) | direct footprint of forced deleveraging |
| `basket_new_low_share` (MU/WDC/STX/SNDK) | fewer names confirming = positive divergence |
| `rs_turn_vs_spy` | relative strength usually turns before price |
| `corr_spike_break` | flushes correlate to 1; the break is the turn |
| `levered_rebalance_need` | the cascade, computed rather than narrated |
| `adr_local_dislocation` | discount to local close = forced selling in the US session |
| `iv_term_slope` | backwardation that hooks over |
| `put_call_with_call_oi` | panic hedging over accumulation |
| `close_location_in_range` | heavy volume + wide range + close in upper third |
| `news_response_asymmetry` | the day it stops falling on bad news |

`amihud_impact` and `levered_etf_shares_out` are the two most likely to have flagged a
forced-seller episode in real time. Build them first.

### Lagged but usable

ETF flows (T+1 to weeks), short interest (bi-monthly ~10d), COT (weekly T+3). N-PORT and
13F are `context_only`, never timing inputs.

### Never available in time

Identity and motive of a forced seller. Model the **footprint** instead.

### Output

`bottom_tells` reported as **how many same-day tells were active and which** — never a
blended score. Plus the forward distribution conditional on that count, fit through 2025
only.

**Note:** these need the completed bar. Verify `quote_age_minutes` before computing.

---

## 7. Phase F — Flush, cross-asset, ATH, committee, event mode

**Flush detector.** Volume as percentile against the ticker's own trailing 2y, never a
multiple. Volume and flow never blend — emit a label:

| Volume z | Flow | Label |
|---|---|---|
| High | Outflows | `CAPITULATION` |
| High | Inflows | `ABSORPTION` — **this was 7/29** |
| Low | Outflows | `DRIFT_LOWER` |
| Low | Inflows | `QUIET_ACCUMULATION` |

Cause: `POSITIONING` / `EXOGENOUS_FORCED_SELLER` / `RATE_PATH` / `THESIS_RELEVANT` /
`MIXED`. Mixed returns **AMBIGUOUS**, never rounds to FLUSH.

**Cross-asset relative strength.** Fixed ladder in code, never expanded at runtime: SPY;
QQQ/RSP/IWM/MDY/IWF/IWD; parent sector; constituent basket; GLD/IEF/HYG/LQD/DXY/WTI. Per
pair per window: **beta-adjusted excess return** (regress on benchmark, use the residual
— raw spreads mostly measure beta), z-score vs its own 3–5y distribution, rolling
correlation and its change, forward distribution after prior instances.

Rotation labels: winner inflows + loser outflows = `FUNDED_ROTATION`; winner up with
loser flows flat = `DRIFT`; both inflows = `CROWDING`.

**Multiple-comparisons guardrails, mandatory.** Ladder fixed in code; print the number
of tests run beside every flag; require agreement across ≥2 windows.

**ATH module.** Days since last ATH, ATH density over 20d, distance from 20/50/200,
breadth at the high, credit direction, revision direction. `LOCAL_HIGH` only on
divergence, else `CONTINUATION`, always with the falsifying level.

**Committee.** `agreement_engine.py` holds the ballot logic. Promote the display, defer
the weights — equal and provisional until earned from per-engine Brier. Compute a
correlation matrix across members' historical votes and report the **effective number of
independent votes**; three correlated bulls is one bull. High dispersion renders both
cases side by side — never averages to "hold."

**Event-pending mode.** Two branches with their own distributions, plus the
**options-implied move** from the front straddle. A bet is only interesting where the
model's expected move differs from implied. Log the branch forecast and which branch
landed; event handicapping scored on its own ledger.

---

## 8. Phase G — Output layer rebuild

White/light theme, indigo-blue **outline** accent on the tear-sheet header,
Robinhood-clean single-focus screens, one primary action per view.

### Plain-language contract, enforced in code

1. **Every number gets a comparison.** Never "1.7x volume." Always "1.7x volume —
   heavier than 88% of days in the last two years."
2. **No jargon reaches the screen.** One `glossary.py` table is the only path from
   computed field to rendered string. A field with no entry **raises** rather than
   printing its raw name.

| Field | Rendered as |
|---|---|
| `ext200` | how stretched it is above its one-year average |
| `relvol_pct` | heaviest selling volume in about N years |
| `brier` | score 0.20 — a coin flip is 0.25, lower is better |
| `n_independent` | built from N similar past setups |
| `regime_episode_count` | only N comparable dips on record |
| `p_positive 0.44` | slightly worse than a coin flip |
| `q20/q80` | rough range |

**Label discipline.** Four separate incidents traced to fields whose names meant
something other than what they measured: `regime_episode_count` surfaced as the sample
size, `as_of_ts` treated as the trading day, `mean(q80−q20)` labelled "sharpness", and
`dip_context`'s `confidence_label` presented as a per-forecast read of today's evidence
when for most (ticker, day) pairs its value is fixed the moment the ticker is chosen —
see `docs/CREDIT_SERIES.md` §7: 14 of 17 replay tickers show the label frozen at "low /
likely mined" across every sample size from 1 to 500, because the two inputs actually
setting it (`decades_cap`, fixed at the ticker's listing date; `depth`, which lands on 3
for the common case regardless of ticker) never move on any given day, and "high" is
mathematically unreachable at `depth=3` for any ticker at all. Any field whose name
could be misread gets renamed at the source, not papered over in the UI. **General
rule from the 4th incident:** if a field's value cannot vary for a given ticker (i.e.
its range is fully determined by a ticker-level constant, not by that day's inputs), it
must be rendered as a property of the ticker (e.g. "this ticker's dip-model bar is
high") — never phrased as a finding about today's forecast specifically. `glossary.py`
should check this at the field level, not rely on each caller remembering to check.

### Card structure

```
SMH — 5 days out
Odds it's higher: 71%.  Typical move: +2.4%.  Rough range: −4% to +9%.
Strongest read is at 20 days: 85% higher, typical +5.7%.

How much to trust this:
 Trend model — 41 similar setups. Moderate confidence.
 Dip model  — only 9 comparable dips on record. Can't judge this one either way.

Why:
 • Selling volume was the heaviest in about two years — usually exhaustion.
 • Money is still flowing in while the price falls, which cuts against that.
 • Two big earnings reports land inside this window, so the range is wider than normal.

What would change my mind:
 ✗ Closes below $403 → odds drop to ~35%
 ✓ Volume stays above the 90th percentile for 3 more days
 ✗ Credit spreads widen past 380bp

What I said before: Last Tuesday, 48%. Today, 71%.
 What changed: selling volume went from the 60th to the 97th percentile.

My record here: 34 calls on SMH. When I said 60%+, it happened 58% of the time —
 I run slightly overconfident on this one.
```

Five things doing the work: a probability and a size instead of a verdict word; the
strongest horizon named rather than buried; each model labelled by the question it
answered; **the trail** (this is how 7/29 would have been caught — the volume percentile
jump would have been visible as a *change*); and the track record where the decision
happens.

`INSUFFICIENT_EVIDENCE` and `WAIT` must render differently. They mean opposite things.

**Empty and error states: never render `—`.** Render the reason and a timestamp.

---

## 9. Phase H — The handoff as a scored second forecaster

### Out

Full `tearsheet_extras`; the **trail** (prior 10 sessions and what changed); the model's
**own track record on this ticker**; active `bottom_tells`; the open question and any
pending event with its implied move; and an explicit list of what the app **cannot** see
(flow lag, positioning, narrative, who is selling).

### Back

Committee-style response plus a **required machine-readable block**:

```json
{
  "source": "llm_committee",
  "ticker": "SMH",
  "horizon_days": 5,
  "p_up_abs": 0.61,
  "p_up_vs_spy": 0.55,
  "median_move_pct": 2.4,
  "q25_pct": -4.0,
  "q75_pct": 9.0,
  "branch_probs": {"beat": 0.6, "miss": 0.4},
  "key_disagreement": "one sentence",
  "falsifiers": ["...", "...", "..."]
}
```

The app writes it to the **same forecast ledger** as `voter='llm_committee'`, matured and
scored like any other forecaster. Within months you know whether the LLM overlay beats
the engines or merely sounds better. **Handoff output that is never scored is the same
failure as a verdict that is never logged.**

### Deep-research mode

When an event is pending, the prompt switches to research mode: macro view, branch
probabilities, reasoning — bounded to what the app cannot compute. Returned branch
probabilities feed the event-pending forecast and are themselves logged and scored.

---

## 10. Execution order

| Order | Phase | Gate |
|---|---|---|
| ✅ | A — verdict layer | Live |
| ✅ | B — schema + integrity | Live |
| ✅ | C1 — DataContext seam | Parity + no-network tests pass |
| 1 | **C3 design writeup** | Reviewed and approved by Sean |
| 2 | C3 — PIT store | Lookahead-safety test passes |
| 3 | C2 — replay | Byte-identical determinism test passes |
| 4 | C4 — backfill | Block count per horizon reported |
| 5 | D — scoring | Per-horizon resolution + AUC + gate scorecard render |
| 6 | E — bottom tells | Every feature declares `availability_lag_days` |
| 7 | F — flush / cross-asset / ATH / committee / event | Test count printed with every flag |
| 8 | G — output rebuild | Glossary raises on any unmapped field |
| 9 | H — handoff round-trip | Pasted JSON lands in the ledger and matures |

---

## 11. Final acceptance

1. Journal renders Calibration and Misses, no `db_error`, all resolved outcomes shown.
2. Look-up shows prices or a stated reason — never bare dashes.
3. `replay('SMH', '2026-07-29')` runs from a store truncated at that date and returns
   `moderate_long_candidate` with the 20d read surfaced.
4. Reliability diagram and sharpness histogram render, with **block count** beside them.
5. Scorecard shows Brier against both baselines, broken out per horizon.
6. Gate scorecard shows whether forcing WAIT helped or hurt.
7. No rendered string contains a raw field name.
8. Every feature declares `availability_lag_days`; a test proves replay cannot read any
   feature earlier than its lag permits.
9. `INSUFFICIENT_EVIDENCE` and `WAIT` render differently.
10. A pasted committee JSON block is parsed, written as `voter='llm_committee'`, and
    appears in the scorecard alongside the engine's forecasts.
11. No threshold anywhere in the codebase was chosen after inspecting 7/29/2026.

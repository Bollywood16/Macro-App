# Credit-series investigation — HY OAS truncation & replacement proposal

**Status: §1's sentinel-matching bug is fixed and merged (`§0a` below). The
OAS→BAA10Y substitution, percentile threshold, rotation_engine fold-in, and
regime_model_version versioning are now implemented (`§0b` below) — committed but not
yet deployed (migration not yet run in Supabase, edge function not yet redeployed; see
§0b's deployment-order note).** Requested off the back
of `docs/C3_DESIGN.md` §2.4's finding that FRED's `BAMLH0A0HYM2` (ICE BofA US High-Yield
OAS — the credit-regime input used everywhere in this codebase) only serves data from
2023-08-07 forward. This document answers: how bad is it today, what would fix it, and
what a fix actually costs given the live/replay comparability constraint. Every number
below comes from running the actual repo code against real yfinance/FRED data on
2026-08-05, not from reading the source and inferring — commands are reproducible from
`scripts/forecast_engine.py`'s own functions.

---

## 0a. Fix #1 applied (2026-08-05) — "unknown" no longer matches "unknown"

`regime_conditioned_positions()` (`forecast_engine.py`) and `_matched_positions()`
(`dip_context.py`) now force backoff to a shallower depth whenever the **query date's
own** tuple carries `"unknown"` at the depth being tried, instead of letting Python
string equality treat two unresolved dimensions as a match. Both changed; `dip_context.py`
carries its own copy of this logic by design (self-contained, no cross-directory
imports — see its module docstring), so the fix had to land in both places or the two
voters would disagree. Regression test: `scripts/tests/test_regime_sentinel.py` (3
cases, synthetic tuples, no network) — confirms the fix fires when the current tuple is
unresolved, confirms it does **not** change anything when the current tuple is fully
known (the already-correct live case), and confirms `dip_context.py` mirrors the same
behavior. Full existing suite (5 files) still green after the change.

**Result on today's 12-ticker table, re-run after the fix — depth did NOT drop:**

| Ticker | Depth (before) | Depth (after) | Regime matches | Change |
|---|---|---|---|---|
| all 12 (SMH, SPY, QQQ, GLD, XLK, XLE, XLF, XLU, IWM, MGK, RSP, ^SOX) | 3 | **3, unchanged** | 26, unchanged | none |

**This is the honest number, but it's the opposite of what was expected going in** —
worth stating plainly rather than only reporting the number. The sentinel bug's failure
mode is specifically "the *query date's own* tuple has an unknown dimension, so it
falsely matches every other unknown date." For **every live query, the query date is
always today**, and today's credit label is always resolved (real OAS data exists from
2023-08-07 onward, which covers all of now-and-forward) — so the query tuple was never
unknown to begin with, and this fix has **zero effect on any live forecast, past or
future**. §0's "26/26, all post-2023-08-07, depth stuck at 3" finding stands exactly as
measured before — that was never the sentinel bug, it's real data scarcity (only ~3 years
of genuine credit history exists to match against), and honest scarcity doesn't get
fixed by a matching-logic patch.

**Where the fix does something, verified directly:** re-ran the SMH/2015-08-24
replay-style query from §1.3 below. Before the fix: depth 3, "35 matches" spanning
2000–2011, entirely fake (matched only on shared missing-data, not shared regime). After
the fix: **depth drops to 1** (vix-only match, 39 candidates) — the credit and spy-trend
dimensions correctly refuse to claim a match they have no basis for, and the model falls
back to the honest amount of conditioning the data actually supports. This is exactly
the replay-time contamination `docs/C3_DESIGN.md` §3.3 was warning about, now closed for
any date the fix reaches — but it only reaches query dates that themselves predate
2023-08-07, i.e. replay, not live.

**Read on the ~0 resolution hypothesis:** this fix doesn't change it, and can't — it
never touched a single live forecast. §0's live finding (26/26 confined to a 3-year
window, depth mislabeled as "3" when it's really "3, but only 3 years deep") is still
standing as the candidate explanation, unaffected by today's change. Sections 2–5 below
(BAA10Y substitution, percentile threshold, rotation_engine fold-in, versioning) are what
would actually move that number, if anything does — this fix was a correctness
prerequisite for replay, not a live-calibration lever.

---

## 0b. Items 2-5 implemented (2026-08-05)

Answers §4's open questions 2-5 with an actual implementation, not just a
recommendation. All four items moved together in one change, per §3.1's own warning
that a partial swap leaves one voter silently disagreeing with the others:

- **Source (Q2):** `research_engine.fetch_credit_spread()` fetches `BAA10Y`. The old
  `fetch_hy_oas()` is kept, unchanged, deliberately — `deployment_ladder.py`'s blowout
  guardrail still needs the real ICE OAS level, calibrated to that series' own scale
  (§3.1 already flagged this as a reason the two fetchers can't just merge).
- **Threshold (Q4):** resolved via the percentile route flagged as an option in §4,
  not the naive rescaled-absolute-cutoff route from §2.2 — `credit_regime_series()`
  classifies the 63-day change against its own trailing 1260-day (5yr) percentile
  distribution (20th/80th cuts), sidestepping the OAS-vs-BAA10Y scale mismatch
  entirely rather than retuning a borrowed cutoff.
- **rotation_engine fold-in (Q3):** done, not left as a separate track. Its own
  `fetch_hy_oas()`/`series_regime_credit()` copies are removed; it now reads credit
  through `data_context.LiveDataContext.credit_spread()` and classifies via the same
  `research_engine.credit_regime_series()` every other voter uses — one fetcher, one
  classifier, per §3.1's "would need to move together."
- **Versioning (Q5):** a dedicated `regime_model_version` column, not folded into
  `model_version` — `supabase/migrations/20260805120000_regime_model_version_column.sql`
  (additive, backfills existing rows to `regime-v0-oas-abs-0.25`, new rows get
  `regime-v1-baa10y-pctile-1260d-20-80` from `forecast_engine.REGIME_MODEL_VERSION`).
  Same shape as `trading_date`'s and `voter`'s migrations for the same reason: cheap
  now, expensive after rows accumulate under the ambiguity.

**A gap this pass found and fixed that the original proposal didn't anticipate:**
`scanner.py` calls `forecast_engine.regime_series()` directly (for its
`regime_transition` trigger) but had its own `fetch_hy_oas()` call feeding it — missed
by the rotation_engine fold-in because it isn't `rotation_engine.py`. Left as-is, it
would have fed the old truncated OAS series into the new percentile classifier, which
needs 1260 trading days of history to produce anything but `"unknown"` — since OAS only
has ~630 trading days on FRED, `scanner.py`'s credit dimension would have silently gone
permanently `"unknown"`, breaking the credit leg of `regime_transition` detection. Fixed
by pointing it at `fetch_credit_spread()` like every other caller.

**Before/after measurement — the actual point of the exercise, run against live data,
same 12 tickers and query date discipline as §1.2 (query date 2026-08-04, the most
recent close as of this run):**

| Ticker | Depth | n_regime | …pre-2023-08-07 | …post-2023-08-07 | Matched-date span |
|---|---|---|---|---|---|
| SMH | 3 | 163 | 133 | 30 | 2005-09-08 → 2026-07-27 |
| SPY | 3 | 196 | 166 | 30 | 1998-05-01 → 2026-07-27 |
| QQQ | 3 | 174 | 144 | 30 | 2004-06-15 → 2026-07-27 |
| GLD | 3 | 137 | 107 | 30 | 2010-03-24 → 2026-07-27 |
| XLK | 3 | 177 | 147 | 30 | 2004-03-29 → 2026-07-27 |
| XLE | 3 | 177 | 147 | 30 | 2004-03-29 → 2026-07-27 |
| XLF | 3 | 177 | 147 | 30 | 2004-03-29 → 2026-07-27 |
| XLU | 3 | 177 | 147 | 30 | 2004-03-29 → 2026-07-27 |
| IWM | 3 | 163 | 133 | 30 | 2005-08-31 → 2026-07-27 |
| MGK | 3 | 115 | 85 | 30 | 2013-04-01 → 2026-07-27 |
| RSP | 3 | 138 | 108 | 30 | 2009-12-22 → 2026-07-27 |
| ^SOX | 3 | 192 | 162 | 30 | 2000-03-03 → 2026-07-27 |

Current query tuple (macro-only, shared across all 12 tickers): `('calm', 'flat',
'above')`.

**Direct comparison to the pre-swap §1.2 table (26/26, zero exceptions, every match
confined to post-2023-08-07):** the pool now spans decades, not 3 years, for every
ticker whose own price history goes back far enough to have them — SPY's matched dates
now reach back to 1998, ^SOX to 2000, most sector ETFs to 2004. Depth stays at 3 (the
same "full 3-dimensional match" it reported before), but it now means what it claims to
mean: **74-85% of each ticker's regime-conditioned matches are pre-2023-08-07** (85-166
pre-cutoff matches out of 115-196 total, per ticker), genuinely conditioned on macro
similarity across multiple market cycles (2004-08 credit boom, 2008 GFC, 2011, 2015-16,
2020, 2022), not on shared recency.
**This is the change the swap bought — not a marginal improvement, the qualitative
difference §0's "26/26 confined to 3 years" finding was warning about.** The
post-2023-08-07 count moved from 26 to 30 (same window, different classifier — the
percentile method labels a handful of recent dates differently than the old absolute
±0.25pp cutoff did; a secondary effect of adopting percentile classification, not of the
source swap itself). This is a first read toward the ~0 resolution hypothesis flagged in
§0/§1.2, not a resolution of it — whether the wider regime-conditioned pool actually
moves calibration is a live-data question for after deployment, not answerable from this
measurement alone.

**Not yet done:** the migration hasn't been run against the live Supabase project, and
the edge function (`index.ts` / the `.txt` mirror, both updated identically) hasn't been
redeployed. Per the migration file's own deployment-order note, running code that sends
`regime_model_version` before the column exists would fail every `create_forecast`
call — so this is committed but must not be treated as live until 1-2 are done in order.
Also unaddressed: `rotation_engine.py`'s existing `oas_al.diff(63)` local variable
naming and the `MARKET_MEMORY_V2_BUILD.md` spec text still describing HY OAS by name in
a couple of places (cosmetic, not functional — not chased down here).

---

## 0. Summary

- The credit gap is **not a uniform "20% of history is missing" problem**. It splits
  cleanly into two different, well-quantified failure modes depending on when the query
  date falls (§1).
- For **every live query today** (query date after 2023-08-07, which is all of them,
  always, from here forward): the regime-conditioned matching layer is currently, for
  every one of 12 tickers tested, **silently and completely confined to the last ~3
  years** — 26 of 26 regime-conditioned matches, zero exceptions, regardless of ticker.
  It still reports `regime_match_depth: 3` (full 3-dimensional match, the code's own
  signal for "high confidence in this conditioning"), which is misleading — it's
  conditioning on a recency window dressed up as a macro-regime match.
- For **any future replay/backfill query on a pre-2023-08-07 date** (i.e., almost the
  entire 2005–2025 replay window): the failure mode is worse and different in kind. The
  query date's own credit label is also `"unknown"`, and `"unknown" == "unknown"` in the
  match logic — so the credit dimension doesn't narrow the pool, it **matches
  indiscriminately across 20+ years of unrelated dates**, actively pretending they share a
  credit regime when the honest answer is "we don't know." Demonstrated concretely in §1.2.
- `BAA10Y` (Moody's Baa − 10yr Treasury) is a viable substitute: 0.85 Pearson / 0.84
  Spearman correlation with OAS on the *63-day change* (the actual quantity thresholded),
  zero directional (widening↔narrowing) flips found in a full confusion-matrix check, and
  it correctly registers every major known credit-stress episode 2008–2022 (§2).
- **The swap is not a one-file change.** There are 2 independent HTTP-fetch
  implementations of HY OAS and 3 independent copies of the 0.25pp-threshold
  classification logic in this codebase today (§3.1) — all would need to move together,
  or `rotation_engine.py`'s regime read would silently disagree with
  `forecast_engine.py`'s/`dip_context.py`'s.
- **Per your constraint: this is a live production change, not a replay-only design
  choice**, and it breaks comparability between forecasts made before and after the swap
  unless versioned (§3.2).

---

## 1. (a) How the missing credit dimension is currently handled

### 1.1 Mechanism — not imputation, not an explicit drop. An accidental recency filter or an accidental universal match, depending on the query date.

`regime_series()` (`forecast_engine.py`, mirrored in `dip_context.py`'s
`_regime_series()`) does:

```python
oas_al = oas.reindex(idx).ffill()
chg = oas_al - oas_al.shift(63)
credit_lab = np.where(chg.isna(), "unknown",
                       np.where(chg > 0.25, "widening",
                                np.where(chg < -0.25, "narrowing", "flat")))
```

`oas` only has real values from 2023-08-07 forward, and `ffill()` only propagates
*forward* — it can't backfill a date before the series starts. So every date before
roughly 2023-11 (2023-08-07 plus the 63-trading-day warmup `chg` needs) gets
`credit_lab = "unknown"`. There is no imputed value and no dimension-drop flag — the
string `"unknown"` sits in the regime tuple like any other label, and Python's `==` on
strings is what decides what happens next in `regime_conditioned_positions()`:

```python
for depth in (3, 2, 1, 0):
    positions = [i for i, t in enumerate(regime_tuples) if t[:depth] == current_tuple[:depth]]
    ...
```

Two very different outcomes depending on whether the *query date itself* is before or
after 2023-08-07:

**Query date is post-2023-08-07 (every live query, always, going forward).** The
current tuple's credit label is a real value (`"flat"`, `"widening"`, or `"narrowing"`).
Any historical date with `credit_lab == "unknown"` (i.e., every pre-2023-08-07 date)
fails the `t[:depth] == current_tuple[:depth]` test at depth 3 *and* depth 2 (both include
the credit slot) — automatically, silently excluded. **Net effect: the regime-conditioned
candidate pool becomes confined to the post-2023-08-07 window**, not because 3 years is a
deliberately chosen lookback, but as an accidental side effect of the credit dimension
requiring an exact-string match against a label the older data structurally cannot have.

**Query date is pre-2023-08-07 (every replay/backfill query for ~18 of the 20
replay years).** The current tuple's own credit label is *also* `"unknown"`. Now
`"unknown" == "unknown"` evaluates `True` for every other pre-2023-08-07 date — the
credit dimension stops filtering anything and instead **falsely asserts a shared credit
regime across 20+ years of genuinely unrelated dates.** Demonstrated in §1.2.

Neither behavior is "drop the dimension" or "impute a value" in the deliberate sense the
question asks about — it's an accident of how missing-data sentinels interact with exact
tuple matching, and the two failure modes point in opposite directions (one silently
narrows, the other silently over-matches).

### 1.2 Live-query quantification (post-2023-08-07 queries) — measured across 12 tickers, today's date

Ran `regime_conditioned_positions()` and the full `run_one()`-equivalent ensemble
construction (`analog_positions()` ∪ `regime_conditioned_positions()`, thinned by
`EPISODE_GAP`) for 12 tickers spanning the replay universe, query date = 2026-08-04 (most
recent close, i.e. a live query today):

| Ticker | Depth used | Regime-conditioned matches (`n_regime`) | …pre-2023-08-07 | …post-2023-08-07 | Analog (feature) matches | Ensemble total (`n_independent`) | …pre-2023-08-07 | …post-2023-08-07 |
|---|---|---|---|---|---|---|---|---|
| SMH | 3 | 26 | **0** | 26 | 31 | 43 | 19 | 24 |
| SPY | 3 | 26 | **0** | 26 | 60 | 70 | 46 | 24 |
| QQQ | 3 | 26 | **0** | 26 | 54 | 65 | 43 | 22 |
| GLD | 3 | 26 | **0** | 26 | 14 | 38 | 12 | 26 |
| XLK | 3 | 26 | **0** | 26 | 46 | 55 | 33 | 22 |
| XLE | 3 | 26 | **0** | 26 | 25 | 49 | 23 | 26 |
| XLF | 3 | 26 | **0** | 26 | 19 | 42 | 15 | 27 |
| XLU | 3 | 26 | **0** | 26 | 31 | 52 | 27 | 25 |
| IWM | 3 | 26 | **0** | 26 | 33 | 52 | 28 | 24 |
| MGK | 3 | 26 | **0** | 26 | 44 | 57 | 34 | 23 |
| RSP | 3 | 26 | **0** | 26 | 35 | 56 | 31 | 25 |
| ^SOX | 3 | 26 | **0** | 26 | 35 | 50 | 28 | 22 |

**Every single ticker: depth stays at 3 (the code's "full confidence" match), and every
one of the 26 regime-conditioned matches is post-2023-08-07. Zero exceptions.** This isn't
ticker-specific — `regime_tuples` for the (vix, credit, spy) columns are macro-only,
independent of the ticker being scored, so the *same* 26 calendar dates show up for every
ticker whose own history covers them (all do). `MIN_REGIME_N = 8` is being cleared
comfortably (26 ≥ 8), so there's no visible signal in the output that anything is wrong —
`regime_match_depth: 3` looks identical whether those 26 matches span 20 years or 3.

The **analog/feature-matching side is a different story and mostly unaffected** — credit
isn't one of `FEATURE_FIELDS`, so it draws candidates across full history regardless
(12–60 matches per ticker here, 40–70% of them pre-2023-08-07). This is why the
**ensemble total** (what's reported as `n_independent`, e.g. the spec's "37–42" range) is
*not* dominated by the credit gap — roughly half its episodes still come from real
multi-decade history via the feature-based side.

**So the precise, calibrated finding:** the credit gap doesn't meaningfully shrink the
ensemble's *headline sample size*. It silently converts the *regime-conditioning*
contribution specifically — the piece of the methodology that's supposed to add "and this
happened when the macro backdrop looked like today," beyond what plain price-pattern
analog matching already gives — into "and this happened sometime in the last 3 years,"
for every ticker, on every query, permanently, until the underlying series either gets a
longer window or gets replaced. `dip_context.py`'s own voter is more exposed than the
ensemble: its docstring describes "a narrower dip-and-regime-matched search" as its
defining method, and the spec's own diagnosis (§1.1) already recorded it landing on only
9 episodes for SMH on 7/29 — consistent with a search that leans on the now-3-year-limited
regime-conditioned pool rather than the broader analog side.

### 1.3 Replay-query demonstration (pre-2023-08-07 queries) — concrete example

Truncated SMH's own history at 2015-08-24 (the Aug 2015 China-deval selloff) and ran the
same `regime_conditioned_positions()` call a `replay('SMH', '2015-08-24')` would make:

```
query date: 2015-08-24
current_tuple: ('stressed', 'unknown', 'below')     # credit label IS "unknown" for the query itself
depth used: 3
n positions: 35
matched dates span: 2000-10-12 -> 2011-12-08   (11.2 years)
```

35 "regime-matched" episodes, confidently reported at full depth 3, spanning
2000–2011 — every one of them matched *only* because they also happen to have no credit
data, not because they share 2015-08-24's actual credit conditions (which are genuinely
unknowable from this series, but the code doesn't say that — it reports a match). This is
the "excellent numbers, no error" contamination pattern from `docs/C3_DESIGN.md` §3.3 in
concrete form: a `regime_match_depth: 3` result that looks like the strongest possible
conditioning while carrying zero real credit information, for what would be **the
majority of 2005–2025 replay dates.**

**Implication for C2/C4, flagged for whoever picks that phase up, not decided here:**
until the credit dimension is fixed, any regime-conditioned replay statistic for a
pre-2023-08-07 query date is not "missing a feature" — it's actively asserting a false
positive match. Blocking or downgrading regime-conditioned matching to `context_only` for
those dates (mirroring `docs/C3_DESIGN.md` §2.4 option 1) is worth stronger consideration
than a "nice to have," specifically *because* of this finding, not just the abstract
missing-data argument.

---

## 2. (b) Candidate replacement: `BAA10Y`

Moody's Baa corporate bond yield minus 10-year Treasury, on FRED — free, daily, full
history from 1986-01-02, not ICE-licensed (verified not truncated, unlike every `BAML*`
series tried in `docs/C3_DESIGN.md` §2.4).

### 2.1 Correlation with OAS over the overlap window (2023-08-07 → 2026-08-03, n=746)

| Quantity | Pearson | Spearman |
|---|---|---|
| **Levels** | 0.57 | 0.52 |
| **63-trading-day change** (the actual quantity `regime_series()` thresholds) | **0.85** | **0.84** |

Level correlation is only moderate — expected, since `BAA10Y` nets out the 10yr Treasury
rate and `BAMLH0A0HYM2` doesn't share that denominator, so their *levels* respond to
different things. The *change* correlation is what matters here, since that's the only
quantity the classifier actually looks at, and 0.85/0.84 is strong.

### 2.2 Label agreement — would regime classifications flip?

Naively applying OAS's own ±0.25pp threshold to `BAA10Y` under-fires ("flat" 559 times
vs. OAS's 347) because `BAA10Y`'s 63-day changes are ~2.5× less volatile (std 0.19 vs.
0.47) — an apples-to-oranges scale mismatch, not a real disagreement. Rescaling the
threshold to match volatility (`0.25 / 2.49 ≈ 0.10`) gives a fairer comparison:

| | BAA10Y: widening | BAA10Y: flat | BAA10Y: narrowing |
|---|---|---|---|
| **OAS: widening** | 74 | 18 | 0 |
| **OAS: flat** | 63 | 221 | 63 |
| **OAS: narrowing** | 4 | 58 | **182** |

Raw agreement 69.8%. **Critically: zero cells in the off-diagonal corners** — `BAA10Y`
never calls "narrowing" when OAS says "widening" or vice versa. Every disagreement is a
one-notch miss against "flat" (a threshold-calibration question, tunable), never a sign
flip (which would be a "same-input" violation of the underlying signal, not fixable by
retuning a threshold). That's the property that actually matters for reusing this as a
regime dimension: **the two series never point in opposite directions**, they sometimes
disagree about how big a move counts as a genuine regime shift.

### 2.3 Sanity check against known credit-stress history

`BAA10Y` correctly registers every major credit event tested, by inspection:

| Episode | BAA10Y start → peak → end |
|---|---|
| 2008 GFC (Sep–Nov 2008) | 3.33 → **6.10** → 6.10 |
| 2011 Europe/US downgrade | 2.77 → 3.39 → 3.02 |
| 2015–16 oil/credit selloff | 3.21 → 3.63 → 3.55 |
| 2020 COVID crash | 2.05 → **4.31** → 3.93 |
| 2022 hike-cycle widening | 1.82 → 2.42 → 2.28 |

The 2008 and 2020 blowouts are unambiguous and large; the smaller episodes (2011, 2015–16,
2022) register directionally correct, smaller moves — consistent with `BAA10Y` being an
investment-grade spread rather than a high-yield one (it moves in the same direction as
HY OAS but with less amplitude on non-systemic stress, exactly what §2.2's confusion
matrix already showed).

**Conclusion: `BAA10Y` is a defensible substitute** — full-window coverage, strong
change-correlation, no directional flips, and it visibly tracks every major stress episode
back to the 1980s. It is not the same instrument (investment-grade vs. high-yield spread),
so this should be understood and documented as "the best available full-history proxy,"
not "the same series with more history."

---

## 3. (c) The same-input constraint — what a swap actually touches

### 3.1 This logic is not centralized today — a swap has to move 2 fetchers and 3 classifiers together

| File | Function | Role |
|---|---|---|
| `scripts/research_engine.py:157` | `fetch_hy_oas()` | HTTP fetch #1 — feeds `forecast_engine.py`, `dip_context.py` (via `data_context.py`'s `DataContext.hy_oas()`), `scanner.py`, `deployment_ladder.py` |
| `scripts/rotation_engine.py:75` | `fetch_hy_oas()` | HTTP fetch #2 — **independent copy**, same URL, feeds only `rotation_engine.py` |
| `scripts/forecast_engine.py:316` | `regime_series()` | Classifier #1 — `chg > 0.25` / `< -0.25` on a 63-bar diff |
| `engines/dip_context.py:92` | `_regime_series()` | Classifier #2 — **independent copy** of the same logic, same 0.25 threshold, by design (its own docstring: self-contained, no cross-directory imports) |
| `scripts/rotation_engine.py:106` | `series_regime_credit()` | Classifier #3 — **independent copy**, same 0.25 threshold, slightly different windowing mechanics (`iloc[-1]-iloc[-64]` vs. `.shift(63)`, immaterial difference) |

`data_context.py`'s `DataContext.hy_oas()` (the C1 seam) already centralizes *one* of the
two fetch paths — `forecast_engine.py` and `dip_context.py` both end up drawing from
`research_engine.fetch_hy_oas()` today, so a source swap there is genuinely one function
edit for those two. `rotation_engine.py` was never routed through `DataContext` (it
predates C1 and isn't in C1's scope per its own docstring — "every data-dependent call in
`forecast_engine.py`") and keeps its own fetch + its own threshold entirely separately.
**A swap that touches only `research_engine.fetch_hy_oas()` would leave
`rotation_engine.py`'s credit regime silently on the old, still-truncated series** —
worth deciding whether rotation's credit read should be pulled into the same seam as part
of this work, or left as a known, separate track.

### 3.2 The comparability problem — this is a live schema-adjacent change, not a config toggle

Swapping the series (or even just the fix for §1.1's "unknown == unknown" bug, with the
series unchanged) changes what every *future* forecast's `regime_match_depth` and
regime-conditioned sample actually means, compared to every forecast already in the
`forecasts` table. Per the spec's own guardrails — **"freeze at creation... corrections
are new rows"** and **"never mix sources, vintages, voters, or market/non-market days in
one calibration number"** — this can't be a silent behavior change to an existing
function:

- Every forecast row made *before* the swap was regime-conditioned against a
  3-year-recency-biased (or, if the "unknown==unknown" bug also gets fixed, differently-
  biased) credit read. Every row made *after* is conditioned against a genuinely different
  20-year credit history. These are not the same measurement, even though they'd sit in
  the same `forecasts` table with the same `model_version` today.
- This needs a version marker — reusing `model_version` (already versioned,
  `MODEL_VERSION_*` constants exist) or a new explicit field — so calibration/Brier
  scoring never silently pools pre-swap and post-swap regime-conditioned forecasts into
  one statistic. This mirrors exactly how `trading_date` got introduced as an additive
  column rather than rewriting `as_of_ts` in place (`fb2bf5e`, Fix 3/B6).
- Per your constraint: **replay/backfill must draw from the identical credit source and
  threshold as live**, or the two ledgers (spec §2: "two ledgers, permanently separate,"
  referring to model-vs-user, but the same reasoning applies to live-vs-replay
  comparability within the model ledger) stop being comparable to each other, defeating
  the entire point of D's gate-scorecard comparison. This means the swap has to land in
  live `forecast_engine.py`/`dip_context.py` *before or alongside* C4's backfill runs, not
  as a replay-only parameter — a live production change with its own review, not a side
  effect of the replay project.

### 3.3 What this does NOT decide

This document doesn't choose between "fix the `unknown==unknown` matching bug and keep
OAS, accepting a real 3-year-only credit dimension" vs. "swap to `BAA10Y` for full
coverage with a different instrument" vs. "do both" (fix the matching bug for correctness
regardless of source, decide on source separately). All three are live options; §4 lays
out the actual sequencing question that decision depends on.

---

## 4. Open questions for review

1. ~~**Fix the matching bug independent of the source decision?**~~ **Resolved
   2026-08-05 — fixed, see §0a.** `"unknown"` no longer matches `"unknown"` in either
   `regime_conditioned_positions()` or `dip_context._matched_positions()`. Confirmed by
   direct measurement, not assumption: zero effect on any live query (today's regime
   tuple is never itself unknown), fixes the replay-date false-match demonstrated in
   §1.3 (SMH/2015-08-24 now correctly backs off from depth 3 to depth 1 instead of
   reporting 35 fake matches). Does not address the resolution~0 hypothesis — that's
   §0's live 3-year-window finding, untouched by this fix.
2. ~~**`BAA10Y` vs. accept-the-gap-honestly.**~~ **Resolved 2026-08-05 — `BAA10Y`
   adopted, see §0b.**
3. ~~**`rotation_engine.py`'s separate copy.**~~ **Resolved 2026-08-05 — folded into
   the shared `data_context`/`research_engine` seam, see §0b.**
4. ~~**Threshold retuning.**~~ **Resolved 2026-08-05 — percentile-based (trailing
   1260-day, 20th/80th cuts), not a rescaled absolute cutoff, see §0b.**
5. ~~**Versioning mechanism.**~~ **Resolved 2026-08-05 — dedicated `regime_model_version`
   column, additive migration, see §0b.**

---

## 5. Implementation status

Fetch code, classification, rotation_engine fold-in, and the schema change have all
been made — see §0b for what and why. **Not yet done: the migration hasn't been run
against the live Supabase project and the edge function hasn't been redeployed** (§0b's
last paragraph) — this is committed code, not yet a live behavior change. C3 (the
replay/backfill design this document was originally requested alongside) remains
proposal-only, untouched by this pass.

---

## 6. Pre-C3 verification pass (2026-08-05)

Five questions asked before starting C3, all answered by running the actual live
pipeline (not by reading source and inferring) — `SMH`, query date 2026-08-05,
`current_regime = ('calm', 'flat', 'above')`. No code changed in this pass: this is
measurement only, confirming what §0b's swap actually did to the confidence machinery
it feeds.

### 6.1 Is `deflated_confidence()`'s gate now effectively dead?

**No — but its shape has changed in a way worth flagging.** `dip_context`'s own pool
for SMH today: `n=45` matched dip-and-regime episodes (44/43 after the horizon-specific
`end < n` filter, see §6.3), `consistency=0.5682`, `depth=3`, `decades=3`, `searched=1`
→ **score=0, label="low / likely mined"**. Up from the `n=9` read on 7/29, confirming
the credit fix did enlarge the pool (~5x), but the label is unchanged.

Why, precisely — not "it's still low," but the actual mechanism:

- The base formula's sample-size term (`min(1, n/12)`) **saturates at n=12**. Going
  from 9→45→163 moves this term from 0.75→1.0→1.0 — the swap bought the last quarter of
  that term and nothing more; everything past n≈12 is invisible to this factor.
- `decades = len({year // 10 for matched dates})` is **structurally capped at 3 for
  SMH forever**, not just today: the ETF launched 2000-06-05, so its own dip history can
  only ever span the 2000s/2010s/2020s — a 4th decade bucket is unreachable regardless
  of how far the credit series' own history goes back (`min(1, decades/4)` is stuck at
  0.75). This is a property of the ticker's listing date, not of the credit fix, and
  won't move with any future data change.
- With `depth=3` (`0.85³ = 0.6141`) and today's `consistency=0.5682`, the base score is
  `100 × 0.5682 × 0.6141 × 0.75 ≈ 26.2`. **This is a hard ceiling on the achievable
  score at today's consistency** — the deflation multiplier only ever multiplies this
  down further (max value 1, approached as n→∞), it can never push the product above
  26.2. Since the "moderate" cutoff is 40, **no value of n — not 163, not 10,000 —
  reaches "moderate" at today's consistency/depth/decades.** Swept `n` from 1 to 500
  holding consistency/depth/decades fixed: label never changes from "low / likely
  mined."
- What *would* move it: consistency, not n. Sweeping consistency at `n=163, depth=3,
  decades=3` (search unchanged): stays "low / likely mined" through consistency=0.75
  (score 38), crosses to "moderate" at consistency≈0.80 (score 42). Solving the base
  formula directly: `moderate` requires `consistency ≳ 40 / (100 × 0.6141 × 0.75) ≈
  0.868` at this depth/decades combination, full stop, independent of n once n clears
  the ~12 saturation point.

**Answer to "is the gate disarmed":** not by this fix. The credit fix removed the
part of the penalty that was an artifact of data scarcity (n<12), which is a real and
correct improvement — but it left the two penalties that were never about data
scarcity (depth-3's `0.85³` conjunction discount, and SMH's decades ceiling) fully
intact, and those two alone cap `dip_context`'s achievable score for any
weak-directional (`consistency` near 0.5–0.75) read on this ticker. The gate is
neither dead nor unchanged — it's now a **consistency-only gate** for tickers/regimes
like SMH's today where n and decades are no longer the binding constraint. For a
different ticker with a longer listing history (decades=4 reachable) or a stronger
directional read, the same fix could plausibly flip the label, so this reading is
SMH-specific and should be re-run per-ticker before being treated as a general
statement about the gate. **Recommend re-running this exact sweep across the other 11
tickers before Phase D treats the gate as calibrated one way or the other** — not done
here, out of scope for a single-ticker verification pass.

### 6.2 `dip_context`'s evidence_json has no sample-size fields at all — confirmed, not a naming mismatch

Traced `persist_dip_context_forecast()` (`forecast_engine.py:886`) and
`dip_context()`/`_horizon_stats()` (`engines/dip_context.py`) directly, then rebuilt
the actual persisted payload for SMH's live horizons (21, 63) to check field-by-field:

- `evidence_json` keys, verified by construction: `recommendation_label,
  source_model, shadow_size, display_range, caveat, plain, volume_forensics,
  current_drawdown_pct`. **None of `depth`, `analog_n`, `regime_n`, `earliest`,
  `latest` (or any spelling of them — `date_start`/`date_end`/`sample_size` aren't
  there either) exist in this dict.** This isn't a `NULL` read of a real column — the
  keys are simply never written.
- `regime_match_depth` (the closest thing to "depth") lives in `features_json`, not
  `evidence_json` — one level up from where the ensemble voter's equivalent field
  (`evidence_json.regime_match_depth`) lives, so a query written to read the same key
  path for both voters will silently get `NULL` for `dip_context` regardless of fix.
- `analog_n`/`regime_n` don't exist as separate concepts for this voter at all —
  `dip_context` never runs the feature-based analog model (that's `forecast_engine`'s
  ensemble only); it has exactly one pool (regime-and-dip matched positions), reported
  as `n_independent` at the **top level of the `forecasts` row**, not inside
  `evidence_json`, and not split into analog/regime components because there's only
  ever one component here.
- `earliest`/`latest` (the sample's date range): confirmed **not computed anywhere**
  in `dip_context.py`. Its `_horizon_stats()` (`engines/dip_context.py:171`) is a
  near-duplicate of `forecast_engine.horizon_stats()` but was never given the
  `date_start`/`date_end`/`distinct_regimes` computation `forecast_engine`'s version
  has (`forecast_engine.py:475-479`) — an omission, not a design choice recorded
  anywhere in the module docstring.

**A voter with no recorded sample date range can't be scored for
concentration/recency bias later, exactly as flagged.** Fixing this is a small,
additive change (mirror `forecast_engine.horizon_stats()`'s three extra fields into
`dip_context._horizon_stats()`, thread them into `persist_dip_context_forecast()`'s
`evidence_json`) but touches a live persisted schema shape, so it's flagged here for
a deliberate follow-up, not fixed inline in this verification pass.

### 6.3 `sample_size.date_end` — horizon-specific, not bucketed; the shared 2026-05-07 is coincidence, not a bug

Confirmed directly: `date_end` is `max(episode_dates)` where `episode_dates` are the
**entry dates** of whichever `ensemble_pos` positions survive `end = pos + horizon <
n` (`forecast_engine.py:452-453,477`) — computed fresh per horizon, nothing bucketed.
For SMH today:

| Horizon | n | date_end | theoretical max entry date (`pos ≤ n−1−h`) | positions excluded by this horizon's cutoff |
|---|---|---|---|---|
| 1 | 164 | 2026-07-23 | 2026-08-04 | 0 |
| 5 | 164 | 2026-07-23 | 2026-07-29 | 0 |
| 20 | 163 | **2026-05-07** | 2026-07-08 | 1 |
| 60 | 163 | **2026-05-07** | 2026-05-08 | 1 |
| 126 | 160 | 2026-01-08 | 2026-02-03 | 4 |
| 252 | 154 | 2025-07-08 | 2025-08-04 | 10 |

20d and 60d land on the identical `2026-05-07` because **the most recent actual
matched position** (an analog/regime-conditioned entry date, not an arbitrary cutoff)
happens to be 2026-05-07 for both — one single ensemble position sits between the
20d and 60d horizons' theoretical ceilings (2026-07-08 and 2026-05-08 respectively)
and gets excluded from both by the `end < n` filter, leaving the same next-most-recent
match as the reported `date_end` for both horizons. This is confirmed **not** a
bucketed/shared cutoff — the mechanism is per-horizon and per-position, it's a
coincidence of which specific dates are in the matched pool this week. The 20d pool is
not discarding two months of usable history by design; it's one real trading-day gap
in the matched-position calendar this week. Re-running this table on a different date
would very likely show 20d and 60d diverge again. Not a lookahead risk, confirmed, and
not a systematic conservatism either — no action needed.

### 6.4 `n_independent` — how far the ensemble pool actually moved

Pre-fix range cited in the spec: 37–42 (§1.2). Post-fix, SMH today, per-horizon `n`
(`ensemble_pos`-derived, i.e. analog ∪ regime, gap-thinned):

| Horizon | 1 | 5 | 20 | 60 | 126 | 252 |
|---|---|---|---|---|---|---|
| n | 164 | 164 | 163 | 163 | 160 | 154 |

Roughly **4x** the pre-fix range, not the ~15-30% bump the raw regime-pool growth
(163 vs. 26) alone would suggest — because `ensemble_pos` is the union of `analog_pos`
(n=28, unaffected by the credit fix, drawn from full multi-decade price history
regardless) and `regime_pos` (n=163, the part that moved), gap-thinned by
`EPISODE_GAP=20` trading days so overlapping matches from both models collapse into
one. The union's growth is dominated by `regime_pos` now contributing far more
distinct (non-overlapping) episodes than before, not by `analog_pos` changing at all.

### 6.5 What fraction of trading days match today's regime tuple — the loose-conditioning check

For SMH, all trading days since 2003-01-01 (n=5,935):

| Match level | Count | Share |
|---|---|---|
| Full 3-dim tuple == today's `('calm','flat','above')` | 2,504 | **42.2%** |
| ...of those, gap-thinned to independent episodes (`regime_pos`) | 163 | 2.7% |
| `vix='calm'` alone | 4,034 | 68.0% |
| `credit='flat'` alone | 3,233 | 54.5% |
| `spy_trend='above'` alone | 4,760 | 80.2% |

**Today's specific tuple is a weak filter — 42% of all days since 2003 share it before
any thinning.** Each individual dimension is doing less work than its label implies:
`spy_trend='above'` matches 80% of days outright (SPY trades above its 200dma most of
the time), `vix='calm'` matches 68%, and even `credit='flat'` (the widest of the three
credit buckets by construction — the 20th–80th percentile band, i.e. 60% of the
distribution by design) matches 54.5%, close to its theoretical maximum. **This is a
property of today's specific regime being the common one** (calm/benign-credit/
uptrend is the modal state of markets, not a rare one) — a rarer combination
(`stressed`/`widening`/`below`) would filter far more aggressively by the same
mechanism, since each of those labels covers a much smaller share of history by
construction. The 163-episode, depth-3 "full match" label is real (not a mismeasurement — it correctly
identifies 163 gap-thinned independent instances of this regime), but for *this
particular* regime it is closer to a mild recency/base-rate filter than a genuine
macro-conditioning signal, and it will not read that way from `regime_match_depth: 3`
alone, which looks identical whether the underlying tuple is common or rare.

**Recommendation for the Phase D scorecard (added to `MARKET_MEMORY_V2_BUILD.md` §5,
not decided here):** the gate-scorecard comparison needs a regime-conditioned vs.
unconditioned (depth-0) Brier/hit-rate comparison, **split by how common the query
regime tuple is** (e.g. tuple's own historical match share, tertiled) — not just
compared in aggregate. Aggregate comparison would average away exactly the effect
found here: conditioning may add real information for rare/stressed tuples while
adding ~nothing for common ones like today's, and a single pooled number can't
distinguish "the regime dimension doesn't help" from "the regime dimension only helps
when it's rare enough to matter." Tuple definition and bucket boundaries are
unchanged by this finding, per instruction — this is a measurement recommendation for
Phase D, not a redesign.

---

## 7. §6.1 escalated — `dip_context`'s confidence label is a ticker property, not a per-forecast finding

§6.1 found SMH's label frozen at "low / likely mined" across n=1..500 at today's
consistency. The question raised in response: is this SMH-specific, or does it hold
across the replay universe — and if most tickers show it, the field isn't measuring
"today's evidence" at all, it's measuring which ticker this is. Answered by running
the same sweep for all 17 replay tickers (2026-08-05, live data, no thresholds
changed):

### 7.1 Two separate ceilings, both fixed by ticker/formula structure, neither by the day's evidence

**"high" (score ≥ 70) is mathematically unreachable at `depth=3`, for any ticker, on
any day, at any consistency — not empirically rare, provably impossible.** The base
formula's non-consistency factors at depth 3 are `0.85³ × min(1, decades/4)`. Even at
the two best-case inputs simultaneously (`consistency=1.0`, the logical maximum, and
`decades≥4`, the highest any ticker's own listing history can ever supply — see table
below), the base score tops out at `100 × 1.0 × 0.6141 × 1.0 = 61.41`, and the
deflation multiplier only ever multiplies that down further (max value 1). 61.41 < 70
for every possible input. `depth=3` is exactly the depth §6.5 found live queries
landing on for common regimes — so for the queries this gate actually sees most often,
"high" is dead code, not a rare outcome.

**"moderate" (score ≥ 40) is reachable, but the bar is a fixed number per ticker,
set entirely by listing date, not by anything in the forecast being evaluated:**

| decades_cap | Tickers (17) | Min. consistency to ever reach "moderate" at depth 3 |
|---|---|---|
| 4 (listed ≤1999, so 1990s+2000s+2010s+2020s reachable) | ^SOX, SPY, QQQ, XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLU, XLB (12) | **0.651** |
| 3 (listed 2000–2009, one fewer decade reachable forever) | SMH, GLD, RSP, IWM, MGK (5) | **0.868** |

Every ticker in this app's replay universe launched after 1993, so `decades_cap=4` is
already the ceiling anyone can ever reach (a 5th decade, the 1980s, is structurally
unreachable for all 17) — this table is a complete partition, not a sample. **The
5 tickers listed 2000 or later face a permanently harder bar (0.868 vs. 0.651
consistency) that has nothing to do with their dip-and-regime evidence being weaker —
it's a side effect of when the ETF happened to launch.**

### 7.2 Empirical scope: is it frozen *today*, for real tickers, not just in the worst case?

For each ticker, swept `n` from 1 to 500 holding **today's actual observed**
consistency/depth/decades fixed (same method as §6.1's SMH check) — does the label
ever leave "low / likely mined" purely from more sample size, given what today's
evidence actually looks like?

| Ticker | decades_cap | today n | today consistency | depth | label changes across n=1..500? |
|---|---|---|---|---|---|
| SMH | 3 | 44 | 0.568 | 3 | No |
| ^SOX | 4 | 72 | 0.556 | 3 | No |
| SPY | 4 | 8 | 0.750 | 2 | **Yes** |
| QQQ | 4 | 13 | 0.539 | 3 | No |
| GLD | 3 | 59 | 0.559 | 3 | No |
| XLK | 4 | 15 | 0.533 | 3 | No |
| XLF | 4 | 21 | 0.714 | 3 | No |
| XLV | 4 | 14 | 0.929 | 3 | **Yes** |
| XLE | 4 | 68 | 0.647 | 3 | No |
| XLI | 4 | 16 | 0.563 | 1 | No |
| XLY | 4 | 12 | 0.500 | 3 | No |
| XLP | 4 | 13 | 0.615 | 1 | No |
| XLU | 4 | 23 | 0.783 | 3 | **Yes** |
| XLB | 4 | 25 | 0.720 | 3 | No |
| RSP | 3 | 8 | 0.625 | 1 | No |
| IWM | 3 | 27 | 0.519 | 3 | No |
| MGK | 3 | 9 | 0.667 | 1 | No |

**14 of 17 tickers (82%): frozen at "low / likely mined" for every n from 1 to 500,
today.** For these, no amount of additional historical sample — 10x, 100x what's
actually available — would move today's label at all, because today's own consistency
sits below that ticker's fixed bar (§7.1). The 3 exceptions (SPY, XLV, XLU) cross into
"moderate" somewhere in that range not because their evidence is unusually strong, but
because their today-consistency (0.75, 0.929, 0.783) happens to already clear their
0.651 bar — once it clears, growing `n` toward saturation (§6.1's `n≈12` term, plus the
deflation factor's own slower approach to 1) finishes the job. **All 17 tickers show
`today_label = "low / likely mined"` right now** — the sweep result is what
distinguishes "this is temporarily low and could move with more data" (3 tickers) from
"this cannot move today no matter what" (14 tickers), a distinction the label itself
does not surface.

### 7.3 This is the same class of defect as two already-named incidents, not a new kind of bug

`MARKET_MEMORY_V2_BUILD.md` §8 ("Label discipline") already names two prior incidents
where a field's name promised something its value didn't deliver:
`regime_episode_count` surfaced as if it were the sample size, `as_of_ts` treated as
if it were the trading day. `dip_context`'s `confidence_label` is a third: it's
presented per-forecast — a fresh read of today's specific dip-and-regime evidence —
but for 14 of 17 tickers today, its value is fully determined before any of that
evidence is even looked at, by two numbers that never change once a ticker is chosen
(`decades_cap`, fixed at listing; `depth`, which §6.5 shows lands on 3 for the common
case regardless of ticker). Added as incident #3 and a general rule to §8's list —
see that section directly (`MARKET_MEMORY_V2_BUILD.md` §8) rather than duplicating the
wording here.

**Not fixed in this pass** (explicitly out of scope per instruction — "do not tune
consistency thresholds to fix this"): the label's formula, the depth/decades weights,
and the "moderate"/"high" cutoffs are all unchanged. This section is measurement and a
rename/rule proposal, not a recalibration.

### 7.4 Phase D gate-scorecard implication

`MARKET_MEMORY_V2_BUILD.md` §5's existing "Gate scorecard — `voter='forecast'` vs.
`voter='dip_context'` Brier, head to head" needs a caveat attached before it's run, not
after: if `dip_context`'s confidence label is frozen at a per-ticker constant for most
(ticker, day) pairs regardless of that day's actual dip evidence, then a Brier/hit-rate
comparison keyed on that label is substantially comparing **which tickers happen to
have `decades_cap=4` vs. `3`, and which tickers' typical consistency clears 0.651 vs.
0.868** — a fixed per-ticker offset baked into the confidence machinery — not "how good
is `dip_context`'s judgment on a given day" vs. "how good is the ensemble's." The
scorecard should stratify by ticker (or at minimum by `decades_cap`) before drawing any
conclusion about which voter's *gating* is better calibrated, or it risks crediting/
blaming the gate for something that was fixed at each ticker's listing date, long
before Phase D's replay window even starts. Noted in `MARKET_MEMORY_V2_BUILD.md` §5
directly.

### 7.4b The confidence-function inversion — logged, not fixed (2026-08-05)

Per instruction: **logged in `MARKET_MEMORY_V2_BUILD.md` §5 (Phase D) as "The
confidence-function inversion," thresholds not retuned by hand.** Two structural
properties, both proven in §7.1, restated here in one place since they're the actual
defect underneath everything else in this section:

1. **The conjunction-depth penalty subtracts more than depth-3 matching can add back.**
   `depth=3`'s ceiling (61.4) sits below the "high" cutoff (70) for every ticker,
   unconditionally — the most rigorous conditioning mode available can never itself be
   reported as high-confidence, while a shallower `depth=1` match has a *higher*
   achievable ceiling (85). Backwards from what depth is supposed to reward.
2. **The "moderate" bar is a function of listing date, not evidence quality** —
   `0.651` vs. `0.868` consistency required, entirely determined by `decades_cap`.

Both are now a named Phase D question with a specific, cheap test attached (not "look
into this sometime"): restrict any label-quality check to the 3 of 17 tickers
(`SPY, XLV, XLU`) where the label is even capable of varying, and check whether it
correlates with realized outcomes there. A field frozen for 14 of 17 tickers has to
earn its place on the remaining 3 or come off the card — full reasoning and the exact
test spec are in `MARKET_MEMORY_V2_BUILD.md` §5, not duplicated here.

### 7.5 Fixed in this pass: `dip_context`'s missing sample provenance (§6.2)

Per instruction, done ahead of C4 rather than left as a flagged gap: `engines/
dip_context.py`'s `_horizon_stats()` now computes `date_start`/`date_end` from the
entry dates of whichever positions survive the horizon's `end < n` filter — the exact
definition `forecast_engine.horizon_stats()` already uses, so the two voters' date
ranges are directly comparable. `forecast_engine.persist_dip_context_forecast()`
threads this into `evidence_json.sample_size` (`{n, date_start, date_end}`) and
mirrors `regime_match_depth` into `evidence_json` alongside its existing
`features_json` copy, so a query reading `evidence_json` for depth doesn't silently
get `NULL` depending on which voter wrote the row. Verified end-to-end against live
SMH data: `evidence_json.sample_size` for the 21d horizon now reads
`{"n": 44, "date_start": "2005-10-19", "date_end": "2026-07-07"}` where before this
fix nothing at all was recorded. Full existing test suite (5 files) still green after
the change — no test covered this shape before, so nothing was broken, but nothing
caught the gap either; also worth a regression test before C4's backfill, not added
here.

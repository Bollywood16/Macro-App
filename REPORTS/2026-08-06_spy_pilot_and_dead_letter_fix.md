# SPY replay pilot — analytical results, and the Fix 5 dead-letter fix

Full detail and methodology: `docs/C3_DESIGN.md` §11 (pilot analysis) and §12
(dead-letter fix). This is the summary reported before dispatching the C4
17-ticker matrix.

**Correction (2026-08-06):** this report originally compared replay's single-ticker
5d sharpness against a pooled 19-ticker live figure and concluded the model doesn't
sharpen with regime diversity. That comparison was apples-to-oranges — corrected
below, §11.3 of `docs/C3_DESIGN.md` has the full derivation and verification.

---

## 1. SPY pilot analytical results

### `regime_match_depth` by era

Queried from the real persisted `forecasts_replay` rows, not re-derived.

| Era | depth=1 | depth=2 | depth=3 | depth<3 share |
|---|---|---|---|---|
| 2005-2006 | 34 | 0 | 469 | 6.8% |
| 2007-2008 | 23 | 12 | 469 | 6.9% |
| 2009-2010 | 48 | 45 | 411 | **18.5%** |
| 2011-2012 | 0 | 4 | 498 | 0.8% |
| 2013-2014 | 0 | 0 | 504 | 0.0% |
| 2015-2016 | 0 | 22 | 482 | 4.4% |
| 2017-2018 | 0 | 3 | 499 | 0.6% |
| 2019-2020 | 0 | 27 | 478 | 5.3% |
| 2021-2022 | 0 | 5 | 498 | 1.0% |
| 2023-2025 | 0 | 1 | 751 | 0.1% |

**No collapse to depth 1 anywhere, including 2008-2010.** 2009-2010 is the one era
with elevated backoff activity (18.5% depth<3, ~3x the next-highest era) — but
that's depth 1 *and* 2 combined, still 81.5% at full depth 3, and it reads as
genuine regime turbulence during the GFC recovery, not a dimension going dark.
Depth 1 disappears entirely after 2010.

### `confidence_label` × `regime_match_depth` cross-tab (ensemble voter)

| depth | high | moderate | low | n | high share |
|---|---|---|---|---|---|
| 1 | 52 | 37 | 16 | 105 | **49.5%** |
| 2 | 7 | 62 | 50 | 119 | 5.9% |
| 3 | 2,436 | 2,144 | 479 | 5,059 | **48.2%** |

Depth 1 and depth 3 are statistically indistinguishable (49.5% vs 48.2%, n=105
well within noise) — the empirical proof: `compute_confidence()` carries no
detectable depth weight. Depth 2 remains a real, unexplained, bounded outlier
(5.9%, n=119) — left alone, not chased down further, by agreement.

### `p_positive` per horizon — and the corrected sharpness comparison

n=5,283 each, exact, from the persisted rows.

| Horizon | mean | std | min | max |
|---|---|---|---|---|
| 1d | 0.527 | 0.0877 | 0.177 | 0.909 |
| 5d | 0.569 | 0.0872 | 0.074 | **1.000** |
| 20d | 0.625 | 0.0768 | 0.269 | 0.920 |
| 60d | 0.660 | 0.0841 | 0.231 | 0.962 |

**Corrected comparison.** `MARKET_MEMORY_V2_BUILD.md` §1.3's cited live 5d sharpness
(0.0946) is `stddev(p_positive)` pooled across **19 correlated tickers over ~3
weeks** — most of that spread is the fixed cross-sectional ranking (semis/tech
forecast consistently higher than financials/utilities), not time-series movement.
Verified directly: decomposing that same pooled sample (n=320) gives **57.6%
cross-sectional variance, 42.4% time-series** — and SPY's own live 5d time-series
std, queried directly, is **0.0513** (n=18), not 0.0946.

**Replay SPY (0.0872) vs. live SPY's own time-series std (0.0513): replay shows
~1.7x MORE time-series variation, not less.** The model *does* respond more across
20 years of genuinely different regimes than across a recent 3-week window. Phase D
should not carry forward "the model doesn't get sharper when regime-conditioned" —
that reading came from an invalid comparison and doesn't survive the like-for-like
one. The depth-vs-confidence finding above is unaffected (same-ticker,
depth-vs-depth throughout) but shouldn't be read as part of a broader
"model-is-regime-insensitive" story — the sharpness evidence now points the
other way.

**Logged, not fixed — no shrinkage floor on `p_positive`.** The 5d max of exactly
`1.0000` traces to three real dates: **2020-03-18, 2020-03-19, 2020-03-23** — the
COVID-crash bottom — where every matched analog (n=25-26) agreed on direction. The
raw formula has no regularization, so unanimous small-`n` agreement produces literal
certainty. **Do not clamp ahead of the backfill** — the replay ledger needs to record
the model's raw, unclamped claims for isotonic/Platt recalibration (already planned
for Phase D) to be fit against correctly.

**Logged, not fixed — the live bullish tilt looks like the drawdown, not the
model.** Live 5d mean forecast was 0.551 against a 0.417 hit rate (§1.3). Replay's
own 20-year 5d mean (0.569) sits close to that live forecast mean, and per-horizon
means climb smoothly and plausibly with horizon length. Suggestive that the
apparent tilt was that specific 3-week correlated drawdown, not a structural model
bias — not proven; the replay ledger is what actually settles it.

### Unknown regime dates and block counts

Zero unknown-regime dates across all 20 years. Block counts: 5,283 for all 6
horizon/voter combinations, 31,698 total — confirmed three separate ways (dry-run
count, real-write count, `forecasts_replay_block_counts` query against the live
table).

---

## 2. Poison queue fixed

`MAX_WRITE_ATTEMPTS=5`, `data/dead_letter_forecast_writes.jsonl` (append-only,
`attempts`/`last_error`/`dead_lettered_at` recorded), loud `[DEAD-LETTER]` logging,
both `forecast-engine.yml`/`manual-forecast.yml` commit-back steps updated. New
test (`scripts/tests/test_dead_letter_queue.py`) verifies exhaustion happens on
exactly the 5th failure, recovery-before-exhaustion leaves no trace, and
dead-lettering doesn't clobber across entries. The 8 stale `SMH` entries are
migrated with their real observed failure reason —
`data/pending_forecast_writes.jsonl` is now empty.

---

**Matrix dispatched following this review** — see the C4 dispatch report for
per-ticker results.

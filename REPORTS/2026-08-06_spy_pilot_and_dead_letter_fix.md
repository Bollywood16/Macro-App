# SPY replay pilot — analytical results, and the Fix 5 dead-letter fix

Full detail and methodology: `docs/C3_DESIGN.md` §11 (pilot analysis) and §12
(dead-letter fix). This is the summary reported before dispatching the C4
17-ticker matrix (still undispatched as of this report).

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
(5.9%, n=119) — not chased down further.

### `p_positive` per horizon — and the sharpness comparison

n=5,283 each, exact, from the persisted rows.

| Horizon | mean | std ("sharpness") | min | max |
|---|---|---|---|---|
| 1d | 0.527 | 0.0877 | 0.177 | 0.909 |
| 5d | 0.569 | 0.0872 | 0.074 | 1.000 |
| 20d | 0.625 | 0.0768 | 0.269 | 0.920 |
| 60d | 0.660 | 0.0841 | 0.231 | 0.962 |

**Live 5d sharpness (0.0946) vs. replay's 5d sharpness (0.0872): nearly identical,
replay marginally tighter.** If the model expressed real regime-conditional
sharpness — tighter in calm periods, wider under stress — 20 years pooled across
every regime SPY has lived through would show visibly *more* spread than one live
window. It doesn't. This changes what Phase D can conclude: it can't assume the
model gets sharper when regime-conditioned — this pilot's own numbers say that's
not happening in `p_positive`. Same underlying gap as the depth-confidence finding
above, seen from a different angle.

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

**Matrix remains undispatched**, pending review of the depth/confidence/sharpness
findings above.

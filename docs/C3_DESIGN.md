# C3 Design — Point-in-Time Data Store

**Status: §1–§3 were investigation/proposal; approved, and both C3 (§5) and C2 (§6) are
now implemented and tested.** `scripts/pit_store.py`, `scripts/pit_seed.py`,
`scripts/replay.py` all exist. C4 (the historical backfill workflow) is next — see §7.

Scope, per §4 of the spec: (1) what's actually in the git-history JSON files today, (2) a
proposed Parquet schema with `effective_date`/`processed_date` defined per table, (3) the
exact lookahead-safety guarantee for `PointInTimeDataContext` and how it's tested. All
three sections below are backed by things actually run against this repo and against
yfinance/FRED today (2026-08-05), not assumptions — commands are reproducible.

---

## 0. Summary — read this first

1. **The git-history JSON files cannot seed the replay store.** They cover ~19
   non-contiguous days (2026-07-11 → 2026-08-04, one dead 2-week stretch), and even on the
   days they exist, they hold *derived summary snapshots* (today/1m/3m/6m/1y point reads
   and mined episode stats), never a raw daily price series. There is nothing to extract
   for 2005–2025. §1.5 of the spec's claim that "`git log --follow` is a free PIT store"
   is true only as a ~month-deep regression-diff safety net, not as replay's data source.
2. **A real, currently-live data gap: FRED's ICE BofA high-yield OAS series
   (`BAMLH0A0HYM2`, used for the credit regime dimension everywhere) now only serves data
   from 2023-08-07 forward.** This isn't a replay-only problem — it's true of the fetch
   the *live* app runs today. Every regime-conditioned computation for dates before
   2023-08-07 is silently missing its credit dimension. Full finding and options in §2.4.
3. **The whole lookahead-safety question reduces to one choke point, which is good news
   and bad news.** Every engine function in the repo (`engines/*.py` and
   `forecast_engine.py`'s analog model) trusts that the series it's handed already ends at
   "now" — none of them independently check a date bound. That means `as_of()` /
   `PointInTimeDataContext` is the *only* place a leak can be introduced, but also the
   *only* place stopping one — nothing downstream will catch a mistake. Full argument and
   a concrete unguarded code path already found in §3.
4. **`pyarrow` isn't installed anywhere in this environment or any workflow's pip step.**
   Trivial to add, but C4's workflow will need it and none of the existing `pip install
   yfinance pandas ...` lines have it.

---

## 1. What's actually in the git-history JSON files

Extraction methodology: `git log --format=%ad --date=short -- <file>` for each digest,
plus loading the current version of each file to see what fields exist at all.

| File | Tickers covered | Commit-days in git history | What's actually stored |
|---|---|---|---|
| `data/research_digest.json` | 5 — `SMH, ^SOX, SPY, QQQ, GLD` (`scripts/universe_config.json`) | 17 | Per-asset `current.snapshots` (today/1m/3m/6m/1y point reads: price, rsi14, macd_hist, ext200_pct, from_hi20_pct) + `studies` (mined trigger/episode statistics). **No raw OHLCV.** |
| `data/rotation_digest.json` | 14 — 11 sectors (`XLK,XLF,XLV,XLE,XLI,XLY,XLP,XLU,XLB,XLRE,XLC`) + 3 size/style (`RSP,IWM,MGK`) (`scripts/rotation_config.json`) | 20 | `rs_table` (1m/3m/6m/1y % return + rank, current only), `hmm_regime`, `cftc_positioning`/`cboe_putcall` (current snapshot only, explicitly documented in its own `meta.caveats` as having no history wired up), `episodes`. **No raw OHLCV.** |
| `data/scanner_digest.json` | Reads back the union universe (`scanned_universe_count: 19` = 5 + 14 above) | 10 | Today's trigger/setup list only — a read of that day's `forecasts` table, not underlying price data. |
| `data/extension_overlay.json` | 1 (SMH/^SOX study only — single-asset by design) | 18 | `current` (today's extension read) + `cohorts`/`median_paths` (mined episode paths). **No raw OHLCV.** |

Total distinct tickers across all four: 19 (5 research + 14 rotation; scanner/extension
don't add new ones). That happens to be exactly the "17 replay tickers + the 2 excluded
(`XLC`, `XLRE`)" from spec §2 — so *coverage-by-name* is complete, but coverage-by-content
is not: **none of these files ever held a raw price series to begin with**, on any date.
They're computed-metric snapshots, generated fresh each run from a `yfinance
period="max"` pull that already only reflects today's (adjusted, retroactively-revised)
view of history — see §2.4. Diffing across commits would only ever show "what the
snapshot metrics were on run day N," which is a fine trail for catching a live regression
(e.g. "did `ext200_pct` jump for a reason") but contributes zero rows toward a 2005–2025
backtest.

**Gap in the run history itself, independent of content:** 2026-07-15 → 2026-07-29 has no
commits in `data/` at all (verified via `git log --format=%ad --date=short -- data/ |
sort -u`) — a ~2-week window where the daily workflows either didn't run or didn't push.
Not investigated further here since it's outside C3's scope, but worth a one-line mention
to whoever owns workflow health.

**Conclusion:** the Parquet store in §2 has to be built from a fresh yfinance/FRED
backfill (C4), not extracted from anything currently in git. The git-history JSONs remain
useful for exactly one thing going forward: a cheap trip-wire that catches a live-pipeline
regression by diffing today's snapshot against yesterday's — not a replay data source.

---

## 2. Proposed Parquet schema

### 2.1 Directory layout (unchanged from spec §4, C3 section)

```
data/pit/prices/{ticker}/{year}.parquet     # OHLCV, adjusted as-of-date
data/pit/flows/{ticker}/{year}.parquet      # net flow, shares out, NAV
data/pit/options/{ticker}/{year}.parquet    # EOD chain summary
data/pit/macro/{series}/{year}.parquet      # as-published vintages
```

One reader for all four, per spec:

```python
def as_of(table: str, ticker: str, date: date) -> pd.DataFrame:
    """Rows whose processed_date <= date. Raises on empty."""
```

### 2.2 `effective_date` vs `processed_date`, defined per table

The spec is right that these mean different things per table — here's the concrete
definition for each, because "prices" genuinely doesn't need the distinction the other
three tables do.

| Table | `effective_date` (what the row is *about*) | `processed_date` (when it *became knowable*) | Do they diverge in practice? |
|---|---|---|---|
| **prices** | The trading date the bar covers. | Same trading date, end-of-day (~4pm ET close + a short print delay). | **No, by design.** A daily close is known same-day after the close. Keep both columns for schema uniformity (every table goes through the same `as_of()` filter on `processed_date`) but expect `processed_date == effective_date` for every row, always. If a future ingestion ever back-fills a corrected bar (a vendor restatement), that correction gets a *new row* with the later `processed_date` and the original `effective_date` — never an in-place edit (mirrors the existing "freeze at creation" guardrail on `forecasts`). |
| **flows** (net flow, shares out, NAV) | The date the flow/NAV is *for*. | When the issuer/vendor actually published it. **Real lag, T+1 to T+several.** Confirmed no fetch code for this exists in the repo yet (`grep`ped `scripts/`+`engines/` for `shares_out`/`net_flow`/`NAV` — zero hits); this table is schema-only until the Phase F paid-data work lands. | **Yes, materially.** This is the one table where getting the distinction wrong is highest-stakes: if a backfill script ever populates this from a source that only exposes *current, latest-known* flow figures, every historical `processed_date` would need to be reconstructed from the vendor's own publication calendar, not assumed same-day. Flag any flows backfill that can't source a real `processed_date` as unsafe to gate replay decisions on — mark it `context_only` (spec §3's own escape hatch) rather than guess. |
| **options** (EOD chain summary) | The trading date the chain snapshot is for. | End of that trading day (chain data doesn't restate the way flows/macro do). | **No**, same reasoning as prices. No fetch code exists yet either — also Phase F. |
| **macro** (VIX, credit spread, rates, etc.) | The date the observation is *for* (e.g. "the OAS reading for 2019-03-04"). | When FRED/the source actually published that observation — for **survey/administrative series this genuinely lags** (initial claims, GDP, and similar get revised after original publication); for **market-observed series** (VIX, credit spreads, Treasury yields) publication is same-day but a subtler problem replaces the lag question — see §2.4. | **Yes for revision-prone series** (not really relevant to what this app currently pulls — VIX/HY-OAS/10yr are all market-quoted, not survey-revised) **but yes in a different way for HY OAS specifically, today** — see below. |

### 2.3 Per-ticker price depth (verified against yfinance today, not assumed)

Replay universe is 17 tickers: the 5 in `scripts/universe_config.json` (`SMH, ^SOX, SPY,
QQQ, GLD`) + the 14 in `scripts/rotation_config.json` minus the 2 the spec excludes
(`XLC`, `XLRE`) = 9 sectors + 3 size/style. Checked each ticker's actual yfinance history
start (`yf.download(ticker, period="max", auto_adjust=True)`):

| Ticker | History starts | Covers 2005–2025 window fully? |
|---|---|---|
| SPY, ^SOX, XLK/XLF/XLV/XLE/XLI/XLY/XLP/XLU/XLB (all 1998-12-22 vintage) | 1993–1998 | Yes |
| QQQ | 1999-03-10 | Yes |
| SMH, IWM | 2000-06-05 / 2000-05-26 | Yes |
| RSP | 2003-05-01 | Yes |
| GLD | 2004-11-18 | Yes (starts 6 weeks before the window) |
| **MGK** | **2007-12-27** | **No — missing ~2005-01 through 2007-12, ~3 years** |

Everything clears 2005 except `MGK`, which has a genuine ~3-year hole inside the replay
window. This isn't a reason to exclude it (same logic that already keeps `XLRE`/`XLC` in
the *live* rotation panel while excluding them from *replay* — "the engine uses whatever
exists on each date," per `rotation_config.json`'s own `_readme`) — just something the
schema and the C4 backfill/block-count reporting need to handle explicitly per ticker,
the same way `rotation_digest`'s `meta.caveats` already documents uneven sector history
today. Recommend the Parquet writer record each ticker's actual first available date
(from the fetch itself, not hardcoded) so `as_of()` can raise a clear "no data before
{date} for {ticker}" rather than silently returning empty or NaN-filled rows.

### 2.4 The adjusted-close question (prices) and the FRED licensing gap (macro)

Two data-fidelity issues, both verified live, that the schema needs to make an explicit,
documented choice about rather than inherit silently from the current fetchers:

**Prices — `auto_adjust=True` is retroactive.** `research_engine.fetch_history()` /
`fetch_ohlcv()` call yfinance with `auto_adjust=True`, which bakes in *every* split and
dividend up to *today* into every historical bar, including bars from 2005. This is
standard backtesting practice (it's a deterministic scale factor, not new information
about future price direction) but it is, read literally, a violation of "use only data
published on or before D" — a 2005 bar's stored value depends on a 2019 stock split that
hadn't happened yet in 2005. Recommend the design doc's review explicitly ratify
adjusted-close as an accepted, documented exception (which is what every quant backtest
does) rather than have it be an unstated assumption someone finds later and treats as a
bug.

**Macro — `BAMLH0A0HYM2` (ICE BofA US High-Yield OAS, the credit-regime input used
throughout `forecast_engine.py`'s `regime_series()` and `dip_context.py`) now only serves
data from 2023-08-07 forward on FRED.** Verified today, not assumed:

```
BAMLH0A0HYM2   → 2023-08-07 .. 2026-08-03  (793 rows)
BAMLH0A0HYM2EY → 2023-08-07 .. 2026-08-03  (793 rows)   # same family
BAMLC0A0CM     → 2023-08-07 .. 2026-08-03  (793 rows)   # same family
T10Y2Y (non-ICE control) → 1976-06-01 .. 2026-08-04  (13,091 rows)
DGS10  (non-ICE control) → 1962-01-02 .. 2026-08-04  (16,850 rows)
```

All three ICE BofA (`BAML*`) series truncate at the identical date regardless of query
parameters (tried with and without explicit `cosd=1996-01-01`); non-ICE FRED series from
the same endpoint return full history with no special parameters. This is consistent with
ICE's 2022 licensing change restricting public FRED access to a rolling window for its
index-family series — not a bug in this repo's fetch code, a change in what FRED is
allowed to serve.

**This is not a replay-only problem.** `fetch_hy_oas()` is called by the *live* forecast
engine today. `oas.reindex(idx).ffill()` on a series that only starts 2023-08-07 produces
`NaN` for every earlier date (ffill only propagates forward from an existing value, it
doesn't backfill), which `regime_series()` currently renders as `"unknown"` — silently
indistinguishable from a genuine missing-data day. Any regime-conditioned statistic that
looks back further than ~3 years already has a blind credit dimension today, live, before
any replay work happens. Recommend flagging this to be fixed independently of C3 (it's a
one-line fetch change, not a design question), and noting it explicitly here because C4's
backfill would otherwise inherit the same 3-year ceiling for the entire 2005–2023 span of
the replay window — the majority of it.

Two options worth naming for the eventual fix (not deciding here — proposal only):
1. Accept the gap and mark `credit_lab='context_only'` (not `'unknown'`) for
   `processed_date < 2023-08-07`, and let `regime_conditioned_positions()`'s existing
   3→2→1→0-dim backoff do its job — it's already built to degrade gracefully on missing
   dimensions, just needs the label to say "known absent" rather than "unknown."
2. Substitute a longer-history proxy for the credit dimension pre-2023 —
   `BAA10Y` (Moody's Baa − 10yr Treasury) has full history back to 1986 on FRED and isn't
   ICE-licensed. Different methodology (investment-grade spread, not high-yield OAS), so
   this would need its own regime thresholds, not a reuse of the existing >0.25/-0.25
   widening/narrowing cutoffs tuned for OAS's scale.

Either way, the macro Parquet writer's `processed_date` for HY-OAS-sourced rows needs to
honestly reflect "not available before 2023-08-07," not silently forward-fill or leave a
gap that `as_of()` papers over.

**Resolved 2026-08-05 — option 2 (`BAA10Y` substitution) chosen and implemented**, live
in `forecast_engine.py`/`dip_context.py`/`rotation_engine.py`/`scanner.py`; see
`docs/CREDIT_SERIES.md` for the full before/after measurement and rationale. Two
follow-on requirements for C4's backfill that fall out of that implementation, flagged
here so they aren't rediscovered mid-backfill:

**Percentile-window warmup pushes C4's data-fetch start date back, independent of the
replay window's own start.** `credit_regime_series()`'s regime classifier ranks the
63-day credit change against its own trailing **1260-trading-day (~5 calendar year)**
window — see `docs/CREDIT_SERIES.md` §0b. If C4's backfill fetches `BAA10Y` starting only
at the replay window's own start (2005-01-01, per spec §2), the first ~5 years of the
replay (2005-2010) get `credit_lab='unknown'` for the same structural reason the original
HY-OAS bug did — a different truncation, same failure shape. **C4 must fetch `BAA10Y`
starting no later than 1999-01-01** (BAA10Y itself has been available since 1986, so this
is a fetch-range choice, not a data-availability limit) to give the window a full 5-year
runway before 2005-01-01, plus roughly a year of margin. Audited every other trailing
window in the codebase for the same risk (`grep` for `rolling(`/`.diff(`/`.shift(` across
`scripts/*.py` and `engines/*.py`): the next-longest is 252 trading days (~1 calendar
year — `hi252`/`roll_high`/`mom_12m`/`trail252`, several files), a quarter of credit's
window. None of them need a similarly extended pre-2005 fetch; `BAA10Y`'s 1260-day window
is the outlier by roughly 5x, not one case of a general pattern.

**`deployment_ladder.py`'s blowout guardrail cannot run in replay before 2023-08-07.**
`hy_oas_blowout()` deliberately still reads the real ICE BofA OAS series (kept, unchanged,
in `research_engine.fetch_hy_oas()` — see `docs/CREDIT_SERIES.md` §0b) because its
halt threshold is calibrated to that series' own scale; the `BAA10Y` swap above applies
only to the regime-classification dimension, not this guardrail. Since ICE OAS itself
has no data before 2023-08-07, **any C4 replay date before 2023-08-07 has no
live-equivalent credit-blowout halt check available** — not a bug to chase during the
backfill, a structural gap in what replay can faithfully reproduce for that guardrail
specifically. Whoever builds C4 should surface this as an explicit caveat on any
pre-2023-08-07 replay result that depends on `deployment_ladder.py`'s halt logic, rather
than silently treating "guardrail didn't fire" as "guardrail checked and passed."

### 2.5 Flows / options tables are schema-only for now

Confirmed via `grep` across `scripts/` and `engines/` — no fetch code exists for ETF flow
data, shares-outstanding, NAV, or options chains anywhere in the repo today. The spec
already defers paid data to Phase F. The Parquet layout above reserves the shape (so C3's
`as_of()` contract doesn't need to change later) but these two tables should ship *empty*
with C3/C4, not stubbed with placeholder data — matches the existing guardrail "no
synthetic history."

### 2.6 Dependency gap

`pyarrow` is not installed in this environment and appears in none of the 9 workflow
files' `pip install` lines (checked all of them). C4's backfill workflow will need `pip
install pyarrow` (or `fastparquet`) added explicitly — flagging now so it's not a
surprise mid-implementation.

---

## 3. Lookahead-safety guarantee for `PointInTimeDataContext`

### 3.1 The guarantee, stated exactly

> For any call through `PointInTimeDataContext(as_of=D)` — `close()`, `ohlcv()`, `vix()`,
> `hy_oas()`, `episodes()`, `benchmark()` — every row returned has `processed_date <= D`,
> and the returned series' last index is the latest available date `<= D` (never later).

That's the entire contract. Everything downstream is built to consume exactly that shape
and nothing more sophisticated — which is both the argument for why this is tractable and
the reason it has to be gotten right in exactly one place.

### 3.2 Why this is a single choke point (verified by reading the call sites, not assumed)

Every `engines/*.py` module (`dip_context.py`, `tech_read.py`, `bottom_scenarios.py`,
`relative_strength.py`, `episodes.py`) computes its "current" reading via `series.iloc[-1]`
and its historical comparison stats via `series.mean()`/`series.std()` over whatever it
was handed — e.g. `relative_strength.py`: `cur = s.iloc[-1]; z = (cur - s.mean()) /
s.std(ddof=1)`. None of these functions take a `date` parameter or check one. They are
correct *only* because every caller today happens to hand them a series that already ends
at "now" — the contract is enforced entirely by convention, not by the functions
themselves.

`forecast_engine.py`'s analog model is architecturally different and worth flagging
specifically. `run_one()` does:

```python
df = build_feature_frame(close, spy_close_run, rotation_ctx, ticker)
query_pos = len(df) - 1                      # "today" = last row, by convention
...
X, norm_stats = normalize_matrix(df, FEATURE_FIELDS)   # z-normalizes over the WHOLE df
analog_pos, nearest_dist = analog_positions(X, query_pos)
```

and inside `analog_positions()`:

```python
def analog_positions(X: np.ndarray, query_pos: int):
    ...
    valid = ~np.isnan(X).any(axis=1)
    valid[query_pos] = False        # excludes ONLY the query row itself
    ...                              # everything else in X is a candidate --
                                     # including rows AFTER query_pos, if any exist
```

`regime_conditioned_positions()`, called two lines later in the same function, *does* get
explicitly sliced (`regime_tuples[:query_pos]`) before being searched — so the two
candidate-selection paths that get unioned into `ensemble_pos` are inconsistently guarded
today. Neither is a live bug right now, because `query_pos` is always `len(df)-1` on the
live path, so there is nothing after it to leak. But `analog_positions()` and
`normalize_matrix()` have no internal defense if `C2`'s `replay()` ever calls this same
function with a `df`/`X` that extends past the replay date (e.g. a naive replay
implementation that fetches the full modern series once and threads a historical
`query_pos` through it, rather than truncating the frame itself before calling in) — the
nearest-neighbor search would then be free to match the query date against future price
action, and the z-normalization would be computed over statistics that include the
future. This is exactly the "excellent numbers, no error" contamination the spec's item 3
warns about — a `moderate_long_candidate` call sitting on an analog match from six months
after the replay date would look like skill.

**Conclusion for C2's design (flagging for that phase, not solving here):** `replay()`
must truncate every series/frame at the `DataContext` boundary — never pass a full-length
array with an index pointer into `forecast_engine.py`'s analog functions. `as_of()`
returning an already-truncated frame makes this automatic and removes the need to trust
`analog_positions()`'s internals at all. Worth also adding an explicit `assert
query_pos == len(df) - 1` at the top of `run_one()` / wherever C2 reuses it, cheap
insurance that turns a silent contamination into a loud crash if the truncation
assumption is ever violated by a future refactor.

### 3.3 How this gets tested — mutation/canary tests, not just equality checks

The spec's own framing is the right bar: *"a contaminated backtest does not error, it
produces excellent numbers."* An equality test between "truncated store" and "full store
filtered to the same date" is a weaker test than it looks, because if the truncation
logic itself has a bug, both code paths can share that bug and agree by coincidence. The
test that actually catches a leak has to inject a signal that *couldn't* be there
honestly and check it has zero effect:

1. **Canary/mutation test (primary mechanism).** Take a fixture store truncated at date
   `D`. Run `replay(ticker, D)` and record the output. Then splice an obviously
   out-of-family value into the *same* fixture at `D+1` (e.g. force `close = $1` or a
   +500% single-day move, and if flows/macro are wired up by then, an extreme flow/OAS
   spike too) and re-run `replay(ticker, D)` against the mutated store. **Assert the
   output is byte-identical.** If it isn't, something downstream of `as_of()` saw data
   past `D` — mechanically detected, not inferred from suspiciously good calibration
   numbers months later. This should run for every table (`prices`, `macro` once wired,
   `flows`/`options` once they exist) and for every engine surfaced through the bundle
   (dip_context, tech_read, bottom_scenarios, relative_strength, episodes), not just the
   top-level forecast — a leak in one card while the headline number stays clean is still
   a leak.
2. **`as_of()` itself gets a direct unit test independent of any engine**: build a tiny
   fixture with rows straddling a boundary date, call `as_of(table, ticker, D)`, and
   assert both that every returned row's `processed_date <= D` *and* that a row with
   `processed_date == D+1` is excluded even though its `effective_date` might be `<= D`
   (this is exactly the flows-table failure mode from §2.2 — an NAV figure *for* day D
   that wasn't *published* until D+2 must not leak into a `D+1` replay).
3. **Runtime assertion inside `PointInTimeDataContext` itself (belt-and-suspenders, not a
   substitute for #1).** Every method return passes through one shared check —
   `assert result.index.max() <= as_of_date` (or the `processed_date` equivalent for
   non-time-indexed returns like `episodes()`) — before handing data back to a caller.
   Cheap, always-on, and turns any *future* violation (a new engine added later that
   doesn't get a canary test written for it) into an immediate crash in dev/CI rather than
   a silently contaminated number three phases later.
4. **Tie into the acceptance test the spec already names** (§4 C2: `replay('SPY', <random
   2019 date>)` byte-identical against a store truncated at that date). That test proves
   determinism; it does not by itself prove *lookahead-freedom* — a function could be
   both deterministic and leaking (e.g., always looking exactly 30 days ahead) and still
   pass a determinism-only check. Recommend it get extended with the canary/mutation
   variant in #1 as a named, separate acceptance criterion for C3 specifically (distinct
   from C2's), since it's testing a different property.

### 3.4 What's *not* proposed here

No isotonic/self-retraining anything, no change to the confidence-gating logic, no
decision on the `BAMLH0A0HYM2` fix (§2.4) beyond naming two options for review. This
section is scoped to "how do we know the pipe doesn't leak," not "what do we do once it
doesn't."

---

## 4. Open questions for review

1. ~~**BAMLH0A0HYM2 fix.**~~ **Resolved 2026-08-05 — `BAA10Y` substituted, own
   (percentile-based) thresholds, see §2.4 and `docs/CREDIT_SERIES.md`.** Two follow-on
   requirements this creates for C4 specifically (fetch-start date, blowout-guardrail
   replay gap) are now documented at the end of §2.4 — read before starting the backfill.
2. ~~**Adjusted-close as an accepted exception (§2.4)**~~ **Resolved 2026-08-05 —
   ratified as-is, per this section's own recommendation.** `write_prices()` in
   `scripts/pit_store.py` takes whatever `research_engine.fetch_ohlcv()` returns
   (`auto_adjust=True`) with no unadjusted/corporate-actions alternative built. Documented
   explicitly in the module docstring, not left as an unstated assumption.
3. ~~**`MGK`'s 2007-12-27 start (§2.3)**~~ **Resolved 2026-08-05:** documented as a 3-year
   hole, same handling as today's rotation panel ("the engine uses whatever exists on
   each date"). No proxy/synthetic series.
4. ~~**Flows/options tables (§2.5)**~~ **Resolved 2026-08-05 — directory shape reserved
   (`pit_store.FLOWS_TABLE`/`OPTIONS_TABLE` constants exist), no writer, ships empty.**
   `as_of()`'s contract doesn't change when Phase F populates them later.

**Correction to an earlier note in this doc:** a prior draft flagged live `HORIZONS`
still including `126, 252` as possibly out of sync with the spec. That's not a
discrepancy — the spec's §2 "Replay horizons: 1d/5d/20d/60d" decision applies to the
**replay grid only**, to save backfill compute. Live keeps producing all six horizons;
those rows already exist in the ledger and cost nothing extra to keep computing. Live
`HORIZONS` is not to be changed.

---

## 5. Implementation status (2026-08-05)

Built exactly what §2/§3 specified, nothing more — `replay()` (C2) is still unbuilt and
out of scope here, this section only covers the store and its context object.

**Code:** `scripts/pit_store.py` (`as_of()`, `write_prices()`, `write_macro()`,
`PointInTimeDataContext` implementing the same `DataContext` protocol
`data_context.LiveDataContext` does — see that module's own docstring for the
interchangeability contract C2 depends on). `scripts/pit_seed.py` is a one-shot local
seed, explicitly **not** C4's chunked/resumable GitHub Actions backfill (own docstring
says so) — it exists only to give this phase's tests real data instead of pure
synthetic fixtures.

**Seed run against live yfinance/FRED data, 2026-08-05** (`python3 scripts/
pit_seed.py`): `BAA10Y` — 10,147 rows, 1986-01-02 → 2026-08-04, clears the
`CREDIT_WARMUP_FLOOR` (1999-01-01) requirement from §2.4 with 13 years to spare. `VIX` —
9,216 rows, 1990-01-02 → 2026-08-05. All 17 replay tickers seeded; every history-start
date matches §2.3's table exactly, including `MGK`'s 2007-12-27 start correctly *not*
covering 2005-01-01 (flagged by the seed script's own output, not silently accepted).
9.5MB, 555 Parquet files total — small enough that `data/pit/` is gitignored (same
treatment as the existing `data/*.json` digests: derived, regenerable, not meaningfully
diffable in git), not committed.

**Tests, all passing, no network:**
- `scripts/tests/test_pit_as_of.py` — `as_of()` unit-level (§3.3 point 2): boundary
  inclusivity, and the flows-table lag case named in §2.2 (`effective_date <= D` but
  `processed_date > D` must still exclude the row) built as a hand-crafted fixture since
  no real flows fetcher exists yet. Both empty-result paths (`never ingested` vs.
  `data exists, none as-of this date`) raise `PITStoreError` with distinguishable
  messages, per §2.3's recommendation.
- `scripts/tests/test_pit_lookahead_canary.py` — the mutation/canary test, §3.3 point 1,
  run against `PointInTimeDataContext` directly (not `replay()`, which doesn't exist
  yet — that's C2's own acceptance criterion per §3.3 point 4, not duplicated here).
  Verified the test isn't vacuous both ways: confirmed the shared runtime assertion
  (§3.3 point 3, `_assert_no_lookahead`) independently catches a simulated broken
  `as_of()`, then confirmed the byte-identical comparison *also* independently catches
  the same leak with the runtime assertion additionally disabled — two independent
  defenses, both proven to actually fire, not just present in the code.

**Aside, unrelated to C3, noticed while running the full suite:**
`scripts/tests/test_fail_loud_persistence.py` reads/writes `forecast_engine.
PENDING_WRITES_PATH` directly — the real `data/pending_forecast_writes.jsonl` queue,
not a temp path — and clears it as a side effect of a normal test run. **Fixed the same
session it was found** (`fe.PENDING_WRITES_PATH` monkeypatched to a tempdir for the
test's duration, restored in a `finally`, real file's content hash asserted unchanged
before/after — verified the check fires both ways, not just present).

---

## 6. C2 implementation (2026-08-05)

`scripts/replay.py`'s `replay(ticker, date)` — built per §3.2's conclusion exactly:
every series handed to `forecast_engine.run_one()` (`close`, `spy_close`, `vix`, `oas`)
comes from `PointInTimeDataContext(as_of=date)`, already filtered by `as_of()` before
`run_one()` ever sees it — no full-length array with an index pointer anywhere in the
call path. The `assert query_pos == len(df) - 1` safety net §3.2 recommended was added
to `run_one()` itself (fires for *any* caller, not just `replay()`).

Two changes to `forecast_engine.py`, both additive, needed to make `replay()`'s return
value actually be "the bundle the app would have produced" rather than a subset of it:
`run_one()` previously computed `tearsheet_extras` (dip_context/tech_read/
bottom_scenarios/relative_strength/episodes/triggers/agreement — most of what a tear
sheet shows) but only exposed it via a persisted `evidence_json`, never to the caller
directly; now returned at the top level. `trading_date` was similarly persisted-only,
now also returned.

**Acceptance test result** (`MARKET_MEMORY_V2_BUILD.md` §4 C2's exact wording:
`replay('SPY', <random 2019 date>)` byte-identical against a store truncated at that
date) — **passing**, `scripts/tests/test_replay_acceptance.py`, run against the real
seeded store from §5 (not synthetic fixtures) and a *physically* truncated copy (every
row after the query date deleted from a separate store directory — stronger than the
§3.3-point-1 mutation test, which keeps future rows present but implausible; this
proves the comparison holds even when future data doesn't exist at all, not just when
it's wrong):

```
replay('SPY', 2019-09-21): byte-identical, full store (8,436 SPY price rows, history
through 2026-08-05) vs. truncated copy (6,710 rows, nothing after 2019-09-21).
n_independent=129, recommendation_label='high_conviction_long_candidate',
confidence_label='high' — identical on both sides down to every horizon row.
```

Spot-checked across 4 additional random 2019 dates (seeds 1/42/999/20250101 →
2019-03-11, 2019-11-25, 2019-12-15, 2019-11-03): all byte-identical, all
`high_conviction_long_candidate`/`high` (unsurprising — SPY's post-2009 bull market
dominates its own regime-conditioned history at any 2019 query date, not a sign the
test is insensitive; the truncated-vs-full row-count sanity check inside the test
confirms real data absence each time, 1,667–1,861 rows depending on the date).

**A real gap the test itself found and fixed, worth recording as a demonstration of why
the *physical* truncation variant matters, not just the mutation one:** the first
version of this test only built a truncated copy of `SPY`/`VIX`/`BAA10Y` (the series
`replay()` fetches directly) and failed — `tearsheet_extras.relative_strength.rows`
differed, 12 vs. 6. Cause: `compute_tearsheet_extras()` also fetches a `QQQ` benchmark
series on demand (`resolve_benchmarks()`, for the Nasdaq-100 comparison row) via
`ctx.close("QQQ")` — present in the full store (all 17 tickers seeded), absent from the
truncated copy (never copied there). Not a `replay()` bug — a test-fixture coverage
gap, fixed by adding `QQQ` to the truncated copy's required keys. Kept in the test file
as a live illustration: a truncated-store comparison is only as strong as its coverage
of every ticker a tear-sheet engine can reach for, not just the one being replayed.

---

## 7. Suggested next step

C2 and C3 are both done and tested. **C4 (the historical backfill workflow) is next**
— `MARKET_MEMORY_V2_BUILD.md` §4's spec: 17 tickers × 2005–2025 × 4 horizons × 2
voters, writing to a separate `forecasts_replay` table, chunked/resumable GitHub
Actions workflow, block-count reporting on completion. `scripts/pit_seed.py` (this
document's §5) is explicitly not that workflow and doesn't become it — C4 needs its own
chunking/resumability/reporting, not a bigger version of the local seed script.

---

## 8. C4 built, NOT dispatched — workflow file and runtime estimate (2026-08-05)

Per instruction: built for review, nothing run. `.github/workflows/replay-backfill.yml`
exists on disk; it has not been dispatched, and cannot succeed yet even if it were —
see "Prerequisites, not yet done" below.

### 8.1 What was built

- **`supabase/migrations/20260805150000_forecasts_replay_table.sql`** — mirrors
  `forecasts` column-for-column, two deliberate differences (`quote_snapshot_id`
  nullable/no FK, `ticker` no FK to `assets`), both explained in the file's own
  docstring. **Not run against live Supabase.**
- **`supabase/functions/mm-journal/index.ts` + `mm-journal-edge-function.txt`** (kept in
  sync, per this file's own "paste into the dashboard" deploy model) — three new ops:
  `create_forecast_replay_batch` (bulk insert, the reason a batch op exists at all — see
  §8.3), `latest_forecast_replay_date` (backs `--resume`), `forecasts_replay_block_counts`
  (the spec's own "report block count per horizon" line). **Not redeployed.**
- **`scripts/replay_backfill.py`** — the per-ticker worker each matrix job runs: iterates
  trading dates via the seeded PIT store, calls `replay()` (C2), builds `forecasts_replay`
  rows for both voters (`forecast` at the replay grid's 4 horizons — `docs/C3_DESIGN.md`
  §4's "Correction" note: 1d/5d/20d/60d only, to save backfill compute, distinct from
  live's 6 — and `dip_context` at its own native 21d/63d), batches at 500 rows/write.
  `--resume` and `--dry-run` both implemented and dry-run-tested against the real seeded
  store (see §8.2).
- **`.github/workflows/replay-backfill.yml`** — `workflow_dispatch` only (no schedule — a
  backfill isn't recurring), a 17-way matrix (one job per ticker, chunking by ticker per
  §8.3's runtime finding), `--resume`/`--dry-run` exposed as dispatch inputs, a final
  `report` job that calls `forecasts_replay_block_counts`.

Two additive `forecast_engine.py` changes this needed, beyond C2's own (`tearsheet_extras`,
`trading_date`, the `assert query_pos` guard — §6): `run_one()`'s return value now also
exposes `horizon_confidence` (per-horizon confidence score/label — previously only the
basis horizon's was returned, and a backfilled row needs its OWN horizon's confidence,
not the headline one's).

**A real, unrelated correctness bug found and fixed while building this, not shipped
broken:** the first draft of `--resume` called a fictional `query_forecasts` shape
(`{table, voter, order_by, limit}`) — the real op is hardcoded to the `forecasts` table
with none of those parameters. Caught before committing (by re-reading the actual op's
implementation, not assumed), fixed by adding the narrowly-scoped
`latest_forecast_replay_date` op instead of a generic passthrough.

### 8.2 Runtime estimate — measured compute, estimated writes, clearly labeled which is which

**Total workload, counted exactly from the real seeded store, not estimated:** 89,060
`replay(ticker, date)` calls across all 17 tickers for 2005-01-01→2025-12-31 (16 tickers
at 5,283 trading dates each; `MGK` at 4,532, per its own 2007-12-27 start, §2.3). Each
call yields 6 `forecasts_replay` rows (4 for `voter='forecast'`, 2 for
`voter='dip_context'`) when it scores at all — **534,360 total rows**, before accounting
for the small fraction of early `MGK` dates that return `None` (insufficient 260-day
history) and cost nothing.

**Compute cost — measured, not guessed**, after finding and fixing a real bottleneck: an
unpatched `replay('SPY', ...)` call ran **~740ms**, ~60% of it (174 separate
`pd.read_parquet` calls) re-reading the same ~30-40 year-files from disk that a call
moments earlier for the same ticker had just read — `pit_store.as_of()` had no cache
across calls, and a backfill iterating one ticker across ~5,000 dates was about to re-read
that ticker's entire history from disk 5,000 times to answer 5,000 different truncation
questions. Added a process-lifetime read cache (`pit_store._read_cache`, keyed by
`(table, key, store_root)` — the disk read, not the date filter, since the files don't
change between calls, only `date` does). Result: **~420-500ms/call for SPY/XLK,
~270-370ms for GLD/MGK** (shorter history → smaller frames to filter/scan each call),
measured directly across 5 tickers and a 252-trading-day 2010 steady-state run for SMH
(1.4 min / 252 dates = 333ms/date). **Working estimate: ~0.4s/call**, blended across
these measurements.

| | Calls | Estimate |
|---|---|---|
| **Sequential, single job, all 17 tickers** | 89,060 | 89,060 × 0.4s ≈ **9.9 hours** |
| **17-way matrix, slowest single job** (SPY/QQQ/etc., 5,283 dates) | 5,283 | 5,283 × 0.4s ≈ **35 min compute** |

**This is why the workflow chunks by ticker, not by date range or as one job**: chunking
turns a ~10-hour sequential run into a ~35-40 minute wall-clock run (dominated by the
slowest matrix job, since jobs run in parallel), comfortably inside GitHub Actions' default
6-hour per-job timeout with room to spare — `timeout-minutes: 90` in the workflow leaves
~2.3x margin over the measured estimate for GitHub-hosted-runner CPU variance, not because
9.9 hours of sequential work was ever seriously considered.

**Per-job overhead, measured:** `scripts/pit_seed.py` (every matrix job runs on a fresh,
empty runner — `data/pit/` is gitignored, nothing to restore from cache) — **8.9 seconds**
for the full 17-ticker + VIX + BAA10Y seed. Negligible next to the backfill itself; not
worth a shared-artifact/cache step.

**Write cost — ESTIMATED, not measured** (no live Supabase access from this environment,
by design — nothing was dispatched or deployed to test against): 534,360 rows ÷ 500
rows/batch ≈ 1,069 total `create_forecast_replay_batch` calls across all 17 jobs. At a
typical Supabase edge-function insert latency of ~300-800ms/batch (not measured here),
total write time ≈ 1,069 × ~0.5s ≈ **~9 minutes total, ≈ 30-40 seconds per matrix job** —
small relative to the ~35 min of compute, but flagged explicitly as an estimate, not a
measurement, unlike every number above it. **This estimate is also the entire reason the
batch op exists at all**: the live path's shape (one `create_forecast` HTTP call per row)
applied to 534,360 rows individually, at a similar ~300-800ms/call network round trip,
would cost **~45-120 hours of pure HTTP overhead** — the batch write isn't a minor
optimization here, it's the difference between a workable backfill and one that can't
finish in any practical timeframe.

**Bottom line: ~35-40 minutes total wall-clock for the full 2005-2025, 17-ticker backfill**,
once dispatched — dominated by compute, not writes, once batched.

### 8.3 Prerequisites, not yet done (deployment order matters, same failure mode every prior migration in this repo documents)

1. Run `20260805150000_forecasts_replay_table.sql` in the Supabase SQL editor.
2. Redeploy `mm-journal` (paste `index.ts`/`mm-journal-edge-function.txt` into the
   dashboard) with the three new ops.
3. Only then is `replay-backfill.yml` dispatchable. Dispatching before 1-2 land fails
   every job identically to every prior migration's own documented order-dependency.

**Not dispatched. Not deployed. Nothing above was run against live Supabase in this
pass** — per instruction, this section is the review checkpoint, not the backfill itself.

---

## 9. SPY pilot (dry-run, full 2005-2025 window) — before dispatching the matrix

Per instruction: one ticker, full window, `--dry-run` (no writes — `forecasts_replay`
doesn't exist yet at time of this run, see §8.3), reviewed here before touching
Supabase at all. `scripts/replay_backfill.py --ticker SPY --start 2005-01-01 --end
2025-12-31 --dry-run`, run to completion (not sampled/extrapolated).

### 9.1 Wall-clock — measured, not extrapolated

**2,207.7s (36.8 min) for 5,283 trading dates — 417.9 ms/date.** §8.2's "working
estimate: ~0.4s/call" (blended from 5 spot-measured tickers) predicted 0.4s;
measured across the *entire* window it's 0.418s — the estimate holds. `dates_scored
= 5,283 / 5,283` — every single trading date in the window scored (SPY's own history
starts 1993, well before the window, so zero insufficient-history skips, unlike
`MGK`'s expected partial gap). This directly validates §8.2's per-job runtime
estimate (~35 min for SPY's own matrix job) rather than leaving it as a projection.

### 9.2 `regime_match_depth` distribution

| Depth | n | Share |
|---|---|---|
| 3 (full match) | 5,059 | 95.8% |
| 2 | 119 | 2.3% |
| 1 | 105 | 2.0% |
| 0 (unconditional) | 0 | 0.0% |

Depth never backs all the way off to 0 for SPY across 20 years — `MIN_REGIME_N=8` is
always clearable at depth ≥ 1. Depth 3 dominates overwhelmingly, consistent with
§6.5's finding that the common regime combination is a weak filter on its own (most
days cluster into whatever the modal macro state is, which clears the 3-dim match
easily).

### 9.3 `confidence_label` × `regime_match_depth` cross-tab (ensemble/`forecast` voter) — the concrete version of the logged gap

| depth | high | moderate | low | row total | high share |
|---|---|---|---|---|---|
| 1 | 52 | 37 | 16 | 105 | **49.5%** |
| 2 | 7 | 62 | 50 | 119 | 5.9% |
| 3 | 2,436 | 2,144 | 479 | 5,059 | **48.2%** |

**Depth 1 and depth 3 — the shallowest and the most rigorous possible conditioning —
produce statistically indistinguishable "high" shares (49.5% vs. 48.2%, well within
noise at n=105).** This is the empirical confirmation, not just the structural
argument from `MARKET_MEMORY_V2_BUILD.md` §5: if `compute_confidence()` rewarded
deeper regime conditioning, depth 3 would show a *meaningfully higher* high-confidence
share than depth 1, since depth 3 represents genuinely more specific conditioning.
It doesn't — confirming directly that regime-match depth carries no detectable
weight in the confidence number the card actually shows, for the voter that drives
`recommendation_label`.

**Depth 2 is a real, unexplained outlier, flagged and not chased down further here**:
5.9% high share against 48-50% at the neighboring depths, on a non-trivial n=119 (not
a small-sample artifact). `compute_confidence()`'s `agreement = 1 - abs(analog_p -
regime_p)` term is the most likely mechanism — `regime_p` is drawn from whichever
positions the depth-2 backoff happens to match, and a depth landing in between the
two backoff endpoints could plausibly draw from a regime-conditioned pool whose
implied probability diverges further from the analog model's than either the
broadest (depth 1) or narrowest (depth 3) pool's does. Not diagnosed further — noted
as a genuine, specific, reproducible pattern (not noise) worth a follow-up look, not
folded into the "no depth awareness" finding above, which stands independently of
whatever's driving depth 2's anomaly.

### 9.4 `p_positive` distribution — moves across the window, does not cluster

| | n | min | p10 | p25 | median | p75 | p90 | max | std |
|---|---|---|---|---|---|---|---|---|---|
| **Basis horizon** | 5,283 | 0.074 | 0.605 | 0.769 | 0.831 | 0.859 | 0.875 | 1.000 | 0.125 |

| Horizon | n | min | median | max | std |
|---|---|---|---|---|---|
| 1d | 5,283 | 0.176 | 0.526 | 0.909 | 0.088 |
| 5d | 5,283 | 0.074 | 0.565 | 1.000 | 0.087 |
| 20d | 5,283 | 0.269 | 0.632 | 0.920 | 0.077 |
| 60d | 5,283 | 0.231 | 0.673 | 0.962 | 0.084 |

Real movement, not a stuck value — every horizon's own median sits meaningfully above
0.5 (SPY's 20-year realized drift shows through, as expected) with genuine spread
down to the 0.07-0.27 range and up to 0.90-1.00, tracking actual stress episodes
(2008-09, 2020, 2022 pull the low end). **One caveat worth naming, not a red flag**:
the basis-horizon distribution (median 0.831) sits noticeably higher than any single
horizon's own raw distribution (medians 0.53-0.67) — `pick_basis_horizon()` selects
whichever horizon shows the *largest directional edge* each day, which mechanically
biases the reported "headline" p_positive toward whichever horizon looks most bullish
that day, not a random or representative one. Worth remembering when reading the
basis-horizon number specifically; the per-horizon rows above are the cleaner read of
whether the model's own probabilities move with the market.

(The "distinct values (rounded to 2dp): 85/5,283" figure in the raw script output is
not a clustering signal — `p_positive` is `k/n` for whatever sample size `n` a given
day's episode pool has, so its possible values are inherently quantized by `n`; 61-80
distinct 2-decimal buckets across a 20-year window is expected quantization, not
evidence of a frozen or repeating output.)

### 9.5 Unknown regime dimension — zero occurrences

**None.** Zero of 5,283 dates returned `"unknown"` in any regime dimension for SPY
across the full 2005-2025 window. This is the direct, positive confirmation that
`docs/C3_DESIGN.md` §2.4's `CREDIT_WARMUP_FLOOR` requirement (fetch `BAA10Y` no later
than 1999-01-01, so the 1260-day percentile window has a full runway before the
replay window's own 2005-01-01 start) actually worked — `pit_seed.py` seeded `BAA10Y`
from 1986-01-02 (§8.1), 13 years ahead of the floor, and it shows: not one single
early-2005 date fell back to `"unknown"` credit the way the pre-fix OAS series would
have (`docs/CREDIT_SERIES.md` §1's finding, the entire reason this warmup requirement
exists).

### 9.6 Block count per horizon

| | forecast (4 horizons) | dip_context (2 horizons) |
|---|---|---|
| 1d | 5,283 | — |
| 5d | 5,283 | — |
| 20d | 5,283 | — |
| 60d | 5,283 | — |
| 21d | — | 5,283 |
| 63d | — | 5,283 |
| **Total** | **21,132** | **10,566** |

**31,698 total rows for SPY alone**, exactly `5,283 × 6` — every date produced a
full row set for both voters at every horizon, zero horizons skipped (unlike a
ticker with thinner episode pools, `dip_context` could in principle return
`INSUFFICIENT_EVIDENCE`-with-no-`stats`, contributing 0 rows for a date; that never
happened for SPY across 20 years). Matches `§8.2`'s row-count arithmetic exactly.

### 9.7 Verdict: looks sane, proceeding per the pre-agreed sequence

No crashes, no unexpected `None` results, no unknown-regime dates, wall-clock and row
counts matching §8's predictions to within a few percent, and the one genuinely
surprising pattern (depth 2's confidence anomaly) is a real, bounded, reproducible
data pattern — not a symptom of something broken in `replay()`, the PIT store, or
the row-building logic. Proceeding to migration + redeploy + live-path verification +
real-write SPY pilot, per the agreed sequence — the 17-way matrix stays undispatched
until that pilot is itself reviewed.

---

## 10. Migration applied, mm-journal redeployed, live path verified, real-write SPY pilot — matrix still undispatched

All four steps of the agreed sequence completed, in order, this session:

### 10.1 Migration applied

`npx supabase migration list` confirmed `20260805150000_forecasts_replay_table.sql`
was the only local migration with an empty `remote` field — every earlier migration's
`local`/`remote` timestamps already matched. `npx supabase db push` applied it;
`migration list` re-run afterward confirmed `remote` now matches `local` for all 8
migrations, including this one.

### 10.2 `mm-journal` redeployed

`npx supabase functions deploy mm-journal --project-ref anzbpxqvibgpxnwgyqoc`.
`functions list` confirmed the version bumped 9 → 10, status `ACTIVE`.

### 10.3 Live write path verified — and a real, pre-existing bug found in the process

`APP_PASSPHRASE=... python3 scripts/forecast_engine.py --ticker SPY --source chat`
(a real, non-dry-run, on-demand run — the same call shape the live app/scheduler
makes). Confirmed via `get_latest_forecast`: a fresh `forecasts` row for SPY,
`as_of_ts` matching this run's own timestamp to the second. **The live write path
survived the redeploy.**

**Found along the way, not fixed, flagged for whoever owns it:** this run's own log
showed 8 `create_forecast` retry failures — `HTTP 400 {"error":"missing fields",
"missing":["regime_model_version"]}`. Traced to `data/pending_forecast_writes.jsonl`:
8 entries, all from one stale `SMH` run at `2026-08-05T20:26:24Z`, staged **before**
`regime_model_version` became a required field. `flush_pending_writes()` /
`rewrite_pending_writes()` (`forecast_engine.py`) have no staleness detection or
dead-letter path — a staged payload is resent byte-for-byte on every future run
forever, so if the required-field shape ever changes after a write is staged, that
entry can never succeed again and will fail identically on every single run from now
on, cluttering the log the same way each time. This predates this session's changes
entirely (the entries are from `20:26:24Z`, hours before the `regime_model_version`
migration referenced in `docs/CREDIT_SERIES.md` even landed) — not caused by C4's
work, just surfaced by it. Worth a real fix (detect a `400 missing fields` response
specifically and drop the entry with a loud error, rather than retrying forever) —
not built here, out of scope for this pass.

### 10.4 Real-write SPY pilot — measured write cost, not estimated

`APP_PASSPHRASE=... python3 scripts/replay_backfill.py --ticker SPY --start
2005-01-01 --end 2025-12-31` (no `--dry-run` — real writes to `forecasts_replay`,
same table the redeployed function now serves).

**37.2 min elapsed, 31,698 rows written, 5,283/5,283 dates scored.** Against §9's
dry-run figure of 36.8 min for the identical compute with writes skipped: **the
measured write overhead is ~24 seconds total for SPY's full 20-year backfill** — 64
batches (31,698 rows ÷ 500/batch), ≈ 380ms/batch. §8.2's estimate ("~9 min total
across all 17 jobs, ~30-40s per matrix job, 300-800ms/batch assumed") holds up as
**slightly conservative, not optimistic** — the real number for SPY (the largest
ticker) came in under the low end of the per-job estimate.

**Verified via `forecasts_replay_block_counts`** (queried after the run, not just
trusted from the script's own exit code): `forecast_1d/5d/20d/60d` and
`dip_context_21d/63d` each show exactly `5,283` — matches the dry-run's predicted
block counts exactly, confirms no rows were dropped, duplicated, or misrouted.
**Verified `--resume`'s own query** (`latest_forecast_replay_date`, both voters):
returns `2025-12-31` for both `forecast` and `dip_context` — the resume mechanism
correctly finds this run's own endpoint.

**Side effect worth noting, not a problem:** `SPY` is now genuinely backfilled in
`forecasts_replay` — this pilot's writes are real progress toward C4, not throwaway
test data. Dispatching the full 17-way matrix later can either skip `SPY` or run it
with `--resume` (which re-writes its last date once, per §8.1's documented tradeoff,
and is otherwise a no-op).

### 10.5 Matrix: still not dispatched

Everything above required live Supabase access (migration + deploy + two write
runs) — all deliberate, all logged here, none of it the 17-way `replay-backfill.yml`
matrix itself. That dispatch is still pending explicit go-ahead.

---

## 11. The actual analytical gate — SPY pilot results, in full

§9 covered the pilot's infrastructure result (it ran, matched runtime predictions,
didn't crash). This section is the data itself — what the 20-year replay actually
shows for SPY, which is the real question the pilot exists to answer before
committing to 16 more tickers of the same thing.

### 11.1 `regime_match_depth` by era — checking specifically for a collapse to depth 1

Queried directly from the persisted `forecasts_replay` rows (not re-derived), grouped
into 2-year eras:

| Era | depth=1 | depth=2 | depth=3 | total | depth<3 share |
|---|---|---|---|---|---|
| 2005-2006 | 34 | 0 | 469 | 503 | 6.8% |
| 2007-2008 | 23 | 12 | 469 | 504 | 6.9% |
| 2009-2010 | 48 | 45 | 411 | 504 | **18.5%** |
| 2011-2012 | 0 | 4 | 498 | 502 | 0.8% |
| 2013-2014 | 0 | 0 | 504 | 504 | 0.0% |
| 2015-2016 | 0 | 22 | 482 | 504 | 4.4% |
| 2017-2018 | 0 | 3 | 499 | 502 | 0.6% |
| 2019-2020 | 0 | 27 | 478 | 505 | 5.3% |
| 2021-2022 | 0 | 5 | 498 | 503 | 1.0% |
| 2023-2025 | 0 | 1 | 751 | 752 | 0.1% |

**No sustained collapse to depth 1 anywhere, including 2008-2010.** Depth 1 (the
specific failure mode asked about — a dimension silently unavailable) appears only in
three eras (2005-2006, 2007-2008, 2009-2010) and never again after 2010; even in
2009-2010, the era with the most backoff activity of any 2-year window in the whole
20-year set, it's 48 of 504 dates (9.5%), not a collapse — depth 3 still holds 81.5%
of that era. The elevated depth<3 share in 2009-2010 (18.5%, roughly 3x the next-
highest era) is real and makes sense on its own terms — the GFC-recovery period is
exactly when VIX/credit/trend regimes were genuinely transitioning fastest, so more
dates landing in a state that hadn't yet accumulated 8 independent historical matches
at full depth is the expected behavior of the 3→2→1→0 backoff, not a data outage.
Nothing here resembles a dimension going dark for a stretch.

### 11.2 `confidence_label` distribution and the depth × confidence cross-tab (ensemble voter) — restated as the direct answer to the depth-awareness question

`high`: 2,495 (47.2%) · `moderate`: 2,243 (42.5%) · `low`: 545 (10.3%).

| depth | high | moderate | low | n | high share |
|---|---|---|---|---|---|
| 1 | 52 | 37 | 16 | 105 | **49.5%** |
| 2 | 7 | 62 | 50 | 119 | 5.9% |
| 3 | 2,436 | 2,144 | 479 | 5,059 | **48.2%** |

**Depth 1 and depth 3 produce statistically indistinguishable confidence
distributions (49.5% vs. 48.2% high, at n=105 that's well within noise).** This is
the empirical proof requested: `compute_confidence()` — the function that sets
`recommendation_label`, the number the card actually leads with — carries no
detectable weight from how many regime dimensions matched. A user reading "high
confidence" learns nothing about whether that call was conditioned on a full
3-dimensional macro match or just one dimension (VIX alone); both produce "high"
about as often as each other. (Depth 2's own anomaly — 5.9% high, a real, bounded,
unexplained pattern on non-trivial n=119 — stands separately, per §9.3; not chased
down further here either.)

### 11.3 `p_positive` — full distribution, and the corrected comparison to live sharpness

**Correction to this section as first published:** the original version of this
section compared replay's single-ticker 5d std (0.0872) directly against
`MARKET_MEMORY_V2_BUILD.md` §1.3's pooled 5d figure (0.0946, cited there as "true
sharpness") and concluded the model doesn't get sharper across 20 years of regimes
than across 3 weeks live. **That comparison was apples-to-oranges and the conclusion
was backwards.** §1.3's 0.0946 is `stddev(p_positive)` pooled across **19 correlated
tickers** over **~3 weeks** (§1.3's own words: "Three weeks, a drawdown, 19
correlated tickers... the fixed ranking puts semis and mega-cap tech highest") — most
of that spread is the fixed cross-sectional ranking (some tickers' forecasts run
consistently higher than others', because the model itself weighs them differently,
not because any one ticker's own forecast moved), not time-series variation within a
single ticker. Replay's 0.0872 is pure time-series variation for **one ticker, 5,283
dates**. These were never measuring the same thing.

**Verified directly** (queried live, not re-derived from memory) — SPY's own 5d
`stddev(p_positive)` restricted to the same live outcome-scored population §1.3's
number came from: **0.0513** (n=18). Decomposing §1.3's full pooled sample
(19 tickers, n=320, current count — more outcomes have resolved since §1.3's original
n=300) into within-ticker and between-ticker variance confirms the mechanism
precisely: **57.6% of the pooled variance is cross-sectional (between-ticker),
42.4% is time-series (within-ticker)** — matching the "~55% cross-sectional"
estimate this correction was built on almost exactly.

**Corrected comparison: replay SPY (0.0872) vs. live SPY's own time-series std
(0.0513) — replay shows ~1.7x MORE time-series variation, not less.** The model
*does* respond more across 20 years of genuinely different regimes than it does
across a recent 3-week window. **Phase D should not carry forward "the model doesn't
get sharper when regime-conditioned" as a finding — that conclusion came from
comparing a time-series statistic to a pooled cross-sectional-plus-time-series one,
and doesn't survive the like-for-like comparison.** §11.2's depth-vs-confidence
finding (confidence carries no weight from `regime_match_depth`) is unaffected by
this correction — that comparison was already same-ticker, same-population,
depth-vs-depth, no cross-sectional contamination — but it should not be read as part
of a broader "the model is insensitive to regime" story; the sharpness evidence now
points the other way.

Per-horizon `p_positive`, queried from the persisted rows (n=5,283 each, exact) —
kept for reference, the raw numbers didn't change, only their live comparison did:

| Horizon | mean | std | min | max |
|---|---|---|---|---|
| 1d | 0.5265 | 0.0877 | 0.1765 | 0.9091 |
| 5d | 0.5694 | 0.0872 | 0.0741 | **1.0000** |
| 20d | 0.6248 | 0.0768 | 0.2692 | 0.9200 |
| 60d | 0.6601 | 0.0841 | 0.2308 | 0.9615 |

Basis-horizon-only (the figure actually shown on the card, per §9.4's caveat about
`pick_basis_horizon()`'s edge-maximizing selection bias): n=5,283, mean/median≈0.83,
std=0.1248, range 0.074–1.000.

**Logged, not fixed — no shrinkage floor on `p_positive`.** The 5d max of exactly
`1.0000` isn't a rounding artifact: queried the three dates directly —
**2020-03-18, 2020-03-19, 2020-03-23 (n=25-26 each)** — the COVID-crash bottom, to
the day. Every single matched analog/regime episode agreed on direction at those
three dates, and `p_positive = (rets > 0).mean()` is a raw empirical fraction with no
regularization, so unanimous agreement among a small-ish `n` produces exactly `1.0`
(or `0.0`) rather than something short of certainty. **Do not clamp this ahead of the
backfill.** The replay ledger's job is to measure what the raw model actually says;
recalibration (isotonic/Platt regression on the ledger, already Phase D's own plan)
is exactly the mechanism that turns "the model claimed 100%" into a properly
shrunk, calibrated probability — and it needs the raw, unclamped extremes to do that
correctly. Clamping before the backfill would destroy the very information
(how confident does the raw model get, and how often is it deserved) recalibration
is supposed to be fit against.

**Logged, not fixed — per-horizon means track SPY's own long-run base rates.**
1d/5d/20d/60d means (0.527/0.569/0.625/0.660) climb smoothly with horizon length,
consistent with SPY's realized historical drift compounding over longer windows, not
a horizon-specific quirk. Worth recording against §1.2's "7/29" read and the live
5d hit-rate figure (0.417, §1.3): **this pilot's own numbers suggest the live
window's apparent bullish tilt (mean forecast 0.551 vs. 0.417 realized) was that
specific 3-week drawdown period, not a structural bias baked into the model** — the
20-year replay's own 5d mean (0.569) sits close to the live period's forecast mean
(0.551), both well above what a short, correlated drawdown sample's hit rate would
suggest, and 20 years of data shows no sign of a runaway bullish drift beyond what
SPY's own history actually contains.

**Depth 2's anomaly (§11.2, §9.3): left alone**, per agreement — bounded, n=119, not
worth chasing before the backfill.

### 11.4 Unknown regime dates and block counts — restated for completeness

Zero dates across the full 20-year window returned `"unknown"` in any regime
dimension (§9.5's finding, unchanged). Block counts: 5,283 for every one of the 6
horizon/voter combinations (`forecast` at 1/5/20/60d, `dip_context` at 21/63d) —
31,698 rows total, confirmed three ways now (dry-run count, real-write count,
`forecasts_replay_block_counts` query against the live table).

---

## 12. Fix 5's poison-queue fix — dead-letter mechanism, and the 8 stale entries migrated

Found while verifying the live write path in §10.3: 8 staged `create_forecast`
writes, all from one stale `SMH` run, all failing identically (`HTTP 400
{"error":"missing fields","missing":["regime_model_version"]}`) because they were
staged before that field existed. `flush_pending_writes()`/`rewrite_pending_writes()`
had no concept of a write that will never succeed as-is — every future run would
retry, fail, and re-stage them, forever, and every future required-field addition
would only add more entries with no way out.

### 12.1 The fix

`scripts/forecast_engine.py`:

- Every staged entry now carries an `attempts` counter (starts at 0).
- `rewrite_pending_writes()` — the one place that already sees every attempt from
  both `persist_or_raise()` (a fresh write) and `flush_pending_writes()` (a retry of
  something already staged) — increments `attempts` for any write_id that was
  attempted and failed this run.
- On reaching `MAX_WRITE_ATTEMPTS = 5`, the entry is moved to
  `data/dead_letter_forecast_writes.jsonl` (append-only) with `last_error`,
  `last_attempted_at`, and `dead_lettered_at` recorded, and removed from the pending
  file — it stops being retried, stops being reported as a fresh failure on every
  future run.
- Logged loudly (`[DEAD-LETTER] ...`) — a dead-lettered entry is a forecast that
  will never be persisted without manual intervention, the same severity class B4/B5
  were built to make impossible to miss silently, not a routine retry outcome.
- `mm_journal()` now records the specific failure reason (`_last_mm_journal_error`,
  set right before every failing return) so the dead-letter record carries the real
  HTTP error, not just "it failed."
- `.github/workflows/forecast-engine.yml` and `manual-forecast.yml`'s existing
  "commit pending-write queue if changed" steps now also `git add
  data/dead_letter_forecast_writes.jsonl` — same ephemeral-runner reasoning as the
  pending file's own commit-back step; without this the dead-letter record (and the
  fact that data loss happened) would vanish with the container.

`MAX_WRITE_ATTEMPTS = 5`: deliberately not 1 (a genuinely transient outage — the
entire reason this mechanism exists — should get several real chances across
several separate runs, not fail out on the first retry) and not unbounded (an entry
that's failed 5 times across 5 separate invocations of this script is past the point
where "try again next run" is a credible theory).

### 12.2 Test coverage

`scripts/tests/test_dead_letter_queue.py`, isolated from both real queue files the
same way `test_fail_loud_persistence.py` is (tempdir + before/after content-hash
assertion on the real files). Verifies: an entry dead-letters on exactly the
`MAX_WRITE_ATTEMPTS`th failure, not before, with `write_id`/`attempts`/`last_error`/
payload all preserved; a write that recovers before exhausting its attempts leaves
no trace in either file; dead-lettering is append-only across independently
exhausted entries (one doesn't clobber another's record). All passing, full existing
suite (now 9 files) still green.

### 12.3 The 8 existing entries — migrated, not deleted

Moved directly to `data/dead_letter_forecast_writes.jsonl` (not left to exhaust
naturally over 5 more runs, per instruction) with `attempts = MAX_WRITE_ATTEMPTS`,
the real observed `last_error`, and a note that they were manually migrated ahead of
the normal counter reaching threshold. `data/pending_forecast_writes.jsonl` is now
empty (removed). Original payloads preserved byte-for-byte in the dead-letter
record — nothing about the 8 SMH forecasts themselves was deleted, only their
retry-eligibility.

---

## 13. C4 backfill run — 16-way tried, corrected to 2 workers, checkpointed and stopped

Not the GH Actions matrix itself — `gh workflow run` returns `403: Resource not
accessible by integration` even with the workflow pushed and visible on the remote
(`gh workflow list` confirms it exists); this token has no `actions:write`. Run
directly in this environment instead, against the same `forecasts_replay` table via
the same passphrase.

**First attempt (16 processes, one per remaining ticker, 2 CPU cores): killed.**
8x oversubscription measured directly, not assumed — after ~15-20 min wall-clock,
each ticker had covered only ~150-250 of its 5,000+ dates (`ps` confirmed all 16
alive and CPU-sharing, ~7-8% each). Per-ticker partial progress preserved in
`forecasts_replay` (nothing lost, no duplicate risk yet — `--resume` hadn't run).

**Also discovered and fixed while stopped:** the entire session had never been
pushed to `origin/main` (13 local-only commits). Pushing surfaced independent
automated commits on the remote, including one (`c6fd046`, `rotation-radar-bot`,
2026-08-05T20:39:39Z) that had already cleared the same 8 stale `SMH` queue entries
§12 dead-lettered — verified directly (not assumed) that this was legitimate: two
later successful `SMH` runs share the same `write_id` (keyed on ticker/trading_date/
horizon/model_version/voter, not `as_of_ts`) as the stale entries, so the existing
code correctly recognized them as superseded. No data was lost; §12's dead-letter
fix remains valid for future occurrences of this pattern, it just wasn't fixing an
*active* problem for these specific 8. Rebased cleanly (no real conflicts) and
pushed.

**`--resume` fixed before relying on it for real**: previously re-processed the
last-written date once (an accepted-at-the-time tradeoff) — since `forecasts_replay`
has no unique constraint, this would insert duplicate rows, not overwrite. Fixed to
start from `last_written + 1 day`, letting `_trading_dates()`'s own `>=` filter land
correctly on the next real trading day (verified directly against `MGK`'s partial
data: last written Friday 2009-09-04, correctly resumed at Tuesday 2009-09-08 —
skipping both the weekend and the Labor Day holiday, not just "the next calendar
day"). Also fixed `--resume --dry-run`, which previously skipped the resume lookup
entirely (wrongly gated on `not dry_run`, when the lookup is a read).

**Shared `as_of()` cache benefit: measured directly, found negligible.** Expected
this to meaningfully speed up later tickers in a multi-ticker run (SPY/QQQ/VIX/
BAA10Y already warm). Benchmarked XLK (first ticker in a fresh process, cold) against
XLF and XLV (run immediately after in the same process, shared series warm) over a
300-date sample: 413.8ms/date vs. 426.7ms/date vs. 436.2ms/date — statistically
indistinguishable, not faster. The disk-read savings only apply to a process's very
first call ever; steady-state cost is compute-dominated (`regime_series`/`analog_
positions`/`tech_read`/`dip_context`/`horizon_stats`), which caching doesn't touch.
**The real reason to run multiple tickers sequentially in one process is matching
process count to core count (2), not the cache** — `scripts/replay_backfill.py`'s
module docstring corrected to say this plainly.

**Restarted as 2 workers (matching the 2 cores), 8 tickers each, `--resume`d from
the killed run's partial progress:**

- Worker 1: `^SOX, GLD, IWM, QQQ, SMH, XLB, XLE, XLF`
- Worker 2: `MGK, RSP, XLI, XLK, XLP, XLU, XLV, XLY`

Both launched under `nohup ... & disown`, fully detached from the session, logging
to `worker1.log`/`worker2.log`.

**Projected wall-clock at the measured ~425ms/date, 2-way parallel: ~4.8 hours** —
over the ~3-hour threshold, used 2 workers anyway per instruction (the cap, not a
target to force under 3 hours).

**Stopped per instruction, mid-run, after each worker's then-current ticker
finished** — not left running unattended overnight. A watcher process per worker
(`stop_after_current.py`) tailed each log for its `"Done: TICKER --"` line (printed
only after `backfill()` has already returned, i.e. every batch for that ticker was
already flushed and committed — a safe kill point, nothing in-flight) and sent
`SIGKILL` to the worker's PID the instant it appeared, before the next ticker's
loop iteration could start. Verified against the live table, not just the log:

| Ticker | Rows landed | Complete? |
|---|---|---|
| `^SOX` | 31,194 (5,283/5,283 dates × 6 rows) | **Yes — full window** |
| `MGK` | 25,638 (4,273/4,273 scoreable dates × 6 rows) | **Yes — full scoreable window** (4,532 raw trading dates minus ~259 early-history dates below the 260-day warmup floor, matching expectation) |
| `GLD` | Unchanged at 223/76/34 (from the earlier killed 16-way run) | **Correctly untouched** — worker 1's watcher killed it before `GLD` (next in its list) did any real work; one harmless `"=== [2/8] GLD ..."` header line printed a fraction of a second before the kill landed, but zero rows were written for it |

Checkpoints written to each log listing completed vs. remaining tickers, per
instruction:

```
Worker 1 — completed: ['^SOX']. Remaining: ['GLD','IWM','QQQ','SMH','XLB','XLE','XLF']
Worker 2 — completed: ['MGK']. Remaining: ['RSP','XLI','XLK','XLP','XLU','XLV','XLY']
```

**Next step (not done here, per instruction the user will drive it):** re-run with
the same `--tickers` lists (or just the remaining sub-lists) and `--resume` to pick
up exactly where each worker stopped. 14 of 16 remaining tickers are still fully
untouched beyond whatever the original killed 16-way attempt gave them (~150-250
dates each) — this session covered 2 of 16 to completion.

## 14. C4 backfill resumed — 10 of 14 remaining tickers completed, stopped after in-flight ticker per instruction

Fresh session/environment (no state carried over from §13 — confirmed no
`worker*.log`, no running `replay_backfill.py` processes before this run
started; `APP_PASSPHRASE` had to be re-supplied interactively, same as any
other secret that doesn't persist across sessions).

**Status check before launching** (`latest_forecast_replay_date` per ticker,
both voters — `forecasts_replay_block_counts` was consulted too, but see the
failure noted below): confirmed the §13 checkpoint held for 12 of the 14
tickers (all still at `2005-08-31`, ~168/5,283 dates, matching the killed
16-way run's residue) — except **`GLD`** (already at `2006-10-17`, 452 dates)
and **`RSP`** (already at `2019-08-07`, 3,674 of 5,283 dates — no documented
explanation for this one; flagged to the user, not chased down further here).
Expected window confirmed identical for all 14 via `pit_store`: 5,283 trading
dates, 2005-01-03 → 2025-12-31 (all predate the 2005 start, so no
warmup-floor exclusions expected — matching SPY/`^SOX`).

**Same 2-worker split as §13** (kept — already close to balanced on
remaining-date workload despite `GLD`/`RSP`'s head start: ~35,521 vs. ~32,299
remaining dates, ~9% apart, better than any resplit by ticker count):

- Worker 1: `GLD, IWM, QQQ, SMH, XLB, XLE, XLF`
- Worker 2: `RSP, XLI, XLK, XLP, XLU, XLV, XLY`

Launched via `nohup ... & disown`, logging to `worker1.log`/`worker2.log`.
Progress tracked by polling `latest_forecast_replay_date` per ticker every 10
min (not log-tailing — `replay_backfill.py`'s prints aren't `flush=True`
except the `=== [i/n] TICKER ===` banner, so a log only reliably shows a
ticker's true completion once the *next* ticker's banner has already also
printed, which is too late to use as a stop signal).

**Stopped mid-run per instruction, same safe kill point as §13** (right after
the in-flight ticker's last write lands — batch already flushed/committed,
nothing in-flight) — but via DB polling this time, not log-tailing: polled
`latest_forecast_replay_date` for each worker's *current* ticker every 3s and
sent `SIGKILL` the instant it reached `2025-12-31`, before the next ticker's
loop iteration could start. Confirmed clean: worker 1 killed immediately
after `XLB` (never touched `XLE`), worker 2 killed immediately after `XLU`
(never touched `XLV`).

**Result — 10 of 14 done, verified against every `Done:` line in both full
logs (no `[warn]`/error lines anywhere in either):**

| Ticker | Dates scored | Rows written | Complete? |
|---|---|---|---|
| `GLD` | 4,831/4,831 (+452 pre-existing = 5,283/5,283) | 28,986 | **Yes** |
| `IWM` | 5,115/5,115 (+168 pre-existing = 5,283/5,283) | 30,690 | **Yes** |
| `QQQ` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `SMH` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `XLB` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `RSP` | 1,609/1,609 (+3,674 pre-existing = 5,283/5,283) | 9,654 | **Yes** |
| `XLI` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `XLK` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `XLP` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `XLU` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | **Yes** |
| `XLE` | untouched (killed before this ticker's loop started) | — | No — still at 2005-08-31, 168/5,283 |
| `XLF` | untouched | — | No — still at 2005-08-31, 168/5,283 |
| `XLV` | untouched | — | No — still at 2005-08-31, 168/5,283 |
| `XLY` | untouched | — | No — still at 2005-08-31, 168/5,283 |

No shortfalls — every completed ticker hit its full expected 5,283-date
window, and `dates_scored == dates_total` on every `Done:` line (rows always
exactly `dates × 6`, i.e. no dip-context gaps in the resumed range, unlike
the sparser early-history dip-context coverage baked into the pre-run global
counts).

**Bug found: `forecasts_replay_block_counts` is now broken** — reproducibly
(3/3 retries), returning `HTTP 500 {"error":"db_error","detail":""}`. Not
attempted here (production edge function, needs sign-off), but the likely
cause: its 12 sequential `COUNT(*)` queries (6 horizons × 2 voters, one
Postgres round-trip each) probably no longer finish inside whatever timeout
applies now that the table's grown well past the ~530,000 rows its own
comment cites as the safe case it was checked against — a single grouped SQL
query (the alternative the comment already considered "marginally cheaper")
would likely fix it and is worth doing before relying on this op again.
**Global counts below are computed from this run's per-ticker deltas, not
independently re-queried against the live table** — every `Done:` line
confirmed `rows_written == dates_scored × 6`, so the run's total of 47,360
newly-scored dates (across both workers) applies identically to all 6
buckets:

| Bucket | Pre-run (§13 baseline) | This run | New total |
|---|---|---|---|
| `forecast_1d` / `5d` / `20d` / `60d` | 20,752 | +47,360 | 68,112 each |
| `dip_context_21d` | 19,926 | +47,360 | 67,286 |
| `dip_context_63d` | 19,842 | +47,360 | 67,202 |

**Next step (not done here, per instruction — user resumes in the
morning):** re-run `--tickers XLE,XLF` (worker 1) and `--tickers XLV,XLY`
(worker 2) with `--resume`. All 4 are confirmed untouched beyond their
§13-era ~168/5,283 dates — no partial-batch risk. 13 of 17 total tickers
(`SPY`, `^SOX`, `MGK` + the 10 above) are now fully backfilled.

## 15. C4 backfill complete (17/17) — `forecasts_replay_block_counts` fixed

**Before launching**, confirmed §14's flagged RSP anomaly (undocumented
3,674-date head start) wasn't masking a gap: `worker2.log`'s `RSP` block
resumed cleanly at `2019-08-08` (the day after its `2019-08-07` last-written
date), checkpoint dates progressed monotonically (`2021-08-02` →
`2023-07-28` → `2025-07-28`, consistent with the trading calendar's ~250
dates/year pace), and it finished `1609/1609` dates scored with exactly
`9654` rows (`1609 × 6`, no shortfall) — a clean continuation, not a gappy
one. Pre-launch `latest_forecast_replay_date` check confirmed all 4
remaining tickers still sat at `2005-08-31` (§14's checkpoint), untouched.

**Same 2-worker split, same `--resume`, same `nohup ... & disown` pattern**
as §13-14:

- Worker 1: `XLE, XLF`
- Worker 2: `XLV, XLY`

**Result — all 4 completed clean, no `[warn]`/error lines in either log:**

| Ticker | Dates scored | Rows written | Elapsed |
|---|---|---|---|
| `XLE` | 5,115/5,115 (+168 pre-existing = 5,283/5,283) | 30,690 | 56.5 min |
| `XLF` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | 52.9 min |
| `XLV` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | 52.8 min |
| `XLY` | 5,115/5,115 (+168 = 5,283/5,283) | 30,690 | 53.7 min |

Verified against the live table (not just logs): `latest_forecast_replay_date`
for all 4 returns `2025-12-31`. **All 17 replay tickers are now fully
backfilled to the full 2005-2025 window.**

### 15.1 `forecasts_replay_block_counts` fix

§14 flagged this as broken (`HTTP 500 {"error":"db_error","detail":""}`,
3/3 retries) and guessed the cause: 12 sequential `count:"exact"` queries no
longer finishing inside the function's execution window against the grown
table. Fixed in two steps, the first of which turned out to be
insufficient — recorded here because it's a real lesson, not because it's
interesting:

1. **First attempt** (`20260807140000_forecasts_replay_block_counts_summary.sql`):
   added a singleton summary table
   (`forecasts_replay_block_counts_summary`, `id=1`, `counts jsonb`,
   `computed_at`) and a new op, `refresh_forecasts_replay_block_counts`,
   that ran the *same* 12 queries as before but on a schedule instead of at
   read time, upserting the result. **Verified directly this was NOT
   enough**: calling the refreshed op still returned the identical 500.
   Moving 12 round trips from request-time to schedule-time doesn't help
   if the 12 round trips themselves are what's blowing the window —
   relocating the cost isn't the same as reducing it.
2. **Actual fix** (`20260807143000_forecasts_replay_block_counts_agg_fn.sql`):
   a Postgres function, `forecasts_replay_block_counts_agg()`
   (`SECURITY DEFINER`, `service_role`-only `EXECUTE`), doing one
   `GROUP BY voter, horizon_days` — all 12 combinations in a single scan —
   plus a supporting composite index `(voter, horizon_days)` (the two
   pre-existing single-column indexes only supported the old per-combo
   query shape). `refresh_forecasts_replay_block_counts` now calls this via
   one `supabase.rpc(...)`, one round trip, instead of 12.
   `forecasts_replay_block_counts` itself is now a plain single-row
   `SELECT` from the summary table — cheap regardless of table size.

Both migrations applied via `npx supabase db push --linked`; `mm-journal`
redeployed twice (once per step) via `npx supabase functions deploy
mm-journal --project-ref anzbpxqvibgpxnwgyqoc`. Nightly refresh:
`.github/workflows/replay-block-counts-refresh.yml` (`scripts/
refresh_replay_block_counts.py`, 10:00 UTC daily — not gated to weekdays
since `forecasts_replay` isn't a daily-market-day table).

**Verified working, not just deployed**: after the real fix, the refresh
op completed in ~4s (was timing out before) and the read op returned the
same result 3/3 times (matching the rigor of the original 3/3-failure
check):

| Bucket | Pre-this-run (§14) | +this run (4 × 5,115) | New total | Live-verified |
|---|---|---|---|---|
| `forecast_1d` / `5d` / `20d` / `60d` | 68,112 | +20,460 | **88,572** each | ✓ |
| `dip_context_21d` | 67,286 | +20,460 | **87,746** | ✓ |
| `dip_context_63d` | 67,202 | +20,460 | **87,662** | ✓ |

Every value matches the arithmetic prediction from this run's own
`Done:` lines exactly — the fixed op's numbers are internally consistent,
not just non-erroring.
